"""More of the website's fits made by helper Pythons (tests/test_page_fit.py
has the hand-over itself): the warm-up after a project opens, coming back for
each fit without asking again and again (`_work.spawn(after_s=...)`), and the
gating plugin's auto-gate fit through `plexora.api`."""

import json
import time

import numpy as np
import pytest

import plexora
from plexora import _work, api
from plexora.browser import bridge, cooperative
from plexora.server.models import data_model, mixture_fit
from plexora.server.providers import browser as page
from tests.agent_fixtures import make_synthetic_project
from tests.test_page_fit import Helpers


@pytest.fixture
def cooperative_work(monkeypatch):
    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    _work._reset_for_tests()
    yield
    _work._reset_for_tests()


class PeekingHelpers(Helpers):
    """`Helpers` that also answers `peek` (whether `result` would answer)."""

    def __call__(self, kind, args):
        if args.get("op") == "peek":
            # Time passes between two looks: the fit gets on.
            self.asked.append("peek")
            job = self.jobs.get(args["id"])
            if job is not None and job["left"] > 0:
                job["left"] -= 1
            return {"done": job is None or job["left"] <= 0}
        return super().__call__(kind, args)


# -- a task that comes back later -----------------------------------------------

def test_a_delayed_task_waits_its_turn(cooperative_work):
    ran = []
    _work.spawn(ran.append, "later", background=True, after_s=0.2)
    _work.spawn(ran.append, "now", background=True)
    assert _work.run_pending() == 1 and ran == ["now"]
    assert 0 < _work.due_in() <= 0.2
    step = cooperative.step(budget_ms=50, background=True)
    assert step["ran"] == 0 and 0 < step["wait_ms"] <= 200 and step["background"] == 1
    time.sleep(0.21)
    assert _work.due_in() == 0.0
    assert _work.run_pending() == 1 and ran == ["now", "later"]


def test_on_threads_a_delayed_task_is_a_thread_that_waits(monkeypatch):
    monkeypatch.setenv("PLEXORA_WORK", "threads")
    ran = []
    started = time.monotonic()
    handle = _work.spawn(lambda: ran.append(time.monotonic() - started), after_s=0.1)
    handle.join(5)
    assert ran and ran[0] >= 0.1


# -- the warm-up --------------------------------------------------------------------

@pytest.fixture
def opened(tmp_path, monkeypatch, cooperative_work):
    make_synthetic_project(tmp_path, "warm")
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    data_model.load_datasource("warm", reload=True)
    _work._reset_for_tests()
    pool = PeekingHelpers(pending=2)
    page.set_asker(pool)
    monkeypatch.setattr(data_model, "_FIT_LOOK_AGAIN_S", 0.0)
    yield pool
    page.set_asker(None)


def _channels(name):
    return [c["fullname"] for c in data_model.config[name]["imageData"] if c["name"] != "Area"]


def test_the_warm_up_hands_its_fits_over_and_comes_back_for_them(opened):
    names = _channels("warm")
    plan = [(data_model._fit_ahead, name) for name in names]
    data_model._warm_on_page("warm", data_model.load_generation, plan, 0)
    for _ in range(50):
        if not _work.run_pending():
            break
    made = {name: data_model._provisional_fits.get(("warm", data_model.load_generation, name))
            for name in names}
    assert all(made.values())
    # Asked whether each was back before taking it, not handed over twice.
    assert opened.asked.count("start") == len(names)
    assert "peek" in opened.asked
    # What a fit made here gives.
    data_model._provisional_fits.clear()
    page.set_asker(None)
    for name in names:
        here = data_model._provisional_gmm(name, "warm")
        assert json.dumps(data_model._plain(here)) == json.dumps(data_model._plain(made[name]))


def test_without_a_helper_the_warm_up_fits_here(opened):
    opened.ready = False
    names = _channels("warm")
    plan = [(data_model._fit_ahead, name) for name in names]
    data_model._warm_on_page("warm", data_model.load_generation, plan, 0)
    _work.run_pending()
    assert all(data_model._provisional_fits.get(("warm", data_model.load_generation, name))
               for name in names)
    assert "start" not in opened.asked


# -- the gating plugin's auto gate ------------------------------------------------

def test_gatings_fit_is_the_same_through_the_api():
    from plexora.plugins.gating.server import model

    rng = np.random.default_rng(1)
    values = np.log1p(np.concatenate([rng.gamma(2, 50, 30000), rng.gamma(12, 90, 6000)]))
    before = model._fit_mixture(values, 3)
    made = []

    def elsewhere(sample, components):
        made.append((sample.size, components))
        return mixture_fit.fit_mixture(sample, components)

    with data_model.fits_elsewhere(elsewhere):
        after = model._fit_mixture(values, 3)
    assert made == [(values.size, 3)]
    assert all(a.tobytes() == b.tobytes() for a, b in zip(before, after))


def test_the_gating_route_asks_again_then_answers_the_same(tmp_path, monkeypatch, cooperative_work):
    make_synthetic_project(tmp_path, "gated")
    data_model.load_datasource("gated", reload=True)
    body = json.dumps({"channel": "CD8", "datasource": "gated", "selection_ids": []}).encode()

    def ask(retried=None):
        headers = [["Content-Type", "application/json"]]
        if retried is not None:
            headers.append(["X-Plexora-Retried", str(retried)])
        return bridge.handle(plexora.app, {"method": "POST", "path": "/plugins/gating/get_gating_gmm",
                                           "query": "", "headers": headers, "body": body,
                                           "retryable": True}, "http://127.0.0.1")

    expected = ask()
    assert expected["status"] == 200 and "retry" not in expected
    data_model._gmm_cache.clear()
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    pool = Helpers(pending=1)
    page.set_asker(pool)
    try:
        tries = 0
        while True:
            answered = ask(retried=tries or None)
            if "retry" not in answered:
                break
            assert answered["retry"] == page.FitPending.retry_ms
            tries += 1
            assert tries < 10
        assert tries == 2 and pool.asked.count("start") == 1
        assert json.loads(answered["body"]) == json.loads(expected["body"])
    finally:
        page.set_asker(None)


def test_the_api_names_what_a_plugin_route_needs():
    assert api.AskAgain is page.WindowPending and issubclass(page.FitPending, api.AskAgain)
    with api.page_fits():          # off the website: nothing changes
        assert data_model._FIT_ELSEWHERE.get() is None
