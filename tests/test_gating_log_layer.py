"""A matrix already log-transformed and read as it is (log1p off) is gated on
its own scale, never log1p'd a second time.

The import reads a SpatialData `log` layer with the log1p switch off -- right,
since the values are logged already -- and gating used to take the switch at
its word: "not transformed" meant "raw", so it fitted log1p(log). On
ImageLSP23362 (2026-10-28) that compressed every marker's candidates until two
or three were left, and 14 of 15 gates came out low. QC's cell calls and the
display's cell cap read the same switch the same way; all of them now ask
`expression.values_logged`.
"""

import json
from pathlib import Path
from types import SimpleNamespace

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


def test_qc_cell_calls_read_a_log_layer_as_it_is(tmp_path, store):
    """QC compares cells on a log scale: a log layer is taken as it stands,
    raw intensities are log1p'd -- the same cells, the same numbers."""
    from plexora.plugins.qc.server.cells import calls

    layer = _open(tmp_path, "qclayer", store, features={"source": "layer", "layer": "log"})
    raw = _open(tmp_path, "qcraw", store, features={"source": "X"})
    from_layer = calls._to_log(layer, layer.table.columns(["CD45"])["CD45"])
    from_raw = calls._to_log(raw, raw.table.columns(["CD45"])["CD45"])
    np.testing.assert_allclose(from_layer, from_raw, rtol=1e-5)


def test_the_display_cap_is_in_image_units_from_either_matrix(tmp_path, store):
    """The cell-side cap on a marker's display window is in pixel units
    whichever matrix the project reads: a log layer comes back with expm1,
    not used as if its log values were intensities."""
    from plexora import api
    from plexora.agent.evidence import calibration

    _open(tmp_path, "caplayer", store, features={"source": "layer", "layer": "log"})
    _open(tmp_path, "capraw", store, features={"source": "X"})
    session = SimpleNamespace(data=api.project_data)
    from_layer = calibration._cell_caps(session, "caplayer", list(MARKERS))
    from_raw = calibration._cell_caps(session, "capraw", list(MARKERS))
    assert set(from_layer) == set(MARKERS)
    for marker in MARKERS:
        assert from_layer[marker] == pytest.approx(from_raw[marker], rel=1e-3)
        assert from_layer[marker] > 1000          # intensities, not log units


def test_values_logged_without_a_matrix_to_read():
    """No table is not logged; a table that names no matrix (a stand-in with
    no expression fingerprint) is taken at its own flag."""
    assert expression.values_logged(SimpleNamespace()) is False
    assert expression.values_logged(SimpleNamespace(table=None)) is False
    flagged = SimpleNamespace(table=SimpleNamespace(log_transformed=True))
    unflagged = SimpleNamespace(table=SimpleNamespace(log_transformed=False))
    assert expression.values_logged(flagged) is True
    assert expression.values_logged(unflagged) is False
