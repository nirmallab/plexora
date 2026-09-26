"""render_region: deterministic, honest about what it drew, and never the viewer's."""

import io

import numpy as np
import pytest
from PIL import Image

from plexora.agent import AgentSession, invoke, registry
from plexora.agent import render as agent_render
from plexora.agent.render_spec import RenderInput
from plexora.server.models import data_model
from tests.agent_fixtures import make_synthetic_project


@pytest.fixture
def made(tmp_path):
    info = make_synthetic_project(tmp_path)
    registry.discover(["gating"])
    return info


def _render(session, **spec):
    return agent_render.render_region(session, RenderInput(project="synth", **spec))


def test_identical_specs_give_identical_bytes_and_manifests(made):
    spec = dict(center={"center_x": 160, "center_y": 160, "size_px": 200}, marker="CD8")
    a = _render(AgentSession(), **spec)
    b = _render(AgentSession(), **spec)
    assert a["png"] == b["png"]
    assert a["manifest"] == b["manifest"]
    assert a["artifact"]["id"] == b["artifact"]["id"]


def test_the_highlight_counts_match_the_table(made):
    session = AgentSession()
    invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": 1500})
    # The top-left 2x2 block of cells: ids 1, 2, 9, 10.
    result = _render(session, bounds={"x": 0, "y": 0, "width": 128, "height": 128},
                     marker="CD8", output={"width": 256})
    cells = result["manifest"]["cells"]
    expected = {c["id"] for c in made["cells"] if c["id"] in (1, 2, 9, 10)
                and c["cd8"] > 1500}
    assert cells["visible_cells"] == 4
    assert cells["positive_cells"] == len(expected)
    assert result["manifest"]["highlight"]["gate_source"] == "stored_gate"
    # Magenta outline pixels exist exactly when something is positive.
    rgb = np.asarray(Image.open(io.BytesIO(result["png"])))
    magenta = (rgb[..., 0] > 200) & (rgb[..., 1] < 120) & (rgb[..., 2] > 200)
    assert magenta.any() == bool(expected)


def test_auto_windows_and_levels_are_recorded(made):
    whole = _render(AgentSession(), output={"width": 128})
    manifest = whole["manifest"]
    assert manifest["level"] == 2 and manifest["level_source"] == "auto"
    assert all(c["window_source"].startswith("auto:") for c in manifest["channels"])
    close = _render(AgentSession(), bounds={"x": 0, "y": 0, "width": 128, "height": 128},
                    output={"width": 256})
    assert close["manifest"]["level"] == 0
    clamped = _render(AgentSession(), level=9, output={"width": 64})
    assert clamped["manifest"]["level"] == 2 and clamped["manifest"]["level_source"] == "clamped"


def test_off_the_edge_is_padded_not_shifted(made):
    result = _render(AgentSession(), bounds={"x": -64, "y": -64, "width": 128, "height": 128},
                     output={"width": 128}, segmentation="none", scale_bar=False)
    manifest = result["manifest"]
    assert manifest["padded"] is True
    assert manifest["bounds_fullres"]["x"] == -64
    rgb = np.asarray(Image.open(io.BytesIO(result["png"])))
    assert rgb[:60, :60].max() < 30


def test_uncalibrated_images_get_no_scale_bar_and_refuse_microns(tmp_path):
    make_synthetic_project(tmp_path, calibrated=False)
    result = _render(AgentSession(), output={"width": 256})
    assert result["manifest"]["scale_bar"] is None
    assert result["manifest"]["physical"] is False
    outcome = invoke(AgentSession(), "render_region",
                     {"project": "synth", "center": {"center_x": 10, "center_y": 10,
                                                      "size_um": 50}})
    assert outcome["error"]["code"] == "precondition_missing"


def test_an_unknown_channel_is_never_substituted(made):
    outcome = invoke(AgentSession(), "render_region",
                     {"project": "synth", "channels": [{"name": "CD4"}]})
    assert outcome["error"]["code"] == "invalid_input"
    assert outcome["error"]["detail"]["channels"] == ["DNA", "CD8"]


def test_a_node_mask_is_reported_unsupported(made, tmp_path):
    import json

    from plexora.server.models.project import Project, ResourceBinding

    record = Project.load("synth").with_resource(
        "segmentation", ResourceBinding(kind="segmentation", provider="node", node="n",
                                        resource_id="m"))
    config = json.loads((tmp_path / "config.json").read_text())
    config["synth"] = record.to_entry()
    (tmp_path / "config.json").write_text(json.dumps(config))
    result = _render(AgentSession(), marker="CD8", output={"width": 128})
    manifest = result["manifest"]
    assert manifest["segmentation"]["status"] == "unsupported"
    assert manifest["cells"]["highlight_rendering"] == "centroids"
    assert any(item["layer"] == "__mask__" for item in manifest["not_rendered"])


def test_rendering_never_loads_a_datasource(made, monkeypatch):
    monkeypatch.setattr(data_model, "load_datasource",
                        lambda *a, **k: pytest.fail("loaded a datasource"))
    _render(AgentSession(), marker="CD8", cells={"label_ids": True}, output={"width": 200})
    assert data_model._loaded_source is None


def test_the_tool_returns_the_image_inline(made):
    outcome = invoke(AgentSession(), "render_region", {"project": "synth",
                                                       "output": {"width": 64}})
    assert outcome["ok"]
    result = outcome["result"]
    assert result["image_inline"] is True
    assert result["_images"][0][:8] == b"\x89PNG\r\n\x1a\n"
    fetched = invoke(AgentSession(), "get_artifact",
                     {"artifact_id": result["artifact"]["id"]})["result"]
    assert fetched["_images"][0] == result["_images"][0]
    assert fetched["manifest"] == result["manifest"]
