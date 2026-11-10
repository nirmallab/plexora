"""Auto-contrast's calibration on the website gives way between channels.

`display_contrast.compute` calibrates a project's channels in the background:
a thread on the desktop. The website has one Python thread (`_work`'s
cooperative mode), where the job was one task -- 7 s on LSP11385's 35
channels, the page, `/health` and every request waiting behind it. There it
is `STEP_CHANNELS` channels a task now, each a background task, and the
record at the end is the one the desktop's single call makes.
"""

import pytest

from plexora import _work
from tests.autogate_fixtures import make_gating_project

MARKERS = ("CD3", "CD8", "CD20", "CD4", "FOXP3")


def _forget(project):
    from plexora.agent.evidence import calibration

    calibration.save(project, {"version": -1})       # another rule's: not loaded
    assert calibration.load(project) is None


def _channels(record):
    return {name: {k: v for k, v in entry.items()} for name, entry in record["channels"].items()}


def test_the_websites_calibration_is_a_few_channels_a_step(tmp_path, monkeypatch):
    from plexora.agent import AgentSession
    from plexora.agent.evidence import calibration, display_contrast

    make_gating_project(tmp_path, markers=MARKERS)
    session = AgentSession()
    whole, _ = calibration.calibrate(session, "gsynth", None, force=True)
    _forget("gsynth")

    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    monkeypatch.setattr(display_contrast, "STEP_CHANNELS", 2)
    asked = []
    real = calibration.calibrate
    monkeypatch.setattr(calibration, "calibrate",
                        lambda s, p, names=None, **kw: asked.append(list(names or [])) or real(s, p, names, **kw))
    _work._reset_for_tests()

    started = display_contrast.compute("gsynth", None, session)
    assert started["state"] == "computing"
    # Queued, not run here: a step at a time, the page's requests between.
    assert _work.pending(background=False) == 1 and not asked
    ran = []
    _work.run_pending(ran=ran)

    names = list(whole["channels"])
    assert all(len(step) <= 2 for step in asked) and sorted(sum(asked, [])) == sorted(names)
    # A step that reads the table first, then the channels' steps, and one
    # that finds nothing left (and starts the calibration ahead, which finds
    # every channel calibrated already).
    assert ran.count("display-contrast:gsynth") == len(asked) + 2
    assert set(ran) <= {"display-contrast:gsynth", "display-contrast-ahead:gsynth"}
    assert display_contrast.describe("gsynth", session)["state"] == "ready"
    stepped = calibration.load("gsynth")
    assert _channels(stepped) == _channels(whole)


def test_channels_asked_for_meanwhile_are_calibrated_after(tmp_path, monkeypatch):
    from plexora.agent import AgentSession
    from plexora.agent.evidence import calibration, display_contrast

    make_gating_project(tmp_path, markers=MARKERS)
    session = AgentSession()
    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    monkeypatch.setattr(display_contrast, "STEP_CHANNELS", 1)
    _work._reset_for_tests()

    display_contrast.compute("gsynth", ["CD3"], session)
    _work.run_pending(budget_s=0)                     # the table's step
    _work.run_pending(budget_s=0)                     # CD3's
    assert display_contrast.describe("gsynth", session)["state"] == "computing"
    display_contrast.compute("gsynth", ["CD20", "CD8"], session)
    _work.run_pending()

    assert {"CD3", "CD8", "CD20"} <= set(calibration.load("gsynth")["channels"])
    assert display_contrast.describe("gsynth", session)["state"] == "ready"


@pytest.mark.parametrize("state", ["computing", "failed"])
def test_asking_while_a_job_runs_or_has_just_failed_answers(tmp_path, monkeypatch, state):
    """`compute` answered with `describe` while holding the lock `describe`
    takes: a second ask while a job ran waited on itself for ever -- on the
    website the only Python thread, and the whole site froze (a channel
    switched on while a 40-channel slide's first calibration ran)."""
    import threading

    from plexora.agent import AgentSession
    from plexora.agent.evidence import display_contrast

    make_gating_project(tmp_path, markers=MARKERS)
    session = AgentSession()
    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    _work._reset_for_tests()
    display_contrast.compute("gsynth", ["CD3"], session)      # queued, computing
    if state == "failed":
        import time

        display_contrast._JOBS["gsynth"] = {"state": "failed", "finished": time.time(), "error": "x"}

    answer = {}
    asking = threading.Thread(daemon=True, target=lambda: answer.update(
        display_contrast.compute("gsynth", ["CD8"], session)))
    asking.start()
    asking.join(20)

    assert not asking.is_alive(), "compute waited on its own lock"
    if state == "computing":
        assert answer["state"] == "computing"
        assert display_contrast._JOBS["gsynth"]["queued"] == ["CD8"]
    else:
        assert display_contrast._JOBS["gsynth"]["state"] == "failed"   # not retried yet


def test_the_desktop_calibrates_on_a_thread_as_before(tmp_path, monkeypatch):
    from plexora.agent import AgentSession
    from plexora.agent.evidence import display_contrast

    make_gating_project(tmp_path, markers=MARKERS)
    monkeypatch.delenv("PLEXORA_WORK", raising=False)
    spawned = []
    monkeypatch.setattr(_work, "spawn", lambda target, *a, **kw: spawned.append((target, kw)))

    display_contrast.compute("gsynth", None, AgentSession())

    [(target, options)] = spawned
    assert target.__name__ == "run" and not options.get("background")


@pytest.fixture(autouse=True)
def _jobs(monkeypatch):
    import threading

    from plexora.agent.evidence import display_contrast

    monkeypatch.setattr(display_contrast, "_AHEAD", set())

    # A lock of each test's own: one left held by a deadlocked ask fails its
    # test, not every later one.
    monkeypatch.setattr(display_contrast, "_LOCK", threading.Lock())
    display_contrast._JOBS.clear()
    yield
    display_contrast._JOBS.clear()
    _work._reset_for_tests()


def test_the_website_calibrates_the_other_channels_ahead_of_their_click(tmp_path, monkeypatch):
    """Once what the viewer asked for is done, the channels nobody asked
    about yet are calibrated in background tasks, a few a task: a channel
    switched on later finds its record there."""
    from plexora.agent import AgentSession
    from plexora.agent.evidence import calibration, display_contrast

    make_gating_project(tmp_path, markers=MARKERS)
    session = AgentSession()
    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    monkeypatch.setattr(display_contrast, "STEP_CHANNELS", 2)
    _work._reset_for_tests()

    display_contrast.compute("gsynth", ["CD3"], session)
    _work.run_pending(background=False)               # what was asked, only
    assert set(calibration.load("gsynth")["channels"]) < set(display_contrast._channel_names(session, "gsynth"))
    assert _work.pending(background=True) == 1        # ahead, waiting for quiet
    left = set(display_contrast._channel_names(session, "gsynth")) - set(calibration.load("gsynth")["channels"])
    ran = []
    _work.run_pending(ran=ran)
    assert set(calibration.load("gsynth")["channels"]) == set(display_contrast._channel_names(session, "gsynth"))
    assert set(ran) == {"display-contrast-ahead:gsynth"}
    # One channel a task (AHEAD_CHANNELS): a click on another channel waits
    # for one channel's work at most, never four.
    assert len(ran) == len(left) > 1
