"""Hybrid gating: the cell model beside the vision model.

The pieces, each pinned on its own: the correction of an approved gate
(`correction.solve`, `hybrid.plan_correction`'s rules); the multi-view look
(`collage` layout `t2m`); the session option; the model's runs (a narrower
reach, kept a few to a marker, queued per session); and, in a session, that
`off` is today's session, `tiles` draws every look as the multi-view block
and `model` corrects an approved gate after one question.
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


# -- the correction of an approved gate ------------------------------------------------


def _scene(n_unsure=200, n_not_own=60, n_own=0, seed=0):
    """Values in the fit space: unsure cells spread over [-1, 1], confident
    not-own cells in (0, 0.5], confident own cells in (-0.5, 0]."""
    rng = np.random.default_rng(seed)
    vf = [rng.uniform(-1, 1, n_unsure), rng.uniform(0.001, 0.5, n_not_own),
          rng.uniform(-0.5, 0.0, n_own)]
    p = [np.full(n_unsure, 0.5), np.full(n_not_own, 0.02), np.full(n_own, 0.97)]
    vf, p = np.concatenate(vf), np.concatenate(p)
    order = np.sort(vf)

    def between(a, b):
        return int(np.searchsorted(order, b, "right") - np.searchsorted(order, a, "right"))

    return vf, p, between


def test_the_gate_moves_only_through_values_the_model_says_it_miscalled():
    from plexora.plugins.gating.server.autogate import correction

    vf, p, between = _scene()
    grid = np.round(np.arange(-1.0, 1.0001, 0.1), 6)
    up = correction.solve(grid, 0.0, vf=vf, p=p, between=between, confident=0.1, lam=1.0)
    # 60 confident not-own cells in (0, 0.5], ~50 unsure there: worth crossing.
    assert up["moved"] and up["direction"] == "up" and up["g_star"] == pytest.approx(0.5)
    assert up["flagged_after"] < up["flagged_before"]
    # At a high enough price for the unsure cells, the approved gate stands.
    stay = correction.solve(grid, 0.0, vf=vf, p=p, between=between, confident=0.1, lam=4.0)
    assert not stay["moved"] and stay["g_star"] == 0.0
    # Never past what the looks judged.
    clipped = correction.solve(grid, 0.0, vf=vf, p=p, between=between, confident=0.1,
                               lam=1.0, clip=(-np.inf, 0.2))
    assert clipped["g_star"] <= 0.2 + 1e-9
    # Confident own cells below the gate pull it down.
    vf2, p2, between2 = _scene(n_not_own=0, n_own=60, seed=1)
    down = correction.solve(grid, 0.0, vf=vf2, p=p2, between=between2, confident=0.1,
                            lam=1.0)
    assert down["moved"] and down["direction"] == "down" and down["g_star"] < 0


def test_no_move_without_a_confident_disagreement_or_under_one_step():
    from plexora.plugins.gating.server.autogate import correction

    vf, p, between = _scene(n_not_own=0)
    grid = np.round(np.arange(-1.0, 1.0001, 0.1), 6)
    out = correction.solve(grid, 0.0, vf=vf, p=p, between=between, confident=0.1, lam=0.5)
    assert not out["moved"] and out["changed"] == 0 and out["steps"] == 0
    vf, p, between = _scene()
    far = correction.solve(grid, 0.0, vf=vf, p=p, between=between, confident=0.1, lam=1.0,
                           min_steps=99)
    assert not far["moved"]
    assert correction.model_cut(grid, vf=vf, p=p, confident=0.1) >= 0.5 - 1e-9
    assert correction.model_cut(grid, vf=vf, p=np.full(vf.size, 0.5), confident=0.1) is None


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


# -- the option ---------------------------------------------------------------------


def test_the_hybrid_option_defaults_off_and_resumes_off(monkeypatch):
    from plexora.plugins.gating import capabilities_session as cs
    from plexora.plugins.gating.server.autogate import schemas

    monkeypatch.delenv(schemas.LIMIT_ENV["hybrid"], raising=False)
    assert cs.SessionOptions().hybrid == "off"
    monkeypatch.setenv(schemas.LIMIT_ENV["hybrid"], "full")
    assert cs.SessionOptions().hybrid == "full"
    # A stored session completes as `off` whatever the environment says.
    assert cs.option_defaults()["hybrid"] == "off"
    monkeypatch.setenv(schemas.LIMIT_ENV["hybrid"], "nonsense")
    assert cs.SessionOptions().hybrid == "off"
    assert schemas.HYBRID_FEATURES["full"] == {"tiles", "model"}


# -- the model's runs ------------------------------------------------------------------


class _Counting:
    """A stand-in model: own when the cell's mask is bright against its pixel
    gate (as `test_gating_cellcall`'s)."""

    def __init__(self):
        self.seen = 0

    def __call__(self, x):
        self.seen += len(x)
        own = x[:, 3] > 0
        inside = (x[:, 0] * own).sum(axis=(1, 2)) / np.maximum(own.sum(axis=(1, 2)), 1)
        return (1.0 / (1.0 + np.exp(-6.0 * (inside - np.log1p(1.0))))).astype(np.float32)


def test_a_narrow_sweep_is_stored_beside_others_with_each_cells_value(tmp_path):
    from plexora.plugins.gating.server.autogate import cellcall
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import profile as profmod

    make_gating_project(tmp_path, "sw", grid=16, size=768)
    session = AgentSession()
    try:
        ds = session.data("sw")
        values = cellmod.values(ds, "CD8")
        gate = float(np.nanmedian(values[values > np.nanpercentile(values, 60)]))
        gates = {"CD8": gate, "CD3": float(np.nanmedian(cellmod.values(ds, "CD3")))}
        model = _Counting()
        found = cellcall.sweep(session, ds, [
            {"marker": "CD8", "low": gate, "gates": gates, "band_halves": 2.0,
             "max_cells": 50}], model=model)["CD8"]
        info = found["info"]
        assert info["n_run"] <= 50 and info["band_halves"] == 2.0
        # Each cell's value in the fit space beside its p: the run can be read
        # at another gate (the correction's grid).
        col = profmod.column(ds, "CD8")
        assert found["vf"].shape == found["p"].shape
        assert np.all(np.abs(found["vf"] - col.to_fit(gate)) <= 2.0 * info["band_half"] + 1e-5)
        # A marker keeps its newest few runs: a session reads it at more than
        # one gate.
        for factor in (1.02, 1.04, 1.06):
            cellcall.run(session, ds, "CD8", gate * factor, gates=gates, model=model,
                         band_halves=2.0, max_cells=50)
        kept = list(tmp_path.rglob("cellcall/CD8_*.npz"))
        assert len(kept) == cellcall.CELLCALL["keep_runs"]
        assert cellcall.load(ds, "CD8", gate * 1.06) is not None
    finally:
        session.close()


def test_a_run_reads_the_same_with_one_worker_or_several(tmp_path):
    from plexora.plugins.gating.server.autogate import cellcall
    from plexora.plugins.gating.server.autogate import cells as cellmod

    class _Parallel(_Counting):
        workers = 3

    make_gating_project(tmp_path, "par", grid=16, size=768)
    session = AgentSession()
    try:
        ds = session.data("par")
        values = cellmod.values(ds, "CD8")
        gate = float(np.nanmedian(values[values > np.nanpercentile(values, 60)]))
        one = cellcall.run(session, ds, "CD8", gate, model=_Counting(), refresh=True)
        many = cellcall.run(session, ds, "CD8", gate, model=_Parallel(), refresh=True)
        np.testing.assert_array_equal(one["rows"], many["rows"])
        np.testing.assert_allclose(one["p"], many["p"], rtol=0, atol=1e-6)
    finally:
        session.close()


# -- in a session ------------------------------------------------------------------------


def _start(session, project, **options):
    from plexora.agent import invoke, jobs
    from tests.test_gating_session import ok

    started = ok(invoke(session, "gating_session_start", {"scope": "project",
                                                          "project": project, **options}))
    jobs.drain(120)
    return started


@pytest.mark.paid
def test_hybrid_off_is_todays_session(tmp_path):
    from plexora.agent import invoke
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import provenance
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
        again = _start(session, "gsynth", mode="propose", agent="same", hybrid="off")
        replayed = drive(session, again["session_id"], Oracle(info))
        status = ok(invoke(session, "gating_session_status",
                           {"session_id": again["session_id"]}))
        assert replayed == []
        assert status["summary"]["replayed"] == plain["summary"]["replayed"] > 0
        assert {m: (u["state"], u.get("proposed")) for m, u in gates.items()} == \
            {m: (u["state"], u.get("proposed"))
             for m, u in units(session, again["session_id"]).items()}
        for row in provenance.read("gsynth").values():
            assert "cellcall" not in (row.get("detail") or {})
        store = engines.store()
        assert "cellcall" not in store.load(again["session_id"])
        for event in store.decisions(again["session_id"]):
            if event.get("event") == "issued":
                packet, _images = store.read_packet(again["session_id"], event["packet_id"])
                assert "partner_panels" not in packet["evidence"]
                assert packet["kind"] != "correction_check"
    finally:
        session.close()


@pytest.mark.paid
def test_tiles_draw_every_look_as_the_multi_view_block(tmp_path):
    from plexora.agent import artifacts
    from tests.test_gating_session import Oracle, drive

    info = make_gating_project(tmp_path, grid=24, size=960, markers=("CD3", "CD8", "CD4"))
    session = AgentSession()
    try:
        started = _start(session, "gsynth", mode="propose", agent="tiles", hybrid="tiles",
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


@pytest.mark.paid
@pytest.mark.parametrize("confirm", [False, True])
def test_the_model_corrects_an_approved_gate(monkeypatch, confirm):
    """On a scene whose dim positives the looks stop above, the oracle cell
    model's confident own cells below the approved gate move it down --
    applied as computed, or after one `correction_check` the truth agrees
    with (`HYBRID["confirm"]`) -- and the gates improve."""
    from plexora.ai import bench
    from plexora.plugins.gating.server.autogate import hybrid

    monkeypatch.setitem(hybrid.HYBRID, "confirm", confirm)
    rows = bench.run_synthetic(["dim_positive"], "oracle", arms=("session",),
                               variants=("baseline", "model"))
    code = {r["arm"]: r["code_agreement"] for r in rows if r["marker"] == "*code*"}
    cost = {r["arm"]: r["packets"] for r in rows if r["marker"] == "*cost*"}
    assert code["session:model"] > code["session:baseline"]
    if confirm:
        assert cost["session:baseline"] < cost["session:model"] <= cost["session:baseline"] + 2
    else:
        assert cost["session:model"] == cost["session:baseline"]
    model_rows = [r for r in rows if r["arm"] == "session:model" and r.get("gate") is not None]
    assert all(r.get("state") in ("accepted", "accepted_low_confidence") for r in model_rows)


def test_the_correction_never_crosses_what_a_look_judged():
    from types import SimpleNamespace

    from plexora.plugins.gating.server.autogate import hybrid

    col = SimpleNamespace(to_fit=lambda v: float(v))
    unit = {"candidate": 5.0, "judged": [
        [4.6, 4.8, "unstained"], [4.8, 5.0, "neighbour_only"],     # passed going up
        [5.0, 5.2, "own_stain"],                                     # where it stopped
        [5.4, 5.6, "unstained"],                                     # above: not a limit
        [4.0, 4.2, "own_stain"],                                     # below: not a limit
        [5.2, 5.4, "mixed"]]}                                        # no word, no limit
    assert hybrid.judged_limits(unit, col) == (5.0, 5.0)
    assert hybrid.judged_limits({"candidate": 5.0}, col) == (float("-inf"), float("inf"))


def test_a_correction_is_taken_only_when_the_cells_agree_with_it():
    from plexora.plugins.gating.server.autogate import hybrid

    def unit(direction):
        return {"candidate": 5.0, "model": {"correction": {
            "status": "asked", "g_v": 5.0, "g_star": 5.3 if direction == "up" else 4.7,
            "direction": direction}}}

    up = unit("up")
    record = hybrid.apply_answer(None, up, {"c1": "not_own", "c2": "not_own", "c3": "own",
                                            "c4": "cannot_tell"})
    assert record["status"] == "accepted" and up["candidate"] == 5.3
    assert record["answered"] == 3 and record["agree"] == 2
    down = unit("down")
    record = hybrid.apply_answer(None, down, {"c1": "not_own", "c2": "not_own", "c3": "own"})
    assert record["status"] == "rejected" and down["candidate"] == 5.0
    assert "model_disagrees" in down["look_flags"]
    nothing = unit("up")
    assert hybrid.apply_answer(None, nothing, {"c1": "cannot_tell"})["status"] == "rejected"


def test_the_queue_keeps_one_run_a_purpose_and_the_wait_follows_it():
    from types import SimpleNamespace

    from plexora.plugins.gating.server.autogate import hybrid

    unit = {"project": "p", "marker": "CD8", "high": None}
    engine = SimpleNamespace(record={"units": {"p::CD8": unit}})
    first = hybrid.request(engine, unit, 1.0, purpose="decide", wait=True)
    assert hybrid.waiting(unit) and unit["model"]["waiting"] == first
    # The same run asked again is the one queued.
    assert hybrid.request(engine, unit, 1.0, purpose="decide") is None
    # A run for a gate the unit moved to replaces the one not started, and
    # the wait follows it.
    second = hybrid.request(engine, unit, 1.2, purpose="decide")
    queue = hybrid.queue_of(engine.record)["queue"]
    assert [r["id"] for r in queue] == [second] and unit["model"]["waiting"] == second
    # A run started is never dropped; the layer is a purpose of its own.
    queue[0]["taken"] = "job"
    third = hybrid.request(engine, unit, 1.3, purpose="decide", wait=True)
    layer = hybrid.request(engine, unit, 1.3, purpose="layer")
    assert [r["id"] for r in hybrid.queue_of(engine.record)["queue"]] == [second, third, layer]
    hybrid.done(engine, {**queue[0]}, {"status": "ready", "low": 1.2})
    assert unit["model"]["waiting"] == third and unit["model"]["runs"]["decide"]["low"] == 1.2
    hybrid.done(engine, {"id": third, "unit": "p::CD8", "purpose": "decide", "low": 1.3},
                {"status": "unavailable", "why": "no mask"})
    assert not hybrid.waiting(unit) and unit["model"]["unavailable"] == "no mask"
    assert hybrid.request(engine, unit, 1.4, purpose="decide") is None   # never again


def test_a_gate_written_for_review_is_corrected_from_a_stored_run(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from plexora.plugins.gating.server.autogate import cellcall, correction, hybrid
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import profile as profmod

    make_gating_project(tmp_path, "rv", grid=16, size=768)
    session = AgentSession()
    try:
        ds = session.data("rv")
        values = cellmod.values(ds, "CD8")
        gate = float(np.nanmedian(values[values > np.nanpercentile(values, 60)]))
        found = cellcall.run(session, ds, "CD8", gate, model=_Counting(), band_halves=6.0,
                             max_cells=2000)
        col = profmod.column(ds, "CD8")
        h = float(found["info"]["band_half"])
        points = [{"low": float(col.from_fit(col.to_fit(gate) + k * h))} for k in range(-4, 5)]
        engine = SimpleNamespace(call=SimpleNamespace(session=session),
                                 lattice_for=lambda unit: {"points": points})

        def unit(**model):
            return {"project": "rv", "marker": "CD8", "path": "t2", "candidate": gate,
                    "high": None, "model": model}

        # No run stored for it: the review gate is written as it is.
        bare = unit()
        assert hybrid.correct_for_review(engine, bare) is None and bare["candidate"] == gate
        ran = {"runs": {"decide": {"status": "ready", "low": gate, "model": found["info"]["model"]}}}
        target = float(col.to_fit(gate) + 2 * h)
        monkeypatch.setattr(correction, "solve", lambda grid, g_v, **kw: {
            "moved": True, "g_star": target, "direction": "up", "steps": 2,
            "flagged_before": 9, "flagged_after": 2, "changed": 30})
        moved = unit(**ran)
        corrected = hybrid.correct_for_review(engine, moved)
        assert corrected == pytest.approx(float(col.from_fit(target)))
        assert moved["candidate"] == corrected
        assert moved["model"]["correction"]["status"] == "applied_review"
        # Once per unit.
        assert hybrid.correct_for_review(engine, moved) is None
    finally:
        session.close()
