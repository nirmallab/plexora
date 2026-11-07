"""The one extension: src/plexora_kernels/_kernels.pyx (and the .pxi it includes).

Built for speed (-O3) with floating-point contraction off: a compiler that
fuses `a * b + c` into one FMA rounds once where numba's LLVM rounds twice,
and the ports are held to numba's results bit for bit (the agreement tests in
Plexora's tests/test_kernels_package.py). numba does not contract without
fastmath, and WebAssembly has no FMA to contract into.
"""

import os

from Cython.Build import cythonize
from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

DIRECTIVES = {
    "language_level": 3,
    "boundscheck": False,
    "wraparound": False,
    "initializedcheck": False,
    "cdivision": True,
    "binding": True,
    "embedsignature": False,
}


class BuildExt(build_ext):
    def initialize_options(self):
        super().initialize_options()
        # A compiler other than the platform's default, e.g. `mingw32` (with
        # CC=gcc from llvm-mingw on PATH) on a Windows machine without MSVC.
        self.compiler = os.environ.get("PLEXORA_KERNELS_COMPILER") or None

    def build_extensions(self):
        kind = self.compiler.compiler_type
        if kind == "msvc":
            flags = ["/O2", "/fp:precise"]
        else:
            flags = ["-O3", "-ffp-contract=off", "-fno-strict-aliasing"]
            if kind == "mingw32":
                # CPython for Windows is built with MSVC; its headers need
                # telling it is 64-bit when another compiler reads them.
                flags.append("-DMS_WIN64")
        for ext in self.extensions:
            ext.extra_compile_args = flags + list(ext.extra_compile_args or [])
        super().build_extensions()


extensions = [
    Extension(
        "plexora_kernels._kernels",
        ["src/plexora_kernels/_kernels.pyx"],
        define_macros=[("NPY_NO_DEPRECATED_API", "NPY_1_7_API_VERSION")],
    )
]

setup(
    ext_modules=cythonize(extensions, compiler_directives=DIRECTIVES,
                          annotate=bool(os.environ.get("PLEXORA_KERNELS_ANNOTATE"))),
    cmdclass={"build_ext": BuildExt},
)
