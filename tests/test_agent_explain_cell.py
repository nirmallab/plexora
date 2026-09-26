"""explain_cell: one cell's markers, regions, neighbours and crop."""

from dataclasses import replace

import pytest

from plexora.agent import AgentSession, Policy, invoke, registry
from tests.agent_fixtures import make_synthetic_project

TRIANGLE = [[0, 0], [200, 0], [0, 200]]


@pytest.fixture
def info(tmp_path):
    return make_synthetic_project(tmp_path)


@pytest.fixture
def session(info):
    registry.discover(["roi"])
    return AgentSession()


def _ok(result):
    assert result["ok"], result
    return result["result"]


def _cell(info, cell_id):
    return next(c for c in info["cells"] if c["id"] == cell_id)


def test_values_and_percentiles_match_the_table(session, info):
    result = _ok(invoke(session, "explain_cell", {"project": "synth", "cell_id": 10,
                                                  "include_crop": False}))
    cd8 = sorted(c["cd8"] for c in info["cells"])
    mine = _cell(info, 10)["cd8"]
    assert result["markers"]["CD8"]["value"] == pytest.approx(mine, abs=1e-3)
    expected = sum(1 for v in cd8 if v <= mine + 1e-6) / len(cd8) * 100
    assert result["markers"]["CD8"]["percentile"] == pytest.approx(expected)
    assert result["top_markers"][0]["percentile"] >= result["top_markers"][-1]["percentile"]
    assert result["centroid"] == {"x": 96.0, "y": 96.0, "unit": "px"}


def test_the_four_grid_neighbours_are_32_um_away(session):
    # Cell 19 is row 2, column 2 of the 8x8 grid: four neighbours at 64 px.
    result = _ok(invoke(session, "explain_cell", {"project": "synth", "cell_id": 19, "k": 4,
                                                  "include_crop": False}))
    assert sorted(n["cell_id"] for n in result["neighbours"]) == [11, 18, 20, 27]
    assert all(n["distance"] == pytest.approx(32.0) and n["unit"] == "um"
               for n in result["neighbours"])
    within = _ok(invoke(session, "explain_cell", {"project": "synth", "cell_id": 19, "k": 32,
                                                  "within_um": 40, "include_crop": False}))
    assert len(within["neighbours"]) == 4


def test_region_membership_uses_the_polygon_not_its_box(session):
    _ok(invoke(session, "create_roi", {"project": "synth", "category": "Tumour",
                                       "points": TRIANGLE, "name": "wedge"}))
    inside = _ok(invoke(session, "explain_cell", {"project": "synth", "cell_id": 1,
                                                  "include_crop": False}))
    assert [(r["name"], r["category"]) for r in inside["regions"]] == [("wedge", "Tumour")]
    # (160, 160) is inside the triangle's bounding box but not the triangle.
    outside = _ok(invoke(session, "explain_cell", {"project": "synth", "cell_id": 19,
                                                   "include_crop": False}))
    assert outside["regions"] == []


def test_an_unknown_cell_names_the_id_column(session):
    result = invoke(session, "explain_cell", {"project": "synth", "cell_id": 4242})
    assert result["error"]["code"] == "invalid_input"
    assert result["error"]["detail"]["id_column"] == "CellID"


def test_the_crop_is_centred_on_the_cell_and_stored(session, tmp_path):
    result = _ok(invoke(session, "explain_cell", {"project": "synth", "cell_id": 19,
                                                  "marker": "CD8"}))
    bounds = result["crop"]["bounds_fullres"]
    assert bounds["x"] + bounds["width"] / 2 == pytest.approx(160, abs=2)
    assert bounds["y"] + bounds["height"] / 2 == pytest.approx(160, abs=2)
    assert result["crop"]["outline_drawn"] == "mask"
    art = result["crop"]["artifact"]["id"]
    assert (tmp_path / ".agent" / "artifacts" / "synth" / f"{art}.png").exists()
    assert result["_images"] and all("value" in n for n in result["neighbours"])


def test_it_is_refused_without_rendered_pixels_egress(session):
    policy = replace(Policy(), egress=frozenset({"metadata", "aggregates"}))
    refused = invoke(session, "explain_cell", {"project": "synth", "cell_id": 1},
                     policy=policy)
    assert refused["error"]["code"] == "permission_required"
