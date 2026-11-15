"""A revision every receipted write moves, so a remembered read can tell it is stale.

`<data_root>/.agent/revision.json` holds three counters: `all` (every write),
`global` (writes that named no project: a dataset, the panel context, an
undo replayed across images) and one per project. `make_receipt`
(plexora/agent/receipts.py) bumps them -- every write that changes anything
leaves a receipt, so a read cached against `token(project)` can never outlive
the state it described. The token for a project changes when that project, or
anything project-less, is written; a read that names no project is keyed on
`all`.

A file rather than a process counter because two processes write the same data
root (the server and an MCP server). The read-modify-write is held across
both (`plexora._process_lock`, `revision.lock` beside the file): unheld, a
bump that read the file before another process wrote it would put the counter
back to a token an earlier read is already cached under, and that stale read
would be served again.

Edits a person makes by hand in the viewer do not go through the capability
registry and leave no receipt, which is why the tool-result cache that reads
this also bounds the age of what it keeps (plexora/ai/harness/toolcache.py).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

FILENAME = "revision.json"


def path() -> Path:
    from plexora import paths

    return paths.agent_root() / FILENAME


def _lock_file() -> Path:
    file = path().with_name("revision.lock")
    file.parent.mkdir(parents=True, exist_ok=True)
    return file


def _lock():
    from plexora import _process_lock

    return _process_lock.lock_for("agent-revision", _lock_file)


def _read(file: Path) -> dict:
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def bump(project: str | None = None) -> dict:
    """Record one write (to `project`, or to nothing project-shaped)."""
    file = path()
    with _lock():
        data = _read(file)
        data["all"] = int(data.get("all") or 0) + 1
        if project:
            projects = data.setdefault("projects", {})
            projects[str(project)] = int(projects.get(str(project)) or 0) + 1
        else:
            data["global"] = int(data.get("global") or 0) + 1
        file.parent.mkdir(parents=True, exist_ok=True)
        tmp = file.with_name(f"{file.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        for attempt in range(40):
            try:
                os.replace(tmp, file)
                break
            except PermissionError:          # a reader on Windows; it holds it for microseconds
                if attempt == 39:
                    raise
                import time

                time.sleep(0.005 * (attempt + 1))
        return data


def token(project: str | None = None) -> str:
    """What a read of `project` (or of no project) is valid for."""
    data = _read(path())
    if project:
        return f"g{int(data.get('global') or 0)}.p{int((data.get('projects') or {}).get(str(project)) or 0)}"
    return f"a{int(data.get('all') or 0)}"
