"""Automatic gating's deterministic parts, one at a time, against known truth."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

from tests.autogate_fixtures import FakeData, make_gating_project, populations


# -- compiled kernels ----------------------------------------------------------------


def test_neighbour_counts_match_a_kd_tree():
    from scipy.spatial import cKDTree

    from plexora.plugins.gating.server.autogate import kernels

    rng = np.random.default_rng(3)
    xs, ys = rng.uniform(0, 3000, 20_000), rng.uniform(0, 2000, 20_000)
    flags = rng.random(20_000) < 0.3
    grid = kernels.Grid(xs, ys, 45.0)
    n, f = kernels.neighbour_counts(grid, flags=flags)
    tree = cKDTree(np.c_[xs, ys])
    expected = tree.query_ball_point(np.c_[xs, ys], 45.0, return_length=True) - 1
    assert np.array_equal(n, expected)
    some = rng.choice(20_000, 300, replace=False)
    for i in some[:50]:
        hits = [j for j in tree.query_ball_point([xs[i], ys[i]], 45.0) if j != i]
        assert f[i] == int(flags[hits].sum())
    q, _ = kernels.neighbour_counts(grid, query=some)
    assert np.array_equal(q, expected[some])


def test_label_kernels_match_numpy():
    from plexora.server.utils import label_kernels, label_overlay

    rng = np.random.default_rng(1)
    labels = rng.integers(0, 9, (97, 131)).astype(np.uint32)
    for radius in (1, 2, 3):
        assert np.array_equal(label_kernels.boundary_mask_kernel(labels, radius),
                              label_overlay.boundary_mask_numpy(labels, radius))
    boxes = label_kernels.label_bboxes(labels)
    for i, label in enumerate(boxes["ids"]):
        ys, xs = np.nonzero(labels == label)
        assert boxes["count"][i] == ys.size
        assert (boxes["y0"][i], boxes["x0"][i], boxes["y1"][i], boxes["x1"][i]) == \
            (ys.min(), xs.min(), ys.max(), xs.max())
        assert boxes["cy"][i] == pytest.approx(ys.mean())


def test_kernels_give_the_same_answers_without_numba(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(f"""
        import sys, json
        sys.path.insert(0, {str(repo)!r})
        import numpy as np
        from plexora.server.utils import jit, label_kernels
        from plexora.plugins.gating.server.autogate import kernels
        assert not jit.enabled()
        rng = np.random.default_rng(3)
        xs, ys = rng.uniform(0, 500, 800), rng.uniform(0, 500, 800)
        n, f = kernels.neighbour_counts(kernels.Grid(xs, ys, 30.0), flags=xs > 250)
        labels = rng.integers(0, 5, (40, 40)).astype(np.uint32)
        edge = label_kernels.boundary_mask_kernel(labels, 2)
        print(json.dumps([int(n.sum()), int(f.sum()), int(edge.sum())]))
    """)
    env = {**os.environ, "PLEXORA_NO_NUMBA": "1"}
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          env=env, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    plain = json.loads(done.stdout.strip().splitlines()[-1])
    from plexora.plugins.gating.server.autogate import kernels
    from plexora.server.utils import label_kernels

    rng = np.random.default_rng(3)
    xs, ys = rng.uniform(0, 500, 800), rng.uniform(0, 500, 800)
    n, f = kernels.neighbour_counts(kernels.Grid(xs, ys, 30.0), flags=xs > 250)
    labels = rng.integers(0, 5, (40, 40)).astype(np.uint32)
    edge = label_kernels.boundary_mask_kernel(labels, 2)
    assert plain == [int(n.sum()), int(f.sum()), int(edge.sum())]


def test_priming_is_idempotent():
    from plexora.server.utils import jit

    first = jit.prime()
    assert jit.prime() == []  # already primed
    assert set(first) <= set(jit._PRIMERS)


# -- the profile -----------------------------------------------------------------------


def _profile(values, **kwargs):
    from plexora.plugins.gating.server.autogate import profile

    return profile.profile_marker(FakeData({"M": values}), "M", **kwargs)


def test_a_clean_bimodal_marker_is_accepted_at_its_crossover():
    values, truth = populations(200_000, 0.2, bg=(4.0, 0.5), pos=(7.0, 0.4))
    p = _profile(values)
    assert p["distribution_class"] == "bimodal"
    assert p["t1"]["accept"] and p["t1"]["recommended_tier"] == "T1"
    assert p["positive_fraction"] == pytest.approx(truth.mean(), abs=0.005)


def test_classes_follow_the_shape():
    one, _ = populations(50_000, 0.0, bg=(4.0, 0.5))
    assert _profile(one)["distribution_class"] == "unimodal"
    overlap, _ = populations(50_000, 0.3, bg=(4.0, 0.6), pos=(4.9, 0.6))
    assert _profile(overlap)["distribution_class"] in ("continuous", "weakly_bimodal")
    assert not _profile(overlap)["t1"]["accept"]
    flat = np.full(5_000, 7.0, dtype=np.float32)
    assert _profile(flat)["distribution_class"] == "degenerate"


def test_the_profile_is_deterministic_and_small():
    values, _ = populations(60_000, 0.1)
    a, b = _profile(values, seed=3), _profile(values, seed=3)
    a.pop("cost_ms"), b.pop("cost_ms")
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True,
                                                                     default=str)
    from plexora.plugins.gating.server.autogate import engine

    assert len(json.dumps(engine.compact_profile(a))) < 2_000


def test_positive_counts_are_the_viewers_float32_rule():
    from plexora.agent import gate_rule
    from plexora.plugins.gating.server.autogate import profile

    values = np.array([0.1, 1.0, 1.0000001, 2.5, 16777217.0, np.nan], dtype=np.float64)
    col = profile.Column("M", values, False)
    for low in (0.0, 1.0, 2.5, 3.0):
        expected = int(gate_rule.passes(values, low, float(np.nanmax(values))).sum())
        assert col.n_positive(low) == expected


def test_two_positive_subsets_are_one_population():
    """A fit of background + two nearby bright components gates below both."""
    from plexora.plugins.gating.server.autogate import profile

    fitted = (np.array([4.1, 6.97, 7.25]), np.array([0.22, 0.17, 0.17]),
              np.array([0.5, 0.28, 0.22]))
    pools = profile.pools(fitted)
    assert pools["split"] == 1
    assert 4.5 < pools["gate"] < 6.5
    zero_inflated = (np.array([0.1, 4.0, 7.0]), np.array([0.1, 0.5, 0.4]),
                     np.array([0.2, 0.6, 0.2]))
    assert profile.pools(zero_inflated)["split"] == 2


# -- sampling ------------------------------------------------------------------------------


def _fake(n=120_000, fraction=0.2, seed=0):
    values, _ = populations(n, fraction, seed=seed)
    return FakeData({"M": values, "N": populations(n, 0.3, seed=seed + 5)[0]}, seed=seed)


def test_strata_hold_exactly_the_cells_in_their_band():
    from plexora.plugins.gating.server.autogate import profile, sampler

    ds = _fake()
    col = profile.column(ds, "M")
    gate = profile.profile_marker(ds, "M")["fit"]["gate_raw"]
    s = sampler.stratified_cells(ds, "M", gate, seed=1)
    edges = [-np.inf] + s["edges_fit"] + [np.inf]
    seen = set()
    for index, name in enumerate(sampler.STRATA):
        for cell in s["strata"].get(name, []):
            f = float(col.to_fit(cell["value"]))
            assert edges[index] <= f <= edges[index + 1] + 1e-9, (name, f)
            assert cell["cell_id"] not in seen
            seen.add(cell["cell_id"])
    again = sampler.stratified_cells(ds, "M", gate, seed=1)
    assert json.dumps(again, sort_keys=True) == json.dumps(s, sort_keys=True)
    borderline = s["strata"]["borderline"]
    tiles = {(int(c["x"] // 1250), int(c["y"] // 1250)) for c in borderline}
    assert len(tiles) >= min(6, len(borderline))


def test_delta_cells_are_the_cells_that_flip():
    from plexora.plugins.gating.server.autogate import profile, sampler

    ds = _fake()
    gate = profile.profile_marker(ds, "M")["fit"]["gate_raw"]
    thresholds = [gate * 0.7, gate, gate * 1.4]
    d = sampler.delta_cells(ds, "M", thresholds)
    for interval in d["intervals"]:
        for cell in interval["cells"]:
            v = np.float32(cell["value"])
            assert np.float32(interval["from"]) < v <= np.float32(interval["to"])
    assert all(c["value"] <= thresholds[0] or c["value"] > thresholds[-1]
               for c in d["regression"])


def test_candidates_move_one_way_inside_the_guard_band():
    from plexora.plugins.gating.server.autogate import candidates, profile

    ds = _fake(fraction=0.25)
    gate = profile.profile_marker(ds, "M")["fit"]["gate_raw"]
    for direction, start in (("up", gate * 0.8), ("down", gate * 1.2)):
        c = candidates.candidate_thresholds(ds, "M", current_low=start, direction=direction)
        lows = [x["low"] for x in c["candidates"]]
        assert lows, c["removed"]
        assert all((x > start) if direction == "up" else (x < start) for x in lows)
        assert lows == sorted(lows, reverse=(direction == "down"))
        assert all(c["guard"]["low"] - 1e-6 <= x <= c["guard"]["high"] + 1e-6 for x in lows)


def test_bivariate_quadrants_match_a_brute_force_count():
    from plexora.agent import gate_rule
    from plexora.plugins.gating.server.autogate import bivariate

    ds = _fake()
    a = ds.table.columns(["M"])["M"]
    b = ds.table.columns(["N"])["N"]
    result = bivariate.bivariate_numbers(ds, "M", 400.0, "N", 300.0, relation="coexpressed")
    pa = gate_rule.passes(a, 400.0, np.nanmax(a))
    pb = gate_rule.passes(b, 300.0, np.nanmax(b))
    assert result["quadrants"] == {"neither": int((~pa & ~pb).sum()),
                                   "b_only": int((~pa & pb).sum()),
                                   "a_only": int((pa & ~pb).sum()),
                                   "both": int((pa & pb).sum())}
    assert 0.0 <= result["contradiction"] <= 1.0


# -- the gate grid, locks and provenance ----------------------------------------------------


@pytest.mark.parametrize("low, high, rng, expected", [
    (612.7, 65535.0, (0, 65535), (612.0, 65535.0)),
    (0.29, 10.0, (0, 10), (0.29, 10.0)),
    (1.23456, 2.2, (1.7, 2.2), (1.234, 2.2)),
    (5.00001, 7.99999, (0, 8), (5.0, 8.0)),
])
def test_snap_to_grid_is_the_sidebars_rounding(low, high, rng, expected):
    """normalizeGateRange: low floored, high ceiled, at ~200 steps of the range."""
    from plexora.plugins.gating.server import model

    assert model.snap_to_grid(low, high, {"min": rng[0], "max": rng[1]}) == \
        pytest.approx(expected)


def test_a_locked_gate_survives_the_sidebars_own_save(tmp_path):
    from plexora.plugins.gating.server.autogate import provenance

    make_gating_project(tmp_path, grid=8, size=384)
    provenance.set_status("gsynth", "CD8", "locked", current=(120.0, 999.0))
    saved = [{"channel": "CD8", "gate_start": 50.0, "gate_end": 999.0},
             {"channel": "CD3", "gate_start": 70.0, "gate_end": 800.0}]
    rows, reverted = provenance.merge_locked("gsynth", saved)
    assert reverted == ["CD8"]
    assert rows[0]["gate_start"] == 120.0 and rows[1]["gate_start"] == 70.0


# -- biology ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name, canonical", [
    ("CD8a", "CD8"), ("cd8_AF488", "CD8"), ("CD8-2", "CD8"), ("Ki-67", "Ki67"),
    ("pan-CK", "PanCK"), ("AE1/AE3", "PanCK"), ("HLA-DR", "HLADR"), ("DAPI", "DNA"),
    ("Ir191", "DNA"), ("anti-CD20", "CD20"), ("aSMA_Opal570", "aSMA"), ("mystery", None),
])
def test_marker_names_fold_to_the_vocabulary(name, canonical):
    from plexora.ai import vocabulary

    assert vocabulary.canonical(name) == canonical


def test_partners_are_gated_before_the_markers_that_need_them():
    from plexora.plugins.gating.server.autogate import context

    panel = context.build(["DNA", "CD8", "FOXP3", "CD4", "CD3", "CD45", "PanCK", "Ki67",
                           "pERK", "Mystery"])
    order = panel["order"]
    assert "DNA" not in order
    assert order.index("CD3") < order.index("CD8")
    assert order.index("CD4") < order.index("FOXP3")
    assert order.index("CD45") < order.index("CD3")
    assert order[-1] == "Mystery" and panel["unresolved"] == ["Mystery"]
    refs = context.references_for(panel, "CD8", {"CD3": "high", "CD45": "moderate"})
    assert [r["marker"] for r in refs] == ["CD3", "CD45"]
    assert context.references_for(panel, "CD8", {"CD3": "low"}) == []


def test_the_vocabulary_outranks_an_agent():
    from plexora.plugins.gating.server.autogate import context

    panel = context.build(["CD3", "CD8", "Mystery"])
    context.apply_entry(panel, "CD8", {"role": "signalling"}, source="ai")
    assert panel["entries"]["CD8"]["role"] == "lineage_other"
    assert "CD8" in panel["ai_suggestions"]
    context.apply_entry(panel, "Mystery", {"role": "state", "partners": [
        {"marker": "CD3", "relation": "subset", "confidence": "moderate"}]}, source="ai")
    assert panel["entries"]["Mystery"]["source"] == "ai"
    with pytest.raises(ValueError):
        context.apply_entry(panel, "Mystery", {"partners": [
            {"marker": "NotInPanel", "relation": "subset"}]}, source="ai")
    context.apply_entry(panel, "CD8", {"binary": False}, source="user")
    assert panel["entries"]["CD8"]["binary"] is False


# -- pixels ------------------------------------------------------------------------------------


def test_display_calibration_is_deterministic_and_drives_auto_windows(tmp_path):
    from plexora.agent import AgentSession
    from plexora.agent.evidence import calibration
    from plexora.agent.render import render_region
    from plexora.agent.render_spec import ChannelSpec, RenderInput

    make_gating_project(tmp_path, grid=12, size=512)
    session = AgentSession()
    first, changed = calibration.calibrate(session, "gsynth", force=True)
    again, _ = calibration.calibrate(session, "gsynth", force=True)
    assert changed and first["channels"] == again["channels"]
    assert first["channels"]["DNA"]["color"] == calibration.NUCLEAR_MUTED_BLUE
    window = first["channels"]["CD8"]["window"]
    rendered = render_region(session, RenderInput(project="gsynth", channels=[
        ChannelSpec(name="CD8", color="#ffd60a")]), store=False)
    channel = rendered["manifest"]["channels"][0]
    assert channel["window"] == window and channel["window_source"].startswith("calib:")
    legacy = render_region(session, RenderInput(project="gsynth", channels=[
        ChannelSpec(name="CD8", color="#ffd60a", window="percentiles")]), store=False)
    assert legacy["manifest"]["channels"][0]["window_source"].startswith("auto:p01")


def test_a_collage_keeps_its_budget_and_its_bytes(tmp_path):
    import io

    from PIL import Image

    from plexora.agent import AgentSession
    from plexora.agent.evidence import collage
    from plexora.plugins.gating.server.autogate import profile, sampler, views

    make_gating_project(tmp_path, grid=16, size=768)
    session = AgentSession()
    ds = session.data("gsynth")
    gate = profile.profile_marker(ds, "CD8")["fit"]["gate_raw"]
    sample = sampler.stratified_cells(ds, "CD8", gate)
    rows = views.t2_rows(sample, gate, None)
    a = collage.render_collage(session, ds, layout="t2", rows=rows, marker="CD8", gate=gate)
    b = collage.render_collage(session, ds, layout="t2", rows=rows, marker="CD8", gate=gate)
    assert a["png"] == b["png"] and a["artifact"]["id"] == b["artifact"]["id"]
    width, height = a["manifest"]["size"]
    assert width <= 1024 and a["manifest"]["estimated_vision_tokens"] < 800
    assert Image.open(io.BytesIO(a["image"])).format == "WEBP"
    placed = [c["cell_id"] for row in a["manifest"]["rows"] for c in row["cells"]]
    assert len(placed) == len(set(placed)) > 0


def test_batched_crops_read_far_fewer_blocks_than_cells(tmp_path):
    from plexora.agent import AgentSession
    from plexora.agent.evidence import crops
    from plexora.server.utils import source_image

    info = make_gating_project(tmp_path, grid=16, size=768)
    session = AgentSession()
    record = session.project("gsynth")
    cells = [{"cell_id": c["id"], "x": c["x"], "y": c["y"]} for c in info["cells"][:40]]
    keys = {c.get("fullname"): source_image.channel_key(c)
            for c in record.image.real_channels if c.get("fullname") in ("DNA", "CD8")}
    got, stats = crops.read_cell_crops(session, record, cells, channels=keys, crop_px=48,
                                       tile_px=48)
    assert len(got) == 40
    assert stats["reads"] < 40
    crop = got[cells[0]["cell_id"]]
    assert crop.mask_stats["area_level_px"] > 0
    assert crop.mask_stats["centroid_offset_px"] < 3


# -- across images -----------------------------------------------------------------------------


def test_alignment_recovers_a_staining_shift():
    from plexora.plugins.gating.server.autogate import reference

    base = {"quantiles_raw": {f"p{p:g}": float(np.expm1(3 + p / 25))
                              for p in reference.ALIGN_P}}
    shifted = {"quantiles_raw": {f"p{p:g}": float(np.expm1(3.4 + p / 25))
                                 for p in reference.ALIGN_P}}
    a = reference.align(shifted, base, "log1p")
    assert a["a"] == pytest.approx(0.4, abs=1e-6) and a["b"] == pytest.approx(1.0, abs=1e-6)
    ref = {"class": "bimodal", "metrics": {"sd_bg": 0.5, "mu_bg": 4.0}, "summary": {}}
    target = {"class": "bimodal", "summary": {}}
    assert reference.classify(reference.align(base, base, "log1p"), target, ref) == "stable"
    assert reference.classify(a, target, ref) == "image_specific_shift"
    assert reference.predict(a, float(np.expm1(5.0)), "log1p") == pytest.approx(
        float(np.expm1(5.4)))


def test_the_mirror_script_shows_what_the_packet_shows():
    from plexora.plugins.gating.server.autogate import mirror_script

    packet = {"kind": "t2_confirm", "units": [{"project": "p", "marker": "CD8"}],
              "evidence": {"candidate": {"low": 350.0, "high": 4000.0}},
              "question": "is it right?",
              "images": [{"artifact_id": "art_00000000000000000000"}]}
    manifest = {"rows": [{"cells": [{"cell_id": 7, "x": 100.0, "y": 200.0, "value": 400.0,
                                     "call": "positive"}]}]}
    calibration = {"nuclear": "DNA", "channels": {
        "DNA": {"window": [10, 200]}, "CD8": {"window": [50, 900]}}}
    script = mirror_script.script_for(packet, manifest, calibration, current_project="q")
    types = [c["type"] for c in script]
    assert types == ["open_project", "set_hd_mode", "set_channels", "set_cell_render_mode",
                     "open_tool", "set_active_marker", "preview_gate", "fit_region",
                     "highlight_cells", "show_evidence"]
    channels = script[2]["arguments"]["channels"]
    assert [c["name"] for c in channels] == ["DNA", "CD8"]
    assert channels[1]["window"] == [50, 900]
    assert script[6]["arguments"]["low"] == 350.0 and not script[6]["arguments"]["persist"]
    assert script[8]["arguments"]["cells"][0] == {"id": 7, "caption": "#7 400+", "x": 100.0,
                                                  "y": 200.0}
