"""The image reader core now owns, shared by Figure Builder and the agent."""

import dataclasses
import json

import numpy as np
import pytest

from plexora.server.models import data_model
from plexora.server.models.project import Project
from plexora.server.utils import source_image
from tests.agent_fixtures import DNA_LEVEL, local_handles, make_synthetic_project


@pytest.fixture
def ds(tmp_path):
    info = make_synthetic_project(tmp_path)
    yield local_handles(info["name"])
    source_image.close_readers()


def test_figure_builder_reads_through_the_same_class():
    from plexora.plugins.figure_builder.server import render

    assert render.SourceImage is source_image.SourceImage
    assert render.choose_level is source_image.choose_level
    assert render.CHANNEL_ALPHA == source_image.CHANNEL_ALPHA


def test_composite_is_the_shaders_arithmetic(ds):
    channels = [{"key": "DNA", "color": {"r": 0, "g": 0, "b": 255}, "window": [0, DNA_LEVEL * 2]}]
    with source_image.SHELF.reader(ds) as source:
        pixels, rendered, background = source_image.composite(source, channels, 0, (0, 0, 64, 64))
    assert rendered == 1 and background == 0
    assert pixels.shape == (64, 64, 3)
    # The centre of cell 1 (32, 32): DNA at ~DNA_LEVEL, half the window.
    expected = np.clip(DNA_LEVEL / (DNA_LEVEL * 2), 0, 1) * 255 * source_image.CHANNEL_ALPHA
    assert pixels[32, 32, 2] == pytest.approx(expected, abs=0.12 * 255)
    assert pixels[32, 32, 0] == 0


def test_composite_pads_off_the_edge(ds):
    channels = [{"key": "DNA", "color": {"r": 255, "g": 255, "b": 255}, "window": [0, 100]}]
    with source_image.SHELF.reader(ds) as source:
        pixels, _, _ = source_image.composite(source, channels, 0, (-10, -10, 20, 20))
    assert pixels.shape == (30, 30, 3)
    assert pixels[:10, :10].max() == 0


def test_channel_stats_reads_the_coarsest_level(ds):
    with source_image.SHELF.reader(ds) as source:
        stats = source_image.channel_stats(source, "CD8")
        assert stats["level"] == source.levels - 1
        with pytest.raises(source_image.RenderError):
            source_image.channel_stats(source, "missing")
    assert stats["dtype"] == "uint16"
    assert stats["p999"] > stats["p01"]


def test_the_shelf_reopens_when_the_image_moves(ds, tmp_path):
    with source_image.SHELF.reader(ds) as first:
        pass
    with source_image.SHELF.reader(ds) as again:
        assert again is first
    moved = tmp_path / "moved.ome.tif"
    moved.write_bytes(open(ds.image.source.path, "rb").read())
    record = dataclasses.replace(ds.project, image=dataclasses.replace(
        ds.project.image, src=str(moved)))
    other = dataclasses.replace(ds, project=record, image=type(ds.image)(record))
    with source_image.SHELF.reader(other) as reopened:
        assert reopened is not first


def test_reading_never_loads_a_datasource(ds, monkeypatch):
    monkeypatch.setattr(data_model, "load_datasource",
                        lambda *a, **k: pytest.fail("loaded a datasource"))
    with source_image.SHELF.reader(ds) as source:
        source.read(0, 1, (0, 0, 32, 32))
