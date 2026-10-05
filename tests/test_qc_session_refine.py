"""A session writes each confirmed artifact as its traced pixels, inside the
envelope the agent judged -- and the envelope itself whenever tracing is off,
not possible, or fails, so a region is never lost and never a stray trace.
"""

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke
from plexora.plugins.qc.server import polygons
from tests.qc_fixtures import make_qc_project
from tests.test_qc_session import QCOracle, _qc, drive, ok, start  # noqa: F401

pytestmark = pytest.mark.paid

TRACED = {"saturation_or_clipping", "tissue_fold", "autofluorescence", "antibody_aggregate",
          "debris_or_foreign_object"}


def _record(session_id):
    from plexora.plugins.qc.server.engine import store

    return store().load(session_id)


def _candidates(project="qcsynth"):
    from plexora.plugins.qc.server import results

    return list((results.active(results.load(project)) or {}).get("candidates", {}).values())


def _features(session, project="qcsynth"):
    from plexora.plugins.qc.server import results, roi_link

    ds = session.image_data(project)
    return {r["roi_id"]: r for r in roi_link.live_regions(ds, results.active(
        results.load(project)))}


def _inside(geometry, envelope):
    from shapely.geometry import shape

    return shape(envelope).buffer(1e-3).contains(shape(geometry))


def test_confirmed_regions_are_traced_inside_their_envelopes(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation", "fold", "aggregates"))
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], QCOracle(info))
    ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"]}))
    # Written, or written and then consolidated into a place's one ROI.
    candidates = [c for c in _candidates() if c.get("roi_id") or c.get("consolidated_into")]
    features = _features(session)
    traced = [c for c in candidates if (c.get("refinement") or {}).get("status") == "refined"]
    assert {c["class"] for c in traced} & {"saturation_or_clipping"}
    assert {c["class"] for c in traced} & {"tissue_fold", "autofluorescence"}
    assert {c["class"] for c in traced} & {"antibody_aggregate", "debris_or_foreign_object"}
    for candidate in traced:
        envelope = candidate["envelope_geometry"]
        assert _inside(candidate["geometry"], envelope)
        assert polygons.area_of(candidate["geometry"]) <= polygons.area_of(envelope)
        assert candidate["measurement"]["refined_fraction"] <= \
            candidate["measurement"]["tissue_fraction"] + 1e-9
        if not candidate.get("roi_id"):
            continue        # consolidated: its place's ROI holds it (test_qc_consolidate)
        written = features[candidate["roi_id"]]["geometry"]
        assert polygons.geometry_hash(written) == polygons.geometry_hash(candidate["geometry"])
    rois = ok(invoke(session, "list_rois", {"project": "qcsynth"}))["rois"]
    notes = {r["id"]: r.get("notes") or "" for r in rois}
    own = [c for c in traced if c.get("roi_id")]
    assert not own or any("traced: " in notes[c["roi_id"]] for c in own)
    regions = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))["regions"]
    assert any((r.get("refinement") or {}).get("status") == "refined" for r in regions)


def test_with_tracing_off_the_envelope_is_written(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    started = start(session, refine=False)
    drive(session, started["session_id"], QCOracle(info))
    ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"]}))
    # Written, or written and then consolidated into a place's one ROI.
    candidates = [c for c in _candidates() if c.get("roi_id") or c.get("consolidated_into")]
    assert candidates
    for candidate in candidates:
        assert candidate["refinement"]["status"] == "not_applicable"
        assert candidate["geometry"] == candidate["envelope_geometry"]


def test_a_tracer_that_fails_leaves_the_envelope(tmp_path, monkeypatch):
    from plexora.plugins.qc.server import refine

    def broken(*args, **kwargs):
        raise RuntimeError("the tracer broke")

    monkeypatch.setattr(refine, "refine", broken)
    info = make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    started = start(session)
    sid = started["session_id"]
    packets = drive(session, sid, QCOracle(info))
    assert packets[-1]["kind"] == "final_qc_review"
    record = _record(sid)
    written = [u for u in record["units"].values()
               if u["type"] == "candidate" and u.get("roi_id")]
    assert written
    for unit in written:
        assert unit["refinement"]["status"] == "fallback"
        assert "the tracer broke" in unit["refinement"]["reason"]
        assert unit["geometry"] == unit["envelope_geometry"]
        assert not unit.get("write_error")
    from plexora.plugins.qc.server.engine import store

    assert any(e.get("event") == "refine_failed" for e in store().decisions(sid))


class _TooLarge(QCOracle):
    """Says the outline is too large, then chooses the tightest one."""

    def _confirm(self, record, unit):
        out = super()._confirm(record, unit)
        if out.get("verdict") == "artifact":
            out["boundary"] = "too_large"
        return out

    def answer(self, packet, session_id):
        if packet["kind"] == "artifact_localize":
            return {"kind": "artifact_localize", "chosen": "A", "confidence": "sure"}
        return super().answer(packet, session_id)


def test_the_localize_sheet_shows_what_each_outline_would_trace(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    started = start(session)
    sid = started["session_id"]
    packets = drive(session, sid, _TooLarge(info))
    localize = [p for p in packets if p["kind"] == "artifact_localize"]
    assert localize
    evidence = localize[0]["evidence"]
    assert evidence["refinement"]["status"] == "refined"
    traced = [a for a in evidence["alternatives"] if a.get("refined_area_px2") is not None]
    assert traced
    for alternative in traced:
        assert alternative["refined_area_px2"] <= alternative["area_px2"] + 1e-6
    record = _record(sid)
    unit = next(u for u in record["units"].values()
                if u["type"] == "candidate" and u.get("variant") == "tight")
    assert unit["refinement"]["clipped_from"] == "bbox"
    assert _inside(unit["geometry"], unit["envelope_geometry"])


class _ReopenOnce(QCOracle):
    """Names the first region at the final review once, then is content."""

    def __init__(self, info):
        super().__init__(info)
        self.reviews = 0

    def answer(self, packet, session_id):
        if packet["kind"] == "final_qc_review":
            self.reviews += 1
            regions = packet["evidence"]["regions"]
            if self.reviews == 1 and regions:
                return {"kind": "final_qc_review", "verdict": "inconsistent",
                        "concerns": [{"target": regions[0]["label"], "issue": "boundary",
                                      "note": "look again"}]}
            return {"kind": "final_qc_review", "verdict": "consistent"}
        return super().answer(packet, session_id)


def test_a_reopened_region_is_traced_again(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    started = start(session)
    sid = started["session_id"]
    agent = _ReopenOnce(info)
    packets = drive(session, sid, agent)
    kinds = [p["kind"] for p in packets]
    # Looked at again after the review named it (the second review of an
    # unchanged picture is the memo's).
    assert kinds.index("artifact_confirm", kinds.index("final_qc_review")) > 0
    reopened = [u for u in _record(sid)["units"].values()
                if u["type"] == "candidate" and u.get("reopened")]
    assert reopened
    ok(invoke(session, "qc_session_finish", {"session_id": sid}))
    unit = reopened[0]
    assert unit["refinement"]["status"] == "refined"
    features = _features(session)
    assert polygons.geometry_hash(features[unit["roi_id"]]["geometry"]) == \
        polygons.geometry_hash(unit["geometry"])
    assert np.isfinite(unit["measurement"]["refined_fraction"])
