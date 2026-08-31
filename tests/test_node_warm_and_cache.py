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


def test_warming_opens_the_pyramid_and_scans_nothing(tmp_path):
    """The warm-up opens the file; it must never read whole planes. It used
    to compute every channel's window here, and a node restored from its
    manifest with several slides on a cluster filesystem spent its first
    minutes reading a hundred gigabytes nobody had asked for -- deaf to
    /health the whole time. Windows now come from the store (a JSON read) or
    on demand."""
    from plexora.server.models import data_model

    app, resource = _node(tmp_path)
    assert resource.opened is None, "a freshly built node should not have opened anything"

    calls = []
    real = data_model.quantization_window_of
    data_model.quantization_window_of = (
        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    try:
        warm_resources(app.config["PLEXORA_NODE_RESOURCES"], log=_quiet).join(timeout=120)
    finally:
        data_model.quantization_window_of = real

    assert resource.opened is not None
    assert calls == [], "the warm-up read a full-resolution plane unprompted"
    assert not [key for key in resource.derived if key[0] == "qwindow"]


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


# -- the window scan itself ------------------------------------------------
#
# The scan used to be `np.asarray(plane[index]).max()` -- the whole plane in
# one slice. For a wide slide that is gigabytes in a single numpy call, and a
# node grinding through one of those per channel right after it starts (the
# warm-up above walks every channel) answered nothing at all -- not even
# /health -- for as long as the pile lasted. Observed live against two
# clusters: the node registers, goes silent within three minutes, and the
# panel says "Not answering" over a machine that is merely busy. The window
# is still every pixel; it is read in bounded slabs, at most two scans at a
# time, and remembered across jobs.


class _RecordingPlane:
    """ndim-3 plane whose reads are observable, for the group path."""

    def __init__(self, data, on_read=None):
        self.data = data
        self.reads = []
        self.on_read = on_read
        self.ndim = data.ndim
        self.shape = data.shape
        self.dtype = data.dtype

    def __getitem__(self, key):
        self.reads.append(key)
        if self.on_read is not None:
            self.on_read()
        return self.data[key]


def test_the_window_scan_reads_the_plane_in_bounded_slabs(monkeypatch):
    """Every pixel, never all at once -- and the same ceiling either way."""
    from plexora.server.models import data_model

    rng = np.random.default_rng(7)
    data = rng.integers(0, 1000, size=(2, 37, 64), dtype=np.uint16)
    data[1, 36, 63] = 41281  # the hot pixel, in the final partial slab
    plane = _RecordingPlane(data)
    # 64 columns x 2 bytes = 128 bytes/row; 1280 bytes = 10 rows per slab.
    monkeypatch.setattr(data_model, "_WINDOW_SCAN_SLAB_BYTES", 1280)

    window = data_model.quantization_window_of({"0": plane}, 1)

    assert window == (0.0, 41281.0)
    assert len(plane.reads) == 4, "37 rows in 10-row slabs is four reads"
    for key in plane.reads:
        index, rows = key[0], key[1]
        assert index == 1
        assert rows.stop - rows.start <= 10


def test_at_most_two_window_scans_run_at_once(monkeypatch):
    """Six channels arriving at once is the browser's ordinary opening move.
    Six parallel full-resolution reads of one file on a network filesystem is
    how sequential-read minutes become a seek-bound quarter of an hour."""
    from plexora.server.models import data_model

    active = []
    peak = []
    gate = threading.Lock()

    def on_read():
        with gate:
            active.append(1)
            peak.append(len(active))
        time.sleep(0.02)
        with gate:
            active.pop()

    data = np.zeros((1, 40, 64), dtype=np.uint16)
    monkeypatch.setattr(data_model, "_WINDOW_SCAN_SLAB_BYTES", 1280)
    planes = [_RecordingPlane(data, on_read=on_read) for _ in range(4)]
    threads = [threading.Thread(
        target=lambda p=p: data_model.quantization_window_of({"0": p}, 0))
        for p in planes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert max(peak) <= 2, f"{max(peak)} scans ran at once"


# -- windows survive the job that computed them ----------------------------
#
# A node's in-process cache dies with the process, and on a cluster the
# process dies with every job: a saved connection starts a fresh `srun` each
# time. Without a store, every reconnect re-read every pixel of every channel
# -- the warm-up alone was the whole image again -- so reconnecting to fix a
# connection was the very thing that buried it.


def test_a_window_survives_the_death_of_the_job_that_computed_it(tmp_path):
    from plexora.server.models import data_model

    image = _image_file(tmp_path)
    first_app = create_node_app([f"image:slide={image}"], token="x", log=_quiet)
    first = first_app.config["PLEXORA_NODE_RESOURCES"].get("slide")
    with node_api._reading(first):
        window = node_api._quantization(first, 0)

    # The next job: same file, fresh process, nothing in memory.
    second_app = create_node_app([f"image:slide={image}"], token="x", log=_quiet)
    second = second_app.config["PLEXORA_NODE_RESOURCES"].get("slide")

    def never(*_a, **_k):
        raise AssertionError("the plane was re-read")

    real = data_model.quantization_window_of
    data_model.quantization_window_of = never
    try:
        assert node_api._quantization(second, 0) == window
    finally:
        data_model.quantization_window_of = real


def test_the_warm_up_is_a_file_read_on_the_second_job(tmp_path):
    """The warm-up seeds windows the last job already paid for -- from the
    store, a JSON read -- so a warmed second job serves first tiles without
    the scan, while a first job skips them entirely."""
    from plexora.server.models import data_model

    image = _image_file(tmp_path)
    first_app = create_node_app([f"image:slide={image}"], token="x", log=_quiet)
    first = first_app.config["PLEXORA_NODE_RESOURCES"].get("slide")
    # The first job's windows are computed the way they now happen: on
    # demand, when a request touches the channel.
    for index in range(CHANNELS):
        with node_api._reading(first):
            node_api._quantization(first, index)

    second_app = create_node_app([f"image:slide={image}"], token="x", log=_quiet)
    second = second_app.config["PLEXORA_NODE_RESOURCES"].get("slide")
    calls = []
    real = data_model.quantization_window_of
    data_model.quantization_window_of = (
        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    try:
        warm_resources(second_app.config["PLEXORA_NODE_RESOURCES"], log=_quiet).join(timeout=120)
    finally:
        data_model.quantization_window_of = real

    assert calls == [], "the second job re-read planes the first already scanned"
    for index in range(CHANNELS):
        assert ("qwindow", index) in second.derived


def test_a_rewritten_file_does_not_serve_the_old_ceiling(tmp_path):
    """The one thing worse than re-reading is not re-reading: a stale ceiling
    saturates a channel to a solid colour. Same rule as the primary's store --
    size or mtime moves, the lot is dropped."""
    import os

    from plexora.server.models import data_model

    image = _image_file(tmp_path)
    first_app = create_node_app([f"image:slide={image}"], token="x", log=_quiet)
    first = first_app.config["PLEXORA_NODE_RESOURCES"].get("slide")
    with node_api._reading(first):
        node_api._quantization(first, 0)

    os.utime(image, ns=(1, 1))  # the file is not what it was

    second_app = create_node_app([f"image:slide={image}"], token="x", log=_quiet)
    second = second_app.config["PLEXORA_NODE_RESOURCES"].get("slide")
    calls = []
    real = data_model.quantization_window_of
    data_model.quantization_window_of = (
        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    try:
        with node_api._reading(second):
            node_api._quantization(second, 0)
    finally:
        data_model.quantization_window_of = real

    assert calls == [1], "a changed file must be re-read, not trusted"


# -- the scan thread ------------------------------------------------------
#
# The store fixed the second job; these fix the first. A request that misses
# both caches used to run the full-resolution read itself, and a page
# restoring its channels put every waitress worker behind the first two of
# those reads -- the node answered nothing, /health included, for minutes.
# Now the miss answers immediately with a provisional window read off the
# in-memory overview, and the real scan runs on one background thread. The
# suite at large runs the scans inline (see conftest); these tests are about
# the asynchrony itself, so they turn that off and drive the queue by hand.


@pytest.fixture()
def _async_scans(monkeypatch):
    """Scans queue but never run until the test drains them itself."""
    monkeypatch.setattr(node_api, "_WINDOW_SCANS_INLINE", False)
    monkeypatch.setattr(node_api, "_ensure_window_scanner", lambda: None)


def test_a_cold_channel_answers_at_once_and_owes_the_scan(tmp_path, _async_scans):
    """The request path never waits on a plane read: the miss returns a
    provisional window without touching full-resolution data, and the exact
    read it owes sits in the queue."""
    from plexora.server.models import data_model

    app, resource = _node(tmp_path)
    calls = []
    real = data_model.quantization_window_of
    data_model.quantization_window_of = (
        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    try:
        with node_api._reading(resource):
            provisional = node_api._quantization(resource, 0)
        assert calls == [], "the request thread read a full-resolution plane"
        assert ("qwindow", 0) not in resource.derived
        assert [(r.id, i) for r, i in node_api._window_scan_queue] == [("slide", 0)]

        node_api._drain_window_scans()
    finally:
        data_model.quantization_window_of = real

    assert calls == [1]
    exact = resource.derived[("qwindow", 0)]
    # The overview is mean-pooled, so the guess sits at or below the true
    # ceiling; the drained scan replaces it and drops it.
    assert provisional[1] <= exact[1]
    assert ("qwindow-provisional", 0) not in resource.derived
    with node_api._reading(resource):
        assert node_api._quantization(resource, 0) == exact
    # And the scan banked it: a fresh process reads it instead of the plane.
    assert node_api._stored_window(resource, 0) == tuple(exact)


def test_the_channel_a_viewer_waits_on_jumps_the_queue(tmp_path, _async_scans):
    """A request's channel goes to the front: whoever is looking at it is
    waiting on it, and a straggler queued earlier is not."""
    app, resource = _node(tmp_path)
    with node_api._reading(resource):
        pass
    node_api._scan_soon(resource, 1)
    node_api._scan_soon(resource, 2)
    with node_api._reading(resource):
        node_api._quantization(resource, 0)
    assert [i for _r, i in node_api._window_scan_queue] == [0, 1, 2]
    # Asking again while it is pending does not queue it twice.
    with node_api._reading(resource):
        node_api._quantization(resource, 0)
    assert [i for _r, i in node_api._window_scan_queue] == [0, 1, 2]


def test_tiles_drawn_with_a_guess_are_not_kept_anywhere(tmp_path, _async_scans,
                                                        monkeypatch):
    """A provisional rendering must vanish on its own: the browser holds node
    tiles under a year-long max-age, so a guess served cachable would outlive
    the exact window by months. Uncachable going out, keyed by its window in
    the node's own cache, and replaced by a durable rendering once the exact
    window has landed.

    The guess is forced away from the truth: on an image this small the
    pooled overview and the full-resolution plane can share a ceiling, and
    identical windows would make the two renderings identical for the right
    reason, proving nothing about the key."""
    monkeypatch.setattr(node_api, "_provisional_window", lambda r, i: (0.0, 100.0))
    app, resource = _node(tmp_path)
    client = app.test_client()

    guessed = client.get("/node/v1/image/slide/tile/ch_0/0/0_0", headers=TOKEN_HEADER)
    assert guessed.status_code == 200
    assert guessed.headers["Cache-Control"] == "no-store"
    assert "ETag" not in guessed.headers

    node_api._drain_window_scans()

    settled = client.get("/node/v1/image/slide/tile/ch_0/0/0_0", headers=TOKEN_HEADER)
    assert settled.status_code == 200
    assert "max-age" in settled.headers["Cache-Control"]
    assert "ETag" in settled.headers
    # Two windows, two pictures, two cache entries -- a key that conflated
    # them would have handed back the guess.
    assert guessed.data != settled.data


def test_a_failed_scan_leaves_the_channel_askable_again(tmp_path):
    """A scan that dies is logged and dropped, not raised into a request that
    already answered -- and the next request queues it again rather than
    trusting a failure forever."""
    from plexora.server.models import data_model

    app, resource = _node(tmp_path)

    def broken(*_a, **_k):
        raise OSError("filesystem went away")

    real = data_model.quantization_window_of
    data_model.quantization_window_of = broken
    try:
        with node_api._reading(resource):
            window = node_api._quantization(resource, 0)
    finally:
        data_model.quantization_window_of = real

    assert window[1] >= 1.0, "the request still got an answer"
    assert ("qwindow", 0) not in resource.derived
    assert not node_api._window_scan_pending, "a failure must not stay pending"

    with node_api._reading(resource):
        node_api._quantization(resource, 0)
    assert ("qwindow", 0) in resource.derived, "the retry never ran"
