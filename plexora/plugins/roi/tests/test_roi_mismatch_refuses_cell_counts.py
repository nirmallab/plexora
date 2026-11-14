"""Regions drawn on another image are not counted on, or written onto, this
image's cells.

The stored coordinate_space is what catches the same datasource name now
pointing at a different image (ROIRepository.status). Editing already refuses
while it does not match; counting cells in a region (the agent's
count_cells_in_roi) and Map to cells (which writes `<name>_category` and
`<name>_name` into the user's table) must refuse the same way, or the old
slide's regions are measured against the new slide's cells and reported as
valid.
"""

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora.plugins.roi.server import service
from plexora.plugins.roi.server.repository import ImageMismatch, ROIRepository
from plexora.server import plugins as plugin_registry
from plexora.server.models import data_model
from tests.agent_fixtures import local_handles, make_synthetic_project
from tests.helpers import use_data_root

SQUARE = [[0, 0], [130, 0], [130, 130], [0, 130]]
QUADRANT = {"type": "Polygon", "coordinates": [
    [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]}
IMAGE_WIDTH = IMAGE_HEIGHT = 256

_DATA_MODEL_GLOBALS = ("ball_tree", "source", "config", "seg", "zarray",
                       "channels", "metadata", "_loaded_source", "datasource")


def _double_stored_size(name):
    """What a different-sized image under the same name looks like from the
    ROI store: the recorded coordinate_space no longer matches the image."""
    repository = ROIRepository(name)
    state = repository.load()
    space = state["images"]["default"]["coordinate_space"]
    space.update(width=space["width"] * 2, height=space["height"] * 2)
    repository._write(state)
    assert ROIRepository(name).status()["dimension_mismatch"] is True


def test_cells_are_not_counted_in_a_region_drawn_on_a_different_image(tmp_path):
    info = make_synthetic_project(tmp_path, name="roi_mismatch_count")
    ds = local_handles(info["name"])
    _, _, roi = service.create_roi(ds, category="Tumor", points=SQUARE)
    _double_stored_size(ds.name)

    with pytest.raises(ImageMismatch):
        service.cells_in_roi(ds, roi["id"])


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    csv_path = tmp_path / "cells.csv"
    tifffile.imwrite(image_path, np.zeros((2, IMAGE_HEIGHT, IMAGE_WIDTH), dtype=np.uint8))
    pl.DataFrame({
        "CellID": np.arange(4, dtype=np.uint32),
        "X_centroid": np.linspace(10, 200, 4, dtype=np.float32),
        "Y_centroid": np.linspace(10, 200, 4, dtype=np.float32),
        "MarkerA": np.linspace(0, 3, 4, dtype=np.float32),
    }).write_csv(csv_path)

    use_data_root(monkeypatch, data_dir)
    for name in _DATA_MODEL_GLOBALS:
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)
    if plugin_registry.find(plexora.app, "roi") is None:  # pragma: no cover
        pytest.skip("roi is not installed")

    from plexora import datasource as datasource_module

    datasource_module.register_datasource(
        name="roi_mismatch_map", image=image_path, features=csv_path,
        x="X_centroid", y="Y_centroid", segmentation=None, data_dir=data_dir,
    )
    return plexora.app.test_client()


def test_map_to_cells_refuses_regions_drawn_on_a_different_image(client, tmp_path):
    """422 image_dimensions_changed, as an edit gets, and the user's table is
    left without the wrong image's region columns."""
    body = {"datasource": "roi_mismatch_map"}
    response = client.post("/plugins/roi/api/operations", json={**body, "base_revision": 0,
        "operations": [{"op": "roi.bulk_create", "image": "default",
                        "categories": [{"id": "c-1", "label": "Tumor"}],
                        "features": [{"id": "r-1", "category_id": "c-1",
                                      "geometry": QUADRANT, "name": "Tumor 1"}]}]})
    assert response.get_json()["success"] is True
    _double_stored_size("roi_mismatch_map")

    response = client.post("/plugins/roi/api/map_to_cells", json=body)
    payload = response.get_json()
    assert (response.status_code, payload.get("error")) == (422, "image_dimensions_changed"), payload
    written = pl.read_csv(tmp_path / "cells.csv")
    assert "rois_category" not in written.columns
