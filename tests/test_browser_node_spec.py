"""plexora.browser.node_spec: what the website's browser node may serve.

The page reads a project's tiles from the picked file itself only where Python
says it reads the same file the same way; otherwise every tile stays
Python's. Under CPython no file is a pick (`/picked/...` is the website's
mount), so the served case is reached by giving the loaded provider such a
path.
"""

import json

import pytest

from tests.agent_fixtures import make_synthetic_project

tifffile = pytest.importorskip("tifffile")


@pytest.fixture
def info(tmp_path):
    return make_synthetic_project(tmp_path)


def test_a_file_on_this_computer_is_left_to_python(info):
    from plexora.browser import node_spec

    spec = node_spec.describe(info["name"])
    assert spec["served"] is False
    assert spec["reason"] == "the file is not one the page holds"


def test_a_picked_pyramid_is_described_as_python_reads_it(info, monkeypatch):
    from plexora.browser import node_spec
    from plexora.server.models import data_model

    data_model.ensure_loaded(info["name"])
    monkeypatch.setattr(data_model._providers.image, "_path", "/picked/ab12cd/image.ome.tif")
    spec = json.loads(node_spec.describe_json(info["name"]))
    assert spec["served"] is True, spec
    assert spec["path"] == "/picked/ab12cd/image.ome.tif"
    with tifffile.TiffFile(info["image_path"]) as tif:
        levels = [list(level.shape) for level in tif.series[0].levels]
        dtype = str(tif.series[0].dtype)
    # Every level Python serves, as the file holds it: the page checks its
    # own reading of the file against exactly these.
    assert spec["levels"] == levels[:len(spec["levels"])] and spec["levels"]
    assert spec["dtype"] == dtype
    config = data_model.config[info["name"]]
    assert spec["tile"] == [config["tileWidth"], config["tileHeight"]]
    assert len(spec["channels"]) == spec["levels"][0][0]
    assert spec["generation"] == data_model.load_generation


def test_a_picked_file_that_is_not_a_tiff_is_left_to_python(info, monkeypatch):
    from plexora.browser import node_spec
    from plexora.server.models import data_model

    data_model.ensure_loaded(info["name"])
    monkeypatch.setattr(data_model._providers.image, "_path", "/picked/ab12cd/image.zarr")
    spec = node_spec.describe(info["name"])
    assert spec["served"] is False and spec["reason"] == "not a TIFF file"
