"""Start Plexora's Python in the website's worker.

`workers/pythonWorker.js` loads Pyodide and the base packages, mounts the
user's files and the project store, and then calls `start` here once. Every
part that talks to the page is handed in as a function -- JavaScript functions
arrive as callables -- so the same boot runs under CPython in the tests
(tests/test_boot.py) with Python functions in their place:

- `post(message)`: a dict to the page (job records, wakeups);
- `cancelled(job_id)`: the page's cancel flags (shared memory);
- `idle(seconds)`: let time pass while the page is served;
- `waiting()`: whether a request is waiting to be served;
- `columns(spec)`: a picked table's frame, its columns parsed by the page
  (plexora/browser/columns.py);
- `ask(kind, args)`: a question only the page can answer -- a picked image's
  full-resolution maximum (providers/browser.py `page_window`), the AI token
  the page exchanged against its licence (browser/licence.py), magic
  select's model run (plexora/browser/sam_page.py) -- or None. numpy arrays
  at the top level of `args` cross as `{dtype, shape, data}`.

What it does, in order: the environment the app reads at build; the
`imagecodecs` stand-in, before anything imports tifffile; HTTP through the
browser's fetch for the standard library's urllib (`pyodide_http`), until the
packages that call out move to urllib3's Emscripten backend; cooperative
work; the wakeup, the column source and the page's answers (magic select's
model among them); and the app itself, built once.

Nothing here monkeypatches Plexora: every substitution the first proof needed
(P9's eight) is now a seam in the product, and each remaining one is a
function named here with a test that removes it when it is no longer needed.
"""

from __future__ import annotations

import asyncio
import os
import time


def environment(origin: str, *, base_url: str = "", data_root: str = "/data",
                licence_dir: str = "/bioc") -> None:
    """What the app reads from the environment as it builds (create_app) and
    as it runs: where projects live, that telemetry is the page's business,
    where the licence the page verified is, that the page's own origin may
    write, and the path the site is served under (`base_url`: "" at a site's
    root; `/rnode/<host>/<port>/app` when a node serves the app behind a
    portal), which every page's links carry."""
    os.environ.update({
        "PLEXORA_BASE_URL": base_url or "",
        "PLEXORA_DATA_PATH": data_root,
        "PLEXORA_TELEMETRY": "off",
        "PLEXORA_ALLOWED_ORIGINS": origin,
        "PLEXORA_WORK": "cooperative",
        "PLEXORA_BACKEND": "browser",
        "PLEXORA_NO_NUMBA": "1",
        "BIOCOGNIA_DIR": licence_dir,
        "BIOCOGNIA_NO_HEARTBEAT": "1",
    })
    for name in ("PLEXORA_SHARED_PATH", "PLEXORA_AUTH_TOKEN", "PLEXORA_TRUSTED_HOSTS",
                 "PLEXORA_PLUGINS"):
        os.environ.pop(name, None)


def codecs() -> bool:
    """The imagecodecs stand-in, when there is no imagecodecs (there never is
    in Pyodide; under CPython there is, and nothing changes). True if
    installed."""
    try:
        import imagecodecs  # noqa: F401
    except ImportError:
        from plexora.browser import imagecodecs_shim

        imagecodecs_shim.install()
        return True
    return False


def http() -> bool:
    """urllib through the browser's fetch (`pyodide_http`), where there is no
    socket. True if patched."""
    try:
        import pyodide_http
    except ImportError:
        return False
    pyodide_http.patch_all()
    return True


def native_parquet() -> bool:
    """Whether this polars reads and writes Parquet itself. Pyodide's is
    built without it (no `sink_parquet`, no `new_from_parquet`)."""
    import polars as pl

    return hasattr(type(pl.LazyFrame()._ldf), "sink_parquet")


def parquet(native=None) -> bool:
    """Parquet through pyarrow where polars has none (`native_parquet`).

    Plexora keeps tables beside a project as Parquet -- a z-stack's measured
    objects (volume/objects.py), a surface's object table (surface/store.py),
    QC results -- and the desktop's are the same files, so the format stays.
    pyarrow reads and writes it: its `parquet` stage is fetched the first time
    one is read or written (an import from here loads it, through JSPI), and
    polars, which decided at its own import that pyarrow was absent, is told
    it is not (`_tell_polars`). `scan_parquet` reads eagerly: these tables
    are a row per object, not a run's transcripts. True if patched."""
    import polars as pl

    if native_parquet() if native is None else native:
        return False
    write, read = pl.DataFrame.write_parquet, pl.read_parquet

    def arrow():
        import pyarrow.parquet  # noqa: F401 -- the parquet stage, on first use

        _tell_polars()

    def write_parquet(self, file, *args, **kwargs):
        arrow()
        kwargs["use_pyarrow"] = True
        return write(self, file, *args, **kwargs)

    def read_parquet(source, *args, n_rows=None, **kwargs):
        arrow()
        kwargs["use_pyarrow"] = True
        frame = read(source, *args, **kwargs)
        return frame if n_rows is None else frame.head(n_rows)

    def scan_parquet(source, *args, n_rows=None, **kwargs):
        # What a scan takes and a read does not (row-group hints, cloud
        # options), dropped: an eager read has no use for them.
        keep = {k: v for k, v in kwargs.items() if k in ("columns", "storage_options")}
        return read_parquet(source, n_rows=n_rows, **keep).lazy()

    write_parquet.__doc__, read_parquet.__doc__ = write.__doc__, read.__doc__
    pl.DataFrame.write_parquet = write_parquet
    pl.read_parquet = read_parquet
    pl.scan_parquet = scan_parquet
    return True


class _NoSelector:
    """What `asyncio.BaseEventLoop._run_once` asks of a selector, for a loop
    that watches no file or socket: the wait for the next timer is a sleep."""

    def __init__(self, sleep):
        self._sleep = sleep

    def select(self, timeout=None):
        if timeout:
            self._sleep(timeout)
        return []

    def close(self):
        pass


class SyncLoop(asyncio.BaseEventLoop):
    """An event loop with no I/O of its own, run to completion inside the call
    that needs a coroutine's answer. Pyodide's loop blocks for one only through
    JSPI (`run_until_complete` -> `run_sync`), and Python's selector loop needs a
    socket pair Pyodide has not got; this one needs neither, because what Plexora
    runs this way -- zarr's synchronous API over a store tifffile or the page
    reads -- never waits on a socket."""

    def __init__(self, sleep=time.sleep):
        super().__init__()
        self._selector = _NoSelector(sleep)

    def _process_events(self, event_list):
        pass

    def _write_to_self(self):
        pass


#: The loop `run_to_completion` keeps: one, so what a library binds to the
#: loop it first ran on (an asyncio.Lock in a store) still works the next time.
_sync_loop = None


def run_to_completion(awaitable, sleep=time.sleep):
    """`awaitable`'s result, on a `SyncLoop`, whatever loop is said to be
    running (Pyodide's always is). A call from inside one (a coroutine on it
    asking again) gets a loop of its own."""
    global _sync_loop
    nested = _sync_loop is not None and _sync_loop.is_running()
    if _sync_loop is None or _sync_loop.is_closed():
        _sync_loop = SyncLoop(sleep)
    loop = SyncLoop(sleep) if nested else _sync_loop
    running = asyncio.events._get_running_loop()
    asyncio.events._set_running_loop(None)
    try:
        return loop.run_until_complete(awaitable)
    finally:
        asyncio.events._set_running_loop(running)
        if nested:
            loop.close()


def without_jspi(sleep=time.sleep) -> bool:
    """Where the browser has no JavaScript Promise Integration (Firefox and
    Safari before it shipped): Pyodide's loop cannot block for a coroutine, and
    zarr's synchronous API -- every TIFF Plexora opens goes through it -- says
    so and stops. Its `run_until_complete` runs the coroutine on a `SyncLoop`
    instead. True if patched."""
    try:
        from pyodide import webloop
    except ImportError:
        return False
    original = webloop.WebLoop.run_until_complete
    if getattr(original, "plexora_sync", False):
        return True

    def run_until_complete(self, future):
        if asyncio.isfuture(future) and getattr(future, "get_loop", lambda: None)() is self:
            return original(self, future)  # a task of this loop: only JSPI can wait for it
        return run_to_completion(future, sleep)

    run_until_complete.plexora_sync = True
    webloop.WebLoop.run_until_complete = run_until_complete
    return True


def start(origin: str, *, base_url: str = "", post=None, cancelled=None, idle=None, waiting=None,
          columns=None, ask=None, jspi: bool = True, sleep=None, data_root: str = "/data",
          licence_dir: str = "/bioc") -> dict:
    """Boot once; returns what the page shows in its status chip and records:
    `{version, rules, seconds, shims}`. `jspi` False: the browser has no JSPI,
    and `sleep(seconds)` blocks (the worker's Atomics.wait)."""
    started = time.perf_counter()
    environment(origin, base_url=base_url, data_root=data_root, licence_dir=licence_dir)
    shims = {"imagecodecs": codecs(), "pyodide_http": http(), "parquet": parquet()}
    if not jspi:
        shims["sync_loop"] = without_jspi(sleep or time.sleep)

    from plexora.browser import cooperative, licence
    from plexora.server.models import viewer_sessions
    from plexora.server.providers import browser as browser_provider

    cooperative.install(post=post, cancelled=cancelled, idle=idle, waiting=waiting)
    if post is not None:
        viewer_sessions.set_wakeup(lambda: post({"kind": "wakeup"}))
    browser_provider.set_column_source(columns)
    browser_provider.set_asker(ask)
    licence.set_asker(ask)

    from plexora.browser import sam_page

    sam_page.install(ask)

    import plexora

    app = plexora.app
    return {"version": plexora.__version__, "rules": len(list(app.url_map.iter_rules())),
            "seconds": round(time.perf_counter() - started, 3), "shims": shims}


def stage(name: str) -> None:
    """A package stage the page has just loaded (`stages.json`): whatever it
    needs before Plexora's adapters import it."""
    if name in ("parquet", "h5ad"):
        # polars decided at its own import that pyarrow and pandas were absent
        # (they were); it is told otherwise now they are here.
        _tell_polars()
    if name == "spatialdata":
        _numba_stand_in()
        import dask

        # One thread: dask's default scheduler starts a pool.
        dask.config.set(scheduler="synchronous")


def _tell_polars() -> None:
    try:
        from polars import dependencies
    except ImportError:
        return
    for module, flag in (("pyarrow", "_PYARROW_AVAILABLE"), ("pandas", "_PANDAS_AVAILABLE")):
        import importlib.util

        if importlib.util.find_spec(module) is not None:
            lazy = getattr(dependencies, module, None)
            if lazy is not None and hasattr(lazy, "_module_available"):
                lazy._module_available = True
            if hasattr(dependencies, flag):
                setattr(dependencies, flag, True)


def _numba_stand_in() -> None:
    """spatialdata imports numba at module scope for a few optional kernels
    Plexora never calls; there is no numba for WebAssembly. Its decorators
    become the identity, which `plexora.server.utils.jit` already treats as
    "run the Python" (PLEXORA_NO_NUMBA)."""
    import sys
    import types

    if "numba" in sys.modules:
        return
    numba = types.ModuleType("numba")
    numba.__version__ = "0+plexora-browser"

    def decorator(*args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]
        return lambda function: function

    for name in ("jit", "njit", "vectorize", "guvectorize", "generated_jit", "cfunc",
                 "stencil", "jit_module"):
        setattr(numba, name, decorator)
    class _Anything:
        """`numba.types.float64[:]`, `numba.types.int64(...)` in a signature."""

        def __getattr__(self, name):
            return _Anything()

        def __getitem__(self, key):
            return _Anything()

        def __call__(self, *args, **kwargs):
            return _Anything()

    numba.prange = range
    numba.types = _Anything()
    numba.typed = _Anything()
    numba.core = _Anything()
    numba.config = types.SimpleNamespace(DISABLE_JIT=True)
    sys.modules["numba"] = numba
