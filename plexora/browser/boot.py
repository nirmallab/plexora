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
  the page exchanged against its licence (browser/licence.py) -- or None.

What it does, in order: the environment the app reads at build; the
`imagecodecs` stand-in, before anything imports tifffile; HTTP through the
browser's fetch for the standard library's urllib (`pyodide_http`), until the
packages that call out move to urllib3's Emscripten backend; cooperative
work; the wakeup, the column source and the page's answers; and the app
itself, built once.

Nothing here monkeypatches Plexora: every substitution the first proof needed
(P9's eight) is now a seam in the product, and each remaining one is a
function named here with a test that removes it when it is no longer needed.
"""

from __future__ import annotations

import os
import time


def environment(origin: str, *, data_root: str = "/data", licence_dir: str = "/bioc") -> None:
    """What the app reads from the environment as it builds (create_app) and
    as it runs: where projects live, that telemetry is the page's business,
    where the licence the page verified is, and that the page's own origin
    may write."""
    os.environ.update({
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


def start(origin: str, *, post=None, cancelled=None, idle=None, waiting=None, columns=None,
          ask=None, data_root: str = "/data", licence_dir: str = "/bioc") -> dict:
    """Boot once; returns what the page shows in its status chip and records:
    `{version, rules, seconds, shims}`."""
    started = time.perf_counter()
    environment(origin, data_root=data_root, licence_dir=licence_dir)
    shims = {"imagecodecs": codecs(), "pyodide_http": http()}

    from plexora.browser import cooperative, licence
    from plexora.server.models import viewer_sessions
    from plexora.server.providers import browser as browser_provider

    cooperative.install(post=post, cancelled=cancelled, idle=idle, waiting=waiting)
    if post is not None:
        viewer_sessions.set_wakeup(lambda: post({"kind": "wakeup"}))
    browser_provider.set_column_source(columns)
    browser_provider.set_asker(ask)
    licence.set_asker(ask)

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
