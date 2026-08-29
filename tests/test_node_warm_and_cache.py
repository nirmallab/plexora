"""What a node keeps in memory, and when it fills it.

Three changes are covered here, and they exist because of one measurement: a
node that had just restarted underneath an open project took 7.3 s to fill a
viewport that took 0.7 s once warm. Nothing was broken -- the primary's caches
survive a node restart, so its own warm-up finds everything cached and asks the
node for nothing, and the node stayed cold until a user zoomed.

- The node caches encoded tiles, as the primary already did. Direct routing
  sends the browser straight here, so the primary's cache is not in the path
  at all and there was nothing on this side to take its place.
- A node warms itself when it starts serving something, rather than doing the
  pyramid open and the per-channel quantization reads inside a user's request.
- The resource lock lets readers share, so one channel's mixture fit -- a
  second of CPU touching no file -- no longer blocks every tile of every
  channel.

The assertions are about *work done*, not about wall-clock time: a test that
asserted "the second request was faster" would pass on a fast machine whatever
the code did.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
import tifffile

from plexora.server.node import api as node_api
from plexora.server.node import resources as node_resources
from plexora.server.node.app import create_node_app, warm_resources


TOKEN_HEADER = {"X-Plexora-Node-Token": "x"}
CHANNELS = 3
SIZE = 512


def _quiet(*_args, **_kwargs):
    pass


@pytest.fixture(autouse=True)
def _empty_tile_cache():
    """The tile cache is module state, so a test must not inherit another's.

    Cleared after as well as before: leaving entries behind would make the next
    test in the file pass for a reason it did not intend.
    """
    node_api._tile_cache.clear()
    yield
    node_api._tile_cache.clear()


def _image_file(directory, name="slide.ome.tif"):
    """A small multi-channel image with real structure in it.

    Not zeros, for the reason `test_node_image.py` gives: the quantization
    window is a max over full-resolution pixels, so a blank image would make
    these pass for the wrong reason.
    """
    rng = np.random.default_rng(11)
    data = np.zeros((CHANNELS, SIZE, SIZE), dtype=np.uint16)
    for index in range(CHANNELS):
        data[index] = rng.poisson(40 * (index + 1), (SIZE, SIZE)).astype(np.uint16)
        data[index, 100:160, 100:160] += 4000 * (index + 1)
    path = directory / name
    tifffile.imwrite(path, data, photometric="minisblack")
    return path


def _node(tmp_path):
    image = _image_file(tmp_path)
    app = create_node_app([f"image:slide={image}"], token="x", log=_quiet)
    return app, app.config["PLEXORA_NODE_RESOURCES"].get("slide")


# -- the tile cache -------------------------------------------------------


def test_a_repeated_tile_is_not_read_or_encoded_twice(tmp_path, monkeypatch):
    """The point of the cache, stated as the work it removes."""
    from plexora.server.models import data_model

    app, _resource = _node(tmp_path)
    reads = []
    real = data_model.read_tile
    monkeypatch.setattr(data_model, "read_tile",
                        lambda *a, **k: (reads.append(1), real(*a, **k))[1])

    client = app.test_client()
    first = client.get("/node/v1/image/slide/tile/ch_0/0/0_0", headers=TOKEN_HEADER)
    second = client.get("/node/v1/image/slide/tile/ch_0/0/0_0", headers=TOKEN_HEADER)

    assert first.status_code == second.status_code == 200
    # Byte-identical, because the second answer never went near the file. This
    # is the same property the primary relies on to forward node bytes without
    # decoding them.
    assert first.data == second.data
    assert len(reads) == 1, "the second request re-read the pyramid"


def test_different_tiles_and_channels_do_not_collide(tmp_path):
    """A cache key that dropped any of these would serve the wrong picture,
    silently -- the worst failure this code can have.

    Addressed in a 256 px grid rather than the default 1024, because the test
    image is one 512 px level: at the default there is exactly one tile and no
    two requests to tell apart. `level` is not an axis here for the same
    reason -- a single-level pyramid answers every level with the same pixels,
    so a difference would prove nothing about the key.
    """
    app, _resource = _node(tmp_path)
    client = app.test_client()

    def tile(path):
        answer = client.get(path, headers=TOKEN_HEADER)
        assert answer.status_code == 200
        return answer.data

    grid = "?tw=256&th=256"
    one = tile("/node/v1/image/slide/tile/ch_0/0/0_0" + grid)
    other_channel = tile("/node/v1/image/slide/tile/ch_1/0/0_0" + grid)
    other_tile = tile("/node/v1/image/slide/tile/ch_0/0/1_0" + grid)
    other_quality = tile("/node/v1/image/slide/tile/ch_0/0/0_0" + grid + "&q=hd")
    assert one != other_channel
    assert one != other_tile
    assert one != other_quality


def test_the_tile_grid_is_part_of_the_key(tmp_path):
    """Two projects can point at one node image with different tile grids, and
    `tw`/`th` decide which pixels a tile name refers to. A key without them
    would hand the second project the first one's crop."""
    app, _resource = _node(tmp_path)
    client = app.test_client()

    def tile(query):
        answer = client.get("/node/v1/image/slide/tile/ch_0/0/0_0" + query,
                            headers=TOKEN_HEADER)
        assert answer.status_code == 200
        return answer.data

    assert tile("?tw=256&th=256") != tile("?tw=512&th=512")


def test_a_reload_makes_cached_tiles_unreachable(tmp_path):
    """Generation is in the key, so a reopen invalidates by construction
    rather than by remembering to go and find the stale entries."""
    app, resource = _node(tmp_path)
    client = app.test_client()
    client.get("/node/v1/image/slide/tile/ch_0/0/0_0", headers=TOKEN_HEADER)
    cached_under = {key[1] for key in node_api._tile_cache}
    assert cached_under == {resource.generation}

    resource.generation += 1
    client.get("/node/v1/image/slide/tile/ch_0/0/0_0", headers=TOKEN_HEADER)
    assert {key[1] for key in node_api._tile_cache} == {
        resource.generation - 1, resource.generation}


def test_the_cache_is_bounded(tmp_path, monkeypatch):
    """An unbounded tile cache on a long-lived node is a memory leak with a
    pleasant name."""
    monkeypatch.setattr(node_api, "_TILE_CACHE_MAX", 4)
    app, _resource = _node(tmp_path)
    client = app.test_client()
    for y in range(8):
        client.get(f"/node/v1/image/slide/tile/ch_0/0/0_{y}", headers=TOKEN_HEADER)
    assert len(node_api._tile_cache) <= 4


# -- warming --------------------------------------------------------------


def test_warming_opens_the_pyramid_and_reads_every_quantization_window(tmp_path):
    """What a first zoom would otherwise have paid for, paid before it."""
    app, resource = _node(tmp_path)
    assert resource.opened is None, "a freshly built node should not have opened anything"

    warm_resources(app.config["PLEXORA_NODE_RESOURCES"], log=_quiet).join(timeout=120)

    assert resource.opened is not None
    for index in range(CHANNELS):
        assert ("qwindow", index) in resource.derived, (
            f"channel {index} would still be read inside a user's first tile")


def test_warming_does_not_fit_mixtures(tmp_path):
    """Deliberately out of scope: a GMM only refines contrast, costs about a
    second each, and the primary keeps its own copy across a node restart --
    so fitting them here is work for an answer nobody asks this node for."""
    app, resource = _node(tmp_path)
    warm_resources(app.config["PLEXORA_NODE_RESOURCES"], log=_quiet).join(timeout=120)
    assert not [key for key in resource.derived if key[0] == "gmm"]


def test_a_resource_that_cannot_be_warmed_does_not_take_the_node_down(tmp_path):
    """Warming is best-effort by construction. A node that refused to serve
    its other resources because one file had gone would be trading a partial
    answer for no answer."""
    app, resource = _node(tmp_path)

    def explode():
        raise OSError("the file went away")

    resource.provider.open = explode
    # Joins cleanly and leaves the node usable, rather than raising on a
    # daemon thread where nothing would see it.
    warm_resources(app.config["PLEXORA_NODE_RESOURCES"], log=_quiet).join(timeout=120)
    assert app.test_client().get("/node/v1/health", headers=TOKEN_HEADER).status_code == 200


# -- the readers-writer lock ---------------------------------------------


def test_readers_share_and_writers_exclude():
    """The property the whole change rests on, asserted directly."""
    lock = node_resources.RWLock()
    seen = {"readers": 0, "peak": 0, "writer": False}
    faults = []
    guard = threading.Lock()

    def reader():
        for _ in range(80):
            with lock.read:
                with guard:
                    if seen["writer"]:
                        faults.append("a reader ran during a write")
                    seen["readers"] += 1
                    seen["peak"] = max(seen["peak"], seen["readers"])
                time.sleep(0.001)
                with guard:
                    seen["readers"] -= 1

    def writer():
        for _ in range(20):
            with lock:
                with guard:
                    if seen["writer"]:
                        faults.append("two writers at once")
                    if seen["readers"]:
                        faults.append("a write ran during a read")
                    seen["writer"] = True
                time.sleep(0.001)
                with guard:
                    seen["writer"] = False

    threads = ([threading.Thread(target=reader) for _ in range(6)]
               + [threading.Thread(target=writer) for _ in range(2)])
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not faults
    assert seen["peak"] > 1, "readers never actually overlapped"


def test_the_write_lock_is_reentrant_and_admits_a_nested_read():
    """It replaced an RLock, and `_cached` reads inside code that may already
    hold the write lock -- a plain readers-writer lock would deadlock there."""
    lock = node_resources.RWLock()
    with lock:
        with lock:
            with lock.read:
                pass
        with lock.read:
            pass
    # Still usable afterwards: the nested acquisitions have to have unwound.
    with lock.read:
        pass


def test_a_writer_is_not_starved_by_a_stream_of_readers():
    """A reload waiting forever behind tile requests would break the one thing
    the lock exists to guarantee."""
    lock = node_resources.RWLock()
    stop = threading.Event()
    waited = []

    def spam():
        while not stop.is_set():
            with lock.read:
                time.sleep(0.0005)

    readers = [threading.Thread(target=spam) for _ in range(6)]
    for thread in readers:
        thread.start()
    time.sleep(0.05)

    def write():
        started = time.monotonic()
        with lock:
            waited.append(time.monotonic() - started)

    writer = threading.Thread(target=write)
    writer.start()
    writer.join(timeout=30)
    stop.set()
    for thread in readers:
        thread.join(timeout=30)

    assert waited, "the writer never got in"


def test_one_expensive_derived_value_is_computed_once_under_concurrent_misses(tmp_path):
    """A quantization window is a full-resolution read of a whole channel
    plane. Two readers racing used to mean two of those."""
    from plexora.server.models import data_model

    app, resource = _node(tmp_path)
    with node_api._reading(resource):
        pass

    calls = []
    real = data_model.quantization_window_of

    def counted(pyramid, index):
        calls.append(index)
        time.sleep(0.05)
        return real(pyramid, index)

    data_model.quantization_window_of = counted
    try:
        threads = [threading.Thread(
            target=lambda: node_api._quantization(resource, 0)) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    finally:
        data_model.quantization_window_of = real

    assert calls == [0], f"computed {len(calls)} times, expected once"
