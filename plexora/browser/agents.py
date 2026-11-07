"""spatialagents on the website: its threads and sleeps through `plexora._work`.

Plexora AI's conversations and its in-app runs are spatialagents' (shared with
SCIMAP Pro): a chat turn runs on a thread its service starts
(`conversations.ChatService.send`), sub-agents and lanes on a pool
(`orchestrator.Scheduler`), and a turn waiting for the user -- an approval, a
pause -- polls with `time.sleep`. The website's worker has no threads (a
`Thread.start()` raises "can't start new thread") and a `time.sleep` there
blocks the worker for real, so the approval the turn waits for could never
arrive. spatialagents calls `threading` and `time` directly, with no hook a
product can set, so this module gives the modules that do a `threading` whose
`Thread` is `_work.spawn`'s queued work and a `time` whose `sleep` is
`_work.pause` (a sleep that serves the page meanwhile), and the scheduler a
pool that is `_work.executor`. Everything else in those modules' `threading`
and `time` is the real module's.

Installed by the website's boot only (`plexora/browser/boot.py`); on threads
(`PLEXORA_WORK` unset) `_work.spawn` and `_work.executor` are real threads and
pools and `_work.pause` a sleep, so installing it under CPython (the tests do)
changes no behaviour. It goes when spatialagents takes a spawn and a sleep of
the product's: tests/test_browser_agents.py fails as soon as the installed
spatialagents has one (`spatialagents.runtime`), to say so.

One thing it cannot give: a conversation that keeps its prompt cache warm
(`ChatProfile.keep_warm`) runs a keeper loop forever, which a cooperative
worker cannot; Plexora's profile does not ask for it, and the keeper is not
started here.
"""

from __future__ import annotations

import importlib
import importlib.util
import threading as _threading
import time as _time
import types

from plexora import _work

#: The modules that start threads or sleep, and what each is given.
PATCHED = {
    "spatialagents.conversations": ("threading",),
    "spatialagents.runner": ("threading", "time"),
    "spatialagents.approvals": ("time",),
    "spatialagents.orchestrator": ("threading", "time", "ThreadPoolExecutor"),
    "spatialagents.plans": ("threading", "time"),
}

#: The keeper loop of `ChatProfile.keep_warm`: never started here (above).
_UNSTARTED = ("spatialagents-keep-warm",)


class Thread:
    """`threading.Thread` as queued work (`_work.spawn`): `start`, `join`,
    `is_alive`, `name`, `daemon`, `ident` mean what they mean there. On threads
    `_work.spawn` starts a real thread, so this is one."""

    def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, *, daemon=None):
        self._target = target
        self._args = tuple(args or ())
        self._kwargs = dict(kwargs or {})
        self.name = name or "spatialagents-thread"
        self.daemon = True if daemon is None else bool(daemon)
        self._handle = None
        self.ident = None

    def run(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def start(self):
        if self._handle is not None:
            raise RuntimeError("threads can only be started once")
        if self.name in _UNSTARTED:
            self._handle = _Never()
            return
        self._handle = _work.spawn(self.run, name=self.name, daemon=self.daemon)

    def is_alive(self) -> bool:
        return self._handle is not None and self._handle.is_alive()

    def join(self, timeout=None):
        if self._handle is not None:
            self._handle.join(timeout)


class _Never:
    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def _pool(max_workers=None, thread_name_prefix="", **kwargs):
    return _work.executor(max_workers=max_workers, thread_name_prefix=thread_name_prefix, **kwargs)


def _stand_in(real: types.ModuleType, **overrides) -> types.ModuleType:
    """`real` with some names replaced: what a module sees as `threading` or `time`."""
    module = types.ModuleType(real.__name__, getattr(real, "__doc__", None))
    module.__dict__.update({name: value for name, value in vars(real).items() if not name.startswith("__")})
    module.__dict__.update(overrides)
    module.__plexora_stand_in__ = True
    return module


THREADING = _stand_in(_threading, Thread=Thread)
TIME = _stand_in(_time, sleep=_work.pause)


def hooked() -> bool:
    """Whether the installed spatialagents takes a product's spawn and sleep
    itself (then nothing here is needed)."""
    return importlib.util.find_spec("spatialagents.runtime") is not None


def install() -> dict:
    """Give spatialagents' modules the stand-ins; returns `{module: names}`
    for what was replaced (empty when spatialagents has its own hook)."""
    if hooked():
        from spatialagents import runtime

        runtime.configure(spawn=_work.spawn, sleep=_work.pause, executor=_pool)
        return {}
    replaced = {}
    for name, attributes in PATCHED.items():
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        done = []
        for attribute in attributes:
            if not hasattr(module, attribute):
                continue
            value = {"threading": THREADING, "time": TIME, "ThreadPoolExecutor": _pool}[attribute]
            setattr(module, attribute, value)
            done.append(attribute)
        replaced[name] = done
    return replaced
