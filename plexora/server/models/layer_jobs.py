"""One progress vocabulary for everything a sample is still preparing.

Plexora already had two, and they did not agree. The segmentation pyramid
reports `{status, progress, stage, stage_label, message, error}` out of
`data_model.get_segmentation_job_status`, with contiguous percentage bands
(`SEGMENTATION_STAGES`) so the bar never sits still without saying why. The
transcripts plugin reported `{status, stage, done, total}` out of a dict of its
own, which no other surface could read and which a server restart lost
entirely. A third modality would have been a third.

So: one registry, one record shape, one status document per sample.

    GET /import/status?sample=demo
    {"layers": {"__mask__": {...}, "transcripts": {...}}, "pending": true}

**What builds a modality's artefact is still the modality's business.** Core
knows that a layer is `pending` and how to run a daemon thread with a staged
reporter; it does not know what a transcript file is. A plugin calls
`register_builder("transcripts", fn)` at blueprint creation and core calls it --
the same shape the plugin registry itself uses, and the reason the Xenium reader
stays out of a core build (see tests/test_plugin_boundary.py).

A layer whose modality nobody registered a builder for is not an error. It stays
`pending` with a message naming the plugin that would prepare it, which is
something a user can act on; the alternative -- refusing the import -- would
mean a core build could not even record that a Xenium run has transcripts in it.

Same concurrency model as everything else here: a daemon thread and a polled
status. No Celery, no Redis, no async.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import replace

from plexora.server.models.project import (MASK_LAYER_ID, Project)

#: modality -> `(fn(project, layer, stage, report), stages)`. Filled by plugins
#: at blueprint creation; empty in a build with no plugins installed, which is
#: a working state and not a degraded one.
_BUILDERS: dict[str, tuple] = {}

#: (sample, layer_id) -> record. In memory, like the segmentation job's: a
#: restart falls back to what the project record persists, which is `status`.
_jobs: dict[tuple, dict] = {}
_lock = threading.RLock()

#: The default bands, for a builder that names no stages of its own. Same shape
#: and same reasoning as SEGMENTATION_STAGES: a bar that leaves zero on the
#: first tick and always names what it is doing.
DEFAULT_STAGES = {
    "reading": (0, 35, "Reading the source file"),
    "preparing": (35, 55, "Preparing the data"),
    "building": (55, 97, "Building the layer"),
}


def register_builder(modality: str, builder, *, stages=None) -> None:
    """Say how a modality's drawable form is built.

    `builder(project, layer, stage, report)` runs on a daemon thread. `stage`
    and `report` are `_staged_reporter`'s pair -- `stage(key)` enters a band,
    `report(done, total)` moves within it -- so a plugin's progress reads
    exactly like the segmentation pyramid's without the plugin knowing how the
    bar is drawn.

    `stages` are that modality's own phases as percentage bands. Named by the
    plugin because only it knows that reading a 6 GB parquet is most of the
    work and tiling the rest; drawn by core, because a progress bar is not a
    thing every plugin should re-invent.

    Replacing an existing registration is allowed and is what reloading a
    plugin does; two plugins claiming one modality is a packaging mistake that
    would show up as whichever loaded last, and is not worth a failure mode.
    """
    if not modality or builder is None:
        raise ValueError("register_builder needs a modality and a callable.")
    _BUILDERS[str(modality)] = (builder, dict(stages) if stages else None)


def builder_for(modality):
    """`(fn, stages)` for a modality, or `(None, None)`."""
    return _BUILDERS.get(str(modality or ""), (None, None))


def _record(status, *, stage=None, stage_label=None, message=None,
            progress=0, error=None, install=None):
    return {
        "status": status,
        "progress": int(progress),
        "stage": stage,
        "stage_label": stage_label,
        "message": message,
        "error": error,
        # The install line for a dependency the build needs and this
        # environment has not got. Carried on the record rather than folded
        # into `error`, so the card can offer a command to copy rather than
        # showing a stack trace to somebody who cannot act on one.
        "install": install,
    }


def get(project_name, layer_id):
    with _lock:
        found = _jobs.get((project_name, layer_id))
        return dict(found) if found else None


def forget(project_name=None):
    """Drop job records. Everything when called with nothing -- for teardown
    and for the tests; nothing in the request path calls this."""
    with _lock:
        for key in [k for k in _jobs
                    if project_name is None or k[0] == project_name]:
            _jobs.pop(key, None)


def start(project_name, layer_id, work, *, stages=None, message=None):
    """Run `work(stage, report)` on a daemon thread, reporting as it goes.

    Returns the record the poller will see first, so the caller can hand it
    straight back from the request that started the job -- the browser begins
    polling the moment it posts, and an absent record would read as "finished".

    A layer already running is left alone and its record returned: two builds
    of the same layer would race for the same cache directory.
    """
    from plexora.server.models import data_model

    key = (project_name, layer_id)
    with _lock:
        running = _jobs.get(key)
        if running and running.get("status") == "pending":
            return dict(running)
        bands = stages or DEFAULT_STAGES
        first = next(iter(bands))
        _jobs[key] = _record("pending", stage=first,
                             stage_label=bands[first][2],
                             message=message or bands[first][2])

    bands = stages or DEFAULT_STAGES

    def on_change(percent, stage_key, text):
        with _lock:
            record = _jobs.get(key)
            if record is None:
                return
            record["progress"] = percent
            record["stage"] = stage_key
            record["stage_label"] = bands.get(stage_key, (0, 0, stage_key))[2]
            record["message"] = text

    def run():
        stage, report = data_model._staged_reporter(bands, on_change)
        try:
            project = Project.load(project_name)
            layer = project.layer(layer_id)
            if layer is None:
                raise KeyError(f"{project_name} has no layer {layer_id!r}")
            work(project, layer, stage, report)
            _finish(project_name, layer_id, "ready")
        except Exception as error:  # reported on the card, not swallowed
            # A dependency this environment has not got carries its own install
            # line, if the exception knows one. Duck-typed rather than imported
            # so core does not have to know which plugin raised it.
            _finish(project_name, layer_id, "failed", error=str(error),
                    install=getattr(type(error), "INSTALL", None))

    threading.Thread(target=run, name=f"layer-{project_name}-{layer_id}",
                     daemon=True).start()
    return get(project_name, layer_id)


def _finish(project_name, layer_id, status, *, error=None, install=None):
    """Record the outcome, in memory and on the project.

    Both, and that is the point of writing it twice: the in-memory record
    carries the message and the install line for the card that is on screen
    now, and the project record carries the bare status so a server that
    restarts tomorrow still knows this layer never finished building.
    """
    with _lock:
        _jobs[(project_name, layer_id)] = _record(
            status,
            progress=100 if status == "ready" else 0,
            stage="ready" if status == "ready" else "failed",
            stage_label="Ready" if status == "ready" else "Failed",
            message=None if status == "ready" else error,
            error=error, install=install)

    def change(project):
        layer = next((l for l in project.spatial_layers if l.id == layer_id), None)
        if layer is None or layer.status == status:
            return project
        return project.with_layer(replace(layer, status=status))

    # The project may have been deleted while the build ran -- `mutate` answers
    # None for that, which is the state the segmentation job already handles.
    Project.mutate(project_name, change)


# -- tasks that are not layers ---------------------------------------------
#
# Some background work belongs to a sample without being one of its layers:
# fetching a remote image's coarse levels into the chunk cache while the
# viewer opens, or bringing a whole store down to make it available offline.
# Same record shape, same thread naming (the test suite joins every `layer-*`
# thread at teardown), none of the layer bookkeeping -- a task has no project
# record to update and must never flip the sample's `pending` flag, because
# the viewer would then poll for something that is not a layer.

#: (sample, task_id) -> (thread, cancel event), for the tasks started below.
_tasks: dict[tuple, tuple] = {}


def start_task(project_name, task_id, work, *, stages=None, message=None,
               cancel=None):
    """Run `work(stage, report, cancel)` on a daemon thread.

    `cancel` is a `threading.Event` the work checks between steps; one is made
    when none is passed. A task already running is left alone and its record
    returned, exactly as `start` does for a layer.
    """
    from plexora.server.models import data_model

    key = (project_name, task_id)
    bands = stages or DEFAULT_STAGES
    with _lock:
        running = _jobs.get(key)
        if running and running.get("status") == "pending":
            return dict(running)
        first = next(iter(bands))
        _jobs[key] = _record("pending", stage=first,
                             stage_label=bands[first][2],
                             message=message or bands[first][2])
        cancel = cancel or threading.Event()

    def on_change(percent, stage_key, text):
        with _lock:
            record = _jobs.get(key)
            if record is None:
                return
            record["progress"] = percent
            record["stage"] = stage_key
            record["stage_label"] = bands.get(stage_key, (0, 0, stage_key))[2]
            record["message"] = text

    def run():
        stage, report = data_model._staged_reporter(bands, on_change)
        try:
            work(stage, report, cancel)
            outcome = _record(
                "cancelled" if cancel.is_set() else "ready",
                progress=0 if cancel.is_set() else 100,
                stage="cancelled" if cancel.is_set() else "ready",
                stage_label="Stopped" if cancel.is_set() else "Ready")
        except Exception as error:  # reported on the record, not swallowed
            outcome = _record("failed", stage="failed", stage_label="Failed",
                              message=str(error), error=str(error),
                              install=getattr(type(error), "INSTALL", None))
        with _lock:
            _jobs[key] = outcome
            _tasks.pop(key, None)

    thread = threading.Thread(target=run, name=f"layer-{project_name}-{task_id}",
                              daemon=True)
    with _lock:
        _tasks[key] = (thread, cancel)
        # Taken before the thread starts: a quick task can finish before this
        # returns, and the caller is owed the record the poller sees first.
        first_record = dict(_jobs[key])
    thread.start()
    return first_record


def stop_task(project_name, task_id, timeout=5.0) -> bool:
    """Ask a running task to stop, and wait up to `timeout` for it to.

    @returns True when nothing is running under that name any more.
    """
    with _lock:
        running = _tasks.get((project_name, task_id))
    if running is None:
        return True
    thread, cancel = running
    cancel.set()
    thread.join(timeout)
    return not thread.is_alive()


def tasks(project_name=None, prefix=None) -> dict:
    """{task_id: record} for tasks of one sample (or every sample)."""
    with _lock:
        return {
            key[1]: dict(record) for key, record in _jobs.items()
            if (project_name is None or key[0] == project_name)
            and (prefix is None or str(key[1]).startswith(prefix))}


def running_tasks(prefix=None) -> list[tuple]:
    """(sample, task_id) for every task still running."""
    with _lock:
        return [key for key in _tasks
                if prefix is None or str(key[1]).startswith(prefix)]


def start_builder(project, layer) -> bool:
    """Start whatever prepares this layer, if anything here knows how.

    @returns True when a build was started. False leaves the layer `pending`
        with a message naming what would prepare it -- which is the honest
        state of a Xenium run imported into a build with no transcripts plugin,
        and is strictly better than refusing the import.
    """
    builder, stages = builder_for(layer.modality)
    if builder is None:
        with _lock:
            _jobs[(project.name, layer.id)] = _record(
                "pending", stage="waiting", stage_label="Not prepared",
                message=(f"Install the {layer.modality} plugin to prepare "
                         f"this layer." if layer.modality
                         else "Nothing here knows how to prepare this layer."))
        return False
    start(project.name, layer.id,
          lambda p, l, stage, report: builder(p, l, stage, report),
          stages=stages)
    return True


def status(project_name):
    """One document covering every layer of one sample that is being prepared.

    Composed rather than stored, from the three places that already know:
    `data_model`'s segmentation job for the mask, its table job while a table is
    loading, and this module's records (falling back to the persisted
    `LayerSpec.status`) for everything else. One shape out, so the viewer polls
    one endpoint whichever of them started the work.
    """
    from plexora.server.models import data_model

    try:
        project = Project.load(project_name)
    except KeyError:
        return {"layers": {}, "pending": False}

    layers = {}

    mask = data_model.get_segmentation_job_status(project_name)
    if project.segmentation.requested:
        layers[MASK_LAYER_ID] = {
            "status": "ready" if mask.get("status") == "ready" else (
                "failed" if mask.get("status") == "error" else "pending"),
            "progress": mask.get("progress", 0),
            "stage": mask.get("stage"),
            "stage_label": mask.get("stage_label"),
            "message": mask.get("message"),
            "error": mask.get("error"),
            "install": None,
            # The path the viewer adopts the finished mask at, without a page
            # reload. Already served by the segmentation status route; carried
            # here so this document can replace that poll rather than join it.
            "segmentation": mask.get("segmentation"),
        }

    for layer in project.spatial_layers:
        live = get(project_name, layer.id)
        if live is None:
            # No record in this process. The project's own `status` is what a
            # restarted server knows, and for every layer that was never a job
            # it is `ready`, which is the truth.
            live = _record(layer.status,
                           progress=100 if layer.available else 0,
                           stage="ready" if layer.available else None,
                           stage_label="Ready" if layer.available else None)
        layers[layer.id] = live

    return {
        "layers": layers,
        # One boolean, so the client can decide whether to keep polling without
        # walking the document. The viewer starts its poll only when something
        # is pending and stops when nothing is -- which is what keeps an
        # ordinary project from paying for this at all.
        "pending": any(entry.get("status") == "pending"
                       for entry in layers.values()),
    }


# -- a registration in progress ------------------------------------------------
#
# `/import/sample` does its slow work BEFORE the project exists -- tiling the
# reference, converting a 10x matrix (minutes for a Visium HD run) -- and the
# records above are keyed by a sample that has no name yet. So the dialog
# names its own request with a token and polls `/import/status?token=` while
# the POST runs; each proposed layer is a row here, in the same record shape,
# so the rail it draws does not care which of the two documents it read.

#: token -> {layer_id: record}. Dropped when the registration returns.
_registrations: dict[str, dict] = {}
_active = threading.local()


@contextlib.contextmanager
def registering(token, layer_ids):
    """Record a registration's progress under `token` for as long as it runs.

    A falsy token records nothing -- the Python API and the CLI have nobody
    polling -- and every `registration_*` call below is then a no-op.
    """
    if not token:
        yield
        return
    token = str(token)
    with _lock:
        _registrations[token] = {
            layer_id: _record("pending", stage="waiting", stage_label="Waiting")
            for layer_id in layer_ids}
    previous = getattr(_active, "state", None)
    _active.state = (token, None)
    try:
        yield
    finally:
        _active.state = previous
        with _lock:
            _registrations.pop(token, None)


def registration_step(layer_id, label=None, *, status="pending", progress=0,
                      message=None):
    """Mark `layer_id` as the step now running.

    `status="ready"` finishes it; `status="waiting"` parks it with `message`
    (the rail's not-progress state -- a build that starts after the POST).
    """
    state = getattr(_active, "state", None)
    if not state:
        return
    token, _ = state
    with _lock:
        rows = _registrations.get(token)
        if rows is None or layer_id not in rows:
            return
        if status == "waiting":
            rows[layer_id] = _record("pending", stage="waiting",
                                     message=message)
            return
        rows[layer_id] = _record(
            status, stage="ready" if status == "ready" else "registering",
            stage_label=label or ("Ready" if status == "ready" else "Preparing"),
            progress=100 if status == "ready" else progress, message=message)
    if status == "pending":
        _active.state = (token, layer_id)


def registration_progress(done, total, label=None):
    """Move the running step's bar. Called from deep inside a conversion."""
    state = getattr(_active, "state", None)
    if not state or state[1] is None:
        return
    token, layer_id = state
    with _lock:
        row = (_registrations.get(token) or {}).get(layer_id)
        if row is None:
            return
        row["progress"] = int(100 * done / total) if total else 0
        if label:
            row["stage_label"] = label


def registration(token):
    """The document `/import/status?token=` answers, or None once it is done."""
    with _lock:
        rows = _registrations.get(str(token or ""))
        if rows is None:
            return None
        return {"layers": {k: dict(v) for k, v in rows.items()},
                "pending": True, "registering": True}
