"""An append-only record of every mutation an agent attempted.

One JSON object per line in `<data_root>/.agent/audit.jsonl`: what was asked,
whether it ran, and the receipt when it did. Refusals and conflicts are
recorded too -- "the agent tried to write the source file and was stopped" is
exactly the line somebody reviewing a session wants to find.

Appended under a process lock with a flush and an fsync, because two tool
calls can land at once and a half-written line is worse than a missing one.
Nothing ever rewrites or truncates it; rotating it is the user's business.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()

AUDIT_FILENAME = "audit.jsonl"


def default_path() -> Path:
    from plexora import paths

    return paths.agent_root() / AUDIT_FILENAME


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class AuditLog:
    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path is not None else None

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else default_path()

    def append(self, record: dict) -> Path:
        path = self.path
        line = json.dumps({"timestamp": now_iso(), **record}, default=str,
                          separators=(",", ":"), ensure_ascii=False)
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:  # pragma: no cover - some filesystems refuse
                    pass
        return path

    def tail(self, limit: int = 50) -> list:
        path = self.path
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
