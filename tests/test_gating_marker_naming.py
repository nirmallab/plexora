"""Naming a marker the vocabulary does not know, before describing it.

A panel marker no spelling resolves is first tried as a typing slip from one
vocabulary marker (`vocabulary.close_match`), then a reader is asked only its
standard name (`marker_naming`); code looks that name up. Only what is still
unknown is described (`panel_context`). A guess never takes a marker the
panel already has, and two markers are never guessed as one.
"""

from __future__ import annotations

import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from plexora.ai import vocabulary
from plexora.plugins.gating.server.autogate import context
from tests.autogate_fixtures import make_gating_project
from tests.test_gating_session import Oracle, answer, next_packet, ok, start


# -- the close match ------------------------------------------------------------------


@pytest.mark.parametrize("typed, meant", [
    ("FOXP33", "FOXP3"),            # a doubled character
    ("CD45R0", "CD45RO"),           # a zero for an O
    ("FXOP3", "FOXP3"),             # two neighbours swapped
    ("SOX1O", "SOX10"),             # an O for a zero
    ("E-cadherln", "ECAD"),         # an l for an i
    ("Vimentn", "Vimentin"),        # a letter dropped from a long name
    ("03_FOXP33_ArgoFluor520", "FOXP3"),  # through a position and a fluorophore
])
def test_a_typing_slip_finds_its_marker(typed, meant):
    assert vocabulary.canonical(typed) is None
    assert vocabulary.close_match(typed)["canonical"] == meant


@pytest.mark.parametrize("typed", [
    "Ki-76",    # two digits swapped: CD146 and CD164 are both markers
    "CD45RU",   # a letter from CD45RA and from CD45RO: two markers, no match
    "HLADX",    # too short for a letter edit (HLA-DR, -DQ, -DP)
    "CD9999",
    "Mystery1",
])
def test_no_close_match_is_a_guess(typed):
    assert vocabulary.close_match(typed) is None


def test_a_known_name_is_never_close_matched():
    assert vocabulary.close_match("CD45RO") is None
    assert vocabulary.close_match("Ki67") is None


# -- the panel context --------------------------------------------------------------


def test_a_close_match_takes_the_vocabulary_entry_and_its_partners_both_ways():
    panel = context.build(["CD4", "FOXP33", "CD8"])
    assert panel["unresolved"] == []
    entry = panel["entries"]["FOXP33"]
    assert entry["canonical"] == "FOXP3" and entry["source"] == "vocabulary"
    assert entry["named"]["by"] == "close_match"
    stated = vocabulary.entry("FOXP3")["partners"]
    if any(p["marker"] in ("CD4", "CD8") for p in stated):
        assert entry["partners"], entry
    # Another marker's partner names the panel's own spelling.
    plain = context.build(["CD4", "FOXP3", "CD8"])
    ours = {(m, p["relation"]) for e in panel["entries"].values() for p in e["partners"]
            for m in [p["marker"]]}
    theirs = {("FOXP33" if m == "FOXP3" else m, p["relation"])
              for e in plain["entries"].values() for p in e["partners"] for m in [p["marker"]]}
    assert ours == theirs


def test_a_guess_never_takes_a_marker_the_panel_already_has():
    panel = context.build(["CD45RO", "CD45R0"])
    assert panel["unresolved"] == ["CD45R0"]
    assert panel["named"] == {}
    assert panel["naming_refused"][0]["marker"] == "CD45R0"


def test_two_markers_guessed_as_one_are_both_refused():
    panel = context.build(["FOXP33", "FXOP3"])
    assert sorted(panel["unresolved"]) == ["FOXP33", "FXOP3"]
    assert {r["marker"] for r in panel["naming_refused"]} == {"FOXP33", "FXOP3"}


def test_a_readers_name_is_used_only_when_the_vocabulary_knows_it():
    panel = context.build(["CD3", "Mystery1", "Mystery2", "Mystery3"],
                          ai_names={"Mystery1": "CD20", "Mystery2": "NotAMarker",
                                    "Mystery3": None})
    assert panel["entries"]["Mystery1"]["canonical"] == "CD20"
    assert panel["named"]["Mystery1"] == {"canonical": "CD20", "by": "ai", "said": "CD20"}
    assert panel["unresolved"] == ["Mystery2", "Mystery3"]
    # Each was asked once: none is asked its name again.
    assert context.naming_due(panel) == []
    assert context.naming_due(context.build(["CD3", "Mystery1"])) == ["Mystery1"]


def test_a_readers_name_never_takes_a_marker_the_panel_has():
    panel = context.build(["CD3", "Mystery1"], ai_names={"Mystery1": "CD3"})
    assert panel["unresolved"] == ["Mystery1"]
    assert "already has CD3" in panel["naming_refused"][0]["reason"]


# -- the session ----------------------------------------------------------------------


@pytest.fixture
def _gating():
    registry.discover(["gating"])


def _panel(tmp_path, monkeypatch, markers):
    from tests import autogate_fixtures

    for marker in markers:
        if marker not in autogate_fixtures.MARKER_LEVELS:
            monkeypatch.setitem(autogate_fixtures.MARKER_LEVELS, marker,
                                autogate_fixtures.MARKER_LEVELS["CD20"])
    vocabulary.clear_cache()
    return make_gating_project(tmp_path, markers=tuple(markers))


@pytest.mark.pro
@pytest.mark.usefixtures("_gating")
def test_a_typo_is_never_asked_about(tmp_path, monkeypatch):
    _panel(tmp_path, monkeypatch, ["CD3", "CD8", "FOXP33"])
    session = AgentSession()
    started = start(session, markers=["CD3", "CD8", "FOXP33"])
    assert started["unresolved_markers"] == []
    packet, _ = next_packet(session, started["session_id"])
    assert packet is None or packet["kind"] not in ("marker_naming", "panel_context")
    ok(invoke(session, "gating_session_finish",
              {"session_id": started["session_id"], "action": "close"}))


@pytest.mark.pro
@pytest.mark.usefixtures("_gating")
def test_a_named_marker_is_not_described(tmp_path, monkeypatch):
    """Round one names Mystery1 CD20: it gets CD20's entry, and no
    `panel_context` packet follows, since nothing is left unknown."""
    _panel(tmp_path, monkeypatch, ["CD3", "CD8", "Mystery1"])
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD8", "Mystery1"])["session_id"]
    packet, _ = next_packet(session, sid)
    assert packet["kind"] == "marker_naming"
    assert packet["evidence"]["unresolved"] == ["Mystery1"]
    outcome = answer(session, sid, packet, {
        "kind": "marker_naming", "names": [{"marker": "Mystery1", "name": "CD20"}]})["outcome"]
    assert outcome["named"] == {"Mystery1": "CD20"}
    assert outcome["still_unresolved"] == []
    packet, _ = next_packet(session, sid)
    assert packet is None or packet["kind"] not in ("marker_naming", "panel_context")
    ok(invoke(session, "gating_session_finish", {"session_id": sid, "action": "close"}))


@pytest.mark.pro
@pytest.mark.usefixtures("_gating")
def test_an_unknown_name_goes_on_to_be_described_and_is_not_asked_again(tmp_path,
                                                                        monkeypatch):
    info = _panel(tmp_path, monkeypatch, ["CD3", "CD8", "Mystery1"])
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD8", "Mystery1"])["session_id"]
    packet, _ = next_packet(session, sid)
    outcome = answer(session, sid, packet, {
        "kind": "marker_naming", "names": [{"marker": "Mystery1", "name": "NotAMarker"}]})
    assert outcome["outcome"]["not_in_vocabulary"] == ["Mystery1"]
    packet, _ = next_packet(session, sid)
    assert packet["kind"] == "panel_context"
    assert packet["evidence"]["unresolved"] == ["Mystery1"]
    answer(session, sid, packet, {"kind": "panel_context", "entries": []})
    ok(invoke(session, "gating_session_finish", {"session_id": sid, "action": "close"}))
    # The same panel again: its name was asked once, and is not asked again.
    again = ok(invoke(session, "gating_session_start", {"engine": "v1", "scope": "project",
                                                        "project": "gsynth",
                                                        "markers": ["CD3", "CD8", "Mystery1"]}))
    jobs.drain(120)
    assert again["unresolved_markers"] == ["Mystery1"]
    agent, kinds = Oracle(info), []
    for _ in range(8):
        packet, _ = next_packet(session, again["session_id"])
        if packet is None:
            break
        kinds.append(packet["kind"])
        if packet["kind"] == "t1_strip":
            break
        answer(session, again["session_id"], packet, agent.answer(packet))
    assert kinds and "marker_naming" not in kinds, kinds


@pytest.mark.pro
@pytest.mark.usefixtures("_gating")
def test_an_alias_names_the_marker_and_names_that_disagree_name_nothing(tmp_path,
                                                                        monkeypatch):
    _panel(tmp_path, monkeypatch, ["CD3", "CD8", "Mystery1", "Mystery2"])
    session = AgentSession()
    sid = start(session, markers=["CD3", "CD8", "Mystery1", "Mystery2"])["session_id"]
    packet, _ = next_packet(session, sid)
    assert packet["kind"] == "marker_naming"
    outcome = answer(session, sid, packet, {"kind": "marker_naming", "names": [
        {"marker": "Mystery1", "name": "B-lymphocyte antigen X", "aliases": ["MS4A1"]},
        {"marker": "Mystery2", "name": "CD20", "aliases": ["PTPRC"]}]})["outcome"]
    assert outcome["named"] == {"Mystery1": "CD20"}
    assert outcome["refused"][0]["marker"] == "Mystery2"
    assert outcome["still_unresolved"] == ["Mystery2"]
    ok(invoke(session, "gating_session_finish", {"session_id": sid, "action": "close"}))
