"""Renaming channels from a file must move their 8-bit tile windows with them.

The default (non-HD) tiles are quantized with each channel's full-resolution
ceiling, and those ceilings are persisted in the project database keyed by
channel NAME so a restart does not re-read every plane. A channel-name file is
usually uploaded because the names were in the wrong order, so after it the
same name sits on a different plane. The window served for a name must then be
the one measured for the plane that name now points at: the window of the plane
that used to carry the name saturates a bright plane to solid 255 and crushes a
dim one into a handful of grey levels.
"""

import io

import anndata as ad
import numpy as np
import pandas as pd
import tifffile
from PIL import Image

import plexora
from plexora import datasource
from plexora.server.models import data_model
from plexora.server.models.project import Project
from tests.helpers import use_data_root

NAME = "qwin_upload_swap"


def _register(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)

    planes = np.zeros((2, 256, 256), dtype=np.uint16)
    planes[0, :, :] = 400
    planes[0, 10, 10] = 1000          # plane 0's ceiling
    planes[1, :, :] = 20000
    planes[1, 20, 20] = 40000         # plane 1's ceiling
    image_path = tmp_path / "image.tif"
    tifffile.imwrite(image_path, planes)

    h5ad_path = tmp_path / "cells.h5ad"
    adata = ad.AnnData(
        X=np.random.default_rng(0).random((10, 2)).astype(np.float32),
        obs=pd.DataFrame(index=[f"cell_{i}" for i in range(10)]),
        var=pd.DataFrame(index=["DNA", "CD3"]))
    adata.obsm["spatial"] = np.stack(
        [np.linspace(10, 50, 10), np.linspace(10, 50, 10)], axis=1)
    adata.write_h5ad(h5ad_path)

    datasource.register_anndata_datasource(
        name=NAME, image=image_path, features=h5ad_path,
        coordinate_source="obsm", obsm_key="spatial", data_dir=data_dir,
        channel_names=["DNA", "CD3"],
    )


def _decoded_tile(plane):
    key = Project.find(NAME).image.channels[plane]["src"].rstrip("/").rsplit("/", 1)[-1]
    data, _ = data_model.encode_tile(NAME, key, 0, "0_0", "webp")
    return np.asarray(Image.open(io.BytesIO(data)).convert("L"))


def test_a_name_moved_by_an_uploaded_file_is_quantized_with_its_new_planes_ceiling(
        tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch)
    data_model.load_datasource(NAME, reload=True)

    # Both channels viewed under the first names: their ceilings are measured
    # and persisted by name.
    assert data_model.get_channel_quantization_window("DNA", NAME) == (0.0, 1000.0)
    assert data_model.get_channel_quantization_window("CD3", NAME) == (0.0, 40000.0)

    # The panel list was the wrong way round; the corrected one is uploaded.
    response = plexora.app.test_client().post(
        "/upload_channels",
        data={"datasource": NAME,
              "file": (io.BytesIO(b"CD3\nDNA\n"), "panel.csv")},
        content_type="multipart/form-data")
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["names"] == ["CD3", "DNA"]

    # Plane 0 (max 1000) is called CD3 now and plane 1 (max 40000) DNA.
    assert data_model.real_channel_index("DNA", NAME) == 1
    assert data_model.get_channel_quantization_window("CD3", NAME) == (0.0, 1000.0), \
        "CD3 now names plane 0, so its window is plane 0's ceiling"
    assert data_model.get_channel_quantization_window("DNA", NAME) == (0.0, 40000.0), \
        "DNA now names plane 1, so its window is plane 1's ceiling"

    # And the tiles the viewer draws: plane 1's bulk (20000 of 40000) is mid
    # grey, not clipped white.
    plane_1 = _decoded_tile(1)
    assert np.mean(plane_1 >= 250) < 0.01, "plane 1's tile is saturated"
