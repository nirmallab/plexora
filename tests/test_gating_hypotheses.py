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
    # The classifier disagrees with the leader on most cells of the outer
    # intervals, both ways, and agrees beside the leader: its picks fall
    # outside the two intervals the blind cells come from.
    raw = np.where(log_v > 2.0, 1, 0)
    outer = ((log_v > 1.2) & (log_v <= 1.6)) | ((log_v > 2.4) & (log_v <= 2.8))
    flip = outer & (rng.random(values.size) < 0.8)
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
    # Without a label-set score on record the check is exploratory: counted,
    # kept, no look.
    monkeypatch.setattr(classifier_check, "trust_records", lambda *_a: [])
    assert classifier_check.plan(_check_engine(ds), unit, dict(state), []) is None
    explored = dict(state)
    classifier_check.plan(_check_engine(ds), unit, explored, [])
    assert explored["classifier"]["status"] == "exploratory"
    assert explored["classifier"]["counts"]["below"]["flagged"] > 0
    monkeypatch.setattr(classifier_check, "trust_records", lambda *_a: [
        {"project": "another", "marker": "m", "n": 80, "classifier": 0.80, "gate": 0.70}])
    plan = classifier_check.plan(_check_engine(ds), unit, state, [])
    assert plan is not None and state["classifier"]["trusted_on"]["project"] == "another"
    chosen = np.isin(np.arange(1, values.size + 1), plan["ids"])
    assert (log_v[chosen] <= 2.0).any() and (log_v[chosen] > 2.0).any()
    picked = int((chosen & outer).sum())
    blind = int((chosen & ~outer).sum())
    assert picked <= PARAMS["panel_cells"] * classifier_check.FLAGGED_SHARE + 1
    assert blind >= PARAMS["panel_cells"] * (1 - classifier_check.FLAGGED_SHARE) - 1
    assert plan["intervals"] == [] and len(plan["ids"]) <= PARAMS["panel_cells"]
    # Every chosen cell carries the classifier's call, and every interval of
    # the range its cells by call, for the post-stratified evidence.
    assert set(state["classifier"]["calls"]) >= {str(i) for i in plan["ids"]}
    assert len(state["classifier"]["strata"]) == len(bounds) - 1
    # Once per marker, and never without `check`.
    assert classifier_check.plan(_check_engine(ds), unit, state, []) is None
    assert classifier_check.plan(_check_engine(ds, "layer"), unit,
                                 {**state, "classifier": None}, []) is None


def test_a_record_from_the_same_image_or_a_narrow_win_does_not_trust_the_classifier():
    from plexora.plugins.gating.server.autogate.hypotheses import classifier_check

    same = [{"project": "img", "marker": "CD3e", "n": 200, "classifier": 0.9, "gate": 0.6}]
    narrow = [{"project": "other", "marker": "CD3e", "n": 200, "classifier": 0.71,
               "gate": 0.70}]
    few = [{"project": "other", "marker": "CD3e", "n": 20, "classifier": 0.9, "gate": 0.6}]
    good = [{"project": "other", "marker": "cd3e", "n": 200, "classifier": 0.8, "gate": 0.7}]
    for records in (same, narrow, few):
        assert classifier_check.trusted("CD3e", "img", records) is None
    assert classifier_check.trusted("CD3e", "img", good) is good[0]


def test_an_enriched_pick_is_read_per_classifier_call_not_as_the_interval():
    # 1000 cells: 100 the classifier calls positive, 900 negative. Its picks
    # (positive calls, all stained) and blind picks (negative, unstained):
    # pooled they say one half; weighted by the strata, one tenth.
    cells = ([{"cell_id": i, "value": 1.5, "label": "own_stain"} for i in range(10)]
             + [{"cell_id": 100 + i, "value": 1.5, "label": "not_own_stain"} for i in range(10)])
    calls = {**{str(i): 1 for i in range(10)}, **{str(100 + i): 0 for i in range(10)}}
    table = evidence.counts([1.0, 2.0], [{"cells": cells}], calls)
    plain, _n = evidence.estimate(table[0])
    share, n_eff = evidence.estimate(table[0], {"1": 100, "0": 900})
    assert plain == pytest.approx(0.5) and share == pytest.approx(0.1)
    assert 0 < n_eff < 20


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


# -- the evidence weighted by the cells each interval holds ---------------------------------


def test_the_cells_between_two_candidates_are_weighed_by_interval_not_by_reads():
    # A survey reads 20 cells from each interval: pooled as read, a sliver of
    # 40 stained cells would outvote 1000 mostly unstained ones.
    obs = _obs([1.5] * 20 + [2.5] * 20, ["own_stain"] * 9 + ["not_own_stain"] * 11
               + ["own_stain"] * 20)
    table = evidence.counts([1.0, 2.0, 3.0], [obs])
    share, n_eff = evidence.between(table, [1000, 40], 0, 2)
    assert share == pytest.approx((1000 * 0.45 + 40 * 1.0) / 1040)
    assert n_eff < 40
    low, _high = evidence.wilson(share * n_eff, n_eff)
    assert low < 0.5
    pooled_low, _ = evidence.wilson(29, 40)
    assert pooled_low > 0.5


def test_a_rival_that_would_remove_a_rare_population_blocks():
    # 15 positives above the leader's interval: fewer than `min_between`, but
    # all of the population whose existence the rival disputes.
    bounds = [1.0, 2.0, 3.0]
    obs = _obs([1.5] * 10, ["not_own_stain"] * 10)
    plain = evidence.support(bounds, [500, 12], [obs], prior=1, to_fit=IDENTITY,
                             tolerance=0.2, resolve_within=0.3, min_between=20, n_above=3)
    rare = evidence.support(bounds, [500, 12], [obs], prior=1, to_fit=IDENTITY,
                            tolerance=0.2, resolve_within=0.3, min_between=20,
                            min_between_share=0.25, n_above=3)
    assert plain["leader"] == rare["leader"] == 1
    assert plain["resolved"] and not rare["resolved"]
    assert [r["low"] for r in rare["blocking"]] == [3.0]


# -- rare markers --------------------------------------------------------------------------


def _rare_state():
    state = _state(lows=(1.0, 2.0, 3.0), n_positive=(2000, 40, 10), window=(0, 2), prior=1)
    state.update(rare={"fraction_at_start": 0.004, "class": "long_tail"}, top=5.0)
    return state


def test_a_rare_marker_reads_its_own_cells_before_its_gate_resolves():
    state = _rare_state()
    params = dict(PARAMS, min_between=20, min_between_share=0.25, resolve_within=0.3)
    # The survey reads everything below 3 unstained: the leader is the top
    # candidate, whose 10 positives lie above the range and were never shown.
    state["observations"].append(_obs([1.5] * 10 + [2.5] * 10, ["not_own_stain"] * 20))
    status, s = controller.decide(state, IDENTITY, params)
    assert s["leader"] == 2 and s["resolved"] and status is None
    plan = controller.next_action(state, IDENTITY, params)
    assert plan["kind"] == "tail"
    assert plan["intervals"] == [{"interval": None, "from": 3.0, "to": 5.0, "n": 10}]
    stained = dict(state, observations=state["observations"]
                   + [_obs([3.5] * 10, ["own_stain"] * 10)])
    assert controller.decide(stained, IDENTITY, params)[0] == "resolved"
    unstained = dict(state, observations=state["observations"]
                     + [_obs([3.5] * 10, ["not_own_stain"] * 10)])
    assert controller.decide(unstained, IDENTITY, params)[0] == "positives_not_confirmed"
    # Not rare: the same evidence resolves at once.
    assert controller.decide(dict(state, rare=None), IDENTITY, params)[0] == "resolved"


def test_a_marker_that_is_not_rare_resolves_without_a_tail_look():
    state = _rare_state()
    state["rare"] = None
    params = dict(PARAMS, min_between=20, min_between_share=0.25, resolve_within=0.3)
    state["observations"].append(_obs([1.5] * 10 + [2.5] * 10, ["not_own_stain"] * 10
                                      + ["own_stain"] * 10))
    assert controller.decide(state, IDENTITY, params)[0] == "resolved"


# -- a lattice with one point --------------------------------------------------------------


def test_a_single_candidate_gets_steps_either_side_on_the_grid(monkeypatch):
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import hypotheses
    from plexora.plugins.gating.server.autogate import profile as profmod

    values = np.expm1(np.random.default_rng(3).normal(2.0, 0.4, 4000).clip(0.1))
    ds = FakeData({"M": values})
    monkeypatch.setattr(model, "_description", lambda _ds: {})
    col = profmod.column(ds, "M")
    only = [{"id": "gmm", "low": float(np.expm1(2.2)), "sources": ["gmm"],
             "n_positive": int((values > np.expm1(2.2)).sum())}]
    top = float(values.max())
    added = hypotheses._steps(ds, "M", col, only, None, top)
    fits = sorted(round(float(col.to_fit(p["low"])) - 2.2, 6) for p in added)
    assert fits == pytest.approx(sorted(s * k for s in hypotheses.SINGLE_STEPS
                                        for k in (-1, 1)), abs=1e-6)
    assert all(p["n_positive"] == int(col.n_positive(p["low"], top)) for p in added)


# -- the range grows where the evidence points ----------------------------------------------


GROW = dict(PARAMS, min_between=20, min_between_share=0.25, resolve_within=0.3,
            edge_step=0.25, edge_steps=2, split_width=0.4, split_share=[0.2, 0.8],
            split_reads=6, split_parts=4, unreadable_looks=2)


def test_one_look_of_uncertain_cells_does_not_close_a_marker():
    state = _state()
    crowded = _obs([2.5] * 6 + [3.5] * 14, ["not_own_stain"] * 6 + ["uncertain"] * 14)
    state["observations"].append(crowded)
    assert controller.decide(state, IDENTITY, GROW)[0] is None
    state["observations"].append(_obs([2.6] * 6 + [3.6] * 14,
                                      ["not_own_stain"] * 6 + ["uncertain"] * 14))
    assert controller.decide(state, IDENTITY, GROW)[0] == "unreadable"


def test_a_leader_on_the_lattices_top_point_steps_beyond_it():
    # Every cell shown below the top point read unstained: the gate is there
    # or above it, and the cells above were never shown.
    state = _state(lows=(1.0, 2.0, 3.0), n_positive=(1000, 600, 300), window=(0, 2), prior=1)
    state.update(n_finite=1500, grown={"up": 0, "down": 0})
    state["observations"].append(_obs([1.5] * 10 + [2.5] * 10, ["not_own_stain"] * 20))
    status, s = controller.decide(state, IDENTITY, GROW)
    assert s["leader"] == 2 and s["resolved"] and status is None
    assert controller.next_action(state, IDENTITY, GROW) == {"kind": "step", "side": "up"}
    # Once the steps are spent (or found no cells), it stands.
    state["grown"]["up"] = GROW["edge_steps"]
    assert controller.decide(state, IDENTITY, GROW)[0] == "resolved"
    # Without the new parameters the engine behaves as before.
    assert controller.decide(dict(state, grown={}), IDENTITY, PARAMS)[0] == "resolved"


def test_a_leader_on_the_top_point_with_a_rival_still_blocking_is_refined_first():
    # The cells just below the top point read partly stained: the dispute is
    # there, and a look above the lattice could not settle it. (The interval
    # is narrower than `split_width`: a wide one would be split first.)
    state = _state(lows=(1.0, 2.0, 2.35), n_positive=(1000, 600, 300), window=(0, 2), prior=1)
    state.update(n_finite=1500, grown={"up": 0, "down": 0})
    state["observations"].append(_obs([1.5] * 10 + [2.2] * 10,
                                      ["not_own_stain"] * 10 + ["own_stain"] * 4
                                      + ["not_own_stain"] * 6))
    status, s = controller.decide(state, IDENTITY, GROW)
    assert s["leader"] == 2 and not s["resolved"] and status is None
    assert controller.next_action(state, IDENTITY, GROW)["kind"] == "refine"


def test_a_wide_mixed_interval_beside_the_leader_is_split_once():
    state = _state(lows=(1.0, 2.0, 3.0, 4.0), n_positive=(1000, 800, 200, 100), window=(0, 3),
                   prior=1)
    state.update(n_finite=1200, grown={"up": 0, "down": 0}, split=[])
    # Unstained below 2, stained above 3, half and half in the 600 cells
    # between: the boundary is inside (2, 3].
    state["observations"].append(_obs([1.5] * 8 + [2.5] * 10 + [3.5] * 8,
                                      ["not_own_stain"] * 8 + ["own_stain", "not_own_stain"] * 5
                                      + ["own_stain"] * 8))
    status, s = controller.decide(state, IDENTITY, GROW)
    assert status is None
    plan = controller.next_action(state, IDENTITY, GROW)
    assert plan == {"kind": "split", "interval": 1, "from": 2.0, "to": 3.0}
    state["split"].append([2.0, 3.0])
    assert (controller.next_action(state, IDENTITY, GROW) or {}).get("kind") != "split"


def test_a_homogeneous_wide_interval_is_not_split():
    state = _state(lows=(1.0, 2.0, 3.0, 4.0), n_positive=(1000, 800, 200, 100), window=(0, 3),
                   prior=1)
    state.update(n_finite=1200, grown={"up": 0, "down": 0}, split=[])
    state["observations"].append(_obs([1.5] * 8 + [2.5] * 10 + [3.5] * 8,
                                      ["not_own_stain"] * 18 + ["own_stain"] * 8))
    s = controller.support(state, IDENTITY, GROW)
    assert not any(controller.splittable(state, s, k, IDENTITY, GROW) for k in range(3))


def test_points_joined_to_the_range_keep_its_ends_and_its_prior():
    from plexora.plugins.gating.server.autogate import hypotheses

    state = _state(lows=(1.0, 2.0, 3.0, 4.0), n_positive=(1000, 800, 200, 100), window=(1, 3),
                   prior=2)
    hypotheses._insert(state, [{"id": "split:2.5", "low": 2.5, "n_positive": 500,
                                "sources": ["split:2.5"]}])
    assert [p["low"] for p in state["points"]] == [1.0, 2.0, 2.5, 3.0, 4.0]
    assert state["window"] == [1, 4] and state["points"][state["prior"]]["low"] == 3.0
    hypotheses._insert(state, [{"id": "step:5", "low": 5.0, "n_positive": 40,
                                "sources": ["step:5"]}], side="up")
    assert state["window"] == [1, 5] and state["added_points"] == ["split:2.5", "step:5"]


def test_a_step_and_a_split_are_lattice_like_points_on_the_column(monkeypatch):
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import hypotheses
    from plexora.plugins.gating.server.autogate import profile as profmod

    values = np.expm1(np.random.default_rng(5).normal(2.0, 0.5, 5000).clip(0.1))
    ds = FakeData({"M": values})
    monkeypatch.setattr(model, "_description", lambda _ds: {})
    col = profmod.column(ds, "M")
    top = float(values.max())
    lows = [float(np.expm1(x)) for x in (1.6, 2.0, 2.4)]
    state = {"points": [{"id": f"p{i}", "low": lo, "sources": ["gmm"],
                         "n_positive": int(col.n_positive(lo, top))} for i, lo in enumerate(lows)],
             "window": [0, 2], "prior": 1, "top": top, "n_finite": int(col.n_finite),
             "grown": {"up": 0, "down": 0}, "split": [], "observations": [],
             "params": dict(GROW, tolerance=0.2, min_gap=0.1)}
    step = hypotheses._step(ds, "M", col, state, "up")
    assert len(step) == 1 and float(col.to_fit(step[0]["low"])) == pytest.approx(2.65, abs=1e-6)
    assert state["window"] == [0, 3] and state["grown"]["up"] == 1
    added = hypotheses._split(ds, "M", col, state, lows[0], lows[1])
    assert [round(float(col.to_fit(p["low"])), 6) for p in added] == [1.8]
    assert state["split"] == [[round(lows[0], 6), round(lows[1], 6)]]
    assert all(p["n_positive"] == int(col.n_positive(p["low"], top)) for p in step + added)
