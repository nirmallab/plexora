"""The telemetry singleton: free when off, lossless under threads, and unable
to take anything down with it."""

import gzip
import json
import threading
import time

import pytest

from plexora.telemetry import client as client_module, config, schema
from plexora.telemetry.client import Telemetry


def counters(t):
    rows, _ = t.queue.snapshot(limit=100000)
    return rows


def test_off_is_a_no_op_and_writes_nothing(tmp_path):
    t = Telemetry()
    assert t.start(None, "terminal") is False  # pytest pins it off
    t.count("tool.summary", "open", tool="gating")
    t.observe("server.summary", "route_ms", 12, route="health", status="2xx")
    t.emit("project.load", {"outcome": "ready"})
    assert t._agg == {} and t._records == []
    assert not (tmp_path / ".telemetry").exists()
    assert not [x for x in threading.enumerate() if x.name.startswith("plexora-telemetry")]


def test_start_counts_and_flushes(telemetry_enabled, tmp_path):
    t, _fake = telemetry_enabled
    assert t.start(None, "terminal", upload=False)
    t.count("tool.summary", "open", tool="gating")
    t.count("tool.summary", "open", tool="gating")
    t.observe("server.summary", "route_ms", 17, route="health", status="2xx")
    assert t.sync()
    rows = {(r[1], r[2]): r for r in counters(t)}
    assert rows[("tool.summary", "open")][5] == 2
    hist = rows[("server.summary", "route_ms")]
    assert list(hist[6:15]) == [0, 1, 0, 0, 0, 0, 0, 0, 0]
    assert (tmp_path / ".telemetry" / "queue.sqlite").exists()


def test_many_threads_lose_nothing(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)

    def work():
        for _ in range(10_000):
            t.count("tool.summary", "open", tool="roi")

    threads = [threading.Thread(target=work) for _ in range(32)]
    for thread in threads:
        thread.start()
    # A flush racing the counting must not lose increments either.
    t.sync()
    for thread in threads:
        thread.join()
    t.sync()
    total = sum(r[5] for r in counters(t) if r[1] == "tool.summary")
    assert total == 320_000


@pytest.mark.parametrize("ms,index", [(0, 0), (16, 0), (17, 1), (50, 1), (51, 2),
                                      (5000, 7), (5001, 8), (1e9, 8)])
def test_ms_band_edges(ms, index):
    assert schema.ms_bin(ms) == index


def test_unknown_dim_rejected_and_counted(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    t.count("tool.summary", "open", tool="gating", project="patient_7")
    t.count("tool.summary", "open", tool="/Users/alice/x")
    t.count("no.such", "n")
    t.sync()
    rows = counters(t)
    assert not [r for r in rows if r[1] == "tool.summary"]
    assert t._health["rejected"] == 3
    health = [json.loads(r[3]) for r in rows if r[1] == "telemetry.health"]
    assert {"event": "tool.summary"} in health and {"event": "unknown.event"} in health
    assert "patient_7" not in json.dumps(rows)


def test_record_validation(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    t.emit("project.load", {"outcome": "ready", "remote": False, "total_ms": "<16"})
    t.emit("project.load", {"outcome": "ready", "path": "/data/x"})
    t.sync()
    _, records = t.queue.snapshot()
    assert [r[2] for r in records if r[2] == "project.load"] == ["project.load"]
    assert "/data/x" not in json.dumps(records)


def test_key_overflow_drops_and_counts(telemetry_enabled, monkeypatch):
    t, _fake = telemetry_enabled
    monkeypatch.setattr(client_module, "MAX_KEYS", 5)
    t.start(None, "terminal", upload=False)
    for i in range(10):
        t.count("error.fingerprint", "n", where="server", fp=f"{i:016x}")
    assert len(t._agg) == 5
    assert t._health["dropped"] == 5


def test_three_internal_failures_trip_it_off(telemetry_enabled, monkeypatch):
    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    t._ready.wait(5)
    monkeypatch.setattr(t.queue, "add_counters", lambda *a, **k: False)
    for _ in range(3):
        t.count("tool.summary", "open", tool="roi")
        t.flush()
    assert t.enabled is False
    t.count("tool.summary", "open", tool="roi")
    assert t._agg == {}


def test_shutdown_writes_session_summary_within_budget(telemetry_enabled):
    t, fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    t._ready.wait(5)
    queue = t.queue
    started = time.monotonic()
    t.shutdown(budget_s=2.0)
    assert time.monotonic() - started < 2.5
    assert queue.open()
    _, records = queue.snapshot()
    summaries = [json.loads(r[3]) for r in records if r[2] == "session.summary"]
    assert len(summaries) == 1 and summaries[0]["ended"] == "clean"
    assert schema.validate_record("session.summary", summaries[0])
    assert fake.requests == []  # upload=False


def test_crashed_session_reported_as_unknown(telemetry_enabled):
    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    t.sync()
    queue = t.queue
    queue.set_state("session:" + "f" * 32, {"updated": time.time() - 3600, "window":
                                            "2026-09-27T10", "props": t.session_props("unknown")})
    queue.set_state("session:" + "e" * 32, {"updated": time.time(), "window":
                                            "2026-09-27T10", "props": t.session_props("unknown")})
    t._recover_sessions()
    _, records = queue.snapshot()
    unknown = [json.loads(r[3]) for r in records if r[2] == "session.summary"]
    assert len(unknown) == 1 and unknown[0]["ended"] == "unknown"
    assert queue.get_state("session:" + "e" * 32) is not None  # live neighbour kept


def test_turning_off_clears_the_queue(telemetry_enabled, monkeypatch):
    t, _fake = telemetry_enabled
    t.start(None, "terminal", upload=False)
    t.count("tool.summary", "open", tool="roi")
    t.sync()
    assert counters(t)
    monkeypatch.setenv("PLEXORA_TELEMETRY", "off")
    t.reconfigure()
    assert t.enabled is False
    assert counters(t) == []


def test_diagnostics_only_fields_dropped_in_anonymous(telemetry_enabled, monkeypatch):
    t, _fake = telemetry_enabled
    monkeypatch.setenv("PLEXORA_TELEMETRY", "anonymous")
    t.start(None, "terminal", upload=False)
    t.count("render.summary", "gestures", browser="chrome", os="mac", gpu="nvidia",
            label_renderer="gpu")
    t.sync()
    (row,) = [r for r in counters(t) if r[1] == "render.summary"]
    assert json.loads(row[3]) == {"browser": "chrome", "os": "mac", "label_renderer": "gpu"}
