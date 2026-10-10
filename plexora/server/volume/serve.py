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
"""

from __future__ import annotations

import functools
import hashlib
import os
import threading
from collections import OrderedDict

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


def level_payload(key, hint_key, read, window, voxel_format, stats_of, mark=None):
    """`(bytes, shape, hint)` for `key`, built at most once at a time.

    `read()` returns the (pooled) block; `mark(name)` ends a Server-Timing
    phase. A second request for a payload being built waits for that build
    instead of reading the level again."""
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
