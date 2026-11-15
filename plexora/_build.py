"""Which build of Plexora this is, beside its version: the commit it was built from.

`BUILD` is empty in the repository. `scripts/release.py wheel` writes the
short commit into it for the length of `uv build` and puts it back after, so a
released wheel (and the desktop app and website built from it) carries the
commit while the tree never does. A checkout answers `source-<commit>` from
git, and anything else `source`.

Sent with a bug report (server/routes/feedback_routes.py) so a report from a
release and one from somebody's checkout can be told apart.
"""

from __future__ import annotations

import functools
import subprocess
from pathlib import Path

from plexora._subprocess import popen_kwargs

BUILD = ""


@functools.lru_cache(maxsize=1)
def build_id() -> str:
    """The build identifier: the stamped commit, `source-<commit>` in a git
    checkout, or `source`. Cached: git is asked at most once per process."""
    if BUILD:
        return BUILD
    try:
        done = subprocess.run(["git", "rev-parse", "--short=7", "HEAD"],
                              cwd=Path(__file__).resolve().parent, capture_output=True,
                              text=True, timeout=2, **popen_kwargs())
        sha = done.stdout.strip() if done.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        sha = ""
    return f"source-{sha}" if sha else "source"
