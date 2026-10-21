"""The candidates engine (`SessionOptions.engine = "candidates"`).

- the evidence: per-interval own-stain shares made monotone, candidates
  scored by the cells they would get wrong, a leader decisive only when the
  pooled cells between it and a rival say so;
- the controller: a survey first, then looks between the leader and an
  unbeaten rival, never a cell twice, and review with a reason when the
  looks run out, the sentinels fail or the cells cannot be read;
- the panel: cells from the planned intervals, sentinels below every
  candidate, shuffled and numbered, and nothing in the packet that says
  where any gate is;
- a session: every marker that needs a look is decided by panels, written,
  and with a truthful reader lands on the truth's side of the candidates.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from plexora.plugins.gating.server.autogate.hypotheses import controller, evidence
from tests.autogate_fixtures import FakeData, make_gating_project

IDENTITY = float


def _obs(values, labels, valid=True):
    return {"valid": valid, "cells": [{"value": v, "label": lab}
                                      for v, lab in zip(values, labels)]}


# -- evidence ------------------------------------------------------------------------------


def test_the_wilson_interval_narrows_with_cells_and_stays_in_bounds():
    lo, hi = evidence.wilson(6, 10)
    assert 0.0 <= lo < 0.6 < hi <= 1.0
    lo2, hi2 = evidence.wilson(60, 100)
    assert hi2 - lo2 < hi - lo
    assert evidence.wilson(0, 0) == (0.0, 1.0)


def test_shares_are_made_non_decreasing_by_value():
    fitted = evidence.monotone([0.2, 0.6, 0.4, 0.9], [5, 5, 5, 5])
    assert fitted == sorted(fitted)
    assert fitted[1] == fitted[2] == pytest.approx(0.5)
    assert evidence.monotone([0.3, 0.0, 0.8], [4, 0, 4])[1] is None


def test_the_leader_is_the_candidate_with_fewest_cells_wrong():
    bounds = [1.0, 2.0, 3.0, 4.0]          # intervals (1,2], (2,3], (3,4]
    n_cells = [100, 100, 100]
    values = [1.5] * 10 + [2.5] * 10 + [3.5] * 10
    labels = (["not_own_stain"] * 10 + ["not_own_stain"] * 9 + ["own_stain"]
              + ["own_stain"] * 10)
    s = evidence.support(bounds, n_cells, [_obs(values, labels)], prior=0,
                         to_fit=IDENTITY, tolerance=0.2)
    assert s["low"] == 3.0
    beaten = {r["low"]: r["decisive"] for r in s["rivals"]}
    assert beaten[1.0] and beaten[2.0] and beaten[4.0]
    assert s["resolved"]


def test_a_rival_is_not_beaten_on_a_handful_of_cells():
    bounds = [1.0, 2.0, 3.0]
    s = evidence.support(bounds, [100, 100], [_obs([1.5, 1.5, 2.5, 2.5],
                                                   ["not_own_stain", "own_stain",
                                                    "own_stain", "own_stain"])],
                         prior=1, to_fit=IDENTITY, tolerance=0.2)
    assert not s["resolved"] and s["undecided"]


def test_a_rival_within_the_tolerance_need_not_be_beaten():
    bounds = [1.0, 1.1, 3.0]
    values = [1.05] * 3 + [2.0] * 20
    labels = ["own_stain", "not_own_stain", "own_stain"] + ["own_stain"] * 20
    s = evidence.support(bounds, [50, 500], [_obs(values, labels)], prior=0,
                         to_fit=IDENTITY, tolerance=0.2)
    assert s["low"] in (1.0, 1.1)
    assert all(r["low"] == 3.0 for r in s["rivals"]) and s["resolved"]


def test_unread_intervals_favour_no_candidate_and_ties_go_to_the_start():
    bounds = [1.0, 2.0, 3.0]
    s = evidence.support(bounds, [100, 100], [_obs([], [])], prior=2,
                         to_fit=IDENTITY, tolerance=0.2)
    assert s["low"] == 3.0 and s["labelled"] == 0


def test_a_close_rival_may_stay_undecided_but_a_far_one_must_be_beaten():
    bounds = [1.0, 1.25, 2.0]
    values = [1.1] * 4 + [1.5] * 12
    labels = ["own_stain", "not_own_stain", "own_stain", "not_own_stain"] + ["own_stain"] * 12
    s = evidence.support(bounds, [100, 300], [_obs(values, labels)], prior=0,
                         to_fit=IDENTITY, tolerance=0.2, resolve_within=0.3)
    assert s["low"] == 1.0
    near = [r for r in s["rivals"] if r["low"] == 1.25][0]
    assert not near["decisive"] and s["resolved"] and not s["blocking"]


def test_candidates_a_few_hundredths_apart_are_merged_keeping_the_estimate():
    from plexora.plugins.gating.server.autogate.hypotheses import candidates

    points = [{"low": 1.0, "sources": ["edge:low"]}, {"low": 1.03, "sources": ["kde"]},
              {"low": 1.05, "sources": ["up:0.5sd"]}, {"low": 1.4, "sources": ["gmm"]}]
    kept = candidates.thin(points, IDENTITY, 0.1)
    assert [p["low"] for p in kept] == [1.03, 1.4]


# -- controller ----------------------------------------------------------------------------


def _state(lows=(1.0, 2.0, 3.0, 4.0, 5.0), n_positive=(1000, 800, 600, 400, 200),
           window=(1, 3), prior=2):
    return {"points": [{"low": lo, "n_positive": n, "id": f"p{i}", "sources": ["gmm"]}
                       for i, (lo, n) in enumerate(zip(lows, n_positive))],
            "window": list(window), "prior": prior, "observations": []}


PARAMS = {"panel_cells": 20, "sentinels": 4, "sentinel_misses": 2, "looks_max": 3,
          "invalid_looks": 2, "tolerance": 0.2, "resolve_within": 0.2, "min_gap": 0.0,
          "min_labelled": 10, "max_other_share": 0.5}


def test_the_first_look_surveys_every_disputed_interval():
    state = _state()
    plan = controller.next_action(state, IDENTITY, PARAMS)
    assert plan["kind"] == "survey"
    assert [iv["interval"] for iv in plan["intervals"]] == [0, 1]
    assert sum(iv["n"] for iv in plan["intervals"]) == PARAMS["panel_cells"]


def test_the_survey_gives_every_interval_an_equal_share():
    # Neither the crowded background nor a long bright tail takes the look.
    state = _state(lows=(1.0, 1.1, 2.1, 9.0), n_positive=(10_000, 500, 100, 10),
                   window=(0, 3), prior=1)
    plan = controller.next_action(state, IDENTITY, PARAMS)
    n = [iv["n"] for iv in plan["intervals"]]
    assert len(n) == 3 and max(n) - min(n) <= 1


def test_a_leader_at_the_edge_of_the_range_is_not_resolved_while_points_lie_beyond():
    state = _state(window=(1, 3))
    state["observations"].append(_obs([2.5] * 10 + [3.5] * 10, ["not_own_stain"] * 20))
    status, s = controller.decide(state, IDENTITY, PARAMS)
    assert s["resolved"] and status is None
    assert controller.next_action(state, IDENTITY, PARAMS) == {"kind": "extend", "side": "up"}


def test_a_rival_that_changes_almost_no_cell_never_blocks():
    bounds = [1.0, 1.5, 2.0]
    s = evidence.support(bounds, [5, 300], [_obs([1.8] * 12, ["own_stain"] * 12)], prior=0,
                         to_fit=IDENTITY, tolerance=0.2, resolve_within=0.3, min_between=20)
    rival = [r for r in s["rivals"] if r["low"] == 1.5][0]
    assert rival["n_between"] == 5 and not rival["decisive"] and s["resolved"]


def test_allocation_never_asks_for_more_cells_than_an_interval_has():
    picks = controller.allocate([1000, 4, 0], [1000, 4, 0], 20)
    assert picks[1] <= 4 and picks[2] == 0 and sum(picks) == 20


def test_a_leader_at_the_edge_of_the_range_widens_it_by_a_lattice_point():
    state = _state()
    state["observations"].append(_obs([2.5] * 10 + [3.5] * 10, ["own_stain"] * 20))
    plan = controller.next_action(state, IDENTITY, PARAMS)
    assert plan == {"kind": "extend", "side": "down"}
    controller.extend(state, "down")
    assert state["window"] == [0, 3]


def test_the_next_look_is_between_the_leader_and_an_unbeaten_rival():
    state = _state(window=(0, 4), prior=2)
    values = [1.5, 1.5, 2.5, 2.5, 3.5, 3.5, 4.5, 4.5]
    labels = ["not_own_stain", "not_own_stain", "not_own_stain", "own_stain",
              "own_stain", "own_stain", "own_stain", "own_stain"]
    state["observations"].append(_obs(values, labels))
    plan = controller.next_action(state, IDENTITY, PARAMS)
    assert plan["kind"] == "refine"
    leader = controller.support(state, IDENTITY, PARAMS)["low"]
    for iv in plan["intervals"]:
        assert min(leader, plan["rival"]) <= iv["from"] < iv["to"] <= max(leader, plan["rival"])


def test_no_cell_is_shown_twice_and_a_spent_dispute_ends_the_looks():
    state = _state(lows=(1.0, 2.0, 3.0), n_positive=(30, 20, 10), window=(0, 2), prior=1)
    cells = [{"value": 1.5, "label": "own_stain"}] * 10 + \
        [{"value": 2.5, "label": "own_stain"}] * 10
    state["observations"].append({"valid": True, "cells": cells})
    # The leader is the lowest candidate; every cell of the range was shown,
    # so no novel look can test the rivals again.
    assert controller.shown(state) == [10, 10]
    status, _s = controller.decide(state, IDENTITY, PARAMS)
    if status is None:
        assert controller.next_action(state, IDENTITY, PARAMS) is None


def test_running_out_of_looks_is_review_never_resolved():
    state = _state(window=(0, 4))
    for _ in range(3):
        state["observations"].append(_obs([2.5, 2.5, 3.5], ["own_stain", "not_own_stain",
                                                            "own_stain"]))
    status, s = controller.decide(state, IDENTITY, PARAMS)
    assert status == "budget_exhausted" and not s["resolved"]
    assert controller.decide(state, IDENTITY, PARAMS, continuous=True)[0] == \
        "no_identifiable_boundary"


def test_a_reader_that_calls_clear_negatives_stained_twice_is_not_trusted():
    state = _state()
    state["observations"] += [_obs([2.5], ["own_stain"], valid=False)] * 2
    assert controller.decide(state, IDENTITY, PARAMS)[0] == "readers_unreliable"


def test_cells_nobody_could_read_close_the_marker_as_unreadable():
    state = _state()
    state["observations"].append(_obs([2.5] * 12, ["uncertain"] * 9 + ["own_stain"] * 3))
    assert controller.decide(state, IDENTITY, PARAMS)[0] == "unreadable"


# -- the panel's cells -------------------------------------------------------------------


def test_a_panel_draws_unshown_cells_from_its_intervals_and_sentinels_below():
    from plexora.plugins.gating.server.autogate.hypotheses import panel

    rng = np.random.default_rng(0)
    values = np.expm1(np.concatenate([rng.normal(1.0, 0.2, 4000), rng.normal(3.0, 0.3, 1000)]))
    ds = FakeData({"M": values})
    lows = [float(np.expm1(1.6)), float(np.expm1(2.2)), float(np.expm1(2.8))]
    intervals = [{"from": lows[0], "to": lows[1], "n": 6}, {"from": lows[1], "to": lows[2],
                                                            "n": 6}]
    first = panel.panel_cells(ds, "M", intervals, n_sentinels=4, sentinel_below=lows[0],
                              seed=1)
    assert len(first["cells"]) == 12 and len(first["sentinels"]) == 4
    for cell in first["cells"]:
        iv = intervals[cell["interval"]]
        assert iv["from"] < cell["value"] <= iv["to"] + 1e-3
    below = sorted(v for v in values if v <= lows[0])
    assert all(s["value"] <= below[len(below) // 2] + 1e-3 for s in first["sentinels"])
    shown = [c["cell_id"] for c in first["cells"]]
    again = panel.panel_cells(ds, "M", intervals, exclude=shown, n_sentinels=0, seed=2)
    assert not set(shown) & {c["cell_id"] for c in again["cells"]}
    arranged = panel.arrange(first["cells"], first["sentinels"], seed=3)
    assert [c["caption"] for c in arranged] == [str(i + 1) for i in range(16)]
    assert [c["cell_id"] for c in arranged] != [c["cell_id"] for c in
                                               first["cells"] + first["sentinels"]]


# -- a session ------------------------------------------------------------------------------


HARD = ("CD3", "CD8", "CD20", "CD4", "FOXP3")


def _no_numbers_about_gates(packet):
    text = json.dumps({k: packet.get(k) for k in ("question", "evidence", "allowed")})
    for word in ("candidate", "current", "\"value\"", "gate\"", "\"low\"", "leader"):
        assert word not in text, word


@pytest.mark.paid
def test_the_candidates_engine_decides_markers_by_blind_panels(tmp_path):
    from plexora.plugins.gating.server.autogate import engine as engines
    from tests.test_gating_session import Oracle, ok

    registry.discover(["gating"])
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    started = ok(invoke(session, "gating_session_start", {
        "scope": "project", "project": "gsynth", "markers": list(HARD),
        "engine": "candidates"}))
    jobs.drain(120)
    sid = started["session_id"]
    oracle = Oracle(info)
    index = {c["id"]: i for i, c in enumerate(info["cells"])}
    panels = []
    result = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 20}))
    for _ in range(120):
        if result["state"] != "decision":
            break
        packet = result["packet"]
        if packet["kind"] == "cell_panel":
            panels.append(packet)
            _no_numbers_about_gates(packet)
            assert packet["allowed"]["cells"] == [str(i + 1) for i in
                                                  range(len(packet["allowed"]["cells"]))]
            stored, _ = engines.store().read_packet(sid, packet["packet_id"])
            marker = packet["units"][0]["marker"]
            truth = oracle.truth[marker]
            reply = {"kind": "cell_panel", "cells": {
                c["number"]: ("own_stain" if truth[index[c["cell_id"]]] else "not_own_stain")
                for c in stored["_panel"]["cells"]}}
        else:
            assert packet["kind"] not in ("t2_confirm", "t3_biological", "t4_candidates"), \
                packet["kind"]
            reply = oracle.answer(packet)
        answered = ok(invoke(session, "gating_answer", {
            "session_id": sid, "packet_id": packet["packet_id"], "answer": reply}))
        if packet["kind"] == "cell_panel" and answered["outcome"]["state"] == "awaiting_panel":
            # Still looking: the reader is told nothing about where a gate is.
            assert set(answered["outcome"]) == {"unit", "state", "look"}
        result = answered["next"]
    assert result["state"] in ("decided", "done"), result
    assert panels, "no marker was decided by a panel"
    status = ok(invoke(session, "gating_session_status", {"session_id": sid}))
    record = engines.store().load(sid)
    for unit in record["units"].values():
        assert unit["state"] in ("accepted", "accepted_low_confidence",
                                 "manual_review_recommended", "not_binary",
                                 "no_positive_population", "technically_failed",
                                 "insufficient_information"), unit["state"]
        assert unit.get("final") is not None or unit["state"] in (
            "no_positive_population", "technically_failed")
        hyp = unit.get("hypotheses")
        if not hyp or hyp.get("status") in (None, "no_candidates"):
            continue
        assert len(hyp["observations"]) <= hyp["params"]["looks_max"]
        if hyp["status"] == "resolved":
            # A truthful reader: the chosen candidate gets few more cells
            # wrong than the best any single threshold could.
            n = len(oracle.values[unit["marker"]])
            assert oracle.excess(unit["marker"], unit["final"]) <= 0.05 * n, unit["marker"]
    assert {u["marker"] for u in status["units"]} == set(HARD)


# -- calibration ------------------------------------------------------------------------------


def test_a_calibration_fits_and_predicts_within_zero_and_one():
    from plexora.plugins.gating.server.autogate.hypotheses import calibration

    rng = np.random.default_rng(0)
    rows, outcomes = [], []
    for _ in range(200):
        margin = float(rng.uniform(-0.3, 0.5))
        rows.append({"margin": margin, "spread": float(rng.uniform(0, 1)),
                     "labelled": float(rng.integers(10, 60)),
                     "other_share": float(rng.uniform(0, 0.4)), "looks": float(rng.integers(1, 4))})
        outcomes.append(int(rng.random() < 0.2 + 0.7 * (margin > 0.1)))
    table = calibration.fit(rows, outcomes)
    clear = calibration.predict(table, {**rows[0], "margin": 0.45})
    blocked = calibration.predict(table, {**rows[0], "margin": -0.25})
    assert 0.0 < blocked < clear < 1.0
    bins = calibration.reliability(table, rows, outcomes)
    assert sum(b["n"] for b in bins) == len(rows)
    assert calibration.level(0.85) == "high" and calibration.level(0.1) == "low"


def test_without_a_table_confidence_is_the_uncalibrated_rule(monkeypatch, tmp_path):
    from plexora.plugins.gating.server.autogate import hypotheses
    from plexora.plugins.gating.server.autogate.hypotheses import calibration

    monkeypatch.setattr(calibration, "TABLE_PATH", tmp_path / "none.json")
    unit = {"hypotheses": {"status": "resolved", "spread": 0.1, "params": dict(PARAMS),
                           "support": {"rivals": [{"ci": [0.7, 0.95], "low": 3.0,
                                                   "distance": 1.0}]}}}
    assert hypotheses.confidence(unit) == "high"
    assert hypotheses.provenance(unit)["confidence"] == "uncalibrated"


# -- the classifier as a two-way check -----------------------------------------------------


def _check_engine(ds, mode="check"):
    from types import SimpleNamespace

    return SimpleNamespace(options={"classifier": mode, "purpose": "gating", "seed": 0},
                           id="gs_test", call=SimpleNamespace(
                               session=SimpleNamespace(data=lambda _p: ds)),
                           log=lambda **_k: None)


def test_a_classifier_check_shows_both_sides_and_at_most_half_the_panel(monkeypatch):
    from plexora.plugins.gating.server.autogate.hypotheses import classifier_check
    from plexora.plugins.gating.server.classifier import features, runs

    rng = np.random.default_rng(1)
    values = np.expm1(rng.uniform(1.0, 3.0, 6000))
    ds = FakeData({"M": values})
    log_v = np.log1p(values)
    leader = float(np.expm1(2.0))
    # The classifier calls a third of the cells below the leader positive and
    # a third of those above it negative.
    raw = np.where(log_v > 2.0, 1, 0)
    flip = rng.random(values.size) < 0.33
    raw = np.where(flip, 1 - raw, raw)
    monkeypatch.setattr(features, "pixels_ready", lambda _ds, _m: (True, None))
    monkeypatch.setattr(runs, "session_run", lambda *_a, **_k: {
        "record": {"status": "ready", "snr": {"grade": "ok"}},
        "arrays": {"gate_calls": np.zeros(values.size, dtype=np.int8), "raw_calls": raw}})
    bounds = [float(np.expm1(x)) for x in (1.2, 1.6, 2.0, 2.4, 2.8)]
    n_pos = [int((values > b).sum()) for b in bounds]
    state = {"points": [{"low": b, "n_positive": n, "id": f"p{i}", "sources": ["gmm"]}
                        for i, (b, n) in enumerate(zip(bounds, n_pos))],
             "window": [0, 4], "leader": leader, "params": dict(PARAMS), "observations": []}
    unit = {"project": "fake", "marker": "M", "high": None}
    plan = classifier_check.plan(_check_engine(ds), unit, state, [])
    assert plan is not None and state["classifier"]["status"] == "look"
    chosen = np.isin(np.arange(1, values.size + 1), plan["ids"])
    assert (log_v[chosen] <= 2.0).any() and (log_v[chosen] > 2.0).any()
    assert len(plan["ids"]) <= PARAMS["panel_cells"] * classifier_check.FLAGGED_SHARE + 1
    assert sum(iv["n"] for iv in plan["intervals"]) + len(plan["ids"]) <= PARAMS["panel_cells"] + 1
    # Once per marker, and never without `check`.
    assert classifier_check.plan(_check_engine(ds), unit, state, []) is None
    assert classifier_check.plan(_check_engine(ds, "layer"), unit,
                                 {**state, "classifier": None}, []) is None


def test_a_classifier_with_a_low_signal_grade_is_not_consulted(monkeypatch):
    from plexora.plugins.gating.server.autogate.hypotheses import classifier_check
    from plexora.plugins.gating.server.classifier import features, runs

    ds = FakeData({"M": np.expm1(np.linspace(1, 3, 500))})
    monkeypatch.setattr(features, "pixels_ready", lambda _ds, _m: (True, None))
    monkeypatch.setattr(runs, "session_run", lambda *_a, **_k: {
        "record": {"status": "ready", "snr": {"grade": "low"}}, "arrays": {}})
    state = {"points": [], "window": [0, 0], "leader": 1.0, "params": dict(PARAMS),
             "observations": []}
    assert classifier_check.plan(_check_engine(ds), {"project": "f", "marker": "M"},
                                 state, []) is None
    assert state["classifier"] == {"status": "untrusted", "grade": "low"}
