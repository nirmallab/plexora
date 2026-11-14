"""A data node keeps a bounded number of finished jobs, even while an older
job is still running.

`node/api.py` keeps every measurement, cell-feature and surface job in one
ordered store with its result bytes, and promises that only `JOBS_KEPT`
finished jobs stay ("Finished jobs kept for their result; the oldest go
first"). An hour-long measurement started first must not stop the finished
jobs behind it from going: otherwise every later result (a parquet table and
plane tar, a feature npz, a mesh) stays resident for as long as it runs.
"""

from collections import OrderedDict

from plexora.server.node import api as node_api


def _queued_spawn(spawned):
    """`_work.spawn` that keeps each job's runner instead of starting it, so
    the test decides which jobs finish and which are still running."""
    def spawn(target, *args, name=None, **kwargs):
        spawned.append(target)
        return None
    return spawn


def test_finished_jobs_beyond_the_bound_go_while_an_older_job_still_runs(monkeypatch):
    monkeypatch.setattr(node_api, "_JOBS", OrderedDict())
    spawned = []
    monkeypatch.setattr(node_api._work, "spawn", _queued_spawn(spawned))

    def long_measurement(job):  # never run: still measuring an hour-long stack
        raise AssertionError("not run in this test")

    def quick(job):
        job["result"] = b"x" * 1024  # a feature npz or a mesh, in miniature

    first = node_api._start_job("measure", long_measurement)
    later = []
    for _ in range(20):
        later.append(node_api._start_job("cell-features", quick))
        spawned[-1]()  # this one finishes at once

    jobs = node_api._JOBS
    assert jobs[first]["state"] == "running"
    finished = [job_id for job_id, job in jobs.items() if job["state"] == "done"]
    assert len(finished) <= node_api.JOBS_KEPT, (
        f"{len(finished)} finished jobs (and their result bytes) kept behind the running "
        f"one; the bound is {node_api.JOBS_KEPT}")
    # The ones kept are the newest, so a primary polling its job finds it.
    assert finished == later[-len(finished):]
