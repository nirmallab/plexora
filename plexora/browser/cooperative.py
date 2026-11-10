"""Background work on the website, where Python has one thread and no other.

`plexora._work` queues every job, thread and pool call when `PLEXORA_WORK` is
``cooperative``; this module is the worker's side of that. The worker
(`workers/pythonWorker.js`) calls `step` whenever it has no request to answer,
so a job starts once the request that queued it has been answered and runs in
the gaps between requests; `install` wires the three things the page needs to
stay in charge meanwhile:

- **A mirror of every job's record** (`post`), sent as the record is written.
  While a job holds the worker, `GET /agent/v1/jobs/<id>` cannot reach Python,
  so the page answers it from the last record it was sent.
- **A cancel the page can make while Python is busy** (`cancelled`): the page
  flags the job in shared memory (the site is cross-origin isolated), and the
  job sees it at its next `call.check_cancelled()`. Without shared memory a
  cancel lands between jobs, like any other request.
- **A sleep that lets the page in** (`idle`): a poll waiting on the user (a
  paused session, a set-up form) yields to the event loop for its seconds
  rather than blocking the worker the answer has to arrive through.

Under CPython this is testable as it is (tests/test_cooperative.py), with
plain functions for the three.
"""

from __future__ import annotations

import os

from plexora import _work


def install(*, post=None, cancelled=None, idle=None, waiting=None) -> None:
    """Switch this process to cooperative work and connect it to the page.

    `post(message)` sends a dict to the page; `cancelled(job_id)` reads the
    page's cancel flags; `idle(seconds)` passes time while serving requests;
    `waiting()` says whether a request is waiting (long tasks then let it in,
    `_work.checkpoint`). Each may be None (that part is off)."""
    from plexora.agent import jobs
    from plexora.server.models import layer_jobs

    os.environ["PLEXORA_WORK"] = "cooperative"
    _work.set_idle(idle, waiting)
    jobs.set_cancel_probe(cancelled)
    # A long task's status poll answered by the page, and its Stop passed on
    # (New Dataset's scan and create: server/models/layer_jobs.py).
    layer_jobs.set_page(post, cancelled)
    if post is not None:
        from plexora.agent.bridge_wire import job_state

        # `state` is what `GET /agent/v1/jobs/<id>` answers with, so the page's
        # answer from the mirror is the route's own.
        jobs.observe(lambda record: post({"kind": "job", "job": record,
                                          "state": job_state(record)}))
        _work.on_start(lambda name: post({"kind": "task", "name": name}))


def step(budget_ms: float = 50.0, background: bool = True) -> dict:
    """Run queued work for about `budget_ms` (a task is never cut short); the
    worker calls this when it has no request waiting, with `background` False
    while requests have not been quiet long enough for work nobody is waiting
    on. `{ran, pending, background, names, wait_ms}`: the tasks still queued
    that are not background and those that are, `names` the tasks that ran
    (what the page records when a step was long), and `wait_ms` how long
    until a queued task that waits out a delay may run (nothing ran)."""
    from plexora.server.utils import source_writes

    names = []
    # A job's write into a picked file goes to the page as the step ends
    # (server/utils/source_writes.py), as a request's does.
    with source_writes.batch():
        ran = _work.run_pending(budget_s=max(0.0, budget_ms) / 1000.0, ran=names,
                                background=False)
        if background and not ran:
            # At most ONE background task a step, and only in a step that ran
            # nothing else: the worker then looks for quiet again before the
            # next (pythonWorker.js `step`). Several in one step -- the budget
            # is only checked between tasks -- let a warm-up's second task
            # start after a request had arrived, and the request waited for it.
            ran = _work.run_pending(budget_s=0.0, ran=names, background=True)
    # Queued work that waits out a delay (`_work.spawn(after_s=...)`): how long
    # the worker may sleep before it can run (0 when something can run now).
    wait_ms = 0 if ran else round(_work.due_in() * 1000)
    return {"ran": ran, "pending": _work.pending(background=False),
            "background": _work.pending(background=True), "names": names,
            "wait_ms": wait_ms}
