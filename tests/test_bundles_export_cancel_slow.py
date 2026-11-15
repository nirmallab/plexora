"""An export cancel whose thread does not stop in time (audit lead S22).

`cancel_job` waits a few seconds for the export thread. It used to remove the
`.part` file (and a staged export's folder) whether or not the thread had
stopped -- under a thread still writing to it (on Windows the unlink of an open
file raises, so the cancel itself failed). Now it removes them only once the
thread has stopped; a thread that stops later cleans up after itself."""

import threading
import time
from pathlib import Path

import pytest

from plexora.server.models import bundles, layer_jobs
from tests.test_bundles import project  # noqa: F401 -- fixture


def _slow_export(release, entered):
    def export(names, dest, *, cancel=None, **_):
        part = Path(str(dest) + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"partial")
        entered.set()
        release.wait(10)
        # What the real sink does on a cancel: abort, removing its own part.
        if cancel is not None and cancel.is_set():
            part.unlink(missing_ok=True)
            raise bundles.Cancelled()
        raise AssertionError("expected a cancel")
    return export


def _wait_stopped(token, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not layer_jobs.running_tasks(prefix=token):
            return
        time.sleep(0.02)
    raise AssertionError("the export thread did not stop")


@pytest.mark.parametrize("staged", [False, True])
def test_a_cancel_leaves_a_still_running_exports_files_to_it(project, tmp_path,  # noqa: F811
                                                            monkeypatch, staged):
    release, entered = threading.Event(), threading.Event()
    monkeypatch.setattr(bundles, "export", _slow_export(release, entered))
    real_stop = layer_jobs.stop_task
    monkeypatch.setattr(layer_jobs, "stop_task",
                        lambda owner, task, timeout=5.0: real_stop(owner, task, timeout=0.1))

    dest = None if staged else str(tmp_path / "slow.plexora")
    token = bundles.start_export(["slide"], dest=dest)["job"]
    assert entered.wait(10)
    target = Path(bundles._JOBS[token]["dest"])
    part = Path(str(target) + ".part")

    try:
        bundles.cancel_job(token)
        # The thread is still in its write: what it is writing is left to it.
        assert part.exists()
    finally:
        release.set()
    _wait_stopped(token)
    assert not part.exists()
    if staged:
        assert not target.parent.exists()
