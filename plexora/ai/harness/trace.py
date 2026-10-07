"""The local trace: spatialagents' `TraceStore`, at Plexora's place for it.

`<data_root>/.agent/ai/trace.sqlite` -- runs, model calls (tokens, cache
verdict, credit, gateway request id), tool calls and tasks, read by `plexora ai
trace` and `ai.run_status`. `TraceStore()` opens it there; a test passes its
own path.
"""

from __future__ import annotations

from pathlib import Path

from spatialagents import trace as _trace
from spatialagents.trace import LATE_COLUMNS, SCHEMA, NullTrace

__all__ = ["LATE_COLUMNS", "SCHEMA", "NullTrace", "TraceStore", "default_path"]


def default_path() -> Path:
    from plexora import paths

    return Path(paths.data_root()) / ".agent" / "ai" / "trace.sqlite"


class TraceStore(_trace.TraceStore):
    def __init__(self, path: Path | str | None = None):
        super().__init__(path or default_path())
