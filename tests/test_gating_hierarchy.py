"""Gating decisions form an evidence graph, not a list of independent gates.

- the panel becomes a tree from its own relations (broad -> lineage ->
  subtype -> state), and gating follows it;
- a reference is chosen for relevance x reliability: a failed gate is never
  leaned on, a low-reliability one only when nothing better exists;
- the tree implies relations the vocabulary does not state (a subset of a
  subset; a subset's inherited exclusions);
- children's positives place a parent whose own distribution is weak;
- a reference that fails later re-scores the markers that leaned on it;
- the viewer is asked to put a set-up question to the user, not to open a
  panel that cannot open yet.
"""

from __future__ import annotations

import numpy as np

from tests.autogate_fixtures import FakeData

PANEL = ["DNA1", "AF1", "CD45", "CD3e", "CD4", "CD8a", "FOXP3", "CD20", "CD68", "CD163",
         "CD11c", "SOX10", "MART1", "PD1", "Ki67", "PCNA", "Mystery"]


def _panel(markers=PANEL):
    from plexora.plugins.gating.server.autogate import context

    return context.build(markers)


# -- the tree --------------------------------------------------------------------------------


def test_the_panel_becomes_a_tree_from_its_own_relations():
    from plexora.plugins.gating.server.autogate import hierarchy

    tree = hierarchy.build(_panel())
    nodes = tree["nodes"]
    assert "DNA1" not in nodes and "AF1" not in nodes            # structural: never gated
    assert nodes["CD45"]["stage"] == "broad"
    assert nodes["CD3e"]["stage"] == "lineage" and nodes["CD3e"]["placed_under"] == "CD45"
    assert nodes["CD8a"]["stage"] == "subtype" and nodes["CD8a"]["placed_under"] == "CD3e"
    assert nodes["CD163"]["placed_under"] == "CD68"               # a co-expressed placement
    assert nodes["FOXP3"]["stage"] == "state" and nodes["PD1"]["stage"] == "state"
    assert nodes["Mystery"]["stage"] == "unplaced"
    assert {"CD3e", "CD20", "CD68", "CD11c"} <= set(nodes["CD45"]["children"])


def test_gating_follows_the_tree_and_partners_still_come_first():
    order = _panel()["order"]
    stage = {m: i for i, m in enumerate(order)}
    assert order[0] == "CD45" and order[-1] == "Mystery"
    assert stage["CD3e"] < stage["CD8a"] < stage["FOXP3"]
    assert stage["CD68"] < stage["CD163"]
    assert max(stage[m] for m in ("CD3e", "CD20", "CD68", "CD11c")) \
        < min(stage[m] for m in ("CD4", "CD8a", "CD163"))
    assert max(stage[m] for m in ("CD4", "CD8a", "CD163")) \
        < min(stage[m] for m in ("PD1", "Ki67", "PCNA", "FOXP3"))


def test_a_mutual_relation_is_not_a_level():
    from plexora.plugins.gating.server.autogate import hierarchy

    tree = hierarchy.build(_panel(["HLAABC", "B2M", "CD45"]))
    depths = sorted(tree["nodes"][m]["depth"] for m in ("HLAABC", "B2M"))
    assert depths == [0, 1]


def test_the_tree_implies_relations_the_vocabulary_does_not_state():
    from plexora.plugins.gating.server.autogate import hierarchy

    rels = {(r["marker"], r["relation"], r["direction"]): r
            for r in hierarchy.relations(_panel(), "FOXP3")}
    # FOXP3 within CD3, CD3 exclusive of CD68: FOXP3 is exclusive of CD68.
    derived = rels[("CD68", "exclusive", "forward")]
    assert derived["derived"] and derived["via"] == ["CD3e"]
    assert not rels[("CD3e", "subset", "forward")]["derived"]     # stated, not derived
    # CD45's children include its grandchildren; state readouts are not children.
    kids = {r["marker"]: r for r in hierarchy.relations(_panel(), "CD45")
            if r["kind"] == "child"}
    assert {"CD3e", "CD8a", "CD163"} <= set(kids) and "PD1" not in kids
    assert kids["CD8a"]["relevance"] < 1.0 or not kids["CD8a"]["derived"]


# -- reliability ----------------------------------------------------------------------------


def test_a_gate_earns_its_grade_and_a_review_write_earns_none():
    from plexora.plugins.gating.server.autogate import hierarchy

    assert hierarchy.grade({"state": "accepted_t1"})[0] == "high"
    assert hierarchy.grade({"state": "accepted", "confidence": "moderate"})[0] == "moderate"
    assert hierarchy.grade({"state": "accepted_low_confidence", "confidence": "low"})[0] == "low"
    assert hierarchy.grade({"state": "manual_review_recommended", "needs_review": True,
                            "final": 6.0})[0] == "failed"
    assert hierarchy.grade({"state": "technically_failed"})[0] == "failed"
    assert hierarchy.grade({"state": "awaiting_t2"})[0] == "pending"
    assert hierarchy.grade({"state": "skipped_manual", "thresholded": True,
                            "seen": [5.0, None]})[0] == "high"
    assert hierarchy.grade_from_provenance({"method": "needs_review",
                                            "confidence": "manual_review"}) == "failed"
    assert hierarchy.grade_from_provenance({"status": "approved"}) == "high"


def test_a_failed_reference_is_avoided_and_a_reliable_one_takes_its_place():
    from plexora.plugins.gating.server.autogate import context, hierarchy

    panel = _panel()
    grades = {"CD45": "failed", "CD3e": "high", "CD20": "moderate"}
    plan = hierarchy.plan(panel, hierarchy.build(panel), "CD8a", grades)
    used = [r["marker"] for r in plan["used"]]
    assert "CD45" not in used and used[0] == "CD3e"
    assert "CD20" in used                                         # the negative reference
    assert any(a["marker"] == "CD45" and a["grade"] == "failed" for a in plan["avoided"])
    refs = context.references_for(panel, "CD8a", grades)
    assert [r["marker"] for r in refs] == ["CD3e", "CD20"]


def test_a_low_reliability_reference_is_used_only_when_nothing_better_exists():
    from plexora.plugins.gating.server.autogate import hierarchy

    rels = hierarchy.relations(_panel(), "CD8a")
    both = hierarchy.select(rels, {"CD3e": "low", "CD45": "high"})
    assert [r["marker"] for r in both["used"] if r["kind"] == "positive"] == ["CD45"]
    assert any(a["marker"] == "CD3e" for a in both["avoided"])
    alone = hierarchy.select(rels, {"CD3e": "low", "CD45": "failed"})
    lone = [r for r in alone["used"] if r["kind"] == "positive"]
    assert [r["marker"] for r in lone] == ["CD3e"] and lone[0]["down_weighted"]
    assert lone[0]["weight"] < 0.5


def test_the_scoring_never_receives_a_failed_partner():
    from plexora.plugins.gating.server.autogate import scoring

    gates = {"CD45": (900.0, "failed"), "CD3e": (700.0, "high")}
    partners = scoring.evidence_partners(_panel(), "CD8a", gates)
    names = {p["marker"] for p in partners}
    assert names == {"CD3e"} and partners[0]["gate_confidence"] == "high"


# -- children place a weak parent ------------------------------------------------------------


def _leukocyte_scene(n=60_000, seed=7):
    """CD45 overlaps its background (a weak mixture); T and B cells, gated
    reliably, sit on its upper part. Truth: leukocytes."""
    rng = np.random.default_rng(seed)
    kind = rng.choice(3, size=n, p=[0.2, 0.1, 0.7])               # T, B, other
    t, b = kind == 0, kind == 1
    leuko = t | b
    cd45 = np.where(leuko, rng.normal(6.6, 0.25, n), rng.normal(5.7, 0.3, n))
    cd3 = np.where(t, rng.normal(7.2, 0.2, n), rng.normal(5.4, 0.15, n))
    cd20 = np.where(b, rng.normal(7.0, 0.2, n), rng.normal(5.4, 0.15, n))
    ds = FakeData({"CD45": np.expm1(cd45), "CD3e": np.expm1(cd3), "CD20": np.expm1(cd20)},
                  seed=seed)
    return ds, leuko


def test_reliable_children_place_a_parent_whose_own_distribution_is_weak():
    from plexora.plugins.gating.server.autogate import context, scoring

    ds, leuko = _leukocyte_scene()
    panel = context.build(["CD45", "CD3e", "CD20"])
    gates = {"CD3e": (float(np.expm1(6.4)), "high"), "CD20": (float(np.expm1(6.3)), "high")}
    partners = scoring.evidence_partners(panel, "CD45", gates)
    assert {(p["marker"], p["direction"]) for p in partners} == {("CD3e", "reverse"),
                                                                ("CD20", "reverse")}
    weak = scoring.score(ds, "CD45", gmm=float(np.expm1(5.5)), separation_d=1.0,
                         partners=partners)
    assert weak["leading"] == "children"
    by_id = {c["id"]: c for c in weak["candidates"]}
    assert by_id["children"]["n_children"] == 2
    proposal = float(np.log1p(weak["proposal"]))
    assert 5.9 < proposal < 6.4
    # The children only bound it when the parent's own mixture is clear.
    clear = scoring.score(ds, "CD45", gmm=float(np.expm1(6.15)), separation_d=3.0,
                          partners=partners)
    assert clear["leading"] != "children" and "children" in {c["id"] for c in
                                                             clear["candidates"]}
    # Low-reliability children do not place anything.
    shaky = [{**p, "gate_confidence": "low"} for p in partners]
    low = scoring.score(ds, "CD45", gmm=float(np.expm1(5.5)), separation_d=1.0,
                        partners=shaky)
    assert low["leading"] != "children"


# -- the engine: deferral and failure ---------------------------------------------------------


class _Stub:
    """The parts of `Engine` the evidence-graph methods use."""

    def __init__(self, units, options=None):
        self.units = units
        self.options = {"defer_parents": True, **(options or {})}
        self.events = []
        self.rescored = []

    def units_of(self, project):
        return [u for u in self.units if u["project"] == project]

    def waiting_for_user(self):
        return []

    def log(self, **event):
        self.events.append(event)

    def propose(self, unit, rescore=False):
        self.rescored.append((unit["marker"], rescore))


def _u(marker, state, **extra):
    return {"marker": marker, "project": "p", "state": state, **extra}


def test_a_weak_parent_waits_for_its_children_once_then_is_rescored():
    from plexora.plugins.gating.server.autogate.engine import Engine

    parent = _u("CD45", "awaiting_t2", metrics={"d": 1.0}, scoring={"basis": "gmm+ceiling"},
                evidence_plan={"children": ["CD3e", "CD20"]})
    kids = [_u("CD3e", "awaiting_t2"), _u("CD20", "accepted_t1")]
    stub = _Stub([parent, *kids])
    assert Engine.defer_for_children(stub, parent) is True
    assert parent["deferred"] == "waiting"
    assert Engine.defer_for_children(stub, parent) is True        # still waiting
    kids[0]["state"] = "accepted"
    assert Engine.defer_for_children(stub, parent) is False
    assert parent["deferred"] == "resumed" and stub.rescored == [("CD45", True)]
    assert Engine.defer_for_children(stub, parent) is False       # never twice


def test_a_clear_or_looked_at_parent_never_waits():
    from plexora.plugins.gating.server.autogate.engine import Engine

    clear = _u("CD45", "awaiting_t2", metrics={"d": 3.5}, scoring={"basis": "gmm+ceiling"},
               evidence_plan={"children": ["CD3e"]})
    looked = _u("CD20", "awaiting_t2", metrics={"d": 0.5}, used={"packets": 1},
                evidence_plan={"children": ["CD3e"]})
    stub = _Stub([clear, looked, _u("CD3e", "awaiting_t2")])
    assert Engine.defer_for_children(stub, clear) is False
    assert Engine.defer_for_children(stub, looked) is False
    off = _Stub([_u("CD45", "awaiting_t2", metrics={"d": 1.0}, scoring={},
                    evidence_plan={"children": ["CD3e"]}), _u("CD3e", "awaiting_t2")],
                options={"defer_parents": False})
    assert Engine.defer_for_children(off, off.units[0]) is False


def test_a_reference_that_fails_rescores_the_markers_that_leaned_on_it():
    from plexora.plugins.gating.server.autogate.engine import Engine

    failed = _u("CD45", "manual_review_recommended", needs_review=True)
    fresh = _u("CD11c", "awaiting_t2", scoring={"partners": [{"marker": "CD45"}]})
    looked = _u("CD68", "awaiting_t4", used={"packets": 2},
                reference_gates=[{"marker": "CD45"}])
    unrelated = _u("SOX10", "awaiting_t2", scoring={"partners": []})
    stub = _Stub([failed, fresh, looked, unrelated])
    Engine.reference_failed(stub, failed)
    assert stub.rescored == [("CD11c", True)]
    assert looked["stood_on_failed"] == ["CD45"]
    assert "stood_on_failed" not in unrelated
    accepted = _u("CD3e", "accepted", confidence="high")
    Engine.reference_failed(stub, accepted)                       # not a failure: nothing
    assert stub.rescored == [("CD11c", True)]


# -- the viewer -----------------------------------------------------------------------------


def test_a_setup_question_asks_the_tab_instead_of_opening_the_panel():
    from plexora.plugins.gating.server.autogate import mirror_script

    packet = {"kind": "expression_setup", "units": [],
              "evidence": {"projects": ["lsp"], "current": {}}}
    same = mirror_script.script_for(packet, None, None, current_project="lsp")
    assert same == [{"type": "open_tool", "arguments": {"tool": "gating", "ask": True}}]
    other = mirror_script.script_for(packet, None, None, current_project="elsewhere")
    assert other[0]["type"] == "open_project" and "tool" not in other[0]["arguments"]
    assert other[-1]["arguments"].get("ask") is True


# -- the sample's biology ------------------------------------------------------------------


def test_the_users_words_resolve_to_tissue_and_disease_contexts():
    from plexora.plugins.gating.server.autogate import biology

    record = biology.resolve(tissue="skin", disease="This is a melanoma sample")
    assert record["source"] == "user" and record["contexts"] == ["skin", "melanoma"]
    unknown = biology.resolve(tissue="axolotl gill")
    assert unknown["contexts"] == [] and unknown["unmatched"] == ["axolotl gill"]
    assert biology.match("metastatic melanoma") == ["melanoma"]


def test_melanoma_makes_mart1_with_sox10_count_for_more_without_touching_the_panel():
    from plexora.plugins.gating.server.autogate import biology, hierarchy

    panel = _panel(["CD45", "SOX10", "MART1", "SOX9", "PanCK"])
    before = {r["marker"]: r["relevance"] for r in hierarchy.relations(panel, "MART1")}
    over = biology.overlay(panel, biology.resolve(disease="melanoma"))
    after = {r["marker"]: r for r in hierarchy.relations(over, "MART1")}
    assert after["SOX10"]["relevance"] > before["SOX10"]
    assert after["SOX10"]["context"] == "melanoma"
    assert "PanCK" in after and "PanCK" not in before             # the disease's exclusion
    assert "context" not in str(panel["entries"]["MART1"]["partners"])   # stored panel intact


def test_a_relation_the_user_stated_is_never_overridden_by_context():
    from plexora.plugins.gating.server.autogate import biology, context

    panel = _panel(["SOX10", "MART1"])
    context.apply_entry(panel, "MART1", {"partners": [
        {"marker": "SOX10", "relation": "coexpressed", "confidence": "low"}]}, source="user")
    over = biology.overlay(panel, biology.resolve(disease="melanoma"))
    partner = over["entries"]["MART1"]["partners"][0]
    assert partner["confidence"] == "low" and "context" not in partner


def test_a_look_is_told_what_the_marker_also_marks_in_this_tissue():
    from plexora.plugins.gating.server.autogate import biology

    record = biology.resolve(tissue="skin", disease="melanoma")
    panel = _panel(["SOX9", "SOX10", "PanCK"])
    brief = biology.brief(record, panel, "SOX9", ["SOX10"])
    assert any("hair follicle" in note for note in brief["expect"]["SOX9"])
    assert "SOX10" in brief["expect"]
    assert any(s["name"] == "hair follicles and adnexa" for s in brief["structures"])
    assert biology.brief(None, panel, "SOX9") is None


def test_inferred_context_says_it_was_inferred():
    from plexora.plugins.gating.server.autogate import biology

    inferred = biology.infer(_panel(["SOX10", "MART1", "CD45"]))
    assert inferred["source"] == "inferred" and "melanoma" in inferred["contexts"]
    assert biology.infer(_panel(["CD45", "CD3e"])) is None
    stated = biology.infer(_panel(["CD45"]), {"tissue": "skin punch biopsy"})
    assert stated["source"] == "metadata" and stated["contexts"] == ["skin"]


def test_biology_caps_a_conflicting_look_and_lifts_one_where_everything_agrees():
    from plexora.plugins.gating.server.autogate import engine

    base = {"path": "t2", "ai_confidence": 0.7, "metrics": {"d": 2.0}, "flags": [],
            "context": {"compartment": "nuclear"},
            "scoring": {"basis": "coexpression", "leading": "SOX10",
                        "partners": [{"marker": "SOX10", "gate_confidence": "high"}]}}
    plain = engine.confidence_for(dict(base))
    agrees = engine.confidence_for({**base, "biology_fit": "consistent"})
    conflicts = engine.confidence_for({**base, "ai_confidence": 0.95,
                                       "biology_fit": "conflicts"})
    assert plain == "moderate" and agrees == "high" and conflicts == "moderate"
    shaky_lead = {**base, "biology_fit": "consistent",
                  "scoring": {**base["scoring"],
                              "partners": [{"marker": "SOX10", "gate_confidence": "low"}]}}
    assert engine.confidence_for(shaky_lead) == "moderate"
