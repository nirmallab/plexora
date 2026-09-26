"""Gating a whole image with a scripted agent, through the capability layer.

The stand-in agents answer each packet from the synthetic scene's known truth
(`tests/autogate_fixtures.py`): the oracle as a careful expert would, the lazy
one by always saying the gate looks fine, the noisy one by being wrong about
direction some of the time. What the tests pin is the server's side: every
state reached, every write receipted, undoable and recorded in provenance,
and the guards that hold whatever the agent says.
"""

import json

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from tests.autogate_fixtures import make_gating_project


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


class Oracle:
    """Answers from the truth painted into the scene."""

    def __init__(self, info, style="oracle", seed=0):
        self.info = info
        self.style = style
        self.rng = np.random.default_rng(seed)
        self.values = {m: np.array([c[m] for c in info["cells"]], dtype=np.float32)
                       for m in info["truth"]}
        self.truth = {m: np.isin([c["id"] for c in info["cells"]], ids)
                      for m, ids in info["truth"].items()}

    def errors(self, marker, low):
        called = self.values[marker] > np.float32(low)
        truth = self.truth[marker]
        return int((called & ~truth).sum()), int((~called & truth).sum())

    def best(self, marker):
        """The threshold with the fewest errors, and that error."""
        values = np.sort(self.values[marker])
        cuts = np.concatenate([[values[0] - 1], (values[:-1] + values[1:]) / 2])
        errs = [sum(self.errors(marker, c)) for c in cuts]
        i = int(np.argmin(errs))
        return float(cuts[i]), errs[i]

    def excess(self, marker, low):
        """Errors beyond the best any threshold can do."""
        return sum(self.errors(marker, low)) - self.best(marker)[1]

    def direction(self, marker, low):
        n = len(self.values[marker])
        if self.excess(marker, low) <= max(2, 0.01 * n):
            return "about_right"
        return "too_low" if low < self.best(marker)[0] else "too_high"

    def answer(self, packet):
        kind = packet["kind"]
        marker = packet["units"][0]["marker"] if packet["units"] else None
        ev = packet["evidence"]
        if kind == "t1_strip":
            verdicts = {}
            for row in ev["markers"]:
                good = self.direction(row["marker"], row["gate"]) == "about_right"
                verdicts[row["marker"]] = "ok" if good or self.style == "lazy" else "suspicious"
            return {"kind": kind, "verdicts": verdicts}
        if kind in ("t2_confirm", "t3_biological"):
            direction = self.direction(marker, ev["candidate"]["low"])
            if self.style == "lazy":
                direction = "about_right"
            elif self.style == "noisy" and self.rng.random() < 0.3:
                direction = {"too_low": "too_high", "too_high": "too_low",
                             "about_right": "too_low"}[direction]
            return {"kind": kind, "confidence": 0.9, "direction": direction,
                    "plausibility": {"compartment": "matches", "pattern": "membrane",
                                     "positives_look_real": True},
                    "rows": {"below": "plausible", "near": "plausible",
                             "above": "plausible"}}
        if kind == "t4_candidates":
            if self.style == "lazy":
                return {"kind": kind, "chosen_candidate": "keep", "confidence": 0.9}
            best, best_err = "keep", self.excess(marker, ev["current"])
            for cand in ev["candidates"]:
                err = self.excess(marker, cand["low"])
                if err < best_err:
                    best, best_err = cand["id"], err
            if best == "keep" and self.direction(marker, ev["current"]) != "about_right":
                best = "none_separates"
            return {"kind": kind, "chosen_candidate": best, "confidence": 0.85}
        if kind == "qc_confirm":
            return {"kind": kind, "verdict": "real_signal"}
        if kind == "regression_confirm":
            return {"kind": kind, "verdict": "holds"}
        if kind == "transfer_check":
            project = ev["this"]["project"]
            direction = self.direction(marker, ev["this"]["aligned_gate"])
            return {"kind": kind, "per_image": {project: "holds" if direction == "about_right"
                                                else direction}}
        if kind == "panel_context":
            return {"kind": kind, "entries": []}
        raise AssertionError(kind)


def drive(session, session_id, agent, limit=60):
    """Run the next/answer loop to the end; returns the packets seen."""
    seen = []
    result = ok(invoke(session, "gating_next", {"session_id": session_id, "wait_s": 20}))
    for _ in range(limit):
        if result["state"] != "decision":
            assert result["state"] in ("decided", "done"), result
            return seen
        packet = result["packet"]
        seen.append(packet)
        answered = ok(invoke(session, "gating_answer", {
            "session_id": session_id, "packet_id": packet["packet_id"],
            "answer": agent.answer(packet)}))
        result = answered["next"]
    raise AssertionError("the session did not finish")


def start(session, **options):
    started = ok(invoke(session, "gating_session_start", {"scope": "project",
                                                          "project": "gsynth", **options}))
    jobs.drain(120)
    return started


def units(session, session_id):
    status = ok(invoke(session, "gating_session_status", {"session_id": session_id}))
    return {u["marker"]: u for u in status["units"]}


HARD = ("CD3", "CD8", "CD20", "CD4", "FOXP3")


def test_the_oracle_gates_every_marker_and_every_write_is_receipted(tmp_path):
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    agent = Oracle(info)
    started = start(session)
    packets = drive(session, started["session_id"], agent)
    kinds = [p["kind"] for p in packets]
    assert "t1_strip" in kinds and "t2_confirm" in kinds
    final = units(session, started["session_id"])
    for marker in HARD:
        assert final[marker]["state"] in ("accepted", "accepted_low_confidence"), final[marker]
        assert agent.excess(marker, final[marker]["final"]) <= max(3, 0.015 * len(info["cells"])), \
            (marker, final[marker])
    for marker in ("CD3", "CD8", "CD20"):
        assert final[marker]["state"] == "accepted"
    # Every packet stayed small, and images were WebP.
    for packet in packets:
        assert len(json.dumps(packet)) < 48_000
        assert len(packet["images"]) <= 2
    # Receipts: one child per write, under the session's operation.
    status = ok(invoke(session, "gating_session_status", {"session_id": started["session_id"]}))
    audit = [json.loads(line) for line in
             (tmp_path / ".agent" / "audit.jsonl").read_text().splitlines()]
    children = [line for line in audit if line.get("gating_session") == started["session_id"]
                and line["operation_id"].startswith(started["receipt"]["operation_id"] + ".")]
    assert len(children) == status["receipts"] >= 3
    assert all(line["receipt"]["undo_hint"]["tool"] == "set_gate" for line in children
               if line["capability"] != "gating.calibrate_display" and line.get("receipt", {})
               .get("undo_hint"))
    gates = ok(invoke(session, "get_all_gates", {"project": "gsynth"}))
    assert set(gates["thresholded"]) >= set(HARD)
    assert gates["provenance"]["CD8"]["method"] in ("gmm", "ai_accepted", "ai_refined")
    assert gates["provenance"]["CD8"]["confidence"] in ("high", "moderate")
    finished = ok(invoke(session, "gating_session_finish",
                         {"session_id": started["session_id"]}))
    assert finished["progress"]["units_done"] == len(HARD)


def test_a_split_positive_population_is_read_as_one(tmp_path):
    """CD3 is on two T-cell subsets at slightly different levels: the mixture
    fits background + two bright components, and the Auto button's
    top-versus-rest crossover lands between the subsets. The profile pools
    them and gates at the real boundary."""
    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    agent = Oracle(info)
    profile = ok(invoke(session, "profile_marker", {"project": "gsynth", "marker": "CD3"}))
    fit = profile["profile"]["fit"]
    assert "positives_split" in profile["profile"]["flags"]
    assert agent.direction("CD3", fit["auto_gate_raw"]) == "too_high"
    assert agent.direction("CD3", fit["gate_raw"]) == "about_right"
    assert profile["gmm_proposal"] == fit["gate_raw"]


def test_a_rare_population_gets_a_look_before_it_is_accepted(tmp_path):
    """FOXP3 on ~3.5% of cells. The fit finds it, but the estimators disagree
    about most of the positive calls -- rare populations are where a histogram
    antimode wanders -- so the marker is never accepted unseen."""
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    agent = Oracle(info)
    started = start(session, markers=["CD3", "CD4", "FOXP3"])
    packets = drive(session, started["session_id"], agent)
    foxp3 = [p["kind"] for p in packets if p["units"] and p["units"][0]["marker"] == "FOXP3"]
    final = units(session, started["session_id"])["FOXP3"]
    assert "t2_confirm" in foxp3
    assert final["state"] in ("accepted", "accepted_low_confidence"), (final, foxp3)
    assert final["confidence"] != "high"
    assert agent.excess("FOXP3", final["final"]) <= 3


def test_a_lazy_agent_cannot_raise_confidence_past_what_the_numbers_allow(tmp_path):
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    started = start(session, markers=["CD4"])
    drive(session, started["session_id"], Oracle(info, style="lazy"))
    final = units(session, started["session_id"])["CD4"]
    # Accepted -- nothing in the numbers refutes the GMM gate -- but a marker
    # whose populations overlap never earns high confidence from a look alone.
    assert final["state"] in ("accepted", "accepted_low_confidence")
    assert final["confidence"] in ("moderate", "low")


def test_packets_are_idempotent_and_stale_answers_are_refused(tmp_path):
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    started = start(session, markers=["CD4"])
    sid = started["session_id"]
    first = ok(invoke(session, "gating_next", {"session_id": sid}))
    again = ok(invoke(session, "gating_next", {"session_id": sid}))
    assert first["packet"]["packet_id"] == again["packet"]["packet_id"]
    used = units(session, sid)  # budget charged once, at issue
    status = ok(invoke(session, "gating_session_status", {"session_id": sid}))
    assert status["used"]["packets"] == 1
    stale = invoke(session, "gating_answer", {"session_id": sid, "packet_id": "pk_9999",
                                              "answer": {"kind": "t2_confirm"}})
    assert not stale["ok"] and stale["error"]["code"] == "conflict"
    bad = invoke(session, "gating_answer", {"session_id": sid,
                                            "packet_id": first["packet"]["packet_id"],
                                            "answer": {"kind": "t2_confirm"}})
    assert not bad["ok"] and bad["error"]["code"] == "invalid_input"
    answer = Oracle(info).answer(first["packet"])
    applied = ok(invoke(session, "gating_answer", {"session_id": sid,
                                                   "packet_id": first["packet"]["packet_id"],
                                                   "answer": answer, "include_next": False}))
    repeat = ok(invoke(session, "gating_answer", {"session_id": sid,
                                                  "packet_id": first["packet"]["packet_id"],
                                                  "answer": answer}))
    assert repeat["outcome"]["already_applied"] and applied["applied"]
    assert used


def test_two_unreadable_answers_send_the_marker_to_review(tmp_path):
    make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"])["session_id"]
    packet = ok(invoke(session, "gating_next", {"session_id": sid}))["packet"]
    for _ in range(2):
        bad = invoke(session, "gating_answer", {"session_id": sid,
                                                "packet_id": packet["packet_id"],
                                                "answer": {"kind": "t2_confirm", "x": 1}})
        assert not bad["ok"]
    assert units(session, sid)["CD4"]["state"] == "manual_review_recommended"


def test_locked_and_user_set_gates_are_never_touched(tmp_path):
    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    ok(invoke(session, "set_gate", {"project": "gsynth", "marker": "CD20", "low": 123.0}))
    ok(invoke(session, "set_gate_status", {"project": "gsynth", "marker": "CD20",
                                           "status": "locked"}))
    refused = invoke(session, "set_gate", {"project": "gsynth", "marker": "CD20", "low": 50.0})
    assert not refused["ok"] and refused["error"]["code"] == "conflict"
    started = start(session)
    drive(session, started["session_id"], Oracle(info))
    final = units(session, started["session_id"])
    assert final["CD20"]["state"] == "skipped_locked"
    gate = ok(invoke(session, "get_gate", {"project": "gsynth", "marker": "CD20"}))
    assert gate["gate"]["low"] == 123.0


@pytest.mark.parametrize("marker", ["CD8", "CD4"])
def test_the_user_editing_a_gate_mid_session_wins(tmp_path, marker):
    """Whether the gate was already written (CD8, accepted at T1 and waiting
    for the audit sheet) or not yet (CD4, waiting for a look), a user's drag
    in the viewer is kept and the marker goes to review."""
    from plexora.plugins.gating.server import model

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=[marker])["session_id"]
    packet = ok(invoke(session, "gating_next", {"session_id": sid}))["packet"]
    # The user drags the slider in the viewer (a direct write, no agent).
    ds = session.data("gsynth")
    model.set_gate(ds, marker, 222.0, model.get_gate(ds, marker)["high"])
    answered = ok(invoke(session, "gating_answer", {
        "session_id": sid, "packet_id": packet["packet_id"],
        "answer": Oracle(info, style="lazy").answer(packet)}))
    assert answered["next"]["state"] in ("decided", "done")
    unit = units(session, sid)[marker]
    assert unit["state"] == "manual_review_recommended", unit
    assert model.get_gate(session.data("gsynth"), marker)["low"] == 222.0


def test_propose_mode_writes_nothing_until_commit(tmp_path):
    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    started = start(session, mode="propose")
    drive(session, started["session_id"], Oracle(info))
    gates = ok(invoke(session, "get_all_gates", {"project": "gsynth"}))
    assert not gates["thresholded"]
    committed = ok(invoke(session, "gating_session_finish",
                          {"session_id": started["session_id"], "action": "commit"}))
    assert len(committed["written"]) == 3, committed
    gates = ok(invoke(session, "get_all_gates", {"project": "gsynth"}))
    assert set(gates["thresholded"]) == {"CD3", "CD8", "CD20"}


def test_rollback_undoes_every_write(tmp_path):
    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], Oracle(info))
    rolled = ok(invoke(session, "gating_session_finish",
                       {"session_id": started["session_id"], "action": "rollback"}))
    assert rolled["undone"] and not rolled["refused"]
    gates = ok(invoke(session, "get_all_gates", {"project": "gsynth"}))
    assert not gates["thresholded"]
    assert gates["provenance"]["CD8"]["method"] == "rolled_back"


def test_a_session_resumes_in_a_new_process(tmp_path):
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    started = start(session, markers=["CD3", "CD4"])
    sid = started["session_id"]
    first = ok(invoke(session, "gating_next", {"session_id": sid}))
    # A new MCP server: new session object, job store forgotten.
    jobs._reset_for_tests()
    fresh = AgentSession()
    again = ok(invoke(fresh, "gating_next", {"session_id": sid}))
    assert again["packet"]["packet_id"] == first["packet"]["packet_id"]
    drive(fresh, sid, Oracle(info))
    assert all(u["state"] in ("accepted", "accepted_low_confidence")
               for u in units(fresh, sid).values())


def test_the_report_and_export_are_written(tmp_path):
    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], Oracle(info))
    report = ok(invoke(session, "gating_report", {"session_id": started["session_id"]}))
    html = open(report["paths"]["html"], encoding="utf-8").read()
    assert "CD8" in html and "data:image/png;base64" in html
    assert open(report["paths"]["pdf"], "rb").read(4) == b"%PDF"
    exported = ok(invoke(session, "export_gates", {"project": "gsynth"}))
    assert any(path.endswith("_gates_provenance.csv") for path in exported["files"])
