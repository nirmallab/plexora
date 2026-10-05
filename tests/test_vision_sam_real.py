"""Magic select with the real model, on the synthetic QC scene.

Skipped unless `PLEXORA_TEST_SEGMENT_MODEL_DIR` names a directory holding the
two verified model files (`plexora ai segment install --model-dir DIR`) and
ONNX Runtime is installed. Asserts outlines a person would accept and prints
the timings and the device each graph ran on.
"""

import json
import os

import numpy as np
import pytest

MODEL_DIR = os.environ.get("PLEXORA_TEST_SEGMENT_MODEL_DIR")

pytestmark = pytest.mark.skipif(
    not MODEL_DIR, reason="set PLEXORA_TEST_SEGMENT_MODEL_DIR to run against the real model")


@pytest.fixture
def real_model(monkeypatch):
    pytest.importorskip("onnxruntime")
    from plexora.vision import sam, segment, sam_weights

    monkeypatch.setenv("PLEXORA_SEGMENT_MODEL_DIR", MODEL_DIR)
    if sam_weights.missing(deep=True):
        pytest.skip(f"{MODEL_DIR} does not hold verified model files")
    segment.forget()
    yield sam
    segment.forget()


def _rasterise(geometry, size):
    import cv2
    from shapely.geometry import shape

    canvas = np.zeros((size, size), np.uint8)
    found = shape(geometry)
    for polygon in (found.geoms if found.geom_type == "MultiPolygon" else [found]):
        cv2.fillPoly(canvas, [np.round(np.asarray(polygon.exterior.coords) - 0.5)
                              .astype(np.int32)], 1)
    return canvas.astype(bool)


def test_the_real_model_outlines_a_speck_and_a_dark_patch(real_model, tmp_path):
    from plexora.agent import AgentSession
    from plexora.vision import segment
    from tests.qc_fixtures import make_qc_project

    info = make_qc_project(tmp_path, size=1024, grid=40, artifacts=("speck", "dark_region"))
    session = AgentSession()
    for region in info["truth"]["regions"]:
        ys, xs = np.nonzero(region["mask"])
        pad = 150
        view = {"x": float(max(0, xs.min() - pad)), "y": float(max(0, ys.min() - pad))}
        view["width"] = float(min(1024, xs.max() + pad)) - view["x"]
        view["height"] = float(min(1024, ys.max() + pad)) - view["y"]
        box = {"x": float(xs.min()), "y": float(ys.min()),
               "width": float(xs.max() - xs.min() + 1), "height": float(ys.max() - ys.min() + 1)}
        found = segment.segment(session, info["name"], view=view, points=[], box=box,
                                channels=[{"name": n} for n in info["channels"][:3]])
        mine = _rasterise(found["geometry"], info["size"])
        iou = (mine & region["mask"]).sum() / (mine | region["mask"]).sum()
        print(region["name"], round(float(iou), 3), found["timing"], found["device"])
        assert iou >= 0.8, (region["name"], iou)


@pytest.mark.paid
def test_the_real_model_tightens_a_loose_region_from_itself(real_model, tmp_path):
    """A region three times the size of the object it holds -- a box that
    loose comes back as the box -- is redrawn snug by `segment_qc_roi` from
    the region alone (mode replace, no points)."""
    from plexora.agent import AgentSession, invoke, registry
    from tests.qc_fixtures import make_qc_project

    from plexora.agent import jobs

    registry.discover(["roi", "qc"])
    info = make_qc_project(tmp_path, size=1024, grid=40, artifacts=("speck", "dark_region"))
    session = AgentSession()
    invoke(session, "profile_image_qc", {"project": info["name"]})
    jobs.drain(180)
    for region in info["truth"]["regions"]:
        ys, xs = np.nonzero(region["mask"])
        x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
        pad = max(x1 - x0, y1 - y0) * 0.6 + 10
        loose = {"type": "Polygon", "coordinates": [[
            [x0 - pad, y0 - pad], [x1 + pad, y0 - pad], [x1 + pad, y1 + pad],
            [x0 - pad, y1 + pad], [x0 - pad, y0 - pad]]]}
        made = invoke(session, "create_roi", {"project": info["name"],
                                              "category": "QC: " + ("Debris or foreign object" if region["name"] == "speck"
                                                     else "Tissue damage or detachment"),
                                              "geometry": loose})["result"]
        invoke(session, "refresh_qc", {"project": info["name"]})
        tight = invoke(session, "segment_qc_roi", {
            "project": info["name"], "roi_id": made["roi"]["id"], "mode": "replace",
            "force": True})
        assert tight["ok"], tight
        result = tight["result"]
        roi = invoke(session, "get_roi", {"project": info["name"], "roi_id": made["roi"]["id"],
                                          })["result"]["roi"]
        mine = _rasterise(roi["geometry"], info["size"])
        iou = (mine & region["mask"]).sum() / (mine | region["mask"]).sum()
        before = _rasterise(loose, info["size"])
        was = (before & region["mask"]).sum() / (before | region["mask"]).sum()
        print(region["name"], result.get("traced_with"), round(float(was), 3), "->",
              round(float(iou), 3), json.dumps(result.get("model_check"), default=str))
        if region["name"] == "speck":
            assert result["written"] and iou >= 0.75, (iou, result.get("traced_with"))
        # Whatever was written is never a worse outline than the one it replaced.
        assert iou >= was - 0.02, (region["name"], was, iou)
