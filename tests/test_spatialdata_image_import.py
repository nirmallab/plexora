"""Images that live inside a SpatialData store, read with the real writer.

Plexora already read a SpatialData store's *tables*; its images sat untouched
beside them, so the usual gesture -- point both fields at the one store you
have -- registered a project whose feature table came from the store and whose
image had to come from somewhere else entirely.

Everything here uses `spatialdata` itself rather than the hand-written fixtures
in tests/ngff_fixtures.py, because interoperating with what that library writes
is the whole subject: its NGFF version string, its `s0` dataset naming, and the
`"ome"`-nested attributes are all things a reader can get wrong on its own.
"""

import numpy as np
import pytest

from plexora import datasource
from plexora.server.models import data_model
from plexora.server.utils import ome_zarr
from tests.helpers import use_data_root

spatialdata = pytest.importorskip("spatialdata")


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    directory.mkdir()
    use_data_root(monkeypatch, directory)
    return directory


def _table(n=12, markers=("DNA", "CD3", "CD8")):
    import anndata as ad
    import pandas as pd
    from spatialdata.models import TableModel

    rng = np.random.default_rng(0)
    adata = ad.AnnData(
        X=rng.random((n, len(markers))).astype(np.float32),
        obs=pd.DataFrame(
            {"region": pd.Categorical(["morphology"] * n),
             "cell_id": np.arange(n, dtype=np.int64)},
            index=[f"cell_{i}" for i in range(n)]),
        var=pd.DataFrame(index=list(markers)),
    )
    adata.obsm["spatial"] = rng.random((n, 2)) * 100
    return TableModel.parse(adata, region="morphology", region_key="region",
                            instance_key="cell_id")


def _store(path, *, images=("morphology",), channels=("DNA", "CD3", "CD8"),
           size=256, scale_factors=None, with_table=True):
    from spatialdata.models import Image2DModel

    rng = np.random.default_rng(1)
    parsed = {}
    for name in images:
        parsed[name] = Image2DModel.parse(
            rng.integers(1, 4000, (len(channels), size, size)).astype("uint16"),
            dims=("c", "y", "x"), c_coords=list(channels),
            scale_factors=scale_factors)
    tables = {"cells": _table(markers=channels)} if with_table else {}
    spatialdata.SpatialData(images=parsed, tables=tables).write(path)
    return path


def test_store_root_resolves_to_its_only_image(tmp_path, data_dir):
    store = _store(tmp_path / "sample.zarr")

    entry = datasource.register_image_datasource(
        name="sample", image=store, data_dir=data_dir)

    assert entry["channelFile"] == str(store / "images" / "morphology")
    assert entry["image_kind"] == "ome_zarr"
    assert entry["num_channels"] == 3
    # spatialdata names its arrays "s0"/"s1"/..., never "0"/"1" -- the reader
    # follows `datasets[].path` rather than assuming.
    assert entry["maxLevel"] >= 1


def test_channel_names_come_from_the_stores_own_labels(tmp_path, data_dir):
    store = _store(tmp_path / "sample.zarr", channels=("DAPI", "CD45"))

    entry = datasource.register_image_datasource(
        name="labelled", image=store, data_dir=data_dir)

    assert [c["name"] for c in entry["imageData"]] == ["DAPI", "CD45"]


def test_an_explicit_element_path_is_taken_as_given(tmp_path, data_dir):
    store = _store(tmp_path / "sample.zarr", images=("dapi", "morphology"))

    entry = datasource.register_image_datasource(
        name="explicit", image=store / "images" / "morphology", data_dir=data_dir)

    assert entry["channelFile"] == str(store / "images" / "morphology")


def test_an_ambiguous_store_names_its_images(tmp_path, data_dir):
    store = _store(tmp_path / "sample.zarr", images=("dapi", "morphology"))

    with pytest.raises(ValueError) as caught:
        datasource.register_image_datasource(
            name="ambiguous", image=store, data_dir=data_dir)

    assert "dapi" in str(caught.value) and "morphology" in str(caught.value)


def test_a_multiscale_element_serves_every_level(tmp_path, data_dir):
    store = _store(tmp_path / "sample.zarr", size=512, scale_factors=[2, 2])

    datasource.register_image_datasource(
        name="pyramided", image=store, data_dir=data_dir)
    data_model.load_datasource("pyramided", reload=True)

    assert len(data_model.channels) == 3
    assert data_model.read_tile(
        data_model.channels, 0, 2, "0_0", 1024, 1024).shape == (128, 128)
    # Nothing had to be derived: the store's own levels reach far enough out.
    assert not (data_dir / "pyramided" / ome_zarr.EXTENSION_NAME).exists()


def test_one_store_serves_as_both_the_image_and_the_table(tmp_path, data_dir):
    """The gesture the whole feature exists for: point both fields at the one
    store you have."""
    store = _store(tmp_path / "sample.zarr")

    entry = datasource.register_spatialdata_datasource(
        name="both", image=store, store=store, table="cells", data_dir=data_dir)

    assert entry["channelFile"] == str(store / "images" / "morphology")
    assert entry["dataset"]["src"] == str(store)
    assert entry["dataset"]["table"] == "cells"
    # var_names still wins over the image's own labels -- gating matches
    # channels to var_names by name, so the tier order is unchanged.
    assert [c["name"] for c in entry["imageData"]] == ["DNA", "CD3", "CD8"]

    data_model.load_datasource("both", reload=True)
    assert data_model.datasource is not None
    assert len(data_model.datasource) == 12
    assert data_model.read_tile(
        data_model.channels, 1, 0, "0_0", 1024, 1024).shape == (256, 256)


def test_copying_one_store_used_for_both_copies_it_once(tmp_path, data_dir):
    """`copy=True` has to copy the store whole and THEN resolve the image out of
    the copy -- resolving first would copy an image element away from the tables
    that describe it."""
    store = _store(tmp_path / "sample.zarr", size=128)

    entry = datasource.register_spatialdata_datasource(
        name="copied", image=store, store=store, table="cells",
        copy=True, data_dir=data_dir)

    copied = data_dir / "copied" / "sample.zarr"
    assert copied.is_dir()
    assert entry["channelFile"] == str(copied / "images" / "morphology")
    assert entry["dataset"]["src"] == str(copied)
    # One copy, not two.
    assert sorted(p.name for p in (data_dir / "copied").iterdir()) == ["sample.zarr"]


def test_the_import_wizard_accepts_one_store_in_both_fields(tmp_path, data_dir):
    """Through the route, not the API -- `/import`'s image check had to become
    `.exists()` for a folder to get this far at all."""
    import json

    import plexora

    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    store = _store(tmp_path / "sample.zarr", size=128)
    client = plexora.app.test_client()

    response = client.post("/import", data={
        "name": "wizard",
        "image_file": str(store),
        "label_file": "",
        "data_file": str(store),
        "data_table": "cells",
    }, follow_redirects=False)

    assert response.status_code == 302, response.get_data(as_text=True)[:400]
    config = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
    entry = config["wizard"]
    assert entry["image_kind"] == "ome_zarr"
    assert entry["channelFile"] == str(store / "images" / "morphology")
    assert entry["dataset"]["src"] == str(store)


def test_the_import_wizard_refuses_a_folder_that_is_not_a_store(tmp_path, data_dir):
    import plexora

    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    folder = tmp_path / "pictures"
    folder.mkdir()
    client = plexora.app.test_client()

    response = client.post("/import", data={
        "name": "nope", "image_file": str(folder),
        "label_file": "", "data_file": "",
    })

    # The upload page re-rendered with the reason on it, not a bare 400.
    assert response.status_code == 400
    assert "folder" in response.get_data(as_text=True)


def test_a_store_with_no_image_says_so(tmp_path, data_dir):
    store = tmp_path / "tables_only.zarr"
    spatialdata.SpatialData(tables={"cells": _table()}).write(store)

    with pytest.raises(ValueError, match="no OME-Zarr image"):
        datasource.register_image_datasource(
            name="tables", image=store, data_dir=data_dir)
