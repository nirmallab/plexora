"""Running an export without holding the request open.

A four-panel figure at 300 DPI is a second or two; an eighteen-panel one at 600
is minutes, and a browser gives up on a request long before that. So an export
is a job: POST starts it, GET follows it, DELETE cancels it, and a fourth route
hands back the file.

Threads rather than processes. The work is numpy and Pillow -- both release the
GIL for the parts that take the time -- and a process would have to re-open
every source image and re-read the config in a fresh interpreter. Plexora serves
on waitress with several threads, so one long job does not block the viewer.

**The document is frozen at the moment the job starts.** A figure edited while
an export is running must not produce a file that is half of one layout and half
of another (spec §73), and the user going on working is the ordinary case rather
than the exceptional one. The job holds its own copy.

Jobs are process-local and are not persisted: this is a desktop application, the
server and the browser live and die together, and a job that survived a restart
would be a job whose output nobody is waiting for.
"""

from __future__ import annotations

import shutil
import threading
import uuid
from datetime import datetime, timezone

from plexora.plugins.figure_builder.server import export, repository

#: Finished jobs kept before the oldest is swept. Each holds a document copy
#: and a path, so this is kilobytes -- the sweep is about the files on disk.
MAX_JOBS = 20

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def start(figure_id, document, options):
    """Begin an export. Returns the job id immediately."""
    job_id = "job_" + uuid.uuid4().hex[:12]
    cancel = threading.Event()
    job = {
        "job_id": job_id,
        "figure_id": figure_id,
        "status": "running",
        "progress": {"done": 0, "total": 1, "message": "Validating sources"},
        "started_at": _now(),
        "finished_at": None,
        "result": None,
        "error": None,
        "_cancel": cancel,
    }
    with _LOCK:
        _sweep()
        _JOBS[job_id] = job

    thread = threading.Thread(
        target=_run, args=(job, document, options), name=f"figure-export-{job_id}", daemon=True)
    thread.start()
    return job_id


def _run(job, document, options):
    def progress(done, total, message):
        job["progress"] = {"done": done, "total": total, "message": message}

    out_dir = repository.figure_dir(job["figure_id"]) / "exports" / job["job_id"]
    try:
        result = export.export(document, out_dir, options,
                               progress=progress, cancelled=job["_cancel"].is_set)
        if result.get("cancelled"):
            job["status"] = "cancelled"
            # Nothing half-written is left behind: a partial PDF in the
            # downloads folder is worse than no PDF, because it looks finished.
            shutil.rmtree(out_dir, ignore_errors=True)
        else:
            job["status"] = "done"
            job["result"] = result
    except export.ExportUnavailable as exc:
        job["status"] = "unavailable"
        job["error"] = str(exc)
    except Exception as exc:   # noqa: BLE001 - a job must never take the server with it
        job["status"] = "failed"
        job["error"] = str(exc)
        shutil.rmtree(out_dir, ignore_errors=True)
    finally:
        job["finished_at"] = _now()


def get(job_id):
    with _LOCK:
        job = _JOBS.get(job_id)
    return None if job is None else describe(job)


def describe(job):
    return {key: value for key, value in job.items() if not key.startswith("_")}


def cancel(job_id):
    """Ask a job to stop. Returns whether there was one to ask.

    Checked between panels, so cancelling a job rendering a large panel takes
    until that panel is done. That is the honest trade: the alternative is
    checking inside the numpy work, which costs every export a little to make
    one rare one end sooner.
    """
    with _LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        return False
    job["_cancel"].set()
    return True


def download_path(job_id):
    """The file a finished job produced, or None."""
    with _LOCK:
        job = _JOBS.get(job_id)
    if job is None or job["status"] != "done" or not job["result"]:
        return None
    return job["result"].get("download")


def _sweep():
    """Drop the oldest finished jobs, and their files with them.

    Called under the lock at start time rather than on a timer: the only moment
    the count can grow is when one begins, and a background sweeper on a desktop
    app is a thread that exists to do nothing.
    """
    finished = [job for job in _JOBS.values() if job["status"] != "running"]
    if len(_JOBS) <= MAX_JOBS:
        return
    finished.sort(key=lambda job: job["finished_at"] or "")
    for job in finished[:max(0, len(_JOBS) - MAX_JOBS)]:
        _JOBS.pop(job["job_id"], None)
        try:
            shutil.rmtree(repository.figure_dir(job["figure_id"]) / "exports" / job["job_id"],
                          ignore_errors=True)
        except ValueError:
            pass
