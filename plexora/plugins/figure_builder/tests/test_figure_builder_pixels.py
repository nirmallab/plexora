"""Raw channel pixels for Quick Edit.

The claim being tested is mostly about what these routes do NOT do. Quick Edit
needs real pixels from a figure's source, and the obvious way to get them --
the main viewer's own tile routes -- goes through `data_model`, which holds one
loaded datasource behind a lock and loads the cell table and the segmentation
mask with it. Quick Edit would pay that per panel, evict whatever the user had
open, and pay it again on the way back. So these read the TIFF directly, the
same way export does, and `test_reading_pixels_never_loads_a_datasource` pins
it the same way -- by making the loader explode.

The other half is the shape of the answer: ONE channel, greyscale, uint16, no
compositing. That is what makes dragging a contrast slider in Quick Edit cost
no network at all, which is the whole reason the feature is worth having.
"""

import json

import numpy as np
import pytest
import tifffile

import plexora
from plexora.plugins.figure_builder.server import pixels, render, repository
from plexora.server import plugins as plugin_registry
from plexora.server.models import data_model, database_model
from tests.helpers import ALL_CONFIRMED, image_spec, project, use_data_root

API = "/plugins/figure_builder/api"

IMAGE_WIDTH = 512
IMAGE_HEIGHT = 384
DNA_VALUE = 50
CD8_VALUE = 4000


@pytest.fixture
def figure(tmp_path, monkeypatch):
    """A registered project with a real two-channel TIFF, and a figure on it."""
    image_path = tmp_path / "image.ome.tif"
    plane = np.zeros((2, IMAGE_HEIGHT, IMAGE_WIDTH), dtype=np.uint16)
    plane[0, :, :] = DNA_VALUE
    plane[1, :, :] = CD8_VALUE
    # One bright corner, so a region read can be shown to come from the right
    # PART of the image rather than merely from the right image.
    plane[0, 0:32, 0:32] = 60000
    tifffile.imwrite(image_path, plane)

    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "demo": project("demo", image=image_spec(
            channels=("DNA", "CD8"), width=IMAGE_WIDTH, height=IMAGE_HEIGHT,
            src=str(image_path)), confirmed=ALL_CONFIRMED).to_entry(),
    }), encoding="utf-8")

    figure_id = repository.create("Figure 1")
    repository.apply(figure_id, 0, [
        {"op": "add_source", "source": {
            "source_id": "src_1", "kind": "plexora_project", "datasource": "demo",
            "image": {"width": IMAGE_WIDTH, "height": IMAGE_HEIGHT},
            "channels": [{"key": "DNA"}, {"key": "CD8"}]}},
        {"op": "add_source", "source": {
            "source_id": "src_asset", "kind": "imported_asset", "asset_id": "ast_1"}},
    ])
    return figure_id


@pytest.fixture
def client(figure):
    if plugin_registry.find(plexora.app, "figure_builder") is None:  # pragma: no cover
        pytest.skip("figure_builder is not installed")
    return plexora.app.test_client()


def region_url(figure, **params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"{API}/figures/{figure}/sources/src_1/pixels?{query}"


# -- the reader ------------------------------------------------------------

def test_a_region_comes_back_at_the_size_that_was_asked_for(figure):
    array, _ = pixels.read_region("demo", "DNA", (0, 0, 256, 192), (64, 48))
    assert array.shape == (48, 64)
    assert array.dtype == np.uint16


def test_a_region_is_read_from_the_part_of_the_image_it_names(figure):
    """The bright corner is 32x32 at the origin. A read of the far corner must
    not contain it -- a route that ignored the box and returned the whole image
    would look right in every other assertion here."""
    corner, _ = pixels.read_region("demo", "DNA", (0, 0, 32, 32), (8, 8))
    far, _ = pixels.read_region("demo", "DNA", (256, 192, 32, 32), (8, 8))
    assert corner.max() > 50000
    assert far.max() == pytest.approx(DNA_VALUE, abs=2)


def test_sixteen_bits_survive_the_round_trip(figure):
    """Windows are chosen in raw units. An 8-bit response would have to be
    pre-windowed, and the slider could then only move inside whatever window the
    server happened to pick."""
    array, _ = pixels.read_region("demo", "CD8", (0, 0, 64, 64), (16, 16))
    assert array.max() == pytest.approx(CD8_VALUE, abs=2)


def test_a_region_running_off_the_slide_reports_what_it_actually_read(figure):
    """Clipped rather than refused -- a capture that overhangs the edge is an
    ordinary thing to have drawn. The clipped box comes back so the caller can
    draw it in the right PLACE; given only the pixels it would centre the wrong
    thing."""
    _, box = pixels.read_region("demo", "DNA", (400, 300, 400, 300), (32, 32))
    assert box[2] == pytest.approx(IMAGE_WIDTH, abs=1)
    assert box[3] == pytest.approx(IMAGE_HEIGHT, abs=1)


def test_a_region_larger_than_the_ceiling_is_refused(figure):
    with pytest.raises(render.RenderError, match="past what one read"):
        pixels.read_region("demo", "DNA", (0, 0, 100, 100),
                           (pixels.MAX_OUT_PIXELS + 1, 10))


def test_a_channel_that_is_not_in_the_file_is_named_rather_than_substituted(figure):
    with pytest.raises(render.RenderError, match="not a channel"):
        pixels.read_region("demo", "NOT_A_CHANNEL", (0, 0, 64, 64), (16, 16))


def test_channel_stats_describe_a_slider_domain(figure):
    stats = pixels.channel_stats("demo", "CD8")
    assert stats["min"] == pytest.approx(CD8_VALUE, abs=2)
    assert stats["max"] == pytest.approx(CD8_VALUE, abs=2)
    assert "p01" in stats and "p999" in stats


def test_reading_pixels_never_loads_a_datasource(figure, monkeypatch):
    """The whole reason this module exists rather than reusing the viewer's
    routes. `load_datasource` holds ONE datasource behind a lock and loads the
    cell table and the segmentation with it; Quick Edit would evict the user's
    session per panel. Asserted by making the loader explode, because the wrong
    call would otherwise merely be slow -- and slow passes."""
    def explode(*args, **kwargs):  # pragma: no cover - the point is not calling it
        raise AssertionError("Quick Edit must not load a datasource")

    monkeypatch.setattr(data_model, "load_datasource", explode)
    pixels.read_region("demo", "DNA", (0, 0, 128, 128), (32, 32))
    pixels.channel_stats("demo", "DNA")


# -- the routes ------------------------------------------------------------

def test_the_route_returns_raw_bytes_with_the_shape_in_a_header(client, figure):
    response = client.get(region_url(figure, channel="DNA", x=0, y=0, w=128, h=96,
                                     out_w=32, out_h=24))
    assert response.status_code == 200
    assert response.mimetype == "application/octet-stream"
    assert response.headers["X-Fb-Shape"] == "32x24"
    # Little-endian uint16, row-major: 32 * 24 * 2 bytes and nothing else.
    assert len(response.data) == 32 * 24 * 2
    values = np.frombuffer(response.data, dtype="<u2")
    assert values.max() > 0


def test_the_route_says_which_box_it_actually_read(client, figure):
    response = client.get(region_url(figure, channel="DNA", x=400, y=300, w=400,
                                     h=300, out_w=32, out_h=32))
    box = [float(part) for part in response.headers["X-Fb-Box"].split(",")]
    assert box[2] == pytest.approx(IMAGE_WIDTH, abs=1)


def test_pixels_are_revalidated_rather_than_cached(client, figure):
    """Same reasoning as a preview: what is behind one URL changes whenever the
    panel does."""
    response = client.get(region_url(figure, channel="DNA", x=0, y=0, w=64, h=64,
                                     out_w=16, out_h=16))
    assert "no-cache" in response.headers["Cache-Control"]


def test_a_source_that_is_not_a_project_image_is_refused(client, figure):
    response = client.get(
        f"{API}/figures/{figure}/sources/src_asset/pixels"
        f"?channel=DNA&x=0&y=0&w=64&h=64&out_w=16&out_h=16")
    assert response.status_code == 400
    assert response.get_json()["error"] == "not_a_project_image"


def test_an_unknown_source_is_404(client, figure):
    response = client.get(
        f"{API}/figures/{figure}/sources/src_nope/pixels"
        f"?channel=DNA&x=0&y=0&w=64&h=64&out_w=16&out_h=16")
    assert response.status_code == 404


def test_a_region_with_no_size_is_400(client, figure):
    response = client.get(region_url(figure, channel="DNA", x=0, y=0, w=0, h=0,
                                     out_w=16, out_h=16))
    assert response.status_code == 400


def test_a_region_past_the_ceiling_is_400_rather_than_a_huge_response(client, figure):
    response = client.get(region_url(
        figure, channel="DNA", x=0, y=0, w=128, h=128,
        out_w=pixels.MAX_OUT_PIXELS + 1, out_h=16))
    assert response.status_code == 400


def test_pixel_info_answers_with_a_slider_domain(client, figure):
    response = client.get(
        f"{API}/figures/{figure}/sources/src_1/pixel_info?channel=CD8")
    assert response.status_code == 200
    stats = response.get_json()["stats"]
    assert stats["max"] == pytest.approx(CD8_VALUE, abs=2)


def test_pixel_info_for_a_channel_that_is_gone_is_400(client, figure):
    response = client.get(
        f"{API}/figures/{figure}/sources/src_1/pixel_info?channel=GONE")
    assert response.status_code == 400
