"""The QC check tools an agent calls outside a session: sample, adjust in
steps, write snug regions and cell flags -- every field documented."""

import json

import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from tests.qc_fixtures import make_qc_project


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def ok(answer):
    assert answer["ok"], json.dumps(answer.get("error"), default=str)[:3000]
    return answer["result"]


NEW_OR_CHANGED = ("qc.sample_examples", "qc.registration_write_regions",
                  "qc.segmentation_write_flags", "qc.blur_set", "qc.blur_write_regions",
                  "qc.blur_status", "qc.registration_status", "qc.segmentation_status",
                  "qc.export", "qc.get_results", "qc.artifacts_run", "qc.artifacts_status",
                  "qc.artifacts_set", "qc.artifacts_clear", "qc.artifacts_write_regions")


def test_every_field_of_the_check_tools_is_documented():
    names = {cap.name: cap for cap in registry.all_capabilities()}
    for name in NEW_OR_CHANGED:
        model = names[name].input_model
        for field, info in model.model_fields.items():
            if field == "project":
                continue
            assert info.description, f"{name}.{field} has no description"
    tools = [cap.tool_name for cap in registry.all_capabilities()]
    assert len(tools) == len(set(tools))
    sampler = names["qc.sample_examples"]
    assert sampler.visual_output and sampler.entitlement == "ai:qc:analytics"
    for name in ("qc.registration_write_regions", "qc.segmentation_write_flags"):
        assert names[name].entitlement in (None, "free")


def test_misregistered_regions_are_written_snug_in_their_category(tmp_path):
    from plexora.plugins.qc.server import results

    make_qc_project(tmp_path, artifacts=("misregistration",))
    session = AgentSession()
    ok(invoke(session, "compute_registration_mismatch", {"project": "qcsynth",
                                                         "comparison": "DNA_2"}))
    seen = ok(invoke(session, "get_registration_check", {"project": "qcsynth",
                                                         "include_regions": True}))
    written = ok(invoke(session, "write_registration_regions", {"project": "qcsynth",
                                                                "comparison": "DNA_2"}))
    assert written["written"] and written["category"] == "registration"
    one = written["comparisons"]["DNA_2"]
    assert one["threshold"]["source"] == "auto" and one["denominator"] == "nuclear_area"
    assert one["highlighted_pct"] > 0
    rois = ok(invoke(session, "list_rois", {"project": "qcsynth"}))["rois"]
    assert {r["category_id"] for r in rois} == {"qc_registration"}
    meta = results.roi_meta("qcsynth")
    assert set(meta["class"].to_list()) == {"cross_cycle_registration_error"}
    if seen.get("mismatch_map"):
        assert seen["mismatch_map"]["threshold"]["source"] == "auto"
    # A step tighter: a lower bar, stored as a step, the old regions replaced.
    tighter = ok(invoke(session, "write_registration_regions", {"project": "qcsynth",
                                                                "comparison": "DNA_2",
                                                                "adjust": "tighter"}))
    bar = tighter["comparisons"]["DNA_2"]["threshold"]
    assert bar["source"] == "user_relative" and bar["offset_steps"] == 1
    assert bar["value"] < one["threshold"]["value"]
    assert set(tighter["removed"]) == set(written["written"])
    both = invoke(session, "write_registration_regions", {"project": "qcsynth",
                                                          "comparison": "all",
                                                          "adjust": "tighter"})
    assert not both["ok"] and both["error"]["code"] == "invalid_input"


def test_segmentation_flags_become_cell_reasons_and_clear_undoes_them(tmp_path):
    from plexora.plugins.qc.server import results

    info = make_qc_project(tmp_path, size=768, grid=30, levels=3, artifacts=(),
                           seg_errors={"merge": 6, "split": 6})
    session = AgentSession()
    ok(invoke(session, "run_segmentation_qc", {"project": "qcsynth"}))
    jobs.drain(180)
    written = ok(invoke(session, "write_segmentation_flags", {"project": "qcsynth",
                                                              "clusters": True}))
    assert written["excluded"]["seg_under"] > 0 and written["denominator"] > 0
    assert written["excluded"]["seg_irregular"] == 0
    assert written["thresholds"]["seg_under"]["threshold_source"] == "user"
    cells = results.cells("qcsynth")
    excluded = set(cells.filter(~cells["pass"])["cell_id"].to_list())
    assert len(excluded & set(info["truth"]["segmentation"]["under"])) >= 4
    status = ok(invoke(session, "get_segmentation_qc", {"project": "qcsynth",
                                                        "include_regions": True}))
    assert status["cell_calls"]["excluded"]["seg_under"] == written["excluded"]["seg_under"]
    assert "distribution" in status["clusters"]
    looser = ok(invoke(session, "write_segmentation_flags", {
        "project": "qcsynth", "adjust": {"under": "looser"}}))
    assert looser["thresholds"]["seg_under"]["threshold_source"] == "user_relative"
    assert looser["excluded"]["seg_under"] <= written["excluded"]["seg_under"]
    undo = written["receipt"]["undo_hint"]
    assert undo["tool"] == "write_segmentation_flags" and undo["arguments"]["clear"]
    cleared = ok(invoke(session, "write_segmentation_flags", {"project": "qcsynth",
                                                              "clear": True}))
    assert cleared["excluded"]["seg_under"] == 0


def test_the_export_says_why_each_region_and_cell_was_flagged(tmp_path):
    import csv

    make_qc_project(tmp_path, artifacts=("blur_local",))
    session = AgentSession()
    ok(invoke(session, "run_blur_check", {"project": "qcsynth", "channel": "DNA_2"}))
    jobs.drain(120)
    ok(invoke(session, "write_blur_regions", {"project": "qcsynth", "channel": "DNA_2"}))
    found = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))
    region = next(r for r in found["regions"] if r.get("roi_id"))
    assert region["category"] == "blur_focus" and region["class"] == "out_of_focus"
    assert region["threshold"] is not None and region["threshold_source"] == "auto"
    blur = next(c for c in found["categories"] if c["category"] == "blur_focus")
    assert blur["n_regions"] >= 1 and "out of focus" in blur["subtypes"]
    exported = ok(invoke(session, "export_qc", {"project": "qcsynth", "what": "provenance"}))
    files = exported["files"]
    body = json.loads(open(files["provenance"], encoding="utf-8").read())
    assert body["schema"] == "plexora.qc.provenance/1"
    record = body["regions"][0]
    assert record["category"] == "blur_focus" and record["tool"]["name"] == "blur"
    assert record["geometry_ref"].startswith("qc_regions.geojson#")
    assert record["threshold_source"] == "auto" and record["n_cells"]
    reasons = {r["reason"] for r in body["cell_reasons"]}
    assert "region:out_of_focus" in reasons
    with open(files["findings"], newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert {r["kind"] for r in rows} >= {"region", "cell_reason"}
    assert all(r["category"] for r in rows)
    geo = json.loads(open(files["regions"], encoding="utf-8").read())
    props = geo["features"][0]["properties"]
    assert props["category"] == "blur_focus" and props["threshold_source"] == "auto"
    both = ok(invoke(session, "export_qc", {"project": "qcsynth"}))
    with open(both["files"]["cells"], newline="", encoding="utf-8") as handle:
        table = list(csv.DictReader(handle))
    flagged = [r for r in table if r["reasons"]]
    assert flagged and all(r["qc_category"] == "blur_focus" for r in flagged)
    assert {r["flag_source"] for r in flagged} == {"roi"}


@pytest.mark.paid
def test_the_sampler_shows_rows_across_the_score_and_previews_a_step(tmp_path):
    make_qc_project(tmp_path, artifacts=("blur_local",))
    session = AgentSession()
    ok(invoke(session, "run_blur_check", {"project": "qcsynth", "channel": "DNA_2"}))
    jobs.drain(120)
    look = invoke(session, "sample_qc_examples", {"project": "qcsynth", "check": "blur",
                                                  "channel": "DNA_2"})
    result = ok(look)
    assert result["threshold"]["source"] == "auto" and not result["threshold"]["preview"]
    assert result["manifest"] and all("stratum" in m for m in result["manifest"])
    assert any(m["stratum"] == "global" for m in result["manifest"])
    assert result["distribution"]["n"] > 0
    preview = ok(invoke(session, "sample_qc_examples", {"project": "qcsynth", "check": "blur",
                                                        "channel": "DNA_2",
                                                        "adjust": "tighter"}))
    assert preview["threshold"]["preview"] and preview["threshold"]["offset_steps"] == 1
    assert preview["threshold"]["value"] < result["threshold"]["value"]
    # Nothing was stored by a preview.
    status = ok(invoke(session, "get_blur_check", {"project": "qcsynth", "channel": "DNA_2"}))
    assert status["blur_qc"]["results"][0]["threshold"]["source"] == "auto"
    refused = invoke(session, "sample_qc_examples", {"project": "qcsynth", "check": "blur",
                                                     "channel": "DNA_2", "adjust": "tighter",
                                                     "threshold": 0.4})
    assert not refused["ok"] and refused["error"]["code"] == "invalid_input"
    missing = invoke(session, "sample_qc_examples", {"project": "qcsynth",
                                                     "check": "registration"})
    assert not missing["ok"] and missing["error"]["code"] == "precondition_missing"


@pytest.mark.paid
def test_a_registration_region_retraces_to_its_map(tmp_path):
    from plexora.plugins.qc.server import polygons

    make_qc_project(tmp_path, artifacts=("misregistration",))
    session = AgentSession()
    ok(invoke(session, "compute_registration_mismatch", {"project": "qcsynth",
                                                         "comparison": "DNA_2"}))
    written = ok(invoke(session, "write_registration_regions", {"project": "qcsynth",
                                                                "comparison": "DNA_2"}))
    roi_id = written["written"][0]
    ok(invoke(session, "profile_image_qc", {"project": "qcsynth"}))
    jobs.drain(180)
    same = ok(invoke(session, "refine_qc_roi", {"project": "qcsynth", "roi_id": roi_id}))
    assert not same["refined"] and "score map" in same["skipped"][0]["why"]
    found = ok(invoke(session, "get_roi", {"project": "qcsynth", "roi_id": roi_id}))
    original = found["roi"]["geometry"] if "roi" in found else found["geometry"]
    ok(invoke(session, "update_roi", {"project": "qcsynth", "roi_id": roi_id,
                                      "geometry": {"type": "Polygon", "coordinates": [
                                          [[0, 0], [60, 0], [60, 60], [0, 60], [0, 0]]]}}))
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    back = ok(invoke(session, "refine_qc_roi", {"project": "qcsynth", "roi_id": roi_id,
                                                "force": True}))
    assert back["refined"] and back["refined"][0]["method"] == "registration_map"
    now = ok(invoke(session, "get_roi", {"project": "qcsynth", "roi_id": roi_id}))
    geometry = now["roi"]["geometry"] if "roi" in now else now["geometry"]
    assert polygons.geometry_hash(geometry) == polygons.geometry_hash(original)
