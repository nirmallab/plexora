"""The Artifact Detector: folds, tears, debris and saturation across every
channel, as snug scored objects, on synthetic scenes with known truth."""

import json

import numpy as np
import pytest

from plexora.agent.errors import AgentError

CATEGORY_OF = {"tissue_fold": "fold", "tissue_damage_or_detachment": "tear",
               "debris_or_foreign_object": "debris", "saturation_or_clipping": "saturation"}


def _project(tmp_path, **kwargs):
    from tests.qc_fixtures import make_qc_project

    kwargs.setdefault("artifacts", ("fold", "dark_region", "saturation"))
    return make_qc_project(tmp_path, **kwargs)


def _run(**kwargs):
    from plexora.agent import AgentSession
    from plexora.plugins.qc.server import artifacts

    return artifacts.load_or_run(AgentSession(), "qcsynth", **kwargs)


def _raster(geometry, size):
    """A GeoJSON (Multi)Polygon in full-resolution pixels as a mask."""
    import cv2

    mask = np.zeros((size, size), dtype=np.uint8)
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" \
        else [geometry["coordinates"]]
    for polygon in polygons:
        cv2.fillPoly(mask, [np.round(np.asarray(polygon[0])).astype(np.int32)], 1)
        for hole in polygon[1:]:
            cv2.fillPoly(mask, [np.round(np.asarray(hole)).astype(np.int32)], 0)
    return mask.astype(bool)


def _retained(summary, category, size, bar=0.5):
    mask = np.zeros((size, size), dtype=bool)
    for o in summary["objects"]:
        if o["category"] == category and o["score"] >= bar:
            mask |= _raster(o["geometry"], size)
    return mask


def _ok(answer):
    assert answer["ok"], json.dumps(answer.get("error"), default=str)[:2000]
    return answer["result"]


def _post(client, path, body):
    return client.post(f"/plugins/qc/{path}", data=json.dumps({"datasource": "qcsynth",
                                                                **body})).get_json()


def test_folds_tears_and_saturation_are_found_with_their_channels(tmp_path):
    made = _project(tmp_path)
    summary, reused = _run()
    assert not reused and summary["status"] == "ok"
    size = made["size"]
    recall_floor = {"fold": 0.6, "dark_region": 0.8, "saturation": 0.8}
    for region in made["truth"]["regions"]:
        category = CATEGORY_OF[region["class"]]
        found = _retained(summary, category, size)
        truth = region["mask"]
        overlap = float((found & truth).sum())
        assert overlap / truth.sum() >= recall_floor[region["name"]], region["name"]
        assert overlap / max(1, found.sum()) >= 0.6, region["name"]
    saturated = [o for o in summary["objects"] if o["category"] == "saturation"]
    assert len(saturated) == 1
    assert saturated[0]["channels"] == ["CD3"] and saturated[0]["source_channel"] == "CD3"
    for category in ("fold", "tear"):
        objects = [o for o in summary["objects"] if o["category"] == category]
        assert len(objects) == 1 and len(objects[0]["channels"]) >= 4, category
        assert objects[0]["on_tissue"] is True and objects[0]["refined"] is True
    assert summary["counts"] == {"fold": 1, "tear": 1, "debris": 0, "saturation": 1}
    for o in summary["objects"]:
        assert 0.0 <= o["score"] <= 1.0 and o["id"].startswith(f"art_{o['category']}_")
        assert o["class"] in CATEGORY_OF and o["area_um2"] > 0


def test_a_hair_and_a_speck_on_the_glass_are_debris(tmp_path):
    made = _project(tmp_path, artifacts=("hair", "speck"))
    summary, _ = _run()
    debris = [o for o in summary["objects"] if o["category"] == "debris"]
    shapes = sorted(o["metrics"]["shape"] for o in debris)
    assert shapes == ["compact", "fiber"]
    for name, shape in (("hair", "fiber"), ("speck", "compact")):
        truth = next(r for r in made["truth"]["regions"] if r["name"] == name)["mask"]
        obj = next(o for o in debris if o["metrics"]["shape"] == shape)
        found = _raster(obj["geometry"], made["size"])
        assert (found & truth).sum() / truth.sum() >= 0.8, name
        # Snug: the outline is the object, not its bounding box.
        assert found.sum() <= 3 * truth.sum() + 200, name
        assert obj["on_tissue"] is False and obj["score"] >= 0.5
    assert summary["counts"]["fold"] == summary["counts"]["tear"] == 0


def test_a_clean_scene_has_no_objects(tmp_path):
    from plexora.agent import AgentSession
    from plexora.plugins.qc.server import artifacts

    _project(tmp_path, artifacts=())
    summary, _ = _run()
    assert summary["status"] == "ok" and summary["objects"] == []
    assert artifacts.evaluation("qcsynth")["n_objects"] == 0
    status = artifacts.public_status(AgentSession(), "qcsynth")
    assert status["available"] is True and status["results"]["stale"] is False
    for entry in status["categories"]:
        assert entry["threshold"]["source"] == "auto"
        assert entry["threshold"]["value"] == artifacts.AUTO_THRESHOLD


def test_thresholds_filter_the_stored_objects_without_reading_pixels(tmp_path, monkeypatch):
    from plexora.plugins.qc.server import artifacts
    from plexora.plugins.roi.server import geometry as roi_geometry
    from plexora.server.utils import source_image

    _project(tmp_path, artifacts=("fold", "dark_region", "saturation", "hair"))
    summary, _ = _run()
    again, reused = _run()
    assert reused and again["fingerprint"] == summary["fingerprint"]

    def no_pixels(*args, **kwargs):
        raise AssertionError("a threshold change must not read the image")

    monkeypatch.setattr(source_image.SourceImage, "read", no_pixels)
    low = artifacts.objects_at(summary, {c: 0.2 for c in artifacts.CATEGORIES})
    high = artifacts.objects_at(summary, {c: 0.9 for c in artifacts.CATEGORIES})
    low_ids = {o["id"] for o in low}
    assert {o["id"] for o in high} <= low_ids
    by_id = {o["id"]: o for o in low}
    for o in high:
        assert o["geometry"] == by_id[o["id"]]["geometry"]
    for o in low:
        assert roi_geometry.validate_geometry(o["geometry"])
    result = artifacts.evaluate(summary, {c: 0.2 for c in artifacts.CATEGORIES})
    assert result["n_objects"] == len(low) and result["denominator"] == "analysis_roi"
    channel = artifacts.objects_at(summary, {c: 0.0 for c in artifacts.CATEGORIES},
                                   channels=["CD3"])
    assert all("CD3" in o["channels"] for o in channel)
    # From disk, with the memory gone.
    artifacts._MEMORY.clear()
    assert artifacts.evaluate(artifacts.current("qcsynth"),
                              {c: 0.2 for c in artifacts.CATEGORIES}) == result


def test_saturation_is_per_channel_and_not_bright_specks(tmp_path):
    _project(tmp_path, artifacts=("saturation", "aggregates"))
    summary, _ = _run()
    saturated = [o for o in summary["objects"] if o["category"] == "saturation"]
    assert [o["source_channel"] for o in saturated] == ["CD3"]
    assert summary["saturated_channels"] == ["CD3"]
    assert summary["ceilings"]["CD8"] == 65535.0


def test_the_panel_runs_sets_and_writes_regions(tmp_path):
    import plexora
    from plexora.agent import AgentSession, invoke, jobs, registry
    from plexora.plugins.qc.server import results

    _project(tmp_path)
    client = plexora.app.test_client()
    state = client.get("/plugins/qc/state?datasource=qcsynth").get_json()
    check = state["checks"]["artifacts"]
    assert check["available"] and check["results"] is None and check["shown"] == []
    assert [c["key"] for c in check["categories"]] == ["fold", "tear", "debris", "saturation"]
    started = _post(client, "artifacts/run", {})
    assert started["ok"] and started["job_id"]
    jobs.store().wait(started["job_id"], 180)
    job = client.get(f"/plugins/qc/jobs/{started['job_id']}").get_json()["job"]
    assert job["status"] == "done", job
    assert job["progress"]["message"]
    read = client.get("/plugins/qc/artifacts?datasource=qcsynth&include_regions=1") \
        .get_json()
    assert read["ok"], read
    read = read["artifacts_qc"]
    assert read["results"]["fingerprint"] == job["result"]["fingerprint"]
    assert read["results"]["stale"] is False
    assert {o["category"] for o in read["results"]["objects"]} == {"fold", "tear",
                                                                    "saturation"}
    fold = next(c for c in read["categories"] if c["key"] == "fold")
    assert fold["n_objects"] == 1 and fold["distribution"]
    objects = client.get("/plugins/qc/artifacts/objects?datasource=qcsynth").get_json()
    assert objects["ok"] and len(objects["objects"]) == 3
    assert all(o["geometry"] for o in objects["objects"])
    # A threshold is per category, receipted and undoable.
    changed = _post(client, "artifacts/set", {"category": "fold", "threshold": 0.95})
    assert changed["ok"] and changed["threshold"]["source"] == "user"
    assert changed["receipt"]["undo_hint"]["arguments"]["threshold"] == "auto"
    assert changed["evaluation"]["per_category"]["fold"]["n_objects"] == 0
    session = AgentSession()
    registry.discover(["roi", "qc"])
    _ok(invoke(session, "undo_operation", {"operation_id": changed["receipt"]["operation_id"]}))
    read = client.get("/plugins/qc/artifacts?datasource=qcsynth").get_json()["artifacts_qc"]
    assert next(c for c in read["categories"] if c["key"] == "fold")["threshold"][
        "source"] == "auto"
    assert _post(client, "artifacts/set", {"category": "nope", "threshold": 0.5})["ok"] is False
    assert _post(client, "artifacts/set", {"category": "fold", "threshold": 1.5})["ok"] is False
    assert _post(client, "artifacts/set", {"threshold": 0.5})["ok"] is False
    # The listed channels never move a threshold.
    listed = _post(client, "artifacts/set", {"channels": ["CD3"]})
    assert listed["shown"] == ["CD3"]
    assert listed["receipt"]["undo_hint"]["arguments"]["channels"] == "default"
    colour = _post(client, "artifacts/set", {"category": "tear", "color": "#123456"})
    assert colour["color"] == "#123456"
    # The retained objects as ROIs, each with its own class.
    written = _post(client, "artifacts/regions/write", {})
    assert written["ok"] and len(written["written"]) == 3, written
    meta = {r["roi_id"]: r for r in results.roi_meta("qcsynth").to_dicts()}
    rows = [meta[i] for i in written["written"]]
    assert {r["class"] for r in rows} == {"tissue_fold", "tissue_damage_or_detachment",
                                          "saturation_or_clipping"}
    assert all(r["detector"] == "artifacts" and r["approved_action"] == "exclude"
               for r in rows)
    saturation = next(r for r in rows if r["class"] == "saturation_or_clipping")
    assert saturation["channels"] == ["CD3"] and saturation["scope"] == "channel"
    rois = _ok(invoke(session, "list_rois", {"project": "qcsynth"}))["rois"]
    for roi_id in written["written"]:
        assert next(r for r in rois if r["id"] == roi_id)["category_id"] == \
            "qc_tissue_acquisition"
    # Written again: replaced, not doubled; one the user reshaped is kept.
    again = _post(client, "artifacts/regions/write", {})
    assert sorted(again["removed"]) == sorted(written["written"])
    fold_id = next(i for i in again["written"] if meta.get(i) is None and
                   results.roi_meta("qcsynth").filter(
                       results.roi_meta("qcsynth")["roi_id"] == i).to_dicts()[0]["class"]
                   == "tissue_fold")
    square = {"type": "Polygon", "coordinates": [[[500, 500], [600, 500], [600, 600],
                                                  [500, 600], [500, 500]]]}
    _ok(invoke(session, "update_roi", {"project": "qcsynth", "roi_id": fold_id,
                                       "geometry": square}))
    third = _post(client, "artifacts/regions/write", {"categories": ["fold"]})
    assert third["kept"] == [fold_id] and third["removed"] == []
    cleared = _post(client, "artifacts/clear", {})
    assert cleared["ok"] and cleared["cleared"] is True
    assert client.get("/plugins/qc/artifacts/objects?datasource=qcsynth").get_json()[
        "available"] is False


def test_a_cancelled_job_stores_nothing(tmp_path):
    from plexora.agent import AgentSession, jobs, registry
    from plexora.plugins.qc.server import artifacts

    _project(tmp_path)
    registry.discover(["qc"])
    started = registry.invoke(AgentSession(), "run_artifact_check", {"project": "qcsynth"})
    assert started["ok"]
    job_id = started["result"]["job_id"]
    jobs.store().cancel(job_id)
    record = jobs.store().wait(job_id, 60)
    assert record["status"] in ("cancelled", "done")
    if record["status"] == "cancelled":
        assert artifacts.current_fingerprint("qcsynth") is None
    assert artifacts.running_job("qcsynth") is None


def test_without_a_pixel_size_the_check_asks_for_one(tmp_path):
    from plexora.agent import AgentSession
    from plexora.plugins.qc.server import artifacts

    _project(tmp_path, calibrated=False)
    with pytest.raises(AgentError) as caught:
        _run()
    assert caught.value.code == "precondition_missing"
    assert caught.value.detail["hint"] == "params.pixel_um"
    status = artifacts.public_status(AgentSession(), "qcsynth")
    assert status["available"] is False and status["available_reason"] == "no_pixel_size"
    summary, _ = _run(params={"pixel_um": 1.0})
    assert summary["pixel_um"] == 1.0 and summary["pixel_um_source"] == "params"
    assert summary["counts"]["fold"] == 1


def test_unknown_parameters_and_categories_are_refused(tmp_path):
    _project(tmp_path, size=256, grid=10, levels=2)
    with pytest.raises(AgentError) as caught:
        _run(params={"nope": 1})
    assert caught.value.code == "invalid_input"
    with pytest.raises(AgentError):
        _run(params={"categories": ["smudge"]})


def test_scores_are_soft_and_never_saturate():
    from plexora.plugins.qc.server import artifacts

    values = artifacts.soft(np.array([0.0, 2.0, 4.0, 6.0, 40.0]), 2.0, 4.0)
    assert values[0] == 0 and values[1] == 0
    assert values[2] == pytest.approx(0.5) and values[3] == pytest.approx(0.75)
    assert values[4] < 1.0 and np.all(np.diff(values) >= 0)


@pytest.mark.paid
def test_the_session_reviews_each_category_and_writes_the_objects(tmp_path):
    """Opt-in: one score review per category; what is confirmed becomes the
    object's own snug outline, of its own class."""
    from plexora.agent import AgentSession, registry
    from plexora.plugins.qc.server import artifacts, polygons, results
    from plexora.plugins.qc.server.engine import store
    from tests.test_qc_session import QCOracle, drive, start

    class Oracle(QCOracle):
        def _score_review(self, unit, ev):
            if unit["check"] != "artifacts":
                return super()._score_review(unit, ev)
            target = artifacts.CATEGORY_CLASS[unit["category"]]
            regions = [r for r in self.info["truth"]["regions"] if r["class"] == target]
            strata = {}
            for stratum, places in (unit.get("shown") or {}).get("places", {}).items():
                inside = sum(any(r["mask"][int(p["y"]), int(p["x"])] for r in regions)
                             for p in places)
                share = inside / max(1, len(places))
                strata[stratum] = "artifact" if share >= 0.6 else \
                    ("normal" if share <= 0.2 else "mixed")
            return {"kind": "score_review", "strata": strata, "threshold": "accept",
                    "severity": "severe", "confidence": "sure"}

    registry.discover(["roi", "qc"])
    info = _project(tmp_path)
    session = AgentSession()
    started = start(session, checks={"artifacts": True, "blur": False, "registration": False,
                                     "segmentation": False})
    assert {f"artifacts:{c}" for c in artifacts.CATEGORIES} <= set(started["checks"])
    sid = started["session_id"]
    skipped = store().load(sid)["scan"]["qcsynth"]["skipped_detectors"]
    assert any(s["name"] == "saturation" and s.get("superseded_by") == "artifacts"
               for s in skipped)
    drive(session, sid, Oracle(info))
    document = results.load("qcsynth")
    result = next(iter(sorted((document.get("results") or {}).values(),
                              key=lambda r: r["created_at"], reverse=True)))
    assert set(result["checks"]["artifacts"]) == set(artifacts.CATEGORIES)
    summary = artifacts.current("qcsynth")
    found = [c for c in result["candidates"].values() if c.get("detector") == "artifacts"]
    tear = next(c for c in found if c["class"] == "tissue_damage_or_detachment")
    obj = next(o for o in summary["objects"] if o["category"] == "tear")
    assert tear["trace"] == "object" and tear["fine_grid"] is True
    assert polygons.geometry_hash(tear["envelope_geometry"]) == \
        polygons.geometry_hash(obj["geometry"])
    assert tear["refinement"]["status"] == "detector"
    assert tear["metrics"]["check"] == "artifacts"
    saturated = [c for c in found if c["class"] == "saturation_or_clipping"]
    assert saturated and saturated[0]["channels"] == ["CD3"]
    # The fold is written: by the detector, or merged into the scan's own
    # fold candidate at the same place (they coexist in v1).
    assert any(c["class"] == "tissue_fold" for c in result["candidates"].values())
