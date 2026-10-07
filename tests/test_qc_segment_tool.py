"""`segment_qc_roi`: the agent outlines an object with magic select.

On a scene with a bright speck on the tissue, through `FakeSamBackend`: a
point on a picture the agent was shown becomes a snug QC region with the
agent's provenance; a preview writes nothing; union and subtract reshape it,
each undoable; a locked region is refused, and so is a server without the
model. (The glass speck is `tests/test_qc_visual_tools.py`'s: an outline on
the glass is refused.)
"""

import json

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.vision import sam, sam_backend
from plexora.vision import segment as segmenter

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])
    segmenter.forget()
    yield
    segmenter.forget()


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


@pytest.fixture
def scene(tmp_path):
    from tests.qc_fixtures import make_qc_project

    info = make_qc_project(tmp_path, size=512, grid=20, artifacts=("speck_tissue",))
    region = next(r for r in info["truth"]["regions"] if r["name"] == "speck_tissue")
    ys, xs = np.nonzero(region["mask"])
    info["speck"] = (float(xs.mean()), float(ys.mean()))
    return info


def test_a_point_on_a_picture_becomes_a_snug_region_with_the_agents_provenance(scene):
    session = AgentSession()
    cx, cy = scene["speck"]
    rendered = ok(invoke(session, "render_region", {
        "project": "qcsynth", "bounds": {"x": cx - 64, "y": cy - 64, "width": 128,
                                         "height": 128},
        "channels": [{"name": scene["channels"][0]}], "output": {"width": 256}}))
    art = rendered["artifact"]["id"]
    with sam.use_backend(sam_backend.FakeSamBackend()):
        preview = invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "points": [{"artifact_id": art, "px": [128, 128]}],
            "artifact_class": "debris_or_foreign_object", "preview": True})
        assert ok(preview)["written"] is False and ok(preview)["preview"] is True
        assert ok(invoke(session, "list_rois", {"project": "qcsynth"}))["count"] == 0
        made = ok(invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "points": [{"artifact_id": art, "px": [128, 128]}],
            "artifact_class": "debris_or_foreign_object", "notes": "a bright speck"}))
    assert made["written"] and made["class"] == "debris_or_foreign_object"
    roi = ok(invoke(session, "get_roi", {"project": "qcsynth", "roi_id": made["roi_id"]}))["roi"]
    assert roi["method"] == "sam"
    left, top, right, bottom = roi["bounds"]
    # The picture's centre is the speck: the outline sits round it, snugly.
    assert left <= cx <= right and top <= cy <= bottom
    assert (right - left) < 60 and (bottom - top) < 60
    results = ok(invoke(session, "get_qc_results", {"project": "qcsynth",
                                                    "include_regions": True}))
    region = next(r for r in results["regions"] if r["roi_id"] == made["roi_id"])
    assert region["method"] == "sam_agent"
    assert region["origin"] == "agent"
    assert made["receipt"]["undo_hint"]["tool"] == "delete_roi"


def test_union_and_subtract_reshape_a_region_and_a_locked_one_is_refused(scene):
    session = AgentSession()
    cx, cy = scene["speck"]
    with sam.use_backend(sam_backend.FakeSamBackend()):
        made = ok(invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "points": [{"x": cx, "y": cy, "label": 1}],
            "artifact_class": "debris_or_foreign_object"}))
        roi_id = made["roi_id"]
        before = ok(invoke(session, "get_roi", {"project": "qcsynth", "roi_id": roi_id}))[
            "roi"]["bounds"]
        grown = ok(invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "roi_id": roi_id, "mode": "union",
            "box": {"x": cx - 40, "y": cy - 40, "width": 30, "height": 30}}))
        assert grown["written"] and grown["mode"] == "union"
        after = ok(invoke(session, "get_roi", {"project": "qcsynth", "roi_id": roi_id}))[
            "roi"]["bounds"]
        assert after[0] < before[0] and after[1] < before[1]
        assert grown["receipt"]["undo_hint"]["tool"] == "update_roi"
        ok(invoke(session, "update_roi", {"project": "qcsynth", "roi_id": roi_id,
                                          "locked": True}))
        ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
        refused = invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "roi_id": roi_id, "mode": "subtract",
            "points": [{"x": cx, "y": cy, "label": 1}]})
    assert not refused["ok"] and "locked" in refused["error"]["message"]


def test_bad_requests_are_refused_in_words(scene):
    session = AgentSession()
    cx, cy = scene["speck"]
    with sam.use_backend(sam_backend.FakeSamBackend()):
        nothing = invoke(session, "segment_qc_roi", {"project": "qcsynth",
                                                     "artifact_class": "tissue_fold"})
        no_class = invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "points": [{"x": cx, "y": cy}]})
        no_roi = invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "points": [{"x": cx, "y": cy}], "mode": "replace"})
        stranger = invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "points": [{"artifact_id": "art_0000000000000000", "px": [1, 1]}],
            "artifact_class": "tissue_fold"})
    assert "point or a box" in nothing["error"]["message"]
    assert "artifact_class" in no_class["error"]["message"]
    assert "roi_id" in no_roi["error"]["message"]
    assert not stranger["ok"]


def test_without_the_model_the_tool_says_it_is_not_set_up(scene, monkeypatch, tmp_path):
    monkeypatch.setenv("PLEXORA_SEGMENT_MODEL_DIR", str(tmp_path / "none"))
    cx, cy = scene["speck"]
    refused = invoke(AgentSession(), "segment_qc_roi", {
        "project": "qcsynth", "points": [{"x": cx, "y": cy}],
        "artifact_class": "debris_or_foreign_object"})
    assert not refused["ok"] and refused["error"]["code"] == "capability_unavailable"
    assert "plexora ai segment install" in refused["error"]["message"]


def test_refine_with_method_sam_needs_the_model(scene, monkeypatch, tmp_path):
    monkeypatch.setenv("PLEXORA_SEGMENT_MODEL_DIR", str(tmp_path / "none"))
    refused = invoke(AgentSession(), "refine_qc_roi", {"project": "qcsynth", "all": True,
                                                       "method": "sam"})
    assert not refused["ok"] and refused["error"]["code"] == "capability_unavailable"


@pytest.fixture
def glass_scene(tmp_path):
    """The speck on the glass: the tracer's debris trace is measured against
    the glass, which is what tightening from the region alone rests on."""
    from tests.qc_fixtures import make_qc_project

    info = make_qc_project(tmp_path, size=512, grid=20, artifacts=("speck",))
    region = next(r for r in info["truth"]["regions"] if r["name"] == "speck")
    ys, xs = np.nonzero(region["mask"])
    info["speck"] = (float(xs.mean()), float(ys.mean()))
    return info


def test_a_loose_region_is_tightened_from_itself_with_no_points(glass_scene):
    from plexora.agent import jobs

    session = AgentSession()
    cx, cy = glass_scene["speck"]
    loose = {"type": "Polygon", "coordinates": [[[cx - 45, cy - 45], [cx + 45, cy - 45],
                                                 [cx + 45, cy + 45], [cx - 45, cy + 45],
                                                 [cx - 45, cy - 45]]]}
    made = ok(invoke(session, "create_roi", {"project": "qcsynth",
                                             "category": "QC: Debris or foreign object",
                                             "geometry": loose}))
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    with sam.use_backend(sam_backend.FakeSamBackend()):
        unscanned = invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "roi_id": made["roi"]["id"], "mode": "replace",
            "force": True})
        assert unscanned["error"]["code"] == "precondition_missing"
        ok(invoke(session, "profile_image_qc", {"project": "qcsynth"}))
        jobs.drain(180)
        tight = ok(invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "roi_id": made["roi"]["id"], "mode": "replace",
            "force": True}))
    assert tight["written"] and tight["started_from_roi"], tight
    left, top, right, bottom = ok(invoke(session, "get_roi", {
        "project": "qcsynth", "roi_id": made["roi"]["id"]}))["roi"]["bounds"]
    # Tighter than the region it started from (the real model's fit is
    # checked in test_vision_sam_real; the stand-in's scores mean nothing).
    assert (right - left) < 90 and (bottom - top) < 90
