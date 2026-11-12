"""How long reads wait on the network, per host (plexora/server/utils/read_pace.py).

What the website's "Waiting on the network" notice is judged from
(services/handoffNotice.js `pace`): intervals with the time a fetch was in
flight (`busy`), never the work done between fetches, and fetches that
overlap counted once.
"""

import numpy as np
import pytest

from plexora.server.utils import read_pace, remote_store

from tests.remote_fixtures import cache_root, http_store  # noqa: F401


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def told(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(read_pace, "_clock", clock)
    events = []
    read_pace.set_reporter(events.append)
    yield events, clock
    read_pace.set_reporter(None)


def at(clock, seconds):
    clock.now = 1000.0 + seconds


def test_nothing_is_measured_without_a_reporter():
    read_pace.set_reporter(None)
    read_pace.began("host")
    read_pace.ended("host", 10)
    assert read_pace._hosts == {}


def test_a_long_wait_is_reported_every_second_with_its_waiting(told):
    events, clock = told
    at(clock, 0.0); read_pace.began("h")
    at(clock, 0.5); read_pace.ended("h", 1_000_000)
    at(clock, 0.6); read_pace.began("h")
    at(clock, 1.2); read_pace.ended("h", 2_000_000)
    assert events == [{"cause": "network", "source": "h", "start": 1_000_000, "end": 1_001_200,
                       "busy": 1100, "bytes": 3_000_000}]


def test_quiet_ends_an_interval(told):
    events, clock = told
    at(clock, 0.0); read_pace.began("h")
    at(clock, 0.1); read_pace.ended("h", 500)
    assert events == []
    at(clock, 1.0); read_pace.began("h")       # 0.9 s quiet: the first is one interval
    assert events == [{"cause": "network", "source": "h", "start": 1_000_000, "end": 1_000_100,
                       "busy": 100, "bytes": 500}]


def test_fetches_in_flight_together_are_waited_on_once(told):
    events, clock = told
    at(clock, 0.0); read_pace.began("h")
    at(clock, 0.1); read_pace.began("h")
    at(clock, 0.5); read_pace.ended("h", 1)
    at(clock, 1.0); read_pace.ended("h", 1)
    [event] = events
    assert event["busy"] == 1000           # not 0.5 + 0.9


def test_a_wait_still_going_is_reported_and_goes_on(told):
    events, clock = told
    at(clock, 0.0); read_pace.began("h"); read_pace.began("h")
    at(clock, 1.5); read_pace.ended("h", 7)    # the other is still in flight
    at(clock, 2.0); read_pace.ended("h", 3)
    read_pace.flush()
    assert [(e["start"], e["end"], e["busy"], e["bytes"]) for e in events] == [
        (1_000_000, 1_001_500, 1500, 7), (1_001_500, 1_002_000, 500, 3)]


def test_hosts_are_kept_apart(told):
    events, clock = told
    at(clock, 0.0); read_pace.began("a"); read_pace.began("b")
    at(clock, 1.0); read_pace.ended("a", 1); read_pace.ended("b", 2)
    assert sorted((e["source"], e["bytes"]) for e in events) == [("a", 1), ("b", 2)]


def test_a_reporter_that_fails_never_fails_a_read(told):
    events, clock = told

    def broken(event):
        raise RuntimeError("the page went away")

    read_pace.set_reporter(broken)
    at(clock, 0.0); read_pace.began("h")
    at(clock, 2.0); read_pace.ended("h", 1)    # no exception


def test_a_remote_read_is_measured_by_its_host(tmp_path, http_store):  # noqa: F811
    folder = tmp_path / "served"
    folder.mkdir(exist_ok=True)
    data = np.random.default_rng(3).integers(0, 256, 3 * 2**20, dtype=np.uint8).tobytes()
    (folder / "blob.ims").write_bytes(data)
    server = http_store()
    events = []
    read_pace.set_reporter(events.append)
    try:
        stream = remote_store.open_file(server.url("blob.ims"))
        assert stream.pread_many([(0, 2**20), (2**21, 2**20)], exact=True) == [
            data[:2**20], data[2**21:2**21 + 2**20]]
        read_pace.flush()
    finally:
        read_pace.set_reporter(None)
    assert events, "the fetches were not measured"
    host = remote_store.host_of(server.url("blob.ims"))
    assert {e["source"] for e in events} == {host}
    assert sum(e["bytes"] for e in events) >= 2 * 2**20
    assert all(0 <= e["busy"] <= e["end"] - e["start"] for e in events)


# -- the desktop: kept for the page, and a drive's reads ------------------------

@pytest.fixture
def kept():
    read_pace.set_reporter(None)
    read_pace._kept.clear()
    read_pace.keep()
    yield
    read_pace.set_reporter(None)
    read_pace._kept.clear()


def test_the_desktop_keeps_intervals_for_the_page_to_ask_for(kept):
    read_pace.began("h"); read_pace.ended("h", 5)
    read_pace.flush()
    events, last = read_pace.since(0)
    assert [(e["cause"], e["source"], e["bytes"]) for e in events] == [("network", "h", 5)]
    assert read_pace.since(last) == ([], last)
    # A page older than this process (its `after` past the last) gets them all.
    assert read_pace.since(last + 100)[0] == events


def test_keeping_leaves_the_websites_reporter_alone():
    posted = []
    read_pace.set_reporter(posted.append)
    try:
        read_pace.keep()
        assert not read_pace.keeping()
        read_pace.began("h"); read_pace.ended("h", 1); read_pace.flush()
        assert posted and read_pace.since(0)[0] == []
    finally:
        read_pace.set_reporter(None)


def test_a_drive_and_a_host_of_the_same_name_are_kept_apart(told):
    events, clock = told
    at(clock, 0.0); read_pace.began("x"); read_pace.began("x", "drive")
    at(clock, 1.0); read_pace.ended("x", 1); read_pace.ended("x", 2, "drive")
    assert sorted((e["cause"], e["bytes"]) for e in events) == [("drive", 2), ("network", 1)]


def test_tifffiles_reads_of_a_file_are_timed_as_its_drive(tmp_path, kept):
    import io

    import tifffile

    assert read_pace.time_tifffile()
    image = np.random.default_rng(1).integers(0, 65535, (512, 512), dtype=np.uint16)
    path = tmp_path / "slide.ome.tif"
    tifffile.imwrite(path, image, tile=(128, 128))
    assert np.array_equal(tifffile.imread(path), image)
    read_pace.flush()
    events = read_pace.since(0)[0]
    assert {(e["cause"], e["source"]) for e in events} == {("drive", "slide.ome.tif")}
    assert sum(e["bytes"] for e in events) >= image.nbytes
    # A stream tifffile is given (a remote file) is the network's, not a drive's.
    read_pace._kept.clear()
    assert np.array_equal(tifffile.imread(io.BytesIO(path.read_bytes())), image)
    read_pace.flush()
    assert read_pace.since(0)[0] == []


def test_tifffiles_reads_are_untouched_while_nothing_is_kept(tmp_path):
    import tifffile

    read_pace.set_reporter(None)
    read_pace.time_tifffile()
    path = tmp_path / "plain.tif"
    tifffile.imwrite(path, np.arange(64, dtype=np.uint8).reshape(8, 8))
    assert tifffile.imread(path).sum() == np.arange(64).sum()
    assert read_pace._hosts == {}


def test_the_desktop_page_asks_its_server(monkeypatch):
    import plexora

    monkeypatch.delenv("PLEXORA_BACKEND", raising=False)
    read_pace.set_reporter(None)
    read_pace._kept.clear()
    client = plexora.app.test_client()
    try:
        first = client.get("/read_pace").get_json()
        assert read_pace.keeping()
        assert first["events"] == [] and isinstance(first["now"], int)
        read_pace.began("example.org"); read_pace.ended("example.org", 9); read_pace.flush()
        answer = client.get(f"/read_pace?after={first['last']}").get_json()
        assert [(e["source"], e["bytes"]) for e in answer["events"]] == [("example.org", 9)]
        assert client.get(f"/read_pace?after={answer['last']}").get_json()["events"] == []
    finally:
        read_pace.set_reporter(None)
        read_pace._kept.clear()


def test_the_website_has_no_use_for_the_route(monkeypatch):
    import plexora

    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    read_pace.set_reporter(None)
    answer = plexora.app.test_client().get("/read_pace").get_json()
    assert answer["events"] == [] and not read_pace.keeping()
