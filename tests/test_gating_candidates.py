"""Which channels are gated, and where a hard marker's first look starts.

- nuclear counterstains and autofluorescence / blank channels are recognised by
  name pattern, never by an exact list, and never gated;
- a lineage marker whose mixture splits off another lineage's spill starts at
  the scored proposal, which a lineage-specific partner's curve leads;
- a continuously expressed marker is gated at its background's noise ceiling,
  never skipped as "not binary";
- the scored candidates join the lattice even outside the mixture's band.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from tests.autogate_fixtures import FakeData, make_gating_project


# -- structural channels -----------------------------------------------------------------


@pytest.mark.parametrize("name", ["DNA", "DNA1", "DNA_2", "dna3", "DAPI", "DAPI_cycle3",
                                  "c2_DAPI", "Hoechst", "Hoechst33342", "Hoechst_04",
                                  "Nucleus", "Nucleus2", "NUCLEI", "Nuclear", "Ir191",
                                  "Iridium", "SYTO13", "DRAQ5"])
def test_a_nuclear_counterstain_is_recognised_by_its_name(name):
    from plexora.agent import presets
    from plexora.ai import vocabulary

    assert vocabulary.canonical(name) == vocabulary.NUCLEAR
    assert presets.is_nuclear_name(name)


@pytest.mark.parametrize("name", ["AF", "AF1", "AF2", "AF_3", "Autofluorescence",
                                  "autofluor2", "Blank", "Blank_Cy5", "Background", "Empty"])
def test_an_autofluorescence_channel_is_recognised_by_its_name(name):
    from plexora.ai import vocabulary

    assert vocabulary.canonical(name) == vocabulary.AUTOFLUORESCENCE


@pytest.mark.parametrize("name", ["CD3_AF488", "pDNA", "DNase", "Nucleolin", "DNA-PK",
                                  "H3K27me3", "AFP", "CD8a"])
def test_a_marker_that_only_looks_structural_is_not(name):
    from plexora.ai import vocabulary

    assert vocabulary.canonical(name) not in (vocabulary.NUCLEAR, vocabulary.AUTOFLUORESCENCE)


def test_structural_channels_are_context_and_never_gated():
    from plexora.plugins.gating import capabilities_session
    from plexora.plugins.gating.server.autogate import context

    panel = context.build(["Nucleus", "Nucleus2", "AF1", "CD45", "HLAABC"])
    for marker in ("Nucleus", "Nucleus2", "AF1"):
        assert panel["entries"][marker]["role"] == "context"
        assert capabilities_session._structural(marker)
    assert "CD45" in panel["order"] and "Nucleus2" not in panel["order"]
    assert not capabilities_session._structural("CD45")


# -- scoring --------------------------------------------------------------------------------


def _myeloid_scene(n=60_000, seed=3):
    """CD68 is real on macrophages (10%, CD45+ CD163+), spills dimly into
    T cells (25%, CD45+ CD3+), and sits on a background elsewhere. The truth
    boundary is between the spill and the macrophages, near 6.2."""
    rng = np.random.default_rng(seed)
    kind = rng.choice(3, size=n, p=[0.10, 0.25, 0.65])     # macrophage, T cell, other
    mac, t = kind == 0, kind == 1
    cd68 = np.where(mac, rng.normal(6.7, 0.18, n),
                    np.where(t, rng.normal(5.75, 0.12, n), rng.normal(5.4, 0.08, n)))
    cd45 = np.where(mac | t, rng.normal(7.6, 0.25, n), rng.normal(5.5, 0.2, n))
    cd163 = np.where(mac, rng.normal(7.2, 0.25, n), rng.normal(5.4, 0.15, n))
    cd3 = np.where(t, rng.normal(7.3, 0.2, n), rng.normal(5.4, 0.15, n))
    columns = {m: np.expm1(v) for m, v in
               (("CD68", cd68), ("CD45", cd45), ("CD163", cd163), ("CD3", cd3))}
    return FakeData(columns, seed=seed), mac


def _gmm(ds, marker):
    from plexora.plugins.gating.server.autogate import profile as profmod

    fit = profmod.fit_for(ds, marker)
    pools = profmod.pools(fit)
    d = profmod._pair_d(pools["mu_bg"], pools["sd_bg"], pools["mu_pos"], pools["sd_pos"])
    return float(profmod.column(ds, marker).from_fit(pools["gate"])), d


def test_a_lineage_partner_leads_the_start_where_the_mixture_split_off_spill():
    from plexora.plugins.gating.server.autogate import context, scoring

    ds, mac = _myeloid_scene()
    panel = context.build(["CD45", "CD68", "CD163", "CD3"])
    gates = {"CD45": (float(np.expm1(6.6)), "high"), "CD163": (float(np.expm1(6.4)), "high"),
             "CD3": (float(np.expm1(6.4)), "high")}
    partners = scoring.evidence_partners(panel, "CD68", gates)
    relations = {(p["marker"], p["relation"], p["direction"]) for p in partners}
    assert ("CD45", "subset", "forward") in relations
    assert ("CD163", "coexpressed", "forward") in relations     # named by CD163's entry
    # The mixture as it goes wrong on real tissue: split at the spill, with
    # little separation.
    gmm, d = float(np.expm1(5.62)), 1.0
    result = scoring.score(ds, "CD68", gmm=gmm, separation_d=d, partners=partners)
    proposal = float(np.log1p(result["proposal"]))
    assert result["leading"] == "CD163"                         # stricter than CD45
    assert 5.95 < proposal < 6.45
    by_id = {c["id"]: c for c in result["candidates"]}
    assert by_id["bio:CD163"]["robust"] and "score" in by_id
    for cand in result["candidates"]:
        for value in cand["components"].values():
            assert value is None or value >= 0
    called = np.log1p(np.asarray(ds.table.columns(["CD68"])["CD68"])) > proposal
    assert (called & mac).sum() / called.sum() > 0.9
    again = scoring.score(ds, "CD68", gmm=gmm, separation_d=d, partners=partners)
    assert again["proposal"] == result["proposal"]


def test_a_partner_that_follows_its_own_gate_does_not_lead():
    """CD4 T cells are nearly all of the CD3+ cells here: the share of CD3+
    cells along CD4 tracks wherever CD3 is gated, so that curve is evidence
    of nothing about CD4's own boundary."""
    from plexora.plugins.gating.server.autogate import scoring

    rng = np.random.default_rng(5)
    n = 40_000
    t = rng.random(n) < 0.3
    level = rng.normal(0, 1, n)
    cd3 = np.where(t, 6.6 + 0.5 * level, rng.normal(5.4, 0.15, n))
    cd4 = np.where(t, 6.4 + 0.5 * level + rng.normal(0, 0.05, n), rng.normal(5.4, 0.12, n))
    ds = FakeData({"CD4": np.expm1(cd4), "CD3": np.expm1(cd3)})
    partners = [{"marker": "CD3", "relation": "subset", "direction": "forward",
                 "gate": float(np.expm1(6.2)), "confidence": "high"}]
    gmm, d = _gmm(ds, "CD4")
    result = scoring.score(ds, "CD4", gmm=gmm, separation_d=d, partners=partners)
    bio = [c for c in result["candidates"] if c["id"] == "bio:CD3"]
    assert bio and not bio[0]["robust"] and bio[0]["elasticity"] > 0.75
    assert result["leading"] is None


def test_a_continuous_marker_is_gated_at_its_noise_ceiling():
    from plexora.plugins.gating.server.autogate import scoring

    rng = np.random.default_rng(7)
    n = 40_000
    negative = rng.random(n) < 0.25
    hla = np.where(negative, rng.normal(5.4, 0.1, n), rng.uniform(5.9, 8.5, n))
    ds = FakeData({"HLAABC": np.expm1(hla)})
    gmm, d = _gmm(ds, "HLAABC")
    result = scoring.score(ds, "HLAABC", gmm=gmm, separation_d=d, continuous=True)
    background = result["background"]
    assert background["ceiling"] is not None
    ceiling = float(np.log1p(background["ceiling"]))
    assert 5.8 < ceiling < 6.2                                   # 5.4 + 6 x 0.1
    proposal = float(np.log1p(result["proposal"]))
    assert ceiling - 0.1 < proposal < ceiling + 0.4               # the Auto gate barely pulls
    assert "ceiling" in result["proposal_source"]


def test_a_majority_positive_marker_finds_its_background_below_the_positives():
    from plexora.plugins.gating.server.autogate import scoring

    rng = np.random.default_rng(8)
    n = 40_000
    lost = rng.random(n) < 0.15
    b2m = np.where(lost, rng.normal(5.5, 0.1, n), rng.normal(7.5, 0.5, n))
    found = scoring.background(np.sort(b2m))
    assert abs(found["mode"] - 5.5) < 0.1                         # not the tallest peak
    assert 5.9 < found["ceiling"] < 6.4


def test_a_degenerate_mixture_barely_counts():
    from plexora.plugins.gating.server.autogate import scoring

    rng = np.random.default_rng(9)
    n = 40_000
    pcna = rng.normal(5.6, 0.15, n)
    pcna[: n // 50] = rng.uniform(0.5, 4.0, n // 50)              # a low tail
    pcna[n // 50: n // 50 + n // 30] = rng.normal(7.2, 0.3, n // 30)
    ds = FakeData({"PCNA": np.expm1(pcna)})
    low_tail_gate = float(np.expm1(4.2))                          # calls ~98% positive
    result = scoring.score(ds, "PCNA", gmm=low_tail_gate, separation_d=1.6)
    assert float(np.log1p(result["proposal"])) > 6.0
    assert any("barely counts" in note for note in result["notes"])


# -- the lattice ------------------------------------------------------------------------------


def test_scored_candidates_join_the_lattice_outside_the_band_and_anchor_the_walk(tmp_path):
    from plexora.agent import AgentSession
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import lattice

    make_gating_project(tmp_path)
    ds = AgentSession().data("gsynth")
    gmm = model.fit_for(ds, "CD8")["gate"]
    plain = lattice.build(ds, "CD8", gmm=gmm)
    top = float(np.asarray(ds.table.columns(["CD8"])["CD8"]).max())
    far = float(plain["guard"][1] + (top - plain["guard"][1]) / 2)
    scoring = {"candidates": [{"id": "bio:CD3", "low": far, "robust": True},
                              {"id": "bio:CD20", "low": far * 0.9, "robust": False}],
               "background": {"sd_fit": 0.1}}
    scored = lattice.build(ds, "CD8", gmm=gmm, scoring=scoring)
    ids = {s for p in scored["points"] for s in p["sources"]}
    assert "bio:CD3" in ids and "bio:CD20" not in ids              # not-robust stays out
    assert scored["fingerprint"] != plain["fingerprint"]
    up = lattice.chain(scored, gmm, "up", max_points=10)
    assert "bio:CD3" in up[-1]["sources"]                          # the walk stops there
    started = lattice.build(ds, "CD8", gmm=gmm, scoring=scoring, start=far)
    steps = [p for p in started["points"] if any(s.startswith("up:") for s in p["sources"])]
    assert steps and all(p["low"] > far for p in steps)            # steps around the start


# -- the engine ---------------------------------------------------------------------------------


class _Engine:
    def __init__(self, max_tier=4):
        self.options = {"max_tier": max_tier, "audit_sheet": True, "seed": 0}
        self.closed = []
        self.proposed = []

    def close(self, unit, state, reason, **kwargs):
        unit["state"] = state
        self.closed.append((state, reason, kwargs))

    def propose(self, unit, rescore=False):
        self.proposed.append(rescore)
        unit["start"] = {"source": "scoring", "low": 1.0}
        unit["candidate"] = 1.0

    def references_ready(self, unit):
        return []


def _unit(binary):
    return {"marker": "M", "project": "p", "state": "pending", "gmm": 2.0, "candidate": 2.0,
            "flags": [], "class": "continuous", "t1": {"recommended_tier": "T3"},
            "context": {"canonical": "M", "binary": binary}, "no_image_channel": False}


def test_a_continuous_vocabulary_marker_waits_for_a_look_instead_of_closing():
    from plexora.plugins.gating.server.autogate import bulk

    engine = _Engine()
    unit = _unit(False)
    bulk.decide_first(engine, unit)
    assert unit["state"] == "awaiting_t2" and unit["continuous"] and not engine.closed
    unknown = _unit(None)
    bulk.decide_first(engine, unknown)
    assert not unknown.get("continuous")                          # None is not a statement


def test_not_binary_recentres_on_the_onset_once_then_closes_unwritten():
    from plexora.plugins.gating.server.autogate import transitions

    engine = _Engine()
    unit = {**_unit(None), "state": "awaiting_t2"}
    outcome = transitions._continuous(engine, unit, "the look")
    assert outcome["continuous"] and unit["state"] == "awaiting_t2"
    assert engine.proposed == [True] and unit["continuous_asked"]
    outcome = transitions._continuous(engine, unit, "the look")
    assert unit["state"] == "not_binary"
    state, _reason, kwargs = engine.closed[-1]
    assert kwargs.get("proposed") == 1.0 and "write" not in kwargs


def test_a_look_at_a_scored_start_is_not_contradicted_by_the_mixtures_band():
    from plexora.plugins.gating.server.autogate import transitions

    unit = {**_unit(True), "start": {"source": "scoring", "low": 6.0}, "candidate": 6.0}
    assert transitions._contradicts(SimpleNamespace(), unit, "too_low") is None


# -- every gated marker ends with a value ----------------------------------------------------


@pytest.mark.paid
def test_a_marker_sent_to_review_is_written_and_tagged_not_left_ungated(tmp_path):
    from plexora.agent import AgentSession, invoke, jobs, registry
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import provenance

    registry.discover(["gating"])
    make_gating_project(tmp_path, grid=32, size=1280,
                        markers=("CD3", "CD8", "CD20", "CD4", "FOXP3"))
    session = AgentSession()
    started = invoke(session, "gating_session_start", {"scope": "project",
                                                       "project": "gsynth",
                                                       "markers": ["CD4", "DNA"]})
    assert started["ok"], started.get("error")
    sid = started["result"]["session_id"]
    assert "DNA" in started["result"]["skipped_markers"]          # named, still not gated
    jobs.drain(120)
    packet = invoke(session, "gating_next", {"session_id": sid})["result"]["packet"]
    for _ in range(2):
        bad = invoke(session, "gating_answer", {"session_id": sid,
                                                "packet_id": packet["packet_id"],
                                                "answer": {"kind": "t2_confirm", "x": 1}})
        assert not bad["ok"]
    status = invoke(session, "gating_session_status", {"session_id": sid})["result"]
    unit = {u["marker"]: u for u in status["units"]}["CD4"]
    assert unit["state"] == "manual_review_recommended"
    gate = model.get_gate(session.data("gsynth"), "CD4")
    assert gate["thresholded"]                                    # a value, not the full range
    row = provenance.read("gsynth")["CD4"]
    assert row["method"] == "needs_review" and row["confidence"] == "manual_review"


@pytest.mark.paid
def test_the_scored_candidates_are_a_read_only_tool(tmp_path):
    from plexora.agent import AgentSession, invoke, registry
    from plexora.plugins.gating.server import model

    registry.discover(["gating"])
    make_gating_project(tmp_path)
    session = AgentSession()
    before = model.get_gate(session.data("gsynth"), "CD8")
    result = invoke(session, "score_gate_candidates", {"project": "gsynth", "marker": "CD8"})
    assert result["ok"], result.get("error")
    scored = result["result"]["scored"]
    assert scored["proposal"] is not None
    assert {c["id"] for c in scored["candidates"]} >= {"gmm", "score"}
    assert model.get_gate(session.data("gsynth"), "CD8") == before
