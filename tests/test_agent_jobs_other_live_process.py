"""Jobs another live process is running: a second store leaves them running.

The MCP process and the server share `<data_root>/.agent/jobs`. The first
`jobs.store()` either of them makes calls `recover()`, which must mark only the
jobs whose process has died -- not one the other process is still running.
"""

import json
import subprocess
import sys

import pytest

from plexora.agent import jobs


def _running_record(folder, job_id, pid):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{job_id}.json").write_text(json.dumps({
        "job_id": job_id, "status": "running", "pid": pid,
        "capability": "gating.bulk", "project": "p",
        "submitted_at": "2026-01-01T00:00:00.000000Z",
        "progress": {"done": 3, "total": 10, "message": "running"}}), encoding="utf-8")


@pytest.fixture
def live_pid():
    """The pid of a process that is alive for the whole test."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait(timeout=30)


def test_a_new_store_leaves_a_job_another_live_process_runs_as_running(tmp_path, live_pid):
    folder = tmp_path / "jobs"
    _running_record(folder, "job_live00000001", live_pid)

    store = jobs.JobStore(folder)

    on_disk = json.loads((folder / "job_live00000001.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "running", on_disk
    got = store.get("job_live00000001")
    assert got["status"] == "running", got
    assert got["status"] not in jobs.TERMINAL
    # Not reachable from here, but not finished either: job_cancel answers
    # "running in another process", not "already finished".
    assert store.cancel("job_live00000001") == ("running", "running")


def test_a_new_store_still_marks_a_dead_process_job_interrupted(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    folder = tmp_path / "jobs"
    _running_record(folder, "job_dead00000001", child.pid)

    store = jobs.JobStore(folder)

    assert store.get("job_dead00000001")["status"] == "interrupted"
