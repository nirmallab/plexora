"""Which images, masks and tables under a folder belong together.

`dataset_scan` finds the files; this decides what they make: one project per
image, its segmentation mask, its cell table and its channel names, each with
a confidence and the reasons for it. Deterministic, explainable, and a pure
function of the inventory and the user's corrections -- the same tree and the
same corrections always give the same plan, and nothing here reads a file
except the few channel lists it names channels from.

The rules -- identity, scores and margins, one table for many images,
conventions and corrections -- are `spatialbridge.grouping`'s, shared with
SCIMAP Pro so both applications pair a folder the same way; its docstring
explains them. What is Plexora's is what the rules ask of a host: which
images are already projects (`_existing_for`), the names a channel list holds
(`_panel_names`, `_sheet_names`), and the names already taken.
"""

from __future__ import annotations

import copy
import os

from spatialbridge import grouping
from spatialbridge.grouping.identity import (  # noqa: F401 -- read here by callers and tests
    GENERIC_WORDS,
    IMAGE_WORDS,
    MASK_WORDS,
    ROLE_FOLDER_NAMES,
    ROLE_WORDS,
    STRUCTURAL_WORDS,
    TABLE_WORDS,
    Identity,
    dir_role,
    identity_of,
    is_generic,
    pattern_of,
)
from spatialbridge.grouping.plan import (  # noqa: F401
    HIGH,
    MEDIUM,
    ROLE_LABELS,
    SLOTS,
    WEIGHTS,
    OrderedDictList,
    clean_overrides,
    confidence,
)


def plan(inventory, overrides=None, *, config=None) -> dict:
    """The plan for `inventory` under `overrides`. Pure and deterministic.

    @param config - the registered projects, `{name: record}`, for finding
        images already imported. Read from the data root when None.
    """
    if config is None:
        from plexora import get_config

        config = get_config() or {}
    from plexora.server.models.dataset_scan import SCAN_MAX_DEPTH, SCAN_MAX_ENTRIES

    hooks = grouping.Hooks(existing_for=lambda entry: _existing_for(entry, config),
                           panel_names=_panel_names, sheet_names=_sheet_names,
                           taken_names=set(config))
    return grouping.plan(inventory, overrides, hooks=hooks,
                         limits={"max_depth": SCAN_MAX_DEPTH, "max_entries": SCAN_MAX_ENTRIES})


def _existing_for(entry, config):
    from plexora.datasource import _find_existing_datasource_for_image

    if not config:
        return None
    if entry.kind == "bundle":
        from plexora.server.models.project import Project

        remote = "://" in entry.path
        root = entry.path if remote else os.path.realpath(entry.path)
        for name, record in config.items():
            try:
                bundles = Project.from_entry(name, record).bundles
            except Exception:  # noqa: BLE001 -- one odd record is not a plan failure
                continue
            for bundle in bundles:
                if bundle.get("root") and (bundle["root"] if remote else
                                           os.path.realpath(bundle["root"])) == root:
                    return name
        return None
    try:
        return _find_existing_datasource_for_image(entry.real, config)
    except Exception:  # noqa: BLE001
        return None


_PANEL_CACHE: dict = {}


def _panel_names(entry, count):
    from plexora.server.models.import_proposal import _panel_names as names

    # By what is read, not only by which file: a remote list is planned before
    # its local copy (`proxy`) exists, while the scan is still reading, and the
    # None that first read gave must not outlive the copy's arrival.
    key = (entry.key, entry.proxy, count)
    if key not in _PANEL_CACHE:
        try:
            _PANEL_CACHE[key] = names(entry.proxy or entry.path, count)
        except Exception:  # noqa: BLE001
            _PANEL_CACHE[key] = None
    return _PANEL_CACHE[key]


def _sheet_names(entry):
    """Every name a channel list holds, when its header says which column."""
    from plexora.server.models.import_proposal import _marker_sheet
    from plexora.server.utils import channel_file

    try:
        sheet = _marker_sheet(entry.proxy or entry.path)
        if sheet is None or sheet[2] is None:
            return None
        grid, column, has_header = sheet
        return list(channel_file.names(grid, column, has_header)) or None
    except Exception:  # noqa: BLE001 -- an unreadable list is no list
        return None


# -- after the plan ------------------------------------------------------------------


def verify(result) -> dict:
    """`result` with every file that has since disappeared taken out.

    A vanished mask or table is cleared from its project with a warning; a
    vanished image makes its project `unavailable`. Nothing else changes.
    """
    result = copy.deepcopy(result)
    if result.get("place"):
        # Asked of a server, a file each, on every correction: too slow to be
        # worth it. Creation checks every path before it registers anything.
        return result
    unavailable = []
    for project in result["projects"]:
        image = project["image"]
        if image.get("path") and not os.path.exists(image["path"]):
            project["status"] = "unavailable"
            unavailable.append({"path": image["path"], "project": project["id"],
                                "slot": "image"})
            continue
        for slot in ("mask", "table", "channels"):
            found = project.get(slot)
            if found and found.get("path") and not os.path.exists(found["path"]):
                unavailable.append({"path": found["path"], "project": project["id"],
                                    "slot": slot})
                project["warnings"].append(f"{found['name']} is no longer there.")
                project[slot] = None
    result["unavailable"] = unavailable
    result["counts"]["unavailable"] = sum(1 for u in unavailable if u["slot"] == "image")
    return result
