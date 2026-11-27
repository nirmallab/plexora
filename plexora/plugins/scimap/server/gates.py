"""What the notebook needs to know about the gates set in Plexora's gating.

The gates themselves reach the working file through Scimap Pro: Cell types
from gates calls `hl.syncFromViewer(what="gates")`, which reads them out of
this Plexora and writes `uns["gates"]` for `pp.rescale`. Nothing here reads
or converts cell values. This module only answers, from the gating plugin's
store, the questions the page asks before a run:

* whether any gates are set (the sidebar's availability);
* which markers have a stored gate in which project (the rules editor, and a
  refusal naming the marker before anything runs);
* which project each image of the working file is, and the scale its gates
  are on: Plexora stores a gate on the scale it read the table on, log1p when
  the project's log switch is on, so `pp.rescale` is told `log=` to match;
* one revision of every project's gates, so a result can say it is outdated.
"""

from __future__ import annotations

import hashlib


def _projects(document) -> list:
    return [str(p) for p in ((document.get("context") or {}).get("projects") or [])]


def _rows_of(project):
    """`(rows, logged, revision)` for one project, read from the store only."""
    from plexora import api
    from plexora.plugins.gating.server import model

    ds = api.project_data(project)
    rows = model.gate_rows(ds)
    logged = bool(getattr(ds.project, "log_transformed", False))
    return rows, logged, model.revision(ds)


def any_set(document) -> bool:
    """Whether any project of the notebook has stored gates."""
    for project in _projects(document):
        try:
            rows, _logged, _rev = _rows_of(project)
        except Exception:  # noqa: BLE001 -- a project that cannot be read has none
            continue
        if rows:
            return True
    return False


def sources(document, work) -> dict:
    """`{projects: {image: project}, gated: {image: set(markers)}, logged}`.

    An image of the working file is a project (Prepare names it so). Refuses
    a dataset whose projects mix log switches: one `pp.rescale` call reads its
    gates on one scale."""
    projects = _projects(document)
    images = [str(i) for i in work.get("images") or []]
    out = {"projects": {}, "gated": {}, "logged": None}
    flags = set()
    for image in images:
        project = image if image in projects else (projects[0] if len(projects) == 1 else image)
        try:
            rows, logged, _rev = _rows_of(project)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Could not read the gates of {project}: {exc}") from exc
        out["projects"][image] = project
        out["gated"][image] = {str(r.get("channel")) for r in rows if r.get("channel")}
        flags.add(logged)
    if len(flags) > 1:
        raise ValueError("The projects of this dataset were gated on different scales "
                         "(some with the log switch on); gate them on one scale first.")
    out["logged"] = bool(flags.pop()) if flags else False
    return out


def missing(source_of, markers) -> list:
    """`[(marker, image)]` with no stored gate."""
    return [(m, image) for m in markers for image, have in source_of["gated"].items() if m not in have]


def revision(document) -> str:
    """One digest of every project's stored gates (store only)."""
    revisions = []
    for project in _projects(document):
        try:
            _rows, _logged, rev = _rows_of(project)
        except Exception:  # noqa: BLE001
            rev = "?"
        revisions.append(f"{project}:{rev}")
    return hashlib.sha1("|".join(revisions).encode()).hexdigest()[:16]


def state(document) -> dict:
    """`{marker: [projects with a stored gate]}`, for the rules editor."""
    out = {}
    for project in _projects(document):
        try:
            rows, _logged, _rev = _rows_of(project)
        except Exception:  # noqa: BLE001
            continue
        for row in rows:
            channel = row.get("channel")
            if channel:
                out.setdefault(str(channel), []).append(project)
    return out
