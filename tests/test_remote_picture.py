"""A PNG or JPEG at a web address: registered, served whole, tiled on import."""

import io

import numpy as np
import pytest
from PIL import Image

from plexora import datasource
from plexora.server.utils import remote_store
from tests.remote_fixtures import cache_root, http_store  # noqa: F401


@pytest.fixture
def served(tmp_path):
    root = tmp_path / "served"
    root.mkdir(exist_ok=True)
    pixels = np.zeros((120, 200, 3), dtype=np.uint8)
    pixels[..., 0] = 200
    pixels[30:60, 50:90] = (10, 200, 30)
    Image.fromarray(pixels).save(root / "photo.png")
    return root


def test_a_picture_registers_with_its_size(served, http_store):
    server = http_store()
    entry = datasource.register_rgb_datasource(name="pic", image=server.url("photo.png"))
    assert entry["image_kind"] == "rgb"
    assert entry["channelFile"] == server.url("photo.png")
    assert (entry["width"], entry["height"]) == (200, 120)


def test_the_picture_route_serves_the_bytes_once(served, http_store):
    import plexora

    plexora_client = plexora.app.test_client()
    server = http_store()
    datasource.register_rgb_datasource(name="pic", image=server.url("photo.png"))
    first = plexora_client.get("/generated/rgb/pic")
    assert first.status_code == 200
    assert first.mimetype == "image/png"
    assert first.data == (served / "photo.png").read_bytes()
    second = plexora_client.get("/generated/rgb/pic")
    assert second.data == first.data
    assert server.count("photo.png", method="GET") == 1


def test_quick_view_verifies_a_remote_picture(served, http_store):
    from plexora.datasource import _sniff_quick_view_kind

    (served / "broken.png").write_bytes(b"not a png at all")
    server = http_store()
    assert _sniff_quick_view_kind(server.url("photo.png")) == "rgb"
    with pytest.raises(Exception):
        _sniff_quick_view_kind(server.url("broken.png"))


def test_a_reference_picture_is_tiled_into_the_project(served, http_store):
    from plexora.server.models import import_sample

    server = http_store()
    tiled = import_sample._tiled_picture("picsample", server.url("photo.png"))
    assert tiled.exists() and tiled.name.startswith("photo")
    import tifffile as tf

    with tf.TiffFile(str(tiled)) as tiff:
        assert tiff.series[0].shape[:2] == (120, 200)


def test_read_bytes_refuses_a_file_known_to_be_huge(served, http_store, monkeypatch):
    server = http_store()
    url = server.url("photo.png")
    store, key = remote_store.open_store(url)
    store.remember_size(key, 10 ** 9)
    with pytest.raises(remote_store.RemoteReadTooLarge):
        remote_store.read_bytes(url)
    assert io  # keep the import honest
