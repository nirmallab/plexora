"""Plexora's compiled kernels (plexora-kernels).

The loops Plexora writes as plain numpy-typed Python and compiles with numba
(plexora/server/utils/jit.py), ported to Cython and built ahead of time:
wheels for Windows, macOS and Linux, and for Pyodide, where the website's
Python runs and numba does not exist. Plexora takes a kernel from here when
this package is installed, speaks its interface (`API`) and was ported from
the same source (`FINGERPRINTS`); otherwise numba, otherwise the Python.

    KERNELS        {"plexora.module.kernel": compiled function}
    FINGERPRINTS   {"plexora.module": {"kernel": fingerprint of its source}}
    set_fallback   what a kernel runs for arguments its port was not built for
    lzw_decode     TIFF LZW, for the website's imagecodecs stand-in
"""

from plexora_kernels._fingerprints import FINGERPRINTS, PORTED_FROM
from plexora_kernels._kernels import FALLBACK_CALLS, KERNELS, lzw_decode, set_fallback

__version__ = "0.1.0"

#: The interface Plexora's jit.py checks (`KERNELS_API` there).
API = 1

__all__ = ["API", "FALLBACK_CALLS", "FINGERPRINTS", "KERNELS", "PORTED_FROM", "lzw_decode",
           "set_fallback", "__version__"]
