"""The uploader against a fake telemetry service: every outcome it can meet,
and that none of them can hurt Plexora."""

import gzip
import json
import time

import pytest

from plexora.telemetry import uploader
from plexora.telemetry.queue import window_of

OLD = "2026-01-01T10"


@pytest.fixture
def started(telemetry_enabled):
    t, fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    t._ready.wait(5)
    return t, fake


def queue_rows(t, n=3, window=OLD):
    t.queue.add_counters(window, [("tool.summary", "open", '{"tool":"roi"}', "count", n,
                                   None, 0.0, 0.0)])


def state(t):
    return t.queue.get_state("uploader", {}) or {}


def test_happy_path_registers_then_sends(started):
    t, fake = started
    queue_rows(t)
    assert uploader.upload_once(t) == "sent"
    assert [r["path"] for r in fake.requests] == ["/v1/telemetry/register",
                                                  "/v1/telemetry/events"]
    register = fake.requests[0]["json"]
    assert register["install_id"] == t.client_block()["install_id"]
    assert "install_id" not in register["client"]
    events = fake.requests[1]
    assert events["headers"]["Authorization"] == "Bearer PLEXORAT1.test.token"
    assert events["headers"]["Content-Encoding"] == "gzip"
    body = events["json"]
    assert body["client"]["install_id"] == register["install_id"]
    assert body["events"][0]["rows"] == [{"k": "open", "d": {"tool": "roi"}, "n": 3}]
    assert len(events["raw"]) <= 64 * 1024
    assert t.queue.pending()["batches_waiting"] == 0
    assert uploader.upload_once(t) == "nothing"
    assert len(fake.of("/v1/telemetry/register")) == 1  # token reused


def test_open_window_waits_unless_asked(started):
    t, fake = started
    queue_rows(t, window=window_of())
    assert uploader.upload_once(t) == "nothing"
    assert uploader.upload_once(t, include_open=True) == "sent"


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:9", "http://nonexistent.invalid"])
def test_unreachable_keeps_the_batch_and_backs_off(started, monkeypatch, endpoint):
    t, _fake = started
    monkeypatch.setenv("PLEXORA_TELEMETRY_ENDPOINT", endpoint)
    queue_rows(t)
    token = {"token": "PLEXORAT1.x", "install_id": t.client_block()["install_id"],
             "expires": time.time() + 1e6}
    t.queue.set_state("token", token)
    started_at = time.monotonic()
    assert uploader.upload_once(t) == "failed"
    assert time.monotonic() - started_at < 12
    assert t.queue.pending()["batches_waiting"] == 1
    assert state(t)["backoff_until"] > time.time() + 30
    assert uploader.upload_once(t) == "backoff"


def test_read_timeout(started, monkeypatch):
    t, fake = started
    monkeypatch.setattr(uploader, "READ_TIMEOUT", 0.5)
    uploader._pools.clear()
    queue_rows(t)
    uploader.register(t, t.queue, t.client_block())
    fake.delay = 1.5
    assert uploader.upload_once(t) == "failed"
    fake.delay = 0
    uploader._pools.clear()
    assert t.queue.pending()["batches_waiting"] == 1


@pytest.mark.parametrize("status,headers,backoff_at_least", [
    (429, {}, 30), (429, {"Retry-After": "120"}, 100), (500, {}, 30),
    (503, {"Retry-After": "600"}, 500),
])
def test_retryable_statuses(started, status, headers, backoff_at_least):
    t, fake = started
    queue_rows(t)
    fake.script("/v1/telemetry/events", status, {"error": "x"}, headers)
    assert uploader.upload_once(t) == "failed"
    assert t.queue.pending()["batches_waiting"] == 1
    assert state(t)["backoff_until"] >= time.time() + backoff_at_least
    # The same batch, the same bytes, when it is retried.
    first = fake.of("/v1/telemetry/events")[0]["raw"]
    assert uploader.upload_once(t, force=True) == "sent"
    assert fake.of("/v1/telemetry/events")[1]["raw"] == first


def test_401_re_registers_and_retries(started):
    t, fake = started
    queue_rows(t)
    fake.script("/v1/telemetry/events", 401, {"error": "expired"})
    assert uploader.upload_once(t) == "unregistered"
    assert t.queue.get_state("token") is None
    assert uploader.upload_once(t) == "sent"
    assert len(fake.of("/v1/telemetry/register")) == 2


def test_413_halves_the_batch(started):
    t, fake = started
    queue_rows(t)
    fake.script("/v1/telemetry/events", 413, {"error": "too big"})
    assert uploader.upload_once(t) == "rejected"
    assert state(t)["max_events"] == 100
    assert t.queue.pending()["batches_waiting"] == 0


def test_422_three_times_pauses_for_a_day(started):
    t, fake = started
    for _ in range(3):
        queue_rows(t)
        fake.script("/v1/telemetry/events", 422, {"error": "schema"})
        assert uploader.upload_once(t) == "rejected"
    server = t.queue.get_state("server")
    assert server["disabled_until"] > time.time() + 3600
    assert t.enabled is False


def test_server_config_is_applied_at_once(started):
    t, fake = started
    queue_rows(t)
    fake.script("/v1/telemetry/events", 202, {"accepted": 1, "config": {
        "level_max": "anonymous", "upload_interval_s": 5, "sample": 0.5, "disabled_until": 0}})
    assert uploader.upload_once(t) == "sent"
    assert t.mode == "anonymous" and t.resolved.source == "ceiling"
    assert uploader.interval(t) == uploader.MIN_INTERVAL  # clamped
    t.queue.add_counters(OLD, [("render.summary", "gestures",
                                '{"browser":"chrome","gpu":"nvidia","os":"mac"}', "count", 1,
                                None, 0.0, 0.0)])
    assert uploader.upload_once(t) == "sent"
    rows = fake.uploads[-1]["events"][0]["rows"]
    assert rows == [{"k": "gestures", "d": {"browser": "chrome", "os": "mac"}, "n": 1}]
    assert fake.uploads[-1]["client"]["mode"] == "anonymous"


def test_server_pause_stops_everything(started):
    t, fake = started
    queue_rows(t)
    fake.script("/v1/telemetry/events", 202, {"accepted": 1, "config": {
        "disabled_until": time.time() + 3600}})
    assert uploader.upload_once(t) == "sent"
    assert t.enabled is False and t.resolved.source == "server"
    queue_rows(t)
    assert uploader.upload_once(t) == "off"


def test_rotate_drops_the_token(started):
    t, fake = started
    queue_rows(t)
    fake.script("/v1/telemetry/events", 202, {"accepted": 1, "rotate": True})
    assert uploader.upload_once(t) == "sent"
    assert t.queue.get_state("token") is None


def test_backoff_survives_a_restart(started):
    t, fake = started
    queue_rows(t)
    fake.script("/v1/telemetry/events", 503, {}, {"Retry-After": "900"})
    uploader.upload_once(t)
    until = state(t)["backoff_until"]
    t.shutdown(0.5)
    from plexora.telemetry.queue import Queue

    fresh = Queue()
    assert fresh.open()
    assert fresh.get_state("uploader")["backoff_until"] == until


def test_proxy_from_the_environment(monkeypatch):
    import urllib3

    uploader._pools.clear()
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    pool = uploader._pool("https://telemetry.example/v1/telemetry/events")
    assert isinstance(pool, urllib3.ProxyManager)
    uploader._pools.clear()


def test_the_client_block_names_nothing_personal(started, tmp_path):
    import getpass
    import socket

    t, fake = started
    queue_rows(t)
    uploader.upload_once(t)
    raw = json.dumps([r["json"] for r in fake.requests])
    for leak in (getpass.getuser(), socket.gethostname(), str(tmp_path), "license_token"):
        assert leak not in raw
    client = fake.uploads[0]["client"]
    # `license_tier` is a word (free/paid/trial), never who holds a licence;
    # the licence identity fields stay reserved and unset.
    assert set(client) <= {"install_id", "session_id", "plexora_version", "python", "os",
                           "arch", "launch_mode", "deployment", "scheduler", "install_kind",
                           "mode", "plugins", "license_tier"}


def test_shutdown_sends_the_last_window(telemetry_enabled):
    t, fake = telemetry_enabled
    t.start(None, "terminal", upload=True)
    t._ready.wait(5)
    t.count("tool.summary", "open", tool="gating")
    t.shutdown(budget_s=3.0)
    types = {e["type"] for body in fake.uploads for e in body["events"]}
    assert {"tool.summary", "session.summary"} <= types


def test_no_endpoint_means_no_thread_and_no_request(telemetry_enabled, monkeypatch):
    import threading

    t, fake = telemetry_enabled
    monkeypatch.delenv("PLEXORA_TELEMETRY_ENDPOINT")
    t.start(None, "terminal", upload=True)
    t._ready.wait(5)
    assert not [x for x in threading.enumerate() if x.name == "plexora-telemetry-upload"]
    assert uploader.upload_once(t) == "no_endpoint"
    assert fake.requests == []
