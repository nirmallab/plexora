"""On the website a channel's fit waits for the calibration asked for with it.

The viewer asks for a channel's fit and its calibration at the same moment and
keeps the calibrated window (a fit landing after it never replaces it). With
one thread the fits, being requests, went first and the calibration waited
behind them -- four channels switched on as LSP11385 opened: 2.5 s. The route
now asks the fit again a moment later while the job has that channel still to
do. Results are unchanged; only their order is. The desktop runs both at once.
"""

import json
from urllib.parse import urlencode

import numpy as np
import pytest
import tifffile

import plexora
from plexora import _work, datasource
from plexora.agent.evidence import display_contrast
from plexora.browser import bridge
from plexora.server.models import data_model
from tests.helpers import use_data_root

NAME = "fitted"


@pytest.fixture
def project(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    rng = np.random.default_rng(5)
    planes = rng.gamma(2.0, 150.0, size=(3, 96, 80)).astype(np.uint16)
    image = tmp_path / f"{NAME}.tif"
    tifffile.imwrite(image, planes, photometric="minisblack")
    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    datasource.register_image_datasource(name=NAME, image=image, data_dir=data_dir)
    data_model.load_datasource(NAME, reload=True)
    _work._reset_for_tests()
    display_contrast.reset()
    yield
    display_contrast.reset()
    _work._reset_for_tests()


def _channels():
    return [c["fullname"] for c in data_model.config[NAME]["imageData"] if c["name"] != "Area"]


def _fit(name, retryable=True, retried=None):
    headers = [["X-Plexora-Retried", str(retried)]] if retried is not None else []
    return bridge.handle(plexora.app, {"method": "GET", "path": "/get_channel_gmm",
                                       "query": urlencode({"channel": name, "datasource": NAME,
                                                           "provisional": "1"}),
                                       "headers": headers, "retryable": retryable}, "http://127.0.0.1")


def _job(channels, **more):
    display_contrast._JOBS[NAME] = {"state": "computing", "channels": list(channels), **more}


def test_a_fit_asked_with_its_calibration_comes_after_it(project):
    first, second = _channels()[:2]
    _job([first, second])
    later = _fit(first)
    assert later["retry"] == 250
    # The job's first step resolved what it has to do; the first channel is
    # done after the next one.
    display_contrast._still_to_do(NAME, [first, second])
    assert _fit(first)["retry"] == 250
    display_contrast._still_to_do(NAME, [second])
    answered = _fit(first)
    assert "retry" not in answered and answered["status"] == 200
    assert "vmin" in json.loads(answered["body"])
    assert _fit(second)["retry"] == 250


def test_a_fit_in_hand_is_answered_at_once(project):
    name = _channels()[0]
    assert _fit(name)["status"] == 200
    _job([name])
    answered = _fit(name)
    assert "retry" not in answered and answered["status"] == 200


def test_a_job_for_every_channel_does_not_hold_fits_up(project):
    name = _channels()[0]
    _job([])
    assert "retry" not in _fit(name)
    display_contrast._JOBS[NAME]["everything"] = True
    display_contrast._JOBS[NAME]["channels"] = [name]
    assert "retry" not in _fit(name)


def test_a_channel_queued_on_the_running_job_waits_too(project):
    first, second = _channels()[:2]
    _job([first], left=[], queued=[second])
    assert "retry" not in _fit(first)
    assert _fit(second)["retry"] == 250


def test_a_worker_that_does_not_follow_a_retry_is_answered(project):
    name = _channels()[0]
    _job([name])
    answered = _fit(name, retryable=False)
    assert "retry" not in answered and answered["status"] == 200


def test_after_enough_retries_the_fit_is_answered(project):
    from plexora.server.routes import data_routes

    name = _channels()[0]
    _job([name])
    answered = _fit(name, retried=data_routes._MAX_RETRIES)
    assert "retry" not in answered and answered["status"] == 200


def test_the_job_notes_what_it_has_left(project, monkeypatch):
    from plexora.agent.evidence import calibration

    names = _channels()
    done = []
    monkeypatch.setattr(calibration, "prepare", lambda session, project: None)
    monkeypatch.setattr(calibration, "calibrate",
                        lambda session, project, channels: done.extend(channels))
    monkeypatch.setattr(display_contrast, "_known", lambda session, project, wanted: list(wanted))
    monkeypatch.setattr(display_contrast, "STEP_CHANNELS", 2)
    _job(names)
    display_contrast._stepped(None, NAME, names, None)
    assert display_contrast._JOBS[NAME]["left"] == names
    display_contrast._stepped(None, NAME, names, names)
    assert done == names[:2]
    assert display_contrast._JOBS[NAME]["left"] == names[2:]
    # Done, but the viewer has not read it yet: its fit still waits.
    assert display_contrast.calibrating(NAME, names[0])
    assert display_contrast.calibrating(NAME, names[2])


def test_a_fit_waits_until_the_viewer_has_read_its_calibration(project, monkeypatch):
    """Answered the moment the step ended, the four fits ran before the
    viewer's read of the window it was waiting for (2.5 s more)."""
    from plexora.agent.evidence import calibration

    first, second = _channels()[:2]
    display_contrast._UNSEEN[NAME] = {first: display_contrast.time.monotonic()}
    assert _fit(first)["retry"] == 250
    monkeypatch.setattr(calibration, "load", lambda project: {"channels": {first: {}}})
    monkeypatch.setattr(calibration, "load_current", lambda session, project: {"channels": {first: {}}})
    monkeypatch.setattr(calibration, "revision", lambda project: 1)
    monkeypatch.setattr(display_contrast, "_unavailable", lambda session, project: None)
    display_contrast.describe(NAME, session=object())
    assert "retry" not in _fit(first)
    # A viewer that never reads it holds the fit up no longer than its
    # longest gap between two reads.
    display_contrast._UNSEEN[NAME] = {second: display_contrast.time.monotonic()
                                      - display_contrast.SEEN_WAIT_S - 0.1}
    assert "retry" not in _fit(second)
