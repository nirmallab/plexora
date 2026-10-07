"""The QC session's visual pass: after the checks are reviewed one
`visual_scan` packet is served; while it is out the agent writes regions
into the session with `segment_qc_roi`, which merge with the session's own,
take in the detector candidates inside them, and are consolidated at the
close -- through `FakeSamBackend` and the scripted oracle."""

import json
from pathlib import Path

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.plugins.qc.server import schemas
from plexora.vision import sam, sam_backend
from plexora.vision import segment as segmenter
from tests.qc_fixtures import make_qc_project
from tests.test_qc_session import QCOracle, classes_of, start

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])
    segmenter.forget()
    yield
    segmenter.forget()


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


ONLY_VISUAL = {"checks": {"blur": False, "registration": False, "segmentation": False,
                          "artifacts": False, "visual": True}}


def _bbox(info, name):
    region = next(r for r in info["truth"]["regions"] if r["name"] == name)
    ys, xs = np.nonzero(region["mask"])
    return float(xs.min()), float(ys.min()), float(xs.max() - xs.min()), float(ys.max() - ys.min())


def _drive_to(session, session_id, oracle, kind, limit=60):
    """The oracle answers until a packet of `kind` is in hand; returns it."""
    result = ok(invoke(session, "qc_next", {"session_id": session_id, "wait_s": 20}))
    for _ in range(limit):
        assert result["state"] == "decision", result
        packet = result["packet"]
        if packet["kind"] == kind:
            return packet
        answered = ok(invoke(session, "qc_answer", {
            "session_id": session_id, "packet_id": packet["packet_id"],
            "answer": oracle.answer(packet, session_id)}))
        result = answered["next"]
    raise AssertionError(f"no {kind} packet came")


def _finish_with(session, session_id, oracle, packet_id, answer, limit=60):
    """`answer` goes to the packet in hand; the oracle takes the rest."""
    result = ok(invoke(session, "qc_answer", {"session_id": session_id, "packet_id": packet_id,
                                              "answer": answer}))["next"]
    for _ in range(limit):
        if result["state"] != "decision":
            assert result["state"] in ("decided", "done"), result
            return
        packet = result["packet"]
        result = ok(invoke(session, "qc_answer", {
            "session_id": session_id, "packet_id": packet["packet_id"],
            "answer": oracle.answer(packet, session_id)}))["next"]
    raise AssertionError("the session did not finish")


def test_the_visual_pass_writes_into_the_session_and_its_regions_are_kept(tmp_path, monkeypatch):
    info = make_qc_project(tmp_path, size=512, grid=20, artifacts=("fold",))
    session = AgentSession()
    oracle = QCOracle(info)
    x, y, w, h = _bbox(info, "fold")
    box = {"x": x + w * 0.3, "y": y + h * 0.3, "width": w * 0.3, "height": h * 0.3}
    other = {"x": x + w * 0.65, "y": y + h * 0.65, "width": w * 0.15, "height": h * 0.15}
    with sam.use_backend(sam_backend.FakeSamBackend()):
        started = start(session, **ONLY_VISUAL)
        sid = started["session_id"]
        assert "visual:scan" in started["checks"]
        # Before the pass, the session owns its regions.
        early = invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "session_id": sid, "box": box, "artifact_class": "tissue_fold"})
        assert not early["ok"] and early["error"]["code"] == "conflict"
        packet = _drive_to(session, sid, oracle, "visual_scan")
        assert packet["allowed"] == ["done", "nothing_found"]
        evidence = packet["evidence"]
        assert evidence["session_id"] == sid and packet["images"][0]["artifact_id"]
        assert evidence["max_regions"] == schemas.VISUAL["max_regions"]
        assert evidence["tools"]["outline"] == "segment_qc_roi"
        written = ok(invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "session_id": sid, "box": box, "artifact_class": "tissue_fold",
            "confidence": "sure", "severity": "severe",
            "reasoning": "A bright doubled band across the tissue.",
            "evidence_artifacts": [packet["images"][0]["artifact_id"]]}))
        assert written["written"] and written["roi_id"], written
        assert written["state"] == "confirmed_exclude" and written["written_so_far"] == 1
        assert written["receipt"]["undo_hint"]["children"]
        # The zoom, during the pass, outlines the session's regions -- not the
        # project's last finished result (none here).
        zoom = ok(invoke(session, "render_artifact_overview", {
            "project": "qcsynth", "region": {"x": x, "y": y, "width": w, "height": h}}))
        assert written["roi_id"] in [r["roi_id"] for r in zoom["manifest"]["regions"]]
        assert "bbox" not in zoom["manifest"]["regions"][0]
        assert set(zoom["manifest"]["grid"]) == {"rows", "columns"}
        again = ok(invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "session_id": sid, "box": box, "artifact_class": "tissue_fold"}))
        assert not again["written"]
        assert again["duplicate_of"]["candidate_id"] == written["candidate_id"]
        # The pass's allowance.
        monkeypatch.setitem(schemas.VISUAL, "max_regions", 1)
        capped = invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "session_id": sid, "box": other, "artifact_class": "tissue_fold"})
        assert not capped["ok"] and "regions" in capped["error"]["message"]
        monkeypatch.setitem(schemas.VISUAL, "max_regions", 8)
        # The same packet again, still out, now listing what was written.
        shown = ok(invoke(session, "qc_next", {"session_id": sid}))
        assert shown["packet"]["packet_id"] == packet["packet_id"]
        _finish_with(session, sid, oracle, packet["packet_id"],
                     {"kind": "visual_scan", "status": "done",
                      "left": [{"where": "the lower right corner", "why": "could be a vessel"}]})
        late = invoke(session, "segment_qc_roi", {
            "project": "qcsynth", "session_id": sid, "box": other, "artifact_class": "tissue_fold"})
        assert not late["ok"] and late["error"]["code"] == "conflict"
        ok(invoke(session, "qc_session_finish", {"session_id": sid}))
    assert "tissue_fold" in classes_of(session)
    from plexora.plugins.qc.server import results

    result = results.active(results.load("qcsynth"))
    visual = result["checks"]["visual"]["image"]
    assert visual["status"] == "done" and visual["regions"]["written"] == 1
    assert visual["left"][0]["where"] == "the lower right corner"
    mine = next(c for c in result["candidates"].values() if c.get("origin") == "agent")
    assert mine["ai_decision"]["reasoning"].startswith("A bright")
    assert mine["method"] == "sam_agent" and mine["detector"] == "sam"
    status = ok(invoke(session, "qc_session_status", {"session_id": sid}))
    assert status["checks"]["visual:scan"]["state"] == "decided"
    html = Path(ok(invoke(session, "qc_report", {"session_id": sid, "format": "html"}))
                ["html"]).read_text(encoding="utf-8")
    assert "large artifacts seen on the overview" in html and "could be a vessel" in html


def test_a_pass_that_finds_nothing_closes_and_the_session_goes_on(tmp_path):
    info = make_qc_project(tmp_path, size=512, grid=20, artifacts=())
    session = AgentSession()
    oracle = QCOracle(info)
    with sam.use_backend(sam_backend.FakeSamBackend()):
        started = start(session, **ONLY_VISUAL)
        sid = started["session_id"]
        packet = _drive_to(session, sid, oracle, "visual_scan")
        _finish_with(session, sid, oracle, packet["packet_id"],
                     {"kind": "visual_scan", "status": "nothing_found"})
        ok(invoke(session, "qc_session_finish", {"session_id": sid}))
    from plexora.plugins.qc.server import results

    visual = results.active(results.load("qcsynth"))["checks"]["visual"]["image"]
    assert visual["status"] == "nothing_found" and visual["regions"]["written"] == 0
    assert "nothing clearly abnormal" in visual["reason"]


def test_without_magic_select_the_pass_is_planned_not_run(tmp_path, monkeypatch):
    make_qc_project(tmp_path, size=512, grid=20, artifacts=())
    monkeypatch.setenv("PLEXORA_SEGMENT_MODEL_DIR", str(tmp_path / "none"))
    session = AgentSession()
    started = start(session, **ONLY_VISUAL)
    assert "visual:scan" not in started["checks"]
    note = next(n for n in started["planning_notes"] if n["check"] == "visual")
    assert note["status"] == "not_run" and "magic select" in note["reason"]
    ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"],
                                             "action": "cancel"}))
