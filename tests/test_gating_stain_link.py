"""QC's stain check in automatic gating (`autogate/stain_link.py`): evidence,
never a rule. A stain read failed is failed only when gating's own
whole-image check says so too; a stain the stain check did not read failed is
never failed on gating's word; weak or contested stains are gated with a look,
capped and listed. Without QC, or with `stain_check: off`, gating is
unchanged. Benched in docs/internal/bench/runs/2026-11-23-qc-aware."""

import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.plugins.gating.server.autogate import engine as engines, stain_link
from tests.autogate_fixtures import make_gating_project
from tests.test_gating_session import FailingQC, Oracle, drive, ok, start, units

pytestmark = pytest.mark.pro


@pytest.fixture(autouse=True)
def _with_qc():
    registry.discover(["gating", "roi", "qc"])


class StainReader(Oracle):
    """Answers each stain reading from a script per marker (one verdict per
    reading); everything else as the oracle."""

    def __init__(self, info, script, **kw):
        super().__init__(info, **kw)
        self.script = {m: list(v) for m, v in script.items()}
        self.stain_packets = []

    def answer(self, packet):
        if packet["kind"] == "stain_check":
            self.stain_packets.append(packet)
            marker = packet["evidence"]["marker"]
            verdict = self.script.get(marker, ["real"]).pop(0)
            return {"kind": "stain_check", "expectation": "a membrane ring on T cells",
                    "observed": "cells", "structure": "yes" if verdict != "failed" else "no",
                    "verdict": verdict, "why": "scripted"}
        return super().answer(packet)


class StainReaderFailingQC(StainReader, FailingQC):
    pass


def _stored(session, project="gsynth"):
    return ok(invoke(session, "get_stain_verdicts", {"project": project}))


def test_each_stain_is_read_first_and_stored_where_qc_reuses_it(tmp_path):
    info = make_gating_project(tmp_path)
    session = AgentSession()
    agent = StainReader(info, {"CD20": ["failed", "failed"], "CD8": ["weak"]})
    started = start(session, markers=["CD3", "CD8", "CD20"], stain_check="auto")
    seen = drive(session, started["session_id"], agent)
    per_marker = {}
    for packet in seen:
        marker = (packet.get("evidence") or {}).get("marker")
        if marker:
            per_marker.setdefault(marker, []).append(packet["kind"])
    # The stain is read before anything else is asked of the marker.
    assert all(kinds[0] == "stain_check" for kinds in per_marker.values()), per_marker
    cd20 = [p for p in agent.stain_packets if p["evidence"]["marker"] == "CD20"]
    assert len(cd20) == 2
    # Blind: the second reader is sent what the first was, in a worker of its own.
    assert cd20[0]["question"] == cd20[1]["question"]
    assert {k: v for k, v in cd20[1]["evidence"].items() if k != "check"} == cd20[0]["evidence"]
    assert cd20[1]["evidence"]["check"] == "reader_2"
    stored = _stored(session)["channels"]
    assert {m: stored[m]["verdict"] for m in stored} == {"CD3": "real", "CD8": "weak",
                                                         "CD20": "failed"}
    assert {stored[m]["source"] for m in stored} == {"gating"}
    final = units(session, started["session_id"])
    assert final["CD20"]["stain"]["readings"] == ["failed", "failed"]
    # Failed by the stain check: gating's own check reads it too (blind to the
    # stain check), says real here, so it is gated -- at low confidence.
    assert "qc_confirm" in per_marker["CD20"]
    assert final["CD20"]["state"] == "accepted_low_confidence"
    # Weak: gated with a look, never above moderate.
    assert final["CD8"]["state"] in ("accepted", "accepted_low_confidence")
    assert final["CD8"]["confidence"] in ("moderate", "low")
    # The review list says so.
    qc = ok(invoke(session, "gating_qc", {"project": "gsynth"}))
    flags = {s["marker"]: s["category"] for s in qc["stain_flags"]}
    assert flags == {"CD20": "failed", "CD8": "weak"}
    assert "CD20" in qc["needs_review"]


def test_a_stored_verdict_is_reused_and_nothing_is_read_again(tmp_path):
    info = make_gating_project(tmp_path)
    session = AgentSession()
    first = StainReader(info, {"CD8": ["weak"]})
    drive(session, start(session, markers=["CD3", "CD8"], stain_check="auto")["session_id"],
          first)
    assert len(first.stain_packets) == 2
    again = StainReader(info, {})
    started = start(session, markers=["CD3", "CD8"], stain_check="auto")
    drive(session, started["session_id"], again)
    assert again.stain_packets == []
    final = units(session, started["session_id"])
    assert final["CD8"]["stain"] == {"category": "weak", "source": "gating", "reused": True,
                                     "readings": ["weak"]}
    assert final["CD8"]["confidence"] in ("moderate", "low")


def test_gatings_check_alone_never_fails_a_stain_read_real(tmp_path):
    info = make_gating_project(tmp_path, variant="flat")
    session = AgentSession()
    started = start(session, markers=["CD20"], stain_check="auto")
    drive(session, started["session_id"], StainReaderFailingQC(info, {"CD20": ["real"]}))
    final = units(session, started["session_id"])["CD20"]
    assert final["state"] == "manual_review_recommended"
    assert "the two checks disagree" in final["reason"]


def test_both_checks_saying_failed_fail_the_stain(tmp_path):
    info = make_gating_project(tmp_path, variant="flat")
    session = AgentSession()
    started = start(session, markers=["CD20"], stain_check="auto")
    drive(session, started["session_id"],
          StainReaderFailingQC(info, {"CD20": ["failed", "failed"]}))
    assert units(session, started["session_id"])["CD20"]["state"] == "technically_failed"


def test_by_default_a_stored_verdict_is_used_and_nothing_is_read(tmp_path):
    info = make_gating_project(tmp_path)
    session = AgentSession()
    drive(session, start(session, markers=["CD8"], stain_check="auto")["session_id"],
          StainReader(info, {"CD8": ["weak"]}))
    agent = StainReader(info, {})
    started = start(session, markers=["CD3", "CD8"])
    drive(session, started["session_id"], agent)
    assert agent.stain_packets == []
    final = units(session, started["session_id"])
    assert final["CD8"]["stain"]["category"] == "weak" and "stain" not in final["CD3"]


def test_a_stain_not_read_yet_is_read_before_it_is_failed(tmp_path, monkeypatch):
    # As in a dataset, whose bulk pass reads no stain.
    monkeypatch.setattr(stain_link, "prepare", lambda *a, **k: None)
    info = make_gating_project(tmp_path, variant="flat")
    session = AgentSession()
    agent = StainReaderFailingQC(info, {"CD20": ["real"]})
    started = start(session, markers=["CD20"], stain_check="auto")
    seen = drive(session, started["session_id"], agent)
    kinds = [p["kind"] for p in seen if (p.get("evidence") or {}).get("marker") == "CD20"]
    assert kinds.index("stain_check") > kinds.index("qc_confirm")
    final = units(session, started["session_id"])["CD20"]
    assert final["state"] == "manual_review_recommended"
    assert "the two checks disagree" in final["reason"]


def test_off_reads_nothing_and_gates_as_before(tmp_path):
    info = make_gating_project(tmp_path)
    session = AgentSession()
    agent = StainReader(info, {})
    started = start(session, markers=["CD3", "CD8"], stain_check="off")
    drive(session, started["session_id"], agent)
    assert agent.stain_packets == []
    final = units(session, started["session_id"])
    assert set(final) == {"CD3", "CD8"}
    assert all("stain" not in u for u in final.values())


def _unit(verdict, contested=False, **extra):
    return {"stain": {"verdict": verdict, "contested": contested, "readings": []}, **extra}


def test_the_categories_caps_and_the_failure_guard():
    assert stain_link.category({}) is None and not stain_link.blocks_failure({})
    assert stain_link.category(_unit("real")) == "real"
    assert stain_link.category(_unit("weak", contested=True)) == "contested"
    assert [stain_link.cap(_unit(v)) for v in ("real", "weak", "failed", "cannot_tell")] == \
        [None, "moderate", "low", None]
    assert stain_link.cap(_unit("real", contested=True)) == "low"
    assert stain_link.blocks_failure(_unit("real"))
    assert stain_link.blocks_failure(_unit("cannot_tell"))
    assert not stain_link.blocks_failure(_unit("failed"))
    # A clean, audited split keeps high confidence only when nobody doubted the stain.
    clean = {"path": "t1", "audited": True, "metrics": {"d": 3.0}, "flags": []}
    assert engines.confidence_for(clean) == "high"
    assert engines.confidence_for({**clean, **_unit("weak")}) == "moderate"
    assert engines.confidence_for({**clean, **_unit("failed")}) == "low"
    assert engines.confidence_for({**clean, **_unit("real")}) == "high"
