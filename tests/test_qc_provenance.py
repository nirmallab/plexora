"""Five categories over the classes: legacy projects migrate, a subtype is
never lost, and moving a region between categories is the user's say."""

import json

import pytest

from plexora.agent import AgentSession, invoke, registry
from tests.qc_fixtures import make_qc_project


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def ok(answer):
    assert answer["ok"], json.dumps(answer.get("error"), default=str)[:3000]
    return answer["result"]


def _box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                [x0, y0]]]}


def _legacy_project(tmp_path):
    """A project QC wrote before the five: a `qc_tissue_fold` category (QC's)
    holding a region QC wrote, and a hand-made "QC: Out of focus" one."""
    from plexora.plugins.qc.server import polygons, results
    from plexora.plugins.roi.server.repository import ROIRepository

    make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    repo = ROIRepository("qcsynth")
    state = repo.load()
    geometry = _box(100, 100, 300, 300)
    repo.apply(state["revision"], [
        {"op": "category.create", "category": {"id": "qc_tissue_fold",
                                               "label": "QC: Tissue fold",
                                               "color": "#123456", "sort_order": 901}},
        {"op": "roi.create", "feature": {"id": "qcroi_old", "category_id": "qc_tissue_fold",
                                         "name": "QC exclude: tissue fold · all channels",
                                         "geometry": geometry,
                                         "notes": "tissue fold\nqc:cand_0123456789"}}])
    document = results.load("qcsynth")
    result = results.ensure_active(document, "qcsynth")
    result["candidates"]["cand_0123456789"] = {
        "id": "cand_0123456789", "class": "tissue_fold", "detector": "diffuse_bright",
        "roi_id": "qcroi_old", "action": "exclude", "geometry": geometry, "channels": [],
        "scope": "all_channels", "user_state": {}, "created_by": "agent"}
    results.put_result(document, result)
    results.save("qcsynth", document)
    results.upsert_roi_meta("qcsynth", [{
        "roi_id": "qcroi_old", "candidate_id": "cand_0123456789",
        "result_id": result["result_id"], "class": "tissue_fold", "action": "exclude",
        "detector": "diffuse_bright", "created_by": "agent", "user_edited": False,
        "approved": False, "locked": False, "deleted": False, "removed_from_qc": False,
        "written_geometry_hash": polygons.geometry_hash(geometry),
        "written_category_id": "qc_tissue_fold"}])
    ok(invoke(session, "create_roi", {"project": "qcsynth", "category": "QC: Out of focus",
                                      "geometry": _box(400, 400, 500, 500)}))
    return session


def test_a_legacy_project_moves_into_the_five_keeping_each_class(tmp_path):
    from plexora.plugins.qc.server import results
    from plexora.plugins.roi.server.repository import ROIRepository

    session = _legacy_project(tmp_path)
    read = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))
    assert set(read["sync"]["legacy"]) >= {"qc_tissue_fold"}
    refreshed = ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    assert "qcroi_old" in refreshed["sync"]["migrated"]
    state = ROIRepository("qcsynth").load()
    ids = {c["id"]: c for c in state["categories"]}
    assert "qc_tissue_fold" not in ids and "qc_tissue_acquisition" in ids
    assert not any(c["label"] == "QC: Out of focus" for c in state["categories"])
    # The user's colour on the legacy category is carried to its target.
    assert ids["qc_tissue_acquisition"]["color"] == "#123456"
    features = {f["id"]: f for f in state["images"]["default"]["features"]}
    assert features["qcroi_old"]["category_id"] == "qc_tissue_acquisition"
    rows = {r["roi_id"]: r for r in results.roi_meta("qcsynth").to_dicts()}
    old = rows["qcroi_old"]
    assert old["class"] == "tissue_fold" and not old["user_edited"]
    assert old["written_category_id"] == "qc_tissue_acquisition"
    drawn = next(r for r in rows.values() if r["roi_id"] != "qcroi_old")
    assert drawn["class"] == "out_of_focus"
    assert features[drawn["roi_id"]]["category_id"] == "qc_blur_focus"
    regions = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))["regions"]
    assert {r["class"] for r in regions if r.get("roi_id")} == {"tissue_fold", "out_of_focus"}
    # Nothing is migrated twice.
    again = ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    assert not again["sync"].get("migrated") and not again["sync"]["relabelled"]


def test_a_half_written_migration_is_repaired_not_relabelled(tmp_path):
    from plexora.plugins.qc.server import results, roi_link
    from plexora.plugins.roi.server.repository import ROIRepository

    session = _legacy_project(tmp_path)
    repo = ROIRepository("qcsynth")
    state = repo.load()
    ops = roi_link._category_ops(state, ["tissue_acquisition"])
    ops.append({"op": "category.delete", "id": "qc_tissue_fold", "orphans": "reassign",
                "reassign_to": "qc_tissue_acquisition"})
    repo.apply(state["revision"], ops)     # the ROIs moved, the meta row did not
    refreshed = ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    assert "qcroi_old" not in refreshed["sync"]["relabelled"]
    row = next(r for r in results.roi_meta("qcsynth").to_dicts() if r["roi_id"] == "qcroi_old")
    assert row["class"] == "tissue_fold"
    assert row["written_category_id"] == "qc_tissue_acquisition"


def test_moving_a_region_to_another_category_is_the_users_say(tmp_path):
    session = _legacy_project(tmp_path)
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    ok(invoke(session, "update_roi", {"project": "qcsynth", "roi_id": "qcroi_old",
                                      "category": "QC: Staining / signal artifact"}))
    moved = ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    assert "qcroi_old" in moved["sync"]["relabelled"]
    regions = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))["regions"]
    region = next(r for r in regions if r.get("roi_id") == "qcroi_old")
    assert region["class"] == "staining_artifact" and region["category"] == "staining_signal"
    # A category that is not QC's takes the region out of QC.
    ok(invoke(session, "update_roi", {"project": "qcsynth", "roi_id": "qcroi_old",
                                      "category": "Tumour"}))
    out = ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    assert "qcroi_old" in out["sync"]["removed"]


def test_the_vocabulary_and_provenance_records_name_every_category(tmp_path):
    from plexora.plugins.qc.server import provenance, schemas

    vocabulary = provenance.vocabulary()
    assert [c["id"] for c in vocabulary["categories"]] == [*schemas.CATEGORY_IDS, "review"]
    assert vocabulary["classes"]["stitching_or_tile_seam"] == "tissue_acquisition"
    assert vocabulary["reasons"]["seg_over"] == "segmentation"
    record = provenance.region_record(
        {"id": "cand_x", "class": "out_of_focus", "detector": "blur", "detector_version": "1",
         "origin": "check", "score": 0.7, "channels": ["DNA_2"],
         "metrics": {"threshold": 0.4, "threshold_source": "agent_refined",
                     "offset_steps": 1},
         "ai_decision": {"verdict": "artifact", "confidence": "sure",
                         "source": "score_review"}},
        live={"roi_id": "r1", "category": "blur_focus", "class": "out_of_focus"})
    assert record["category"] == "blur_focus" and record["score_kind"] == "blur_score"
    assert record["threshold_source"] == "agent_refined" and record["offset_steps"] == 1
    assert record["ai"]["source"] == "score_review" and record["tool"]["origin"] == "check"
    rows = provenance.findings_rows({"regions": [record], "cell_reasons": [],
                                     "marker_reasons": []})
    assert set(rows[0]) == set(provenance.FINDINGS_COLUMNS)


def test_the_agents_notes_are_kept_with_its_judgment():
    """What the agent wrote on a look travels with the region: in its `ai`
    record, the findings row, the ROI's notes (with nothing in them that reads
    as a QC token), and -- for an image check's region with none of its own --
    from the check's score review."""
    from plexora.plugins.qc.server import provenance, roi_link

    candidate = {"id": "cand_0123456789", "class": "tissue_fold", "detector": "diffuse_bright",
                 "detector_version": "2", "notes": ["Folded edge, nuclei doubled.",
                                                    "Folded edge, nuclei doubled.",
                                                    "see qc:cand_9999999999 qc-class:other"],
                 "ai_decision": {"verdict": "artifact", "confidence": "sure"}}
    record = provenance.region_record(candidate, live={"roi_id": "r1",
                                                       "category": "tissue_acquisition",
                                                       "class": "tissue_fold"})
    assert record["ai"]["notes"].startswith("Folded edge, nuclei doubled. see")
    assert record["ai"]["notes"].count("Folded edge") == 1
    rows = provenance.findings_rows({"regions": [record], "cell_reasons": [],
                                     "marker_reasons": []})
    assert rows[0]["ai_notes"] == record["ai"]["notes"]
    notes = roi_link.notes_for(candidate)
    agent = next(line for line in notes.split("\n") if line.startswith("agent: "))
    assert "qc:cand_9999999999" not in agent and "qc-class:" not in agent
    assert roi_link.NOTE_TOKEN.findall(notes) == ["cand_0123456789"]
    # Notes alone still make an `ai` record; nothing at all makes none.
    assert provenance.region_summary({"notes": ["Looks real."]})["ai"]["notes"] == "Looks real."
    assert provenance.region_summary({})["ai"] is None
    # A check's region borrows its score review's notes.
    check = {"id": "cand_b", "origin": "check", "detector": "blur", "check_unit": "blur:DNA_2",
             "channels": ["DNA_2"], "ai_decision": {"verdict": "artifact"}}
    checks = {"blur": {"DNA_2": {"notes": ["Soft field at the top edge."]}}}
    assert provenance.region_summary(check, checks=checks)["ai"]["notes"] == \
        "Soft field at the top edge."
    assert provenance.region_summary(check)["ai"]["notes"] is None
    assert provenance.notes_text(["x" * 400, "y" * 400]).endswith("…")


def test_one_cells_record_names_the_value_and_the_bar_it_crossed(tmp_path):
    """The hover card's record of one cell: each reason with the value that
    crossed its module's cutoff, the cutoff, the side, the channels and the
    agent's verdict and notes; a marker flag with its own value and bar."""
    from plexora.plugins.qc.server import provenance, strictness
    from plexora.plugins.qc.server.cells import calls

    make_qc_project(tmp_path, artifacts=("cycle_dropout",))
    ds = AgentSession().data("qcsynth")
    looked = {"low": {"verdict": "accept"}, "high": {"verdict": "accept"}}
    modules = {n: {"available": True, "state": "decided", "decision": looked}
               for n in ("counterstain_intensity", "segmentation_area", "cycle_stability")}
    modules["cycle_stability"]["notes"] = ["Nuclei faded by cycle 2 at the lower edge."]
    result = {"result_id": "qr_cell", "candidates": {}, "cycles": [
        {"index": 1, "channels": ["DNA_1", "CD3", "CD8"], "nuclear": "DNA_1"},
        {"index": 2, "channels": ["DNA_2", "CD20"], "nuclear": "DNA_2"}],
        "cells": {"modules": modules}}
    frame, _pairs, summary = calls.derive(ds, result, strictness.thresholds("strict"))
    result["cells"] = {**summary, "modules": modules}
    lost = next(r for r in frame.to_dicts() if "cycle_loss" in (r["reasons"] or []))
    record = provenance.cell_record(result, lost)
    entry = next(r for r in record["reasons"] if r["reason"] == "cycle_loss")
    assert entry["measure"] == "m_cycle_log10_ratio" and entry["side"] == "low"
    assert entry["value"] == pytest.approx(lost["m_cycle_log10_ratio"], rel=1e-5)
    assert entry["cutoff"] == pytest.approx(summary["evidence"]["cycle_loss"]["cutoffs"]["low"])
    assert entry["value"] < entry["cutoff"]
    assert entry["channels"] == ["DNA_1", "DNA_2"] and entry["verdict"] == "accept"
    assert entry["notes"] == "Nuclei faded by cycle 2 at the lower edge."
    assert entry["status"] in ("fail", "warn") and entry["category"]
    assert record["reasons"][0]["reason"] == (lost["primary_reason"]
                                              or record["reasons"][0]["reason"])

    flagged = {"cell_id": 7, "pass": True, "action": "pass", "primary_reason": "",
               "reasons": [], "excluded_by": [], "unreliable_markers": ["CD3"],
               "marker_flags": ["CD3|extreme_value|exclude"], "roi_ids": [],
               "m_outlier_CD3": 9.5}
    marked = {"cells": {"marker_evidence": [
        {"marker": "CD3", "reason": "extreme_value", "module": "channel_outlier:CD3",
         "status": "unreliable", "cutoffs": {"high": 7.0, "space": "log1p"}}],
        "modules": {"channel_outlier:CD3": {"notes": ["Bright specks on the cells."]}}}}
    record = provenance.cell_record(marked, flagged)
    marker = record["markers"][0]
    assert marker["status"] == "unreliable" and marker["value"] == 9.5
    assert marker["cutoff"] == 7.0 and marker["notes"] == "Bright specks on the cells."
    assert record["reasons"] == [] and record["pass"]
    clean = provenance.cell_record({}, None, cell_id=12)
    assert clean["cell_id"] == 12 and clean["pass"] and clean["reasons"] == []
