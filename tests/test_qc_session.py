"""A QC session driven end to end by a scripted agent that knows the truth.

The stand-in agent answers each packet from the scene's painted artifacts
(`plexora.ai.qc_scenes`): a careful expert (oracle) or one that always says
"clean" (lazy). What the tests pin is the server's side: every artifact
confirmed becomes an ROI of its class, clean channels close clean, every write
is receipted and undoable, packets carry no strictness, and the user's edits
win.
"""

import json

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from tests.qc_fixtures import make_qc_project

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def _grid_truth(mask, grid):
    ny, nx = grid["shape"]
    s = grid["cell_full_px"]
    out = np.zeros((ny, nx), dtype=bool)
    for iy in range(ny):
        for ix in range(nx):
            block = mask[int(iy * s):int((iy + 1) * s), int(ix * s):int((ix + 1) * s)]
            out[iy, ix] = block.size and block.mean() >= 0.3
    return out


class QCOracle:
    """Answers QC packets from the scene's truth, reading candidate masks from
    the session record (a real agent would read the pictures)."""

    def __init__(self, info, style="oracle"):
        self.info = info
        self.style = style

    def _record(self, session_id):
        from plexora.plugins.qc.server.engine import store

        return store().load(session_id)

    def _truth_for(self, record, unit):
        from plexora.plugins.qc.server import candidates as cand

        grid = record["scan"][unit["project"]]["grid"]
        mask = cand.decode_mask(unit["mask"])
        best = (0.0, None)
        for region in self.info["truth"]["regions"]:
            truth = _grid_truth(region["mask"], grid)
            inter = np.logical_and(mask, truth).sum()
            if not inter:
                continue
            score = max(inter / np.logical_or(mask, truth).sum(), inter / mask.sum())
            if set(region["channels"]) & set(unit.get("channels") or []) and score > best[0]:
                best = (score, region)
        return best

    def answer(self, packet, session_id):
        kind = packet["kind"]
        ev = packet["evidence"]
        record = self._record(session_id)
        truth = self.info["truth"]
        if kind == "channel_audit":
            verdicts = {}
            for row in ev["rows"]:
                name = row["channel"]
                bad = [r for r in truth["regions"] if name in r["channels"]] or \
                    truth["channels"].get(name) == "failed"
                if self.style == "lazy" or not bad:
                    verdicts[name] = {"verdict": "clean"}
                    continue
                named = []
                for c in row.get("candidates") or []:
                    unit = record["units"][f"{ev['project']}::candidate::{c['id']}"]
                    score, _region = self._truth_for(record, unit)
                    if score >= 0.2 or truth["channels"].get(name) == "failed":
                        named.append(c["label"])
                verdicts[name] = {"verdict": "suspicious", "where": named or ["elsewhere"]}
            return {"kind": kind, "verdicts": verdicts}
        unit = record["units"][f"{packet['units'][0]['project']}::"
                               f"{packet['units'][0]['type']}::{packet['units'][0]['id']}"]
        if kind == "artifact_confirm":
            score, region = self._truth_for(record, unit)
            if truth["channels"].get(unit.get("channel")) == "failed":
                return {"kind": kind, "verdict": "artifact",
                        "artifact_class": "empty_or_failed_channel", "severity": "severe",
                        "boundary": "covers", "scope": "channel", "confidence": "sure"}
            if region is None or score < 0.2:
                return {"kind": kind, "verdict": "not_artifact", "confidence": "sure"}
            scope = "all_channels" if len(region["channels"]) >= 5 else \
                ("channel" if len(region["channels"]) == 1 else None)
            return {"kind": kind, "verdict": "artifact", "artifact_class": region["class"],
                    "severity": "severe", "boundary": "covers", "scope": scope,
                    "exclude_recommended": True, "confidence": "sure"}
        if kind == "artifact_scope":
            _score, region = self._truth_for(record, unit)
            wanted = set(region["channels"]) if region else set()
            best = max(ev["options"], key=lambda o: len(wanted & set(o["channels"]))
                       / len(wanted | set(o["channels"])))
            return {"kind": kind, "chosen": best["id"], "confidence": "sure"}
        if kind == "artifact_localize":
            return {"kind": kind, "chosen": "current", "confidence": "fairly_sure"}
        if kind == "artifact_grid":
            return {"kind": kind, "cells": list(ev.get("pre_selected") or [])[:64]
                    or [ev.get("allowed", ["A1"])[0]], "confidence": "fairly_sure"}
        if kind == "final_qc_review":
            return {"kind": kind, "verdict": "consistent"}
        if kind in ("cell_intensity", "cell_area", "cycle_stability", "channel_outlier"):
            return {"kind": kind, "low": "accept", "high": "accept", "confidence": "sure"}
        raise AssertionError(kind)


def drive(session, session_id, agent, limit=80):
    seen = []
    result = ok(invoke(session, "qc_next", {"session_id": session_id, "wait_s": 20}))
    for _ in range(limit):
        if result["state"] != "decision":
            assert result["state"] in ("decided", "done"), result
            return seen
        packet = result["packet"]
        seen.append(packet)
        answered = ok(invoke(session, "qc_answer", {
            "session_id": session_id, "packet_id": packet["packet_id"],
            "answer": agent.answer(packet, session_id)}))
        result = answered["next"]
    raise AssertionError("the session did not finish")


def start(session, project="qcsynth", **options):
    started = ok(invoke(session, "qc_session_start", {"project": project,
                                                      "map_cell_um": 25.0, **options}))
    jobs.drain(180)
    return started


def rois_of(session, project="qcsynth"):
    return ok(invoke(session, "list_rois", {"project": project}))["rois"]


def test_the_oracle_confirms_every_artifact_as_a_region_of_its_class(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation", "fold", "cycle_dropout"))
    session = AgentSession()
    started = start(session)
    packets = drive(session, started["session_id"], QCOracle(info))
    kinds = [p["kind"] for p in packets]
    assert kinds[0] == "channel_audit"
    assert "artifact_confirm" in kinds
    rois = rois_of(session)
    categories = {r["category_id"] for r in rois}
    assert "qc_saturation_or_clipping" in categories
    assert "qc_tissue_fold" in categories or "qc_autofluorescence" in categories
    assert "qc_cycle_specific_tissue_loss" in categories or \
        "qc_tissue_damage_or_detachment" in categories
    assert all(r["name"].startswith("QC ") for r in rois)
    # Every packet stayed small, carried at most two images, and no strictness.
    for packet in packets:
        assert len(packet["images"]) <= 2
        assert len(json.dumps(packet)) < 40_000
        text = json.dumps(packet["evidence"])
        assert "strict" not in text and "lenient" not in text
    finished = ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"]}))
    assert finished["result"]["active"]
    status = ok(invoke(session, "qc_session_status", {"session_id": started["session_id"]}))
    channels = {u["id"]: u for u in status["units"] if u["type"] == "channel"}
    assert channels["CD8"]["state"] in ("clean", "flagged")
    assert channels["CD3"]["state"] == "flagged"


def test_every_region_is_a_child_receipt_and_rollback_removes_them(tmp_path):
    make_qc_project(tmp_path, artifacts=("saturation",))
    info = make_qc_project(tmp_path, name="qcsynth", artifacts=("saturation",))
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], QCOracle(info))
    rois = rois_of(session)
    assert rois
    audit = [json.loads(line) for line in
             (tmp_path / ".agent" / "audit.jsonl").read_text().splitlines()]
    children = [line for line in audit if line.get("qc_session") == started["session_id"]
                and line["operation_id"].startswith(started["receipt"]["operation_id"] + ".")]
    assert len(children) == len(rois)
    assert all(line["receipt"]["undo_hint"]["tool"] == "delete_roi" for line in children)
    rolled = ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"],
                                                      "action": "rollback"}))
    assert len(rolled["undone"]) == len(rois) and not rolled["refused"]
    assert rois_of(session) == []


def test_a_clean_scene_writes_nothing(tmp_path):
    info = make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], QCOracle(info))
    assert rois_of(session) == []
    finished = ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"]}))
    assert finished["summary"]["regions"]["exclude"] == 0


def test_propose_mode_writes_on_commit_only(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    started = start(session, mode="propose")
    drive(session, started["session_id"], QCOracle(info))
    assert rois_of(session) == []
    committed = ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"],
                                                         "action": "commit"}))
    assert committed["written"]
    assert rois_of(session)


def test_a_rerun_replays_every_answer_from_the_memo(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    first = start(session)
    asked = drive(session, first["session_id"], QCOracle(info))
    ok(invoke(session, "qc_session_finish", {"session_id": first["session_id"],
                                             "action": "rollback"}))
    second = start(session)
    again = drive(session, second["session_id"], QCOracle(info))
    assert len(again) < len(asked)
    status = ok(invoke(session, "qc_session_status", {"session_id": second["session_id"]}))
    assert status["replayed"] >= 1
    assert rois_of(session)


def test_stale_and_invalid_answers_are_refused(tmp_path):
    make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    started = start(session)
    sid = started["session_id"]
    result = ok(invoke(session, "qc_next", {"session_id": sid, "wait_s": 20}))
    packet = result["packet"]
    assert packet["kind"] == "channel_audit"
    # A verdict missing for a row is refused with the rows it needs.
    refused = invoke(session, "qc_answer", {"session_id": sid, "packet_id": packet["packet_id"],
                                            "answer": {"kind": "channel_audit", "verdicts": {}}})
    assert not refused["ok"] and refused["error"]["code"] == "invalid_input"
    assert refused["error"]["detail"]["missing"]
    # The same packet comes back until it is answered.
    again = ok(invoke(session, "qc_next", {"session_id": sid}))
    assert again["packet"]["packet_id"] == packet["packet_id"]
    stale = invoke(session, "qc_answer", {"session_id": sid, "packet_id": "pk_9999",
                                          "answer": {"kind": "channel_audit", "verdicts": {}}})
    assert not stale["ok"] and stale["error"]["code"] == "conflict"


def test_cells_in_a_lost_region_fail_and_the_calls_are_stored(tmp_path):
    from plexora.plugins.qc.server import results

    info = make_qc_project(tmp_path, artifacts=("cycle_dropout",))
    session = AgentSession()
    started = start(session)
    packets = drive(session, started["session_id"], QCOracle(info))
    assert any(p["kind"] in ("cycle_stability", "cell_intensity") for p in packets)
    ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"]}))
    cells = results.cells("qcsynth")
    assert cells is not None and cells.height == len(info["cells"])
    lost = {cid for cid, reasons in info["truth"]["cells"].items() if "cycle_loss" in reasons}
    assert lost
    failed = set(cells.filter(~cells["pass"])["cell_id"].to_list())
    assert len(lost & failed) >= 0.8 * len(lost), (len(lost & failed), len(lost))
    # Cells far from the dropout mostly pass.
    assert len(failed - lost) <= 0.1 * cells.height
    reasons = {r for row in cells.filter(~cells["pass"])["reasons"].to_list() for r in row}
    assert reasons & {"cycle_loss", "region:cycle_specific_tissue_loss",
                      "region:tissue_damage_or_detachment"}
    row = cells.filter(~cells["pass"]).row(0, named=True)
    assert row["primary_reason"] in row["reasons"]


def test_the_report_states_its_denominators(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation", "cycle_dropout"))
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], QCOracle(info))
    ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"]}))
    report = ok(invoke(session, "qc_report", {"session_id": started["session_id"]}))
    text = open(report["html"], encoding="utf-8").read()
    assert "Tissue excluded" in text and "data:image/png;base64," in text
    assert "union of excluded regions on tissue" in text
    assert open(report["pdf"], "rb").read(4) == b"%PDF"
    d = report["denominators"]
    assert 0 < d["excluded_tissue_fraction"] < 0.5
    assert d["cells"] == len(info["cells"]) and d["cells_excluded"] > 0
