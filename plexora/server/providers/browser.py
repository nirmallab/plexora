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

import os

from plexora.server.providers.base import BROWSER, ResourceLocator, ResourceNotLocal
from plexora.server.providers.local import LocalTableProvider

_column_source = None


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
    if not path.startswith("/picked/") or not path.lower().endswith((".tif", ".tiff")):
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
