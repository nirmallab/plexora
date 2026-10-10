"""A volume's first tiles do not wait for the whole-stack window scan.

The exact window of a large stack reads gigabytes; the viewer starts with a
provisional one (server/volume/windows.py) and the exact one is computed in the
background, persisted, and served from then on. Pinned: the provisional window
comes back at once and says so; the exact one replaces it without anyone
asking twice; a tile or a level made for a named window (`w=`) is quantized with
exactly that window, so its address fixes its bytes; and a level made with a
provisional window is never cached by the browser.
"""

import io
import time

import numpy as np
import pytest
from PIL import Image

import plexora
from plexora.datasource import register_image_datasource
from plexora.server.models import data_model
from plexora.server.volume import serve, windows
from tests import volume_fixtures as vf


@pytest.fixture()
def client():
    serve.clear()
    return plexora.app.test_client()


@pytest.fixture()
def background(monkeypatch):
    """Windows as the viewer gets them outside pytest: provisional first."""
    monkeypatch.setattr(data_model, "_WINDOW_SCANS_INLINE", False)
    data_model._provisional_windows.clear()


def _volume(tmp_path, name="vol"):
    path, data = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX", channels=2,
                                     depth=6, levels=2)
    entry = register_image_datasource(name, path)
    data_model.load_datasource(name)
    return entry, data


def _wait_exact(name, datasource, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        window, exact = data_model.volume_window_state(name, datasource)
        if exact:
            return window
        time.sleep(0.05)
    raise AssertionError("the exact window never landed")


def test_the_provisional_window_reads_a_plane_and_a_coarse_sample():
    class Source:
        class volume:
            dtype = np.uint16
            frames = 1
            depth = 100
            levels = [0, 1]

            class default_view:
                t, z = 0, 50

        def level_shape(self, level):
            return (64, 64) if level == 0 else (8, 8)

        reads = []

        def level_array(self, t, z, level):
            Source.reads.append((level, z))
            value = 900 if (level == 0 and z == 50) else 100 + z
            return np.full((1, *self.level_shape(level)), value, np.uint16)

    window, how = windows.provisional_window(Source(), 0)
    assert window == (0.0, 900.0) and how["provisional"] and not how["exact"]
    coarse = [z for level, z in Source.reads if level == 1]
    assert 0 < len(coarse) <= windows.PROVISIONAL_PLANES, "a sample, not every plane"


def test_a_volume_answers_with_a_provisional_window_and_then_the_exact_one(tmp_path, background):
    entry, data = _volume(tmp_path)
    name = entry["imageData"][1]["fullname"]
    window, exact = data_model.volume_window_state(name, "vol")
    if exact:
        pytest.skip("the scan finished before the first answer could be read")
    assert window[1] >= 1
    exact_window = _wait_exact(name, "vol")
    assert exact_window == (0.0, float(data[0, 1].max()))
    stored = data_model._quantization_windows("vol").get(f"{name}|volume")
    assert stored is not None and tuple(stored) == tuple(exact_window), "the exact window is kept"


def test_the_window_route_says_which_window_it_is(tmp_path, client, background):
    entry, data = _volume(tmp_path)
    names = [entry["imageData"][i]["fullname"] for i in (0, 1)]
    first = client.get("/volume/vol/window?" + "&".join(f"channel={n}" for n in names)).get_json()
    assert set(first["windows"]) == set(names)
    for name in names:
        _wait_exact(name, "vol")
    later = client.get(f"/volume/vol/window?channel={names[1]}").get_json()["windows"][names[1]]
    assert later == {"qmin": 0.0, "qmax": float(data[0, 1].max()), "exact": True}


def test_a_tile_made_for_a_named_window_is_quantized_with_it(tmp_path, client):
    entry, data = _volume(tmp_path)
    channel = entry["imageData"][0]["src"].rstrip("/").rsplit("/", 1)[-1]
    url = f"/generated/data/vol/{channel}/0/0_0.png?view=z2"
    plane = data[0, 0, 2]
    for qmax in (float(plane.max()), float(plane.max()) / 2):
        response = client.get(url + f"&w=0,{qmax:g}")
        assert response.status_code == 200
        pixels = np.asarray(Image.open(io.BytesIO(response.data)).convert("L"))
        expected = np.clip(np.rint(plane.astype(np.float32) / qmax * 255), 0, 255)
        assert np.abs(pixels.astype(int) - expected[:pixels.shape[0], :pixels.shape[1]]).max() <= 2
        assert f"-w0,{qmax:g}" in response.headers["ETag"]


def test_a_level_made_for_a_named_window_is_kept_and_a_provisional_one_never(tmp_path, client,
                                                                            background):
    entry, _ = _volume(tmp_path)
    name = entry["imageData"][0]["fullname"]
    named = client.get(f"/volume/vol/level?channel={name}&level=0&w=0,1000")
    assert named.headers["X-Plexora-Window"] == "0,1000"
    assert named.headers["Cache-Control"] == "private, max-age=31536000, immutable"
    data_model._gmm_cache.clear()
    data_model._provisional_windows.clear()
    monkey_store = data_model._quantization_windows("vol")
    monkey_store.pop(f"{name}|volume", None)
    plain = client.get(f"/volume/vol/level?channel={name}&level=0")
    if plain.headers.get("X-Plexora-Window-Provisional") == "1":
        assert plain.headers["Cache-Control"] == "no-store"
    else:
        assert plain.headers["Cache-Control"] == "private, no-cache"


def test_the_stats_packet_says_whether_its_window_is_exact(tmp_path, client):
    entry, _ = _volume(tmp_path)
    name = entry["imageData"][0]["fullname"]
    stats = data_model.get_image_channel_stats(name, "vol")
    assert stats["window_exact"] is True
