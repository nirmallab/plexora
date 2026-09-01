"""A brightfield project is a whole project, not a picture.

The scope decision this file exists to hold: masks, feature tables, gating, ROI
and Figure Builder all work on an H&E slide. Nothing is gated off for the kind,
so what has to be checked is that the paths which assume a channel stack survive
a project that has none -- an empty channel list, a marker that matches no
channel, a figure panel with nothing captured to composite.
"""

import numpy as np
import polars as pl
import pytest
import tifffile as tf

import plexora
from plexora.datasource import register_datasource, register_image_datasource
from plexora.server.models import data_model
from plexora.server.models.project import Project
from tests.brightfield_fixtures import write_rgb_ome_tiff


def _mask(path, height=512, width=640, cells=6):
    """A label mask over the same field the fixture image covers."""
    labels = np.zeros((height, width), np.uint32)
    for index in range(1, cells + 1):
        top = (index * height) // (cells + 2)
        left = (index * width) // (cells + 2)
        labels[top:top + 24, left:left + 24] = index
    tf.imwrite(path, labels, photometric="minisblack")
    return path


def _table(path, cells=6, height=512, width=640):
    pl.DataFrame({
        "CellID": list(range(1, cells + 1)),
        "X_centroid": [(index * width) // (cells + 2) + 12 for index in range(1, cells + 1)],
        "Y_centroid": [(index * height) // (cells + 2) + 12 for index in range(1, cells + 1)],
        "CD3": np.linspace(0.1, 4.0, cells),
        "PanCK": np.linspace(4.0, 0.1, cells),
        "phenotype": ["T cell", "Tumour"] * (cells // 2),
    }).write_csv(path)
    return path


@pytest.fixture
def he_project(tmp_path):
    """An H&E slide with a mask and a feature table, registered."""
    image = write_rgb_ome_tiff(tmp_path / "he.ome.tif", height=512, width=640)
    register_datasource(
        name="he",
        image=image,
        features=_table(tmp_path / "cells.csv"),
        segmentation=_mask(tmp_path / "mask.tif"),
    )
    return Project.find("he")


def test_a_mask_and_a_table_register_onto_a_slide(he_project):
    """The channel list is empty and the mask placeholder still has to be
    first: `load_label_image` reads `imageData[0]` as the label layer, so a
    project that names a mask without it in front draws the wrong thing."""
    assert he_project.image.kind == "brightfield"
    assert he_project.segmentation.available
    names = [channel["fullname"] for channel in he_project.image.channels]
    assert names == ["Area", "Image"]
    assert he_project.image.channels[0]["src"].rstrip("/").endswith("mask")  \
        or "Area" == he_project.image.channels[0]["fullname"]


def test_both_layers_serve_tiles(he_project):
    client = plexora.app.test_client()
    label_key = he_project.image.channels[0]["src"].rstrip("/").rsplit("/", 1)[-1]

    colour = client.get("/generated/data/he/rgb/0/0_0.png")
    labels = client.get(f"/generated/data/he/{label_key}/0/0_0.png")

    assert colour.status_code == 200 and colour.mimetype == "image/webp"
    assert labels.status_code == 200 and labels.mimetype == "image/png"


def test_the_table_is_readable_and_the_markers_match_no_channel(he_project):
    """Gating matches a marker to a channel by name and does nothing when it
    finds none -- which is every marker of a brightfield project. The test is
    that the description is still built, so the tool opens rather than fails."""
    description = data_model.get_datasource_description("he")

    assert "CD3" in description and "PanCK" in description
    assert data_model.real_channels("he") == [
        channel for channel in he_project.image.channels
        if channel["fullname"] != "Area"]


def test_cells_load_for_a_brightfield_project(he_project):
    client = plexora.app.test_client()

    response = client.get(
        "/get_all_cells/float/?datasource=he&start_keys=id,X_centroid,Y_centroid")

    assert response.status_code == 200
    assert len(response.data) > 0


def test_a_figure_panel_renders_the_slide_in_colour(he_project):
    """Figure Builder composites channels; a brightfield panel has none to
    composite, and the pixels are the answer. Without the dispatch it would
    export a black rectangle -- the state it draws for "every channel gone"."""
    from plexora.plugins.figure_builder.server import render

    with render.SourceImage("he") as source:
        assert source.is_brightfield
        image, report = render.render_panel(
            source,
            {"viewport": {"x": 0, "y": 0, "w": 640, "h": 512}, "channels": []},
            320, 256)

    assert image.size == (320, 256)
    pixels = np.asarray(image)
    assert pixels.shape == (256, 320, 3)
    # A stained section, not the black an empty composite would give.
    assert pixels.mean() > 100
    assert pixels[..., 0].mean() != pytest.approx(pixels[..., 2].mean(), abs=1.0)
    assert report["channels_rendered"] == 1


def test_a_fluorescence_figure_panel_still_composites(tmp_path):
    """The other half of the dispatch: an ordinary project must not take the
    brightfield branch."""
    from plexora.plugins.figure_builder.server import render
    from tests.brightfield_fixtures import write_planar_fluorescence

    path = write_planar_fluorescence(tmp_path / "panel.ome.tif",
                                     height=256, width=256)
    entry = register_image_datasource("panel", path)
    key = entry["imageData"][0]["src"].rstrip("/").rsplit("/", 1)[-1]

    with render.SourceImage("panel") as source:
        assert not source.is_brightfield
        image, report = render.render_panel(
            source,
            {"viewport": {"x": 0, "y": 0, "w": 256, "h": 256},
             "channels": [{"key": key, "visible": True, "window": [0, 65535],
                           "color": {"r": 255, "g": 255, "b": 255}}]},
            128, 128)

    assert image.size == (128, 128)
    assert report["channels_rendered"] == 1
