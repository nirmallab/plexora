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

A project with no image to calibrate is `unavailable`, and so is one with no
cell table -- there is nothing for the cells to anchor on, and the viewer's
own auto-levelling stands -- until an auto-contrast run has anchored it on
cells found in its image (`calibration.calibrate(image_cells=True)`). The
viewer never starts that read itself: it reads fields of every channel.
"""

from __future__ import annotations

import threading
import time

from plexora import _work

_LOCK = threading.Lock()
#: project -> {state: computing|failed, started, finished?, error?, channels}
_JOBS: dict = {}
_SESSION = None
#: A failed calibration is not retried for this long (seconds): a broken
#: image would otherwise be read again on every channel switched on.
RETRY_AFTER_S = 300.0
#: On the website (one Python thread, no other: `_work.threaded()` False),
#: how many channels one step of a job calibrates before the page's requests
#: are answered again. A channel's cell crops and overview plane are a few
#: tenths of a second on a large slide; the whole job in one step (35
#: channels: 7 s on LSP11385) held the page, `/health` and every request
#: behind it.
STEP_CHANNELS = 4
#: The channels nobody has asked about yet (`_ahead`), calibrated one a step:
#: four held a click on another channel, and the channel list's polls, up to
#: 4 s behind a step of work nobody was waiting for.
AHEAD_CHANNELS = 1


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
    if not getattr(record, "has_table", False) and not _anchored_on_image(project):
        return "no cell table: the contrast is anchored on the cells"
    return None


def _anchored_on_image(project) -> bool:
    """Whether an auto-contrast run anchored `project`'s record on cells it
    found in the image."""
    from plexora.agent.evidence import calibration

    try:
        source = str((calibration.load(project) or {}).get("cells_from") or "")
    except Exception:
        return False
    return source.startswith("image:") and source != "image:none"


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
    # `describe` takes the lock itself, and a threading.Lock is not
    # re-entrant: it is called once the lock is let go. Called inside it, a
    # second ask while a job ran (a channel switched on as a project opens)
    # waited on itself for ever -- a request thread on the desktop, and on
    # the website the one Python thread there is: the whole site froze.
    with _LOCK:
        job = _JOBS.get(project)
        started = False
        if job and job.get("state") == "computing":
            queued = job.setdefault("queued", [])
            queued.extend(c for c in wanted if c not in queued)
            if not wanted:
                job["everything"] = True
        elif not (job and job.get("state") == "failed"
                  and time.time() - float(job.get("finished") or 0) < RETRY_AFTER_S):
            _JOBS[project] = {"state": "computing", "started": time.time(), "channels": wanted}
            started = True
    if not started:
        return describe(project, session)

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
    elif _work.threaded():
        _work.spawn(run, name=f"display-contrast:{project}")
    else:
        _work.spawn(_stepped, session, project, wanted, None,
                    name=f"display-contrast:{project}")
    return describe(project, session)


def _stepped(session, project, wanted, todo):
    """`compute`'s job on the website: `STEP_CHANNELS` channels a step,
    each step its own task, the page's requests answered between them (not
    background work: the viewer is waiting on it, polling, and background
    work waits for the requests to fall quiet), until every channel asked for
    and every one queued meanwhile is calibrated. `calibrate` merges each
    step's channels into the stored record, so the record at the end is the
    one a single call would have made. `todo` None: the first step, which
    resolves what was asked (nothing named: every channel) and reads what
    every calibration reads first (`calibration.prepare`), a step of its own."""
    from plexora.agent.evidence import calibration

    try:
        if todo is None:
            # As `run`: none of what was asked is the image's, every channel.
            todo = _known(session, project, wanted) or _channel_names(session, project)
            # What every calibration reads first, as a step of its own.
            calibration.prepare(session, project)
            _work.spawn(_stepped, session, project, wanted, todo,
                        name=f"display-contrast:{project}")
            return
        while not todo:
            with _LOCK:
                job = _JOBS.get(project) or {}
                queued, everything = job.pop("queued", []), job.pop("everything", False)
                if not queued and not everything:
                    _JOBS.pop(project, None)
                    start_ahead = project not in _AHEAD
                    _AHEAD.add(project)
                    break
            todo = _channel_names(session, project) if everything else _known(session, project, queued)
        if not todo:
            if start_ahead:
                _work.spawn(_ahead, session, project, name=f"display-contrast-ahead:{project}",
                            background=True)
            return
        calibration.calibrate(session, project, todo[:STEP_CHANNELS])
        _work.spawn(_stepped, session, project, wanted, todo[STEP_CHANNELS:],
                    name=f"display-contrast:{project}")
    except Exception as exc:  # the viewer keeps its own levelling
        with _LOCK:
            _JOBS[project] = {"state": "failed", "finished": time.time(),
                              "error": str(getattr(exc, "message", None) or exc)[:300]}


#: Projects whose channels the website calibrates ahead (`_ahead`), once a session.
_AHEAD: set = set()


def _ahead(session, project):
    """The website: the channels nobody has asked about yet calibrated ahead
    of their click, `AHEAD_CHANNELS` a background task -- run only once the
    page's requests have been quiet -- so a channel switched on later finds
    its calibration stored, as the desktop's thread makes it in about the
    time the viewer takes to ask. A job asked for meanwhile goes first (it is
    not background work), and this one stops when the project is no longer
    open or its record has every channel."""
    from plexora.agent.evidence import calibration

    try:
        if _unavailable(session, project):
            return
        with _LOCK:
            if (_JOBS.get(project) or {}).get("state") == "computing":
                # A job asked for runs first; this goes on after it.
                _work.spawn(_ahead, session, project, name=f"display-contrast-ahead:{project}",
                            background=True)
                return
        record = calibration.load(project) or {}
        done = set(record.get("channels") or {})
        todo = [name for name in _channel_names(session, project) if name not in done]
        if not todo:
            return
        calibration.calibrate(session, project, todo[:AHEAD_CHANNELS])
        if len(todo) > AHEAD_CHANNELS:
            _work.spawn(_ahead, session, project, name=f"display-contrast-ahead:{project}",
                        background=True)
    except Exception:  # noqa: BLE001 -- ahead of need: a failure here is nobody's
        return


def _channel_names(session, project):
    """Every real channel of the project's image, as `calibrate` names them."""
    return [c.get("fullname") or c.get("name")
            for c in session.project(project).image.real_channels]


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
