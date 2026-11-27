"""Is Scimap Pro here, and new enough? Answered without importing it.

`find_spec` locates the package through the import system's finder, which does
not execute it, and `importlib.metadata` reads its version from the
distribution's metadata. Importing `scimappro` would load numba, scanpy's
stack and the licence banner for a page that may only be showing "add a
licence" -- so the page asks this, and only a run imports the library.
"""

from __future__ import annotations

import functools
import importlib.metadata
import importlib.util
import os
import sys
from pathlib import Path

#: The oldest Scimap Pro whose API this plugin is written against.
MIN_VERSION = (0, 1, 28)


def _parse(text):
    parts = []
    for piece in str(text).split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def package_dir() -> Path | None:
    """The installed `scimappro/` directory, or None."""
    try:
        spec = importlib.util.find_spec("scimappro")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(list(spec.submodule_search_locations)[0])


@functools.lru_cache(maxsize=1)
def _probe_cached():
    root = package_dir()
    if root is None:
        return {"installed": False, "version": None, "compatible": False}
    try:
        version = importlib.metadata.version("scimappro")
    except importlib.metadata.PackageNotFoundError:
        version = "0"
    return {"installed": True, "version": version,
            "compatible": _parse(version) >= MIN_VERSION}


def desktop() -> bool:
    """Whether this process is the desktop app's bundled Python, whose packages
    are read-only and are set when the app is built."""
    return bool(os.environ.get("PLEXORA_DESKTOP")) or getattr(sys, "frozen", False)


def probe() -> dict:
    """`{installed, version, compatible, minimum, hint}` for the page."""
    found = dict(_probe_cached())
    minimum = ".".join(map(str, MIN_VERSION))
    found["minimum"] = minimum
    if found["installed"] and found["compatible"]:
        found["hint"] = ""
    elif desktop():
        found["hint"] = ("Scimap Pro is part of the desktop app. Reinstall Plexora from "
                         "plexoraapp.com to restore it.")
    else:
        found["hint"] = f'pip install "scimappro>={minimum}"'
    return found


def has_function(key: str) -> bool:
    """Whether the installed Scimap Pro offers `ns.fn`, from its catalogue."""
    from plexora.plugins.scimap.server import catalog

    return key in (catalog.load().get("functions") or {})


def has_param(key: str, name: str) -> bool:
    """Whether the installed Scimap Pro's `ns.fn` takes the argument `name`."""
    from plexora.plugins.scimap.server import catalog

    record = (catalog.load().get("functions") or {}).get(key) or {}
    return any((p.get("name") or "").lstrip("*") == name for p in record.get("params") or [])


#: What a notebook feature needs from Scimap Pro, and the words it says when
#: the installed release does not have it.
NEEDS = {
    "metadata": (("tl.addMetadata", "overwrite"), "Sample metadata needs a newer Scimap Pro (tl.addMetadata)."),
    "gated_layer": (("pp.rescale", "targetLayer"), "Cell types from gates needs a newer Scimap Pro (pp.rescale with targetLayer)."),
    "labels": (("tl.labelCells", None), "Naming chosen cells needs a newer Scimap Pro (tl.labelCells)."),
    # What the data is, which analyses fit it and the standard workflow for it
    # (`sm.ai.facts`, `sm.ai.roadmap`, `sm.ai.recipe`): what the notebook lists
    # and the defaults of the counts steps.
    "advice": (("ai.recipe", None), "Spatial transcriptomics needs a newer Scimap Pro (sm.ai.recipe)."),
    "svg_plot": (("pl.spatiallyVariableFeatures", None),
                 "Spatially variable genes needs a newer Scimap Pro (pl.spatiallyVariableFeatures)."),
}


def supports(feature: str) -> bool:
    (key, name), _why = NEEDS[feature]
    if key.startswith("ai."):
        # Not registry functions: `scimappro.ai` describes the package.
        try:
            import scimappro.ai as _ai

            return callable(getattr(_ai, key.split(".", 1)[1], None))
        except Exception:  # noqa: BLE001
            return False
    return has_function(key) and (name is None or has_param(key, name))


def why_not(feature: str) -> str:
    return "" if supports(feature) else NEEDS[feature][1]


def forget():
    """Drop the cached answer (a test, or after an install in place)."""
    _probe_cached.cache_clear()
