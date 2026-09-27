"""render_cell_gallery: single cells, cropped, outlined and sorted by expression."""

import json

import pytest

from plexora.agent import AgentSession, Policy, invoke, registry
from tests.agent_fixtures import make_synthetic_project


@pytest.fixture
def info(tmp_path):
    return make_synthetic_project(tmp_path)


@pytest.fixture
def session(info):
    registry.discover(["gating"])
    return AgentSession()


def _ok(result):
    assert result["ok"], result
    return result["result"]


def test_requested_cells_are_sorted_by_expression(session, info):
    wanted = [c["id"] for c in info["cells"][:6]]
    result = _ok(invoke(session, "render_cell_gallery", {
        "project": "synth", "cell_ids": wanted, "marker": "CD8"}))
    tiles = result["manifest"]["tiles"]
    assert sorted(t["cell_id"] for t in tiles) == sorted(wanted)
    values = [t["value"] for t in tiles]
    assert values == sorted(values, reverse=True)
    by_id = {c["id"]: c["cd8"] for c in info["cells"]}
    for tile in tiles:
        assert tile["value"] == pytest.approx(by_id[tile["cell_id"]], abs=1e-3)
        assert tile["outline_drawn"] == "mask"
        assert tile["segmentation_status"] == "rendered"
    assert result["image_inline"] is True and result["_images"][0][:8] == b"\x89PNG\r\n\x1a\n"


def test_borderline_picks_the_cells_nearest_the_gate(session, info):
    _ok(invoke(session, "set_gate", {"project": "synth", "marker": "CD8", "low": 1500}))
    result = _ok(invoke(session, "render_cell_gallery", {
        "project": "synth", "marker": "CD8", "select": "borderline", "n": 8}))
    manifest = result["manifest"]
    assert manifest["gate"] == {"low": 1500.0, "high": pytest.approx(manifest["gate"]["high"]),
                                "source": "stored_gate"}
    drawn = {t["cell_id"] for t in manifest["tiles"]}
    assert drawn <= set(info["borderline"]) and len(drawn) == 8
    assert all(t["call"] == "negative" for t in manifest["tiles"])
    # Nearest the gate: the eight brightest borderline cells.
    brightest = sorted((c for c in info["cells"] if c["group"] == "borderline"),
                       key=lambda c: -c["cd8"])[:8]
    assert drawn == {c["id"] for c in brightest}


def test_identical_requests_give_identical_bytes_and_one_artifact(session, tmp_path):
    ask = {"project": "synth", "marker": "CD8", "select": "positive", "n": 4}
    first = _ok(invoke(session, "render_cell_gallery", ask))
    second = _ok(invoke(session, "render_cell_gallery", ask))
    assert first["_images"][0] == second["_images"][0]
    assert first["artifact"]["id"] == second["artifact"]["id"]
    stored = list((tmp_path / ".agent" / "artifacts" / "synth").glob("art_*.png"))
    assert len(stored) == 1


def test_bad_requests_are_structured_errors(session):
    unknown = invoke(session, "render_cell_gallery", {"project": "synth",
                                                      "cell_ids": [1, 99999]})
    assert unknown["error"]["code"] == "invalid_input"
    assert unknown["error"]["detail"]["unknown"] == [99999]
    too_many = invoke(session, "render_cell_gallery", {"project": "synth", "marker": "CD8",
                                                       "select": "positive", "n": 500})
    assert too_many["error"]["code"] == "invalid_input"
    neither = invoke(session, "render_cell_gallery", {"project": "synth"})
    assert neither["error"]["code"] == "invalid_input"


def test_an_uncalibrated_image_needs_a_pixel_crop(tmp_path):
    make_synthetic_project(tmp_path, "raw", calibrated=False)
    session = AgentSession()
    refused = invoke(session, "render_cell_gallery", {"project": "raw", "cell_ids": [1],
                                                      "crop_um": 30})
    assert refused["error"]["code"] == "precondition_missing"
    fine = _ok(invoke(session, "render_cell_gallery", {"project": "raw", "cell_ids": [1]}))
    assert fine["manifest"]["crop"] == {"size_px": 160.0, "how": "default_px"}


def test_a_marker_without_a_channel_is_a_missing_precondition(session, tmp_path):
    import dataclasses

    from plexora.server.models.project import Project

    record = Project.load("synth")
    renamed = tuple({**c, "name": "CD8_img", "fullname": "CD8_img"}
                    if c.get("name") == "CD8" else c for c in record.image.channels)
    record = dataclasses.replace(record, image=dataclasses.replace(record.image,
                                                                   channels=renamed))
    config = json.loads((tmp_path / "config.json").read_text())
    config["synth"] = record.to_entry()
    (tmp_path / "config.json").write_text(json.dumps(config))
    refused = invoke(session, "render_cell_gallery", {"project": "synth", "marker": "CD8",
                                                      "select": "positive"})
    assert refused["error"]["code"] == "precondition_missing"


def test_it_arrives_as_one_image_over_mcp(session):
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client
    from mcp.types import ImageContent

    from plexora.mcp.server import build_server

    server = build_server(session, names=["gating"])

    async def go():
        async with Client(server) as client:
            return await client.call_tool("render_cell_gallery", {
                "project": "synth", "marker": "CD8", "select": "brightest", "n": 6})

    result = anyio.run(go)
    assert not result.is_error
    assert sum(isinstance(part, ImageContent) for part in result.content) == 1


def test_it_is_refused_without_rendered_pixels_egress(session):
    from dataclasses import replace

    policy = replace(Policy(), egress=frozenset({"metadata", "aggregates"}))
    refused = invoke(session, "render_cell_gallery", {"project": "synth", "cell_ids": [1]},
                     policy=policy)
    assert refused["error"]["code"] == "permission_required"
