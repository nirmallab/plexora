"""The multi-view looks (`SessionOptions.tiles`): each cell a 2 x 2 block.

The pieces, each pinned on its own: the collage layout `t2m`; the partners
its bottom panels show (`partners.partner_plan`, from the panel context's
vocabulary alone); the session option and the memo key it keeps apart; a
session stored with the removed `hybrid` option resuming; and, in a session,
that off is today's session and on draws every look as the block.
"""

import pytest

from plexora.agent import AgentSession, cell_exclusions, registry
from tests.autogate_fixtures import make_gating_project


@pytest.fixture(autouse=True)
def _plugins():
    registry.discover(["gating"])
    cell_exclusions._reset_for_tests()
    yield
    cell_exclusions._reset_for_tests()


# -- the multi-view look ----------------------------------------------------------------


def test_the_multi_view_collage_draws_partners_and_redraws_byte_for_byte(tmp_path):
    from plexora.agent.evidence import collage
    from plexora.plugins.gating.server.autogate import cells as cellmod

    make_gating_project(tmp_path, grid=16, size=640)
    session = AgentSession()
    try:
        ds = session.data("gsynth")
        c = cellmod.cells(ds)
        picked = [{"cell_id": int(c.ids[i]), "x": float(c.xs[i]), "y": float(c.ys[i]),
                   "value": 1.0} for i in range(0, 60, 5)]
        rows = [{"label": "a", "key": "below", "cells": picked[:6]},
                {"label": "b", "key": "above", "cells": picked[6:]}]
        partners = {"co": "CD3", "anti": "CD20", "gates": {"CD3": 2.0}}
        out = collage.render_collage(session, ds, layout="t2m", rows=rows, marker="CD8",
                                     gate=1.5, partners=partners, fmt="png", store=False)
        manifest = out["manifest"]
        assert manifest["panels"] == ["morphology", "outlined", "co", "anti"]
        assert tuple(manifest["size"]) == collage.layout_size("t2m", rows=2)
        assert manifest["tile_px"] == 64
        assert manifest["request"]["partners"]["co"] == "CD3"
        assert set(manifest["partner_windows"]) == {"CD3", "CD20"}
        again = collage.rerender(session, ds, manifest)
        assert again["png"] == out["png"]
        # Without partners the partner panels are dark, never another channel.
        bare = collage.render_collage(session, ds, layout="t2m", rows=rows, marker="CD8",
                                      gate=1.5, fmt="png", store=False)
        assert "partners" not in bare["manifest"]["request"]
    finally:
        session.close()


def test_the_multi_view_look_costs_at_most_a_fifth_more_pixels():
    from plexora.agent.evidence import collage

    for rows in (3, 4):
        assert collage.layout_pixels("t2m", rows) <= 1.2 * collage.layout_pixels("t2", rows)


# -- the partners ---------------------------------------------------------------------


def test_the_partner_plan_reads_the_vocabularys_relations():
    from plexora.plugins.gating.server.autogate import partners

    entries = {
        "CD8": {"canonical": "CD8", "partners": [
            {"marker": "CD3", "relation": "subset", "confidence": "high"},
            {"marker": "CD3e", "relation": "subset", "confidence": "high"},
            {"marker": "CD4", "relation": "exclusive", "confidence": "moderate"}]},
        "CD3": {"canonical": "CD3", "partners": [
            {"marker": "CD45", "relation": "subset", "confidence": "high"},
            {"marker": "CD20", "relation": "exclusive", "confidence": "high"}]},
        "CD3e": {"canonical": "CD3"},
        "CD45": {"canonical": "CD45"}, "CD20": {"canonical": "CD20"}, "CD4": {"canonical": "CD4"},
        "PanCK": {"canonical": "PanCK", "partners": [
            {"marker": "CD8", "relation": "exclusive", "confidence": "high"}]},
    }
    present = {"CD3", "CD3e", "CD45", "CD20", "CD4", "PanCK"}
    plan = partners.partner_plan(entries, "CD8", present, {}, 2)
    co = [p["marker"] for p in plan if p["kind"] == "co"]
    anti = [p["marker"] for p in plan if p["kind"] == "anti"]
    # Its own superset first, then the superset's superset; never a channel
    # read as the same marker twice (CD3 and CD3e).
    assert co == ["CD3", "CD45"]
    # Exclusive either way, and those of its supersets.
    assert set(anti) <= {"CD4", "CD20", "PanCK"} and len(anti) == 2
    assert {"PanCK", "CD20"} == set(anti)            # high before moderate
    # A gated partner comes first, whatever its confidence.
    gated = partners.partner_plan(entries, "CD8", present, {}, 2, gated={"CD4"})
    assert [p["marker"] for p in gated if p["kind"] == "anti"][0] == "CD4"
    # A partner without an image channel is never planned.
    assert all(p["marker"] != "CD45" for p in
               partners.partner_plan(entries, "CD8", present - {"CD45"}, {}, 2))


# -- the option ---------------------------------------------------------------------


def test_tiles_default_off_and_keep_their_answers_apart():
    from types import SimpleNamespace

    from plexora.plugins.gating import capabilities_session as cs
    from plexora.plugins.gating.server.autogate import memo
    from plexora.plugins.gating.server.autogate.engine import Engine

    assert cs.SessionOptions().tiles is False
    assert cs.option_defaults()["tiles"] is False
    packet = {"kind": "t2_confirm", "evidence": {"marker": "CD8"}}

    def key(options):
        fake = SimpleNamespace(options=options)
        fake.tiles_on = lambda: Engine.tiles_on(fake)
        return Engine.memo_key(fake, packet, [])

    # Off, the key is the one a session without the option made.
    assert key({"tiles": False}) == memo.key(packet, [], versions=())
    assert key({"tiles": True}) != key({"tiles": False})


def test_a_session_stored_with_the_cell_model_resumes_without_it(tmp_path):
    """A record from before the cell model was removed: its `hybrid` tiles
    become `tiles`, a unit waiting on the model's correction goes back to its
    regression, the correction packet still out is dropped, and what else the
    model left is never read."""
    from types import SimpleNamespace

    from plexora.agent.sessions.store import SessionStore
    from plexora.plugins.gating.server.autogate.engine import Engine

    st = SessionStore("gating", root=tmp_path)
    for option, tiles in (("full", True), ("tiles", True), ("model", False), ("off", False)):
        record = {"session_id": f"s_{option}", "options": {"hybrid": option},
                  "model_withheld": ["A"], "cellcall": {"queue": [], "job_id": None},
                  "units": {"A::CD8": {"project": "A", "marker": "CD8",
                                       "state": "awaiting_correction", "model": {"runs": {}}},
                            "A::CD3": {"project": "A", "marker": "CD3", "state": "awaiting_t2"}},
                  "outstanding": {"p1": {"kind": "correction_check", "units": ["A::CD8"]},
                                  "p2": {"kind": "t2_confirm", "units": ["A::CD3"]}},
                  "outstanding_packet": "p2", "outstanding_kind": "t2_confirm"}
        st.create(record)
        engine = Engine(SimpleNamespace(), f"s_{option}", st=st)
        assert engine.options["tiles"] is tiles and "hybrid" not in engine.options
        assert engine.tiles_on() is tiles
        assert engine.record["units"]["A::CD8"]["state"] == "awaiting_regression"
        assert engine.record["units"]["A::CD3"]["state"] == "awaiting_t2"
        assert list(engine.record["outstanding"]) == ["p2"]
        assert engine.record["outstanding_kind"] == "t2_confirm"


# -- in a session ------------------------------------------------------------------------


def _start(session, project, **options):
    from plexora.agent import invoke, jobs
    from tests.test_gating_session import ok

    started = ok(invoke(session, "gating_session_start", {"engine": "v1", "scope": "project",
                                                          "project": project, **options}))
    jobs.drain(120)
    return started


@pytest.mark.pro
def test_tiles_off_is_todays_session(tmp_path):
    from plexora.agent import invoke
    from plexora.plugins.gating.server.autogate import engine as engines
    from tests.test_gating_session import Oracle, drive, ok, units

    info = make_gating_project(tmp_path, grid=24, size=960, markers=("CD3", "CD8", "CD4"))
    session = AgentSession()
    try:
        for _ in range(3):
            first = _start(session, "gsynth", mode="propose", agent="same")
            drive(session, first["session_id"], Oracle(info))
        gates = units(session, first["session_id"])
        plain = ok(invoke(session, "gating_session_status", {"session_id": first["session_id"]}))
        # The option said out loud: the same packets, so every answer replayed.
        again = _start(session, "gsynth", mode="propose", agent="same", tiles=False)
        replayed = drive(session, again["session_id"], Oracle(info))
        status = ok(invoke(session, "gating_session_status",
                           {"session_id": again["session_id"]}))
        assert replayed == []
        assert status["summary"]["replayed"] == plain["summary"]["replayed"] > 0
        assert {m: (u["state"], u.get("proposed")) for m, u in gates.items()} == \
            {m: (u["state"], u.get("proposed"))
             for m, u in units(session, again["session_id"]).items()}
        store = engines.store()
        for event in store.decisions(again["session_id"]):
            if event.get("event") == "issued":
                packet, _images = store.read_packet(again["session_id"], event["packet_id"])
                assert "partner_panels" not in packet["evidence"]
    finally:
        session.close()


@pytest.mark.pro
def test_tiles_draw_every_look_as_the_multi_view_block(tmp_path):
    from plexora.agent import artifacts
    from tests.test_gating_session import Oracle, drive

    info = make_gating_project(tmp_path, grid=24, size=960, markers=("CD3", "CD8", "CD4"))
    session = AgentSession()
    try:
        started = _start(session, "gsynth", mode="propose", agent="tiles", tiles=True,
                         reuse_answers=False)
        seen = drive(session, started["session_id"], Oracle(info))
        looks = [p for p in seen if p["kind"] in ("t2_confirm", "t3_biological",
                                                 "t4_candidates")]
        assert looks
        for packet in looks:
            assert "partner_panels" in packet["evidence"]
            guide = packet["evidence"].get("guide") or []
            assert "collage_t2m" in guide and "collage_t2" not in guide
            art = packet["images"][0]["artifact_id"]
            assert artifacts.get(art)[1]["manifest"]["layout"] == "t2m"
    finally:
        session.close()
