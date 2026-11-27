"""What the notebook's data is, and so which tools, words and defaults it gets.

The notebook never decides on its own whether an analysis fits the data:
SCIMAP Pro does (`sm.ai.facts`, `sm.ai.roadmap`), so the sidebar and the
agent give the same answer. This module gathers what Plexora knows that the
file does not say, hands it to SCIMAP Pro as context, and reduces the answer
to what the page needs:

    mode    imaging | st-cell | st-spot | st-bin, or None when unknown: the
            key the tool table, the lexicon and the recipes are chosen by
    chip    one line for the header ("Visium HD · cells · 18,085 genes · µm")

Two moments:

- Plexora's records are the context: a 10x conversion's provenance
  (`tenx_matrix.read_provenance`: bins, cells or spots, Space Ranger's
  microns per pixel and bin size, as written), the project's pixel size.
  Plexora derives nothing from them.
- `sm.ai.facts` reads the data with that context -- the working file after
  Prepare, the project's own AnnData before it -- cached on the file's
  modification time. Everything else (the modality of an unstamped table,
  raw counts, the panel) is SCIMAP Pro's reading. A SCIMAP Pro without
  `sm.ai.facts` (0.1.28) gets the records alone, and nothing is guessed:
  the mode stays unknown unless the records name it.

A person's correction (`override`: modality, resolution, coordinate_unit)
wins over everything, as SCIMAP Pro treats the study context.

Coordinates in a Plexora table are image pixels (Xenium's micrometres are
converted at import), so `coordinate_unit` is `px` and `microns_per_unit` is
the project's pixel size whenever Plexora made the table.
"""

from __future__ import annotations

import os
import threading

#: The modes the notebook adapts to, in the order the data-type menu lists them.
MODES = ("imaging", "st-cell", "st-spot", "st-bin")
#: What a person may correct, and the values each takes.
OVERRIDABLE = {"modality": ("imaging", "spatialTranscriptomics", "scRNASeq"),
               "resolution": ("cell", "spot", "bin"),
               "coordinate_unit": ("um", "px")}
#: Table types (`records.project_info`'s `type`) whose coordinates are whatever the file says, not image pixels.
ANNDATA_TYPES = ("anndata", "h5ad", "spatialdata")
#: A 10x conversion's row kind -> what one row is.
TENX_RESOLUTION = {"bins": "bin", "cells": "cell", "spots": "spot"}
#: The facts the page reads; everything else SCIMAP Pro returns stays on the server.
KEEP = ("modality", "resolution", "platform", "coordinate_unit", "microns_per_unit", "bin_size_um",
        "spot_diameter_um", "raw_counts", "normalised", "panel", "n_features", "cells", "n_samples",
        "sample_column", "group_by", "per_arm_min", "unit", "n_units", "deconvolved", "clustered", "phenotype",
        "coordinates", "caveats", "unknown", "provenance", "candidate_groups", "elements")
PLATFORM_NAMES = {"xenium": "Xenium", "cosmx": "CosMx", "merscope": "MERSCOPE", "merfish": "MERFISH",
                  "visium": "Visium", "visium_hd": "Visium HD", "cell-table": "Cell table",
                  "chromium": "Chromium"}

_cache: dict = {}
_lock = threading.Lock()


# -- what Plexora knows ------------------------------------------------------------------------------------------

def tenx_context(table_path) -> dict:
    """What a 10x conversion recorded about its rows and scale; `{}` for any other table."""
    if not table_path:
        return {}
    try:
        from plexora.server.utils import tenx_matrix

        found = tenx_matrix.read_provenance(table_path)
    except Exception:  # noqa: BLE001 -- a table Plexora did not convert says nothing here
        return {}
    if not found:
        return {}
    kind = TENX_RESOLUTION.get(str(found.get("kind") or ""))
    factors = found.get("scalefactors") or {}
    source = str(found.get("source") or "")
    out = {"modality": "spatialTranscriptomics", "coordinate_unit": "px"}
    if kind:
        out["resolution"] = kind
    per_pixel = _number(factors.get("microns_per_pixel"))
    bin_um = _number(factors.get("bin_size_um"))
    if "cell_feature_matrix" in source:
        out["platform"] = "xenium"
    elif kind == "bin" or bin_um or "segmented_outputs" in source or "binned_outputs" in source:
        out["platform"] = "visium_hd"
    elif kind == "spot":
        out["platform"] = "visium"
    if bin_um:
        out["bin_size_um"] = bin_um
    if per_pixel:
        out["microns_per_unit"] = per_pixel
    return out


def context_for(info=None, *, table_path=None, override=None) -> dict:
    """The context SCIMAP Pro reads the data with: Plexora's records, then the person's corrections on top.

    `info` is a project's `plexora.api.records.project_info` (or a list of them, for a dataset; their shared
    facts are kept). `by` says who said each value, which SCIMAP Pro records as each fact's source."""
    infos = [i for i in (info if isinstance(info, (list, tuple)) else [info]) if i]
    found, by = {}, {}
    paths = [table_path] if table_path else [_table(i) for i in infos]
    readings = [tenx_context(p) for p in paths if p]
    shared = _shared(readings) if readings else {}
    for key, value in shared.items():
        found[key] = value
        by[key] = "decision"
    sizes = {_number(i.get("pixel_size_effective") or i.get("pixel_size")) for i in infos}
    sizes.discard(None)
    if len(sizes) == 1 and "microns_per_unit" not in found:
        found["microns_per_unit"] = sizes.pop()
        by["microns_per_unit"] = "decision"
    if infos and "coordinate_unit" not in found and all((i.get("type") or "") not in ANNDATA_TYPES for i in infos):
        # A table Plexora read from an image's segmentation: centroids in that image's pixels.
        found["coordinate_unit"] = "px"
        by["coordinate_unit"] = "decision"
    for key, value in clean_override(override).items():
        found[key] = value
        by[key] = "user"
    if found:
        found["by"] = by
    return found


def clean_override(override) -> dict:
    """A person's corrections, kept to `OVERRIDABLE`."""
    return {key: value for key, value in (override or {}).items()
            if key in OVERRIDABLE and value in OVERRIDABLE[key]}


def _table(info) -> str | None:
    table = (info or {}).get("table") or (info or {}).get("src")
    return str(table) if table and str(table).endswith(".h5ad") else None


def _shared(readings) -> dict:
    """The values every reading agrees on."""
    first, rest = readings[0], readings[1:]
    return {key: value for key, value in first.items() if all(r.get(key) == value for r in rest)}


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and number == number and number != float("inf") else None


# -- the answer --------------------------------------------------------------------------------------------------

def detect(path=None, *, context=None, n_features=None) -> dict:
    """The facts of the AnnData at `path` as `sm.ai.facts` reads them with `context` (or, with no file or no
    `sm.ai.facts`, the context alone), reduced to `KEEP` plus `mode`, `chip` and `source` (`scimappro` or
    `records`)."""
    context = dict(context or {})
    key = None
    if path and os.path.exists(path):
        key = (str(path), os.stat(path).st_mtime_ns, repr(sorted(context.items(), key=lambda kv: kv[0])))
        with _lock:
            if key in _cache:
                return _cache[key]
    found = _from_scimappro(path, context) if key else None
    if found is None:
        found = _from_records(path, context, n_features)
    found["mode"] = mode(found)
    found["chip"] = chip(found)
    if key:
        with _lock:
            if len(_cache) > 64:
                _cache.clear()
            _cache[key] = found
    return found


def _from_scimappro(path, context) -> dict | None:
    try:
        import scimappro as sm

        reader = getattr(getattr(sm, "ai", None), "facts", None)
        if reader is None:
            return None
        found = reader(str(path), context=context)
    except ValueError as problem:
        # Facts that contradict each other (a correction the file refutes): said, and the records stand.
        found = _from_records(path, context, None)
        found.setdefault("caveats", []).append(str(problem))
        return found
    except Exception:  # noqa: BLE001 -- an unreadable file falls back to what Plexora recorded
        return None
    return reduce(found)


def reduce(found) -> dict:
    """SCIMAP Pro's facts (`sm.ai.facts`) as the page and the tools read them: `KEEP`, `mode`, `chip`."""
    out = {key: found.get(key) for key in KEEP}
    out["elements"] = {k: v for k, v in (found.get("elements") or {}).items() if k != "var"}
    out["source"] = "scimappro"
    out["mode"] = mode(out)
    out["chip"] = chip(out)
    return out


def _from_records(path, context, n_features) -> dict:
    """What Plexora's records say and the file's header counts, nothing read past them."""
    found = {key: None for key in KEEP}
    found.update(caveats=[], unknown={}, provenance={})
    if n_features is None and path:
        n_features = _header_features(path)
    for key in ("modality", "resolution", "platform", "coordinate_unit", "microns_per_unit", "bin_size_um",
                "spot_diameter_um"):
        if context.get(key) is not None:
            found[key] = context[key]
            found["provenance"][key] = {"source": (context.get("by") or {}).get(key, "user"),
                                        "basis": "Plexora's records" if (context.get("by") or {}).get(key) !=
                                        "user" else "the person"}
    if found["modality"] is None:
        found["unknown"]["modality"] = "SCIMAP Pro has not read the data, and Plexora's records do not say"
    if n_features:
        found["n_features"] = int(n_features)
    found["source"] = "records"
    return found


def _header_features(path) -> int | None:
    """`n_vars` from an `.h5ad` header, without reading the matrix: X's shape (a dense dataset's, or the `shape`
    a sparse group records; the var index itself is a group in newer anndata)."""
    try:
        import h5py

        with h5py.File(path, "r") as handle:
            matrix = handle["X"]
            shape = matrix.shape if isinstance(matrix, h5py.Dataset) else matrix.attrs.get("shape")
            return int(shape[1])
    except Exception:  # noqa: BLE001
        return None


def mode(found) -> str | None:
    """`imaging`, `st-cell`, `st-spot` or `st-bin`; None when the modality is not known."""
    modality, resolution = found.get("modality"), found.get("resolution")
    if modality == "imaging":
        return "imaging"
    if modality in ("spatialTranscriptomics", "scRNASeq"):
        return {"spot": "st-spot", "bin": "st-bin"}.get(resolution, "st-cell")
    return None


def chip(found) -> str:
    """One line naming the data: platform, what one row is, how many features, the coordinates' unit."""
    parts = []
    platform = found.get("platform")
    if platform and platform != "cell-table":
        parts.append(PLATFORM_NAMES.get(platform, platform))
    current = mode(found)
    parts.append({"imaging": "multiplexed imaging", "st-cell": "single-cell RNA", "st-spot": "spots",
                  "st-bin": f"{_um(found.get('bin_size_um'))} bins" if found.get("bin_size_um") else "bins",
                  None: "data type unknown"}[current])
    n = found.get("n_features")
    if n:
        word = "markers" if current == "imaging" else "genes" if current else "features"
        parts.append(f"{int(n):,} {word}")
    if found.get("microns_per_unit"):
        parts.append("µm")
    elif found.get("coordinate_unit") == "px":
        parts.append("px")
    return " · ".join(parts)


def _um(value) -> str:
    number = float(value)
    return f"{number:g} µm"


def forget(path=None):
    """Drop the cached facts (of one file, or all)."""
    with _lock:
        for key in [k for k in _cache if path is None or k[0] == str(path)]:
            _cache.pop(key, None)


__all__ = ["MODES", "OVERRIDABLE", "chip", "clean_override", "context_for", "detect", "forget", "mode",
           "tenx_context"]
