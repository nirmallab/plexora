"""A state marker's carriers (ImageLSP23362 PD-1, 2026-11-02).

PD-1 climbed from 6.88 to 7.47 on looks of borderline cells alone, most of
them not T cells. A state marker's carriers -- the lineages whose cells carry
it, stated by the vocabulary or named by both draws of the partner review --
now set its start beside the whole population's (a split inside the carrier
cells, used only when it holds on cleaning out the lineages the carrier
excludes, on drawing the cells again, and is not found in a lineage none of
the carriers can be), and its looks show carrier negatives and clear positives
(anchors) beside the borderline cells. The gate is still one threshold for
every cell, scored over the whole population.
"""

from __future__ import annotations

import numpy as np
import pytest

from plexora.plugins.gating.server.autogate import context

PANEL = ["DNA", "CD45", "CD3", "CD20", "CD68", "CD11c", "Pan-CK", "PD-1", "Ki-67", "FOXP3",
         "CD4"]


def _panel():
    return context.build(PANEL)


# -- which lineages carry a marker ------------------------------------------------------


def test_a_state_markers_carriers_are_its_narrowest_lineage_partners():
    panel = _panel()
    # PD-1 is carried by CD8 and CD4 T cells (the graph's narrowest carriers) within CD3 and CD45:
    # with no CD8 in this panel, the T cells and the CD4 cells carry it; CD45 is wider.
    assert context.carriers_for(panel, "PD-1") == [{"marker": "CD3", "source": "vocabulary"},
                                                   {"marker": "CD4", "source": "vocabulary"}]
    assert [c["marker"] for c in context.carriers_for(panel, "FOXP3")] == ["CD4", "CD3"]
    assert context.carriers_for(panel, "Ki-67") == []
    # A lineage marker is its own carrier.
    assert context.carriers_for(panel, "CD3") == []


def test_a_planners_carriers_are_checked_and_laid_beside_the_stated_ones():
    panel = _panel()
    kept, refused = context.check_carriers(
        panel, "PD-1", ["CD20", "CD45", "Pan-CK", "PD-1", "Ki-67", "CD20", "CD68"])
    assert kept == ["CD20", "CD45", "Pan-CK", "CD68"]
    assert {r["marker"] for r in refused} == {"PD-1", "Ki-67", "CD20"}
    # Only a state marker has carriers.
    assert context.check_carriers(panel, "CD3", ["CD45"])[0] == []
    # Never a lineage the panel says it excludes.
    panel["entries"]["PD-1"]["partners"].append(
        {"marker": "Pan-CK", "relation": "exclusive", "confidence": "moderate"})
    assert "Pan-CK" not in context.check_carriers(panel, "PD-1", ["Pan-CK"])[0]
    view = context.apply_carriers(_panel(), {"PD-1": ["CD20", "CD45", "CD68"]})
    carriers = context.carriers_for(view, "PD-1")
    # CD45 goes: CD3 (stated) and CD20 sit inside it.
    assert carriers == [{"marker": "CD3", "source": "vocabulary"},
                        {"marker": "CD4", "source": "vocabulary"},
                        {"marker": "CD20", "source": "planner"},
                        {"marker": "CD68", "source": "planner"}]


def test_carriers_are_kept_only_when_both_draws_name_them():
    panel = _panel()
    record = {"source": "user", "said": {"tissue": "colon", "disease": "cancer"}}
    markers = context.review_markers(panel, record, panel["markers"])
    for draw, named in zip(context.REVIEW_DRAWS, (["CD20", "CD68"], ["CD68"])):
        kept, _ = context.check_carriers(panel, "PD-1", named)
        context.store_review_draw(panel, record, draw,
                                  {"PD-1": {"checks": [], "partners": [], "carriers": kept}},
                                  markers)
    settled = context.settle_review(panel, record)
    assert settled["carriers"] == {"PD-1": ["CD68"]}
    assert context.carriers_planned(panel, record) == {"PD-1": ["CD68"]}
    assert context.review_brief(settled)["carriers"] == {"PD-1": ["CD68"]}
    assert context.review_done(panel, record, markers)
    # A review kept before carriers were asked for is asked again, once.
    panel["reviewed"][context.proposal_key(record)].pop("version")
    assert not context.review_done(panel, record, markers)


# -- the split inside a carrier -------------------------------------------------------


def _lineages(n=12_000, *, t_split=True, spill=False, background=False, seed=3):
    """Cells: 40 % T cells (CD3+), 10 % B cells (CD20+), the rest tumour
    (CK+). With `t_split`, PD-1 is two populations inside the T cells (70 %
    at 4, 30 % at 6); with `spill`, half the B cells read CD3+ and carry
    PD-1 at 6.5 -- a split that is the contamination's; with `background`,
    30 % of every lineage reads PD-1 at 6 for no PD-1 -- a split that is
    the channel's."""
    from tests.autogate_fixtures import FakeData

    rng = np.random.default_rng(seed)
    kind = rng.choice(3, size=n, p=[0.4, 0.1, 0.5])
    t, b = kind == 0, kind == 1
    cd3 = np.where(t, 7.0, 4.0) + rng.normal(0, 0.2, n)
    cd20 = np.where(b, 7.0, 4.0) + rng.normal(0, 0.2, n)
    pd1 = 4.0 + rng.normal(0, 0.3, n)
    if t_split:
        on = t & (rng.random(n) < 0.3)
        pd1[on] = 6.0 + rng.normal(0, 0.3, int(on.sum()))
    if spill:
        leak = b & (rng.random(n) < 0.5)
        cd3[leak] = 7.0 + rng.normal(0, 0.2, int(leak.sum()))
        pd1[leak] = 6.5 + rng.normal(0, 0.3, int(leak.sum()))
    if background:
        hazed = rng.random(n) < 0.3
        pd1[hazed] = 6.0 + rng.normal(0, 0.3, int(hazed.sum()))
    # Drawn last, so the other columns are the cells they always were.
    ck = np.where(kind == 2, 7.0, 4.0) + rng.normal(0, 0.2, n)
    return FakeData({"CD3": np.expm1(cd3), "CD20": np.expm1(cd20), "CK": np.expm1(ck),
                     "PD1": np.expm1(pd1)})


def _gate(ds, marker, low_fit):
    from plexora.plugins.gating.server.autogate import profile as profmod

    col = profmod.column(ds, marker)
    return {"marker": marker, "low": float(col.from_fit(low_fit)),
            "high": float(col.sorted32[-1])}


def test_a_split_inside_the_carrier_is_usable_when_cleaning_leaves_it():
    from plexora.plugins.gating.server.autogate import bivariate

    ds = _lineages()
    fit = bivariate.carrier_fit(ds, "PD1", _gate(ds, "CD3", 5.5),
                                exclusives=[_gate(ds, "CD20", 5.5)],
                                controls=[_gate(ds, "CK", 5.5)])
    assert fit["usable"], fit
    assert fit["n_clean"] <= fit["n"] and fit["cleaned_of"] == ["CD20"]
    assert 4.5 < np.log1p(fit["low"]) < 6.0
    assert fit["wander"] <= 0.2 and [c["marker"] for c in fit["controls"]] == ["CK"]


def test_a_split_the_channels_background_makes_is_not_usable():
    from plexora.plugins.gating.server.autogate import bivariate

    # Half of every lineage reads PD-1 high for no PD-1 (LSP23362: 6.45 in the
    # T cells, 6.46 in the tumour cells): the tumour cells split the same way.
    ds = _lineages(t_split=False, background=True)
    fit = bivariate.carrier_fit(ds, "PD1", _gate(ds, "CD3", 5.5),
                                exclusives=[_gate(ds, "CD20", 5.5)],
                                controls=[_gate(ds, "CK", 5.5)])
    assert not fit["usable"] and "background" in fit["why"], fit
    alone = bivariate.carrier_fit(ds, "PD1", _gate(ds, "CD3", 5.5),
                                  exclusives=[_gate(ds, "CD20", 5.5)])
    # Without the control the same split would have passed.
    assert alone["usable"], alone


class _Populations:
    """Just what `Engine.carrier_populations` reads."""

    def __init__(self, gates):
        self.gates = gates

    def panel_for(self, project):
        return {}

    def ledger(self, project, without=None):
        return {m: {"grade": "moderate", "state": "accepted", "gate": g}
                for m, g in self.gates.items()}

    def _gated(self, ds, marker, low):
        from plexora.plugins.gating.server.autogate.engine import Engine

        return Engine._gated(self, ds, marker, low)

    def _carrier_grade(self, entry):
        from plexora.plugins.gating.server.autogate.engine import Engine

        return Engine._carrier_grade(entry)

    def _excluded_lineages(self, ds, project, marker, book):
        return []


def test_a_carrier_must_carry_the_marker_more_often_than_the_average_cell(monkeypatch):
    from plexora.plugins.gating.server.autogate.engine import Engine

    ds = _lineages()
    named = [{"marker": m, "source": s} for m, s in
             (("CD3", "vocabulary"), ("CD20", "planner"), ("CK", "planner"))]
    monkeypatch.setattr(context, "carriers_for", lambda panel, marker: named)
    stub = _Populations({m: _gate(ds, m, 5.5)["low"] for m in ("CD3", "CD20", "CK")})
    unit = {"project": "p", "marker": "PD1", "gmm": float(np.expm1(5.0))}
    found = Engine.carrier_populations(stub, unit, ds, {"top": 2, "min_enrichment": 1.0})
    # The tumour cells are the most cells but carry no PD-1; nor do the B cells.
    assert [c["marker"] for c in found["chosen"]] == ["CD3"]
    assert found["chosen"][0]["enrichment"] > 2 and found["chosen"][0]["positives"] > 1000
    assert {s["marker"] for s in found["skipped"]} == {"CD20", "CK"}
    assert all("no more often than the average cell" in s["why"] for s in found["skipped"])
    # With nothing to count positives at, the most cells, as before.
    bare = Engine.carrier_populations(stub, {"project": "p", "marker": "PD1"}, ds,
                                      {"top": 2, "min_enrichment": 1.0})
    assert [c["marker"] for c in bare["chosen"]] == ["CK", "CD3"]


def test_when_no_narrow_carrier_carries_it_the_wider_ones_are_tried_narrowest_first(monkeypatch):
    from plexora.plugins.gating.server.autogate.engine import Engine

    ds = _lineages()
    monkeypatch.setattr(context, "carriers_for", lambda panel, marker: [{"marker": "CD20", "source": "vocabulary"}])
    monkeypatch.setattr(context, "wider_carriers", lambda panel, marker: [
        {"marker": "CD3", "source": "vocabulary"}, {"marker": "CK", "source": "vocabulary"}])
    stub = _Populations({m: _gate(ds, m, 5.5)["low"] for m in ("CD3", "CD20", "CK")})
    unit = {"project": "p", "marker": "PD1", "gmm": float(np.expm1(5.0))}
    found = Engine.carrier_populations(stub, unit, ds, {"top": 2, "min_enrichment": 1.0})
    # The B cells carry no PD-1: the climb goes on to the T cells and stops there.
    assert found["climbed"] == ["CD3"] and [c["marker"] for c in found["chosen"]] == ["CD3"]
    assert [c["marker"] for c in found["listed"]] == ["CD20", "CD3"]
    # A narrow carrier that carries it: no climb.
    monkeypatch.setattr(context, "carriers_for", lambda panel, marker: [{"marker": "CD3", "source": "vocabulary"}])
    assert Engine.carrier_populations(stub, unit, ds, {"top": 2, "min_enrichment": 1.0})["climbed"] == []


def test_the_wider_carriers_are_the_stated_ones_left_out_narrowest_first():
    panel = _panel()                       # no CD8: PD-1 is read in its CD4 cells and T cells; CD45 is wider
    assert [c["marker"] for c in context.wider_carriers(panel, "PD-1")] == ["CD45"]
    full = context.build([*PANEL, "CD8"])
    assert [c["marker"] for c in context.wider_carriers(full, "PD-1")] == ["CD3", "CD45"]
    assert context.wider_carriers(panel, "CD3") == []


class _Controls:
    """Just what `Engine._marker_controls` reads."""

    def __init__(self, panel):
        self.panel = panel

    def panel_for(self, project):
        return self.panel

    def _carrier_grade(self, entry):
        from plexora.plugins.gating.server.autogate.engine import Engine

        return Engine._carrier_grade(entry)

    def _gated(self, ds, marker, low):
        return {"marker": marker, "low": low, "high": 9.0, "n": 10}


def test_a_control_is_a_lineage_no_carrier_can_be():
    from plexora.plugins.gating.server.autogate.engine import Engine

    stub = _Controls(_panel())
    book = {m: {"grade": "moderate", "state": "accepted", "gate": 6.0}
            for m in ("CD45", "CD3", "CD20", "CD68", "CD11c", "Pan-CK")}
    # PD-1's own exclusions are derived through CD3, low: never the test.
    # CD3 excludes CD20, CD68 and Pan-CK (CD11c only weakly: some CD8 T cells
    # carry it); CD68 excludes only Pan-CK of them, and is a carrier.
    both = Engine._marker_controls(stub, None, "p", "PD-1",
                                   [{"marker": "CD3"}, {"marker": "CD68"}], book)
    assert [c["marker"] for c in both] == ["Pan-CK"]
    t_only = Engine._marker_controls(stub, None, "p", "PD-1", [{"marker": "CD3"}], book)
    assert [c["marker"] for c in t_only] == ["CD20", "CD68", "Pan-CK"]
    # A lineage not gated in the run is no control.
    book.pop("CD20")
    assert "CD20" not in [c["marker"] for c in
                          Engine._marker_controls(stub, None, "p", "PD-1",
                                                  [{"marker": "CD3"}], book)]


def test_a_control_carries_the_marker_less_often_than_the_average_cell(monkeypatch):
    from plexora.plugins.gating.server.autogate import bivariate
    from plexora.plugins.gating.server.autogate.engine import Engine

    # LSP23362 PD-L1, carriers the CD11c and CD68 cells: the T cells (x1.6)
    # still carry it, so they split where the stain does; not a control.
    rates = {"CD3": 1.6, "CD20": 0.4, "Pan-CK": 0.07}
    monkeypatch.setattr(bivariate, "carrier_enrichment",
                        lambda ds, marker, low, gate, exclusives=(): {
                            "enrichment": rates.get(gate["marker"])})
    stub = _Controls(_panel())
    book = {m: {"grade": "moderate", "state": "accepted", "gate": 6.0}
            for m in ("CD3", "CD20", "CD68", "CD11c", "Pan-CK")}
    # (Ki-67 states no partners here, so none is left out as its own.)
    found = Engine._marker_controls(stub, None, "p", "Ki-67", [{"marker": "CD68"}], book,
                                    low=6.27, chosen=[{"marker": "CD68"}])
    assert [(c["marker"], c["enrichment"]) for c in found] == [("Pan-CK", 0.07)]
    found = Engine._marker_controls(stub, None, "p", "Ki-67", [{"marker": "CD3"}], book,
                                    low=6.27, chosen=[{"marker": "CD3"}])
    assert [c["marker"] for c in found] == ["CD20", "Pan-CK"]


def test_a_split_that_wanders_when_the_cells_are_drawn_again_is_not_usable(monkeypatch):
    from plexora.plugins.gating.server.autogate import bivariate
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import profile as profmod

    ds = _lineages()
    values = cellmod.values(ds, "PD1")
    split, _d, body = bivariate._fit_within(profmod.column(ds, "PD1"), values,
                                            np.isfinite(values), 0)
    # A clear split barely moves on redrawing.
    assert bivariate._wander(body, split, resamples=10, sure=0.8, seed=0) < 0.1
    monkeypatch.setattr(bivariate, "_wander", lambda *a, **k: 0.64)
    fit = bivariate.carrier_fit(ds, "PD1", _gate(ds, "CD3", 5.5),
                                exclusives=[_gate(ds, "CD20", 5.5)])
    assert not fit["usable"] and fit["why"].startswith("the split wanders 0.64"), fit


def test_a_split_the_contamination_makes_is_not_usable():
    from plexora.plugins.gating.server.autogate import bivariate

    # PD-1 is one population in the T cells; B cells spilling CD3 carry it.
    ds = _lineages(t_split=False, spill=True)
    fit = bivariate.carrier_fit(ds, "PD1", _gate(ds, "CD3", 5.5),
                                exclusives=[_gate(ds, "CD20", 5.5)])
    assert not fit["usable"], fit
    raw = bivariate.carrier_fit(ds, "PD1", _gate(ds, "CD3", 5.5))
    # Uncleaned, the same cells would have looked like a split.
    assert raw["usable"] or raw.get("d", 0) > fit.get("d", 0)


def test_a_carrier_gate_closed_for_review_still_serves_and_a_failed_one_does_not():
    from plexora.plugins.gating.server.autogate.engine import Engine

    grade = Engine._carrier_grade
    # LSP23362 arm 4: CD3e closed for review with 6.0 / 6.27 left -- still the T cells.
    assert grade({"grade": "failed", "state": "manual_review_recommended", "gate": 6.0}) == "review"
    assert grade({"grade": "moderate", "state": "accepted", "gate": 6.6}) == "moderate"
    assert grade({"grade": "failed", "state": "technically_failed", "gate": 9.2}) is None
    assert grade({"grade": "failed", "state": "no_positive_population", "gate": 9.2}) is None
    assert grade({"grade": "pending", "state": "awaiting_panel", "gate": None}) is None
    assert grade({}) is None


# -- the start --------------------------------------------------------------------------


class _Engine:
    """Just what `Engine.carrier_start` reads."""

    def __init__(self, engine="v2", chosen=None):
        self.options = {"engine": engine, "seed": 0}
        self.chosen = chosen
        self.logged = []

    def carrier_populations(self, unit, ds, rule):
        if self.chosen is None:
            return None
        return {"listed": [{"marker": c["marker"], "source": "vocabulary"} for c in self.chosen],
                "chosen": [dict(c) for c in self.chosen], "skipped": []}

    def ledger(self, project, without=None):
        return {}

    def _excluded_lineages(self, ds, project, marker, book):
        return []

    def _marker_controls(self, ds, project, marker, listed, book, *, low=None, chosen=(),
                         bar=1.0):
        return []

    def clamp_to_judged(self, unit, ds, low, *, why):
        return low

    def log(self, **event):
        self.logged.append(event)


def _start(fits, *, engine="v2", chosen=True):
    from plexora.plugins.gating.server.autogate import bivariate, engine as engmod

    unit = {"project": "p", "marker": "PD-1", "gmm": 6.73,
            "start": {"source": "scoring", "low": 6.88}, "scoring": {"candidates": []}}
    carriers = [{"marker": m, "low": 6.0, "high": 9.0, "n": 1000, "grade": "high",
                 "source": "vocabulary"} for m in fits] if chosen else None
    fake = _Engine(engine, carriers)
    by_marker = dict(fits)
    original = bivariate.carrier_fit
    bivariate.carrier_fit = lambda ds, marker, carrier, **kw: dict(by_marker[carrier["marker"]])
    try:
        engmod.Engine.carrier_start(fake, unit, None)
    finally:
        bivariate.carrier_fit = original
    return unit


def test_the_start_is_the_median_of_the_whole_population_and_the_usable_splits():
    unit = _start({"CD3": {"usable": True, "low": 6.53}})
    # Today's start 6.88, the Auto gate 6.73, the split inside the carrier 6.53.
    assert unit["start"]["low"] == pytest.approx(6.73)
    assert unit["start"]["carriers"]["inputs"] == [6.88, 6.73, 6.53]
    assert [c["id"] for c in unit["scoring"]["candidates"]] == ["carrier:CD3"]
    two = _start({"CD3": {"usable": True, "low": 6.53}, "CD11c": {"usable": True, "low": 6.55}})
    assert two["start"]["low"] == pytest.approx((6.55 + 6.73) / 2)


def test_no_usable_split_or_engine_v1_leaves_the_start():
    unit = _start({"CD68": {"usable": False, "low": 6.24, "why": "moved on cleaning"}})
    assert unit["start"] == {"source": "scoring", "low": 6.88}
    assert unit["carriers"]["start"] == {"unchanged": "no carrier split usable"}
    assert unit["scoring"]["candidates"] == []
    v1 = _start({"CD3": {"usable": True, "low": 6.53}}, engine="v1")
    assert v1["start"]["low"] == 6.88 and "carriers" not in v1
    none = _start({}, chosen=False)
    assert none["start"]["low"] == 6.88 and "carriers" not in none


def test_a_carrier_split_is_a_scored_family_the_guard_band_keeps():
    from plexora.plugins.gating.server.autogate import lattice
    from plexora.plugins.gating.server.autogate.hypotheses import candidates

    assert "carrier" in lattice.SCORED and "carrier" in candidates.ESTIMATORS
    assert lattice.PRIORITY.index("within") < lattice.PRIORITY.index("carrier") \
        < lattice.PRIORITY.index("bio")


# -- what a look shows ------------------------------------------------------------------


def test_a_carrier_look_shows_carrier_negatives_and_anchors_and_no_cell_twice():
    from plexora.plugins.gating.server.autogate import profile as profmod
    from plexora.plugins.gating.server.autogate.hypotheses import panel

    ds = _lineages()
    col = profmod.column(ds, "PD1")
    edges = [float(col.from_fit(v)) for v in (4.4, 4.7, 5.0, 5.3)]
    intervals = [{"from": a, "to": b, "n": 6} for a, b in zip(edges[:-1], edges[1:])]
    plain = panel.panel_cells(ds, "PD1", intervals, n_sentinels=4, sentinel_below=edges[0],
                              seed=2)
    again = panel.panel_cells(ds, "PD1", intervals, n_sentinels=4, sentinel_below=edges[0],
                              seed=2)
    assert plain == again and "anchors" not in plain
    assert all("carrier" not in c for c in plain["cells"] + plain["sentinels"])
    out = panel.panel_cells(
        ds, "PD1", intervals, n_sentinels=4, sentinel_below=edges[0], seed=2,
        carriers=[_gate(ds, "CD3", 5.5)], n_carrier_negatives=2, n_anchors=2,
        anchor_above=float(col.from_fit(5.5)), anchor_pool=10, anchor_margin=0.2)
    negatives = [c for c in out["sentinels"] if c["side"] == "negative"]
    anchors = [c for c in out["sentinels"] if c.get("anchor")]
    assert len(out["cells"]) == 18 and len(negatives) == 4
    assert sum(c["carrier"] for c in negatives) == 2
    assert all("carrier" in c for c in out["cells"])
    if out["anchors"].get("why", "").startswith("no cell positions"):
        pytest.skip("the fake table has no neighbour features")
    assert len(anchors) == 2 and all(c["carrier"] == 1 for c in anchors)
    assert all(c["value"] > float(col.from_fit(5.5)) for c in anchors)
    ids = [c["cell_id"] for c in out["cells"] + out["sentinels"]]
    assert len(ids) == len(set(ids))
    # Too small a pool: no anchors, and why.
    few = panel.panel_cells(
        ds, "PD1", intervals, n_sentinels=4, sentinel_below=edges[0], seed=2,
        carriers=[_gate(ds, "CD3", 5.5)], n_carrier_negatives=2, n_anchors=2,
        anchor_above=float(col.from_fit(5.5)), anchor_pool=10 ** 6, anchor_margin=0.2)
    assert few["anchors"]["shown"] == 0 and "anchors need" in few["anchors"]["why"]


def _cells(labels, *, anchors=("not_own_stain", "not_own_stain"), evidence="own_stain"):
    cells = [{"number": str(i), "interval": 0, "value": 5.0, "label": evidence}
             for i in range(4)]
    cells += [{"number": f"n{i}", "sentinel": True, "side": "negative", "value": 3.0,
               "label": lab} for i, lab in enumerate(labels)]
    cells += [{"number": f"a{i}", "sentinel": True, "side": "positive", "anchor": True,
               "value": 7.0, "label": lab} for i, lab in enumerate(anchors)]
    return cells


def test_both_anchors_read_unstained_discard_a_look_once_any_stain_was_read():
    from plexora.plugins.gating.server.autogate.hypotheses import sentinel_check

    negatives = ["not_own_stain"] * 4
    valid, record = sentinel_check(_cells(negatives), 2, own_read=True)
    assert not valid and record["anchors_read_unstained"] == 2 and record["anchors_judged"]
    # A reader who has seen no stain anywhere: the anchors are the marker's
    # question (`positives_not_confirmed`), not a slip.
    valid, record = sentinel_check(_cells(negatives, evidence="not_own_stain"), 2,
                                   own_read=False)
    assert valid and record["anchors_judged"] is False
    # One anchor missed is not enough.
    assert sentinel_check(_cells(negatives, anchors=("own_stain", "not_own_stain")), 2,
                          own_read=True)[0]
    # A positive sentinel that is not an anchor is judged as before.
    plain = [dict(c, anchor=False) if c.get("anchor") else c for c in _cells(negatives)]
    assert not sentinel_check(plain, 2, own_read=False)[0]


def test_a_discarded_look_is_drawn_again_without_spending_a_look():
    from plexora.plugins.gating.server.autogate.hypotheses import controller

    state = {"observations": [{"valid": True}, {"valid": False}, {"valid": True}]}
    assert controller.looks_spent(state, {"redraw_invalid": True}) == 2
    assert controller.looks_spent(state, {"redraw_invalid": False}) == 3
    assert controller.looks_spent(state, {}) == 3


def test_an_anchor_above_the_leader_is_shown_but_never_read():
    from plexora.plugins.gating.server.autogate.hypotheses import controller

    state = {"observations": [{"valid": True, "cells": [
        {"value": 7.0, "label": "own_stain", "interval": 2},
        {"value": 7.5, "label": "not_own_stain", "sentinel": True, "side": "positive",
         "anchor": True},
        {"value": 3.0, "label": "not_own_stain", "sentinel": True, "side": "negative"}]}]}
    tail = controller.tail(state, {"low": 6.0, "positives": 10})
    assert tail == {"own": 1, "read": 1, "available": 8}


def test_new_rules_stay_off_for_bench_v5_arms():
    from plexora.plugins.gating.server.autogate import hypotheses

    assert hypotheses.PARAMS["carriers"] and hypotheses.PARAMS["redraw_invalid"]
    assert hypotheses.V5_RULES["carriers"] is None and hypotheses.V5_RULES["redraw_invalid"] \
        is False
