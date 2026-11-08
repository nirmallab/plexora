"""A long task's status poll answered by the website's page.

On the website a background task holds Python's only thread for as long as it
runs, so New Dataset's scan and create -- one task each -- left the dialog's
poll unanswered and its Stop undelivered until they ended. A task started with
a `mirror` (server/models/layer_jobs.py) posts its status route's answer to
the page as it goes, which the page answers the poll from
(services/pythonClient.js), and hears a Stop pressed meanwhile through the
page's flag (`cancelled(key)`).
"""

from __future__ import annotations

import time

import pytest

from plexora.server.models import layer_jobs


@pytest.fixture
def page(monkeypatch):
    posted, stopped = [], set()
    layer_jobs.set_page(posted.append, lambda key: key in stopped)
    monkeypatch.setattr(layer_jobs, "MIRROR_EVERY_S", 0.0)
    yield posted, stopped
    layer_jobs.set_page(None, None)


def _finish(owner, task_id, timeout=10.0):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        record = layer_jobs.tasks(owner).get(task_id)
        if record and record.get("status") != "pending":
            return record
        time.sleep(0.01)
    raise AssertionError("the task did not finish")


def test_the_task_posts_its_answer_as_it_goes_and_hears_the_page_stop(page):
    posted, stopped = page
    steps = []

    def work(stage, report, cancel):
        for n in range(1000):
            if cancel.is_set():
                return
            steps.append(n)
            report(n, 1000)
            if n == 3:
                stopped.add("dataset-scan:t1/1")
            time.sleep(0.001)

    mirror = {"key": "dataset-scan:t1/1", "get": "/datasets/scan/t1",
              "answer": lambda: {"token": "t1", "done": len(steps)},
              "cancel": "/datasets/scan/t1/cancel", "cancelled": {"cancelled": True, "stopped": True}}
    layer_jobs.start_task("test-mirror", "t1/1", work, mirror=mirror)
    record = _finish("test-mirror", "t1/1")

    assert record["status"] == "cancelled"
    assert len(steps) < 10  # stopped by the page's flag, not run to the end
    running = [m for m in posted if m["running"]]
    assert running and all(m["kind"] == "mirror" and m["key"] == "dataset-scan:t1/1" for m in posted)
    assert running[0]["get"] == "/datasets/scan/t1"
    assert running[0]["cancel"] == "/datasets/scan/t1/cancel"
    assert running[-1]["answer"]["done"] >= 3
    # The last word says it has ended: the page asks Python again from then on.
    assert posted[-1]["running"] is False and "answer" not in posted[-1]


def test_an_answer_that_fails_never_fails_the_task(page):
    posted, _stopped = page

    def work(stage, report, cancel):
        report(1, 2)

    def broken():
        raise RuntimeError("no answer yet")

    layer_jobs.start_task("test-mirror", "t2", work, mirror={
        "key": "k2", "get": "/x", "answer": broken})
    assert _finish("test-mirror", "t2")["status"] == "ready"
    assert [m["running"] for m in posted] == [False]


def test_without_a_page_nothing_is_posted_and_the_cancel_is_the_tasks_own():
    layer_jobs.set_page(None, None)
    seen = {}

    def work(stage, report, cancel):
        seen["cancel"] = type(cancel).__name__
        report(1, 1)

    layer_jobs.start_task("test-mirror", "t3", work, mirror={
        "key": "k3", "get": "/x", "answer": lambda: {}})
    assert _finish("test-mirror", "t3")["status"] == "ready"
    assert seen["cancel"] == "Event"


def test_new_dataset_scan_and_create_are_mirrored():
    """Both of New Dataset's tasks name their status and cancel routes."""
    import inspect

    from plexora.server.models import dataset_build, dataset_scan

    scan = inspect.getsource(dataset_scan._run)
    assert '"get": f"/datasets/scan/{token}"' in scan
    assert '"cancel": f"/datasets/scan/{token}/cancel"' in scan
    build = inspect.getsource(dataset_build.start_build)
    assert '"get": f"/datasets/scan/{token}/create"' in build
    assert '"cancel": f"/datasets/scan/{token}/create/cancel"' in build
