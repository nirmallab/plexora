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
