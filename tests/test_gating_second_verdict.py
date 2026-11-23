"""A whole-image check answered by a second reader that never saw it, and a
third when they differ (`autogate.second_verdict`, 2026-11-23)."""

from __future__ import annotations

import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from tests.autogate_fixtures import make_gating_project

HARD = ("CD3", "CD8", "CD20", "CD4", "FOXP3")


def test_two_alike_stand_two_apart_take_a_third_and_none_alike_abstain():
    from plexora.plugins.gating.server.autogate.second_verdict import combine

    assert combine(["technical_failure"], "cannot_tell") is None
    assert combine(["technical_failure", "technical_failure"], "cannot_tell") == "technical_failure"
    assert combine(["technical_failure", "cannot_tell"], "cannot_tell") is None
    assert combine(["technical_failure", "cannot_tell", "technical_failure"],
                   "cannot_tell") == "technical_failure"
    assert combine(["real_signal", "cannot_tell", "technical_failure"],
                   "cannot_tell") == "cannot_tell"


def _drive(tmp_path, check_rereads, *, contrary_second=False):
    """A session read by a truthful reader; with `contrary_second` the
    second reading of every whole-image check answers `cannot_tell` where
    the first did not (`suspicious` where it was). Returns (check packets
    issued in order, the session record)."""
    from plexora.plugins.gating.server.autogate import engine as engines
    from tests.test_gating_session import Oracle, ok

    registry.discover(["gating"])
    info = make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)
    session = AgentSession()
    started = ok(invoke(session, "gating_session_start", {
        "scope": "project", "project": "gsynth", "markers": list(HARD), "engine": "v5",
        "panel_rereads": "off", "check_rereads": check_rereads}))
    jobs.drain(120)
    sid = started["session_id"]
    oracle = Oracle(info)
    index = {c["id"]: i for i, c in enumerate(info["cells"])}
    checks = []
    result = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 20}))
    for _ in range(400):
        if result["state"] != "decision":
            break
        packet = result["packet"]
        if packet["kind"] == "cell_panel":
            stored, _ = engines.store().read_packet(sid, packet["packet_id"])
            truth = oracle.truth[packet["units"][0]["marker"]]
            reply = {"kind": "cell_panel", "cells": {
                c["number"]: "own_stain" if truth[index[c["cell_id"]]] else "not_own_stain"
                for c in stored["_panel"]["cells"]}}
        else:
            reply = oracle.answer(packet)
            if packet["kind"] in ("qc_confirm", "t1_strip"):
                checks.append(packet)
                read = (packet.get("evidence") or {}).get("check")
                if contrary_second and read == "reader_2":
                    if packet["kind"] == "qc_confirm":
                        reply = {**reply, "verdict": "cannot_tell"
                                 if reply["verdict"] != "cannot_tell" else "real_signal"}
                    else:
                        reply = {**reply, "verdicts": {
                            m: "cannot_tell" if v != "cannot_tell" else "suspicious"
                            for m, v in reply["verdicts"].items()}}
        result = ok(invoke(session, "gating_answer", {
            "session_id": sid, "packet_id": packet["packet_id"], "answer": reply}))["next"]
    assert result["state"] in ("decided", "done"), result
    return checks, engines.store().load(sid)


@pytest.mark.pro
def test_every_whole_image_check_is_answered_again_by_a_reader_of_its_own(tmp_path):
    checks, record = _drive(tmp_path, "all", contrary_second=True)
    assert any(p["kind"] == "t1_strip" for p in checks), [p["kind"] for p in checks]
    by_id = {p["packet_id"]: p for p in checks}
    settled = 0
    for unit in record["units"].values():
        assert not unit.get("verdict_reads"), unit["marker"]      # nothing left half read
        for entry in unit.get("verdict_history") or ():
            settled += 1
            packets = [by_id[pid] for pid in entry["packets"]]
            # The first reading is the image reader's; each later one goes to
            # the task of that reading -- a worker that never saw the check.
            assert packets[0]["task"] == "gating.image_inspection"
            for n, p in enumerate(packets[1:], start=2):
                assert p["task"] == f"gating.image_reader_{n}"
                assert p["evidence"]["check"] == f"reader_{n}"
                assert p["kind"] == packets[0]["kind"]
            # The contrary second reading asks for a third, whose verdict --
            # the first's -- is the one two gave.
            assert len(entry["verdicts"]) == 3, entry
            assert entry["settled"] == entry["verdicts"][0] == entry["verdicts"][2]
    assert settled


@pytest.mark.pro
def test_with_check_rereads_off_a_check_is_one_verdict(tmp_path):
    checks, record = _drive(tmp_path, "off")
    assert checks
    assert all(p["task"] == "gating.image_inspection" for p in checks)
    assert all(not (p.get("evidence") or {}).get("check") for p in checks)
    assert all(not u.get("verdict_history") for u in record["units"].values())
