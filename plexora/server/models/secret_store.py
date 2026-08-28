"""Writing a JSON file that holds a secret, without ever widening it.

Two registries now keep credentials in the user's data root -- `nodes.json`
holds a token per data node, `remotes.json` holds how to reach a remote server
-- and both need exactly the same care, so it lives here once rather than
twice.

The care is one detail and it is the whole module: the temp file is chmod'd
BEFORE the rename, never the destination after it. A rename-then-chmod leaves a
window in which the file is world-readable, and on the shared cluster
filesystem where a `$HOME` is visible to every other account, that window is the
entire threat this is defending against.

Everything else is the write discipline `models/project.py` established -- one
lock, temp file, atomic replace -- because a background probe updating
`last_seen` can land on top of a request adding an entry, and losing that write
means a project that no longer knows where its data is.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from plexora.server.models.project import _CONFIG_LOCK, _past_transient_locks


def write_private_json(path, raw) -> None:
    """Replace `path` with `raw`, atomically, owner-readable only."""
    path = Path(path)
    with _CONFIG_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(raw, handle, indent=4)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                # Windows has no meaningful equivalent, and POSIX may refuse on
                # some network filesystems. Not fatal: losing the registry
                # entirely is worse than a permissive mode on a file that is
                # already inside the user's own data root.
                pass
            _past_transient_locks(lambda: os.replace(tmp, path))
        finally:
            tmp.unlink(missing_ok=True)
