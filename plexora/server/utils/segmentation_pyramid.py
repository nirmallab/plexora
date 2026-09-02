"""Single-pass conversion of a raster label mask into the tiled, pyramidal
outline OME-TIFF the viewer draws.

This replaces the old three-step pipeline (pyramid_assemble -> pyramid_upgrade
-> ensure_outline_segmentation), which built a full *filled* pyramid only to
discard it: nothing ever served the filled file, because the viewer's label
layer is outlines-only (see imageViewer.js's "Label/segmentation tiles must
stay transparent outside outlines"). Writing outlines directly collapses three
passes into one and drops the throwaway intermediate -- on a 34050x5797 uint32
Orion mask that is ~60s and ~5MB, where step one alone previously cost ~60s and
1.19GB.

Outlines are recomputed from filled labels independently at every pyramid level
rather than by downsampling an outline image, which is what keeps boundaries
continuous (and one pixel wide) as the user zooms out. Labels are downsampled
with nearest-neighbour sampling so integer cell IDs survive exactly, and the
full-resolution mask is never required in RAM: each output tile is produced by
one indexed read of the source.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable, NamedTuple, Optional

import numpy as np
import tifffile as tf
import zarr

# Suffix for masks this module generates. The old pipeline wrote
# ".fast-outlines.pyramid.ome.tiff" even when the boundaries were exact;
# generated files are now named for what they are. Paths already recorded in
# config.json keep working -- nothing derives this suffix from a stored path.
OUTLINE_SUFFIX = ".outlines.pyramid.ome.tiff"

# The other thing this module can write: a *filled* label pyramid, for the
# viewer's on-the-fly mode where the shader derives boundaries per fragment
# instead of reading them out of the file (see u32_rgba_map in frag.glsl).
# Cheaper to produce than outlines -- no halo reads, no boundary pass -- and it
# keeps a cell ID on every pixel rather than only on boundaries.
FILLED_SUFFIX = ".labels.pyramid.ome.tiff"

# Stamped into the OME XML of every mask we write, so recognising our own
# output is an exact metadata check rather than a statistical guess about
# pixel content (see generated_mask_kind). The two kinds need distinct markers:
# a filled pyramid must never be mistaken for outlines, since serving it to a
# viewer that is not outlining paints solid blobs over the image.
OUTLINE_MARKER = "plexora-outline-mask"
FILLED_MARKER = "plexora-label-pyramid"

# config.json's `segmentationMode`, and this module's `mode=` argument.
MODE_OUTLINES = "outlines"
MODE_FILLED = "filled"

# What an import produces unless told otherwise. Filled, because measuring the
# two against each other on a real 47296x64246 mask found no reason to prefer
# outlines: per tile, end to end (server read + PNG encode + UPNG decode +
# renderLabelTile), filled came out at 33.9 ms against 34.7 ms for outlines and
# 41 KB against 57 KB -- filled labels compress better than scattered boundary
# pixels, which pays back the client-side boundary pass several times over. And
# a mask that is *already* a tiled pyramid is served untouched in this mode,
# where outlines would spend a minute-plus deriving a second copy of it.
#
# MODE_OUTLINES is still fully supported and exercised by the tests; nothing in
# the UI selects it any more. Pass mode= explicitly to get it back.
DEFAULT_MODE = MODE_FILLED

# Ceiling on the level-0 bytes we will pull into RAM in a single read. Reading
# the whole mask at once is dramatically faster than per-tile reads when the
# source lives on a network mount -- measured 60s vs 179s for the Orion mask on
# an SMB share, because hundreds of small scattered reads pay the round-trip
# each time. Above this budget we fall back to streaming tiles, which bounds
# memory at the cost of that speedup.
_DEFAULT_FULL_READ_MAX_BYTES = 8 * 1024 ** 3

_FINGERPRINT_SCAN_LIMIT = 20000


def _full_read_budget() -> int:
    raw = os.environ.get("PLEXORA_MASK_FULL_READ_MAX_BYTES")
    if not raw:
        return _DEFAULT_FULL_READ_MAX_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_FULL_READ_MAX_BYTES


def derived_output_path(segmentation_path, data_directory=None, *,
                        mode: str = MODE_OUTLINES) -> Path:
    """The NAME a derived mask takes in `data_directory`, or beside the source
    when that is None.

    Naming only -- deciding *which* directory is `resolve_derived_mask`, and
    callers that have to write something should ask that instead.

    The two modes get distinct names so switching a datasource between them
    neither overwrites nor silently reuses the other kind of file.
    """
    source_path = Path(segmentation_path)
    target_dir = Path(data_directory) if data_directory else source_path.parent
    stem = source_path.name
    for extension in (".ome.tiff", ".ome.tif", ".tiff", ".tif", ".png", ".zarr"):
        if stem.lower().endswith(extension):
            stem = stem[: -len(extension)]
            break
    suffix = FILLED_SUFFIX if mode == MODE_FILLED else OUTLINE_SUFFIX
    return target_dir / f"{stem}{suffix}"


def outline_output_path(segmentation_path, data_directory=None) -> Path:
    """Destination for the outline mask derived from `segmentation_path`."""
    return derived_output_path(segmentation_path, data_directory, mode=MODE_OUTLINES)


class DerivedMask(NamedTuple):
    """Where a mask's derived pyramid is, and where a new one would go.

    `existing` is a pyramid that can be served right now, or None. `target` is
    where one would be built. `writable` is False when nothing can be built
    anywhere, which is a thing to say up front rather than halfway through a
    conversion.
    """

    existing: Optional[Path]
    target: Path
    writable: bool


def _newest_mtime_ns(path: Path) -> Optional[int]:
    """Modification time of a file, or of the newest file under a directory.

    A `.zarr` mask is a directory whose own mtime says nothing about its
    contents on most filesystems, so the tree has to be walked -- bounded the
    same way `source_fingerprint` bounds its scan, since the answer only has to
    be good enough to notice that a mask was regenerated.
    """
    try:
        if not path.is_dir():
            return path.stat().st_mtime_ns
    except OSError:
        return None
    newest = 0
    scanned = 0
    try:
        for root, _, files in os.walk(path):
            for name in files:
                try:
                    newest = max(newest, (Path(root) / name).stat().st_mtime_ns)
                except OSError:
                    continue
                scanned += 1
                if scanned >= _FINGERPRINT_SCAN_LIMIT:
                    return newest
    except OSError:
        return None
    return newest


def _is_adoptable(derived: Path, source: Path, mode: str) -> bool:
    """Whether `derived` can be served for `source` without rebuilding it.

    Two conditions, both cheap. It has to be a mask we wrote in the mode this
    datasource wants -- an exact metadata check, not a guess about pixels. And
    it must not be older than the source, which is what stops a mask that was
    regenerated after its pyramid from being served as though nothing had
    happened.

    Deliberately weaker than `_segmentation_mapping_is_current`, which compares
    against the fingerprint config.json recorded at build time and is the right
    check whenever there IS a recorded fingerprint. This one exists for the
    callers that have none: a fresh import, and a data node, which has no
    project and therefore nothing to have recorded.
    """
    if generated_mask_kind(derived) != mode:
        return False
    derived_at = _newest_mtime_ns(derived)
    source_at = _newest_mtime_ns(source)
    if derived_at is None or source_at is None:
        return False
    return derived_at >= source_at


def resolve_derived_mask(segmentation_path, data_directory=None, *,
                         mode: str = MODE_OUTLINES) -> DerivedMask:
    """Where `segmentation_path`'s derived pyramid is, and where one would go.

    Two locations, searched in this order.

    **Beside the source** is the shared one, and it is shared precisely because
    it is derivable from the mask's path alone: a second project, a second user
    on the same mount, and a data node that has no projects at all each arrive
    at the same filename, so the conversion is paid for once. That is the whole
    reason a node can be handed a raw mask and get on with it, where before it
    needed an operator to convert one by hand and then say where the result
    went.

    **`data_directory`** -- the project's own derived root -- is the fallback,
    and is where every mask generated before this convention still lives. That
    is why it is still searched and not merely written to: reopening an
    existing project has to find its existing pyramid, not spend a minute
    rebuilding one it already has.

    Writing beside the source is not always possible, and this is the case the
    old scheme sidestepped by never trying: pipeline output routinely lands in
    a directory that is read-only to the person opening it. So `target` is the
    first of the two that accepts writes, and `writable` is False only when
    neither does.

    `paths.mask_output_preference()` swaps the order for somebody who would
    rather Plexora kept its output under the project -- a mask in a synced or
    backed-up folder is the case that asks for it. It swaps the order for
    FINDING as well as for writing, because a preference that changed only
    where new files go would answer two different things once both existed.
    Neither setting narrows the search: both places are looked in either way,
    so changing your mind costs nothing and orphans nothing.
    """
    from plexora import paths

    source_path = Path(segmentation_path)
    candidates = [derived_output_path(source_path, None, mode=mode)]
    if data_directory is not None:
        # No project directory means a data node, which has no projects to keep
        # anything under. The preference is about a viewer's own filing and
        # does not apply.
        in_project = derived_output_path(source_path, data_directory, mode=mode)
        if paths.mask_output_preference() == "project":
            candidates.insert(0, in_project)
        else:
            candidates.append(in_project)

    existing = next(
        (c for c in candidates if _is_adoptable(c, source_path, mode)), None)
    for candidate in candidates:
        if paths.is_writable(candidate.parent):
            return DerivedMask(existing, candidate, True)
    return DerivedMask(existing, candidates[0], False)


def source_fingerprint(path) -> Optional[str]:
    """Cheap staleness key for a mask source, used to decide whether an
    already-generated outline file still corresponds to it. Stat-only: never
    reads pixels, so it is safe to call on every datasource load.
    """
    source_path = Path(path)
    try:
        stat_result = source_path.stat()
    except OSError:
        return None
    if not source_path.is_dir():
        return f"{stat_result.st_size}-{stat_result.st_mtime_ns}"
    # A .zarr mask is a directory tree, so aggregate over its entries instead.
    count = 0
    total_size = 0
    newest = 0
    for root, _, files in os.walk(source_path):
        for name in files:
            try:
                entry = os.stat(os.path.join(root, name))
            except OSError:
                continue
            count += 1
            total_size += entry.st_size
            newest = max(newest, entry.st_mtime_ns)
            if count >= _FINGERPRINT_SCAN_LIMIT:
                # Pathologically large store: fall back to the directory's own
                # stat rather than walking forever.
                return f"dir-{stat_result.st_mtime_ns}"
    return f"{count}-{total_size}-{newest}"


def _open_level_zero(path):
    """Return (array, closer) for the highest-resolution plane of `path`.

    `is_ome=False` matches how the rest of plexora opens masks: OME metadata on
    these files is decorative, and trusting it makes tifffile reinterpret the
    page layout of some third-party masks.
    """
    if str(path).endswith(".zarr"):
        group = zarr.open(str(path), mode="r")
        if isinstance(group, zarr.Array):
            return group, lambda: None
        return group["0"], lambda: None
    reader = tf.TiffFile(str(path), is_ome=False)
    store = reader.series[0].aszarr(level=0)
    array = zarr.open(store, mode="r")

    def _close():
        store.close()
        reader.close()

    return array, _close


def _memmap_plane(path, expected_shape=None):
    """A memory map of `path`'s full-resolution plane, or None if it can't be
    mapped.

    Worth reaching for before the Zarr adapter whenever pixels are read in
    windows. Segmentation masks are routinely written uncompressed and
    *untiled*, frequently as a single strip spanning the whole image, and the
    adapter has to decode a whole strip to answer any read inside it. Sampling
    twelve 512x512 windows out of a 12GB single-strip Orion mask costs 59s that
    way and 0.015s through a memory map.

    Returns None for anything compressed, tiled or multi-plane, where memmap
    either raises or hands back something that isn't the plane asked for --
    hence `expected_shape`, which callers pass from the reader whose geometry
    they actually trust.
    """
    if str(path).endswith(".zarr"):
        return None
    try:
        candidate = tf.memmap(str(path))
    except Exception:
        return None
    shape = tuple(getattr(candidate, "shape", ()))
    if len(shape) != 2:
        return None
    if expected_shape is not None and shape != tuple(expected_shape):
        return None
    return candidate


def generated_mask_kind(path) -> Optional[str]:
    """MODE_OUTLINES, MODE_FILLED, or None for a mask we did not write.

    Keyed on the OME marker rather than the filename, so a mask the user has
    since renamed or moved is still recognised as generated output.
    """
    candidate = Path(path)
    if not candidate.is_file():
        return None
    try:
        with tf.TiffFile(str(candidate)) as reader:
            recorded = reader.ome_metadata or ""
    except Exception:
        return None
    if OUTLINE_MARKER in recorded:
        return MODE_OUTLINES
    if FILLED_MARKER in recorded:
        return MODE_FILLED
    return None


def is_generated_outline_mask(path) -> bool:
    """True when `path` is an outline mask this module wrote."""
    return generated_mask_kind(path) == MODE_OUTLINES


def label_pyramid_gaps(path) -> Optional[list]:
    """Requirements `path` fails for being served straight to the tile route as
    a filled label layer. An empty list means it can be served untouched; None
    means it could not be read at all.

    Both requirements matter, and "the mask is already filled labels" satisfies
    neither on its own -- which is the usual surprise. A single-level mask has
    nothing to answer a request for level 3 with (`_zarr_level` hands back full
    resolution for every level, so the viewer draws the wrong scale), and an
    untiled mask makes every tile request decode whole strips -- masks are
    routinely written as one strip spanning the entire image.
    """
    candidate = Path(path)
    try:
        if str(path).endswith(".zarr"):
            group = zarr.open(str(path), mode="r")
            if isinstance(group, zarr.Array) or len(group) <= 1:
                return ["it has only one resolution level (no pyramid)"]
            return []
        if not candidate.is_file():
            return None
        with tf.TiffFile(str(candidate), is_ome=False) as reader:
            series = reader.series[0]
            page = series.levels[0].pages[0]
            gaps = []
            if len(series.levels) <= 1:
                gaps.append("it has only one resolution level (no pyramid)")
            if not page.is_tiled:
                gaps.append("it is not tiled")
            return gaps
    except Exception:
        return None


def is_servable_label_pyramid(path) -> bool:
    """True when `path` can be served straight to the tile route as a filled
    label layer: tiled, and pyramidal enough for the zoomed-out levels."""
    return label_pyramid_gaps(path) == []


def looks_like_outline_mask(path) -> bool:
    """Heuristic "is this mask already outlines?" test for *user-supplied*
    files, where there is no metadata marker to trust.

    Sampled from contiguous windows on purpose. An earlier version strided the
    whole plane (`level[::step, ::step]`), which for a large mask meant a step
    of ~23px -- far enough apart that neighbouring samples almost never shared
    a label, so every big filled mask scored as "outlines" and outline
    generation was skipped entirely. Interior-ness is a local property and only
    survives contiguous sampling.
    """
    try:
        array, close = _open_level_zero(path)
    except Exception:
        return False
    try:
        if len(array.shape) != 2 or array.size == 0:
            return False
        height, width = (int(size) for size in array.shape)
        # Geometry stays authoritative from the reader opened above (the one
        # that serves tiles); the memory map, when there is one, only makes the
        # windowed reads below cheap enough to run on a datasource load.
        mapped = _memmap_plane(path, (height, width))
        if mapped is not None:
            array = mapped
        window = 512
        nonzero_total = 0
        pixel_total = 0
        interior_total = 0
        boundary_total = 0
        for y_fraction in (0.3, 0.5, 0.7):
            for x_fraction in (0.2, 0.4, 0.6, 0.8):
                y = min(max(0, int(height * y_fraction) - window // 2), max(0, height - window))
                x = min(max(0, int(width * x_fraction) - window // 2), max(0, width - window))
                sample = np.asarray(array[y:y + window, x:x + window])
                if sample.ndim != 2 or sample.size == 0:
                    continue
                nonzero = sample != 0
                nonzero_total += int(np.count_nonzero(nonzero))
                pixel_total += sample.size
                center = nonzero[1:-1, 1:-1]
                if center.size == 0:
                    continue
                center_values = sample[1:-1, 1:-1]
                same_id_interior = (
                    center
                    & (center_values == sample[:-2, 1:-1])
                    & (center_values == sample[2:, 1:-1])
                    & (center_values == sample[1:-1, :-2])
                    & (center_values == sample[1:-1, 2:])
                )
                interior_total += int(np.count_nonzero(same_id_interior))
                boundary_total += int(np.count_nonzero(center))
    finally:
        close()

    if pixel_total == 0 or nonzero_total == 0 or boundary_total == 0:
        return False
    density = nonzero_total / pixel_total
    interior_fraction = interior_total / boundary_total
    return density <= 0.20 and interior_fraction <= 0.05


def pyramidize_segmentation_mask(
    input_path,
    output_path=None,
    *,
    tile_size: int = 1024,
    compression: Optional[str] = "zlib",
    max_workers: Optional[int] = None,
    outline: bool = True,
    method: str = "exact",
    overwrite: bool = False,
    full_read: Optional[bool] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    stage_callback: Optional[Callable[..., None]] = None,
) -> str:
    """Convert a 2-D label TIFF/Zarr to a tiled, pyramidal OME-TIFF.

    Parameters
    ----------
    input_path
        A single-plane 2-D integer label mask with zero as background. Already
        pyramidal input is fine -- its highest-resolution level is used and a
        fresh outline pyramid is derived from it.
    output_path
        Destination. Defaults to `outline_output_path(input_path)`.
    tile_size
        Square TIFF tile edge, a multiple of 16. Larger tiles cut conversion
        time noticeably on huge masks.
    compression
        Lossless TIFF compression; None writes uncompressed tiles.
    max_workers
        Worker threads tifffile may use for compression.
    outline
        Write only label-boundary pixels, keeping their original cell IDs.
        False writes a conventional filled label pyramid.
    method
        "exact" marks every cell-cell and cell-background boundary
        (8-neighbour). "fast" marks only foreground-background boundaries
        (4-neighbour) -- cheaper, but misses seams where two cells touch.
    overwrite
        Replace an existing destination.
    full_read
        True bulk-reads the whole mask into RAM before tiling, False always
        streams per tile. None (default) decides from the level-0 byte size
        against `PLEXORA_MASK_FULL_READ_MAX_BYTES`.
    progress_callback
        Called as (tiles_written, tiles_total) as the pyramid is written.
    """
    source_path = Path(input_path).expanduser()
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    if not isinstance(tile_size, int) or tile_size < 16 or tile_size % 16:
        raise ValueError("tile_size must be an integer multiple of 16.")
    outline_method = str(method).strip().lower()
    if outline_method not in {"exact", "fast"}:
        raise ValueError("method must be 'exact' or 'fast'.")

    destination = (
        outline_output_path(source_path)
        if output_path is None
        else Path(output_path).expanduser()
    )
    if destination.resolve() == source_path.resolve():
        raise ValueError("output_path must be different from input_path.")
    if destination.exists() and not overwrite:
        raise FileExistsError(
            f"Destination already exists: {destination}. Pass overwrite=True to replace it."
        )

    def announce(stage):
        """Say which phase this is. Deliberately NOT folded into
        `progress_callback`: that reports `(done, total)` within the tile loop
        and its contract is pinned by tests, while this reports the phases
        BEFORE the tile loop -- which is where all the unexplained waiting was.
        """
        if stage_callback is not None:
            stage_callback(stage)

    labels = None
    close_source = None
    memmap = None
    temporary_path: Optional[Path] = None
    try:
        # Metadata-only probe first: shape and dtype decide the read strategy,
        # so they must be known before anything is pulled into memory.
        announce("inspecting")
        if str(source_path).endswith(".zarr"):
            probe, probe_close = _open_level_zero(source_path)
            shape, dtype = probe.shape, np.dtype(probe.dtype)
            probe_close()
        else:
            with tf.TiffFile(str(source_path), is_ome=False) as reader:
                series = reader.series[0]
                shape, dtype = series.shape, np.dtype(series.dtype)
        if len(shape) != 2:
            raise ValueError(
                "input_path must contain one 2-D label plane; "
                f"found shape {tuple(shape)!r}."
            )
        if not np.issubdtype(dtype, np.integer):
            raise TypeError(f"Label mask must have an integer dtype, found {dtype}.")
        height, width = (int(size) for size in shape)
        if height == 0 or width == 0:
            raise ValueError("Label mask must have non-zero height and width.")

        use_full_read = (
            bool(full_read)
            if full_read is not None
            else (height * width * dtype.itemsize) <= _full_read_budget()
        )

        # The dominant stall, and the one that used to sit at 0%: a full-plane
        # read of a whole-slide mask is 60 s locally and 179 s streaming.
        # There is no fraction to report inside `tf.imread` -- it is one call --
        # so this stage moves the bar to the foot of its band and names itself,
        # which is the difference between "working" and "hung".
        announce("preparing")
        if use_full_read:
            # One sequential read, then every tile is served from RAM. Routed
            # through `memmap` so the direct-indexing branches below (which
            # already special-case a real ndarray) pick it up unchanged.
            if str(source_path).endswith(".zarr"):
                array, close_source = _open_level_zero(source_path)
                memmap = np.asarray(array)
                close_source()
                close_source = None
            else:
                memmap = np.asarray(tf.imread(str(source_path), is_ome=False, series=0, level=0))
            labels = memmap
        else:
            # Streaming per tile: map the source where possible, so each tile is
            # a handful of page faults rather than a strip decode.
            memmap = _memmap_plane(source_path, (height, width))
            if memmap is not None:
                labels = memmap
        if labels is None:
            labels, close_source = _open_level_zero(source_path)

        factors = [1]
        while max(
            (height + factors[-1] - 1) // factors[-1],
            (width + factors[-1] - 1) // factors[-1],
        ) > tile_size:
            factors.append(factors[-1] * 2)
        total_tiles = sum(
            (((height + factor - 1) // factor + tile_size - 1) // tile_size)
            * (((width + factor - 1) // factor + tile_size - 1) // tile_size)
            for factor in factors
        )
        tiles_written = 0

        announce("building")
        destination.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}-",
            suffix=".tmp.ome.tiff",
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)

        def outline_tile(halo: np.ndarray) -> np.ndarray:
            center = halo[1:-1, 1:-1]
            if outline_method == "fast":
                foreground = center != 0
                is_boundary = foreground & (
                    (halo[:-2, 1:-1] == 0) | (halo[2:, 1:-1] == 0)
                    | (halo[1:-1, :-2] == 0) | (halo[1:-1, 2:] == 0)
                )
            else:
                # An exact boundary is any labelled pixel adjacent to a
                # *different* label or to background -- not just those next to
                # empty space, which is what makes touching cells separable.
                is_boundary = (center != 0) & (
                    (halo[:-2, 1:-1] != center) | (halo[2:, 1:-1] != center)
                    | (halo[1:-1, :-2] != center) | (halo[1:-1, 2:] != center)
                    | (halo[:-2, :-2] != center) | (halo[:-2, 2:] != center)
                    | (halo[2:, :-2] != center) | (halo[2:, 2:] != center)
                )
            return np.where(is_boundary, center, 0)

        def read_block(y_start, y_stop, x_start, x_stop, factor):
            if factor == 1:
                return np.asarray(labels[y_start:y_stop, x_start:x_stop])
            rows = np.arange(y_start * factor, y_stop * factor, factor)
            columns = np.arange(x_start * factor, x_stop * factor, factor)
            if memmap is not None:
                return np.asarray(labels[np.ix_(rows, columns)])
            return np.asarray(labels.oindex[rows, columns])

        def tiles_for_level(factor: int):
            nonlocal tiles_written
            level_height = (height + factor - 1) // factor
            level_width = (width + factor - 1) // factor
            for y_start in range(0, level_height, tile_size):
                y_stop = min(y_start + tile_size, level_height)
                for x_start in range(0, level_width, tile_size):
                    x_stop = min(x_start + tile_size, level_width)
                    if outline:
                        # Read a one-pixel halo *in this level's* coordinates.
                        # Comparing against real neighbours rather than a
                        # zero-padded edge is what keeps an outline unbroken
                        # where two TIFF tiles meet.
                        read_y_start = max(0, y_start - 1)
                        read_y_stop = min(level_height, y_stop + 1)
                        read_x_start = max(0, x_start - 1)
                        read_x_stop = min(level_width, x_stop + 1)
                        sampled = read_block(
                            read_y_start, read_y_stop, read_x_start, read_x_stop, factor
                        )
                        halo = np.zeros(
                            (y_stop - y_start + 2, x_stop - x_start + 2), dtype=dtype
                        )
                        insert_y = read_y_start - (y_start - 1)
                        insert_x = read_x_start - (x_start - 1)
                        halo[
                            insert_y:insert_y + sampled.shape[0],
                            insert_x:insert_x + sampled.shape[1],
                        ] = sampled
                        tile = outline_tile(halo)
                    else:
                        tile = read_block(y_start, y_stop, x_start, x_stop, factor)
                    if tile.shape != (tile_size, tile_size):
                        padded = np.zeros((tile_size, tile_size), dtype=dtype)
                        padded[: tile.shape[0], : tile.shape[1]] = tile
                        tile = padded
                    tiles_written += 1
                    if progress_callback is not None:
                        progress_callback(tiles_written, total_tiles)
                    yield np.ascontiguousarray(tile)

        with tf.TiffWriter(str(temporary_path), bigtiff=True, ome=True) as writer:
            for level_index, factor in enumerate(factors):
                level_shape = (
                    (height + factor - 1) // factor,
                    (width + factor - 1) // factor,
                )
                metadata = None
                if level_index == 0:
                    metadata = {"axes": "YX", "Channel": {"Name": "cell"}}
                    metadata["Name"] = OUTLINE_MARKER if outline else FILLED_MARKER
                writer.write(
                    tiles_for_level(factor),
                    shape=level_shape,
                    dtype=dtype,
                    tile=(tile_size, tile_size),
                    compression=compression,
                    photometric="minisblack",
                    metadata=metadata,
                    subifds=len(factors) - 1 if level_index == 0 else None,
                    subfiletype=1 if level_index else None,
                    maxworkers=max_workers,
                )
        # The tile loop reported 100% as the last tile was YIELDED to the
        # writer; the compression flush and the rename happen after that, and
        # on a large pyramid they are not instant. A bar that reaches 100% and
        # then waits is the same complaint as one that sits at 0%.
        announce("writing")
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if memmap is not None:
            underlying = getattr(memmap, "_mmap", None)
            if underlying is not None:
                underlying.close()
        if close_source is not None:
            close_source()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return str(destination)


__all__ = [
    "FILLED_MARKER",
    "FILLED_SUFFIX",
    "MODE_FILLED",
    "MODE_OUTLINES",
    "OUTLINE_MARKER",
    "OUTLINE_SUFFIX",
    "DerivedMask",
    "derived_output_path",
    "generated_mask_kind",
    "is_generated_outline_mask",
    "is_servable_label_pyramid",
    "label_pyramid_gaps",
    "looks_like_outline_mask",
    "outline_output_path",
    "pyramidize_segmentation_mask",
    "resolve_derived_mask",
    "source_fingerprint",
]
