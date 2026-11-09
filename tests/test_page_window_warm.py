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
    # Each channel's stats were measured once (the shown one first), after
    # its scan was done...
    assert measured == [shown] + [name for name in channels if name != shown]
    # ...and Python never waited for a scan: every wait it made was answered
    # at once from a scan already finished (the page's kept answer).
    assert all(scans.seen[i] == 3 for i in range(len(channels)))
    # Every look gives the worker back within 250 ms, is not urgent, and
    # queues the next channel's scan behind it.
    assert {look["wait_ms"] for look in scans.looks} == {data_model._PAGE_WINDOW_LOOK_MS}
    assert not any(look["urgent"] for look in scans.looks)
    first = [look for look in scans.looks if look["index"] == 1]
    assert first[0]["ahead"] == [0]
    # While a scan was pending the task was queued again under its own name.
    assert names.count(f"warm-window-{shown}") == 2
    # Stats for every channel; the contrast fit (seconds of WebAssembly no
    # request can interrupt) only for the channel last shown.
    for name in channels:
        assert (NAME, name) in data_model._image_stats_cache
        assert (data_model._gmm_cache.get((NAME, name)) is not None) == (name == shown)


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


def _stats_request(name, retried=None):
    from urllib.parse import urlencode

    import plexora
    from plexora.browser import bridge

    headers = [["X-Plexora-Retried", str(retried)]] if retried is not None else []
    return bridge.handle(plexora.app, {"method": "GET", "path": "/get_image_channel_stats",
                                       "query": urlencode({"channel": name, "datasource": NAME}),
                                       "headers": headers}, "http://127.0.0.1")


def test_the_route_answers_again_later_and_the_bridge_says_when(project, monkeypatch):
    Page(monkeypatch, pending=1)
    name = _channels()[0]

    later = _stats_request(name)
    assert later["retry"] == page.WindowPending.retry_ms
    assert later["wrote"] is False and later["body"] == b""

    answered = _stats_request(name, retried=1)
    assert "retry" not in answered and answered["status"] == 200


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
    assert out["rounds"] == [0, 0, 2, 2, 2, 2, 0, 0, 1, 1, 1, 1]
    assert out["urgent"] == 2006 and out["resumed"] == {"done": True, "max": 6}
    assert out["windowsRead"] == out["windowsInPlane"]
    # Kept: asked again, nothing is read.
    assert out["again"] == 2006 and out["readAgain"] == 0
    assert out["notHeld"] == "the page does not hold that file now"
    # The scan pool is let go once the line is empty.
    assert out["workersLeft"] == 0
