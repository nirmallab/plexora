"""A QC session closed after the user drew a QC region keeps that region.

The user's regions win: a region drawn by hand in a QC category excludes its
cells, and an AI QC session started and closed afterwards must not quietly
drop it from the cell calls (or from what `get_qc_results` reports) just
because the session's result became the active one.
"""

import json

import pytest

from plexora.agent import AgentSession, invoke, registry
from tests.qc_fixtures import make_qc_project
from tests.test_qc_session import QCOracle, drive, start

pytestmark = pytest.mark.pro


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def _box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                [x0, y0]]]}


def test_a_hand_drawn_region_still_excludes_its_cells_after_a_session_closes(tmp_path):
    from plexora.plugins.qc.server import results

    info = make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    made = ok(invoke(session, "create_roi", {"project": "qcsynth",
                                             "category": "QC: Tissue fold",
                                             "geometry": _box(200, 200, 500, 500)}))
    roi_id = made["roi"]["id"]
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    cells = results.cells("qcsynth")
    failed_before = set(cells.filter(~cells["pass"])["cell_id"].to_list())
    assert failed_before, "the hand-drawn fold should fail the cells inside it"

    started = start(session)
    drive(session, started["session_id"], QCOracle(info))
    ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"],
                                             "action": "close"}))
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))

    # The ROI itself is untouched in the ROI panel ...
    rois = ok(invoke(session, "list_rois", {"project": "qcsynth"}))["rois"]
    assert roi_id in {r["id"] for r in rois}
    # ... so QC must still count it: its cells still failing, and listed as
    # the user's region.
    cells = results.cells("qcsynth")
    failed_after = set(cells.filter(~cells["pass"])["cell_id"].to_list())
    assert failed_before <= failed_after, \
        (len(failed_before), len(failed_after), len(failed_before - failed_after))
    found = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))
    regions = found.get("regions") or []
    assert any(r.get("roi_id") == roi_id and r.get("created_by") == "user"
               for r in regions), \
        [(r.get("roi_id"), r.get("class"), r.get("created_by")) for r in regions]
