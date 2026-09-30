"""`refine_qc_roi`: retrace a QC region at pixel level inside its outline.

A box drawn by hand round a fold becomes the fold's own band, receipted and
undoable; a region the user locked is theirs until they say `force`; and
nothing is traced before the image has been scanned.
"""

import json

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from plexora.plugins.qc.server import polygons
from tests.qc_fixtures import make_qc_project

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def _box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                [x0, y0]]]}


def _fold_box(info, half=110):
    region = next(r for r in info["truth"]["regions"] if r["name"] == "fold")
    ys, xs = np.nonzero(region["mask"])
    cx, cy = int(np.median(xs)), int(np.median(ys))
    return _box(cx - half, cy - half, cx + half, cy + half)


def _scanned(session, project="qcsynth"):
    ok(invoke(session, "profile_image_qc", {"project": project}))
    jobs.drain(180)


def _geometry(session, roi_id, project="qcsynth"):
    from plexora.plugins.roi.server import geometry as geometry_rules
    from plexora.plugins.roi.server import service

    return service.get_roi(session.image_data(project), roi_id,
                           max_vertices=geometry_rules.MAX_VERTICES)[1]["geometry"]


def test_a_hand_drawn_box_is_traced_to_the_fold_and_undo_puts_it_back(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("fold",))
    session = AgentSession()
    box = _fold_box(info)
    made = ok(invoke(session, "create_roi", {"project": "qcsynth",
                                             "category": "QC: Tissue fold", "geometry": box}))
    roi_id = made["roi"]["id"]
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    _scanned(session)
    traced = ok(invoke(session, "refine_qc_roi", {"project": "qcsynth", "roi_id": roi_id}))
    assert [r["roi_id"] for r in traced["refined"]] == [roi_id], traced
    assert traced["refined"][0]["method"] == "diffuse_bright"
    assert 0 < traced["refined"][0]["kept_fraction"] < 0.9
    now = _geometry(session, roi_id)
    assert polygons.area_of(now) < polygons.area_of(box)
    found = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))
    region = next(r for r in found["regions"] if r["roi_id"] == roi_id)
    assert region["refinement"]["status"] == "refined"
    assert not region["user"].get("edited"), "a retrace is QC's shape, not an edit"
    receipt = traced["receipts"][0]
    assert receipt["undo_hint"]["tool"] == "update_roi"
    ok(invoke(session, "update_roi", receipt["undo_hint"]["arguments"]))
    assert polygons.area_of(_geometry(session, roi_id)) == pytest.approx(
        polygons.area_of(box))


def test_a_locked_region_is_never_retraced_and_an_edited_one_only_with_force(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("fold",))
    session = AgentSession()
    box = _fold_box(info)
    locked = ok(invoke(session, "create_roi", {"project": "qcsynth",
                                               "category": "QC: Tissue fold",
                                               "geometry": box}))["roi"]["id"]
    ok(invoke(session, "update_roi", {"project": "qcsynth", "roi_id": locked, "locked": True}))
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    _scanned(session)
    refused = invoke(session, "refine_qc_roi", {"project": "qcsynth", "roi_id": locked,
                                                "force": True})
    assert not refused["ok"] and "unlock" in refused["error"]["message"]
    every = ok(invoke(session, "refine_qc_roi", {"project": "qcsynth", "all": True}))
    assert not every["refined"] and every["skipped"][0]["why"] == "it is locked"
    # Unlocked, traced, then reshaped by the user: theirs until `force`.
    ok(invoke(session, "update_roi", {"project": "qcsynth", "roi_id": locked, "locked": False}))
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    assert ok(invoke(session, "refine_qc_roi", {"project": "qcsynth", "roi_id": locked}))[
        "refined"]
    ok(invoke(session, "update_roi", {"project": "qcsynth", "roi_id": locked, "geometry": box}))
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    edited = invoke(session, "refine_qc_roi", {"project": "qcsynth", "roi_id": locked})
    assert not edited["ok"] and "force" in edited["error"]["message"]
    forced = ok(invoke(session, "refine_qc_roi", {"project": "qcsynth", "roi_id": locked,
                                                  "force": True}))
    assert forced["refined"]


def test_nothing_is_traced_before_the_image_is_scanned(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("fold",))
    session = AgentSession()
    made = ok(invoke(session, "create_roi", {"project": "qcsynth",
                                             "category": "QC: Tissue fold",
                                             "geometry": _fold_box(info)}))
    roi_id = made["roi"]["id"]
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    answer = invoke(session, "refine_qc_roi", {"project": "qcsynth", "roi_id": roi_id})
    assert not answer["ok"] and answer["error"]["code"] == "precondition_missing"
    neither = invoke(session, "refine_qc_roi", {"project": "qcsynth"})
    assert not neither["ok"] and neither["error"]["code"] == "invalid_input"
