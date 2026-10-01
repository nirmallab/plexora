"""Multi-agent orchestration: a task graph, a parallel scheduler, and a shared
blackboard.

    graph = TaskGraph()
    ref = graph.add("gate:ref", lambda ctx: run(ctx, "lsp_ref"))
    for name in others:
        graph.add(f"gate:{name}", partial(run, project=name), depends_on=[ref])
    Scheduler(graph, max_parallel=4).run()

- **Dynamic spawning.** A running task may add tasks (`ctx.spawn(...)`); they
  join the same graph, inherit the parent's depth + 1 and are bounded by
  `max_depth`.
- **Parallel execution.** Every task whose dependencies are met and whose
  `ready_when(board)` predicate holds runs, up to `max_parallel` at a time.
  A fan-out of model calls that share a cached prefix should let the first
  call warm the cache before the rest fire: `stagger_first=True` holds the
  others until the first task of the graph reports progress (`ctx.warm()`)
  or finishes.
- **Dependencies.** Hard edges (default) cancel dependents when a task fails;
  soft edges (`soft=[...]`) let them run with the input missing.
- **Communication.** By reference, never by copying transcripts: `facts` (typed,
  versioned key/value), `artifacts` (ids, never bytes) and a `mailbox` per task.
  A task's return value is published as fact `result:<task id>`.

Threads, not asyncio: every task body here calls the synchronous capability
registry, and the gateway client blocks on its stream; the GIL is released on
both. The scheduler is plain data plus ~200 lines, so a different runner could
replace it without touching the engines.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable

TERMINAL = ("done", "failed", "cancelled", "skipped")


class Blackboard:
    """Shared state for one run. Thread-safe; values are small JSON-able data."""

    def __init__(self):
        self._lock = threading.Condition()
        self._facts: dict[str, tuple[Any, int, str | None]] = {}
        self._mail: dict[str, list[dict]] = {}
        self.artifacts: dict[str, dict] = {}
        self.events: list[dict] = []

    def post(self, key: str, value: Any, *, by: str | None = None) -> int:
        with self._lock:
            version = self._facts.get(key, (None, 0, None))[1] + 1
            self._facts[key] = (value, version, by)
            self.events.append({"kind": "fact", "key": key, "version": version, "by": by, "at": time.time()})
            self._lock.notify_all()
            return version

    def read(self, key: str, default=None):
        with self._lock:
            return self._facts.get(key, (default, 0, None))[0]

    def version(self, key: str) -> int:
        with self._lock:
            return self._facts.get(key, (None, 0, None))[1]

    def facts(self, prefix: str = "") -> dict:
        with self._lock:
            return {k: v[0] for k, v in self._facts.items() if k.startswith(prefix)}

    def wait_for(self, key: str, timeout: float | None = None):
        with self._lock:
            self._lock.wait_for(lambda: key in self._facts, timeout=timeout)
            return self._facts.get(key, (None, 0, None))[0]

    def send(self, to: str, body: dict, *, sender: str | None = None) -> None:
        with self._lock:
            self._mail.setdefault(to, []).append({"from": sender, "body": body, "at": time.time()})
            self._lock.notify_all()

    def inbox(self, task_id: str, *, drain: bool = True) -> list[dict]:
        with self._lock:
            mail = self._mail.get(task_id, [])
            if drain:
                self._mail[task_id] = []
            return list(mail)


@dataclass
class Task:
    id: str
    fn: Callable[["TaskContext"], Any]
    depends_on: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    ready_when: Callable[[Blackboard], bool] | None = None
    label: str | None = None
    parent: str | None = None
    depth: int = 0
    retries: int = 0
    state: str = "pending"
    result: Any = None
    error: str | None = None
    attempts: int = 0
    started_at: float | None = None
    finished_at: float | None = None


class TaskGraph:
    def __init__(self):
        self.tasks: dict[str, Task] = {}
        self._lock = threading.RLock()
        self.order: list[str] = []

    def add(self, task_id: str | None, fn, *, depends_on=(), soft=(), ready_when=None, label=None,
            parent=None, depth=0, retries=0) -> str:
        task_id = task_id or f"t_{uuid.uuid4().hex[:8]}"
        with self._lock:
            if task_id in self.tasks:
                raise ValueError(f"task {task_id!r} already exists")
            for dep in (*depends_on, *soft):
                if dep not in self.tasks:
                    raise ValueError(f"task {task_id!r} depends on unknown task {dep!r}")
            self.tasks[task_id] = Task(task_id, fn, list(depends_on), list(soft), ready_when, label or task_id,
                                       parent, depth, retries)
            self.order.append(task_id)
        return task_id

    def ready(self, board: Blackboard) -> list[Task]:
        with self._lock:
            out = []
            for tid in self.order:
                task = self.tasks[tid]
                if task.state != "pending":
                    continue
                deps = [self.tasks[d] for d in task.depends_on]
                if any(d.state in ("failed", "cancelled", "skipped") for d in deps):
                    task.state, task.error = "cancelled", "a hard dependency did not finish"
                    continue
                if not all(d.state == "done" for d in deps):
                    continue
                if not all(self.tasks[s].state in TERMINAL for s in task.soft):
                    continue
                if task.ready_when is not None and not task.ready_when(board):
                    continue
                out.append(task)
            return out

    def pending(self) -> list[Task]:
        with self._lock:
            return [t for t in self.tasks.values() if t.state not in TERMINAL]


class TaskContext:
    def __init__(self, scheduler: "Scheduler", task: Task):
        self._scheduler = scheduler
        self.task = task
        self.board = scheduler.board

    def spawn(self, fn, *, task_id=None, depends_on=(), soft=(), ready_when=None, label=None, retries=0) -> str:
        if self.task.depth + 1 > self._scheduler.max_depth:
            raise RuntimeError(f"spawning below depth {self._scheduler.max_depth} is not allowed")
        new = self._scheduler.graph.add(task_id, fn, depends_on=depends_on, soft=soft, ready_when=ready_when,
                                        label=label, parent=self.task.id, depth=self.task.depth + 1,
                                        retries=retries)
        self._scheduler._wake()
        return new

    def warm(self) -> None:
        """Say the shared prefix is now cached; staggered siblings may start."""
        self._scheduler._warmed.set()

    def cancelled(self) -> bool:
        return self._scheduler.cancel_event.is_set()

    def result_of(self, task_id: str):
        return self.board.read(f"result:{task_id}")


class Scheduler:
    def __init__(self, graph: TaskGraph, *, max_parallel: int = 4, max_depth: int = 2,
                 board: Blackboard | None = None, stagger_first: bool = False, on_event=None,
                 cancel: threading.Event | None = None):
        self.graph = graph
        self.max_parallel = max(1, int(max_parallel))
        self.max_depth = max_depth
        self.board = board or Blackboard()
        self.stagger_first = stagger_first
        self.on_event = on_event or (lambda event: None)
        self.cancel_event = cancel or threading.Event()
        self._warmed = threading.Event()
        self._wakeup = threading.Event()
        self.peak = 0

    def _wake(self):
        self._wakeup.set()

    def _execute(self, task: Task):
        ctx = TaskContext(self, task)
        while True:
            task.attempts += 1
            try:
                return task.fn(ctx)
            except Exception:
                if task.attempts > task.retries or self.cancel_event.is_set():
                    raise

    def run(self) -> dict:
        running: dict[Future, Task] = {}
        launched_any = False
        with ThreadPoolExecutor(max_workers=self.max_parallel, thread_name_prefix="plexora-agent") as pool:
            while True:
                if self.cancel_event.is_set():
                    for task in self.graph.pending():
                        if task.state == "pending":
                            task.state = "cancelled"
                ready = [] if self.cancel_event.is_set() else self.graph.ready(self.board)
                hold = bool(self.stagger_first and launched_any and not self._warmed.is_set() and running)
                for task in ready:
                    if len(running) >= self.max_parallel or hold:
                        break
                    task.state, task.started_at = "running", time.time()
                    self.on_event({"event": "task_started", "task": task.id, "label": task.label,
                                   "parent": task.parent})
                    running[pool.submit(self._execute, task)] = task
                    self.peak = max(self.peak, len(running))
                    if self.stagger_first and not launched_any:
                        launched_any = True
                        hold = not self._warmed.is_set()
                if not running:
                    if not self.graph.ready(self.board):
                        stuck = [t for t in self.graph.pending() if t.state == "pending"]
                        for task in stuck:
                            task.state, task.error = "skipped", "never became ready"
                        break
                    continue
                done, _ = wait(list(running), timeout=0.2, return_when=FIRST_COMPLETED)
                self._wakeup.clear()
                for future in done:
                    task = running.pop(future)
                    task.finished_at = time.time()
                    try:
                        task.result = future.result()
                        task.state = "done"
                        self.board.post(f"result:{task.id}", task.result, by=task.id)
                    except Exception as exc:          # noqa: BLE001 -- recorded on the task
                        task.state, task.error = "failed", f"{type(exc).__name__}: {exc}"
                    if self.stagger_first:
                        self._warmed.set()
                    self.on_event({"event": "task_finished", "task": task.id, "state": task.state,
                                   "error": task.error})
        tasks = self.graph.tasks.values()
        return {"done": sum(t.state == "done" for t in tasks), "failed": sum(t.state == "failed" for t in tasks),
                "cancelled": sum(t.state in ("cancelled", "skipped") for t in tasks), "peak_parallel": self.peak,
                "tasks": {t.id: {"state": t.state, "error": t.error, "parent": t.parent,
                                 "seconds": round((t.finished_at or 0) - (t.started_at or 0), 3)
                                 if t.started_at else None} for t in tasks}}
