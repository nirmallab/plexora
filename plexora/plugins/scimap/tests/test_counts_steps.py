"""The counts steps with real Scimap Pro: Normalize, PCA & neighbours, then Clustering and Spatially variable
genes, on a small table shaped like a Xenium run (raw counts, a targeted panel, two cell kinds placed in two halves
of the tissue). Every call is Scimap Pro's; on a release without `sm.ai.recipe` the steps run with the tool
table's own defaults.
"""

from __future__ import annotations

import anndata as ad
import numpy as np
import pandas as pd
import pytest
import scipy.sparse as sparse

from plexora.plugins.scimap.server import adapter, results, tools, working

pytest.importorskip("scimappro")


@pytest.fixture
def counts_file(tmp_path):
    rng = np.random.default_rng(0)
    n, g = 600, 40
    x = rng.uniform(0, 1000, n)
    left = x < 500
    rates = np.full((n, g), 0.5)
    rates[left, :10] = 6.0          # genes 0-9 high on the left: patterned in space
    rates[~left, 10:20] = 6.0       # genes 10-19 high on the right
    counts = rng.poisson(rates * rng.uniform(0.5, 2.0, (n, 1)))
    adata = ad.AnnData(X=sparse.csr_matrix(counts.astype("float32")),
                       obs=pd.DataFrame({"imageid": pd.Categorical(["s1"] * n), "X_centroid": x,
                                         "Y_centroid": rng.uniform(0, 1000, n)},
                                        index=[f"c{i}" for i in range(n)]),
                       var=pd.DataFrame(index=[f"GENE{i}" for i in range(g)]))
    path = tmp_path / "work.h5ad"
    adata.write_h5ad(path)
    return path


def _work(path):
    info = working.describe(str(path), refresh=True)
    return {**info, "exists": True, "path": str(path), "image_column": "imageid", "x_column": "X_centroid",
            "y_column": "Y_centroid", "data_mode": "st-cell", "coordinate_unit": "px"}


def _run(path, eff):
    for step in eff.get("before") or []:
        adapter.execute(str(path), step["key"], step["kwargs"], label=step.get("label"),
                        writes=step.get("writes") or ())
    return adapter.execute(str(path), eff["key"], eff["kwargs"], label=eff.get("label"))


def test_normalize_then_cluster_and_rank_spatial_genes(counts_file):
    from plexora.plugins.scimap.server import install

    if not install.supports("advice"):
        pytest.skip("the counts steps need a Scimap Pro with sm.ai.recipe (and graphs that flush in memory)")
    document = {"cells": []}
    work = _work(counts_file)
    assert tools.availability("cluster", document, work, {"installed": True, "compatible": True})["reason"] == \
        "Requires normalized counts"

    eff = tools.effective("normalize", {"target": 1000}, work, document, "n1")
    assert eff["before"][0]["key"] == "pp.normalizeTotal" and eff["kwargs"]["targetLayer"] == "log"
    _run(counts_file, eff)
    work = _work(counts_file)
    assert "log" in work["layers"]
    assert "Counts scaled to 1,000 per cell" in results.summarize("normalize", eff, str(counts_file),
                                                                   work)["headline"]["strong"]

    assert tools.availability("cluster", document, work, {"installed": True, "compatible": True})["reason"] == \
        "Requires PCA & neighbours"
    eff = tools.effective("pca", {"components": 10, "variable": False}, work, document, "p1")
    assert [s["key"] for s in eff["before"]] == ["pp.pca"] and eff["key"] == "pp.neighbors"
    assert eff["before"][0]["kwargs"]["layer"] == "log"
    _run(counts_file, eff)
    work = _work(counts_file)
    assert "X_pca" in work["obsm"]

    eff = tools.effective("cluster", {"resolution": 0.5}, work, document, "c1")
    assert eff["kwargs"]["layer"] == "log" and eff["kwargs"]["log"] is False
    _run(counts_file, eff)
    work = _work(counts_file)
    assert eff["label"] in {c["name"] for c in work["columns"]}

    # A radius is in the coordinates' own units, as for images; nothing is converted in Plexora.
    eff = tools.effective("svg", {"graph": "radius", "radius": 30}, work, document, "s1")
    assert eff["kwargs"]["radius"] == 30 and eff["kwargs"]["layer"] == "log"
    assert "Radius 30 px" in tools.chips("svg", eff, work)
    _run(counts_file, eff)
    if not install.supports("svg_plot"):
        pytest.skip("this Scimap Pro has no pl.spatiallyVariableFeatures to summarize with")
    body = results.summarize("svg", eff, str(counts_file), _work(counts_file))
    top = set(body["tops"][:10])
    assert len(top & {f"GENE{i}" for i in range(20)}) >= 8, body["tops"]
    assert body["table"]["rows"]


def test_imaging_steps_are_unchanged_by_the_counts_rules(counts_file):
    work = {**_work(counts_file), "data_mode": "imaging", "markers": ["DNA1", "CD3", "CD20"]}
    eff = tools.effective("interaction", {"radius": 30, "source": None}, {**work, "columns": [
        {"name": "kind", "kind": "category", "n": 2, "categories": ["a", "b"]}]}, {"cells": []}, "i1")
    assert eff["kwargs"]["radius"] == 30
    assert tools._markers(None, work) == ["CD3", "CD20"]
    assert tools._markers(None, {**work, "data_mode": "st-cell", "markers": ["DNAJB1", "CD3"]}) is None
