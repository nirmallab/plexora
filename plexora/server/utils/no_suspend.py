"""Where the website's Python must block rather than suspend.

On the website Python waits on the page and the network by suspending
(JavaScript Promise Integration: `pyodide.ffi.run_sync`). A suspension works
only while every frame between the call into Python and the wait is Python
or WebAssembly. A C library calling back into a Python file object -- h5py's
`fileobj` driver reading a remote .ims through `remote_store.RemoteFile` --
puts JavaScript frames there, and the wait ends Python for good:
"SuspendError: trying to suspend JS frames". 0.0.52 died that way opening a
349 GB .ims over SFTP (2026-11-12), reproduced in the live worker by h5py
reading a file object whose `readinto` waits on a promise.

Inside `held()` the website's waits block instead, as they do in a browser
without JSPI:
- coroutines (zarr's synchronous API, remote_store) run on the boot's
  `SyncLoop` (plexora/browser/boot.py `blocking_loop`);
- requests are synchronous XMLHttpRequests (plexora/browser/fetch.py
  `_fetcher`), which the site's service worker relays like any `fetch` --
  a files-only server's reads included;
- a question to the page goes over the shared-memory channel, a stage that
  is not loaded yet is refused, and a picked file is not read ahead
  (workers/pythonWorker.js).

Elsewhere -- the desktop, a node -- it is a counter nothing reads.
"""

from __future__ import annotations

import contextlib
import threading

_state = threading.local()


@contextlib.contextmanager
def held():
    """Waits inside block (the website); nests."""
    _state.depth = getattr(_state, "depth", 0) + 1
    try:
        yield
    finally:
        _state.depth -= 1


def active() -> bool:
    """Whether a wait here must block."""
    return getattr(_state, "depth", 0) > 0
