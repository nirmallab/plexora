"""A 10x feature-barcode matrix, converted once into an AnnData Plexora reads.

Space Ranger writes its expression as `filtered_feature_bc_matrix.h5`: CSC
with BARCODES as the columns, which is byte for byte CSR of barcodes x genes.
Every question a viewer asks of it is per GENE -- colour the cells by INS,
gate on GCG, describe PRSS1 -- and against that layout one gene is a scan of
the whole matrix (870 million entries at 2 microns, half a billion at 8). So
the matrix is rewritten once, gene-major, into an `.h5ad` whose `X` is
`csc_matrix` over barcodes x genes: one gene is then one contiguous slice.

The target is an ordinary AnnData, registered as one. That is deliberate: ROI
write-back, gating's save, the notebook's `plexora.view`, SCIMAP -- everything
that already reads and writes `.h5ad` works on a Visium HD sample unchanged,
because it IS one by the time anything looks at it. What makes 18,000 genes
tractable is the adapter's wide mode (see `AnnDataAdapter.read_feature_column`),
not anything here.

What the conversion adds besides the layout:

- `obs`: `in_tissue`, `array_row`, `array_col` from the positions parquet, and
  one categorical per Space Ranger clustering (`graphclust`, `kmeans_2`, ...).
  For segmented cells, `cell_id` -- the integer the cell polygons are labelled
  with, so a cell's row and its outline share an id without a lookup table.
- `obsm["spatial"]`: centres in the run's FULL-RES microscope pixels; the
  project's coordinate spec scales them into whichever picture is the
  reference. `obsm["X_umap"]` when Space Ranger computed one.
- `var["plx_*"]`: per-gene count, sum, mean, std, quartiles, min/max and a
  50-bin histogram, computed during the write. A description of 18,000 genes
  must not cost 18,000 column reads.

Memory is bounded by one bucket of entries (~a few hundred MB), whatever the
matrix size. Heavy imports (h5py, anndata, pyarrow) are inside functions: the
boundary tests pin that building the app imports none of them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

#: Bumped whenever what `convert` writes changes; a target from an older
#: version is reconverted rather than read.
CONVERTER_VERSION = 1

#: Matrix entries read per slab of the source.
CHUNK_ENTRIES = 16 << 20

#: Genes per bucket file. The source is barcode-major; a bucket collects one
#: range of genes' entries so it can be sorted gene-major in memory.
BUCKETS = 48

#: Histogram bins in a feature's description -- the same 50 `_describe_column`
#: draws, so a precomputed description and a computed one look identical.
HISTOGRAM_BINS = 50

_BIN_BARCODE = re.compile(rb"^s_(\d{3})um_(\d+)_(\d+)-\d+$")
_CELL_BARCODE = re.compile(rb"^cellid_(\d+)-\d+$")
_CENTROID = re.compile(
    rb'"cell_id"\s*:\s*(\d+)\s*,\s*"cell_centroid"\s*:\s*\[\s*'
    rb'([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\]')


@dataclass(frozen=True)
class TenxLayout:
    """A 10x matrix and the Space Ranger files that give its rows a place."""

    matrix: Path
    kind: str                                  # "bins" or "cells"
    positions: Path | None = None              # bins: tissue_positions.parquet
    geojson: Path | None = None                # cells: cell_segmentations.geojson
    scalefactors: Path | None = None
    clusterings: tuple = field(default_factory=tuple)
    umap: Path | None = None


def is_tenx_matrix(path) -> bool:
    """Whether this `.h5` is a 10x feature-barcode matrix."""
    path = Path(path)
    if path.suffix.lower() != ".h5" or not path.is_file():
        return False
    try:
        import h5py

        with h5py.File(path, "r") as handle:
            kind = handle.attrs.get("filetype")
            if isinstance(kind, bytes):
                kind = kind.decode()
            return kind == "matrix" and "matrix" in handle
    except Exception:
        return False


def layout_for(matrix) -> TenxLayout:
    """What sits beside this matrix, in Space Ranger's own layout."""
    matrix = Path(matrix)
    folder = matrix.parent
    analysis = folder / "analysis"
    clusterings = tuple(sorted(analysis.glob("clustering/*/clusters.csv")))
    umap = next(iter(sorted(analysis.glob("umap/*/projection.csv"))), None)
    scalefactors = folder / "spatial" / "scalefactors_json.json"
    scalefactors = scalefactors if scalefactors.is_file() else None
    geojson = folder / "cell_segmentations.geojson"
    if geojson.is_file() and "cell" in matrix.name:
        return TenxLayout(matrix=matrix, kind="cells", geojson=geojson,
                          scalefactors=scalefactors, clusterings=clusterings,
                          umap=umap)
    positions = folder / "spatial" / "tissue_positions.parquet"
    return TenxLayout(matrix=matrix, kind="bins",
                      positions=positions if positions.is_file() else None,
                      scalefactors=scalefactors, clusterings=clusterings,
                      umap=umap)


def matrix_summary(path):
    """`{genes, barcodes, nnz, kind, bin_um}` from metadata alone."""
    import h5py

    with h5py.File(path, "r") as handle:
        matrix = handle["matrix"]
        genes, barcodes = (int(v) for v in matrix["shape"][:])
        first = bytes(matrix["barcodes"][0]) if barcodes else b""
        nnz = int(matrix["data"].shape[0])
    found = _BIN_BARCODE.match(first.rstrip(b"\0"))
    return {"genes": genes, "barcodes": barcodes, "nnz": nnz,
            "kind": "bins" if found else (
                "cells" if _CELL_BARCODE.match(first.rstrip(b"\0")) else "other"),
            "bin_um": int(found.group(1)) if found else None}


# -- barcodes --------------------------------------------------------------

def _fixed_width_numbers(barcodes, pattern, groups):
    barcodes = np.asarray(barcodes)
    if not len(barcodes):
        return [np.empty(0, np.int64) for _ in groups]
    first = bytes(barcodes[0]).rstrip(b"\0")
    match = pattern.match(first)
    if not match:
        raise ValueError(f"{first!r} is not a barcode of the expected shape")
    width = barcodes.dtype.itemsize
    raw = np.frombuffer(np.ascontiguousarray(barcodes).tobytes(),
                        dtype=np.uint8).reshape(len(barcodes), width)
    out = []
    for group in groups:
        start, stop = match.span(group)
        digits = raw[:, start:stop].astype(np.int64) - 48
        if ((digits < 0) | (digits > 9)).any():
            bad = int(np.flatnonzero(((digits < 0) | (digits > 9)).any(1))[0])
            raise ValueError(f"barcode {bytes(barcodes[bad])!r} does not have "
                             f"the shape of {first!r}")
        powers = 10 ** np.arange(stop - start - 1, -1, -1, dtype=np.int64)
        out.append(digits @ powers)
    return out


def parse_bin_barcodes(barcodes):
    """`(rows, cols)` from Visium HD bin barcodes, `s_002um_RRRRR_CCCCC-1`.

    Fixed width, which every Space Ranger bin barcode is: the digit runs are
    located in the first barcode and read out of all of them by byte offset,
    which for twenty million barcodes is a second rather than a minute.
    """
    rows, cols = _fixed_width_numbers(barcodes, _BIN_BARCODE, (2, 3))
    return rows, cols


def parse_cell_barcodes(barcodes):
    """The integer cell ids in `cellid_000000001-1` barcodes."""
    (ids,) = _fixed_width_numbers(barcodes, _CELL_BARCODE, (1,))
    return ids


# -- positions ---------------------------------------------------------------

def read_bin_positions(layout, rows, cols):
    """`(x, y, in_tissue)` in full-res pixels for these grid squares.

    Joined on the grid square rather than on the barcode string: both sides
    carry the row and column, and an integer join over twenty million rows is
    a sort where a string join would be twenty million Python objects.
    """
    import pyarrow.parquet as pq

    if layout.positions is None:
        raise ValueError(f"No spatial/tissue_positions.parquet beside "
                         f"{layout.matrix.name}")
    table = pq.read_table(layout.positions, columns=[
        "in_tissue", "array_row", "array_col", "pxl_row_in_fullres",
        "pxl_col_in_fullres"])
    p_row = table.column("array_row").to_numpy().astype(np.int64)
    p_col = table.column("array_col").to_numpy().astype(np.int64)
    width = int(max(p_col.max(initial=0), np.max(cols, initial=0))) + 1
    key = p_row * width + p_col
    order = np.argsort(key, kind="stable")
    key = key[order]
    wanted = np.asarray(rows, np.int64) * width + np.asarray(cols, np.int64)
    at = np.searchsorted(key, wanted)
    at = np.clip(at, 0, len(key) - 1)
    missing = key[at] != wanted
    if missing.any():
        bad = int(np.flatnonzero(missing)[0])
        raise ValueError(
            f"{int(missing.sum())} barcodes have no position in "
            f"{layout.positions.name} (first: row {int(rows[bad])}, column "
            f"{int(cols[bad])})")
    source = order[at]
    x = table.column("pxl_col_in_fullres").to_numpy()[source]
    y = table.column("pxl_row_in_fullres").to_numpy()[source]
    tissue = table.column("in_tissue").to_numpy()[source]
    return x.astype(np.float64), y.astype(np.float64), tissue.astype(np.uint8)


def read_cell_centroids(geojson):
    """`{cell_id: (x, y)}` arrays from Space Ranger's cell GeoJSON.

    Space Ranger states each cell's centroid in its properties, so this reads
    those rather than computing them from the rings -- by pattern over the
    raw bytes, because the file is several hundred megabytes and a full
    `json.load` of 800,000 polygons is gigabytes of Python objects. A file
    that does not match the pattern (another writer, reordered properties)
    falls back to the full parse and the ring's vertex mean.

    @returns `(ids int64, x float64, y float64)`
    """
    data = Path(geojson).read_bytes()
    found = _CENTROID.findall(data)
    features = data.count(b'"Feature"')
    if found and len(found) >= features:
        ids = np.fromiter((int(m[0]) for m in found), dtype=np.int64,
                          count=len(found))
        x = np.fromiter((float(m[1]) for m in found), dtype=np.float64,
                        count=len(found))
        y = np.fromiter((float(m[2]) for m in found), dtype=np.float64,
                        count=len(found))
        return ids, x, y
    doc = json.loads(data)
    ids, xs, ys = [], [], []
    for feature in doc.get("features") or ():
        props = feature.get("properties") or {}
        if "cell_id" not in props:
            continue
        centre = props.get("cell_centroid")
        if not centre:
            geometry = feature.get("geometry") or {}
            rings = geometry.get("coordinates") or []
            if geometry.get("type") == "MultiPolygon":
                rings = rings[0] if rings else []
            ring = np.asarray(rings[0] if rings else [[np.nan, np.nan]], float)
            centre = ring[:, :2].mean(0)
        ids.append(int(props["cell_id"]))
        xs.append(float(centre[0]))
        ys.append(float(centre[1]))
    return (np.asarray(ids, np.int64), np.asarray(xs, np.float64),
            np.asarray(ys, np.float64))


def _read_clustering(path, barcodes):
    """One `clusters.csv` as labels aligned to `barcodes`, or None."""
    import pandas as pd

    frame = pd.read_csv(path)
    if frame.shape[1] < 2:
        return None
    index = pd.Index(frame.iloc[:, 0].astype(str))
    positions = index.get_indexer(barcodes)
    labels = frame.iloc[:, 1].astype(str).to_numpy()
    values = np.where(positions >= 0, labels[np.clip(positions, 0, None)], "")
    levels = sorted({v for v in values if v},
                    key=lambda v: (0, int(v)) if v.isdigit() else (1, v))
    return pd.Categorical(np.where(values == "", None, values),
                          categories=levels)


def clustering_name(path) -> str:
    """`gene_expression_graphclust` -> `graphclust`."""
    name = Path(path).parent.name
    return name[len("gene_expression_"):] if name.startswith(
        "gene_expression_") else name


def _read_umap(path, barcodes):
    import pandas as pd

    frame = pd.read_csv(path)
    index = pd.Index(frame.iloc[:, 0].astype(str))
    positions = index.get_indexer(barcodes)
    values = frame.iloc[:, 1:3].to_numpy(dtype=np.float32)
    out = np.full((len(barcodes), 2), np.nan, dtype=np.float32)
    hit = positions >= 0
    out[hit] = values[positions[hit]]
    return out


# -- freshness -----------------------------------------------------------------

def _stamp(path):
    if path is None:
        return None
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    return [str(path.resolve()), stat.st_size, stat.st_mtime_ns]


def source_fingerprint(layout) -> dict:
    return {
        "version": CONVERTER_VERSION,
        "matrix": _stamp(layout.matrix),
        "positions": _stamp(layout.positions),
        "geojson": _stamp(layout.geojson),
        "clusterings": [_stamp(p) for p in layout.clusterings],
        "umap": _stamp(layout.umap),
    }


def read_provenance(target) -> dict | None:
    """The `uns["plexora_tenx"]` a conversion wrote, or None."""
    target = Path(target)
    if not target.is_file():
        return None
    try:
        import h5py

        with h5py.File(target, "r") as handle:
            raw = handle.attrs.get("plexora_tenx")
            if raw is None:
                return None
            return json.loads(raw if isinstance(raw, str) else raw.decode())
    except Exception:
        return None


def is_current(target, layout) -> bool:
    found = read_provenance(target)
    return bool(found) and found.get("fingerprint") == source_fingerprint(layout)


# -- statistics ------------------------------------------------------------------

def _quantiles_with_zeros(sorted_nonzero, n, qs):
    """`np.percentile(column, qs)` for a column of `n` values, mostly zero."""
    zeros = n - len(sorted_nonzero)
    out = []
    for q in qs:
        position = q * (n - 1)
        lo, hi = int(np.floor(position)), int(np.ceil(position))
        frac = position - lo

        def value(i):
            return 0.0 if i < zeros else float(sorted_nonzero[i - zeros])

        low = value(lo)
        out.append(low + (value(hi) - low) * frac)
    return out


def _histogram_with_zeros(values, n):
    """`np.histogram(column, 50, density=True)` for a column mostly zero.

    `(edges_min, edges_max, densities)` -- the densities over
    `linspace(min, max, 51)`, exactly as `_describe_column` computes them.
    """
    zeros = n - len(values)
    low = 0.0 if zeros else float(values.min()) if len(values) else 0.0
    high = float(values.max()) if len(values) else 0.0
    if zeros and not len(values):
        high = 0.0
    if high == low:
        low, high = low - 0.5, high + 0.5
    counts, edges = np.histogram(values, bins=HISTOGRAM_BINS, range=(low, high))
    counts = counts.astype(np.float64)
    if zeros:
        at = int(np.clip(np.searchsorted(edges, 0.0, side="right") - 1,
                         0, HISTOGRAM_BINS - 1))
        counts[at] += zeros
    width = (high - low) / HISTOGRAM_BINS
    return low, high, (counts / (n * width)).astype(np.float32)


class _GeneStats:
    def __init__(self, genes, rows):
        self.rows = rows
        self.values = {name: np.zeros(genes, dtype=np.float64) for name in (
            "nnz", "sum", "mean", "std", "min", "max", "q25", "q50", "q75",
            "log_mean", "log_std", "log_min", "log_max", "log_q25", "log_q50",
            "log_q75", "hist_lo", "hist_hi", "log_hist_lo", "log_hist_hi")}
        self.hist = np.zeros((genes, HISTOGRAM_BINS), dtype=np.float32)
        self.log_hist = np.zeros((genes, HISTOGRAM_BINS), dtype=np.float32)

    def add(self, gene, values):
        n = self.rows
        v = self.values
        for prefix, data, hist in (("", values, self.hist),
                                   ("log_", np.log1p(values), self.log_hist)):
            data = np.sort(data)
            total = float(data.sum(dtype=np.float64))
            squares = float((data.astype(np.float64) ** 2).sum())
            mean = total / n if n else 0.0
            variance = ((squares - n * mean * mean) / (n - 1)) if n > 1 else 0.0
            v[f"{prefix}mean"][gene] = mean
            v[f"{prefix}std"][gene] = np.sqrt(max(variance, 0.0))
            v[f"{prefix}min"][gene] = 0.0 if len(data) < n else float(data[0])
            v[f"{prefix}max"][gene] = float(data[-1]) if len(data) else 0.0
            q25, q50, q75 = _quantiles_with_zeros(data, n, (0.25, 0.5, 0.75))
            v[f"{prefix}q25"][gene], v[f"{prefix}q50"][gene] = q25, q50
            v[f"{prefix}q75"][gene] = q75
            lo, hi, densities = _histogram_with_zeros(data, n)
            v[f"{prefix}hist_lo"][gene], v[f"{prefix}hist_hi"][gene] = lo, hi
            hist[gene] = densities
            if not prefix:
                v["nnz"][gene] = len(data)
                v["sum"][gene] = total


# -- conversion ------------------------------------------------------------------

def _deduplicate(names):
    seen, out = {}, []
    for name in names:
        if name in seen:
            seen[name] += 1
            out.append(f"{name}-{seen[name]}")
        else:
            seen[name] = 0
            out.append(name)
    return out


def convert(layout, target, progress=None, stage=None):
    """Write `layout` as a gene-major `.h5ad` at `target`. Returns the path.

    Written beside the target and moved into place, so a conversion that dies
    halfway leaves the previous one (or nothing) rather than a file that reads
    as current and holds half the genes.

    @param progress - optional callable(done, total)
    """
    import h5py
    import pandas as pd
    from anndata.io import write_elem

    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    buckets_dir = target.with_name(f"{target.name}.buckets.{os.getpid()}")
    shutil.rmtree(buckets_dir, ignore_errors=True)
    buckets_dir.mkdir(parents=True)
    fingerprint = source_fingerprint(layout)

    try:
        with h5py.File(layout.matrix, "r") as source:
            matrix = source["matrix"]
            genes, n_rows = (int(v) for v in matrix["shape"][:])
            nnz = int(matrix["data"].shape[0])
            barcodes = matrix["barcodes"][:]
            names = [n.decode() for n in matrix["features"]["name"][:]]
            ids = [n.decode() for n in matrix["features"]["id"][:]]
            kinds = ([n.decode() for n in matrix["features"]["feature_type"][:]]
                     if "feature_type" in matrix["features"] else
                     ["Gene Expression"] * genes)
            indptr = matrix["indptr"][:].astype(np.int64)

            # -- where each barcode is -----------------------------------------
            if stage:
                stage("positions")
            obs = pd.DataFrame(index=pd.Index(
                [b.decode() for b in barcodes], name=None))
            if layout.kind == "cells":
                cell_ids = parse_cell_barcodes(barcodes)
                obs["cell_id"] = cell_ids
                spatial = np.full((n_rows, 2), np.nan)
                if layout.geojson is not None:
                    got, x, y = read_cell_centroids(layout.geojson)
                    order = np.argsort(got)
                    at = np.clip(np.searchsorted(got[order], cell_ids), 0,
                                 max(len(got) - 1, 0))
                    hit = got[order][at] == cell_ids if len(got) else np.zeros(
                        n_rows, bool)
                    spatial[hit, 0] = x[order][at][hit]
                    spatial[hit, 1] = y[order][at][hit]
                    if not hit.all():
                        raise ValueError(
                            f"{int((~hit).sum())} cells in {layout.matrix.name} "
                            f"have no polygon in {layout.geojson.name}")
                array = None
            else:
                rows, cols = parse_bin_barcodes(barcodes)
                x, y, tissue = read_bin_positions(layout, rows, cols)
                obs["in_tissue"] = tissue
                obs["array_row"] = rows.astype(np.int32)
                obs["array_col"] = cols.astype(np.int32)
                spatial = np.column_stack((x, y))
                array = np.column_stack((cols, rows)).astype(np.float64)
            text_barcodes = obs.index.to_numpy(dtype=str)
            for path in layout.clusterings:
                labels = _read_clustering(path, text_barcodes)
                if labels is not None:
                    obs[clustering_name(path)] = labels
            umap = (_read_umap(layout.umap, text_barcodes)
                    if layout.umap is not None else None)

            # -- pass 1: entries to gene buckets ---------------------------------
            if stage:
                stage("converting")
            edges = np.linspace(0, genes, BUCKETS + 1).astype(np.int64)
            data = matrix["data"]
            indices = matrix["indices"]
            steps = max(1, -(-nnz // CHUNK_ENTRIES))
            spill_dtype = np.dtype([("gene", "<i4"), ("row", "<u4"),
                                    ("value", "<f4")])
            row_of = None
            for step, start in enumerate(range(0, nnz, CHUNK_ENTRIES), 1):
                stop = min(start + CHUNK_ENTRIES, nnz)
                gene = indices[start:stop].astype(np.int64)
                value = data[start:stop].astype(np.float32)
                first = int(np.searchsorted(indptr, start, side="right")) - 1
                last = int(np.searchsorted(indptr, stop - 1, side="right")) - 1
                local = indptr[first:last + 2].clip(start, stop) - start
                row_of = np.repeat(np.arange(first, last + 1, dtype=np.int64),
                                   np.diff(local))
                bucket = np.searchsorted(edges, gene, side="right") - 1
                order = np.argsort(bucket, kind="stable")
                records = np.empty(len(gene), dtype=spill_dtype)
                records["gene"] = gene[order]
                records["row"] = row_of[order]
                records["value"] = value[order]
                bucket = bucket[order]
                starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
                stops = np.r_[starts[1:], len(bucket)]
                for s, e in zip(starts, stops):
                    with open(buckets_dir / f"b{int(bucket[s]):03d}.bin",
                              "ab") as handle:
                        handle.write(records[s:e].tobytes())
                if progress:
                    progress(step, steps + BUCKETS)

        # -- pass 2: each bucket gene-major into X ---------------------------------
        with h5py.File(tmp, "w") as out:
            out.attrs["encoding-type"] = "anndata"
            out.attrs["encoding-version"] = "0.1.0"
            group = out.create_group("X")
            group.attrs["encoding-type"] = "csc_matrix"
            group.attrs["encoding-version"] = "0.1.0"
            group.attrs["shape"] = np.array([n_rows, genes], dtype=np.int64)
            chunk = (min(max(nnz, 1), 1 << 18),)
            x_data = group.create_dataset("data", shape=(nnz,), dtype="f4",
                                          chunks=chunk)
            x_indices = group.create_dataset(
                "indices", shape=(nnz,),
                dtype="i4" if n_rows < 2 ** 31 else "i8", chunks=chunk)
            x_indptr = np.zeros(genes + 1, dtype=np.int64)
            stats = _GeneStats(genes, n_rows)
            cursor = 0
            for k in range(BUCKETS):
                path = buckets_dir / f"b{k:03d}.bin"
                if path.is_file():
                    records = np.fromfile(path, dtype=spill_dtype)
                    path.unlink()
                    # Stable: rows arrive ascending from the barcode-major
                    # source, so they stay ascending within every gene.
                    records = records[np.argsort(records["gene"], kind="stable")]
                else:
                    records = np.empty(0, dtype=spill_dtype)
                lo, hi = int(edges[k]), int(edges[k + 1])
                counts = np.bincount(records["gene"] - lo,
                                     minlength=hi - lo) if len(records) else \
                    np.zeros(hi - lo, dtype=np.int64)
                x_indptr[lo + 1:hi + 1] = cursor + np.cumsum(counts)
                if len(records):
                    x_data[cursor:cursor + len(records)] = records["value"]
                    x_indices[cursor:cursor + len(records)] = records["row"]
                    offsets = np.r_[0, np.cumsum(counts)]
                    for g in range(hi - lo):
                        if counts[g]:
                            stats.add(lo + g, records["value"][
                                offsets[g]:offsets[g + 1]])
                        else:
                            stats.add(lo + g, np.empty(0, np.float32))
                else:
                    for g in range(hi - lo):
                        stats.add(lo + g, np.empty(0, np.float32))
                cursor += len(records)
                if progress:
                    progress(steps + k + 1, steps + BUCKETS)
            group.create_dataset("indptr", data=x_indptr)

            var = pd.DataFrame(index=pd.Index(_deduplicate(names)))
            var["gene_ids"] = ids
            var["feature_types"] = kinds
            for name, values in stats.values.items():
                var[f"plx_{name}"] = values.astype(np.float64)
            write_elem(out, "var", var)
            write_elem(out, "obs", obs)
            obsm = {"spatial": spatial.astype(np.float64)}
            if array is not None:
                obsm["array"] = array
            if umap is not None:
                obsm["X_umap"] = umap
            write_elem(out, "obsm", obsm)
            write_elem(out, "varm", {"plx_hist": stats.hist,
                                     "plx_log_hist": stats.log_hist})
            for empty in ("layers", "obsp", "varp"):
                write_elem(out, empty, {})
            scalefactors = {}
            if layout.scalefactors is not None:
                try:
                    scalefactors = json.loads(
                        Path(layout.scalefactors).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    scalefactors = {}
            provenance = {
                "fingerprint": fingerprint,
                "kind": layout.kind,
                "rows": n_rows, "genes": genes, "nnz": nnz,
                "scalefactors": scalefactors,
                "source": str(layout.matrix),
            }
            write_elem(out, "uns", {"plexora_tenx": json.dumps(provenance)})
            out.attrs["plexora_tenx"] = json.dumps(provenance)
        os.replace(tmp, target)
    finally:
        shutil.rmtree(buckets_dir, ignore_errors=True)
        if tmp.exists():
            tmp.unlink()
    return target


def ensure_converted(layout, target, progress=None, stage=None):
    """`target`, converted now only when it is missing or stale."""
    if is_current(target, layout):
        return Path(target)
    return convert(layout, target, progress=progress, stage=stage)
