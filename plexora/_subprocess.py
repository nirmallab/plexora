"""Keyword arguments every child process of Plexora is started with.

One concern today, and it is Windows-only: a process that has no console of
its own -- the server the desktop app starts, which the shell launches with
CREATE_NO_WINDOW -- makes Windows allocate a brand-new console for every
console program it starts. That is a black window flashing up for each
`nvidia-smi` probe, each tkinter file dialog, each ssh a connection opens.
Passing CREATE_NO_WINDOW on to those children gives them a hidden console
instead.

Only when this process has no console itself. From a terminal, a child
sharing the terminal is exactly right -- ssh prompting for a password needs it
-- so there the answer is `{}` and nothing changes.

Leaf module, stdlib only: `cli.py`, `connect.py` and `gcloud.py` must stay
importable without the package, so they reach this lazily and fall back to
`{}` when it is not there.
"""

from __future__ import annotations

import os
import subprocess

#: `subprocess.CREATE_NO_WINDOW` where it exists; spelled out for the tests,
#: which run on every platform and check the value.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _has_console():
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        # Not knowing is treated as having one: the cost of guessing wrong
        # that way is a flash, and the cost of guessing wrong the other way is
        # an ssh that can no longer prompt in the terminal it was run from.
        return True


def popen_kwargs(*, os_name=None, has_console=None):
    """`{"creationflags": CREATE_NO_WINDOW}` for a console-less Windows
    process, `{}` everywhere else. Spread into every Popen/run call."""
    if (os.name if os_name is None else os_name) != "nt":
        return {}
    console = _has_console() if has_console is None else has_console
    if console:
        return {}
    return {"creationflags": CREATE_NO_WINDOW}
