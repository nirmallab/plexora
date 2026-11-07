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


def test_a_picked_label_pyramid_is_described_level_by_level(info, monkeypatch):
    from plexora.browser import node_spec
    from plexora.server.models import data_model

    data_model.ensure_loaded(info["name"])
    spec = node_spec.describe(info["name"])
    assert spec["labels"]["served"] is False
    assert spec["labels"]["reason"].startswith("the mask is not a file the page holds")
    monkeypatch.setattr(data_model._providers.segmentation, "_path", "/picked/ab12cd/mask.tif")
    labels = node_spec.describe(info["name"])["labels"]
    assert labels["served"] is True, labels
    with tifffile.TiffFile(info["mask_path"], is_ome=False) as tif:
        levels = [[k, *level.shape] for k, level in enumerate(tif.series[0].levels)]
        dtype = str(tif.series[0].dtype)
    # The levels the file holds, by number: the page answers those and
    # leaves any level Python draws from a finer one to Python.
    assert labels["levels"] == levels
    assert labels["dtype"] == dtype


def test_a_label_pyramid_the_page_built_is_served_too(info, monkeypatch):
    """A flat mask's label pyramid built by the page is the page's file, under
    /derived/ (providers/browser.py `page_pyramid`): its tiles are the page's
    to answer, as a picked pyramid's are. One Python drew into the project
    store is not a file the page holds."""
    from plexora.browser import node_spec
    from plexora.server.models import data_model

    data_model.ensure_loaded(info["name"])
    built = "/derived/0123456789abcdef01234567/mask.labels.pyramid.ome.tiff"
    monkeypatch.setattr(data_model._providers.segmentation, "_path", built)
    labels = node_spec.describe(info["name"])["labels"]
    assert labels["served"] is True, labels
    assert labels["path"] == built
    monkeypatch.setattr(data_model._providers.segmentation, "_path",
                        "/data/derived/sample/mask.labels.pyramid.ome.tiff")
    labels = node_spec.describe(info["name"])["labels"]
    assert labels["served"] is False
    assert labels["reason"].startswith("the mask is not a file the page holds")


def test_a_project_without_a_mask_says_so(tmp_path):
    from plexora.browser import node_spec

    made = make_synthetic_project(tmp_path, name="bare", mask=False)
    assert node_spec.describe(made["name"])["labels"] == {"served": False, "reason": "no mask"}
