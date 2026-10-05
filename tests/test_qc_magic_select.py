"""Magic select's provenance and stored views in the ROI and QC stores, the
QC routes the panel calls, and the tracer's model method.

The panel's own acts (draw, reshape) are Free and never need the model; the
tracer test drives the model method with a stand-in prediction so its guards
and its competition with the classical trace are what is tested.
"""

import json

import numpy as np
import pytest

from plexora.agent import AgentSession
from plexora.vision import sam, sam_backend
from plexora.vision import segment as segmenter


@pytest.fixture(autouse=True)
def _clean():
    segmenter.forget()
    yield
    segmenter.forget()


def _square(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                [x0, y0]]]}


# -- the ROI store ---------------------------------------------------------------


def test_flags_keep_how_a_shape_was_drawn_and_nothing_else():
    from plexora.plugins.roi.server import schema

    assert schema.normalize_flags({"method": "sam", "junk": 1}) == {
        "self_intersecting": False, "method": "sam"}
    assert schema.normalize_flags({"method": "laser"}) == {"self_intersecting": False}
    assert schema.normalize_flags(None) == {"self_intersecting": False}


def test_a_reshape_keeps_the_method_unless_it_names_another(tmp_path):
    from plexora.plugins.roi.server import service
    from tests.qc_fixtures import make_qc_project

    make_qc_project(tmp_path, size=256, grid=10, artifacts=(), table=False, mask=False)
    ds = AgentSession().image_data("qcsynth")
    _b, _a, made = service.create_roi(ds, category="Tumour", geometry=_square(10, 10, 60, 60),
                                      method="sam")
    assert made["method"] == "sam"
    from plexora.plugins.roi.server.repository import ROIRepository

    repo = ROIRepository(ds.name)
    repo.apply(repo.load()["revision"], [{"op": "roi.update_geometry", "id": made["id"],
                                          "geometry": _square(10, 10, 70, 70),
                                          "flags": {"self_intersecting": False}}])
    assert service.get_roi(ds, made["id"])[1]["method"] == "sam"
    service.update_roi(ds, made["id"], geometry=_square(5, 5, 70, 70), method="freehand")
    assert service.get_roi(ds, made["id"])[1]["method"] == "freehand"


# -- the QC routes -----------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    import plexora
    from tests.qc_fixtures import make_qc_project

    make_qc_project(tmp_path, size=512, grid=20, artifacts=())
    return plexora.app.test_client()


def _post(client, path, body):
    return client.post(path, data=json.dumps(body))


VIEW = {"sample": "qcsynth", "viewport": {"x": 40, "y": 50, "width": 300, "height": 200},
        "zoom": 1.5, "hd_mode": True, "captured_at": "2026-10-04T12:00:00Z",
        "channels": [{"name": "DNA_1", "color": "#0000ff", "range": [100, 4000], "visible": True},
                     {"name": "CD8", "color": "#00ff00", "visible": True}]}


def test_vocabulary_says_regions_take_a_whole_view(client):
    assert client.get("/plugins/qc/vocabulary").get_json()["views_format"] == 1


def test_a_magic_outline_drawn_in_qc_keeps_its_method_and_its_view(client):
    geometry = {"type": "Polygon", "coordinates": [
        [[60, 60], [200, 60], [200, 200], [60, 200], [60, 60]],
        [[100, 100], [140, 100], [140, 140], [100, 140], [100, 100]]]}   # a hole, kept
    drawn = _post(client, "/plugins/qc/regions/draw", {
        "datasource": "qcsynth", "category": "tissue_acquisition", "geometry": geometry,
        "method": "sam", "views": VIEW}).get_json()
    assert drawn["ok"], drawn
    roi_id = drawn["roi"]["id"]
    assert drawn["roi"]["method"] == "sam"
    region = next(r for r in client.get("/plugins/qc/regions?datasource=qcsynth")
                  .get_json()["regions"] if r["roi_id"] == roi_id)
    assert region["method"] == "sam"
    assert region["view"]["viewport"] == VIEW["viewport"]
    assert region["view"]["hd_mode"] is True and region["view"]["zoom"] == 1.5
    # The channel list older readers use mirrors the view's visible channels.
    assert [v["name"] for v in region["view_channels"]] == ["DNA_1", "CD8"]
    assert region["view_channels"][0]["range"] == [100, 4000]
    # A second refresh never replaces the view it was drawn under.
    _post(client, "/plugins/qc/refresh", {"datasource": "qcsynth", "views": {
        roi_id: {**VIEW, "viewport": {"x": 0, "y": 0, "width": 10, "height": 10}}}})
    again = next(r for r in client.get("/plugins/qc/regions?datasource=qcsynth")
                 .get_json()["regions"] if r["roi_id"] == roi_id)
    assert again["view"]["viewport"] == VIEW["viewport"]


def test_a_hand_drawn_region_still_takes_a_channel_list(client):
    drawn = _post(client, "/plugins/qc/regions/draw", {
        "datasource": "qcsynth", "category": "tissue_acquisition",
        "points": [[20, 20], [120, 20], [120, 120], [20, 120]],
        "views": [{"name": "DNA_1", "color": "#0000ff"}]}).get_json()
    region = next(r for r in client.get("/plugins/qc/regions?datasource=qcsynth")
                  .get_json()["regions"] if r["roi_id"] == drawn["roi"]["id"])
    assert region["method"] == "freehand" and region["view"] is None
    assert region["view_channels"] == [{"name": "DNA_1", "color": "#0000ff"}]


def test_reshape_marks_the_region_the_users_and_refuses_a_locked_one(client):
    drawn = _post(client, "/plugins/qc/regions/draw", {
        "datasource": "qcsynth", "category": "tissue_acquisition",
        "points": [[20, 20], [120, 20], [120, 120], [20, 120]]}).get_json()
    roi_id = drawn["roi"]["id"]
    reshaped = _post(client, "/plugins/qc/regions/reshape", {
        "datasource": "qcsynth", "roi_id": roi_id, "geometry": _square(20, 20, 160, 140),
        "method": "sam"}).get_json()
    assert reshaped["ok"] and reshaped["roi"]["method"] == "sam"
    region = next(r for r in client.get("/plugins/qc/regions?datasource=qcsynth")
                  .get_json()["regions"] if r["roi_id"] == roi_id)
    assert region["bbox"][2] == pytest.approx(160)
    from plexora.plugins.roi.server import service

    service.update_roi(AgentSession().image_data("qcsynth"), roi_id, locked=True)
    refused = _post(client, "/plugins/qc/regions/reshape", {
        "datasource": "qcsynth", "roi_id": roi_id, "geometry": _square(0, 0, 50, 50),
        "method": "sam"})
    assert refused.status_code in (400, 409)
    assert not refused.get_json()["ok"]


def test_draw_without_points_or_geometry_is_a_bad_request(client):
    assert client.post("/plugins/qc/regions/draw", data=json.dumps(
        {"datasource": "qcsynth", "category": "tissue_acquisition"})).status_code == 400


# -- provenance ----------------------------------------------------------------------


def test_method_of_says_how_each_outline_was_made():
    from plexora.plugins.qc.server import provenance

    assert provenance.method_of({"method": "sam", "created_by": "user"}) == "sam"
    assert provenance.method_of({"detector": "fold", "refinement": {
        "status": "refined", "method": "sam"}}) == "sam_agent"
    assert provenance.method_of({"detector": "fold", "refinement": {
        "status": "refined", "method": "diffuse_bright"}}) == "traced"
    assert provenance.method_of({"detector": "blur", "trace": "map"}) == "map"
    assert provenance.method_of({"detector": "fold"}) == "envelope"
    assert provenance.method_of({"detector": "user"}) is None
    # A user's reshape wins over the tracer's record.
    assert provenance.method_of({"method": "sam", "user_state": {"edited": True},
                                 "refinement": {"status": "refined",
                                                "method": "bright_compact"}}) == "sam"
    vocabulary = provenance.vocabulary()
    assert "sam_agent" in vocabulary["methods"] and "agent" in vocabulary["origins"]
    assert "method" in provenance.FINDINGS_COLUMNS


# -- the tracer's model method ---------------------------------------------------------


def _dark_scene(tmp_path):
    from tests.test_qc_refine import _best, _envelope, _scene

    info, session, result, built = _scene(tmp_path, ("dark_region",))
    region = next(r for r in info["truth"]["regions"] if r["name"] == "dark_region")
    candidate = _best(built, result, region, ("tissue_damage_or_detachment",))
    candidate.class_hint = region["class"]
    mask, geometry = _envelope(candidate, result)
    return info, session, result, candidate, mask, geometry, region


def _truth_predictor(monkeypatch, truth, *, shrink=0):
    """sam.predict -> the painted truth inside the job's crop (optionally eroded)."""
    from plexora.plugins.qc.server import refine_sam

    captured = {}
    original_prepare = refine_sam.prepare

    def prepare(*args, **kwargs):
        job = original_prepare(*args, **kwargs)
        captured["job"] = job
        return job

    def predict_batch(embedding, prompts, *, mask_input=None, max_fraction=None):
        return [predict(embedding, p, l, box=b) for p, l, b in prompts]

    def predict(embedding, points, labels, *, box=None, mask_input=None, max_fraction=None):
        import cv2

        job = captured["job"]
        h, w = job.rgb.shape[:2]
        fx, fy = job.factor
        ys = ((np.arange(h) + job.origin[1] + 0.5) * fy).astype(int).clip(0, truth.shape[0] - 1)
        xs = ((np.arange(w) + job.origin[0] + 0.5) * fx).astype(int).clip(0, truth.shape[1] - 1)
        mask = truth[np.ix_(ys, xs)]
        if shrink:
            mask = cv2.erode(mask.astype(np.uint8), np.ones((3, 3), np.uint8),
                             iterations=shrink).astype(bool)
        return sam.Prediction(mask=mask, iou=0.95, low_res=np.zeros((1, 256, 256), np.float32),
                              index=0)

    monkeypatch.setattr(refine_sam, "prepare", prepare)
    monkeypatch.setattr(sam, "predict", predict)
    monkeypatch.setattr(sam, "predict_batch", predict_batch)
    monkeypatch.setattr(sam, "embed", lambda rgb: None)


def test_without_the_model_the_tracer_is_exactly_the_classical_one(tmp_path):
    from plexora.plugins.qc.server import refine, refine_sam
    from plexora.server.utils import source_image

    info, session, result, candidate, mask, geometry, _region = _dark_scene(tmp_path)
    assert not refine_sam.available()
    traced = refine_sam.trace(candidate, mask, result, session.image_data(info["name"]),
                              pixel_um=1.0, envelope=geometry)
    with source_image.SHELF.reader(session.image_data(info["name"])) as source:
        classical = refine.refine(candidate, mask, result, source, pixel_um=1.0,
                                  envelope=geometry)
    assert traced.method == classical.method != "sam"
    assert traced.geometry == classical.geometry


def test_the_models_outline_wins_when_it_agrees_and_is_tighter(tmp_path, monkeypatch):
    from plexora.plugins.qc.server import refine_sam

    info, session, result, candidate, mask, geometry, region = _dark_scene(tmp_path)
    _truth_predictor(monkeypatch, region["mask"])
    with sam.use_backend(sam_backend.FakeSamBackend()):
        traced = refine_sam.trace(candidate, mask, result, session.image_data(info["name"]),
                                  pixel_um=1.0, envelope=geometry)
    assert traced.method == "sam" and traced.status == "refined", traced.to_record()
    record = traced.to_record()
    assert record["params"]["model"] == "mobile_sam"
    assert record["params"]["alternative"]["method"] == "dark"
    assert record["guards"]["agrees_with_classical"]["ok"]
    from shapely.geometry import shape

    assert shape(geometry).buffer(1e-6).contains(shape(traced.geometry))


def test_an_outline_that_disagrees_with_a_good_classical_trace_loses(tmp_path, monkeypatch):
    from plexora.plugins.qc.server import refine_sam

    info, session, result, candidate, mask, geometry, region = _dark_scene(tmp_path)
    corner = np.zeros_like(region["mask"])
    ys, xs = np.nonzero(region["mask"])
    corner[ys.min():ys.min() + 12, xs.min():xs.min() + 12] = True
    _truth_predictor(monkeypatch, corner)
    with sam.use_backend(sam_backend.FakeSamBackend()):
        traced = refine_sam.trace(candidate, mask, result, session.image_data(info["name"]),
                                  pixel_um=1.0, envelope=geometry)
    assert traced.method != "sam"
    assert traced.params["sam"]["status"] == "rejected"


def _blur_scene(tmp_path):
    from tests.test_qc_refine import _best, _envelope, _scene

    info, session, result, built = _scene(tmp_path, ("blur_local",))
    region = next(r for r in info["truth"]["regions"] if r["name"] == "blur_local")
    candidate = _best(built, result, region, ("out_of_focus",))
    candidate.class_hint = "out_of_focus"
    mask, geometry = _envelope(candidate, result)
    return info, session, result, candidate, mask, geometry, region


def test_a_blurred_patch_takes_the_models_outline_when_it_agrees(tmp_path, monkeypatch):
    from shapely.geometry import shape

    from plexora.plugins.qc.server import refine_sam

    assert "out_of_focus" in refine_sam.SAM_CLASSES
    info, session, result, candidate, mask, geometry, region = _blur_scene(tmp_path)
    _truth_predictor(monkeypatch, region["mask"])
    with sam.use_backend(sam_backend.FakeSamBackend()):
        traced = refine_sam.trace(candidate, mask, result, session.image_data(info["name"]),
                                  pixel_um=1.0, envelope=geometry)
    record = traced.to_record()
    assert traced.method == "sam" and traced.status == "refined", record
    assert record["params"]["alternative"]["method"] == "blur"
    assert shape(geometry).buffer(1e-6).contains(shape(traced.geometry))


def test_a_blur_outline_that_disagrees_with_the_focus_trace_loses(tmp_path, monkeypatch):
    from plexora.plugins.qc.server import refine, refine_sam
    from plexora.server.utils import source_image

    info, session, result, candidate, mask, geometry, region = _blur_scene(tmp_path)
    with source_image.SHELF.reader(session.image_data(info["name"])) as source:
        classical = refine.refine(candidate, mask, result, source, pixel_um=1.0,
                                  envelope=geometry)
    assert classical.status == "refined", classical.to_record()
    corner = np.zeros_like(region["mask"])
    ys, xs = np.nonzero(region["mask"])
    corner[ys.min():ys.min() + 12, xs.min():xs.min() + 12] = True
    _truth_predictor(monkeypatch, corner)
    with sam.use_backend(sam_backend.FakeSamBackend()):
        traced = refine_sam.trace(candidate, mask, result, session.image_data(info["name"]),
                                  pixel_um=1.0, envelope=geometry)
    assert traced.method == "blur" and traced.geometry == classical.geometry
    assert traced.params["sam"]["status"] == "rejected"


def test_blur_needs_closer_agreement_than_a_fold(tmp_path):
    from plexora.plugins.qc.server import refine_sam

    envelope = np.ones((100, 100), bool)
    theirs = np.zeros_like(envelope)
    theirs[30:70, 30:70] = True
    mine = np.zeros_like(envelope)
    mine[30:70, 30:96] = True   # IoU 0.61 with the classical trace

    def judge(klass):
        job = refine_sam.SamJob(rgb=np.zeros((100, 100, 3), np.uint8), origin=(0, 0),
                                factor=(1.0, 1.0), image_size=(100, 100), envelope={},
                                point=None, box=(0, 0, 100, 100), pixel_um=None, channels=[],
                                klass=klass, peak_full=None, read_s=0.0)
        prediction = sam.Prediction(mask=mine, iou=0.9, low_res=None, index=0)
        return refine_sam._judge(prediction, job, envelope, theirs)[1]

    assert judge("tissue_fold") is None
    assert judge("out_of_focus") == "agrees_with_classical"


def test_a_blurred_patch_is_never_outlined_by_the_model_alone(tmp_path, monkeypatch):
    from plexora.plugins.qc.server import refine, refine_sam

    info, session, result, candidate, mask, geometry, region = _blur_scene(tmp_path)
    _truth_predictor(monkeypatch, region["mask"])
    classical_refine = refine.refine

    def fell_back(*args, **kwargs):
        traced = classical_refine(*args, **kwargs)
        traced.status, traced.geometry = "fallback", kwargs.get("envelope")
        return traced

    monkeypatch.setattr(refine, "refine", fell_back)
    asked = []
    monkeypatch.setattr(refine_sam, "prepare", lambda *a, **k: asked.append(1))
    with sam.use_backend(sam_backend.FakeSamBackend()):
        traced = refine_sam.trace(candidate, mask, result, session.image_data(info["name"]),
                                  pixel_um=1.0, envelope=geometry)
    assert traced.method == "blur" and traced.status == "fallback" and not asked


def test_the_reader_lock_is_not_held_while_the_model_runs(tmp_path, monkeypatch):
    from plexora.plugins.qc.server import refine_sam
    from plexora.server.utils import source_image

    info, session, result, candidate, mask, geometry, region = _dark_scene(tmp_path)
    _truth_predictor(monkeypatch, region["mask"])
    seen = {}
    ds = session.image_data(info["name"])

    def embed(rgb):
        held = source_image.SHELF._held.get(ds.name)
        seen["locked"] = bool(held and held.lock.locked())

    monkeypatch.setattr(sam, "embed", embed)
    with sam.use_backend(sam_backend.FakeSamBackend()):
        refine_sam.trace(candidate, mask, result, ds, pixel_um=1.0, envelope=geometry)
    assert seen == {"locked": False}
