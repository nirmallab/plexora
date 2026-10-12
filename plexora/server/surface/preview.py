"""The quick extraction behind the threshold slider.

Dragging the slider must answer in a frame or two, so the preview works on a
small grid (`PREVIEW_VOXELS`, a coarse level of the area asked for) read and
preprocessed ONCE and kept here; each new isovalue is then only marching
cubes over a block already in memory -- tens of milliseconds for two million
voxels. The block is raw intensity (the same values the final extraction
reads, at a coarser level), so the preview's topology at its level is the
final surface's at that level; the final build reads its own, finer, grid.

Each answer also carries the isovalue's contours through the plane on screen
and through the two section planes, so the 2-D views show where the surface
will cut them while the slider moves.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict

import numpy as np

from plexora.server.surface import isosurface, levels, marching
from plexora.server.surface import mesh as mesh_mod
from plexora.server.surface import suggest as suggest_mod


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


PREVIEW_VOXELS = max(64_000, _env_int("PLEXORA_SURFACE_PREVIEW_VOXELS", 2_000_000))
CACHE_BYTES = 512 * 2**20
#: Contour points per plane at most (the header stays small).
CONTOUR_POINTS = 40_000

_blocks: OrderedDict = OrderedDict()
_bytes = 0
_lock = threading.Lock()


def clear() -> None:
    global _bytes
    with _lock:
        _blocks.clear()
        _bytes = 0


def preview_plan(volume, region, budget=None) -> levels.Plan:
    """The finest level whose grid of `region` fits the preview budget."""
    budget = int(budget or PREVIEW_VOXELS)
    plan = None
    for info in volume.levels:
        plan = levels.plan_at(volume, info.index, region)
        if plan.voxels <= budget:
            return plan
    return plan


def block_for(source, identity, *, channel, t, region, pre=None, budget=None):
    """`(block, plan)`: the preview grid of `region`, preprocessed, cached."""
    global _bytes
    volume = source.volume
    plan = preview_plan(volume, region, budget)
    pre = pre or isosurface.Preprocess()
    key = (identity, int(channel), int(t), plan.level, plan.rows.start, plan.rows.stop,
           plan.cols.start, plan.cols.stop, plan.z0, plan.z1, plan.stride,
           pre.sigma_um, pre.background_um)
    with _lock:
        held = _blocks.get(key)
        if held is not None:
            _blocks.move_to_end(key)
            return held, plan
    read = levels.plan_reader(source, plan, t=t, channel=channel)
    block = read(0, plan.grid.shape[0] - 1)
    if pre.active:
        block = pre.apply(block, plan.voxel_um(_scale(volume)[0]))
    block = np.ascontiguousarray(block, dtype=np.float32)
    with _lock:
        if key not in _blocks:
            _blocks[key] = block
            _bytes += block.nbytes
            while _bytes > CACHE_BYTES and len(_blocks) > 1:
                _, dropped = _blocks.popitem(last=False)
                _bytes -= dropped.nbytes
    return block, plan


def _scale(volume):
    """`(scale, physical)`: one level-0 voxel's size in µm when x, y and z are
    all known, else ones (voxel units)."""
    spacing = volume.spacing
    if spacing.volumetric_ok():
        return (spacing.x.microns(), spacing.y.microns(), spacing.z.microns()), True
    return (1.0, 1.0, 1.0), False


def _lines(contours, to_xy):
    out, points = [], 0
    for line in contours:
        if len(line) < 2:
            continue
        xy = to_xy(line)
        out.append(np.round(xy, 2).ravel().tolist())
        points += len(line)
        if points > CONTOUR_POINTS:
            break
    return out


def contours(block, plan: levels.Plan, iso, *, z=None, x=None, y=None) -> dict:
    """The isovalue's contours through plane `z` (XY), row `y` (XZ) and column
    `x` (YZ), all level-0 coordinates; each line flat `[a0, b0, a1, b1, ...]`."""
    from skimage import measure

    grid = plan.grid
    sx, sy, sz = grid.scale
    ox, oy, oz = grid.offset
    depth, height, width = block.shape
    out = {}

    def index(value, scale, offset, size):
        return int(min(size - 1, max(0, round((float(value) - offset) / scale))))

    if z is not None:
        g = index(z, sz, oz, depth)
        found = measure.find_contours(block[g], iso) if block[g].max() > iso > block[g].min() else []
        out["xy"] = {"z": int(z), "lines": _lines(
            found, lambda p: np.stack([p[:, 1] * sx + ox, p[:, 0] * sy + oy], axis=1))}
    if y is not None and depth > 1:
        r = index(y, sy, oy, height)
        part = block[:, r, :]
        found = measure.find_contours(part, iso) if part.max() > iso > part.min() else []
        out["xz"] = {"y": int(y), "lines": _lines(
            found, lambda p: np.stack([p[:, 1] * sx + ox, p[:, 0] * sz + oz], axis=1))}
    if x is not None and depth > 1:
        c = index(x, sx, ox, width)
        part = block[:, :, c]
        found = measure.find_contours(part, iso) if part.max() > iso > part.min() else []
        out["yz"] = {"x": int(x), "lines": _lines(
            found, lambda p: np.stack([p[:, 1] * sy + oy, p[:, 0] * sz + oz], axis=1))}
    return out


def preview(source, identity, *, channel, t, iso, region, pre=None, min_size=0.0,
            z=None, x=None, y=None, budget=None) -> bytes:
    """The preview mesh at `iso` as a mesh payload whose header carries the
    level, the grid, the counts and the contours."""
    block, plan = block_for(source, identity, channel=channel, t=t, region=region, pre=pre,
                            budget=budget)
    volume = source.volume
    scale, physical = _scale(volume)
    header = {"preview": True, "iso": float(iso), "level": plan.level, "plan": plan.to_dict(),
              "units": "um" if physical else "voxel",
              "dims": [volume.width, volume.height, volume.depth]}
    try:
        surface, report = isosurface.extract(
            lambda a, b: block[a:b + 1], plan.grid, iso, scale=scale, min_size=min_size,
            strip=max(2, block.shape[0]), keep_raw=False)
        header["report"] = report
    except marching.NoSurface as nothing:
        surface = mesh_mod.empty()
        header["empty"] = str(nothing)
    header["contours"] = contours(block, plan, float(iso), z=z, x=x, y=y)
    return mesh_mod.to_bytes(surface, header)


def suggest(source, identity, *, channel, t, region, pre=None, budget=None) -> dict:
    block, plan = block_for(source, identity, channel=channel, t=t, region=region, pre=pre,
                            budget=budget)
    out = suggest_mod.suggest(block)
    out["level"] = plan.level
    out["plan"] = plan.to_dict()
    return out
