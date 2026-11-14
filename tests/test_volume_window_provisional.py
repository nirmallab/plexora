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


@pytest.fixture()
def no_warm(monkeypatch):
    """No warm-up thread after a load, scanning while the test looks."""
    monkeypatch.setattr(data_model, "_warm_datasource_caches", lambda *_a, **_k: None)


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


def test_a_volumes_fit_answers_at_once_with_the_provisional_window(tmp_path, background, no_warm,
                                                                   monkeypatch):
    """The viewer's provisional ask for a volume's fit is not held behind the
    stack's scan -- on the website that scan held the one Python thread for
    minutes. The same fit comes back with the exact window once it is known,
    and a plain ask still waits for the exact one."""
    entry, data = _volume(tmp_path)
    name = entry["imageData"][1]["fullname"]
    queued = []
    monkeypatch.setattr(data_model, "queue_exact_window", lambda *a, **k: queued.append(a))

    def no_scan(*_args, **_kwargs):
        raise AssertionError("the provisional ask scanned the stack")

    real_scan = windows.volume_window
    monkeypatch.setattr(windows, "volume_window", no_scan)
    first = data_model.get_channel_gmm(name, "vol", provisional=True)
    provisional, exact = data_model.volume_window_state(name, "vol")
    assert first["window_exact"] is False and not exact and queued
    assert (first["qmin"], first["qmax"]) == provisional
    monkeypatch.setattr(windows, "volume_window", real_scan)
    exact_window = data_model._volume_quantization_window(name, "vol")
    assert exact_window == (0.0, float(data[0, 1].max()))
    later = data_model.get_channel_gmm(name, "vol", provisional=True)
    assert later.get("window_exact", True) is True
    assert (later["qmin"], later["qmax"]) == exact_window
    assert (later["vmin"], later["vmax"]) == (first["vmin"], first["vmax"]), "the same fit"
    assert data_model.get_channel_gmm(name, "vol") == later


class _Stack:
    """A 100-plane uint16 stack of 4 x 4 chunks of 16 px a plane at level 0,
    every read recorded: one bright pixel in the default plane's chunk (1, 1),
    which a lattice of four over its 4 x 4 visits."""

    class volume:
        dtype = np.uint16
        frames = 1
        depth = 100
        levels = [0, 1]

        class default_view:
            t, z = 0, 50

    def __init__(self):
        self.reads = []

    def level_shape(self, level):
        return (64, 64) if level == 0 else (8, 8)

    def decode_chunk(self, level):
        return (1, 16, 16)

    def level_array(self, t, z, level):
        self.reads.append(("array", level, z))
        return np.full((1, *self.level_shape(level)), 100, np.uint16)

    def read_plane(self, level, t, z, channel, rows, cols):
        self.reads.append(("plane", level, z, rows.start, cols.start))
        block = np.full((rows.stop - rows.start, cols.stop - cols.start), 200, np.uint16)
        if (level, z, rows.start, cols.start) == (0, 50, 16, 16):
            block[3, 3] = 4000
        return block


def test_a_web_stacks_window_on_the_website_is_a_sample_with_headroom():
    stack = _Stack()
    window, how = windows.sampled_window(stack, 0, chunks=4, headroom=1.25)
    assert window == (0.0, 4000 * 1.25) and how["sampled"] and not how["exact"]
    chunks = [r for r in stack.reads if r[0] == "plane"]
    assert len(chunks) == 4 and {r[2] for r in chunks} == {50}, "level-0 chunks of the default plane"
    assert len({(r[3], r[4]) for r in chunks}) == 4, "spread, each once"
    # Never above what the type holds.
    window, _ = windows.sampled_window(stack, 0, chunks=4, headroom=100.0)
    assert window == (0.0, 65535.0)


def test_only_the_website_samples_a_web_stacks_window(tmp_path, no_warm, monkeypatch):
    """`_volume_quantization_window` asks the provider for a sample only where
    nothing else can run meanwhile; the desktop scans as before."""
    entry, data = _volume(tmp_path)
    name = entry["imageData"][0]["fullname"]
    asked = []

    def sampled(self, source, channel, gate=None):
        asked.append(channel)
        return (0.0, 1234.0), {"exact": False, "sampled": True}

    monkeypatch.setattr(type(data_model._providers.image), "volume_window", sampled, raising=False)
    assert data_model._volume_quantization_window(name, "vol") == (0.0, float(data[0, 0].max()))
    assert not asked, "the desktop scans"
    data_model._gmm_cache.clear()
    data_model._quantization_windows("vol").pop(f"{name}|volume", None)
    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    assert data_model._volume_quantization_window(name, "vol") == (0.0, 1234.0)
    assert asked == [0]


def test_without_threads_each_exact_window_is_a_background_task_of_its_own(tmp_path, no_warm,
                                                                          monkeypatch):
    """The website: the exact windows wait for requests to be quiet, and each
    channel's is a task of its own, so a tile asked for meanwhile waits for
    one channel's at most."""
    from plexora import _work

    entry, data = _volume(tmp_path)
    names = [entry["imageData"][i]["fullname"] for i in (0, 1)]
    monkeypatch.setattr(data_model, "_exact_window_queue", [])
    # An earlier test's worker thread waits up to 30 s for more channels;
    # woken with none, it ends -- rather than taking one of these.
    old = data_model._exact_window_worker
    for _ in range(100):
        if old is None or not old.is_alive():
            break
        with data_model._exact_window_cv:
            data_model._exact_window_cv.notify_all()
        old.join(0.05)
    assert old is None or not old.is_alive()
    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    _work._reset_for_tests()
    monkeypatch.setattr(data_model, "_exact_window_worker", None)
    try:
        for name in names:
            data_model.queue_exact_window(name, "vol")
        assert _work.pending(background=False) == 0 and _work.pending(background=True) == 1
        assert _work.run_pending(budget_s=0) == 1
        assert _work.pending(background=True) == 1, "the second channel's queued behind it"
        assert _work.run_pending() == 1 and _work.pending() == 0
        assert [data_model.volume_window_state(name, "vol")[0] for name in names] == [
            (0.0, float(data[0, 0].max())), (0.0, float(data[0, 1].max()))]
    finally:
        _work._reset_for_tests()


def test_the_remote_provider_samples_unless_the_store_is_offline(monkeypatch):
    from plexora.server.models import remote_sources
    from plexora.server.providers import remote

    provider = remote.RemoteImageProvider("https://example.org/stack.zarr")
    calls = []
    monkeypatch.setattr(windows, "sampled_window",
                        lambda source, channel, gate=None, **kw: calls.append(("sampled", kw)) or ((0.0, 1.0), {}))
    monkeypatch.setattr(windows, "volume_window",
                        lambda source, channel, gate=None: calls.append(("scan", {})) or ((0.0, 2.0), {}))
    monkeypatch.setattr(remote_sources, "is_pinned", lambda path: False)
    provider.volume_window(object(), 0)
    monkeypatch.setattr(remote_sources, "is_pinned", lambda path: True)
    provider.volume_window(object(), 0)
    assert calls == [("sampled", {"chunks": remote.WINDOW_SAMPLE_CHUNKS, "headroom": remote.WINDOW_HEADROOM}),
                     ("scan", {})]
