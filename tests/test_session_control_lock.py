"""control.json is read-modify-written by the viewer's control route and by the
engine driving the session, which may be another process (`plexora mcp
serve`). Unlocked, a pause, stop or limit answer posted between the other
side's read and write was lost (audit ai-agent-core-6)."""

import os
import subprocess
import sys
import threading

import pytest

from plexora.agent.sessions.store import SessionStore


@pytest.fixture
def store(tmp_path):
    st = SessionStore("gating", root=tmp_path)
    st.folder("s1").mkdir(parents=True, exist_ok=True)
    return st


def test_set_control_waits_for_another_process_holding_control(store):
    if os.name == "nt":
        pytest.skip("POSIX flock probe")
    lock = store.folder("s1") / "control.json.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, sys; h = open(sys.argv[1], 'a+b'); "
         "fcntl.flock(h.fileno(), fcntl.LOCK_EX); print('held', flush=True); "
         "sys.stdin.read()", str(lock)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        worker = threading.Thread(target=store.set_control, args=("s1",),
                                  kwargs={"paused": True})
        worker.start()
        worker.join(0.5)
        assert worker.is_alive(), "set_control wrote while another process held control.json"
    finally:
        holder.stdin.close()
        holder.wait(10)
    worker.join(10)
    assert store.control("s1")["paused"] is True


def test_update_control_merges_under_the_lock(store):
    """A limit answer merged into what is there NOW, not into a copy read
    earlier: another answer written in between survives."""
    store.set_control("s1", limit_answers={"a": "continue"}, paused=False)
    store.update_control("s1", lambda c: {"limit_answers": {
        **(c.get("limit_answers") or {}), "b": "stop"}})
    store.set_control("s1", paused=True)

    control = store.control("s1")
    assert control["limit_answers"] == {"a": "continue", "b": "stop"}
    assert control["paused"] is True
