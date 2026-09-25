"""Gridded expression, tiled so a gene's view of the slide is a few seeks.

A sibling of `transcript_tiles.py` -- same manifest, same staleness rule, same
temp-dir-and-move, same per-layer lock -- and deliberately a copy of that
scaffolding rather than a base class, for the reason the two differ:

**A bin is already a count on a grid.** Transcripts are a scatter, and their
rasterizers count one per record and smooth it into a density. A sequencing
array (Visium HD's 2 micron squares, and anything shaped like it) states the
count per square itself, so a record here is `(dx, dy, count)` inside a store
tile and a tile of the picture is the counts pooled EXACTLY -- no blur, no
estimate. Pooling by powers of two from the grid origin is also precisely how
Space Ranger makes its own 8 and 16 micron bins out of the 2 micron ones, so
the 8 micron picture here is the 8 micron matrix, square for square.

**Layers of the store are poolings, not images.** The store holds the grid at
pooling 1 and at every power of four above it that a zoomed-out tile can ask
for (1, 4, 16 for a 2 micron grid). A tile reads the coarsest stored pooling
that divides what it draws, so a whole-slide tile of one gene is a handful of
short runs instead of every molecule on the slide.

**A dense gene index per store tile.** Every store tile starts with one
`(offset, count)` row per gene -- plus one for the TOTAL pseudo-gene, which is
every gene summed -- so reading one gene is two seeks whatever the panel size.
The sorted, sparse index transcripts use would have to be read whole (180 KB
for an 18,000-gene panel) on every one of the several hundred store tiles a
whole-slide view touches.

    file layout, per store tile
      u4   n_rows                     -- genes + 1
      n_rows x (u4 offset, u4 count)  -- in records, row = gene index
      body: records, gene-major, the TOTAL run last

**The frame is the grid, not the reference image.** A tile's pixel coordinates
are the bin grid times `supersample`, and the grid's registration onto the
reference -- for Visium HD a 180-degree turn and a mirror -- is the layer's
transform, drawn by the viewer. Nothing about the registration is baked into
the store, which is why re-registering a layer never rebuilds it.

The reader is not here. Which file a grid came from and how its barcodes name
squares is the vendor plugin's business; `build` takes blocks of a CSR matrix
with a row and column per barcode, which is also what lets the tests build a
store without an h5 file in sight.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

import numpy as np

from plexora import paths
from plexora.server.models import transcript_tiles

#: Bumped when the RECORD LAYOUT or the manifest's contract changes. A store
#: written by an older version must never be decoded by a newer one.
CACHE_VERSION = 1

#: Grid units per store-tile side, at every pooling. 256 so that `dx`/`dy` fit
#: a byte.
STORE_TILE = 256

#: The render tile, in LAYER pixels.
DEFAULT_TILE_SIZE = 512

#: Layer pixels per bin at level 0. A power of two, so that every level's tile
#: spans a whole number of bins. Eight makes a 2 micron bin eight screen
#: pixels at full zoom -- big enough that the grid between bins is visible,
#: which is what says "this is a measurement per square" rather than a blur.
DEFAULT_SUPERSAMPLE = 8

#: A level-0 record. Two micron squares never hold 65,535 molecules; a count
#: that did is clamped and counted into the manifest's `saturated_records`.
RECORD_DTYPE = np.dtype([("dx", "u1"), ("dy", "u1"), ("count", "<u2")])

#: A pooled record. A 32 micron square of TOTAL is past a u2 easily.
POOLED_DTYPE = np.dtype([("dx", "u1"), ("dy", "u1"), ("count", "<u4")])

INDEX_DTYPE = np.dtype([("offset", "<u4"), ("count", "<u4")])

#: What pass 1 spills per record: the store-tile-local bin and the gene.
_SPILL_DTYPE = np.dtype([("gene", "<u2"), ("dx", "u1"), ("dy", "u1"),
                         ("count", "<u2")])
_POOLED_SPILL_DTYPE = np.dtype([("gene", "<u2"), ("dx", "u1"), ("dy", "u1"),
                                ("count", "<u4")])

#: The last index row is TOTAL, so the vocabulary tops out one short of u2.
MAX_GENES = 65_534

#: The name the style asks for to mean "every gene summed".
TOTAL = "total"

#: Per-gene histogram bucket edges, for the automatic contrast window. Exact
#: integers at the low end, where 2 micron counts live, geometric above.
HIST_EDGES = np.unique(np.rint(np.geomspace(1, 1 << 24, 160))).astype(np.int64)

#: The share of a gene's non-empty bins that the automatic window leaves
#: unsaturated.
WINDOW_PERCENTILE = 0.99

_locks = {}
_locks_guard = threading.Lock()
_manifest_cache = {}
_stats_cache = {}
_tissue_cache = {}


def _lock_for(key):
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.RLock()
        return _locks[key]


def _safe(layer_id):
    return "".join(c if (c.isalnum() or c in "-_") else "_"
                   for c in str(layer_id)) or "layer"


def cache_dir(datasource_name, layer_id):
    """This layer's bin store, beside the project when that root is writable."""
    return (paths.derived_root(datasource_name) / f"bins_v{CACHE_VERSION}"
            / _safe(layer_id))


def manifest_path(datasource_name, layer_id):
    return cache_dir(datasource_name, layer_id) / "manifest.json"


def _cached(cache, path, loader):
    """`loader(path)`, remembered for as long as the file's mtime holds.

    The manifest carries every gene name (300 KB for a whole-transcriptome
    panel), and it is read on every tile request. Re-parsing it per tile would
    cost more than the tile.
    """
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        cache.pop(str(path), None)
        return None
    hit = cache.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        value = loader(path)
    except (OSError, ValueError):
        return None
    cache[str(path)] = (stamp, value)
    return value


def _load_manifest(path):
    with path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    # A name -> row lookup, built once per manifest rather than per request.
    lookup = {}
    for index, name in enumerate(manifest.get("genes") or []):
        lookup.setdefault(str(name), index)
        lookup.setdefault(str(name).lower(), index)
    for index, gene_id in enumerate(manifest.get("gene_ids") or []):
        lookup.setdefault(str(gene_id), index)
    manifest["_lookup"] = lookup
    return manifest


def read_manifest(datasource_name, layer_id):
    """The manifest as written, or None when this layer has no store."""
    return _cached(_manifest_cache, manifest_path(datasource_name, layer_id),
                   _load_manifest)


def public_manifest(manifest):
    """The manifest without the private lookup, for a JSON answer."""
    if not manifest:
        return manifest
    return {key: value for key, value in manifest.items()
            if not key.startswith("_")}


def read_stats(datasource_name, layer_id):
    """`(genes + 1, len(hist_poolings), 4)` float32: nnz, sum, p99, max."""
    path = cache_dir(datasource_name, layer_id) / "gene_stats.npy"
    return _cached(_stats_cache, path, lambda p: np.load(p))


def read_tissue(datasource_name, layer_id, pooling):
    """Which squares of this pooling hold at least one measured bin, memmapped."""
    path = cache_dir(datasource_name, layer_id) / f"tissue_p{int(pooling)}.npy"
    return _cached(_tissue_cache, path, lambda p: np.load(p, mmap_mode="r"))


def revision(manifest):
    """A token that changes whenever the tiles this store serves could.

    Rides the tile's ETag. A bin tile is derived per request, so a rebuild
    or a change to this module's arithmetic changes the picture without
    changing the project record the rest of the ETag is built from.
    """
    return f"b{CACHE_VERSION}-{(manifest or {}).get('built_ns', 0)}"


def is_current(manifest, expected):
    keys = ("version", "record_dtype", "source", "source_size",
            "source_mtime_ns", "columns", "rows", "store_tile", "store_sides",
            "tile_size", "supersample")
    if not manifest:
        return False
    return all(manifest.get(key) == expected.get(key) for key in keys)


def _side_for(store_tile, pooling):
    return max(16, min(store_tile, (store_tile * 4) // max(1, pooling)))


def _side(manifest, pooling):
    """Grid units per store-tile side at one stored pooling."""
    sides = manifest.get("store_sides") or {}
    found = sides.get(str(int(pooling)))
    return int(found) if found else int(manifest.get("store_tile") or STORE_TILE)


def _powers(limit, base):
    found, value = [], 1
    while value <= limit:
        found.append(value)
        value *= base
    return found


def expected_manifest(source, *, columns, rows, bin_um, microns_per_pixel=None,
                      tile_size=DEFAULT_TILE_SIZE,
                      supersample=DEFAULT_SUPERSAMPLE,
                      store_tile=STORE_TILE, layer_id="bins"):
    """What a store built from this file, for this grid, would record."""
    path = Path(source).expanduser()
    stat = path.stat() if path.exists() else None
    supersample = int(supersample)
    if supersample < 1 or supersample & (supersample - 1):
        raise ValueError("supersample must be a power of two")
    tile_size = max(1, int(tile_size))
    level_count = transcript_tiles.aggregate_levels(
        int(columns) * supersample, int(rows) * supersample, tile_size)
    # The coarsest pooling any level draws: at level L one tile pixel is 2^L
    # layer pixels, which is 2^L / supersample bins.
    max_pooling = max(1, (2 ** (level_count - 1)) // supersample)
    return {
        "version": CACHE_VERSION,
        "layer_id": str(layer_id),
        "source": str(path.resolve()) if stat else str(path),
        "source_size": stat.st_size if stat else None,
        "source_mtime_ns": stat.st_mtime_ns if stat else None,
        "columns": int(columns),
        "rows": int(rows),
        "bin_um": float(bin_um),
        "microns_per_pixel": (float(microns_per_pixel)
                              if microns_per_pixel else None),
        "store_tile": int(store_tile),
        "tile_size": tile_size,
        "supersample": supersample,
        "level_count": level_count,
        "width": int(columns) * supersample,
        "height": int(rows) * supersample,
        "store_poolings": _powers(max_pooling, 4),
        # Grid units per store-tile side AT EACH POOLING. Level 0 tiles 256
        # squares a side; a pooled level tiles 1,024 bins of ground a side
        # (256 squares at 8 microns, 64 at 32), so no store tile at any
        # pooling holds much more than a level-0 tile does. One side for all
        # of them made a 32 micron tile a sixteenth of the slide -- tens of
        # millions of records sorted in one piece, and nine gigabytes of
        # memory to do it.
        "store_sides": {str(p): _side_for(int(store_tile), p)
                        for p in _powers(max_pooling, 4)},
        "hist_poolings": _powers(max(max_pooling, 8), 2),
        "record_dtype": "dx:u1,dy:u1,count:u2|pooled:dx:u1,dy:u1,count:u4",
        "units": "bins",
    }


# -- building ---------------------------------------------------------------

def _sweep_old_versions(root):
    parent = Path(root).parent.parent
    current = f"bins_v{CACHE_VERSION}"
    if not parent.is_dir():
        return
    for sibling in parent.glob("bins_v*"):
        if sibling.name != current and sibling.is_dir():
            shutil.rmtree(sibling, ignore_errors=True)


def build(datasource_name, layer_id, *, genes, gene_ids=None, blocks, expected,
          progress=None, stage=None, block_count=None):
    """Write the bin store for one layer.

    @param genes    - the vocabulary, in the matrix's feature order
    @param gene_ids - the matching stable ids (Ensembl), or None
    @param blocks   - iterable of `(rows, cols, indptr, indices, data)`: one
                      block of barcodes as CSR over genes, with each barcode's
                      grid row and column. `indptr` is block-local (starts 0).
    @param expected - `expected_manifest(...)`
    @param progress - optional callable(done, total), within the current stage
    @param stage    - optional callable(name) announcing "read", "tiling",
                      "stats" as the build crosses them
    """
    if len(genes) > MAX_GENES:
        raise ValueError(
            f"{len(genes)} genes is past the {MAX_GENES} one store can index")
    root = cache_dir(datasource_name, layer_id)
    with _lock_for((datasource_name, layer_id)):
        return _build_locked(root, genes=list(genes),
                             gene_ids=list(gene_ids or []), blocks=blocks,
                             expected=expected, progress=progress, stage=stage,
                             block_count=block_count)


class _Spill:
    """Per-store-tile append files, opened and closed per write.

    Never held open: a whole slide is several hundred store tiles, and macOS
    gives a process 256 descriptors by default.
    """

    def __init__(self, directory, prefix):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.prefix = prefix
        self.keys = set()

    def path(self, sx, sy):
        return self.directory / f"{self.prefix}_{sx}_{sy}.bin"

    def append(self, sx, sy, records):
        if not len(records):
            return
        self.keys.add((int(sx), int(sy)))
        with open(self.path(sx, sy), "ab") as handle:
            handle.write(records.tobytes())

    def scatter(self, sx, sy, records):
        """Append `records` to the tile each row names, one open per tile."""
        if not len(records):
            return
        side = int(sx.max()) + 1
        key = sy.astype(np.int64) * side + sx
        order = np.argsort(key, kind="stable")
        key, records = key[order], records[order]
        starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
        stops = np.r_[starts[1:], len(key)]
        for start, stop in zip(starts, stops):
            tile_key = int(key[start])
            self.append(tile_key % side, tile_key // side, records[start:stop])


def _bucket(values):
    return np.clip(np.searchsorted(HIST_EDGES, values, side="right") - 1,
                   0, len(HIST_EDGES) - 1)


class _Stats:
    """Per-gene, per-pooling histograms of non-empty bin counts."""

    def __init__(self, gene_rows, poolings):
        self.rows = gene_rows
        self.poolings = list(poolings)
        self.hist = np.zeros((gene_rows, len(self.poolings), len(HIST_EDGES)),
                             dtype=np.int64)
        self.maximum = np.zeros((gene_rows, len(self.poolings)), dtype=np.int64)
        self.total = np.zeros(gene_rows, dtype=np.float64)

    def add(self, pooling, gene, count):
        """Fold in one store tile's squares. `gene` must be ascending."""
        if not len(gene):
            return
        index = self.poolings.index(pooling)
        buckets = _bucket(count)
        flat = np.bincount(gene.astype(np.int64) * len(HIST_EDGES) + buckets,
                           minlength=self.rows * len(HIST_EDGES))
        self.hist[:, index, :] += flat.reshape(self.rows, len(HIST_EDGES))
        starts = np.flatnonzero(np.r_[True, gene[1:] != gene[:-1]])
        peaks = np.maximum.reduceat(count.astype(np.int64), starts)
        owners = gene[starts]
        self.maximum[owners, index] = np.maximum(self.maximum[owners, index],
                                                 peaks)
        if pooling == 1:
            self.total += np.bincount(gene, weights=count,
                                      minlength=self.rows)

    def finish(self):
        """`(rows, poolings, 4)` float32: nnz, sum, p99, max."""
        out = np.zeros((self.rows, len(self.poolings), 4), dtype=np.float32)
        nnz = self.hist.sum(axis=2)
        out[..., 0] = nnz
        out[..., 1] = self.total[:, None]
        out[..., 3] = self.maximum
        cumulative = np.cumsum(self.hist, axis=2)
        target = np.ceil(nnz * WINDOW_PERCENTILE)
        edges = HIST_EDGES.astype(np.float64)
        upper = np.r_[edges[1:], edges[-1] * 2]
        for g in range(self.rows):
            for p in range(len(self.poolings)):
                if not nnz[g, p]:
                    continue
                row = cumulative[g, p]
                bucket = int(np.searchsorted(row, target[g, p], side="left"))
                bucket = min(bucket, len(edges) - 1)
                before = row[bucket - 1] if bucket else 0
                inside = max(1, self.hist[g, p, bucket])
                share = (target[g, p] - before) / inside
                # Bucket [lo, hi) holds integers lo..hi-1, so the p99 sits
                # somewhere in there; interpolate, and never past the max.
                value = edges[bucket] + (upper[bucket] - 1 - edges[bucket]) * share
                out[g, p, 2] = min(value, self.maximum[g, p])
        return out


def _pool(gene, ux, uy, count, factor, rows):
    """Records summed over `factor` x `factor` squares. Coordinates divide."""
    from scipy import sparse

    px, py = ux // factor, uy // factor
    side = int(max(px.max(), py.max())) + 1 if len(px) else 1
    cell = py.astype(np.int64) * side + px
    matrix = sparse.coo_matrix(
        (count.astype(np.int64), (gene.astype(np.int64), cell)),
        shape=(rows, side * side)).tocsr()
    matrix.sum_duplicates()
    out_gene = np.repeat(np.arange(rows, dtype=np.int64), np.diff(matrix.indptr))
    out_cell = matrix.indices.astype(np.int64)
    return (out_gene, out_cell % side, out_cell // side,
            matrix.data.astype(np.int64))


def _write_store_tile(path, gene_rows, gene, dx, dy, count, dtype):
    if len(gene) > 1 and not (gene[1:] >= gene[:-1]).all():
        order = np.argsort(gene.astype(np.uint16), kind="stable")
        gene, dx, dy, count = gene[order], dx[order], dy[order], count[order]
    counts = np.bincount(gene, minlength=gene_rows).astype(np.int64)
    if len(counts) > gene_rows:
        raise ValueError("a gene index past the vocabulary")
    index = np.empty(gene_rows, dtype=INDEX_DTYPE)
    index["count"] = counts
    index["offset"] = np.r_[0, np.cumsum(counts)[:-1]]
    body = np.empty(len(gene), dtype=dtype)
    body["dx"] = dx
    body["dy"] = dy
    body["count"] = count
    with open(path, "wb") as handle:
        handle.write(np.uint32(gene_rows).tobytes())
        handle.write(index.tobytes())
        handle.write(body.tobytes())


def _build_locked(root, *, genes, gene_ids, blocks, expected, progress, stage,
                  block_count):
    started = time.time()
    _sweep_old_versions(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    tmp_root = root.with_name(
        f"{root.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    tmp_root.mkdir(parents=True)
    spill_root = tmp_root / "spill"

    columns, rows = expected["columns"], expected["rows"]
    side = expected["store_tile"]
    total_index = len(genes)
    gene_rows = len(genes) + 1
    store_poolings = list(expected["store_poolings"])
    hist_poolings = list(expected["hist_poolings"])
    tissue = np.zeros((rows, columns), dtype=np.uint8)
    saturated = 0
    nnz = 0
    bins = 0

    try:
        # -- pass 1: every record to the store tile it lands in ------------
        if stage:
            stage("read")
        spill = _Spill(spill_root, "p1")
        if block_count is None and hasattr(blocks, "__len__"):
            block_count = len(blocks)
        for done, (brows, bcols, indptr, indices, data) in enumerate(blocks, 1):
            brows = np.asarray(brows, dtype=np.int64)
            bcols = np.asarray(bcols, dtype=np.int64)
            indptr = np.asarray(indptr, dtype=np.int64)
            keep = ((brows >= 0) & (brows < rows)
                    & (bcols >= 0) & (bcols < columns))
            tissue[brows[keep], bcols[keep]] = 1
            bins += int(keep.sum())
            lengths = np.diff(indptr)
            owner = np.repeat(np.arange(len(lengths)), lengths)
            data = np.asarray(data)
            indices = np.asarray(indices)
            live = keep[owner] & (data > 0)
            owner, gene, value = owner[live], indices[live], data[live]
            gx, gy = bcols[owner], brows[owner]
            saturated += int((value > 65_535).sum())
            records = np.empty(len(gene), dtype=_SPILL_DTYPE)
            records["gene"] = gene
            records["dx"] = gx % side
            records["dy"] = gy % side
            records["count"] = np.minimum(value, 65_535)
            nnz += len(records)
            spill.scatter(gx // side, gy // side, records)
            if progress:
                progress(done, block_count or done + 1)

        # -- pass 2: level-0 store tiles, and the pooled records ---------
        if stage:
            stage("tiling")
        stats = _Stats(gene_rows, hist_poolings)
        pooled_spills = {p: _Spill(spill_root, f"p{p}")
                         for p in store_poolings if p > 1}
        (tmp_root / "p1").mkdir()
        keys = sorted(spill.keys)
        for done, (sx, sy) in enumerate(keys, 1):
            path = spill.path(sx, sy)
            records = np.fromfile(path, dtype=_SPILL_DTYPE)
            path.unlink()
            # Gene-major once, here: the store tile is written in this order
            # and the stats want it. A stable sort of u2 keys is a radix sort.
            records = records[np.argsort(records["gene"], kind="stable")]
            gene = records["gene"].astype(np.int64)
            ux = records["dx"].astype(np.int64)
            uy = records["dy"].astype(np.int64)
            count = records["count"].astype(np.int64)
            del records
            raster = np.bincount(uy * side + ux, weights=count,
                                 minlength=side * side).astype(np.int64)
            where = np.flatnonzero(raster)
            t_gene = np.full(len(where), total_index, dtype=np.int64)
            t_ux, t_uy, t_count = where % side, where // side, raster[where]
            stats.add(1, np.r_[gene, t_gene], np.r_[count, t_count])
            _write_store_tile(
                tmp_root / "p1" / f"tile_{sx}_{sy}.bin", gene_rows,
                np.r_[gene, t_gene], np.r_[ux, t_ux], np.r_[uy, t_uy],
                np.minimum(np.r_[count, t_count], 65_535), RECORD_DTYPE)

            # Pooled up by twos, each step from the one before: a record at
            # pooling 2q is the sum of four at q, and every pooled square
            # lies inside this store tile because q divides the tile side.
            current = (gene, ux, uy, count)
            total_raster = raster.reshape(side, side)
            q = 1
            while q * 2 <= hist_poolings[-1] and q * 2 <= side:
                current = _pool(*current, 2, gene_rows)
                q *= 2
                block = total_raster.reshape(side // q, q, side // q, q).sum((1, 3))
                t_where = np.flatnonzero(block)
                t_gene = np.full(len(t_where), total_index, dtype=np.int64)
                t_count = block.ravel()[t_where]
                if q in hist_poolings:
                    stats.add(q, np.r_[current[0], t_gene],
                              np.r_[current[3], t_count])
                if q in pooled_spills:
                    per = side // q                  # pooled squares per tile side
                    q_side = _side(expected, q)
                    pgene = np.r_[current[0], t_gene]
                    pux = np.r_[current[1], t_where % per] + sx * per
                    puy = np.r_[current[2], t_where // per] + sy * per
                    records = np.empty(len(pgene), dtype=_POOLED_SPILL_DTYPE)
                    records["gene"] = pgene
                    records["dx"] = pux % q_side
                    records["dy"] = puy % q_side
                    records["count"] = np.r_[current[3], t_count]
                    pooled_spills[q].scatter(pux // q_side, puy // q_side,
                                             records)
            if progress:
                progress(done, len(keys))

        # -- pass 3: the pooled store tiles --------------------------------
        for q, pooled in pooled_spills.items():
            (tmp_root / f"p{q}").mkdir()
            for sx, sy in sorted(pooled.keys):
                path = pooled.path(sx, sy)
                records = np.fromfile(path, dtype=_POOLED_SPILL_DTYPE)
                path.unlink()
                # Sorted as the packed records, never widened: a u2 key sorts
                # by radix, and one copy of 8-byte records is the peak.
                records = records[np.argsort(records["gene"], kind="stable")]
                _write_store_tile(
                    tmp_root / f"p{q}" / f"tile_{sx}_{sy}.bin", gene_rows,
                    records["gene"], records["dx"], records["dy"],
                    records["count"], POOLED_DTYPE)
                del records

        # -- stats and tissue ------------------------------------------------
        if stage:
            stage("stats")
        np.save(tmp_root / "gene_stats.npy", stats.finish())
        for q in store_poolings:
            if q == 1:
                pooled_tissue = tissue
            else:
                prow, pcol = -(-rows // q), -(-columns // q)
                padded = np.zeros((prow * q, pcol * q), dtype=np.uint8)
                padded[:rows, :columns] = tissue
                pooled_tissue = padded.reshape(prow, q, pcol, q).max((1, 3))
            np.save(tmp_root / f"tissue_p{q}.npy", pooled_tissue)
        shutil.rmtree(spill_root, ignore_errors=True)

        gene_counts = stats.total[:len(genes)].astype(np.int64).tolist()
        manifest = {
            **expected,
            "genes": [str(g) for g in genes],
            "gene_ids": [str(g) for g in gene_ids],
            "gene_count": len(genes),
            "total_index": total_index,
            "gene_counts": gene_counts,
            "total_count": int(stats.total[total_index]),
            "bin_count": bins,
            "nnz": int(nnz),
            "saturated_records": saturated,
            "build_seconds": round(time.time() - started, 1),
            "built_ns": time.time_ns(),
        }
        with (tmp_root / "manifest.json").open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle)

        if root.exists():
            shutil.rmtree(root)
        shutil.move(str(tmp_root), str(root))
    except BaseException:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise
    _manifest_cache.pop(str(root / "manifest.json"), None)
    return manifest


# -- reading ------------------------------------------------------------------

def levels(manifest):
    return int((manifest or {}).get("level_count") or 1)


def effective_pooling(manifest, level, requested=None):
    """The pooling a level-L tile draws: what was asked, or coarser.

    One tile pixel at level L is 2^L layer pixels, which is 2^L / supersample
    bins -- a square finer than that has nowhere to be drawn. Always a power
    of two, so it lines up with Space Ranger's own nesting.
    """
    supersample = int(manifest.get("supersample") or DEFAULT_SUPERSAMPLE)
    floor = max(1, (2 ** int(level)) // supersample)
    return max(requested_pooling(requested), floor)


def requested_pooling(requested=None):
    """The square size the user asked for, as a power of two (1 for junk).

    What the COLOUR SCALE is measured at, at every zoom: a coarser level
    only draws those squares merged, it does not change what a colour means.
    """
    try:
        asked = max(1, int(requested or 1))
    except (TypeError, ValueError):
        asked = 1
    return 1 << (asked.bit_length() - 1)           # down to a power of two


def _scaled_fields(fields, pooling, scale_pooling):
    """Pooled SUMS as the mean per requested square.

    A level that draws 8x8-bin squares when 2x2 was asked holds sixteen
    requested squares' counts in each; dividing by sixteen puts them back on
    the requested squares' count scale, so they read off the same window and
    the same ramp -- a region keeps its colour as the view zooms out.
    """
    if not scale_pooling or pooling <= scale_pooling:
        return fields
    area = float((pooling // scale_pooling) ** 2)
    return {gene: field / area for gene, field in fields.items()}


def gene_indices(manifest, names):
    """Store rows for these names (or Ensembl ids). `total` is TOTAL."""
    lookup = manifest.get("_lookup") or {}
    found = []
    for name in names or []:
        text = str(name).strip()
        if text.lower() == TOTAL:
            found.append(int(manifest["total_index"]))
            continue
        index = lookup.get(text, lookup.get(text.lower()))
        if index is not None:
            found.append(int(index))
    return found


def read_run(datasource_name, layer_id, pooling, sx, sy, gene):
    """One gene's records in one store tile. Two seeks."""
    path = (cache_dir(datasource_name, layer_id) / f"p{int(pooling)}"
            / f"tile_{int(sx)}_{int(sy)}.bin")
    dtype = RECORD_DTYPE if int(pooling) == 1 else POOLED_DTYPE
    try:
        with open(path, "rb") as handle:
            handle.seek(4 + int(gene) * INDEX_DTYPE.itemsize)
            entry = np.frombuffer(handle.read(INDEX_DTYPE.itemsize),
                                  dtype=INDEX_DTYPE)
            if not len(entry) or not entry[0]["count"]:
                return np.empty(0, dtype=dtype)
            rows = int(np.frombuffer(_peek(handle, 0, 4), dtype="<u4")[0])
            start = 4 + rows * INDEX_DTYPE.itemsize
            handle.seek(start + int(entry[0]["offset"]) * dtype.itemsize)
            return np.fromfile(handle, dtype=dtype, count=int(entry[0]["count"]))
    except FileNotFoundError:
        return np.empty(0, dtype=dtype)


def _peek(handle, offset, size):
    here = handle.tell()
    handle.seek(offset)
    data = handle.read(size)
    handle.seek(here)
    return data


def _stored_pooling(manifest, pooling):
    stored = [p for p in manifest.get("store_poolings") or [1] if p <= pooling]
    return max(stored) if stored else 1


def _tile_grid(manifest, level, tx, ty, pooling):
    tile_size = int(manifest.get("tile_size") or DEFAULT_TILE_SIZE)
    supersample = int(manifest.get("supersample") or DEFAULT_SUPERSAMPLE)
    return transcript_tiles.grid_for(tile_size, int(level), int(tx), int(ty),
                                     bin_pixels=pooling * supersample)


def pooled_fields(datasource_name, layer_id, manifest, level, tx, ty, *,
                  genes, pooling):
    """`{gene: (ny, nx) float32}` of summed counts, and the grid they are on.

    `genes` are store rows. Squares are `pooling` bins wide, anchored to the
    grid origin, so a square is the same square in every tile and at every
    level that draws it.
    """
    grid = _tile_grid(manifest, level, tx, ty, pooling)
    stored = _stored_pooling(manifest, pooling)
    ratio = pooling // stored
    side = _side(manifest, stored)
    units_x = -(-int(manifest["columns"]) // stored)
    units_y = -(-int(manifest["rows"]) // stored)
    ux0, uy0 = grid.first_x * ratio, grid.first_y * ratio
    ux1 = min((grid.first_x + grid.nx) * ratio, units_x)
    uy1 = min((grid.first_y + grid.ny) * ratio, units_y)
    fields = {}
    for gene in genes:
        field = np.zeros(grid.ny * grid.nx, dtype=np.float64)
        if ux1 > ux0 and uy1 > uy0:
            for sy in range(uy0 // side, (uy1 - 1) // side + 1):
                for sx in range(ux0 // side, (ux1 - 1) // side + 1):
                    run = read_run(datasource_name, layer_id, stored, sx, sy,
                                   gene)
                    if not len(run):
                        continue
                    ux = sx * side + run["dx"].astype(np.int64)
                    uy = sy * side + run["dy"].astype(np.int64)
                    px = ux // ratio - grid.first_x
                    py = uy // ratio - grid.first_y
                    inside = ((px >= 0) & (px < grid.nx)
                              & (py >= 0) & (py < grid.ny))
                    if not inside.any():
                        continue
                    field += np.bincount(
                        py[inside] * grid.nx + px[inside],
                        weights=run["count"][inside],
                        minlength=grid.ny * grid.nx)
        fields[gene] = field.reshape(grid.ny, grid.nx).astype(np.float32)
    return fields, grid


def tissue_field(datasource_name, layer_id, manifest, grid, pooling):
    """`(ny, nx)` bool: which squares of this tile hold a measured bin."""
    stored = _stored_pooling(manifest, pooling)
    ratio = pooling // stored
    mask = read_tissue(datasource_name, layer_id, stored)
    out = np.zeros((grid.ny * ratio, grid.nx * ratio), dtype=np.uint8)
    if mask is None:
        return out.reshape(grid.ny, ratio, grid.nx, ratio).max((1, 3)) > 0
    uy0, ux0 = grid.first_y * ratio, grid.first_x * ratio
    uy1 = min(uy0 + grid.ny * ratio, mask.shape[0])
    ux1 = min(ux0 + grid.nx * ratio, mask.shape[1])
    if uy1 > uy0 and ux1 > ux0:
        out[:uy1 - uy0, :ux1 - ux0] = mask[uy0:uy1, ux0:ux1]
    return out.reshape(grid.ny, ratio, grid.nx, ratio).max((1, 3)) > 0


def auto_window(manifest, stats, gene, pooling):
    """The count at which one gene saturates, at this pooling.

    The 99th percentile of that gene's non-empty squares, measured at the
    pooling asked for -- so a rare gene and an abundant one each fill the
    range. The tiles pass the REQUESTED square size here, not the one a
    zoomed-out level happens to draw (see `_scaled_fields`): a window that
    followed the merged squares would repaint the same tissue in another
    colour at every zoom. Floor of one count.
    """
    poolings = list(manifest.get("hist_poolings") or [1])
    if stats is None or gene >= len(stats):
        return 1.0
    if pooling in poolings:
        value = float(stats[gene, poolings.index(pooling), 2])
    else:
        top = poolings[-1]
        value = float(stats[gene, len(poolings) - 1, 2]) * (pooling / top) ** 2
    return max(1.0, value)


def stretch(field, low, high, log=False):
    """Counts to 0..1 through `[low, high]`, linearly or through log1p."""
    lo, hi = float(low), max(float(high), float(low) + 1e-6)
    shifted = np.clip(field - lo, 0, None)
    if log:
        return np.clip(np.log1p(shifted) / np.log1p(hi - lo), 0, 1)
    return np.clip(shifted / (hi - lo), 0, 1)


#: What the gutter costs a tile on average: a strip `DENSITY_GUTTER` wide on
#: two edges of every square. Applied uniformly once a square is one pixel and
#: there is no room left for a strip, so the coarsest levels carry the same ink
#: as the finest.
GUTTER_MEAN_ALPHA = (1.0 - transcript_tiles.DENSITY_GUTTER) ** 2


def _alpha_grid(tile_size, grid):
    """Per-pixel alpha multiplier for the gutter between squares, 0..1.

    THE SAME INK AT EVERY ZOOM. The strip is strictly proportional to the
    square (no visibility floor, unlike the transcript density map), and a
    one-pixel square pays the strip's average cost across the whole tile, so
    a tile's mean alpha is about `GUTTER_MEAN_ALPHA` at every level -- which
    is what keeps a ramp colour over the H&E the same colour on zoom.
    """
    alpha = np.ones((tile_size, tile_size), dtype=np.float32)
    if transcript_tiles.DENSITY_GUTTER > 0 and grid.size <= grid.step:
        alpha *= GUTTER_MEAN_ALPHA
        return alpha
    gap = transcript_tiles._gutter(tile_size, grid, min_coverage=0.0)
    if gap is not None:
        columns, rows, coverage = gap
        alpha[rows, :] *= 1.0 - coverage
        alpha[:, columns] *= 1.0 - coverage
    return alpha


#: How several genes become the one field a ramp reads, and the default.
#: MEAN first: a heatmap of three genes should read on the same count scale
#: as a heatmap of one, which a sum does not -- its numbers triple.
AGGREGATIONS = ("mean", "sum", "max", "min")
DEFAULT_AGGREGATION = "mean"


def aggregation(name):
    """A known aggregation name, or the default for anything else."""
    name = str(name or "").strip().lower()
    return name if name in AGGREGATIONS else DEFAULT_AGGREGATION


def aggregate(values, how):
    """Several same-shaped arrays (or numbers) combined per element.

    Raw counts in, raw counts out -- the window is applied afterwards -- so
    "max" is the most abundant of the genes in that square and "min" is zero
    wherever any one of them is absent, which is what makes it the
    co-expression view.
    """
    values = [np.asarray(v, dtype=np.float32) for v in values]
    if not values:
        return np.float32(0.0)
    how = aggregation(how)
    if how == "sum":
        return np.sum(values, axis=0)
    if how == "max":
        return np.max(values, axis=0)
    if how == "min":
        return np.min(values, axis=0)
    return np.mean(values, axis=0)


def _window(manifest, stats, genes, pooling, low, high, how="sum"):
    """The window of the aggregated field, in counts.

    The genes' own automatic windows, aggregated the way their counts are --
    so a mean of three genes saturates at the mean of their three windows and
    a sum at their sum. That is what keeps each aggregation filling the ramp
    rather than whiting out (a sum against one gene's window) or going dark
    (a min against the largest).
    """
    top = float(aggregate([auto_window(manifest, stats, g, pooling)
                           for g in genes], how)) or 1.0
    return top * float(low or 0.0), top * (1.0 if high is None else float(high))


def rgb_tile(datasource_name, layer_id, manifest, stats, level, tx, ty, *,
             groups, pooling, low=0.0, high=1.0, log=False,
             scale_pooling=None):
    """Several genes in their own colours, as one RGBA tile drawn source-over.

    Each gene is stretched against its own window, so a rare gene beside an
    abundant one is still visible. Colour is the level-weighted mix of the
    genes' colours at full strength and ALPHA is the strongest gene's level --
    a square with none of the selection is transparent and the H&E shows
    through it, and a square with one gene is that gene's colour at the
    opacity its count earns. (Additive `lighter`, which the transcript
    composite uses over fluorescence, washes to white over a bright H&E.)

    @param groups - `[(store row, (r, g, b))]`
    @param scale_pooling - the square size the colour scale is measured at
        (the one asked for); None measures it at `pooling`.
    """
    tile_size = int(manifest.get("tile_size") or DEFAULT_TILE_SIZE)
    rows = [row for row, _ in groups]
    fields, grid = pooled_fields(datasource_name, layer_id, manifest, level,
                                 tx, ty, genes=rows, pooling=pooling)
    fields = _scaled_fields(fields, pooling, scale_pooling)
    scale = scale_pooling or pooling
    colour = np.zeros((grid.ny, grid.nx, 3), dtype=np.float32)
    strongest = np.zeros((grid.ny, grid.nx), dtype=np.float32)
    for row, rgb in groups:
        lo, hi = _window(manifest, stats, [row], scale, low, high)
        level_ = stretch(fields[row], lo, hi, log).astype(np.float32)
        colour += level_[..., None] * np.asarray(rgb, dtype=np.float32)
        strongest = np.maximum(strongest, level_)
    colour = np.clip(colour / np.maximum(strongest, 1e-6)[..., None], 0, 255)
    alpha = strongest * tissue_field(datasource_name, layer_id, manifest, grid,
                                     pooling)
    return _assemble(colour, alpha, tile_size, grid)


def ramp_tile(datasource_name, layer_id, manifest, stats, level, tx, ty, *,
              genes, ramp, pooling, low=0.0, high=1.0, log=False,
              how=DEFAULT_AGGREGATION, scale_pooling=None):
    """The selection aggregated into one field and read off a colour ramp.

    `how` is one of AGGREGATIONS; it only matters with more than one gene.
    Every measured square is painted, zero included -- zero is a value on a
    heat map, not a hole -- and nothing is painted off the tissue, so the
    array's empty margin never hides the image under it.

    `scale_pooling` is the square size the colour scale is measured at -- the
    one asked for -- so a coarser level draws the mean of those squares
    against their window and a region keeps its colour on zoom. None
    measures it at `pooling`.
    """
    tile_size = int(manifest.get("tile_size") or DEFAULT_TILE_SIZE)
    fields, grid = pooled_fields(datasource_name, layer_id, manifest, level,
                                 tx, ty, genes=genes, pooling=pooling)
    fields = _scaled_fields(fields, pooling, scale_pooling)
    total = (aggregate(list(fields.values()), how).astype(np.float32)
             if fields else np.zeros((grid.ny, grid.nx), dtype=np.float32))
    lo, hi = _window(manifest, stats, genes, scale_pooling or pooling, low,
                     high, how)
    level8 = np.rint(stretch(total, lo, hi, log) * 255).astype(np.uint8)
    colour = np.asarray(ramp, dtype=np.uint8)[level8].astype(np.float32)
    alpha = tissue_field(datasource_name, layer_id, manifest, grid,
                         pooling).astype(np.float32)
    return _assemble(colour, alpha, tile_size, grid)


#: Below this many tile pixels per square a treemap's cells would be a pixel
#: or less, so each square is filled with its dominant gene's colour instead.
COMPOSITION_MIN_GLYPH_PX = 4


def parse_components(text, n_genes):
    """A `comp=` string as `[((gene position, ...), how), ...]`.

    Components are comma-separated. A standalone gene is one index into
    `genes=`; a group is its members' indices joined by `|` and followed by
    `:how` -- `0|1|2:mean,3` is a three-gene group combined by mean, then one
    gene. A one-member group is still written `2:mean`.

    Strict about structure -- an empty component, a non-integer, an index
    out of range or used twice is a ValueError -- and lenient about the
    aggregation word, which falls back to the default exactly as `agg=` does.
    """
    components, seen = [], set()
    for part in str(text or "").split(","):
        body, colon, how = part.partition(":")
        if not body.strip():
            raise ValueError(f"empty component in {text!r}")
        members = []
        for token in body.split("|"):
            token = token.strip()
            if not token.isdigit():
                raise ValueError(f"not a gene index: {token!r}")
            index = int(token)
            if index >= int(n_genes):
                raise ValueError(f"gene index {index} out of range")
            if index in seen:
                raise ValueError(f"gene index {index} used twice")
            seen.add(index)
            members.append(index)
        if colon:
            components.append((tuple(members), aggregation(how)))
        elif len(members) == 1:
            components.append((tuple(members), None))
        else:
            raise ValueError(f"group {body!r} has no aggregation")
    return components


def _squarify(values, x, y, w, h):
    """A squarified treemap of `values` in the rectangle `(x, y, w, h)`.

    `values` is `(K, ...)` -- K items in every square at once -- and the
    rectangle is per square too. Returns `(x0, x1, y0, y1)`, each `(K, ...)`,
    in the items' ORIGINAL order; an item worth nothing gets an empty
    rectangle.

    Bruls, Huizing & van Wijk's layout, the one every treemap draws: items
    largest first, added to a row along the rectangle's shorter side for as
    long as that makes the row's worst aspect ratio no worse, then the row is
    laid down and the rest fill what is left. Vectorised over the squares --
    the greedy choice differs from square to square, so every step is a mask
    rather than a branch, and K is a handful, so the loops are short. Ties
    keep the given order (a stable sort).
    """
    values = np.asarray(values, dtype=np.float64)
    count = values.shape[0]
    shape = values.shape[1:]
    x0, x1, y0, y1 = (np.zeros(values.shape) for _ in range(4))
    if not count:
        return x0, x1, y0, y1
    rx, ry = (np.broadcast_to(v, shape).astype(np.float64) for v in (x, y))
    rw, rh = (np.broadcast_to(v, shape).astype(np.float64) for v in (w, h))
    total = values.sum(0)
    area = np.divide(values * (rw * rh), total, out=np.zeros_like(values),
                     where=total > 0)
    order = np.argsort(-area, axis=0, kind="stable")
    ranked = np.take_along_axis(area, order, axis=0)
    start = np.zeros(shape, dtype=np.int64)      # first item of the open row
    row_sum = np.zeros(shape)
    row_min = np.full(shape, np.inf)
    row_max = np.zeros(shape)

    def worst(total_, low, high, side):
        side2 = side * side
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.maximum(side2 * high / (total_ * total_),
                              total_ * total_ / (side2 * low))

    def flush(mask, stop):
        """Lay the open row (items `start..stop-1`) down where `mask`."""
        nonlocal rx, ry, rw, rh
        wide = rw >= rh
        side = np.where(wide, rh, rw)
        thick = np.divide(row_sum, side, out=np.zeros_like(row_sum),
                          where=side > 0)
        offset = np.zeros(shape)
        for j in range(count):
            member = mask & (j >= start) & (j < stop)
            if not member.any():
                continue
            a = ranked[j]
            length = np.divide(a, thick, out=np.zeros_like(a), where=thick > 0)
            # A row down the left of a wide rectangle, stacked top to
            # bottom; across the top of a tall one, left to right.
            ix0 = np.where(wide, rx, rx + offset)
            ix1 = np.where(wide, rx + thick, rx + offset + length)
            iy0 = np.where(wide, ry + offset, ry)
            iy1 = np.where(wide, ry + offset + length, ry + thick)
            item = order[j]
            for target, value in ((x0, ix0), (x1, ix1), (y0, iy0), (y1, iy1)):
                current = np.take_along_axis(target, item[None], axis=0)[0]
                np.put_along_axis(target, item[None],
                                  np.where(member, value, current)[None],
                                  axis=0)
            offset = np.where(member, offset + length, offset)
        rx = np.where(mask & wide, rx + thick, rx)
        rw = np.where(mask & wide, rw - thick, rw)
        ry = np.where(mask & ~wide, ry + thick, ry)
        rh = np.where(mask & ~wide, rh - thick, rh)

    for i in range(count):
        a = ranked[i]
        live = a > 0
        side = np.minimum(rw, rh)
        open_ = row_sum > 0
        grows = worst(row_sum + a, np.minimum(row_min, a),
                      np.maximum(row_max, a), side) \
            <= worst(row_sum, row_min, row_max, side)
        close = live & open_ & ~grows
        flush(close, i)
        start = np.where(close, i, start)
        row_sum = np.where(close, 0.0, row_sum)
        row_min = np.where(close, np.inf, row_min)
        row_max = np.where(close, 0.0, row_max)
        row_sum = np.where(live, row_sum + a, row_sum)
        row_min = np.where(live, np.minimum(row_min, a), row_min)
        row_max = np.where(live, np.maximum(row_max, a), row_max)
    flush(row_sum > 0, count)
    return x0, x1, y0, y1


def composition_shares(fields, components):
    """Each leaf gene's rectangle in every square's unit glyph: a treemap.

    `fields` is `[(ny, nx) counts]` by gene position; `components` is
    `[(positions, how or None)]` with `how` None for a standalone gene.

    OUTER LEVEL: the components are a squarified treemap of the unit square
    (`_squarify`), each as large as its share of the square's selected
    signal. A standalone gene's value is its count; a group's is its
    members combined per square by `how` (mean, sum, max or min).

    INNER LEVEL: a group's rectangle is itself a squarified treemap of its
    members, in proportion to their COUNTS whatever `how` is. The rule
    decides how much area the group earns; the split shows who is inside
    it. So under `min` a group with any member absent earns nothing in that
    square, and under `max` its area is its strongest member's count but its
    colours are all of its members'. A group's members are therefore always
    one rectangle together, as in any nested treemap.

    Returns `(leaf positions, share, total, x0, x1, y0, y1)`: `share` and the
    edges are `(N, ny, nx)` in the unit square, half-open, and they tile
    every square whose total is positive. Squares with no selected signal
    have every share zero.
    """
    outer = []
    for positions, how in components:
        members = [fields[p] for p in positions]
        outer.append(members[0] if how is None
                     else aggregate(members, how))
    outer = np.asarray(outer, dtype=np.float64)
    total = outer.sum(0)
    fraction = np.divide(outer, total, out=np.zeros_like(outer),
                         where=total > 0)
    box = _squarify(outer, 0.0, 0.0, 1.0, 1.0)
    leaves, share, x0, x1, y0, y1 = [], [], [], [], [], []
    for k, (positions, how) in enumerate(components):
        counts = np.asarray([fields[p] for p in positions], dtype=np.float64)
        members = counts.sum(0)
        inner = np.divide(counts, members, out=np.zeros_like(counts),
                          where=members > 0)
        bx0, bx1, by0, by1 = (edge[k] for edge in box)
        cells = _squarify(counts, bx0, by0, bx1 - bx0, by1 - by0)
        for j, position in enumerate(positions):
            leaves.append(position)
            share.append(fraction[k] * inner[j])
            x0.append(cells[0][j])
            x1.append(cells[1][j])
            y0.append(cells[2][j])
            y1.append(cells[3][j])
    stack = lambda parts: np.asarray(parts, dtype=np.float64)    # noqa: E731
    return (leaves, stack(share), total, stack(x0), stack(x1), stack(y0),
            stack(y1))


def _paint_glyphs(leaf_rgb, x0, x1, y0, y1, tile_size, grid):
    """Every square's glyph at tile resolution, `(T, T, 3)` uint8.

    Each tile pixel is placed by its CENTRE in its square's unit glyph and
    takes the colour of the one leaf rectangle holding it. The rectangles
    are half-open and partition the square, so every pixel is exactly one
    gene's colour -- no blending, no anti-aliasing.
    """
    columns, rows = transcript_tiles._block_index(grid, tile_size)
    pixels = np.arange(int(tile_size), dtype=np.float64) * grid.step
    u = (((grid.origin_x + pixels) % grid.size) + 0.5 * grid.step) / grid.size
    v = (((grid.origin_y + pixels) % grid.size) + 0.5 * grid.step) / grid.size
    uu, vv = u[None, :], v[:, None]
    index = np.ix_(rows, columns)
    # Under everything, each square's largest leaf: a pixel centre that falls
    # in a floating-point sliver between two rectangles still gets a gene's
    # colour, never black.
    rgb = np.asarray(leaf_rgb, dtype=np.uint8)[
        np.argmax((x1 - x0) * (y1 - y0), axis=0)][index]
    for j, colour in enumerate(leaf_rgb):
        hit = ((uu >= x0[j][index]) & (uu < x1[j][index])
               & (vv >= y0[j][index]) & (vv < y1[j][index]))
        rgb[hit] = colour
    return rgb


def composition_tile(datasource_name, layer_id, manifest, level, tx, ty, *,
                     genes, colours, components, pooling):
    """Each square as a treemap of its selected genes' shares, RGBA.

    `genes` are store rows by position, `colours` their `(r, g, b)` by the
    same position, `components` as `parse_components` returns them.

    Shares are ratios of raw counts, so they need no window and no scaling:
    a merged square's shares are its sub-squares' summed counts, and the
    picture means the same at every zoom. Squares with no selected signal
    are transparent. Once a square is under `COMPOSITION_MIN_GLYPH_PX` tile
    pixels the glyph cannot be read, and the square takes the colour of its
    largest single-gene region (ties to the first in component order).
    """
    tile_size = int(manifest.get("tile_size") or DEFAULT_TILE_SIZE)
    if not components:
        return np.zeros((tile_size, tile_size, 4), dtype=np.uint8)
    unique = sorted(set(genes))
    by_row, grid = pooled_fields(datasource_name, layer_id, manifest, level,
                                 tx, ty, genes=unique, pooling=pooling)
    fields = [by_row[row] for row in genes]
    leaves, share, total, x0, x1, y0, y1 = composition_shares(fields,
                                                              components)
    leaf_rgb = np.asarray([colours[p] for p in leaves], dtype=np.uint8)
    alpha = (total > 0).astype(np.float32)
    if grid.size // grid.step >= COMPOSITION_MIN_GLYPH_PX:
        rgb = _paint_glyphs(leaf_rgb, x0, x1, y0, y1, tile_size, grid)
        return _assemble(rgb, alpha, tile_size, grid, pixels=True)
    colour = leaf_rgb[share.argmax(0)].astype(np.float32)
    return _assemble(colour, alpha, tile_size, grid)


def _assemble(colour, alpha, tile_size, grid, *, pixels=False):
    """Square fields to a `(T, T, 4)` uint8 tile, one block per square.

    `pixels` means `colour` is already at tile resolution (a glyph), so only
    the alpha is expanded from squares.
    """
    rgb = (np.asarray(colour, dtype=np.uint8) if pixels else
           transcript_tiles._to_tile(np.rint(colour).astype(np.uint8),
                                     tile_size, grid))
    a = transcript_tiles._to_tile(np.rint(alpha * 255).astype(np.uint8),
                                  tile_size, grid).astype(np.float32)
    a *= _alpha_grid(tile_size, grid)
    return np.dstack((rgb, np.rint(a).astype(np.uint8)))


def square_at(datasource_name, layer_id, manifest, column, row, genes,
              pooling=1):
    """Counts of these genes in the square holding bin `(column, row)`."""
    pooling = max(1, int(pooling))
    stored = _stored_pooling(manifest, pooling)
    ratio = pooling // stored
    side = _side(manifest, stored)
    first_x, first_y = (int(column) // pooling) * ratio, (int(row) // pooling) * ratio
    out = {}
    for gene in genes:
        total = 0
        sx0, sx1 = first_x // side, (first_x + ratio - 1) // side
        sy0, sy1 = first_y // side, (first_y + ratio - 1) // side
        for sy in range(sy0, sy1 + 1):
            for sx in range(sx0, sx1 + 1):
                run = read_run(datasource_name, layer_id, stored, sx, sy, gene)
                if not len(run):
                    continue
                ux = sx * side + run["dx"].astype(np.int64)
                uy = sy * side + run["dy"].astype(np.int64)
                hit = ((ux >= first_x) & (ux < first_x + ratio)
                       & (uy >= first_y) & (uy < first_y + ratio))
                total += int(run["count"][hit].sum())
        out[gene] = total
    return out
