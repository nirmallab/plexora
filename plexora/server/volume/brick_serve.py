"""Bricks for the 3-D view's atlas: a stack served in small blocks, each level.

The 3-D view draws one whole level as its base -- the finest that fits the GPU
budget -- and, where the camera is close enough for that level's voxels to be
larger than a screen pixel, finer blocks of the same stack from an atlas
(client `views/volumeAtlas.js`). This module is what those blocks are:

- **The grid.** Every level that keeps every plane is cut into bricks of
  `BRICK` voxels (z, y, x). A level's planes are max-pooled `pool` at a time
  so its voxels stay about as deep as they are wide (`pool_for`): the
  coarse levels of a 2000-plane light-sheet stack would otherwise be bricks
  of 64 x 64 pixels that each hold a sliver of depth, and fine bricks of
  depth no screen can show. The pools double from level to level, or stay,
  so a brick's children are always 2 x 2 (or 2 x 2 x 2) bricks one level
  finer.
- **The apron.** Each brick is sent with one voxel of its neighbours on
  every side (`APRON`), the stack's edge repeated where it has none, so the
  GPU's trilinear filter blends across brick boundaries as it does inside a
  whole texture and the atlas shows no seams.
- **The statistics.** Each brick says its largest voxel per channel. A brick
  whose voxels are all at or below the window's floor is empty: the client
  stores nothing for it and its rays skip it.

Voxels are quantized with the window the tiles and the base level use
(`serve.quantize`), so a brick's byte is the base texture's byte for the same
place at that level. One request carries up to `MAX_PER_REQUEST` bricks of
one level for up to `MAX_CHANNELS` channels (`payload`): the browser keeps six
connections to a server, and a brick is too small to be worth one of them.
"""

from __future__ import annotations

import json
import struct
import threading
from collections import OrderedDict

import numpy as np

#: Voxels per brick before its apron, (z, y, x).
BRICK = (32, 64, 64)
APRON = 1
MAX_PER_REQUEST = 48
MAX_CHANNELS = 6
#: Encoded bricks kept in memory, by what their bytes are.
CACHE_BYTES = 256 * 2**20

_CACHE: OrderedDict = OrderedDict()
_CACHE_SIZE = [0]
_LOCK = threading.Lock()


class BadBricks(ValueError):
    pass


def pool_for(volume, level: int) -> int:
    """Planes max-pooled into one voxel at `level`: `model.pool_for`, the one
    rule the level route and the stored pooled levels share. A stack with no
    stated z step is drawn one pixel per plane, as the renderer draws it."""
    from plexora.server.volume.model import pool_for as rule

    return rule(volume, level, assume_square=True)


def levels(volume) -> list[dict]:
    """The brick grid of every level that keeps every plane, finest first."""
    out = []
    bz, by, bx = BRICK
    for info in volume.levels:
        if info.depth is not None and info.depth != volume.depth:
            continue
        pool = pool_for(volume, info.index)
        depth = -(-volume.depth // pool)
        out.append({
            "level": info.index, "pool": pool,
            "shape": [depth, info.height, info.width],
            "grid": [-(-depth // bz), -(-info.height // by), -(-info.width // bx)],
            # One brick's extent in full-resolution pixels and planes.
            "brick_full": [bz * pool, by * 2 ** info.index, bx * 2 ** info.index],
        })
    return out


def manifest(volume) -> dict:
    return {"brick": list(BRICK), "apron": APRON, "levels": levels(volume),
            "max_per_request": MAX_PER_REQUEST, "max_channels": MAX_CHANNELS,
            "depth": volume.depth, "width": volume.width, "height": volume.height}


def level_of(volume, level: int) -> dict:
    for entry in levels(volume):
        if entry["level"] == level:
            return entry
    raise BadBricks(f"level {level} is not served as bricks (it pools planes, or does not "
                    "exist)")


def parse_at(text, grid) -> list[tuple[int, int, int]]:
    """`z.y.x,z.y.x,...` -> brick coordinates inside `grid`."""
    out = []
    for part in str(text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            bz, by, bx = (int(v) for v in part.split("."))
        except ValueError:
            raise BadBricks(f"{part!r} is not a brick (z.y.x)") from None
        if not (0 <= bz < grid[0] and 0 <= by < grid[1] and 0 <= bx < grid[2]):
            raise BadBricks(f"brick {part} is outside the level's grid {grid}")
        out.append((bz, by, bx))
    if not out:
        raise BadBricks("no brick asked for (at=z.y.x,...)")
    if len(out) > MAX_PER_REQUEST:
        raise BadBricks(f"at most {MAX_PER_REQUEST} bricks a request")
    return list(dict.fromkeys(out))


def _window(entry, at):
    """A brick's voxels with its apron, as wanted -- `(z0, z1, y0, y1, x0, x1)`
    in pooled planes and level pixels, half-open, possibly past the stack --
    and as they exist (clamped to it)."""
    bz, by, bx = BRICK
    depth_pooled, height, width = entry["shape"]
    z, y, x = at
    wanted = (z * bz - APRON, (z + 1) * bz + APRON, y * by - APRON, (y + 1) * by + APRON,
              x * bx - APRON, (x + 1) * bx + APRON)
    held = (max(0, wanted[0]), min(depth_pooled, wanted[1]), max(0, wanted[2]),
            min(height, wanted[3]), max(0, wanted[4]), min(width, wanted[5]))
    return wanted, held


def _read(source, entry, t, channel, z0, z1, y0, y1, x0, x1) -> np.ndarray:
    """Pooled planes z0..z1 (half-open) of a level's rows and columns, raw:
    `model.read_pooled`, so a level the stack's 3-D copy holds pooled
    (volume/bricks.py) is read from that, and the last pool may be short."""
    from plexora.server.volume.model import read_pooled

    pool = entry["pool"]
    planes = source.volume.depth
    return np.asarray(read_pooled(source, entry["level"], t, channel, z0 * pool,
                                  min(planes, z1 * pool) - 1, pool, slice(y0, y1),
                                  slice(x0, x1)))


def _cut(region, origin, entry, at) -> np.ndarray:
    """One brick out of `region` (read from `origin` = (z0, y0, x0)), its
    apron the stack's edge repeated where the stack ends."""
    wanted, held = _window(entry, at)
    block = region[held[0] - origin[0]:held[1] - origin[0],
                   held[2] - origin[1]:held[3] - origin[1],
                   held[4] - origin[2]:held[5] - origin[2]]
    pad = ((held[0] - wanted[0], wanted[1] - held[1]), (held[2] - wanted[2], wanted[3] - held[3]),
           (held[4] - wanted[4], wanted[5] - held[5]))
    if any(a or b for a, b in pad):
        block = np.pad(block, pad, mode="edge")
    return block


def read_brick(source, entry, t, channel, at) -> np.ndarray:
    """One brick with its apron, raw voxels, `(BZ+2, BY+2, BX+2)`: max-pooled
    planes, the stack's edge repeated past its ends."""
    _wanted, held = _window(entry, at)
    region = _read(source, entry, t, channel, *held)
    return _cut(region, (held[0], held[2], held[4]), entry, at)


def read_bricks(source, entry, t, channel, ats) -> dict:
    """Several bricks of one level, each source tile decoded once: grouped by
    the tile their origin is in (`LevelInfo.chunks`, else 512 pixels), each
    group's bounding window read in one go. A tiled image decodes a whole
    tile to give up any pixel of it, and a request's bricks -- the nearest
    ones to the camera -- mostly share a few tiles."""
    info = source.volume.levels[entry["level"]]
    tile = tuple(info.chunks) if getattr(info, "chunks", None) else (512, 512)
    groups: dict = {}
    for at in ats:
        _wanted, held = _window(entry, at)
        groups.setdefault((held[2] // max(1, tile[0]), held[4] // max(1, tile[1])),
                          []).append(at)
    out = {}
    for members in groups.values():
        helds = [_window(entry, at)[1] for at in members]
        box = (min(h[0] for h in helds), max(h[1] for h in helds),
               min(h[2] for h in helds), max(h[3] for h in helds),
               min(h[4] for h in helds), max(h[5] for h in helds))
        region = _read(source, entry, t, channel, *box)
        for at in members:
            out[at] = _cut(region, (box[0], box[2], box[4]), entry, at)
    return out


def _core(block):
    return block[APRON:-APRON, APRON:-APRON, APRON:-APRON]


def _cached(key):
    with _LOCK:
        held = _CACHE.get(key)
        if held is not None:
            _CACHE.move_to_end(key)
        return held


def _remember(key, found):
    with _LOCK:
        if key in _CACHE:
            return
        _CACHE[key] = found
        _CACHE_SIZE[0] += len(found[0])
        while _CACHE_SIZE[0] > CACHE_BYTES and _CACHE:
            _, (data, _peak) = _CACHE.popitem(last=False)
            _CACHE_SIZE[0] -= len(data)


def encode(raw, window, voxel_format):
    """`(bytes, max over the brick without its apron)` of one raw brick."""
    from plexora.server.volume import serve

    voxels = np.ascontiguousarray(serve.quantize(raw, window[0], window[1], voxel_format))
    return voxels.tobytes(), float(_core(voxels).max() if voxels.size else 0)


def payload(source, entry, t, channels, at_list, voxel_format, identity) -> bytes:
    """The response body: a little-endian uint32 header length, the header
    (JSON), then for each brick not empty in every channel, each channel's
    voxels in the order asked.

    `channels` is `[(index, name, window)]`. Header: `{brick: [z, y, x] with
    apron, format, level, pool, channels: [{name, window}], bricks: [{at,
    max: [per channel], offset}]}` -- `offset` null for a brick empty in
    every channel (nothing sent for it)."""
    bz, by, bx = BRICK
    shape = [bz + 2 * APRON, by + 2 * APRON, bx + 2 * APRON]
    encoded = {}
    for index, _name, window in channels:
        keys = {at: (identity, entry["level"], entry["pool"], t, index, tuple(window),
                     voxel_format, at) for at in at_list}
        missing = [at for at in at_list if _cached(keys[at]) is None]
        raw = read_bricks(source, entry, t, index, missing) if missing else {}
        for at in at_list:
            found = _cached(keys[at])
            if found is None:
                found = encode(raw[at], window, voxel_format)
                _remember(keys[at], found)
            encoded[(index, at)] = found
    chunks, bricks, offset = [], [], 0
    for at in at_list:
        parts, peaks = [], []
        for index, _name, _window_ in channels:
            data, peak = encoded[(index, at)]
            parts.append(data)
            peaks.append(peak)
        if any(peak > 0 for peak in peaks):
            bricks.append({"at": list(at), "max": peaks, "offset": offset})
            for data in parts:
                chunks.append(data)
                offset += len(data)
        else:
            bricks.append({"at": list(at), "max": peaks, "offset": None})
    header = json.dumps({
        "brick": shape, "apron": APRON, "format": voxel_format, "level": entry["level"],
        "pool": entry["pool"], "bytes_per_brick": int(np.prod(shape))
        * (2 if voxel_format == "f16" else 1),
        "channels": [{"name": name, "window": list(window)} for _i, name, window in channels],
        "bricks": bricks,
    }).encode("utf-8")
    return struct.pack("<I", len(header)) + header + b"".join(chunks)


def clear() -> None:
    with _LOCK:
        _CACHE.clear()
        _CACHE_SIZE[0] = 0
