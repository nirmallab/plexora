"""One Scimap Pro run at a time, in order, on one thread.

Scimap Pro's kernels are numba `parallel=True` and numba's thread pool is not
re-entrant: two analyses in one process corrupt each other. So every run --
a Run button, Run all, an agent's call -- is a job in this one FIFO queue, run
by one long-lived worker thread. The page shows what a step is waiting behind.

The thread is also what makes runs fast after the first:

* **Settings once, on this thread.** `scimappro.settings.override` is a
  contextvar, so it is applied inside the worker -- every core the licence
  allows (`maxWorkers`), progress bars and chatter off.
* **Warm before it is needed.** Scimap Pro's first UMAP spends ~10 s
  compiling numba kernels. `warm()` -- called when a notebook page opens --
  imports Scimap Pro and runs a tiny clustering and embedding on synthetic
  cells, so that cost is paid while the user is still reading the page.
* **Matplotlib never draws a window.** `Agg` is selected before Scimap Pro is
  imported; plots are drawn by the page from returned data.

Cancelling a queued job removes it. Cancelling a running one is cooperative:
it stops at the next stage boundary (`check()`), and a result already written
out of core is dropped again, so a cancelled step leaves nothing behind.
"""

from __future__ import annotations

import collections
import logging
import os
import secrets
import threading
import time
import traceback

from plexora.plugins.scimap.server.adapter import RunError

_log = logging.getLogger(__name__)


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, kind, work, *, notebook_id=None, cell_id=None, label=""):
        self.id = "job_" + secrets.token_hex(6)
        self.kind = kind
        self.work = work
        self.notebook_id = notebook_id
        self.cell_id = cell_id
        self.label = label
        self.status = "queued"
        self.stage = ""
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None
        self.error = None
        self.result = None
        self.cancel_requested = False
        self.done = threading.Event()

    def say(self, stage):
        """A stage boundary: record what is happening, and stop here if asked."""
        self.stage = str(stage)
        self.check()

    def check(self):
        if self.cancel_requested:
            raise Cancelled()

    def to_dict(self, ahead=0):
        return {"id": self.id, "kind": self.kind, "status": self.status, "stage": self.stage,
                "notebook_id": self.notebook_id, "cell_id": self.cell_id, "label": self.label,
                "ahead": ahead, "created_at": self.created_at, "started_at": self.started_at,
                "finished_at": self.finished_at, "error": self.error}


_cond = threading.Condition()
_queue: collections.deque = collections.deque()
_jobs: dict = {}
_current: list = [None]
_thread: list = [None]
_warmed = threading.Event()
_warm_queued = [False]
_FINISHED_KEEP = 200


def _ensure_thread():
    with _cond:
        if _thread[0] is not None and _thread[0].is_alive():
            return
        thread = threading.Thread(target=_loop, name="scimap-runner", daemon=True)
        _thread[0] = thread
        thread.start()


def submit(job: Job, front: bool = False) -> Job:
    """Queue `job`. `front` puts it next in line, for the short jobs a page is
    waiting on (drawing a figure), never ahead of the one already running."""
    _ensure_thread()
    with _cond:
        _jobs[job.id] = job
        if front:
            _queue.appendleft(job)
        else:
            _queue.append(job)
        _prune()
        _cond.notify_all()
    return job


def _prune():
    finished = [j for j in _jobs.values() if j.status in ("done", "failed", "cancelled")]
    if len(finished) > _FINISHED_KEEP:
        for job in sorted(finished, key=lambda j: j.finished_at or 0)[:len(finished) - _FINISHED_KEEP]:
            _jobs.pop(job.id, None)


def get(job_id):
    with _cond:
        job = _jobs.get(job_id)
        if job is None:
            return None
        return job.to_dict(ahead=_ahead(job))


def _ahead(job):
    if job.status != "queued":
        return 0
    position = 0
    if _current[0] is not None:
        position += 1
    for queued in _queue:
        if queued is job:
            break
        if queued.kind != "warm":
            position += 1
    return position


def active(notebook_id=None) -> list:
    with _cond:
        out = []
        for job in list(_queue) + ([_current[0]] if _current[0] else []):
            if job.kind == "warm":
                continue
            if notebook_id and job.notebook_id != notebook_id:
                continue
            out.append(job.to_dict(ahead=_ahead(job)))
        return out


def find(notebook_id, cell_id):
    """The queued or running job of one step, or None."""
    with _cond:
        for job in list(_queue) + ([_current[0]] if _current[0] else []):
            if job.notebook_id == notebook_id and job.cell_id == cell_id and job.kind != "warm":
                return job
    return None


def cancel(job_id) -> bool:
    with _cond:
        job = _jobs.get(job_id)
        if job is None or job.status in ("done", "failed", "cancelled"):
            return False
        job.cancel_requested = True
        if job.status == "queued" and job in _queue:
            _queue.remove(job)
            job.status = "cancelled"
            job.finished_at = time.time()
            job.done.set()
        return True


def wait(job_id, timeout=None):
    job = _jobs.get(job_id)
    if job is not None:
        job.done.wait(timeout)
    return get(job_id)


def _loop():
    configured = False
    while True:
        with _cond:
            while not _queue:
                _cond.wait()
            job = _queue.popleft()
            if job.cancel_requested:
                job.status = "cancelled"
                job.finished_at = time.time()
                job.done.set()
                continue
            _current[0] = job
            job.status = "running"
            job.started_at = time.time()
        try:
            if not configured:
                _configure_thread()
                configured = True
            with _settings():
                job.result = job.work(job)
            job.status = "done"
        except Cancelled:
            job.status = "cancelled"
        except RunError as exc:
            job.status = "failed"
            job.error = exc.to_dict()
        except Exception as exc:  # noqa: BLE001 -- a run's failure is the page's to show
            _log.exception("SCIMAP run failed")
            job.status = "failed"
            job.error = {"code": "internal_error", "message": _plain(exc),
                         "detail": {"traceback": traceback.format_exc()[-4000:]}}
        finally:
            job.finished_at = time.time()
            with _cond:
                _current[0] = None
            job.done.set()


def _plain(exc):
    text = str(exc).strip() or type(exc).__name__
    return text.splitlines()[0][:500]


def _configure_thread():
    """Once per worker: a non-interactive matplotlib, before Scimap Pro."""
    os.environ.setdefault("MPLBACKEND", "Agg")
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
    except Exception:  # noqa: BLE001 -- no matplotlib, no plots; runs still work
        pass


class _settings:
    """`scimappro.settings.override(...)` for one job, or nothing before
    Scimap Pro has been imported by anything (Prepare imports it itself)."""

    def __enter__(self):
        import sys

        self._cm = None
        sm = sys.modules.get("scimappro")
        if sm is None:
            try:
                from plexora.plugins.scimap.server.adapter import scimappro

                sm = scimappro()
            except RunError:
                return self
        from plexora.plugins.scimap.server import licence

        cap = licence.max_workers()
        cores = max(1, (os.cpu_count() or 2) - 1)
        workers = min(cores, cap) if cap else cores
        try:
            self._cm = sm.settings.override(maxWorkers=workers, progress=False, verbose=False)
            self._cm.__enter__()
        except Exception:  # noqa: BLE001 -- an older settings API: defaults stand
            self._cm = None
        return self

    def __exit__(self, *exc):
        if self._cm is not None:
            self._cm.__exit__(*exc)
        return False


def warm():
    """Pay Scimap Pro's import and numba compilation once, early, in the
    background. Idempotent; a no-op when Scimap Pro is not installed."""
    from plexora.plugins.scimap.server import install

    if _warmed.is_set() or _warm_queued[0] or not install.probe()["compatible"]:
        return
    _warm_queued[0] = True
    submit(Job("warm", _warm_work, label="Getting Scimap Pro ready"))


def _warm_work(job):
    import numpy as np

    from plexora.plugins.scimap.server.adapter import scimappro

    started = time.time()
    sm = scimappro()
    try:
        import anndata as ad
        import pandas as pd

        rng = np.random.default_rng(0)
        n = 400
        X = np.log1p(rng.gamma(2.0, 40.0, (n, 6))).astype("float32")
        obs = pd.DataFrame({"imageid": ["warm"] * n, "X_centroid": rng.uniform(0, 500, n),
                            "Y_centroid": rng.uniform(0, 500, n)},
                           index=[f"c{i}" for i in range(n)])
        adata = ad.AnnData(X=X, obs=obs)
        adata.raw = adata
        adata.var_names = [f"m{i}" for i in range(6)]
        sm.tl.cluster(adata, method="leiden", label="w", verbose=False, progress=False)
        sm.tl.umap(adata, randomState=0, label="wu", verbose=False, progress=False)
        sm.sp.spatialDistance(adata, phenotype="w", verbose=False, progress=False)
    except Exception:  # noqa: BLE001 -- warming is an optimisation, never an error
        _log.debug("SCIMAP warm-up incomplete", exc_info=True)
    _warmed.set()
    return {"seconds": round(time.time() - started, 2)}


def warmed() -> bool:
    return _warmed.is_set()
