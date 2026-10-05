"""Magic select on a project: a view and a few clicks -> one polygon, and the
`/segment/v1` routes the ROI and QC panels call.

On a synthetic QC scene with a painted dark region, through `FakeSamBackend`
(which outlines the bright -- here, the dark-inverted -- connected region
under a click), so neither ONNX Runtime nor the weights are needed.
"""

import json

import numpy as np
import pytest

from plexora.vision import sam, sam_backend
from plexora.vision import segment as segmenter


@pytest.fixture(autouse=True)
def _clean():
    segmenter.forget()
    yield
    segmenter.forget()


@pytest.fixture
def scene(tmp_path):
    from tests.qc_fixtures import make_qc_project

    return make_qc_project(tmp_path, size=512, grid=20, artifacts=("speck",))


def _speck(info):
    region = next(r for r in info["truth"]["regions"] if r["name"] == "speck")
    ys, xs = np.nonzero(region["mask"])
    return region["mask"], float(xs.mean()), float(ys.mean())


def _view_round(cx, cy, half=120, size=512):
    x0, y0 = max(0.0, cx - half), max(0.0, cy - half)
    return {"x": x0, "y": y0, "width": min(size, cx + half) - x0,
            "height": min(size, cy + half) - y0}


def _rasterise(geometry, size):
    import cv2
    from shapely.geometry import shape

    canvas = np.zeros((size, size), np.uint8)
    found = shape(geometry)
    for polygon in (found.geoms if found.geom_type == "MultiPolygon" else [found]):
        cv2.fillPoly(canvas, [np.round(np.asarray(polygon.exterior.coords) - 0.5)
                              .astype(np.int32)], 1)
    return canvas.astype(bool)


def test_a_click_on_the_speck_outlines_it_and_a_second_click_reuses_the_picture(scene):
    from plexora.agent import AgentSession

    mask, cx, cy = _speck(scene)
    fake = sam_backend.FakeSamBackend()
    session = AgentSession()
    with sam.use_backend(fake):
        first = segmenter.segment(session, "qcsynth", view=_view_round(cx, cy),
                                  points=[{"x": cx, "y": cy, "label": 1}],
                                  channels=[{"name": scene["channels"][0]}])
        again = segmenter.segment(session, "qcsynth", view=_view_round(cx, cy),
                                  points=[{"x": cx, "y": cy, "label": 1},
                                          {"x": cx + 60, "y": cy + 60, "label": 0}],
                                  channels=[{"name": scene["channels"][0]}],
                                  token=first["token"])
    assert first["geometry"]["type"] == "Polygon"
    mine = _rasterise(first["geometry"], scene["size"])
    iou = (mine & mask).sum() / (mine | mask).sum()
    assert iou >= 0.6, iou
    assert first["provenance"]["method"] == "sam"
    assert first["flags"] == {"empty": False, "touches_edge": False, "too_large": False,
                              "capped_view": False}
    assert first["area_um2"] is not None and first["area_um2"] > 0
    # One embedding for both clicks: the second is a decoder call only.
    assert [c[0] for c in fake.calls].count("embed") == 1
    assert again["token"] == first["token"]
    assert "embed_ms" not in again["timing"]


def test_the_same_view_hits_the_cache_without_a_token(scene):
    from plexora.agent import AgentSession

    _mask, cx, cy = _speck(scene)
    fake = sam_backend.FakeSamBackend()
    with sam.use_backend(fake):
        for _ in range(2):
            segmenter.segment(AgentSession(), "qcsynth", view=_view_round(cx, cy),
                              points=[{"x": cx, "y": cy, "label": 1}],
                              channels=[{"name": scene["channels"][0], "range": [0, 6000]}])
    assert [c[0] for c in fake.calls].count("embed") == 1


def test_bad_prompts_are_refused_in_words(scene):
    from plexora.agent import AgentSession

    session = AgentSession()
    view = {"x": 0, "y": 0, "width": 200, "height": 200}
    with sam.use_backend(sam_backend.FakeSamBackend()):
        with pytest.raises(segmenter.SegmentError, match="include point or a box"):
            segmenter.segment(session, "qcsynth", view=view,
                              points=[{"x": 50, "y": 50, "label": 0}])
        with pytest.raises(segmenter.SegmentError, match="inside the view"):
            segmenter.segment(session, "qcsynth", view=view,
                              points=[{"x": 400, "y": 400, "label": 1}])
        with pytest.raises(segmenter.SegmentError, match="at most"):
            segmenter.segment(session, "qcsynth", view=view,
                              points=[{"x": 5, "y": 5, "label": 1}] * 33)
        with pytest.raises(segmenter.SegmentError, match="not a channel"):
            segmenter.segment(session, "qcsynth", view=view,
                              points=[{"x": 50, "y": 50, "label": 1}],
                              channels=[{"name": "NOPE"}])


def test_a_box_bigger_than_the_view_is_cut_to_it(scene):
    from plexora.agent import AgentSession

    _mask, cx, cy = _speck(scene)
    view = _view_round(cx, cy, half=60)
    with sam.use_backend(sam_backend.FakeSamBackend()):
        found = segmenter.segment(AgentSession(), "qcsynth", view=view, points=[],
                                  box={"x": 0, "y": 0, "width": 512, "height": 512})
    box = found["provenance"]["prompts"]["box"]
    assert box["x"] >= view["x"] - 1e-6 and box["x"] + box["width"] <= view["x"] + view[
        "width"] + 1e-6


def test_the_crop_is_the_finest_level_that_fits_and_never_bigger():
    class Source:
        levels = 4

        @staticmethod
        def level_shape(level):
            side = 8192 // (2 ** level)
            return (side, side)

    level, lbox, factor, capped = segmenter.choose_crop(
        Source, (8192, 8192), (0, 0, 3000, 3000), np.zeros((0, 2)))
    assert level == 2 and factor == (4.0, 4.0) and not capped
    assert max(lbox[2] - lbox[0], lbox[3] - lbox[1]) <= segmenter.CROP_SIDE
    Source.levels = 3                             # coarsest 2048 px: too big
    level, lbox, factor, capped = segmenter.choose_crop(
        Source, (8192, 8192), (0, 0, 8192, 8192), np.array([[100.0, 100.0]]))
    assert capped and level == 2
    assert max(lbox[2] - lbox[0], lbox[3] - lbox[1]) <= segmenter.CROP_SIDE


def test_outline_keeps_the_part_under_the_click_and_flags_the_edge():
    mask = np.zeros((100, 100), bool)
    mask[10:30, 10:30] = True
    mask[60:90, 0:40] = True                      # touches the left edge
    geometry, flags, cleaned = segmenter.outline(
        mask, origin=(0, 0), factor=(1.0, 1.0), image_size=(1000, 1000),
        positives_rc=[(20, 20)], image_border=(False, False, False, False))
    assert geometry["type"] == "Polygon"
    assert cleaned[20, 20] and not cleaned[70, 10]
    assert not flags["touches_edge"]
    _g, flags, _c = segmenter.outline(
        mask, origin=(0, 0), factor=(1.0, 1.0), image_size=(1000, 1000),
        positives_rc=[(70, 10)], image_border=(False, False, False, False))
    assert flags["touches_edge"]
    _g, flags, _c = segmenter.outline(np.ones((50, 50), bool), origin=(0, 0),
                                      factor=(1.0, 1.0), image_size=(1000, 1000))
    assert flags["too_large"] and _g is None


# -- the routes -------------------------------------------------------------------


@pytest.fixture
def client(scene):
    import plexora

    return plexora.app.test_client()


def test_status_and_a_click_before_setup_say_not_ready(client, monkeypatch, tmp_path):
    monkeypatch.setenv("PLEXORA_SEGMENT_MODEL_DIR", str(tmp_path / "empty"))
    status = client.get("/segment/v1/status").get_json()
    assert status["ok"] and status["state"] in ("weights_missing", "not_installed_runtime")
    answer = client.post("/segment/v1/segment", data=json.dumps({
        "datasource": "qcsynth", "view": {"x": 0, "y": 0, "width": 100, "height": 100},
        "points": [{"x": 50, "y": 50, "label": 1}]}), content_type="application/json")
    assert answer.status_code == 409
    error = answer.get_json()["error"]
    # The refusal carries the status: the client starts the setup without asking.
    assert error["code"] == "not_ready" and error["state"] == status["state"]
    assert "SAM" not in error["message"]


def test_a_click_through_the_route(client, scene):
    _mask, cx, cy = _speck(scene)
    with sam.use_backend(sam_backend.FakeSamBackend()):
        answer = client.post("/segment/v1/segment", data=json.dumps({
            "datasource": "qcsynth", "view": _view_round(cx, cy),
            "points": [{"x": cx, "y": cy, "label": 1}],
            "options": {"simplify_px": 0.5}}), content_type="application/json")
        bad = client.post("/segment/v1/segment", data=json.dumps({
            "datasource": "qcsynth", "view": _view_round(cx, cy), "points": "nope"}),
            content_type="application/json")
        unknown = client.post("/segment/v1/segment", data=json.dumps({
            "datasource": "nope", "view": _view_round(cx, cy),
            "points": [{"x": cx, "y": cy, "label": 1}]}), content_type="application/json")
    body = answer.get_json()
    assert answer.status_code == 200 and body["ok"]
    assert body["geometry"]["type"] == "Polygon" and body["token"].startswith("sam_")
    assert bad.status_code == 400 and bad.get_json()["error"]["code"] == "invalid_input"
    assert unknown.status_code == 404


def test_a_fifth_waiting_click_is_told_to_wait(client, scene):
    _mask, cx, cy = _speck(scene)
    held = [sam.QUEUE.acquire(blocking=False) for _ in range(4)]
    try:
        with sam.use_backend(sam_backend.FakeSamBackend()):
            answer = client.post("/segment/v1/segment", data=json.dumps({
                "datasource": "qcsynth", "view": _view_round(cx, cy),
                "points": [{"x": cx, "y": cy, "label": 1}]}), content_type="application/json")
    finally:
        for ok in held:
            if ok:
                sam.QUEUE.release()
    assert answer.status_code == 429
    assert answer.get_json()["error"]["retry_after_ms"] > 0


def test_install_is_refused_when_switched_off(client, monkeypatch):
    monkeypatch.setenv(sam.ENV_SWITCH, "0")
    answer = client.post("/segment/v1/install", data="{}", content_type="application/json")
    assert answer.status_code == 409 and answer.get_json()["error"]["code"] == "disabled"


def test_core_javascript_calls_only_core_segment_routes():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "plexora/client/src/js/services/"
            "segmentService.js").read_text(encoding="utf-8")
    assert "segment/v1/segment" in text and "plugins/" not in text


def test_an_existing_outline_becomes_the_mask_prompt_in_the_models_frame():
    embedding = sam.Embedding(array=np.zeros((1, 256, 64, 64), np.float32), scale=2.0,
                              crop_hw=(512, 512), input_hw=(1024, 1024))
    entry = segmenter._Entry(token="t", key=(), identity=None, level=0, lbox=(100, 200, 612, 712),
                             factor=(2.0, 2.0), image_size=(4000, 4000), embedding=embedding,
                             channels=[], capped=False, pixel_um=None)
    # Full-res (300..500, 600..800) -> crop (50..150, 100..200) -> low-res /2.
    square = {"type": "Polygon", "coordinates": [[[300, 600], [500, 600], [500, 800],
                                                  [300, 800], [300, 600]]]}
    prompt = segmenter.mask_prompt(entry, square)
    assert prompt.shape == (1, 256, 256)
    inside = prompt[0] > 0
    ys, xs = np.nonzero(inside)
    assert 23 <= xs.min() <= 27 and 73 <= xs.max() <= 77
    assert 48 <= ys.min() <= 52 and 98 <= ys.max() <= 102
    assert segmenter.mask_prompt(entry, None) is None


def test_refining_from_a_region_sends_its_outline_as_the_mask(scene, monkeypatch):
    from plexora.agent import AgentSession

    _mask, cx, cy = _speck(scene)
    seen = {}
    original = sam.predict

    def predict(embedding, points, labels, **kwargs):
        seen["mask_input"] = kwargs.get("mask_input")
        return original(embedding, points, labels, **kwargs)

    monkeypatch.setattr(sam, "predict", predict)
    ring = [[cx - 20, cy - 20], [cx + 20, cy - 20], [cx + 20, cy + 20], [cx - 20, cy + 20],
            [cx - 20, cy - 20]]
    with sam.use_backend(sam_backend.FakeSamBackend()):
        segmenter.segment(AgentSession(), "qcsynth", view=_view_round(cx, cy),
                          points=[{"x": cx, "y": cy, "label": 1}],
                          options={"mask_geometry": {"type": "Polygon", "coordinates": [ring]}})
    assert seen["mask_input"] is not None and (seen["mask_input"] > 0).any()
