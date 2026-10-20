"""What gating sessions decided, and what people did with it afterwards.

The learning across runs is a person's, never the code's: this file is the
record a person reads to decide what to change. Each finished gating session
appends one `decision` row per marker it handled -- the versions it ran on,
where the scored start and the final gate were, how it got there (tier,
rounds, code moves), what its classifier layer did -- and later events on
those markers append `correction` rows: a gate an agent moved after the
session, a layer adopted, reverted or discarded, a gate approved or locked, a
session rolled back. What the user did by hand in the sidebar is read off
the project when the record is read (`observe`): the gate stored now against
the one the session wrote, and the labels the user clicked since.

Nothing here reads the rows back into a decision. `plexora ai bench
experience` (plexora/ai/bench_experience.py) turns them into ledger lines;
a change they argue for becomes a `[cal]` constant or a markers.yaml caveat
by hand, with a VERSION bump of the module it lives in and bench evidence on
the frozen label sets -- and rows carry those versions, so a later reader can
leave out the runs of a superseded version.

**What a row holds**: names, counts, gates and versions. Never a cell id, a
value per cell, an image or free text a model wrote.

One JSON line per row, appended under a lock: a lab's synced folder can
merge or truncate a file, and a cut-off last line is skipped on read.
`PLEXORA_EXPERIENCE_PATH` names a folder to keep the files in instead of the
data root's (a lab's shared one).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

SCHEMA_VERSION = 1
ENV_PATH = "PLEXORA_EXPERIENCE_PATH"
ROWS = ("decision", "correction")
#: What a correction row can say happened.
CORRECTIONS = ("gate_moved", "classifier_adopted", "classifier_reverted",
               "classifier_discarded", "status_set", "rolled_back", "committed")

_LOCK = threading.Lock()


def _safe(name):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(name)) or "_"


def folder() -> Path:
    configured = os.environ.get(ENV_PATH, "").strip()
    if configured:
        return Path(configured).expanduser()
    from plexora import paths

    return paths.agent_root() / "gating" / "experience"


def path(project) -> Path:
    return folder() / f"{_safe(project)}.jsonl"


def _now():
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def append(project, row) -> None:
    """One row; never raises (the record is a record, never a reason for a
    gate write or a session's end to fail)."""
    try:
        row = {"schema": SCHEMA_VERSION, "at": _now(), "project": project, **row}
        target = path(project)
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, sort_keys=True, default=_plain) + "\n"
        with _LOCK, target.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:  # noqa: BLE001
        pass


def _plain(value):
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
    except Exception:  # noqa: BLE001
        pass
    return str(value)


def read(project=None) -> list:
    """Every row (of one project, or of every file in the folder)."""
    files = [path(project)] if project else sorted(folder().glob("*.jsonl"))
    out = []
    for target in files:
        if not target.is_file():
            continue
        with _LOCK:
            text = target.read_text(encoding="utf-8")
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue    # a line cut off by a sync
            if isinstance(row, dict) and row.get("row") in ROWS:
                out.append(row)
    return out


# -- writers -----------------------------------------------------------------------------


def versions(gating=()) -> dict:
    """The versions of everything a decision stood on: automatic gating's
    (passed in by it -- this half never imports it) and the classifier's,
    so a later reader can leave out a superseded one."""
    out = {"gating": [str(v) for v in gating]} if gating else {}
    try:
        from plexora.plugins.gating.server.classifier import runs

        out["classifier"] = runs.versions()
    except Exception:  # noqa: BLE001
        pass
    return out


def _start_low(unit):
    start = unit.get("start") or {}
    if isinstance(start, dict):
        return start.get("low")
    return start


def decision_row(record, unit, action, gating_versions=()) -> dict:
    """One marker of a finished session, as a decision row."""
    layer = unit.get("classifier") or {}
    compare = layer.get("compare") or {}
    checked = unit.get("classifier_check") or {}
    context = unit.get("context") or {}
    return {
        "row": "decision", "session_id": record.get("session_id"), "action": action,
        "marker": unit.get("marker"), "scope": record.get("scope"),
        "dataset": record.get("dataset"),
        "agent": (record.get("options") or {}).get("agent"),
        "mode": (record.get("options") or {}).get("mode"),
        "classifier_mode": (record.get("options") or {}).get("classifier"),
        "versions": versions(gating_versions),
        "role": context.get("role"), "compartment": context.get("compartment"),
        "class": unit.get("class"), "gmm": unit.get("gmm"), "start": _start_low(unit),
        "final": unit.get("final"), "proposed": unit.get("proposed"),
        "state": unit.get("state"), "confidence": unit.get("confidence"),
        "method": unit.get("method"), "tier": unit.get("tier"), "path": unit.get("path"),
        "rounds": int(unit.get("rounds") or 0), "looks": len(unit.get("packets") or ()),
        "code_moves": list(unit.get("code_moves") or ()),
        "flags": sorted({str(f) for f in unit.get("flags") or ()}),
        "needs_review": bool(unit.get("needs_review")),
        "user_edited": bool(unit.get("user_edited")),
        "written": unit.get("written"),
        "classifier": ({"status": layer.get("status"), "why": layer.get("why"),
                        "grade": (layer.get("snr") or {}).get("grade"),
                        "d": (layer.get("snr") or {}).get("d"),
                        "gate_used": layer.get("gate_used"),
                        "n_added": compare.get("n_added"), "n_removed": compare.get("n_removed"),
                        "n_raw_added": compare.get("n_raw_added"),
                        "n_raw_removed": compare.get("n_raw_removed"),
                        "n_gate_positive": compare.get("n_gate_positive"),
                        "run_id": layer.get("run_id")} if layer else None),
        "check": ({"direction": checked.get("direction"), "from": checked.get("at"),
                   "n_flagged": checked.get("n_flagged"), "share": checked.get("share"),
                   "confirmed": checked.get("confirmed"), "rows": len(checked.get("rows") or ())}
                  if checked else None),
        "check_skipped": unit.get("classifier_check_skipped"),
    }


def after_finish(record, action, gating_versions=()) -> int:
    """A session closed, committed or rolled back: a decision row per marker
    it decided (per image); a rollback adds a correction row each. How many."""
    skipped = ("skipped_no_marker", "skipped_no_channel", "skipped_locked",
               "skipped_excluded", "display_set")
    n = 0
    for unit in (record.get("units") or {}).values():
        if unit.get("state") in skipped or not unit.get("project"):
            continue
        append(unit["project"], decision_row(record, unit, action, gating_versions))
        n += 1
        if action == "rollback":
            correction(unit["project"], unit.get("marker"), "rolled_back",
                       session_id=record.get("session_id"))
    return n


def correction(project, marker, what, *, session_id=None, by=None, **detail) -> None:
    """Something done to a marker a session decided (`CORRECTIONS`)."""
    if what not in CORRECTIONS:
        raise ValueError(f"unknown correction {what!r}")
    append(project, {"row": "correction", "what": what, "marker": marker,
                     "session_id": session_id, "by": by, **detail})


def session_of(project, marker) -> str | None:
    """The gating session that wrote a marker's current gate, by its
    provenance, or None."""
    try:
        from plexora.plugins.gating.server import provenance

        row = provenance.read(project).get(marker) or {}
        return row.get("session_id") or None
    except Exception:  # noqa: BLE001
        return None


def after_gate_write(project, marker, before, after, *, by) -> None:
    """A gate set after a session wrote it (an agent's `set_gate`)."""
    session_id = session_of(project, marker)
    if not session_id or before == after:
        return
    correction(project, marker, "gate_moved", session_id=session_id, by=by,
               before=before, after=after)


# -- what the project says now -------------------------------------------------------


def observe(ds, marker) -> dict:
    """The marker as it stands: its gate, its provenance's method and
    session, whether the stored gate differs from what was written, and how
    many labels the user clicked, by origin -- what a correction the sidebar
    made leaves behind."""
    from plexora.plugins.gating.server import model, provenance
    from plexora.plugins.gating.server.classifier import store

    gate = model.get_gate(ds, marker) or {}
    row = provenance.read(ds.name).get(marker) or {}
    origins = {}
    for origin in store.origins(ds.name, marker).values():
        origins[origin] = origins.get(origin, 0) + 1
    entry = store.read_state(ds.name)["markers"].get(marker) or {}
    return {"low": gate.get("low"), "high": gate.get("high"),
            "method": row.get("method"), "status": row.get("status"),
            "session_id": row.get("session_id"),
            "edited_since": bool(provenance.edited_since(row, gate)) if row else None,
            "labels": origins,
            "classifier_active": entry.get("active") == "classifier" and bool(entry.get("run")),
            "classifier_origin": (entry.get("run") or {}).get("origin")}
