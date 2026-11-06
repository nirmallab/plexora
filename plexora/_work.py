"""Where background work runs: on threads, or queued for a runtime that has none.

Every place Plexora starts a thread or a pool goes through here (`spawn`,
`executor`), and every place it waits for one to make progress (`wait`,
`wait_event`, `wait_condition`, `Task.join`). CPython keeps real threads, so
the desktop app, the server and the nodes behave exactly as they did before
this module existed. The website runs Plexora's Python in Pyodide, in a Web
Worker that has no threads at all, and there the same calls queue the work and
run it at the points where a thread would have been given its turn.

The mode is `PLEXORA_WORK`, read on every call so a test can switch it:

- ``threads`` (the default): real threads and real pools.
- ``inline``: the browser's scheduling under CPython, for tests. Spawned work
  is queued and runs to completion where its starter yields to it -- a
  `wait`, a join, an event or condition wait -- or between two outermost
  calls (`boundary`: as the next request or capability call begins), never
  inside the starter's own call. Pools run each call as it is submitted.
- ``cooperative``: the browser. Queued as ``inline``; the worker's runner
  (plexora/browser/cooperative.py) calls `run_pending` between requests, so
  the request that started a job is answered before the job runs.

Never inside the starter's own call, because that is what broke the first
browser proof (docs/internal/bench/browser_node/P9.md): a gating session's bulk
pass, run where it was started, saved the session record, and the starter then
wrote its stale copy over it. A queued task also runs in a fresh
`contextvars.Context`, as a new thread would, so nothing the starter had set
leaks into it.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import contextvars
import logging
import os
import threading
import time

MODES = ("threads", "inline", "cooperative")

_log = logging.getLogger(__name__)


def mode() -> str:
    """``threads``, ``inline`` or ``cooperative``; anything else is ``threads``."""
    value = os.environ.get("PLEXORA_WORK", "threads").strip().lower()
    return value if value in MODES else "threads"


def threaded() -> bool:
    """Whether work runs on real threads (every CPython process by default)."""
    return mode() == "threads"


class Task:
    """Queued work standing in for a `threading.Thread`: `join`, `is_alive`,
    `name` and `daemon` mean what they mean there."""

    def __init__(self, target, args=(), kwargs=None, name=None, daemon=True):
        self._target = target
        self._args = tuple(args)
        self._kwargs = dict(kwargs or {})
        self.name = name or getattr(target, "__name__", "task")
        self.daemon = daemon
        self.state = "queued"

    def run(self):
        if self.state != "queued":  # cancelled while it waited
            return
        self.state = "running"
        try:
            contextvars.Context().run(self._target, *self._args, **self._kwargs)
        except BaseException:  # noqa: BLE001 -- reported as a thread's would be
            _log.exception("background work %r failed", self.name)
        finally:
            self.state = "done"

    def join(self, timeout=None):
        """Run what is queued, this task included. A task that is itself
        running (a join from inside it, or from work it is waiting on)
        cannot finish now and is left to finish where it is."""
        if self.state == "queued":
            run_pending()

    def is_alive(self) -> bool:
        return self.state != "done"

    def start(self):  # already queued by spawn(); here for Thread's interface
        return None

    def cancel(self):
        """Never run it, if it has not started (`threading.Timer.cancel`)."""
        if self.state == "queued":
            self.state = "done"


_queue: list = []
_queue_lock = threading.Lock()
_depth = 0


def spawn(target, *args, name=None, daemon=True, **kwargs):
    """Start `target(*args, **kwargs)` in the background and return its handle:
    a started `threading.Thread`, or a queued `Task`."""
    if threaded():
        thread = threading.Thread(target=target, args=args, kwargs=kwargs,
                                  name=name, daemon=daemon)
        thread.start()
        return thread
    task = Task(target, args, kwargs, name=name, daemon=daemon)
    with _queue_lock:
        _queue.append(task)
    return task


def pending() -> int:
    """How many queued tasks have not started."""
    with _queue_lock:
        return len(_queue)


def run_pending(budget_s: float | None = None) -> int:
    """Run queued tasks in the order they were queued, and those they queue in
    turn, until none is left or `budget_s` has passed; returns how many ran.
    A task is never cut short: the budget is checked between tasks."""
    started = time.monotonic()
    ran = 0
    while True:
        with _queue_lock:
            if not _queue:
                return ran
            task = _queue.pop(0)
        task.run()
        ran += 1
        if budget_s is not None and time.monotonic() - started >= budget_s:
            return ran


@contextlib.contextmanager
def boundary():
    """An outermost call: a request, or a capability call. Work queued by one
    runs after it has answered and before the next begins -- in ``inline``
    mode as that next call starts; in ``cooperative`` mode whenever the
    worker's runner gets to it, sooner if it is idle. On threads it is nothing
    at all."""
    global _depth
    if threaded():
        yield
        return
    if _depth == 0 and mode() == "inline":
        run_pending()
    _depth += 1
    try:
        yield
    finally:
        _depth -= 1


_idle = None


def set_idle(function) -> None:
    """How a cooperative runtime lets time pass without blocking: the browser
    worker installs a sleep that serves other requests meanwhile (a poll that
    waits on the user must not stop the user's answer from arriving)."""
    global _idle
    _idle = function


def wsgi(application):
    """`application` (a WSGI callable) with every request a `boundary`."""
    def call(environ, start_response):
        with boundary():
            return application(environ, start_response)
    return call


def _pause(seconds: float) -> None:
    if _idle is not None and mode() == "cooperative":
        _idle(seconds)
    else:
        time.sleep(seconds)


def wait(seconds: float) -> None:
    """A poll's sleep. Queued, it is where the work being polled for runs, and
    no time passes when some did: the caller looks again at once. With nothing
    queued the poll is waiting on something outside this process -- the user,
    a node -- and the time passes for real."""
    if not threaded() and run_pending():
        return
    _pause(seconds)


def wait_event(event: threading.Event, timeout: float | None = None) -> bool:
    """`event.wait(timeout)`. Queued: run what is pending first, since that is
    usually what sets it. If it is still not set, whatever sets it is outside
    the queue: a real wait under ``inline`` (a test's own thread), and in the
    browser up to `timeout` of serving other requests (a cancel arriving).
    An untimed wait returns rather than wait for what cannot come."""
    if threaded():
        return event.wait(timeout)
    run_pending()
    if event.is_set() or not timeout:
        return event.is_set()
    if mode() == "inline":
        return event.wait(timeout)
    _pause(timeout)
    return event.is_set()


def wait_condition(condition: threading.Condition, timeout: float | None = None) -> bool:
    """`condition.wait(timeout)`, called with the condition held. Queued, the
    pending work runs first with the condition still held -- one thread, and a
    Condition's default lock is reentrant -- and when some ran the answer is
    False at once: the caller re-checks what it was waiting for. When none
    ran, a timed wait waits as `wait_event` does (a show held for its
    seconds); an untimed one returns, since nothing could notify it."""
    if threaded():
        return condition.wait(timeout)
    if run_pending() or not timeout:
        return False
    if mode() == "inline":
        return condition.wait(timeout)
    _pause(timeout)
    return False


class InlineExecutor(concurrent.futures.Executor):
    """A pool with no threads: each call runs as it is submitted, and its
    future is already done when `submit` returns. `map`, `shutdown` and the
    `with` block are `Executor`'s own."""

    def __init__(self, max_workers=None, thread_name_prefix="", **_):
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix

    def submit(self, fn, /, *args, **kwargs):
        future = concurrent.futures.Future()
        future.set_running_or_notify_cancel()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 -- delivered as a pool would
            future.set_exception(exc)
        return future


def executor(max_workers=None, thread_name_prefix="", **kwargs):
    """`ThreadPoolExecutor(max_workers, thread_name_prefix, ...)`, or a pool that
    runs each call as it is submitted."""
    if threaded():
        return concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix, **kwargs)
    return InlineExecutor(max_workers, thread_name_prefix)


def daemon_timer(seconds: float, function, *args, name=None, **kwargs):
    """Call `function` once, `seconds` from now, on a daemon thread; returns a
    handle with `cancel()`. Queued, it waits for the next `run_pending`."""
    if threaded():
        timer = threading.Timer(seconds, function, args=args, kwargs=kwargs)
        timer.daemon = True
        if name:
            timer.name = name
        timer.start()
        return timer
    return spawn(function, *args, name=name, **kwargs)


def _reset_for_tests():
    """Forget queued work (a test that switched modes leaves none behind)."""
    global _depth, _idle
    with _queue_lock:
        _queue.clear()
    _depth = 0
    _idle = None
