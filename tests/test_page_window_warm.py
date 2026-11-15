"""The website's warm-up and stats requests never hold Python for a window scan.

On the website the page's workers scan a picked image's full-resolution plane
for each channel's quantization window (services/browserNode.js `windowOf`).
On a 66 GB, 40-channel slide that is ~7 s a channel, and Python used to wait
for every one: the warm-up queued one task per channel, each holding the only
thread for its scan, and every request of the page waited 7-9 s behind them
for minutes after the project opened. Now the warm-up looks at the page's scan
and comes back later (`_warm_on_page`), and a request for a channel the page
is still scanning is answered "again later": the route says so and the
worker puts it back in its queue (`_page_window_or_retry`). Python never
waits inside the request with others answered in the gap -- that nested
requests on one thread, and the worker froze. The desktop's threads are
untouched.

The page is played here by a stand-in for `providers.browser.page_window`.
"""

import numpy as np
import pytest
import tifffile

from plexora import _work, datasource
from plexora.browser import cooperative
from plexora.server.models import data_model
from plexora.server.providers import browser as page
from tests.helpers import use_data_root

NAME = "scanned"

#: conftest.py stubs the warm-up for every test (its thread outliving the test
#: that started it); the ones about it hold their own reference, taken at import.
_warm_up = data_model._warm_datasource_caches


@pytest.fixture
def project(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    rng = np.random.default_rng(3)
    planes = rng.gamma(2.0, 150.0, size=(3, 96, 80)).astype(np.uint16)
    image = tmp_path / f"{NAME}.tif"
    tifffile.imwrite(image, planes, photometric="minisblack")
    # Loaded as the website loads it, without the warm-up the load queues:
    # each test runs what it is about.
    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    datasource.register_image_datasource(name=NAME, image=image, data_dir=data_dir)
    data_model.load_datasource(NAME, reload=True)
    _work._reset_for_tests()
    yield
    _work._reset_for_tests()


class Page:
    """The page's scans: each channel's plane still being read for its first
    `pending` looks; a wait for it (no `wait_ms`) answers at once."""

    def __init__(self, monkeypatch, pending=2):
        self.pending = pending
        self.looks = []
        self.waits = []
        self.seen = {}
        monkeypatch.setattr(page, "page_window", self)

    def __call__(self, provider, pyramid, index, *, wait_ms=None, urgent=True, ahead=()):
        window = (0.0, 1000.0 + index)
        if wait_ms is None:
            self.waits.append(index)
            return window
        self.looks.append({"index": index, "wait_ms": wait_ms, "urgent": urgent, "ahead": list(ahead)})
        self.seen[index] = self.seen.get(index, 0) + 1
        return page.WINDOW_PENDING if self.seen[index] <= self.pending else window


def _channels():
    return [c["fullname"] for c in data_model.config[NAME]["imageData"] if c["name"] != "Area"]


def test_the_warm_up_looks_and_comes_back_instead_of_waiting(project, monkeypatch):
    scans = Page(monkeypatch, pending=2)
    measured = []
    real = data_model.get_image_channel_stats
    monkeypatch.setattr(data_model, "get_image_channel_stats",
                        lambda name, ds, *a, **k: measured.append(name) or real(name, ds, *a, **k))

    # The project last had its second channel on: only that one is fitted.
    shown = _channels()[1]
    monkeypatch.setattr(data_model, "get_saved_channel_list",
                        lambda ds: [{"channel": shown, "channel_active": True}])
    _warm_up(NAME)
    names = []
    for _ in range(200):
        stepped = cooperative.step(budget_ms=10_000)
        names += stepped["names"]
        if not stepped["pending"] and not stepped["background"]:
            break
        # One background task a step: never two in one.
        assert stepped["ran"] <= 1

    channels = _channels()
    # Only the channel last shown was measured, after its scan was done: a
    # scan is a whole full-resolution plane (1.9 GB a channel on a 106 GB
    # slide), and one for every channel kept the file busy under every tile
    # a zoom asked for. A channel switched on later is scanned then.
    assert measured == [shown]
    # ...and Python never waited for a scan: every wait it made was answered
    # at once from a scan already finished (the page's kept answer).
    assert scans.seen == {1: 3}
    # Every look gives the worker back within 250 ms, is not urgent, and
    # queues no other channel's scan behind it.
    assert {look["wait_ms"] for look in scans.looks} == {data_model._PAGE_WINDOW_LOOK_MS}
    assert not any(look["urgent"] for look in scans.looks)
    assert all(look["ahead"] == [] for look in scans.looks)
    # While a scan was pending the task was queued again under its own name.
    assert names.count(f"warm-window-{shown}") == 2
    # Stats and the contrast fit (seconds of WebAssembly no request can
    # interrupt) only for the channel last shown.
    for name in channels:
        assert ((NAME, name) in data_model._image_stats_cache) == (name == shown)
        assert (data_model._gmm_cache.get((NAME, name)) is not None) == (name == shown)
    # And every channel's fit ahead of its click (a chain of its own, the
    # shown channel first, one fit a task, never looking at a scan): the
    # rest with the window still provisional, the exact packet's fit then.
    fits = [n for n in names if n.startswith("warm-fit-")]
    assert fits[0] == f"warm-fit-{shown}" and len(fits) == len(channels)
    for name in channels:
        assert (NAME, name) in data_model._gmm_cache \
            or (NAME, data_model.load_generation, name) in data_model._provisional_fits


def test_a_reload_ends_the_old_warm_up(project, monkeypatch):
    Page(monkeypatch, pending=0)
    plan = [(data_model.get_image_channel_stats, name) for name in _channels()]
    data_model._warm_on_page(NAME, data_model.load_generation - 1, plan, 0)
    assert _work.pending() == 0
    assert (NAME, plan[0][1]) not in data_model._image_stats_cache


def test_a_stats_request_for_a_scan_in_progress_is_asked_again_later(project, monkeypatch):
    scans = Page(monkeypatch, pending=1)
    # Python never sleeps inside the request: no gap for others to nest in.
    _work.set_idle(lambda seconds: pytest.fail("paused inside a request"))
    name = _channels()[1]

    with pytest.raises(page.WindowPending):
        data_model.get_image_channel_stats(name, NAME, patient=True)
    # The next time it is asked, the scan is done.
    stats = data_model.get_image_channel_stats(name, NAME, patient=True)

    assert [look["urgent"] for look in scans.looks] == [True, True]
    assert {look["wait_ms"] for look in scans.looks} == {data_model._PAGE_WINDOW_REQUEST_LOOK_MS}
    assert stats["qmax"] == 1001.0


def _stats_request(name, retried=None, retryable=True, provisional=False):
    from urllib.parse import urlencode

    import plexora
    from plexora.browser import bridge

    headers = [["X-Plexora-Retried", str(retried)]] if retried is not None else []
    query = {"channel": name, "datasource": NAME, **({"provisional": "1"} if provisional else {})}
    return bridge.handle(plexora.app, {"method": "GET", "path": "/get_image_channel_stats",
                                       "query": urlencode(query),
                                       "headers": headers, "retryable": retryable}, "http://127.0.0.1")


def test_the_route_answers_again_later_and_the_bridge_says_when(project, monkeypatch):
    Page(monkeypatch, pending=1)
    name = _channels()[0]

    later = _stats_request(name)
    assert later["retry"] == page.WindowPending.retry_ms
    assert later["wrote"] is False and later["body"] == b""

    answered = _stats_request(name, retried=1)
    assert "retry" not in answered and answered["status"] == 200


def test_a_worker_that_does_not_follow_a_retry_gets_the_wait(project, monkeypatch):
    """An older pythonWorker.js still in a browser's cache, with a newer bundle:
    never answered "again later", which it would hand the page as a 503."""
    scans = Page(monkeypatch, pending=10_000)
    name = _channels()[0]

    answered = _stats_request(name, retryable=False)

    assert "retry" not in answered and answered["status"] == 200
    assert scans.looks == [] and scans.waits == [0]


def test_after_enough_retries_the_route_waits_as_it_always_did(project, monkeypatch):
    from plexora.server.routes import data_routes

    scans = Page(monkeypatch, pending=10_000)
    name = _channels()[0]

    answered = _stats_request(name, retried=data_routes._MAX_RETRIES)

    assert "retry" not in answered and answered["status"] == 200
    # No look: the one wait get_channel_quantization_window always made.
    assert scans.looks == [] and scans.waits == [0]


def test_a_known_window_is_not_asked_for(project, monkeypatch):
    scans = Page(monkeypatch, pending=0)
    name = _channels()[0]
    data_model._gmm_cache[("qwindow", NAME, name)] = (0.0, 77.0)
    _work.set_idle(lambda seconds: pytest.fail("paused for a known window"))
    assert data_model.get_image_channel_stats(name, NAME, patient=True)["qmax"] == 77.0
    assert scans.looks == []


def test_on_threads_nothing_changes(project, monkeypatch):
    monkeypatch.setenv("PLEXORA_WORK", "threads")
    scans = Page(monkeypatch, pending=5)
    name = _channels()[0]
    stats = data_model.get_image_channel_stats(name, NAME, patient=True)
    # The one wait get_channel_quantization_window always made, and no look.
    assert scans.looks == [] and scans.waits == [0]
    assert stats["qmax"] == 1000.0


def test_an_hd_viewer_is_answered_at_once_with_a_provisional_window(project, monkeypatch):
    """The website, a viewer drawing HD tiles (`provisional`): while the page
    still reads the channel's plane, the packet made with (0, the overview's
    largest value) and `window_exact` False, at once -- not "again later"
    until a 4.8 GB scan is done -- and not kept; asked again once the scan is
    done, the exact one, the same packet but for its window."""
    Page(monkeypatch, pending=1)
    _work.set_idle(lambda seconds: pytest.fail("paused inside a request"))
    name = _channels()[1]
    plane = data_model._require_overview(NAME)[data_model.real_channel_index(name, NAME)]

    first = data_model.get_image_channel_stats(name, NAME, patient=True, provisional=True)
    assert first["window_exact"] is False
    assert (first["qmin"], first["qmax"]) == (0.0, float(plane.max()))
    assert first["vmin_hint"] <= first["vmax_hint"] <= first["qmax"]
    assert (NAME, name) not in data_model._image_stats_cache

    exact = data_model.get_image_channel_stats(name, NAME, patient=True, provisional=True)
    assert "window_exact" not in exact and exact["qmax"] == 1001.0
    keys = ("image_histogram", "image_min", "image_max", "vmin_hint", "vmax_hint")
    assert {k: first[k] for k in keys} == {k: exact[k] for k in keys}


def test_the_route_answers_provisionally_only_when_asked(project, monkeypatch):
    import json

    Page(monkeypatch, pending=10)
    name = _channels()[0]
    # An 8-bit viewer's tiles are made with the window: it waits for it.
    assert _stats_request(name)["retry"] == page.WindowPending.retry_ms
    answered = _stats_request(name, provisional=True)
    assert "retry" not in answered and answered["status"] == 200
    assert json.loads(answered["body"])["window_exact"] is False


def test_an_hd_viewer_has_its_contrast_fit_at_once_fitted_once(project, monkeypatch):
    """The fit is of the overview alone (the window only rides in the
    packet): had at once with the provisional window, and the exact packet,
    once the scan is done, is that same fit -- not fitted again."""
    Page(monkeypatch, pending=1)
    _work.set_idle(lambda seconds: pytest.fail("paused inside a request"))
    name = _channels()[1]
    plane = data_model._require_overview(NAME)[data_model.real_channel_index(name, NAME)]
    fitted = []
    real = data_model.channel_gmm_of
    monkeypatch.setattr(data_model, "channel_gmm_of", lambda *a: fitted.append(a[1:]) or real(*a))

    first = data_model.get_channel_gmm(name, NAME, patient=True, provisional=True)
    assert first["window_exact"] is False and first["qmax"] == float(plane.max())
    assert first["vmin"] <= first["vmax"] <= first["qmax"]
    assert (NAME, name) not in data_model._gmm_cache

    exact = data_model.get_channel_gmm(name, NAME, patient=True, provisional=True)
    assert "window_exact" not in exact and (exact["qmin"], exact["qmax"]) == (0.0, 1001.0)
    assert len(fitted) == 1
    keys = ("vmin", "vmax", "log_window", "image_gmm_1", "image_gmm_2", "image_gmm_3")
    assert {k: first[k] for k in keys} == {k: exact[k] for k in keys} \
        == {k: real(plane, 0.0, 1001.0)[k] for k in keys}


def test_the_fit_route_answers_provisionally_only_when_asked(project, monkeypatch):
    import json
    from urllib.parse import urlencode

    import plexora
    from plexora.browser import bridge

    Page(monkeypatch, pending=10)
    name = _channels()[0]

    def ask(**extra):
        return bridge.handle(plexora.app, {"method": "GET", "path": "/get_channel_gmm",
                                           "query": urlencode({"channel": name, "datasource": NAME, **extra}),
                                           "headers": [], "retryable": True}, "http://127.0.0.1")

    assert ask()["retry"] == page.WindowPending.retry_ms
    answered = ask(provisional="1")
    assert "retry" not in answered and json.loads(answered["body"])["window_exact"] is False


def test_the_desktop_answers_at_once_and_makes_the_window_behind(project, monkeypatch):
    """The desktop's threads: a viewer's provisional ask for a channel whose
    exact window is not known is answered now, from the overview, and the
    exact window -- every full-resolution pixel, 18.7 s a channel from a file
    share -- is made on a thread of its own (`_window_behind`); the viewer's
    next ask gets the exact packet. An ask that is not provisional (a tile's
    encode, the warm-up) makes it inline, as it always did."""
    import time

    import threading

    monkeypatch.setenv("PLEXORA_WORK", "threads")
    scans = Page(monkeypatch, pending=5)
    # The scan behind held until both provisional answers are in.
    release = threading.Event()
    real = data_model.get_channel_quantization_window

    def held(*args, **kwargs):
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(data_model, "get_channel_quantization_window", held)
    name = _channels()[0]
    first = data_model.get_image_channel_stats(name, NAME, patient=True, provisional=True)
    assert first["window_exact"] is False
    gmm = data_model.get_channel_gmm(name, NAME, patient=True, provisional=True)
    assert gmm["window_exact"] is False
    # One scan behind for the channel, however many asks.
    assert len(data_model._windows_behind) == 1
    release.set()
    deadline = time.monotonic() + 10
    while data_model._windows_behind and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not data_model._windows_behind
    stats = data_model.get_image_channel_stats(name, NAME, patient=True, provisional=True)
    assert "window_exact" not in stats and stats["qmax"] == 1000.0
    # Made once, behind; never looked at the website's way.
    assert scans.looks == [] and scans.waits == [0]
    other = data_model.get_image_channel_stats(_channels()[1], NAME, patient=True)
    assert "window_exact" not in other and other["qmax"] == 1001.0
    assert scans.waits == [0, 1]


def test_the_page_scans_one_line_urgent_first_and_keeps_the_answers():
    """services/browserNode.js `windowOf`, run in Node with its workers stood
    in for (tests/js/window_scan_probe.mjs)."""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    probe = Path(__file__).parent / "js" / "window_scan_probe.mjs"
    done = subprocess.run([node, str(probe)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    # A look answers within its wait while the scan goes on.
    assert out["firstLook"] == {"done": False} and out["firstLookMs"] < 200
    # The urgent channel's scan went between two rounds of the warm-up's,
    # and the interrupted one resumed where it was: every window read once.
    # A round of the warm-up's reads one 4096-px window of this 16-bit plane
    # (`ROUND_BACKGROUND_SAMPLES`: a zoom's tiles never wait long behind
    # it), an urgent one's as many as its workers take two at a time.
    assert out["rounds"] == [0, 0, 2, 2, 2, 2] + [0] * 6 + [1] * 8
    assert out["urgent"] == 2006 and out["resumed"] == {"done": True, "max": 6}
    assert out["windowsRead"] == out["windowsInPlane"]
    # Kept: asked again, nothing is read.
    assert out["again"] == 2006 and out["readAgain"] == 0
    assert out["notHeld"] == "the page does not hold that file now"
    # The scan pool is let go once the line is empty.
    assert out["workersLeft"] == 0


def _overview_request(name, retryable=True):
    from urllib.parse import quote

    import plexora
    from plexora.browser import bridge

    return bridge.handle(plexora.app, {"method": "GET",
                                       "path": f"/generated/overview/{NAME}/{quote(name)}",
                                       "query": "", "headers": [], "retryable": retryable},
                         "http://127.0.0.1")


def _header(answer, name):
    return dict((k.lower(), v) for k, v in answer["headers"]).get(name.lower())


def _decoded(body):
    import io

    from PIL import Image

    return np.asarray(Image.open(io.BytesIO(bytes(body))))


def test_the_mini_map_is_drawn_at_once_while_the_page_scans(project, monkeypatch):
    """The website's mini-map (views/miniMap.js) asked for a channel whose
    plane the page is still reading: the overview at once, quantized with the
    provisional window the channel's stats carry meanwhile, said so and not
    cached -- not Python held for the whole scan inside the request (103 s on
    a 1.2-gigapixel plane, the map black throughout). Asked again once the
    scan is done: the exact window's overview, cacheable."""
    from plexora.server.routes import data_routes

    scans = Page(monkeypatch, pending=1)
    _work.set_idle(lambda seconds: pytest.fail("paused inside a request"))
    name = _channels()[1]

    first = _overview_request(name)
    assert first["status"] == 200 and "retry" not in first
    assert _header(first, data_routes.OVERVIEW_WINDOW_HEADER) == "provisional"
    assert _header(first, "Cache-Control") == "no-store"
    # The scan asked for, not waited on.
    assert scans.waits == [] and [look["wait_ms"] for look in scans.looks] == [0]
    # The bytes are in the provisional stats window, the one the sidebar holds.
    index = data_model.real_channel_index(name, NAME)
    plane = np.asarray(data_model._require_overview(NAME)[index])
    stats = data_model._provisional_stats(name, NAME)
    expected = _decoded(data_model.encode_overview(plane, stats["qmin"], stats["qmax"]))
    assert np.array_equal(_decoded(first["body"]), expected)

    second = _overview_request(name)
    assert _header(second, data_routes.OVERVIEW_WINDOW_HEADER) is None
    assert _header(second, "ETag")
    assert np.array_equal(_decoded(second["body"]),
                          _decoded(data_model.encode_overview(plane, 0.0, 1000.0 + index)))


def test_an_overview_with_its_window_known_is_answered_as_always(project, monkeypatch):
    from plexora.server.routes import data_routes

    scans = Page(monkeypatch, pending=10_000)
    name = _channels()[0]
    data_model._gmm_cache[("qwindow", NAME, name)] = (0.0, 77.0)

    answered = _overview_request(name)

    assert _header(answered, data_routes.OVERVIEW_WINDOW_HEADER) is None
    assert scans.looks == [] and scans.waits == []


def test_the_overview_on_threads_and_for_an_older_worker_is_unchanged(project, monkeypatch):
    from plexora.server.routes import data_routes

    scans = Page(monkeypatch, pending=10_000)
    name = _channels()[0]
    # A worker that does not follow a retry is not the website's newer one:
    # the route answers as it always did.
    answered = _overview_request(name, retryable=False)
    assert _header(answered, data_routes.OVERVIEW_WINDOW_HEADER) is None
    assert scans.looks == []

    data_model._gmm_cache.pop(("qwindow", NAME, name), None)
    monkeypatch.setenv("PLEXORA_WORK", "threads")
    assert data_model.provisional_channel_overview(NAME, name) is None


class Declines:
    """The page, for a file its workers cannot read exactly (JPEG 2000):
    every look answers None, and a wait would too."""

    def __init__(self, monkeypatch):
        self.asked = 0
        monkeypatch.setattr(page, "page_window", self)
        monkeypatch.setenv("PLEXORA_BACKEND", "browser")

    def __call__(self, provider, pyramid, index, *, wait_ms=None, urgent=True, ahead=()):
        self.asked += 1
        return None


def _run_queued(limit=10_000):
    for _ in range(limit):
        if not _work.run_pending(budget_s=0.0):
            return


def test_a_plane_the_page_cannot_scan_is_scanned_by_python_between_requests(project, monkeypatch):
    """The website, a plane the page's workers cannot read (a JPEG 2000
    Xenium image): the window was every pixel read inside the request that
    asked -- 102 s for 1.2 gigapixels, the page frozen. Now the request is
    answered at once (provisional, or "again later"), and Python reads the
    plane a block a queued task, the exact window kept at the end."""
    Declines(monkeypatch)
    monkeypatch.setattr(data_model, "_STEPPED_WINDOW_BYTES", 1)
    _work.set_idle(lambda seconds: pytest.fail("paused inside a request"))
    name = _channels()[1]
    index = data_model.real_channel_index(name, NAME)

    # The HD viewer's ask: the provisional packet now.
    stats = data_model.get_image_channel_stats(name, NAME, patient=True, provisional=True)
    assert stats["window_exact"] is False
    # Anyone else is asked to come back.
    with pytest.raises(page.WindowPending):
        data_model.get_image_channel_stats(name, NAME, patient=True)
    # The mini-map is drawn now, in the provisional window.
    assert data_model.provisional_channel_overview(NAME, name) is not None

    # One block a task, each a whole chunk: never the plane in one read.
    reads = []
    real_level = data_model._zarr_level

    class Watched:
        def __init__(self, plane):
            self.plane = plane

        def __getitem__(self, key):
            reads.append(key)
            return self.plane[key]

    monkeypatch.setattr(data_model, "_zarr_level", lambda group, level: Watched(real_level(group, level)))
    _run_queued()
    # Whole chunks only (this fixture's plane is one chunk).
    assert reads == [(index, slice(0, 96), slice(0, 80))]
    plane = np.asarray(real_level(data_model.channels, 0))
    assert data_model._gmm_cache[("qwindow", NAME, name)] == (0.0, float(plane[index].max()))
    # ...and the next ask is exact.
    exact = data_model.get_image_channel_stats(name, NAME, patient=True)
    assert exact.get("window_exact", True) is not False and exact["qmax"] == float(plane[index].max())
    assert data_model.provisional_channel_overview(NAME, name) is None


def test_a_reload_ends_python_s_own_scan(project, monkeypatch):
    Declines(monkeypatch)
    monkeypatch.setattr(data_model, "_STEPPED_WINDOW_BYTES", 1)
    name = _channels()[0]
    with pytest.raises(page.WindowPending):
        data_model._page_window_or_retry(name, NAME)
    data_model.load_datasource(NAME, reload=True)
    _run_queued()
    assert ("qwindow", NAME, name) not in data_model._gmm_cache


def test_off_the_website_a_declined_plane_is_read_as_it_always_was(project, monkeypatch):
    Declines(monkeypatch)
    monkeypatch.delenv("PLEXORA_BACKEND")
    name = _channels()[0]
    assert data_model._page_window_look(name, NAME, wait_ms=0, urgent=False) is None
    assert data_model._stepped_windows == {}


def test_python_s_own_scan_reads_whole_chunks_a_few_at_a_time():
    import zarr

    plane = zarr.zeros((2, 50, 70), chunks=(1, 16, 16), dtype="uint16")
    blocks = data_model._scan_blocks(plane)
    # 16-row bands, at most _STEPPED_WINDOW_BYTES of whole chunks across.
    covered = np.zeros((50, 70), dtype=int)
    for y0, y1, x0, x1 in blocks:
        assert y0 % 16 == 0 and x0 % 16 == 0 and y1 - y0 <= 16
        covered[y0:y1, x0:x1] += 1
    assert (covered == 1).all()
    small = data_model._STEPPED_WINDOW_BYTES
    try:
        data_model._STEPPED_WINDOW_BYTES = 16 * 16 * 2 * 2
        assert {x1 - x0 for _, _, x0, x1 in data_model._scan_blocks(plane)} <= {32, 6}
    finally:
        data_model._STEPPED_WINDOW_BYTES = small
