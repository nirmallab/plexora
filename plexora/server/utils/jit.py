"""Compiled kernels for the loops numpy cannot vectorise, and how they are primed.

Most of the analysis Plexora does is whole-column numpy: a sort, a
`searchsorted`, a `bincount`. A few things are loops by nature -- counting each
cell's neighbours within a radius, a label mask's per-label bounding boxes,
Hartigan's dip statistic -- and in Python they are the slowest line of every
profile. Those are written once, as plain numpy-typed Python, and compiled
through `njit` here, by the first of:

1. **plexora-kernels** (`plexora_kernels`, the `kernels/` directory of this
   repository): the same loops ported to Cython, built ahead of time as wheels
   for Windows, macOS, Linux and Pyodide. Nothing compiles at run time, so the
   desktop has no LLVM warm-up, and the website -- where there is no numba --
   runs them as WebAssembly instead of as Python.
2. **numba**, compiling the Python on first use.
3. **The Python itself.**

Four rules, and why:

- **A port is used only for the code it was ported from.** The package
  records a fingerprint of every kernel's source (tokens, without comments
  or docstrings) per module, and a module's kernels are taken from it only if
  every one of them still matches this checkout's -- all or none, since a
  numba kernel cannot call a compiled one. A kernel changed here and not yet
  ported falls back to numba, logged once; `kernels/tools/fingerprints.py`
  re-records them after the port is updated, and tests/test_kernels_package.py
  fails until it is. Where the source is not shipped (the website's bytecode),
  the website's build checked the fingerprints instead.
- **`nogil=True, cache=True`, never `parallel=True`.** A kernel runs on a
  request or job thread beside Waitress's pool and the BLAS pool that
  `plexora._resources` caps; numba's own threading layer would be a third pool
  nobody sized. `nogil` lets two threads each run one kernel instead (the
  compiled ports release the GIL too).
- **Compiled before any request can need it** (`prime`). Compiling is an LLVM
  run that imports and dlopens on first call -- exactly the first-use work
  `data_model.prime_hot_code` exists to take off the request path, for the
  same deadlock it describes. Every kernel module registers a `prime()` that
  calls each kernel once on tiny arrays; `prime_hot_code` and the MCP server
  call `prime()` at start.
- **An identity fallback.** Without either (or with `PLEXORA_KERNELS=0` and
  `PLEXORA_NO_NUMBA=1`) `njit` returns the function unchanged, so every kernel
  still runs -- slowly, as the Python it is written in -- and the tests run
  every way to prove they agree. A compiled port handed arguments it was not
  built for (a dtype no caller uses today) runs the numba or Python form for
  that call rather than failing.

The compiled-code cache goes under the user's cache directory, not beside the
source: a bundled install's site-packages may be read-only, and a data root is
the user's work, not ours.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import io
import logging
import os
import sys
import textwrap
import threading
import tokenize

_log = logging.getLogger(__name__)

#: The plexora-kernels interface this Plexora speaks: the registry
#: (`KERNELS`, `FINGERPRINTS`, `set_fallback`). A package with another is not used.
KERNELS_API = 1

#: Kernel modules that have a `prime()` to call, by import path.
_PRIMERS: list[str] = []
_PRIMED: set[str] = set()
_LOCK = threading.Lock()

_NUMBA = None
_CHECKED = False
_KERNELS = None
_KERNELS_CHECKED = False
#: {module: bool} -- whether a module's kernels are taken from plexora-kernels.
_PORTED: dict[str, bool] = {}


def _cache_dir() -> str:
    try:
        from platformdirs import user_cache_dir

        return os.path.join(user_cache_dir("plexora", appauthor=False), "numba")
    except Exception:  # pragma: no cover - platformdirs is a core dependency
        return os.path.join(os.path.expanduser("~"), ".cache", "plexora", "numba")


def _off(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("0", "off", "false", "no")


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


def kernels_module():
    """plexora_kernels, or None when it is missing, switched off
    (`PLEXORA_KERNELS=0`) or speaks another interface."""
    global _KERNELS, _KERNELS_CHECKED
    if _KERNELS_CHECKED:
        return _KERNELS
    _KERNELS_CHECKED = True
    if _off("PLEXORA_KERNELS"):
        return None
    try:
        import plexora_kernels
    except Exception:
        return None
    api = getattr(plexora_kernels, "API", None)
    if api != KERNELS_API:
        _log.warning("plexora-kernels %s speaks interface %s, not %s: not used",
                     getattr(plexora_kernels, "__version__", "?"), api, KERNELS_API)
        return None
    _KERNELS = plexora_kernels
    return plexora_kernels


def enabled() -> bool:
    """Whether kernels are compiled in this process (by either route)."""
    return kernels_module() is not None or numba_module() is not None


def implementation(fn) -> str:
    """How a kernel runs here: `compiled` (plexora-kernels), `numba` or `python`."""
    package = kernels_module()
    if package is not None and (fn in package.KERNELS.values()
                                or fn is getattr(package, "lzw_decode", None)):
        return "compiled"
    if getattr(fn, "py_func", None) is not None:
        return "numba"
    return "python"


def is_compiled(fn) -> bool:
    """Whether calling `fn` runs compiled code (either route)."""
    return implementation(fn) != "python"


# -- fingerprints ----------------------------------------------------------------------


def _is_njit(decorator) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return (isinstance(target, ast.Name) and target.id == "njit") or (
        isinstance(target, ast.Attribute) and target.attr == "njit")


def _token_hash(text: str) -> str:
    """A function's tokens, without comments, blank lines, indentation width or
    its docstring: what a port has to follow, not how it is laid out."""
    kept = []
    state = 0  # 0: header, 1: after the header's NEWLINE, 2: in the body
    pending = None
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        kind = tok.type
        if kind in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING, tokenize.ENDMARKER):
            continue
        if state == 0 and kind == tokenize.NEWLINE:
            state = 1
        elif state == 1 and kind == tokenize.INDENT:
            state = 2
            kept.append("INDENT")
            pending = "doc?"
            continue
        if pending == "doc?":
            pending = "doc" if kind == tokenize.STRING else None
            if pending == "doc":
                continue
        elif pending == "doc":
            pending = None
            if kind == tokenize.NEWLINE:
                continue
        if kind in (tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE):
            kept.append(tokenize.tok_name[kind])
        else:
            kept.append(tok.string)
    return hashlib.sha256("\x1f".join(kept).encode("utf-8")).hexdigest()[:20]


def kernel_fingerprints(source: str) -> dict:
    """{name: fingerprint} of every `@njit` function in a module's source."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    out = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and any(_is_njit(d) for d in node.decorator_list):
            text = textwrap.dedent("".join(lines[node.lineno - 1:node.end_lineno]))
            out[node.name] = _token_hash(text if text.endswith("\n") else text + "\n")
    return out


def module_fingerprints(module_name: str):
    """`kernel_fingerprints` of an imported (or importing) module, or None
    where its source is not shipped."""
    module = sys.modules.get(module_name)
    try:
        source = inspect.getsource(module) if module is not None else None
    except (OSError, TypeError):
        source = None
    if not source:
        return None
    return kernel_fingerprints(source)


def stale_kernels(package=None) -> dict:
    """{module: [kernel names whose source differs from the port]} over every
    module the package ports -- empty when every port is current. For the
    tests and the website's build, which import each module to read it."""
    package = package or kernels_module()
    if package is None:
        return {}
    out = {}
    for module_name, recorded in package.FINGERPRINTS.items():
        importlib.import_module(module_name)
        found = module_fingerprints(module_name)
        if found is None:
            continue
        names = sorted(set(found) | set(recorded))
        differ = [n for n in names if found.get(n) != recorded.get(n)]
        if differ:
            out[module_name] = differ
    return out


def _ported(module_name: str, package) -> bool:
    """Whether `module_name`'s kernels come from the package: it ports them
    all, and every one is the code it was ported from."""
    with _LOCK:
        if module_name in _PORTED:
            return _PORTED[module_name]
    recorded = package.FINGERPRINTS.get(module_name)
    ok = recorded is not None
    if ok:
        found = module_fingerprints(module_name)
        if found is not None and found != recorded:
            differ = sorted(n for n in set(found) | set(recorded) if found.get(n) != recorded.get(n))
            _log.warning("plexora-kernels %s ports an older %s (%s differ): using numba or "
                         "Python for its kernels", getattr(package, "__version__", "?"),
                         module_name, ", ".join(differ))
            ok = False
    with _LOCK:
        _PORTED[module_name] = ok
    return ok


class _Fallback:
    """What a compiled port runs for a call it was not built for: the kernel
    through numba where there is numba, else as Python."""

    def __init__(self, function, options):
        self._function = function
        self._options = options
        self._chosen = None

    def __call__(self, *args):
        if self._chosen is None:
            numba = numba_module()
            self._chosen = (numba.njit(**self._options)(self._function) if numba is not None
                            else self._function)
        try:
            return self._chosen(*args)
        except Exception as exc:
            if self._chosen is self._function or "Typing" not in type(exc).__name__:
                raise
            # Its callees are compiled ports, which numba cannot call.
            self._chosen = self._function
            return self._function(*args)


def _compiled(function, options):
    package = kernels_module()
    if package is None:
        return None
    key = f"{function.__module__}.{function.__qualname__}"
    port = package.KERNELS.get(key)
    if port is None or not _ported(function.__module__, package):
        return None
    package.set_fallback(key, _Fallback(function, options))
    try:
        port.py_func = function
    except (AttributeError, TypeError):  # pragma: no cover - cyfunctions take attributes
        pass
    return port


def njit(fn=None, **options):
    """The compiled port of a kernel, else `numba.njit` with Plexora's
    defaults, else the function itself.

    Usable bare (`@njit`) or with options (`@njit(fastmath=False)`).
    """
    options.setdefault("cache", True)
    options.setdefault("nogil", True)
    if options.pop("parallel", False):
        raise ValueError("Plexora kernels never use parallel=True; see plexora/server/utils/jit.py")

    def wrap(function):
        port = _compiled(function, options)
        if port is not None:
            return port
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
              "plexora.server.utils.cell_features",
              "plexora.plugins.gating.server.autogate.kernels",
              "plexora.plugins.qc.server.segqc.kernels",
              "plexora.server.volume.measure",
              "plexora.server.surface.mesh",
              "plexora.server.surface.nets"):
    register_primer(_name)


def prime(log=None) -> list:
    """Compile every registered kernel now. Returns the modules primed.

    Never raises: a kernel module that fails to import or compile is reported
    and skipped, and its kernels are then compiled on first use instead. A
    module whose kernels are all compiled ports has nothing to compile; its
    `prime()` runs anyway (a few microseconds) and proves the port loads.
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
