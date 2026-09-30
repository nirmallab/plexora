"""The QC panel's routes: the user's own acts in the viewer, never licence-gated."""

import json

import pytest


@pytest.fixture
def client(tmp_path):
    import plexora
    from tests.qc_fixtures import make_qc_project

    make_qc_project(tmp_path, size=512, grid=20, artifacts=())
    return plexora.app.test_client()


def _post(client, path, body):
    return client.post(path, data=json.dumps(body))


def test_the_panel_reads_an_image_with_no_qc_yet(client):
    state = client.get("/plugins/qc/state?datasource=qcsynth").get_json()
    assert state["ok"] and state["result"] is None
    vocabulary = client.get("/plugins/qc/vocabulary").get_json()
    assert {c["id"] for c in vocabulary["classes"]} >= {"tissue_fold", "out_of_focus"}


def test_a_category_is_prepared_and_a_hand_drawn_region_flags_cells(client):
    from plexora.plugins.roi.server import service
    from plexora.agent import AgentSession

    made = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                     "class": "tissue_fold"}).get_json()
    assert made["ok"] and made["category_id"] == "qc_tissue_fold"
    ds = AgentSession().image_data("qcsynth")
    service.create_roi(ds, category=made["label"], points=[[50, 50], [250, 50], [250, 250],
                                                            [50, 250]])
    refreshed = _post(client, "/plugins/qc/refresh", {"datasource": "qcsynth"}).get_json()
    assert refreshed["ok"] and refreshed["sync"]["adopted"]
    state = client.get("/plugins/qc/state?datasource=qcsynth").get_json()
    assert state["regions"][0]["class"] == "tissue_fold"
    assert state["summary"]["cells"]["n_fail"] > 0
    csv_file = client.get("/plugins/qc/download/qcsynth?kind=cells.csv")
    assert csv_file.status_code == 200 and b"primary_reason" in csv_file.data
    strict = _post(client, "/plugins/qc/strictness", {"datasource": "qcsynth",
                                                      "preset": "strict"}).get_json()
    assert strict["ok"]
    refused = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                       "class": "not_a_class"})
    assert refused.status_code == 400


def test_a_custom_category_is_its_own_group_and_a_technical_artifact(client):
    """A QC category the user names is made once, listed on the project's
    vocabulary, drawn in like any class, grouped under its own name -- and a
    technical artifact to everything that reasons by class."""
    from plexora.plugins.roi.server import service
    from plexora.agent import AgentSession

    made = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                     "label": "  Pen   mark "}).get_json()
    assert made["ok"] and made["custom"] and made["key"] == "custom_pen_mark"
    assert made["category_id"] == "qc_custom_pen_mark" and made["label"] == "QC: Pen mark"
    again = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                      "label": "pen mark"}).get_json()
    assert again["category_id"] == made["category_id"] and again["words"] == "Pen mark"
    vocabulary = client.get("/plugins/qc/vocabulary?datasource=qcsynth").get_json()
    assert [c["id"] for c in vocabulary["custom"]] == ["custom_pen_mark"]
    assert client.get("/plugins/qc/vocabulary").get_json()["custom"] == []
    assert _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                     "label": " -- "}).status_code == 400

    ds = AgentSession().image_data("qcsynth")
    service.create_roi(ds, category=made["label"], points=[[50, 50], [250, 50], [250, 250]])
    assert _post(client, "/plugins/qc/refresh", {"datasource": "qcsynth"}).get_json()["ok"]
    region = client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["regions"][0]
    assert region["category"] == "custom_pen_mark" and region["category_words"] == "Pen mark"
    assert region["custom"] and region["class"] == "other_technical"
    assert region["color"] == made["color"] == region["default_color"]
    cells = client.get("/plugins/qc/cells?datasource=qcsynth").get_json()
    assert any(g["reason"] == "region:other_technical" for g in cells["groups"])

    recoloured = _post(client, "/plugins/qc/color", {"datasource": "qcsynth",
                                                     "class": "custom_pen_mark",
                                                     "color": "#123456"}).get_json()
    assert recoloured["ok"]
    assert client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["regions"][0][
        "color"] == "#123456"
    assert _post(client, "/plugins/qc/color", {"datasource": "qcsynth",
                                               "class": "custom_nothing_here",
                                               "color": "#123456"}).status_code == 400


def test_a_hand_drawn_region_keeps_the_channels_it_was_drawn_under(client):
    """The channels on screen when a region was drawn are kept with it -- the
    first time only -- and are what a click on it puts back."""
    from plexora.plugins.roi.server import service
    from plexora.agent import AgentSession

    made = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                     "class": "tissue_fold"}).get_json()
    ds = AgentSession().image_data("qcsynth")
    _, _, summary = service.create_roi(ds, category=made["label"],
                                       points=[[50, 50], [250, 50], [250, 250]])
    roi_id = summary["id"]
    seen = [{"name": "DAPI", "color": "#0000ff", "range": [10, 900]}, {"name": "CD3"}]
    taken = _post(client, "/plugins/qc/refresh", {"datasource": "qcsynth",
                                                  "views": {roi_id: seen}}).get_json()
    assert taken["ok"] and taken["sync"]["viewed"] == [roi_id]
    region = client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["regions"][0]
    assert region["evidence_channels"] == ["DAPI", "CD3"]
    assert region["view_channels"] == [{"name": "DAPI", "color": "#0000ff",
                                        "range": [10.0, 900.0]}, {"name": "CD3"}]

    later = _post(client, "/plugins/qc/refresh", {"datasource": "qcsynth", "views": {
        roi_id: [{"name": "CD8"}]}}).get_json()
    assert later["ok"] and "viewed" not in later["sync"]
    assert client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["regions"][0][
        "evidence_channels"] == ["DAPI", "CD3"]
    refused = _post(client, "/plugins/qc/refresh", {"datasource": "qcsynth", "views": {
        roi_id: [{"name": "DAPI", "color": "blue"}]}})
    assert refused.status_code == 400


def test_a_region_drawn_in_the_qc_panel_is_an_ordinary_roi(client):
    """Drawn from QC, never the ROI panel: stored through the ROI plugin as a
    region named like the ROI panel names one, taken into QC with the channels
    it was drawn under, and named on its cells as where they came from."""
    from plexora.plugins.roi.server.repository import ROIRepository

    square = [[50, 50], [250, 50], [250, 250], [50, 250]]
    drawn = _post(client, "/plugins/qc/regions/draw", {
        "datasource": "qcsynth", "class": "tissue_fold", "points": square,
        "views": [{"name": "DNA_1", "color": "#2388ff", "range": [400, 3300]}]}).get_json()
    assert drawn["ok"] and drawn["roi"]["name"] == "QC: Tissue fold 1"
    assert drawn["sync"]["adopted"] == [drawn["roi"]["id"]]
    assert drawn["sync"]["viewed"] == [drawn["roi"]["id"]]
    state = ROIRepository("qcsynth").load()
    assert any(c["id"] == "qc_tissue_fold" for c in state["categories"])

    second = _post(client, "/plugins/qc/regions/draw", {
        "datasource": "qcsynth", "label": "Pen mark",
        "points": [[300, 300], [400, 300], [400, 400]]}).get_json()
    assert second["ok"] and second["roi"]["name"] == "QC: Pen mark 1"
    assert second["category"]["key"] == "custom_pen_mark"

    regions = client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["regions"]
    fold = next(r for r in regions if r["roi_id"] == drawn["roi"]["id"])
    assert fold["created_by"] == "user" and fold["evidence_channels"] == ["DNA_1"]
    cells = client.get("/plugins/qc/cells?datasource=qcsynth").get_json()
    group = next(g for g in cells["groups"] if g["reason"] == "region:tissue_fold")
    assert group["derived_from"] == [{"roi_id": drawn["roi"]["id"],
                                      "name": "QC: Tissue fold 1"}]
    assert fold["name"] == "QC: Tissue fold 1"

    # Renamed from the QC panel: the ROI's own name, taken in at once.
    renamed = _post(client, "/plugins/qc/regions/rename", {
        "datasource": "qcsynth", "roi_id": drawn["roi"]["id"], "name": "Fold at the edge"})
    assert renamed.status_code == 200 and renamed.get_json()["ok"]
    regions = client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["regions"]
    fold = next(r for r in regions if r["roi_id"] == drawn["roi"]["id"])
    assert fold["name"] == "Fold at the edge" and fold["class"] == "tissue_fold"
    assert fold["action"] == "exclude"
    assert _post(client, "/plugins/qc/regions/rename", {
        "datasource": "qcsynth", "roi_id": drawn["roi"]["id"], "name": "  "}).status_code == 400

    for bad in ({"class": "tissue_fold", "points": [[1, 1], [2, 2]]},
                {"class": "not_a_class", "points": square},
                {"points": square}):
        assert _post(client, "/plugins/qc/regions/draw",
                     {"datasource": "qcsynth", **bad}).status_code == 400


def test_a_missing_session_is_404(client):
    missing = _post(client, "/plugins/qc/agent_session/qs_nope/control", {"action": "pause"})
    assert missing.status_code == 404


def test_the_panel_reads_regions_and_cells_to_draw(client):
    """What the panel draws on the tissue: outlines with a box to fit the view
    to, and the flagged cells grouped by reason and status with the box of
    their centroids."""
    from plexora.plugins.roi.server import service
    from plexora.agent import AgentSession

    nothing = client.get("/plugins/qc/cells?datasource=qcsynth").get_json()
    assert nothing["ok"] and nothing["available"] is False and nothing["groups"] == []
    assert client.get("/plugins/qc/regions?project=qcsynth").get_json()["regions"] == []
    assert client.get("/plugins/qc/regions").status_code == 400

    made = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                     "class": "tissue_fold"}).get_json()
    ds = AgentSession().image_data("qcsynth")
    service.create_roi(ds, category=made["label"], points=[[50, 50], [250, 50], [250, 250],
                                                            [50, 250]])
    assert _post(client, "/plugins/qc/refresh", {"datasource": "qcsynth"}).get_json()["ok"]

    regions = client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["regions"]
    assert len(regions) == 1
    region = regions[0]
    assert region["class"] == "tissue_fold" and region["action"] == "exclude"
    assert region["bbox"] == [50.0, 50.0, 250.0, 250.0]
    assert region["geometry"]["type"] == "Polygon" and region["color"].startswith("#")
    # Drawn by hand: no channel of its own, so a click shows the nuclear stain.
    assert region["evidence_channels"] == []
    display = client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["display"]
    assert display["colors"]["marker"].startswith("#") and len(
        display["colors"]["references"]) == 2

    cells = client.get("/plugins/qc/cells?datasource=qcsynth").get_json()
    assert cells["ok"] and cells["available"] and cells["has_positions"]
    fold = next(g for g in cells["groups"] if g["reason"] == "region:tissue_fold")
    assert fold["status"] == "fail" and fold["count"] == len(fold["ids"]) > 0
    assert fold["count"] == cells["n_fail"]
    assert fold["evidence_channels"] == []
    x0, y0, x1, y1 = fold["bbox"]
    assert 50 <= x0 <= x1 <= 250 and 50 <= y0 <= y1 <= 250

    # Flagging only, under the lenient-most reading: the same cells, now warned.
    approved = _post(client, "/plugins/qc/approve", {"datasource": "qcsynth",
                                                     "roi_id": region["roi_id"],
                                                     "action": "warn"}).get_json()
    assert approved["ok"]
    warned = client.get("/plugins/qc/cells?datasource=qcsynth").get_json()
    fold = next(g for g in warned["groups"] if g["reason"] == "region:tissue_fold")
    assert fold["status"] == "warn" and warned["n_fail"] == 0


def test_a_region_is_deleted_from_the_panel(client):
    from plexora.plugins.roi.server import service
    from plexora.agent import AgentSession

    made = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                     "class": "out_of_focus"}).get_json()
    ds = AgentSession().image_data("qcsynth")
    service.create_roi(ds, category=made["label"], points=[[10, 10], [90, 10], [90, 90]])
    _post(client, "/plugins/qc/refresh", {"datasource": "qcsynth"})
    roi_id = client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["regions"][0][
        "roi_id"]
    deleted = _post(client, "/plugins/qc/regions/delete", {"datasource": "qcsynth",
                                                           "roi_id": roi_id}).get_json()
    assert deleted["ok"] and roi_id in deleted["sync"]["deleted"]
    assert client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["regions"] == []
    assert _post(client, "/plugins/qc/regions/delete", {"datasource": "qcsynth"}).status_code \
        == 400


def test_each_cell_call_records_the_channels_it_was_made_on(tmp_path):
    """What a click on a cell reason shows is what the call recorded: the
    nuclear stain for counterstain and shape, the first and last nuclear
    cycles for cycle stability."""
    from plexora.agent import AgentSession
    from plexora.plugins.qc.server import strictness
    from plexora.plugins.qc.server.cells import calls
    from tests.qc_fixtures import make_qc_project

    make_qc_project(tmp_path, artifacts=("cycle_dropout",))
    ds = AgentSession().data("qcsynth")
    looked = {"low": {"verdict": "accept"}, "high": {"verdict": "accept"}}
    result = {"result_id": "qr_ev", "candidates": {}, "cycles": [
        {"index": 1, "channels": ["DNA_1", "CD3", "CD8"], "nuclear": "DNA_1"},
        {"index": 2, "channels": ["DNA_2", "CD20"], "nuclear": "DNA_2"}],
        "cells": {"modules": {n: {"available": True, "state": "decided", "decision": looked}
                              for n in ("counterstain_intensity", "segmentation_area",
                                        "cycle_stability")}}}
    _frame, _pairs, summary = calls.derive(ds, result, strictness.thresholds("strict"))
    evidence = summary["evidence"]
    assert evidence["cycle_loss"]["channels"] == ["DNA_1", "DNA_2"]
    assert evidence["cycle_loss"]["verdicts"] == {"low": "accept", "high": "accept"}
    for reason, entry in evidence.items():
        if entry.get("module") in ("counterstain_intensity", "segmentation_area"):
            assert entry["channels"] == ["DNA_1"], reason
    assert summary["segmentation_channel"] == "DNA_1"


def test_a_result_from_before_recorded_evidence_still_names_its_channels(monkeypatch):
    from plexora.plugins.qc.server import viewer_data

    monkeypatch.setattr(viewer_data, "_nuclear_pair", lambda ds, result: ("DNA1", "DNA4"))
    evidence = viewer_data._cell_evidence(None, {"cells": {"modules": {}}}, {
        "antibody_aggregate": ["CD16", "CD16", "CD20", "CD8", "CD4"]})
    assert evidence["counterstain_low"] == ["DNA1"] == evidence["morphology"]
    assert evidence["cycle_loss"] == ["DNA1", "DNA4"]
    assert evidence["region:antibody_aggregate"] == ["CD16", "CD20", "CD8"]


def test_a_class_and_a_reason_are_recoloured_from_the_panel(client):
    """A class's colour is its ROI category's -- the ROI panel follows, and so
    do the cells inside its regions; a reason's is kept in QC's store; null
    puts back the default."""
    from plexora.plugins.roi.server import service
    from plexora.plugins.roi.server.repository import ROIRepository
    from plexora.plugins.qc.server import results, schemas
    from plexora.agent import AgentSession

    made = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                     "class": "tissue_fold"}).get_json()
    ds = AgentSession().image_data("qcsynth")
    service.create_roi(ds, category=made["label"], points=[[50, 50], [250, 50], [250, 250],
                                                            [50, 250]])
    assert _post(client, "/plugins/qc/refresh", {"datasource": "qcsynth"}).get_json()["ok"]

    set_class = _post(client, "/plugins/qc/color", {"datasource": "qcsynth",
                                                    "class": "tissue_fold", "color": "#12AB34"})
    assert set_class.get_json() == {"ok": True, "class": "tissue_fold", "color": "#12ab34"}
    region = client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["regions"][0]
    assert region["color"] == "#12ab34"
    assert region["default_color"] == schemas.CLASS_COLORS["tissue_fold"]
    category = next(c for c in ROIRepository("qcsynth").load()["categories"]
                    if c["id"] == "qc_tissue_fold")
    assert category["color"] == "#12ab34"
    fold = next(g for g in client.get("/plugins/qc/cells?datasource=qcsynth").get_json()[
        "groups"] if g["reason"] == "region:tissue_fold")
    assert fold["color"] == "#12ab34"

    assert _post(client, "/plugins/qc/color", {"datasource": "qcsynth", "reason": "cycle_loss",
                                               "color": "#abcdef"}).get_json()["ok"]
    assert results.reason_colors("qcsynth") == {"cycle_loss": "#abcdef"}
    assert _post(client, "/plugins/qc/color", {"datasource": "qcsynth", "reason": "cycle_loss",
                                               "color": None}).get_json()["ok"]
    assert results.reason_colors("qcsynth") == {}
    assert _post(client, "/plugins/qc/color", {"datasource": "qcsynth",
                                               "class": "tissue_fold", "color": None}).get_json()[
        "color"] == schemas.CLASS_COLORS["tissue_fold"]

    for bad in ({"class": "tissue_fold", "color": "red"},
                {"reason": "region:tissue_fold", "color": "#000000"},
                {"class": "not_a_class", "color": "#000000"},
                {"class": "tissue_fold", "reason": "cycle_loss", "color": "#000000"}):
        assert _post(client, "/plugins/qc/color", {"datasource": "qcsynth", **bad}
                     ).status_code == 400


@pytest.mark.paid
def test_a_region_is_traced_from_the_panel(client):
    from plexora.agent import AgentSession, invoke, jobs
    from plexora.plugins.roi.server import service

    made = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                     "class": "saturation_or_clipping"}).get_json()
    session = AgentSession()
    ds = session.image_data("qcsynth")
    service.create_roi(ds, category=made["label"], points=[[60, 60], [200, 60], [200, 200]])
    _post(client, "/plugins/qc/refresh", {"datasource": "qcsynth"})
    roi_id = client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["regions"][0][
        "roi_id"]
    unscanned = _post(client, "/plugins/qc/regions/refine", {"datasource": "qcsynth",
                                                             "roi_id": roi_id})
    assert unscanned.status_code == 400
    assert unscanned.get_json()["error"]["code"] == "precondition_missing"
    assert invoke(session, "profile_image_qc", {"project": "qcsynth"})["ok"]
    jobs.drain(180)
    # Nothing is saturated in a clean scene: the region is left as drawn, and says why.
    answer = _post(client, "/plugins/qc/regions/refine", {"datasource": "qcsynth",
                                                          "roi_id": roi_id}).get_json()
    assert answer["ok"] and not answer["refined"]
    assert answer["skipped"][0]["roi_id"] == roi_id and answer["skipped"][0]["why"]
    every = _post(client, "/plugins/qc/regions/refine", {"datasource": "qcsynth", "all": True})
    assert every.status_code == 200
