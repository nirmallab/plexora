"""The gate-validation panel through the pipeline."""

import io

import pytest
from PIL import Image

from plexora.agent import AgentSession, invoke, registry
from tests.agent_fixtures import make_synthetic_project

#: These exercise Pro (AI) capabilities, so they run with a test licence
#: installed; what Free refuses is tests/test_licensing_enforcement.py's job.
pytestmark = pytest.mark.pro


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


def _deep_cells(tmp_path):
    """Sixteen cells, every one on planes 6-8 of ten; CD8 bright in half."""
    import numpy as np
    import tifffile

    from plexora import datasets
    from plexora.datasource import register_image_datasource
    from plexora.server.models.project import Project
    from plexora.server.volume import objects

    rng = np.random.default_rng(1)
    image = rng.normal(100, 5, (2, 10, 96, 96)).clip(0).astype(np.uint16)
    labels = np.zeros((10, 96, 96), dtype=np.uint16)
    for i in range(16):
        y, x = 6 + (i // 4) * 22, 6 + (i % 4) * 22
        labels[6:9, y:y + 8, x:x + 8] = i + 1
        image[0, 6:9, y:y + 8, x:x + 8] = 2000
        image[1, 6:9, y:y + 8, x:x + 8] = 3000 if i % 2 else 300
    path = tmp_path / "deep.ome.tif"
    tifffile.imwrite(path, image, ome=True, metadata={
        "axes": "CZYX", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5, "PhysicalSizeZ": 2.0,
        "Channel": {"Name": ["DNA", "CD8"]}})
    tifffile.imwrite(tmp_path / "deep_mask.tif", labels)
    register_image_datasource("deep", path)
    datasets.add_layers("deep", tmp_path / "deep_mask.tif", wait=True)
    objects.measure_project_objects(Project.find("deep"), units="physical")


def test_on_a_z_stack_a_field_is_drawn_at_its_cells_plane_with_its_depth(tmp_path):
    _deep_cells(tmp_path)
    registry.discover(["gating"])
    session = AgentSession()
    gated = invoke(session, "set_gate", {"project": "deep", "marker": "CD8", "low": 1000})
    assert gated["ok"], gated
    outcome = invoke(session, "render_gate_validation",
                     {"project": "deep", "marker": "CD8", "field_size_um": 30,
                      "max_fields": 2, "panel_px": 200})
    assert outcome["ok"], outcome
    result = outcome["result"]
    field = result["fields"][0]
    # The middle plane (5) holds none of the cells; the field is drawn at 7.
    assert field["plane"] == 7 and field["view"] == "z7"
    assert field["plane_from"] in ("borderline cells", "field cells")
    picture = Image.open(io.BytesIO(result["_images"][0]))
    assert picture.height > 2 * 200, "a row of sections under A | B | C"
    from plexora.agent import artifacts

    _png, sidecar = artifacts.get(field["artifact"]["id"])
    manifest = sidecar.get("manifest", sidecar)
    assert manifest["view"] == "z7" and manifest["panels"]["D"]["kind"] == "sections"
    assert manifest["panels"]["D"]["outlines"] is True
    assert manifest["panels"]["B"]["segmentation"]["mask_plane"] == 7
