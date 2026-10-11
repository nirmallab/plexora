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

PROVISIONAL FIRST. The scan above reads gigabytes for a large stack -- seconds
to minutes before the first tile could be quantized. `provisional_window` is
what the viewer starts with instead: the larger of the default plane's maximum
at the finest level whose plane fits PROVISIONAL_BUDGET and the maximum over
the coarsest level's planes (all of them, or evenly spaced ones up to the
budget). It can be lower than the exact window -- a pyramid's coarse levels
are averages -- so the brightest few pixels may clip in the 8-bit picture
until the exact scan, run in the background, replaces it. It is never
persisted, and every byte served with it says which window it was made with.
"""

from __future__ import annotations

import math

import numpy as np

from plexora.server.volume.model import block_depth, reads_blocks

#: Level-0 bytes of one channel's whole stack the scan reads exactly.
EXACT_BUDGET = 4 * 2**30

#: Rows per read in the scan, in bytes.
_SLAB_BYTES = 128 * 2**20


def _scan_blocks(source, level: int, channel: int, planes, gate, height, width,
                 itemsize) -> float:
    """`_scan_level` for a store chunked across z: runs of planes read a block
    of whole chunks at a time, so each chunk is decoded once -- plane by plane,
    an Imaris file's 64-plane chunks were decoded 64 times over, 14 GB of
    decompression for one channel of a 220 MB stack."""
    chunk = block_depth(source, level)
    plane_bytes = max(1, height * width * itemsize)
    if plane_bytes * chunk <= _SLAB_BYTES:
        depth = max(chunk, (_SLAB_BYTES // plane_bytes) // chunk * chunk)
        rows = height
    else:
        depth = chunk
        rows = max(1, _SLAB_BYTES // max(1, chunk * width * itemsize))
    runs: list[list[int]] = []  # [t, first, last]
    for t, z in sorted(planes):
        if runs and runs[-1][0] == t and runs[-1][2] == z - 1:
            runs[-1][2] = z
        else:
            runs.append([t, z, z])
    ceiling = 0.0
    for t, first, last in runs:
        for z0 in range(first, last + 1, depth):
            z1 = min(last, z0 + depth - 1)
            for start in range(0, height, rows):
                window = (slice(start, min(height, start + rows)), slice(0, width))
                if gate is not None:
                    with gate:
                        block = np.asarray(source.read_block(level, t, channel, z0, z1, *window))
                else:
                    block = np.asarray(source.read_block(level, t, channel, z0, z1, *window))
                if block.size:
                    ceiling = max(ceiling, float(block.max()))
    return ceiling


def _scan_level(source, level: int, channel: int, planes, gate=None) -> float:
    height, width = source.level_shape(level)
    itemsize = max(1, np.dtype(source.volume.dtype).itemsize)
    if reads_blocks(source, level):
        return _scan_blocks(source, level, channel, planes, gate, height, width, itemsize)
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


#: What a provisional window may read: one fine plane and a coarse sample.
PROVISIONAL_BUDGET = 64 * 2**20
#: At most this many planes of the coarsest level, evenly spaced.
PROVISIONAL_PLANES = 32


def provisional_window(source, channel: int) -> tuple[tuple[float, float], dict]:
    """`((0, ceiling), how)` without a whole-stack scan -- see the module notes."""
    volume = source.volume
    itemsize = max(1, np.dtype(volume.dtype).itemsize)
    default = volume.default_view

    def plane_bytes(level):
        height, width = source.level_shape(level)
        return height * width * itemsize

    fine = next((index for index in range(len(volume.levels))
                 if plane_bytes(index) <= PROVISIONAL_BUDGET), len(volume.levels) - 1)
    ceiling = _scan_level(source, fine, channel, [(default.t, default.z)])
    coarse = len(volume.levels) - 1
    planes = [(t, z) for t in range(volume.frames) for z in range(volume.depth)]
    fits = max(1, PROVISIONAL_BUDGET // max(1, plane_bytes(coarse)))
    count = min(len(planes), fits, PROVISIONAL_PLANES)
    if count < len(planes):
        step = len(planes) / count
        planes = sorted({planes[min(len(planes) - 1, int(i * step))] for i in range(count)})
    ceiling = max(ceiling, _scan_level(source, coarse, channel, planes))
    return (0.0, max(ceiling, 1.0)), {"exact": False, "provisional": True,
                                      "level": fine, "coarse": coarse}


def parse_stamp(text) -> tuple[float, float] | None:
    """`w=qmin,qmax` from a request: the window bytes are to be made with."""
    try:
        low, high = (float(part) for part in str(text).split(","))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(low) and math.isfinite(high)) or high <= low:
        return None
    return (low, high)


def std_window(volume_window_: tuple[float, float]) -> tuple[float, float]:
    return (0.0, float(max(1.0, math.ceil(float(volume_window_[1]) / 2.0))))
