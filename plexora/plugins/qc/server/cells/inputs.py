"""What a project's cell calls were derived from, as a fingerprint.

The calls go stale when something they read changes, not only when a region
is drawn: a mask replaced in place, a table re-exported, a pixel size set, a
segmentation transform corrected. `fingerprints` reads stats and record
fields only -- no pixel, no table row -- so the exclusion provider can ask on
every gating estimate (`exclusions._stale`).

Which change costs what (`write_for_active`, `exclusions`):

- `MAPPING` keys (mask, table, pixel size, transform, cells version): every
  region's overlap is computed again;
- `DIRECT` keys (Segmentation QC, DNA retention): the per-cell flags are read
  again, no mask read;
- the ROI revision: only the regions that changed are read (`calls`);
- strictness and the category switch: a re-derive from the stored pairs.
"""

from __future__ import annotations

import hashlib
import json
import os

#: Inputs whose change invalidates every stored region-to-cell pair.
MAPPING = ("mask_fp", "table_fp", "pixel_um", "transform_hash", "cells_version")
#: Inputs that change the per-cell flags read beside the regions.
DIRECT = ("seg_fp", "dna_fp")


def _stamp(path):
    if not path:
        return None
    try:
        stat = os.stat(path)
        return f"{stat.st_size}-{stat.st_mtime_ns}"
    except OSError:
        return None


def _hash(value) -> str:
    blob = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def table_fingerprint(record) -> str | None:
    """The table file's size and mtime; for a node-hosted table, what it
    looked like when attached (no request to the node)."""
    binding = (record.resources or {}).get("table") if hasattr(record, "resources") else None
    if binding is not None and getattr(binding, "provider", "local") != "local":
        return _hash({"node": binding.node, "id": binding.resource_id,
                      "fp": dict(binding.fingerprint or {})})
    spec = getattr(record, "dataset", None)
    src = getattr(spec, "src", None) if spec is not None else None
    stamp = _stamp(src)
    return _hash({"src": str(src), "stamp": stamp, "table": getattr(spec, "table", None),
                  "feature_source": getattr(record, "feature_source", None)}) if src else None


def fingerprints(project, document=None) -> dict:
    """{key: value} of every input the calls depend on."""
    from plexora import api
    from plexora.plugins.qc.server import schemas
    from plexora.plugins.qc.server.cells import areas
    from plexora.server.utils import pixel_scale

    ds = api.project_data(project) if isinstance(project, str) else project
    record = ds.project
    seg = record.segmentation
    try:
        pixel = pixel_scale.pixel_size(record)
    except Exception:
        pixel = None
    out = {"mask_fp": areas.mask_fingerprint(record) if seg.available else None,
           "table_fp": table_fingerprint(record),
           "pixel_um": float(pixel["value"]) if pixel else None,
           "transform_hash": _hash({"transform": list(seg.transform) if seg.transform else None,
                                    "scale": int(getattr(seg, "scale", 1) or 1),
                                    "extra": int(getattr(seg, "extra_levels", 0) or 0)}),
           "cells_version": schemas.CELLS_VERSION}
    out["seg_fp"] = _seg_fp(ds.name)
    out["dna_fp"] = _dna_fp(ds.name)
    if document is not None:
        out["strictness_hash"] = _hash(document.get("strictness") or {})
        out["filter_hash"] = _hash(document.get("exclusion_filter") or {})
    return out


def _seg_fp(project):
    from plexora.plugins.qc.server.segqc import run as segqc

    try:
        return segqc.current_fingerprint(project)
    except Exception:
        return None


def _dna_fp(project):
    from plexora.plugins.qc.server import dna_retention

    try:
        summary = dna_retention.current(project)
    except Exception:
        return None
    return (summary or {}).get("fingerprint")


def digest(inputs, keys=None) -> str:
    """A short hash of `inputs` (only `keys`, when given)."""
    picked = {k: inputs.get(k) for k in (keys or sorted(inputs))}
    return _hash(picked)


def changed(stored, now) -> set:
    """The keys whose value differs between two fingerprints (a key missing
    from `stored` counts as changed: an older result)."""
    if not stored:
        return set(now)
    return {k for k in now if stored.get(k) != now.get(k)}


def needs_full(stored, now) -> bool:
    """Whether every region's overlap must be read again."""
    return bool(changed(stored, now) & set(MAPPING))
