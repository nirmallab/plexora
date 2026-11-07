# plexora-kernels

Plexora's compiled kernels: the loops Plexora writes as numpy-typed Python and
compiles with numba (`plexora/server/utils/jit.py`), ported to Cython and built
ahead of time -- wheels for Windows, macOS, Linux and Pyodide (WebAssembly, the
website's Python, where numba does not exist).

Plexora uses a kernel from here when this package is installed, speaks the same
interface (`plexora_kernels.API`) and was ported from the same source
(`plexora_kernels.FINGERPRINTS`); otherwise numba; otherwise the Python itself.
`PLEXORA_KERNELS=0` switches it off.

Building, testing and releasing: `docs/internal/bench/browser_node/L5.md` in the
Plexora repository.
