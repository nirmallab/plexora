"""A frame column mapped on a still image does not make a cell's picture ask
for a frame the image has not got.

The edit page offers every role, so a table can map a `t` column on a planar
image. render_cell_gallery and explain_cell draw each cell at its row's frame
only when the image has frames; on a still image they draw the image itself,
as they did before frames were drawn at all.
"""

import json
from pathlib import Path

import pytest

from plexora.agent import AgentSession, invoke, registry
from tests.agent_fixtures import make_synthetic_project


@pytest.fixture
def info(tmp_path):
    made = make_synthetic_project(tmp_path)
    csv = Path(made["csv_path"])
    lines = csv.read_text(encoding="utf-8").splitlines()
    rows = [lines[0] + ",Frame"] + [line + f",{i % 2}" for i, line in enumerate(lines[1:])]
    csv.write_text("\n".join(rows) + "\n", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["synth"]["dataset"]["roles"]["t"] = "Frame"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return made


@pytest.fixture
def session(info):
    registry.discover(["gating"])
    return AgentSession()


def test_gallery_draws_a_still_image_with_a_frame_column(session):
    result = invoke(session, "render_cell_gallery", {
        "project": "synth", "marker": "CD8", "select": "brightest", "n": 6})
    assert result["ok"], result
    manifest = result["result"]["manifest"]
    assert manifest["tiles"] and "frames" not in manifest
    assert all("t" not in tile for tile in manifest["tiles"])


def test_explain_cell_draws_a_still_image_with_a_frame_column(session, info):
    # The second row is frame 1's.
    cell_id = info["cells"][1]["id"]
    result = invoke(session, "explain_cell", {"project": "synth", "cell_id": cell_id})
    assert result["ok"], result
