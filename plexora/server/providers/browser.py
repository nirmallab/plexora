"""The cell table of a project opened on the website, read from the page.

On the website Plexora's Python runs in a Web Worker beside the viewer
(Pyodide), and polars there has no threads: a large CSV makes it panic, and a
panic poisons it for the rest of the session. So a CSV the user picked is
parsed by the page instead (client services/browserTable.js, as
plexora/browser/columns.py says polars would read it), and everything after
the read -- normalisation, the corrections a project records, every frame
computation -- is `LocalTableProvider`'s, unchanged. Every table provider asks
here first (`page_adapter`), so no reader of a picked CSV goes round it.

Whole-slide images the page reads with openslide-js (MRXS, Sakura, NDPI, SVS;
plan L2) reach Python the same way: `page_slide` is the slide as the page
holds it (`PageSlide`, which brightfield.py reads as it reads OpenSlide on the
desktop), each region one question to the page.

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
    global _asker, _fits_ready
    _asker = ask
    _slides.clear()
    _page_fits.clear()
    _fits_ready = (float("-inf"), False)


#: `page_window(..., wait_ms=...)`'s answer while the page is still scanning.
WINDOW_PENDING = object()


class WindowPending(Exception):
    """A request for a channel the page is still scanning, on the website:
    answered later rather than waited for. Python cannot wait inside a
    request with the page's other requests answered meanwhile -- one of them
    taking a lock the suspended one holds, or suspending in turn, freezes the
    only thread (plexora/_work.py `checkpoint`) -- so the route says "again
    in `retry_ms`" (`RETRY_HEADER`) and the worker puts the request back in
    its queue, behind whatever arrived meanwhile (workers/pythonWorker.js)."""

    #: How long the worker waits before asking the request again, in ms.
    retry_ms = 200


#: The header a route's answer carries to be asked again later on the
#: website (plexora/browser/bridge.py `handle`); never seen anywhere else.
RETRY_HEADER = "X-Plexora-Retry-Ms"


class FitPending(WindowPending):
    """A channel's mixture fit a helper Python is making (`page_fit`): the
    request is asked again in `retry_ms`, the one Python thread free
    meanwhile."""

    #: How long the worker waits before asking the request again, in ms.
    retry_ms = 100


#: Fits handed to the page's helpers: digest of the values -> the page's job
#: id. Bounded: a fit nobody asks for again (the viewer moved on) is dropped
#: here, and the page drops its result on its own.
_page_fits: "dict[str, str]" = {}
_PAGE_FITS_KEPT = 64


def _mixture_code():
    """(kind, bytes, digest) of mixture_fit as this Python runs it, for the
    page's helpers to run the same: its compiled file ("pyc") where that is
    what was imported -- the website's bundle ships no sources -- else its
    text ("source")."""
    global _MIXTURE_CODE
    if _MIXTURE_CODE is None:
        import hashlib
        from pathlib import Path

        from plexora.server.models import mixture_fit

        path = str(getattr(mixture_fit, "__file__", "") or "")
        if path.endswith(".pyc"):
            kind, data = "pyc", Path(path).read_bytes()
        else:
            import inspect

            kind, data = "source", inspect.getsource(mixture_fit).encode("utf-8")
        _MIXTURE_CODE = (kind, data, hashlib.blake2b(data, digest_size=8).hexdigest())
    return _MIXTURE_CODE


_MIXTURE_CODE = None

#: (when asked, time.monotonic; whether the page had a helper ready).
_fits_ready = (float("-inf"), False)
#: How long `page_fits_ready`'s answer is taken as still true.
_FITS_READY_FOR_S = 2.0


def page_fits_ready() -> bool:
    """Whether the page has a helper Python ready for a fit (`page_fit`):
    asked at most every `_FITS_READY_FOR_S`. False anywhere but the website."""
    global _fits_ready
    if _asker is None or os.environ.get("PLEXORA_BACKEND") != "browser":
        return False
    import time

    asked, ready = _fits_ready
    now = time.monotonic()
    if now - asked < _FITS_READY_FOR_S:
        return ready
    found = _plain(_asker("fit", {"op": "ready"}))
    ready = bool(isinstance(found, dict) and found.get("ready"))
    _fits_ready = (now, ready)
    return ready


def page_fit_done(key) -> bool:
    """Whether the fit handed over as `key` (`FitPending`'s) is back, or will
    never be (failed, forgotten): a task coming back for it asks this first,
    a question cheaper than redoing what led to the fit."""
    job = _page_fits.get(str(key))
    if job is None or _asker is None:
        return True
    found = _plain(_asker("fit", {"op": "peek", "id": job}))
    return not (isinstance(found, dict) and found.get("done") is False)


def page_fit(values, components=3):
    """`mixture_fit.fit_mixture(values, components)` made by one of the page's helper
    Pythons (services/fitHelpers.js, workers/fitWorker.js): its floats
    when it is done; `FitPending` while it is being made (the first call hands
    it over); None when the page has no helper to give it to, or the helper
    failed or went away (a reload) -- the caller then fits here, as always.

    The helper runs mixture_fit as this Python does (`_mixture_code`: its
    compiled file) with the same numpy and scikit-learn builds over the same
    bytes, so its fit is the one this thread would make
    (tests/test_page_fit.py; measured bit for bit on six LSP11385 channels)."""
    if _asker is None or os.environ.get("PLEXORA_BACKEND") != "browser":
        return None
    import hashlib

    import numpy as np

    values = np.asarray(values)
    if values.dtype != np.float64 or values.ndim != 1:
        return None          # fitted in its own type, here (mixture_fit)
    values = np.ascontiguousarray(values)
    components = int(components)
    digest = hashlib.blake2b(values.view(np.uint8).reshape(-1), digest_size=16)
    digest.update(f"|{components}".encode())
    key = digest.hexdigest()
    job = _page_fits.get(key)
    if job is None:
        # An array crosses as {dtype, shape, data} (workers/pythonWorker.js
        # `_ask`: its bytes copied out of Python's memory, then moved).
        kind, code, code_id = _mixture_code()
        started = _plain(_asker("fit", {"op": "start", "values": values,
                                        "components": components,
                                        "code": np.frombuffer(code, dtype=np.uint8),
                                        "code_kind": kind, "code_id": code_id}))
        if not isinstance(started, dict) or not started.get("id"):
            return None
        _page_fits[key] = str(started["id"])
        while len(_page_fits) > _PAGE_FITS_KEPT:
            _page_fits.pop(next(iter(_page_fits)))
        raise FitPending(key)
    found = _plain(_asker("fit", {"op": "result", "id": job}))
    if isinstance(found, dict) and found.get("pending"):
        raise FitPending(key)
    _page_fits.pop(key, None)
    params = found.get("params") if isinstance(found, dict) else None
    if not isinstance(params, str):
        return None
    try:
        out = np.frombuffer(bytes.fromhex(params), dtype=np.float64)
    except ValueError:
        return None
    return out.copy() if out.size == 3 * components else None


def page_window(provider, pyramid, index, *, wait_ms=None, urgent=True, ahead=()):
    """`(0.0, qmax)` for channel `index` of a picked TIFF the page reads
    (`data_model.quantization_window_of`, computed by the page's workers over
    the same full-resolution plane), or None: not on the website, not a file
    the page reads as Python does -- Python then reads every pixel itself.

    Without `wait_ms` Python waits for the scan, as it always did. With it,
    this only looks: the page answers within `wait_ms`, and `WINDOW_PENDING`
    means it is still reading (the scan goes on without Python, behind any
    `urgent` one; `ahead`, other channel indices, are queued after it). A
    scan can take seconds a channel on a large slide, and a Python waiting
    for it answers nothing else (services/browserNode.js `windowOf`)."""
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
    question = {"path": path, "channel": int(index), "shape": shape, "dtype": dtype}
    if wait_ms is None:
        found = _asker("window", question)
        if found is None:
            return None
        return (0.0, max(float(found), 1.0))
    found = _plain(_asker("window", {**question, "wait": max(0, int(wait_ms)), "urgent": bool(urgent),
                                     "ahead": [int(other) for other in ahead]}))
    if not isinstance(found, dict):
        return None
    if not found.get("done"):
        return WINDOW_PENDING
    if found.get("failed") or found.get("max") is None:
        return None
    return (0.0, max(float(found["max"]), 1.0))


def page_regions(path, levels, dtype, reads):
    """The rectangles `reads` -- [(channel, level, (x0, y0, x1, y1))], each
    inside its level -- of the picked TIFF at `path`, read by the page's
    workers (services/browserNode.js `regionsOf`: the browser's own zlib,
    side by side) rather than one at a time in WebAssembly: [2-D array, or
    None where the page declined that one] in order. None when the page does
    not read this file -- not the website, not the page's TIFF, read
    differently there than here (`levels`, every level's (channels, height,
    width), and `dtype` are how Python reads it) -- and Python reads them all."""
    if _asker is None or os.environ.get("PLEXORA_BACKEND") != "browser":
        return None
    import numpy as np

    path = str(path or "")
    if not path.startswith(PAGE_ROOTS) or not path.lower().endswith((".tif", ".tiff")):
        return None
    dtype = str(dtype)
    if dtype not in ("uint8", "uint16", "uint32") or not reads:
        return None
    found = _plain(_asker("regions", {
        "path": path, "dtype": dtype,
        "levels": [[int(v) for v in shape] for shape in levels],
        "reads": [{"channel": int(channel), "level": int(level), "box": [int(v) for v in box]}
                  for channel, level, box in reads]}))
    if not isinstance(found, dict) or found.get("error") or not isinstance(found.get("regions"), list) \
            or len(found["regions"]) != len(reads):
        return None
    out = []
    for (_channel, _level, box), one in zip(reads, found["regions"]):
        values = (one or {}).get("values") if isinstance(one, dict) else None
        if values is None or one.get("refused"):
            out.append(None)
            continue
        x0, y0, x1, y1 = (int(v) for v in box)
        pixels = np.frombuffer(values, dtype=dtype)
        out.append(pixels.reshape(y1 - y0, x1 - x0) if pixels.size == (y1 - y0) * (x1 - x0) else None)
    return out


def is_page_file(path) -> bool:
    """Whether `path` is one of the page's files on the website: picked by the
    user (read-only to a web page) or made by the page."""
    return os.environ.get("PLEXORA_BACKEND") == "browser" and str(path).startswith(PAGE_ROOTS)


def _plain(value):
    """A page's answer as Python values (a JS object arrives as a proxy)."""
    to_py = getattr(value, "to_py", None)
    return to_py() if callable(to_py) else value


#: A build whose page went away (a reload of the tab), asked of the page
#: there now at most this many times.
_LOST_BUILDS = 3


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
    key = derived_key(PurePosixPath(path).name, wanted["fingerprint"], kind)
    asked = {"path": path, "key": key, "name": name, "kind": kind,
             **{field: wanted[field] for field in ("shape", "dtype", "factors", "tile", "description")}}
    started = _plain(_asker("pyramid", asked))
    if not started or started.get("id") is None:
        return None
    if stage is not None:
        stage("building", None)
    lost = 0
    while True:
        state = _plain(_asker("pyramid_status", {"id": started["id"],
                                                 "wait": 0 if hold else int(_POLL_S * 1000)})) or {}
        if (not state or state.get("state") == "lost") and lost < _LOST_BUILDS:
            # The page building it is gone -- the tab reloaded, and kept this
            # Python (client services/pythonClient.js), which answers what it
            # had asked that page with nothing -- so the page there now builds
            # it again.
            lost += 1
            started = _plain(_asker("pyramid", asked))
            if not started or started.get("id") is None:
                return None
            continue
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


def derived_key(source_name: str, fingerprint: str, kind: str = "image") -> str:
    """The folder a copy the page builds of a file is kept under
    (/derived/<key>/, OPFS plexora/derived/<key>/, Documents/Plexora/
    pyramids/<key>/): the source's name and its fingerprint as the website
    reads it (`segmentation_pyramid.source_fingerprint`, times in whole
    milliseconds), a mask's apart from an image's. The desktop app works it
    out from the same file to find the website's copy (utils/relink.py
    `website_copies`)."""
    seed = f"{source_name}|{fingerprint}"
    if kind != "image":
        seed = f"{kind}|{seed}"
    return hashlib.sha256(seed.encode()).hexdigest()[:24]


def derived_index() -> dict:
    """{path: {state, home, kind, key, name, place}} of every copy the page
    has made or found (client services/derivedIndex.js), `state` "ready" (the
    page reads it), "waiting" (in a folder the browser opens again only after
    a click) or "missing"; {} anywhere but the website."""
    if _asker is None or os.environ.get("PLEXORA_BACKEND") != "browser":
        return {}
    try:
        found = _plain(_asker("derived_index", {})) or {}
    except Exception:  # noqa: BLE001 -- a page that cannot say: nothing is known to wait
        return {}
    copies = found.get("copies") if isinstance(found, dict) else None
    return dict(copies) if isinstance(copies, dict) else {}


def derived_status(path):
    """Of a copy the page made, as the website sees it now: "mounted" (Python
    reads it), "waiting" (kept in a folder the browser has not opened again
    this visit: one click, and it is back -- neither rebuilt nor asked to be
    found) or "missing". None for anything else, and anywhere but the
    website: the page is asked only about a /derived/ path not mounted."""
    path = str(path)
    if not is_page_file(path) or not path.startswith("/derived/"):
        return None
    if os.path.exists(path):
        return "mounted"
    found = derived_index().get(path) or {}
    state = found.get("state") if isinstance(found, dict) else None
    return "waiting" if state == "waiting" else "missing"


class PageSlideError(RuntimeError):
    """The page tried to open a whole-slide image and could not; the message
    is its reason (an MRXS picked without its folder, a file OpenSlide does
    not read)."""


class PageSlide:
    """A whole-slide image the page reads with openslide-js (OpenSlide in
    WebAssembly, client node/slideReader.js; plan L2), shaped as much like
    `openslide.OpenSlide` as `brightfield._OpenSlideSource` uses it:
    `level_count`, `level_dimensions`, `level_downsamples`, `properties`,
    `read_region((x, y), level, (w, h))` -> (h, w, 4) uint8 straight RGBA, and
    `close()`. The page holds the slide open; each region is one question to
    it. Its pixels are native OpenSlide's byte for byte (L2.md)."""

    def __init__(self, path, info: dict):
        self.path = str(path)
        self.level_count = int(info["levels"])
        self.level_dimensions = tuple((int(w), int(h)) for w, h in info["dims"])
        self.level_downsamples = tuple(float(d) for d in info["downsamples"])
        self.properties = dict(info.get("properties") or {})
        self.associated = tuple(info.get("associated") or ())
        self.dimensions = self.level_dimensions[0]

    def read_region(self, location, level, size):
        import numpy as np

        x, y = (int(v) for v in location)
        w, h = (int(v) for v in size)
        if _asker is None:
            raise PageSlideError(f"{PurePosixPath(self.path).name}: the page that held this slide is gone")
        found = _plain(_asker("slide_region", {"path": self.path, "level": int(level),
                                               "x": x, "y": y, "w": w, "h": h}))
        if not found or found.get("error"):
            raise PageSlideError(f"{PurePosixPath(self.path).name}: "
                                 f"{(found or {}).get('error') or 'the page could not read this region'}")
        data = found["data"]
        pixels = np.frombuffer(data if isinstance(data, (bytes, bytearray, memoryview)) else bytes(data),
                               dtype=np.uint8)
        return pixels.reshape(int(found["height"]), int(found["width"]), 4)

    def close(self):
        pass


#: The slides the page opened, by path: one question per pick, not per reader.
_slides: dict = {}


def page_slide(path):
    """The whole-slide image at `path` as the page reads it (`PageSlide`), or
    None where the page does not read it: not the website, not one of the
    page's files. Raises `PageSlideError` with the page's reason when it tried
    and could not -- an MRXS picked without the folder beside it says so."""
    if _asker is None or not is_page_file(path):
        return None
    key = str(path)
    held = _slides.get(key)
    if held is not None:
        return held
    info = _plain(_asker("slide", {"path": key}))
    if not info:
        raise PageSlideError(f"{PurePosixPath(key).name}: this browser could not open the slide")
    if info.get("error"):
        raise PageSlideError(f"{PurePosixPath(key).name}: {info['error']}")
    slide = PageSlide(key, info)
    _slides[key] = slide
    return slide


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
