"""The image checks inside a QC session: score first, look at sampled
places, then snug regions -- and the agent never types a threshold."""

import pytest

from plexora.agent import AgentSession, invoke, registry
from tests.qc_fixtures import make_qc_project
from tests.test_qc_session import (FIVE, QCOracle, classes_of, drive, findings_of, ok,
                                   rois_of, start)

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def _record(sid):
    from plexora.plugins.qc.server.engine import store

    return store().load(sid)


def _checks(sid):
    return {u["id"]: u for u in _record(sid)["units"].values() if u["type"] == "check"}


def _result(project="qcsynth"):
    from plexora.plugins.qc.server import results

    document = results.load(project)
    return next(iter(sorted((document.get("results") or {}).values(),
                            key=lambda r: r["created_at"], reverse=True)))


def test_a_blurred_field_is_reviewed_after_the_audit_and_written_traced(tmp_path):
    from plexora.plugins.qc.server import polygons

    info = make_qc_project(tmp_path, artifacts=("blur_local",))
    session = AgentSession()
    started = start(session)
    assert "blur:DNA_2" in started["checks"]
    packets = drive(session, started["session_id"], QCOracle(info))
    kinds = [p["kind"] for p in packets]
    assert kinds[0] == "channel_audit"
    # Registration is reviewed first (`engine.REVIEW_ORDER`), then blur.
    reviews = [p for p in packets if p["kind"] == "score_review"]
    assert reviews and [p["evidence"]["check"] for p in reviews][0] in ("registration", "blur")
    reviews = [p for p in reviews if p["evidence"]["check"] == "blur"]
    assert reviews and reviews[0]["evidence"]["channel"] == "DNA_2"
    assert reviews[0]["images"][0]["role"] == "score_sheet"
    first_confirm = next((i for i, k in enumerate(kinds) if k == "artifact_confirm"), None)
    assert first_confirm is None or kinds.index("score_review") < first_confirm
    checks = _checks(started["session_id"])
    blur = checks["blur:DNA_2"]
    assert blur["state"] == "decided" and blur["threshold_source"] == "auto"
    assert blur["regions"]["decided"] >= 1
    # The scan's own focus detector stood down for the check.
    skipped = _record(started["session_id"])["scan"]["qcsynth"]["skipped_detectors"]
    assert any(s["name"] == "focus" and s.get("superseded_by") == "blur" for s in skipped)
    result = _result()
    regions = [c for c in result["candidates"].values() if c.get("detector") == "blur"]
    assert regions, list(result["candidates"].values())
    region = regions[0]
    assert region["origin"] == "check" and region["class"] == "out_of_focus"
    assert region["metrics"]["threshold"] == blur["threshold"]
    assert region["metrics"]["threshold_source"] == "auto"
    assert region["ai_decision"]["source"] == "score_review"
    assert region["refinement"]["status"] in ("refined", "fallback", "not_applicable")
    if region["refinement"]["status"] == "refined":
        envelope = polygons.area_of(region["envelope_geometry"])
        assert polygons.area_of(region["geometry"]) <= envelope + 1e-6
    categories = {r["category_id"] for r in findings_of(session)}
    assert categories <= FIVE and "qc_blur_focus" in categories
    assert "out_of_focus" in classes_of(session)
    # The result records the check's bar and where it came from.
    record = result["checks"]["blur"]["DNA_2"]
    assert record["threshold"] == blur["threshold"] and record["threshold_source"] == "auto"
    assert record["strata_verdicts"][0]["strata"]


def test_a_local_misregistration_is_outlined_by_its_nuclei_in_the_registration_category(
        tmp_path):
    info = make_qc_project(tmp_path, artifacts=("misregistration",))
    session = AgentSession()
    started = start(session)
    assert "registration:DNA_2" in started["checks"]
    packets = drive(session, started["session_id"], QCOracle(info))
    reviews = [p for p in packets if p["kind"] == "score_review"
               and p["evidence"]["check"] == "registration"]
    assert reviews
    result = _result()
    regions = [c for c in result["candidates"].values() if c.get("detector") == "registration"
               and c.get("roi_id")]
    assert regions
    region = regions[0]
    assert region["class"] == "cross_cycle_registration_error"
    # The map cells were the envelope; the displaced nuclei are the outline.
    assert region["refinement"]["status"] == "refined"
    assert region["refinement"]["method"] == "nuclei"
    assert region["refinement"]["kept"] == region["refinement"]["nuclei"]["displaced"]
    assert region["scope"] == "cycle"
    categories = {r["category_id"] for r in findings_of(session)}
    assert "qc_registration" in categories


class _Stepper(QCOracle):
    """Says the bar is too aggressive on the first look at each check."""

    def __init__(self, info, first="too_aggressive"):
        super().__init__(info)
        self.first = first
        self.seen = set()

    def _score_review(self, unit, ev):
        answer = super()._score_review(unit, ev)
        if unit["id"] not in self.seen:
            self.seen.add(unit["id"])
            answer["threshold"] = self.first
        return answer


def test_a_look_moves_the_bar_one_step_and_looks_again(tmp_path):
    from plexora.plugins.qc.server import schemas

    info = make_qc_project(tmp_path, artifacts=("blur_local",))
    session = AgentSession()
    sid = start(session)["session_id"]
    packets = drive(session, sid, _Stepper(info))
    reviews = [p for p in packets if p["kind"] == "score_review"
               and p["evidence"]["check"] == "blur" and p["evidence"]["channel"] == "DNA_2"]
    assert len(reviews) == 2
    first, second = (r["evidence"]["threshold"] for r in reviews)
    assert first["offset_steps"] == 0 and second["offset_steps"] == -1
    assert second["value"] > first["value"]
    assert second["source"] == "agent_refined"
    assert reviews[1]["evidence"]["round"] == 2
    unit = _checks(sid)["blur:DNA_2"]
    assert unit["offset_steps"] == -1 and unit["threshold_source"] == "agent_refined"
    assert unit["rounds"] <= schemas.ENGINE["score_rounds"]
    result = _result()
    written = [c for c in result["candidates"].values() if c.get("detector") == "blur"]
    assert written and all(c["metrics"]["threshold_source"] == "agent_refined"
                           and c["metrics"]["offset_steps"] == -1 for c in written)


class _Mixed(QCOracle):
    """Says every row of the blur review is mixed (the registration review,
    served first, is answered as the oracle sees it)."""

    def _score_review(self, unit, ev):
        answer = super()._score_review(unit, ev)
        if unit["check"] == "blur":
            answer["strata"] = {k: "mixed" for k in answer["strata"]}
        return answer


def _is_blur_review(packet):
    return packet["kind"] == "score_review" and packet["evidence"]["check"] == "blur"


def test_a_mixed_review_confirms_each_region_before_anything_is_excluded(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("blur_local",))
    session = AgentSession()
    sid = start(session)["session_id"]
    agent = _Mixed(info)
    result = ok(invoke(session, "qc_next", {"session_id": sid, "wait_s": 20}))
    while result["state"] == "decision" and not _is_blur_review(result["packet"]):
        packet = result["packet"]
        result = ok(invoke(session, "qc_answer", {"session_id": sid,
                                                  "packet_id": packet["packet_id"],
                                                  "answer": agent.answer(packet, sid)}))["next"]
    packet = result["packet"]
    assert _is_blur_review(packet)
    answered = ok(invoke(session, "qc_answer", {"session_id": sid,
                                                "packet_id": packet["packet_id"],
                                                "answer": agent.answer(packet, sid)}))
    unit = _checks(sid)[packet["units"][0]["id"]]
    assert unit["regions"]["decided"] == 0 and unit["regions"]["to_confirm"] >= 1
    pending = [u for u in _record(sid)["units"].values() if u.get("origin") == "check"
               and u.get("check_unit") == unit["id"]]
    assert pending and all(u["state"] == "awaiting_confirm" for u in pending)
    assert not any(r["name"].startswith("QC exclude") for r in rois_of(session))
    # The check's regions are each looked at before any is decided.
    result = answered["next"]
    confirmed = []
    while result["state"] == "decision":
        packet = result["packet"]
        if packet["kind"] == "artifact_confirm":
            confirmed += [ref["id"] for ref in packet["units"]]
        result = ok(invoke(session, "qc_answer", {"session_id": sid,
                                                  "packet_id": packet["packet_id"],
                                                  "answer": agent.answer(packet, sid)}))["next"]
    assert {u["id"] for u in pending} <= set(confirmed)


def test_a_row_the_packet_did_not_show_is_refused(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("blur_local",))
    session = AgentSession()
    sid = start(session)["session_id"]
    agent = QCOracle(info)
    result = ok(invoke(session, "qc_next", {"session_id": sid, "wait_s": 20}))
    while result["packet"]["kind"] != "score_review":
        packet = result["packet"]
        result = ok(invoke(session, "qc_answer", {"session_id": sid,
                                                  "packet_id": packet["packet_id"],
                                                  "answer": agent.answer(packet, sid)}))["next"]
    packet = result["packet"]
    refused = invoke(session, "qc_answer", {"session_id": sid, "packet_id": packet["packet_id"],
                                            "answer": {"kind": "score_review",
                                                       "strata": {"nowhere": "artifact"}}})
    assert not refused["ok"] and refused["error"]["code"] == "invalid_input"
    assert "allowed" in refused["error"]["detail"]


def test_a_check_that_cannot_run_hands_back_to_its_detector(tmp_path, monkeypatch):
    from plexora.plugins.qc.server import checks_bulk

    def broken(*_args, **_kwargs):
        raise RuntimeError("no scores today")

    monkeypatch.setitem(checks_bulk.RUNNERS, "blur", broken)
    info = make_qc_project(tmp_path, artifacts=("blur_local",))
    session = AgentSession()
    sid = start(session)["session_id"]
    checks = _checks(sid)
    assert checks["blur:DNA_2"]["state"] == "skipped_not_applicable"
    assert "no scores today" in checks["blur:DNA_2"]["reason"]
    skipped = _record(sid)["scan"]["qcsynth"]["skipped_detectors"]
    assert any(s["name"] == "focus" and s.get("fallback") for s in skipped)
    drive(session, sid, QCOracle(info))
    assert "out_of_focus" in classes_of(session)


def test_checks_can_be_turned_off(tmp_path):
    make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    started = start(session, checks={"blur": False, "registration": False,
                                     "segmentation": False, "artifacts": False})
    assert started["checks"] == []
    skipped = _record(started["session_id"])["scan"]["qcsynth"]["skipped_detectors"]
    assert not any(s.get("superseded_by") for s in skipped)


def test_blur_everywhere_is_one_region_over_the_tissue(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("blur_global",))
    session = AgentSession()
    sid = start(session)["session_id"]
    packets = drive(session, sid, QCOracle(info))
    reviews = [p for p in packets if p["kind"] == "score_review"
               and p["evidence"]["check"] == "blur"]
    assert reviews and reviews[0]["evidence"]["global"]["possible"]
    result = _result()
    whole = [c for c in result["candidates"].values() if c.get("whole_tissue")]
    assert whole and whole[0]["refinement"]["status"] == "not_applicable"
    assert whole[0]["class"] == "out_of_focus"
