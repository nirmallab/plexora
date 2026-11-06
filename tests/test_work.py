"""Where background work runs (plexora/_work.py), in each of its modes.

`threads` is every CPython process: real threads, as before the seam existed.
`inline` is the website's scheduling under CPython -- no threads; queued work
runs where its starter yields, or when the next outermost call begins, never
inside the starter's own call -- and it is how the suites that start work are
checked to come out the same without threads.
"""

import contextvars
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from plexora import _work

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mode(monkeypatch):
    def use(name):
        monkeypatch.setenv("PLEXORA_WORK", name)
        _work._reset_for_tests()
    yield use
    _work._reset_for_tests()


def test_threads_are_real_threads(mode):
    mode("threads")
    ran = threading.Event()
    handle = _work.spawn(ran.set, name="work-test")
    assert isinstance(handle, threading.Thread) and handle.name == "work-test"
    handle.join(5)
    assert ran.is_set() and not handle.is_alive()
    with _work.executor(max_workers=2) as pool:
        assert type(pool).__name__ == "ThreadPoolExecutor"
        assert pool.submit(lambda: 7).result() == 7


def test_an_unknown_mode_is_threads(mode):
    mode("fibres")
    assert _work.mode() == "threads" and _work.threaded()


def test_queued_work_never_runs_inside_its_starters_call(mode):
    mode("inline")
    seen = []

    def starter():
        _work.spawn(seen.append, "job")
        seen.append("starter returned")

    starter()
    assert seen == ["starter returned"]
    assert _work.pending() == 1
    _work.wait(0.01)
    assert seen == ["starter returned", "job"]


def test_it_runs_where_the_starter_yields_to_it(mode):
    mode("inline")
    done = threading.Event()
    handle = _work.spawn(done.set)
    assert handle.is_alive() and not done.is_set()
    assert _work.wait_event(done, 5) is True
    assert not handle.is_alive()

    order = []
    handle = _work.spawn(order.append, 1)
    handle.join()
    assert order == [1] and not handle.is_alive()


def test_it_runs_as_the_next_outermost_call_begins(mode):
    mode("inline")
    seen = []
    with _work.boundary():
        _work.spawn(seen.append, "queued by the first call")
        with _work.boundary():  # nested: not a new call
            pass
        assert seen == []
    assert seen == []  # answered first, as the website answers
    with _work.boundary():
        assert seen == ["queued by the first call"]


def test_work_queued_by_work_runs_too(mode):
    mode("inline")
    seen = []

    def first():
        seen.append(1)
        _work.spawn(seen.append, 2)

    _work.spawn(first)
    assert _work.run_pending() == 2
    assert seen == [1, 2]


def test_a_failure_is_logged_as_a_threads_would_be_and_the_rest_still_run(mode, caplog):
    mode("inline")
    seen = []
    _work.spawn(lambda: 1 / 0, name="broken")
    _work.spawn(seen.append, "after")
    _work.run_pending()
    assert seen == ["after"]
    assert "broken" in caplog.text and "ZeroDivisionError" in caplog.text


def test_a_task_starts_with_a_fresh_context_as_a_thread_does(mode):
    mode("inline")
    marker = contextvars.ContextVar("marker", default="unset")
    marker.set("the starter's")
    seen = []
    _work.spawn(lambda: seen.append(marker.get()))
    _work.run_pending()
    assert seen == ["unset"]


def test_a_cancelled_timer_never_runs(mode):
    mode("inline")
    seen = []
    timer = _work.daemon_timer(0.01, seen.append, "fired")
    timer.cancel()
    _work.run_pending()
    assert seen == []


def test_an_inline_pool_runs_each_call_as_it_is_submitted(mode):
    mode("inline")
    seen = []
    with _work.executor(max_workers=4, thread_name_prefix="x") as pool:
        future = pool.submit(seen.append, "now")
        assert seen == ["now"] and future.done()
        assert list(pool.map(lambda v: v * 2, [1, 2, 3])) == [2, 4, 6]
        failed = pool.submit(lambda: 1 / 0)
        assert isinstance(failed.exception(), ZeroDivisionError)


def test_a_poll_with_nothing_queued_lets_time_pass(mode):
    """A poll waiting on the user or a node must not spin."""
    mode("inline")
    started = time.monotonic()
    _work.wait(0.05)
    assert time.monotonic() - started >= 0.04


def test_the_budget_is_checked_between_tasks(mode):
    mode("inline")
    for _ in range(3):
        _work.spawn(time.sleep, 0.02)
    assert _work.run_pending(budget_s=0.01) == 1
    assert _work.pending() == 2


def test_the_cooperative_runtime_sleeps_through_its_idle_hook(mode):
    mode("cooperative")
    slept = []
    _work.set_idle(slept.append)
    _work.wait(0.25)
    assert slept == [0.25]


def test_a_condition_wait_returns_at_once_when_work_ran(mode):
    mode("inline")
    condition = threading.Condition()
    box = []
    _work.spawn(box.append, "arrived")
    with condition:
        started = time.monotonic()
        assert _work.wait_condition(condition, 5) is False
        assert time.monotonic() - started < 1
    assert box == ["arrived"]


#: The suites that start background work, run again with work queued. The
#: gating sessions' suite is the website's real workload and passes too, but
#: serially it takes most of an hour: it runs when PLEXORA_TEST_INLINE_GATING=1
#: (2026-11-06: 81 tests, every one passing; the two longest need a per-test
#: timeout over 300 s on a loaded machine).
SUITES = ("tests/test_agent_jobs.py", "tests/test_layer_jobs.py")
GATING = ("tests/test_gating_session.py",)


@pytest.mark.skipif(os.environ.get("PLEXORA_WORK", "threads") != "threads",
                    reason="this is the outer run")
@pytest.mark.parametrize("suites", [
    SUITES,
    pytest.param(GATING, marks=pytest.mark.skipif(
        os.environ.get("PLEXORA_TEST_INLINE_GATING") != "1",
        reason="most of an hour serially; PLEXORA_TEST_INLINE_GATING=1 runs it")),
], ids=["jobs", "gating"])
def test_the_suites_that_start_work_pass_with_it_queued(suites):
    """The same assertions, without threads. Tests marked `threads` (they
    watch work while it runs) skip themselves under PLEXORA_WORK=inline."""
    env = {**os.environ, "PLEXORA_WORK": "inline"}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-p", "no:cacheprovider",
         "--timeout", "1800", *suites],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=7200)
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]


def test_a_checkpoint_never_runs_a_request_inside_a_task(mode):
    """Answering a request inside a suspended task froze the website's worker
    (a lock the task held): a checkpoint yields to nothing, in every mode."""
    yielded = []
    for name in ("threads", "inline", "cooperative"):
        mode(name)
        _work.set_idle(yielded.append, waiting=lambda: True)
        _work.checkpoint()
    assert yielded == []
