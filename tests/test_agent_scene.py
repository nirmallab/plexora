"""The scene view (schema 0.5) across the shapes a project comes in."""

import dataclasses
import json

import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.agent.core.scene import build_scene, derived_id
from plexora.server.models.project import LayerSpec, Project, ResourceBinding
from tests.agent_fixtures import make_synthetic_project


@pytest.fixture(autouse=True)
def discovered():
    registry.discover(["gating", "roi"])


def _save(tmp_path, record):
    config = json.loads((tmp_path / "config.json").read_text())
    config[record.name] = record.to_entry()
    (tmp_path / "config.json").write_text(json.dumps(config))


def _scene(name):
    outcome = invoke(AgentSession(), "get_scene", {"project": name})
    assert outcome["ok"], outcome
    return outcome["result"]


def test_a_full_project(tmp_path):
    make_synthetic_project(tmp_path)
    scene = _scene("synth")
    assert scene["schema_version"] == "0.5"
    kinds = {a["layer_id"]: a["element_type"] for a in scene["assets"]}
    assert kinds == {"__image__": "image", "__mask__": "labels", "__centroids__": "points"}
    assert [cs["units"] for cs in scene["coordinate_systems"]] == ["pixel", "µm"]
    assert scene["transforms"][0]["kind"] == "scale"
    assert scene["entity_sets"][0]["id_column"] == "CellID"
    markers = next(f for f in scene["feature_spaces"] if f["name"] == "markers")
    assert markers["features"] == ["DNA", "CD8"] and markers["scale"] == "raw"
    kinds = {a["kind"] for a in scene["associations"]}
    assert {"labels_entities", "points_are_entities"} <= kinds
    image = next(a for a in scene["assets"] if a["layer_id"] == "__image__")
    assert "render_region" in image["capabilities"]


def test_ids_are_deterministic(tmp_path):
    make_synthetic_project(tmp_path)
    assert _scene("synth")["assets"] == _scene("synth")["assets"]
    assert derived_id("asset", "synth", "__image__") == derived_id("asset", "synth", "__image__")
    assert derived_id("asset", "synth", "__image__") != derived_id("asset", "other", "__image__")


def test_image_only_and_uncalibrated(tmp_path):
    make_synthetic_project(tmp_path, calibrated=False, mask=False, table=False)
    scene = _scene("synth")
    assert [a["layer_id"] for a in scene["assets"]] == ["__image__"]
    assert scene["entity_sets"] == [] and scene["transforms"] == []
    assert any("uncalibrated" in note for note in scene["notes"])


def test_a_transformed_layer_gets_its_own_system(tmp_path):
    make_synthetic_project(tmp_path)
    record = Project.load("synth").with_layer(LayerSpec(
        id="he", kind="image", label="H&E", modality="he",
        transform=(2.0, 0.0, 0.0, 2.0, 10.0, 20.0)))
    _save(tmp_path, record)
    scene = _scene("synth")
    edge = next(t for t in scene["transforms"] if t["kind"] == "affine2d")
    assert edge["matrix"] == [2.0, 0.0, 0.0, 2.0, 10.0, 20.0]
    assert edge["target"] == scene["reference_coordinate_system"]


def test_a_node_bound_table_reads_nothing(tmp_path):
    make_synthetic_project(tmp_path)
    record = Project.load("synth").with_resource("table", ResourceBinding(
        kind="table", provider="node", node="asleep", resource_id="t"))
    _save(tmp_path, record)
    scene = _scene("synth")
    centroids = next(a for a in scene["assets"] if a["layer_id"] == "__centroids__")
    assert centroids["provider"] == "node" and centroids["node"] == "asleep"
    assert scene["entity_sets"][0]["table"]["provider"] == "node"
