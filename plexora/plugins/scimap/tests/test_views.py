"""The views of a step's result: the catalogue, the settings kept with the
notebook, and that switching or narrowing a view never runs the step again.

The first tests read only server/views.py and server/figures.py (no Scimap Pro
import); the flow at the end drives the routes the page uses with the real
Scimap Pro, skipped where it is not installed.
"""

from __future__ import annotations

import base64
import json

import numpy as np
import pytest

from plexora.plugins.scimap.server import advanced, figures, views

WORK = {
    "image_column": "imageid", "x_column": "X_centroid", "y_column": "Y_centroid",
    "images": ["a", "b", "c"], "markers": ["DNA1", "CD3", "CD20", "CD68"], "raw": True,
    "columns": [
        {"name": "imageid", "kind": "category", "n": 3, "categories": ["a", "b", "c"]},
        {"name": "kmeans", "kind": "category", "n": 4, "categories": ["0", "1", "2", "3"]},
        {"name": "patient", "kind": "category", "n": 2, "categories": ["P1", "P2"]},
        {"name": "nbh", "kind": "category", "n": 3, "categories": ["0", "1", "2"]},
    ],
}


def _ctx(tool, label=None, source=None):
    return views.context(tool, {"label": label, "inputs": {"source": source} if source else {}}, WORK)


def test_every_tool_offers_its_default_first_and_one_vocabulary_of_controls():
    every = set(views.controls(_ctx("cluster", "kmeans")))
    for tool, label, source in [("cluster", "kmeans", None), ("composition", None, "kmeans"),
                                ("expression", None, "kmeans"), ("distance", "spatialDistance", "kmeans"),
                                ("interaction", "spatialCooccurrence", "kmeans"), ("neighborhoods", "nbh", "kmeans"),
                                ("name_groups", "nbh_named", "nbh"), ("umap", "umap", None),
                                ("markers", "rankFeatures", "kmeans"), ("compare", "compareGroups", "kmeans"),
                                ("quality", "spatialOutlier", None)]:
        c = _ctx(tool, label, source)
        cat = views.catalogue(tool, c)
        ids = [v["id"] for v in cat["views"]]
        defaults = [p["view"] for p in views.default_panels(tool, c)]
        assert defaults and defaults[0] == ids[0], tool
        assert set(defaults) <= set(ids), tool
        # Each control a view names exists, in a known section.
        for v in cat["views"]:
            assert set(v["controls"]) <= every, (tool, v["id"])
            assert set(v["quick"]) <= set(v["controls"]) | {"markers"}, (tool, v["id"])
        assert all(c["section"] in views.SECTIONS for c in cat["controls"].values())


def test_the_catalogue_drops_views_the_data_cannot_have():
    one = dict(WORK, images=["a"], columns=[c for c in WORK["columns"] if c["name"] != "patient"])
    c = views.context("composition", {"inputs": {"source": "kmeans"}}, one)
    ids = [v.id for v in views.offered("composition", c)]
    assert "where" not in ids and "similarity" not in ids and "grouped" not in ids
    many = [v.id for v in views.offered("composition", _ctx("composition", None, "kmeans"))]
    assert {"where", "grouped", "similarity"} <= set(many)
    # Distance by image needs more than one image.
    assert "byimage" not in [v.id for v in views.offered("distance", views.context(
        "distance", {"label": "spatialDistance", "inputs": {"source": "kmeans"}}, one))]


def test_default_panels_are_named_after_their_view_so_figure_urls_stay_stable():
    assert views.default_panels("cluster") == [{"id": "heatmap", "view": "heatmap", "opts": {}},
                                               {"id": "sizes", "view": "sizes", "opts": {}}]


def test_kept_settings_are_cleaned_to_known_views_and_keys():
    kept = views.clean_panels("distance", [
        {"id": "matrix", "view": "matrix", "opts": {"from": ["1"], "evil": "__import__('os')", "points": 2}},
        {"id": "x", "view": "not_a_view"},
        {"id": "matrix", "view": "distribution"},  # a repeated id is dropped
        {"id": "p2", "view": "tissue", "opts": {"target": "2", "image": None}},
        {"id": "p3", "view": "distribution"},
    ])
    assert [p["id"] for p in kept] == ["matrix", "p2", "p3"]
    assert kept[0]["opts"] == {"from": ["1"], "points": 2.0}
    assert kept[1]["opts"] == {"target": "2"}
    assert len(views.clean_panels("distance", [{"view": "matrix"}] * 9)) <= views.MAX_PANELS


def test_a_view_maps_its_settings_onto_one_scimappro_call():
    c = _ctx("distance", "spatialDistance", "kmeans")
    view = views.find("distance", "distribution", c)
    spec = views.spec("distance", view, c, {"from": ["2"], "to": ["0", "3"], "split": "imageid", "scale": "log"})
    assert spec["fn"] == "spatialDistance"
    kw = spec["kwargs"]
    assert kw["kind"] == "distribution" and kw["distanceFrom"] == ["2"] and kw["distanceTo"] == ["0", "3"]
    assert kw["facetBy"] == "imageid" and kw["log"] is True and kw["phenotype"] == "kmeans"
    assert spec["title"] == "Distance from each cell of 2 to the nearest 0, 3"
    matrix = views.spec("distance", views.find("distance", "matrix", c), c, {"images": ["b"], "averaged": "cell"})
    assert matrix["kwargs"]["subsetCol"] == "imageid" and matrix["kwargs"]["subsetValue"] == ["b"]
    assert matrix["kwargs"]["unit"] == "cell" and matrix["title"].startswith("Mean")
    # The copyable line is the call a script would make.
    assert figures.call_text(matrix).startswith("sm.pl.spatialDistance(adata, kind='heatmap'")


def test_a_view_says_what_it_draws_by_default_when_that_is_not_the_first_choice():
    c = _ctx("cluster", "kmeans")
    cat = {v["id"]: v for v in views.catalogue("cluster", c)["views"]}
    assert cat["heatmap"]["defaults"]["order"] == "cluster"
    assert cat["sizes"]["defaults"]["bars"] == "absolute"
    assert cat["violin"]["defaults"]["log"] is True  # raw intensities
    assert cat["heatmap"]["defaults"]["layer"] == "X" and cat["heatmap"]["defaults"]["log"] is False
    heat = views.spec("cluster", views.find("cluster", "heatmap", c), c, {})
    assert heat["kwargs"]["clusterRows"] is True
    assert views.spec("cluster", views.find("cluster", "heatmap", c), c, {"order": "name"})["kwargs"]["clusterRows"] is False


def test_heatmaps_and_distributions_read_a_chosen_layer_and_log_it_when_asked():
    c = _ctx("cluster", "kmeans")
    heat = views.spec("cluster", views.find("cluster", "heatmap", c), c, {"layer": "raw", "log": True})
    assert heat["kwargs"]["layer"] == "raw" and heat["kwargs"]["_log"] is True
    # Scimap Pro's heatmap has no log: the copyable call says how it was drawn.
    assert figures.call_text(heat).endswith("# on log1p of the layer's values, made in memory")
    assert "_log" not in figures.call_text(heat)
    # A layer the file does not have falls back to the default.
    assert views.spec("cluster", views.find("cluster", "heatmap", c), c, {"layer": "nope"})["kwargs"]["layer"] is None
    violin = views.spec("cluster", views.find("cluster", "violin", c), c, {"layer": "X", "log": False})
    assert violin["kwargs"]["layer"] == "X" and violin["kwargs"]["log"] is False
    # Kept settings from before the Log switch still read.
    assert views.spec("cluster", views.find("cluster", "violin", c), c, {"scale": "linear"})["kwargs"]["log"] is False
    e = _ctx("expression", None, "kmeans")
    for view in ("histograms", "two", "correlation"):
        kw = views.spec("expression", views.find("expression", view, e), e, {"layer": "raw", "log": True})["kwargs"]
        assert kw["layer"] == "raw" and kw["_log"] is True, view
    assert views.clean_opts({"layer": "raw", "log": True}) == {"layer": "raw", "log": True}


def test_a_logged_view_reads_scimap_pros_log1p_of_the_layer(tmp_path):
    ad = pytest.importorskip("anndata")
    import pandas as pd

    X = np.arange(12, dtype="float32").reshape(4, 3)
    obs = pd.DataFrame({"kmeans": ["0", "1", "0", "1"], "imageid": ["a"] * 4}, index=[f"c{i}" for i in range(4)])
    adata = ad.AnnData(X=X.copy(), obs=obs, var=pd.DataFrame(index=["CD3", "CD20", "CD68"]))
    adata.raw = adata.copy()
    adata.X = np.zeros_like(X)
    path = tmp_path / "w.h5ad"
    adata.write_h5ad(path)
    from plexora.plugins.scimap.server.adapter import scimappro

    sm = scimappro()
    held = figures._logged(sm, str(path), "raw")
    # pp.log1p made the layer, in memory: the file is left as it was.
    assert np.allclose(np.asarray(held.layers[figures.LOG_LAYER]), np.log1p(X))
    assert figures._logged(sm, str(path), "raw") is held
    whole = figures._logged(sm, adata, "X")
    assert float(np.asarray(whole.layers[figures.LOG_LAYER]).max()) == 0.0
    back = ad.read_h5ad(path)
    assert figures.LOG_LAYER not in back.layers


def test_a_density_is_drawn_without_cell_gaps_and_a_table_of_categories_with_them():
    z = [[1.0, 2.0], [3.0, 4.0]]
    dens, _ = figures.restyle({"data": [{"type": "heatmap", "x": [0.1, 0.2], "y": [0.1, 0.2], "z": z, "xgap": 1}],
                               "layout": {"height": 900}}, {})
    assert "xgap" not in dens["data"][0] and dens["layout"]["height"] <= 460
    table, _ = figures.restyle({"data": [{"type": "heatmap", "x": ["CD3", "CD20"], "y": ["0", "1"], "z": z}],
                                "layout": {"height": 900}}, {})
    assert table["data"][0]["xgap"] == 1 and table["layout"]["height"] == 170  # rows*30+96, at least 170


def test_a_dot_plots_sizes_are_kept_for_the_page_to_scale():
    sizes = [5.0, 12.0, 23.0]
    fig, _ = figures.restyle({"data": [{"type": "scatter", "mode": "markers", "x": ["a", "b", "c"], "y": ["0", "0", "0"],
                                        "marker": {"size": sizes}}], "layout": {}}, {}, {"points": 3})
    assert fig["data"][0]["marker"]["size"] == sizes


def test_advanced_offers_every_plotting_function_a_view_can_call():
    keys = [f["key"] for f in advanced.functions("cluster")]
    assert keys[0] == "tl.cluster"
    assert {"pl.heatmap", "pl.dotplot", "pl.violin", "pl.spatialScatterPlot", "pl.barplot"} <= set(keys)


def _bdata(values, dtype="f8"):
    arr = np.asarray(values, dtype=dtype)
    return {"dtype": dtype, "bdata": base64.b64encode(arr.tobytes()).decode()}


def test_a_large_spread_is_drawn_from_a_fixed_sample_of_its_values():
    n = figures.SPREAD_POINTS * 4
    values = np.random.default_rng(1).gamma(2.0, 10.0, n)
    data = [{"type": "violin", "y": _bdata(values), "x": ["k"] * n, "name": "k"},
            {"type": "violin", "y": [1.0, 2.0, 3.0], "name": "small"}]
    note = figures._thin(data)
    assert str(n // 1000) in note.replace(",", "") and "every cell" in note
    kept = figures._decode(data[0]["y"])
    assert len(kept) == figures.SPREAD_POINTS and len(data[0]["x"]) == figures.SPREAD_POINTS
    assert abs(np.median(kept) - np.median(values)) < 0.05 * np.median(values)
    assert data[1]["y"] == [1.0, 2.0, 3.0]
    again = [{"type": "violin", "y": _bdata(values), "x": ["k"] * n}]
    figures._thin(again)
    assert again[0]["y"] == data[0]["y"]  # the same sample every time


# -- over the routes, with the real Scimap Pro ---------------------------------------


@pytest.fixture
def flow(tmp_path, monkeypatch):
    pytest.importorskip("scimappro")
    import plexora
    from plexora.plugins.scimap.tests import test_scimap_flow as base
    from plexora.server import plugins as plugin_registry
    from tests.helpers import ALL_CONFIRMED, csv_spec, image_spec, project, use_data_root

    use_data_root(monkeypatch, tmp_path)
    table = tmp_path / "tables" / "cells.csv"
    table.parent.mkdir()
    base._table(table)
    spec = csv_spec(str(table), markers=base.MARKERS)
    (tmp_path / "config.json").write_text(json.dumps({
        "tonsil": project("tonsil", dataset=spec, image=image_spec(channels=("DNA", "CD3")),
                          confirmed=ALL_CONFIRMED).to_entry(),
    }), encoding="utf-8")
    if plugin_registry.find(plexora.app, "scimap") is None:  # pragma: no cover
        pytest.skip("scimap is not installed")
    client = plexora.app.test_client()
    made = base._ok(client.post("/plugins/scimap/api/notebooks", json={"kind": "project", "ref": "tonsil"}))
    nb = made["notebook_id"]
    base._run(client, nb, made["view"]["notebook"]["cells"][0]["id"])
    cluster = base._add(client, nb, "cluster", {"method": "kmeans", "k": 4})
    base._run(client, nb, cluster)
    return client, nb, cluster, base


def test_switching_and_narrowing_a_view_never_reruns_the_step(flow):
    client, nb, cluster, base = flow
    url = f"/plugins/scimap/api/notebooks/{nb}"
    before = next(c for c in base._ok(client.get(url))["view"]["notebook"]["cells"] if c["id"] == cluster)
    figs = base._ok(client.get(f"{url}/cells/{cluster}/figures"))["figures"]
    assert [v["id"] for v in figs["catalogue"]["views"]][:2] == ["heatmap", "dotplot"]

    violin = base._ok(client.post(f"{url}/cells/{cluster}/draw",
                                  json={"view": "violin", "opts": {"markers": ["CD3"]}}))["figure"]
    assert not violin.get("error"), violin
    assert violin["figure"]["data"][0]["type"] == "violin" and "CD3" in violin["title"]
    assert violin["call"].startswith("sm.pl.violin(adata, keys=['CD3']")
    assert client.post(f"{url}/cells/{cluster}/draw", json={"view": "nope"}).status_code == 400

    # The panels are kept with the notebook as display settings.
    base._ok(client.patch(url, json={"ops": [{"op": "set_views", "id": cluster, "views": [
        {"id": "heatmap", "view": "violin", "opts": {"markers": ["CD3"]}},
        {"id": "p2", "view": "sizes", "opts": {"bars": "percent"}}]}]}))
    after = next(c for c in base._ok(client.get(url))["view"]["notebook"]["cells"] if c["id"] == cluster)
    assert after["state"] == "done" and not after.get("stale")
    assert after["run"]["run_id"] == before["run"]["run_id"]
    assert [p["view"] for p in after["views"]] == ["violin", "sizes"]
    figs = base._ok(client.get(f"{url}/cells/{cluster}/figures"))["figures"]
    assert [(f["name"], f["view"]) for f in figs["figures"]] == [("heatmap", "violin"), ("p2", "sizes")]

    # A download draws the view as the page shows it, through matplotlib.
    png = client.get(f"{url}/cells/{cluster}/figures/heatmap.png?view=violin&opts="
                     + json.dumps({"markers": ["CD3"]}))
    assert png.status_code == 200 and png.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert "violin" in png.headers["Content-Disposition"]
