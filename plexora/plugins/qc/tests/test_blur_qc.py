"""Blur QC: multi-scale gradient focus against the image's own sharpest tiles,
on synthetic scenes with a blurred disc, none, and blur everywhere."""

import json

import numpy as np
import pytest

from plexora.agent.errors import AgentError


def _project(tmp_path, **kwargs):
    from tests.qc_fixtures import make_qc_project

    kwargs.setdefault("artifacts", ("blur_local",))
    return make_qc_project(tmp_path, **kwargs)


def _run(**kwargs):
    from plexora.agent import AgentSession
    from plexora.plugins.qc.server import blur

    return blur.load_or_run(AgentSession(), "qcsynth", **kwargs)


def _truth_on_grid(grid, mask):
    """A full-resolution truth mask, as the fraction of each grid cell it covers."""
    ny, nx = grid["shape"]
    s = grid["cell_full_px"]
    out = np.zeros((ny, nx))
    for iy in range(ny):
        for ix in range(nx):
            y0, x0 = int(iy * s), int(ix * s)
            block = mask[y0:int(y0 + s), x0:int(x0 + s)]
            out[iy, ix] = block.mean() if block.size else 0
    return out


def _ok(answer):
    assert answer["ok"], json.dumps(answer.get("error"), default=str)[:2000]
    return answer["result"]


def test_a_blurred_disc_is_found_on_its_channel_and_not_on_a_sharp_one(tmp_path):
    from plexora.plugins.qc.server import blur

    made = _project(tmp_path)
    summary, reused = _run(channel="DNA_2")
    assert not reused and summary["status"] == "ok"
    assert summary["channel"] == "DNA_2" and summary["grid"]["cell_um"] == pytest.approx(40)
    assert summary["global_blur"]["possible"] is False
    assert summary["noise"]["source"] == "glass"
    auto = summary["auto_threshold"]
    assert blur.AUTO_FLOOR <= auto <= blur.AUTO_RANGE[1]
    arrays = blur.load_arrays("qcsynth", summary["fingerprint"])
    result = blur.evaluate(arrays, summary, auto)
    region = next(r for r in made["truth"]["regions"] if r["name"] == "blur_local")
    truth = _truth_on_grid(summary["grid"], region["mask"])
    core, touched = truth > 0.8, truth > 0.2
    mask = result["mask"]
    assert (mask & core).sum() / core.sum() >= 0.8
    assert (mask & touched).sum() / mask.sum() >= 0.8
    assert result["n_regions"] == 1 and result["denominator"] == "evaluable_tissue"
    assert 1.0 < result["blurred_pct"] < 20.0
    # The first cycle's DNA was never blurred.
    sharp, _ = _run(channel="DNA_1")
    assert sharp["fingerprint"] != summary["fingerprint"]
    clean = blur.evaluate(blur.load_arrays("qcsynth", sharp["fingerprint"]), sharp,
                          sharp["auto_threshold"])
    assert clean["blurred_pct"] < 2.0 and clean["n_regions"] == 0
    # Each channel keeps its own result.
    assert blur.current_fingerprint("qcsynth", "DNA_2") == summary["fingerprint"]
    assert blur.current_fingerprint("qcsynth", "DNA_1") == sharp["fingerprint"]


def test_a_clean_scene_is_not_blurred(tmp_path):
    from plexora.plugins.qc.server import blur

    _project(tmp_path, artifacts=())
    summary, _ = _run()
    assert summary["channel"] == "DNA_1"
    assert summary["auto_threshold"] >= blur.AUTO_FLOOR
    assert summary["global_blur"]["possible"] is False
    result = blur.evaluation("qcsynth", "DNA_1")
    assert result["blurred_pct"] < 2.0
    assert sum(summary["histogram"]["counts"]) == summary["n_evaluable"]


def test_blur_everywhere_is_flagged_as_possible_global_blur(tmp_path):
    from plexora.ai.qc_scenes import ARTIFACTS, BLUR_ARTIFACTS

    assert not set(BLUR_ARTIFACTS) & set(ARTIFACTS)
    made = _project(tmp_path, artifacts=("blur_global",))
    assert made["truth"]["global_blur"] is True
    summary, _ = _run()
    assert summary["global_blur"]["possible"] is True
    assert summary["global_blur"]["fine_share"] < summary["global_blur"]["floor"]


def test_thresholds_are_applied_from_the_stored_scores(tmp_path, monkeypatch):
    from plexora.plugins.qc.server import blur, scan
    from plexora.plugins.roi.server import geometry as roi_geometry

    _project(tmp_path)
    summary, _ = _run(channel="DNA_2")
    again, reused = _run(channel="DNA_2")
    assert reused and again["fingerprint"] == summary["fingerprint"]
    forced, reused = _run(channel="DNA_2", force=True)
    assert not reused and forced["fingerprint"] == summary["fingerprint"]

    def no_pixels(*args, **kwargs):
        raise AssertionError("a threshold change must not read the image")

    monkeypatch.setattr(scan, "_read", no_pixels)
    arrays = blur.load_arrays("qcsynth", summary["fingerprint"])
    low = blur.evaluate(arrays, summary, 0.2)
    high = blur.evaluate(arrays, summary, 0.6)
    assert low["blurred_pct"] >= high["blurred_pct"]
    assert (high["mask"] <= low["mask"]).all()
    for region in low["regions"]:
        assert roi_geometry.validate_geometry(region["geometry"])
        assert region["mean_blur"] <= region["max_blur"]
    # A region smaller than the minimum is not one.
    tiles = low["regions"][0]["tiles"]
    assert blur.evaluate(arrays, summary, 0.2, min_region_tiles=tiles + 1)["n_regions"] == \
        low["n_regions"] - 1
    # From disk, with the memory gone.
    blur._MEMORY.clear()
    assert blur.evaluate(blur.load_arrays("qcsynth", summary["fingerprint"]), summary,
                         0.2)["blurred_pct"] == \
        low["blurred_pct"]


def test_an_image_without_a_pixel_size_is_scored_in_pixels(tmp_path):
    _project(tmp_path, calibrated=False)
    summary, _ = _run(channel="DNA_2")
    assert summary["grid"]["cell_um"] is None and summary["pixel_um"] is None
    assert summary["level"] == 0 and summary["grid"]["cell_level_px"] == 96
    assert summary["status"] == "ok"


def test_an_unknown_channel_is_refused(tmp_path):
    _project(tmp_path, size=256, grid=10, levels=2)
    with pytest.raises(AgentError) as caught:
        _run(channel="nope")
    assert caught.value.code == "invalid_input"


def test_too_little_tissue_is_insufficient_not_blurred(tmp_path):
    from plexora.plugins.qc.server import blur

    _project(tmp_path, size=256, grid=10, levels=2)
    summary, _ = _run()
    assert summary["status"] == "insufficient"
    assert summary["global_blur"]["possible"] is True
    label = blur.label_of(summary["channel"])
    assert blur.evaluation("qcsynth", label) is None
    assert blur.viewer_map("qcsynth", label) == {"available": False, "channel": label}


def test_a_cancelled_job_stores_nothing(tmp_path, monkeypatch):
    from plexora.agent import AgentSession, jobs, registry
    from plexora.plugins.qc.server import blur, scan

    _project(tmp_path)
    monkeypatch.setattr(scan, "BLOCK_PX", 64)
    registry.discover(["qc"])
    started = registry.invoke(AgentSession(), "run_blur_check", {"project": "qcsynth"})
    assert started["ok"]
    job_id = started["result"]["job_id"]
    jobs.store().cancel(job_id)
    record = jobs.store().wait(job_id, 30)
    assert record["status"] in ("cancelled", "done")
    if record["status"] == "cancelled":
        assert blur._pointers("qcsynth") == {}


def _post(client, path, body):
    return client.post(f"/plugins/qc/{path}", data=json.dumps({"datasource": "qcsynth",
                                                                **body})).get_json()


def test_each_listed_channel_has_its_own_threshold_and_colour(tmp_path):
    from plexora.agent import AgentSession, invoke, registry

    _project(tmp_path)
    session = AgentSession()
    registry.discover(["roi", "qc"])
    status = _ok(invoke(session, "get_blur_check", {"project": "qcsynth"}))["blur_qc"]
    # The nuclear channels, in order, until someone picks.
    assert status["shown"] == ["DNA_1", "DNA_2"]
    colors = [r["color"] for r in status["results"]]
    assert len(set(colors)) == 2 and all(r["summary"] is None for r in status["results"])
    # One channel's threshold and colour leave the other's alone.
    changed = _ok(invoke(session, "set_blur_check", {"project": "qcsynth", "channel": "DNA_2",
                                                     "threshold": 0.5, "color": "#123456"}))
    assert changed["threshold"]["value"] == 0.5 and changed["color"] == "#123456"
    assert changed["receipt"]["undo_hint"]["arguments"] == {
        "project": "qcsynth", "channel": "DNA_2", "threshold": "auto", "color": "auto"}
    status = _ok(invoke(session, "get_blur_check", {"project": "qcsynth"}))["blur_qc"]
    first, second = status["results"]
    assert first["threshold"]["source"] == "auto" and first["color"] == colors[0]
    assert second["threshold"] == {"value": 0.5, "auto": None, "source": "user",
                                   "offset_steps": None}
    assert second["color"] == "#123456"
    _ok(invoke(session, "undo_operation", {"operation_id": changed["receipt"]["operation_id"]}))
    second = _ok(invoke(session, "get_blur_check", {"project": "qcsynth"}))["blur_qc"][
        "results"][1]
    assert second["threshold"]["source"] == "auto" and second["color"] == colors[1]
    # The list: picked, in order, then back to the default.
    picked = _ok(invoke(session, "set_blur_check", {"project": "qcsynth",
                                                    "channels": ["DNA_2", "CD3"]}))
    assert picked["shown"] == ["DNA_2", "CD3"]
    assert picked["receipt"]["undo_hint"]["arguments"]["channels"] == "default"
    assert _ok(invoke(session, "set_blur_check", {"project": "qcsynth",
                                                  "channels": "default"}))["shown"] == [
        "DNA_1", "DNA_2"]
    refused = invoke(session, "set_blur_check", {"project": "qcsynth", "channels": ["nope"]})
    assert not refused["ok"] and refused["error"]["code"] == "invalid_input"
    assert not invoke(session, "set_blur_check", {"project": "qcsynth",
                                                  "color": "orange"})["ok"]


def test_the_panel_runs_previews_sets_and_writes_regions(tmp_path):
    import base64

    import plexora
    from plexora.agent import AgentSession, invoke, jobs, registry
    from plexora.plugins.qc.server import results

    _project(tmp_path)
    client = plexora.app.test_client()
    state = client.get("/plugins/qc/state?datasource=qcsynth").get_json()
    check = state["checks"]["blur"]
    assert check["available"] and check["shown"] == ["DNA_1", "DNA_2"]
    assert [r["summary"] for r in check["results"]] == [None, None]
    # Play: every listed channel, in one job.
    started = _post(client, "blur/run", {})
    assert started["ok"] and started["job_id"]
    jobs.store().wait(started["job_id"], 120)
    job = client.get(f"/plugins/qc/jobs/{started['job_id']}").get_json()["job"]
    assert job["status"] == "done", job
    assert [r["channel"] for r in job["result"]["results"]] == ["DNA_1", "DNA_2"]
    read = client.get("/plugins/qc/blur?datasource=qcsynth").get_json()["blur_qc"]
    sharp, blurred = read["results"]
    summary = blurred["summary"]
    assert summary["fingerprint"] == job["result"]["results"][1]["fingerprint"]
    assert blurred["stale"] is False and blurred["threshold"]["source"] == "auto"
    assert blurred["threshold"]["value"] == summary["auto_threshold"]
    assert blurred["evaluation"]["n_regions"] == 1 and sharp["evaluation"]["n_regions"] == 0
    one = client.get("/plugins/qc/blur?datasource=qcsynth&channel=DNA_2").get_json()["blur_qc"]
    assert [r["channel"] for r in one["results"]] == ["DNA_2"]
    # The heatmap: one byte per grid cell, per channel.
    heat = client.get("/plugins/qc/blur/map?datasource=qcsynth&channel=DNA_2").get_json()
    ny, nx = summary["grid"]["shape"]
    assert heat["channel"] == "DNA_2" and heat["fingerprint"] == summary["fingerprint"]
    assert heat["grid"]["nx"] == nx and heat["grid"]["ny"] == ny
    assert len(base64.b64decode(heat["blur"])) == nx * ny
    assert heat["grid"]["step"] == pytest.approx(summary["grid"]["cell_full_px"])
    # The slider's preview stores nothing.
    preview = client.get("/plugins/qc/blur/mask?datasource=qcsynth&channel=DNA_2"
                         "&threshold=0.2").get_json()
    assert preview["threshold"] == 0.2 and preview["regions"][0]["geometry"]
    assert len(base64.b64decode(preview["mask"])) == nx * ny
    assert client.get("/plugins/qc/blur/mask?datasource=qcsynth&channel=DNA_2&threshold=nan") \
        .status_code == 400
    assert client.get("/plugins/qc/blur/mask?datasource=qcsynth&channel=nope") \
        .status_code == 400
    assert client.get("/plugins/qc/blur?datasource=qcsynth").get_json()["blur_qc"][
        "results"][1]["threshold"]["source"] == "auto"
    # A typed threshold is held in full, receipted and undoable, for its channel only.
    changed = _post(client, "blur/set", {"channel": "DNA_2", "threshold": 0.4137})
    assert changed["ok"] and changed["threshold"] == {
        "value": 0.4137, "auto": summary["auto_threshold"], "source": "user",
        "offset_steps": None}
    assert changed["receipt"]["undo_hint"]["arguments"]["threshold"] == "auto"
    read = client.get("/plugins/qc/blur?datasource=qcsynth").get_json()["blur_qc"]
    assert read["results"][0]["threshold"]["source"] == "auto"
    session = AgentSession()
    registry.discover(["roi", "qc"])
    _ok(invoke(session, "undo_operation", {"operation_id": changed["receipt"]["operation_id"]}))
    assert client.get("/plugins/qc/blur?datasource=qcsynth").get_json()["blur_qc"][
        "results"][1]["threshold"]["source"] == "auto"
    _post(client, "blur/set", {"channel": "DNA_2", "threshold": 0.3})
    back = _post(client, "blur/set", {"channel": "DNA_2", "threshold": "auto"})
    assert back["threshold"]["source"] == "auto"
    assert _post(client, "blur/set", {"channel": "DNA_2", "threshold": 1.5})["ok"] is False
    assert _post(client, "blur/regions/write", {"threshold": 0.3})["ok"] is False
    # The regions as ROIs: every listed channel's, at its own threshold.
    written = _post(client, "blur/regions/write", {})
    assert written["ok"] and len(written["written"]) == 1 and written["removed"] == []
    assert written["channels"]["DNA_2"]["written"] == written["written"]
    assert written["channels"]["DNA_1"]["written"] == []
    roi_id = written["written"][0]
    meta = results.roi_meta("qcsynth").to_dicts()
    row = next(r for r in meta if r["roi_id"] == roi_id)
    assert row["created_by"] == "blur" and row["detector"] == "blur"
    assert row["class"] == "out_of_focus" and row["approved_action"] == "exclude"
    assert row["channels"] == ["DNA_2"]
    rois = _ok(invoke(session, "list_rois", {"project": "qcsynth"}))
    feature = next(r for r in rois["rois"] if r["id"] == roi_id)
    assert feature["category_id"] == "qc_blur_focus"
    assert feature["name"].startswith("QC exclude")
    regions = client.get("/plugins/qc/regions?datasource=qcsynth").get_json()
    listed = next(r for r in regions["regions"] if r["roi_id"] == roi_id)
    assert listed["created_by"] == "blur"
    assert not listed.get("severity")
    # A strictness change leaves the regions the user asked for alone.
    for preset in ("strict", "lenient"):
        _ok(invoke(session, "set_qc_strictness", {"project": "qcsynth", "preset": preset}))
        rois = _ok(invoke(session, "list_rois", {"project": "qcsynth"}))
        assert next(r for r in rois["rois"] if r["id"] == roi_id)["name"] == feature["name"]
    # The cells inside are flagged for it.
    cells = results.cells("qcsynth")
    flagged = [r for r in cells.to_dicts() if "region:out_of_focus" in (r["reasons"] or [])]
    assert flagged
    # Another channel's write leaves this channel's regions alone.
    other = _post(client, "blur/regions/write", {"channel": "DNA_1"})
    assert other["ok"] and other["removed"] == [] and other["written"] == []
    # Written again: replaced, not doubled.
    again = _post(client, "blur/regions/write", {})
    assert again["removed"] == [roi_id] and len(again["written"]) == 1
    new_id = again["written"][0]
    # One the user reshaped is theirs: kept on the next write.
    geometry = preview["regions"][0]["geometry"]
    _ok(invoke(session, "update_roi", {"project": "qcsynth", "roi_id": new_id,
                                       "geometry": geometry}))
    third = _post(client, "blur/regions/write", {})
    assert third["kept"] == [new_id] and third["removed"] == []
    # A region's own receipt undoes it alone.
    last = third["written"][0]
    child = f"{third['receipt']['operation_id']}.001"
    _ok(invoke(session, "undo_operation", {"operation_id": child}))
    rois = {r["id"] for r in _ok(invoke(session, "list_rois", {"project": "qcsynth"}))["rois"]}
    assert last not in rois and new_id in rois
    # One channel's result forgotten; the other's stays.
    cleared = _post(client, "blur/clear", {"channel": "DNA_2"})
    assert cleared["ok"] and cleared["cleared"] == ["DNA_2"]
    assert client.get("/plugins/qc/blur/map?datasource=qcsynth&channel=DNA_2").get_json()[
        "available"] is False
    assert client.get("/plugins/qc/blur/map?datasource=qcsynth&channel=DNA_1").get_json()[
        "available"] is True
    assert _post(client, "blur/clear", {})["cleared"] == ["DNA_1"]


def test_a_threshold_moves_in_steps_never_typed(tmp_path):
    """`adjust` moves a channel's bar one step from its automatic one: stored
    as steps (user_relative), undone back to auto, bounded, never with a
    typed threshold."""
    from plexora.agent import AgentSession, invoke
    from plexora.plugins.qc.server import schemas

    _project(tmp_path)
    session = AgentSession()
    summary, _ = _run(channel="DNA_2")
    auto = summary["auto_threshold"]
    tighter = _ok(invoke(session, "set_blur_check", {"project": "qcsynth", "channel": "DNA_2",
                                                     "adjust": "tighter"}))
    bar = tighter["threshold"]
    assert bar["source"] == "user_relative" and bar["offset_steps"] == 1
    assert bar["value"] < auto and bar["auto"] == auto
    assert tighter["receipt"]["undo_hint"]["arguments"]["threshold"] == "auto"
    again = _ok(invoke(session, "set_blur_check", {"project": "qcsynth", "channel": "DNA_2",
                                                   "adjust": "tighter"}))
    assert again["threshold"]["offset_steps"] == 2
    assert again["receipt"]["undo_hint"]["arguments"]["adjust"] == "looser"
    for _ in range(schemas.ENGINE["adjust_max_steps"] - 2):
        _ok(invoke(session, "set_blur_check", {"project": "qcsynth", "channel": "DNA_2",
                                               "adjust": "tighter"}))
    past = invoke(session, "set_blur_check", {"project": "qcsynth", "channel": "DNA_2",
                                              "adjust": "tighter"})
    assert not past["ok"] and past["error"]["code"] == "invalid_input"
    both = invoke(session, "set_blur_check", {"project": "qcsynth", "channel": "DNA_2",
                                              "adjust": "looser", "threshold": 0.5})
    assert not both["ok"] and both["error"]["code"] == "invalid_input"
    alone = invoke(session, "set_blur_check", {"project": "qcsynth", "adjust": "looser"})
    assert not alone["ok"]
    written = _ok(invoke(session, "write_blur_regions", {"project": "qcsynth",
                                                         "channel": "DNA_2"}))
    assert written["channels"]["DNA_2"]["threshold_source"] == "user_relative"
    _ok(invoke(session, "undo_operation", {"operation_id": tighter["receipt"]["operation_id"]}))


def test_the_foreground_level_is_the_nuclei_not_the_tissue():
    """Two Otsu splits: tissue from glass, then nuclei from stroma -- the
    level sits between the stroma and the nuclei, not at the tissue's haze."""
    from plexora.plugins.qc.server import blur

    plane = np.zeros((100, 100))
    plane[:, 20:] = 100.0
    plane[:, 70:] = 1000.0
    assert 100.0 < blur.foreground_level(plane) < 1000.0
    rng = np.random.default_rng(0)
    glass = rng.normal(10.0, 1.0, 5000)
    stroma = rng.normal(100.0, 10.0, 3500)
    nuclei = rng.normal(1000.0, 100.0, 1500)
    level = blur.foreground_level(np.concatenate([glass, stroma, nuclei]))
    assert np.percentile(stroma, 99) < level < np.percentile(nuclei, 1)
    # One population in the tissue: the first split stands.
    level = blur.foreground_level(np.concatenate([glass, rng.normal(1000.0, 100.0, 5000)]))
    assert 12.0 < level < 800.0
    assert blur.PARAMS_DEFAULT["min_nuclear_fraction"] == 0.08


def test_a_lone_evaluable_cell_is_not_a_tile():
    from plexora.plugins.qc.server import blur

    evaluable = np.zeros((7, 7), dtype=bool)
    evaluable[1, 1] = True                      # alone
    evaluable[4:6, 4:6] = True                  # four together
    kept, lone = blur.drop_lone(evaluable)
    assert lone[1, 1] and not kept[1, 1]
    assert kept[4:6, 4:6].all() and int(lone.sum()) == 1
