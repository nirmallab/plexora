"""The cell table of a project opened on the website, read from the page.

On the website Plexora's Python runs in a Web Worker beside the viewer
(Pyodide), and polars there has no threads: a large CSV makes it panic, and a
panic poisons it for the rest of the session. So a CSV the user picked is
parsed by the page instead (client services/browserTable.js, as
plexora/browser/columns.py says polars would read it), and everything after
the read -- normalisation, the corrections a project records, every frame
computation -- is `LocalTableProvider`'s, unchanged. Every table provider asks
here first (`page_adapter`), so no reader of a picked CSV goes round it.

The page registers how to reach its columns (`set_column_source`) when the
worker boots. Anywhere else -- the desktop app, a server, a node -- there is no
page, and a picked path never occurs. A project whose table binding says
`provider: "browser"` (`BrowserTableProvider`) says so when opened there: the
project came from a browser, and moving it means binding the table to the file
again.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import PurePosixPath

from plexora.server.providers.base import BROWSER, ResourceLocator, ResourceNotLocal
from plexora.server.providers.local import LocalTableProvider

_column_source = None

#: Where the page's own files are in Python's: picked by the user, or made from
#: one by the page (a pyramid it built, kept in the site's storage). The page
#: reads both itself (services/pickedFiles.js `fileFor`).
PAGE_ROOTS = ("/picked/", "/derived/")

#: How often an import waiting on the page's pyramid looks again; between
#: looks the requests that arrived meanwhile are answered (`_work.pause`). A
#: background job's one look waits as long for the build to end.
_POLL_S = 0.25


class PageBuilding(Exception):
    """The page is still building the pyramid a background job needs
    (`page_pyramid(hold=False)`): the job gives the worker back and looks again
    later. `tiles` of `total` are written."""

    def __init__(self, tiles: int = 0, total: int = 0):
        super().__init__(f"the page is building a pyramid ({tiles} of {total} tiles)")
        self.tiles = tiles
        self.total = total


_asker = None


def set_asker(ask) -> None:
    """`ask(kind, args)` -> what the page answers, or None. Installed by the
    website's worker at boot; None removes it."""
    global _asker
    _asker = ask


def page_window(provider, pyramid, index):
    """`(0.0, qmax)` for channel `index` of a picked TIFF the page reads
    (`data_model.quantization_window_of`, computed by the page's workers over
    the same full-resolution plane), or None: not on the website, not a file
    the page reads as Python does -- Python then reads every pixel itself."""
    if _asker is None or os.environ.get("PLEXORA_BACKEND") != "browser":
        return None
    path = str(getattr(provider, "_path", "") or "")
    if not path.startswith(PAGE_ROOTS) or not path.lower().endswith((".tif", ".tiff")):
        return None
    try:
        level = pyramid["0"]
    except Exception:  # noqa: BLE001 -- one level, or not a level group
        return None
    shape = list(getattr(level, "shape", ()))
    dtype = str(getattr(level, "dtype", ""))
    if len(shape) != 3 or dtype not in ("uint8", "uint16"):
        return None
    found = _asker("window", {"path": path, "channel": int(index), "shape": shape, "dtype": dtype})
    if found is None:
        return None
    return (0.0, max(float(found), 1.0))


def is_page_file(path) -> bool:
    """Whether `path` is one of the page's files on the website: picked by the
    user (read-only to a web page) or made by the page."""
    return os.environ.get("PLEXORA_BACKEND") == "browser" and str(path).startswith(PAGE_ROOTS)


def _plain(value):
    """A page's answer as Python values (a JS object arrives as a proxy)."""
    to_py = getattr(value, "to_py", None)
    return to_py() if callable(to_py) else value


def page_pyramid(source, *, name, spec, progress=None, stage=None, adoptable=None, hold=True):
    """The pyramid of the picked flat TIFF `source`, built by the page
    (services/browserPyramid.js; tests/test_tiff_writer.py holds it to
    Python's file): its path in Python's files, /derived/<key>/<name>. Two
    kinds, as `spec()` says: an image's pyramidized copy
    (`image_pyramid.page_build`, the copy `pyramidize_image` writes) and a
    mask's filled label pyramid (`segmentation_pyramid.page_build`, the file
    `pyramidize_segmentation_mask` writes). None when the page does not build
    it -- not the website, not a picked TIFF, or `spec()` None because the
    page would not read what Python reads -- and Python then builds it
    itself. A build the page began and could not finish raises `PyramidError`
    with the page's reason.

    An import (a request) waits for it a look at a time (`_work.pause`): the
    page's progress reaches the import's bar, and other requests are answered
    between looks. Never `_work.wait`, which runs Python's queued work in the
    gap.

    A background job passes `hold=False` (a mask's conversion,
    `data_model.start_segmentation_job`): it looks once -- the page answers
    when the build ends or after `_POLL_S` -- and raises `PageBuilding` while
    the page is still at it, and the job is queued again. A job must not hold
    the worker for the build: polled with `_work.wait`, the viewer's own
    requests waited behind the warm-up it ran (10 s on a 2.4-gigapixel mask);
    with `_work.pause`, they were answered inside the job, on top of its
    suspended frames, and the worker stopped answering for good (not traced
    further: `_work.checkpoint` says why answering requests inside a task was
    taken out once already).

    Where the copy was to be saved in a folder and a file of that name is
    already there, the page hands that file back unread (`existing`): it is
    used when `adoptable(path)` says it is a finished copy of this source as
    it is now (`image_pyramid.is_adoptable_image` by default), and otherwise
    the copy is built in the browser instead -- a user's file is never
    written over."""
    if _asker is None or os.environ.get("PLEXORA_BACKEND") != "browser":
        return None
    path = str(source)
    if not path.startswith("/picked/") or not path.lower().endswith((".tif", ".tiff")):
        return None
    wanted = spec()
    if wanted is None:
        return None
    from plexora import _work
    from plexora.server.utils import image_pyramid
    from plexora.server.utils.segmentation_pyramid import PyramidError

    if adoptable is None:
        adoptable = lambda found: image_pyramid.is_adoptable_image(found, source)  # noqa: E731
    kind = wanted.get("kind") or "image"
    # One copy per source file as it is now, whichever pick it came through;
    # a mask's apart from an image's of the same file.
    seed = f"{PurePosixPath(path).name}|{wanted['fingerprint']}"
    if kind != "image":
        seed = f"{kind}|{seed}"
    key = hashlib.sha256(seed.encode()).hexdigest()[:24]
    asked = {"path": path, "key": key, "name": name, "kind": kind,
             **{field: wanted[field] for field in ("shape", "dtype", "factors", "tile", "description")}}
    started = _plain(_asker("pyramid", asked))
    if not started or started.get("id") is None:
        return None
    if stage is not None:
        stage("building", None)
    while True:
        state = _plain(_asker("pyramid_status", {"id": started["id"],
                                                 "wait": 0 if hold else int(_POLL_S * 1000)})) or {}
        if progress is not None and state.get("total"):
            progress(int(state.get("tiles") or 0), int(state["total"]))
        if state.get("state") == "done" and state.get("existing"):
            if adoptable(state["path"]):
                return str(state["path"])
            started = _plain(_asker("pyramid", {**asked, "browser": True}))
            if not started or started.get("id") is None:
                return None
            continue
        if state.get("state") == "done":
            if stage is not None:
                stage("writing", None)
            return str(state["path"])
        if state.get("state") != "building":
            raise PyramidError(
                f"{PurePosixPath(path).name}: this browser could not build the "
                f"{'mask' if kind == 'labels' else 'image'}'s pyramid "
                f"({state.get('error') or 'no reason given'}), so no copy was made.")
        if not hold:
            raise PageBuilding(int(state.get("tiles") or 0), int(state.get("total") or 0))
        _work.pause(_POLL_S)


def set_column_source(source) -> None:
    """`source(spec)` -> a polars DataFrame of the table `spec` names, parsed
    by the page (None: it cannot, read the file). Installed by the website's
    worker at boot; None removes it."""
    global _column_source
    _column_source = source


def picked(spec) -> bool:
    """Whether `spec` is a CSV the user picked on the website (mounted under
    /picked/ in the page's worker)."""
    return (os.environ.get("PLEXORA_BACKEND") == "browser" and spec is not None
            and getattr(spec, "type", None) == "csv"
            and str(getattr(spec, "src", "") or "").startswith("/picked/"))


def page_frame(spec):
    """The frame of `spec`'s table as the page parsed it, or None. Parsed
    afresh for each reader (the viewer's load, an agent session): each keeps
    its own normalised frame, and a raw one kept here as well would be one
    more copy in a 32-bit heap."""
    if _column_source is None:
        return None
    return _column_source(spec)


def page_adapter(spec):
    """An adapter over the page's parse of a picked CSV, or None: not picked,
    or the page cannot parse it (the file is then read as any other)."""
    if not picked(spec):
        return None
    from plexora.server.models.adapters.memory_adapter import MemoryFrameAdapter

    frame = page_frame(spec)
    return None if frame is None else MemoryFrameAdapter(spec, frame)


class BrowserTableProvider(LocalTableProvider):
    """`LocalTableProvider` for a table bound to the page that opened it."""

    @property
    def locator(self) -> ResourceLocator:
        return ResourceLocator(kind="table", provider=BROWSER,
                               path=self._spec.src if self._spec else None)

    def _open_adapter(self):
        from plexora.server.models.adapters.memory_adapter import MemoryFrameAdapter

        if _column_source is None:
            raise ResourceNotLocal(
                f"{self._spec.src}: this table was opened on the Plexora website, and "
                "its rows are held by that browser; here, open the file itself "
                "(Import the project again, or relink its table)")
        frame = page_frame(self._spec)
        if frame is None:
            # The page cannot parse it: the file is mounted where the binding
            # says, so read it as any other.
            return super()._open_adapter()
        return MemoryFrameAdapter(self._spec, frame)
