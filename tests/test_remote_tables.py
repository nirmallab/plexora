"""Cell tables at a web address: AnnData-zarr and SpatialData `tables/`.

A table is read by key -- `obs`, `var`, `X`, named `obsm` entries -- never by
enumerating the group, because an HTTPS host that cannot list makes
`anndata.read_zarr` return an EMPTY AnnData without an error. Enumeration
(which tables a store has, which obsm arrays) needs consolidated metadata or a
listing, and when neither is there the answer says so.
"""


import anndata as ad
import numpy as np
import pandas as pd
import pytest
import spatialdata as sd

from plexora import datasource
from plexora.server.models import data_model, import_proposal
from plexora.server.models.adapters import detect_data_type, inspection
from plexora.server.models.adapters import spatialdata_adapter
from plexora.server.models.project import Project
from plexora.server.routes.import_routes import replace_project_data
from tests.remote_fixtures import cache_root, http_store  # noqa: F401


def _adata(n=40, n_vars=5):
    rng = np.random.default_rng(0)
    obs = pd.DataFrame({"cell_type": (["T", "B"] * n)[:n],
                        "area": rng.random(n).astype(np.float32)},
                       index=[f"c{i}" for i in range(n)])
    var = pd.DataFrame(index=[f"gene_{i}" for i in range(n_vars)])
    adata = ad.AnnData(X=rng.random((n, n_vars)).astype(np.float32), obs=obs, var=var)
    adata.obsm["spatial"] = np.stack([np.arange(n) * 5.0, np.arange(n) * 3.0], axis=1)
    return adata


@pytest.fixture
def served(tmp_path):
    return tmp_path / "served"


def _anndata_store(served, consolidated=True):
    path = served / "cells.h5ad.zarr"
    _adata().write_zarr(path)
    if not consolidated:
        (path / ".zmetadata").unlink(missing_ok=True)
    return path


@pytest.mark.parametrize("mode", ["gateway", "forbidden"])
def test_an_anndata_zarr_is_detected_and_inspected(served, http_store, mode):
    local = _anndata_store(served, consolidated=False)
    server = http_store(mode)
    url = server.url("cells.h5ad.zarr")
    assert detect_data_type(url) == "anndata"
    remote_doc = inspection.inspect_anndata(url)
    local_doc = inspection.inspect_anndata(local)
    assert remote_doc["obs_count"] == local_doc["obs_count"] == 40
    assert remote_doc["var_names"] == local_doc["var_names"]
    assert [c["name"] for c in remote_doc["obs_columns"]] == \
        [c["name"] for c in local_doc["obs_columns"]]
    # Nothing to list obsm with, so the well-known names were asked for.
    assert {"name": "spatial", "shape": [40, 2]} in remote_doc["obsm"]


def test_a_remote_table_attaches_and_loads_like_the_local_one(served, http_store, tmp_path):
    local = _anndata_store(served)
    server = http_store()
    url = server.url("cells.h5ad.zarr")

    datasource.register_blank_datasource("web", width=500, height=500)
    replace_project_data("web", url, {})
    assert Project.load("web").dataset.src == url
    data_model.load_datasource("web", reload=True)
    remote_rows = data_model.datasource.to_dict(as_series=False)

    datasource.register_blank_datasource("here", width=500, height=500)
    replace_project_data("here", str(local), {})
    data_model.load_datasource("here", reload=True)
    local_rows = data_model.datasource.to_dict(as_series=False)

    assert remote_rows == local_rows
    assert remote_rows["X"][:3] == [0.0, 5.0, 10.0]


def test_a_local_anndata_zarr_is_readable_too(served):
    """It used to be detected as AnnData and then opened with h5py, which
    cannot open a directory -- fixed on the way."""
    local = _anndata_store(served)
    datasource.register_blank_datasource("here", width=500, height=500)
    replace_project_data("here", str(local), {})
    data_model.load_datasource("here", reload=True)
    assert data_model.datasource.height == 40


def test_the_proposal_for_a_remote_anndata_is_a_table(served, http_store):
    _anndata_store(served)
    server = http_store()
    [sample] = import_proposal.inspect_paths(
        [server.url("cells.h5ad.zarr")]).to_dict()["samples"]
    [layer] = sample["layers"]
    assert layer["role"] == "table" and layer["src"] == server.url("cells.h5ad.zarr")


def test_spatialdata_tables_are_listed_from_consolidated_metadata(served, http_store):
    sd.SpatialData(tables={"cells": _adata(), "nuclei": _adata(n=10)}).write(served / "sd.zarr")
    server = http_store()
    url = server.url("sd.zarr")

    assert detect_data_type(url) == "spatialdata"
    tables = spatialdata_adapter.list_spatialdata_tables(url)
    assert {t["name"]: (t["n_obs"], t["n_var"]) for t in tables} == {
        "cells": (40, 5), "nuclei": (10, 5)}
    doc = inspection.inspect_spatialdata_table(url, "cells")
    assert doc["obs_count"] == 40 and doc["table"] == "cells"
    table = spatialdata_adapter.read_spatialdata_table(url, "nuclei")
    assert table.shape == (10, 5)
    np.testing.assert_allclose(table.obsm["spatial"][:, 0], np.arange(10) * 5.0)


def test_the_proposal_asks_which_table(served, http_store):
    sd.SpatialData(tables={"cells": _adata(), "nuclei": _adata(n=10)}).write(served / "sd.zarr")
    server = http_store()
    [sample] = import_proposal.inspect_paths([server.url("sd.zarr")]).to_dict()["samples"]
    table = next(l for l in sample["layers"] if l["role"] == "table")
    assert table["table"] is None
    [question] = [q for q in sample["questions"] if q["id"] == "table"]
    assert {o["value"] for o in question["options"]} == {"cells", "nuclei"}


def test_a_store_that_cannot_be_enumerated_says_so(served, http_store):
    sd.SpatialData(tables={"cells": _adata()}).write(served / "sd.zarr")
    # Strip the consolidated metadata a real writer leaves.
    import json

    root = served / "sd.zarr" / "zarr.json"
    doc = json.loads(root.read_text())
    doc.pop("consolidated_metadata", None)
    root.write_text(json.dumps(doc))
    (served / "sd.zarr" / ".zmetadata").unlink(missing_ok=True)
    server = http_store()
    with pytest.raises(ValueError, match="cannot list it"):
        spatialdata_adapter.list_spatialdata_tables(server.url("sd.zarr"))
    # A named table is still read by key.
    assert spatialdata_adapter.read_spatialdata_table(server.url("sd.zarr"), "cells").shape == (40, 5)


def test_a_table_too_large_to_fetch_is_refused(served, http_store, monkeypatch):
    _anndata_store(served)
    server = http_store()
    monkeypatch.setattr(spatialdata_adapter, "REMOTE_TABLE_MAX_BYTES", 100)
    with pytest.raises(ValueError, match="too large"):
        spatialdata_adapter.read_remote_table(
            spatialdata_adapter.remote_node(server.url("cells.h5ad.zarr")))


def test_remote_table_signature_is_the_metadata_fingerprint(served, http_store):
    from plexora.server.utils import remote_store

    _anndata_store(served)
    server = http_store()
    signature = remote_store.source_signature(server.url("cells.h5ad.zarr"))
    assert signature["csv_path"] == server.url("cells.h5ad.zarr")
    assert signature["csv_size"] and signature["csv_mtime_ns"]
