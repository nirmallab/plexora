"""Compiled kernels for the loops numpy cannot vectorise, and how they are primed.

Most of the analysis Plexora does is whole-column numpy: a sort, a
`searchsorted`, a `bincount`. A few things are loops by nature -- counting each
cell's neighbours within a radius, a label mask's per-label bounding boxes,
Hartigan's dip statistic -- and in Python they are the slowest line of every
profile. Those are written once, as plain numpy-typed Python, and compiled
with numba through `njit` here.

Three rules, and why:

- **`nogil=True, cache=True`, never `parallel=True`.** A kernel runs on a
  request or job thread beside Waitress's pool and the BLAS pool that
  `plexora._resources` caps; numba's own threading layer would be a third pool
  nobody sized. `nogil` lets two threads each run one kernel instead.
- **Compiled before any request can need it** (`prime`). Compiling is an LLVM
  run that imports and dlopens on first call -- exactly the first-use work
  `data_model.prime_hot_code` exists to take off the request path, for the
  same deadlock it describes. Every kernel module registers a `prime()` that
  calls each kernel once on tiny arrays; `prime_hot_code` and the MCP server
  call `prime()` at start.
- **An identity fallback.** Without numba (or with `PLEXORA_NO_NUMBA=1`) `njit`
  returns the function unchanged, so every kernel still runs -- slowly, as the
  Python it is written in -- and the tests run both ways to prove they agree.

The compiled-code cache goes under the user's cache directory, not beside the
source: a bundled install's site-packages may be read-only, and a data root is
the user's work, not ours.
"""

from __future__ import annotations

import importlib
import os
import threading

#: Kernel modules that have a `prime()` to call, by import path.
_PRIMERS: list[str] = []
_PRIMED: set[str] = set()
_LOCK = threading.Lock()

_NUMBA = None
_CHECKED = False


def _cache_dir() -> str:
    try:
        from platformdirs import user_cache_dir

        return os.path.join(user_cache_dir("plexora", appauthor=False), "numba")
    except Exception:  # pragma: no cover - platformdirs is a core dependency
        return os.path.join(os.path.expanduser("~"), ".cache", "plexora", "numba")


def numba_module():
    """numba, or None when it is missing or switched off."""
    global _NUMBA, _CHECKED
    if _CHECKED:
        return _NUMBA
    _CHECKED = True
    if os.environ.get("PLEXORA_NO_NUMBA", "").strip() not in ("", "0"):
        return None
    os.environ.setdefault("NUMBA_CACHE_DIR", _cache_dir())
    try:
        import numba
    except Exception:
        return None
    _NUMBA = numba
    return numba


def enabled() -> bool:
    """Whether kernels are compiled in this process."""
    return numba_module() is not None


def njit(fn=None, **options):
    """`numba.njit` with Plexora's defaults, or the function itself.

    Usable bare (`@njit`) or with options (`@njit(fastmath=False)`).
    """
    options.setdefault("cache", True)
    options.setdefault("nogil", True)
    if options.pop("parallel", False):
        raise ValueError("Plexora kernels never use parallel=True; see plexora/server/utils/jit.py")

    def wrap(function):
        numba = numba_module()
        if numba is None:
            return function
        return numba.njit(**options)(function)

    if fn is not None and callable(fn):
        return wrap(fn)
    return wrap


def register_primer(module: str):
    """Name a module whose `prime()` compiles its kernels. Idempotent."""
    with _LOCK:
        if module not in _PRIMERS:
            _PRIMERS.append(module)


#: Kernel modules known before anything imports them, so `prime()` reaches
#: every one of them even in a process that has not used them yet.
for _name in ("plexora.server.utils.label_kernels",
              "plexora.plugins.gating.server.autogate.kernels",
              "plexora.plugins.qc.server.segqc.kernels"):
    register_primer(_name)


def prime(log=None) -> list:
    """Compile every registered kernel now. Returns the modules primed.

    Never raises: a kernel module that fails to import or compile is reported
    and skipped, and its kernels are then compiled on first use instead.
    """
    done = []
    with _LOCK:
        modules = list(_PRIMERS)
    for name in modules:
        with _LOCK:
            if name in _PRIMED:
                continue
        try:
            module = importlib.import_module(name)
            primer = getattr(module, "prime", None)
            if callable(primer):
                primer()
        except Exception as exc:
            if log:
                log(f"  could not compile kernels in {name}: {exc}")
            continue
        with _LOCK:
            _PRIMED.add(name)
        done.append(name)
    return done
