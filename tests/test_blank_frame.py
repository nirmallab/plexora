"""A sample whose reference frame has no image behind it.

The structural claim the whole import rework rests on: the image is ONE POSSIBLE
LAYER, not the anchor. Transcripts on their own, a table and a mask, spots from a
run with no picture -- each of those is a real sample, and each needs exactly one
coordinate system for every other layer's transform to be expressed in.

A blank frame is that coordinate system with nothing drawn in it. What these
tests pin is that it is a FIRST-CLASS project rather than a degraded one: it
loads, it serves its (transparent) tiles, it reads as complete to the
requirements machinery, and the one thing it genuinely cannot produce -- a
thumbnail -- is None rather than an exception.
"""

import json

import numpy as np
import pytest
import tifffile

import plexora
from plexora.datasource import _blank_levels, register_blank_datasource
from plexora.server.models import manifest
from plexora.server.models.project import IMAGE_KIND_BLANK, Project

from tests.helpers import use_data_root


@pytest.fixture
def blank(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    register_blank_datasource("frame", width=12000, height=9000,
                              pixel_size=0.2125)
    return Project.load("frame")


def test_a_blank_frame_round_trips(blank):
    """And carries no `channelFile`, which is the point: there is no file."""
    entry = blank.to_entry()
    assert entry["image_kind"] == IMAGE_KIND_BLANK
    assert "channelFile" not in entry
    assert entry["imageData"] == []
    assert entry["width"] == 12000 and entry["height"] == 9000
    assert Project.from_entry("frame", entry).to_entry() == entry
    assert blank.image.is_blank


def test_the_frame_zooms_out_to_the_whole_sample(blank):
    """`maxLevel` is a COUNT, and getting it wrong is visible either way.

    Too few and the viewer cannot zoom out to the whole sample; too many and it
    asks for levels past the point the frame has collapsed to a pixel.
    """
    assert blank.image.max_level == _blank_levels(12000, 9000)
    # 12000 -> 6000 -> 3000 -> 1500 -> 750, which is the first that fits a tile.
    assert blank.image.max_level == 5
    assert _blank_levels(500, 500) == 1


def test_a_calibration_read_from_a_file_is_not_reported_as_typed(blank):
    """C4. `_with_pixel_size` used to call every stored size "manual".

    The difference is not cosmetic: "manual" is what the viewer's calibration
    control offers to change, and a number that came out of a run's own
    manifest is evidence, not a guess somebody entered.
    """
    assert blank.image.pixel_size["source"] == "metadata"
    assert blank.image.pixel_size["value"] == pytest.approx(0.2125)


def test_a_blank_sample_reads_as_complete(blank):
    """So it gets the whole plugin pipeline rather than a setup badge.

    Right for a transcripts-only sample: there is nothing missing. The thing
    somebody would have to supply is not an image -- it is a tool that draws
    what is already there.
    """
    summary = manifest.summary(blank)
    assert summary["imageKind"] == IMAGE_KIND_BLANK
    assert summary["needsSetup"] is False
    assert manifest.status(blank, "image")["status"] == manifest.PRESENT


def test_it_has_no_thumbnail_and_that_is_not_an_error(blank):
    from plexora.server.models import data_model

    assert data_model.generate_thumbnail("frame") is None


def test_the_provider_opens_nothing(blank):
    from plexora.server.providers import resolve_providers

    resolved = resolve_providers(blank)
    channels, overview, metadata = resolved.image.open()
    assert channels is None
    # Zero channels deep, because it HAS none: anything counting them off this
    # array gets the true answer and nothing indexes into it.
    assert overview.shape[0] == 0
    assert metadata == {}
    assert resolved.image.path is None


def test_its_tiles_are_transparent_and_cacheable(blank):
    client = plexora.app.test_client()
    response = client.get("/generated/blank/frame/0/0_0.png")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert "max-age=31536000" in response.headers["Cache-Control"]

    from PIL import Image
    import io

    with Image.open(io.BytesIO(response.data)) as image:
        assert image.mode == "RGBA"
        assert image.getextrema()[3] == (0, 0)  # alpha is zero everywhere

    # The same bytes at every level, which is why the ETag is not keyed on a
    # generation counter: re-registering the project cannot change them.
    assert client.get("/generated/blank/frame/4/2_3.png").data == response.data


def test_a_project_with_a_real_image_has_no_blank_route(tmp_path, monkeypatch):
    """404, not a transparent square. A route that answered for every project
    would hide a genuinely broken reference behind an empty frame."""
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "slide.ome.tif"
    tifffile.imwrite(source, np.full((1, 64, 64), 7, dtype=np.uint16))
    plexora.datasource.register_image_datasource("slide", source)

    client = plexora.app.test_client()
    assert client.get("/generated/blank/slide/0/0_0.png").status_code == 404


def test_config_describes_it_as_a_layer(blank):
    client = plexora.app.test_client()
    config = json.loads(client.get("/config").data)["frame"]
    reference = config["layers"][0]
    assert reference["id"] == "__image__"
    assert reference["render"]["imageKind"] == IMAGE_KIND_BLANK
    assert reference.get("channels") is None
    assert config["width"] == 12000
