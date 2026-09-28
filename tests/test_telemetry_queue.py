"""The local telemetry queue: bounded, exactly-once, and never fatal."""

import json
import os
import sqlite3
import stat
import sys

import pytest

from plexora.telemetry import batch, queue as queue_module, schema
from plexora.telemetry.queue import Queue, window_of

WINDOW_OLD = "2026-01-01T10"


def dims(**kwargs):
    return json.dumps(kwargs, sort_keys=True, separators=(",", ":"))


@pytest.fixture
def q(tmp_path):
    queue = Queue(tmp_path / ".telemetry" / "queue.sqlite")
    assert queue.open()
    yield queue
    queue.close()


def test_rollback_journal_not_wal(q):
    mode = q._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "delete"


def test_default_path_is_under_the_data_root(tmp_path):
    assert queue_module.default_path() == tmp_path / ".telemetry" / "queue.sqlite"


def test_upsert_adds_counts_and_bins(q):
    row = ("server.summary", "route_ms", dims(route="health", status="2xx"), "hist")
    bins = [1, 0, 0, 0, 0, 0, 0, 0, 0]
    assert q.add_counters(WINDOW_OLD, [(*row, 1, bins, 5.0, 5.0)])
    assert q.add_counters(WINDOW_OLD, [(*row, 2, [0, 2, 0, 0, 0, 0, 0, 0, 0], 60.0, 40.0)])
    counters, _ = q.snapshot()
    assert len(counters) == 1
    got = counters[0]
    assert got[5] == 3
    assert list(got[6:15]) == [1, 2, 0, 0, 0, 0, 0, 0, 0]
    assert got[15] == 65.0 and got[16] == 40.0


def build_with(mode="diagnostics"):
    client = {"install_id": "0" * 32, "session_id": "1" * 32}
    return batch.builder(client, mode, frozenset())


def test_take_batch_only_closed_windows_and_exactly_once(q):
    row = ("tool.summary", "open", dims(tool="gating"), "count", 4, None, 0, 0)
    q.add_counters(WINDOW_OLD, [row])
    q.add_counters(window_of(), [row])
    taken = q.take_batch(build_with())
    assert taken is not None
    batch_id, payload = taken
    body = json.loads(__import__("gzip").decompress(payload))
    assert body["batch_id"] == batch_id
    assert [e["window"] for e in body["events"]] == [WINDOW_OLD]
    # Claimed: a second taker gets nothing new (the open window stays).
    assert q.take_batch(build_with()) is None
    # Unacked: the same bytes come back under the same id.
    q.unack(batch_id)
    again = q.take_batch(build_with())
    assert again == (batch_id, payload)
    q.ack(batch_id)
    assert q.take_batch(build_with()) is None
    counters, _ = q.snapshot()
    assert [c[0] for c in counters] == [window_of()]


def test_take_batch_include_open_takes_everything(q):
    q.add_counters(window_of(), [("tool.summary", "open", dims(tool="roi"), "count", 1,
                                  None, 0, 0)])
    assert q.take_batch(build_with(), include_open=True) is not None
    assert q.snapshot() == ([], [])


def test_rejected_rows_are_dropped_not_retried(q):
    q.add_counters(WINDOW_OLD, [("tool.summary", "open", dims(tool="/etc/passwd"), "count",
                                 1, None, 0, 0)])
    assert q.take_batch(build_with()) is None
    assert q.snapshot() == ([], [])


def test_records_round_trip(q):
    props = {"outcome": "ready", "remote": False}
    q.add_events(WINDOW_OLD, [("project.load", json.dumps(props), 3)])
    batch_id, payload = q.take_batch(build_with())
    body = json.loads(__import__("gzip").decompress(payload))
    assert body["events"] == [{"type": "project.load", "window": WINDOW_OLD, "props": props}]


def test_state_round_trip(q):
    assert q.get_state("token") is None
    q.set_state("token", {"token": "x"})
    assert q.get_state("token") == {"token": "x"}
    q.set_state("token", None)
    assert q.get_state("token", "gone") == "gone"


def test_trim_by_age(q):
    q.add_counters("2000-01-01T00", [("tool.summary", "open", dims(tool="roi"), "count", 1,
                                      None, 0, 0)])
    q.add_counters(window_of(), [("tool.summary", "open", dims(tool="roi"), "count", 1,
                                  None, 0, 0)])
    assert q.trim() == 1
    assert [c[0] for c in q.snapshot()[0]] == [window_of()]


def test_trim_by_size_drops_oldest_windows_first(q):
    for hour in range(24):
        window = f"2026-09-{10 + hour // 12:02d}T{hour % 12:02d}"
        rows = [("error.fingerprint", "n",
                 dims(where="server", fp=f"{i:016x}", exc_type="KeyError"), "count", 1,
                 None, 0, 0) for i in range(300)]
        q.add_counters(window, rows)
    before = q.size_bytes()
    q.trim(max_bytes=before // 2, max_age_days=100000)
    after = q.size_bytes()
    assert after <= before // 2
    windows = sorted({c[0] for c in q.snapshot(limit=100000)[0]})
    assert windows and windows[-1] == "2026-09-11T11"
    assert "2026-09-10T00" not in windows


def test_corrupt_file_is_moved_aside(tmp_path):
    path = tmp_path / ".telemetry" / "queue.sqlite"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not a database" * 100)
    queue = Queue(path)
    assert queue.open()
    assert queue.add_counters(WINDOW_OLD, [("tool.summary", "open", dims(tool="roi"),
                                            "count", 1, None, 0, 0)])
    assert list(path.parent.glob("queue.sqlite.corrupt-*"))
    queue.close()


@pytest.mark.skipif(sys.platform.startswith("win") or os.geteuid() == 0,
                    reason="POSIX permissions")
def test_read_only_root_disables_quietly(tmp_path):
    root = tmp_path / "ro"
    root.mkdir()
    root.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        queue = Queue(root / ".telemetry" / "queue.sqlite")
        assert queue.open() is False
        assert queue.add_counters(WINDOW_OLD, []) is True  # nothing to do
        assert queue.add_counters(WINDOW_OLD, [("tool.summary", "open", "{}", "count", 1,
                                                None, 0, 0)]) is False
        assert queue.get_state("x", 5) == 5
    finally:
        root.chmod(stat.S_IRWXU)


def test_sqlite_failure_is_an_answer_not_an_exception(q, monkeypatch):
    q._conn.close()
    q._conn = sqlite3.connect(":memory:")  # tables missing
    assert q.add_counters(WINDOW_OLD, [("tool.summary", "open", "{}", "count", 1, None, 0,
                                        0)]) is False
    assert q.errors >= 1


def test_diagnostics_fields_stripped_when_sent_anonymous(q):
    q.add_counters(WINDOW_OLD, [
        ("render.summary", "gestures", dims(browser="chrome", os="mac", gpu="nvidia"),
         "count", 2, None, 0, 0),
        ("render.summary", "gestures", dims(browser="chrome", os="mac", gpu="amd"),
         "count", 3, None, 0, 0),
    ])
    _id, payload = q.take_batch(build_with("anonymous"))
    body = json.loads(__import__("gzip").decompress(payload))
    rows = body["events"][0]["rows"]
    assert rows == [{"k": "gestures", "d": {"browser": "chrome", "os": "mac"}, "n": 5}]


def test_batch_respects_gzip_cap(q):
    rows = [("error.fingerprint", "n", dims(where="server", fp=os.urandom(8).hex(),
                                            exc_type="KeyError"), "count", 1, None, 0, 0)
            for _ in range(2000)]
    q.add_counters(WINDOW_OLD, rows)
    client = {"install_id": "0" * 32}
    build = batch.builder(client, "diagnostics", frozenset(), max_gzip=8 * 1024)
    taken = q.take_batch(build)
    assert taken is not None and len(taken[1]) <= 8 * 1024
    assert q.snapshot(limit=10000)[0]  # the rest stays for the next batch
