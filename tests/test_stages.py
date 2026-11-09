"""Stages loaded on first import (plexora/browser/stages.py)."""

import importlib
import importlib.util
import sys

import pytest

from plexora.browser import stages


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """A stage `demo` whose module appears on disk only when it is loaded."""
    monkeypatch.setitem(stages.STAGES, "demo", {"pyodide": [], "wheels": [],
                                                "modules": ["plexora_demo_stage"]})
    monkeypatch.syspath_prepend(str(tmp_path))
    loads = []

    def load(stage):
        loads.append(stage)
        (tmp_path / "plexora_demo_stage.py").write_text("VALUE = 42\n", encoding="utf-8")

    finder = stages.StageFinder(load)
    sys.meta_path.insert(0, finder)
    yield finder, loads
    sys.meta_path.remove(finder)
    sys.modules.pop("plexora_demo_stage", None)


def test_asking_whether_it_is_there_loads_nothing(demo):
    finder, loads = demo
    assert importlib.util.find_spec("plexora_demo_stage") is None
    assert loads == [] and "demo" not in finder.loaded


def test_importing_it_loads_its_stage_once(demo):
    finder, loads = demo
    module = importlib.import_module("plexora_demo_stage")
    assert module.VALUE == 42
    assert loads == ["demo"] and "demo" in finder.loaded
    importlib.reload(module)
    assert loads == ["demo"]


def test_a_stage_that_fails_is_a_missing_package_and_not_asked_again(tmp_path, monkeypatch):
    monkeypatch.setitem(stages.STAGES, "broken", {"pyodide": [], "wheels": [],
                                                  "modules": ["plexora_broken_stage"]})
    calls = []

    def load(stage):
        calls.append(stage)
        raise RuntimeError("offline")

    finder = stages.StageFinder(load)
    sys.meta_path.insert(0, finder)
    try:
        for _ in range(2):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module("plexora_broken_stage")
    finally:
        sys.meta_path.remove(finder)
    assert calls == ["broken"] and finder.failed == {"broken": "offline"}


def test_every_stage_module_belongs_to_one_stage():
    seen = {}
    for name, stage in stages.STAGES.items():
        for module in stage["modules"]:
            assert module not in seen, (module, seen.get(module), name)
            seen[module] = name
    assert stages.stage_of("cv2.dnn") == "qc" and stages.stage_of("numpy") is None


# -- a stage loaded at its module's first use (polars on the website) ------------------

DEFERRED_NAME = "plexora_demo_deferred"


@pytest.fixture
def deferred(tmp_path, monkeypatch):
    """A package whose stage is deferred: on disk only once its stage loads,
    with a submodule, as polars has hundreds."""
    monkeypatch.setitem(stages.STAGES, "demo_deferred", {"pyodide": [], "wheels": [],
                                                         "modules": [DEFERRED_NAME]})
    monkeypatch.syspath_prepend(str(tmp_path))
    loads, hooks = [], []

    def load(stage):
        loads.append(stage)
        package = tmp_path / DEFERRED_NAME
        package.mkdir()
        (package / "__init__.py").write_text(
            "from . import part\nVALUE = 7\nINIT_RUNS = globals().get('INIT_RUNS', 0) + 1\n",
            encoding="utf-8")
        (package / "part.py").write_text("PART = 'p'\n", encoding="utf-8")

    from plexora.browser import boot

    def hook(stage):
        module = sys.modules[DEFERRED_NAME]
        hooks.append((stage, getattr(module, "VALUE", None)))

    monkeypatch.setattr(boot, "stage", hook)
    finder = stages.StageFinder(load)
    yield finder, loads, hooks
    for name in [n for n in sys.modules if n.split(".")[0] == DEFERRED_NAME]:
        sys.modules.pop(name)


def test_a_deferred_module_imports_without_loading_its_stage(deferred):
    finder, loads, _hooks = deferred
    stand_in = finder.defer(DEFERRED_NAME)

    imported = importlib.import_module(DEFERRED_NAME)

    assert imported is stand_in and stages.waiting(DEFERRED_NAME)
    assert loads == []
    # Asking whether it is there says yes, and loads nothing either.
    assert importlib.util.find_spec(DEFERRED_NAME) is not None and loads == []


def test_its_first_attribute_loads_the_stage_and_it_becomes_the_module(deferred):
    finder, loads, hooks = deferred
    stand_in = finder.defer(DEFERRED_NAME)
    held = importlib.import_module(DEFERRED_NAME)  # what a module's top bound

    assert held.VALUE == 7

    assert loads == ["demo_deferred"] and "demo_deferred" in finder.loaded
    assert held is stand_in is sys.modules[DEFERRED_NAME] and not stages.waiting(DEFERRED_NAME)
    assert held.INIT_RUNS == 1 and held.part.PART == "p"
    assert held.__file__.endswith("__init__.py") and held.__spec__.origin == held.__file__
    assert hooks == [("demo_deferred", 7)]  # once, after the module ran
    assert "_plexora_finder" not in vars(held)
    assert finder.importers["demo_deferred"].startswith(__name__ + ":")
    # Everything else the import system does with it works as with any package.
    from plexora_demo_deferred import VALUE  # noqa: F401
    from plexora_demo_deferred.part import PART
    assert PART == "p" and loads == ["demo_deferred"]


def test_a_from_import_or_a_submodule_import_is_a_first_use_too(deferred):
    finder, loads, _hooks = deferred
    finder.defer(DEFERRED_NAME)

    part = importlib.import_module(DEFERRED_NAME + ".part")

    assert part.PART == "p" and loads == ["demo_deferred"]


def test_a_deferred_stage_that_fails_says_so_every_time_and_is_asked_once(deferred, monkeypatch):
    finder, _loads, _hooks = deferred
    calls = []

    def offline(stage):
        calls.append(stage)
        raise RuntimeError("offline")

    monkeypatch.setattr(finder, "_load", offline)
    held = finder.defer(DEFERRED_NAME)
    for _ in range(2):
        with pytest.raises(ModuleNotFoundError, match="offline"):
            held.VALUE  # noqa: B018
    assert calls == ["demo_deferred"] and stages.waiting(DEFERRED_NAME)


def test_nothing_is_deferred_where_its_stage_is_loaded_already(deferred):
    """A browser without JSPI loads every stage at boot (pythonWorker.js)."""
    _finder, loads, _hooks = deferred
    finder = stages.StageFinder(lambda stage: None, loaded=("base", "demo_deferred"))
    assert finder.defer(DEFERRED_NAME) is None and DEFERRED_NAME not in sys.modules


def test_a_preload_loads_its_stages_in_one_go_and_runs_a_deferred_module(deferred, monkeypatch):
    finder, loads, hooks = deferred
    monkeypatch.setitem(stages.STAGES, "demo_other", {"pyodide": [], "wheels": [],
                                                      "modules": ["plexora_demo_other"]})
    together = []

    def load_many(names):
        together.append(list(names))
        for name in names:
            if name == "demo_deferred":
                finder._load(name)  # the fixture's files

    finder.load_many = load_many
    held = finder.defer(DEFERRED_NAME)

    assert finder.preload(("demo_deferred", "demo_other")) == ["demo_deferred", "demo_other"]

    assert together == [["demo_deferred", "demo_other"]]
    assert {"demo_deferred", "demo_other"} <= finder.loaded
    assert not stages.waiting(DEFERRED_NAME) and held.VALUE == 7 and "_plexora_finder" not in vars(held)
    assert hooks == [("demo_deferred", 7), ("demo_other", 7)]
    assert finder.importers["demo_deferred"] == "preload"
    assert finder.preload(("demo_deferred", "demo_other")) == []  # nothing left to load


def test_a_preload_that_fails_leaves_the_stage_for_its_first_use(deferred):
    finder, loads, _hooks = deferred

    def offline(names):
        raise RuntimeError("offline")

    finder.load_many = offline
    held = finder.defer(DEFERRED_NAME)

    assert finder.preload(("demo_deferred",)) == []
    assert "demo_deferred" not in finder.loaded and "demo_deferred" not in finder.failed
    assert stages.waiting(DEFERRED_NAME)
    assert held.VALUE == 7 and loads == ["demo_deferred"]  # its first use loads it


def test_polars_is_deferred_and_in_no_base():
    assert "polars" in stages.DEFERRED and stages.stage_of("polars.dataframe") == "polars"
    assert "polars" not in stages.STAGES["base"]["pyodide"]

