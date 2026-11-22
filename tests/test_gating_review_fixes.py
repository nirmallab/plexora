"""Two fixes from the owner's review of lsp35766_p193 (2026-11-22).

- A stain its looks could not read -- most cells `uncertain` or `artifact`,
  a stain artifact named -- is shown on the whole image before it closes for
  review (`replan.check_unreadable`): a failed or empty answer closes it so,
  any other keeps the review close it was about to make. A crowded stain
  that works (CD3E) is not sent there. (Owner: "panCK and NKG2A failed, but
  autogating picked them up".)
- A marker with no expected partner gets a supporting tile: the panel's
  lineage markers whose positives sit inside its own (`partners._supports`),
  read as support only. (Owner: "for IBA1 no co-expression markers were
  used".)
"""

from __future__ import annotations

import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from tests.autogate_fixtures import make_gating_project
from tests.test_gating_replan import _Engine, _u

# -- 1. a stain the looks could not read ---------------------------------------------------


def _look(own=0, not_own=0, uncertain=0, artifact=0, valid=True):
    """One cell-panel look: its numbered cells' labels, and four sentinels read
    clear (they never count toward the reads)."""
    labels = (["own_stain"] * own + ["not_own_stain"] * not_own + ["uncertain"] * uncertain
              + ["artifact"] * artifact)
    cells = [{"number": str(i + 1), "label": label, "value": 6.0, "interval": 0}
             for i, label in enumerate(labels)]
    cells += [{"number": f"s{i}", "label": "not_own_stain", "value": 4.0, "sentinel": True}
              for i in range(4)]
    return {"valid": valid, "cells": cells}


#: The looks of lsp35766_p193's markers (own / not own / uncertain / artifact).
NKG2A = [_look(0, 3, 7, 10), _look(0, 2, 1, 17), _look(0, 0, 1, 14)]
PANCK = [_look(4, 2, 14, 0), _look(5, 2, 13, 0)]
CD3E = [_look(13, 0, 7, 0), _look(2, 2, 4, 0), _look(3, 0, 8, 1)]
CD8A = [_look(5, 1, 14, 0), _look(5, 1, 14, 0), _look(5, 0, 13, 2)]
IBA1 = [_look(0, 11, 9, 0), _look(1, 9, 10, 0), _look(2, 4, 8, 0)]


def _unit(marker, looks, flags=(), **extra):
    return _u(marker, "awaiting_panel", candidate=6.33, seen=[4.0, 9.0],
              artifact_flags=list(flags),
              hypotheses={"leader": 6.82, "start": 6.33, "observations": looks, "support": {}},
              **extra)


@pytest.mark.parametrize("looks, flags, routed", [
    (NKG2A, ("autofluorescence", "uneven_staining"), "artifact_reads"),
    (NKG2A, (), "artifact_reads"),            # artifact reads need no flag: they are the picture's
    (PANCK, ("uneven_staining",), "unread_stain"),
    (PANCK, (), None),                        # uncertain alone, nothing named: crowded, not failed
    (CD3E, (), None),                         # a crowded stain that works
    (CD8A, ("segmentation",), None),          # an outline's fault, not the stain's
    (IBA1, (), None)])
def test_reads_that_could_not_read_a_stain_ask_for_the_picture(looks, flags, routed):
    from plexora.plugins.gating.server.autogate import replan

    why = replan.unreadable(_unit("PD1", looks, flags))
    assert (why or {}).get("why") == routed
    if routed:
        assert why["words"].startswith("its looks read ")


@pytest.mark.parametrize("verdict, state", [
    ("technical_failure", "technically_failed"),
    ("no_positive_population", "no_positive_population"),
    ("real_signal", "manual_review_recommended"),
    ("cannot_tell", "manual_review_recommended")])
def test_a_stain_its_looks_could_not_read_is_shown_whole_before_it_closes(verdict, state):
    """NKG2A's looks called most of its cells artifact and ran out: instead of
    closing for review at a gate with 22-80 % of cells positive, it goes to
    one whole-image check. A failed or empty answer closes it so; anything
    else makes the review close it was about to make, at the gate it would
    have had."""
    from plexora.plugins.gating.server.autogate import answers, hypotheses, replan, transitions

    class _Checking(_Engine):
        def close(self, unit, state, reason, *, proposed=None, confidence=None, write=False):
            unit["proposed"] = proposed
            super().close(unit, state, reason, proposed=proposed, confidence=confidence)

    pd1 = _unit("PD1", NKG2A, ("autofluorescence", "uneven_staining"))
    engine = _Checking([_u("CD45", "accepted", confidence="high"), pd1])
    hypotheses.finish(engine, pd1, "budget_exhausted")
    assert pd1["state"] == "qc_confirm" and not engine.closed
    assert pd1["qc_reason"][0].startswith("its looks read 0 of 55 cells")
    assert pd1["unreadable_check"]["state"] == "manual_review_recommended"
    assert [e["event"] for e in engine.events if e["event"].startswith("unreadable")] \
        == ["unreadable_whole_image"]
    transitions.apply_qc(engine, {"units": [{"project": "p", "marker": "PD1"}]},
                         answers.QCAnswer(verdict=verdict))
    assert pd1["state"] == state and pd1["whole_image"] == verdict
    if state == "manual_review_recommended":
        assert pd1["reason"].startswith("cell panels: the looks allowed were spent")
        assert "a whole-image check of its unread stain" in pd1["reason"]
        assert pd1["proposed"] == 6.82                     # where its looks left it
        assert "unreadable_check" not in pd1
    # Once per marker: a later close of this unit is never held again.
    pd1["state"] = "awaiting_panel"
    assert not replan.check_unreadable(engine, pd1, "manual_review_recommended", "again", 6.8)


def test_a_crowded_stain_that_works_closes_for_review_without_the_picture():
    """CD3E on that run: own stain on 18 of 40 read cells, the rest mostly
    uncertain in a crowded T-cell zone, no stain artifact named. Its review
    close is made as before; no whole-image check is spent on it."""
    from plexora.plugins.gating.server.autogate import hypotheses

    cd3 = _unit("CD3e", CD3E)
    engine = _Engine([cd3])
    hypotheses.finish(engine, cd3, "budget_exhausted")
    assert cd3["state"] == "manual_review_recommended" and "unreadable_check" not in cd3
    assert engine.closed == [("CD3e", "manual_review_recommended")]


def test_clear_regions_after_a_failed_answer_keep_the_review_close_never_reopen_the_looks():
    """With the absence check on, a `technical_failure` answer is first put to
    the regions; regions that show clear positives say the stain worked --
    the marker is then closed for review as its looks left it, not sent back
    for looks it has already spent."""
    from plexora.plugins.gating.server.autogate import answers, hypotheses, transitions

    pd1 = _unit("PD1", NKG2A, ("autofluorescence",))
    engine = _Engine([pd1], options={"absence_check": True})
    hypotheses.finish(engine, pd1, "unreadable")
    transitions.apply_qc(engine, {"units": [{"project": "p", "marker": "PD1"}]},
                         answers.QCAnswer(verdict="technical_failure"))
    assert pd1["state"] == "awaiting_absence"
    transitions._qc_real_signal(engine, pd1, overruled="technically_failed")
    assert pd1["state"] == "manual_review_recommended"
    assert engine.closed == [("PD1", "manual_review_recommended")]


class _FailedStain:
    """An `Oracle` that reads one marker as a failed stain: every numbered cell
    `artifact`, autofluorescence named, its sentinels read right; and answers
    that marker's whole-image check with `verdict`."""

    def __init__(self, oracle, marker, verdict):
        self.oracle, self.marker, self.verdict = oracle, marker, verdict
        self.checked = []

    def answer(self, packet):
        from plexora.plugins.gating.server.autogate import engine as engines

        marker = packet["units"][0]["marker"] if packet["units"] else None
        if marker == self.marker and packet["kind"] == "cell_panel":
            stored, _ = engines.store().read_packet(packet["session_id"], packet["packet_id"])
            return {"kind": "cell_panel", "artifact_flags": ["autofluorescence"], "cells": {
                c["number"]: "not_own_stain" if c.get("sentinel") else "artifact"
                for c in stored["_panel"]["cells"]}}
        if marker == self.marker and packet["kind"] == "qc_confirm":
            self.checked.append(packet["question"])
            return {"kind": "qc_confirm", "verdict": self.verdict}
        return self.oracle.answer(packet)


@pytest.mark.pro
@pytest.mark.parametrize("verdict, state", [("technical_failure", "technically_failed"),
                                            ("real_signal", "manual_review_recommended")])
def test_a_session_shows_a_failed_stain_whole_and_closes_it_on_the_answer(tmp_path, verdict,
                                                                          state):
    from plexora.plugins.gating.server.autogate import engine as engines
    from tests.test_gating_session import Oracle, answer_review, drive, ok

    registry.discover(["gating"])
    # CD4's populations overlap (`HARD`): the Auto gate is not accepted, so it
    # is looked at with cell panels.
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=("CD3", "CD4"))
    session = AgentSession()
    started = ok(invoke(session, "gating_session_start", {
        "scope": "project", "project": "gsynth", "markers": ["CD3", "CD4"], "engine": "v3"}))
    jobs.drain(120)
    answer_review(session, started)
    agent = _FailedStain(Oracle(info), "CD4", verdict)
    seen = drive(session, started["session_id"], agent, limit=80)
    cd4 = engines.store().load(started["session_id"])["units"]["gsynth::CD4"]
    assert len(agent.checked) == 1 and "its looks read 0 of" in agent.checked[0]
    kinds = [p["kind"] for p in seen if p["units"] and p["units"][0]["marker"] == "CD4"]
    assert kinds.index("qc_confirm") > kinds.index("cell_panel")
    assert cd4["state"] == state and cd4["whole_image"] == verdict
    if state == "manual_review_recommended":
        assert cd4["final"] is not None and cd4["needs_review"]
        assert "a whole-image check of its unread stain called its stain real" in cd4["reason"]


# -- 2. a supporting tile ---------------------------------------------------------------------


def _entries(**partners):
    out = {}
    for marker, rels in partners.items():
        out[marker] = {"marker": marker, "canonical": marker, "role": "lineage_other",
                       "partners": [{"marker": p, "relation": r, "confidence": c,
                                     **({"proposed": True} if proposed else {})}
                                    for p, r, c, *proposed in rels]}
    return out


def _myeloid(**more):
    return _entries(
        IBA1=[("CD20", "exclusive", "high"), ("CD3E", "exclusive", "high")],
        CD163=[("IBA1", "subset", "moderate"), ("CD3E", "exclusive", "high")],
        CD206=[("CD163", "subset_any", "moderate"), ("CD11c", "subset_any", "moderate"),
               ("CD31", "subset_any", "moderate"), ("IBA1", "subset_any", "moderate")],
        CD11b=[("CD19", "exclusive", "moderate")],
        CD66b=[("CD11b", "subset", "moderate")],
        CD11c=[("HLADRA", "subset", "moderate")],
        CD19=[("HLADRA", "subset", "moderate"), ("CD11b", "exclusive", "moderate")],
        HLADRA=[], cKit=[], CD20=[], CD3E=[], CD31=[], **more)


def _co(entries, marker, gated=()):
    from plexora.plugins.gating.server.autogate.partners import partner_plan

    present = set(entries) - {marker}
    return next((p for p in partner_plan(entries, marker, present, {}, 1, gated=set(gated))
                 if p["kind"] == "co"), None)


def test_a_marker_with_no_expected_partner_is_shown_what_sits_inside_it_as_support():
    """IBA1 has only exclusive partners: its supporting tile is CD163 (inside
    it), not CD206 (also on dendritic cells and endothelium, which IBA1 does
    not mark). CD11b gets CD66b; HLA-DR both CD11c and CD19 as one tile; cKit,
    with no mast-cell partner on the panel, none. A marker with an expected
    partner keeps it."""
    entries = _myeloid()
    assert _co(entries, "IBA1") == {"marker": "CD163", "kind": "co", "relation": "supports",
                                    "confidence": "moderate", "steps": 1}
    assert _co(entries, "CD11b")["marker"] == "CD66b"
    hla = _co(entries, "HLADRA")
    assert hla["relation"] == "supports" and set(hla["members"]) == {"CD11c", "CD19"}
    assert _co(entries, "cKit") is None
    assert _co(entries, "CD163")["relation"] == "subset"
    # A gated member leads.
    assert _co(entries, "HLADRA", gated={"CD19"})["members"][0] == "CD19"
    # CD206 supports IBA1 once every carrier it names lies within IBA1.
    entries["CD206"]["partners"] = [p for p in entries["CD206"]["partners"]
                                    if p["marker"] in ("CD163", "IBA1")]
    assert set(_co(entries, "IBA1")["members"]) == {"CD163", "CD206"}


def test_a_supporting_tile_stands_only_on_stated_relations_and_real_populations():
    # A pair a planner proposed for CD11b's own tile says nothing of IBA1's cells.
    entries = _myeloid()
    entries["CD11b"]["partners"].append({"marker": "IBA1", "relation": "subset",
                                         "confidence": "moderate", "proposed": True})
    assert _co(entries, "IBA1")["marker"] == "CD163"
    # A state is never a supporter; a ubiquitous marker or a context channel gets none.
    states = _entries(CD3=[], FOXP3=[("CD3", "subset", "high")], HLAA=[],
                      CD31=[("HLAA", "subset", "low")], NaK=[], ECAD=[("NaK", "subset", "low")])
    states["FOXP3"]["role"] = "state"
    states["HLAA"].update(role="state", expected_fraction=[0.5, 1.0])
    states["NaK"]["role"] = "context"
    assert _co(states, "CD3") is None and _co(states, "HLAA") is None \
        and _co(states, "NaK") is None
    # A pair a partner review dropped for this sample is not one.
    from plexora.plugins.gating.server.autogate.partners import partner_plan

    entries = _myeloid()
    plan = partner_plan(entries, "IBA1", set(entries) - {"IBA1"}, {}, 1,
                        dropped={("CD163", "IBA1", "subset")})
    assert not [p for p in plan if p["kind"] == "co"]


def test_a_supporting_tile_is_named_apart_and_read_as_support_only():
    """The look names the tile `supporting` (never `expected`) and its guide
    says that dark says nothing; the review names it a supporting partner."""
    from plexora.plugins.gating.server.autogate import explain, packets, partners

    panels = {"markers": {"co": "CD163", "anti": "CD20"}, "co": "CD163", "anti": "CD20",
              "relations": {"co": "supports", "anti": "exclusive"}, "gates": {}}
    assert packets.partner_words(panels) == {
        "partner_panels": {"supporting": "CD163", "not_expected": "CD20"}}
    assert packets._panel_words("t2m", panels) == \
        "morphology | outlined context / supporting CD163 | not expected CD20"
    assert packets.partner_guide(panels) == ["supporting_tile"]
    assert "says nothing" in packets.READING_GUIDE["supporting_tile"]
    assert "never read a dark supporting tile as evidence against" \
        in packets.READING_GUIDE["supporting_tile"]
    plain = {**panels, "relations": {"co": "subset", "anti": "exclusive"}}
    assert packets.partner_words(plain)["partner_panels"]["expected"] == "CD163"
    assert packets.partner_guide(plain) == []
    tiles = partners.tiles_of(panels)
    assert tiles[0] == {"marker": "CD163", "channel": "CD163", "panel": "co",
                        "relation": "supports"}
    unit = {"marker": "IBA1", "packets": ["pk_1", "pk_2"], "state": "manual_review_recommended",
            "partners_drawn": [{"look": 1, "kind": "cell_panel", "tiles": tiles},
                               {"look": 2, "kind": "cell_panel", "tiles": tiles}]}
    review = explain.build(unit, state="accepted", method="ai_panel", confidence="moderate")
    cd163 = next(s for s in review["supporting"] if s["marker"] == "CD163")
    assert cd163["relation"] == "supports" and cd163["roles"] == ["tile_expected"]
    assert "supporting partner in 2 looks" in cd163["influence"] and "says nothing" \
        in cd163["influence"]
    assert "Supported where lit by CD163" in review["notes"]


def test_the_colon_panel_gets_its_supporting_tiles_from_the_vocabulary():
    """The lsp35766 panel's myeloid and antigen-presenting markers, from the
    shipped graph alone (no partner review): IBA1 -> CD163, CD11b -> CD66b,
    HLA-DR -> CD11c and CD19; cKit none; CD3E keeps its lineages beneath."""
    from plexora.plugins.gating.server.autogate import context

    markers = ["CD3E", "CD20", "panCK", "CD8a", "CD4", "IBA1", "CD11b", "CD163", "CD206",
               "CD66b", "cKit", "CD11c", "CD31", "Ecadherin", "CD19", "CDX2", "HLADRA", "VIM",
               "aSMA"]
    entries = context.build(markers)["entries"]
    assert _co(entries, "IBA1")["marker"] == "CD163"
    assert _co(entries, "CD11b")["marker"] == "CD66b"
    hla = _co(entries, "HLADRA")
    assert hla["relation"] == "supports" and set(hla["members"]) == {"CD11c", "CD19"}
    assert _co(entries, "cKit") is None
    assert _co(entries, "CD3E")["relation"] == "contains"
