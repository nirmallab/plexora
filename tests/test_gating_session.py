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
from plexora.plugins.gating.server.autogate import schemas
from tests.autogate_fixtures import make_gating_project

#: These exercise Paid (AI) capabilities, so they run with a test licence
#: installed; what Free refuses is tests/test_licensing_enforcement.py's job.
pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


#: Every packet this test process was sent, by (session, packet): the reader
#: a conversation is, so a `{as_in: packet_id}` reads as the value it points at.
_HELD: dict = {}


def _read(result):
    from plexora.plugins.gating.server.autogate.packets import resolve_as_in

    for holder in (result, result.get("next") if isinstance(result.get("next"), dict)
                   else None):
        packet = (holder or {}).get("packet")
        if isinstance(packet, dict) and packet.get("session_id"):
            resolve_as_in(packet, _HELD.setdefault(packet["session_id"], {}))
    return result


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    out = result["result"]
    return _read(out) if isinstance(out, dict) else out


class Oracle:
    """Answers from the truth painted into the scene."""

    def __init__(self, info, style="oracle", seed=0):
        self.info = info
        self.style = style
        self.rng = np.random.default_rng(seed)
        # In the table's own units: a log1p'd project's gates are log1p values.
        to_table = np.log1p if info.get("log_transformed") else (lambda v: v)
        self.values = {m: np.asarray(to_table(np.array([c[m] for c in info["cells"]],
                                                       dtype=np.float64)), dtype=np.float32)
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
            from plexora.ai.bench import rows_for

            direction = self.direction(marker, ev["candidate"]["low"])
            if self.style == "lazy":
                direction = "about_right"
            elif self.style == "noisy" and self.rng.random() < 0.3:
                direction = {"too_low": "too_high", "too_high": "too_low",
                             "about_right": "too_low"}[direction]
            return {"kind": kind, "confidence": "sure", **rows_for(direction, ev)}
        if kind == "t4_candidates":
            from plexora.ai.bench import interval_verdicts

            rows = ev["intervals"]
            if self.style == "lazy":
                return {"kind": kind, "intervals": {r["row"]: "cannot_tell" for r in rows},
                        "confidence": "sure"}
            return {"kind": kind, "confidence": "fairly_sure",
                    "intervals": interval_verdicts(self.values[marker], self.truth[marker],
                                                   rows)}
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
        if kind == "display_setup":
            # The darker half of the ladder for both questions: the rule's own
            # column wherever the numbers allow it.
            rows = [r["marker"] for r in ev["rows"]]
            return {"kind": kind, "background_ok": {m: ["c1", "c2", "c3"] for m in rows},
                    "structure_ok": {m: ["c2", "c3"] for m in rows}}
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


def test_a_log1p_project_is_gated_in_its_own_units(tmp_path):
    """exemplar-001's arrangement: the table log1p'd on read, the pixels raw."""
    info = make_gating_project(tmp_path, grid=24, size=1024, log_transformed=True)
    session = AgentSession()
    agent = Oracle(info)
    started = start(session)
    drive(session, started["session_id"], agent)
    final = units(session, started["session_id"])
    for marker in ("CD3", "CD8", "CD20"):
        assert final[marker]["state"] in ("accepted", "accepted_low_confidence"), final[marker]
        assert final[marker]["final"] < 12        # log1p units, not raw intensities
        assert agent.excess(marker, final[marker]["final"]) <= 3, (marker, final[marker])


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


def test_a_rare_but_separated_population_is_accepted_and_shown(tmp_path):
    """FOXP3 on ~3.5% of cells, well apart from the background. The numbers
    accept it -- one estimator wandering into a dip inside the background does
    not outvote the rest -- and it is never accepted unseen: it goes on the
    audit sheet."""
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    agent = Oracle(info)
    started = start(session, markers=["CD3", "CD4", "FOXP3"])
    packets = drive(session, started["session_id"], agent)
    shown = [p["kind"] for p in packets
             if any(u["marker"] == "FOXP3" for u in p["units"])]
    final = units(session, started["session_id"])["FOXP3"]
    assert shown, "FOXP3 was accepted without being shown"
    assert final["state"] in ("accepted", "accepted_low_confidence"), (final, shown)
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
    sid = start(session, markers=["CD4"], display_check="rule")["session_id"]
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


def test_a_dataset_is_gated_from_its_reference_image(tmp_path):
    from tests.autogate_fixtures import make_gating_dataset

    made = make_gating_dataset(tmp_path, shifts=(0.0, 0.0, 0.5), grid=20, size=800)
    session = AgentSession()
    agents = {name: Oracle(info) for name, info in made.items()}

    class ByImage:
        def answer(self, packet):
            project = packet["units"][0]["project"] if packet["units"] else "cohort_1"
            return agents[project].answer(packet)

    started = ok(invoke(session, "gating_session_start", {"scope": "dataset",
                                                          "dataset": "cohort"}))
    jobs.drain(180)
    reference = started["reference_image"]
    assert started["images"][0] == reference
    packets = drive(session, started["session_id"], ByImage(), limit=80)
    status = ok(invoke(session, "gating_session_status", {"session_id": started["session_id"]}))
    by_unit = {(u["project"], u["marker"]): u for u in status["units"]}
    for (project, marker), unit in by_unit.items():
        assert unit["state"] in ("accepted", "accepted_low_confidence"), unit
        assert agents[project].excess(marker, unit["final"]) <= max(
            3, 0.015 * len(made[project]["cells"])), unit
    # The two unshifted images are carried without a question; the shifted one
    # is checked or re-gated, never copied blindly.
    summary = status["dataset"]["markers"]
    shifted = next(p for p in made if p.endswith("_3"))
    for marker, info in summary.items():
        classes = {row["project"]: row["class"] for row in info["images"]}
        assert classes[reference] == "reference"
        assert classes[shifted] != "stable", (marker, classes)
    gates = {p: ok(invoke(session, "get_all_gates", {"project": p})) for p in made}
    methods = {gates[p]["provenance"][m]["method"] for p in made if p != reference
               for m in ("CD8", "CD20")}
    assert "transfer_aligned" in methods
    assert any(p["kind"] in ("transfer_check", "t2_confirm", "t1_strip") for p in packets)


def test_a_bulk_pass_cut_off_by_a_restart_is_picked_up_again(tmp_path):
    from plexora.plugins.gating.server.autogate import engine

    info = make_gating_project(tmp_path, grid=24, size=1024, markers=HARD)
    session = AgentSession()
    started = start(session, markers=["CD3", "CD8"])
    sid = started["session_id"]
    # As a restarted server finds it: mid-pass, one marker never reached, and
    # the job that was running it gone with the old process.
    st = engine.store()
    record = st.load(sid)
    record["state"] = "bulk_running"
    record["bulk_job_id"] = "job_gone"
    record["units"][engine.unit_key("gsynth", "CD8")] = {
        "project": "gsynth", "marker": "CD8", "state": "pending"}
    st.save(record)
    jobs._reset_for_tests()
    fresh = AgentSession()
    # Status says what happened rather than looking busy.
    before = ok(invoke(fresh, "gating_session_status", {"session_id": sid}))
    assert before["progress"]["not_profiled"] == 1
    assert before["bulk"]["status"] is None and "not running" in before["bulk"]["note"]
    first = ok(invoke(fresh, "gating_next", {"session_id": sid, "wait_s": 30}))
    jobs.drain(60)
    assert st.load(sid).get("bulk_resumed")
    drive(fresh, sid, Oracle(info))
    final = units(fresh, sid)
    assert final["CD8"]["state"] in ("accepted", "accepted_low_confidence"), final["CD8"]
    assert first["state"] in ("decision", "bulk_running")
    record = st.load(sid)
    assert record["images_profiled"] == ["gsynth"]
    after = ok(invoke(fresh, "gating_session_status", {"session_id": sid}))
    assert after["progress"]["not_profiled"] == 0 and "note" not in after["bulk"]


# -- the engine's rules, one answer at a time ------------------------------------------


def look(direction="about_right", *, kind="t2_confirm", confidence="sure", **extra):
    """A T2/T3 answer whose rows say `direction` (`bench.ROWS_FOR`: too_low,
    too_high, about_right; `cannot_tell` judges no row; `no_positives` sees
    no stain anywhere). `extra` adds fields (`within`, `request`, ...)."""
    from plexora.ai.bench import ROWS_FOR

    pattern = extra.pop("pattern", "none" if direction == "no_positives" else "membrane")
    rows = extra.pop("rows", None) or dict(
        ROWS_FOR["too_low"] if direction == "no_positives" else
        ROWS_FOR.get(direction, {"below": "cannot_tell", "near": "cannot_tell",
                                 "above": "cannot_tell"}))
    return {"kind": kind, "rows": rows, "pattern": pattern, "confidence": confidence, **extra}


def next_packet(session, sid):
    result = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 20}))
    return result.get("packet"), result


def answer(session, sid, packet, reply):
    return ok(invoke(session, "gating_answer", {"session_id": sid,
                                                "packet_id": packet["packet_id"],
                                                "answer": reply, "include_next": False}))


def first_packet_for(session, sid, marker, info):
    """Answer every other packet from the truth until `marker` is asked."""
    agent = Oracle(info)
    for _ in range(20):
        packet, result = next_packet(session, sid)
        assert packet is not None, result
        if any(u["marker"] == marker for u in packet["units"]) and packet["kind"] != "t1_strip":
            return packet
        answer(session, sid, packet, agent.answer(packet))
    raise AssertionError(f"{marker} was never asked about")


def test_confirmations_are_free(tmp_path):
    """A technical check does not spend the looks a marker is allowed."""
    make_gating_project(tmp_path, grid=24, size=1024, variant="saturated")
    session = AgentSession()
    sid = start(session, markers=["CD3"], budget={"packets": 1}, display_check="rule")["session_id"]
    packet, _ = next_packet(session, sid)
    assert packet["kind"] == "qc_confirm", packet["kind"]
    answer(session, sid, packet, {"kind": "qc_confirm", "verdict": "real_signal"})
    packet, result = next_packet(session, sid)
    assert packet is not None and packet["kind"] == "t2_confirm", result


def test_a_pending_regression_confirm_is_always_issued(tmp_path, monkeypatch):
    from plexora.plugins.gating.server.autogate import regression

    make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"], budget={"packets": 1}, display_check="rule")["session_id"]
    packet, _ = next_packet(session, sid)
    real = regression.numeric_checks

    def failing(*args, **kwargs):
        result = real(*args, **kwargs)
        return {**result, "ok": False, "failed": ["spatial_tiles"]}

    monkeypatch.setattr(regression, "numeric_checks", failing)
    answer(session, sid, packet, look("about_right"))
    packet, result = next_packet(session, sid)
    assert packet is not None and packet["kind"] == "regression_confirm", result
    answer(session, sid, packet, {"kind": "regression_confirm", "verdict": "holds"})
    assert units(session, sid)["CD4"]["state"] in ("accepted", "accepted_low_confidence")


def test_a_spatial_only_regression_never_moves_the_gate(tmp_path, monkeypatch):
    """Where the positives sit (an enriched edge, one odd region) says nothing
    about which way the threshold is off: the whole-image look may keep or
    reject the gate, and an answer that would chase the artifact is refused
    (lsp11385_2: SMA was raised 6.05 -> 6.36 after an edge check)."""
    from plexora.plugins.gating.server.autogate import regression

    make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"], budget={"packets": 1}, display_check="rule")["session_id"]
    packet, _ = next_packet(session, sid)
    real = regression.numeric_checks

    def failing(*args, **kwargs):
        result = real(*args, **kwargs)
        return {**result, "ok": False, "failed": ["edge_ratio"]}

    monkeypatch.setattr(regression, "numeric_checks", failing)
    answer(session, sid, packet, look("about_right"))
    packet, _ = next_packet(session, sid)
    assert packet["kind"] == "regression_confirm"
    assert set(packet["allowed"]) == {"holds", "artifact", "cannot_tell"}
    refused = invoke(session, "gating_answer", {
        "session_id": sid, "packet_id": packet["packet_id"],
        "answer": {"kind": "regression_confirm", "verdict": "too_low"}})
    assert not refused["ok"] and refused["error"]["code"] == "invalid_input"
    answer(session, sid, packet, {"kind": "regression_confirm", "verdict": "holds"})
    assert units(session, sid)["CD4"]["state"] in ("accepted", "accepted_low_confidence")


@pytest.mark.parametrize("manual", [False, True])
def test_partner_numbers_are_in_the_first_look(tmp_path, manual):
    """CD4's first look already carries its partners -- gated by this run, or
    set by the user and kept."""
    from plexora.plugins.gating.server import model

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    if manual:
        ds = session.data("gsynth")
        cut = float(np.expm1(5.6))
        model.set_gate(ds, "CD3", cut, model.get_gate(ds, "CD3")["high"])
    sid = start(session, markers=["CD3", "CD20", "CD4"])["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    assert packet["kind"] == "t2_confirm"
    partners = {p["partner"]: p for p in packet["evidence"]["partners"]}
    assert "CD3" in partners, packet["evidence"]["partners"]
    assert partners["CD3"]["relation"] in ("subset", "coexpressed")
    assert 0 <= partners["CD3"]["orphan_fraction"] <= 1
    # One line per partner: the rest is code's (the plot, the purity check).
    assert set(partners["CD3"]) <= {"partner", "relation", "grade", "orphan_fraction",
                                    "double_positive_fraction"}
    if manual:
        assert units(session, sid)["CD3"]["state"] == "skipped_manual"
        assert partners["CD3"]["grade"] == "high"


def test_a_reference_request_is_served_when_the_look_is_undecided(tmp_path):
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD20", "CD4"])["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    outcome = answer(session, sid, packet, look(
        "cannot_tell", confidence="unsure", artifact_flags=["image_quality"],
        request={"kind": "reference_channel", "marker": "CD3", "reason": "is it on T cells"}))
    assert outcome["outcome"].get("request_honoured"), outcome
    packet, _ = next_packet(session, sid)
    assert packet["kind"] == "t3_biological"
    assert packet["evidence"]["references"][0] == "CD3"
    assert packet["evidence"]["sheet"]["plot"]["partner"] == "CD3"


def test_an_artifact_flag_lowers_confidence_but_never_reroutes(tmp_path):
    """An incidental remark cannot move a gate: the same judgment with and
    without an artifact flag takes the same path to the same gate."""
    results = {}
    for name, extra in (("plainp", {}), ("flagp", {"artifact_flags": ["image_quality"]})):
        info = make_gating_project(tmp_path, name=name, grid=32, size=1280, markers=HARD)
        session = AgentSession()
        sid = ok(invoke(session, "gating_session_start", {
            "scope": "project", "project": name, "markers": ["CD4"]}))["session_id"]
        jobs.drain(120)
        agent = Oracle(info)
        for _ in range(20):
            packet, _result = next_packet(session, sid)
            if packet["units"] and packet["units"][0]["marker"] == "CD4" \
                    and packet["kind"] == "t2_confirm":
                break
            answer(session, sid, packet, agent.answer(packet))
        outcome = answer(session, sid, packet, look("about_right", **extra))["outcome"]
        results[name] = (outcome["state"], outcome.get("candidate"))
    assert results["flagp"][1] == results["plainp"][1]
    assert results["flagp"][0] != "qc_confirm"


def test_real_signal_after_a_look_gets_a_second_look(tmp_path):
    """A look that saw no stain anywhere is checked on the whole image; when
    the channel is real after all, the next look is beside a reference."""
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD20", "CD4"])["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    answer(session, sid, packet, look("no_positives"))
    packet, _ = next_packet(session, sid)
    assert packet["kind"] == "qc_confirm"
    assert "no cell is positive" in packet["question"]
    answer(session, sid, packet, {"kind": "qc_confirm", "verdict": "real_signal"})
    packet, _ = next_packet(session, sid)
    # A reference is gated, so the second look is beside it.
    assert packet["kind"] == "t3_biological", packet["kind"]
    assert packet["units"][0]["marker"] == "CD4"


def test_a_kept_manual_gate_costs_nothing_twice(tmp_path):
    """ELANE in the live run: kept as the user's gate in one session, then
    asked about in the next because its row looked like the agent's."""
    from plexora.plugins.gating.server import model

    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    ds = session.data("gsynth")
    model.set_gate(ds, "CD20", 777.0, model.get_gate(ds, "CD20")["high"])
    for _ in range(2):
        started = start(session)
        packets = drive(session, started["session_id"], Oracle(info))
        assert not any(u["marker"] == "CD20" for p in packets for u in p["units"])
        assert units(session, started["session_id"])["CD20"]["state"] == "skipped_manual"
        ok(invoke(session, "gating_session_finish", {"session_id": started["session_id"]}))
    gates = ok(invoke(session, "get_all_gates", {"project": "gsynth"}))
    assert gates["provenance"]["CD20"]["method"] == "manual"
    gate = ok(invoke(session, "get_gate", {"project": "gsynth", "marker": "CD20"}))
    assert gate["gate"]["low"] == 777.0


def test_the_next_packet_stays_on_the_unit_being_refined(tmp_path):
    from plexora.plugins.gating.server.autogate import engine

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4", "FOXP3"])["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    st = engine.store()
    record = st.load(sid)
    # Another marker ahead of CD4 in gating order, also waiting for a look.
    other = engine.unit_key("gsynth", "FOXP3")
    record["units"][other]["state"] = "awaiting_t2"
    record["order"] = ["FOXP3", "CD4"]
    st.save(record)
    answer(session, sid, packet, look("too_low"))
    packet, _ = next_packet(session, sid)
    assert packet["units"][0]["marker"] == "CD4" and packet["kind"] == "t4_candidates"


def test_a_low_separation_marker_is_refined_not_reviewed(tmp_path):
    """CD4's populations overlap (D about 2): the mixture's means are no
    ceiling, so "too low" gets candidates rather than manual review."""
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"])["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    outcome = answer(session, sid, packet, look("too_low"))
    assert not outcome["outcome"].get("contradiction"), outcome
    packet, _ = next_packet(session, sid)
    assert packet["kind"] == "t4_candidates"


def test_a_mixed_first_row_keeps_the_gate_at_a_capped_confidence(tmp_path):
    """Overlapping populations: the cells at the boundary are honestly mixed.
    The gate stays where the look found it -- never closed for that alone --
    at no more than moderate confidence, and the whole-image checks still have
    their say."""
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"])["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    answer(session, sid, packet, look("too_high"))
    packet, _ = next_packet(session, sid)
    assert packet["kind"] == "t4_candidates"
    from plexora.plugins.gating.server.autogate import engine

    unit = engine.store().load(sid)["units"][engine.unit_key("gsynth", "CD4")]
    assert unit["candidates_reach_edge"], unit.get("candidates")
    rows = {r["row"]: "mixed" for r in packet["evidence"]["intervals"]}
    outcome = answer(session, sid, packet, {"kind": "t4_candidates", "intervals": rows,
                                            "confidence": "fairly_sure"})["outcome"]
    assert outcome["chosen"] == "keep" and outcome["why"] == "mixed", outcome
    following, _ = next_packet(session, sid)
    if following is not None:
        assert following["kind"] == "regression_confirm", following["kind"]
        answer(session, sid, following, {"kind": "regression_confirm", "verdict": "holds"})
    final = units(session, sid)["CD4"]
    assert final["state"] in ("accepted", "accepted_low_confidence"), final
    assert final["confidence"] in ("moderate", "low")


def test_an_outstanding_packet_can_be_drawn_again(tmp_path):
    """The same packet, the same charge, new images -- after a renderer
    change, or when its images were lost."""
    import shutil

    from plexora.plugins.gating.server.autogate import engine

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"], display_check="rule")["session_id"]
    first, _ = next_packet(session, sid)
    folder = engine.store().folder(sid) / "packets" / first["packet_id"]
    shutil.rmtree(folder)
    again = ok(invoke(session, "gating_next", {"session_id": sid}))
    assert again["packet"]["packet_id"] == first["packet_id"]
    assert len(again["_images"]) == len(first["images"]) and again["packet"]["rerendered"] == 1
    redrawn = ok(invoke(session, "gating_next", {"session_id": sid, "rerender": True}))
    assert redrawn["packet"]["packet_id"] == first["packet_id"]
    assert redrawn["packet"]["rerendered"] == 2
    record = engine.store().load(sid)
    assert record["units"][engine.unit_key("gsynth", "CD4")]["used"]["packets"] == 1
    answered = answer(session, sid, redrawn["packet"], Oracle(info).answer(redrawn["packet"]))
    assert answered["applied"]


# -- failed markers, stop, the expression source (2026-09-26, second round) ---------


class FailingQC(Oracle):
    """Calls a flagged channel failed (the oracle calls it real)."""

    def answer(self, packet):
        if packet["kind"] == "qc_confirm":
            return {"kind": "qc_confirm", "verdict": "technical_failure"}
        return super().answer(packet)


def test_a_failed_stain_is_written_as_an_empty_gate_and_undone(tmp_path):
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import provenance

    info = make_gating_project(tmp_path, variant="flat")
    session = AgentSession()
    started = start(session, markers=["CD20"])
    drive(session, started["session_id"], FailingQC(info))
    final = units(session, started["session_id"])["CD20"]
    assert final["state"] == "technically_failed"
    ds = session.data("gsynth")
    gate = model.get_gate(ds, "CD20")
    top = float(model._description(ds)["CD20"]["max"])
    assert gate["low"] == gate["high"] == top
    assert model.gated_summary(ds, "CD20")["n_positive"] == 0
    assert "CD20" in model.active_gates(ds)
    row = provenance.read("gsynth")["CD20"]
    assert row["method"] == "failed_marker" and row["confidence"] == "failed_qc"
    assert "failed_marker" in row["detail"]["flags"] and row["detail"]["reason"]
    finished = ok(invoke(session, "gating_session_finish",
                         {"session_id": started["session_id"], "action": "rollback"}))
    assert finished["undone"] and not finished["refused"]
    assert "CD20" not in model.active_gates(session.data("gsynth"))


class NoPositives(Oracle):
    """Says a marker has no positive cell, then confirms it on the whole image."""

    def __init__(self, info, marker, **kwargs):
        super().__init__(info, **kwargs)
        self.marker = marker

    def answer(self, packet):
        marker = packet["units"][0]["marker"] if packet["units"] else None
        if marker == self.marker and packet["kind"] in ("t2_confirm", "t3_biological"):
            return look("no_positives", kind=packet["kind"])
        if marker == self.marker and packet["kind"] == "qc_confirm":
            assert "no cell is positive" in packet["question"] or \
                "no cell is positive" in " ".join(packet["evidence"].get("flags") or []) \
                or "no_positive_population" in packet["allowed"]
            return {"kind": "qc_confirm", "verdict": "no_positive_population"}
        return super().answer(packet)


def test_no_positives_from_a_look_is_confirmed_on_the_whole_image_then_written(tmp_path):
    from plexora.plugins.gating.server import model

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    started = start(session, markers=["CD4"])
    seen = drive(session, started["session_id"], NoPositives(info, "CD4"))
    kinds = [p["kind"] for p in seen]
    final = units(session, started["session_id"])["CD4"]
    if final["state"] == "accepted" and "t2_confirm" not in kinds:
        pytest.skip("CD4 was settled at T1 in this scene")
    assert "qc_confirm" in kinds
    assert final["state"] == "no_positive_population"
    gate = model.get_gate(session.data("gsynth"), "CD4")
    assert gate["low"] == gate["high"]
    qc = ok(invoke(session, "gating_qc", {"project": "gsynth"}))
    assert "CD4" in qc["needs_review"]
    assert qc["gated"]["CD4"]["no_positives"]


def test_every_look_carries_the_context_sheet(tmp_path):
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    started = start(session, markers=["CD3", "CD4"])
    seen = drive(session, started["session_id"], Oracle(info, style="noisy", seed=2))
    looks = [p for p in seen if p["kind"] in ("t2_confirm", "t3_biological",
                                              "t4_candidates", "qc_confirm",
                                              "regression_confirm")]
    assert looks
    for packet in looks:
        roles = [i["role"] for i in packet["images"]]
        # A sheet this conversation was already shown whole is sent as a
        # pointer to that packet (`sheets: trim`).
        assert ("context_sheet" in roles or "sheet" in packet.get("_as_in", ())) \
            and len(roles) <= 2
        # A whole-image check draws the slide row only when the reader was
        # shown the fields already (`sheets: trim`).
        assert "fields" in packet["evidence"] or packet["kind"] == "regression_confirm"
        guide = packet["evidence"]["guide"]
        assert "sheet" in guide and any(k.startswith("compartment:") for k in guide)
        assert packet["answer_schema"]["see"].endswith(packet["kind"])


def test_stop_from_the_viewer_halts_the_session(tmp_path):
    from plexora.plugins.gating.server.autogate import engine

    make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    started = start(session, markers=["CD3", "CD4"])
    sid = started["session_id"]
    engine.store().set_control(sid, stopped=True, stopped_by="viewer")
    halted = ok(invoke(session, "gating_next", {"session_id": sid}))
    assert halted["state"] == "stopped" and "rollback" in halted["next"]
    again = ok(invoke(session, "gating_answer", {"session_id": sid, "packet_id": "pk_0001",
                                                 "answer": {"kind": "t2_confirm"}}))
    assert again["state"] == "stopped"
    finished = ok(invoke(session, "gating_session_finish", {"session_id": sid}))
    assert finished["action"] == "cancel"
    assert finished["summary"]["units_total"] == 2
    ok(invoke(session, "gating_session_finish", {"session_id": sid}))


def _unconfirm_features(name="gsynth"):
    from plexora.server.models.project import Project

    Project.mutate(name, lambda p: p.patch(
        confirmed=tuple(k for k in p.confirmed if k != "features")))


def test_raw_intensities_get_log1p_without_asking(tmp_path):
    make_gating_project(tmp_path)
    _unconfirm_features()
    session = AgentSession()
    inspected = ok(invoke(session, "inspect_expression_sources", {"project": "gsynth"}))
    assert inspected["options"][0]["kind"] in ("raw_intensity", "raw_counts")
    assert inspected["recommendation"]["confidence"] == "certain"
    started = start(session, markers=["CD3"])
    assert started["expression"]["status"] == "applied"
    assert started["state"] == "created"
    record = session.project("gsynth")
    assert "features" in record.confirmed and record.log_transformed
    listed = ok(invoke(session, "inspect_project", {"project": "gsynth"}))
    assert listed["table"]["expression"]["confirmed"]


def test_an_unclear_matrix_is_asked_before_the_bulk_pass(tmp_path):
    make_gating_project(tmp_path, log_transformed=False)
    _unconfirm_features()
    session = AgentSession()
    from plexora.plugins.gating import capabilities_session as cs

    real = cs._expression_check

    def ask(call, images):
        # What an ambiguous file yields (two log-like layers); a CSV cannot.
        receipts = []
        out = {"status": "pending", "projects": images, "applied": [],
               "source_kind": "csv",
               "current": {"features_layer": "X", "features_log": False,
                           "confirmed": False},
               "options": [{"value": "X", "label": "the table's values",
                            "kind": "log_like", "stats": {}}],
               "recommendation": {"choice": None, "confidence": "ask", "why": "test"},
               "rule": "r"}
        return out, receipts

    cs._expression_check = ask
    try:
        started = ok(invoke(session, "gating_session_start", {
            "scope": "project", "project": "gsynth", "markers": ["CD3"]}))
    finally:
        cs._expression_check = real
    assert started["state"] == "needs_setup" and started["job_id"] is None
    packet = started["packet"]
    assert packet["kind"] == "expression_setup" and packet["evidence"]["ask_user_required"]
    sid = started["session_id"]
    repeat = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 0}))
    assert repeat["state"] == "needs_setup"
    refused = invoke(session, "gating_answer", {"session_id": sid,
                                                "packet_id": packet["packet_id"],
                                                "answer": {"kind": "expression_setup",
                                                           "features_layer": "X",
                                                           "features_log": True}})
    assert not refused["ok"] and refused["error"]["code"] == "invalid_input"
    answered = ok(invoke(session, "gating_answer", {
        "session_id": sid, "packet_id": packet["packet_id"],
        "answer": {"kind": "expression_setup", "features_layer": "X",
                   "features_log": False}}))
    assert answered["outcome"]["applied"] == ["gsynth"]
    jobs.drain(120)
    assert "features" in session.project("gsynth").confirmed
    status = ok(invoke(session, "gating_session_status", {"session_id": sid}))
    assert status["state"] in ("bulk_running", "deciding", "created")
    assert status["bulk"]["job_id"]


def _unconfirm(keys, name="gsynth"):
    from plexora.server.models.project import Project

    Project.mutate(name, lambda p: p.patch(confirmed=tuple(k for k in p.confirmed
                                                           if k not in keys)))


def _pending_matrix(call, images):
    """What `_expression_check` yields for a matrix its values leave open."""
    return {"status": "pending", "projects": images, "applied": [], "source_kind": "csv",
            "current": {"features_layer": "X", "features_log": False, "confirmed": False},
            "options": [{"value": "X", "label": "the table's values", "kind": "log_like",
                         "stats": {}}],
            "recommendation": {"choice": None, "confidence": "ask", "why": "test"},
            "rule": "r"}, []


ROLES = ("role:cell_id", "role:x", "role:y", "role:image_id")


def test_a_run_from_the_viewer_waits_for_the_set_up_form(tmp_path):
    """setup=wait (Plexora AI started in the viewer): columns nobody
    confirmed hold the session in needs_setup -- no packet a model could
    answer in the user's place -- until the user's form confirms them; it is
    then planned and the bulk pass runs (2026-10-15: a run went on while the
    form was still open, and the user's answer switched the matrix under it)."""
    from plexora.server.models.project import Project

    make_gating_project(tmp_path)
    _unconfirm(ROLES)
    session = AgentSession()
    started = ok(invoke(session, "gating_session_start", {
        "scope": "project", "project": "gsynth", "markers": ["CD3"], "setup": "wait"}))
    assert started["state"] == "needs_setup" and started["job_id"] is None
    assert "packet" not in started
    assert set(started["setup"]["unconfirmed"]) == set(ROLES)
    sid = started["session_id"]
    waiting = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 0}))
    assert waiting["state"] == "needs_setup" and "packet" not in waiting
    assert "Cell ID column" in waiting["note"]
    status = ok(invoke(session, "gating_session_status", {"session_id": sid}))
    assert status["setup"]["keys"] == started["setup"]["keys"]
    # What the form's Continue does (tool_routes.satisfy_requirements).
    Project.mutate("gsynth", lambda p: p.with_confirmed(ROLES))
    going = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 0}))
    assert going["state"] != "needs_setup"
    jobs.drain(120)
    status = ok(invoke(session, "gating_session_status", {"session_id": sid}))
    assert status["n_units"] == 1 and "setup" not in status
    assert status["state"] in ("bulk_running", "deciding", "created")


def test_a_column_not_set_is_refused_unless_the_run_can_wait(tmp_path):
    import dataclasses

    from plexora.server.models.project import Project

    make_gating_project(tmp_path)
    # A multi-image table with no image-id column named: only the user knows it.
    Project.mutate("gsynth", lambda p: p.patch(
        dataset=dataclasses.replace(p.dataset, single_image=False)))
    session = AgentSession()
    for setup in ("ask", "fail"):
        refused = invoke(session, "gating_session_start", {
            "scope": "project", "project": "gsynth", "markers": ["CD3"], "setup": setup})
        assert not refused["ok"] and refused["error"]["code"] == "precondition_missing"
        assert "Image ID column is not set" in refused["error"]["message"]
        assert refused["error"]["detail"]["setup"]["missing"] == ["role:image_id"]
    waited = ok(invoke(session, "gating_session_start", {
        "scope": "project", "project": "gsynth", "markers": ["CD3"], "setup": "wait"}))
    # Nothing planned without it: the plan is made once the form has it.
    assert waited["state"] == "needs_setup" and waited["n_units"] == 0
    assert waited["setup"]["missing"] == ["role:image_id"]


def test_a_command_line_run_says_how_to_name_a_matrix_it_cannot_settle(tmp_path, monkeypatch):
    from plexora.plugins.gating import capabilities_session as cs

    make_gating_project(tmp_path)
    _unconfirm(("features",))
    monkeypatch.setattr(cs, "_expression_check", _pending_matrix)
    session = AgentSession()
    refused = invoke(session, "gating_session_start", {
        "scope": "project", "project": "gsynth", "markers": ["CD3"], "setup": "fail"})
    assert not refused["ok"] and refused["error"]["code"] == "precondition_missing"
    assert "--matrix" in refused["error"]["message"] and "--log1p" in refused["error"]["message"]
    # Waiting, the matrix is one more thing the form asks.
    waited = ok(invoke(session, "gating_session_start", {
        "scope": "project", "project": "gsynth", "markers": ["CD3"], "setup": "wait"}))
    assert waited["state"] == "needs_setup" and "features" in waited["setup"]["keys"]
    assert "packet" not in waited


def test_a_named_matrix_is_applied_and_a_model_reading_never_overrides_a_confirmed_one(tmp_path):
    make_gating_project(tmp_path)          # raw intensities, confirmed with log1p off
    session = AgentSession()
    started = start(session, markers=["CD3"], expression={
        "features_layer": "X", "features_log": True, "basis": "user"})
    assert started["expression"]["status"] == "applied"
    assert started["expression"]["applied"][0]["basis"] == "user"
    assert session.project("gsynth").log_transformed
    inferred = start(session, markers=["CD3"], expression={
        "features_layer": "X", "features_log": False, "basis": "inferred"})
    assert not inferred["expression"].get("applied")
    assert session.project("gsynth").log_transformed
    refused = invoke(session, "gating_session_start", {
        "scope": "project", "project": "gsynth", "markers": ["CD3"],
        "expression": {"features_layer": "layer:nope", "features_log": False}})
    assert not refused["ok"] and refused["error"]["code"] == "invalid_input"


def test_a_matrix_switched_under_a_run_stops_it_saying_why(tmp_path):
    from plexora.api import features

    make_gating_project(tmp_path)
    session = AgentSession()
    started = start(session, markers=["CD3", "CD8"])
    sid = started["session_id"]
    # What the set-up form or Edit project does while the run goes on.
    features.apply("gsynth", features_log=True)
    halted = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 0}))
    assert halted["state"] == "stopped" and halted["by"] == "matrix_changed"
    assert "from X with log1p off to X with log1p on" in halted["reason"]
    again = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 0}))
    assert again["state"] == "stopped" and again["reason"] == halted["reason"]


def test_a_rollback_undoes_every_write_newest_first(tmp_path):
    from plexora.plugins.gating.server import model

    info = make_gating_project(tmp_path)
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], Oracle(info))
    status = ok(invoke(session, "gating_session_status", {"session_id": started["session_id"]}))
    assert status["receipts"] >= 2
    finished = ok(invoke(session, "gating_session_finish",
                         {"session_id": started["session_id"], "action": "rollback"}))
    assert not finished["refused"], finished["refused"]
    assert len(finished["undone"]) == status["receipts"]
    assert model.active_gates(session.data("gsynth")) == {}


# -- round three -----------------------------------------------------------------------


def test_the_reading_guide_comes_once_and_packets_stay_lean(tmp_path):
    import json as _json

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    started = start(session, markers=["CD3", "CD20", "CD4"], delegate=False)
    sid = started["session_id"]
    guide = started["reading_guide"]
    assert "sheet" in guide and "partners" in guide
    packet = first_packet_for(session, sid, "CD4", info)
    assert packet["kind"] == "t2_confirm"
    evidence = packet["evidence"]
    assert list(evidence)[0] == "partners"
    assert set(evidence["guide"]) <= set(guide)
    for dropped in ("display", "strata_counts", "qc_flags"):
        assert dropped not in evidence
    assert set(evidence["profile"]) <= {"class", "separation_d", "positive_fraction",
                                        "n_positive", "n_cells", "signal_to_background"}
    assert all(set(f) <= {"field_id", "class", "cells", "positives"}
               for f in evidence["fields"])
    assert evidence["sheet"]["plot"].get("why") or evidence["sheet"]["plot"]["kind"] == \
        "histogram"
    # The first live run's packets were ~9k characters of JSON; the schema is
    # in the guide, so a packet only names it.
    assert packet["answer_schema"] == {"see": "reading_guide.answer_schemas.t2_confirm"}
    assert "t2_confirm" in guide["answer_schemas"]
    assert len(_json.dumps(packet, default=str, separators=(",", ":"))) <= 4500
    status = ok(invoke(session, "gating_session_status", {"session_id": sid}))
    assert status["reading_guide"] == guide
    held = ok(invoke(session, "gating_session_status", {
        "session_id": sid, "known_guide": started["guide_version"]}))
    assert held["guide_version"] == started["guide_version"]
    assert not isinstance(held["reading_guide"], dict)


def test_every_packet_can_carry_its_own_reading(tmp_path):
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    started = start(session, markers=["CD3", "CD20", "CD4"], reading="every_packet")
    assert "reading_guide" not in started
    packet = first_packet_for(session, started["session_id"], "CD4", info)
    assert "three scales" in packet["evidence"]["how_to_read"]
    assert "guide" not in packet["evidence"]


def _pixel_answer(session, sid, **reply):
    packet, result = next_packet(session, sid)
    assert packet is not None and packet["kind"] == "pixel_setup", result
    assert packet["images"][0]["role"] == "pixel_snapshots"
    answer(session, sid, packet, {"kind": "pixel_setup", **reply})
    return packet


def test_an_image_without_a_pixel_size_is_estimated_first(tmp_path):
    from plexora.server.utils import pixel_scale

    info = make_gating_project(tmp_path, calibrated=False)
    session = AgentSession()
    started = start(session, markers=["CD8"])
    sid = started["session_id"]
    assert started["pixel"]["status"] == "pending"
    packet = _pixel_answer(session, sid, basis="adjusted", microns_per_pixel=0.5)
    estimate = packet["evidence"]["estimate"]
    # Synthetic discs are ~36 px across: a 9 µm prior puts them at ~0.25 µm/px.
    assert 0.15 < estimate["microns_per_pixel"] < 0.4, estimate
    assert estimate["how"] == "mask"
    # The session's value sizes the pictures; the project stays uncalibrated.
    assert pixel_scale.pixel_size(session.project("gsynth")) is None
    status = ok(invoke(session, "gating_session_status", {"session_id": sid}))
    assert status["pixel"]["projects"]["gsynth"] == {"status": "applied", "value": 0.5,
                                                     "basis": "adjusted"}
    agent = Oracle(info)
    for _ in range(10):
        look_packet, result = next_packet(session, sid)
        if look_packet is None:
            break
        if look_packet["kind"] in ("t2_confirm", "qc_confirm", "regression_confirm"):
            field = look_packet["evidence"]["sheet"]["field"]
            assert field["source"] == "estimated" and field["um_per_px"] == 0.5
            assert field["label"].startswith("≈")
            break
        answer(session, sid, look_packet, agent.answer(look_packet))


def test_a_pixel_size_the_user_stated_is_written_and_undone(tmp_path):
    from plexora.server.utils import pixel_scale

    info = make_gating_project(tmp_path, calibrated=False)
    session = AgentSession()
    sid = start(session, markers=["CD8"])["session_id"]
    _pixel_answer(session, sid, basis="user_stated", microns_per_pixel=0.65)
    session.invalidate("gsynth")
    stated = pixel_scale.pixel_size(session.project("gsynth"))
    assert stated["value"] == pytest.approx(0.65) and stated["source"] == "manual"
    drive(session, sid, Oracle(info))
    finished = ok(invoke(session, "gating_session_finish",
                         {"session_id": sid, "action": "rollback"}))
    assert not finished["refused"], finished["refused"]
    session.invalidate("gsynth")
    assert pixel_scale.pixel_size(session.project("gsynth")) is None


def test_a_pixel_answer_without_a_value_is_refused(tmp_path):
    make_gating_project(tmp_path, calibrated=False)
    session = AgentSession()
    sid = start(session, markers=["CD8"])["session_id"]
    packet, _ = next_packet(session, sid)
    refused = invoke(session, "gating_answer", {
        "session_id": sid, "packet_id": packet["packet_id"], "include_next": False,
        "answer": {"kind": "pixel_setup", "basis": "user_stated"}})
    assert not refused["ok"] and refused["error"]["code"] == "invalid_input"


def test_a_calibrated_image_needs_no_pixel_setup(tmp_path):
    make_gating_project(tmp_path)
    session = AgentSession()
    started = start(session, markers=["CD8"])
    assert started["pixel"]["status"] == "calibrated"
    packet, _ = next_packet(session, started["session_id"])
    assert packet is None or packet["kind"] != "pixel_setup"


def _to_display(session, sid, agent):
    """Answer the set-up before the contrast check; returns its packet."""
    for _ in range(6):
        packet, result = next_packet(session, sid)
        assert packet is not None, result
        if packet["kind"] == "display_setup":
            return packet
        assert packet["kind"] in schemas.SETUP_KINDS, packet["kind"]
        answer(session, sid, packet, agent.answer(packet))
    raise AssertionError("no contrast check")


def test_the_contrast_check_comes_before_the_first_look(tmp_path):
    from plexora.agent.evidence import calibration, collage

    info = make_gating_project(tmp_path)
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD8", "CD20"])["session_id"]
    packet = _to_display(session, sid, Oracle(info))
    rows = packet["evidence"]["rows"]
    assert {r["marker"] for r in rows} == {"CD3", "CD8", "CD20"}
    assert len(rows) <= schemas.ENGINE["display_batch"] and all(r["pattern"] for r in rows)
    assert packet["images"][0]["role"] == "display_grid"
    assert packet["allowed"]["columns"] == list(calibration.COLUMN_IDS)
    assert packet["task"] == "gating.display"
    answer(session, sid, packet, {
        "kind": "display_setup",
        "background_ok": {"CD3": ["c2", "c3"], "CD8": ["c3", "c4"], "CD20": ["c3"]},
        "structure_ok": {"CD3": ["c1", "c2"], "CD8": ["c3", "c4"], "CD20": []}})
    record = calibration.load("gsynth")
    choices = record["choices"]
    assert (choices["CD3"]["column"], choices["CD3"]["chosen_by"]) == ("c2", "vision")
    assert (choices["CD8"]["column"], choices["CD8"]["chosen_by"]) == ("c3", "vision")
    assert choices["CD20"]["chosen_by"] == "rule:pattern_not_seen"
    assert choices["CD3"]["answers"]["structure_ok"] == ["c1", "c2"]
    cd3 = record["channels"]["CD3"]
    assert cd3["window"][1] == pytest.approx(cd3["columns"][1]["white"])
    assert cd3["chosen_by"] == "vision"
    # Every later picture draws with the choice.
    drawn = collage.resolve_windows(session, session.project("gsynth"), {"CD3": None})
    assert drawn["CD3"]["window"] == pytest.approx(cd3["window"])
    following, _ = next_packet(session, sid)
    assert following is None or following["kind"] != "display_setup"
    status = ok(invoke(session, "gating_session_status", {"session_id": sid}))
    assert status["display"]["status"] == "applied"
    assert status["display"]["projects"]["gsynth"]["chosen"]["CD3"] == "vision"


def test_a_contrast_check_drawn_again_is_the_same_grid(tmp_path):
    import shutil

    from plexora.plugins.gating.server.autogate import engine

    info = make_gating_project(tmp_path)
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD8"])["session_id"]
    first = _to_display(session, sid, Oracle(info))
    shutil.rmtree(engine.store().folder(sid) / "packets" / first["packet_id"])
    again = ok(invoke(session, "gating_next", {"session_id": sid, "rerender": True}))["packet"]
    assert again["packet_id"] == first["packet_id"] and again["kind"] == "display_setup"
    assert again["evidence"]["rows"] == first["evidence"]["rows"]


def test_a_contrast_choice_outlives_the_session(tmp_path):
    info = make_gating_project(tmp_path)
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD8"])["session_id"]
    seen = drive(session, sid, Oracle(info))
    assert [p["kind"] for p in seen].count("display_setup") == 1
    ok(invoke(session, "gating_session_finish", {"session_id": sid, "action": "close"}))
    again = start(session, markers=["CD3", "CD8"])["session_id"]
    seen = drive(session, again, Oracle(info))
    assert "display_setup" not in [p["kind"] for p in seen]


def test_a_rule_session_asks_no_contrast_question(tmp_path):
    info = make_gating_project(tmp_path)
    session = AgentSession()
    started = start(session, markers=["CD3", "CD8"], display_check="rule")
    seen = drive(session, started["session_id"], Oracle(info))
    assert "display_setup" not in [p["kind"] for p in seen]
    status = ok(invoke(session, "gating_session_status",
                       {"session_id": started["session_id"]}))
    assert status["display"]["status"] == "rule"


def test_two_unusable_contrast_answers_keep_the_rule(tmp_path):
    from plexora.agent.evidence import calibration

    info = make_gating_project(tmp_path)
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD8"])["session_id"]
    packet = _to_display(session, sid, Oracle(info))
    for _ in range(2):
        refused = invoke(session, "gating_answer", {
            "session_id": sid, "packet_id": packet["packet_id"], "include_next": False,
            "answer": {"kind": "display_setup", "background_ok": {"CD99": ["c3"]},
                       "structure_ok": {}}})
        assert not refused["ok"] and refused["error"]["code"] == "invalid_input"
    following, _ = next_packet(session, sid)
    assert following is None or following["kind"] != "display_setup"
    status = ok(invoke(session, "gating_session_status", {"session_id": sid}))
    assert set(status["display"]["projects"]["gsynth"]["chosen"].values()) == {"rule:no_answer"}
    assert not (calibration.load("gsynth") or {}).get("choices")


def test_the_viewer_is_shown_each_marker_settle(tmp_path):
    """The mirror: the contrast check's image opened in HD, and what it
    settles shown channel by channel in the second slot (`animate_contrast`,
    one command the tab plays while the session goes on), each with one of
    its bright cells to zoom in on."""
    from plexora.agent.evidence import calibration
    from plexora.plugins.gating.server.autogate import mirror_script

    make_gating_project(tmp_path)
    session = AgentSession()
    record, _changed = calibration.calibrate(session, "gsynth")
    script = mirror_script.script_for({"kind": "display_setup", "units": [],
                                       "evidence": {"project": "gsynth"}}, None, record,
                                      current_project="elsewhere")
    assert [c["type"] for c in script] == ["open_project", "set_hd_mode"]
    animate = mirror_script.display_script(record, ["DNA", "CD3", "CD8", "nope"])
    assert [c["type"] for c in animate] == ["animate_contrast"]
    channels = animate[0]["arguments"]["channels"]
    assert [c["name"] for c in channels] == ["DNA", "CD3", "CD8"]
    # The windows the canvas keeps: a marker a touch brighter than the evidence.
    cd3 = record["channels"]["CD3"]
    assert channels[1]["window"] == pytest.approx(calibration.view_spec(cd3)["window"])
    assert channels[1]["window"][0] == pytest.approx(cd3["window"][0])
    assert channels[1]["window"][1] < cd3["window"][1]
    assert channels[1]["curve"] == calibration.viewer_curve(calibration.entry_curve(cd3))
    # The nuclear stain in the first slot, in the tab's own colour.
    assert channels[0]["slot"] == mirror_script.NUCLEAR_SLOT and "color" not in channels[0]
    assert channels[0]["window"] == pytest.approx(record["channels"]["DNA"]["view"]["window"])
    assert all("slot" not in c for c in channels[1:])
    assert "{name}" in animate[0]["arguments"]["caption"]
    arguments = animate[0]["arguments"]
    assert arguments["pace"] == "natural" and arguments["slot"] == mirror_script.DISPLAY_SLOT
    bright = cd3["cells"]["bright"]
    best = max(bright, key=lambda c: c["value"])
    assert channels[1]["focus"] == {"x": pytest.approx(best["x"], abs=0.1),
                                    "y": pytest.approx(best["y"], abs=0.1)}
    rule = {"window": [0.0, 10.0], "curve": None}
    many = {"channels": {f"M{i}": {"role": "marker", "window": [0.0, 10.0], "rule": rule}
                         for i in range(mirror_script.DISPLAY_PACED + 1)}}
    quick = mirror_script.display_script(many, list(many["channels"]))[0]["arguments"]
    assert quick["pace"] == "brisk" and "focus" not in quick["channels"][0]
    # The nuclear stain does not count towards the pace.
    paced = dict(list(many["channels"].items())[:mirror_script.DISPLAY_PACED])
    paced["DNA"] = record["channels"]["DNA"]
    natural = mirror_script.display_script({"channels": paced}, list(paced))[0]["arguments"]
    assert natural["pace"] == "natural"
    # A contrast run opens its image with no tool: the channel panel is the show.
    bare = mirror_script.script_for({"kind": "display_setup", "units": [],
                                     "evidence": {"project": "gsynth"}}, None, record,
                                    current_project="elsewhere", tool=None)
    assert "tool" not in bare[0]["arguments"]
    # A contrast check names its one image; the mirror opens that one.
    assert mirror_script._packet_project({"evidence": {"project": "gsynth"}}) == "gsynth"


def test_the_viewer_is_shown_only_what_the_check_judged(tmp_path):
    """A marker the rule or an earlier check settled is not played again;
    the rows this answer judged are."""
    from plexora.plugins.gating.server.autogate import engine

    info = make_gating_project(tmp_path)
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD8", "CD20"])["session_id"]
    packet = _to_display(session, sid, Oracle(info))
    answer(session, sid, packet, {
        "kind": "display_setup", "background_ok": {"CD3": ["c3"], "CD8": ["c3"]},
        "structure_ok": {"CD3": ["c3"], "CD8": ["c3"]}})
    entry = engine.store().load(sid)["display"]["projects"]["gsynth"]
    assert set(entry["unshown"]) == {"CD3", "CD8"}           # CD20: left out, kept the rule


def test_the_nuclear_stain_leads_the_first_show_only():
    from types import SimpleNamespace

    from plexora.plugins.gating.server.autogate import mirror_script

    projects = {"p": {"unshown": []}}
    engine = SimpleNamespace(record={"display": {"projects": projects}})
    assert mirror_script._unshown(engine, "p", nuclear="DNA") == []     # never alone
    projects["p"]["unshown"] = ["CD3", "CD8"]
    assert mirror_script._unshown(engine, "p", nuclear="DNA") == ["DNA", "CD3", "CD8"]
    projects["p"]["unshown"] = ["CD20"]
    assert mirror_script._unshown(engine, "p", nuclear="DNA") == ["CD20"]


def test_the_canvas_is_offered_the_nuclear_stain_and_brighter_markers(tmp_path):
    """The viewer's offer (`display_contrast.entries`): the nuclear stain on
    its own nuclei, dimmer than a marker, and each marker a touch brighter
    than the evidence -- while the evidence keeps its own windows."""
    from plexora.agent.evidence import calibration, display_contrast
    from plexora.server.utils import display_curve

    make_gating_project(tmp_path)
    session = AgentSession()
    record, _changed = calibration.calibrate(session, "gsynth")
    offered = display_contrast.entries(record)
    dna = record["channels"]["DNA"]
    assert dna["role"] == "nuclear" and dna["view"]["chosen_by"] == "rule:nuclear"
    assert offered["DNA"]["chosen_by"] == "rule:nuclear"
    view, anchors = dna["view"], dna["view"]["anchors"]
    curve = display_curve.normalise(view["curve"])
    typical = display_curve.level(anchors["typical"], view["window"], curve)
    # At its level on a curve; at least there when a straight line already lifts it.
    assert typical == pytest.approx(calibration.NUCLEAR_LEVELS["typical"], abs=1e-3) \
        if curve else typical >= calibration.NUCLEAR_LEVELS["typical"] - 1e-3
    assert display_curve.level(anchors["titre"], view["window"], curve) == \
        pytest.approx(calibration.NUCLEAR_LEVELS["titre"], abs=1e-3)
    assert calibration.NUCLEAR_LEVELS["titre"] < calibration.TITRE_LEVEL
    # The evidence's nuclear window is the overview's, as before.
    assert dna["window"] != view["window"]
    cd3 = record["channels"]["CD3"]
    titre = cd3["anchors"]["titre"]
    shown = display_curve.level(titre, offered["CD3"]["window"],
                                display_curve.normalise(offered["CD3"]["curve"]))
    evidence = display_curve.level(titre, cd3["window"], calibration.entry_curve(cd3))
    assert shown == pytest.approx(min(1.0, evidence * calibration.VIEW_GAIN), abs=1e-6)
    linear = calibration.linear_window(cd3)
    assert display_curve.level(titre, offered["CD3"]["linear_window"]) == \
        pytest.approx(min(1.0, display_curve.level(titre, linear) * calibration.VIEW_GAIN))
    # The Auto wand carries a channel's auto contrast once a check settled it.
    assert not any(e["checked"] for e in offered.values())
    record, _rev = calibration.record_choices("gsynth", {"CD3": {
        "column": "c2", "gain": 0.8, "band": "negatives", "chosen_by": "vision"}})
    offered = display_contrast.entries(record)
    assert offered["CD3"]["checked"] and offered["DNA"]["checked"]
    assert not offered["CD8"]["checked"]


def _contrast(session, **options):
    started = ok(invoke(session, "contrast_session_start", {"scope": "project",
                                                            "project": "gsynth", **options}))
    jobs.drain(120)
    return started


def test_auto_contrast_runs_on_its_own_and_gates_nothing(tmp_path):
    from plexora.agent.evidence import calibration
    from plexora.plugins.gating.server import model

    info = make_gating_project(tmp_path)
    session = AgentSession()
    started = _contrast(session, markers=["CD3", "CD8", "CD20"])
    assert started["purpose"] == "contrast"
    sid = started["session_id"]
    seen = drive(session, sid, Oracle(info))
    kinds = [p["kind"] for p in seen]
    assert kinds.count("display_setup") == 1
    assert set(kinds) <= set(schemas.SETUP_KINDS), kinds          # no look, no strip
    assert {u["state"] for u in units(session, sid).values()} == {"display_set"}
    assert set((calibration.load("gsynth") or {}).get("choices") or {}) == {"CD3", "CD8", "CD20"}
    ds = session.data("gsynth")
    assert not [r for r in model.gate_rows(ds) if r.get("channel") in ("CD3", "CD8", "CD20")]
    following, result = next_packet(session, sid)
    assert following is None and result["state"] == "decided"
    assert "gating_report" not in result["next"]
    ok(invoke(session, "gating_session_finish", {"session_id": sid, "action": "close"}))
    # Gating afterwards skips the step: the image's contrast is already set.
    gating = start(session, markers=["CD3", "CD8", "CD20"])["session_id"]
    seen = drive(session, gating, Oracle(info))
    assert "display_setup" not in [p["kind"] for p in seen]
    status = ok(invoke(session, "gating_session_status", {"session_id": gating}))
    assert status["display"]["status"] == "applied"


def test_auto_contrast_skips_what_is_set_unless_redone(tmp_path):
    info = make_gating_project(tmp_path)
    session = AgentSession()
    first = _contrast(session, markers=["CD3", "CD8"])["session_id"]
    drive(session, first, Oracle(info))
    ok(invoke(session, "gating_session_finish", {"session_id": first, "action": "close"}))
    again = _contrast(session, markers=["CD3", "CD8"])["session_id"]
    assert "display_setup" not in [p["kind"] for p in drive(session, again, Oracle(info))]
    assert {u["state"] for u in units(session, again).values()} == {"display_set"}
    ok(invoke(session, "gating_session_finish", {"session_id": again, "action": "close"}))
    redo = _contrast(session, markers=["CD3"], redo=True)["session_id"]
    seen = drive(session, redo, Oracle(info))
    grids = [p for p in seen if p["kind"] == "display_setup"]
    assert [[r["marker"] for r in g["evidence"]["rows"]] for g in grids] == [["CD3"]]


def test_gating_can_leave_auto_contrast_out(tmp_path):
    info = make_gating_project(tmp_path)
    session = AgentSession()
    started = start(session, markers=["CD3", "CD8"], display_check="off")
    seen = drive(session, started["session_id"], Oracle(info))
    assert "display_setup" not in [p["kind"] for p in seen]
    status = ok(invoke(session, "gating_session_status",
                       {"session_id": started["session_id"]}))
    assert status["display"]["status"] == "off"


def test_a_panel_answer_is_learned_for_the_next_panel(tmp_path, monkeypatch):
    """What the model said about a marker the vocabulary does not know is
    kept: a project with another panel that has it is not asked again."""
    from tests import autogate_fixtures

    from plexora.ai import vocabulary

    monkeypatch.setitem(autogate_fixtures.MARKER_LEVELS, "Mystery1",
                        autogate_fixtures.MARKER_LEVELS["CD20"])
    vocabulary.clear_cache()
    info = make_gating_project(tmp_path, markers=("CD3", "CD8", "Mystery1"))
    session = AgentSession()
    started = start(session, markers=["CD3", "CD8", "Mystery1"])
    assert started["unresolved_markers"] == ["Mystery1"]
    packet, _ = next_packet(session, started["session_id"])
    assert packet["kind"] == "panel_context"
    outcome = answer(session, started["session_id"], packet, {
        "kind": "panel_context", "entries": [{
            "marker": "Mystery1", "role": "lineage_other", "compartment": "membrane",
            "lineage": "b_cell", "binary": True,
            "partners": [{"marker": "CD3", "relation": "exclusive", "confidence": "moderate"}]}]})
    assert outcome["outcome"]["learned"] == ["Mystery1"]
    assert vocabulary.origin("Mystery1") == "learned"
    ok(invoke(session, "gating_session_finish",
              {"session_id": started["session_id"], "action": "close"}))
    make_gating_project(tmp_path, name="gother", markers=("CD3", "Mystery1"))
    other = ok(invoke(session, "gating_session_start", {"scope": "project",
                                                        "project": "gother"}))
    jobs.drain(120)
    assert not other.get("unresolved_markers")
    del info


def test_a_marker_real_only_within_its_partner_gets_a_conditional_gate(tmp_path):
    from plexora.plugins.gating.server import model

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD8"])["session_id"]
    ds = session.data("gsynth")
    # CD8 is a clean marker: send it to a look by answering its strip, if any.
    agent = Oracle(info)
    packet = None
    for _ in range(20):
        packet, result = next_packet(session, sid)
        assert packet is not None, result
        if packet["kind"] == "t2_confirm" and packet["units"][0]["marker"] == "CD8":
            break
        if packet["kind"] == "t1_strip":
            verdicts = {r["marker"]: ("suspicious" if r["marker"] == "CD8" else "ok")
                        for r in packet["evidence"]["markers"]}
            answer(session, sid, packet, {"kind": "t1_strip", "verdicts": verdicts})
            continue
        answer(session, sid, packet, agent.answer(packet))
    assert packet["kind"] == "t2_confirm", packet["kind"]
    allowed = packet["evidence"]["within_allowed"]
    assert allowed == ["CD3"], packet["evidence"]["partners"]
    refused = invoke(session, "gating_answer", {
        "session_id": sid, "packet_id": packet["packet_id"], "include_next": False,
        "answer": look("within_partner", within="CD20")})
    assert not refused["ok"] and refused["error"]["code"] == "invalid_input"
    outcome = answer(session, sid, packet, look("within_partner", within="CD3"))["outcome"]
    assert outcome["condition"] == "CD3" and outcome["state"] == "awaiting_t2"
    again, _ = next_packet(session, sid)
    assert again["kind"] == "t2_confirm"
    condition = again["evidence"]["condition"]
    assert condition["within"] == "CD3" and condition["method"] in ("gmm_within",
                                                                     "control_p99")
    assert again["evidence"]["sheet"]["overview"]["within"] == "CD3"
    assert again["evidence"]["sheet"]["plot"]["partner"] == "CD3"
    answer(session, sid, again, look("about_right"))
    for _ in range(5):
        tail, _ = next_packet(session, sid)
        if tail is None:
            break
        answer(session, sid, tail, agent.answer(tail))
    unit = units(session, sid)["CD8"]
    assert unit["state"] in ("accepted", "accepted_low_confidence"), unit
    assert unit["confidence"] in ("moderate", "low")
    gates = ok(invoke(session, "get_all_gates", {"project": "gsynth"}))
    row = gates["provenance"]["CD8"]
    assert row["method"] == "ai_conditional"
    from plexora.plugins.gating.server.autogate import provenance

    detail = provenance.read("gsynth")["CD8"]["detail"]
    assert detail["condition"]["within"] == "CD3"
    assert model.get_gate(ds, "CD8")["low"] == pytest.approx(unit["final"])
    rolled = ok(invoke(session, "gating_session_finish", {"session_id": sid,
                                                          "action": "rollback"}))
    assert not rolled["refused"], rolled["refused"]
    assert "CD8" not in ok(invoke(session, "get_all_gates", {"project": "gsynth"}))[
        "thresholded"]


def test_a_t4_sheet_shows_each_candidate_in_the_tissue(tmp_path):
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"])["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    answer(session, sid, packet, look("too_low"))
    t4, result = next_packet(session, sid)
    assert t4 is not None and t4["kind"] == "t4_candidates", result
    ids = {c["id"] for c in t4["evidence"]["candidates"]}
    fields = t4["evidence"]["fields"]
    assert fields and {f["candidate"] for f in fields} <= ids
    assert "sheet_candidates" in t4["evidence"]["guide"]


# -- determinism -----------------------------------------------------------------------


def _to_t4(session, sid, info, marker="CD4", direction="too_low"):
    packet = first_packet_for(session, sid, marker, info)
    answer(session, sid, packet, look(direction))
    packet, result = next_packet(session, sid)
    assert packet is not None and packet["kind"] == "t4_candidates", result
    return packet


def test_the_rows_place_the_gate_on_a_lattice_point(tmp_path):
    from plexora.plugins.gating.server.autogate import engine

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"])["session_id"]
    packet = _to_t4(session, sid, info)
    ev = packet["evidence"]
    rows = [r["row"] for r in ev["intervals"]]
    assert rows[0] == "i1" and [c["id"] for c in ev["candidates"]] == \
        [f"c{i + 1}" for i in range(len(rows))]
    lows = [c["low"] for c in ev["candidates"]]
    assert lows == sorted(lows)                     # nearest the current gate first, going up
    verdicts = {r: "unstained" for r in rows}
    if len(rows) > 1:
        verdicts[rows[1]] = "own_stain"            # stop after the first row
    missing = invoke(session, "gating_answer", {
        "session_id": sid, "packet_id": packet["packet_id"], "include_next": False,
        "answer": {"kind": "t4_candidates", "intervals": {rows[0]: "unstained"} if
                   len(rows) > 1 else {}, "confidence": "sure"}})
    assert not missing["ok"] and missing["error"]["code"] == "invalid_input"
    # The agent never names a threshold: only the rows' words are taken.
    assert "chosen_candidate" not in packet["allowed"]
    outcome = answer(session, sid, packet, {"kind": "t4_candidates", "intervals": verdicts,
                                            "confidence": "sure"})["outcome"]
    unit = engine.store().load(sid)["units"][engine.unit_key("gsynth", "CD4")]
    points = {p["low"] for p in unit["lattice"]["points"]}
    assert outcome["chosen"] == "c1" and unit["candidate"] in points


def test_the_lattice_is_the_same_whatever_the_path(tmp_path):
    from plexora.plugins.gating.server.autogate import engine

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    first = start(session, markers=["CD4"], agent="a")["session_id"]
    _to_t4(session, first, info, direction="too_low")
    second = start(session, markers=["CD4"], agent="b")["session_id"]
    _to_t4(session, second, info, direction="too_high")
    lats = [engine.store().load(s)["units"][engine.unit_key("gsynth", "CD4")]["lattice"]
            for s in (first, second)]
    assert lats[0]["fingerprint"] == lats[1]["fingerprint"]
    assert [p["low"] for p in lats[0]["points"]] == [p["low"] for p in lats[1]["points"]]


def test_a_rerun_replays_the_same_answers_to_the_same_gates(tmp_path):
    info = make_gating_project(tmp_path, grid=24, size=1024)
    session = AgentSession()
    first = start(session, mode="propose", display_check="rule")["session_id"]
    asked = drive(session, first, Oracle(info))
    assert asked
    gates = {m: u.get("proposed", u.get("final")) for m, u in units(session, first).items()}
    second = start(session, mode="propose", display_check="rule")["session_id"]
    again = drive(session, second, Oracle(info))
    assert again == []                              # nothing was asked a second time
    status = ok(invoke(session, "gating_session_status", {"session_id": second}))
    assert status["replayed"] == len(asked)
    assert {m: u.get("proposed", u.get("final"))
            for m, u in units(session, second).items()} == gates
    # Another agent's judgment is its own: asked afresh.
    third = start(session, mode="propose", agent="another-model")["session_id"]
    assert drive(session, third, Oracle(info))
    fourth = start(session, mode="propose", reuse_answers=False)["session_id"]
    assert drive(session, fourth, Oracle(info))


def test_a_decisive_answer_with_a_request_goes_on_to_the_candidates(tmp_path):
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD20", "CD4"])["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    outcome = answer(session, sid, packet, look(
        "too_low", request={"kind": "bivariate", "marker": "CD3", "reason": "plot it"}))
    assert outcome["outcome"]["state"] == "awaiting_t4"
    packet, _ = next_packet(session, sid)
    assert packet["kind"] == "t4_candidates"
    assert packet["evidence"]["sheet"]["plot"]["partner"] == "CD3"


def test_a_changed_partner_gate_flags_its_dependants(tmp_path):
    from plexora.plugins.gating.server import model

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD20", "CD4"])["session_id"]
    drive(session, sid, Oracle(info))
    qc = ok(invoke(session, "gating_qc", {"project": "gsynth"}))
    assert qc["stale_dependencies"] == []
    ds = session.data("gsynth")
    gate = model.get_gate(ds, "CD3")
    model.set_gate(ds, "CD3", gate["low"] * 1.05, gate["high"])
    qc = ok(invoke(session, "gating_qc", {"project": "gsynth"}))
    stale = {(s["marker"], s["partner"]) for s in qc["stale_dependencies"]}
    assert ("CD4", "CD3") in stale and "CD4" in qc["needs_review"]


def test_a_stored_session_replays_and_its_pictures_draw_again_byte_for_byte(tmp_path):
    """The bench's replay (plexora/ai/bench.py): every packet a session issued,
    asked again from disk. Its pictures are a function of their manifests and
    the code, so drawing each collage again gives the stored bytes -- the
    guard every change to the renderer or the calibration is checked against,
    since an identical packet is what lets a rerun replay a remembered answer
    instead of asking a model."""
    from plexora.ai import bench

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    agent = Oracle(info)
    sid = start(session, mode="propose")["session_id"]
    asked = drive(session, sid, agent)
    assert {"t1_strip", "t2_confirm"} <= {p["kind"] for p in asked}
    rows = bench.replay_session(sid, agent, rerender=True, session=session)
    assert [r["packet_id"] for r in rows] == [p["packet_id"] for p in asked]
    compared = [r["agreement"] for r in rows if r["agreement"] is not None]
    assert compared and all(a == 1.0 for a in compared)
    drawn = [i for r in rows for i in r["images"] if i["rerendered"]]
    assert drawn and all(i["identical"] for i in drawn), drawn
