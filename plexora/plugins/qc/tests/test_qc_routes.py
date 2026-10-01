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
    assert [c["id"] for c in vocabulary["categories"]] == [
        "blur_focus", "registration", "segmentation", "tissue_acquisition", "staining_signal"]
    assert all(c["help"] and c["color"].startswith("#") for c in vocabulary["categories"])
    assert next(c for c in vocabulary["classes"] if c["id"] == "tissue_fold")[
        "category"] == "tissue_acquisition"


def test_a_category_is_prepared_and_a_hand_drawn_region_flags_cells(client):
    from plexora.plugins.roi.server import service
    from plexora.agent import AgentSession

    made = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                     "category": "tissue_acquisition"}).get_json()
    assert made["ok"] and made["category_id"] == "qc_tissue_acquisition"
    assert made["label"] == "QC: Tissue / acquisition artifact"
    # A subtype names its category.
    assert _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                     "class": "tissue_fold"}).get_json()[
        "category_id"] == "qc_tissue_acquisition"
    ds = AgentSession().image_data("qcsynth")
    service.create_roi(ds, category=made["label"], points=[[50, 50], [250, 50], [250, 250],
                                                            [50, 250]])
    refreshed = _post(client, "/plugins/qc/refresh", {"datasource": "qcsynth"}).get_json()
    assert refreshed["ok"] and refreshed["sync"]["adopted"]
    state = client.get("/plugins/qc/state?datasource=qcsynth").get_json()
    # Drawn in the category with no subtype: the category's own class.
    assert state["regions"][0]["class"] == "tissue_artifact"
    assert state["regions"][0]["category"] == "tissue_acquisition"
    assert [c["category"] for c in state["categories"]][:5] == [
        "blur_focus", "registration", "segmentation", "tissue_acquisition", "staining_signal"]
    assert state["summary"]["cells"]["n_fail"] > 0
    csv_file = client.get("/plugins/qc/download/qcsynth?kind=cells.csv")
    assert csv_file.status_code == 200 and b"primary_reason" in csv_file.data
    strict = _post(client, "/plugins/qc/strictness", {"datasource": "qcsynth",
                                                      "preset": "strict"}).get_json()
    assert strict["ok"]
    for bad in ({"class": "not_a_class"}, {"category": "not_a_category"}):
        refused = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth", **bad})
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
                                                     "category": "tissue_acquisition"}).get_json()
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
    # A subtype typed into the picker: drawn in its category, the subtype kept.
    drawn = _post(client, "/plugins/qc/regions/draw", {
        "datasource": "qcsynth", "class": "tissue_fold", "points": square,
        "views": [{"name": "DNA_1", "color": "#2388ff", "range": [400, 3300]}]}).get_json()
    name = "QC: Tissue / acquisition artifact 1"
    assert drawn["ok"] and drawn["roi"]["name"] == name
    assert drawn["sync"]["adopted"] == [drawn["roi"]["id"]]
    assert drawn["sync"]["viewed"] == [drawn["roi"]["id"]]
    assert drawn["category"]["class"] == "tissue_fold"
    state = ROIRepository("qcsynth").load()
    assert any(c["id"] == "qc_tissue_acquisition" for c in state["categories"])
    assert not any(c["id"] == "qc_tissue_fold" for c in state["categories"])

    # One of the five: drawn as that category's own class.
    blurred = _post(client, "/plugins/qc/regions/draw", {
        "datasource": "qcsynth", "category": "blur_focus",
        "points": [[300, 50], [450, 50], [450, 200]]}).get_json()
    assert blurred["ok"] and blurred["roi"]["name"] == "QC: Blur / focus issue 1"

    second = _post(client, "/plugins/qc/regions/draw", {
        "datasource": "qcsynth", "label": "Pen mark",
        "points": [[300, 300], [400, 300], [400, 400]]}).get_json()
    assert second["ok"] and second["roi"]["name"] == "QC: Pen mark 1"
    assert second["category"]["key"] == "custom_pen_mark"

    regions = client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["regions"]
    fold = next(r for r in regions if r["roi_id"] == drawn["roi"]["id"])
    assert fold["created_by"] == "user" and fold["evidence_channels"] == ["DNA_1"]
    assert fold["class"] == "tissue_fold" and fold["category"] == "tissue_acquisition"
    assert fold["words"] == "tissue fold"
    blur = next(r for r in regions if r["roi_id"] == blurred["roi"]["id"])
    assert blur["class"] == "out_of_focus" and blur["category"] == "blur_focus"
    cells = client.get("/plugins/qc/cells?datasource=qcsynth").get_json()
    group = next(g for g in cells["groups"] if g["reason"] == "region:tissue_fold")
    assert group["derived_from"] == [{"roi_id": drawn["roi"]["id"], "name": name}]
    assert group["category"] == "tissue_acquisition"
    assert fold["name"] == name

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
                {"category": "not_a_category", "points": square},
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
                                                     "category": "tissue_acquisition"}).get_json()
    ds = AgentSession().image_data("qcsynth")
    service.create_roi(ds, category=made["label"], points=[[50, 50], [250, 50], [250, 250],
                                                            [50, 250]])
    assert _post(client, "/plugins/qc/refresh", {"datasource": "qcsynth"}).get_json()["ok"]

    regions = client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["regions"]
    assert len(regions) == 1
    region = regions[0]
    assert region["class"] == "tissue_artifact" and region["action"] == "exclude"
    assert region["category"] == "tissue_acquisition"
    assert region["tool"]["name"] == "user" and region["n_cells"] > 0
    assert region["bbox"] == [50.0, 50.0, 250.0, 250.0]
    assert region["geometry"]["type"] == "Polygon" and region["color"].startswith("#")
    # Drawn by hand: no channel of its own, so a click shows the nuclear stain.
    assert region["evidence_channels"] == []
    display = client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["display"]
    assert display["colors"]["marker"].startswith("#") and len(
        display["colors"]["references"]) == 2

    cells = client.get("/plugins/qc/cells?datasource=qcsynth").get_json()
    assert cells["ok"] and cells["available"] and cells["has_positions"]
    fold = next(g for g in cells["groups"] if g["reason"] == "region:tissue_artifact")
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
    fold = next(g for g in warned["groups"] if g["reason"] == "region:tissue_artifact")
    assert fold["status"] == "warn" and warned["n_fail"] == 0


def test_a_region_is_deleted_from_the_panel(client):
    from plexora.plugins.roi.server import service
    from plexora.agent import AgentSession

    made = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                     "category": "blur_focus"}).get_json()
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


def test_a_category_and_a_reason_are_recoloured_from_the_panel(client):
    """A category's colour is its ROI category's -- the ROI panel follows, and
    so do the cells inside its regions; a reason's is kept in QC's store;
    null puts back the default. A class names its category."""
    from plexora.plugins.roi.server import service
    from plexora.plugins.roi.server.repository import ROIRepository
    from plexora.plugins.qc.server import results, schemas
    from plexora.agent import AgentSession

    made = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                     "category": "tissue_acquisition"}).get_json()
    ds = AgentSession().image_data("qcsynth")
    service.create_roi(ds, category=made["label"], points=[[50, 50], [250, 50], [250, 250],
                                                            [50, 250]])
    assert _post(client, "/plugins/qc/refresh", {"datasource": "qcsynth"}).get_json()["ok"]

    set_category = _post(client, "/plugins/qc/color", {
        "datasource": "qcsynth", "category": "tissue_acquisition", "color": "#12AB34"})
    assert set_category.get_json() == {"ok": True, "category": "tissue_acquisition",
                                       "color": "#12ab34"}
    region = client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["regions"][0]
    assert region["color"] == "#12ab34"
    assert region["default_color"] == schemas.CATEGORY_COLORS["tissue_acquisition"]
    category = next(c for c in ROIRepository("qcsynth").load()["categories"]
                    if c["id"] == "qc_tissue_acquisition")
    assert category["color"] == "#12ab34"
    tissue = next(g for g in client.get("/plugins/qc/cells?datasource=qcsynth").get_json()[
        "groups"] if g["reason"] == "region:tissue_artifact")
    assert tissue["color"] == "#12ab34"

    assert _post(client, "/plugins/qc/color", {"datasource": "qcsynth", "reason": "cycle_loss",
                                               "color": "#abcdef"}).get_json()["ok"]
    assert results.reason_colors("qcsynth") == {"cycle_loss": "#abcdef"}
    assert _post(client, "/plugins/qc/color", {"datasource": "qcsynth", "reason": "cycle_loss",
                                               "color": None}).get_json()["ok"]
    assert results.reason_colors("qcsynth") == {}
    assert _post(client, "/plugins/qc/color", {"datasource": "qcsynth",
                                               "class": "tissue_fold", "color": None}).get_json()[
        "color"] == schemas.CATEGORY_COLORS["tissue_acquisition"]

    for bad in ({"category": "tissue_acquisition", "color": "red"},
                {"reason": "region:tissue_fold", "color": "#000000"},
                {"class": "not_a_class", "color": "#000000"},
                {"category": "tissue_acquisition", "reason": "cycle_loss",
                 "color": "#000000"}):
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


def _cell_at(client, x, y, radius=None, project="qcsynth"):
    query = f"/plugins/qc/cell_at?datasource={project}&x={x}&y={y}"
    if radius is not None:
        query += f"&radius={radius}"
    answer = client.get(query).get_json()
    assert answer["ok"], answer
    return answer


def _gaps(labels):
    """A background pixel 2-4 px from a cell, and the one furthest from any."""
    import numpy as np
    from scipy import ndimage

    distance = ndimage.distance_transform_edt(labels == 0)
    h, w = labels.shape
    inner = np.zeros_like(distance, dtype=bool)
    inner[20:h - 20, 20:w - 20] = True
    near = np.argwhere(inner & (distance >= 2) & (distance <= 4))
    far = np.unravel_index(int(np.argmax(distance)), distance.shape)
    return tuple(int(v) for v in near[0]), tuple(int(v) for v in far), distance


def test_the_hover_card_reads_the_cell_under_the_pointer(tmp_path):
    """The mask says which cell is under the pointer; the answer is QC's
    record of it -- the region it is in, how much of it, the reason -- or
    nothing on glass, and a pointer just off a cell finds it only when asked
    to look that far."""
    import plexora
    from plexora.agent import AgentSession
    from plexora.plugins.roi.server import service
    from tests.qc_fixtures import make_qc_project

    made = make_qc_project(tmp_path, size=512, grid=20, artifacts=())
    client = plexora.app.test_client()
    labels = made["labels"]
    category = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                         "category": "tissue_acquisition"})
    ds = AgentSession().image_data("qcsynth")
    service.create_roi(ds, category=category.get_json()["label"],
                       points=[[50, 50], [250, 50], [250, 250], [50, 250]])
    assert _post(client, "/plugins/qc/refresh", {"datasource": "qcsynth"}).get_json()["ok"]
    roi_id = client.get("/plugins/qc/regions?datasource=qcsynth").get_json()["regions"][0][
        "roi_id"]

    def on(cell):
        return labels[int(cell["y"]), int(cell["x"])] == cell["id"]

    inside = next(c for c in made["cells"] if 90 < c["x"] < 210 and 90 < c["y"] < 210
                  and on(c))
    answer = _cell_at(client, inside["x"], inside["y"])
    assert answer["method"] == "mask" and answer["result_id"]
    cell = answer["cell"]
    assert cell["cell_id"] == inside["id"] and cell["calls"] is True
    assert cell["action"] == "exclude" and cell["primary_reason"] == "region:tissue_artifact"
    first = cell["reasons"][0]
    assert first["status"] == "fail" and first["category"] == "tissue_acquisition"
    assert first["via_regions"][0]["roi_id"] == roi_id
    assert cell["regions"][0]["roi_id"] == roi_id
    assert cell["regions"][0]["category_words"]
    assert cell["regions"][0]["fraction"] == pytest.approx(1.0, abs=0.05)
    # The cell's own pixels come back with it, so the browser knows where it ends.
    import base64

    import numpy as np

    shape = answer["shape"]
    bx, by, bw, bh = shape["box"]
    bits = np.unpackbits(np.frombuffer(base64.b64decode(shape["bits"]), dtype=np.uint8))
    patch = bits[:bw * bh].reshape(bh, bw).astype(bool)
    assert np.array_equal(patch, labels[by:by + bh, bx:bx + bw] == inside["id"])

    outside = next(c for c in made["cells"] if c["x"] > 320 and c["y"] > 320 and on(c))
    clean = _cell_at(client, outside["x"], outside["y"])["cell"]
    assert clean["cell_id"] == outside["id"] and clean["pass"] and clean["reasons"] == []
    assert clean["markers"] == [] and clean["regions"] == []

    near, far, distance = _gaps(labels)
    y, x = near
    assert _cell_at(client, x + 0.5, y + 0.5, radius=0)["cell"] is None
    found = _cell_at(client, x + 0.5, y + 0.5, radius=6)["cell"]
    assert found is not None and found["cell_id"] > 0
    y, x = far
    assert distance[far] > 8
    assert _cell_at(client, x + 0.5, y + 0.5, radius=6)["cell"] is None
    assert _cell_at(client, -1, 10)["method"] == "out_of_bounds"
    assert _cell_at(client, 10, 600)["method"] == "out_of_bounds"
    assert client.get("/plugins/qc/cell_at?datasource=qcsynth&y=10").status_code == 400
    assert client.get("/plugins/qc/cell_at?datasource=qcsynth&x=a&y=10").status_code == 400
    assert client.get("/plugins/qc/cell_at?x=1&y=10").status_code == 400


def test_without_a_mask_the_hover_card_finds_the_nearest_centroid(tmp_path):
    """No mask: the nearest centroid within about a cell's radius; before QC
    has made any calls the cell is named, with a note that there are none."""
    import plexora
    from tests.qc_fixtures import make_qc_project

    made = make_qc_project(tmp_path, size=512, grid=20, artifacts=(), mask=False)
    client = plexora.app.test_client()
    cell = made["cells"][len(made["cells"]) // 2]
    answer = _cell_at(client, cell["x"] + 2, cell["y"] - 1)
    assert answer["method"] == "centroid"
    assert answer["cell"]["cell_id"] == cell["id"] and answer["cell"]["calls"] is False
    assert answer["cell"]["note"]
    _near, far, distance = _gaps(made["labels"])
    y, x = far
    assert _cell_at(client, x + 0.5, y + 0.5, radius=2)["cell"] is None
