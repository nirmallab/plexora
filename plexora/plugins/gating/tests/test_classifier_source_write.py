"""The classifier's results written into AnnData and SpatialData files.

Layers when every classified marker is a var name, obsm frames otherwise;
only this image's rows of the saved markers are written; uns says what the
codes mean and records each image's examples and run; a SpatialData table is
written the same way and stays readable by spatialdata.
"""

from __future__ import annotations

import json

import anndata as ad
import numpy as np
import pytest

from plexora.plugins.gating.server.classifier import source_write as sw
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
