"""A channel's mixture fit made by one of the website's helper Pythons.

The website's Python is one thread; a fit there took up to 2.7 s on one of
LSP11385's channels, and a click's calibration waited behind it. The fit route
now hands the fit to a helper in the page (providers/browser.py `page_fit`,
services/fitHelpers.js, workers/fitWorker.js), which runs mixture_fit.py's own
text, and is asked again until it is back. These hold the fit to what the main
Python makes and the hand-over to its states; tests/js/fit_helpers_probe.mjs
holds the page's pool.
"""

import json
import marshal
from urllib.parse import urlencode

import numpy as np
import pytest
import tifffile

import plexora
from plexora import _work, datasource
from plexora.browser import bridge
from plexora.server.models import data_model, mixture_fit
from plexora.server.providers import browser as page
from tests.helpers import use_data_root

NAME = "helped"


def _values(seed=0, n=4000):
    rng = np.random.default_rng(seed)
    return np.log(np.concatenate([rng.gamma(2.0, 40.0, n), rng.gamma(9.0, 120.0, n // 4)]) + 1.0)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_the_fit_is_scikit_learns_own(seed):
    from sklearn.mixture import GaussianMixture

    values = _values(seed)
    gmm = GaussianMixture(3, max_iter=1000, tol=1e-6, random_state=data_model._FIT_SEED)
    gmm.fit(values.reshape((-1, 1)))
    means, covars, weights = mixture_fit.mixture_parts(mixture_fit.fit_mixture(values))
    assert means.tobytes() == gmm.means_[:, 0].tobytes()
    assert covars.tobytes() == gmm.covariances_[:, 0, 0].tobytes()
    assert weights.tobytes() == gmm.weights_.tobytes()


def test_a_fit_made_elsewhere_makes_the_same_packet():
    plane = np.exp(_values(3, 9000)[:8100]).reshape(90, 90)
    here = data_model.channel_gmm_of(plane, 0.0, 9000.0)
    made = []

    def elsewhere(values, components):
        made.append((values.size, components))
        return mixture_fit.fit_mixture(values, components)

    with data_model.fits_elsewhere(elsewhere):
        there = data_model.channel_gmm_of(plane, 0.0, 9000.0)
    assert made and json.dumps(data_model._plain(here)) == json.dumps(data_model._plain(there))


def test_mixture_fit_imports_nothing_of_plexoras():
    """The helpers run its text with numpy and scikit-learn alone."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(mixture_fit))
    names = {alias.name.split(".")[0] for node in ast.walk(tree)
             if isinstance(node, ast.Import)
             for alias in node.names} | {node.module.split(".")[0] for node in ast.walk(tree)
                                         if isinstance(node, ast.ImportFrom) and node.module}
    assert names <= {"numpy", "sklearn"}, names


class Helpers:
    """The page's pool as Python sees it: `ready`, `start` and `result`, each
    fit made in-process with what the page would run (the source it is sent)."""

    def __init__(self, ready=True, pending=1, fail=False):
        self.ready, self.pending, self.fail = ready, pending, fail
        self.jobs = {}
        self.asked = []
        self.kinds = []

    def __call__(self, kind, args):
        assert kind == "fit"
        op = args["op"]
        self.asked.append(op)
        if op == "ready":
            return {"ready": self.ready}
        if op == "start":
            if not self.ready:
                return None
            namespace = {}
            data = bytes(np.asarray(args["code"]))
            code = (marshal.loads(data[16:]) if args["code_kind"] == "pyc"
                    else compile(data.decode("utf-8"), "mixture_fit.py", "exec"))
            exec(code, namespace)
            self.kinds.append(args["code_kind"])
            job = f"fit-{len(self.jobs) + 1}"
            self.jobs[job] = {"left": self.pending,
                              "params": namespace["fit_mixture"](np.asarray(args["values"]))}
            return {"id": job}
        job = self.jobs.get(args["id"])
        if job is None:
            return {"gone": True}
        if job["left"] > 0:
            job["left"] -= 1
            return {"pending": True}
        del self.jobs[args["id"]]
        if self.fail:
            return {"failed": "the helper failed"}
        return {"params": job["params"].tobytes().hex()}


@pytest.fixture
def helpers(monkeypatch):
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    pool = Helpers()
    page.set_asker(pool)
    yield pool
    page.set_asker(None)


def test_a_fit_is_handed_over_then_collected(helpers):
    values = _values(4)
    with pytest.raises(page.FitPending):
        page.page_fit(values)          # handed over
    with pytest.raises(page.FitPending):
        page.page_fit(values)          # still being made
    params = page.page_fit(values)
    assert params.tobytes() == mixture_fit.fit_mixture(values).tobytes()
    assert helpers.asked == ["start", "result", "result"]


def test_no_helper_ready_is_a_fit_here(helpers):
    helpers.ready = False
    assert page.page_fit(_values(5)) is None
    assert page.page_fits_ready() is False


def test_a_failed_or_forgotten_fit_is_a_fit_here(helpers):
    values = _values(6)
    helpers.fail, helpers.pending = True, 0
    with pytest.raises(page.FitPending):
        page.page_fit(values)
    assert page.page_fit(values) is None
    helpers.fail = False
    with pytest.raises(page.FitPending):
        page.page_fit(values)
    helpers.jobs.clear()                # a reload: the page knows no such job
    assert page.page_fit(values) is None


def test_the_helpers_run_the_compiled_file_where_python_runs_one(helpers, tmp_path, monkeypatch):
    """The website's bundle ships .pyc only: the helper is sent that file."""
    import py_compile

    compiled = tmp_path / "mixture_fit.pyc"
    py_compile.compile(mixture_fit.__file__.replace(".pyc", ".py"), cfile=str(compiled), doraise=True)
    monkeypatch.setattr(mixture_fit, "__file__", str(compiled))
    monkeypatch.setattr(page, "_MIXTURE_CODE", None)
    values = _values(8)
    with pytest.raises(page.FitPending):
        page.page_fit(values)
    with pytest.raises(page.FitPending):
        page.page_fit(values)
    assert page.page_fit(values).tobytes() == mixture_fit.fit_mixture(values).tobytes()
    assert helpers.kinds == ["pyc"]
    monkeypatch.setattr(page, "_MIXTURE_CODE", None)


def test_off_the_website_nothing_is_asked(monkeypatch):
    monkeypatch.delenv("PLEXORA_BACKEND", raising=False)
    pool = Helpers()
    page.set_asker(pool)
    try:
        assert page.page_fit(_values(7)) is None
        assert page.page_fits_ready() is False
        assert pool.asked == []
    finally:
        page.set_asker(None)


# -- the route, as the website's worker asks it --------------------------------

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
    yield
    _work._reset_for_tests()


def _channels():
    return [c["fullname"] for c in data_model.config[NAME]["imageData"] if c["name"] != "Area"]


def _fit(name, retried=None):
    headers = [["X-Plexora-Retried", str(retried)]] if retried is not None else []
    return bridge.handle(plexora.app, {"method": "GET", "path": "/get_channel_gmm",
                                       "query": urlencode({"channel": name, "datasource": NAME,
                                                           "provisional": "1"}),
                                       "headers": headers, "retryable": True}, "http://127.0.0.1")


def test_the_route_answers_with_the_helpers_fit_and_the_same_packet(project, monkeypatch):
    name = _channels()[1]
    expected = data_model.get_channel_gmm(name, NAME)      # made here, no helper
    data_model._gmm_cache.clear()
    monkeypatch.setattr(data_model, "_stored_fit", lambda *a, **k: None)
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    pool = Helpers(pending=1)
    page.set_asker(pool)
    monkeypatch.setattr(page, "page_window", lambda *a, **k: (0.0, float(expected["qmax"])))
    try:
        tries = 0
        while True:
            answered = _fit(name, retried=tries or None)
            if "retry" not in answered:
                break
            assert answered["retry"] == page.FitPending.retry_ms
            tries += 1
            assert tries < 10
        assert answered["status"] == 200
        assert tries == 2 and "start" in pool.asked
        assert json.loads(answered["body"]) == json.loads(json.dumps(data_model._plain(expected)))
    finally:
        page.set_asker(None)
