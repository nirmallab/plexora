"""Which plane of a z-stack QC's checks read.

Every QC check reads one 2-D picture of the image. On a z-stack that is a
plane, and which one decides what is found: the stack's default (middle)
plane unless the user chose another (`set_qc_plane`, the panel's "Check the
plane on screen"). The choice is the project's, kept beside its QC results,
and it is part of every check's fingerprint (`identity`) -- so a result on
plane 12 is never served for plane 20, and changing the plane asks every
check to run again. A flat image has no choice and its fingerprints are what
they always were.

QC never reads a projection: a maximum over planes is not a picture any
check's thresholds were made for (the stack checks, capabilities_stack.py,
compare planes instead).
"""

from __future__ import annotations

import json

RECORD = "qc_plane_v1.json"


def _path(project):
    from plexora import paths

    return paths.project_state_dir(project) / RECORD


def view_of(project) -> str:
    """The plane QC reads as a view token ("t2z12", "z12"), or "" for the
    image's default view (every flat image, and a stack nobody chose for)."""
    path = _path(project)
    if not path.exists():
        return ""
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("view") or "")
    except (OSError, ValueError):
        return ""


def set_view(project, z=None, t=0) -> str:
    """Choose the plane (`z`, frame `t`); None goes back to the default plane.
    Returns the token now in force."""
    path = _path(project)
    token = "" if z is None else (f"t{int(t)}z{int(z)}" if t else f"z{int(z)}")
    if not token:
        if path.exists():
            path.unlink()
        return ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"view": token}), encoding="utf-8")
    return token


def identity(image_data) -> str | None:
    """What a check's fingerprint calls the image: the file, and on a z-stack
    the plane read."""
    from plexora.server.utils import source_image

    base = source_image.ReaderShelf.identity_of(image_data)
    token = view_of(image_data.name)
    return f"{base}|{token}" if token and base is not None else base


def reader(image_data):
    """The open reader for QC, at the plane chosen (`view_of`)."""
    from plexora.server.utils import source_image

    token = view_of(image_data.name)
    return source_image.SHELF.reader(image_data, view=token or None)
