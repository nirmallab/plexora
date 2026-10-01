"""A session on disk: the record, its packets, its decisions, and who holds it.

    <data_root>/.agent/sessions/<kind>/<session_id>/
        session.json      the record (atomic writes)
        control.json      pause / take-over, written by the viewer's route
        decisions.jsonl   every packet issued and every answer applied
        packets/<id>.json the outstanding (and recent) packets
        packets/<id>/     their images, as sent
        report.html       the review report, once written
        .lock             the process driving the session (pid)

The thread that runs a session's bulk job and the tool calls answering its
packets are in one process; `lock` serialises them. A second process that
tries to drive the same session is refused (`conflict`) while the first is
alive, and takes over when it is not.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from plexora.agent.errors import AgentError

KEEP_SESSIONS = 20
KEEP_DAYS = 60

_LOCKS: dict = {}
_GUARD = threading.Lock()


def new_session_id(prefix="gs") -> str:
    return f"{prefix}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:6]}"


def _atomic(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    # Windows refuses to replace a file another thread has open for reading
    # (WinError 5/32) -- the bulk job and an answering loop read session.json
    # while the other writes it, and parallel sessions make that common. The
    # reader holds it for microseconds, so a short retry always gets through.
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.005 * (attempt + 1))


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True



class SessionBusy(Exception):
    """`SessionStore.lock(timeout=...)` ran out: another thread holds the session."""

class SessionStore:
    """The sessions of one kind under one data root."""

    def __init__(self, kind: str, root: Path | None = None):
        if root is None:
            from plexora import paths

            root = paths.agent_root() / "sessions" / kind
        self.kind = kind
        self.root = Path(root)

    # -- paths -----------------------------------------------------------

    def folder(self, session_id) -> Path:
        if not str(session_id).replace("_", "").replace("-", "").isalnum():
            raise AgentError("invalid_input", f"not a session id: {session_id!r}")
        return self.root / str(session_id)

    def exists(self, session_id) -> bool:
        return (self.folder(session_id) / "session.json").is_file()

    # -- the record ------------------------------------------------------

    def create(self, record: dict) -> dict:
        folder = self.folder(record["session_id"])
        folder.mkdir(parents=True, exist_ok=False)
        self.save(record)
        self.claim(record["session_id"])
        return record

    def load(self, session_id) -> dict:
        path = self.folder(session_id) / "session.json"
        if not path.is_file():
            raise AgentError("invalid_input", f"no {self.kind} session {session_id!r}",
                             detail={"hint": "gating_session_status lists the sessions"})
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, record: dict):
        record["updated_at"] = time.time()
        _atomic(self.folder(record["session_id"]) / "session.json",
                json.dumps(record, default=str, ensure_ascii=False))

    @contextmanager
    def lock(self, session_id, *, timeout: float = -1):
        """Exclusive access to one session's record within this process.

        `timeout` (seconds) bounds the wait and raises `SessionBusy` when it
        runs out; the default waits as long as it takes. Best-effort work
        that runs while holding another lock (a bulk pass's progress, inside
        an open image reader) must not wait: an answer drawing the next
        packet holds this lock and waits for that reader."""
        with _GUARD:
            lock = _LOCKS.setdefault((str(self.root), str(session_id)), threading.RLock())
        if not lock.acquire(timeout=timeout):
            raise SessionBusy(session_id)
        try:
            yield
        finally:
            lock.release()

    # -- ownership across processes ---------------------------------------

    def claim(self, session_id):
        """Take the session for this process, or refuse while another live
        process holds it."""
        path = self.folder(session_id) / ".lock"
        if path.exists():
            try:
                holder = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                holder = {}
            if holder.get("pid") != os.getpid() and _pid_alive(holder.get("pid")):
                raise AgentError("conflict", "another Plexora process is driving this session",
                                 detail={"holder_pid": holder.get("pid"),
                                         "since": holder.get("since")}, retryable=True)
        _atomic(path, json.dumps({"pid": os.getpid(), "since": time.time()}))

    def release(self, session_id):
        path = self.folder(session_id) / ".lock"
        try:
            holder = json.loads(path.read_text(encoding="utf-8"))
            if holder.get("pid") == os.getpid():
                path.unlink()
        except (OSError, ValueError):
            pass

    # -- control -----------------------------------------------------------

    def control(self, session_id) -> dict:
        path = self.folder(session_id) / "control.json"
        if not path.is_file():
            return {"paused": False, "paused_by": None, "locked_markers": []}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"paused": False, "paused_by": None, "locked_markers": []}

    def set_control(self, session_id, **changes) -> dict:
        control = self.control(session_id)
        control.update(changes)
        _atomic(self.folder(session_id) / "control.json", json.dumps(control))
        return control

    # -- packets and decisions ---------------------------------------------

    def write_packet(self, session_id, packet: dict, images=()):
        folder = self.folder(session_id) / "packets"
        _atomic(folder / f"{packet['packet_id']}.json", json.dumps(packet, default=str))
        image_dir = folder / packet["packet_id"]
        if image_dir.is_dir():
            # A re-rendered packet replaces its images; none may linger.
            for stale in image_dir.iterdir():
                stale.unlink()
        if images:
            image_dir.mkdir(parents=True, exist_ok=True)
        for index, (data, fmt) in enumerate(images):
            (image_dir / f"{index}.{fmt}").write_bytes(data)

    def read_packet(self, session_id, packet_id) -> tuple:
        """(packet, [(bytes, format)])."""
        folder = self.folder(session_id) / "packets"
        path = folder / f"{packet_id}.json"
        if not path.is_file():
            raise AgentError("invalid_input", f"no packet {packet_id!r} in this session")
        packet = json.loads(path.read_text(encoding="utf-8"))
        images = []
        image_dir = folder / packet_id
        if image_dir.is_dir():
            for file in sorted(image_dir.iterdir(), key=lambda p: int(p.stem)
                               if p.stem.isdigit() else 0):
                images.append((file.read_bytes(), file.suffix.lstrip(".")))
        return packet, images

    def log(self, session_id, record: dict):
        path = self.folder(session_id) / "decisions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"t": time.time(), **record}, default=str, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def decisions(self, session_id) -> list:
        path = self.folder(session_id) / "decisions.jsonl"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def clear_packets(self, session_id):
        folder = self.folder(session_id) / "packets"
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)

    # -- listing and retention ---------------------------------------------

    def list(self, limit=20) -> list:
        if not self.root.is_dir():
            return []
        records = []
        for folder in self.root.iterdir():
            path = folder / "session.json"
            if not path.is_file():
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            records.append(record)
        records.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return records[:limit]

    def sweep(self, keep=KEEP_SESSIONS, days=KEEP_DAYS, now=None) -> int:
        """Remove finished sessions past the newest `keep` and older than `days`."""
        now = now or time.time()
        records = self.list(limit=10_000)
        removed = 0
        for index, record in enumerate(records):
            finished = record.get("state") in ("done", "cancelled", "failed", "rolled_back")
            old = now - float(record.get("updated_at") or now) > days * 86400
            if finished and (index >= keep or old):
                shutil.rmtree(self.folder(record["session_id"]), ignore_errors=True)
                removed += 1
        return removed
