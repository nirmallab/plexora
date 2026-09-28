"""The Free half of QC: manual regions, results, strictness, exports, reset.

Nothing here needs a licence: a user drawing regions in a QC category gets
the cells flagged, can change the strictness, export and write the calls back,
and can reset and restore -- the same store a session writes.
"""

import csv
import json

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from plexora.agent.policy import Policy
from plexora.plugins.qc.server import results, schemas, strictness
from tests.qc_fixtures import make_qc_project


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def _box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                [x0, y0]]]}


def test_a_region_drawn_by_hand_in_a_qc_category_flags_the_cells(tmp_path):
    info = make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    ok(invoke(session, "create_roi", {"project": "qcsynth", "category": "QC: Tissue fold",
                                      "geometry": _box(200, 200, 500, 500)}))
    refreshed = ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    assert refreshed["sync"]["adopted"]
    found = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))
    assert found["regions"][0]["class"] == "tissue_fold"
    assert found["regions"][0]["created_by"] == "user"
    cells = results.cells("qcsynth")
    # Membership is by mask overlap: every cell well inside fails, no cell
    # well outside does, and a cell the edge cuts goes by how much of it is in.
    well_inside = {c["id"] for c in info["cells"] if 215 < c["x"] < 485 and 215 < c["y"] < 485}
    near = {c["id"] for c in info["cells"] if 185 < c["x"] < 515 and 185 < c["y"] < 515}
    failed = set(cells.filter(~cells["pass"])["cell_id"].to_list())
    assert well_inside and well_inside <= failed <= near
    assert set(cells.filter(~cells["pass"])["roi_method"].unique().to_list()) == {"mask"}
    reasons = cells.filter(~cells["pass"])["primary_reason"].unique().to_list()
    assert reasons == ["region:tissue_fold"]


def test_a_user_region_always_excludes_whatever_the_strictness(tmp_path):
    make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    ok(invoke(session, "create_roi", {"project": "qcsynth", "category": "QC: Out of focus",
                                      "geometry": _box(300, 300, 400, 400)}))
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    counts = {}
    for preset in ("lenient", "standard", "strict"):
        changed = ok(invoke(session, "set_qc_strictness", {"project": "qcsynth",
                                                           "preset": preset}))
        counts[preset] = changed["summary"]["cells"]["n_fail"]
        regions = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))["regions"]
        assert [r["action"] for r in regions] == ["exclude"], preset
    # The region excludes under every preset; how much of a cell must lie in
    # it to count is the preset's (`cells.roi_overlap_fraction`).
    assert 0 < counts["lenient"] <= counts["standard"] <= counts["strict"]


def test_strictness_decisions_are_nested(tmp_path):
    """Property: under every combination of an agent's answers, what Lenient
    excludes Standard excludes, and what Standard excludes Strict does."""
    rng = np.random.default_rng(0)
    order = {a: i for i, a in enumerate(schemas.ACTIONS)}
    tables = [strictness.thresholds(p) for p in ("lenient", "standard", "strict")]
    for _ in range(2000):
        decision = {"artifact_class": rng.choice(schemas.ARTIFACT_CLASSES[:-1]),
                    "severity": rng.choice(schemas.SEVERITIES),
                    "confidence": rng.choice(list(schemas.AI_CONFIDENCE)),
                    "exclude_recommended": rng.choice([True, False, None])}
        measurement = {"tissue_fraction": float(rng.choice([None, 0.0001, 0.001, 0.01, 0.2,
                                                            0.5]) or 0) or None}
        actions = [strictness.decide_artifact(decision, measurement, t)["action"]
                   for t in tables]
        assert order[actions[0]] <= order[actions[1]] <= order[actions[2]], \
            (decision, measurement, actions)


def test_cell_calls_are_nested_across_presets(tmp_path):
    from plexora.plugins.qc.server.cells import calls, modules

    make_qc_project(tmp_path, artifacts=("cycle_dropout",))
    session = AgentSession()
    ds = session.data("qcsynth")
    names = ["counterstain_intensity", "segmentation_area", "cycle_stability"]
    result = {"result_id": "qr_test", "candidates": {}, "cycles": [
        {"index": 1, "channels": ["DNA_1", "CD3", "CD8"], "nuclear": "DNA_1"},
        {"index": 2, "channels": ["DNA_2", "CD20"], "nuclear": "DNA_2"}],
        "cells": {"modules": {n: {"available": True, "state": "decided",
                                  "decision": {"low": {"offset_steps": o, "veto": False},
                                               "high": {"offset_steps": -o, "veto": False}}}
                              for n, o in zip(names, (1, -1, 0))}}}
    failing = []
    for preset in ("lenient", "standard", "strict"):
        frame, _pairs, _summary = calls.derive(ds, result, strictness.thresholds(preset),
                                               pairs=None)
        failing.append(set(frame.filter(~frame["pass"])["cell_id"].to_list()))
    assert failing[0] <= failing[1] <= failing[2]
    assert failing[2]
    assert modules.module("cycle_stability").available(
        ds, {"cycles": {"cycles": result["cycles"]}})[0]


def test_exports_and_a_csv_source_write(tmp_path):
    info = make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    ok(invoke(session, "create_roi", {"project": "qcsynth", "category": "QC: Tissue fold",
                                      "geometry": _box(100, 100, 300, 300)}))
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    exported = ok(invoke(session, "export_qc", {"project": "qcsynth"}))
    rows = list(csv.DictReader(open(exported["files"]["cells"], encoding="utf-8")))
    assert len(rows) == len(info["cells"])
    assert {"cell_id", "pass", "primary_reason", "reasons", "roi_ids"} <= set(rows[0])
    regions = json.loads(open(exported["files"]["regions"], encoding="utf-8").read())
    assert regions["features"][0]["properties"]["class"] == "tissue_fold"
    refused = invoke(session, "write_qc_to_source", {"project": "qcsynth", "confirm": True})
    assert not refused["ok"]            # source writes need the server's permission
    written = ok(invoke(session, "write_qc_to_source", {"project": "qcsynth", "confirm": True},
                        policy=Policy.from_flags(allow_source_writes=True)))
    assert written["written"]["n_fail"] > 0
    table = list(csv.DictReader(open(tmp_path / "_qcsynth_files" / "cells.csv",
                                     encoding="utf-8")))
    assert "plexora_qc_pass" in table[0] and "plexora_qc_reasons" in table[0]
    again = invoke(session, "write_qc_to_source", {"project": "qcsynth", "confirm": True},
                   policy=Policy.from_flags(allow_source_writes=True))
    assert not again["ok"] and again["error"]["code"] == "conflict"


def test_reset_snapshots_and_restore_puts_it_back(tmp_path):
    make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    ok(invoke(session, "create_roi", {"project": "qcsynth", "category": "QC: Tissue fold",
                                      "geometry": _box(100, 100, 300, 300)}))
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    before = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))
    reset = ok(invoke(session, "reset_qc", {"project": "qcsynth", "confirm": True}))
    # A region the user drew is theirs: reset never deletes it.
    assert reset["kept_rois"] and not reset["deleted_rois"]
    cleared = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))
    assert cleared["active_result_id"] is None or cleared["sync"]["adopted"]
    undone = ok(invoke(session, "undo_operation",
                       {"operation_id": reset["receipt"]["operation_id"]}))
    assert undone
    after = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))
    assert after["active_result_id"] == before["active_result_id"]


@pytest.mark.paid
def test_a_session_region_changes_action_with_strictness_and_undo_puts_it_back(tmp_path):
    from tests.test_qc_session import QCOracle, drive

    info = make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    started = ok(invoke(session, "qc_session_start", {"project": "qcsynth",
                                                      "map_cell_um": 25.0}))
    jobs.drain(180)

    class Moderate(QCOracle):
        def answer(self, packet, session_id):
            out = super().answer(packet, session_id)
            if out.get("verdict") == "artifact":
                out["severity"] = "minor"
            return out

    drive(session, started["session_id"], Moderate(info))
    ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"]}))
    region = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))["regions"][0]
    assert region["action"] == "warn"                      # minor, under standard
    strict = ok(invoke(session, "set_qc_strictness", {"project": "qcsynth",
                                                      "preset": "strict"}))
    assert strict["renamed"] and strict["renamed"][0]["to"] == "exclude"
    rois = ok(invoke(session, "list_rois", {"project": "qcsynth"}))["rois"]
    assert rois[0]["name"].startswith("QC exclude:")
    ok(invoke(session, "undo_operation", {"operation_id": strict["receipt"]["operation_id"]}))
    rois = ok(invoke(session, "list_rois", {"project": "qcsynth"}))["rois"]
    assert rois[0]["name"].startswith("QC warn:")
