"""`mirror.run_script` stops where the user detached the viewer.

"Continue in background" flips the session's control while a packet's
script may be half sent: the rest of it is not sent, and the pause before
the next command is not waited out.
"""

from plexora.agent.sessions import mirror

SCRIPT = [{"type": "set_channels"}, {"type": "fit_region"}, {"type": "preview_gate"}]


def test_a_script_stops_at_the_command_after_a_detach():
    sent, slept = [], []
    attached = {"on": True}

    def send(type, arguments):
        sent.append(type)
        attached["on"] = False          # the user detaches while this command runs

    result = mirror.run_script(send, SCRIPT, delay_ms=100, sleep=slept.append,
                               keep_going=lambda: attached["on"])
    assert sent == ["set_channels"]
    assert result["sent"] == 1 and result["aborted"] is True
    assert result["status"] == "ok"     # the vocabulary the panel and the agent read
    assert slept == []


def test_a_detached_viewer_is_sent_nothing():
    sent = []
    result = mirror.run_script(lambda t, a: sent.append(t), SCRIPT, delay_ms=0,
                               keep_going=lambda: False)
    assert sent == [] and result["aborted"] is True and result["sent"] == 0


def test_without_keep_going_the_whole_script_is_sent():
    sent = []
    result = mirror.run_script(lambda t, a: sent.append(t), SCRIPT, delay_ms=0)
    assert sent == [c["type"] for c in SCRIPT] and "aborted" not in result


# -- the presenter: the mirror off the session's path -------------------------

import threading
import time


def _show(log, name, *, gate=None, hold=0.0, seconds=0.0):
    """A job that records `name` when sent, optionally held on `gate`."""
    def job():
        if gate is not None:
            gate.wait(5)
        log.append(name)
        time.sleep(seconds)
        return hold
    return job


def test_an_offer_returns_while_the_tab_is_still_being_shown_the_last():
    log, gate = [], threading.Event()
    presenter = mirror.Presenter("s_async")
    started = time.monotonic()
    assert presenter.offer(_show(log, "A", gate=gate), subject="p::A", key="pk_1") == "queued"
    assert time.monotonic() - started < 0.5 and log == []
    gate.set()
    assert presenter.idle(5) and log == ["A"]


def test_looks_faster_than_the_tab_leave_no_backlog():
    """Five looks while one is shown: at most two shows, the newest last --
    and a marker never shown is kept over a second look at one that was."""
    log, gate = [], threading.Event()
    presenter = mirror.Presenter("s_latest")
    presenter.offer(_show(log, "A1", gate=gate), subject="p::A", key="pk_1")
    time.sleep(0.1)                      # A1 is being sent
    for n in range(2, 5):
        presenter.offer(_show(log, f"A{n}"), subject="p::A", key=f"pk_{n}")
    presenter.offer(_show(log, "B1"), subject="p::B", key="pk_5")
    # A re-look of A (shown) does not replace B (never shown) waiting.
    assert presenter.offer(_show(log, "A5"), subject="p::A", key="pk_6") == "dropped"
    gate.set()
    assert presenter.idle(5)
    assert log == ["A1", "B1"]
    assert presenter.take_dropped() == 4


def test_a_look_is_kept_its_time_before_the_next_replaces_it():
    log = []
    presenter = mirror.Presenter("s_hold")
    presenter.offer(_show(log, "A", hold=0.6), subject="p::A", key="pk_1")
    time.sleep(0.1)
    started = time.monotonic()
    presenter.offer(_show(log, "B"), subject="p::B", key="pk_2")
    assert presenter.idle(5) and log == ["A", "B"]
    assert time.monotonic() - started >= 0.4
    # A set-up question (wait=False) does not wait the show out.
    presenter.offer(_show(log, "C", hold=5.0), subject="p::C", key="pk_3")
    presenter.idle(5)
    started = time.monotonic()
    presenter.offer(_show(log, "setup"), wait=False, key="pk_4")
    assert presenter.idle(5) and time.monotonic() - started < 1.0


def test_a_kept_show_is_never_replaced_and_goes_before_a_look():
    log, gate = [], threading.Event()
    presenter = mirror.Presenter("s_kept")
    presenter.offer(_show(log, "A", gate=gate), subject="p::A", key="pk_1")
    time.sleep(0.1)
    presenter.offer(_show(log, "B"), subject="p::B", key="pk_2")
    presenter.offer(_show(log, "contrast"), keep=True)
    gate.set()
    assert presenter.idle(5) and log == ["A", "contrast", "B"]


def test_the_same_packet_is_not_shown_twice():
    log = []
    presenter = mirror.Presenter("s_dedupe")
    presenter.offer(_show(log, "A"), subject="p::A", key="pk_1")
    presenter.idle(5)
    presenter.offer(_show(log, "A again"), subject="p::A", key="pk_1")
    assert presenter.idle(5) and log == ["A"]


def test_close_drops_what_waits_and_sends_the_teardown_last():
    log, gate = [], threading.Event()
    presenter = mirror.presenter("s_close")
    presenter.offer(_show(log, "A", gate=gate), subject="p::A", key="pk_1")
    time.sleep(0.1)
    presenter.offer(_show(log, "B"), subject="p::B", key="pk_2")
    presenter.close(_show(log, "teardown"))
    # The script under way is told to stop at its next command.
    assert mirror.closing("s_close")
    assert mirror.attached(type("S", (), {"control": lambda self, sid: {}})(), "s_close")() \
        is False
    gate.set()
    assert mirror.wait_idle("s_close", 5) and log == ["A", "teardown"]
    # Nothing offered after the session ended is sent.
    assert mirror.presenter("s_close").offer(_show(log, "late")) == "dropped"
    assert mirror.wait_idle("s_close", 1) and log == ["A", "teardown"]


def test_a_failing_show_does_not_stop_the_presenter():
    log = []
    presenter = mirror.Presenter("s_fail")

    def broken():
        raise RuntimeError("tab gone")

    presenter.offer(broken, subject="p::A", key="pk_1")
    presenter.offer(_show(log, "B"), subject="p::B", key="pk_2", wait=False)
    assert presenter.idle(5) and log == ["B"]


def test_background_reads_step_aside_while_a_packet_is_drawn():
    """`engine.yield_to_foreground`: a classifier's pass waits between reads
    while a packet is drawn (bounded), never on a thread that is drawing."""
    from plexora.agent.sessions import engine

    released = threading.Event()
    waited = {}

    def background():
        started = time.monotonic()
        engine.yield_to_foreground(timeout=5)
        waited["s"] = time.monotonic() - started

    with engine.foreground():
        thread = threading.Thread(target=background)
        thread.start()
        time.sleep(0.3)
        assert thread.is_alive()           # held while the packet is drawn
        started = time.monotonic()
        engine.yield_to_foreground(timeout=5)   # the drawing thread never waits
        assert time.monotonic() - started < 0.1
        released.set()
    thread.join(5)
    assert 0.25 <= waited["s"] < 2
    started = time.monotonic()
    engine.yield_to_foreground(timeout=5)
    assert time.monotonic() - started < 0.1
