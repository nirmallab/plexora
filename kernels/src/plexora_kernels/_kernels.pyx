# distutils: language = c
"""Plexora's kernels, compiled: one extension, every port.

Each public function here has the name, the arguments and the results of the
`@njit` kernel it ports (its key in `KERNELS` is that kernel's module and
name), so plexora/server/utils/jit.py hands it out in the kernel's place.
Each checks what it was handed first -- array dtypes, dimensions, that an
output is writable, scalar kinds -- and a call it was not built for runs the
fallback Plexora set (`set_fallback`: the kernel through numba, or as Python),
counted in `FALLBACK_CALLS`. The loops run without the GIL, as numba's
`nogil=True` kernels do.

Each port follows its kernel's arithmetic step by step -- the same operations
in the same order, in the same precision (a float32 sum stays float32 where
numba's does; `math.expm1` on float32 is `expm1f`, as numba calls it; float
`//` is CPython's algorithm, as numba's is) -- so that what it returns equals
numba's bit for bit wherever the C library agrees. The files it includes are
grouped by product feature: planes.pxi (2-D label planes), points.pxi
(neighbour counts), surfaces.pxi (surface nets and meshes), lzw.pxi (TIFF LZW).
"""

import numpy as np

from libc.math cimport INFINITY, NAN, expm1, expm1f, floor, fmod, isfinite, isnan, sqrt
from libc.stdint cimport (int8_t, int16_t, int32_t, int64_t, uint8_t, uint16_t, uint32_t,
                          uint64_t)
from libc.stdlib cimport free, malloc, realloc
from libc.string cimport memcpy

ctypedef fused label_t:
    uint8_t
    uint16_t
    uint32_t
    uint64_t
    int8_t
    int16_t
    int32_t
    int64_t

ctypedef fused real_t:
    float
    double

_INTS = frozenset(np.dtype(t) for t in ("u1", "u2", "u4", "u8", "i1", "i2", "i4", "i8"))
_REALS = frozenset(np.dtype(t) for t in ("f4", "f8"))
_F8 = frozenset([np.dtype("f8")])
_F4 = frozenset([np.dtype("f4")])
_I8 = frozenset([np.dtype("i8")])
_I1 = frozenset([np.dtype("i1")])
_U4 = frozenset([np.dtype("u4")])
_BOOL = frozenset([np.dtype("?")])

#: {kernel key: what runs for a call no port here was built for}.
_FALLBACKS = {}
#: {kernel key: calls that went to the fallback} -- zero for every call the
#: product makes; the tests hold it to that.
FALLBACK_CALLS = {}


def set_fallback(key, function):
    """What kernel `key` runs for arguments its port was not built for."""
    _FALLBACKS[key] = function


def _fall(key, args):
    FALLBACK_CALLS[key] = FALLBACK_CALLS.get(key, 0) + 1
    function = _FALLBACKS.get(key)
    if function is None:
        kinds = ", ".join(f"{a.dtype}{list(a.shape)}" if isinstance(a, np.ndarray)
                          else type(a).__name__ for a in args)
        raise TypeError(f"plexora_kernels: {key} has no compiled form for ({kinds}) "
                        "and no fallback is set")
    return function(*args)


cdef inline bint _arr(object a, object dtypes, int ndim, bint write=False):
    if not isinstance(a, np.ndarray):
        return False
    if a.ndim != ndim or a.dtype not in dtypes:
        return False
    if write and not a.flags.writeable:
        return False
    return True


cdef inline bint _int(object v):
    return isinstance(v, (int, np.integer))


cdef inline bint _real(object v):
    return isinstance(v, (int, float, np.integer, np.floating))


cdef inline double _floordiv(double vx, double wx) noexcept nogil:
    """CPython's float `//` (Objects/floatobject.c float_divmod), as numba's."""
    cdef double mod = fmod(vx, wx)
    cdef double div = (vx - mod) / wx
    cdef double result
    if mod != 0.0 or mod != mod:
        if (wx < 0) != (mod < 0):
            div -= 1.0
    if div != 0.0 or div != div:
        result = floor(div)
        if div - result > 0.5:
            result += 1.0
    else:
        div = div * div
        result = div * vx / wx
    return result


include "planes.pxi"
include "points.pxi"
include "surfaces.pxi"
include "lzw.pxi"


#: Every port, by the key of the kernel it ports (module.name).
KERNELS = {
    "plexora.server.utils.label_kernels._boundary": _boundary,
    "plexora.server.utils.label_kernels._bboxes": _bboxes,
    "plexora.server.volume.measure._bbox_kernel": _bbox_kernel,
    "plexora.server.utils.cell_features._background_blocks": _background_blocks,
    "plexora.server.utils.cell_features._tile_kernel": _tile_kernel,
    "plexora.server.utils.cell_features._run_labels": _run_labels,
    "plexora.plugins.qc.server.segqc.kernels.label_stats": label_stats,
    "plexora.plugins.qc.server.segqc.kernels.count_pairs": count_pairs,
    "plexora.plugins.qc.server.segqc.kernels.fill_pairs": fill_pairs,
    "plexora.plugins.gating.server.autogate.kernels._count": _neighbour_count,
    "plexora.server.surface.nets._pool_runs": _pool_runs,
    "plexora.server.surface.nets._corners": _corners,
    "plexora.server.surface.nets._distinct": _distinct,
    "plexora.server.surface.nets._count": _nets_count,
    "plexora.server.surface.nets._fill": _fill,
    "plexora.server.surface.nets._triangulate": _triangulate,
    "plexora.server.surface.mesh._face_normals_into": _face_normals_into,
    "plexora.server.surface.mesh._adjacency": _adjacency,
    "plexora.server.surface.mesh._umbrella_step": _umbrella_step,
    "plexora.server.surface.mesh._signed_volumes": _signed_volumes,
    "plexora.server.surface.mesh._object_sums": _object_sums,
    "plexora.server.surface.mesh._object_moments": _object_moments,
}
