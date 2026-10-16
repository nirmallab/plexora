"""The display calibration offered to the viewer's own channels.

`calibration` decides how bright a marker is drawn for automatic gating: an
asinh curve anchored on the marker's own cells, black under the background,
the bright cells' structure near the top (and, after a gating run, the column
a vision check chose). The viewer offers that window on its canvas, a touch
brighter, and the nuclear stain measured on its own nuclei, a little dimmer
than the markers (`calibration.view_spec`): applied to the channels nobody set
(the viewer's own auto-levelling, a seeded default, an earlier calibration);
on the rest, a channel a contrast check settled (`checked`) takes it from
the Auto wand, which otherwise fits as ever.

Two calls, both cheap to the caller:

- `describe(project)`: what the project's stored record holds now, for the
  markers it anchored on their cells and the nuclear stain -- {state, rev,
  channels: {name: {window, curve, linear_window, chosen_by, titre,
  checked}}}. A record anchored on another table or other QC exclusions is
  not offered.
- `compute(project, channels)`: calibrate those channels in the background,
  one job per project at a time; `describe` says `computing` meanwhile.

A project with no cell table, or no image to calibrate, is `unavailable`:
there is nothing for the cells to anchor on, and the viewer's own
auto-levelling stands.
"""

from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
#: project -> {state: computing|failed, started, finished?, error?, channels}
_JOBS: dict = {}
_SESSION = None
#: A failed calibration is not retried for this long (seconds): a broken
#: image would otherwise be read again on every channel switched on.
RETRY_AFTER_S = 300.0


def _session():
    global _SESSION
    from plexora.agent import AgentSession

    if _SESSION is None:
        _SESSION = AgentSession()
    return _SESSION


def _unavailable(session, project):
    """Why `project` cannot be calibrated for the viewer, or None."""
    try:
        record = session.project(project)
    except Exception as exc:
        return f"no such project: {exc}"
    if record.image.is_blank:
        return "this sample has no image"
    if not getattr(record, "has_table", False):
        return "no cell table: the contrast is anchored on the cells"
    return None


def entries(record) -> dict:
    """{channel: {window, curve, linear_window, chosen_by, titre, checked}} for the
    markers a record anchored on their cells, and the nuclear stain measured
    on its own nuclei, at the windows a canvas is offered (`calibration.
    view_spec`); overview-only channels are left to the viewer's own
    levelling."""
    from plexora.agent.evidence import calibration

    choices = (record or {}).get("choices") or {}
    out = {}
    for name, entry in ((record or {}).get("channels") or {}).items():
        spec = calibration.view_spec(entry)
        if spec is None:
            continue
        nuclear = entry.get("role") == "nuclear"
        anchors = (entry.get("view") or {}).get("anchors") if nuclear else entry.get("anchors")
        out[name] = {"window": spec["window"],
                     "curve": calibration.viewer_curve(spec["curve"]),
                     # The same channel for a viewer drawing Linear.
                     "linear_window": spec["linear_window"],
                     "chosen_by": spec["chosen_by"],
                     "titre": (anchors or {}).get("titre"),
                     # A contrast check (an AI auto-contrast run, or gating's)
                     # settled it: the viewer's Auto wand applies it then. The
                     # nuclear stain is settled with its image's markers.
                     "checked": bool(choices) if nuclear else name in choices}
    return out


def describe(project, session=None) -> dict:
    """{state: ready|computing|none|unavailable, rev, channels, calibrated,
    reason?}. `calibrated` names every channel the record holds, offered or
    not (an overview-only channel is calibrated and never offered), so the
    viewer does not ask for it again. While a job runs the stored record is read
    as it is -- its table and QC fingerprints are not checked, which would
    read the table the job is reading."""
    from plexora.agent.evidence import calibration

    session = session or _session()
    with _LOCK:
        job = dict(_JOBS.get(project) or {})
    why = _unavailable(session, project)
    if why:
        return {"state": "unavailable", "rev": None, "channels": {}, "calibrated": [],
                "reason": why}
    computing = job.get("state") == "computing"
    try:
        record = calibration.load(project) if computing             else calibration.load_current(session, project)
    except Exception:
        record = None
    channels = entries(record)
    calibrated = sorted((record or {}).get("channels") or {})
    rev = calibration.revision(project) if record else None
    if computing:
        state = "computing"
    elif job.get("state") == "failed" and not channels:
        return {"state": "unavailable", "rev": rev, "channels": {}, "calibrated": calibrated,
                "reason": job.get("error") or "the calibration failed"}
    else:
        state = "ready" if channels else "none"
    return {"state": state, "rev": rev, "channels": channels, "calibrated": calibrated}


def compute(project, channels=None, session=None, *, wait=False) -> dict:
    """Start calibrating `channels` (default every channel) in the background;
    returns `describe` as it stands. One job per project: channels asked for
    while one runs are queued on it and calibrated when it finishes. `wait`
    runs it here (tests)."""
    session = session or _session()
    why = _unavailable(session, project)
    if why:
        return {"state": "unavailable", "rev": None, "channels": {}, "reason": why}
    wanted = [str(c) for c in (channels or []) if c]
    with _LOCK:
        job = _JOBS.get(project)
        if job and job.get("state") == "computing":
            queued = job.setdefault("queued", [])
            queued.extend(c for c in wanted if c not in queued)
            if not wanted:
                job["everything"] = True
            return describe(project, session)
        if job and job.get("state") == "failed" \
                and time.time() - float(job.get("finished") or 0) < RETRY_AFTER_S:
            return describe(project, session)
        _JOBS[project] = {"state": "computing", "started": time.time(), "channels": wanted}

    def run():
        from plexora.agent.evidence import calibration

        try:
            names = _known(session, project, wanted)
            calibration.calibrate(session, project, names or None)
            while True:
                with _LOCK:
                    job = _JOBS.get(project) or {}
                    queued, everything = job.pop("queued", []), job.pop("everything", False)
                    if not queued and not everything:
                        _JOBS.pop(project, None)
                        break
                names = None if everything else _known(session, project, queued)
                if names == []:
                    continue          # nothing the image has
                calibration.calibrate(session, project, names)
        except Exception as exc:  # the viewer keeps its own levelling
            with _LOCK:
                _JOBS[project] = {"state": "failed", "finished": time.time(),
                                  "error": str(getattr(exc, "message", None) or exc)[:300]}

    if wait:
        run()
    else:
        threading.Thread(target=run, name=f"display-contrast:{project}", daemon=True).start()
    return describe(project, session)


def _known(session, project, wanted):
    """The names in `wanted` the image has (by name or fullname)."""
    if not wanted:
        return []
    record = session.project(project)
    names = set()
    for channel in record.image.real_channels:
        names.update(n for n in (channel.get("fullname"), channel.get("name")) if n)
    return [n for n in wanted if n in names]


def reset():
    """Forget every job (tests)."""
    with _LOCK:
        _JOBS.clear()
