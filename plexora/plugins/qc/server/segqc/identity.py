"""What a mask is, without reading it: the inputs' half of segmentation QC's
fingerprint (`run.py`), and of the cell-area check's (`cells/areas.py`); and
which result is current (`current_fingerprint`), which QC's cell inputs ask.

Apart from `run.py` so that asking for a mask's identity imports nothing
heavier than `os`: `run.py` brings the QC kernels with it, and on the website
those bring the scipy stage, which a fingerprint taken while a project opens
(display contrast asks QC for its exclusions) has no use for.
"""

from __future__ import annotations

import json
import os


def _folder(project):
    from plexora import api

    path = api.store(project, "qc").directory() / "segqc"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def current_fingerprint(project):
    """The fingerprint of the last result stored, or None."""
    pointer = _read_json(_folder(project) / "current.json") or {}
    return pointer.get("fingerprint")


def _stamp(path):
    if not path:
        return None
    try:
        stat = os.stat(path)
        return f"{stat.st_size}-{stat.st_mtime_ns}"
    except OSError:
        return None


def mask_identity(record) -> dict:
    seg = record.segmentation
    binding = record.resources.get("segmentation")
    if binding is not None:
        where = f"node://{binding.node}/{binding.resource_id}"
        stamp = None
    else:
        where = str(seg.derived) if seg.derived else None
        stamp = _stamp(seg.derived)
    return {"where": where, "stamp": stamp, "scale": int(getattr(seg, "scale", 1) or 1),
            "mode": getattr(seg, "mode", None)}
