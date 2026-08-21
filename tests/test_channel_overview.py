"""The whole-tissue overview the viewer's mini-map draws from.

It exists because there is no tile that reliably holds the whole image.
`tileWidth` is fixed at 1024 while pyramid depth is whatever wrote the file --
convertOmeTiff reads `len(channels)`, it does not build the pyramid -- so the
coarsest level is a single tile for some files and a 4x4 grid for others. The
overview route sidesteps that by serving `zarray`, the downsampled array
load_datasource already keeps resident.

The one thing that has to stay true is the quantization: these bytes are the
input to a contrast window the client applies on top, so they have to be in the
same [0, 255] domain the WebP tiles are in. Anything else and the mini-map
disagrees with the viewer it is an overview of, in a way that reads as "a bit
dark" rather than as a bug.

The data_model globals are owned via monkeypatch throughout -- see SKILL.md on
how they leak between test files.
"""

import io
import json

import numpy as np
import pytest
import tifffile
from PIL import Image

import plexora
from plexora import datasource
from plexora.server.models import data_model


#: Everything load_datasource writes. Nulled per test rather than snapshotted,
#: matching the isolate_data_model fixtures in test_metadata_columns.py and the
#: ROI plugin's tests.
_DATA_MODEL_GLOBALS = (
    "source", "_loaded_source", "config", "channels", "seg", "zarray",
    "datasource", "ball_tree", "metadata",
)


def _write_image(path, size=512, channels=3):
    """A synthetic source. (channels, 256, 256) is the floor -- a
    single-channel write comes back 2D and data_model indexes shape[2], and
    the pyramid walk needs every dimension >= 200 -- but 512 is deliberate:
    load_datasource only block_reduces `zarray` above 400 px a side, so a
    smaller fixture would leave it at full resolution and every test here
    about pooling would pass without exercising any."""
    rng = np.random.default_rng(7)
    tifffile.imwrite(
        path, rng.integers(1, 4000, size=(channels, size, size), dtype=np.uint16)
    )


@pytest.fixture
def loaded(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    _write_image(image_path)

    datasource.register_image_datasource(
        name="overview_sample", image=image_path, data_dir=data_dir
    )

    monkeypatch.setattr(data_model, "config_json_path", data_dir / "config.json")
    monkeypatch.setattr(data_model, "data_path", data_dir)
    # Own every global load_datasource writes, so this file is neither served
    # the previous one's datasource nor able to leave its own behind.
    for name in _DATA_MODEL_GLOBALS:
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)
    monkeypatch.setattr(data_model, "_gmm_cache", {})
    monkeypatch.setattr(data_model, "_image_stats_cache", {})

    # The background cache-warming thread load_datasource spawns is disabled
    # suite-wide (see the repo-root conftest.py) -- it outlives the test that
    # starts it and rewrites these globals afterwards.

    data_model.load_datasource("overview_sample", reload=True)
    names = [
        d["fullname"] for d in data_model.config["overview_sample"]["imageData"]
        if d["fullname"] != "Area"
    ]
    return "overview_sample", names


def _decode(payload):
    """Overview bytes back to one plane. PIL hands back RGB for a mode-'L'
    WebP -- the container has no grey mode -- so this also pins that the three
    components really are identical, which is what lets the client read a
    single byte per pixel."""
    array = np.asarray(Image.open(io.BytesIO(payload)))
    assert array.ndim == 3, array.shape
    assert (array[..., 0] == array[..., 1]).all()
    assert (array[..., 1] == array[..., 2]).all()
    return array[..., 0]


def test_the_overview_covers_the_whole_image(loaded):
    name, names = loaded
    plane = _decode(data_model.generate_channel_overview(name, names[0]))
    assert plane.shape == data_model.zarray[0].shape


def test_the_overview_is_in_the_same_byte_domain_as_the_tiles(loaded):
    """The client applies its contrast window to these bytes directly, so they
    have to be quantized against the window encode_tile uses -- not rescaled
    to their own min/max, which would look right on its own and disagree with
    every tile the moment a slider moved."""
    name, names = loaded
    for channel in names:
        qmin, qmax = data_model.get_channel_quantization_window(channel, name)
        expected = data_model._quantize_to_uint8(
            np.asarray(data_model.zarray[names.index(channel)]), qmin, qmax - qmin
        )
        assert (_decode(data_model.generate_channel_overview(name, channel)) == expected).all()


def test_the_quantization_ceiling_still_comes_from_full_resolution(loaded):
    """The pixels are mean-pooled; the ceiling must not be. Deriving it from
    `zarray` is cheaper and wrong -- pooling dilutes real single-pixel peaks,
    so the ceiling lands far below what full-resolution data contains and whole
    channels saturate to one flat colour. get_channel_quantization_window's
    docstring records this as verified live."""
    name, names = loaded
    _, qmax = data_model.get_channel_quantization_window(names[0], name)
    full_res_max = float(np.asarray(data_model._zarr_level(data_model.channels, 0)[0]).max())
    pooled_max = float(np.asarray(data_model.zarray[0]).max())
    assert qmax == pytest.approx(full_res_max)
    # If this stops being true the test above can no longer tell the two apart.
    assert pooled_max < full_res_max


def test_the_overview_is_lossless(loaded):
    """Lossy WebP at quality 90 costs up to 11 grey levels on an array this
    size. Tiles can afford that; the mini-map cannot, because a narrow contrast
    window multiplies a small byte error into a large visible one."""
    name, names = loaded
    payload = data_model.generate_channel_overview(name, names[0])
    qmin, qmax = data_model.get_channel_quantization_window(names[0], name)
    expected = data_model._quantize_to_uint8(
        np.asarray(data_model.zarray[0]), qmin, qmax - qmin
    )
    assert (_decode(payload) == expected).all()


def test_a_channel_this_datasource_does_not_have_is_not_an_overview(loaded):
    name, _ = loaded
    assert data_model.generate_channel_overview(name, "no_such_channel") is None


def test_the_area_placeholder_does_not_shift_the_channel_index(loaded, monkeypatch):
    """`zarray` holds only the real image channels. 'Area' is a Plexora-side UI
    placeholder that sits at imageData[0] whenever a segmentation exists, so a
    raw imageData index is off by one for every channel of every segmented
    project -- the mini-map would draw each channel in its neighbour's colour."""
    name, names = loaded
    entry = data_model.config[name]
    without_area = data_model.generate_channel_overview(name, names[1])

    patched = json.loads(json.dumps(entry))
    patched["imageData"] = [{"name": "Area", "fullname": "Area", "src": "/x/"}] + entry["imageData"]
    monkeypatch.setitem(data_model.config, name, patched)
    data_model._gmm_cache.clear()

    assert data_model.generate_channel_overview(name, names[1]) == without_area


# -- the route ------------------------------------------------------------

@pytest.fixture
def client(loaded, monkeypatch, tmp_path):
    name, names = loaded
    monkeypatch.setattr(plexora, "data_path", data_model.data_path)
    monkeypatch.setattr(plexora, "config_json_path", data_model.config_json_path)
    return plexora.app.test_client(), name, names


def test_the_route_serves_an_image(client):
    test_client, name, names = client
    response = test_client.get(f"/generated/overview/{name}/{names[0]}")
    assert response.status_code == 200
    assert response.mimetype == "image/webp"
    assert _decode(response.data).shape == data_model.zarray[0].shape


def test_the_route_lets_the_browser_keep_it(client):
    """Same bargain as the tile route: immutable for a given load_generation,
    with the generation in the ETag rather than the URL so a reload invalidates
    without the client rewriting anything."""
    test_client, name, names = client
    response = test_client.get(f"/generated/overview/{name}/{names[0]}")
    etag = response.headers["ETag"]
    assert str(data_model.load_generation) in etag
    assert response.headers["Cache-Control"] == "private, max-age=31536000"

    conditional = test_client.get(
        f"/generated/overview/{name}/{names[0]}", headers={"If-None-Match": etag}
    )
    assert conditional.status_code == 304


def test_an_unknown_channel_is_a_404(client):
    test_client, name, _ = client
    assert test_client.get(f"/generated/overview/{name}/nope").status_code == 404
