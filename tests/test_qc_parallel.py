"""AutoQC with several packets out at once: the same decisions, no extra calls.

The harness answers a QC session with lanes (`QCWorkflow.parallel`), and the
engine hands out side by side only decisions no pending answer can change
(`QCEngine.next_ready`): stain checks, score reviews and audit pages at any
time; candidates only where they do not touch, once the bulk pass is done.
What is pinned: a 4-lane run decides exactly what the serial run decides, with
the same model calls and no reissue, whatever order the answers come back in;
and the pieces the lanes lean on -- the call limiter, the warm gate, the stop
that cuts a streaming reply, the memo's index.
"""

import json
import threading

import pytest

from plexora.ai import bench_qc
from plexora.ai.harness import decision


@pytest.mark.pro
@pytest.mark.parametrize("jitter", [0.0, 0.5])
def test_four_lanes_decide_what_one_lane_decides_with_the_same_calls(jitter):
    rows = bench_qc.run_synthetic(["mixed", "dropout"], "oracle", arms=("harness:1", "harness:4"),
                                  latency_scale=0.01, jitter=jitter, seed=1)
    by_scene: dict = {}
    for row in rows:
        assert row.get("status") == "done", row
        by_scene.setdefault(row["scenario"], {})[row["arm"]] = row
    for scene, arms in by_scene.items():
        one, four = arms["harness:1"], arms["harness:4"]
        assert json.dumps(one["signature"], sort_keys=True) == json.dumps(four["signature"], sort_keys=True), scene
        assert four["model_calls"] == one["model_calls"], (scene, one["calls_by_task"], four["calls_by_task"])
        assert four["calls_by_task"] == one["calls_by_task"], scene
        assert four["reissued"] == 0 and four["invalid_answers"] == 0, scene
        # Side by side for real: more than one call was out at once.
        assert four["peak_calls_out"] >= 2, scene


@pytest.mark.pro
def test_lanes_from_the_panel_score_every_check_draw_ahead_and_decide_the_same():
    """A sixteen-channel panel: the session sizes its lanes from its planned
    work (twelve stain checks fill twelve and more), its checks score side by
    side with none lost to two threads writing one pointer file (blur
    `current.json`, WinError 5 on Windows), the packets' panels are drawn
    outside the session's lock and read back when built, and the lanes decide
    what 1 does with the same calls."""
    rows = bench_qc.run_synthetic(["cyclic"], "oracle", arms=("harness:1", "harness:auto"),
                                  latency_scale=0.01, jitter=0.3)
    one, many = rows
    assert one.get("status") == many.get("status") == "done", rows
    assert many["lanes"] >= 12, many["lanes"]
    assert not [c for c in many["checks"] if "skipped" in c and "magic select" not in c], many["checks"]
    assert json.dumps(one["signature"], sort_keys=True) == json.dumps(many["signature"], sort_keys=True)
    assert many["model_calls"] == one["model_calls"] and many["reissued"] == 0
    assert many["peak_calls_out"] >= 4        # side by side (fewer on a loaded machine)
    # Drawn ahead, then read back: about one hit per panel drawn.
    assert many["panels"]["hits"] >= 0.8 * many["panels"]["misses"], many["panels"]
    assert one["panels"]["hits"] < 0.2 * one["panels"]["misses"], one["panels"]


def test_the_lanes_follow_the_planned_work_between_the_floor_and_the_cap(monkeypatch):
    from plexora.plugins.qc import capabilities_session as qc

    monkeypatch.delenv("PLEXORA_QC_LANES", raising=False)

    def units(stains, reviews):
        out = {f"s{i}": {"type": "check", "check": "stain"} for i in range(stains)}
        out.update({f"r{i}": {"type": "check", "check": "blur"} for i in range(reviews)})
        out["c"] = {"type": "channel"}
        return out
    assert qc.lanes_for(None, units(3, 3)) == 3 + 1 + 1            # five channels
    assert qc.lanes_for(None, units(12, 8)) == 12 + 2 + 1          # four cycles of four
    assert qc.lanes_for(None, units(36, 18)) == qc.MAX_LANES       # a 40-channel panel
    assert qc.lanes_for(None, {}) == qc.MIN_LANES
    assert qc.lanes_for(3, units(36, 18)) == 3                     # the start's own number
    monkeypatch.setenv("PLEXORA_QC_LANES", "6")
    assert qc.lanes_for(None, units(36, 18)) == 6


def test_the_pace_holds_calls_past_the_per_minute_rate(monkeypatch):
    monkeypatch.setenv("PLEXORA_AI_CALLS_PER_MIN", "3")
    pace = decision._Pace(window_s=0.6)
    waits = [pace.wait() for _ in range(3)]
    assert max(waits) < 0.05                                        # within the rate: no wait
    held = pace.wait()                                              # the fourth waits its turn
    assert 0.4 <= held <= 1.5, held
    stopped = decision._Pace(window_s=30.0)
    for _ in range(3):
        stopped.wait()
    assert stopped.wait(halted=lambda: True) < 0.05                # a halt is never held


def test_a_reservation_holds_its_units_and_lapses_when_they_moved(tmp_path):
    from plexora.agent.sessions.engine import BaseEngine

    class Engine(BaseEngine):
        PREPARABLE = ("look",)

        def __init__(self, record):
            self.record = record
            self._scope = self._needs = None

        def key_of(self, unit):
            return unit["id"]

        def next_unit(self):
            return "look", [self.record["units"]["a"]]

        def accepts(self, kind, units):
            return True

    record = {"units": {"a": {"id": "a", "state": "ready"}}, "outstanding": {}, "packet_seq": 3}
    engine = Engine(record)
    kind, units = engine.choose()
    assert kind == "look" and units[0]["id"] == "a"
    token = engine.reserve(kind, units)
    assert engine.reserved_keys() == {"a"}
    assert engine.unreserve(token) == ("look", [record["units"]["a"]])   # as reserved: built
    token = engine.reserve(kind, units)
    record["units"]["a"]["state"] = "closed"                             # moved meanwhile
    assert engine.unreserve(token) is None and not engine.reserved_keys()
    engine.reserve(kind, units)
    record["reserved"] = {t: {**r, "at": 0} for t, r in record["reserved"].items()}
    assert not engine.reserved_keys()                                    # a dead reader's lapses
    snap = engine.snapshot()
    snap.record["units"]["a"]["state"] = "drawn"
    assert record["units"]["a"]["state"] == "closed"                     # the snapshot is a copy


def test_external_workers_get_lanes_for_qc(monkeypatch):
    from plexora.ai import delegation, models_config

    monkeypatch.setattr(models_config, "mapped", lambda module: False)
    block = delegation.block("qc_worker", session_id="qs_1", lanes=4)
    assert [lane["reader"] for lane in block["lanes"]] == ["lane1", "lane2", "lane3", "lane4"]
    assert all("parallel: 4" in lane["brief"] and "qs_1" in lane["brief"] for lane in block["lanes"])
    assert "decisions that stand on nothing another lane holds" in block["how"]


def test_the_limiter_gives_a_lane_back_after_clean_calls_and_takes_one_on_a_rate_limit():
    throttle = decision._Throttle(4)
    assert throttle.slow_down() and throttle.limit == 3
    assert throttle.slow_down() and throttle.slow_down() and throttle.limit == 1
    assert not throttle.slow_down() and throttle.limit == 1          # never below one
    for _ in range(decision.THROTTLE_RECOVER_AFTER):
        throttle.acquire()
        throttle.release(True)
    assert throttle.limit == 2 and throttle.lowest == 1
    # A full limiter holds the next call until one is released.
    throttle.acquire()
    throttle.acquire()
    got = threading.Event()
    waiter = threading.Thread(target=lambda: (throttle.acquire(), got.set()))
    waiter.start()
    assert not got.wait(0.3)
    throttle.release(True)
    assert got.wait(2)
    waiter.join(2)


def test_a_stop_cuts_a_streaming_reply_and_nothing_else_does(monkeypatch):
    monkeypatch.setattr(decision, "STOP_LOOK_S", 0.0)
    stopped = {"now": False}
    first = []
    guard = decision._runaway_guard(lambda: stopped["now"], on_first=lambda: first.append(1))
    guard('{"kind": ')
    guard('"stain_check"')
    assert first == [1]                                   # told once, at the first piece
    stopped["now"] = True
    with pytest.raises(decision._Cut):
        guard(', "verdict"')


def test_the_first_call_of_a_task_goes_alone_and_the_rest_wait_for_it(monkeypatch):
    monkeypatch.setattr(decision, "WARM_WAIT_S", 5.0)

    class Run:
        parallel = 3
        _halt = None
        _lock = threading.Lock()
        _warming: dict = {}

        @staticmethod
        def check():
            return None

    run = Run()
    lanes = [decision._Lane(i, f"lane{i}", decision._Worker(i)) for i in range(3)]
    warmer = decision.DecisionRun._await_warm(run, "qc.stain_check", lanes[0])
    assert isinstance(warmer, threading.Event)
    # Another task is not held back.
    other = decision.DecisionRun._await_warm(run, "qc.blur", lanes[1])
    assert isinstance(other, threading.Event)
    released = []

    def second():
        assert decision.DecisionRun._await_warm(run, "qc.stain_check", lanes[2]) is None
        released.append(1)
    waiter = threading.Thread(target=second)
    waiter.start()
    waiter.join(0.3)
    assert not released                                    # waits for the first
    warmer.set()                                           # its reply's first piece
    waiter.join(2)
    assert released == [1]
    # A worker that has called before holds the prefix: it never waits.
    lanes[2].worker.calls = 1
    assert decision.DecisionRun._await_warm(run, "qc.final_review", lanes[2]) is None


def test_the_memo_reads_its_file_once_and_follows_another_writer(tmp_path, monkeypatch):
    from plexora.agent.sessions import memo

    monkeypatch.setattr(memo, "path", lambda kind, project: tmp_path / f"{kind}_{project}.jsonl")
    memo.put("qc", "p", "k1", "a", {"kind": "x", "n": 1})
    assert memo.get("qc", "p", "k1", "a")["answer"] == {"kind": "x", "n": 1}
    calls = {"n": 0}
    original = memo._index

    def counted(target):
        calls["n"] += 1
        return original(target)
    monkeypatch.setattr(memo, "_index", counted)
    for _ in range(5):
        assert memo.get("qc", "p", "k1", "a") is not None
    assert memo.get("qc", "p", "k1", "other") is None
    held = memo._INDEX[str(memo.path("qc", "p"))]
    # Another process appends: the stamp changes and the file is read afresh.
    with memo.path("qc", "p").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"key": "k2", "agent": "a", "answer": {"kind": "y"}}) + "\n")
    assert memo.get("qc", "p", "k2", "a")["answer"] == {"kind": "y"}
    assert memo._INDEX[str(memo.path("qc", "p"))][0] != held[0]
    # A later answer to the same key wins, as the whole-file read let it.
    memo.put("qc", "p", "k1", "a", {"kind": "x", "n": 2})
    assert memo.get("qc", "p", "k1", "a")["answer"]["n"] == 2
    assert memo.forget("qc", "p") == 3 and memo.get("qc", "p", "k1", "a") is None
