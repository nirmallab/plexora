"""Route-level check that imports store filled labels, and record that they did.

Every import converts to a filled label pyramid -- served untouched when the
user's mask already is one -- and leaves boundary-finding to the viewer's
renderLabelTile. There is no UI for choosing, so these tests pin the default
down at the two places it has to survive: the background job the import route
starts, and the `segmentationMode` key written into config.json, which is what
a datasource load reads to decide whether the viewer outlines the mask itself.
A break in either is silent -- the mask still converts, just into the kind the
viewer is not expecting, which paints solid blobs over the image.

Both formats go through one route now (`POST /import`), so each assertion below
runs against the same handler rather than against a per-format one.

MODE_OUTLINES is still implemented and covered by test_segmentation_pyramid.py;
it is simply not reachable from a request any more, which is asserted below.
"""

import json

import numpy as np
import polars as pl
import tifffile

import plexora
from plexora.server.models import centroid_tiles, data_model, database_model
from plexora.server.routes import import_routes, page_routes, project_routes
from plexora.server.utils import segmentation_pyramid as sp


def _isolate(tmp_path, monkeypatch, config="{}"):
    """Point the app at a scratch data directory.

    Every module that does `from plexora import data_path` binds it at import
    time, so patching `plexora.data_path` alone does not reach them. The upload
    route is one of those, and it creates `<data_path>/<datasetName>/` -- an
    incomplete patch here silently writes test datasets into the developer's
    real plexora/data/ directory instead of failing.
    """
    (tmp_path / "config.json").write_text(config, encoding="utf-8")
    config_path = tmp_path / "config.json"
    # Every module found by: grep -n "^from plexora import .*\(data_path\|
    # config_json_path\)" -r plexora/ --include=*.py
    for module in (plexora, data_model, import_routes, database_model, centroid_tiles,
                   page_routes, project_routes):
        if hasattr(module, "data_path"):
            monkeypatch.setattr(module, "data_path", tmp_path)
        if hasattr(module, "config_json_path"):
            monkeypatch.setattr(module, "config_json_path", config_path)
    return plexora.app.test_client()


def test_the_isolation_helper_actually_isolates(tmp_path, monkeypatch):
    """Guards the helper above: if a new module starts binding data_path at
    import time, these tests must fail rather than quietly write into the real
    plexora/data/."""
    real_data_dir = plexora.data_path
    client = _isolate(tmp_path, monkeypatch)
    image, mask, csv_path = _inputs(tmp_path)
    _capture_jobs(monkeypatch)

    before = set(p.name for p in real_data_dir.iterdir()) if real_data_dir.exists() else set()
    client.post("/import", data={
        "name": "isolation_probe_ds",
        "image_file": str(image),
        "label_file": str(mask),
        "data_file": str(csv_path),
    })
    after = set(p.name for p in real_data_dir.iterdir()) if real_data_dir.exists() else set()

    assert after == before, f"upload wrote into the real data dir: {sorted(after - before)}"
    assert (tmp_path / "isolation_probe_ds").is_dir()


def _inputs(tmp_path):
    image = tmp_path / "image.ome.tif"
    tifffile.imwrite(image, np.zeros((3, 256, 256), dtype=np.uint8))
    mask = tmp_path / "mask.tiff"
    labels = np.zeros((256, 256), dtype=np.uint32)
    labels[20:60, 20:60] = 1
    labels[20:60, 60:100] = 2
    tifffile.imwrite(mask, labels)
    csv_path = tmp_path / "cells.csv"
    pl.DataFrame({
        "CellID": [1, 2],
        "X_centroid": [40.0, 80.0],
        "Y_centroid": [40.0, 40.0],
        "MarkerA": [1.0, 2.0],
    }).write_csv(csv_path)
    return image, mask, csv_path


def _capture_jobs(monkeypatch):
    calls = []
    monkeypatch.setattr(
        data_model, "start_segmentation_job",
        lambda name, source, directory, mode=sp.DEFAULT_MODE: calls.append(mode),
    )
    return calls


def test_csv_upload_starts_the_job_in_filled_mode(tmp_path, monkeypatch):
    client = _isolate(tmp_path, monkeypatch)
    image, mask, csv_path = _inputs(tmp_path)
    calls = _capture_jobs(monkeypatch)

    response = client.post("/import", data={
        "name": "filled_ds",
        "image_file": str(image),
        "label_file": str(mask),
        "data_file": str(csv_path),
    })

    # A CSV import lands on the column-classification screen.
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/project/filled_ds/columns")
    assert calls == [sp.MODE_FILLED]


def test_a_posted_mode_field_cannot_select_a_mode(tmp_path, monkeypatch):
    """The form field is gone, so a hand-crafted or stale post carrying one has
    to be ignored outright rather than steering the conversion. Checked with a
    valid mode name and with a traversal attempt, since the old code path fed
    this string towards a derived filename."""
    client = _isolate(tmp_path, monkeypatch)
    image, mask, csv_path = _inputs(tmp_path)

    for index, planted in enumerate(("outlines", "../../etc/passwd")):
        calls = _capture_jobs(monkeypatch)
        client.post("/import", data={
            "name": "odd_ds_%d" % index,
            "image_file": str(image),
            "label_file": str(mask),
            "data_file": str(csv_path),
            "segmentation_mode": planted,
        })
        assert calls == [sp.MODE_FILLED], "%r changed the mode" % planted


def test_a_csv_import_records_the_mode_immediately(tmp_path, monkeypatch):
    """The mode reaches config.json at import, not after a second form post.

    It used to be handed to the step-two page as template data and echoed back
    to a save endpoint -- so a user who abandoned that page left an entry with
    no mode recorded, and a later load had to infer one by reading the derived
    file's OME marker.
    """
    client = _isolate(tmp_path, monkeypatch)
    image, mask, csv_path = _inputs(tmp_path)
    _capture_jobs(monkeypatch)

    client.post("/import", data={
        "name": "echo_ds",
        "image_file": str(image),
        "label_file": str(mask),
        "data_file": str(csv_path),
    })

    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["echo_ds"]["segmentationMode"] == sp.MODE_FILLED
    assert data_model.segmentation_mode(saved["echo_ds"]) == sp.MODE_FILLED


def test_an_anndata_import_records_the_mode(tmp_path, monkeypatch):
    anndata = __import__("anndata")
    import pandas as pd

    client = _isolate(tmp_path, monkeypatch)
    image, mask, _ = _inputs(tmp_path)
    _capture_jobs(monkeypatch)
    h5ad = tmp_path / "cells.h5ad"
    adata = anndata.AnnData(
        X=np.random.default_rng(0).random((6, 3)).astype(np.float32),
        obs=pd.DataFrame(index=[f"cell_{i}" for i in range(6)]),
        var=pd.DataFrame(index=["MarkerA", "MarkerB", "MarkerC"]),
    )
    adata.obsm["spatial"] = np.random.default_rng(1).random((6, 2)).astype(np.float32) * 100
    adata.write_h5ad(h5ad)

    # No read spec is posted: obsm["spatial"] is detected from the file, which
    # is what lets the import page ask for a path and nothing else.
    response = client.post("/import", data={
        "name": "ann_ds",
        "image_file": str(image),
        "label_file": str(mask),
        "data_file": str(h5ad),
    })

    # AnnData skips the classification screen -- var/obs already draw that line.
    assert response.status_code == 302, response.get_data(as_text=True)
    assert response.headers["Location"].endswith("/ann_ds")
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["ann_ds"]["segmentationMode"] == sp.MODE_FILLED
    # And that key is exactly what the viewer reads to decide whether
    # renderLabelTile derives boundaries.
    assert data_model.segmentation_mode(saved["ann_ds"]) == sp.MODE_FILLED
