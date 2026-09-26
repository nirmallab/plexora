"""ROI through the agent pipeline."""

import pytest

from plexora.agent import AgentSession, Policy, invoke, registry
from tests.agent_fixtures import make_synthetic_project

SQUARE = [[0, 0], [130, 0], [130, 130], [0, 130]]


@pytest.fixture
def session(tmp_path):
    make_synthetic_project(tmp_path)
    registry.discover(["roi"])
    return AgentSession()


def test_create_list_count_delete(session):
    created = invoke(session, "create_roi", {"project": "synth", "category": "Tumor",
                                             "points": SQUARE, "name": "corner"})
    assert created["ok"], created
    roi = created["result"]["roi"]
    assert created["result"]["receipt"]["undo_hint"]["tool"] == "delete_roi"
    listed = invoke(session, "list_rois", {"project": "synth"})["result"]
    assert listed["count"] == 1 and listed["revision"] == 1
    cells = invoke(session, "count_cells_in_roi", {"project": "synth", "roi_id": roi["id"]})
    assert cells["result"]["n_cells"] == 4

    refused = invoke(session, "delete_roi", {"project": "synth", "roi_id": roi["id"],
                                             "confirm": True})
    assert refused["error"]["code"] == "permission_required"
    deleted = invoke(session, "delete_roi", {"project": "synth", "roi_id": roi["id"],
                                             "confirm": True},
                     policy=Policy.from_flags(allow_destructive=True))
    assert deleted["ok"], deleted
    assert deleted["result"]["receipt"]["reversible"] is False
    assert invoke(session, "list_rois", {"project": "synth"})["result"]["count"] == 0


def test_an_edit_against_a_stale_revision_conflicts(session):
    invoke(session, "create_roi", {"project": "synth", "category": "Tumor", "points": SQUARE})
    stale = invoke(session, "create_roi", {"project": "synth", "category": "Tumor",
                                           "points": SQUARE, "base_revision": 0})
    assert stale["error"]["code"] == "conflict"
    assert stale["error"]["detail"]["current_revision"] == 1


def test_bad_geometry_is_invalid_input(session):
    result = invoke(session, "create_roi", {"project": "synth", "category": "Tumor",
                                            "points": [[0, 0], [1, 1]]})
    assert result["error"]["code"] == "invalid_input"
