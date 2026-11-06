"""What Pyodide needs before Plexora's gating path imports (P9).

Each shim is one function, counted by `install()` (its source lines go into the
record as `shims`), and none of them changes what the gating path computes:
they substitute scheduling (threads run inline), encoding (zlib for zlib-ng)
and the licence store (a throwaway issuer, as the test suite's), never a
gate, a packet field or a state transition.

    import p9_boot
    shims = p9_boot.install(site="/lib/python3.14/site-packages", data_root="/data")

Runs under CPython too (`truth_p9.py` installs only `licence`), so the
reference and the browser share the licence and the options.
"""

from __future__ import annotations

import inspect
import os
import sys
import types

SHIMS = []


def shim(function):
    SHIMS.append(function)
    return function


@shim
def stub_root(site):
    """`plexora/__init__.py` builds the Flask app at import (`app =
    create_app()`), pulling every route; the gating path needs only its
    package path, `__version__` and `get_config`."""
    from importlib.metadata import version

    root = types.ModuleType("plexora")
    root.__path__ = [os.path.join(site, "plexora")]
    root.__file__ = os.path.join(site, "plexora", "__init__.py")
    root.__version__ = version("plexora")
    root.app = None

    def get_config():
        from plexora.server.models.project import Project

        return Project.load_all()

    root.get_config = get_config
    root.get_config_names = lambda: list(get_config().keys())
    sys.modules["plexora"] = root


@shim
def zlib_for_zlibng():
    """`server/utils/fast_png.py` imports imagecodecs (no wasm build) for one
    call, `zlibng_encode`: zlib's own deflate stands in while the module
    imports, then the stand-in leaves sys.modules so tifffile never sees it."""
    import zlib

    stand_in = types.ModuleType("imagecodecs")
    stand_in.zlibng_encode = lambda data, level=6: zlib.compress(bytes(data), level)
    sys.modules["imagecodecs"] = stand_in
    try:
        import plexora.server.utils.fast_png  # noqa: F401
    finally:
        del sys.modules["imagecodecs"]


@shim
def deferred_threads():
    """No threads in Pyodide: `Thread.start` queues the target (agent/jobs.py's
    jobs: the bulk pass, the classifier), and it runs to completion where its
    starter would yield to it -- a join, a sleep or an Event wait (nothing
    else could set the event meanwhile) -- never inside the starter's own call.
    jobs.JobStore._run catches what a job raises."""
    import threading
    import time

    def run_pending():
        while start.queue:
            thread = start.queue.pop(0)
            t0 = time.perf_counter()
            thread.run()
            thread._p9_done = True
            start.ran.append([thread.name, time.perf_counter() - t0])

    def start(self):
        self._started.set()
        start.queue.append(self)

    start.queue, start.ran, start.run_pending = [], [], run_pending
    threading.Thread.start = start
    threading.Thread.join = lambda self, timeout=None: run_pending()
    threading.Thread.is_alive = lambda self: self._started.is_set() and not hasattr(self, "_p9_done")
    threading.Event.wait = lambda self, timeout=None: (run_pending(), self.is_set())[1]


@shim
def jobs_between_calls():
    """What an idle worker does between messages: a job queued by a capability
    call (the classifier, started mid-session) runs once that call returns,
    where CPython would have run it on its own thread meanwhile."""
    import threading

    from plexora import agent
    from plexora.agent import registry

    def invoke(*args, **kwargs):
        invoke.depth += 1
        try:
            return real(*args, **kwargs)
        finally:
            invoke.depth -= 1
            if not invoke.depth:
                threading.Thread.start.run_pending()

    real, invoke.depth = registry.invoke, 0
    registry.invoke = agent.invoke = invoke


@shim
def sync_executor():
    """`ThreadPoolExecutor` (autogate/bulk.py, evidence/crops.py) as a pool
    that runs each call when it is submitted."""
    import concurrent.futures as futures
    import concurrent.futures.thread as thread_module

    class SyncExecutor(futures.Executor):
        def __init__(self, *args, **kwargs):
            pass

        def submit(self, fn, /, *args, **kwargs):
            future = futures.Future()
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001 -- delivered as the pool would
                future.set_exception(exc)
            return future

    futures.ThreadPoolExecutor = thread_module.ThreadPoolExecutor = SyncExecutor


@shim
def yielding_sleep():
    """`time.sleep` is where a poll (gating_next while the bulk pass runs)
    yields to the work it waits on: queued threads run, then no time passes.
    Counted, with the seconds that would have been slept."""
    import threading
    import time

    real = time.sleep

    def sleep(seconds):
        sleep.calls += 1
        sleep.seconds += seconds
        threading.Thread.start.run_pending()
        real(0)

    sleep.calls, sleep.seconds = 0, 0.0
    time.sleep = sleep


@shim
def parquet_by_pyarrow():
    """Pyodide's polars is built without Parquet (no `sink_parquet`, no
    `new_from_parquet`); the gate store (api/store.py) writes and reads it.
    pyarrow, which Pyodide ships, reads and writes the same format. When the
    boot did not load it, it is fetched at the first Parquet call (JSPI's
    `run_sync` lets this synchronous call wait for the download), and polars,
    which decided at its import that pyarrow was absent, is told it is not."""
    import polars as pl
    from polars import dependencies as deps

    if hasattr(type(pl.LazyFrame()._ldf), "sink_parquet"):
        return
    write, read = pl.DataFrame.write_parquet, pl.read_parquet

    def arrow():
        if not deps._PYARROW_AVAILABLE:
            import pyodide_js
            from pyodide.ffi import run_sync

            run_sync(pyodide_js.loadPackage("pyarrow"))
            deps.pyarrow._module_available = deps._PYARROW_AVAILABLE = True

    def write_parquet(self, file, *args, **kwargs):
        arrow()
        return write(self, file, *args, **{"use_pyarrow": True, **kwargs})

    def read_parquet(source, *args, **kwargs):
        arrow()
        return read(source, *args, **{"use_pyarrow": True, **kwargs})

    pl.DataFrame.write_parquet, pl.read_parquet = write_parquet, read_parquet


@shim
def licence(directory):
    """A Pro test licence from a throwaway key, as tests/conftest.py's
    `_issuer` mints one, into an offline licence directory."""
    os.environ.update({"BIOCOGNIA_DIR": directory, "BIOCOGNIA_OFFLINE": "1",
                       "BIOCOGNIA_NO_HEARTBEAT": "1"})
    if not hasattr(os, "link"):
        # Pyodide's os has no link(); biocognia.store's exclusive create
        # already falls back to a replace on OSError, so raise that.
        def link(src, dst):
            raise OSError(38, "link() is not available here")

        os.link = link
    from biocognia.testing import Issuer

    from plexora.licensing import LICENSING, PRODUCT

    class Patch:  # what Issuer asks of pytest's monkeypatch
        setattr = staticmethod(setattr)

    issuer = Issuer(Patch(), PRODUCT)
    issuer.install(LICENSING, issuer.issue(PRODUCT, ent=["ai"]))


def prototype_metadata_cache():
    """Not a shim: a prototype of a product change, applied the same way on
    both sides (`--params '{"prototype": "metacache"}'`, truth_p9.py
    --prototype metacache). A gating session reads one image's TIFF header
    about 400 times (QC's input fingerprints ask `pixel_scale.pixel_size` per
    cell-exclusion check; `brightfield.is_rgb_layout` likewise); both readers
    are kept per (path, size, mtime) here, which a file that has not changed
    makes safe."""
    import functools
    import os as _os

    from plexora.server.utils import brightfield, pixel_scale

    def keyed(fn):
        cached = functools.lru_cache(maxsize=64)(lambda path, size, mtime: fn(path))

        def read(path):
            try:
                stat = _os.stat(path)
            except OSError:
                return fn(path)
            return cached(str(path), stat.st_size, stat.st_mtime_ns)
        return read

    pixel_scale._local_metadata = keyed(pixel_scale._local_metadata)
    brightfield._tiff_layout = keyed(brightfield._tiff_layout)


def environment(data_root):
    """Plexora's environment, as tests/conftest.py's `plexora_data_root` sets it."""
    os.environ.update({"PLEXORA_DATA_PATH": data_root, "PLEXORA_TELEMETRY": "off",
                       "BIOCOGNIA_MARKER_GRAPH": os.path.join(data_root, "_markergraph")})
    for name in ("PLEXORA_SHARED_PATH", "PLEXORA_VOCABULARY_PATH"):
        os.environ.pop(name, None)


def install(site, data_root, licence_dir="/bioc"):
    """Every shim, in order; returns {name: source lines}."""
    environment(data_root)
    stub_root(site)
    zlib_for_zlibng()
    deferred_threads()
    jobs_between_calls()
    sync_executor()
    yielding_sleep()
    parquet_by_pyarrow()
    licence(licence_dir)
    return {fn.__name__: {"lines": len(inspect.getsource(fn).splitlines()), "code": code_lines(fn)}
            for fn in SHIMS}


def code_lines(fn):
    """A shim's lines of code: its source less the docstring, comments and blanks."""
    import ast
    import textwrap

    source = textwrap.dedent(inspect.getsource(fn))
    body = ast.parse(source).body[0].body
    skip = set()
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        skip = set(range(body[0].lineno, body[0].end_lineno + 1))
    return sum(1 for i, line in enumerate(source.splitlines(), 1)
               if i not in skip and line.strip() and not line.strip().startswith(("#", "@")))


def counters():
    """What the shims saw: sleeps, and each thread run inline (name, seconds)."""
    import threading
    import time

    return {"sleep": {"calls": getattr(time.sleep, "calls", None),
                      "seconds": getattr(time.sleep, "seconds", None)},
            "threads": getattr(threading.Thread.start, "ran", None)}
