"""Long work as jobs: submitted at once, run on a thread, reported until done.

A capability declared `execution="job"` does not run inside the call that
asked for it. `registry.invoke` validates it, checks its permissions and
requirements exactly as for any other call, and then hands it here: the call
returns `{job_id}` at once and the handler runs on its own daemon thread --
never inside a Waitress worker, whose pool is the viewer's. The handler reports
progress through `call.progress(done, total, message)` and stops when
`call.check_cancelled()` raises; its receipt and audit line are the ones any
write leaves.

Every job is a JSON record under `<data_root>/.agent/jobs/<job_id>.json`,
written at submit, as it progresses (throttled) and when it ends, so a
restarted MCP process can still say what became of a job -- and says
`interrupted` for one whose process died under it, rather than `running`
forever.

The jobs directory is resolved when a job is submitted and handed to its
thread: a thread that outlives a test must not resolve `data_root()` after the
test's root has gone (it would write into the developer's real install).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from plexora.agent.audit import now_iso
from plexora.agent.errors import as_agent_error

TERMINAL = ("done", "failed", "cancelled", "interrupted")
STATUSES = ("queued", "running", *TERMINAL)

#: Finished jobs kept in memory per store; their files stay on disk.
MAX_JOBS = 200

#: Progress is written to disk at most this often, unless its message changes.
PERSIST_INTERVAL_S = 0.5

_PRIVATE = ("_cancel", "_done", "_thread", "_folder", "_last_persist")


class JobCancelled(Exception):
    """Raised inside a job's handler by `call.check_cancelled()`."""


def new_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:12]}"


def _atomic_json(path: Path, record: dict):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(record, default=str, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def public(record: dict) -> dict:
    return {key: value for key, value in record.items() if key not in _PRIVATE}


class JobStore:
    """The jobs of one data root: in memory while this process runs them, on
    disk for as long as the user keeps the files."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._jobs: dict = {}
        self._lock = threading.Lock()
        self.recover()

    # -- persistence -----------------------------------------------------

    def _path(self, job_id: str) -> Path:
        return self.folder / f"{job_id}.json"

    def _persist(self, record: dict):
        folder = record.get("_folder") or self.folder
        try:
            Path(folder).mkdir(parents=True, exist_ok=True)
            _atomic_json(Path(folder) / f"{record['job_id']}.json", public(record))
        except OSError:  # pragma: no cover - a full disk must not kill the job
            pass
        record["_last_persist"] = time.monotonic()

    def _read(self, job_id: str) -> dict | None:
        path = self._path(job_id)
        if not job_id.startswith("job_") or not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def recover(self) -> list:
        """Mark on-disk jobs that were queued or running in a process that is
        not running them now as `interrupted`. Returns their ids."""
        if not self.folder.is_dir():
            return []
        marked = []
        for path in sorted(self.folder.glob("job_*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if record.get("status") not in ("queued", "running"):
                continue
            if record.get("job_id") in self._jobs:
                continue
            record["status"] = "interrupted"
            record["finished_at"] = now_iso()
            record["error"] = {"code": "resource_unavailable",
                               "message": f"the process running this job (pid "
                                          f"{record.get('pid')}) stopped before it finished",
                               "detail": None, "retryable": True}
            try:
                _atomic_json(path, record)
            except OSError:  # pragma: no cover
                continue
            marked.append(record["job_id"])
        return marked

    # -- running ---------------------------------------------------------

    def submit(self, call, inp) -> dict:
        capability = call.capability
        job_id = new_job_id()
        record = {
            "job_id": job_id, "operation_id": call.operation_id,
            "capability": capability.name, "tool": capability.tool_name,
            "project": call.project_name, "status": "queued",
            "progress": {"done": 0, "total": None, "message": "queued"},
            "submitted_at": now_iso(), "started_at": None, "finished_at": None,
            "result": None, "error": None, "pid": os.getpid(),
            "_cancel": threading.Event(), "_done": threading.Event(),
            "_folder": self.folder, "_last_persist": 0.0,
        }
        call.job = record
        call.store = self
        with self._lock:
            self._sweep()
            self._jobs[job_id] = record
        self._persist(record)
        if capability.permission != "read":
            line = {"status": "started", "operation_id": call.operation_id,
                    "capability": capability.name, "project": call.project_name,
                    "arguments": call.arguments, "job_id": job_id}
            if getattr(call.policy, "principal", None):
                line["principal"] = call.policy.principal
            call.audit.append(line)
        thread = threading.Thread(target=self._run, args=(record, call, inp),
                                  name=f"agent-job-{job_id}", daemon=True)
        record["_thread"] = thread
        thread.start()
        return {"job_id": job_id, "status": "queued", "resource": f"plexora://job/{job_id}",
                "operation_id": call.operation_id,
                "next": "job_wait (streams progress) or job_get"}

    def progress(self, record: dict, done=None, total=None, message=None):
        progress = dict(record["progress"])
        changed_message = message is not None and message != progress.get("message")
        if done is not None:
            progress["done"] = done
        if total is not None:
            progress["total"] = total
        if message is not None:
            progress["message"] = message
        record["progress"] = progress
        if changed_message or time.monotonic() - record["_last_persist"] >= PERSIST_INTERVAL_S:
            self._persist(record)

    def _run(self, record, call, inp):
        from pydantic import BaseModel

        from time import perf_counter

        capability = call.capability
        started = perf_counter()
        record["status"] = "running"
        record["started_at"] = now_iso()
        record["progress"] = {**record["progress"], "message": "running"}
        self._persist(record)
        try:
            if record["_cancel"].is_set():
                raise JobCancelled()
            # The grants the licence had when this job was admitted: a job that
            # started on a valid licence finishes, whatever the licence does
            # meanwhile (plexora/licensing/tokens.py).
            from plexora.licensing import tokens as license_tokens

            with license_tokens.admitted(call.extras.get("license_grants")):
                result = capability.handler(call, inp)
            if isinstance(result, BaseModel):
                result = result.model_dump(mode="json")
            if isinstance(result, dict):
                result = {key: value for key, value in result.items() if key != "_images"}
            record["result"] = result
            record["status"] = "done"
            record["progress"] = {**record["progress"], "message": "done"}
        except JobCancelled:
            record["status"] = "cancelled"
            record["progress"] = {**record["progress"], "message": "cancelled"}
            if capability.permission != "read":
                call.audit.append({"status": "cancelled", "operation_id": call.operation_id,
                                   "capability": capability.name,
                                   "project": call.project_name, "job_id": record["job_id"]})
        except Exception as exc:
            error = as_agent_error(exc)
            record["status"] = "failed"
            record["error"] = error.to_problem()
            if capability.permission != "read" and not call.receipted:
                call.audit.append({"status": "failed", "operation_id": call.operation_id,
                                   "capability": capability.name,
                                   "project": call.project_name,
                                   "arguments": call.arguments, "job_id": record["job_id"],
                                   "error": error.to_problem()})
        finally:
            record["finished_at"] = now_iso()
            self._persist(record)
            record["_done"].set()
            from plexora.telemetry.agent_hooks import job_finished

            job_finished(capability, record.get("status"), (perf_counter() - started) * 1000.0)

    # -- asking ----------------------------------------------------------

    def _record(self, job_id: str) -> dict | None:
        with self._lock:
            record = self._jobs.get(job_id)
        return record if record is not None else self._read(job_id)

    def get(self, job_id: str) -> dict | None:
        record = self._record(job_id)
        return None if record is None else public(record)

    def list(self, status=None, limit=50) -> list:
        seen = {}
        if self.folder.is_dir():
            for path in self.folder.glob("job_*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                seen[record.get("job_id")] = record
        with self._lock:
            for job_id, record in self._jobs.items():
                seen[job_id] = public(record)
        records = [r for r in seen.values() if status is None or r.get("status") == status]
        records.sort(key=lambda r: r.get("submitted_at") or "", reverse=True)
        return records[:limit]

    def cancel(self, job_id: str) -> tuple:
        """(before, after) status. A job in another process cannot be reached;
        a finished job is left as it is."""
        with self._lock:
            record = self._jobs.get(job_id)
        if record is None:
            stored = self._read(job_id)
            return (None, None) if stored is None else (stored["status"], stored["status"])
        before = record["status"]
        if before in TERMINAL:
            return before, before
        record["_cancel"].set()
        return before, "cancelling"

    def wait(self, job_id: str, timeout_s: float) -> dict | None:
        with self._lock:
            record = self._jobs.get(job_id)
        if record is None:
            return self.get(job_id)
        record["_done"].wait(timeout=max(0.0, float(timeout_s)))
        return public(record)

    def _sweep(self):
        finished = [job_id for job_id, record in self._jobs.items()
                    if record["status"] in TERMINAL]
        excess = len(self._jobs) - MAX_JOBS + 1
        for job_id in finished[:max(0, excess)]:
            self._jobs.pop(job_id, None)

    def threads(self) -> list:
        with self._lock:
            return [record["_thread"] for record in self._jobs.values()
                    if record.get("_thread") is not None]


_STORES: dict = {}
_STORES_LOCK = threading.Lock()


def folder() -> Path:
    from plexora import paths

    return paths.agent_root() / "jobs"


def store() -> JobStore:
    """The store for the current data root, made (and recovered) on first use."""
    where = folder()
    with _STORES_LOCK:
        found = _STORES.get(where)
        if found is None:
            found = _STORES[where] = JobStore(where)
        return found


def submit(call, inp) -> dict:
    return store().submit(call, inp)


def drain(timeout: float = 30.0) -> list:
    """Join every job thread this process started; returns those still alive."""
    deadline = time.monotonic() + timeout
    with _STORES_LOCK:
        stores = list(_STORES.values())
    alive = []
    for each in stores:
        for thread in each.threads():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                alive.append(thread)
    return alive


def _reset_for_tests():
    with _STORES_LOCK:
        _STORES.clear()
