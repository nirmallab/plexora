"""What a browser tab can hold, asked before a build or a read that might not fit.

On the website (`PLEXORA_BACKEND=browser`) Python runs in one worker whose
WebAssembly memory stops at 4 GiB, and an allocation that fails there ends
Pyodide for good: the tab's Python is gone until a reload. The data root --
every project folder, and so every derived file Python writes -- is held in the
same tab's memory (client/src/js/workers/storeSync.js), and Emscripten reports a
made-up disk to `shutil.disk_usage`, so a free-space check says there is room
where there is none. A build sized for a disk is a crash there.

So the builds that write in proportion to an image -- a mask's label pyramid,
missing pyramid levels, a 3-D label volume, a copy of a stack -- ask
`check_build` first. On the website one past `BUILD_BUDGET` is refused before a
byte is read, with a sentence naming its size and what does it (the desktop
app, which keeps it on disk), and the page is told too (`set_teller`: the
notice in services/handoffNotice.js). Reads that can stream are kept to
`READ_BUDGET` a piece (`full_read_max`): slower, never a crash.

On the desktop and on a node every answer is "go ahead": nothing here changes
what they do.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

#: The most one build may write under the data root on the website. A label
#: pyramid of a 20k x 20k uint32 mask, or a 2-D image's missing levels of a few
#: GB of pixels, fit; a whole-slide mask's do not.
BUILD_BUDGET = 512 * 2**20
#: The most read into memory in one piece on the website, where a reader can
#: stream instead.
READ_BUDGET = 256 * 2**20
#: The most one slab of a computation that turns pixels into float64 may read
#: on the website: its temporaries are four times it.
SLAB_BUDGET = 64 * 2**20
#: The most one read that cannot stream may take on the website: a quarter of
#: the worker's memory, which leaves room for what it is turned into.
READ_LIMIT = 1 * 2**30

_log = logging.getLogger(__name__)
_teller: Optional[Callable[[dict], None]] = None


class TooLargeForTab(RuntimeError):
    """A build or read this browser tab cannot hold. Its message is the
    sentence a person reads; `nbytes` what it needed."""

    def __init__(self, message: str, nbytes: int = 0):
        super().__init__(message)
        self.nbytes = int(nbytes)


def on_website() -> bool:
    """Whether this Python is the website's (the page's worker)."""
    return os.environ.get("PLEXORA_BACKEND") == "browser"


def set_teller(teller: Optional[Callable[[dict], None]]) -> None:
    """Who tells the page (plexora/browser/boot.py: an "envelope" event)."""
    global _teller
    _teller = teller


def size_text(nbytes: float) -> str:
    """`nbytes` for a sentence: "640 MB", "3.4 GB", "62 GB"."""
    nbytes = max(0.0, float(nbytes))
    if nbytes >= 10e9:
        return f"{nbytes / 1e9:.0f} GB"
    if nbytes >= 1e9:
        return f"{nbytes / 1e9:.1f} GB"
    return f"{max(1, round(nbytes / 1e6))} MB"


def tell(title: str, note: str, reason: str = "tab-limit") -> None:
    """Say `title`/`note` on the page, where there is one to tell. Never raises."""
    if _teller is None:
        return
    try:
        _teller({"reason": reason, "title": title, "note": note})
    except Exception:  # noqa: BLE001 -- a notice is never worth a failure
        _log.debug("tab_room: the page could not be told", exc_info=True)


def check_build(nbytes: float, what: str) -> None:
    """Refuse, on the website, a build that writes about `nbytes` under the
    data root when that is past BUILD_BUDGET. `what` starts the sentence
    ("This mask's pyramid"). Nothing on the desktop."""
    if not on_website() or nbytes <= BUILD_BUDGET:
        return
    note = (f"{what} would take about {size_text(nbytes)} of this browser tab's memory, "
            f"more than it can hold ({size_text(BUILD_BUDGET)} for one build). "
            "The desktop app builds it on disk.")
    # The notice is one short line (services/handoffNotice.js); the error the
    # request answers with says the rest.
    tell("Too large for a browser tab", f"{what} needs about {size_text(nbytes)} here.")
    raise TooLargeForTab(note, int(nbytes))


def check_read(nbytes: float, what: str) -> None:
    """Refuse, on the website, one read of about `nbytes` that cannot stream
    when it is past READ_LIMIT (a quarter of the worker's memory). `what`
    starts the sentence ("This stack's planes"). Nothing on the desktop."""
    if not on_website() or nbytes <= READ_LIMIT:
        return
    note = (f"{what} would each be read whole, about {size_text(nbytes)} at a time, "
            "more than this browser tab can hold. The desktop app reads it.")
    tell("Too large for a browser tab", f"{what} are read {size_text(nbytes)} at a time.")
    raise TooLargeForTab(note, int(nbytes))


def full_read_max(default: int) -> int:
    """The most a reader that can stream should read in one piece."""
    return min(int(default), READ_BUDGET) if on_website() else int(default)


def slab_max(default: int) -> int:
    """The most one slab of a float computation should read."""
    return min(int(default), SLAB_BUDGET) if on_website() else int(default)


def free_bytes(measured: Optional[int]) -> Optional[int]:
    """What a free-space check should believe: on the website the build
    budget (the disk it was told of is not there), elsewhere `measured`."""
    if on_website():
        return BUILD_BUDGET if measured is None else min(int(measured), BUILD_BUDGET)
    return measured
