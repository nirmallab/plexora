"""The gate-validation panel through the pipeline."""

import io

import pytest
from PIL import Image

from plexora.agent import AgentSession, invoke, registry
from tests.agent_fixtures import make_synthetic_project


@pytest.fixture
def session(tmp_path):
    make_synthetic_project(tmp_path)
    registry.discover(["gating"])
    return AgentSession()


def test_panels_per_field_with_counts(session):
    invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": 1500})
    outcome = invoke(session, "render_gate_validation",
                     {"project": "synth", "marker": "CD8", "field_size_um": 60,
                      "max_fields": 3, "panel_px": 200})
    assert outcome["ok"], outcome
    result = outcome["result"]
    images = result["_images"]
    assert len(images) == len(result["fields"]) <= 3
    first = Image.open(io.BytesIO(images[0]))
    assert first.width <= 1600 and first.width >= 3 * 200
    field = result["fields"][0]
    assert {"cells_in_field", "called_positive", "field_positive_fraction",
            "dataset_positive_fraction", "borderline", "borderline_cells",
            "artifact"} <= set(field)
    assert result["gate"]["low"] == 1500
    assert "too_low" in result["response_schema"]["per_field"]["assessment"]


def test_a_marker_without_a_channel_cannot_be_looked_at(tmp_path):
    import json

    from tests.agent_fixtures import make_synthetic_project as make

    make(tmp_path, "other")
    config = json.loads((tmp_path / "config.json").read_text())
    for channel in config["other"]["imageData"]:
        if channel.get("name") == "CD8":
            channel["name"] = channel["fullname"] = "CD8a"
    (tmp_path / "config.json").write_text(json.dumps(config))
    registry.discover(["gating"])
    outcome = invoke(AgentSession(), "render_gate_validation",
                     {"project": "other", "marker": "CD8", "max_fields": 1})
    assert outcome["error"]["code"] == "precondition_missing"
