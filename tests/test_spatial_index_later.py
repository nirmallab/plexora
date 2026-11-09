"""On the website the cell index is built after the viewer's first screen.

The index (server/utils/spatial_index.py, a cKDTree) needs scipy, which the
website loads as a stage of its own: about a second of WebAssembly, then the
tree itself, which grows with the table. Built in `init` (the viewer's
`/init_database`) or with the table loaded behind the viewer, it held the
first screen for nothing a first screen does. Now `init` and the table queue
it as a background task (`data_model._index_later`), and a pick before it is
built builds it then. The desktop's threads build it as they always did.
"""

import numpy as np
import pytest

from plexora import _work
from plexora.browser import cooperative
from plexora.server.models import data_model
from tests.test_nearest_cell import _builds, _register, root  # noqa: F401 -- the fixture


@pytest.fixture
def website(monkeypatch):
    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    _work._reset_for_tests()
    yield
    _work._reset_for_tests()


def _run_background():
    names = []
    for _ in range(50):
        stepped = cooperative.step(budget_ms=10_000)
        names += stepped["names"]
        if not stepped["pending"] and not stepped["background"]:
            break
    return names


def test_init_leaves_the_index_to_a_background_task(root, website, monkeypatch):
    built = _builds(monkeypatch)
    _register(root, "web", [5.0, 20.0, 40.0], [5.0, 20.0, 40.0])

    data_model.init("web")

    assert data_model.ball_tree is None and built == []
    assert "spatial-index-web" in _run_background()
    assert data_model.ball_tree is not None and built == [1]
    assert data_model.query_for_closest_cell(21, 19, "web")["CellID"] == 1


def test_a_pick_before_the_task_builds_the_index_itself(root, website, monkeypatch):
    built = _builds(monkeypatch)
    rng = np.random.default_rng(5)
    xs, ys = rng.uniform(0, 60, 200), rng.uniform(0, 60, 200)
    _register(root, "early", xs, ys)
    data_model.init("early")

    found = data_model.query_for_closest_cell(30, 30, "early")

    assert found["CellID"] == int(np.argmin(np.hypot(xs - 30, ys - 30)))
    assert built == [1]
    # The queued task finds it built and builds nothing more.
    _run_background()
    assert built == [1]


def test_a_task_for_a_project_no_longer_loaded_does_nothing(root, website, monkeypatch):
    built = _builds(monkeypatch)
    _register(root, "first", [1.0, 2.0], [1.0, 2.0])
    _register(root, "second", [9.0, 8.0], [9.0, 8.0])
    data_model.init("first")
    data_model.load_datasource("second")

    _run_background()

    assert built == []


def test_on_threads_init_builds_it_as_before(root, monkeypatch):
    monkeypatch.setenv("PLEXORA_WORK", "threads")
    built = _builds(monkeypatch)
    _register(root, "desk", [5.0, 20.0], [5.0, 20.0])

    data_model.init("desk")

    assert data_model.ball_tree is not None and built == [1]
