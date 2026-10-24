"""Find every image, mask and table under a folder, fast, for a dataset.

The import dialog reads what somebody picked: one file, a few files, or ONE
folder level. A cohort is not one level. It is `registration/ + segmentation/ +
quantification/`, or `Sample01/ ... Sample40/`, or a mix of the two, and the
dataset builder (`dataset_plan`) needs every candidate file under it before it
can say which belong together. This module finds them.

**Two tiers, the cheap one first.**

- `walk` -- names and `stat` only. Breadth-first, nearest files first, every
  directory listed once. A run folder or a store (`*.zarr`, a Xenium run, a
  SpatialData store, a DICOM slide) is recognised from the names inside it and
  kept as ONE entry, never descended into: its chunks are thousands of
  meaningless names. Each file gets a provisional role from its name and folder
  so the table can be drawn before a single header has been read.
- `inspect` -- the import engine's own per-file detection
  (`import_proposal._detect_file`), which reads headers and, for a single-plane
  raster, a few small windows of pixels. In the background, rasters first
  (they anchor projects), each answer cached by `(realpath, size, mtime)` so a
  re-plan, an added folder or a second scan of the same tree never reads a file
  twice. Past `SCAN_INSPECT_BUDGET_S` the rest keep their provisional role and
  are marked uninspected, which caps what the planner may say about them.

What is skipped and why is recorded, not dropped: hidden files and folders,
temporary files, Plexora's own pyramid copies, linked folders (never followed:
a link back up the tree would walk forever), unreadable folders, and the
intermediates a pipeline leaves beside its outputs.

A scan runs as a `layer_jobs` task under the pseudo-sample `dataset-scan`, so
it reports progress in the vocabulary every other background job uses, can be
cancelled between directories or files, and its thread is named `layer-*`
(the test suite joins those at teardown).
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field, fields
from pathlib import Path

#: Folder levels below a root the walk descends. A cohort is two or three deep
#: (`Sample01/registered/image.ome.tiff`); eight covers a pipeline's own
#: nesting without letting a pick of a home directory reach the bottom of it.
SCAN_MAX_DEPTH = 8

#: Directory entries the walk looks at before it stops and says so.
SCAN_MAX_ENTRIES = 200_000

#: Seconds of header reading before the rest are classified from their names.
SCAN_INSPECT_BUDGET_S = 120

#: Inspected files remembered across scans, by `(realpath, size, mtime_ns)`.
INSPECT_CACHE_SIZE = 4096

#: Scans kept in memory at once; the oldest finished one goes first.
MAX_SCANS = 8

#: Folders never entered, whatever is in them.
SKIP_DIR_NAMES = frozenset({"__MACOSX", ".git", "node_modules", "__pycache__",
                            "$RECYCLE.BIN", "System Volume Information"})

#: Folders a pipeline writes beside its outputs that hold images which are not
#: samples: mcmicro's probability maps, illumination profiles and QC.
INTERMEDIATE_DIR_NAMES = frozenset({"probability-maps", "probability_maps",
                                    "probabilities", "illumination", "qc"})

#: Folders of raw cycles, skipped when a registered sibling holds the result.
RAW_DIR_NAMES = frozenset({"raw"})
REGISTERED_DIR_NAMES = frozenset({"registration", "registered", "stitched"})

TEMP_NAME_RE = re.compile(
    r"(^~\$|^\.~|\.(tmp|temp|part|crdownload|download|swp|lock)$|~$|"
    r"^thumbs\.db$|^desktop\.ini$)", re.IGNORECASE)

#: Plain-text names that are documentation, never a table.
NOT_A_TABLE = re.compile(r"^(readme|license|licence|notes?|changelog|citation|"
                         r"manifest|requirements)\b", re.IGNORECASE)

#: A `.txt` larger than this is not looked at as a table.
TXT_TABLE_LIMIT = 512 * 1024 ** 2

#: Directory names whose own name makes them one unit.
STORE_SUFFIXES = (".zarr", ".n5")

SCAN_STAGES = {
    "walking": (0, 30, "Reading folders"),
    "inspecting": (30, 92, "Reading file headers"),
    "planning": (92, 99, "Matching files"),
}

#: The pseudo-sample every scan task is filed under in `layer_jobs`.
TASK_OWNER = "dataset-scan"

#: Rasters in one folder before it is asked whether they are one image split
#: into a file per channel.
CHANNEL_FOLDER_MIN = 6

#: How many of a suspected channel folder's files are opened to decide it.
CHANNEL_FOLDER_PROBES = 3

#: Name fragments that make an uninspected raster a provisional mask.
_MASK_WORDS = ("mask", "label", "seg", "cellpose", "mesmer", "unmicst",
               "stardist", "deepcell", "nuclei", "nucleus", "nuclear",
               "cell", "cyto", "membrane")


@dataclass
class Entry:
    """One candidate file, or one store or run that is a unit."""

    path: str
    real: str
    root: str
    rel: str
    name: str
    size: int = 0
    mtime_ns: int = 0
    #: raster | table | parquet | shapes | bundle
    kind: str = "raster"
    #: The store or run type for a directory entry (`zarr`, `xenium`, ...).
    format: str | None = None
    symlink: bool = False
    inspected: bool = False
    #: image | mask | table | channels | layer | None (nothing Plexora reads)
    role: str | None = None
    modality: str | None = None
    geometry: dict | None = None
    pixel_size: float | None = None
    #: Pixels consistent with both an image and a mask.
    ambiguous: bool = False
    #: `{column, values}` for an AnnData spanning several images.
    subset: dict | None = None
    columns: tuple = ()
    label: str = ""
    detail: str = ""
    #: The image's own name from its OME metadata, when it gives one.
    ome_name: str | None = None
    #: For a run or store: what it holds, `[{role, kind, label}]`.
    layers: tuple = ()
    warnings: tuple = ()
    #: Why inspection found nothing to read, for `unrecognised`.
    reason: str | None = None
    #: Classified from its name because the inspection budget ran out.
    budgeted: bool = False
    #: The folder this file is one channel of, when its folder holds one
    #: single-channel image per marker (`CD3.tif`, `Ki67.tif`, ...).
    channel_folder: str | None = None

    @property
    def key(self):
        return (self.real, self.size, self.mtime_ns)

    def size_wh(self):
        width = (self.geometry or {}).get("width")
        height = (self.geometry or {}).get("height")
        try:
            return (int(width), int(height)) if width and height else None
        except (TypeError, ValueError):
            return None

    def channel_count(self):
        try:
            return int((self.geometry or {}).get("numChannels") or 0)
        except (TypeError, ValueError):
            return 0


#: The fields inspection fills, which is what the cache remembers.
_INSPECTED_FIELDS = ("inspected", "role", "modality", "geometry", "pixel_size",
                     "ambiguous", "subset", "columns", "label", "detail",
                     "ome_name", "layers", "warnings", "reason", "kind",
                     "channel_folder")


@dataclass
class Inventory:
    """Everything one scan has found so far."""

    roots: list = field(default_factory=list)
    entries: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    counts: dict = field(default_factory=lambda: {
        "dirs": 0, "files": 0, "candidates": 0, "inspected": 0})
    budget_hit: dict | None = None
    #: Bumped on every change, so a plan can be cached against it.
    version: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def add(self, entry):
        with self.lock:
            if entry.path in self.entries:
                return
            self.entries[entry.path] = entry
            self.counts["candidates"] = len(self.entries)
            self.version += 1

    def changed(self):
        with self.lock:
            self.version += 1

    def ordered(self):
        """Entries in a stable order: by root as picked, then path."""
        order = {root: index for index, root in enumerate(self.roots)}
        with self.lock:
            entries = list(self.entries.values())
        return sorted(entries, key=lambda e: (order.get(e.root, 1 << 30),
                                              e.rel.lower(), e.rel))


# -- the walk ------------------------------------------------------------------


def _limits(limits):
    limits = dict(limits or {})
    return (int(limits.get("max_depth") or SCAN_MAX_DEPTH),
            int(limits.get("max_entries") or SCAN_MAX_ENTRIES))


def _is_temp(name) -> bool:
    return name.startswith(".") or bool(TEMP_NAME_RE.search(name))


def _derived_copy(name, siblings) -> str | None:
    """Which sibling `name` is Plexora's own pyramid of, or None.

    Names only. A label or outline pyramid is always derived; a plain
    `.pyramid.ome.tiff` is skipped only when the original is beside it, since
    on its own it is the only copy there is.
    """
    from plexora.server.utils import image_pyramid, segmentation_pyramid

    lowered = name.lower()
    for suffix in (segmentation_pyramid.FILLED_SUFFIX,
                   segmentation_pyramid.OUTLINE_SUFFIX):
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    if lowered.endswith(image_pyramid.PYRAMID_SUFFIX):
        stem = lowered[: -len(image_pyramid.PYRAMID_SUFFIX)]
        for sibling in siblings:
            other = sibling.lower()
            if other != lowered and other.startswith(stem + ".") and \
                    not other.endswith(image_pyramid.PYRAMID_SUFFIX):
                return sibling
    return None


def _kind_of_file(name, size):
    """What a file is by its name alone, or None for nothing to look at."""
    from plexora.server.models.import_proposal import IMAGE_SUFFIXES

    lowered = name.lower()
    if lowered.endswith(IMAGE_SUFFIXES) or lowered.endswith((".btf", ".ome.btf")):
        return "raster"
    if lowered.endswith(".parquet"):
        return "parquet"
    if lowered.endswith((".csv", ".tsv", ".h5ad")):
        return "table"
    if lowered.endswith(".txt"):
        if NOT_A_TABLE.match(name) or size > TXT_TABLE_LIMIT:
            return None
        return "table"
    if lowered.endswith(".geojson"):
        return "shapes"
    return None


def _guess_role(entry):
    """A role from names alone, for a file not inspected yet."""
    if entry.kind == "raster":
        words = re.findall(r"[a-z]+", entry.rel.lower())
        if any(word.startswith(_MASK_WORDS) for word in words):
            return "mask"
        return "image"
    if entry.kind == "table":
        return "channels" if entry.name.lower() == "markers.csv" else "table"
    if entry.kind in ("parquet", "shapes"):
        return "layer"
    if entry.kind == "bundle":
        return "image"
    return None


def _store_type(path, name, child_names):
    """`(type, label)` for a directory that is one unit, else None.

    The cheap names test first (`dataset_marks.store_by_names`), then the
    import engine's own detector for the type it suggested, in the engine's
    order, so a folder kept whole here is what Import Sample would make of it.
    """
    from plexora.server.utils import dataset_marks, dicom_wsi, ome_zarr, spatial_scene

    mark = dataset_marks.store_by_names(name, child_names)
    if mark is None:
        if name.lower().endswith(STORE_SUFFIXES):
            return ("n5" if name.lower().endswith(".n5") else "zarr"), "Store"
        return None
    kind = mark["type"]
    confirm = {
        "visium_hd": spatial_scene.is_visium_hd_run,
        "xenium": spatial_scene.is_xenium_run,
        "visium": spatial_scene.is_visium_run,
        "spatialdata": spatial_scene.is_spatialdata_store,
        "dicom": dicom_wsi.is_dicom_path,
        "zarr": ome_zarr.is_zarr_image_path,
    }.get(kind)
    try:
        confirmed = bool(confirm and confirm(path))
    except Exception:  # noqa: BLE001 -- an unreadable store is not one
        confirmed = False
    if not confirmed:
        if name.lower().endswith(STORE_SUFFIXES):
            return "zarr", "Store"
        return None
    return kind, mark.get("label") or kind


def _entitled(kind) -> bool:
    from plexora.server.models import import_proposal
    from plexora.server.utils import dataset_marks

    entitlement = dataset_marks.ENTITLEMENTS.get(kind)
    return entitlement is None or import_proposal._licensed(entitlement)


def _new_entry(path, root, *, kind, size=0, mtime_ns=0, symlink=False, fmt=None):
    real = os.path.realpath(path)
    rel = Path(os.path.relpath(path, root)).as_posix() if path != root else Path(path).name
    entry = Entry(path=path, real=real, root=root, rel=rel, name=Path(path).name,
                  size=size, mtime_ns=mtime_ns, kind=kind, format=fmt,
                  symlink=symlink, label=Path(path).name)
    entry.role = _guess_role(entry)
    return entry


def walk(roots, inventory, *, cancel=None, progress=None, limits=None):
    """List every candidate under `roots` into `inventory`. Stat only.

    @param progress - `progress(done, total, message)`, called per directory.
    @returns The inventory, which also carries what was skipped and why.
    """
    max_depth, max_entries = _limits(limits)
    queue = deque()
    for root in roots:
        root = str(Path(root).expanduser())
        if root not in inventory.roots:
            inventory.roots.append(root)
        queue.append((root, 0, root))
    seen_dirs = set()
    done = 0
    while queue:
        if cancel is not None and cancel.is_set():
            return inventory
        directory, depth, root = queue.popleft()
        real_dir = os.path.realpath(directory)
        if real_dir in seen_dirs:
            continue
        seen_dirs.add(real_dir)
        try:
            with os.scandir(directory) as listing:
                children = sorted(listing, key=lambda e: e.name)
        except PermissionError:
            inventory.errors.append({"path": directory,
                                     "error": "Permission denied"})
            continue
        except OSError as error:
            inventory.errors.append({"path": directory,
                                     "error": error.strerror or str(error)})
            continue
        done += 1
        with inventory.lock:
            inventory.counts["dirs"] += 1
        names = [child.name for child in children]

        store = _store_type(directory, Path(directory).name, names)
        if store is not None:
            _add_store(inventory, directory, root, store)
            if progress:
                progress(done, done + len(queue), directory)
            continue

        child_dirs = {child.name.lower() for child in children
                      if _is_dir(child)}
        has_registered = bool(child_dirs & REGISTERED_DIR_NAMES)
        for child in children:
            name = child.name
            seen = inventory.counts["dirs"] + inventory.counts["files"]
            if seen >= max_entries:
                if inventory.budget_hit is None:
                    inventory.budget_hit = {"reason": "entries", "at": directory,
                                            "limit": max_entries}
                queue.clear()
                break
            if _is_dir(child):
                if name.startswith(".") or name in SKIP_DIR_NAMES:
                    continue
                if _is_link(child):
                    inventory.skipped.append({
                        "path": child.path,
                        "reason": "A link to another folder; links are not followed."})
                    continue
                if name.lower() in INTERMEDIATE_DIR_NAMES:
                    inventory.skipped.append({
                        "path": child.path,
                        "reason": "Pipeline intermediates (probability maps, "
                                  "illumination, QC), not samples."})
                    continue
                if name.lower() in RAW_DIR_NAMES and has_registered:
                    inventory.skipped.append({
                        "path": child.path,
                        "reason": "Raw cycles; the registered images sit beside them."})
                    continue
                if name.lower().endswith(STORE_SUFFIXES):
                    # A store by its name: one unit whatever is inside, and
                    # never walked, so its chunks never count against a budget.
                    queue.append((child.path, depth + 1, root))
                    continue
                if depth + 1 > max_depth:
                    if inventory.budget_hit is None:
                        inventory.budget_hit = {"reason": "depth", "at": child.path,
                                                "limit": max_depth}
                    continue
                queue.append((child.path, depth + 1, root))
                continue
            with inventory.lock:
                inventory.counts["files"] += 1
            if _is_temp(name):
                continue
            try:
                stat = child.stat()  # follows a link to a file
            except OSError:
                continue
            kind = _kind_of_file(name, stat.st_size)
            if kind is None or _side_file(child.path):
                continue
            source = _derived_copy(name, names)
            if source is not None:
                inventory.skipped.append({
                    "path": child.path,
                    "reason": f"Plexora's own pyramid of {source}."})
                continue
            inventory.add(_new_entry(child.path, root, kind=kind,
                                     size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                                     symlink=_is_link(child)))
        if progress:
            progress(done, done + len(queue), directory)
    return inventory


def _side_file(path) -> bool:
    """A file a 10x run writes beside itself (`metrics_summary.csv`): part of
    the run, never a table of its own."""
    from plexora.server.utils import spatial_scene

    try:
        return spatial_scene.is_spaceranger_side_file(Path(path))
    except Exception:  # noqa: BLE001
        return False


def _is_dir(entry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=True)
    except OSError:
        return False


def _is_link(entry) -> bool:
    from plexora.server.utils.dir_listing import _is_link as is_link

    return is_link(entry)


def _add_store(inventory, directory, root, store):
    kind, _label = store
    try:
        stat = os.stat(directory)
        mtime = stat.st_mtime_ns
    except OSError:
        mtime = 0
    if kind == "zarr":
        entry = _new_entry(directory, root, kind="raster", fmt="zarr",
                           mtime_ns=mtime)
    elif kind == "n5":
        entry = _new_entry(directory, root, kind="raster", fmt="n5", mtime_ns=mtime)
        entry.role, entry.inspected = None, True
        entry.reason = "An N5 store; Plexora reads OME-Zarr, not N5."
    else:
        entry = _new_entry(directory, root, kind="bundle", fmt=kind, mtime_ns=mtime)
        if not _entitled(kind):
            from plexora.server.utils import dataset_marks

            entry.role, entry.inspected = None, True
            entry.reason = (f"A {dataset_marks.TYPE_LABELS.get(kind, kind)}; this "
                            "licence does not include spatial transcriptomics.")
    inventory.add(entry)


# -- inspection ----------------------------------------------------------------

_CACHE: OrderedDict = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _cached(entry):
    with _CACHE_LOCK:
        found = _CACHE.get(entry.key)
        if found is not None:
            _CACHE.move_to_end(entry.key)
        return found


def _remember(entry):
    record = {name: getattr(entry, name) for name in _INSPECTED_FIELDS}
    with _CACHE_LOCK:
        _CACHE[entry.key] = record
        _CACHE.move_to_end(entry.key)
        while len(_CACHE) > INSPECT_CACHE_SIZE:
            _CACHE.popitem(last=False)


def _apply(entry, record):
    for name, value in record.items():
        setattr(entry, name, value)


_INSPECT_ORDER = {"raster": 0, "bundle": 1, "parquet": 2, "table": 3, "shapes": 4}


def _channel_folder_suspects(entries):
    """`{folder: [entries]}` for folders whose rasters look like one image
    split into a file per marker, by their names alone.

    The names are what separate the two shapes a folder of many rasters has.
    One sample per file -- `A1.tif ... A12.tif`, `Patient1_001 ...`,
    `S1.tif + S1_mask.tif ...` -- is one or two name SHAPES with the numbers
    changing (`A#`, `Patient#_#`). One channel per file -- `CD3.tif`,
    `Ki67.tif`, `DAPI.tif`, `Vimentin.tif` -- is a different shape nearly every
    time, even in a panel that is half `CD#`. So: at least three shapes, and
    at least a quarter as many shapes as files.
    """
    by_folder = {}
    for entry in entries:
        if entry.kind == "raster" and entry.format is None and not entry.inspected:
            by_folder.setdefault(os.path.dirname(entry.path), []).append(entry)
    found = {}
    for folder, members in by_folder.items():
        if len(members) < CHANNEL_FOLDER_MIN:
            continue
        shapes = {re.sub(r"[0-9]+", "#", _name_stem(e.name).lower()) for e in members}
        if len(shapes) >= 3 and len(shapes) * 4 >= len(members):
            found[folder] = members
    return found


def _name_stem(name):
    from plexora.server.models.import_proposal import _file_stem

    return _file_stem(name)


def _settle_channel_folders(inventory):
    """Open a few files of each suspected channel folder; when they are single
    planes of one size, mark the whole folder and read none of the rest."""
    from plexora.server.models import import_proposal

    with inventory.lock:
        entries = list(inventory.entries.values())
    for folder, members in _channel_folder_suspects(entries).items():
        sizes = set()
        single = True
        for entry in sorted(members, key=lambda e: e.name)[:CHANNEL_FOLDER_PROBES]:
            geometry = import_proposal._geometry_of(entry.path) or {}
            sizes.add((geometry.get("width"), geometry.get("height")))
            single = single and int(geometry.get("numChannels") or 0) == 1
        if not single or len(sizes) != 1 or None in next(iter(sizes)):
            continue
        names = sorted(Path(e.name).stem for e in members)
        listed = ", ".join(names[:4]) + (", …" if len(names) > 4 else "")
        reason = (f"{len(members)} single-channel images, one per marker ({listed}). "
                  "Plexora reads multichannel images: combine them into one "
                  "OME-TIFF first.")
        with inventory.lock:
            for entry in members:
                entry.inspected = True
                entry.role = None
                entry.channel_folder = folder
                entry.reason = reason


def inspect(inventory, *, cancel=None, progress=None, budget_s=None):
    """Read each uninspected entry's header, rasters first. Cached.

    @param progress - `progress(done, total, message)`, called per file.
    """
    budget = SCAN_INSPECT_BUDGET_S if budget_s is None else budget_s
    started = time.monotonic()
    _settle_channel_folders(inventory)
    pending = [entry for entry in inventory.ordered() if not entry.inspected]
    pending.sort(key=lambda e: _INSPECT_ORDER.get(e.kind, 9))
    total = len(pending)
    for index, entry in enumerate(pending):
        if cancel is not None and cancel.is_set():
            return inventory
        if progress:
            progress(index, total, f"Reading {entry.name} ({index + 1} of {total})")
        record = _cached(entry)
        if record is not None:
            with inventory.lock:
                _apply(entry, record)
        elif time.monotonic() - started > budget:
            with inventory.lock:
                _classify_by_name(entry)
        else:
            try:
                found = _inspect_one(entry)
            except Exception as error:  # noqa: BLE001 -- one bad file is one row
                found = {"inspected": True, "role": None,
                         "reason": f"{entry.name}: {error}"}
            with inventory.lock:
                _apply(entry, found)
            if found.get("inspected"):
                _remember(entry)
        with inventory.lock:
            inventory.counts["inspected"] = sum(
                1 for e in inventory.entries.values() if e.inspected)
        inventory.changed()
    if progress:
        progress(total, total, "Done")
    return inventory


def _classify_by_name(entry):
    """After the budget: a role from the name and, for a raster, its header."""
    from plexora.server.models import import_proposal

    if entry.kind == "raster" and entry.format is None:
        entry.geometry = import_proposal._geometry_of(entry.path)
    entry.role = _guess_role(entry)
    entry.ambiguous = entry.kind == "raster"
    entry.inspected = False
    entry.budgeted = True


def _first(layers, *roles):
    for role in roles:
        for layer in layers:
            if layer.role == role:
                return layer
    return layers[0] if layers else None


def _inspect_one(entry):
    """The fields inspection fills for one entry, as a dict."""
    from plexora.server.models import import_proposal as engine

    path = Path(entry.path)
    if entry.kind == "bundle":
        return _inspect_bundle(entry)
    if entry.kind == "raster":
        if entry.format == "zarr":
            layers, questions, _, warnings = engine._detect_directory(path, {})
        else:
            layers, questions, _, warnings = engine._detect_file(path, {})
        layer = _first(layers, "image", "mask")
        if layer is None:
            return {"inspected": True, "role": None,
                    "reason": (warnings[0] if warnings
                               else f"{entry.name} is not an image Plexora reads."),
                    "warnings": tuple(warnings)}
        if layer.dependency:
            install = layer.dependency.get("install") or "an extra package"
            return {"inspected": True, "role": None,
                    "reason": f"{entry.name} needs {install} to be read."}
        if layer.geometry is None and entry.format is None:
            return {"inspected": True, "role": None,
                    "reason": f"{entry.name} could not be read as an image."}
        ambiguous = any(str(need).startswith("mask-or-image:")
                        for need in layer.needs)
        role = layer.role if layer.role in ("image", "mask") else "layer"
        if ambiguous and role == "image" and _guess_role(entry) == "mask":
            # Pixels consistent with both, and the name and folder say mask.
            role = "mask"
        ome_name = None
        if role == "image" and entry.name.lower().endswith((".tif", ".tiff")):
            ome_name = engine._ome_image_name(path)
        return {"inspected": True, "role": role, "modality": layer.modality,
                "geometry": dict(layer.geometry) if layer.geometry else None,
                "pixel_size": layer.pixel_size, "ambiguous": ambiguous,
                "label": entry.name, "detail": layer.detail or "",
                "ome_name": ome_name, "warnings": tuple(warnings)}
    if entry.kind == "parquet":
        layers, _, _, warnings = engine._detect_parquet(path)
        layer = _first(layers, "table")
        if layer is None:
            return {"inspected": True, "role": None,
                    "reason": warnings[0] if warnings else
                    f"{entry.name} is not a table Plexora reads."}
        return {"inspected": True,
                "role": "table" if layer.role == "table" else "layer",
                "modality": layer.modality, "detail": layer.detail or "",
                "label": entry.name, "columns": _parquet_columns(path)}
    if entry.kind == "shapes":
        if engine._is_feature_collection(path):
            return {"inspected": True, "role": "layer", "modality": "annotations",
                    "detail": "Shapes", "label": entry.name}
        return {"inspected": True, "role": None,
                "reason": f"{entry.name} is not a GeoJSON feature collection."}
    if entry.kind == "table":
        return _inspect_table(entry, path)
    return {"inspected": True, "role": None}


def _inspect_table(entry, path):
    """A delimited file or an AnnData, by its header only.

    Not `_detect_table`: that counts a CSV's rows, which reads the whole file.
    """
    from plexora.server.models import import_proposal as engine
    from plexora.server.models.adapters import detect_data_type

    if engine._marker_sheet(path) is not None:
        return {"inspected": True, "role": "channels", "label": entry.name,
                "detail": "Channel names"}
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv", ".txt") and not engine._looks_like_a_table(path):
        return {"inspected": True, "role": None,
                "reason": f"{entry.name} has no delimited header."}
    try:
        data_type = detect_data_type(path)
    except ValueError as error:
        return {"inspected": True, "role": None, "reason": str(error)}
    found = {"inspected": True, "role": "table", "modality": "cells",
             "label": entry.name, "detail": data_type}
    if suffix in (".csv", ".tsv", ".txt"):
        found["columns"] = _header_columns(path)
    if data_type == "anndata":
        found["subset"] = engine._image_column(path)
    return found


def _header_columns(path):
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as handle:
            header = handle.readline().strip()
    except OSError:
        return ()
    delimiter = next((d for d in ("\t", ",", ";") if d in header), ",")
    return tuple(cell.strip().strip('"') for cell in header.split(delimiter))


def _parquet_columns(path):
    try:
        import pyarrow.parquet as pq

        return tuple(pq.read_schema(str(path)).names)
    except Exception:  # noqa: BLE001
        return ()


def _inspect_bundle(entry):
    """A run or store, read by the import engine as Import Sample reads it."""
    from plexora.server.models import import_proposal as engine

    proposal = engine.inspect_paths([entry.path])
    if not proposal.samples:
        reasons = [item.get("reason") for item in proposal.unrecognised]
        return {"inspected": True, "role": None,
                "reason": next((r for r in reasons if r), None)
                or f"{entry.name}: nothing Plexora reads."}
    sample = proposal.samples[0]
    layers = tuple({"role": layer.role, "kind": layer.kind,
                    "label": layer.label or layer.kind}
                   for layer in sample.layers)
    reference = next((layer for layer in sample.layers if layer.reference),
                     None) or (sample.layers[0] if sample.layers else None)
    return {"inspected": True, "role": "image",
            "modality": reference.modality if reference else None,
            "geometry": dict(reference.geometry) if reference and reference.geometry else None,
            "pixel_size": reference.pixel_size if reference else None,
            "label": sample.name, "layers": layers,
            "detail": f"{_bundle_label(entry.format)} · {len(layers)} layers",
            "warnings": tuple(proposal.warnings or ())}


def _bundle_label(kind):
    from plexora.server.utils import dataset_marks

    return dataset_marks.TYPE_LABELS.get(kind, kind or "Run")


def scan(roots, *, cancel=None, progress=None, limits=None, budget_s=None):
    """Walk and inspect `roots` synchronously. The API's and the CLI's scan."""
    inventory = Inventory()
    walk(roots, inventory, cancel=cancel, progress=progress, limits=limits)
    inspect(inventory, cancel=cancel, progress=progress, budget_s=budget_s)
    return inventory


# -- scans as background tasks --------------------------------------------------


@dataclass
class Scan:
    token: str
    inventory: Inventory
    limits: dict | None = None
    overrides: dict = field(default_factory=dict)
    seq: int = 0
    started: float = field(default_factory=time.monotonic)
    cancelled: bool = False
    plan_cache: tuple | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


_SCANS: OrderedDict = OrderedDict()
_SCANS_LOCK = threading.Lock()


def _task_id(token, seq):
    return f"{token}/{seq}"


def _evict():
    from plexora.server.models import layer_jobs

    with _SCANS_LOCK:
        while len(_SCANS) > MAX_SCANS:
            for token in list(_SCANS):
                if not layer_jobs.running_tasks(prefix=token + "/"):
                    _SCANS.pop(token)
                    break
            else:
                return


def _check_roots(paths):
    roots = []
    for raw in paths or ():
        path = Path(str(raw)).expanduser()
        if not path.is_dir():
            raise ValueError(f"{raw} is not a folder.")
        roots.append(str(path.resolve()))
    if not roots:
        raise ValueError("Choose a folder to scan.")
    return roots


def start_scan(paths, *, token=None, limits=None) -> dict:
    """Start scanning `paths` in the background; the first snapshot.

    With `token`, adds `paths` to that scan instead (`extend_scan`).
    """
    if token:
        return extend_scan(token, paths)
    roots = _check_roots(paths)
    token = "scan-" + uuid.uuid4().hex[:8]
    from plexora.server.models import dataset_plan

    record = Scan(token=token, inventory=Inventory(), limits=limits,
                  overrides=dataset_plan.clean_overrides({}))
    with _SCANS_LOCK:
        _SCANS[token] = record
    _evict()
    _run(record, roots)
    return snapshot(token)


def extend_scan(token, paths) -> dict:
    """Add folders to a scan: only the new folders are walked."""
    record = _get(token)
    roots = [root for root in _check_roots(paths)
             if root not in record.inventory.roots]
    if roots:
        _run(record, roots)
    return snapshot(token)


def _run(record, roots):
    from plexora.server.models import layer_jobs

    with record.lock:
        record.seq += 1
        record.cancelled = False
        task_id = _task_id(record.token, record.seq)
    inventory = record.inventory

    def work(stage, report, cancel):
        stage("walking")
        walk(roots, inventory, cancel=cancel, limits=record.limits,
             progress=lambda done, total, _message: report(done, total))
        if cancel.is_set():
            return
        stage("inspecting")
        inspect(inventory, cancel=cancel,
                progress=lambda done, total, _message: report(done, total))
        if cancel.is_set():
            return
        stage("planning")
        plan_for(record.token)

    layer_jobs.start_task(TASK_OWNER, task_id, work, stages=SCAN_STAGES)


def _get(token) -> Scan:
    with _SCANS_LOCK:
        record = _SCANS.get(token)
    if record is None:
        raise KeyError(token)
    return record


def get_scan(token):
    """The `Scan`, or None."""
    with _SCANS_LOCK:
        return _SCANS.get(token)


def cancel_scan(token) -> bool:
    """Stop a scan; what it found so far stays and can still be planned."""
    from plexora.server.models import layer_jobs

    record = _get(token)
    record.cancelled = True
    stopped = True
    for owner, task_id in layer_jobs.running_tasks(prefix=token + "/"):
        if owner == TASK_OWNER:
            stopped = layer_jobs.stop_task(owner, task_id) and stopped
    return stopped


def set_overrides(token, overrides) -> None:
    from plexora.server.models import dataset_plan

    record = _get(token)
    clean = dataset_plan.clean_overrides(overrides)
    with record.lock:
        record.overrides = clean


def plan_for(token, *, config=None):
    """The scan's plan under its overrides, recomputed only when either moved."""
    import json

    from plexora.server.models import dataset_plan

    record = _get(token)
    with record.lock:
        overrides = record.overrides
        signature = (record.inventory.version,
                     json.dumps(overrides, sort_keys=True))
        if config is None and record.plan_cache and record.plan_cache[0] == signature:
            return record.plan_cache[1]
    with record.inventory.lock:
        result = dataset_plan.plan(record.inventory, overrides, config=config)
    with record.lock:
        if config is None:
            record.plan_cache = (signature, result)
    return result


def status(token) -> dict:
    """`{status, pending, progress}` for a scan, across its tasks."""
    from plexora.server.models import layer_jobs

    record = _get(token)
    records = layer_jobs.tasks(TASK_OWNER, prefix=token + "/")
    latest = records.get(_task_id(token, record.seq)) or {}
    pending = any(r.get("status") == "pending" for r in records.values())
    if pending:
        state = "pending"
    elif record.cancelled or latest.get("status") == "cancelled":
        state = "cancelled"
    elif latest.get("status") == "failed":
        state = "failed"
    else:
        state = "ready"
    counts = dict(record.inventory.counts)
    stage = latest.get("stage")
    message = latest.get("message")
    if stage == "walking":
        message = f"{counts['dirs']} folders · {counts['files']} files"
    elif stage == "inspecting":
        message = f"Read {counts['inspected']} of {counts['candidates']} files"
    return {"status": state, "pending": pending,
            "error": latest.get("error"),
            "progress": {"percent": 100 if state == "ready" else latest.get("progress", 0),
                         "stage": stage, "stage_label": latest.get("stage_label"),
                         "message": message, **counts}}


def snapshot(token, *, verified=False) -> dict:
    """What the dialog draws: the scan's progress and its current plan."""
    from plexora.server.models import dataset_plan

    record = _get(token)
    state = status(token)
    result = plan_for(token)
    if verified:
        result = dataset_plan.verify(result)
    return {"token": token, **state, **result,
            "overrides": record.overrides}


def forget():
    """Drop every scan and the inspection cache. For the tests."""
    with _SCANS_LOCK:
        _SCANS.clear()
    with _CACHE_LOCK:
        _CACHE.clear()


def cached_fields():
    """The names inspection fills (exported for the tests)."""
    return tuple(f.name for f in fields(Entry) if f.name in _INSPECTED_FIELDS)
