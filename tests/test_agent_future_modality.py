"""A modality core has never heard of, arriving as a third-party plugin."""

import json

import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.server import plugins as plugin_registry
from plexora.server.models.project import LayerSpec, Project
from tests.agent_fixtures import make_synthetic_project


class _EntryPoint:
    name = "future_modality"

    def load(self):
        from tests.fixtures.plugins.future_modality import PLUGIN

        return PLUGIN


@pytest.fixture
def installed(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_registry, "_entry_points_by_name",
                        lambda: {"future_modality": _EntryPoint()})
    make_synthetic_project(tmp_path, "plain")
    make_synthetic_project(tmp_path, "holo")
    record = Project.load("holo").with_layer(LayerSpec(
        id="holo_1", kind="image", label="Hologram", modality="holography",
        extra={"wavelength_nm": 633, "phase_unwrapped": True}))
    config = json.loads((tmp_path / "config.json").read_text())
    config["holo"] = record.to_entry()
    (tmp_path / "config.json").write_text(json.dumps(config))
    registry.discover(["future_modality"])
    return AgentSession()


def test_its_capabilities_are_discovered(installed):
    names = {cap.name for cap in registry.all_capabilities()}
    assert {"holography.summarize", "holography.show"} <= names


def test_it_applies_only_where_there_is_a_hologram(installed):
    plain = invoke(installed, "holography.summarize", {"project": "plain"})
    assert plain["error"]["code"] == "unsupported_modality"
    holo = invoke(installed, "holography.summarize", {"project": "holo"})
    assert holo["ok"], holo
    # Metadata core does not model round-trips untouched.
    assert holo["result"]["holograms"][0]["extra"] == {"wavelength_nm": 633,
                                                       "phase_unwrapped": True}


def test_a_viewer_capability_without_a_viewer_says_so(installed):
    result = invoke(installed, "holography.show", {"project": "holo"})
    assert result["error"]["code"] == "viewer_not_available"


def test_the_scene_carries_the_new_layer(installed):
    scene = invoke(installed, "get_scene", {"project": "holo"})["result"]
    holo = next(a for a in scene["assets"] if a["layer_id"] == "holo_1")
    assert holo["modality"] == "holography" and holo["element_type"] == "image"
    assert holo["extra"]["wavelength_nm"] == 633


def test_a_render_reports_the_layer_it_did_not_draw(installed):
    outcome = invoke(installed, "render_region", {"project": "holo", "output": {"width": 64}})
    assert outcome["ok"], outcome
    assert any(item["layer"] == "holo_1"
               for item in outcome["result"]["manifest"]["not_rendered"])
