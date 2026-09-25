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

**Coarse levels are AGGREGATES, not subsamples and not a different picture.**
Zooming out used to hand the view over to `density_tile`, and that was wrong
in the one way a level of detail must not be: it changed what the user was
looking at. Points mode now stays points at every zoom -- `aggregate_tile`
merges the molecules in a bin, per gene, into one record carrying the count
and the centroid of what it merged, and the client draws it as a dot whose
AREA is that count (so the radius goes as its square root). A bin is one
64th of a tile side at every level, so it doubles in image pixels each time
the level coarsens and stays about the same size on screen: the client picks
the level that makes it eight screen pixels, which is what keeps aggregated
molecules far enough apart to read as separate dots.

Per GENE, and that is the part that cannot be traded away. Merging two genes
into one dot would make the colour a lie, and the colour is the whole reason
somebody picked those genes. It also costs nothing structurally: the records
keep the same gene-major layout with the same header, so a ten-gene selection
out of a five-hundred-gene panel is still ten short reads.

Computed per request rather than written down. An aggregate is a function of
the gene selection and of the quality threshold, both of which are controls
the user turns -- so a precomputed pyramid would have to be one pyramid per
selection, and the only selection-independent version of it (every gene, at
every level) is a record per molecule per level, because a 480-gene panel
almost never puts two molecules of the SAME gene in one bin until the bins
are very coarse. What the pyramid would have bought is bought instead by the
tile addressing: a level-L tile covers 2^L tiles of the level-0 cache, so a
screenful is always about six requests however far out the view is, and each
one is immutable for its (level, tile, genes, threshold) and carries an ETag
that lets the browser keep it.

Density is still its own mode, reached by asking for it -- see `density_tile`
and `density_ramp_tile`. It answers a different question ("how much is here")
with a different encoding, and nothing switches to it on the user's behalf.

    file layout, per tile
      u4   n_genes
      n_genes x (u2 gene, u4 offset, u4 count)      -- sorted by gene
      body: POINT_DTYPE records, gene-major, each gene's run contiguous

**The quality score travels with the point.** Xenium scores every call, and
the vendor's own guidance is to look at Q20 and above -- but which threshold
is right depends on the gene and on the question, so it is a control the user
turns, not a decision the build makes. One byte per record buys that: the
cache holds everything the file held, the client discards below the threshold
in its vertex shader, and moving the slider redraws without refetching a
single tile. Filtering at build time would have meant rebuilding a 19-million
-row cache to answer "what does this look like at Q15".
"""

from __future__ import annotations

import collections
import json
import os
import shutil
import threading
from pathlib import Path

import numpy as np

from plexora import paths

#: Bumped when the RECORD LAYOUT or the manifest's contract changes. A cache
#: written by an older version must never be decoded by a newer one: the stride
#: is different, so every field would be read out of the middle of the one
#: before it and the result is points scattered at random over the slide. The
#: check is in `is_current`, and a stale cache is reported as `stale` by the
#: plugin's `/manifest` rather than silently served.
CACHE_VERSION = 2
DEFAULT_TILE_SIZE = 512

#: 11 bytes, packed. `gene` is an index into the manifest's vocabulary, not a
#: string: a 300-gene panel needs 9 bits, and repeating "PTPRC" 50 million times
#: is 300 MB of the same six characters. `q` is the call's quality score,
#: rounded and clamped into a byte -- Xenium's runs 0 to 40, and the fraction
#: of a point that the extra precision would buy is not worth 3 more bytes on
#: 19 million records.
POINT_DTYPE = np.dtype([
    ("gene", "<u2"),
    ("x", "<f4"),
    ("y", "<f4"),
    ("q", "u1"),
])

#: What `record_dtype` says in the manifest, so a cache carries a description
#: of its own layout rather than only a number that has to be looked up.
RECORD_DTYPE_NAME = "gene:uint16,x:float32,y:float32,q:uint8"

#: One AGGREGATED point: a bin's worth of one gene, at the centroid of the
#: molecules it stands for. 14 bytes.
#:
#: No `q`, and that is the one real difference from POINT_DTYPE. A raw point
#: carries its score because the client filters in the shader, which is what
#: makes the Q slider free; an aggregate's count is a SUM OVER that filter, so
#: the threshold has to be applied before the merge and the answer is a
#: function of it. Aggregates are therefore fetched per threshold, and the
#: score itself has nothing left to say once the merge has happened.
AGGREGATE_DTYPE = np.dtype([
    ("gene", "<u2"),
    ("x", "<f4"),
    ("y", "<f4"),
    ("count", "<u4"),
])

AGGREGATE_DTYPE_NAME = "gene:uint16,x:float32,y:float32,count:uint32"

#: Aggregation bins across one tile, in each axis.
#:
#: A level-L tile covers `tile_size * 2**L` image pixels, so a bin is
#: `tile_size / AGGREGATE_BINS * 2**L` of them -- eleven and a bit at level 0
#: for the usual 512-pixel tile, doubling with the level. The client reads
#: this out of the manifest and picks the level that makes a bin a few SCREEN
#: pixels wide, so the number is part of the contract between the two and not
#: a private constant here.
#:
#: THIS IS THE CONTINUOUS CONTROL OVER HOW DENSE THE ZOOMED-OUT OVERLAY IS,
#: and the only one. The client's level is a quadtree step, so nothing it can
#: do changes the dots on screen by less than a factor of four: asking the
#: level rule for half as many dots gave 69% fewer at one zoom and none at
#: the next, because whether a view crosses a band edge is an accident of
#: where it happens to sit. A bin that is root-two wider halves the dots at
#: EVERY zoom, exactly, and leaves the level the same one it was.
#:
#: 45 AND NOT 64, WHICH IS 64/sqrt(2) ROUNDED -- half the dots per unit area,
#: which is what the whole-slide overlay was asked for. Not round, and it
#: does not need to be: bins are laid out by a float `scale` over the tile's
#: span (`_aggregate_chunk`), nothing indexes them by shifting, and the grid
#: is defined relative to each level-L tile's own origin so neighbouring
#: tiles still line up. The only thing that wants it small is the
#: accumulator, which is `genes * bins * bins` cells.
#:
#: COSTS NO REBUILD. Aggregates are computed per request from the level-0
#: molecules rather than stored (`aggregate_tile`), and `/manifest` reports
#: this constant live, so changing it changes the picture on the next tile
#: fetch. What it does invalidate is tiles a BROWSER already holds under a
#: year-long `Cache-Control`, which is why it rides the aggregate ETag.
AGGREGATE_BINS = 45

#: Bumped when an aggregate's CONTENTS change but the cache it is computed
#: from does not.
#:
#: Aggregates are derived per request from the level-0 molecules, so nothing
#: about them is written to disk and `source_mtime_ns` -- which is what the
#: rest of the ETag is built on -- cannot see a change to how they are
#: derived. A browser holds these tiles under a year-long `max-age`, so
#: without this a retune of the binning or a fix to which molecule stands for
#: a bin reaches only people who have never looked at the layer before.
#:
#: 2: the bin grid moved to 45 and `_priority` was given a hash that
#: avalanches, which moves every aggregate dot there is.
AGGREGATE_REVISION = 2

#: How many points are buffered before they are folded into the accumulator.
#:
#: A fold is a `bincount` over the whole bin-by-gene grid plus a sort of the
#: buffer, so doing it once per source tile would pay for zeroing that grid a
#: thousand times over on a whole-slide request. Buffering instead makes it a
#: handful of folds, at 16 bytes a point of peak memory -- and in practice
#: one fold, since a selection worth drawing is well under this.
AGGREGATE_FLUSH = 4_000_000

#: The largest bin-by-gene grid one pass will allocate. Past it the genes are
#: done in chunks -- which costs re-reading the source tiles, and only ever
#: happens for a selection far larger than anything worth drawing at once.
AGGREGATE_MAX_CELLS = 2_000_000

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
    return (paths.derived_root(datasource_name) / f"transcripts_v{CACHE_VERSION}"
            / _safe(layer_id))


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
    """Whether this cache can be READ, not merely whether it is fresh.

    `record_dtype` is in the list for a reason that is not like the others: a
    cache written at a 10-byte stride and decoded at 11 does not come back
    wrong in one field, it comes back as noise -- every point somewhere else
    on the slide, plausibly distributed, with nothing on screen to say so. The
    version number alone would have been enough if nothing else ever wrote one
    of these, and the dtype string is the belt to its braces.
    """
    keys = ("version", "record_dtype", "source", "source_size",
            "source_mtime_ns", "width", "height", "tile_size", "level_count")
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
        "record_dtype": RECORD_DTYPE_NAME,
    }


# -- building ---------------------------------------------------------------

def _sweep_old_versions(root):
    """Delete caches written by a different CACHE_VERSION.

    A version bump changes where the cache lives, so the previous one is left
    sitting in the project directory -- 183 MB, for the Xenium run this was
    first measured on. Nothing will ever read it again: `cache_dir` names the
    current version and the old directory is unreachable by construction.
    """
    parent = Path(root).parent.parent
    current = f"transcripts_v{CACHE_VERSION}"
    if not parent.is_dir():
        return
    for sibling in parent.glob("transcripts_v*"):
        if sibling.name != current and sibling.is_dir():
            shutil.rmtree(sibling, ignore_errors=True)


def build(datasource_name, layer_id, *, genes, gene_index, x, y, expected,
          q=None, progress=None):
    """Write the tile cache for one layer.

    Takes arrays rather than a path because the reading is the adapter's job and
    the tiling is this module's -- which is also what lets the tests build a
    cache without a parquet file in sight.

    @param genes       - the vocabulary, in index order
    @param gene_index  - uint16 per point, indexing `genes`
    @param x / y       - float32 per point, in REFERENCE PIXELS. The conversion
                         from a vendor's microns is the adapter's, done once,
                         because it needs the pixel size and this does not.
    @param q           - the per-point quality score, 0-255, or None for a
                         file that states none. None is written as 255, which
                         is "keeps whatever threshold you set": a reader with
                         no quality column should not have every point vanish
                         the first time somebody moves the slider.
    @param progress    - optional callable(done, total) for the staged job
    """
    if len(genes) > MAX_GENES:
        raise ValueError(
            f"{len(genes)} genes is past the {MAX_GENES} a uint16 index can hold")

    root = cache_dir(datasource_name, layer_id)
    # Under the lock the readers take, so a `/points` request arriving while
    # the directory is being replaced reads one state or the other and never
    # half of each. The lock existed and was never taken, which is the kind of
    # bug that only shows up on the machine you cannot reproduce it on.
    with _lock_for((datasource_name, layer_id)):
        return _build_locked(datasource_name, layer_id, root, genes=genes,
                             gene_index=gene_index, x=x, y=y, q=q,
                             expected=expected, progress=progress)


def _build_locked(datasource_name, layer_id, root, *, genes, gene_index,
                  x, y, q, expected, progress):
    _sweep_old_versions(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    tmp_root = root.with_name(f"{root.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)

    gene_index = np.asarray(gene_index, dtype=np.uint16)
    x = np.asarray(x, dtype=np.float32)
    y = np.asarray(y, dtype=np.float32)
    quality = (np.full(len(x), 255, dtype=np.uint8) if q is None
               else np.asarray(q, dtype=np.uint8))
    finite = np.isfinite(x) & np.isfinite(y)
    if not finite.all():
        gene_index, x, y = gene_index[finite], x[finite], y[finite]
        quality = quality[finite]

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
                    gene_index[run], x[run], y[run], quality[run])
        if progress and (done % 64 == 0 or done == total):
            progress(done, total)

    manifest = {
        **expected,
        "genes": list(genes),
        "gene_count": len(genes),
        "point_count": int(len(x)),
        "columns": columns,
        "rows": rows,
        # Per-tile counts, for the client's level-of-detail estimate. A few
        # thousand small integers, and it is what lets the viewer work out
        # how coarsely to merge from the manifest alone rather than by
        # fetching a tile to find out how many points are in it.
        "tile_counts": counts.tolist(),
        # How many molecules of each gene, in vocabulary order. What the gene
        # list shows beside each name -- "EPCAM 412,003" is the single most
        # useful thing to know about a gene before you turn it on -- and what
        # the level-of-detail estimate scales a selection by, so selecting
        # one rare gene out of 480 is drawn molecule by molecule rather than
        # merged because the whole panel is dense.
        "gene_counts": np.bincount(
            gene_index, minlength=len(genes)).astype(np.int64).tolist(),
        # Said out loud, because the one thing that has ever been wrong about
        # this cache is what frame it is in.
        "units": "reference_pixels",
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


def _write_tile(path, genes, xs, ys, qs):
    """One tile: a gene index, then the points, gene-major."""
    order = np.argsort(genes, kind="stable")
    genes, xs, ys, qs = genes[order], xs[order], ys[order], qs[order]
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
    body["q"] = qs

    with open(path, "wb") as handle:
        handle.write(np.uint32(len(unique)).tobytes())
        handle.write(index.tobytes())
        handle.write(body.tobytes())


# -- reading ----------------------------------------------------------------

def read_tile(datasource_name, layer_id, tile_x, tile_y, genes=None, level=0,
              min_q=None):
    """The points in one tile, optionally only for some genes.

    With `genes`, this is a seek per gene rather than a scan of the tile: the
    header says where each run starts, so a 10-gene selection out of a 300-gene
    panel reads 10 short ranges. That is the whole reason for the header.

    `min_q` drops calls below a quality score. Applied here only for the
    DENSITY path -- the point path sends the score to the client and discards
    in the vertex shader, so moving the threshold slider redraws without
    refetching a tile.

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
            return _above(np.frombuffer(handle.read(), dtype=POINT_DTYPE), min_q)

        wanted = np.asarray(sorted(set(int(g) for g in genes)), dtype=np.uint16)
        runs = index[np.isin(index["gene"], wanted)]
        if not len(runs):
            return np.empty(0, dtype=POINT_DTYPE)
        parts = []
        for run in runs:
            handle.seek(body_start + int(run["offset"]) * POINT_DTYPE.itemsize)
            raw = handle.read(int(run["count"]) * POINT_DTYPE.itemsize)
            parts.append(np.frombuffer(raw, dtype=POINT_DTYPE))
        found = np.concatenate(parts) if len(parts) > 1 else parts[0]
        return _above(found, min_q)


def _above(records, min_q):
    """`records` at or above a quality score, or all of them when None."""
    if min_q is None or not len(records):
        return records
    return records[records["q"] >= int(min_q)]


def read_region(datasource_name, layer_id, bounds, genes=None, max_points=None,
                min_q=None):
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
            part = read_tile(datasource_name, layer_id, tx, ty, genes,
                             min_q=min_q)
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


# -- aggregated points -------------------------------------------------------

def aggregate_levels(width, height, tile_size=DEFAULT_TILE_SIZE):
    """How many levels it takes to get this layer down to a single tile.

    The same ladder the density pyramid climbs, so "level 3" means one thing
    across the whole plugin: a level-L tile covers `tile_size * 2**L` image
    pixels, and the coarsest level is the first at which one tile covers
    everything.
    """
    tile = max(1, int(tile_size))
    longest = max(int(width or tile), int(height or tile))
    levels = 1
    while longest > tile:
        longest = -(-longest // 2)
        levels += 1
    return levels


def aggregate_tile(datasource_name, layer_id, level, tile_x, tile_y, *,
                   genes=None, tile_size=DEFAULT_TILE_SIZE, min_q=None,
                   bins=AGGREGATE_BINS):
    """One tile of aggregated points: per bin and per gene, a count and a centroid.

    WHAT REPLACES THE DENSITY FALLBACK. Points mode stays points however far
    out the view goes; what changes with the zoom is how many molecules one
    dot stands for. A dot is drawn with the AREA of its count, so a bin
    holding four molecules is twice the radius of one holding a single
    molecule and the picture stays a scatter rather than becoming a slab.

    THE POSITION IS ONE OF THE MOLECULES, not the middle of the bin and not
    the mean of them. Every dot drawn is therefore somewhere a transcript
    actually was, which is the honest claim and also the one that looks
    right. A bin holding a single molecule sits exactly on it, so in sparse
    tissue -- which is most of a panel; the median gene here puts three
    molecules in a whole-slide bin -- zooming through a level boundary moves
    nothing at all.

    The mean was tried first and is wrong for the abundant genes. With two
    hundred molecules in a bin the mean converges on the bin's centre, so
    ACTB at whole-slide zoom came out as a PERFECT LATTICE of evenly spaced
    dots: an artifact of the binning, drawn as though it were the data. A
    member of the bin instead lands wherever that molecule was, so the same
    field reads as a scatter at the density the bin implies. The cost is
    that a dense bin's dot can jump within its bin when the level changes;
    a sparse one, which is the case anybody is actually reading positions
    off, cannot.

    Read straight off level 0 rather than off a coarser stored level. The
    inputs that decide what an aggregate IS -- which genes, and the quality
    threshold -- are both controls the user turns, so there is no one pyramid
    to store; what makes reading the base level affordable is that the gene
    index in each tile header turns a five-gene selection into five short
    seeks rather than a decode of the tile.

    @param level   - 0 is the molecules themselves, binned at
                     `tile_size / bins` image pixels; each level up doubles
                     both the tile's reach and the bin.
    @param genes   - gene INDICES to aggregate, or None for the whole panel.
    @param min_q   - drop calls below this score BEFORE merging, because the
                     count is a count of what survived the filter.
    @returns AGGREGATE_DTYPE records, most populous first.
    """
    manifest = read_manifest(datasource_name, layer_id)
    if not manifest:
        return np.empty(0, dtype=AGGREGATE_DTYPE)

    level = max(0, int(level))
    bins = max(1, int(bins))
    tile_size = max(1, int(tile_size or manifest.get("tile_size")
                           or DEFAULT_TILE_SIZE))

    if genes is None:
        wanted = np.arange(int(manifest.get("gene_count") or 0), dtype=np.int64)
    else:
        wanted = np.asarray(sorted({int(g) for g in genes}), dtype=np.int64)
    if not len(wanted):
        return np.empty(0, dtype=AGGREGATE_DTYPE)

    # Genes in chunks only when the grid would be enormous. One chunk is the
    # ordinary case and the loop then costs a comparison.
    per_chunk = max(1, AGGREGATE_MAX_CELLS // (bins * bins))
    parts = [_aggregate_chunk(datasource_name, layer_id, level, tile_x, tile_y,
                              chunk=wanted[at:at + per_chunk],
                              tile_size=tile_size, bins=bins, min_q=min_q)
             for at in range(0, len(wanted), per_chunk)]
    parts = [part for part in parts if len(part)]
    if not parts:
        return np.empty(0, dtype=AGGREGATE_DTYPE)

    out = np.concatenate(parts) if len(parts) > 1 else parts[0]
    # Biggest first, so the small dots are drawn over the large ones rather
    # than under them. Without it a bin holding one molecule of a rare gene
    # disappears behind the abundant gene it shares the bin with.
    return out[np.argsort(-out["count"].astype(np.int64), kind="stable")]


def _priority(xs, ys):
    """A pseudo-random ordering of points that depends only on where they are.

    What it is for is picking which molecule stands for a bin (see
    `_aggregate_chunk`), and it has two jobs. The same molecule must always
    get the same number, so that the dot representing a crowded bin does not
    move when the user switches a different gene on and a tile the browser
    has cached is still the tile the server would send. And the number must
    AVALANCHE -- every output bit has to depend on every input bit -- which
    is a stronger property than "looks scattered" and is the one that was
    missing.

    WHY AVALANCHE AND NOT MERELY SCATTER. The winner of a bin is the MAXIMUM
    of this over the molecules in it, so what decides the dot's position is
    the hash's HIGH bits. The version before this was the classic spatial
    hash -- quantise, multiply by two large odd constants, exclusive-or,
    fold once by 15 -- and on real coordinates `qx * 73_856_093` never
    reaches 2**63, so it does not wrap: its high bits are a monotone ramp in
    x, only lightly disturbed by the smaller y term. Taking the maximum of a
    ramp picks the same place in every bin. Measured on forty molecules
    scattered through a bin, the winner landed in the middle two tenths of
    it 75% of the time and in the outer four tenths never -- so an abundant
    gene at whole-slide zoom came out as a vertical comb of dots one bin
    apart, which is the binning drawn as though it were the data. That is
    the same artifact `aggregate_tile` rejects the MEAN for, arrived at by a
    different road: a pick that is nearly always central is a centroid with
    extra steps.

    So: xor of two odd multiples into the splitmix64 finalizer, whose whole
    purpose is that the top bits are as mixed as the bottom ones. The same
    measurement gives every tenth of the bin 9.7-10.3% of the winners in
    both axes, which is the uniform pick the docstring above always claimed.
    """
    qx = np.rint(np.asarray(xs, dtype=np.float64) * 16).astype(np.int64)
    qy = np.rint(np.asarray(ys, dtype=np.float64) * 16).astype(np.int64)
    # Unsigned throughout, because every step below relies on wrapping at
    # 2**64 and signed overflow is undefined behaviour's numpy cousin: a
    # RuntimeWarning and a platform-dependent answer.
    mixed = ((qx.astype(np.uint64) * np.uint64(0x9E3779B97F4A7C15))
             ^ (qy.astype(np.uint64) * np.uint64(0xC2B2AE3D27D4EB4F)))
    mixed ^= mixed >> np.uint64(30)
    mixed *= np.uint64(0xBF58476D1CE4E5B9)
    mixed ^= mixed >> np.uint64(27)
    mixed *= np.uint64(0x94D049BB133111EB)
    return mixed ^ (mixed >> np.uint64(31))


def _aggregate_chunk(datasource_name, layer_id, level, tile_x, tile_y, *,
                     chunk, tile_size, bins, min_q):
    """`aggregate_tile` for one slice of the gene list. See there."""
    span = tile_size * (2 ** level)
    origin_x, origin_y = tile_x * span, tile_y * span
    scale = bins / span
    step = 2 ** level

    cells = len(chunk) * bins * bins
    counts = np.zeros(cells, dtype=np.int64)
    # The position of ONE of the molecules in each cell, not the mean of
    # them -- see `aggregate_tile`. A plain scatter, so the last write to a
    # cell wins, and the points are written in order of a priority that
    # depends only on WHERE EACH MOLECULE IS (`_priority`). Two properties
    # come from that and both matter:
    #
    #   The pick is spatially arbitrary. Writing in the order the tiles
    #   happen to be stored in picks the bin's right-hand edge every time,
    #   because a level-0 tile is x-sorted and a bin sits inside one --
    #   measured here as a mean offset of 0.92 across the bin. That is a
    #   lattice again, just shifted.
    #
    #   The pick does not depend on what else was asked for. Switching a
    #   second gene on must not move the first gene's dots, and anything
    #   derived from buffer order (a shuffle, the read sequence) does
    #   exactly that. Strictly this holds within one fold, and a request
    #   big enough to fold twice can let a later batch overwrite an
    #   earlier winner -- still deterministic, which is what the cache and
    #   the eye need, and only reachable by a selection far larger than
    #   anything worth drawing at once.
    rep_x = np.zeros(cells, dtype=np.float32)
    rep_y = np.zeros(cells, dtype=np.float32)

    # Gene index to its row in the accumulator. An array rather than a dict
    # because it is indexed once per molecule.
    rows = np.full(int(chunk.max()) + 1, -1, dtype=np.int64)
    rows[chunk] = np.arange(len(chunk))

    keys, xs, ys, buffered = [], [], [], 0

    def fold():
        nonlocal buffered
        if not buffered:
            return
        key = np.concatenate(keys) if len(keys) > 1 else keys[0]
        px = np.concatenate(xs) if len(xs) > 1 else xs[0]
        py = np.concatenate(ys) if len(ys) > 1 else ys[0]
        counts[:] += np.bincount(key, minlength=cells)
        # Stable sort of an integer key, which numpy does with a radix sort
        # -- linear, and a few milliseconds on the half-million points a
        # whole-slide tile carries.
        order = np.argsort(_priority(px, py), kind="stable")
        rep_x[key[order]] = px[order]
        rep_y[key[order]] = py[order]
        keys.clear()
        xs.clear()
        ys.clear()
        buffered = 0

    for dy in range(step):
        for dx in range(step):
            points = read_tile(datasource_name, layer_id,
                               tile_x * step + dx, tile_y * step + dy,
                               chunk, level=0, min_q=min_q)
            if not len(points):
                continue
            row = rows[points["gene"].astype(np.int64)]
            # A gene the header offered but this chunk did not ask for
            # cannot appear -- `read_tile` seeks only the runs it was given
            # -- but a cache whose manifest undercounts its genes can, and
            # an unchecked -1 would fold it into the last bin of the grid.
            keep = row >= 0
            if not keep.all():
                points, row = points[keep], row[keep]
                if not len(points):
                    continue
            ix = np.clip(((points["x"] - origin_x) * scale).astype(np.int64),
                         0, bins - 1)
            iy = np.clip(((points["y"] - origin_y) * scale).astype(np.int64),
                         0, bins - 1)
            keys.append((row * bins + iy) * bins + ix)
            xs.append(points["x"])
            ys.append(points["y"])
            buffered += len(points)
            if buffered >= AGGREGATE_FLUSH:
                fold()
    fold()

    hit = np.flatnonzero(counts)
    if not len(hit):
        return np.empty(0, dtype=AGGREGATE_DTYPE)
    out = np.empty(len(hit), dtype=AGGREGATE_DTYPE)
    out["gene"] = chunk[hit // (bins * bins)]
    out["x"] = rep_x[hit]
    out["y"] = rep_y[hit]
    out["count"] = np.minimum(counts[hit], np.iinfo(np.uint32).max)
    return out


# -- density ----------------------------------------------------------------

#: One tile's cut of the bin grid: `size` IMAGE pixels per bin, the global
#: index of the first bin the tile touches on each axis, how many it touches,
#: and the tile's own origin and pixel size to measure against.
Grid = collections.namedtuple(
    "Grid", "size first_x first_y nx ny step origin_x origin_y")


def bin_pixels_for(tile_size, level, bin_pixels=None):
    """How many IMAGE pixels one bin covers. EXACTLY what was asked for.

    A BIN SIZE IS ABSOLUTE: 188 pixels is 40 microns at every zoom level, and
    a bin that changed size with the level would be a density map whose
    numbers meant something different in every frame -- which is the one thing
    a density map must not be. This used to round the count of bins to fit a
    tile, which made the drawn bin 204.8 pixels at one level and 186.2 at the
    next: the boxes resized and the grid shifted every time the viewer crossed
    a level, and the colour of a patch of tissue moved with it. That is the
    bug this function exists to not have.

    One floor, and it is a physical one: a bin cannot be finer than the level
    draws. At level L one tile pixel IS 2^L image pixels, so a smaller bin has
    nowhere to be drawn and the size widens to meet it -- the same widening
    the old bin count did by being clipped at `tile_size`.

    No bin size asked for is the original behaviour, spelled out: one bin per
    LEVEL pixel, which is what every density tile served before this control
    existed.
    """
    step = 2 ** int(level)
    if not bin_pixels or float(bin_pixels) <= 0:
        return step
    return max(step, int(round(float(bin_pixels))))


def grid_for(tile_size, level, tile_x, tile_y, bin_pixels=None):
    """The bin grid this tile is cut on, ANCHORED TO THE IMAGE.

    Every bin boundary is a multiple of `size` image pixels from the image's
    own origin -- not from this tile's corner, and not from this level's. That
    is what makes a bin the same patch of tissue at every zoom and in every
    neighbouring tile: box 108 is box 108 whoever is drawing it, so crossing a
    level boundary changes how finely the tile is drawn and nothing else.

    The tile therefore holds a whole number of bins only by accident, and the
    ones at its edges are usually partial -- half of box 108 here, the other
    half in the tile to the left. That is correct, and it is the price of the
    grid being the image's rather than the tile's.
    """
    step = 2 ** int(level)
    size = bin_pixels_for(tile_size, level, bin_pixels)
    span = int(tile_size) * step
    origin_x, origin_y = int(tile_x) * span, int(tile_y) * span
    first_x, first_y = origin_x // size, origin_y // size
    return Grid(size, first_x, first_y,
                (origin_x + span - 1) // size - first_x + 1,
                (origin_y + span - 1) // size - first_y + 1,
                step, origin_x, origin_y)


def _overhang(grid, tile_size):
    """How many level-0 tiles out the rasterization has to read.

    A BIN THAT STRADDLES THIS TILE'S EDGE HOLDS MOLECULES FROM THE TILE NEXT
    DOOR, and a grid anchored to the image is full of them -- box 108 starts
    at image pixel 20,304 whether or not the tile does. Counting only the part
    inside would draw that box darker here than in the tile that owns its
    other half, which is a seam down every tile boundary AND a colour that
    changes with the zoom, because how much of the box falls outside depends
    on the level.

    Zero when the grid happens to line up with the tile, which is every tile
    when no bin size was asked for (one bin per level pixel) -- the case that
    must not pay for this.
    """
    span = int(tile_size) * grid.step
    if (grid.origin_x % grid.size == 0 and grid.origin_y % grid.size == 0
            and span % grid.size == 0):
        return 0
    # A bin wider than a level-0 tile reaches past its immediate neighbours.
    return max(1, -(-grid.size // int(tile_size)))


def _block_index(grid, tile_size):
    """`(columns, rows)`: which of the grid's bins each tile pixel falls in."""
    pixels = np.arange(int(tile_size), dtype=np.int64) * grid.step
    return ((grid.origin_x + pixels) // grid.size - grid.first_x,
            (grid.origin_y + pixels) // grid.size - grid.first_y)


def _to_tile(raster, tile_size, grid):
    """A binned raster back at tile resolution, one block per bin.

    Nearest-neighbour and deliberately so: a density map with 40-micron bins
    IS a grid of squares, and smoothing them into a gradient would draw a
    resolution the bins do not have. The blocks are the honest picture, and
    they are what the bin-size control is for.

    Every tile pixel is sent to the bin the IMAGE puts it in, which is what
    keeps the blocks in step across a tile boundary and across a level: the
    arithmetic is the same one `grid_for` used, so the box a pixel lands in
    does not depend on which tile is being drawn.
    """
    if grid.size == grid.step and grid.nx == tile_size and grid.ny == tile_size:
        return raster
    columns, rows = _block_index(grid, tile_size)
    return raster[np.ix_(rows, columns)]


#: The blank strip between one bin's box and the next, as a fraction of a
#: bin's width.
#:
#: A density map IS a grid of boxes -- `_to_tile` is nearest-neighbour for
#: exactly that reason -- and boxes drawn edge to edge stop reading as boxes:
#: the eye joins them into a continuous field, which is a resolution the bins
#: do not have. The gap is what makes the bin visible as the unit it is. It is
#: also the only place the morphology shows through a ramp that now covers the
#: whole layer, so the two changes are one picture: paint every bin, and leave
#: a gap between them.
DENSITY_GUTTER = 0.06

#: How faint a sub-pixel gap is allowed to get.
#:
#: A pixel is the thinnest line there is, so once a bin is under about
#: seventeen pixels the gap it is owed is less than one and gets drawn at
#: partial strength instead (see `_gutter`). Strictly proportional, a bin
#: three pixels wide would be separated by a line at 18% -- honest, and too
#: faint to see, which defeats the point of drawing it. The grid has to be
#: legible at EVERY zoom, so a gap is never drawn fainter than this. It costs
#: the whole-slide view about a sixth of its ink, which is the price of the
#: bins being visible as bins there at all.
DENSITY_GUTTER_MIN_COVERAGE = 0.5

def _gutter(tile_size, grid, min_coverage=DENSITY_GUTTER_MIN_COVERAGE):
    """`(columns, rows, coverage)` for the blank strip, or None.

    `columns` and `rows` are 1-D boolean masks over the tile's pixels -- two
    of them, not one, because the grid is the IMAGE's and a tile's left edge
    and top edge fall at different places in it. `coverage` is how much of
    that strip is actually gap, 0..1.

    The strip goes on the leading edge of each box IN IMAGE SPACE, so it
    lands on the same tissue in every tile that draws that box and at every
    level. A strip measured from the tile's own edge instead would rule a line
    down every tile boundary, whether a box started there or not.

    THE GAP IS THE SAME FRACTION OF A BIN AT EVERY ZOOM, which is what
    `coverage` is for. A pixel is the smallest line that can be drawn, and at
    the whole-slide levels a bin is only two or three of them -- so a
    one-pixel hole there would take a THIRD of the map rather than a
    twentieth, and the grid would stop being a gap between boxes and become a
    screen door dimming everything behind it. Drawing that line at partial
    strength instead keeps the gap close to `DENSITY_GUTTER` of the bin
    however far out the view is -- never costing the picture much more ink
    than it is meant to -- with `DENSITY_GUTTER_MIN_COVERAGE` underneath it,
    because a gap nobody can see is not a gap.

    `min_coverage` is that floor. A bin layer passes 0.0: its heatmap is a
    colour SCALE, and a floor that dims a zoomed-out view by a sixth more
    than a zoomed-in one reads as a different colour for the same count
    (see `bin_tiles._alpha_grid`).
    """
    # A zero fraction means no grid, and has to be checked before the width is
    # rounded: the strip is at least one pixel wide once it is drawn at all,
    # so `max(1, round(0))` would rule a line anyway and the constant would
    # not be the switch it looks like. One bin per level pixel is the other
    # end of it -- there is no room between boxes one pixel wide.
    if DENSITY_GUTTER <= 0 or grid.size <= grid.step:
        return None
    wanted = (DENSITY_GUTTER * grid.size) / grid.step        # in TILE pixels
    if wanted >= 1.0:
        width, coverage = int(round(wanted)), 1.0
    else:
        width, coverage = 1, max(wanted, float(min_coverage))
    edge = width * grid.step                                 # in IMAGE pixels
    pixels = np.arange(int(tile_size), dtype=np.int64) * grid.step

    def strip(origin):
        image = origin + pixels
        return image - (image // grid.size) * grid.size < edge

    return strip(grid.origin_x), strip(grid.origin_y), coverage


def _cut_gutter(tile, gap):
    """`tile` with the blank strip drawn down towards nothing, in place.

    For the two paths where nothing IS nothing -- the counts raster and the
    per-gene composite, both of which the client adds with `lighter`, where
    black does not draw. The ramp path cannot use this: there zero is a
    colour, and its gap has to be cut out of the alpha instead.

    A strip that is only part gap is scaled rather than cleared, which is the
    same picture the ramp path draws with partial alpha. It does mean the
    counts raster's strip holds a fraction of a count -- that raster is a
    PICTURE by the time it gets here (`_to_tile` has already repeated every
    bin over its block) and the client windows and colours it, so a dimmer
    line is a dimmer line. Nothing reads a count back out of it.
    """
    if gap is None:
        return tile
    columns, rows, coverage = gap
    if coverage >= 1.0:
        tile[rows, ...] = 0
        tile[:, columns, ...] = 0
        return tile
    keep = 1.0 - coverage
    tile[rows, ...] = (tile[rows, ...] * keep).astype(tile.dtype)
    tile[:, columns, ...] = (tile[:, columns, ...] * keep).astype(tile.dtype)
    return tile


def density_tile(datasource_name, layer_id, level, tile_x, tile_y, *,
                 tile_size=DEFAULT_TILE_SIZE, genes=None, bin_pixels=None,
                 min_q=None):
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
    grid = grid_for(tile_size, level, tile_x, tile_y, bin_pixels)
    out = np.zeros((grid.ny, grid.nx), dtype=np.uint32)

    # Every level-0 tile under this one. At level 0 that is one tile; at level 3
    # it is 64, which is still a handful of small reads and avoids keeping a
    # second copy of the points per level on disk. Plus the ring around them,
    # where the grid straddles this tile's edge -- see `_overhang`.
    step = grid.step
    reach = _overhang(grid, tile_size)
    for dy in range(-reach, step + reach):
        for dx in range(-reach, step + reach):
            source_x, source_y = tile_x * step + dx, tile_y * step + dy
            if source_x < 0 or source_y < 0:
                continue
            points = read_tile(datasource_name, layer_id,
                               source_x, source_y, genes, min_q=min_q)
            if not len(points):
                continue
            ix = (np.floor_divide(points["x"], grid.size).astype(np.int64)
                  - grid.first_x)
            iy = (np.floor_divide(points["y"], grid.size).astype(np.int64)
                  - grid.first_y)
            # DROPPED, not clipped: the ring's molecules belong to the bins
            # that reach into this tile and to no others, and a clip would
            # pile the whole neighbourhood onto its border.
            inside = (ix >= 0) & (ix < grid.nx) & (iy >= 0) & (iy < grid.ny)
            np.add.at(out, (iy[inside], ix[inside]), 1)

    # uint16 because that is what the channel path carries. A bin holding more
    # than 65,535 transcripts is clipped rather than wrapped: a saturated hot
    # spot reads as "very dense", and a wrapped one reads as empty.
    return _cut_gutter(
        _to_tile(np.minimum(out, 65_535).astype(np.uint16), tile_size, grid),
        _gutter(tile_size, grid))


#: How far a molecule's contribution is spread, in IMAGE pixels, when a
#: density bin would otherwise be smaller than the spacing between molecules.
#:
#: Without it a density map at full zoom is not a density map: a bin is one
#: image pixel, the window rounds to "one molecule saturates", and every
#: molecule is a single hard dot -- three genes' worth of which sum to white
#: and lose the colours that were the point. Smoothed, an isolated molecule is
#: a faint blob and a crowded region is bright, which is what the word means.
#: Falls away by itself as the level coarsens: at level 3 a bin already covers
#: 64 pixels and sigma is under half a bin, so nothing is blurred.
DENSITY_SMOOTH_PIXELS = 3.0


def _smooth(counts, sigma):
    """`counts` as a local density. Returns molecules per bin, as floats.

    `gaussian_filter` conserves the total, so what comes back is the mean
    count per bin over a sigma-sized neighbourhood: an isolated molecule
    peaks at 1 / (2 pi sigma^2) rather than at 1.
    """
    if sigma < 0.5:
        return counts.astype(np.float32)
    try:
        from scipy.ndimage import gaussian_filter
    except ImportError:  # pragma: no cover - scipy arrives with scikit-image
        return counts.astype(np.float32)
    return gaussian_filter(counts.astype(np.float32), sigma, mode="nearest")


def _density_field(datasource_name, layer_id, level, tile_x, tile_y, *,
                   indices, tile_size, grid, min_q):
    """One tile's local density, in molecules per bin. `(ny, nx)` float32.

    The rasterization every coloured density path shares, pulled out so that
    the per-gene composite and the single-ramp heat map cannot disagree about
    what a bin holds -- they differ only in what colour they paint it.

    The blur is where the care is. A blur that stopped at the tile edge would
    leave a visible seam down every tile boundary: the molecules just outside
    contribute to the bins just inside, and a tile that cannot see them is
    darker at its rim. So the rasterization is padded, the neighbouring
    level-0 tiles are read, and the border is cropped off afterwards. The ring
    costs 4*step+4 extra tile reads and only at the levels that blur at all.
    """
    step = grid.step

    # The smoothing radius is constant in IMAGE pixels. Expressed in BINS it
    # therefore shrinks both as the level coarsens and as the bins are made
    # bigger -- 40-micron bins at level 0 are already wider than the blur, so
    # asking for coarse bins turns the smoothing off by itself, which is what
    # makes the bin-size control read as a bin size and not as a blur.
    sigma = DENSITY_SMOOTH_PIXELS / max(grid.size, 1e-6)
    pad = int(np.ceil(3 * sigma)) if sigma >= 0.5 else 0
    columns, rows = grid.nx + 2 * pad, grid.ny + 2 * pad
    # Two reasons to look outside this tile and one ring that serves both:
    # the blur reaches across the edge, and so does a bin that straddles it.
    reach = max(1 if pad else 0, _overhang(grid, tile_size))

    counts = np.zeros((rows, columns), dtype=np.uint32)
    for dy in range(-reach, step + reach):
        for dx in range(-reach, step + reach):
            source_x, source_y = tile_x * step + dx, tile_y * step + dy
            if source_x < 0 or source_y < 0:
                continue
            points = read_tile(datasource_name, layer_id,
                               source_x, source_y, indices, min_q=min_q)
            if not len(points):
                continue
            ix = (np.floor_divide(points["x"], grid.size).astype(np.int64)
                  - grid.first_x + pad)
            iy = (np.floor_divide(points["y"], grid.size).astype(np.int64)
                  - grid.first_y + pad)
            # DROPPED, not clipped: a clip would pile every molecule in
            # the neighbouring tiles onto this one's border.
            inside = (ix >= 0) & (ix < columns) & (iy >= 0) & (iy < rows)
            np.add.at(counts, (iy[inside], ix[inside]), 1)

    return _smooth(counts, sigma)[pad:pad + grid.ny, pad:pad + grid.nx]


def _stretch(density, ceiling, low=0.0, high=1.0):
    """A density field as a uint8 level, through a window the user can narrow.

    `ceiling` is the density that saturates when the window is wide open (see
    `layer_sources.density_scale`); `low` and `high` are FRACTIONS of it. A
    fraction rather than a count, because the count that means "dense" changes
    by a factor of four with every zoom level and a threshold the user set at
    one zoom has to still mean what they meant at the next.
    """
    top = float(ceiling) if ceiling else 1.0
    lo = max(0.0, float(low)) * top
    hi = max(float(high), float(low) + 1e-6) * top
    scaled = (density - lo) * (255.0 / max(hi - lo, 1e-6))
    return np.clip(scaled, 0, 255)


def density_rgb_tile(datasource_name, layer_id, level, tile_x, tile_y, *,
                     groups, tile_size=DEFAULT_TILE_SIZE, min_q=None,
                     bin_pixels=None, low=0.0, high=1.0):
    """Several genes' densities, each in its own colour, summed into one RGB tile.

    THE REASON DENSITY IS COMPOSITED HERE AND NOT IN THE BROWSER. A gene
    selection is a handful of genes with a colour each, and the obvious
    arrangement -- one tiled layer per gene -- means N tile requests, N decodes
    and N OpenSeadragon world items per pan, for a picture that is the sum of
    them. Summed server side it is one request, one decode, one item, and
    adding a gene costs a slightly slower tile rather than another whole layer.

    Each group is `(gene indices, (r, g, b), hi)`: its own rasterization, its
    own contrast window -- a rare gene and an abundant one share a tile and
    must not share a stretch, or the rare one is invisible -- and its colour
    multiplied in. The sum is clipped rather than wrapped, so a bin where three
    genes are all dense reads as white and not as black.

    @param groups  - list of `(indices, rgb, hi)`; `hi` is the count a bin
                     needs to saturate, as a FLOAT (see
                     `layer_sources.density_scale`).
    @returns (tile_size, tile_size, 3) uint8, which `encode_tile_array`
             recognises by shape.
    """
    grid = grid_for(tile_size, level, tile_x, tile_y, bin_pixels)
    total = np.zeros((grid.ny, grid.nx, 3), dtype=np.uint16)
    for indices, rgb, ceiling in groups:
        density = _density_field(datasource_name, layer_id, level,
                                 tile_x, tile_y, indices=indices,
                                 tile_size=tile_size, grid=grid, min_q=min_q)
        level8 = _stretch(density, ceiling, low, high).astype(np.uint16)
        for channel, component in enumerate(rgb):
            total[..., channel] += (level8 * int(component) // 255).astype(np.uint16)

    return _cut_gutter(
        _to_tile(np.minimum(total, 255).astype(np.uint8), tile_size, grid),
        _gutter(tile_size, grid))


def density_ramp_tile(datasource_name, layer_id, level, tile_x, tile_y, *,
                      indices, ramp, ceiling, tile_size=DEFAULT_TILE_SIZE,
                      min_q=None, bin_pixels=None, low=0.0, high=1.0):
    """The selection's density as ONE heat map, read off a colour ramp.

    The other half of the density story, and the difference from
    `density_rgb_tile` is a question about what is being asked. Per-gene
    colours answer "where is each of these three genes": the picture is three
    overlaid clouds and the colours are identities. A ramp answers "where is
    there a lot of this": one field, and the colour is a QUANTITY -- which is
    what a colormap is for and what a single hue cannot do, because the eye
    reads lightness far better than it reads saturation.

    One field means the genes are summed BEFORE the colour, not after: two
    genes at half strength in the same bin read as one bin's worth of
    transcripts, which is what they are, where the per-gene path would show
    two faint clouds.

    @param ramp - `(256, 3)` uint8, from `utils.colormaps.ramp`
    @returns (tile_size, tile_size, 4) uint8 -- RGBA, and see the gutter below
             for why this one path carries an alpha channel
    """
    grid = grid_for(tile_size, level, tile_x, tile_y, bin_pixels)
    density = _density_field(datasource_name, layer_id, level, tile_x, tile_y,
                             indices=indices, tile_size=tile_size, grid=grid,
                             min_q=min_q)
    level8 = _stretch(density, ceiling, low, high).astype(np.uint8)
    # THE WHOLE FIELD IS PAINTED, empty bins included: a bin with nothing in
    # it gets the ramp's bottom colour rather than a hole. A heat map that
    # stops where the molecules stop is a picture of the selection's OUTLINE
    # -- the eye reads the gaps as "not measured" instead of as "none here",
    # and the legend's bottom end then labels a colour that was never drawn.
    # Covering the layer edge to edge is what makes zero a value.
    #
    # It only works because the ramp raster is composited `source-over` (see
    # `transcriptLayer.densityBlend`). Under the `lighter` the per-gene
    # composite uses, a dark end painted everywhere would be a flat wash
    # ADDED to the morphology image. The two are one decision, and changing
    # either alone is the bug.
    coloured = np.asarray(ramp, dtype=np.uint8)[level8]
    tile = _to_tile(coloured, tile_size, grid)

    # THE ALPHA CHANNEL IS THE GRID. Every box is opaque and the strip between
    # them is not drawn at all, so the morphology shows through the gaps --
    # which is the only way it shows through at all now that the ramp covers
    # every bin. It has to be alpha and not a dark line for the same reason
    # the empty bins above are painted: under `source-over` a black line is a
    # black line, ruled across the tissue.
    #
    # Always RGBA, gutter or none, so the tile has one shape whatever the zoom
    # -- the alpha plane is constant where there is no gap and costs nothing
    # once compressed.
    alpha = np.full(tile.shape[:2], 255, dtype=np.uint8)
    gap = _gutter(tile_size, grid)
    if gap is not None:
        columns, rows, coverage = gap
        # Partial where a whole pixel would be too much (see `_gutter`): the
        # gap is worth the same fraction of a bin at every zoom, and at the
        # whole-slide levels that fraction is less than a pixel wide.
        left = np.uint8(round(255 * (1.0 - coverage)))
        alpha[rows, :] = left
        alpha[:, columns] = left
    return np.dstack((tile, alpha))
