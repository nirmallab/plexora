"""The 10x matrix conversion: gene-major, every entry kept, described once.

Against the dense truth the fixture wrote, never against a re-read of the
source: a converter that transposed wrong would read back consistently wrong.
"""

import json
import os

import h5py
import numpy as np
import pytest

from plexora.server.models import data_model
from plexora.server.utils import tenx_matrix as tm

from tests.tenx_fixtures import write_visium_hd_run


@pytest.fixture
def run(tmp_path):
    return write_visium_hd_run(tmp_path, grid=40, levels=(2, 8),
                               segmented=True)


def _convert(truth, size, tmp_path, monkeypatch, **kw):
    for key, value in kw.items():
        monkeypatch.setattr(tm, key, value)
    level = truth["levels"][size]
    layout = tm.layout_for(level["dir"] / "filtered_feature_bc_matrix.h5")
    target = tmp_path / f"converted_{size}.h5ad"
    tm.convert(layout, target)
    return layout, target


def test_the_layout_is_found_beside_the_matrix(run):
    outs, truth = run
    layout = tm.layout_for(truth["levels"][8]["dir"] /
                           "filtered_feature_bc_matrix.h5")
    assert layout.kind == "bins"
    assert layout.positions.name == "tissue_positions.parquet"
    assert [tm.clustering_name(p) for p in layout.clusterings] == ["graphclust"]
    assert layout.umap is not None
    cells = tm.layout_for(outs / "segmented_outputs" /
                          "filtered_feature_cell_matrix.h5")
    assert cells.kind == "cells" and cells.geojson.name.endswith(".geojson")
    assert tm.is_tenx_matrix(layout.matrix)
    assert not tm.is_tenx_matrix(layout.positions)


@pytest.mark.parametrize("chunk, buckets", [(7, 13), (1 << 20, 48)])
def test_x_is_the_matrix_gene_major(run, tmp_path, monkeypatch, chunk, buckets):
    import anndata as ad

    _, truth = run
    _, target = _convert(truth, 8, tmp_path, monkeypatch,
                         CHUNK_ENTRIES=chunk, BUCKETS=buckets)
    adata = ad.read_h5ad(target)
    level = truth["levels"][8]
    expected = level["pooled"][:, level["rows"], level["cols"]].T
    assert adata.X.format == "csc"
    assert adata.X.has_sorted_indices
    assert np.array_equal(adata.X.toarray(), expected)
    assert list(adata.obs_names) == level["barcodes"]
    # Duplicate symbols are made unique, the ids are kept as they were.
    assert list(adata.var_names)[-1] == "INS-1"
    assert adata.var["gene_ids"].iloc[0].startswith("ENSG")


def test_obs_and_obsm_place_every_bin(run, tmp_path, monkeypatch):
    import anndata as ad

    _, truth = run
    _, target = _convert(truth, 8, tmp_path, monkeypatch)
    adata = ad.read_h5ad(target)
    level = truth["levels"][8]
    assert np.array_equal(adata.obs["array_row"], level["rows"])
    assert np.array_equal(adata.obs["array_col"], level["cols"])
    assert (adata.obs["in_tissue"] == 1).all()
    a, b, c, d, e, f = level["transform"]
    x = a * (level["cols"] + 0.5) + c * (level["rows"] + 0.5) + e
    assert np.allclose(adata.obsm["spatial"][:, 0], x)
    assert adata.obs["graphclust"].dtype.name == "category"
    assert adata.obsm["X_umap"].shape == (len(level["barcodes"]), 2)


def test_every_gene_is_described_as_describe_column_would(run, tmp_path,
                                                            monkeypatch):
    import anndata as ad

    _, truth = run
    _, target = _convert(truth, 8, tmp_path, monkeypatch, BUCKETS=5)
    adata = ad.read_h5ad(target)
    dense = adata.X.toarray()
    for gene in range(dense.shape[1]):
        column = dense[:, gene].astype(np.float32)
        expected = data_model._describe_column(column)
        row = adata.var.iloc[gene]
        assert row["plx_mean"] == pytest.approx(expected["mean"], rel=1e-6)
        assert row["plx_std"] == pytest.approx(expected["std"], rel=1e-6)
        for key in ("min", "max"):
            assert row[f"plx_{key}"] == pytest.approx(expected[key])
        for key, name in (("25%", "q25"), ("50%", "q50"), ("75%", "q75")):
            assert row[f"plx_{name}"] == pytest.approx(expected[key])
        densities = [bin_["y"] for bin_ in expected["histogram"]]
        assert np.allclose(adata.varm["plx_hist"][gene], densities, rtol=1e-5)
        logged = data_model._describe_column(np.log1p(column))
        assert row["plx_log_mean"] == pytest.approx(logged["mean"], rel=1e-5)
        assert row["plx_log_q75"] == pytest.approx(logged["75%"], rel=1e-5)


def test_segmented_cells_carry_their_polygon_id_and_centroid(run, tmp_path):
    import anndata as ad

    outs, truth = run
    layout = tm.layout_for(outs / "segmented_outputs" /
                           "filtered_feature_cell_matrix.h5")
    target = tm.convert(layout, tmp_path / "cells.h5ad")
    adata = ad.read_h5ad(target)
    assert np.array_equal(adata.obs["cell_id"], truth["cells"]["ids"])
    assert np.allclose(adata.obsm["spatial"], truth["cells"]["centres"])
    assert np.array_equal(adata.X.toarray(), truth["cells"]["counts"])


def test_centroids_read_by_pattern_match_a_full_parse(run, tmp_path):
    outs, truth = run
    geojson = outs / "segmented_outputs" / "cell_segmentations.geojson"
    ids, x, y = tm.read_cell_centroids(geojson)
    assert np.array_equal(ids, truth["cells"]["ids"])
    # Reordered properties defeat the pattern, and the full parse answers.
    doc = json.loads(geojson.read_text())
    for feature in doc["features"]:
        props = feature["properties"]
        feature["properties"] = {"cell_centroid": props["cell_centroid"],
                                 "cell_id": props["cell_id"]}
    other = tmp_path / "reordered.geojson"
    other.write_text(json.dumps(doc))
    ids2, x2, y2 = tm.read_cell_centroids(other)
    assert np.array_equal(ids2, ids) and np.allclose(x2, x)


def test_a_conversion_is_reused_until_the_source_changes(run, tmp_path,
                                                         monkeypatch):
    _, truth = run
    layout, target = _convert(truth, 8, tmp_path, monkeypatch)
    assert tm.is_current(target, layout)
    stamp = target.stat().st_mtime_ns
    tm.ensure_converted(layout, target)
    assert target.stat().st_mtime_ns == stamp
    stat = layout.matrix.stat()
    os.utime(layout.matrix, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10 ** 9))
    assert not tm.is_current(target, layout)


def test_barcodes_parse_by_offset(run):
    _, truth = run
    level = truth["levels"][2]
    with h5py.File(level["dir"] / "filtered_feature_bc_matrix.h5") as handle:
        barcodes = handle["matrix"]["barcodes"][:]
    rows, cols = tm.parse_bin_barcodes(barcodes)
    assert np.array_equal(rows, level["rows"]) and np.array_equal(cols, level["cols"])
    with pytest.raises(ValueError):
        tm.parse_bin_barcodes(np.array([b"ACGT-1"]))
    assert tm.matrix_summary(level["dir"] / "filtered_feature_bc_matrix.h5")[
        "bin_um"] == 2
