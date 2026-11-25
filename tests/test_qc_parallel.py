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
def test_eight_lanes_on_a_sixteen_channel_panel_score_every_check_and_decide_the_same():
    """Twelve stain checks and four cycles' blur and registration scored side
    by side in the bulk pass: no check is lost to two threads writing one
    pointer file (blur `current.json`, WinError 5 on Windows), and 8 lanes
    decide what 1 does with the same calls."""
    rows = bench_qc.run_synthetic(["cyclic"], "oracle", arms=("harness:1", "harness:8"),
                                  latency_scale=0.01, jitter=0.3)
    one, eight = rows
    assert one.get("status") == eight.get("status") == "done", rows
    assert not [c for c in eight["checks"] if "skipped" in c and "magic select" not in c], eight["checks"]
    assert json.dumps(one["signature"], sort_keys=True) == json.dumps(eight["signature"], sort_keys=True)
    assert eight["model_calls"] == one["model_calls"] and eight["reissued"] == 0
    assert eight["peak_calls_out"] >= 4


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
