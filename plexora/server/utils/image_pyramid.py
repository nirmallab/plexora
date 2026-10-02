"""A pyramidized copy of a large multiplex image that was written flat.

A channel-stack TIFF with one resolution level is the format Plexora would
otherwise register without deriving anything: the viewer gets one level, so a
whole-slide view decodes every full-resolution tile of every channel, and the
overview is built from the entire stack in memory. This module writes the
copy that fixes both -- a tiled SubIFD OME-TIFF with every level the viewer
asks for -- once, beside the original (or under the project when that folder
cannot take it), and recognises that copy again afterwards so the cost is
paid once.

The conversion is a single streaming pass with bounded memory:

* Level 0 is read one tile row at a time, all of its strips or tiles decoded
  in parallel, and written straight out.
* Each slab is reduced 2x2 as it goes past, into a scratch array for the next
  level -- so level k is built from level k-1, never from level 0 again.
  Reduction is integer arithmetic on integer images (exactly the rounding of
  `ome_zarr._reduce2` + `_cast_like`, at a fraction of the cost of the
  float64 mean), one vectorised expression per slab.
* Scratch levels larger than `_IN_MEMORY_SCRATCH_BYTES` live in memory-mapped
  files in a dot-prefixed folder beside the output, removed whatever happens.

The file is written under a temporary name and renamed into place only once
complete (`segmentation_pyramid.write_tiled_pyramid`), so partial output is
never mistaken for a pyramid.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, NamedTuple, Optional

import numpy as np
import tifffile as tf
import zarr

from plexora.server.utils import segmentation_pyramid
from plexora.server.utils.segmentation_pyramid import PyramidError

#: The longest side a single-level image may have before Plexora offers a
#: pyramid. 4096 is four 1024 tiles: a whole-image view of anything at or below
#: it decodes at most sixteen tiles per channel, which is affordable -- the
#: same "affordable" side `brightfield.MAX_SOURCE_SIDE` uses.
FLAT_IMAGE_MAX_SIDE = 4096

#: Stamped as the OME Image Name of every copy this module writes, so
#: recognising one is an exact metadata check.
IMAGE_MARKER = "plexora-image-pyramid"

PYRAMID_SUFFIX = ".pyramid.ome.tiff"

TILE_SIZE = 1024

#: How much of one level Plexora reads in a single slab. The same budget
#: `ome_zarr` downsamples under; an image wider than this allows is read in
#: column windows, which costs nothing but a few more reads.
_SLAB_BYTES = 256 * 1024 * 1024

#: Raw tile bytes the writer gathers per compression batch. tifffile's own
#: default is 512 MB, which on its own outweighs everything else this module
#: holds; a few tiles per worker keeps every thread busy at a fraction of it.
_WRITE_BUFFER_BYTES = 64 * 1024 * 1024

#: A scratch level this small is kept in memory rather than in a file.
_IN_MEMORY_SCRATCH_BYTES = 64 * 1024 * 1024

#: Leftovers of our own interrupted builds older than this are removed when
#: the next build of the same file looks for somewhere to go.
_STALE_LEFTOVER_SECONDS = 24 * 3600

_SUPPORTED_KINDS = "uif"

_SOURCE_SUFFIXES = (".ome.tiff", ".ome.tif", ".qptiff", ".tiff", ".tif")


def _workers() -> int:
    return max(1, min(16, os.cpu_count() or 1))


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def pyramid_gaps(shape, levels: int, dtype, threshold: Optional[int] = None) -> list:
    """Why an image of this geometry needs a pyramid; empty when it does not.

    Empty -- "leave it alone" -- also for anything this module cannot write:
    a rank other than (C, Y, X), an empty image, or a pixel type outside plain
    integers and floats. Those import exactly as before.
    """
    shape = tuple(int(side) for side in shape)
    dtype = np.dtype(dtype)
    if len(shape) != 3 or min(shape) <= 0 or dtype.kind not in _SUPPORTED_KINDS:
        return []
    if int(levels) > 1:
        return []
    height, width = shape[1], shape[2]
    if threshold is None:
        threshold = FLAT_IMAGE_MAX_SIDE
    if max(height, width) <= int(threshold):
        return []
    return [f"{width} x {height} px with no reduced-resolution levels"]


def _open_series(path):
    """`(tiff, series)` for a local channel stack, read the way registration reads it."""
    from plexora.server.utils import tiff_series

    tiff = tf.TiffFile(str(path), is_ome=False)
    try:
        return tiff, tiff_series.channel_series(tiff)
    except Exception:
        tiff.close()
        raise


def flat_image_report(path, threshold: Optional[int] = None) -> Optional[dict]:
    """What the import dialog says about a flat image, or None when it is fine.

    Header-only: no pixel is decoded.
    """
    try:
        tiff, series = _open_series(path)
    except Exception:
        return None
    try:
        shape = tuple(int(side) for side in series.shape)
        reasons = pyramid_gaps(shape, len(series.levels), series.dtype, threshold)
        if not reasons:
            return None
        channels, height, width = shape
        dtype = np.dtype(series.dtype)
        raw = channels * height * width * dtype.itemsize
        factors = segmentation_pyramid.pyramid_factors(height, width, TILE_SIZE)
        return {
            "height": height,
            "width": width,
            "channels": channels,
            "dtype": dtype.name,
            "bytes": raw,
            "levels": len(factors),
            "output_bytes_estimate": raw * 4 // 3,
            "scratch_bytes_estimate": _scratch_bytes(channels, height, width, dtype),
            "reasons": reasons,
        }
    finally:
        tiff.close()


def _scratch_bytes(channels, height, width, dtype) -> int:
    """Disk the scratch levels need: every level but 0 and the in-memory ones."""
    factors = segmentation_pyramid.pyramid_factors(height, width, TILE_SIZE)
    total = 0
    for factor in factors[1:]:
        size = channels * (-(-height // factor)) * (-(-width // factor)) * np.dtype(dtype).itemsize
        if size > _IN_MEMORY_SCRATCH_BYTES:
            total += size
    return total


# --------------------------------------------------------------------------
# Where the copy goes, and recognising it again
# --------------------------------------------------------------------------

def derived_output_path(source, data_directory=None) -> Path:
    """The NAME a pyramidized copy takes, in `data_directory` or beside `source`."""
    source = Path(source)
    stem = source.name
    for suffix in _SOURCE_SUFFIXES:
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    target_dir = Path(data_directory) if data_directory else source.parent
    return target_dir / f"{stem}{PYRAMID_SUFFIX}"


class DerivedImage(NamedTuple):
    """Where an image's pyramidized copy is, and where a new one would go.

    `existing` is a finished copy that matches the source, or None. `target`
    is where one would be built. `writable` is False when no candidate folder
    both accepts writes and has the room -- `reason` says which, in a sentence.
    """

    existing: Optional[Path]
    target: Path
    writable: bool
    reason: str = ""


def _gb(count: int) -> str:
    return f"{count / 1024 ** 3:.1f} GB"


def _free_bytes(directory: Path) -> Optional[int]:
    probe = directory
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _sweep_leftovers(directory: Path, name: str) -> None:
    """Remove our own interrupted builds of `name` that are old enough to be dead."""
    cutoff = time.time() - _STALE_LEFTOVER_SECONDS
    try:
        entries = list(directory.glob(f".{name}-*"))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


def resolve_derived_image(source, data_directory=None, *, needed_bytes: int = 0) -> DerivedImage:
    """Where `source`'s pyramidized copy is, and where one would be written.

    Beside the source first -- the copy is then shared by every project and
    every person who opens that file -- and the project's own folder second.
    `paths.mask_output_preference() == "project"` swaps the order for finding
    and for writing alike, exactly as it does for derived masks.

    A folder is a target only when it accepts writes AND has `needed_bytes`
    free, so "the disk is full" is said before any work starts.
    """
    from plexora import paths

    source = Path(source)
    candidates = [derived_output_path(source)]
    if data_directory is not None:
        in_project = derived_output_path(source, data_directory)
        if paths.mask_output_preference() == "project":
            candidates.insert(0, in_project)
        else:
            candidates.append(in_project)

    existing = next((c for c in candidates if is_adoptable_image(c, source)), None)
    problems = []
    for candidate in candidates:
        folder = candidate.parent
        if not paths.is_writable(folder):
            problems.append(f"{folder} is not writable")
            continue
        _sweep_leftovers(folder, candidate.name)
        free = _free_bytes(folder)
        if needed_bytes and free is not None and free < needed_bytes:
            problems.append(f"{folder} has {_gb(free)} free")
            continue
        return DerivedImage(existing, candidate, True)
    reason = (
        f"There is nowhere to write a pyramidized copy of {source.name}"
        + (f" (it needs about {_gb(needed_bytes)})" if needed_bytes else "")
        + ": " + "; ".join(problems) + ".")
    return DerivedImage(existing, candidates[0], False, reason)


def _ome_image_fields(path) -> Optional[tuple]:
    """`(name, description)` of the first OME Image in `path`, or None."""
    try:
        with tf.TiffFile(str(path)) as tiff:
            text = tiff.ome_metadata
            if not text:
                return None
            root = ElementTree.fromstring(text)
    except Exception:
        return None
    for element in root.iter():
        if element.tag.endswith("}Image") or element.tag == "Image":
            description = ""
            for child in element:
                if child.tag.endswith("Description"):
                    description = child.text or ""
                    break
            return element.get("Name", ""), description
    return None


def _description(path) -> Optional[dict]:
    fields = _ome_image_fields(path)
    if not fields or fields[0] != IMAGE_MARKER:
        return None
    try:
        payload = json.loads(fields[1] or "{}")
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def is_image_copy(path) -> bool:
    """Whether `path` is a pyramidized copy this module wrote."""
    path = Path(path)
    return path.name.lower().endswith(PYRAMID_SUFFIX) and _description(path) is not None


def copy_source(path) -> Optional[str]:
    """The original a copy was made from, as recorded in the copy."""
    payload = _description(path)
    return payload.get("source") if payload else None


def is_adoptable_image(candidate, source) -> bool:
    """Whether `candidate` is a finished copy of `source` as it is now.

    Every test is exact: our marker, the source's current fingerprint, more
    than one level, and the same (C, Y, X). A leftover temporary file never
    has the candidate's name, so it never gets this far.
    """
    candidate = Path(candidate)
    if not candidate.is_file():
        return False
    payload = _description(candidate)
    if not payload:
        return False
    if payload.get("source_key") != segmentation_pyramid.source_fingerprint(source):
        return False
    try:
        with tf.TiffFile(str(candidate), is_ome=False) as copy:
            series = copy.series[0]
            if len(series.levels) < 2:
                return False
            copy_shape = tuple(int(side) for side in series.shape)
        tiff, series = _open_series(source)
        try:
            source_shape = tuple(int(side) for side in series.shape)
        finally:
            tiff.close()
    except Exception:
        return False
    return copy_shape == source_shape


# --------------------------------------------------------------------------
# Building it
# --------------------------------------------------------------------------

def _accumulator(dtype: np.dtype) -> Optional[np.dtype]:
    """The integer type a 2x2 sum of `dtype` fits in, or None for the float path."""
    if dtype.kind == "u" and dtype.itemsize <= 2:
        return np.dtype(np.uint32)
    if dtype.kind == "i" and dtype.itemsize <= 2:
        return np.dtype(np.int32)
    if dtype.kind in "ui" and dtype.itemsize == 4:
        return np.dtype(np.int64)
    return None


def reduce2(block: np.ndarray, dtype=None) -> np.ndarray:
    """2x2 mean of a 2-D block, edge-replicating an odd last row or column.

    The same numbers as `ome_zarr._cast_like(ome_zarr._reduce2(block), dtype)`
    -- round-half-to-even included -- computed without a float64 copy of the
    block: row pairs are summed first (contiguous memory, into an integer
    accumulator), then column pairs, and the rounding is done in integer
    arithmetic. Floats, and 64-bit integers whose sum could overflow, take a
    float64 accumulator instead.
    """
    dtype = np.dtype(dtype or block.dtype)
    height, width = block.shape
    integer = _accumulator(dtype)
    acc = integer or np.dtype(np.float64)
    even_h = height & ~1

    # Rows: (h/2, w), plus the odd last row counted twice -- the replication
    # `_reduce2` does with a concatenate, without copying the block.
    rows = np.empty(((height + 1) // 2, width), dtype=acc)
    if even_h:
        np.add(block[0:even_h:2], block[1:even_h:2], out=rows[: even_h // 2], dtype=acc)
    if height & 1:
        np.multiply(block[-1], 2, out=rows[-1], dtype=acc)
    even_w = width & ~1
    out = np.empty((rows.shape[0], (width + 1) // 2), dtype=acc)
    if even_w:
        np.add(rows[:, 0:even_w:2], rows[:, 1:even_w:2], out=out[:, : even_w // 2])
    if width & 1:
        np.multiply(rows[:, -1], 2, out=out[:, -1])
    del rows

    if integer is not None:
        # sum / 4, rounded half to even: q = floor(sum / 4) and r = sum mod 4;
        # round up when r is 3, or when r is 2 and q is odd.
        quotient = out >> 2
        quotient += ((out & 3) + (quotient & 1)) > 2
        return quotient.astype(dtype, copy=False)
    out *= 0.25
    if dtype.kind in "ui":
        info = np.iinfo(dtype)
        np.rint(out, out=out)
        np.clip(out, info.min, info.max, out=out)
    return out.astype(dtype, copy=False)


def reduce2_parallel(block: np.ndarray, dtype, pool: Optional[ThreadPoolExecutor],
                     parts: int) -> np.ndarray:
    """`reduce2` over even row bands on `pool` -- numpy releases the GIL in
    every ufunc it calls, so the bands genuinely run at once."""
    height = block.shape[0]
    if pool is None or parts <= 1 or height < 4 * parts:
        return reduce2(block, dtype)
    step = -(-height // parts)
    step += step & 1
    out = np.empty(((height + 1) // 2, (block.shape[1] + 1) // 2), dtype=np.dtype(dtype))

    def band(y0):
        reduced = reduce2(block[y0: y0 + step], dtype)
        out[y0 // 2: y0 // 2 + reduced.shape[0]] = reduced

    for future in [pool.submit(band, y0) for y0 in range(0, height, step)]:
        future.result()
    return out


class _Scratch:
    """The levels between 0 and the coarsest, as arrays the next level reads.

    Small ones in memory; the rest memory-mapped in one dot-prefixed folder
    beside the output (the same filesystem, so one free-space check covers
    both), removed by `close`.
    """

    def __init__(self, shapes, dtype, directory: Path, prefix: str):
        self.directory = None
        self.levels = []
        for shape in shapes:
            nbytes = int(np.prod(shape)) * dtype.itemsize
            if nbytes <= _IN_MEMORY_SCRATCH_BYTES:
                self.levels.append(np.empty(shape, dtype=dtype))
                continue
            if self.directory is None:
                self.directory = Path(tempfile.mkdtemp(dir=directory, prefix=prefix))
            path = self.directory / f"level{len(self.levels) + 1}.npy"
            self.levels.append(np.lib.format.open_memmap(
                path, mode="w+", dtype=dtype, shape=shape))

    def close(self):
        self.levels = []
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)
            self.directory = None


def _ome_metadata(channels, channel_names, physical, source, fingerprint) -> dict:
    metadata = {
        "axes": "CYX",
        "Name": IMAGE_MARKER,
        "Description": json.dumps({
            "plexora": "image-pyramid",
            "source": str(source),
            "source_key": fingerprint,
        }),
    }
    if channel_names and len(channel_names) == channels:
        metadata["Channel"] = {"Name": [str(name) for name in channel_names]}
    physical = physical or {}
    for axis in ("X", "Y"):
        size = physical.get(f"physical_size_{axis.lower()}")
        try:
            size = float(size)
        except (TypeError, ValueError):
            continue
        if size > 0:
            metadata[f"PhysicalSize{axis}"] = size
            unit = physical.get(f"physical_size_{axis.lower()}_unit")
            if unit:
                metadata[f"PhysicalSize{axis}Unit"] = str(unit)
    return metadata


def pyramidize_image(
    source,
    destination,
    *,
    channel_names=None,
    physical: Optional[dict] = None,
    tile_size: int = TILE_SIZE,
    compression: Optional[str] = "zlib",
    compression_args: Optional[dict] = None,
    max_workers: Optional[int] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    stage_callback: Optional[Callable[..., None]] = None,
) -> str:
    """Write a tiled pyramid OME-TIFF copy of the channel stack at `source`.

    `source` is never written to. `destination` appears only once complete.
    `compression_args` defaults to zlib level 1: against the default level 6
    it compresses tiles ~1.7x faster for ~5% more bytes on fluorescence data,
    and decoding a tile costs the same either way.
    Raises `PyramidError` (a ValueError) with one sentence for a source that
    cannot be read, an unsupported layout, or a full disk.
    """
    source = Path(source)
    destination = Path(destination)
    workers = int(max_workers or _workers())

    def stage(key, detail=None):
        if stage_callback is not None:
            stage_callback(key, detail)

    stage("inspecting")
    fingerprint = segmentation_pyramid.source_fingerprint(source)
    try:
        tiff, series = _open_series(source)
    except Exception as error:
        raise PyramidError(
            f"{source.name} could not be opened as a TIFF channel stack "
            f"({type(error).__name__}), so no pyramidized copy was made.") from error

    if compression_args is None and compression == "zlib":
        compression_args = {"level": 1}
    scratch = None
    pools = ()
    try:
        shape = tuple(int(side) for side in series.shape)
        dtype = np.dtype(series.dtype)
        if len(shape) != 3 or dtype.kind not in _SUPPORTED_KINDS:
            raise PyramidError(
                f"{source.name} is laid out as {series.axes} {dtype.name}; a "
                "pyramidized copy can only be made of a (channel, y, x) stack "
                "of integer or float pixels.")
        channels, height, width = shape

        stage("preparing")
        # Parallel chunk decode: zarr requests every strip/tile a slab overlaps
        # at once, and tifffile decodes them on `workers` threads.
        level0 = zarr.open(series.aszarr(level=0, maxworkers=workers), mode="r")
        if not isinstance(level0, zarr.Array):
            level0 = level0["0"]
        factors = segmentation_pyramid.pyramid_factors(height, width, tile_size)
        level_shapes = [(-(-height // f), -(-width // f)) for f in factors]
        scratch = _Scratch([(channels,) + level for level in level_shapes[1:]],
                           dtype, destination.parent,
                           prefix=f".{destination.name}-scratch-")
        sources = [level0] + scratch.levels

        # Column window: whole tile rows when they fit the slab budget,
        # otherwise the widest even multiple of the tile that does.
        span_tiles = max(1, _SLAB_BYTES // max(1, tile_size * tile_size * dtype.itemsize))
        span = span_tiles * tile_size

        # Every slab, in the order the writer asks for them: level, channel,
        # tile row, column window. Knowing the next one lets a worker read and
        # reduce it while the writer is still compressing this one.
        order = [
            (k, channel, y0, window)
            for k, (level_h, level_w) in enumerate(level_shapes)
            for channel in range(channels)
            for y0 in range(0, level_h, tile_size)
            for window in range(0, level_w, span)
        ]
        position = {key: index for index, key in enumerate(order)}
        reduce_pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pyramid-reduce")
        prefetch = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pyramid-read")
        pools = (prefetch, reduce_pool)
        pending = {}

        def load(key):
            k, channel, y0, window = key
            level_h, level_w = level_shapes[k]
            y1 = min(y0 + tile_size, level_h)
            stop = min(window + span, level_w)
            try:
                slab = np.asarray(sources[k][channel, y0:y1, window:stop])
            except Exception as error:
                raise PyramidError(
                    f"{source.name} could not be read past row {y0 * factors[k]} "
                    f"of channel {channel + 1} ({type(error).__name__}: {error}); "
                    "the file looks truncated or corrupt, and no copy was kept."
                ) from error
            if k + 1 < len(sources):
                reduced = reduce2_parallel(slab, dtype, reduce_pool, workers)
                sources[k + 1][channel, y0 // 2: y0 // 2 + reduced.shape[0],
                               window // 2: window // 2 + reduced.shape[1]] = reduced
            return slab

        def fetch(key):
            future = pending.pop(key, None)
            slab = future.result() if future is not None else load(key)
            following = position[key] + 1
            # Never across a level boundary: level k+1's slabs read the scratch
            # that the last of level k's reductions is still filling.
            if following < len(order) and order[following][0] == key[0]:
                pending[order[following]] = prefetch.submit(load, order[following])
            return slab

        cache = {"key": None, "slab": None, "x0": 0}
        last_level = {"k": -1}

        def block(channel, factor, y0, y1, x0, x1, level_h, level_w):
            k = factor.bit_length() - 1
            if k != last_level["k"]:
                last_level["k"] = k
                stage("building", f"Building pyramid levels, level {k + 1} of {len(factors)}")
            window = x0 - x0 % span
            key = (k, channel, y0, window)
            if cache["key"] != key:
                cache.update(key=key, slab=fetch(key), x0=window)
            return cache["slab"][:, x0 - cache["x0"]: x1 - cache["x0"]]

        def writer_stage(key, *_):
            if key == "writing":
                stage("writing")

        try:
            segmentation_pyramid.write_tiled_pyramid(
                destination,
                height=height,
                width=width,
                dtype=dtype,
                block=block,
                channels=channels,
                metadata=_ome_metadata(channels, channel_names, physical, source, fingerprint),
                tile_size=tile_size,
                compression=compression,
                compression_args=compression_args,
                max_workers=workers,
                buffer_size=max(_WRITE_BUFFER_BYTES,
                                4 * workers * tile_size * tile_size * dtype.itemsize),
                progress_callback=progress_callback,
                stage_callback=writer_stage,
            )
        except PermissionError as error:
            raise PyramidError(
                f"Plexora is not allowed to write in {destination.parent}, so no "
                "pyramidized copy was made.") from error
    finally:
        for pool in pools:
            pool.shutdown(wait=True, cancel_futures=True)
        if scratch is not None:
            scratch.close()
        tiff.close()
    return str(destination)
