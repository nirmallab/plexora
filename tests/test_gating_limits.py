"""Limits, narration, resets and the viewer's channels after a gating session.

Getting the gate right comes before spending little: a marker that reaches
its allowance while the evidence still says to go on is asked about (or
extended, or flagged), never accepted because it ran out; a look at candidate
thresholds that runs out of rows goes on from where it stopped. What the panel
says is written for the user, not the prompt the agent was given.
"""

import json

import pytest

from plexora.agent import AgentSession, invoke, registry
from tests.autogate_fixtures import make_gating_project
from tests.test_gating_session import (HARD, Oracle, answer, first_packet_for, look,
                                       next_packet, ok, start, units)


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def _unit(sid, marker="CD4"):
    from plexora.plugins.gating.server.autogate import engine

    return engine.store().load(sid)["units"][engine.unit_key("gsynth", marker)]


def _all_rows(packet, verdict):
    return {r["row"]: verdict for r in packet["evidence"]["intervals"]}


# -- limits ---------------------------------------------------------------------------


def test_stopping_at_the_budget_flags_the_marker_and_writes_nothing(tmp_path):
    """The first live run's CD16: the looks spent the budget and the last said
    "too low". Stopped there, the marker is for a person, the gate proposed."""
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"], budget={"packets": 1, "images": 2},
                on_limit="stop")["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    answer(session, sid, packet, look("too_low"))
    following, result = next_packet(session, sid)
    assert following is None and result["state"] == "decided", result
    unit = units(session, sid)["CD4"]
    assert unit["state"] == "manual_review_recommended", unit
    assert "before a confident conclusion" in unit["reason"]
    assert unit["proposed"] == pytest.approx(unit["gmm"]) and "final" not in unit
    gates = ok(invoke(session, "get_all_gates", {"project": "gsynth"}))
    assert "CD4" not in gates["thresholded"]


def test_extend_keeps_going_without_asking(tmp_path):
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"], budget={"packets": 1, "images": 2},
                on_limit="extend", max_extensions=1)["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    answer(session, sid, packet, look("too_low"))
    t4, result = next_packet(session, sid)
    assert t4 is not None and t4["kind"] == "t4_candidates", result
    assert _unit(sid)["extensions"] == 1
    # The one extension is spent too: the next want is flagged, not granted.
    answer(session, sid, t4, {"kind": "t4_candidates", "confidence": "sure",
                              "intervals": _all_rows(t4, "mostly_negative")})
    following, result = next_packet(session, sid)
    unit = _unit(sid)
    if following is None:
        assert unit["state"] in ("manual_review_recommended",) + tuple(
            ("accepted", "accepted_low_confidence")), unit["state"]
        if unit["state"] == "manual_review_recommended":
            assert "most extensions" in unit["reason"]


def test_asking_holds_the_marker_until_the_user_answers(tmp_path):
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"], budget={"packets": 1, "images": 2},
                on_limit="ask")["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    answer(session, sid, packet, look("too_low"))
    following, result = next_packet(session, sid)
    assert following is None and result["state"] == "waiting_for_user", result
    [request] = result["requests"]
    assert request["marker"] == "CD4" and request["why"] == "budget"
    status = ok(invoke(session, "gating_session_status", {"session_id": sid}))
    assert [r["marker"] for r in status["limit_requests"]] == ["CD4"]
    assert status["progress"]["waiting_for_user"] == ["CD4"]
    # Asked again, it still waits; nothing was written meanwhile.
    again, result = next_packet(session, sid)
    assert again is None and result["state"] == "waiting_for_user"
    bad = invoke(session, "gating_session_status", {"session_id": sid,
                                                    "limits": {"NOPE": "continue"}})
    assert not bad["ok"] and bad["error"]["code"] == "invalid_input"
    ok(invoke(session, "gating_session_status", {"session_id": sid,
                                                 "limits": {"CD4": "continue"}}))
    t4, result = next_packet(session, sid)
    assert t4 is not None and t4["kind"] == "t4_candidates", result
    assert _unit(sid)["limit_log"][-1] == {"why": "budget", "decision": "continue",
                                           "by": "user", "extension": 1}


def test_a_no_from_the_user_flags_the_marker(tmp_path):
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"], budget={"packets": 1, "images": 2},
                on_limit="ask")["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    answer(session, sid, packet, look("too_low"))
    next_packet(session, sid)
    ok(invoke(session, "gating_session_status", {"session_id": sid,
                                                 "limits": {"CD4": "stop"}}))
    following, result = next_packet(session, sid)
    assert following is None and result["state"] == "decided", result
    unit = units(session, sid)["CD4"]
    assert unit["state"] == "manual_review_recommended" and "user chose to stop" in unit["reason"]


def test_the_viewer_answers_a_limit_question_through_its_route(tmp_path):
    from plexora.plugins.gating.server.autogate import engine

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"], budget={"packets": 1, "images": 2},
                on_limit="ask")["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    answer(session, sid, packet, look("too_low"))
    next_packet(session, sid)
    from flask import Flask

    from plexora.plugins.gating.server import routes

    app = Flask(__name__)
    app.register_blueprint(routes.gating_bp, url_prefix="/plugins/gating")
    client = app.test_client()
    refused = client.post(f"/plugins/gating/agent_session/{sid}/control",
                          data=json.dumps({"action": "limit", "marker": "CD4",
                                           "decision": "maybe"}))
    assert refused.status_code == 400
    reply = client.post(f"/plugins/gating/agent_session/{sid}/control",
                        data=json.dumps({"action": "limit", "marker": "CD4",
                                         "decision": "continue"}))
    assert reply.status_code == 200, reply.data
    control = engine.store().control(sid)
    assert control["limit_answers"] == {engine.unit_key("gsynth", "CD4"): "continue"}
    t4, _ = next_packet(session, sid)
    assert t4 is not None and t4["kind"] == "t4_candidates"


def test_limit_defaults_come_from_the_command_line_environment(monkeypatch):
    from plexora import cli
    from plexora.plugins.gating.capabilities_session import option_defaults
    from plexora.plugins.gating.server.autogate import schemas

    assert cli.GATING_LIMIT_POLICIES == schemas.LIMIT_POLICIES
    assert cli.GATING_LIMIT_ENV == schemas.LIMIT_ENV
    assert option_defaults()["on_limit"] == schemas.LIMIT_DEFAULTS["on_limit"]
    monkeypatch.setenv("PLEXORA_GATING_ON_LIMIT", "extend")
    monkeypatch.setenv("PLEXORA_GATING_MAX_EXTENSIONS", "5")
    defaults = option_defaults()
    assert defaults["on_limit"] == "extend" and defaults["max_extensions"] == 5
    monkeypatch.setenv("PLEXORA_GATING_ON_LIMIT", "sometimes")
    assert option_defaults()["on_limit"] == schemas.LIMIT_DEFAULTS["on_limit"]
    parser = cli.build_ai_parser() if hasattr(cli, "build_ai_parser") else None
    if parser is not None:
        args = parser.parse_args(["bench", "gating", "--on-limit", "stop"])
        assert args.gating_on_limit == "stop"


# -- candidates that run out of rows ---------------------------------------------------


class _T4Engine:
    """Just what `transitions.apply_t4` asks of an engine."""

    def __init__(self, unit, lattice):
        self.unit, self.lat = unit, lattice
        self.record = {"units": {"p::M": unit}}
        self.settled = []

    def lattice_for(self, unit):
        return self.lat

    def settle(self, unit):
        self.settled.append(unit["candidate"])


def _point(pid, low):
    return {"id": pid, "low": low, "sources": [pid], "n_positive": 0, "fraction": 0.0}


def _apply_t4(verdicts, points):
    from plexora.plugins.gating.server.autogate import transitions
    from plexora.plugins.gating.server.autogate.answers import T4Answer

    lattice = {"points": points}
    chain = [p for p in points if p["low"] > 1.0][:1]
    unit = {"project": "p", "marker": "M", "candidate": 1.0, "direction": "up",
            "state": "awaiting_t4", "candidates": {"c1": chain[0]["low"]},
            "candidate_steps": {"c1": chain[0]["id"]}}
    engine = _T4Engine(unit, lattice)
    outcome = transitions.apply_t4(
        engine, {"units": [{"project": "p", "marker": "M"}]},
        T4Answer(kind="t4_candidates", intervals=verdicts, confidence="sure"))
    return unit, engine, outcome


def test_a_look_that_runs_out_of_rows_goes_on_from_where_it_stopped():
    """FOXP3 in the live run: a partner's point sat just above the gate, every
    row said move, and the gate was written there. Now the next look starts
    at that point."""
    points = [_point("gmm", 1.0), _point("within:CD45", 1.05), _point("ctrl:CD45", 1.6),
              _point("up:1sd", 2.0)]
    unit, engine, outcome = _apply_t4({"i1": "mostly_negative"}, points)
    assert outcome["continues"] is True and unit["state"] == "awaiting_t4"
    assert unit["candidate"] == 1.05 and unit["direction"] == "up"
    assert unit["t4_continued"] == ["within:CD45"] and not engine.settled


def test_a_mixed_row_after_a_continued_round_keeps_the_gate_reached():
    from plexora.plugins.gating.server.autogate import transitions
    from plexora.plugins.gating.server.autogate.answers import T4Answer

    points = [_point("within:CD45", 1.0), _point("ctrl:CD45", 1.3), _point("up:1sd", 2.0)]
    unit = {"project": "p", "marker": "M", "candidate": 1.0, "direction": "up",
            "state": "awaiting_t4", "candidates": {"c1": 1.3},
            "candidate_steps": {"c1": "ctrl:CD45"}, "t4_continued": ["within:CD45"]}
    engine = _T4Engine(unit, {"points": points})
    outcome = transitions.apply_t4(
        engine, {"units": [{"project": "p", "marker": "M"}]},
        T4Answer(kind="t4_candidates", intervals={"i1": "mixed"}, confidence="sure"))
    assert outcome["chosen"] == "keep" and engine.settled == [1.0]
    assert unit["state"] == "awaiting_regression"


def test_a_look_that_reaches_the_end_of_the_lattice_settles():
    points = [_point("gmm", 1.0), _point("edge:high", 1.4)]
    unit, engine, outcome = _apply_t4({"i1": "mostly_negative"}, points)
    assert not outcome.get("continues") and engine.settled == [1.4]


def test_a_look_stopped_by_a_row_does_not_go_on(tmp_path):
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"])["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    answer(session, sid, packet, look("too_low"))
    t4, _ = next_packet(session, sid)
    rows = _all_rows(t4, "mostly_negative")
    rows["i1"] = "mostly_positive"
    outcome = answer(session, sid, t4, {"kind": "t4_candidates", "confidence": "sure",
                                        "intervals": rows})["outcome"]
    assert not outcome.get("continues") and outcome["chosen"] == "keep"


# -- no uncertain acceptances ----------------------------------------------------------


def test_an_unsure_look_with_no_reference_is_reviewed_not_accepted(tmp_path):
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"])["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    answer(session, sid, packet, look("about_right", confidence="unsure"))
    unit = units(session, sid)["CD4"]
    assert unit["state"] == "manual_review_recommended", unit
    assert unit["proposed"] is not None and "final" not in unit


def test_a_whole_image_check_that_cannot_tell_is_reviewed(tmp_path):
    from plexora.plugins.gating.server.autogate import transitions
    from plexora.plugins.gating.server.autogate.answers import ConfirmAnswer

    closed = {}

    class Engine:
        def close(self, unit, state, reason, **kw):
            closed.update(state=state, reason=reason, **kw)

    unit = {"project": "p", "marker": "M", "candidate": 1.5, "state": "regression_confirm"}
    engine = Engine()
    engine.record = {"units": {"p::M": unit}}
    transitions.apply_regression(engine, {"units": [{"project": "p", "marker": "M"}]},
                                 ConfirmAnswer(kind="regression_confirm",
                                                  verdict="cannot_tell"))
    assert closed["state"] == "manual_review_recommended" and closed["proposed"] == 1.5


# -- what the user reads ---------------------------------------------------------------


def test_the_panel_is_told_what_the_agent_is_doing_not_its_question(tmp_path):
    from plexora.plugins.gating.server.autogate import mirror_script, packets, schemas

    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    sid = start(session, markers=["CD4"])["session_id"]
    packet = first_packet_for(session, sid, "CD4", info)
    narration = packet["narration"]
    assert "CD4" in narration and narration != packet["question"]
    assert narration in {t.format(marker="CD4", partner=p, references=p, n=1)
                         for t in schemas.NARRATION.values()
                         for p in ["", *[x["partner"] for x in
                                         packet["evidence"].get("partners") or []]]}
    script = mirror_script.script_for(packet, None, None, current_project="gsynth")
    captions = [c["arguments"]["caption"] for c in script if c["type"] == "show_evidence"]
    assert all(packet["question"][:40] not in c for c in captions)
    assert packets.evidence_label(packet) in captions or not captions


def test_every_narration_names_only_known_fields():
    from plexora.plugins.gating.server.autogate import answers, schemas

    for key, template in {**schemas.NARRATION, **schemas.EVIDENCE_LABELS}.items():
        template.format(marker="M", partner="P", references="R", n=2)
    kinds = set(answers.KINDS) if hasattr(answers, "KINDS") else set()
    for key in schemas.NARRATION:
        if kinds:
            assert key.split(":", 1)[0] in kinds, key


def test_an_exclusive_partner_is_narrated_as_ruling_cells_out():
    from plexora.plugins.gating.server.autogate import packets

    packet = {"kind": "t2_confirm", "units": [{"project": "p", "marker": "ECAD"}],
              "evidence": {"sheet": {"plot": {"partner": "CD45", "relation": "exclusive"}}}}
    assert "rule out" in packets.narrate(packet)
    packet["evidence"]["sheet"]["plot"]["relation"] = "subset"
    assert "which cells should carry it" in packets.narrate(packet)


def test_the_summary_counts_reused_answers():
    from plexora.plugins.gating.server.autogate import engine

    record = {"units": {"p::A": {"state": "accepted", "extensions": 1}},
              "replayed": ["pk_0001", "pk_0002"]}
    summary = engine.summary_of(record)
    assert summary["replayed"] == 2 and summary["extended"] == 1


# -- starting over ---------------------------------------------------------------------


def test_reset_gates_clears_them_tells_the_viewer_and_undoes(tmp_path):
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    ok(invoke(session, "set_gate", {"project": "gsynth", "marker": "CD4", "low": 150.0}))
    before = ok(invoke(session, "get_all_gates", {"project": "gsynth"}))
    assert "CD4" in before["thresholded"]
    reset = ok(invoke(session, "reset_gates", {"project": "gsynth"}))
    assert "CD4" in reset["reset"] and reset["receipt"]["changed"]
    assert reset["receipt"]["undo_hint"]["tool"] == "restore_gates"
    after = ok(invoke(session, "get_all_gates", {"project": "gsynth"}))
    assert "CD4" not in after["thresholded"] and "CD4" not in (after.get("provenance") or {})
    ok(invoke(session, "undo_operation", {"operation_id": reset["receipt"]["operation_id"]}))
    back = ok(invoke(session, "get_all_gates", {"project": "gsynth"}))
    assert "CD4" in back["thresholded"]
    assert back["provenance"]["CD4"]["method"] == before["provenance"]["CD4"]["method"]


def test_reset_gates_leaves_locked_gates_alone(tmp_path):
    from plexora.plugins.gating.server.autogate import provenance

    make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    ok(invoke(session, "set_gate", {"project": "gsynth", "marker": "CD4", "low": 150.0}))
    provenance.set_status("gsynth", "CD4", "locked", principal="user")
    reset = ok(invoke(session, "reset_gates", {"project": "gsynth"}))
    assert reset["skipped"] == {"CD4": "locked"} and "CD4" not in reset["reset"]
    assert "CD4" in ok(invoke(session, "get_all_gates", {"project": "gsynth"}))["thresholded"]


def test_each_candidate_gets_its_own_field(tmp_path):
    """The live run showed the same borderline field for every candidate."""
    from plexora.agent import gate_sampling

    make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    ds = AgentSession().data("gsynth")
    first = gate_sampling.sample_gate_validation_regions(
        ds, "CD4", 150.0, 1e9, field_px=300, image_size=(1280, 1280), n_per_class=1,
        classes=("borderline",))["fields"][0]
    b = first["bounds"]
    box = (b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"])
    second = gate_sampling.sample_gate_validation_regions(
        ds, "CD4", 150.0, 1e9, field_px=300, image_size=(1280, 1280), n_per_class=1,
        classes=("borderline",), avoid=[box])["fields"]
    assert second and second[0]["bounds"] != first["bounds"]
    assert gate_sampling._iou(box, (second[0]["bounds"]["x"], second[0]["bounds"]["y"],
                                    second[0]["bounds"]["x"] + second[0]["bounds"]["width"],
                                    second[0]["bounds"]["y"] + second[0]["bounds"]["height"])
                              ) <= gate_sampling.MAX_IOU
