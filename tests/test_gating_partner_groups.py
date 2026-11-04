"""Partner groups and proposed partners: who a look shows beside a marker
carried by several lineages, or by lineages the vocabulary cannot name.

The pieces, each pinned on its own: `subset_any` in the vocabulary (two or
more, the note beside it); the panel context marking a group the panel
cannot complete; the partner plan drawing a group as one expected tile and
falling back when it is incomplete; the collage drawing it; the tree and the
references never reading a group or a proposal; the rules a planner's
proposal is held to; and, in a session, a marker with no partner put to the
planner once per panel and tissue.
"""

import numpy as np
import pytest

from plexora.agent import AgentSession, cell_exclusions, registry
from tests.autogate_fixtures import make_gating_project


@pytest.fixture(autouse=True)
def _plugins():
    registry.discover(["gating"])
    cell_exclusions._reset_for_tests()
    yield
    cell_exclusions._reset_for_tests()


def _entries(**partners):
    """A panel context's entries: {marker: [(partner, relation, confidence)]}."""
    out = {}
    for marker, rels in partners.items():
        out[marker] = {"marker": marker, "canonical": marker, "role": "lineage_other",
                       "partners": [{"marker": p, "relation": r, "confidence": c}
                                    for p, r, c in rels]}
    return out


# -- the vocabulary ---------------------------------------------------------------------


def test_the_vocabulary_names_groups_and_leaves_tissue_dependent_markers_to_the_planner():
    from plexora.ai import vocabulary

    vocabulary.clear_cache()
    entries = vocabulary.load()["entries"]
    tigit = {p["marker"] for p in entries["TIGIT"]["partners"] if p["relation"] == "subset_any"}
    assert tigit == {"CD3", "CD56"}
    # Which cells carry PD-L1 depends on the tumour: no partner, a note instead.
    assert entries["PDL1"]["partners"] == []
    assert "myeloid" in entries["PDL1"]["expression"]
    gated = [e for e in entries.values() if e.get("role") not in ("context",)]
    assert all(e.get("expression") for e in gated)
    with pytest.raises(ValueError, match="one subset_any partner is a subset"):
        vocabulary.check({"canonical": "X", "partners": [
            {"marker": "CD3", "relation": "subset_any", "confidence": "moderate"}]})


def test_a_panel_missing_a_lineage_of_a_group_says_so():
    from plexora.plugins.gating.server.autogate import context

    whole = context.build(["CD3", "CD56", "TIGIT"])
    assert "subset_any_missing" not in whole["entries"]["TIGIT"]
    part = context.build(["CD3", "TIGIT"])
    assert part["entries"]["TIGIT"]["subset_any_missing"] == ["CD56"]
    assert {(p["marker"], p["relation"]) for p in part["entries"]["TIGIT"]["partners"]
            if p["relation"] == "subset_any"} == {("CD3", "subset_any")}


# -- the partner plan --------------------------------------------------------------------


def test_a_group_is_one_expected_tile_and_an_incomplete_one_falls_back():
    from plexora.plugins.gating.server.autogate import partners

    entries = _entries(
        TIM3=[("CD45", "subset", "moderate"), ("CD3", "subset_any", "moderate"),
              ("CD68", "subset_any", "moderate"), ("PanCK", "exclusive", "high")],
        CD3=[("CD45", "subset", "high")], CD68=[("CD45", "subset", "moderate")],
        CD45=[], PanCK=[("CD3", "exclusive", "high")])
    present = {"CD3", "CD68", "CD45", "PanCK"}
    plan = partners.partner_plan(entries, "TIM3", present, {}, 1, gated={"CD68"})
    co = [p for p in plan if p["kind"] == "co"]
    assert len(co) == 1 and co[0]["relation"] == "subset_any"
    # Gated members first; the caption names them all.
    assert co[0]["members"] == ["CD68", "CD3"] and co[0]["marker"] == "CD68 or CD3"
    assert [p["marker"] for p in plan if p["kind"] == "anti"] == ["PanCK"]
    # With room for two, the pair after the group is never one of its members.
    two = partners.partner_plan(entries, "TIM3", present, {}, 2)
    assert [p["marker"] for p in two if p["kind"] == "co"] == ["CD3 or CD68", "CD45"]
    # A member with no image channel, or a lineage the panel lacks: the
    # group would draw that lineage's positives dark, so the pairs stand in.
    for broken, where in ((present - {"CD68"}, entries),
                          (present, {**entries, "TIM3": {**entries["TIM3"],
                                                         "subset_any_missing": ["CD56"]}})):
        plan = partners.partner_plan(where, "TIM3", broken, {}, 1)
        assert [p["marker"] for p in plan if p["kind"] == "co"] == ["CD45"]
        assert all("members" not in p for p in plan)


def test_the_tree_and_the_references_never_read_a_group_or_a_proposal():
    from plexora.plugins.gating.server.autogate import context, hierarchy

    panel = {"markers": ["PDL1", "CD3", "CD68", "CD20", "TIM3"], "entries": _entries(
        PDL1=[("CD68", "subset", "moderate")],
        TIM3=[("CD3", "subset_any", "moderate"), ("CD68", "subset_any", "moderate")],
        CD3=[("CD20", "exclusive", "high")], CD68=[], CD20=[])}
    panel["entries"]["PDL1"]["partners"][0]["proposed"] = True
    assert hierarchy.relations(panel, "PDL1") == []
    assert {r["marker"] for r in hierarchy.relations(panel, "TIM3")} == set()
    assert hierarchy.build(panel)["nodes"]["PDL1"]["parents"] == []
    assert context.references_for(panel, "PDL1", {"CD68": "high"}) == []


# -- the planner's proposals ---------------------------------------------------------------


def test_a_proposal_is_held_to_the_rules_a_prompt_cannot_keep():
    from plexora.plugins.gating.server.autogate import context

    panel = context.build(["CD45", "CD3", "CD20", "CD68", "CD11c", "PanCK", "PD-L1", "DNA"])
    kept, refused = context.check_proposal(panel, "PD-L1", [
        {"marker": "CD68", "relation": "subset_any", "confidence": "high",
         "basis": "mostly myeloid: macrophages"},
        {"marker": "CD11c", "relation": "subset_any", "basis": "dendritic cells"},
        {"marker": "PanCK", "relation": "subset_any", "basis": "tumour cells in some cancers"},
        {"marker": "CD45", "relation": "exclusive", "basis": "not immune"},
        {"marker": "CD20", "relation": "exclusive", "basis": "B cells, weakly"},
        {"marker": "CD3", "relation": "subset", "basis": ""},
        {"marker": "DNA", "relation": "subset", "basis": "every cell"},
        {"marker": "CD68", "relation": "coexpressed", "basis": "again"}])
    assert [(p["marker"], p["relation"], p["confidence"]) for p in kept] == [
        ("CD68", "subset_any", "moderate"), ("CD11c", "subset_any", "low"),
        ("PanCK", "subset_any", "low"), ("CD20", "exclusive", "low")]
    why = {r["marker"]: r["reason"] for r in refused}
    assert "parent" in why["CD45"]            # CD68 and CD11c sit within CD45
    assert "basis" in why["CD3"]
    assert "panel" in why["DNA"]              # a counterstain is no partner
    assert sum(r["marker"] == "CD68" for r in refused) == 1
    # Two subsets that exclude each other cannot both hold; a lone group is a subset.
    kept, refused = context.check_proposal(panel, "PD-L1", [
        {"marker": "CD3", "relation": "subset", "basis": "T cells"},
        {"marker": "CD20", "relation": "subset", "basis": "B cells"}])
    assert kept == [] and {r["marker"] for r in refused} == {"CD3", "CD20"}
    kept, _ = context.check_proposal(panel, "PD-L1", [
        {"marker": "CD68", "relation": "subset_any", "basis": "macrophages"}])
    assert kept[0]["relation"] == "subset"


def test_tiles_only_proposals_fill_empty_slots_per_context_and_nothing_else():
    from plexora.plugins.gating.server.autogate import biology, context

    panel = context.build(["CD45", "CD3", "CD68", "PanCK", "PD-L1", "Ki67", "DAPI"])
    assert context.empty_slots(panel, "PD-L1") == {"co", "anti"}
    assert context.empty_slots(panel, "CD45") == {"co"}            # it excludes Pan-CK
    assert context.empty_slots(panel, "CD68") == set()
    assert "DAPI" not in context.review_markers(panel, None, panel["markers"])
    crc = biology.resolve("colorectal", "cancer")
    melanoma = biology.resolve(disease="melanoma")
    assert context.proposal_key(crc) != context.proposal_key(melanoma)
    group = [{"marker": "CD68", "relation": "subset_any", "confidence": "low", "basis": "b"},
             {"marker": "PanCK", "relation": "subset_any", "confidence": "low", "basis": "b"}]
    context.store_proposals(panel, crc, {
        "PD-L1": group,
        # A filled slot is never drawn from a proposal: CD68 has its partners.
        "CD68": [{"marker": "CD3", "relation": "coexpressed", "confidence": "low",
                  "basis": "b"}]})
    over = biology.overlay(panel, crc)
    laid = over["entries"]["PD-L1"]["partners"]
    assert [p["marker"] for p in laid] == ["CD68", "PanCK"] and all(p["proposed"] for p in laid)
    assert all(not p.get("proposed") for p in over["entries"]["CD68"]["partners"])
    assert panel["entries"]["PD-L1"]["partners"] == []          # the stored panel is untouched
    assert biology.overlay(panel, melanoma)["entries"]["PD-L1"]["partners"] == []


# -- the picture ---------------------------------------------------------------------------


def test_a_group_draws_as_one_tile_lit_where_any_member_is(tmp_path):
    from plexora.agent.evidence import collage
    from plexora.plugins.gating.server.autogate import cells as cellmod

    make_gating_project(tmp_path, grid=16, size=640)
    session = AgentSession()
    try:
        ds = session.data("gsynth")
        c = cellmod.cells(ds)
        picked = [{"cell_id": int(c.ids[i]), "x": float(c.xs[i]), "y": float(c.ys[i]),
                   "value": 1.0} for i in range(0, 60, 10)]
        rows = [{"label": "a", "key": "a", "cells": picked}]

        def draw(co):
            return collage.render_collage(session, ds, layout="t2m", rows=rows, marker="CD8",
                                          gate=1.5, partners={"co": co, "gates": {}},
                                          fmt="png", store=False)

        both, one, other = draw(["CD3", "CD20"]), draw("CD3"), draw("CD20")
        assert both["manifest"]["request"]["partners"]["co"] == ["CD3", "CD20"]
        assert set(both["manifest"]["partner_windows"]) == {"CD3", "CD20"}
        tile = both["manifest"]["tile_px"]

        def co_tiles(out):
            from PIL import Image
            import io

            img = np.asarray(Image.open(io.BytesIO(out["png"])).convert("RGB")).astype(int)
            y = collage.HEADER_H + collage.ROW_HEADER_H + tile
            return img[y:y + tile, 0:tile]

        # Each pixel as bright as the brighter member draws it: lit where any is.
        assert np.array_equal(co_tiles(both), np.maximum(co_tiles(one), co_tiles(other)))
        assert co_tiles(both).sum() >= max(co_tiles(one).sum(), co_tiles(other).sum())
        assert collage.rerender(session, ds, both["manifest"])["png"] == both["png"]
    finally:
        session.close()


# -- in a session --------------------------------------------------------------------------


@pytest.mark.pro
def test_every_session_reviews_its_panel_once_and_fills_empty_slots(tmp_path, monkeypatch):
    """No tissue named: the review still runs, and PD-L1 (no partner) gets
    its expected tile. Unsure, the proposal draws the tile only; the same
    panel is not asked again."""
    from plexora.agent import invoke, jobs
    from plexora.ai import vocabulary
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import hierarchy
    from tests import autogate_fixtures
    from tests.test_gating_session import answer, next_packet, ok

    monkeypatch.setitem(autogate_fixtures.MARKER_LEVELS, "PDL1",
                        autogate_fixtures.MARKER_LEVELS["CD20"])
    vocabulary.clear_cache()
    make_gating_project(tmp_path, markers=("CD3", "CD20", "PDL1"))
    session = AgentSession()
    reply = {"kind": "partner_review", "entries": [{"marker": "PDL1", "partners": [
        {"marker": "CD3", "relation": "subset_any", "confidence": "moderate",
         "basis": "activated T cells"},
        {"marker": "CD20", "relation": "subset_any", "confidence": "moderate",
         "basis": "activated B cells"},
        {"marker": "CD3", "relation": "exclusive", "confidence": "low", "basis": "twice"}]}]}
    try:
        started = ok(invoke(session, "gating_session_start", {
            "engine": "v1", "scope": "project", "project": "gsynth"}))
        jobs.drain(120)
        sid = started["session_id"]
        assert started["partner_review"] == {"markers": 3, "packets": 2}
        for _ in range(2):
            packet, _ = next_packet(session, sid)
            assert packet["kind"] == "partner_review"
            pdl1 = next(m for m in packet["evidence"]["markers"] if m["marker"] == "PDL1")
            assert pdl1["empty"] == ["anti", "co"] and pdl1["expression"]
            assert packet["evidence"]["sample"] == {"named": "no tissue or disease was named"}
            assert "inferred_context" in packet["question"]
            outcome = answer(session, sid, packet, reply)["outcome"]
            assert [r["partner"] for r in outcome["refused"]] == ["CD3"]
        settled = outcome["settled"]
        assert settled["applied"] == []
        assert settled["tiles_only"] == {"PDL1": ["subset_any CD3", "subset_any CD20"]}
        assert settled["context"] == "none: only empty slots could be filled"
        with engines.engine_for(session_call(session), sid, save=False) as engine:
            unit = next(u for u in engine.record["units"].values() if u["marker"] == "PDL1")
            shown = engine.partner_panels(unit)
            panel = engine.panel_for("gsynth")
        assert set(shown["co"]) == {"CD20", "CD3"} and " or " in shown["markers"]["co"]
        # Tiles only: no relation, reference or score reads them.
        assert hierarchy.relations(panel, "PDL1") == []
        ok(invoke(session, "gating_session_finish", {"session_id": sid, "action": "close"}))
        again = ok(invoke(session, "gating_session_start", {
            "engine": "v1", "scope": "project", "project": "gsynth"}))
        jobs.drain(120)
        assert "partner_review" not in again
        first, _ = next_packet(session, again["session_id"])
        assert first is None or first["kind"] != "partner_review"
    finally:
        session.close()


@pytest.mark.pro
def test_a_named_context_may_change_a_stated_relation_when_both_draws_are_sure(tmp_path):
    from plexora.agent import invoke, jobs
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import hierarchy
    from tests.test_gating_session import answer, next_packet, ok

    make_gating_project(tmp_path, markers=("CD3", "CD8", "CD20"))
    session = AgentSession()
    biology = {"tissue": "colorectal", "disease": "cancer"}
    sure = {"kind": "partner_review", "entries": [{"marker": "CD8", "checks": [
        {"marker": "CD20", "verdict": "does_not_hold_here", "confidence": "high",
         "basis": "in this colorectal cancer the B-cell aggregates carry CD8"}]}]}
    try:
        started = ok(invoke(session, "gating_session_start", {
            "engine": "v1", "scope": "project", "project": "gsynth", "biology": biology}))
        jobs.drain(120)
        sid = started["session_id"]
        assert started["partner_review"] == {"markers": 3, "packets": 2}
        drawn = []
        for _ in range(2):
            packet, _ = next_packet(session, sid)
            assert packet["kind"] == "partner_review"
            drawn.append(packet)
            outcome = answer(session, sid, packet, sure)["outcome"]
        orders = [[m["marker"] for m in p["evidence"]["markers"]] for p in drawn]
        assert orders[1] == orders[0][::-1] and [p["evidence"]["draw"] for p in drawn] == [1, 2]
        assert drawn[0]["evidence"]["sample"]["said"]["tissue"] == "colorectal"
        assert "inferred_context" not in drawn[0]["question"]
        cd8 = next(m for m in drawn[0]["evidence"]["markers"] if m["marker"] == "CD8")
        assert {"marker": "CD20", "relation": "exclusive", "confidence": "high"} in \
            cd8["partners"] and cd8["expression"]
        assert outcome["settled"]["applied"][0].startswith("drop exclusive CD8 ~ CD20")
        assert outcome["settled"]["context"]["source"] == "user"
        status = ok(invoke(session, "gating_session_status",
                           {"detail": "full", "session_id": sid}))
        assert status["partner_review"]["applied"] == outcome["settled"]["applied"]
        with engines.engine_for(session_call(session), sid, save=False) as engine:
            panel = engine.panel_for("gsynth")
            units = {u["marker"]: u for u in engine.record["units"].values()}
        # Dropped, and not derived back through CD3's exclusion of CD20.
        assert all(r["marker"] != "CD20" for r in hierarchy.relations(panel, "CD8"))
        assert units["CD8"]["context_changes"] == ["drop exclusive CD8 ~ CD20"]
        ok(invoke(session, "gating_session_finish", {"session_id": sid, "action": "close"}))
        again = ok(invoke(session, "gating_session_start", {
            "engine": "v1", "scope": "project", "project": "gsynth", "biology": biology}))
        jobs.drain(120)
        assert "partner_review" not in again
        status = ok(invoke(session, "gating_session_status",
                           {"detail": "full", "session_id": again["session_id"]}))
        assert status["partner_review"]["applied"] == outcome["settled"]["applied"]
    finally:
        session.close()


# -- the review's rules ----------------------------------------------------------------------


def _review(panel, record, draws, inferred=(None, None)):
    """Run a review over `draws` ([{marker: {checks, partners}}] raw answers,
    one per draw; `inferred` each draw's context read off the panel) and
    settle it."""
    from plexora.plugins.gating.server.autogate import context

    markers = context.review_markers(panel, record, panel["markers"])
    for draw, answers, read in zip(context.REVIEW_DRAWS, draws, inferred):
        checked = {}
        for marker, said in answers.items():
            checks, _ = context.check_review(panel, marker, said.get("checks") or [])
            adds, _ = context.check_proposal(panel, marker, said.get("partners") or [])
            checked[marker] = {"checks": checks, "partners": adds}
        context.store_review_draw(panel, record, draw, checked, markers, inferred=read)
    return context.settle_review(panel, record)


def _add(marker, partner, relation, confidence="high", basis="macrophages"):
    return {marker: {"partners": [{"marker": partner, "relation": relation,
                                   "confidence": confidence, "basis": basis}]}}


def _drop(marker, partner, confidence="high", basis="colorectal cancer breaks it"):
    return {marker: {"checks": [{"marker": partner, "verdict": "does_not_hold_here",
                                 "confidence": confidence, "basis": basis}]}}


def test_a_review_change_is_made_only_when_both_draws_are_sure_in_this_context():
    from plexora.plugins.gating.server.autogate import biology, context

    record = biology.resolve(notes="colorectal cancer")
    assert context.context_words(record) >= {"colorectal", "cancer"}
    for draws in ([_drop("CD68", "Pan-CK"), {}],                          # one draw alone
                  [_drop("CD68", "Pan-CK"), _drop("CD68", "CD3e")],       # they disagree
                  [_drop("CD68", "Pan-CK", "moderate")] * 2,              # not sure
                  [_drop("CD68", "Pan-CK", basis="macrophages vary")] * 2):  # not this context
        panel = context.build(["CD45", "CD3e", "CD68", "Pan-CK", "CD20"])
        settled = _review(panel, record, draws)
        assert not any(settled["changes"].values()), draws
        assert settled["unsure"]
    panel = context.build(["CD45", "CD3e", "CD68", "Pan-CK", "CD20"])
    settled = _review(panel, record, [_drop("CD68", "Pan-CK")] * 2)
    assert [(c["marker"], c["partner"]) for c in settled["changes"]["drop"]] == [
        ("CD68", "Pan-CK")]
    assert settled["context"]["source"] == "user"


def test_without_a_context_only_empty_slots_are_filled():
    from plexora.plugins.gating.server.autogate import context

    panel = context.build(["CD45", "CD3e", "CD68", "Pan-CK", "CD20", "PD-L1"])
    # CD68's not-expected slot is full (CD3e, Pan-CK): one beside them needs a
    # context, as does the drop.
    cd68 = {**_drop("CD68", "Pan-CK")["CD68"], **_add("CD68", "CD20", "exclusive")["CD68"]}
    both = {"CD68": cd68, **_add("PD-L1", "CD68", "subset")}
    settled = _review(panel, None, [both] * 2)
    assert settled["context"] is None
    assert settled["changes"]["drop"] == [] and [
        (c["marker"], c["partner"]) for c in settled["changes"]["add"]] == [("PD-L1", "CD68")]
    unsure = {(u["marker"], u["partner"]) for u in settled["unsure"]}
    assert {("CD68", "Pan-CK"), ("CD68", "CD20")} <= unsure


def test_a_context_inferred_from_the_panel_counts_and_says_so():
    from plexora.plugins.gating.server.autogate import biology, context

    # The planner read it off the panel, both draws alike, at high.
    crc = {"tissue": "colon", "disease": "colorectal carcinoma", "confidence": "high",
           "basis": "Pan-CK, CDX2"}
    panel = context.build(["CD45", "CD3e", "CD68", "Pan-CK", "CD20"])
    settled = _review(panel, None, [_drop("CD68", "Pan-CK", basis="colorectal carcinoma")] * 2,
                      inferred=(crc, crc))
    assert settled["changes"]["drop"] and "planner" in settled["context"]["source"]
    # The two draws read different contexts, or one was not sure: none.
    for pair in ((crc, {**crc, "tissue": "lung"}), (crc, {**crc, "confidence": "moderate"})):
        panel = context.build(["CD45", "CD3e", "CD68", "Pan-CK", "CD20"])
        settled = _review(panel, None, [_drop("CD68", "Pan-CK", basis="colorectal")] * 2,
                          inferred=pair)
        assert settled["context"] is None and not settled["changes"]["drop"]
    # The panel's own indicators (SOX10 and MART1: melanoma).
    panel = context.build(["CD45", "CD3e", "CD68", "Pan-CK", "SOX10", "MART1"])
    record = biology.infer(panel)
    assert record["source"] == "inferred" and record["contexts"] == ["melanoma"]
    settled = _review(panel, record, [_drop("CD68", "Pan-CK", basis="in melanoma")] * 2)
    assert settled["changes"]["drop"] and settled["context"]["source"].startswith("inferred")


def test_a_sure_drop_is_not_derived_back_and_a_sure_addition_is_evidence():
    from plexora.plugins.gating.server.autogate import biology, context, hierarchy, partners

    record = biology.resolve(notes="colorectal cancer")
    panel = context.build(["CD45", "CD3e", "CD68", "CD11c", "Pan-CK", "PD-L1"])
    add = {"PD-L1": {"partners": [
        {"marker": "CD68", "relation": "subset", "confidence": "high",
         "basis": "in colorectal cancer PD-L1 sits on macrophages"}]}}
    _review(panel, record, [{**_drop("CD68", "Pan-CK"), **add}] * 2)
    over = biology.overlay(panel, record)
    # CD68 within CD45 and CD45 exclusive of Pan-CK would derive it back.
    assert all(r["marker"] != "Pan-CK" for r in hierarchy.relations(over, "CD68"))
    plan = partners.partner_plan(over["entries"], "CD68", set(over["markers"]) - {"CD68"}, {}, 2,
                                 dropped=context.dropped_pairs(over))
    assert "Pan-CK" not in [p["marker"] for p in plan if p["kind"] == "anti"]
    rel = next(r for r in hierarchy.relations(over, "PD-L1") if r["marker"] == "CD68")
    assert (rel["relation"], rel["confidence"], rel["derived"]) == ("subset", "moderate", False)
    assert context.references_for(over, "PD-L1", {"CD68": "high"})[0]["marker"] == "CD68"
    # Another context sees the vocabulary as it is.
    other = biology.resolve(disease="melanoma")
    assert any(r["marker"] == "Pan-CK"
               for r in hierarchy.relations(biology.overlay(panel, other), "CD68"))


def test_what_a_person_stated_is_never_reviewed_and_a_rewrite_changes_nothing():
    from plexora.plugins.gating.server.autogate import biology, context

    record = biology.resolve(notes="colorectal cancer")
    panel = context.build(["CD45", "CD3e", "CD68", "Pan-CK", "CD20"], overrides={
        "CD68": {"partners": [{"marker": "Pan-CK", "relation": "exclusive"}],
                 "source": "user"}})
    assert context.reviewable(panel, "CD68") == []
    kept, refused = context.check_review(panel, "CD68", _drop("CD68", "Pan-CK")["CD68"]["checks"])
    assert kept == [] and "vocabulary" in refused[0]["reason"]
    # Five sure drops on a panel of few relations: a rewrite, so none is made.
    panel = context.build(["CD45", "CD3e", "CD68", "Pan-CK", "CD20"])
    many = {**_drop("CD68", "Pan-CK"), **_drop("CD20", "CD3e"), **_drop("CD3e", "Pan-CK"),
            **_drop("CD45", "Pan-CK")}
    many["CD68"]["checks"].append({"marker": "CD3e", "verdict": "does_not_hold_here",
                                   "confidence": "high", "basis": "colorectal"})
    settled = _review(panel, record, [many] * 2)
    assert not any(settled["changes"].values()) and "none made" in settled["note"]


def test_a_proposal_never_restates_and_ranks_after_every_stated_partner():
    from plexora.plugins.gating.server.autogate import context, partners

    panel = context.build(["CD45", "CD3e", "CD68", "Pan-CK", "CD20"])
    kept, refused = context.check_proposal(panel, "CD68", [
        {"marker": "Pan-CK", "relation": "coexpressed", "basis": "tumour cells"},
        {"marker": "CD20", "relation": "exclusive", "basis": "B cells"}])
    assert [p["marker"] for p in kept] == ["CD20"]
    assert "already states" in refused[0]["reason"]
    entries = _entries(
        CD68=[("CD45", "subset", "moderate"), ("CD3", "exclusive", "low")],
        CD3=[], CD45=[], CD20=[])
    entries["CD68"]["partners"].append({"marker": "CD20", "relation": "exclusive",
                                        "confidence": "moderate", "proposed": True})
    plan = partners.partner_plan(entries, "CD68", {"CD45", "CD3", "CD20"}, {}, 1, gated={"CD20"})
    assert [p["marker"] for p in plan if p["kind"] == "anti"] == ["CD3"]


def session_call(session):
    """A call for reading a session's engine outside a tool."""
    from types import SimpleNamespace

    return SimpleNamespace(session=session)
