"""Which folders in a listing are one thing to import, not a place to walk into.

A `.zarr` store, a SpatialData store, a Xenium or Visium run: each is a
directory on disk and one dataset to a person, and the explorer offers it as a
row that is chosen on one click rather than a folder that is opened. This
module says which rows those are, and does it within a listing's budget.

**Names first, and never a walk.** Three tiers, each strictly cheaper than the
one after it:

- `store_by_name` -- a child row by its own name (`*.zarr`). Free.
- `store_by_names` -- a child row by the names inside it, from one bounded
  `scandir` the listing makes only when the folder has few subdirectories.
- `dataset_of` -- the OPEN folder, by its names and then the importer's own
  detectors, which may read a manifest or a scale-factor file. One folder per
  listing, and only after its names have said it is worth asking.

The order mirrors `import_proposal._detect_directory`, so a row marked here is
what the importer will make of it. DICOM is matched by names only, at most one
level down: `dicom_wsi.is_dicom_path` walks four levels breadth-first with no
bound on width, which is the right question for an import and the wrong one to
ask of every row on screen.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from spatialbridge.grouping.names import BUNDLE_LABELS

from plexora.server.utils import dicom_wsi, ome_zarr, spatial_scene

#: Directory suffixes that are one store, not a tree: a search never descends
#: into one (its chunks are thousands of meaningless names), and a `.zarr` row
#: is a dataset by its name alone.
STORE_DIR_SUFFIXES = (".zarr", ".n5")

#: What each dataset type is called in a sentence ("This folder is a Xenium
#: run."). The keys are the `type` the client matches on. Shared with the
#: grouping rules, which name a run by it.
TYPE_LABELS = BUNDLE_LABELS

#: The licence entitlement a type needs to be imported. A type with none is
#: free; one listed here reads `licensed: False` when the licence lacks it, so
#: the explorer can say so instead of offering a run the importer will refuse.
ENTITLEMENTS = {
    "xenium": "transcriptomics:xenium",
    "visium": "transcriptomics:visium",
    "visium_hd": "transcriptomics:visium",
}

_XENIUM_NAMES = frozenset((*spatial_scene.XENIUM_FILES, *spatial_scene.XENIUM_TABLES))
_SPATIALDATA_GROUPS = frozenset((*spatial_scene.SPATIALDATA_KINDS,
                                 spatial_scene.SPATIALDATA_TABLES))
_ZARR_MARKERS = frozenset(ome_zarr._ZARR_MARKERS)


def _mark(kind, name=""):
    """`{type, label}` for one dataset type."""
    label = TYPE_LABELS[kind]
    if kind == "zarr" and name.lower().endswith(".ome.zarr"):
        label = "OME-Zarr image"
    return {"type": kind, "label": label}


def store_by_name(name):
    """A child row's mark from its own name, or None. Costs nothing."""
    if str(name).lower().endswith(".zarr"):
        return _mark("zarr", str(name))
    return None


def store_by_names(name, child_names):
    """A directory's mark from the names directly inside it, or None.

    `child_names` is one `scandir`'s worth -- files and folders alike, since a
    name is all a marker is. The order is `_detect_directory`'s: Visium HD
    before Visium (a lone bin folder carries Visium's scale factors), and the
    SpatialData groups before a bare zarr marker (a SpatialData store IS a zarr
    group, and testing for the latter first would claim every one of them).

    A SpatialData group name also needs the zarr to be there -- a marker file
    or a `.zarr` name. An ordinary project folder with an `images/` folder in
    it is the commonest thing on a lab share, and marking it a store would turn
    "open this folder" into "import this folder" on one click.
    """
    names = set(child_names or ())
    lowered = str(name).lower()
    zarr_named = lowered.endswith(".zarr")
    zarr_marked = bool(names & _ZARR_MARKERS)

    if spatial_scene.VISIUM_HD_BINNED in names or (
            lowered == spatial_scene.VISIUM_HD_BINNED
            and any(spatial_scene.VISIUM_HD_BIN_RE.match(n) for n in names)) or (
            spatial_scene.VISIUM_HD_BIN_RE.match(str(name))
            and spatial_scene.VISIUM_SPATIAL in names):
        return _mark("visium_hd")
    if "experiment.xenium" in names or len(names & _XENIUM_NAMES) >= 2:
        return _mark("xenium")
    if spatial_scene.VISIUM_SPATIAL in names and any(
            fnmatch.fnmatchcase(n, "*feature_bc_matrix")
            or fnmatch.fnmatchcase(n, "*feature_bc_matrix.h5") for n in names):
        return _mark("visium")
    groups = names & _SPATIALDATA_GROUPS
    if groups and (zarr_named or zarr_marked):
        if groups == {"labels"}:
            # An OME-Zarr image files its own segmentations under `labels/`;
            # that layout alone is the image, as `is_spatialdata_store` says.
            return _mark("zarr", str(name))
        return _mark("spatialdata")
    if any(dicom_wsi._is_dicom_name(n) for n in names):
        return _mark("dicom")
    if zarr_named or zarr_marked:
        return _mark("zarr", str(name))
    return None


def child_names(path, cap):
    """Up to `cap` names directly inside `path`, or None when it is shut.

    None is reserved for `PermissionError`, which the listing shows as a lock
    on the row: on a cluster's group share most folders are somebody else's,
    and finding that out by clicking each one is the experience this replaces.
    Any other failure (a link to nowhere, a mount that went away) is an empty
    list -- nothing to mark, and nothing to claim about it either.
    """
    found = []
    try:
        with os.scandir(path) as scan:
            for entry in scan:
                found.append(entry.name)
                if len(found) >= cap:
                    break
    except PermissionError:
        return None
    except OSError:
        return []
    return found


def entitled(kind):
    """Whether this licence lets `kind` be imported. Never raises."""
    entitlement = ENTITLEMENTS.get(kind)
    if entitlement is None:
        return True
    from plexora.server.models.import_proposal import _licensed

    return _licensed(entitlement)


def dataset_of(directory, top_names, licensed=entitled, children=None):
    """`{type, label, licensed}` when the OPEN folder is one dataset, else None.

    `top_names` is every name in the folder, hidden ones included -- zarr's
    markers are dotfiles. Its own names decide which detector is worth asking,
    and only that one is: `is_visium_hd_run` reads a scale-factor file, and a
    home directory must not pay for it.

    `children` maps a subdirectory's name to its names (None for one that is
    shut) when the listing already read them; it is how a DICOM export whose
    instances sit one level down is still recognised without a walk.

    `licensed` is asked once, and only for a type that needs a licence.
    """
    directory = Path(directory)
    mark = store_by_names(directory.name, top_names)
    if mark is None and children:
        if any(dicom_wsi._is_dicom_name(name)
               for names in children.values() if names for name in names):
            mark = _mark("dicom")
    if mark is None:
        return None

    kind = mark["type"]
    confirm = {
        "visium_hd": spatial_scene.is_visium_hd_run,
        "xenium": spatial_scene.is_xenium_run,
        "visium": spatial_scene.is_visium_run,
        "spatialdata": spatial_scene.is_spatialdata_store,
    }.get(kind)
    try:
        if confirm is not None and not confirm(directory):
            return None
    except OSError:
        return None
    return {**mark, "licensed": bool(licensed(kind))}
