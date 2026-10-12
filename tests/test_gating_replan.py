"""Re-planning on failure, and the guards beside it.

- a background that is the column's floor (background-subtracted values piled
  at zero) is bounded at the density's valley, not a Gaussian ceiling in the
  pile; an ordinary background peak is untouched;
- `within` is offered only when the data agree that the marker sits inside
  the partner;
- no move code makes after a look crosses a row a look judged;
- even the brightest positives sitting on an exclusive lineage fail a check
  of their own, and an excess against one the node did not check fails
  `partners`;
- a failure re-plans: dependents only the failed gate placed go to review
  (and so on through the tree), what only biology can settle is queued for
  one `replan` packet, and its answer routes the markers -- the loop is
  bounded, each marker asked once;
- strict QC with no QC result says so at the start.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from tests.autogate_fixtures import FakeData, populations


# -- the floor ---------------------------------------------------------------------------


def _floor_column(n=40_000, seed=3):
    """Half the cells at exactly zero, a ramp thinning out above them, a flat
    valley, and a positive mode near 6.6 (log1p units): LSP11361's SOX10."""
    rng = np.random.default_rng(seed)
    zeros = np.zeros(n // 2)
    ramp = rng.exponential(0.6, n // 4)
    valley = rng.uniform(1.5, 5.0, n // 12)
    positive = rng.normal(6.6, 0.35, n - n // 2 - n // 4 - n // 12)
    return np.concatenate([zeros, ramp, valley, positive]).astype(np.float32)


def test_a_floor_background_is_bounded_at_its_valley_not_in_the_pile():
    from plexora.plugins.gating.server.autogate import profile as profmod, scoring

    col = profmod.Column("SOX10", _floor_column(), True)
    bg = scoring.background(col.body)
    assert bg["basis"] == "floor_valley"
    assert 2.0 < bg["ceiling"] < 5.5 and bg["onset"] == bg["ceiling"]


def test_an_ordinary_background_peak_is_untouched():
    from plexora.plugins.gating.server.autogate import profile as profmod, scoring

    raw, _truth = populations(40_000, 0.2)
    col = profmod.Column("CD3", np.log1p(raw), True)
    bg = scoring.background(col.body)
    assert bg["basis"] == "peak"
    assert bg["ceiling"] == pytest.approx(bg["mode"] + scoring.PARAMS["ceiling_sd"] * bg["sd"])


def test_a_floor_without_a_mode_above_it_keeps_the_peak_rule():
    from plexora.plugins.gating.server.autogate import profile as profmod, scoring

    rng = np.random.default_rng(1)
    values = np.concatenate([np.zeros(20_000), rng.exponential(0.8, 20_000)])
    bg = scoring.background(profmod.Column("X", values.astype(np.float32), True).body)
    assert bg["basis"] == "peak"


# -- within ------------------------------------------------------------------------------


def test_within_is_offered_only_when_the_data_agree_the_marker_sits_inside():
    from plexora.plugins.gating.server.autogate import packets

    def partner(orphans):
        return {"partner": "CD45", "relation": "subset", "grade": "moderate",
                "orphan_fraction": orphans}

    assert packets.within_eligible([partner(0.649)]) == []        # LSP11761 CD163
    assert packets.within_eligible([partner(0.2)]) == ["CD45"]
    assert packets.within_eligible([partner(None)]) == ["CD45"]
    low = {**partner(0.1), "grade": "low"}
    assert packets.within_eligible([low]) == []


# -- judged rows -------------------------------------------------------------------------


class _Log:
    def __init__(self):
        self.events = []

    def log(self, **event):
        self.events.append(event)


def test_no_move_after_a_look_crosses_a_row_it_judged():
    from plexora.plugins.gating.server.autogate.engine import Engine

    raw, _truth = populations(20_000, 0.25, bg=(6.0, 0.4), pos=(8.2, 0.4))
    ds = FakeData({"CD163": np.log1p(raw)}, log_transformed=True)
    stub = _Log()
    unit = {"project": "fake", "marker": "CD163", "candidate": 7.86,
            "judged_rows": [[7.87, 8.3, "own_stain"]], "judged": [[7.0, 7.5, "neighbour_only"]]}
    assert Engine.clamp_to_judged(stub, unit, ds, 8.36, why="within") == pytest.approx(7.87)
    assert Engine.clamp_to_judged(stub, unit, ds, 6.8, why="rescore") == pytest.approx(7.5)
    assert Engine.clamp_to_judged(stub, unit, ds, 7.7, why="rescore") == 7.7
    assert [e["why"] for e in stub.events] == ["within", "rescore"]
    assert unit["clamps"][0] == {"why": "within", "from": 8.36, "to": pytest.approx(7.87)}


def test_a_confident_look_records_what_its_rows_judged_and_an_unsure_one_does_not():
    from plexora.plugins.gating.server.autogate import transitions

    packet = {"_row_spans": {"below": [7.5, 7.8], "near": [7.8, 7.9], "above": [7.9, 8.3]}}
    sure = SimpleNamespace(rows={"below": "mixed", "near": "mixed", "above": "own_stain"},
                           confidence="fairly_sure")
    unit = {}
    transitions._judge_rows(unit, packet, sure)
    assert unit["judged_rows"] == [[7.9, 8.3, "own_stain"]] and unit["own_seen"] is True
    assert "judged" not in unit             # the cell model keeps to the intervals
    unsure = SimpleNamespace(rows={"below": "unstained", "near": "mixed", "above": "own_stain"},
                             confidence="unsure")
    other = {}
    transitions._judge_rows(other, packet, unsure)
    assert other == {}


# -- lineage -----------------------------------------------------------------------------


class _LineageStub(_Log):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def lineage_purity(self, unit):
        return self.rows


def _result():
    return {"ok": True, "failed": [], "fraction": 0.1,
            "checks": [{"name": "partners", "ok": True, "value": None, "limit": 0.1}]}


def test_positives_on_an_exclusive_lineage_even_at_the_top_fail_a_check_of_their_own():
    from plexora.plugins.gating.server.autogate.engine import Engine

    rows = [{"partner": "SOX10", "derived": True, "at_gate": 0.5, "at_top": 0.63,
             "excess": -0.13, "prevalence": 0.08, "contaminated": True, "n_top": 60}]
    unit = {"project": "p", "marker": "PD1"}
    result = Engine._with_lineage(_LineageStub(rows), unit, _result(), set())
    assert result["failed"] == ["lineage"] and result["ok"] is False
    assert unit["lineage_contamination"]["partner"] == "SOX10"
    acknowledged = {"project": "p", "marker": "PD1", "lineage_ack": True}
    assert Engine._with_lineage(_LineageStub(rows), acknowledged, _result(), set())["ok"]


def test_an_excess_against_an_exclusive_partner_the_node_did_not_check_fails_partners():
    from plexora.plugins.gating.server.autogate.engine import Engine

    rows = [{"partner": "SOX10", "derived": True, "at_gate": 0.61, "at_top": 0.20,
             "excess": 0.41, "prevalence": 0.25, "contaminated": False, "n_top": 120,
             "confidence": "moderate"}]
    result = Engine._with_lineage(_LineageStub(rows), {"project": "p", "marker": "CD20L"},
                                  _result(), set())
    assert result["failed"] == ["partners"]
    assert result["checks"][0]["value"][0]["partner"] == "SOX10"
    # Checked by the node already: not counted twice.
    again = Engine._with_lineage(_LineageStub(rows), {"project": "p", "marker": "CD20L"},
                                 _result(), {"SOX10"})
    assert again["ok"] is True


def test_purity_against_a_partner_the_whole_population_sits_on():
    from plexora.plugins.gating.server.autogate import bivariate

    rng = np.random.default_rng(5)
    n = 20_000
    tumour = rng.random(n) < 0.1
    # A channel that lights only where SOX10 is: bleed-through, brightest on
    # the brightest tumour cells.
    sox10 = np.where(tumour, rng.normal(7.0, 0.3, n), rng.normal(4.0, 0.3, n))
    bleed = np.where(tumour, rng.normal(6.5, 0.4, n), rng.normal(4.0, 0.3, n))
    ds = FakeData({"SOX10": sox10, "PD1": bleed}, log_transformed=True)
    p = bivariate.purity_excess(ds, "PD1", 5.5, "SOX10", 5.5, relation="exclusive")
    assert p["at_top"] > 0.9 and p["excess"] < 0.1            # the excess cannot see it
    few = FakeData({"SOX10": sox10[:3000], "PD1": bleed[:3000]}, log_transformed=True)
    assert bivariate.purity_excess(few, "PD1", 5.5, "SOX10", 5.5, relation="exclusive") is None
    small = bivariate.purity_excess(few, "PD1", 5.5, "SOX10", 5.5, relation="exclusive",
                                    min_top=30)
    assert small["n_top"] < bivariate.THRESHOLDS["control_min_cells"] and small["at_top"] > 0.9


# -- re-planning -------------------------------------------------------------------------


PANEL = ["CD45", "CD3e", "CD8a", "CD20", "SOX10", "PD1"]


class _Engine(_Log):
    """The parts of `Engine` re-planning uses; `close` runs `after_close` as
    `Engine._close_tail` does."""

    TERMINAL = ("accepted", "accepted_low_confidence", "manual_review_recommended",
                "no_positive_population", "technically_failed", "insufficient_information")
    ASKS = {"awaiting_t2": "t2_confirm", "awaiting_t4": "t4_candidates"}

    def __init__(self, units, options=None):
        super().__init__()
        self.record = {"images": ["p"], "order": [u["marker"] for u in units],
                       "units": {f"p::{u['marker']}": u for u in units}}
        self.options = {"replan": True, "max_tier": 4, **(options or {})}
        self.closed = []
        self.refreshed = []

    def units_of(self, project):
        return [u for u in self.record["units"].values() if u["project"] == project]

    def panel_for(self, project):
        from plexora.plugins.gating.server.autogate import context

        return context.build(PANEL)

    def ledger(self, project, without=None):
        from plexora.plugins.gating.server.autogate import hierarchy

        return hierarchy.ledger([u for u in self.units_of(project) if u is not without])

    def references_ready(self, unit):
        self.refreshed.append(unit["marker"])
        return []

    def close(self, unit, state, reason, *, proposed=None, confidence=None):
        from plexora.plugins.gating.server.autogate import replan

        unit["state"], unit["reason"] = state, reason
        if state == "manual_review_recommended":
            unit["needs_review"] = True
        self.closed.append((unit["marker"], state))
        replan.after_close(self, unit)


def _u(marker, state, **extra):
    return {"project": "p", "marker": marker, "state": state, **extra}


def test_a_failure_sends_what_it_alone_placed_to_review_down_the_tree():
    from plexora.plugins.gating.server.autogate import replan

    cd3 = _u("CD3e", "manual_review_recommended", needs_review=True)
    led = _u("CD8a", "accepted", confidence="moderate", start={"low": 6.5, "leading": "CD3e"},
             final=6.5)
    walked = _u("CD20", "accepted", confidence="moderate",
                start={"low": 6.0, "leading": "CD3e"}, final=6.4)
    looked = _u("PD1", "awaiting_t4", used={"packets": 2},
                reference_gates=[{"marker": "CD3e"}])
    engine = _Engine([_u("CD45", "accepted", confidence="high"), cd3, led, walked, looked,
                      _u("SOX10", "accepted", confidence="high")])
    replan.after_close(engine, cd3)
    assert ("CD8a", "manual_review_recommended") in engine.closed
    assert walked["state"] == "accepted"                    # a look placed it
    assert engine.refreshed == ["PD1"]
    assert not replan.pending(engine.record)                # nothing only biology can call


def test_a_suspect_population_is_queued_once_and_its_answer_routes_it():
    from plexora.agent.errors import AgentError
    from plexora.plugins.gating.server.autogate import answers, replan

    contaminated = {"partner": "SOX10", "at_top": 0.63, "prevalence": 0.08, "at_gate": 0.5}
    pd1 = _u("PD1", "manual_review_recommended", needs_review=True, candidate=7.69,
             lineage_contamination=contaminated, qc_done=True)
    cd20 = _u("CD20", "manual_review_recommended", needs_review=True, candidate=7.12,
              artifact_verdict=True)
    engine = _Engine([_u("CD45", "accepted", confidence="high"),
                      _u("SOX10", "accepted", confidence="high"), pd1, cd20])
    replan.after_close(engine, pd1)
    replan.after_close(engine, cd20)
    assert replan.pending(engine.record)
    packet, images = replan.packet(engine, [])
    assert images == [] and packet["allowed"]["markers"] == ["p::PD1", "p::CD20"]
    shown = {m["marker"]: m for m in packet["evidence"]["markers"]}
    assert shown["PD1"]["suspected"]["why"] == "lineage_contamination"
    assert shown["CD20"]["suspected"]["why"] == "artifact"
    partial = answers.ReplanAnswer(markers={"p::PD1": "absent"})
    with pytest.raises(AgentError):
        replan.apply(engine, packet, partial)
    answer = answers.ReplanAnswer(markers={"p::PD1": "absent", "p::CD20": "bleed_through"})
    out = replan.apply(engine, packet, answer)
    assert pd1["state"] == "no_positive_population"
    assert cd20["state"] == "awaiting_t4" and cd20["direction"] == "up" and cd20["lineage_ack"]
    assert out["round"] == 1 and not replan.pending(engine.record)
    # Asked once: closing again does not queue it again.
    replan.after_close(engine, pd1)
    assert not replan.pending(engine.record)


def test_a_seen_own_stain_is_never_called_absent_on_an_artifact_verdict_alone():
    from plexora.plugins.gating.server.autogate import replan

    seen = _u("CD20", "manual_review_recommended", needs_review=True, artifact_verdict=True,
              own_seen=True)
    engine = _Engine([_u("CD45", "accepted", confidence="high"), seen])
    replan.after_close(engine, seen)
    assert not replan.pending(engine.record)


def test_an_image_whose_looks_keep_naming_one_artifact_is_queued_and_noted_on_its_gates():
    from plexora.plugins.gating.server.autogate import answers, replan

    units = [_u(m, "accepted", confidence="moderate", artifact_flags=["edge"], final=7.0)
             for m in ("CD45", "CD3e", "CD8a")] + [_u("SOX10", "accepted", confidence="high")]
    engine = _Engine(units)
    replan.after_accept(engine, units[0])
    assert replan.pending(engine.record)
    packet, _ = replan.packet(engine, [])
    assert packet["evidence"]["images"][0]["flags"] == {"edge": ["CD3e", "CD45", "CD8a"]}
    replan.apply(engine, packet, answers.ReplanAnswer(images={"p": "image_artifact"}))
    assert engine.record["image_findings"]["p"]["markers"] == ["CD3e", "CD45", "CD8a"]
    # The finding is noted on the flagged gates; their acceptance and gates stand.
    flagged = [u for u in units if u["marker"] in ("CD45", "CD3e", "CD8a")]
    assert all(u["state"] == "accepted" and u["final"] == 7.0 for u in flagged)
    assert all(u["image_finding"]["flags"] == ["edge"] for u in flagged)
    assert all("run QC" in u["reason"] for u in flagged)
    assert "image_finding" not in units[3]
    replan.after_accept(engine, units[3])
    assert not replan.pending(engine.record)                 # each flag asked once


def test_replanning_off_keeps_the_plan_made_at_the_start():
    from plexora.plugins.gating.server.autogate import replan

    cd3 = _u("CD3e", "manual_review_recommended", needs_review=True,
             lineage_contamination={"partner": "SOX10", "at_top": 0.9, "prevalence": 0.1})
    led = _u("CD8a", "accepted", confidence="moderate", start={"low": 6.5, "leading": "CD3e"},
             final=6.5)
    engine = _Engine([cd3, led], options={"replan": False})
    replan.after_close(engine, cd3)
    assert led["state"] == "accepted" and not replan.pending(engine.record)


# -- QC at the start ---------------------------------------------------------------------


def test_strict_qc_without_a_qc_result_is_said_at_the_start():
    from plexora.plugins.gating import capabilities_session as cs

    blocks = {"A": {"mode": "strict", "applied": False, "reason": "no QC result for this image"},
              "B": {"mode": "strict", "applied": True, "n_left_out": 10},
              "C": {"mode": "off", "applied": False, "reason": "QC is off"}}
    assert cs._qc_missing(blocks) == ["A"]
    warning = cs._qc_warning(blocks)["qc_warning"]
    assert warning["images"] == ["A"] and "no QC result" in warning["message"]
    assert cs._qc_warning({"B": blocks["B"]}) == {}


def test_the_cell_model_is_withheld_from_an_image_with_no_qc_result():
    """With the model on, an image strict QC cannot act on gets no model
    (its confident calls would promote the image's artifacts); the start
    says so, and every other image keeps it."""
    from types import SimpleNamespace

    from plexora.plugins.gating import capabilities_session as cs
    from plexora.plugins.gating.server.autogate.engine import Engine

    blocks = {"A": {"mode": "strict", "applied": False, "reason": "no QC result for this image"},
              "B": {"mode": "strict", "applied": True}}
    assert cs._model_withheld(blocks, "full") == ["A"] and cs._model_withheld(blocks, "tiles") == []
    warning = cs._qc_warning(blocks, "model")["qc_warning"]
    assert warning["model_withheld"] == ["A"] and "cell model" in warning["message"]
    assert "model_withheld" not in cs._qc_warning(blocks)["qc_warning"]
    fake = SimpleNamespace(options={"hybrid": "full"}, record={"model_withheld": ["A"]})
    fake.hybrid_option = lambda: Engine.hybrid_option(fake)
    assert not Engine.hybrid_has(fake, "model", {"project": "A"})
    assert Engine.hybrid_has(fake, "model", {"project": "B"})
    assert Engine.hybrid_has(fake, "tiles", {"project": "A"})


def test_a_look_that_went_out_before_the_lineage_partner_is_still_caught_at_the_close():
    from plexora.plugins.gating.server.autogate import replan

    class _Screening(_Engine):
        def lineage_purity(self, unit):
            return [{"partner": "SOX10", "derived": True, "at_gate": 0.4, "at_top": 0.74,
                     "excess": -0.34, "prevalence": 0.08, "contaminated": True}]

    cd20 = _u("CD20", "manual_review_recommended", needs_review=True, candidate=7.12)
    engine = _Screening([_u("SOX10", "accepted", confidence="high"), cd20])
    replan.after_close(engine, cd20)
    assert cd20["lineage_contamination"]["partner"] == "SOX10"
    assert cd20["state"] == "qc_confirm" and cd20["lineage_check"]
    assert not replan.pending(engine.record)


def test_a_marker_closed_before_its_lineage_partner_is_screened_when_the_partner_lands():
    """PD1 closes for review before SOX10 is gated (its look settled
    nothing): the close-time screen has no partner yet. SOX10's acceptance
    screens it again, queues it once, and holds it for the answer."""
    from plexora.plugins.gating.server.autogate import replan

    class _Screening(_Engine):
        def lineage_purity(self, unit):
            if self.record["units"]["p::SOX10"]["state"] != "accepted":
                return []
            return [{"partner": "SOX10", "derived": True, "at_gate": 0.4, "at_top": 0.63,
                     "excess": -0.23, "prevalence": 0.08, "contaminated": True}]

    pd1 = _u("PD1", "manual_review_recommended", needs_review=True, candidate=7.69)
    sox10 = _u("SOX10", "awaiting_t2")
    engine = _Screening([_u("CD45", "accepted", confidence="high"), sox10, pd1])
    replan.after_close(engine, pd1)
    assert not replan.pending(engine.record)
    assert "SOX10" in replan.excluded_by(engine, pd1)
    sox10.update(state="accepted", confidence="high")
    replan.after_accept(engine, sox10)
    assert pd1["state"] == "qc_confirm" and pd1["replan_from"] == "manual_review_recommended"
    # Another partner landing asks nothing again.
    replan.after_accept(engine, engine.record["units"]["p::CD45"])
    assert pd1["state"] == "qc_confirm" and not replan.pending(engine.record)


@pytest.mark.parametrize("verdict, state, planner", [
    ("no_positive_population", "no_positive_population", False),
    ("technical_failure", "technically_failed", False),
    ("real_signal", "awaiting_replan", True),
    ("cannot_tell", "awaiting_replan", True)])
def test_a_lineage_suspect_gets_a_whole_image_check_before_the_planner(verdict, state, planner):
    """The planner calls absent on a picture's verdict, not on numbers: a
    lineage suspect is shown on the whole image first. An empty or failed
    answer closes it there; anything else goes to the planner with the
    verdict beside the numbers."""
    from plexora.plugins.gating.server.autogate import answers, replan, transitions

    class _Checking(_Engine):
        def close(self, unit, state, reason, *, proposed=None, confidence=None, write=False):
            super().close(unit, state, reason, proposed=proposed, confidence=confidence)

    contaminated = {"partner": "SOX10", "at_top": 0.63, "prevalence": 0.08, "at_gate": 0.5}
    pd1 = _u("PD1", "manual_review_recommended", needs_review=True, candidate=7.69,
             lineage_contamination=contaminated)
    engine = _Checking([_u("SOX10", "accepted", confidence="high"), pd1])
    replan.after_close(engine, pd1)
    assert pd1["state"] == "qc_confirm" and "SOX10+" in pd1["qc_reason"][0]
    packet = {"units": [{"project": "p", "marker": "PD1"}]}
    transitions.apply_qc(engine, packet, answers.QCAnswer(verdict=verdict))
    assert pd1["state"] == state and pd1["whole_image"] == verdict
    assert replan.pending(engine.record) is planner
    if planner:
        shown = replan.packet(engine, [])[0]["evidence"]["markers"][0]
        assert shown["whole_image"] == verdict and shown["state"] == "manual_review_recommended"


def test_a_look_waits_for_the_exclusions_the_tree_derives():
    from plexora.plugins.gating.server.autogate.engine import Engine

    stub = _Engine([_u(m, "pending") for m in ("CD45", "SOX10", "CD20", "PD1")])
    links = Engine._links(stub, "p")
    assert "SOX10" in links["CD20"]           # CD20 within CD45, CD45 excludes SOX10


def test_contamination_is_read_across_lineages_never_between_siblings_of_one():
    from plexora.plugins.gating.server.autogate import hierarchy
    from plexora.plugins.gating.server.autogate.engine import Engine

    rng = np.random.default_rng(9)
    n = 20_000
    t_cell = rng.random(n) < 0.3
    tumour = (~t_cell) & (rng.random(n) < 0.2)
    # CD8 light on CD4 cells (spill in an aggregate), PD1 light on tumour.
    cols = {"CD45": np.where(t_cell, 7.5, 4.0) + rng.normal(0, 0.2, n),
            "CD3e": np.where(t_cell, 7.2, 4.0) + rng.normal(0, 0.2, n),
            "CD4": np.where(t_cell, 7.0, 4.0) + rng.normal(0, 0.2, n),
            "CD8a": np.where(t_cell, 6.6, 4.0) + rng.normal(0, 0.2, n),
            "SOX10": np.where(tumour, 7.0, 4.0) + rng.normal(0, 0.2, n),
            "PD1": np.where(tumour, 6.8, 4.0) + rng.normal(0, 0.2, n)}
    ds = FakeData(cols, log_transformed=True, name="p")

    class _Purity(_Engine):
        def __init__(self, units):
            super().__init__(units)
            self.call = SimpleNamespace(session=SimpleNamespace(data=lambda name: ds))

    gated = [_u(m, "accepted", confidence="high", final=5.5) for m in ("CD45", "CD3e", "CD4",
                                                                        "SOX10")]
    engine = _Purity(gated + [_u("CD8a", "awaiting_t2", candidate=5.5),
                              _u("PD1", "awaiting_t2", candidate=5.5)])
    engine.panel_for = lambda project: __import__(
        "plexora.plugins.gating.server.autogate.context", fromlist=["build"]).build(
        ["CD45", "CD3e", "CD4", "CD8a", "SOX10", "PD1"])
    import plexora.plugins.gating.server.autogate.engine as engines
    real = engines._data
    engines._data = lambda call, project: ds
    try:
        cd8 = {r["partner"]: r for r in Engine.lineage_purity(engine, engine.record["units"]["p::CD8a"])}
        pd1 = {r["partner"]: r for r in Engine.lineage_purity(engine, engine.record["units"]["p::PD1"])}
    finally:
        engines._data = real
    assert cd8["CD4"]["cross_lineage"] is False and not cd8["CD4"]["contaminated"]
    assert pd1["SOX10"]["cross_lineage"] is True and pd1["SOX10"]["contaminated"]


