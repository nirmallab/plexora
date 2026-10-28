"""A matrix already log-transformed and read as it is (log1p off) is gated on
its own scale, never log1p'd a second time.

The import reads a SpatialData `log` layer with the log1p switch off -- right,
since the values are logged already -- and gating used to take the switch at
its word: "not transformed" meant "raw", so it fitted log1p(log). On
ImageLSP23362 (2026-10-28) that compressed every marker's candidates until two
or three were left, and 14 of 15 gates came out low.
"""

import json
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import pytest
import spatialdata as sd
import tifffile

from plexora.agent import expression
from tests.helpers import ALL_CONFIRMED, anndata_spec, image_spec, project

MARKERS = ("CD3", "CD8", "CD45", "PanCK")
SIZE = 64


def _raw_intensities(n=4000, seed=0):
    """Per-cell mean intensities: a log-normal background and a brighter
    positive population for each marker."""
    rng = np.random.default_rng(seed)
    columns = []
    for k in range(len(MARKERS)):
        positive = rng.random(n) < 0.1 + 0.05 * k
        columns.append(np.where(positive, rng.lognormal(7.5, 0.3, n),
                                rng.lognormal(5.5, 0.3, n)))
    return np.stack(columns, axis=1).astype(np.float32)


@pytest.fixture
def store(tmp_path):
    """A SpatialData store whose table holds raw intensities in X and their
    log1p in a `log` layer, as scimap writes it."""
    raw = _raw_intensities()
    n = raw.shape[0]
    adata = ad.AnnData(X=raw, obs=pd.DataFrame({"CellID": np.arange(1, n + 1)},
                                               index=[str(i) for i in range(1, n + 1)]),
                       var=pd.DataFrame(index=list(MARKERS)),
                       layers={"log": np.log1p(raw)})
    rng = np.random.default_rng(1)
    adata.obsm["spatial"] = rng.random((n, 2)) * SIZE
    path = Path(tmp_path) / "cells.zarr"
    sd.SpatialData(tables={"cells": adata}).write(path)
    # Loading the table opens the project's image too, so it has to exist.
    tifffile.imwrite(Path(tmp_path) / "image.ome.tif",
                     np.zeros((len(MARKERS), SIZE, SIZE), dtype=np.uint16),
                     ome=True, metadata={"axes": "CYX"})
    return path


def _open(tmp_path, name, store, *, features, is_transformed=False):
    from plexora import api

    spec = anndata_spec(store, table="cells", features=features,
                        coordinates={"source": "obsm", "obsm_key": "spatial"},
                        is_transformed=is_transformed, markers=MARKERS)
    config_path = Path(tmp_path) / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) \
        if config_path.exists() else {}
    image = image_spec(channels=MARKERS, width=SIZE, height=SIZE,
                       src=str(Path(tmp_path) / "image.ome.tif"))
    config[name] = project(name, dataset=spec, image=image,
                           confirmed=ALL_CONFIRMED).to_entry()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return api.project_data(name)


def test_a_log_layer_read_as_it_is_is_gated_as_logged(tmp_path, store):
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import profile

    logged = _open(tmp_path, "loglayer", store, features={"source": "layer", "layer": "log"})
    assert not logged.table.log_transformed          # the switch is off, rightly
    assert model.values_logged(logged)
    column = profile.column(logged, "CD45")
    assert not column.to_log and float(column.to_fit(6.0)) == 6.0
    assert model.fit_for(logged, "CD45")["fitted_in_log"] is False


def test_the_same_cells_gate_the_same_from_the_raw_matrix_and_the_log_layer(tmp_path, store):
    """X read with log1p on and the `log` layer read as it is are one matrix:
    the Auto gate lands on the same cells either way."""
    from plexora.plugins.gating.server import model

    raw = _open(tmp_path, "rawlog1p", store, features={"source": "X"}, is_transformed=True)
    layer = _open(tmp_path, "layerasis", store, features={"source": "layer", "layer": "log"})
    assert model.values_logged(raw) and model.values_logged(layer)
    for marker in MARKERS:
        assert model.fit_for(layer, marker)["gate"] == \
            pytest.approx(model.fit_for(raw, marker)["gate"], abs=1e-4)


def test_raw_intensities_read_as_they_are_are_not_taken_for_logged(tmp_path, store):
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import profile

    raw = _open(tmp_path, "rawasis", store, features={"source": "X"})
    assert not model.values_logged(raw)
    assert profile.column(raw, "CD45").to_log


@pytest.mark.parametrize("stats, source, logged", [
    ({"kind": "log_like", "shape": {"says": "logged"}}, "X", True),
    ({"kind": "log_like", "shape": {"says": "unclear"}}, "X", False),
    ({"kind": "log_like", "shape": {"says": "unclear"}}, "layer:log", True),
    ({"kind": "log_like", "shape": {"says": None}}, "layer:protein", True),
    ({"kind": "log_like", "shape": {"says": "raw"}}, "layer:log", False),
    ({"kind": "raw_intensity"}, "layer:log", False),
    ({"kind": "scaled"}, "X", False),
])
def test_reads_logged_is_recommends_rule(stats, source, logged):
    """`recommend` reads a log-like layer as it is unless its shape says raw,
    and X only when its shape says logged; `reads_logged` agrees."""
    assert expression.reads_logged(stats, source) is logged
