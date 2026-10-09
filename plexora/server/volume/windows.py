"""One quantization window per channel for a whole volume.

The default tile path encodes 8-bit WebP by mapping [0, max] of a channel
linearly onto [0, 255] (`data_model.get_channel_quantization_window` says why
the max must come from full-resolution pixels). A volume's planes share ONE
window per channel: a window that changed with z would make the same raw value
a different byte on every plane, so scrubbing would flicker in brightness and
a cached tile would mean something different from its neighbour.

The ceiling is the maximum over every plane and frame. Reading every pixel of
a large stack is Z times the 2-D scan -- minutes for a 40-plane whole-slide
stack -- so it is exact only while the level-0 stack of one channel fits
`EXACT_BUDGET`. Beyond that it is the larger of the default plane's exact
level-0 maximum and the maximum over every plane at the finest level that fits
the budget. A plane brighter than both only at a few full-resolution pixels
clips those pixels in the 8-bit picture; the HD path and every measurement
read raw values and are unaffected. Which of the two was used is returned and
persisted.

A standard-deviation projection of values in [0, M] is at most M/2, so its
window is (0, M/2) exactly, with no scan at all.
"""

from __future__ import annotations

import math

import numpy as np

#: Level-0 bytes of one channel's whole stack the scan reads exactly.
EXACT_BUDGET = 4 * 2**30

#: Rows per read in the scan, in bytes.
_SLAB_BYTES = 128 * 2**20


def _scan_level(source, level: int, channel: int, planes, gate=None) -> float:
    height, width = source.level_shape(level)
    itemsize = max(1, np.dtype(source.volume.dtype).itemsize)
    rows = max(1, _SLAB_BYTES // max(1, width * itemsize))
    ceiling = 0.0
    for t, z in planes:
        array = source.level_array(t, z, level)
        for start in range(0, height, rows):
            if gate is not None:
                with gate:
                    slab = np.asarray(array[channel, start:start + rows])
            else:
                slab = np.asarray(array[channel, start:start + rows])
            if slab.size:
                ceiling = max(ceiling, float(slab.max()))
    return ceiling


def volume_window(source, channel: int, gate=None) -> tuple[tuple[float, float], dict]:
    """`((0, ceiling), how)` for one channel of `source`."""
    volume = source.volume
    planes = [(t, z) for t in range(volume.frames) for z in range(volume.depth)]
    itemsize = max(1, np.dtype(volume.dtype).itemsize)

    def stack_bytes(level):
        height, width = source.level_shape(level)
        return len(planes) * height * width * itemsize

    if stack_bytes(0) <= EXACT_BUDGET:
        ceiling = _scan_level(source, 0, channel, planes, gate)
        return (0.0, max(ceiling, 1.0)), {"exact": True, "level": 0}
    default = volume.default_view
    exact = _scan_level(source, 0, channel, [(default.t, default.z)], gate)
    level = next((index for index in range(len(volume.levels))
                  if stack_bytes(index) <= EXACT_BUDGET), len(volume.levels) - 1)
    coarse = _scan_level(source, level, channel, planes, gate)
    return (0.0, max(exact, coarse, 1.0)), {"exact": False, "level": level}


def std_window(volume_window_: tuple[float, float]) -> tuple[float, float]:
    return (0.0, float(max(1.0, math.ceil(float(volume_window_[1]) / 2.0))))
