"""The cell table of a project opened on the website, read from the page.

On the website Plexora's Python runs in a Web Worker beside the viewer
(Pyodide), and a table the user picked has already been parsed by the page:
its JavaScript column store reads a 1.26 M-row CSV in about a second, where
polars in WebAssembly cannot parse it at all (it has no threads to spawn). So a
project whose table binding says `provider: "browser"` takes its columns from
the page instead of reading the file, and everything after the read --
normalisation, the corrections a project records, every frame computation -- is
`LocalTableProvider`'s, unchanged.

The page registers how to reach its columns (`set_column_source`) when the
worker boots. Anywhere else -- the desktop app, a server, a node -- there is no
page and no column store, and loading such a table says so: the project came
from a browser, and moving it here means binding the table to the file again.
"""

from __future__ import annotations

from plexora.server.providers.base import BROWSER, ResourceLocator, ResourceNotLocal
from plexora.server.providers.local import LocalTableProvider

_column_source = None


def set_column_source(source) -> None:
    """`source(spec)` -> a polars DataFrame of the table `spec` names, as the
    page's column store holds it (None: the page has no copy, read the file).
    Installed by the website's worker at boot; None removes it."""
    global _column_source
    _column_source = source


class BrowserTableProvider(LocalTableProvider):
    """`LocalTableProvider`, with the rows from the page's column store."""

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
        frame = _column_source(self._spec)
        if frame is None:
            # The page has no copy (it was forgotten to free memory): the file
            # is mounted where the binding says, so read it as any other.
            return super()._open_adapter()
        return MemoryFrameAdapter(self._spec, frame)
