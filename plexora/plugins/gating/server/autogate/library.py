"""The reference library: single cells sorted by eye, kept for sheets a look can lean on.

A model judging own stain has nothing to measure faintness against: how dim
can a ring be and still be a ring, what does spill from a neighbour look like
next to a cell's own halo, how does a fibre differ from a membrane. The
library holds single-cell tiles drawn exactly as a look draws its cells
(`collage.render_collage`'s `t2` cell: morphology | context, the judged
nucleus between four magenta ticks), each sorted by a person into one of
`CLASSES` and tagged with the shape of its stain, and builds reference sheets
from them (`library_sheet`) that a session shows once to each conversation
answering its looks (`SessionOptions.reference_sheet`).

Extendable by design: `sample` draws more tiles from any image and marker at
any time (dense and sparse areas, every brightness band), the person sorts
them in the Finder (`ingest_inbox`) or by id (`label`), and `library_sheet.
build` makes another sheet from whatever is labelled -- one per layout to
compare them, more as edge cases turn up.

On disk, under `root()` (PLEXORA_GATING_LIBRARY_PATH, else
`<data_root>/.agent/gating/library`; never under the swept artifacts):

    tiles/<tile_id>.png    the tile, `TILE_SIZE`
    tiles/<tile_id>.json   everything about it, self-contained (the project may go)
    index.json             a cache of the tiles' JSON (`reindex` rebuilds it)
    sheets/<name>.webp|.png|.json
    inbox/<project>/<marker>/t07-<tile_id>.png   copies to sort, at twice the size,
                                                 one folder per class beside them
    inbox/<project>/<marker>/contact_<seed>.png  every tile of the sample, numbered
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import shutil
import threading
import time
from pathlib import Path

import numpy as np

from plexora.agent.errors import AgentError
from plexora.plugins.gating.server.autogate import schemas

VERSION = "1"
ENV = "PLEXORA_GATING_LIBRARY_PATH"

#: What a person sorts a tile into: a clear positive, a faint one, one at the
#: limit of what still counts (all three positives), a clear negative, a cell
#: lit only by a neighbour's stain, an artifact (debris, a fold, a speck).
CLASSES = ("strong", "faint", "limit", "negative", "spill", "artifact")
#: Sorted out of the library (blurred, cut off, two cells): never on a sheet.
DISCARD = "discard"
POSITIVE_CLASSES = ("strong", "faint", "limit")
#: The shape of a tile's stain: the gating vocabulary's, or not known.
SHAPES = schemas.STAIN_SHAPES + ("unknown",)
#: Where a cell sits, by the share of its neighbours (terciles of the image).
DENSITY = ("dense", "typical", "sparse")
#: The brightness bands a sample is drawn from: percentiles of the marker's
#: body (`cell_window._body`), brightest first.
BANDS = {"bright": (97.0, 99.5), "moderate": (85.0, 92.0), "dim": (60.0, 84.0),
         "limit": (35.0, 60.0), "background": (5.0, 30.0)}
BAND_ORDER = ("bright", "moderate", "dim", "limit", "background")
#: Tiles per sample, by default and at most.
N_DEFAULT = 30
N_MAX = 36
#: A tile: two 80-pixel panels side by side (the `t2` cell).
TILE_PX = 80
TILE_SIZE = (2 * TILE_PX, TILE_PX)
#: Inbox copies are drawn this many times larger, for sorting by eye.
INBOX_SCALE = 2
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_TILE = re.compile(r"^[0-9a-f]{10}$")
_LOCK = threading.Lock()


# -- the store -----------------------------------------------------------------------


def root() -> Path:
    """The library's folder (it may not exist yet)."""
    configured = os.environ.get(ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    from plexora import paths

    return paths.agent_root() / "gating" / "library"


def ensure() -> Path:
    """The library's folder, created; `precondition_missing` naming it (and
    the variable that moves it) when it cannot be written."""
    base = root()
    try:
        for sub in ("tiles", "sheets", "inbox"):
            (base / sub).mkdir(parents=True, exist_ok=True)
        probe = base / f".write-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise AgentError("precondition_missing",
                         f"the reference library at {base} cannot be written ({exc}); set "
                         f"{ENV} to a folder you can write", detail={"path": str(base)}) from None
    return base


def _atomic(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _write_json(path, value):
    _atomic(path, json.dumps(value, indent=1, sort_keys=True, default=str).encode("utf-8"))


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def tile_id(project, cell_id, marker) -> str:
    return hashlib.sha1(f"{project}|{int(cell_id)}|{marker}".encode("utf-8")).hexdigest()[:10]


def _tile_paths(tid):
    base = root() / "tiles"
    return base / f"{tid}.png", base / f"{tid}.json"


def read_tile(tid) -> dict | None:
    if not _TILE.match(str(tid)):
        return None
    return _read_json(_tile_paths(tid)[1])


def tile_png(tid) -> bytes | None:
    if not _TILE.match(str(tid)):
        return None
    try:
        return _tile_paths(tid)[0].read_bytes()
    except OSError:
        return None


def write_tile(meta, png) -> dict:
    """Store a tile; one already labelled is never overwritten (its label is
    a person's work). Returns the stored meta."""
    tid = meta["tile_id"]
    image_path, meta_path = _tile_paths(tid)
    with _LOCK:
        held = _read_json(meta_path)
        if held and held.get("class"):
            return held
        _atomic(image_path, png)
        _write_json(meta_path, meta)
        _index_put(meta)
    return meta


def _summary(meta) -> dict:
    return {k: meta.get(k) for k in ("tile_id", "project", "marker", "cell_id", "band",
                                     "density", "value", "pct", "class", "shape",
                                     "shape_source", "created", "labelled_at", "note")}


def _index_path():
    return root() / "index.json"


def reindex() -> dict:
    """Rebuild `index.json` from the tiles' own JSON (the source of truth)."""
    entries = {}
    folder = root() / "tiles"
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            meta = _read_json(path)
            if meta and meta.get("tile_id") == path.stem:
                entries[path.stem] = _summary(meta)
    index = {"version": VERSION, "tiles": entries, "built": time.time()}
    if root().is_dir():
        _write_json(_index_path(), index)
    return index


def _index() -> dict:
    """The index, rebuilt when it is missing, unreadable or holds another
    count of tiles than the folder does."""
    held = _read_json(_index_path())
    folder = root() / "tiles"
    count = len(list(folder.glob("*.json"))) if folder.is_dir() else 0
    if not isinstance(held, dict) or not isinstance(held.get("tiles"), dict) \
            or len(held["tiles"]) != count:
        return reindex()
    return held


def _index_put(meta):
    index = _index()
    index["tiles"][meta["tile_id"]] = _summary(meta)
    _write_json(_index_path(), index)


def tiles(*, classes=None, shapes=None, markers=None, projects=None, labelled=None):
    """(tiles, warnings): the tiles' summaries matching every filter given
    (`labelled`: True only sorted ones -- discarded ones never -- False only
    unsorted), sorted by id. A tile whose picture is missing is left out and
    said so."""
    out, warnings = [], []
    for tid, entry in sorted(_index()["tiles"].items()):
        klass = entry.get("class")
        if labelled is True and (not klass or klass == DISCARD):
            continue
        if labelled is False and klass:
            continue
        if classes and klass not in classes:
            continue
        if shapes and entry.get("shape") not in shapes:
            continue
        if markers and entry.get("marker") not in markers:
            continue
        if projects and entry.get("project") not in projects:
            continue
        if not _tile_paths(tid)[0].is_file():
            warnings.append(f"tile {tid}: its picture is missing; left out")
            continue
        out.append(dict(entry))
    return out, warnings


def label(labels) -> dict:
    """Sort tiles by id: `{tile_id: {class, shape?, note?}}` (class one of
    `CLASSES` or `discard`). Returns {labelled, refused}."""
    done, refused = [], []
    with _LOCK:
        for tid, said in (labels or {}).items():
            said = said if isinstance(said, dict) else {"class": said}
            klass, shape = said.get("class"), said.get("shape")
            meta = read_tile(tid)
            if meta is None:
                refused.append({"tile_id": tid, "reason": "no such tile"})
                continue
            if klass not in CLASSES + (DISCARD,):
                refused.append({"tile_id": tid, "reason": f"class is one of "
                                f"{CLASSES + (DISCARD,)}"})
                continue
            if shape is not None and shape not in SHAPES:
                refused.append({"tile_id": tid, "reason": f"shape is one of {SHAPES}"})
                continue
            meta["class"] = klass
            if shape is not None:
                meta.update(shape=shape, shape_source="person")
            if said.get("note"):
                meta["note"] = str(said["note"])[:200]
            meta["labelled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _write_json(_tile_paths(tid)[1], meta)
            _index_put(meta)
            done.append(tid)
    return {"labelled": done, "refused": refused}


def forget(tile_ids=(), sheet=None) -> dict:
    """Take tiles (and their inbox copies) or a sheet out of the library."""
    removed, missing = [], []
    with _LOCK:
        for tid in tile_ids or ():
            paths = _tile_paths(tid) if _TILE.match(str(tid)) else ()
            if not paths or not paths[1].exists():
                missing.append(tid)
                continue
            for path in paths:
                path.unlink(missing_ok=True)
            for copy in (root() / "inbox").rglob(f"*{tid}.png"):
                copy.unlink(missing_ok=True)
            removed.append(tid)
        if removed:
            reindex()
        if sheet:
            if not _NAME.match(str(sheet)):
                raise AgentError("invalid_input", "a sheet's name is letters, digits, _ and -")
            found = False
            for ext in ("webp", "png", "json"):
                path = root() / "sheets" / f"{sheet}.{ext}"
                if path.exists():
                    path.unlink()
                    found = True
            (removed if found else missing).append(f"sheet:{sheet}")
    return {"removed": removed, "missing": missing}


# -- sheets on disk --------------------------------------------------------------------


def sheets() -> list:
    """Every sheet's JSON, newest first."""
    folder = root() / "sheets"
    out = []
    if folder.is_dir():
        for path in folder.glob("*.json"):
            meta = _read_json(path)
            if meta and meta.get("name") == path.stem \
                    and (folder / f"{path.stem}.webp").is_file():
                out.append(meta)
    return sorted(out, key=lambda m: (-(m.get("built_at_s") or 0), m["name"]))


def read_sheet(name) -> dict | None:
    if not _NAME.match(str(name or "")):
        return None
    meta = _read_json(root() / "sheets" / f"{name}.json")
    if not meta or not (root() / "sheets" / f"{name}.webp").is_file():
        return None
    return meta


def sheet_bytes(name, fmt="webp") -> bytes | None:
    if not _NAME.match(str(name or "")) or fmt not in ("webp", "png"):
        return None
    try:
        return (root() / "sheets" / f"{name}.{fmt}").read_bytes()
    except OSError:
        return None


def sheet_digest(name) -> str | None:
    """The sheet's digest, or None when it (or its picture) is gone."""
    meta = read_sheet(name)
    return meta.get("digest") if meta else None


def resolve_sheet(requested) -> dict:
    """What a session's `reference_sheet` resolves to: {status, requested,
    name?, digest?, caption?, rows?, reason?}. None -> `off`; `auto` -> the
    sheet named `default`, else the newest, else `none` (no error: a library
    is optional); a name -> that sheet, else `invalid_input` listing the
    sheets there are."""
    if not requested:
        return {"status": "off", "requested": None}
    held = sheets()
    if requested == "auto":
        chosen = read_sheet("default") or (held[0] if held else None)
        if chosen is None:
            return {"status": "none", "requested": "auto",
                    "reason": "no reference sheet built yet (library_build)"}
    else:
        chosen = read_sheet(requested)
        if chosen is None:
            raise AgentError("invalid_input", f"no reference sheet named {requested!r}",
                             detail={"sheets": [m["name"] for m in held],
                                     "hint": "library_list shows them; library_build makes one"})
    return {"status": "on", "requested": requested, "name": chosen["name"],
            "digest": chosen["digest"], "caption": chosen.get("caption"),
            "rows": chosen.get("rows"), "size": chosen.get("size")}


# -- sampling cells (a table operation: pure over the dataset) -------------------------


def _quotas(n):
    base, extra = divmod(int(n), len(BAND_ORDER))
    return {band: base + (1 if i < extra else 0) for i, band in enumerate(BAND_ORDER)}


def _farthest(members, xs, ys, ids, k, rng):
    """`k` of `members` (row indices): a seeded first pick, then each time
    the one farthest from those taken (ties by cell id)."""
    if k <= 0 or members.size == 0:
        return []
    order = members[np.argsort(ids[members], kind="stable")]
    taken = [int(order[int(rng.integers(order.size))])]
    gap = (xs[order] - xs[taken[0]]) ** 2 + (ys[order] - ys[taken[0]]) ** 2
    while len(taken) < min(k, order.size):
        best = int(np.argmax(gap))      # first of the ties: the lowest cell id
        if gap[best] <= 0 and order[best] in taken:
            break
        taken.append(int(order[best]))
        gap = np.minimum(gap, (xs[order] - xs[order[best]]) ** 2
                         + (ys[order] - ys[order[best]]) ** 2)
    return taken


def library_cells(ds, marker, *, n=N_DEFAULT, seed=0) -> dict:
    """{cells: [{cell_id, x, y, value, pct, density_pct, density, band}],
    empty_bands, n_body}: `n` cells of `marker` across `BANDS` (an even share
    each), within each band across dense, sparse and typical areas, picked
    spread out over the image. QC's left-out cells are never picked."""
    from plexora.agent.evidence import cell_window
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import sampler

    n = max(1, min(int(n), N_MAX))
    c = cellmod.cells(ds)
    values = np.asarray(cellmod.values(ds, marker), dtype=np.float64)
    order = cell_window._body(values, c.xs, c.ys)
    order = order[c.valid[order]]
    if order.size == 0:
        return {"cells": [], "empty_bands": list(BAND_ORDER), "n_body": 0}
    density = sampler._density_percentile(ds, c)
    rank = np.arange(order.size) / max(1, order.size - 1) * 100.0
    pct_of = dict(zip(order.tolist(), rank.tolist()))
    out, empty = [], []
    for b, band in enumerate(BAND_ORDER):
        quota = _quotas(n)[band]
        lo, hi = BANDS[band]
        members = order[int(math.floor(lo / 100.0 * order.size)):
                        max(int(math.floor(lo / 100.0 * order.size)) + 1,
                            int(math.ceil(hi / 100.0 * order.size)))]
        if members.size == 0 or quota == 0:
            if quota:
                empty.append(band)
            continue
        d = density[members]
        groups = {"dense": members[d >= 200.0 / 3.0], "sparse": members[d < 100.0 / 3.0],
                  "typical": members[(d >= 100.0 / 3.0) & (d < 200.0 / 3.0)]}
        picked = []
        per = int(math.ceil(quota / 3.0))
        for t, name in enumerate(("dense", "sparse", "typical")):
            want = min(per, quota - len(picked))
            rng = np.random.default_rng([int(seed), b, t])
            for row in _farthest(groups[name], c.xs, c.ys, c.ids, want, rng):
                picked.append((row, name))
        if len(picked) < quota:
            # A tercile short of cells: the rest from the whole band.
            have = {row for row, _name in picked}
            rest = np.array([m for m in members if int(m) not in have], dtype=np.int64)
            rng = np.random.default_rng([int(seed), b, 9])
            for row in _farthest(rest, c.xs, c.ys, c.ids, quota - len(picked), rng):
                dp = float(density[row])
                picked.append((row, "dense" if dp >= 200 / 3 else
                               "sparse" if dp < 100 / 3 else "typical"))
        for row, name in picked:
            out.append({"cell_id": int(c.ids[row]), "x": float(c.xs[row]),
                        "y": float(c.ys[row]), "value": float(values[row]),
                        "pct": round(float(pct_of[int(row)]), 2),
                        "density_pct": round(float(density[row]), 2), "density": name,
                        "band": band})
    return {"cells": out, "empty_bands": empty, "n_body": int(order.size)}


# -- drawing tiles -------------------------------------------------------------------


def _default_shape(session, ds, marker, channel):
    """(shape, source): the panel's expected shapes, else what a contrast
    check saw, else the compartment's, else `unknown`."""
    from plexora.plugins.gating.server.autogate import context

    try:
        entry = (context.for_project(ds, create=False).get("entries") or {}).get(marker) or {}
    except Exception:
        entry = {}
    shapes = entry.get("shapes") or []
    if shapes and shapes[0] in SHAPES:
        return shapes[0], "panel"
    try:
        from plexora.agent.evidence import calibration

        record = calibration.load_current(session, ds.name)
        seen = calibration.observed(record, channel) if record else None
        pattern = (seen or {}).get("pattern_seen")
        if pattern in schemas.STAIN_SHAPES:
            return pattern, "contrast_check"
    except Exception:
        pass
    by_compartment = {"nuclear": "nuclear", "membrane": "membrane",
                      "cytoplasmic": "cytoplasmic", "nuclear_cytoplasmic": "cytoplasmic",
                      "extracellular": "fibrous"}
    compartment = entry.get("compartment")
    if compartment in by_compartment:
        return by_compartment[compartment], "compartment"
    return "unknown", "none"


def render_tiles(session, ds, marker, channel, cells, *, pixel=None, progress=None,
                 cancelled=None) -> list:
    """[(cell, png bytes, morphology info)] -- each cell drawn as the `t2`
    look draws it (the first cell block of a one-row `t2` collage, cut out),
    six at a time."""
    from PIL import Image

    from plexora.agent.evidence import collage
    from plexora.server.utils import fast_png

    side = collage.tile_side("t2")
    top = collage.HEADER_H + collage.ROW_HEADER_H
    out = []
    for start in range(0, len(cells), collage.LAYOUTS["t2"]["per_row"]):
        if cancelled is not None:
            cancelled()
        batch = cells[start:start + collage.LAYOUTS["t2"]["per_row"]]
        rendered = collage.render_collage(
            session, ds, layout="t2", rows=[{"label": "", "cells": [
                {k: c[k] for k in ("cell_id", "x", "y", "value")} for c in batch]}],
            marker=channel, fmt="png", store=False, title="reference library",
            pixel=pixel)
        canvas = np.asarray(Image.open(io.BytesIO(rendered["png"])).convert("RGB"))
        placed = {p["cell_id"]: p for p in rendered["manifest"]["rows"][0]["cells"]}
        for col, cell in enumerate(batch):
            x = col * (2 * side + collage.GAP)
            block = np.ascontiguousarray(canvas[top:top + side, x:x + 2 * side])
            out.append((cell, fast_png.encode_rgb8_png(block),
                        (placed.get(int(cell["cell_id"])) or {}).get("morphology"),
                        rendered["manifest"]))
        if progress is not None:
            progress(min(len(cells), start + len(batch)), len(cells))
    return out


def _scaled(png, factor):
    from PIL import Image

    from plexora.server.utils import fast_png

    image = Image.open(io.BytesIO(png)).convert("RGB")
    image = image.resize((image.width * factor, image.height * factor), Image.NEAREST)
    return fast_png.encode_rgb8_png(np.asarray(image))


def contact_sheet(entries, title) -> bytes:
    """Every tile of a sample in a numbered grid (`t01`..), for sorting by eye."""
    from PIL import Image, ImageDraw

    from plexora.agent.evidence import collage
    from plexora.server.utils import fast_png

    per_row, gap, caption = 6, collage.GAP, collage.CAPTION_H
    width = per_row * TILE_SIZE[0] + (per_row - 1) * gap
    rows = max(1, math.ceil(len(entries) / per_row))
    height = collage.HEADER_H + rows * (TILE_SIZE[1] + caption + gap)
    canvas = Image.new("RGB", (width, height), collage.BG)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, width, collage.HEADER_H - 1), fill=collage.HEADER_BG)
    draw.text((4, 1), title, fill=collage.TEXT, font=collage._font(11))
    small = collage._font(10)
    for i, (number, png) in enumerate(entries):
        x = (i % per_row) * (TILE_SIZE[0] + gap)
        y = collage.HEADER_H + (i // per_row) * (TILE_SIZE[1] + caption + gap)
        canvas.paste(Image.open(io.BytesIO(png)).convert("RGB"), (x, y))
        draw.text((x + 2, y + TILE_SIZE[1]), number, fill=collage.TEXT, font=small)
    return fast_png.encode_rgb8_png(np.asarray(canvas))


HOW_TO_SORT = (
    "Open the marker's inbox folder. Drag each tile into the folder of what you see: "
    "strong, faint or limit (all positives: limit is the faintest you would still call "
    "positive with the cell's shape visible), negative, spill (lit only by a neighbour's "
    "stain), artifact (debris, a speck, a fold), or discard (blurred, cut off, two cells). "
    "To correct a tile's shape, put it one folder deeper, in a folder named for the shape "
    "(e.g. faint/fibrous). Then run library_ingest. Tiles left loose stay unsorted.")


def sample(session, project, marker, *, n=N_DEFAULT, seed=0, progress=None,
           cancelled=None) -> dict:
    """Draw `n` tiles of `marker` from `project` into the library and its
    inbox (`library_cells`, then `render_tiles`), with a contact sheet.
    Labelled tiles already there are kept as they are."""
    from plexora.plugins.gating.server.autogate import tableops, views
    from plexora.server.utils import pixel_scale

    base = ensure()
    ds = session.data(project)
    if marker not in ds.table.markers:
        raise AgentError("invalid_input", f"{marker!r} is not a marker of {project!r}")
    channel = views.image_channel(ds, marker)
    if channel is None:
        raise AgentError("precondition_missing", f"{marker!r} has no image channel to draw")
    found, where = tableops.local_or_node_or_fallback(
        ds, "gating.autogate.library_cells", {"marker": marker, "n": int(n), "seed": int(seed)})
    cells = found.get("cells") or []
    if not cells:
        raise AgentError("precondition_missing", f"{marker!r} has no cells to sample in "
                         f"{project!r} (none left after QC, or no positions)")
    pixel = pixel_scale.pixel_size(session.project(project))
    shape, shape_source = _default_shape(session, ds, marker, channel)
    drawn = render_tiles(session, ds, marker, channel, cells, pixel=pixel, progress=progress,
                         cancelled=cancelled)
    inbox = base / "inbox" / _safe(project) / _safe(marker)
    inbox.mkdir(parents=True, exist_ok=True)
    for klass in CLASSES + (DISCARD,):
        (inbox / klass).mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    listed, contact = [], []
    for number, (cell, png, morphology, manifest) in enumerate(drawn, start=1):
        tid = tile_id(project, cell["cell_id"], marker)
        channel_rows = {c["name"]: c for c in manifest.get("channels") or ()}
        meta = {"tile_id": tid, "version": VERSION, "project": project,
                "image": project, "cell_id": int(cell["cell_id"]), "x": cell["x"],
                "y": cell["y"], "marker": marker, "channel": channel,
                "nuclear": (manifest.get("display") or {}).get("nuclear"),
                "value": cell["value"], "pct": cell["pct"], "band": cell["band"],
                "density": cell["density"], "density_pct": cell["density_pct"],
                "pixel_um": (pixel or {}).get("value"),
                "pixel_source": (pixel or {}).get("source"),
                "crop_side_px": (manifest.get("crop") or {}).get("side_px"),
                "tile_px": manifest.get("tile_px"),
                "radius_px": ((manifest.get("display") or {}).get("centre") or {}).get(
                    "radius_px"),
                "windows": {name: {k: row.get(k) for k in ("window", "window_source",
                                                           "curve", "chosen_by")
                                   if row.get(k) is not None}
                            for name, row in channel_rows.items()},
                "morphology": morphology, "renderer": manifest.get("renderer"),
                "calibration": (manifest.get("calibration") or {}).get("version"),
                "created": stamp, "seed": int(seed), "cells_from": where,
                "class": None, "shape": shape, "shape_source": shape_source,
                "note": None, "labelled_at": None}
        stored = write_tile(meta, png)
        label_ = f"t{number:02d}"
        # One copy per tile in the inbox: a loose one from an earlier sample is
        # replaced (renumbered as this contact sheet numbers it); one the
        # person already sorted into a folder is left where they put it.
        copies = list(inbox.rglob(f"*-{tid}.png"))
        for copy in copies:
            if copy.parent == inbox:
                copy.unlink(missing_ok=True)
        if not stored.get("class") and not any(c.parent != inbox for c in copies):
            _atomic(inbox / f"{label_}-{tid}.png", _scaled(png, INBOX_SCALE))
        listed.append({"tile_id": tid, "label": label_, "band": cell["band"],
                       "density": cell["density"], "value": cell["value"],
                       "pct": cell["pct"], "shape": stored.get("shape"),
                       "class": stored.get("class")})
        contact.append((label_, png))
    sheet_png = contact_sheet(contact, f"{marker} · {project} · {len(contact)} cells · sort "
                                       "each into a class folder")
    contact_path = inbox / f"contact_{int(seed)}.png"
    _atomic(contact_path, sheet_png)
    _write_readme(base)
    return {"library": str(base), "inbox": str(inbox), "contact_sheet": str(contact_path),
            "contact_png": sheet_png, "tiles": listed, "empty_bands": found.get("empty_bands"),
            "cells_from": where, "shape": shape, "shape_source": shape_source,
            "how_to_sort": HOW_TO_SORT}


def _write_readme(base):
    path = base / "inbox" / "README.txt"
    if not path.exists():
        _atomic(path, (HOW_TO_SORT + "\n").encode("utf-8"))


def _safe(name):
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(name)) or "_"


# -- the Finder inbox ------------------------------------------------------------------


def ingest_inbox(*, dry_run=False) -> dict:
    """Read the person's sorting: every `inbox/<project>/<marker>/<class>[/<shape>]/
    *.png` labels its tile. A folder name that is no class or shape refuses
    the whole ingest before anything changes. Sorted copies are removed; a
    marker folder with nothing left to sort goes with its contact sheet.
    `dry_run`: say what would happen."""
    base = root()
    inbox = base / "inbox"
    if not inbox.is_dir():
        return {"labelled": [], "unknown_folders": [], "left_unsorted": 0, "dry_run": dry_run}
    plan, unknown, loose, unmatched = [], [], 0, []
    for marker_dir in sorted(p for p in inbox.glob("*/*") if p.is_dir()):
        for entry in sorted(marker_dir.iterdir()):
            if entry.is_file():
                loose += entry.suffix == ".png" and not entry.name.startswith("contact_")
                continue
            if entry.name not in CLASSES + (DISCARD,):
                unknown.append(str(entry.relative_to(inbox)))
                continue
            for item in sorted(entry.rglob("*.png")):
                parts = item.relative_to(entry).parts
                shape = parts[0] if len(parts) > 1 else None
                if len(parts) > 2 or (shape is not None and shape not in SHAPES):
                    unknown.append(str(item.parent.relative_to(inbox)))
                    continue
                tid = item.stem.rsplit("-", 1)[-1]
                if read_tile(tid) is None:
                    unmatched.append(str(item.relative_to(inbox)))
                    continue
                plan.append((tid, entry.name, shape, item))
    unknown = sorted(set(unknown))
    if unknown:
        raise AgentError("invalid_input", "the inbox has folders that are no class or shape; "
                         "nothing was read", detail={"unknown": unknown,
                                                     "classes": list(CLASSES + (DISCARD,)),
                                                     "shapes": list(SHAPES)})
    out = {"labelled": [{"tile_id": t, "class": k, **({"shape": s} if s else {})}
                        for t, k, s, _p in plan],
           "unmatched": unmatched, "left_unsorted": int(loose), "dry_run": bool(dry_run)}
    if dry_run or not plan:
        return out
    label({t: {"class": k, **({"shape": s} if s else {})} for t, k, s, _p in plan})
    for _t, _k, _s, path in plan:
        path.unlink(missing_ok=True)
    for marker_dir in sorted(p for p in inbox.glob("*/*") if p.is_dir()):
        left = [p for p in marker_dir.rglob("*.png") if not p.name.startswith("contact_")]
        if not left:
            shutil.rmtree(marker_dir, ignore_errors=True)
    for project_dir in sorted(p for p in inbox.glob("*") if p.is_dir()):
        if not any(project_dir.iterdir()):
            project_dir.rmdir()
    return out


def inbox_state() -> list:
    """[{project, marker, unsorted, sorted}] for every inbox folder."""
    inbox = root() / "inbox"
    out = []
    if not inbox.is_dir():
        return out
    for marker_dir in sorted(p for p in inbox.glob("*/*") if p.is_dir()):
        pngs = [p for p in marker_dir.rglob("*.png") if not p.name.startswith("contact_")]
        loose = [p for p in pngs if p.parent == marker_dir]
        out.append({"project": marker_dir.parent.name, "marker": marker_dir.name,
                    "unsorted": len(loose), "sorted": len(pngs) - len(loose),
                    "path": str(marker_dir)})
    return out
