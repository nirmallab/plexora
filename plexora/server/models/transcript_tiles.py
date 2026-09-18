"""Transcript points, tiled so a viewport read is a seek.

Structurally a sibling of `centroid_tiles.py` -- same manifest, same staleness
rule, same atomic temp-dir-and-move, same per-datasource lock -- and deliberately
a COPY rather than a shared base. The two differ in the one dimension that
matters, and a base class covering both would be all branches:

**A transcript has no identity.** Nobody queries transcript 41,203,118; they ask
for every EPCAM within a rectangle. Dropping the id field is 4 bytes off every
record, which at 50 million rows is 200 MB of disk and of wire.

**A gene index per tile.** A tile carries a small header saying where each gene's
run starts, and the body is gene-major. Reading 10 genes out of a 300-gene panel
is then 10 short `np.fromfile` ranges instead of decoding every record in the
tile and masking. This is the entire reason not to reuse the flat centroid
layout, and it is what makes the gene selector feel instant rather than linear in
the panel size.

**No coarse point levels.** Centroids subsample as you zoom out because a cell is
a thing you might click. At whole-slide zoom a transcript is not: what the user
is looking at is density, and density is a raster -- see `density_tile`, which
rasterizes into the same uint16 the channel encoder already takes, so the
low-zoom view costs zero new client rendering code.

    file layout, per tile
      u4   n_genes
      n_genes x (u2 gene, u4 offset, u4 count)      -- sorted by gene
      body: POINT_DTYPE records, gene-major, each gene's run contiguous
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path

import numpy as np

from plexora import paths

CACHE_VERSION = 1
DEFAULT_TILE_SIZE = 512

#: 10 bytes, packed. `gene` is an index into the manifest's vocabulary, not a
#: string: a 300-gene panel needs 9 bits, and repeating "PTPRC" 50 million times
#: is 300 MB of the same six characters.
POINT_DTYPE = np.dtype([
    ("gene", "<u2"),
    ("x", "<f4"),
    ("y", "<f4"),
])

#: One entry of a tile's gene index.
INDEX_DTYPE = np.dtype([
    ("gene", "<u2"),
    ("offset", "<u4"),
    ("count", "<u4"),
])

#: The most genes one panel may have. Xenium's largest published panel is 5,000;
#: the cap exists so `gene` can stay u2 and the failure is a refusal at build
#: time rather than silent wraparound at row 65,537.
MAX_GENES = 65_535

_cache_locks = {}
_cache_locks_guard = threading.Lock()


def _lock_for(key):
    with _cache_locks_guard:
        if key not in _cache_locks:
            _cache_locks[key] = threading.RLock()
        return _cache_locks[key]


def cache_dir(datasource_name, layer_id):
    """This layer's transcript cache.

    A derived artifact, so it goes beside the project when that root can be
    written to and into the user's own root when it cannot -- the same rule
    centroid_tiles follows, through the same function.
    """
    return paths.derived_root(datasource_name) / "transcripts_v1" / _safe(layer_id)


def _safe(layer_id):
    """A layer id as a directory name.

    Layer ids come from an importer reading element names out of somebody's
    store, so they can contain anything a zarr key can. Anything outside the
    safe set becomes an underscore -- and the manifest records the real id, so a
    collision between two ids that sanitize the same is visible as a stale cache
    rather than as one layer serving another's points.
    """
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(layer_id)) or "layer"


def manifest_path(datasource_name, layer_id):
    return cache_dir(datasource_name, layer_id) / "manifest.json"


def read_manifest(datasource_name, layer_id):
    """The manifest as written, or None when this layer has no cache."""
    path = manifest_path(datasource_name, layer_id)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def is_current(manifest, expected):
    keys = ("version", "source", "source_size", "source_mtime_ns",
            "width", "height", "tile_size", "level_count")
    if not manifest:
        return False
    return all(manifest.get(key) == expected.get(key) for key in keys)


def expected_manifest(source, *, width, height, tile_size=DEFAULT_TILE_SIZE,
                      level_count=1, layer_id="transcripts"):
    """What a cache built from this file, for this layer, would record."""
    path = Path(source).expanduser()
    stat = path.stat() if path.exists() else None
    return {
        "version": CACHE_VERSION,
        "layer_id": str(layer_id),
        "source": str(path.resolve()) if stat else str(path),
        "source_size": stat.st_size if stat else None,
        "source_mtime_ns": stat.st_mtime_ns if stat else None,
        "width": int(width),
        "height": int(height),
        "tile_size": max(1, int(tile_size)),
        "level_count": max(1, int(level_count)),
        "record_dtype": "gene:uint16,x:float32,y:float32",
    }


# -- building ---------------------------------------------------------------

def build(datasource_name, layer_id, *, genes, gene_index, x, y, expected,
          progress=None):
    """Write the tile cache for one layer.

    Takes arrays rather than a path because the reading is the adapter's job and
    the tiling is this module's -- which is also what lets the tests build a
    cache without a parquet file in sight.

    @param genes       - the vocabulary, in index order
    @param gene_index  - uint16 per point, indexing `genes`
    @param x / y       - float32 per point, in REFERENCE PIXELS. The conversion
                         from a vendor's microns is the adapter's, done once,
                         because it needs the pixel size and this does not.
    @param progress    - optional callable(done, total) for the staged job
    """
    if len(genes) > MAX_GENES:
        raise ValueError(
            f"{len(genes)} genes is past the {MAX_GENES} a uint16 index can hold")

    root = cache_dir(datasource_name, layer_id)
    tmp_root = root.with_name(f"{root.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)

    gene_index = np.asarray(gene_index, dtype=np.uint16)
    x = np.asarray(x, dtype=np.float32)
    y = np.asarray(y, dtype=np.float32)
    finite = np.isfinite(x) & np.isfinite(y)
    if not finite.all():
        gene_index, x, y = gene_index[finite], x[finite], y[finite]

    tile_size = expected["tile_size"]
    width, height = expected["width"], expected["height"]
    columns = max(1, int(np.ceil(width / tile_size)))
    rows = max(1, int(np.ceil(height / tile_size)))

    tx = np.clip(np.floor(x / tile_size).astype(np.int64), 0, columns - 1)
    ty = np.clip(np.floor(y / tile_size).astype(np.int64), 0, rows - 1)

    # One sort, gene-minor within tile, so each tile's body comes out gene-major
    # and every gene's run is already contiguous. lexsort takes the LAST key as
    # primary, which is the part that is easy to get backwards -- and getting it
    # backwards produces a file that reads fine and returns the wrong gene.
    order = np.lexsort((gene_index, tx, ty))
    tx_sorted, ty_sorted = tx[order], ty[order]

    level_dir = tmp_root / "level_0"
    level_dir.mkdir(parents=True, exist_ok=True)

    starts = ([0] + list(np.flatnonzero(
        (tx_sorted[1:] != tx_sorted[:-1]) | (ty_sorted[1:] != ty_sorted[:-1])) + 1)
        if len(order) else [])
    stops = list(starts[1:]) + [len(order)]

    counts = np.zeros((rows, columns), dtype=np.int64)
    total = len(starts)
    for done, (start, stop) in enumerate(zip(starts, stops), 1):
        run = order[start:stop]
        tile_x, tile_y = int(tx[run[0]]), int(ty[run[0]])
        counts[tile_y, tile_x] = len(run)
        _write_tile(level_dir / f"tile_{tile_x}_{tile_y}.bin",
                    gene_index[run], x[run], y[run])
        if progress and (done % 64 == 0 or done == total):
            progress(done, total)

    manifest = {
        **expected,
        "genes": list(genes),
        "gene_count": len(genes),
        "point_count": int(len(x)),
        "columns": columns,
        "rows": rows,
        # Per-tile counts, for the client's LOD estimate. A few thousand small
        # integers, and it is what lets the viewer decide between points and
        # density from the manifest alone rather than by fetching a tile to find
        # out how many points are in it.
        "tile_counts": counts.tolist(),
    }
    with (tmp_root / "manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle)

    if root.exists():
        shutil.rmtree(root)
    try:
        shutil.move(str(tmp_root), str(root))
    except PermissionError:
        # Windows refuses a rename while another handle is open on the target.
        # If what is already there is current, somebody else built the same
        # thing and there is nothing to do -- the same recovery centroid_tiles
        # makes, for the same reason.
        promoted = read_manifest(datasource_name, layer_id)
        if is_current(promoted, expected):
            return promoted
        raise
    return manifest


def _write_tile(path, genes, xs, ys):
    """One tile: a gene index, then the points, gene-major."""
    order = np.argsort(genes, kind="stable")
    genes, xs, ys = genes[order], xs[order], ys[order]
    unique, starts = np.unique(genes, return_index=True)
    stops = np.r_[starts[1:], len(genes)]

    index = np.empty(len(unique), dtype=INDEX_DTYPE)
    index["gene"] = unique
    index["offset"] = starts
    index["count"] = stops - starts

    body = np.empty(len(genes), dtype=POINT_DTYPE)
    body["gene"] = genes
    body["x"] = xs
    body["y"] = ys

    with open(path, "wb") as handle:
        handle.write(np.uint32(len(unique)).tobytes())
        handle.write(index.tobytes())
        handle.write(body.tobytes())


# -- reading ----------------------------------------------------------------

def read_tile(datasource_name, layer_id, tile_x, tile_y, genes=None, level=0):
    """The points in one tile, optionally only for some genes.

    With `genes`, this is a seek per gene rather than a scan of the tile: the
    header says where each run starts, so a 10-gene selection out of a 300-gene
    panel reads 10 short ranges. That is the whole reason for the header.

    Returns an empty POINT_DTYPE array for a tile that was never written, which
    is the ordinary case -- most of a slide is background.
    """
    path = cache_dir(datasource_name, layer_id) / f"level_{int(level)}" / \
        f"tile_{int(tile_x)}_{int(tile_y)}.bin"
    if not path.exists():
        return np.empty(0, dtype=POINT_DTYPE)

    with open(path, "rb") as handle:
        count = int(np.frombuffer(handle.read(4), dtype="<u4")[0])
        index = np.frombuffer(handle.read(count * INDEX_DTYPE.itemsize), dtype=INDEX_DTYPE)
        body_start = 4 + count * INDEX_DTYPE.itemsize

        if genes is None:
            handle.seek(body_start)
            return np.frombuffer(handle.read(), dtype=POINT_DTYPE)

        wanted = np.asarray(sorted(set(int(g) for g in genes)), dtype=np.uint16)
        runs = index[np.isin(index["gene"], wanted)]
        if not len(runs):
            return np.empty(0, dtype=POINT_DTYPE)
        parts = []
        for run in runs:
            handle.seek(body_start + int(run["offset"]) * POINT_DTYPE.itemsize)
            raw = handle.read(int(run["count"]) * POINT_DTYPE.itemsize)
            parts.append(np.frombuffer(raw, dtype=POINT_DTYPE))
        return np.concatenate(parts) if len(parts) > 1 else parts[0]


def read_region(datasource_name, layer_id, bounds, genes=None, max_points=None):
    """Every point in an image-pixel rectangle, optionally for some genes.

    Tile-aligned rather than exact: a tile that overlaps the rectangle is read
    whole. The client culls, because it is drawing the points anyway and a
    per-point bounds test here would be the same work done twice.
    """
    manifest = read_manifest(datasource_name, layer_id)
    if not manifest:
        return np.empty(0, dtype=POINT_DTYPE)
    tile_size = manifest["tile_size"]
    x0 = max(0, int(np.floor(bounds["minX"] / tile_size)))
    y0 = max(0, int(np.floor(bounds["minY"] / tile_size)))
    x1 = min(manifest["columns"] - 1, int(np.floor(bounds["maxX"] / tile_size)))
    y1 = min(manifest["rows"] - 1, int(np.floor(bounds["maxY"] / tile_size)))

    parts = []
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            part = read_tile(datasource_name, layer_id, tx, ty, genes)
            if len(part):
                parts.append(part)
    if not parts:
        return np.empty(0, dtype=POINT_DTYPE)
    points = np.concatenate(parts) if len(parts) > 1 else parts[0]
    if max_points and len(points) > int(max_points):
        # Evenly spaced rather than the first N: taking a prefix of a gene-major
        # file returns whichever gene sorts first and nothing else, which reads
        # as "the other genes are missing" rather than as "this is a sample".
        step = int(np.ceil(len(points) / int(max_points)))
        points = points[::step][:int(max_points)]
    return points


def gene_indices(manifest, names):
    """Gene names to their indices, skipping any this panel does not have.

    Skipped rather than raising: a saved view naming a gene that a re-imported
    panel no longer carries should draw the rest, not fail.
    """
    if not manifest:
        return []
    lookup = {name: i for i, name in enumerate(manifest.get("genes") or [])}
    return [lookup[name] for name in (names or []) if name in lookup]


# -- density ----------------------------------------------------------------

def density_tile(datasource_name, layer_id, level, tile_x, tile_y, *,
                 tile_size=DEFAULT_TILE_SIZE, genes=None, bin_size=1):
    """Transcript counts per bin, as a uint16 raster the channel encoder takes.

    THE HIGHEST-LEVERAGE DECISION IN THIS FILE. At low zoom what the user is
    looking at is density, not points -- and a density raster served as a
    single-channel uint16 tile goes through `data_model.encode_tile_array`, the
    exact encoder the image channels already use. So the client adds it as an
    ordinary tiled image with `compositeOperation: "lighter"` and a colour, and
    the EXISTING WebGL colorize shader draws it. No new decode path, no shader
    change, nothing added to the per-tile signature. To the viewer, a density
    layer IS a channel -- which is also why it survives into Figure Builder's
    server-side export for free, while points do not.

    Counts stay counts down the pyramid: a level-1 bin holds the sum of the four
    level-0 bins under it, not their mean. Averaging would make a hot spot fade
    out as you zoom away from it, which is exactly backwards.
    """
    span = tile_size * (2 ** int(level))
    origin_x = tile_x * span
    origin_y = tile_y * span
    bins = max(1, int(tile_size // max(1, int(bin_size))))

    out = np.zeros((bins, bins), dtype=np.uint32)
    scale = bins / span

    # Every level-0 tile under this one. At level 0 that is one tile; at level 3
    # it is 64, which is still a handful of small reads and avoids keeping a
    # second copy of the points per level on disk.
    step = 2 ** int(level)
    for dy in range(step):
        for dx in range(step):
            points = read_tile(datasource_name, layer_id,
                               tile_x * step + dx, tile_y * step + dy, genes)
            if not len(points):
                continue
            ix = np.clip(((points["x"] - origin_x) * scale).astype(np.int64), 0, bins - 1)
            iy = np.clip(((points["y"] - origin_y) * scale).astype(np.int64), 0, bins - 1)
            np.add.at(out, (iy, ix), 1)

    # uint16 because that is what the channel path carries. A bin holding more
    # than 65,535 transcripts is clipped rather than wrapped: a saturated hot
    # spot reads as "very dense", and a wrapped one reads as empty.
    return np.minimum(out, 65_535).astype(np.uint16)
