"""A registered layer's pixels, coloured on the way out.

The layer model was fully built, routed, transformed, panelled and tested, and
a registered image layer had never been DRAWN: `placementFor` and
`/generated/layer/...` both had zero production callers. What joined them is a
world item per layer in the browser and a colour on the tile here.

The colour is here rather than in the shader for one reason: the GL colorize
pass is keyed on `config.imageData` indices, and a registered layer has no entry
in that list. Its tiles are composited by the browser, so they have to arrive
already coloured -- which costs the same LUT quantization a channel tile already
pays, plus a multiply.

What these pin is that the addition is OPT-IN. No `color=` in the query and the
route serves exactly the bytes it served before, which is what keeps the layer
model's existing tests meaningful.
"""

import io

import numpy as np
import pytest
import tifffile
from PIL import Image

import plexora
from plexora.server.models import data_model, layer_sources
from plexora.server.models.project import ImageSpec, LayerSpec, Project

from tests.helpers import use_data_root


@pytest.fixture
def scene(tmp_path, monkeypatch):
    """A reference image with a second image layer registered beside it."""
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    reference = tmp_path / "reference.ome.tif"
    tifffile.imwrite(reference, np.full((2, 256, 256), 100, dtype=np.uint16))
    second = tmp_path / "he.ome.tif"
    tifffile.imwrite(second, np.full((1, 256, 256), 60000, dtype=np.uint16))

    Project(
        name="demo",
        image=ImageSpec(src=str(reference), width=256, height=256, max_level=1,
                        tile_width=256, tile_height=256,
                        channels=({"name": "c0", "fullname": "DNA",
                                   "src": "/generated/data/demo/c0/"},)),
    ).with_layer(LayerSpec(
        id="he", kind="image", label="H&E", modality="he", src=str(second),
        width=256, height=256, max_level=1, tile_width=256, tile_height=256,
        channels=({"name": "he_0", "fullname": "he_0",
                   "src": "/generated/layer/demo/he/he_0/"},),
        transform=(1.0, 0.0, 0.0, 1.0, 500.0, 0.0),
    )).save()
    layer_sources.forget()
    return plexora.app.test_client()


def _decode(payload):
    with Image.open(io.BytesIO(payload)) as image:
        return np.asarray(image.convert("RGB"))


def test_a_plain_request_is_byte_for_byte_what_it_always_was(scene):
    """The colour is opt-in. A caller that asks for no colour gets the grey
    uint16 raster this route has always served, which is what keeps every
    existing layer test meaning what it meant."""
    grey = scene.get("/generated/layer/demo/he/he_0/0/0_0.png?q=hd")
    assert grey.status_code == 200
    assert grey.mimetype == "image/png"
    with Image.open(io.BytesIO(grey.data)) as image:
        assert image.mode in ("I;16", "I")


def test_a_colour_comes_back_as_that_colour(scene):
    """A full-intensity layer asked for in red is red, and nothing else."""
    response = scene.get(
        "/generated/layer/demo/he/he_0/0/0_0.png?color=ff0000&lo=0&hi=60000")
    assert response.status_code == 200
    pixels = _decode(response.data)
    assert pixels[..., 0].min() > 200      # red, at full intensity
    assert pixels[..., 1].max() < 40       # and nothing in the other two
    assert pixels[..., 2].max() < 40


def test_the_window_is_what_decides_the_brightness(scene):
    """`lo`/`hi` ride the url because they change while the user drags a
    slider -- presentation, which the server never acts on except here."""
    bright = _decode(scene.get(
        "/generated/layer/demo/he/he_0/0/0_0.png?color=ffffff&lo=0&hi=60000").data)
    dim = _decode(scene.get(
        "/generated/layer/demo/he/he_0/0/0_0.png?color=ffffff&lo=0&hi=600000").data)
    assert bright.mean() > dim.mean() * 4


def test_the_etag_carries_the_style(scene):
    """The same url with a different colour is a different picture. Without
    this the browser would show the old colour until a reload."""
    red = scene.get("/generated/layer/demo/he/he_0/0/0_0.png?color=ff0000")
    blue = scene.get("/generated/layer/demo/he/he_0/0/0_0.png?color=0000ff")
    assert red.headers["ETag"] != blue.headers["ETag"]

    again = scene.get("/generated/layer/demo/he/he_0/0/0_0.png?color=ff0000",
                      headers={"If-None-Match": red.headers["ETag"]})
    assert again.status_code == 304


def test_a_malformed_colour_is_ignored_rather_than_refused(scene):
    """A tile request is not a form. What a bad `color=` means is "draw it the
    way you would have anyway", not a 400 that leaves a hole on screen."""
    assert layer_sources.parse_style({"color": "not-a-colour"}) is None
    assert scene.get(
        "/generated/layer/demo/he/he_0/0/0_0.png?color=zzz").status_code == 200


def test_layer_tiles_never_load_the_datasource(scene, monkeypatch):
    """The invariant the whole module exists for, re-asserted with the colour
    path in place: `data_model` holds ONE open project in module globals, and
    routing layer tiles through it would evict the user's session on every pan
    of a second layer."""
    calls = []
    monkeypatch.setattr(data_model, "load_datasource",
                        lambda *a, **k: calls.append(a))

    for _ in range(8):
        scene.get("/generated/layer/demo/he/he_0/0/0_0.png?color=00ff00")
    assert calls == []


def test_a_points_layer_without_a_manifest_still_has_no_tiles(scene):
    """Retargeted from "a points layer has none" -- one WITH a built manifest
    now serves density, which is what makes the low-zoom transcript view cost
    no new client rendering code. Without one there is nothing built yet, and
    404 is what the card turns into "Preparing...".
    """
    Project.mutate("demo", lambda p: p.with_layer(
        LayerSpec(id="tx", kind="points", modality="transcripts")))
    layer_sources.forget()
    assert layer_sources.layer_tile("demo", "tx", "density", 0, "0_0") is None


def test_the_density_window_scales_with_the_level():
    """A bin at level L covers 4^L times the area, so it holds about 4^L times
    as many transcripts. One stored `density_max` would make every zoomed-out
    view black; this is why the window is derived instead."""
    # A real Xenium run's order of magnitude: 50 million molecules over a
    # 12,000x9,000 px morphology image, which is about half a molecule per
    # pixel.
    manifest = {"point_count": 50_000_000, "width": 12_000, "height": 9_000}
    at_zero = layer_sources.density_window(manifest, 0)
    at_two = layer_sources.density_window(manifest, 2)
    assert at_two == pytest.approx(at_zero * 16, rel=0.1)

    # Never zero, whatever the arithmetic says: a window of zero is a division
    # by zero in the quantizer, and one bin holding one transcript is the least
    # a density raster can distinguish anyway.
    assert layer_sources.density_window({"point_count": 1, "width": 1e6,
                                         "height": 1e6}, 0) == 1
    assert layer_sources.density_window({}, 0) == 1
