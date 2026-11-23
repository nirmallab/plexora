"""The compartment check (gating VERSION 8, QC CELLS_VERSION 8): each
marker's stain against the compartment the graph names, from
full-resolution pixels (`qc.cells.compartment`); a nuclear marker off the
nuclei gets a note in its looks, by default. Measured 2026-11-23 on two
images, two live runs per arm: TCF1 on LSP11385 0.91 -> 0.13 from the
owner's gate, CDX2 on lsp35766_p193_2 0.25 -> 0.17.

Density-aware panel sampling was built beside it and taken out: four live
rounds, no gain (docs/internal/GATING_ENGINE_V2.md).
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from tests.autogate_fixtures import make_gating_project

def test_a_flagged_marker_s_looks_carry_the_note_with_no_session_option():
    """VERSION 8: the note is standard -- a session started with nothing
    asked for measures (or reads QC's finding) and tells a flagged marker's
    looks; an unflagged marker's looks carry nothing."""
    from plexora.plugins.gating.capabilities_session import SessionOptions
    from plexora.plugins.gating.server.autogate import packets
    from plexora.plugins.qc.server.cells import compartment

    options = SessionOptions.model_construct().model_dump(mode="json")
    assert "compartment_check" not in options and "panel_sampling" not in options
    record = {"references": ["TCF1", "Ki67"], "markers": {
        "CDX2": {"call": compartment.FLAG}, "Ki67": {"call": "as_expected"}}}
    engine = SimpleNamespace(options=options, compartment_record=lambda project: record)
    words = packets.compartment_words(engine, {"project": "p", "marker": "CDX2"})
    assert "does not sit in the nuclei" in words["compartment_note"]
    assert packets.compartment_words(engine, {"project": "p", "marker": "Ki67"}) == {}
    broken = SimpleNamespace(options=options, compartment_record=lambda project: 1 / 0)
    assert packets.compartment_words(broken, {"project": "p", "marker": "CDX2"}) == {}


# -- the compartment check -------------------------------------------------------------------


def _scores(**kw):
    comp = {"Ki67": "nuclear", "TCF1": "nuclear", "Rb": "nuclear", "CDX2": "nuclear",
            "CD3E": "membrane", "CD8a": "membrane", "panCK": "cytoplasmic", "Ecad": "membrane"}
    return {m: {"compartment": comp[m], "score": s} for m, s in kw.items()}


def test_a_nuclear_stain_among_the_membrane_stains_is_flagged_against_two_references():
    from plexora.plugins.qc.server.cells import compartment

    found = compartment.call(_scores(Ki67=1.0, TCF1=1.3, Rb=0.8, CDX2=-0.2, CD3E=-0.05,
                                     CD8a=0.03, panCK=-0.5, Ecad=-0.9))
    assert found["status"] == "called" and set(found["flags"]) == {"CDX2"}
    assert found["references"] == ["TCF1", "Ki67", "Rb"]


def test_no_call_without_two_nuclear_markers_clearly_in_the_nuclei():
    from plexora.plugins.qc.server.cells import compartment

    found = compartment.call(_scores(Ki67=1.0, TCF1=-0.1, CDX2=-0.3, CD3E=0.0, CD8a=0.1,
                                     panCK=-0.4))
    assert found["status"] == "no_call" and found["flags"] == {} and found["references"] == ["Ki67"]
    assert compartment.call(_scores(CD3E=0.0, CD8a=0.1))["status"] == "no_call"


def test_autofluorescence_and_nuclear_stains_take_no_part_and_each_marker_reads_its_own_cycle():
    from plexora.plugins.qc.server.cells import compartment

    names = ["DNA1", "Ki67", "CDX2", "AF1", "DNA2", "CD3E", "Hoechst3", "TCF1"]
    todo = compartment.plan(names, ["Ki67", "CDX2", "AF1", "CD3E", "TCF1", "DNA1"],
                            compartment.compartment_of)
    got = {m: dna for m, _ch, dna, _c in todo}
    assert "AF1" not in got and "DNA1" not in got
    assert got == {"Ki67": "DNA1", "CDX2": "DNA1", "CD3E": "DNA2", "TCF1": "Hoechst3"}


def _disc(shape, centre, radius):
    yy, xx = np.indices(shape)
    return (yy - centre[0]) ** 2 + (xx - centre[1]) ** 2 <= radius ** 2


def test_a_cell_score_is_high_for_a_filled_nucleus_and_low_for_a_ring_around_it():
    from plexora.plugins.qc.server.cells import compartment

    shape, centre = (48, 48), (24, 24)
    nucleus = _disc(shape, centre, 8)
    dna = np.where(nucleus, 1000.0, 50.0)
    filled = np.where(nucleus, 800.0, 40.0)
    ring = np.where(_disc(shape, centre, 13) & ~nucleus, 800.0, 40.0)
    assert compartment.cell_score(dna, filled, 0.325) > 2
    assert compartment.cell_score(dna, ring, 0.325) < -2
    assert compartment.cell_score(np.zeros(shape), filled, 0.325) is None


def test_coarse_pixels_or_no_pixel_size_skip_the_check_with_the_reason():
    from plexora.plugins.qc.server.cells import compartment

    args = (lambda *a: {}, ["DNA1", "Ki67"], ["Ki67"], lambda m: np.ones(100),
            np.arange(100.0), np.arange(100.0))
    coarse = compartment.measure(*args, 1.0)
    assert coarse["status"] == "skipped" and "two pixels" in coarse["why"]
    assert compartment.measure(*args, None)["status"] == "skipped"


def test_a_flagged_marker_gets_plain_words_and_others_none():
    from plexora.plugins.qc.server.cells import compartment

    record = {"references": ["TCF1", "Ki67"], "markers": {
        "CDX2": {"call": compartment.FLAG}, "Ki67": {"call": "as_expected"}}}
    words = compartment.note(record, "CDX2")
    assert "does not sit in the nuclei" in words and "TCF1" in words
    assert compartment.note(record, "Ki67") is None and compartment.note(None, "CDX2") is None


# -- a session ----------------------------------------------------------------------------------


@pytest.mark.pro
def test_a_session_runs_the_compartment_check_by_default_and_records_it(
        tmp_path):
    from plexora.plugins.gating.server.autogate import engine as engines
    from tests.test_gating_session import Oracle, ok

    markers = ("CD3", "CD8", "CD20", "CD4", "FOXP3")
    registry.discover(["gating"])
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=markers)
    session = AgentSession()
    started = ok(invoke(session, "gating_session_start", {
        "scope": "project", "project": "gsynth", "markers": list(markers), "engine": "v2"}))
    jobs.drain(120)
    sid = started["session_id"]
    oracle = Oracle(info)
    index = {c["id"]: i for i, c in enumerate(info["cells"])}
    result = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 20}))
    panels = 0
    for _ in range(120):
        if result["state"] != "decision":
            break
        packet = result["packet"]
        if packet["kind"] == "cell_panel":
            panels += 1
            stored, _ = engines.store().read_packet(sid, packet["packet_id"])
            truth = oracle.truth[packet["units"][0]["marker"]]
            reply = {"kind": "cell_panel", "cells": {
                c["number"]: ("own_stain" if truth[index[c["cell_id"]]] else "not_own_stain")
                for c in stored["_panel"]["cells"]}}
        else:
            reply = oracle.answer(packet)
        result = ok(invoke(session, "gating_answer", {
            "session_id": sid, "packet_id": packet["packet_id"], "answer": reply}))["next"]
    assert result["state"] in ("decided", "done"), result
    assert panels
    record = engines.store().load(sid)
    assert "gsynth" in (record.get("compartment") or {})
    for unit in record["units"].values():
        hyp = unit.get("hypotheses") or {}
        assert "panel_sampling" not in (hyp.get("params") or {})
        assert all("dense" not in c for o in hyp.get("observations") or () for c in o["cells"])
