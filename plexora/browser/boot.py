"""Start Plexora's Python in the website's worker.

`workers/pythonWorker.js` loads Pyodide and the base packages, mounts the
user's files and the project store, and then calls `start` here once. Every
part that talks to the page is handed in as a function -- JavaScript functions
arrive as callables -- so the same boot runs under CPython in the tests
(tests/test_boot.py) with Python functions in their place:

- `post(message)`: a dict to the page (job records, conversation events,
  wakeups);
- `cancelled(job_id)`: the page's cancel flags (shared memory);
- `idle(seconds)`: let time pass while the page is served;
- `waiting()`: whether a request is waiting to be served;
- `columns(spec)`: a picked table's frame, its columns parsed by the page
  (plexora/browser/columns.py);
- `ask(kind, args)`: a question only the page can answer -- a picked image's
  full-resolution maximum (providers/browser.py `page_window`), the AI token
  the page exchanged against its licence (browser/licence.py), magic
  select's model run (plexora/browser/sam_page.py) -- or None. numpy arrays
  at the top level of `args` cross as `{dtype, shape, data}`;
- `wait_on(promise)`: wait for a promise (the network) while the page is
  served;
- `deliver(source, staged)`: a picked file Python wrote, as a staged copy,
  handed to the page to save over the original (server/utils/
  source_writes.py; the worker's `plexoraSourceWrite`).

What it does, in order: the environment the app reads at build; the
`imagecodecs` stand-in, before anything imports tifffile; the standard
library's urllib over the page's `fetch` (plexora/browser/fetch.py: streamed,
with urllib's errors); cooperative work, spatialagents' threads and sleeps
through it (plexora/browser/agents.py) and conversations' events to the page;
the wakeup, the column source and the page's answers (magic select's model
among them); and the app itself, built once.

Nothing here monkeypatches Plexora: every substitution the first proof needed
(P9's eight) is now a seam in the product, and each remaining one is a
function named here with a test that removes it when it is no longer needed.
"""

from __future__ import annotations

import asyncio
import os
import time


def environment(origin: str, *, base_url: str = "", data_root: str = "/data",
                licence_dir: str = "/bioc", telemetry: dict | None = None) -> None:
    """What the app reads from the environment as it builds (create_app) and
    as it runs: where projects live, where the licence the page verified is,
    that the page's own origin may write, the path the site is served under
    (`base_url`: "" at a site's root; `/rnode/<host>/<port>/app` when a node
    serves the app behind a portal), which every page's links carry, and
    telemetry's endpoint and Do Not Track (`telemetry`: `{endpoint, dnt}`
    from the page, plexora/browser/telemetry.py; None or no endpoint: off)."""
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    endpoint = str(telemetry.get("endpoint") or "").strip()
    os.environ.update({
        "PLEXORA_BASE_URL": base_url or "",
        "PLEXORA_DATA_PATH": data_root,
        "PLEXORA_ALLOWED_ORIGINS": origin,
        # The page's own address, which its connections to servers are
        # reached at (server/models/web_remote.py).
        "PLEXORA_SITE_ORIGIN": origin,
        "PLEXORA_WORK": "cooperative",
        "PLEXORA_BACKEND": "browser",
        "PLEXORA_NO_NUMBA": "1",
        "BIOCOGNIA_DIR": licence_dir,
        "BIOCOGNIA_NO_HEARTBEAT": "1",
    })
    for name in ("PLEXORA_SHARED_PATH", "PLEXORA_AUTH_TOKEN", "PLEXORA_TRUSTED_HOSTS",
                 "PLEXORA_PLUGINS", "PLEXORA_TELEMETRY", "PLEXORA_TELEMETRY_ENDPOINT",
                 "DO_NOT_TRACK"):
        os.environ.pop(name, None)
    if endpoint:
        os.environ["PLEXORA_TELEMETRY_ENDPOINT"] = endpoint
    else:
        # A build that names no endpoint (a test's) sends nothing, as before.
        os.environ["PLEXORA_TELEMETRY"] = "off"
    if telemetry.get("dnt"):
        os.environ["DO_NOT_TRACK"] = "1"


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


def http(wait_on=None) -> bool:
    """urllib through the page's `fetch` (plexora/browser/fetch.py), where
    there is no socket: streamed, urllib's errors, and the page served while
    Python waits on the network (`wait_on`). True if installed."""
    from plexora.browser import fetch

    return fetch.install(wait_on)


def buckets() -> bool:
    """`s3://`, `gs://` and `https://` read through the page's `fetch`
    (plexora/browser/buckets.py), where aiohttp, s3fs and gcsfs cannot run.
    True if installed."""
    from plexora.browser import buckets as page_buckets

    return page_buckets.install()


def cache() -> str:
    """The chunk cache of what was read from a bucket or web host, in the
    site's own storage rather than under /data, which the worker holds in
    memory and copies into the project store (plexora/browser/cachefiles.py):
    what it is kept in."""
    from plexora.browser import cachefiles

    return cachefiles.install()


def agents() -> dict:
    """spatialagents' threads and sleeps through `_work` (plexora/browser/
    agents.py): what was replaced."""
    from plexora.browser import agents as page_agents

    return page_agents.install()


def chat_events(post) -> None:
    """Every event a conversation records, sent to the page as it is
    recorded (`{kind: "chat", conversation, event}`): the page answers the
    chat panel's poll from them while the worker runs the turn, and wakes the
    poll it holds for that conversation (services/pythonClient.js)."""
    from plexora.ai.harness import conversations

    if post is None:
        conversations.set_observer(None)
        return
    conversations.set_observer(lambda conversation_id, event: post(
        {"kind": "chat", "conversation": conversation_id, "event": event}))


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

    def run_in_executor(self, executor, func, *args):
        """`func(*args)` here and now, as Pyodide's own loop runs it: the worker
        has no threads, and `asyncio.to_thread` (remote_store's chunk cache
        reads its files through it) would start one ("can't start new
        thread")."""
        future = self.create_future()
        try:
            future.set_result(func(*args))
        except Exception as exc:  # noqa: BLE001 -- the caller's to see
            future.set_exception(exc)
        return future


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
          columns=None, ask=None, wait_on=None, jspi: bool = True, sleep=None,
          deliver=None, data_root: str = "/data",
          licence_dir: str = "/bioc", telemetry: dict | None = None) -> dict:
    """Boot once; returns what the page shows in its status chip and records:
    `{version, rules, seconds, shims}`. `jspi` False: the browser has no JSPI,
    and `sleep(seconds)` blocks (the worker's Atomics.wait)."""
    started = time.perf_counter()
    environment(origin, base_url=base_url, data_root=data_root, licence_dir=licence_dir,
                telemetry=telemetry)
    shims = {"imagecodecs": codecs(), "fetch": http(wait_on), "parquet": parquet(),
             "buckets": buckets(), "cache": cache()}
    if not jspi:
        shims["sync_loop"] = without_jspi(sleep or time.sleep)
    from plexora.browser import cooperative, licence
    from plexora.server.models import viewer_sessions
    from plexora.server.providers import browser as browser_provider

    cooperative.install(post=post, cancelled=cancelled, idle=idle, waiting=waiting)
    shims["spatialagents"] = sorted(agents())
    chat_events(post)
    if post is not None:
        viewer_sessions.set_wakeup(lambda: post({"kind": "wakeup"}))
    browser_provider.set_column_source(columns)
    browser_provider.set_asker(ask)
    licence.set_asker(ask)

    from plexora.server.models import web_remote
    from plexora.server.utils import sftp

    # Files-only servers: the page holds their connections, and Python finds
    # one when it first reads an `sftp://` address there.
    sftp.set_resolver(web_remote.files_client)

    from plexora.server.utils import source_writes

    # Saving into a picked file: the user's setting is the page's answer, and
    # the staged copy is the page's to save (services/sourceWrites.js).
    source_writes.configure(ask=ask, deliver=deliver)

    from plexora.browser import sam_page

    sam_page.install(ask)

    import plexora

    app = plexora.app

    from plexora import _work
    from plexora.server.models import data_model

    # PIL's format plugins and the tile encoders' first use, which the
    # desktop pays before its first request (data_model.prime_hot_code): here
    # behind the first page instead, as background work, so a visit's start
    # does not wait for it and the first image Python encodes does not either.
    _work.spawn(data_model.prime_encoders, background=True, name="prime-encoders")

    def scipy_stage():
        # The `scipy` stage (plexora/browser/stages.py), no longer in the
        # base: fetched and compiled once the first page has been answered,
        # while the user looks at it, rather than inside the request that
        # opens a project with cells (its cell index imports scipy).
        try:
            import scipy  # noqa: F401
        except ImportError:  # a stage that could not load: the index says so when asked
            pass

    _work.spawn(scipy_stage, background=True, name="stage-scipy")
    return {"version": plexora.__version__, "rules": len(list(app.url_map.iter_rules())),
            "seconds": round(time.perf_counter() - started, 3), "shims": shims}


def stage(name: str) -> None:
    """A package stage the page has just loaded (`stages.json`): whatever it
    needs before Plexora's adapters import it."""
    if name in ("parquet", "h5ad"):
        # polars decided at its own import that pyarrow and pandas were absent
        # (they were); it is told otherwise now they are here.
        _tell_polars()
    if name == "dicom":
        _venv_stand_in()
    if name == "spatialdata":
        _numba_stand_in()
        import dask

        # One thread: dask's default scheduler starts a pool.
        dask.config.set(scheduler="synchronous")


def _venv_stand_in() -> None:
    """wsidicom 0.35 imports `logger` from the stdlib's `venv` (wsidicom/file/
    io/frame_index/extended.py, a stray auto-import), and Pyodide's standard
    library has no `venv`: `import wsidicom` failed on the website. A module
    with a logger of that name is all it uses."""
    try:
        import venv  # noqa: F401
    except ImportError:
        import logging
        import sys
        import types

        stand_in = types.ModuleType("venv")
        stand_in.logger = logging.getLogger("venv")
        sys.modules["venv"] = stand_in


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
