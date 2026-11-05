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


# -- following the tab: a dataset's images gated side by side ------------------


class Tab:
    """What a followed tab says it shows (`where`)."""

    def __init__(self, project, status="live"):
        self.now = {"project": project, "status": status}
        self.asked = 0

    def __call__(self):
        self.asked += 1
        return dict(self.now)


def test_following_the_tab_sends_only_the_look_for_the_image_it_shows():
    log, tab = [], Tab("img_a")
    presenter = mirror.Presenter("s_follow")
    presenter.offer(_show(log, "B1"), subject="img_b::CD3", key="pk_1", project="img_b",
                    where=tab)
    presenter.offer(_show(log, "A1"), subject="img_a::CD3", key="pk_2", project="img_a",
                    where=tab)
    # Settled with B's look still waiting: it is for an image the tab does not show.
    assert presenter.idle(5) and log == ["A1"]
    # The user switches image; the tab attaches again on B.
    tab.now = {"project": "img_b", "status": "live"}
    presenter.poke()
    assert presenter.idle(5) and log == ["A1", "B1"]


def test_following_the_tab_keeps_the_newest_look_of_each_image():
    log, tab = [], Tab("img_a")
    presenter = mirror.Presenter("s_follow_newest")
    for n in (1, 2, 3):
        presenter.offer(_show(log, f"B{n}"), subject="img_b::CD3", key=f"pk_b{n}",
                        project="img_b", where=tab)
    presenter.offer(_show(log, "A1"), subject="img_a::CD8", key="pk_a1", project="img_a",
                    where=tab)
    assert presenter.idle(5) and log == ["A1"]
    # A's newer look does not replace B's: one slot per image.
    presenter.offer(_show(log, "A2"), subject="img_a::CD8", key="pk_a2", project="img_a",
                    where=tab)
    assert presenter.idle(5) and log == ["A1", "A2"]
    tab.now["project"] = "img_b"
    presenter.poke()
    assert presenter.idle(5) and log == ["A1", "A2", "B3"]
    assert presenter.take_dropped() == 2
    # A look offered for no one image (a set-up question on several) goes to any.
    presenter.offer(_show(log, "setup"), wait=False, key="pk_s", project=None, where=tab)
    assert presenter.idle(5) and log[-1] == "setup"


def test_a_navigating_tab_is_waited_for_not_sent_to():
    log, tab = [], Tab("img_a", status="navigating")
    presenter = mirror.Presenter("s_follow_nav")
    presenter.offer(_show(log, "A1"), subject="img_a::CD3", key="pk_1", project="img_a",
                    where=tab)
    assert presenter.idle(5) and log == []
    time.sleep(2 * mirror.FOLLOW_POLL_S)
    # Asked again while it waits, never sent a look.
    assert log == [] and tab.asked >= 2
    tab.now = {"project": "img_a", "status": "live"}
    presenter.poke()
    assert presenter.idle(5) and log == ["A1"]


def test_a_look_its_script_skipped_waits_again_for_its_image():
    """The tab moved between the presenter asking and the script running: the
    script skips (`SKIPPED`), and the look plays when the tab is back."""
    log, tab = [], Tab("img_a")
    tries = {"n": 0}

    def look():
        tries["n"] += 1
        if tries["n"] == 1:
            tab.now["project"] = "img_b"       # the user switched meanwhile
            return mirror.SKIPPED
        log.append("A1")
        return 0.0

    presenter = mirror.Presenter("s_follow_skip")
    presenter.offer(look, subject="img_a::CD3", key="pk_1", project="img_a", where=tab)
    assert presenter.idle(5) and log == [] and tries["n"] == 1
    tab.now["project"] = "img_a"
    presenter.poke()
    assert presenter.idle(5) and log == ["A1"] and tries["n"] == 2
    # Shown now: the same packet is not shown twice.
    assert presenter.offer(_show(log, "A again"), subject="img_a::CD3", key="pk_1",
                           project="img_a", where=tab) == "queued"
    assert presenter.idle(5) and log == ["A1"]


def test_without_a_tab_to_follow_the_image_named_changes_nothing():
    """A project's session: one slot, latest wins, whatever image a look names."""
    log, gate = [], threading.Event()
    presenter = mirror.Presenter("s_one_slot")
    presenter.offer(_show(log, "A1", gate=gate), subject="p::A", key="pk_1", project="img_a")
    time.sleep(0.1)
    presenter.offer(_show(log, "B1"), subject="p::B", key="pk_2", project="img_b")
    presenter.offer(_show(log, "C1"), subject="p::C", key="pk_3", project="img_a")
    gate.set()
    assert presenter.idle(5) and log == ["A1", "C1"]


def test_a_followed_tab_is_never_sent_to_another_image():
    from plexora.plugins.gating.server.autogate import mirror_script

    look = {"kind": "t4_candidates", "units": [{"project": "img_b", "marker": "CD3"}],
            "evidence": {"candidate": {"low": 5.0}}}
    assert mirror_script.script_for(look, None, None, current_project="img_a", follow=True) == []
    shown = mirror_script.script_for(look, None, None, current_project="img_b", follow=True)
    assert shown and "open_project" not in [c["type"] for c in shown]
    # A session on one image takes the tab there, as before.
    assert mirror_script.script_for(look, None, None,
                                    current_project="img_a")[0]["type"] == "open_project"
    setup = {"kind": "expression_setup", "units": [],
             "evidence": {"projects": ["img_a", "img_b"]}}
    assert [c["type"] for c in mirror_script.setup_script(
        setup, current_project="img_b", follow=True)] == ["open_tool"]
    assert mirror_script.setup_script(setup, current_project="img_c", follow=True) == []
    check = {"kind": "display_setup", "evidence": {"project": "img_b"}}
    assert mirror_script.display_packet_script(check, current_project="img_a",
                                               follow=True) == []
    # Between pages the tab is waited for; on another image it is skipped.
    assert mirror_script._follow_skip({"status": "navigating", "project": "img_b"},
                                      ["img_b"])["reason"] == "navigating"
    assert mirror_script._follow_skip({"status": "live", "project": "img_a"},
                                      ["img_b"])["reason"] == "other_image"
    assert mirror_script._follow_skip({"status": "live", "project": "img_b"}, ["img_b"]) is None


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
