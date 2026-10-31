"""The classifier's results written into AnnData and SpatialData files.

Layers when every saved marker is a var name, obsm frames otherwise; only
this image's rows of the saved markers are written; a column for every
marker the classifier decides and every marker with a gating session's
layer, and `uns["plexora_classifier"]` (schema 2) says which source decides
each; a save is the gates op, marker batches, then a done op -- locally and
on a data node -- and one that fails half way leaves a readable file that
says it is incomplete; a SpatialData table is written the same way and stays
readable by spatialdata.
"""

from __future__ import annotations

import json

import anndata as ad
import numpy as np
import pytest

from plexora.plugins.gating.server.classifier import source_write as sw
from tests.node_harness import node_process  # noqa: F401 - fixture
from plexora.plugins.gating.tests.test_anndata_gates import (
    _feature_config, _make_multi_image_adata, _spatialdata_feature_config, _write_store,
    _consolidated_store)


def _payload(markers, n_rows=5, seed=0):
    rng = np.random.default_rng(seed)
    out = {}
    for marker in markers:
        calls = rng.integers(-1, 2, size=n_rows).astype(np.int8)
        scores = np.where(calls >= 0, rng.random(n_rows), np.nan).astype(np.float32)
        out[marker] = {"calls": sw.pack_array(calls), "scores": sw.pack_array(scores),
                       "positive": [1, 2], "negative": [3], "run": {"run_id": "abc"},
                       "_calls": calls, "_scores": scores}
    return {"n_rows": n_rows, "markers": out, "active": {m: "classifier" for m in markers},
            "segmentation_fingerprint": "seg", "qc_fingerprint": "", "qc_mode": "strict"}


def _image_rows(adata, image):
    return np.flatnonzero(adata.obs["imageid"].to_numpy() == image)


def test_var_aligned_markers_become_layers_and_only_this_images_rows(tmp_path):
    path = tmp_path / "multi.h5ad"
    adata = _make_multi_image_adata()
    adata.write_h5ad(path)
    x_before = adata.X.copy()

    payload = _payload(["marker_1", "marker_3"])
    result = sw.save_classifier(_feature_config(path, "B"), "ds_b", payload, "imageid")
    assert result["classifier_layout"] == "layers"
    assert result["classifier_binary"] == "layers/plexora_binary"

    back = ad.read_h5ad(path)
    binary, scores = back.layers["plexora_binary"], back.layers["plexora_scores"]
    assert binary.shape == back.shape and binary.dtype == np.int8
    rows = _image_rows(back, "B")
    np.testing.assert_array_equal(binary[rows, 1], payload["markers"]["marker_1"]["_calls"])
    np.testing.assert_allclose(scores[rows, 3], payload["markers"]["marker_3"]["_scores"],
                               equal_nan=True)
    # Everything else is "not classified".
    others = np.setdiff1d(np.arange(back.n_obs), rows)
    assert (binary[others] == -1).all() and np.isnan(scores[others]).all()
    assert (binary[:, [0, 2]] == -1).all()
    np.testing.assert_array_equal(back.X, x_before)

    uns = back.uns["plexora_classifier"]
    assert uns["layout"] == "layers" and int(uns["codes"]["not_classified"]) == -1
    record = json.loads(uns["images"]["B"])
    assert record["markers"]["marker_1"]["positive"] == [1, 2]
    assert record["active"] == {"marker_1": "classifier", "marker_3": "classifier"}

    # A second image is written beside the first, which is kept.
    second = _payload(["marker_1"], seed=1)
    sw.save_classifier(_feature_config(path, "C"), "ds_c", second, "imageid")
    again = ad.read_h5ad(path)
    np.testing.assert_array_equal(again.layers["plexora_binary"][rows, 1],
                                  payload["markers"]["marker_1"]["_calls"])
    np.testing.assert_array_equal(again.layers["plexora_binary"][_image_rows(again, "C"), 1],
                                  second["markers"]["marker_1"]["_calls"])
    assert set(again.uns["plexora_classifier"]["images"]) == {"B", "C"}


def test_a_marker_that_is_not_a_var_name_goes_to_obsm(tmp_path):
    path = tmp_path / "multi.h5ad"
    _make_multi_image_adata().write_h5ad(path)
    payload = _payload(["marker_0", "Area"])
    result = sw.save_classifier(_feature_config(path, "A"), "ds_a", payload, "imageid")
    assert result["classifier_layout"] == "obsm"
    back = ad.read_h5ad(path)
    frame = back.obsm["plexora_binary"]
    rows = _image_rows(back, "A")
    np.testing.assert_array_equal(frame["Area"].to_numpy()[rows],
                                  payload["markers"]["Area"]["_calls"])
    assert (frame["Area"].to_numpy()[np.setdiff1d(np.arange(back.n_obs), rows)] == -1).all()
    assert "plexora_binary" not in back.layers
    assert back.uns["plexora_classifier"]["note"]


def test_a_row_count_mismatch_is_refused(tmp_path):
    path = tmp_path / "multi.h5ad"
    _make_multi_image_adata().write_h5ad(path)
    with pytest.raises(ValueError, match="reopen"):
        sw.save_classifier(_feature_config(path, "A"), "ds_a", _payload(["marker_0"], n_rows=4),
                           "imageid")


@pytest.mark.parametrize("consolidated", [False, True])
def test_spatialdata_tables_get_the_same_layers(tmp_path, consolidated):
    import spatialdata as sd

    store = (_consolidated_store(tmp_path) if consolidated else
             _write_store(tmp_path / "s.zarr", {"cells": _make_multi_image_adata()}))
    payload = _payload(["marker_2"])
    result = sw.save_classifier(_spatialdata_feature_config(store, "cells", "B"), "ds_b",
                                payload, "imageid")
    assert result["classifier_layout"] == "layers"
    table = sd.read_zarr(store).tables["cells"]
    rows = _image_rows(table, "B")
    np.testing.assert_array_equal(np.asarray(table.layers["plexora_binary"])[rows, 2],
                                  payload["markers"]["marker_2"]["_calls"])
    assert "plexora_classifier" in table.uns


# -- a save of real classifier state: the gates, then marker by marker -------------------
#
# The synthetic project's table is a CSV; its classifier state is real (a
# panel run on DNA, a gating session's layer on CD8). `ds.table.run` is pointed
# at an h5ad holding the same cells (as image "synth", beside an image of
# someone else's), the way a node's dataset is the same project's table with
# the file beside it.


@pytest.fixture
def classified(tmp_path):
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.classifier import panel, runs
    from tests.agent_fixtures import local_handles, make_synthetic_project

    made = make_synthetic_project(tmp_path)
    ds = local_handles(made["name"])
    values = np.asarray(ds.table.columns(["CD8"])["CD8"], dtype=np.float64)
    low = float(np.nanmedian(values))
    model.set_gate(ds, "CD8", low, float(np.nanmax(values)) + 1.0)
    layer = runs.session_run(ds, "CD8", low=low, session_id="gs_x", c_add=0.4,
                             c_clean=0.4)["record"]
    for cell in made["positives"][:3]:
        panel.annotate(ds, "DNA", cell, "positive")
    for cell in made["negatives"][:3]:
        panel.annotate(ds, "DNA", cell, "negative")
    decided = runs.run(ds, "DNA", pixels=False)
    return {"made": made, "ds": ds, "low": low, "layer": layer, "decided": decided}


def _cohort_h5ad(path, n_rows, extra=3):
    import pandas as pd

    names = [f"synth_{i}" for i in range(n_rows)] + [f"other_{i}" for i in range(extra)]
    images = ["synth"] * n_rows + ["other"] * extra
    adata = ad.AnnData(X=np.zeros((len(names), 3), dtype=np.float32),
                       obs=pd.DataFrame({"imageid": images}, index=names),
                       var=pd.DataFrame(index=["DNA", "CD8", "CD3"]))
    adata.write_h5ad(path)
    return path


def _point_at(monkeypatch, ds, source, fail_on=None):
    """`ds.table.run` against `source`: what a node does with its own copy.
    `fail_on` = (operation, n): the n-th call of that operation is refused."""
    from types import SimpleNamespace

    from plexora.server.providers.operations import run_table_operation

    there = SimpleNamespace(name=ds.name, table=SimpleNamespace(source=source))
    seen = []

    def run(operation, payload=None):
        seen.append(operation)
        if fail_on and operation == fail_on[0] and seen.count(operation) == fail_on[1]:
            return {"ok": False, "reason": "invalid", "message": "the disk is full"}
        return run_table_operation(operation, there, json.loads(json.dumps(payload)))

    monkeypatch.setattr(ds.table, "run", run)
    return seen


def _payload_for_gates(classified):
    return {"image_id": "synth", "gates": {"CD8": classified["low"]}, "table_name": "gates",
            "imageid_column": "imageid",
            "provenance": [{"marker": "CD8", "value": classified["low"], "method": "manual",
                            "source": "threshold",
                            "classifier_run": classified["layer"]["run_id"]}]}


def _synth_rows(adata):
    return _image_rows(adata, "synth")


def test_a_save_writes_decided_markers_and_session_layers_with_their_provenance(
        tmp_path, monkeypatch, classified):
    from plexora.plugins.gating.server.classifier import calls

    ds = classified["ds"]
    n = int(ds.table.geometry().height)
    path = _cohort_h5ad(tmp_path / "cohort.h5ad", n)
    seen = _point_at(monkeypatch, ds, _feature_config(path, "synth"))
    result = sw.save_all(ds, _payload_for_gates(classified))
    assert result["ok"], result
    assert seen == ["gating.save_gates", "gating.save_classifier_marker",
                    "gating.save_classifier_done"]
    assert result["classifier_markers"] == ["CD8", "DNA"]
    assert result["classifier_decides"] == ["DNA"]
    assert result["classifier_session_layers"] == ["CD8"]
    assert result["classifier_layout"] == "layers" and result["n_active_gates"] == 1

    back = ad.read_h5ad(path)
    # Matrix form, two layers and no more: no override layer, no long form.
    assert {k for k in back.layers.keys() if str(k).startswith("plexora")} == {"plexora_binary",
                                                                              "plexora_scores"}
    rows = _synth_rows(back)
    binary = np.asarray(back.layers["plexora_binary"])
    _r, layer = calls.session_arrays(ds, "CD8")
    _r, decided = calls.run_arrays(ds, "DNA")
    np.testing.assert_array_equal(binary[rows, 1], layer["calls"])
    np.testing.assert_array_equal(binary[rows, 0], decided["calls"])
    assert (binary[_image_rows(back, "other")] == -1).all() and (binary[:, 2] == -1).all()

    uns = back.uns["plexora_classifier"]
    assert int(uns["schema_version"]) == 2 and "unless" in uns["note"]
    record = json.loads(uns["images"]["synth"])
    assert record["complete"] is True and record["schema_version"] == 2
    assert record["active"] == {"DNA": "classifier", "CD8": "threshold"}
    cd8, dna = record["markers"]["CD8"], record["markers"]["DNA"]
    compared = calls.compare(ds, "CD8")
    assert cd8["active"] == "threshold" and cd8["which"] == "session_run"
    assert cd8["origin"] == "session" and cd8["session_id"] == "gs_x"
    assert cd8["gate_used"][0] == classified["low"]
    assert cd8["call_rule"] == "gate_unless_confident" and cd8["c_clean"] == 0.4
    assert (cd8["n_added"], cd8["n_removed"]) == (compared["n_added"], compared["n_removed"])
    assert cd8["stale"] is None and set(cd8["versions"]) >= {"learner", "rules"}
    assert dna["active"] == "classifier" and dna["origin"] == "panel"
    assert dna["call_rule"] == "score_0.5" and dna["n_added"] is None
    assert sorted(dna["positive"]) == sorted(classified["made"]["positives"][:3])

    # The threshold stays the threshold, for every thresholded marker.
    gates = back.uns["gates"]
    assert gates.loc["CD8", "synth"] == pytest.approx(classified["low"])
    provenance = back.uns["gates_provenance"]
    assert provenance.loc["synth:CD8", "source"] == "threshold"
    assert provenance.loc["synth:CD8", "classifier_run"] == classified["layer"]["run_id"]


def test_the_panels_save_writes_the_latest_gates_their_provenance_and_every_version(
        tmp_path, monkeypatch, classified):
    # What /save_gates_to_anndata sends now: the latest gates, and beside them
    # `report.source_extras` -- provenance, and the versions kept.
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import report

    ds = classified["ds"]
    n = int(ds.table.geometry().height)
    path = _cohort_h5ad(tmp_path / "cohort.h5ad", n)
    _point_at(monkeypatch, ds, _feature_config(path, "synth"))
    first = model.save_version(ds)
    full = model.get_gate(ds, "DNA")
    dna = (full["default_low"] + full["default_high"]) / 2
    model.set_gate(ds, "DNA", dna, full["default_high"])
    second = model.save_version(ds)
    assert (first["version"], second["version"]) == (1, 2)
    extras = report.source_extras(ds)
    assert sorted(p["marker"] for p in extras["provenance"]) == ["CD8", "DNA"]
    assert [len(v["gates"]) for v in extras["versions"]] == [1, 2]
    gates = {m: low for m, (low, _high) in model.active_gates(ds).items()}
    result = sw.save_all(ds, {"image_id": "synth", "gates": gates, "table_name": "gates",
                              "imageid_column": "imageid", **extras})
    assert result["ok"], result
    back = ad.read_h5ad(path)
    assert back.uns["gates"].loc["DNA", "synth"] == pytest.approx(dna)
    assert set(back.uns["gates_provenance"].index) == {"synth:CD8", "synth:DNA"}
    versions = back.uns["gates_versions"]
    labels = [f"synth | {v['label']}" for v in extras["versions"]]
    assert list(versions.columns) == labels
    assert np.isnan(versions.loc["DNA", labels[0]])
    assert versions.loc["DNA", labels[1]] == pytest.approx(dna)
    assert versions.loc["CD8", labels[0]] == pytest.approx(classified["low"])
    # A marker no version gated: a row of the table, empty.
    assert np.isnan(versions.loc["CD3"]).all()


def test_markers_travel_in_batches_and_a_failed_one_leaves_a_readable_file(
        tmp_path, monkeypatch, classified):
    ds = classified["ds"]
    n = int(ds.table.geometry().height)
    path = _cohort_h5ad(tmp_path / "cohort.h5ad", n)
    monkeypatch.setattr(sw, "BATCH_BYTES", 1)
    seen = _point_at(monkeypatch, ds, _feature_config(path, "synth"),
                     fail_on=("gating.save_classifier_marker", 2))
    result = sw.save_all(ds, _payload_for_gates(classified))
    assert result["ok"] is False and "gates were saved" in result["message"]
    assert "the disk is full" in result["message"]
    assert result["classifier_markers"] == ["DNA"]
    assert seen.count("gating.save_classifier_marker") == 2
    assert "gating.save_classifier_done" not in seen

    back = ad.read_h5ad(path)
    record = json.loads(back.uns["plexora_classifier"]["images"]["synth"])
    assert record["complete"] is False and set(record["markers"]) == {"DNA"}
    assert back.uns["gates"].loc["CD8", "synth"] == pytest.approx(classified["low"])
    assert (np.asarray(back.layers["plexora_binary"])[:, 1] == -1).all()

    # Saved again: one request a marker, then the record made whole.
    seen = _point_at(monkeypatch, ds, _feature_config(path, "synth"))
    result = sw.save_all(ds, _payload_for_gates(classified))
    assert result["ok"] and result["classifier_requests"] == 3
    assert seen.count("gating.save_classifier_marker") == 2
    record = json.loads(ad.read_h5ad(path).uns["plexora_classifier"]["images"]["synth"])
    assert record["complete"] is True and set(record["markers"]) == {"CD8", "DNA"}


def test_a_marker_no_longer_classified_is_cleared_by_the_next_save(
        tmp_path, monkeypatch, classified):
    from plexora.plugins.gating.server.classifier import runs

    ds = classified["ds"]
    n = int(ds.table.geometry().height)
    path = _cohort_h5ad(tmp_path / "cohort.h5ad", n)
    _point_at(monkeypatch, ds, _feature_config(path, "synth"))
    assert sw.save_all(ds, _payload_for_gates(classified))["ok"]
    runs.revert(ds, "DNA")
    result = sw.save_all(ds, _payload_for_gates(classified))
    assert result["classifier_cleared"] == ["DNA"] and result["classifier_markers"] == ["CD8"]
    back = ad.read_h5ad(path)
    assert (np.asarray(back.layers["plexora_binary"])[:, 0] == -1).all()
    assert np.isnan(np.asarray(back.layers["plexora_scores"])[:, 0]).all()
    record = json.loads(back.uns["plexora_classifier"]["images"]["synth"])
    assert set(record["markers"]) == {"CD8"} and record["active"] == {"CD8": "threshold"}
    # Nothing classified: the image's record says so, its columns are cleared.
    runs.drop_session_run(ds, "CD8")
    result = sw.save_all(ds, _payload_for_gates(classified))
    assert result["ok"] and result["classifier_cleared"] == ["CD8"]
    back = ad.read_h5ad(path)
    assert json.loads(back.uns["plexora_classifier"]["images"]["synth"])["markers"] == {}
    assert (np.asarray(back.layers["plexora_binary"]) == -1).all()


@pytest.mark.parametrize("consolidated", [False, True])
def test_a_spatialdata_table_gets_the_session_layer_and_who_decides(
        tmp_path, monkeypatch, classified, consolidated):
    import spatialdata as sd
    import zarr

    from plexora.plugins.gating.server.classifier import calls

    ds = classified["ds"]
    n = int(ds.table.geometry().height)
    adata = ad.read_h5ad(_cohort_h5ad(tmp_path / "cohort.h5ad", n))
    store = _write_store(tmp_path / "s.zarr", {"cells": adata})
    if consolidated:
        zarr.consolidate_metadata(zarr.storage.LocalStore(store / "tables" / "cells"),
                                  zarr_format=3)
    _point_at(monkeypatch, ds, _spatialdata_feature_config(store, "cells", "synth"))
    result = sw.save_all(ds, _payload_for_gates(classified))
    assert result["ok"], result
    table = sd.read_zarr(store).tables["cells"]
    rows = _synth_rows(table)
    _r, layer = calls.session_arrays(ds, "CD8")
    np.testing.assert_array_equal(np.asarray(table.layers["plexora_binary"])[rows, 1],
                                  layer["calls"])
    record = json.loads(table.uns["plexora_classifier"]["images"]["synth"])
    assert record["markers"]["CD8"]["active"] == "threshold"
    assert record["markers"]["DNA"]["active"] == "classifier"
    assert table.uns["gates_provenance"].loc["synth:CD8", "source"] == "threshold"


def test_a_save_with_nothing_classified_leaves_a_file_without_a_record_untouched(tmp_path):
    path = tmp_path / "multi.h5ad"
    _make_multi_image_adata().write_h5ad(path)
    result = sw.save_done(_feature_config(path, "A"), "ds_a",
                          {"n_rows": 5, "save_id": "s1", "markers": [], "image": {}},
                          "imageid")
    assert result == {"classifier_markers": [], "classifier_cleared": []}
    assert "plexora_classifier" not in ad.read_h5ad(path).uns


def test_the_done_op_refuses_a_marker_this_save_did_not_write(tmp_path):
    path = tmp_path / "multi.h5ad"
    _make_multi_image_adata().write_h5ad(path)
    with pytest.raises(ValueError, match="not written by this save"):
        sw.save_done(_feature_config(path, "A"), "ds_a",
                     {"n_rows": 5, "save_id": "s1", "markers": ["marker_0"], "image": {}},
                     "imageid")


# -- a table on a data node --------------------------------------------------------------


def test_the_chunked_save_runs_on_a_data_node(tmp_path, monkeypatch, node_process):
    """The ops are registered where the node runs them, and the payload
    survives the wire: one request a marker batch, then the done op."""
    from plexora import api, app
    from tests.test_node_routes import _image_file, _multi_image_h5ad

    path = _multi_image_h5ad(tmp_path)
    node = node_process(f"table:cells={path}")
    client = app.test_client()
    client.post("/settings/nodes", json={"name": "laptop", "endpoint": node.endpoint,
                                          "token": node.token})
    answer = client.post("/import/sample", json={
        "paths": [str(_image_file(tmp_path)), "node://laptop/cells"], "name": "one-slide",
        "answers": {"subset_column": "image_id", "subset_value": "image_02"}})
    assert answer.status_code == 200, answer.get_data(as_text=True)
    ds = api.project_data("one-slide")
    assert not ds.table.locator.is_local, "the table is the node's"
    n = int(ds.table.geometry().height)
    rng = np.random.default_rng(3)
    fake = [{"marker": m, "calls": rng.integers(-1, 2, n).astype(np.int8),
             "scores": rng.random(n).astype(np.float32),
             "record": {"active": active, "which": which, "run_id": f"r{i}",
                        "origin": "panel" if active == "classifier" else "session"}}
            for i, (m, which, active) in enumerate((("protein_0", "run", "classifier"),
                                                    ("protein_1", "session_run",
                                                     "threshold")))]
    monkeypatch.setattr(sw, "candidates", lambda ds_: {
        e["marker"]: (e["record"]["which"], e["record"]["active"] == "classifier")
        for e in fake})
    monkeypatch.setattr(sw, "entries", lambda ds_, not_written=None: iter(fake))
    monkeypatch.setattr(sw, "_image_meta", lambda ds_, written, not_written: {
        "active": {m: r["active"] for m, r in written.items()}, "not_written": {}})
    monkeypatch.setattr(sw, "BATCH_BYTES", 1)
    result = sw.save_all(ds, {"image_id": "one-slide", "gates": {"protein_0": 0.5},
                              "table_name": "gates", "imageid_column": "image_id"})
    assert result["ok"], result
    assert result["classifier_requests"] == 3
    assert result["classifier_decides"] == ["protein_0"]
    back = ad.read_h5ad(path)
    rows = np.flatnonzero(back.obs["image_id"].to_numpy() == "image_02")
    binary = np.asarray(back.layers["plexora_binary"])
    np.testing.assert_array_equal(binary[rows, 0], fake[0]["calls"])
    np.testing.assert_array_equal(binary[rows, 1], fake[1]["calls"])
    record = json.loads(back.uns["plexora_classifier"]["images"]["image_02"])
    assert record["complete"] is True
    assert record["active"] == {"protein_0": "classifier", "protein_1": "threshold"}
    assert back.uns["gates"].loc["protein_0", "image_02"] == pytest.approx(0.5)
