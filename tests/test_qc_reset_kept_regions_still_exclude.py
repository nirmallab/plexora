"""reset_qc keeps the regions the user made theirs. A region it keeps that
still sits in a QC category, named "QC exclude: ...", must still leave its
cells out afterwards: kept means kept as QC, not kept as a dead shape."""

import json

import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.plugins.qc.server import results
from tests.qc_fixtures import make_qc_project


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def ok(answer):
    assert answer["ok"], json.dumps(answer.get("error"), default=str)[:3000]
    return answer["result"]


def _box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                [x0, y0]]]}


def _qc_rois(session, project):
    return [r for r in ok(invoke(session, "list_rois", {"project": project}))["rois"]
            if str(r.get("category_id") or "").startswith("qc_")]


def test_a_hand_drawn_region_kept_by_reset_still_excludes_its_cells(tmp_path):
    """A region drawn by hand in a QC category survives reset_qc (it is the
    user's) and, after a refresh, its cells are still left out."""
    make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    made = ok(invoke(session, "create_roi", {"project": "qcsynth",
                                             "category": "QC: Tissue fold",
                                             "geometry": _box(200, 200, 500, 500)}))
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    assert results.active(results.load("qcsynth"))["cells"]["n_fail"] > 0
    reset = ok(invoke(session, "reset_qc", {"project": "qcsynth", "confirm": True}))
    assert made["roi"]["id"] in reset["kept_rois"]
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    found = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))
    assert [r["roi_id"] for r in found.get("regions") or []] == [made["roi"]["id"]], \
        "the kept hand-drawn region is no longer one of QC's regions"
    exclusions = ok(invoke(session, "get_qc_exclusions", {"project": "qcsynth"}))
    assert exclusions["applied"], exclusions
    assert sum(exclusions["by_reason"].values()) > 0, exclusions


def test_a_check_region_left_by_reset_is_not_an_exclude_that_excludes_nothing(tmp_path):
    """After write_registration_regions and reset_qc, every region still in
    the ROI document in a QC category (named "QC exclude: ...") still leaves
    its cells out; a region reset cannot keep as QC it deletes."""
    make_qc_project(tmp_path, artifacts=("misregistration",))
    session = AgentSession()
    ok(invoke(session, "compute_registration_mismatch", {"project": "qcsynth",
                                                         "comparison": "DNA_2"}))
    written = ok(invoke(session, "write_registration_regions", {"project": "qcsynth",
                                                                "comparison": "DNA_2"}))
    assert written["written"]
    reset = ok(invoke(session, "reset_qc", {"project": "qcsynth", "confirm": True}))
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    left = [r for r in _qc_rois(session, "qcsynth")
            if str(r.get("name") or "").startswith("QC exclude")]
    if not left:
        return  # reset deleted them: nothing claims to exclude
    exclusions = ok(invoke(session, "get_qc_exclusions", {"project": "qcsynth"}))
    assert exclusions["applied"], (
        f"reset kept {reset['kept_rois']} (deleted {reset['deleted_rois']}); "
        f"{[r['name'] for r in left]} remain but QC excludes nothing: {exclusions}")
    assert sum(exclusions["by_reason"].values()) > 0, exclusions
