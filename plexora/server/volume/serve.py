"""Building what the 3-D view uploads: a channel's level, as texture voxels.

The level route (`/volume/<ds>/level`, server/routes/volume_routes.py) used to
build its payload inline: every request read the whole level, made four
float32 copies of it on the way to bytes, and was sent `no-store`, so the
browser fetched the same 50-100 MB again for every channel switched back on,
every return to 3-D and every reload. This module is that payload, built
once and named:

- QUANTIZED BY LOOKUP TABLE for 8- and 16-bit data -- the float formula the
  route always used, evaluated once per possible value instead of once per
  voxel, so the bytes are identical and no float copy of the level is made;
- NAMED by an ETag over everything the bytes depend on (the source file, the
  channel, frame, level, area, z pooling, window and format), so a browser
  that has them asks with If-None-Match and gets a 304 before any voxel is
  read; and cacheable for a year when the request names the window it was
  made for (`w=qmin,qmax`): a new window is a new address;
- GATED (PLEXORA_LEVEL_GATE builds at once) and SINGLE-FLIGHT per payload: the
  3-D view asks for every channel together, and two asks for one payload are
  one read;
- held in a byte-budgeted LRU (PLEXORA_LEVEL_CACHE_MB) for the next asker, and
  the contrast hint cached apart from the format (HD and fast share it).

A data node builds the same payload for a volume it holds (node/api.py
`/image/<rid>/level`) from the same functions here, and the primary forwards
it: which level, area, planes and pooling are chosen once, by the primary,
and sent explicitly, so the node's bytes are the bytes a local read makes.
"""

from __future__ import annotations

import functools
import hashlib
import math
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

#: Bumped when the bytes for the same inputs change meaning.
PAYLOAD_VERSION = 1


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


LEVEL_CACHE_BYTES = max(16, _env_int("PLEXORA_LEVEL_CACHE_MB", 512)) * 2**20
#: A pooled level is read in runs of about this many bytes (whole pools).
READ_RUN_BYTES = 64 * 2**20
LEVEL_GATE = threading.BoundedSemaphore(max(1, _env_int("PLEXORA_LEVEL_GATE", 3)))

_cache: OrderedDict = OrderedDict()
_cache_bytes = 0
_hints: OrderedDict = OrderedDict()
_lock = threading.Lock()
_flights: dict = {}


# -- quantization ---------------------------------------------------------------


def _unit(values, qmin, qmax):
    """The route's own formula: (v - qmin) / span, clipped to 0..1, in float32."""
    span = max(1.0, float(qmax) - float(qmin))
    return np.clip((values.astype(np.float32) - qmin) / span, 0.0, 1.0)


@functools.lru_cache(maxsize=32)
def _table(dtype, qmin, qmax, voxel_format):
    """Every possible value of an 8- or 16-bit `dtype`, quantized once."""
    values = np.arange(np.iinfo(dtype).max + 1, dtype=dtype)
    unit = _unit(values, qmin, qmax)
    if voxel_format == "f16":
        return unit.astype("<f2")
    return np.rint(unit * 255).astype(np.uint8)


def quantize(block, qmin, qmax, voxel_format="u8"):
    """`block` as texture voxels: uint8 bytes, or little-endian float16 for HD.

    Byte-for-byte the float formula, by table for uint8/uint16 data: no
    float32 copy of the whole level (four bytes a voxel, and two more copies
    on the way) is ever made."""
    block = np.asarray(block)
    if block.dtype in (np.uint8, np.uint16):
        table = _table(block.dtype, float(qmin), float(qmax), voxel_format)
        if block.ndim < 1 or block.size < 2**24 or block.shape[0] < 2:
            return np.take(table, block)
        # A whole level is a hundred million voxels: the lookup in slabs on
        # the decode pool (np.take releases the GIL), about 3x faster.
        from plexora.server.volume.chunk_cache import DECODE_POOL

        out = np.empty(block.shape, dtype=table.dtype)
        step = max(1, -(-block.shape[0] // 8))
        list(DECODE_POOL.map(lambda z: np.take(table, block[z:z + step], out=out[z:z + step]),
                             range(0, block.shape[0], step)))
        return out
    unit = _unit(block, qmin, qmax)
    if voxel_format == "f16":
        return unit.astype("<f2")
    return np.rint(unit * 255).astype(np.uint8)


# -- names ------------------------------------------------------------------------


def source_identity(path) -> str:
    """What the bytes were read from: a local file's path, size and mtime, or
    the address of one that is not local."""
    text = str(path or "")
    try:
        stat = os.stat(text)
        return f"{text}|{stat.st_size}|{stat.st_mtime_ns}"
    except OSError:
        return text


def level_etag(identity, index, t, level, window, voxel_format, covered, pool) -> str:
    """An unquoted entity tag for one level payload."""
    raw = "|".join(str(part) for part in (
        PAYLOAD_VERSION, identity, index, t, level, f"{float(window[0])!r}",
        f"{float(window[1])!r}", voxel_format, tuple(int(v) for v in covered), pool))
    return "lv-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def window_stamp(window) -> str:
    """How a request names the window it was made for: `w=qmin,qmax`."""
    return f"{float(window[0]):g},{float(window[1]):g}"


# -- which level, area and planes -------------------------------------------------

#: The most one channel's level may cost to send. A volume renderer holds one
#: 3-D texture per channel on the GPU; 64 MB of uint8 is 64 M voxels, which is
#: past what an integrated GPU draws at an interactive rate anyway.
LEVEL_BUDGET_BYTES = 64 * 2**20



def z_pool(volume, level: int) -> int:
    """How many planes the level route max-pools into one texel at `level`.

    As many as fit in one of the level's pixels, so the texture's voxels are
    about as deep as they are wide -- what Imaris does to its own coarse
    levels. A light-sheet stack of 1937 planes 5 µm apart is 1937 texels deep
    at every level otherwise: a ray marched across it samples one plane in four
    and misses most of what a MIP should show, and the budget goes on depth
    instead of on detail. A maximum keeps every peak. 1 without a stated z step.
    """
    spacing = volume.spacing
    sx, sz = spacing.x.value, spacing.z.value
    if not (sx and sz) or spacing.z.source not in ("metadata", "manual"):
        return 1
    info = volume.levels[level]
    pixel = float(sx) * volume.width / max(1, info.width)
    return max(1, int(pixel // float(sz)))


def choose_level(volume, budget_bytes=LEVEL_BUDGET_BYTES, max_side=2048, planes=None):
    """The finest level whose (Z, H, W) uint8 stack -- Z after `z_pool` -- fits
    the budget and whose every side fits a 3-D texture (WebGL2 guarantees 256;
    real GPUs 2048). `planes` is how many planes are asked for (all)."""
    planes = volume.depth if planes is None else planes
    for info in volume.levels:
        depth = -(-planes // z_pool(volume, info.index))
        voxels = depth * info.height * info.width
        if voxels <= budget_bytes and max(info.height, info.width, depth) <= max_side:
            return info.index
    return volume.levels[-1].index


def area_at(volume, level, box):
    """(rows, cols) at `level` covering a full-resolution `box` (the whole
    level when None), and the full-resolution box they cover."""
    info = volume.levels[level]
    if box is None:
        return (slice(0, info.height), slice(0, info.width),
                (0, 0, volume.width, volume.height))
    fx = volume.width / max(1, info.width)
    fy = volume.height / max(1, info.height)
    x0, y0, x1, y1 = box
    c0, r0 = int(x0 // fx), int(y0 // fy)
    c1 = max(c0 + 1, min(info.width, int(math.ceil(x1 / fx))))
    r1 = max(r0 + 1, min(info.height, int(math.ceil(y1 / fy))))
    covered = (int(round(c0 * fx)), int(round(r0 * fy)),
               min(volume.width, int(round(c1 * fx))), min(volume.height, int(round(r1 * fy))))
    return slice(r0, r1), slice(c0, c1), covered


def choose_area_level(volume, box, budget_bytes, max_side=2048, planes=None):
    """`choose_level` for an area: the finest level at which `box`, through
    every plane asked for (after `z_pool`), fits the budget and a 3-D texture."""
    planes = volume.depth if planes is None else planes
    for info in volume.levels:
        rows, cols, _ = area_at(volume, info.index, box)
        depth = -(-planes // z_pool(volume, info.index))
        height, width = rows.stop - rows.start, cols.stop - cols.start
        if depth * height * width <= budget_bytes and max(height, width, depth) <= max_side:
            return info.index
    return volume.levels[-1].index


#: The most any one level payload may be, whatever a request asks for.
MAX_LEVEL_BYTES = 512 * 2**20


class BadLevel(ValueError):
    """A level request that cannot be answered: the sentence is the 400's."""


@dataclass(frozen=True)
class LevelAsk:
    """One level request, checked against the volume it is of."""

    t: int
    level: int
    voxel_format: str
    #: Full-resolution (x0, y0, x1, y1), or None for the whole plane.
    box: tuple | None
    z0: int
    z1: int
    #: Planes max-pooled into one texel; None = `z_pool` of the volume read.
    pool: int | None
    #: The window the request names (`w=`), or None.
    named: tuple | None

    @property
    def planes(self) -> int:
        return self.z1 - self.z0 + 1


def parse_level(volume, args) -> LevelAsk:
    """The level route's query (`channel` aside) against `volume`.

    `t`, `budget`, `max_side`, `format`, `box=x,y,width,height`, `z=z0,z1`,
    `level` (an integer or `auto`), `w=qmin,qmax`, and `pool` -- which only
    the primary sends, to a data node, so that the node pools a level as the
    primary's own z spacing says (the project may hold one somebody typed).
    BadLevel for anything out of range."""
    from plexora.server.volume.windows import parse_stamp

    try:
        t = int(args.get('t', 0))
        budget = int(args.get('budget', LEVEL_BUDGET_BYTES))
        max_side = int(args.get('max_side', 2048))
    except ValueError:
        raise BadLevel("t, budget and max_side are integers") from None
    voxel_format = args.get('format', 'u8')
    if voxel_format not in ('u8', 'f16'):
        raise BadLevel("format is u8 or f16")
    voxel_bytes = 2 if voxel_format == 'f16' else 1
    if not 0 <= t < volume.frames:
        raise BadLevel(f"t {t} is outside this image's {volume.frames} frames")
    budget = max(2**20, min(budget, MAX_LEVEL_BYTES))
    box = None
    if args.get('box'):
        try:
            bx, by, bw, bh = (float(v) for v in args['box'].split(","))
        except ValueError:
            raise BadLevel("box is x,y,width,height in full-resolution pixels") from None
        x0, y0 = max(0.0, bx), max(0.0, by)
        x1, y1 = min(float(volume.width), bx + bw), min(float(volume.height), by + bh)
        if x1 - x0 < 1 or y1 - y0 < 1:
            raise BadLevel("the box is outside the image")
        box = (int(x0), int(y0), int(math.ceil(x1)), int(math.ceil(y1)))
    # `z=z0,z1`: only planes z0..z1 (inclusive) -- the 3-D view's detail
    # around where its camera looks, rather than the whole stack's depth.
    z0, z1 = 0, volume.depth - 1
    if args.get('z'):
        try:
            z0, z1 = sorted(int(v) for v in args['z'].split(","))
        except ValueError:
            raise BadLevel("z is z0,z1 (planes, inclusive)") from None
        z0, z1 = max(0, z0), min(volume.depth - 1, z1)
        if z1 < z0:
            raise BadLevel("the planes are outside the stack")
    planes = z1 - z0 + 1
    pool = None
    if args.get('pool'):
        try:
            pool = int(args['pool'])
        except ValueError:
            raise BadLevel("pool is a whole number of planes") from None
        if not 1 <= pool <= volume.depth:
            raise BadLevel(f"pool is 1 to {volume.depth} planes")
    raw_level = args.get('level', 'auto')
    if raw_level == 'auto' and box is not None:
        level = choose_area_level(volume, box, budget // voxel_bytes, max_side, planes)
    elif raw_level == 'auto':
        level = choose_level(volume, budget // voxel_bytes, max_side, planes)
    else:
        try:
            level = int(raw_level)
        except ValueError:
            raise BadLevel("level is an integer or auto") from None
        if not 0 <= level < len(volume.levels):
            raise BadLevel(f"level {level} does not exist")
        rows, cols, _covered = area_at(volume, level, box)
        pooled = -(-planes // (pool or z_pool(volume, level)))
        if (pooled * (rows.stop - rows.start) * (cols.stop - cols.start) * voxel_bytes
                > MAX_LEVEL_BYTES):
            raise BadLevel("that level is too large to send whole")
    named = parse_stamp(args.get('w')) if args.get('w') else None
    return LevelAsk(t, level, voxel_format, box, z0, z1, pool, named)


def read_level(source, ask: LevelAsk, index, rows, cols, pool):
    """The (pooled) block a level payload is quantized from."""
    if pool == 1:
        return source.read_block(ask.level, ask.t, index, ask.z0, ask.z1, rows, cols)
    # Pooled into one preallocated stack. Read in runs of whole pools of
    # about READ_RUN_BYTES, not one pool at a time: a store read is a fixed
    # cost (and a parallel decode) however few planes it covers -- 388 reads
    # of five planes each for a 1937-plane light-sheet level.
    volume = source.volume
    itemsize = np.dtype(volume.dtype).itemsize
    plane = max(1, (rows.stop - rows.start) * (cols.stop - cols.start) * itemsize)
    run_planes = max(pool, (READ_RUN_BYTES // plane) // pool * pool)
    pooled = -(-ask.planes // pool)
    block = np.empty((pooled, rows.stop - rows.start, cols.stop - cols.start),
                     dtype=volume.dtype)
    for start in range(ask.z0, ask.z1 + 1, run_planes):
        stop = min(ask.z1 + 1, start + run_planes)
        run = np.asarray(source.read_block(ask.level, ask.t, index, start, stop - 1, rows, cols))
        for k, first in enumerate(range(0, stop - start, pool)):
            np.max(run[first:first + pool], axis=0, out=block[(start - ask.z0) // pool + k])
    return block


#: The headers a level payload's reader needs, readable across origins.
EXPOSED = ('X-Plexora-Shape, X-Plexora-Level, X-Plexora-Window, X-Plexora-Format, '
           'X-Plexora-Hint, X-Plexora-ZPool, X-Plexora-Box, X-Plexora-Z')


def level_headers(ask: LevelAsk, covered, pool, window) -> dict:
    """What every answer for `ask` says about itself (a 304's too)."""
    return {
        'X-Plexora-Box': ",".join(str(int(v)) for v in covered),
        'X-Plexora-Level': str(ask.level),
        'X-Plexora-ZPool': str(pool),
        'X-Plexora-Window': f"{window[0]:g},{window[1]:g}",
        'X-Plexora-Format': ask.voxel_format,
        'X-Plexora-Z': f"{ask.z0},{ask.z1}",
        'Access-Control-Expose-Headers': EXPOSED,
    }


def payload_headers(shape, hint) -> dict:
    """What only a built payload knows: its shape and its contrast hint."""
    headers = {'X-Plexora-Shape': ",".join(str(int(side)) for side in shape)}
    if hint is not None:
        headers['X-Plexora-Hint'] = f"{hint[0]:g},{hint[1]:g}"
    return headers


# -- the payload ------------------------------------------------------------------


def cached(key):
    with _lock:
        payload = _cache.get(key)
        if payload is not None:
            _cache.move_to_end(key)
        return payload


def _remember(key, payload):
    global _cache_bytes
    with _lock:
        if key in _cache:
            return
        _cache[key] = payload
        _cache_bytes += len(payload[0])
        while _cache_bytes > LEVEL_CACHE_BYTES and len(_cache) > 1:
            _, dropped = _cache.popitem(last=False)
            _cache_bytes -= len(dropped[0])


def hint_of(key, block, qmin, qmax, stats_of):
    """The contrast hint for what is drawn (raw units), once per level and
    window whatever the format: `stats_of` over the block's maximum along z."""
    with _lock:
        if key in _hints:
            _hints.move_to_end(key)
            return _hints[key]
    try:
        stats = stats_of(np.asarray(block.max(axis=0), dtype=np.float64), qmin, qmax)
        hint = (stats["vmin_hint"], stats["vmax_hint"])
    except Exception:  # an all-zero level has no positive voxel to log
        hint = None
    with _lock:
        _hints[key] = hint
        while len(_hints) > 256:
            _hints.popitem(last=False)
    return hint


def hint_over(key, read, window, stats_of):
    """`hint_of` for a block read only when the hint is not already known:
    `read()` returns it. The hint route's (some planes of a level)."""
    with _lock:
        if key in _hints:
            _hints.move_to_end(key)
            return _hints[key]
    with LEVEL_GATE:
        block = read()
    qmin, qmax = window
    return hint_of(key, block, qmin, qmax, stats_of)


def level_payload(key, hint_key, read, window, voxel_format, stats_of, mark=None):
    """`(bytes, shape, hint)` for `key`, built at most once at a time.

    `read()` returns the (pooled) block; `mark(name)` ends a Server-Timing
    phase. A second request for a payload being built waits for that build
    instead of reading the level again."""

    def build():
        with LEVEL_GATE:
            if mark:
                mark("wait")
            block = read()
            if mark:
                mark("read")
            qmin, qmax = window
            voxels = quantize(block, qmin, qmax, voxel_format)
            if mark:
                mark("quant")
            hint = hint_of(hint_key, block, qmin, qmax, stats_of)
            if mark:
                mark("hint")
            payload = (np.ascontiguousarray(voxels).tobytes(), tuple(block.shape), hint)
            if mark:
                mark("enc")
        return payload

    return _single(key, build, mark)


def forwarded_payload(key, fetch, mark=None):
    """`level_payload` for bytes built elsewhere -- a data node's: `fetch()`
    returns `(bytes, shape, hint)` as they arrived, kept and shared alike."""
    return _single(key, fetch, mark)


def _single(key, build, mark):
    payload = cached(key)
    if payload is not None:
        return payload
    with _lock:
        flight = _flights.get(key)
        owner = flight is None
        if owner:
            flight = _flights[key] = {"event": threading.Event(), "payload": None, "error": None}
    if not owner:
        flight["event"].wait()
        if mark:
            mark("wait")
        if flight["error"] is not None:
            raise flight["error"]
        return flight["payload"]
    try:
        payload = build()
        _remember(key, payload)
        flight["payload"] = payload
        return payload
    except BaseException as error:
        flight["error"] = error
        raise
    finally:
        with _lock:
            _flights.pop(key, None)
        flight["event"].set()


def stats() -> dict:
    with _lock:
        return {"entries": len(_cache), "bytes": _cache_bytes, "budget": LEVEL_CACHE_BYTES,
                "hints": len(_hints), "building": len(_flights)}


def clear() -> None:
    global _cache_bytes
    with _lock:
        _cache.clear()
        _hints.clear()
        _cache_bytes = 0
