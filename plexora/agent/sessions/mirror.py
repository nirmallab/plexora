"""Showing an open viewer what a session is looking at -- best effort, always.

A mirrored session sends the open tab a short script of viewer commands per
packet (switch image, set the calibrated channels, fly to the cells, preview
the candidate gate on the slider, highlight the cells shown). Mirroring must
never be the reason a session fails: every command has a short timeout, a
failed command marks the mirror `degraded` and the script goes on, and a
viewer that has gone away turns mirroring `off` for the session -- packets
keep coming, headless.

The user may also detach the viewer while the session runs ("Continue in
background"): the session's control then says `viewer_detached`, and a
script already under way stops at its next command (`keep_going`).

Mirroring is off the session's path (`Presenter`): a packet is offered to the
session's presenter and the driver gets it back at once, while a thread of
its own sends the script when the tab is free. Looks arriving faster than the
tab can play them are not queued up behind each other: the newest waits, the
ones it replaces are dropped (a marker never shown before is kept over a
second look at one that was), and a look playing is given `MIN_SHOW_S` before
the next replaces it. So the viewer shows one coherent marker at a time,
never a backlog after the session has decided -- and how far the session has
got (`progress`, the `issued` and `answered` events) never waits on it.

A session gating several images at once follows the tab (`where`): the tab
is never sent to another image. The newest look waits per image, only the
look for the image the tab shows is sent, and when the user switches image
(the tab navigates, then attaches again: `poke`) the new image's look plays.
"""

from __future__ import annotations

import atexit
import collections
import itertools
import threading
import time

#: Milliseconds between two commands of a script (a session may change it).
DEFAULT_DELAY_MS = 600

#: Commands after which the viewer is given longer to settle before the next.
DWELL_AFTER = ("fit_region", "focus_cell", "preview_gate", "open_project")


def run_script(send, script, *, delay_ms=DEFAULT_DELAY_MS, dwell_factor=2.0,
               sleep=time.sleep, keep_going=None) -> dict:
    """Send `script` = [{type, arguments}] through `send(type, arguments)`.

    Returns {status: ok|degraded|off, sent, errors}, plus `aborted: true`
    when `keep_going()` turned false before a command (the user detached the
    viewer). `send` raises an AgentError for a refused or unanswered command.
    """
    from plexora.agent.errors import AgentError

    sent, errors = 0, []
    status = "ok"
    for index, command in enumerate(script):
        if keep_going is not None and not keep_going():
            return {"status": status, "sent": sent, "errors": errors[:10], "aborted": True}
        try:
            send(command["type"], command.get("arguments") or {})
            sent += 1
        except AgentError as exc:
            errors.append({"command": command["type"], "code": exc.code,
                           "message": exc.message})
            if exc.code == "viewer_not_available":
                return {"status": "off", "sent": sent, "errors": errors}
            status = "degraded"
        except Exception as exc:  # a broken tab must not break the session
            errors.append({"command": command["type"], "code": "internal_error",
                           "message": str(exc)})
            status = "degraded"
        if index < len(script) - 1 and keep_going is not None and not keep_going():
            return {"status": status, "sent": sent, "errors": errors[:10], "aborted": True}
        if index < len(script) - 1 and delay_ms:
            pause = delay_ms / 1000.0
            if command["type"] in DWELL_AFTER:
                pause *= dwell_factor
            sleep(pause)
    return {"status": status, "sent": sent, "errors": errors[:10]}


#: Seconds a command may take to acknowledge (switching image or HD mode
#: rebuilds every tile and is acknowledged when that finishes).
COMMAND_TIMEOUT_S = {"open_project": 20.0, "set_hd_mode": 20.0, "show_evidence": 10.0,
                     "restore_viewer": 20.0}
DEFAULT_TIMEOUT_S = 5.0
#: The first command of a script: long enough for a background tab to wake.
WAKE_TIMEOUT_S = 10.0


def open_view(call, view_id):
    """(control, view, state) of the viewer a session mirrors into, or a
    result dict {status: off, ...} when there is none. `state` is the tab's
    `get_state` (None when it did not answer), which also wakes a throttled
    background tab."""
    from plexora.agent import viewer
    from plexora.agent.errors import AgentError

    try:
        control = viewer.require(call.link)
    except AgentError as exc:
        return {"status": "off", "sent": 0,
                "errors": [{"code": exc.code, "message": exc.message}]}
    try:
        view = viewer.resolve_view(control, view_id)
    except AgentError as exc:
        return {"status": "off", "sent": 0,
                "errors": [{"code": exc.code, "message": exc.message}]}
    state = None
    try:
        state = (control.send(view["view_id"], "get_state", {},
                              timeout=WAKE_TIMEOUT_S) or {}).get("result")
    except AgentError as exc:
        if exc.code == "viewer_not_available":
            return {"status": "off", "sent": 0, "view_id": view["view_id"],
                    "errors": [{"code": exc.code, "message": exc.message}]}
    return control, view, state


def send_script(control, view, script, *, delay_ms=DEFAULT_DELAY_MS, timeouts=None,
                keep_going=None):
    """Run `script` against an opened view (`open_view`); the result carries
    the view id."""
    timeouts = {**COMMAND_TIMEOUT_S, **(timeouts or {})}

    def send(type, arguments):
        return control.send(view["view_id"], type, arguments,
                            timeout=timeouts.get(type, DEFAULT_TIMEOUT_S))

    result = run_script(send, script, delay_ms=delay_ms, keep_going=keep_going)
    result["view_id"] = view["view_id"]
    return result


def attached(store, session_id):
    """A `keep_going` for `run_script`: false once the user has detached the
    session's viewer, or the session has ended (its presenter is closing)."""
    return lambda: not closing(session_id) \
        and not store.control(session_id).get("viewer_detached")


# -- the presenter: mirroring off the session's path -------------------------

#: Seconds a look the tab is playing is kept before a newer one replaces it,
#: at least; a tab that says how long its look's moves take (gating's
#: `preview_gate` answer, `show.ms`) keeps it that long, up to MAX_SHOW_S.
#: Held for 8 s flat, a look of 10-25 s was cut half way and the next marker's
#: began -- the abrupt mirror of 2026-10-30. Paced sessions only: one with
#: `mirror_delay_ms: 0` waits for nothing.
MIN_SHOW_S = 8.0
MAX_SHOW_S = 30.0
#: A presenter with nothing to send ends its thread after this many seconds;
#: the next offer starts another.
IDLE_S = 30.0
#: How long the process waits, on exit, for teardowns still being sent.
EXIT_DRAIN_S = 5.0
#: A presenter that follows the tab asks it again this often which image it
#: shows, while every look waiting is for another image or the tab is
#: navigating -- and waits this long before trying a look its job skipped.
FOLLOW_POLL_S = 1.0

#: What a job returns when it did not show its look (the tab had moved to
#: another image, or was navigating): the look waits again for its image.
SKIPPED = object()
#: `where` failed: the tab's image is not known, so no look goes yet.
_UNKNOWN = object()
#: The slot of a `keep=True` item, which is never put back.
_KEPT = object()


class Presenter:
    """One session's mirror, sent from a thread of its own.

    `offer(job, ...)` returns at once. `job()` sends one script and records
    how it went; it returns the seconds the show it started should be kept
    before a newer look replaces it, or `SKIPPED`. Two kinds of item wait:

    - looks (`subject` a marker, or a set-up packet with none): one slot,
      latest wins. A newer look replaces the one waiting, unless the waiting
      one is a marker this session has not shown yet and the newer is not
      (the viewer sees each marker, not the fifth look at one);
      `wait=False` (a set-up prompt, the tab asking to watch again) does
      not wait out the show playing.
    - `keep=True` items (a contrast show, the teardown): sent in order, never
      replaced, before any look.

    A presenter offered a `where` follows the tab: `where()` says what the
    tab shows ({project, status}, None once it is gone), there is a slot per
    image (`project`; None for a look about no one image, which any image
    shows), and only the look for the tab's image is sent -- none while it
    is `navigating`. The others wait in their slots (`poke` looks again at
    once); a look its job skipped goes back to its slot unless a newer one
    took it.

    `close(job)` ends it: what waits is dropped, the script being sent stops
    at its next command (`closing`, read by `attached`), and `job` -- the
    teardown -- is the last thing sent."""

    def __init__(self, session_id):
        self.session_id = str(session_id)
        self._cv = threading.Condition()
        #: slot -> (job, subject, wait, key, seq): one slot (None) unless
        #: following the tab, then one per image.
        self._looks = {}
        self._kept = collections.deque()
        self._shown = set()
        self._sending = False
        self._closing = False
        self._thread = None
        self._until = 0.0
        self._until_slot = None
        self._last_key = {}
        self._where = None
        self._seq = itertools.count()
        #: Following the tab: every look waiting is for another image (or
        #: the tab is navigating), so `idle` does not wait for them.
        self._parked = False
        self._retry_at = 0.0
        self.dropped = 0

    def offer(self, job, *, subject=None, wait=True, keep=False, key=None, project=None,
              where=None) -> str:
        """`queued`, or `dropped` (a look kept waiting in its place, or the
        session is ending). A look whose `key` (its packet) is waiting, being
        sent or was the last sent is not offered twice. `where` makes the
        presenter follow the tab, `project` naming the image the look is of."""
        with self._cv:
            if self._closing:
                return "dropped"
            if where is not None:
                self._where = where
            slot = project if self._where is not None else None
            waiting = self._looks.get(slot)
            if key is not None and key in (self._last_key.get(slot), (waiting or (None,) * 4)[3]):
                return "queued"
            if keep:
                self._kept.append((job, subject, False, None, None))
            else:
                if waiting is not None and self._fresh(waiting[1]) \
                        and not self._fresh(subject) and wait:
                    self.dropped += 1
                    return "dropped"
                if waiting is not None:
                    self.dropped += 1
                self._looks[slot] = (job, subject, wait, key, next(self._seq))
            self._parked = False
            self._start()
            self._cv.notify_all()
        return "queued"

    def poke(self):
        """The tab may show another image now (it attached again after
        navigating): the looks waiting are weighed again at once."""
        with self._cv:
            if self._closing:
                return
            self._parked = False
            self._retry_at = 0.0
            if self._looks or self._kept:
                self._start()
            self._cv.notify_all()

    def close(self, job=None):
        with self._cv:
            self.dropped += len(self._kept) + len(self._looks)
            self._looks.clear()
            self._kept.clear()
            self._closing = True
            if job is not None:
                self._kept.append((job, None, False, None, None))
            self._start()
            self._cv.notify_all()

    @property
    def closing(self) -> bool:
        return self._closing

    def take_dropped(self) -> int:
        with self._cv:
            count, self.dropped = self.dropped, 0
            return count

    def idle(self, timeout=10.0) -> bool:
        """Wait until nothing waits (but looks for an image the tab does not
        show) and nothing is being sent."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while (self._looks and not self._parked) or self._kept or self._sending:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
            return True

    def _fresh(self, subject) -> bool:
        return subject is not None and subject not in self._shown

    def _start(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name=f"mirror-{self.session_id}")
            self._thread.start()

    def _ask_tab(self):
        """`where()`, called off the condition (it may be a request to the
        server): an offer never waits on it. `_UNKNOWN` when it failed."""
        where = self._where
        self._cv.release()
        try:
            return where()
        except Exception:
            return _UNKNOWN
        finally:
            self._cv.acquire()

    def _choose(self):
        """(slot, None): the slot whose look goes next; (None, seconds): none
        may go yet -- the tab shows another image, is navigating or did not
        say -- so ask again in `seconds`. One slot unless following the tab."""
        if self._where is None:
            return None, None
        wait = self._retry_at - time.monotonic()
        if wait > 0:
            return None, wait
        tab = self._ask_tab()
        if tab is None:
            # The tab is gone: the newest look goes, and finds no tab (the
            # mirror turns itself off, as it does for a session on one image).
            slots = list(self._looks)
        elif tab is _UNKNOWN or tab.get("status") == "navigating":
            slots = []
        else:
            slots = [s for s in (tab.get("project"), None) if s in self._looks]
        if not slots:
            self._parked = True
            self._cv.notify_all()
            return None, FOLLOW_POLL_S
        self._parked = False
        return max(slots, key=lambda s: self._looks[s][4]), None

    def _next(self):
        """(slot, item): the next item to send (under the condition), waiting
        for one -- and for the show playing to have had its time, unless the
        look waiting says not to -- or None when the presenter is done."""
        idle_until = time.monotonic() + IDLE_S
        while True:
            if self._kept:
                return _KEPT, self._kept.popleft()
            if self._looks and not self._closing:
                slot, poll = self._choose()
                if self._kept or self._closing or slot not in self._looks and poll is None:
                    continue           # changed while the tab was asked
                if poll is None:
                    # Following the tab, a show is held only on the image it
                    # plays on: a tab that moved has left it.
                    hold = self._until - time.monotonic() \
                        if self._where is None or slot == self._until_slot else 0.0
                    if not self._looks[slot][2] or hold <= 0:
                        return slot, self._looks.pop(slot)
                    self._cv.wait(hold)
                    continue
                left = idle_until - time.monotonic()
                if left <= 0:
                    return None
                self._cv.wait(min(poll, left))
                continue
            left = idle_until - time.monotonic()
            if self._closing or left <= 0:
                return None
            self._cv.wait(left)

    def _loop(self):
        while True:
            with self._cv:
                taken = self._next()
                if taken is None:
                    self._thread = None
                    # Looks still waiting are for images the tab does not
                    # show: the next offer or `poke` weighs them again.
                    self._parked = bool(self._looks)
                    self._cv.notify_all()
                    return
                slot, item = taken
                self._sending = True
                job, subject, _wait, key, _seq = item
                before = self._last_key.get(slot)
                if key is not None and slot is not _KEPT:
                    self._last_key[slot] = key
                first = self._fresh(subject)
                if subject is not None:
                    self._shown.add(subject)
            started = time.monotonic()
            hold, shown = 0.0, None
            try:
                shown = job()
                hold = 0.0 if shown is SKIPPED else float(shown or 0.0)
            except Exception:  # a broken tab must not end the presenter
                pass
            with self._cv:
                self._sending = False
                if shown is SKIPPED and slot is not _KEPT and self._where is not None:
                    # Not shown: it waits for its image again, unless a newer
                    # look of that image took its place.
                    if first:
                        self._shown.discard(subject)
                    self._last_key[slot] = before
                    if not self._closing and slot not in self._looks:
                        self._looks[slot] = item
                    self._retry_at = time.monotonic() + FOLLOW_POLL_S
                elif slot is _KEPT or slot == self._until_slot:
                    self._until = max(self._until, started + hold)
                else:
                    # A look of another image: the show held is this one.
                    self._until, self._until_slot = started + hold, slot
                self._cv.notify_all()


_PRESENTERS: dict = {}
_PRESENTERS_GUARD = threading.Lock()


def presenter(session_id) -> Presenter:
    """The session's presenter in this process (made on first use). A closed
    one is kept, so nothing offered after the session ended is sent."""
    with _PRESENTERS_GUARD:
        found = _PRESENTERS.get(str(session_id))
        if found is None:
            found = _PRESENTERS[str(session_id)] = Presenter(session_id)
        return found


def closing(session_id) -> bool:
    """Whether the session's presenter is ending (a script stops)."""
    with _PRESENTERS_GUARD:
        found = _PRESENTERS.get(str(session_id))
    return bool(found is not None and found.closing)


def wait_idle(session_id, timeout=10.0) -> bool:
    """Wait for the session's mirror to have sent everything offered (tests,
    and a driver that wants the tab settled). True when it has."""
    with _PRESENTERS_GUARD:
        found = _PRESENTERS.get(str(session_id))
    return True if found is None else found.idle(timeout)


@atexit.register
def _drain_on_exit():
    """A teardown offered just before the process ends (a CLI run's finish)
    still reaches the tab, within `EXIT_DRAIN_S`."""
    deadline = time.monotonic() + EXIT_DRAIN_S
    with _PRESENTERS_GUARD:
        ending = [p for p in _PRESENTERS.values() if p.closing]
    for found in ending:
        found.idle(max(0.0, deadline - time.monotonic()))
