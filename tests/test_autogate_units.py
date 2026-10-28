"""Automatic gating's deterministic parts, one at a time, against known truth."""

import base64
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


def test_a_spike_of_unmeasured_cells_at_zero_is_not_the_background():
    """exemplar-001: ~0.6% of cells at exactly 0 on a log1p'd table. The fit
    must gate the body, not the spike (a gate near 0 calls 99% positive)."""
    from plexora.plugins.gating.server.autogate import profile

    rng = np.random.default_rng(4)
    truth = rng.random(40_000) < 0.3
    logs = np.where(truth, rng.normal(7.4, 0.3, truth.size), rng.normal(6.4, 0.3, truth.size))
    logs[:250] = 0.0
    logs[250:254] = (1.5, 2.7, 3.6, 3.7)          # stragglers between spike and body
    truth[:254] = False
    data = FakeData({"M": logs}, log_transformed=True)
    p = profile.profile_marker(data, "M")
    assert p["counts"]["floor_excluded"] == 250 and "floor_spike" in p["flags"]
    assert 6.6 < p["fit"]["gate_raw"] < 7.2
    assert p["positive_fraction"] == pytest.approx(truth.mean(), abs=0.03)
    assert profile.fit_for(data, "M")["floor_excluded"] == 250
    # A clean column keeps the plugin's own fit.
    clean = FakeData({"M": logs[254:]}, log_transformed=True)
    assert profile.column(clean, "M").floor_n == 0
    assert profile.fit_for(clean, "M")["floor_excluded"] == 0


def test_weak_signal_is_a_caveat_not_a_verdict():
    """CyCIF intensities carry a high background: a real stain under 2x it in
    linear units is common, and must not be sent to a technical check."""
    from plexora.plugins.gating.server.autogate import schemas

    values, _ = populations(60_000, 0.25, bg=(4.0, 0.25), pos=(4.55, 0.25))
    p = _profile(values)
    assert p["version"] == schemas.PROFILE_VERSION == "6"
    assert "weak_signal" in p["flags"]
    assert schemas.hard(p["flags"]) == []
    assert p["t1"]["recommended_tier"] != "QC"


def _spiked(n=40_000, zeros=800, seed=4):
    rng = np.random.default_rng(seed)
    truth = rng.random(n) < 0.3
    logs = np.where(truth, rng.normal(7.4, 0.3, n), rng.normal(6.4, 0.3, n))
    logs[:zeros] = 0.0
    return logs


def test_dynamic_range_ignores_the_floor_spike():
    from plexora.plugins.gating.server.autogate import profile

    p = profile.profile_marker(FakeData({"M": _spiked()}, log_transformed=True), "M")
    # The body spans ~6.4 to ~8.3 in log1p: about one decade, not the four a
    # 1st percentile at 0 would claim.
    assert p["counts"]["floor_excluded"] == 800
    assert 0.8 < p["dynamic_range_decades"] < 1.6


def test_floor_cells_do_not_make_an_illumination_gradient():
    """A dropped tile: every cell in one corner unmeasured (0). They are
    negatives, not a dim background."""
    rng = np.random.default_rng(8)
    n = 60_000
    xs, ys = rng.uniform(0, 10_000, n), rng.uniform(0, 10_000, n)
    logs = _spiked(n=n, zeros=0, seed=8)
    corner = (xs < 3_200) & (ys < 3_200)
    logs[corner] = 0.0
    from plexora.plugins.gating.server.autogate import profile

    data = FakeData({"M": logs}, xs=xs, ys=ys, log_transformed=True)
    p = profile.profile_marker(data, "M")
    assert p["counts"]["floor_excluded"] == int(corner.sum())
    assert "illumination_gradient_cells" not in p["flags"]


def test_estimator_disagreement_reads_as_a_multiple_past_100_percent():
    from plexora.plugins.gating.server.autogate import profile

    assert profile._disagreement(2.95, 3.95) == (
        "the other estimators call 4.0x as many cells positive as the GMM gate")
    assert "only 40% as many" in profile._disagreement(0.6, 0.4)
    assert profile._disagreement(0.2, 1.2) == "estimators disagree about 20% of the positive calls"
    assert profile._disagreement(0.2, None).startswith("estimators disagree about 20%")


def test_answer_schemas_spell_out_nested_fields():
    """An agent cannot follow a `$ref`: every nested model is inlined."""
    from plexora.plugins.gating.server.autogate import answers, schemas

    t2 = answers.schema_for("t2_confirm")["properties"]
    assert t2["rows"]["values"]["enum"] == list(schemas.ROW_VERDICTS)
    assert "none" in t2["pattern"]["enum"] and "direction" not in t2
    entry = answers.schema_for("panel_context")["properties"]["entries"]["items"]
    assert {"marker", "role", "partners"} <= set(entry["properties"])
    assert "enum" in entry["properties"]["role"]
    for kind in answers.KINDS:
        text = json.dumps(answers.schema_for(kind))
        assert '"ref"' not in text and "$ref" not in text and len(text) < 4_000


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


def test_overshooting_steps_are_clipped_to_the_guard_edge():
    """Overlapping populations leave a narrow band: the 1 and 2 sd steps both
    overshoot it, and the edge itself is offered once, as the last candidate."""
    from plexora.plugins.gating.server.autogate import candidates, profile

    values, _ = populations(120_000, 0.3, bg=(6.4, 0.35), pos=(7.2, 0.35))
    ds = FakeData({"M": values})
    gate = profile.profile_marker(ds, "M")["fit"]["gate_raw"]
    c = candidates.candidate_thresholds(ds, "M", current_low=gate, direction="down")
    lows = [x["low"] for x in c["candidates"]]
    assert lows and all(x < gate for x in lows)
    assert c["candidates"][-1]["at_edge"] and c["reaches_edge"]
    assert c["candidates"][-1]["low"] == pytest.approx(c["guard"]["low"], rel=1e-6)
    assert sum(x["at_edge"] for x in c["candidates"]) == 1
    assert any(r["reason"] == "clipped to the guard band edge" for r in c["removed"]) or \
        len(c["candidates"]) >= 2


def test_equal_count_steps_when_the_valley_is_empty():
    """Moving down from inside an empty valley: sd steps of the background land
    where no cells are, so the steps are taken by count -- inside the band and
    the travel bound by construction."""
    from plexora.plugins.gating.server.autogate import candidates

    values, _ = populations(50_000, 0.2, bg=(4.0, 0.5), pos=(7.5, 0.3))
    ds = FakeData({"M": values})
    start = float(np.expm1(6.6))
    c = candidates.candidate_thresholds(ds, "M", current_low=start, direction="down")
    assert c["steps"].startswith("equal-count"), c
    assert len(c["candidates"]) >= 2 and not c["removed"]
    counts = [x["n_positive"] for x in c["candidates"]]
    assert counts == sorted(counts) and len(set(counts)) == len(counts)
    assert all(c["guard"]["low"] <= x["low"] < start for x in c["candidates"])


def test_the_contradiction_guard_only_trusts_a_clean_mixture():
    from plexora.plugins.gating.server.autogate import candidates, profile

    def fit_of(bg, pos, fraction):
        values, _ = populations(120_000, fraction, bg=bg, pos=pos)
        ds = FakeData({"M": values})
        p = profile.profile_marker(ds, "M")
        return profile.fit_for(ds, "M"), p["fit"]

    # Overlapping (D about 1.2): a gate near the positive centre may still be
    # too low -- the means are not a ceiling -- until it reaches the band edge.
    fit, summary = fit_of((6.4, 0.45), (7.1, 0.4), 0.35)
    d = summary["separation"]["ashman_d"]
    assert d < profile.THRESHOLDS["bimodal_d"]
    pools = profile.pools(fit)
    band = candidates.guard_band(fit)
    near_pos = pools["mu_pos"] - 0.3 * pools["sd_pos"]
    assert near_pos < band[1]
    assert not candidates.contradicts(fit, near_pos, "up", d)
    assert candidates.contradicts(fit, band[1], "up", d)
    # Clean (D about 6.6): the positive centre is a ceiling.
    fit, summary = fit_of((4.0, 0.5), (7.0, 0.4), 0.2)
    d = summary["separation"]["ashman_d"]
    assert d >= profile.THRESHOLDS["bimodal_d"]
    pools = profile.pools(fit)
    assert candidates.contradicts(fit, pools["mu_pos"] - 0.3 * pools["sd_pos"], "up", d)
    assert not candidates.contradicts(fit, pools["gate"], "up", d)


def test_the_regression_check_never_fails_the_smallest_candidate_step():
    """A dense boundary: half an sd flips more than a tenth of the positives,
    and the check must not fail the move the generator itself offered."""
    from plexora.plugins.gating.server.autogate import candidates, profile, regression

    values, _ = populations(120_000, 0.41, bg=(6.3, 0.3), pos=(7.0, 0.45))
    ds = FakeData({"M": values})
    gmm = profile.profile_marker(ds, "M")["fit"]["gate_raw"]
    c1 = candidates.candidate_thresholds(ds, "M", current_low=gmm, direction="up")
    first = c1["candidates"][0]["low"]
    checks = {c["name"]: c for c in regression.numeric_checks(ds, "M", first, gmm)["checks"]}
    flip = checks["flip_share"]
    assert flip["value"] > regression.THRESHOLDS["max_flip_share"]   # not vacuous
    assert flip["ok"] and flip["limit"] >= flip["value"]
    assert checks["delta_from_gmm"]["ok"]


def test_the_regression_check_allows_a_step_in_a_wide_population():
    """FOXP3 in the live re-run: half a positive sd was 2.4 background sds, and
    `delta_from_gmm` (limit 1.5) failed the generator's own first step."""
    from plexora.plugins.gating.server.autogate import candidates, profile, regression

    values, _ = populations(120_000, 0.06, bg=(6.2, 0.15), pos=(7.6, 0.8))
    ds = FakeData({"M": values})
    gmm = profile.profile_marker(ds, "M")["fit"]["gate_raw"]
    first = candidates.candidate_thresholds(ds, "M", current_low=gmm,
                                            direction="up")["candidates"][0]
    checks = {c["name"]: c for c in regression.numeric_checks(ds, "M", first["low"],
                                                              gmm)["checks"]}
    assert abs(first["delta_bg_sd"]) > regression.THRESHOLDS["max_delta_bg_sd"]  # not vacuous
    assert checks["delta_from_gmm"]["ok"], checks["delta_from_gmm"]


def test_purity_is_judged_against_the_markers_own_brightest_cells():
    """The partners check is self-calibrated: a gate fails only for the
    impurity it ADDS over the marker's brightest positives, so a low gate
    that admits a partner-negative spill fails, a clean gate passes, and a
    partner whose own gate misses some real positives (CD45 against CD163's
    dim-CD45 macrophages in lsp11385_2) costs the gate nothing."""
    from plexora.plugins.gating.server.autogate import bivariate, profile, regression

    rng = np.random.default_rng(0)
    n = 20_000
    t_cell = rng.random(n) < 0.3
    cd8_pos = t_cell & (rng.random(n) < 0.5)
    cd3 = np.where(t_cell, 7.0, 4.0) + rng.normal(0, 0.25, n)
    cd8 = np.where(cd8_pos, 7.5, 4.0) + rng.normal(0, 0.3, n)
    spill = ~t_cell & (rng.random(n) < 0.08)        # CD3- cells with CD8 spill
    cd8[spill] = 5.6 + rng.normal(0, 0.3, int(spill.sum()))
    ds = FakeData({"CD3": np.expm1(cd3), "CD8": np.expm1(cd8)})
    cd3_gate = float(np.expm1(5.5))
    low, clean = float(np.expm1(5.2)), float(np.expm1(6.6))
    contaminated = bivariate.purity_excess(ds, "CD8", low, "CD3", cd3_gate, relation="subset")
    assert contaminated["excess"] > regression.THRESHOLDS["partner_tolerance"], contaminated
    fine = bivariate.purity_excess(ds, "CD8", clean, "CD3", cd3_gate, relation="subset")
    assert fine["excess"] <= regression.THRESHOLDS["partner_tolerance"], fine
    # A CD3 gate past half the T cells: the CD8+ cells it misses are missing
    # among the brightest CD8+ cells too.
    off = bivariate.purity_excess(ds, "CD8", clean, "CD3", float(np.expm1(7.0)),
                                  relation="subset")
    assert off["at_top"] > 0.3 and off["excess"] <= regression.THRESHOLDS["partner_tolerance"]
    gmm = profile.profile_marker(ds, "CD8")["fit"]["gate_raw"]
    partners = [{"partner": "CD3", "gate": cd3_gate, "relation": "subset"}]
    checks = {c["name"]: c for c in
              regression.numeric_checks(ds, "CD8", low, gmm, partners=partners)["checks"]}
    assert checks["partners"]["ok"] is False
    checks = {c["name"]: c for c in
              regression.numeric_checks(ds, "CD8", clean, gmm, partners=partners)["checks"]}
    assert checks["partners"]["ok"] is True
    # Too few positives to calibrate on: no verdict, not a failure.
    assert bivariate.purity_excess(ds, "CD8", float(np.expm1(9.5)), "CD3", cd3_gate,
                                   relation="subset") is None


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


def test_a_bright_speck_does_not_blow_out_the_display_window():
    """CD57 in the first live run: the overview's p99.5 at 44,896 while the
    brightest cells average a few thousand -- every cell drew black."""
    from plexora.agent.evidence import calibration as cal

    stats = {"p01": 90.0, "p30": 180.0, "p50": 223.0, "p99": 30_000.0, "p995": 44_896.0,
             "p999": 60_000.0, "max": 65_535.0}
    assert cal.channel_window(stats, "marker") == [223.0, 44_896.0]
    assert cal.channel_window(stats, "marker", cap=7_350.0) == [223.0, 7_350.0]
    # Never below the minimum contrast; a cap below the bottom is ignored.
    assert cal.channel_window(stats, "marker", cap=400.0)[1] == pytest.approx(
        cal.MIN_CONTRAST * 223.0)
    assert cal.channel_window(stats, "marker", cap=5.0) == [223.0, 44_896.0]
    assert cal.channel_window(stats, "nuclear", cap=7_350.0) == \
        cal.channel_window(stats, "nuclear")
    raw, _ = populations(20_000, 0.2)
    assert cal.cell_cap(np.log1p(raw), True) == pytest.approx(cal.cell_cap(raw, False),
                                                              rel=1e-3)
    assert cal.cell_cap(raw - 1_000.0, False) is None
    assert cal.cell_cap(raw[:50], False) is None


def test_debris_does_not_black_out_the_marker_window(tmp_path):
    from plexora.agent import AgentSession
    from plexora.agent.evidence import calibration as cal
    from plexora.server.utils import source_image

    from plexora.server.utils import display_curve

    info = make_gating_project(tmp_path, variant="specks")
    session = AgentSession()
    record, _changed = cal.calibrate(session, "gsynth")
    entry = record["channels"]["CD8"]
    # Anchored on the cells at level 0, which the specks cannot own: the
    # titre is the brightest cells' structure, far under a speck.
    from plexora.agent.evidence import cell_window

    assert entry["window_source"].startswith(cell_window.SOURCE)
    assert entry["anchors"]["titre"] < entry["stats"]["max"] / 4
    # A CD8+ cell draws well above black, on the curve it is drawn with.
    positive = float(np.median([c["CD8"] for c in info["cells"] if c["kind"] == "cd8_t"]))
    assert display_curve.level(positive, entry["window"], entry.get("curve")) > 0.25
    # Uncapped, the same pixels give the blown-out window, and say so.
    channels = {"CD8": source_image.channel_key(
        next(c for c in session.project("gsynth").image.real_channels
             if (c.get("fullname") or c.get("name")) == "CD8"))}
    with source_image.SHELF.reader(session.image_data("gsynth")) as source:
        uncapped = cal.compute(source, channels)["channels"]["CD8"]
    assert "wide_window" in uncapped["flags"]
    assert (positive - uncapped["window"][0]) / (uncapped["window"][1]
                                                 - uncapped["window"][0]) < 0.1


def test_a_stale_calibration_is_recomputed(tmp_path):
    from plexora.agent import AgentSession
    from plexora.agent.evidence import calibration as cal

    make_gating_project(tmp_path)
    session = AgentSession()
    record, _ = cal.calibrate(session, "gsynth")
    cal.save("gsynth", {**record, "version": "1"})
    assert cal.load("gsynth") is None
    again, changed = cal.calibrate(session, "gsynth")
    assert changed and again["version"] == cal.VERSION


def test_the_density_grid_spans_the_body_not_the_zero_spike():
    from plexora.agent.evidence import density_plot
    from plexora.plugins.gating.server.autogate import bivariate

    a = _spiked(zeros=300)
    b, _ = populations(a.size, 0.3, seed=9)
    ds = FakeData({"A": a, "B": np.log1p(b)}, log_transformed=True)
    result = bivariate.bivariate_numbers(ds, "A", 6.9, "B", float(np.log1p(400)),
                                         relation="independent")
    density = result["density"]
    assert density["a_range"][0] > 3 and density["floor_excluded"]["a"] == 300
    assert density["spaces"] == {"a": "values", "b": "values"}
    grid = np.frombuffer(base64.b64decode(density["log_density_u8"]),
                         dtype=np.uint8).reshape(density["bins"], density["bins"])
    assert (grid.sum(axis=1) > 0).mean() >= 0.4
    image = np.asarray(density_plot.draw_density(result))
    left, right = 60, image.shape[1] - 12
    lo, hi = density["a_range"]
    x = int(round(left + (6.9 - lo) / (hi - lo) * (right - left)))
    column = image[100:300, x - 1:x + 2]
    assert (np.abs(column.astype(int) - density_plot.GATE).sum(axis=-1) < 30).any()


def test_the_gate_relative_panel_reads_a_log1p_table(tmp_path):
    """exemplar-001: a log1p'd table (gate 6.79) over raw pixels in the
    hundreds. The panel must put the gate at mid-grey in pixel units -- not
    draw every pixel white against a window of single digits."""
    import io

    from PIL import Image

    from plexora.agent import AgentSession
    from plexora.agent.evidence import collage

    info = make_gating_project(tmp_path, log_transformed=True)
    session = AgentSession()
    ds = session.data("gsynth")
    assert ds.table.log_transformed
    positive = next(c for c in info["cells"] if c["kind"] == "cd8_t")
    negative = next(c for c in info["cells"] if c["kind"] == "cd4_t")
    rows = [{"label": "above", "cells": [dict(positive, cell_id=positive["id"])]},
            {"label": "below", "cells": [dict(negative, cell_id=negative["id"])]}]
    rendered = collage.render_collage(session, ds, layout="report", rows=rows, marker="CD8",
                                      gate=6.0, fmt="png", store=False, to_log=False)
    manifest = rendered["manifest"]
    tile = manifest["tile_px"]
    image = np.asarray(Image.open(io.BytesIO(rendered["png"])).convert("L"),
                       dtype=np.float64)
    header, row_header, caption, gap = 14, 12, 11, 2
    index = manifest["panels"].index("gate_relative")
    column, row_in_cell = index % 2, index // 2

    def panel(row):
        top = header + row * (row_header + 2 * tile + caption + gap) + row_header
        y0, x0 = top + row_in_cell * tile, column * tile
        return image[y0:y0 + tile, x0:x0 + tile]

    above, below = panel(0), panel(1)
    assert 5 < above.mean() < 250, above.mean()
    centre = slice(tile // 2 - 2, tile // 2 + 3)
    assert above[centre, centre].mean() > 128
    assert below[centre, centre].mean() < 128


def test_the_morphology_panel_shows_a_cells_own_stain_without_a_mask(tmp_path, monkeypatch):
    """The first panel of a look: the marker windowed on the judged cell's own
    disc. A CD8 T cell draws its stain bright around the ticked centre; a CD4
    T cell (no CD8) stays dark -- the floor keeps its noise from being
    stretched to white; and nothing in the panel depends on the mask."""
    import io

    from PIL import Image

    from plexora.agent import AgentSession, render
    from plexora.agent.evidence import collage

    info = make_gating_project(tmp_path, log_transformed=True)
    session = AgentSession()
    ds = session.data("gsynth")
    positive = next(c for c in info["cells"] if c["kind"] == "cd8_t")
    negative = next(c for c in info["cells"] if c["kind"] == "cd4_t")
    rows = [{"label": "positive", "cells": [dict(positive, cell_id=positive["id"])]},
            {"label": "negative", "cells": [dict(negative, cell_id=negative["id"])]}]

    def draw():
        rendered = collage.render_collage(session, ds, layout="t2", rows=rows, marker="CD8",
                                          gate=6.0, fmt="png", store=False)
        image = np.asarray(Image.open(io.BytesIO(rendered["png"])).convert("RGB"),
                           dtype=np.float64)
        return rendered["manifest"], image

    manifest, image = draw()
    assert manifest["panels"] == ["morphology", "merge"]
    tile = manifest["tile_px"]
    header, row_header, caption, gap = 14, 12, 11, 2

    def morphology(row, picture):
        top = header + row * (row_header + tile + caption + gap) + row_header
        return picture[top:top + tile, 0:tile]

    def yellow(panel):
        centre = panel[tile // 2 - 8:tile // 2 + 8, tile // 2 - 8:tile // 2 + 8]
        return float((np.minimum(centre[..., 0], centre[..., 1]) - centre[..., 2]).mean())

    assert yellow(morphology(0, image)) > 60
    assert yellow(morphology(1, image)) < 20
    cells = [c for row in manifest["rows"] for c in row["cells"]]
    assert not cells[0]["morphology"]["floored"] and cells[1]["morphology"]["floored"]
    # The same cells without a mask: the morphology panel is the same picture.
    monkeypatch.setattr(render, "_mask_for", lambda record: (None, "none", None, None))
    _manifest, bare = draw()
    for row in (0, 1):
        assert np.array_equal(morphology(row, image), morphology(row, bare))


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
    # No fit_region: the preview's `show` flies the tab to the field itself.
    assert types == ["open_project", "set_hd_mode", "set_channels", "set_cell_render_mode",
                     "open_tool", "set_active_marker", "preview_gate", "highlight_cells",
                     "show_evidence"]
    channels = script[2]["arguments"]["channels"]
    assert [c["name"] for c in channels] == ["DNA", "CD8"]
    assert channels[1]["window"] == [50, 900]
    assert script[6]["arguments"]["low"] == 350.0 and not script[6]["arguments"]["persist"]
    assert script[7]["arguments"]["cells"][0] == {"id": 7, "caption": "#7 400+", "x": 100.0,
                                                  "y": 200.0}
    show = script[6]["arguments"]["show"]
    assert show["field"] == {"x": -20.0, "y": 80.0, "width": 240.0, "height": 240.0}
    assert show["cells"] == [{"x": 100.0, "y": 200.0}] and show["stops"] == []
    # A tab already showing HD, outlines and the gating tool on this image is
    # sent only what this packet shows.
    shown = {"project": "p", "hd_mode": True, "cell_mode": "outlines",
             "tools_open": ["gating", "roi"]}
    lean = [c["type"] for c in mirror_script.script_for(packet, manifest, calibration,
                                                        viewer_state=shown)]
    assert lean == ["set_channels", "set_active_marker", "preview_gate", "highlight_cells",
                    "show_evidence"]
    # Switching image: everything, since the new image's state is unknown.
    moved = mirror_script.script_for(packet, manifest, calibration,
                                     viewer_state={**shown, "project": "q"})
    assert [c["type"] for c in moved] == types


def test_a_blind_panel_moves_the_tabs_slider_to_the_engines_leader():
    from plexora.plugins.gating.server.autogate import mirror_script

    # A v2 look names no gate (the reader is blind); the tab is shown the
    # gate the engine holds: where it started, then the leader after a look.
    packet = {"kind": "cell_panel", "units": [{"project": "p", "marker": "CD8"}],
              "evidence": {"marker": "CD8", "project": "p", "cells": 24}}
    assert "preview_gate" not in [c["type"] for c in
                                  mirror_script.script_for(packet, None, None,
                                                           current_project="p")]
    unit = {"high": 4000.0, "hypotheses": {"start": 300.0, "leader": None}}

    def preview(unit):
        script = mirror_script.script_for(packet, None, None, current_project="p",
                                          gate=mirror_script._engine_gate(unit))
        return [c["arguments"] for c in script if c["type"] == "preview_gate"]

    assert [{k: a[k] for k in ("marker", "low", "high", "persist")} for a in preview(unit)] \
        == [{"marker": "CD8", "low": 300.0, "high": 4000.0, "persist": False}]
    unit["hypotheses"]["leader"] = 412.5
    assert [a["low"] for a in preview(unit)] == [412.5]
    assert mirror_script._engine_gate({"state": "t2"}) is None
    # A packet's own candidate still wins.
    v1 = {**packet, "kind": "t2_confirm", "evidence": {"candidate": {"low": 350.0}}}
    script = mirror_script.script_for(v1, None, None, current_project="p",
                                      gate=mirror_script._engine_gate(unit))
    assert [c["arguments"]["low"] for c in script if c["type"] == "preview_gate"] == [350.0]


def test_a_looks_show_carries_the_gates_to_try_and_a_seed_of_its_own():
    from plexora.plugins.gating.server.autogate import mirror_script

    # The candidate gates nearest the leader, two a side; the look's number;
    # a seed per marker, the same every time.
    unit = {"high": None, "hypotheses": {
        "leader": 5.0, "observations": [{}, {}],
        "points": [{"low": v} for v in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0)]}}
    gate = mirror_script._engine_gate(unit)
    assert gate["stops"] == [3.0, 4.0, 6.0, 7.0]
    manifest = {"rows": [{"cells": [{"cell_id": i, "x": 10.0 * i, "y": 5.0} for i in range(1, 9)]
                          + [{"cell_id": 99, "x": 9000.0, "y": 9000.0}]}]}

    def show(marker):
        packet = {"kind": "cell_panel", "units": [{"project": "p", "marker": marker}],
                  "evidence": {"marker": marker, "project": "p", "look": 3}}
        script = mirror_script.script_for(packet, manifest, None, current_project="p",
                                          gate=gate)
        assert "fit_region" not in [c["type"] for c in script]
        return next(c for c in script if c["type"] == "preview_gate")["arguments"]["show"]

    cd8 = show("CD8")
    assert cd8["look"] == 3 and cd8["stops"] == [3.0, 4.0, 6.0, 7.0]
    # Only cells inside the field it flies to, at most SHOW_CELLS of them.
    assert len(cd8["cells"]) == mirror_script.SHOW_CELLS
    assert {"x": 9000.0, "y": 9000.0} not in cd8["cells"]
    assert show("CD8")["seed"] == cd8["seed"] != show("CD4")["seed"]
    # A v1 candidates look: its candidates are the stops, in one preview.
    t4 = {"kind": "t4_candidates", "units": [{"project": "p", "marker": "CD8"}],
          "evidence": {"candidate": {"low": 4.0},
                       "candidates": [{"low": 2.0}, {"low": 4.0}, {"low": 9.0}]}}
    previews = [c for c in mirror_script.script_for(t4, manifest, None, current_project="p")
                if c["type"] == "preview_gate"]
    assert len(previews) == 1 and previews[0]["arguments"]["show"]["stops"] == [2.0, 9.0]


# -- the report --------------------------------------------------------------------


def _report(reason):
    return {"session": {"session_id": "gs_test", "created_at": "2026-09-26T00:00:00Z",
                        "finished_at": None, "state": "done", "scope": "project",
                        "dataset": None, "images": ["exemplar-001"], "reference_image": None,
                        "principal": "agent"},
            "options": {"mode": "apply"}, "used": {}, "vision_tokens": 0,
            "counts": {"accepted_low_confidence": 1}, "questions": [], "receipts": ["op.001"],
            "units": [{"project": "exemplar-001", "marker": "CD16",
                       "state": "accepted_low_confidence", "confidence": "low",
                       "final": 6.47, "gmm": 6.47, "tier": "T2", "class": "weakly_bimodal",
                       "flags": ["weak_signal", "unstable_fit"], "reason": reason,
                       "trail": [{"event": "issued", "kind": "t2_confirm"}]}]}


def test_the_report_keeps_every_word_on_the_page(tmp_path):
    """The first live run's PDF cut outcomes ("accepted (low confidence") and
    ran the reasons off the page; the HTML cut an escaped entity in half."""
    from types import SimpleNamespace

    from plexora.plugins.gating.server.autogate import report

    reason = ("budget spent & the look said <too low>; " + "word " * 60 + "THE-END").strip()
    call = SimpleNamespace(session=None)
    page = report.to_html(call, _report(reason))
    assert "accepted (low confidence)" in page
    assert "&amp; the look said &lt;too low&gt;" in page
    assert "&am " not in page and "&a…" not in page
    path = tmp_path / "r.pdf"
    report.to_pdf(call, _report(reason), path, compress=False)
    data = path.read_bytes()
    assert data[:4] == b"%PDF"
    assert b"low confidence" in data and b"THE-END" in data


# -- one source for every value --------------------------------------------------


def test_every_vocabulary_has_one_source():
    """What the models, packets and tools offer is derived from the vocabulary
    modules, never restated."""
    from typing import get_args

    from plexora.agent.evidence import collage
    from plexora.agent.sessions import budget
    from plexora.ai import vocabulary
    from plexora.plugins.gating import capabilities_autogate, capabilities_session
    from plexora.plugins.gating.server.autogate import (answers, packets, report, schemas,
                                                        transitions)

    assert get_args(answers.Artifact) == schemas.ARTIFACTS
    assert set(answers.KINDS) == set(packets.BUILDERS) == set(transitions.APPLY)
    assert set(report.STATE_LABELS) >= set(schemas.TERMINAL_STATES)
    for model in (answers.PanelEntry, capabilities_autogate.MarkerContext):
        role = model.model_fields["role"].annotation
        assert vocabulary.ROLES in {get_args(role), *(get_args(a) for a in get_args(role))}
    assert collage.LAYOUT_NAMES == tuple(n for n, s in collage.LAYOUTS.items()
                                         if s.get("offered")) + (collage.OVERVIEW,)
    assert not hasattr(capabilities_autogate, "LAYOUT_CHOICES")
    assert get_args(capabilities_autogate.CollageInput.model_fields["layout"].annotation) \
        == collage.LAYOUT_NAMES
    defaults = capabilities_session.option_defaults()
    assert defaults["budget"] == budget.UNIT_DEFAULT
    assert defaults["max_tier"] == schemas.ENGINE["max_tier_default"]
    assert capabilities_session.SessionOptions.model_fields["max_tier"].metadata[-1].le \
        == len(schemas.TIERS)
    # No numbers-only tier: every session looks at the pixels (T2 and up).
    assert any(getattr(m, "ge", None) == 2
               for m in capabilities_session.SessionOptions.model_fields["max_tier"].metadata)
    assert packets._allowed(answers.T2Answer, "pattern") == list(schemas.PATTERNS)
    # `counts` answers a classifier check read by counts (`check_reading`).
    assert set(answers.T4Answer.model_fields) - set(answers.T2Answer.model_fields) \
        == {"intervals", "counts"}


def test_the_row_builders_draw_as_many_cells_as_their_layouts():
    from plexora.agent.evidence import collage
    from plexora.plugins.gating.server.autogate import views

    many = [{"cell_id": i, "x": 0.0, "y": 0.0, "value": float(i)} for i in range(40)]
    sample = {"strata": {n: many for n in ("just_below", "borderline", "just_above")},
              "counts": {}}
    rows = views.t2_rows(sample, 20.0, None)
    assert max(len(r["cells"]) for r in rows) == collage.LAYOUTS["t2"]["per_row"]
    flips = views.flip_rows({"intervals": [{"from": 1, "to": 2, "n_flip": 40,
                                            "cells": many}]})
    assert len(flips[0]["cells"]) == collage.LAYOUTS["flips"]["per_row"]


def test_the_vocabulary_refuses_an_entry_outside_its_schema():
    from plexora.ai import vocabulary

    vocabulary.check({"canonical": "X", "role": "lineage_reliable", "partners": [
        {"marker": "CD3", "relation": "subset", "confidence": "high"}]})
    with pytest.raises(ValueError, match="'X'.*role"):
        vocabulary.check({"canonical": "X", "role": "lineage"})
    with pytest.raises(ValueError, match="relation"):
        vocabulary.check({"canonical": "X", "partners": [{"marker": "CD3",
                                                          "relation": "friend"}]})
    vocabulary._shipped.cache_clear()
    vocabulary.clear_cache()
    assert vocabulary.load()["entries"]      # the shipped file passes


def test_the_nuclear_stain_is_found_by_the_vocabulary():
    from plexora.agent.presets import nuclear_channel

    assert nuclear_channel(["CD3", "SYTO13", "CD8"]) == "SYTO13"
    assert nuclear_channel(["CD3", "DAPI_1"]) == "DAPI_1"
    assert nuclear_channel(["CD3", "CD8"]) is None


def test_options_are_read_without_fallbacks():
    """The engine completes a session's options once (`option_defaults`); a
    `.get(..., default)` downstream would be a second copy of a default."""
    import re

    root = Path(__file__).resolve().parent.parent / "plexora" / "plugins" / "gating"
    pattern = re.compile(r"options\.get\(|or \"webp\"|\.get\(\"max_tier\"|mirror_delay_ms\", 600")
    offenders = []
    for path in [*(root / "server" / "autogate").glob("*.py"), root / "capabilities_session.py"]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == []


# -- the viewer, the evidence and the failure cases (2026-09-26, second round) ------


def test_session_vocabularies_are_partitioned_and_derived():
    from typing import get_args

    from plexora.agent.evidence import collage
    from plexora.ai import vocabulary
    from plexora.plugins.gating.server.autogate import (answers, bivariate, engine, packets,
                                                        provenance, schemas)

    kinds = schemas.SETUP_KINDS + schemas.PLAN_KINDS + schemas.LOOK_KINDS + schemas.CHECK_KINDS
    assert len(kinds) == len(set(kinds)) and set(kinds) == set(packets.BUILDERS)
    assert set(schemas.USER_SETUP_KINDS) <= set(schemas.SETUP_KINDS) <= set(answers.KINDS)
    assert set(schemas.PLAN_KINDS) <= set(answers.KINDS)
    # Review states are written too (tagged `needs_review`): every gated
    # marker ends with a value.
    assert schemas.WRITTEN_STATES == (schemas.ACCEPTED_STATES + schemas.EMPTY_GATE_STATES
                                      + schemas.REVIEW_WRITE_STATES)
    assert set(schemas.REVIEW_WRITE_STATES) <= set(schemas.REVIEW_STATES)
    assert set(schemas.EMPTY_GATE_STATES) <= set(schemas.TERMINAL_STATES)
    assert set(engine.EMPTY_GATE_METHOD) == set(schemas.EMPTY_GATE_STATES)
    assert set(engine.EMPTY_GATE_METHOD.values()) <= set(provenance.METHODS)
    assert "needs_setup" in schemas.SESSION_STATES and "needs_setup" in schemas.NEXT_STATES
    assert set(schemas.COMPARTMENT_POLICY) == set(vocabulary.COMPARTMENTS)
    assert set(schemas.IMAGE_LED_RELAXED) <= set(schemas.SOFT_FLAGS)
    assert set(packets.BIVARIATE_READING) == set(bivariate.RELATIONS)
    assert get_args(answers.Request.model_fields["kind"].annotation) == (
        "reference_channel", "bivariate")
    assert "no_positive_population" in packets._allowed(answers.QCAnswer, "verdict")
    for policy in schemas.COMPARTMENT_POLICY.values():
        assert set(policy["patterns"]) <= set(schemas.PATTERNS)
    assert collage.LAYOUTS["t2"]["per_row"] == collage.LAYOUTS["t3"]["per_row"]


def test_the_unit_pixel_budget_covers_every_look():
    from plexora.agent.evidence import collage
    from plexora.agent.sessions import budget
    from plexora.plugins.gating.server.autogate import sheet

    look = collage.layout_pixels("t2") + sheet.max_pixels()
    assert budget.UNIT_DEFAULT["pixels"] >= budget.UNIT_DEFAULT["packets"] * look
    assert budget.UNIT_DEFAULT["images"] >= 2 * budget.UNIT_DEFAULT["packets"]
    assert collage.tile_side("t2") == collage.TILE_PX


def test_expression_kinds_are_classified():
    from plexora.agent import expression

    rng = np.random.default_rng(0)
    raw = rng.gamma(2.0, 400.0, 5000)
    assert expression.classify(np.round(raw))["kind"] == "raw_counts"
    assert expression.classify(raw + 0.5)["kind"] == "raw_intensity"
    assert expression.classify(np.log1p(raw))["kind"] == "log_like"
    assert expression.classify(rng.normal(0, 1, 5000))["kind"] == "scaled"
    assert expression.classify([])["kind"] == "unknown"


def test_expression_recommendation_rules():
    from plexora.agent import expression

    def opt(value, kind):
        return {"value": value, "kind": kind}

    current = {"features_layer": "X", "features_log": False}
    rec = expression.recommend([opt("X", "raw_counts"), opt("layer:log1p", "log_like")],
                               current, False)
    assert rec["confidence"] == "certain"
    assert rec["choice"] == {"features_layer": "layer:log1p", "features_log": False}
    two = [opt("X", "raw_counts"), opt("layer:log1p", "log_like"),
           opt("layer:lognorm", "log_like")]
    assert expression.recommend(two, current, False)["confidence"] == "ask"
    named = [opt("X", "raw_counts"), opt("layer:log1p", "log_like"),
             opt("layer:smoothed", "log_like")]
    assert expression.recommend(named, current, False)["choice"]["features_layer"] \
        == "layer:log1p"
    assert expression.recommend([opt("X", "raw_intensity")], current, False)["choice"] \
        == {"features_layer": "X", "features_log": True}
    assert expression.recommend([opt("X", "log_like")], current, False)["confidence"] == "ask"
    assert expression.recommend([opt("X", "scaled")], current, False)["confidence"] == "ask"
    assert expression.recommend([opt("X", "log_like")], current, True)["choice"] is None
    assert expression.fallback_choice(two) == {"features_layer": "layer:log1p",
                                               "features_log": False}
    assert expression.fallback_choice([opt("X", "raw_counts")])["features_log"] is True


def _panel(rng, scale=1.0):
    """Six markers of background and positive cells, as measured."""
    return {f"m{i}": np.concatenate([rng.lognormal(5, 0.5, 1500),
                                     rng.lognormal(5 + 1 + i % 3, 0.5, 500)]) * scale
            for i in range(6)}


def test_the_shape_test_tells_logged_values_from_raw_ones_at_any_scale():
    """A range alone cannot: log1p values and a raw export divided by 65535
    both stay under LOG_CEILING. Skewness does not depend on scale."""
    from plexora.agent import expression

    rng = np.random.default_rng(0)
    raw = _panel(rng)
    assert expression.shape(raw)["says"] == "raw"
    assert expression.shape({k: v / 65535 for k, v in raw.items()})["says"] == "raw"
    assert expression.shape({k: np.log1p(v) for k, v in raw.items()})["says"] == "logged"
    assert expression.shape({k: np.log2(v + 1) for k, v in raw.items()})["says"] == "logged"
    # Too few markers with values to judge: no verdict.
    assert expression.shape({"m0": raw["m0"], "m1": np.zeros(10)})["says"] is None
    assert len(expression.shape(raw)["detail"]) == 6


def test_an_unconfirmed_x_in_the_log_range_is_settled_by_its_shape():
    """The default matrix X, nobody confirmed it: logged values are read as
    they are, small raw ones get log1p, and only an unclear shape is asked --
    marked `ambiguous` (a model may read it) unless nothing could be read."""
    from plexora.agent import expression

    rng = np.random.default_rng(1)
    raw = _panel(rng)

    def x(samples):
        stats = expression.classify_markers(samples)
        return {"value": "X", "kind": stats.pop("kind"), "stats": stats}

    current = {"features_layer": "X", "features_log": False}
    logged = expression.recommend([x({k: np.log1p(v) for k, v in raw.items()})], current, False)
    assert logged["confidence"] == "certain"
    assert logged["choice"] == {"features_layer": "X", "features_log": False}
    small = expression.recommend([x({k: v / 65535 for k, v in raw.items()})], current, False)
    assert small["choice"] == {"features_layer": "X", "features_log": True}
    unclear = {"value": "X", "kind": "log_like", "stats": {"shape": {"says": "unclear"}}}
    asked = expression.recommend([unclear], current, False)
    assert asked["confidence"] == "ask" and asked["ambiguous"]
    unread = expression.recommend([{"value": "X", "kind": "unknown", "stats": {"n": 0}}],
                                  current, False)
    assert unread["confidence"] == "ask" and not unread.get("ambiguous")
    assert "could not be read" in unread["why"]
    # A log-range layer whose shape says raw is a low-scale export, not a log.
    low = {"value": "layer:scaled", "kind": "log_like", "stats": {"shape": {"says": "raw"}}}
    picked = expression.recommend([{"value": "X", "kind": "raw_intensity"}, low], current, False)
    assert picked["choice"] == {"features_layer": "X", "features_log": True}


def test_a_model_reading_of_the_matrix_is_only_a_choice_when_it_says_one():
    from plexora.ai.harness import expression as reader

    assert reader.settle({"verdict": "raw", "why": "long tails"}).choice == {
        "features_layer": "X", "features_log": True, "basis": "inferred",
        "why": "read by a model: long tails"}
    assert reader.settle({"verdict": "logged", "why": "narrow"}).choice["features_log"] is False
    unsure = reader.settle({"verdict": "unsure", "why": "between"})
    assert unsure.choice is None and unsure.verdict == "unsure"
    assert reader.settle({"verdict": "maybe"}).problem
    inspected = {"current": {"confirmed": False}, "source_kind": "anndata",
                 "recommendation": {"confidence": "ask", "ambiguous": True},
                 "options": [{"value": "X", "kind": "log_like",
                              "stats": {"n": 12000, "p99_9": 14.2, "shape": {
                                  "says": "unclear", "skew_linear": 1.4, "skew_log": -0.2,
                                  "detail": [{"marker": "CD3", "p50": 6.1}]}}}]}
    assert reader.worth_asking(inspected)
    text = reader.prompt(inspected)
    assert "CD3" in text and "14.2" in text and "anndata" in text
    assert not reader.worth_asking({**inspected, "recommendation": {"confidence": "ask"}})
    assert not reader.worth_asking({**inspected, "current": {"confirmed": True}})


def test_anchor_cells_are_seeded_and_banded():
    from plexora.agent.evidence import cell_window

    rng = np.random.default_rng(1)
    values = rng.gamma(2.0, 100.0, 4000)
    ids = np.arange(4000)
    xs, ys = rng.uniform(0, 1000, 4000), rng.uniform(0, 1000, 4000)
    a, _ = cell_window.select_anchor_cells(values, ids, xs, ys, seed=3)
    b, _ = cell_window.select_anchor_cells(values, ids, xs, ys, seed=3)
    assert a == b
    spec = cell_window.CELL_WINDOW
    lo_neg, hi_neg = np.percentile(values, spec["negative_pct"])
    assert all(lo_neg - 1e-9 <= c["value"] <= hi_neg + 1e-9 for c in a["negatives"])
    assert set(a) == {"negatives"} and len(a["negatives"]) == spec["n_each"]
    none, _ = cell_window.select_anchor_cells(values[:50], ids[:50], xs[:50], ys[:50])
    assert none is None
    # QC's left-out rows arrive as NaN and are never anchor cells.
    left_out = values.copy()
    left_out[::2] = np.nan
    c, _ = cell_window.select_anchor_cells(left_out, ids, xs, ys, seed=3)
    assert all(int(cell["cell_id"]) % 2 == 1 for cell in c["negatives"])


def test_the_background_is_measured_on_a_disc():
    """Black under the negatives' median pixel, the noise p99 above it --
    read in a disc around the centroid, so a mask (or none) does not change
    it, and a bright neighbour inside the crop but outside the disc does not
    either."""
    from types import SimpleNamespace

    from plexora.agent.evidence import cell_window, crops

    rng = np.random.default_rng(0)
    made, negatives = {}, []
    for cell_id in range(24):
        plane = rng.normal(100.0, 30.0, (32, 32)).astype(np.float32)
        plane[:4, :4] = 5000.0                 # a neighbour in the corner
        crop = crops.Crop(cell_id, 16.0, 16.0, (0, 0, 32, 32), 0)
        crop.planes["M"] = plane
        made[cell_id] = crop
        negatives.append({"cell_id": cell_id, "x": 16.0, "y": 16.0, "value": 1.0})
    measured = cell_window.measure(made, "M", {"negatives": negatives}, radius_px=9.0)
    assert measured["background"] == pytest.approx(100.0, abs=3.0)
    spread = measured["noise_p99"] - measured["background"]
    assert spread == pytest.approx(70.0, rel=0.15)        # 2.33 sds of the noise
    assert measured["black"] == pytest.approx(100.0 - 1.645 * 30.0, abs=8.0)   # the p5
    assert measured["titre"] is None
    for crop in made.values():
        crop.labels = np.zeros((32, 32), dtype=np.int32)   # a mask says nothing here
    assert cell_window.measure(made, "M", {"negatives": negatives}, radius_px=9.0) == measured
    assert crops.cell_radius_px(SimpleNamespace(image=SimpleNamespace(pixel_size=None)),
                                pixel={"value": 0.5})[0] == pytest.approx(9.0)


def test_phase_and_summary_follow_the_record():
    from plexora.plugins.gating.server.autogate import engine

    units = {"a": {"state": "accepted"}, "b": {"state": "awaiting_t2"}}
    record = {"state": "deciding", "units": units, "outstanding_kind": "t2_confirm"}
    assert engine.phase_for(record) == "thinking"
    assert engine.phase_for(record, mirroring=True) == "inspecting"
    assert engine.phase_for({**record, "outstanding_kind": "qc_confirm"}) == "validating"
    assert engine.phase_for({**record, "outstanding_kind": "expression_setup"}) == "planning"
    assert engine.phase_for({**record, "outstanding_kind": None}) == "analyzing"
    assert engine.phase_for({**record, "state": "done"}) == "summarizing"
    done = {"units": {"a": {"state": "accepted", "receipts": ["op.001"]},
                      "b": {"state": "accepted_low_confidence", "receipts": ["op.002"]},
                      "c": {"state": "technically_failed", "receipts": ["op.003"]},
                      "d": {"state": "no_positive_population"},
                      "e": {"state": "manual_review_recommended", "proposed": 1.0,
                            "receipts": ["op.004"], "needs_review": True},
                      "f": {"state": "skipped_manual"}}}
    summary = engine.summary_of(done)
    assert summary["units_total"] == summary["units_done"] == 6
    assert (summary["accepted"], summary["accepted_low_confidence"]) == (2, 1)
    assert (summary["empty"], summary["failed"], summary["review"]) == (2, 1, 1)
    # The review unit is written (for a person to check), not left proposed.
    assert (summary["skipped"], summary["written"], summary["proposed"]) == (1, 4, 0)


def test_the_teardown_script_is_one_restore():
    from plexora.plugins.gating.server.autogate import mirror_script

    assert mirror_script.teardown_script("stopped") == [
        {"type": "restore_viewer", "arguments": {"reason": "stopped"}}]
    assert "restore_viewer" in mirror_script.COMMAND_TIMEOUT_S


def test_image_led_markers_are_not_capped_by_shape_flags():
    from plexora.plugins.gating.server.autogate import engine

    def unit(compartment, path="t4", regression_ok=True):
        return {"path": path, "ai_confidence": 0.9, "delta_bg_sd": 0.1,
                "metrics": {"d": 2.5}, "flags": ["unstable_fit"],
                "context": {"compartment": compartment, "source": "vocabulary"},
                "regression": {"ok": regression_ok, "failed": [] if regression_ok else ["x"]}}

    membrane = unit("membrane")
    assert engine.confidence_for(membrane) == "high"
    assert membrane["confidence_notes"]
    assert engine.confidence_for(unit("nuclear")) == "moderate"
    # One first look is not enough to lift the cap.
    assert engine.confidence_for(unit("membrane", path="t2")) == "moderate"
    assert engine.confidence_for(unit("membrane", regression_ok=False)) == "low"


def test_the_panel_speaks_the_servers_vocabulary():
    import re
    from pathlib import Path

    from plexora.plugins.gating.server.autogate import schemas

    root = Path(__file__).resolve().parents[1] / "plexora" / "client"
    panel = (root / "src" / "js" / "views" / "agentPanel.js").read_text()
    orbs = (root / "external" / "thinking-orbs-0.3.2" / "orbs.js").read_text()

    def block(name, text):
        return re.search(rf"const {name} = \{{\n(.*?)\n\s*\}};", text, re.S).group(1)

    def keys(name, text):
        body = block(name, text)
        depth = min(len(m) for m in re.findall(r"^( +)[a-z_]+\s*[:(]", body, re.M))
        return re.findall(rf"^ {{{depth}}}([a-z_]+)\s*[:(]", body, re.M)

    assert tuple(keys("PHASES", panel)) == schemas.PHASES
    assert set(keys("OUTCOMES", panel)) == set(schemas.TERMINAL_STATES)
    # Every session event, plus the harness's own on the same channel.
    from plexora.ai.harness.capabilities import AI_EVENTS

    assert set(keys("HANDLERS", panel)) == set(schemas.SESSION_EVENTS) | set(AI_EVENTS)
    orb_states = set(re.findall(r'orb:\s*"([a-z]+)"', block("PHASES", panel)))
    table = re.search(r"STATE_TO_MODE = \{(.*?)\}", orbs, re.S).group(1)
    assert orb_states <= set(re.findall(r"([a-z]+)\s*:", table))


def test_the_context_sheet_has_three_scales(tmp_path):
    from plexora.agent import AgentSession
    from plexora.plugins.gating.server.autogate import sheet

    make_gating_project(tmp_path)
    session = AgentSession()
    ds = session.data("gsynth")
    values = np.asarray(ds.table.columns(["CD8"])["CD8"], dtype=np.float64)
    low = float(np.percentile(values, 70))
    first = sheet.render_context_sheet(session, ds, marker="CD8", channel="CD8", low=low,
                                       fmt="png", store=False)
    again = sheet.render_context_sheet(session, ds, marker="CD8", channel="CD8", low=low,
                                       fmt="png", store=False)
    assert first["png"] == again["png"]
    manifest = first["manifest"]
    assert tuple(manifest["size"]) == sheet.sheet_size()
    assert manifest["estimated_vision_tokens"] <= sheet.max_pixels() // 750 + 1
    assert [f["class"] for f in manifest["fields"]] == [
        c for c in sheet.CLASSES if c not in manifest["classes_without_field"]]
    assert manifest["plot"]["kind"] == "histogram"
    partner = {"marker": "CD3", "gate": float(np.percentile(np.asarray(
        ds.table.columns(["CD3"])["CD3"]), 60)), "relation": "subset"}
    paired = sheet.render_context_sheet(session, ds, marker="CD8", channel="CD8", low=low,
                                        partner=partner, candidates=[{"id": "c1",
                                                                      "low": low * 1.05}],
                                        fmt="png", store=False)
    assert paired["manifest"]["plot"]["kind"] == "density"
    assert paired["manifest"]["plot"]["candidates"][0]["id"] == "c1"
    # The image is calibrated (0.5 µm/px): fields are the preset's microns.
    assert manifest["field"]["source"] == "metadata"
    assert manifest["field_px"] == pytest.approx(min(sheet.field_um_default() / 0.5, 768))
    # A look at candidates: one tissue field per candidate, same geometry.
    chain = [{"id": "c1", "low": low * 1.05, "prev": low},
             {"id": "c2", "low": low * 1.1, "prev": low * 1.05}]
    per_candidate = sheet.render_context_sheet(session, ds, marker="CD8", channel="CD8",
                                               low=low, candidate_fields=chain, fmt="png",
                                               store=False)
    assert per_candidate["manifest"]["field_mode"] == "candidates"
    assert tuple(per_candidate["manifest"]["size"]) == sheet.sheet_size()
    assert {f.get("candidate") for f in per_candidate["manifest"]["fields"]} <= {"c1", "c2"}


def test_a_negative_control_candidate_is_offered(tmp_path):
    from plexora.agent import AgentSession
    from plexora.plugins.gating.server.autogate import bivariate, candidates
    from plexora.plugins.gating.server.autogate import profile as profmod

    make_gating_project(tmp_path)
    session = AgentSession()
    ds = session.data("gsynth")
    cd20 = np.asarray(ds.table.columns(["CD20"])["CD20"], dtype=np.float64)
    gate_b = float(np.percentile(cd20, 75))
    cd3 = np.asarray(ds.table.columns(["CD3"])["CD3"], dtype=np.float64)
    low = float(np.percentile(cd3, 20))
    control = bivariate.negative_control(ds, "CD3", low, "CD20", gate_b,
                                         relation="exclusive")
    assert control["population"] == "P+" and control["n"] > 0
    assert bivariate.negative_control(ds, "CD3", low, "CD20", gate_b,
                                      relation="independent") is None
    col = profmod.column(ds, "CD3")
    if control["p99_fit"] > float(col.to_fit(low)):
        up = candidates.candidate_thresholds(ds, "CD3", current_low=low, direction="up",
                                             controls=[control])
        steps = [c["step"] for c in up["candidates"]] + [r["step"] for r in up["removed"]]
        assert "ctrl:CD20" in steps
        down = candidates.candidate_thresholds(ds, "CD3", current_low=low, direction="down",
                                               controls=[control])
        assert not any(c["step"].startswith("ctrl:") for c in down["candidates"])


# -- round three: field size, pixel estimate, lean packets, partners -----------------


def test_the_pixel_size_is_estimated_from_the_cells_own_size():
    from plexora.plugins.gating.server.autogate import pixel_estimate

    rng = np.random.default_rng(0)
    # Discs of radius ten pixels (a twenty-pixel diameter), a little spread.
    areas = np.pi * rng.normal(10.0, 0.5, 400) ** 2
    found = pixel_estimate.estimate(areas)
    prior = pixel_estimate.PRIOR
    assert found["median_diameter_px"] == pytest.approx(20.0, rel=0.03)
    assert found["microns_per_pixel"] == pytest.approx(prior["cell_diameter_um"] / 20.0,
                                                       rel=0.03)
    assert found["low"] < found["microns_per_pixel"] < found["high"]
    assert pixel_estimate.estimate(areas[:5]) is None
    assert pixel_estimate.estimate([0, -1, np.nan] * 50) is None


def test_the_field_side_follows_each_images_pixel_size(tmp_path):
    from plexora.agent import AgentSession
    from plexora.plugins.gating.server.autogate import sheet

    make_gating_project(tmp_path, calibrated=False)
    record = AgentSession().project("gsynth")
    at = {mpp: sheet.field_side_px(record, 400, {"value": mpp, "source": "metadata"})
          for mpp in (0.65, 0.325)}
    assert at[0.65]["side_px"] == pytest.approx(400 / 0.65)
    assert at[0.325]["side_px"] == pytest.approx(2 * at[0.65]["side_px"])
    assert at[0.65]["label"] == "400 µm fields"
    estimated = sheet.field_side_px(record, 400, {"value": 0.5, "source": "estimated"})
    assert estimated["side_px"] == pytest.approx(800) and estimated["label"].startswith("≈")
    fallback = sheet.field_side_px(record, 400)
    assert fallback["source"] == "fallback_px" and fallback["field_um"] is None
    assert sheet.field_side_px(record)["side_px"] == fallback["side_px"]


def test_session_field_size_is_bounded_by_the_preset():
    from pydantic import ValidationError

    from plexora.agent import presets
    from plexora.plugins.gating.capabilities_session import SessionOptions

    low, high = presets.PRESETS["gating_context"]["field_um_bounds"]
    assert SessionOptions().field_um == presets.field_size("gating_context")[0]
    assert SessionOptions(field_um=low).field_um == low
    with pytest.raises(ValidationError):
        SessionOptions(field_um=high + 1)


def test_a_profile_digest_keeps_what_a_look_uses():
    from plexora.plugins.gating.server.autogate import packets

    summary = {"class": "bimodal", "separation_d": 3.1, "positive_fraction": 0.2,
               "n_positive": 20, "n_cells": 100, "signal_to_background": 4.0,
               "estimators_raw": {"gmm3": 1.0}, "quantiles_raw": {"p1": 0.1}, "flags": ["x"]}
    assert set(packets.profile_digest(summary)) == {
        "class", "separation_d", "positive_fraction", "n_positive", "n_cells",
        "signal_to_background"}


def test_the_plot_partner_is_the_most_contradictory_or_the_requested_one():
    from plexora.plugins.gating.server.autogate import packets

    refs = [{"marker": "CD45", "relation": "subset", "gate": 1.0},
            {"marker": "CD3", "relation": "exclusive", "gate": 2.0}]
    numbers = [{"partner": "CD45", "contradiction": 0.05},
               {"partner": "CD3", "contradiction": 0.4}]
    assert packets.plot_partner({}, refs, numbers)["marker"] == "CD3"
    calm = [{"partner": "CD45", "contradiction": 0.0}, {"partner": "CD3", "contradiction": 0.0}]
    assert packets.plot_partner({}, refs, calm)["marker"] == "CD45"
    asked = {"requests": [{"kind": "bivariate", "marker": "CD45", "served": True}]}
    assert packets.plot_partner(asked, refs, numbers)["why"].startswith("named")
    conditional = {"condition": {"within": "CD45"}}
    assert packets.plot_partner(conditional, refs, numbers)["marker"] == "CD45"
    assert packets.plot_partner({}, [], numbers) is None


def test_every_immune_marker_names_cd45_as_its_superset():
    """A lineage marker of leukocytes has CD45 as its `subset` partner, so its
    first look carries the CD45 numbers and a conditional gate can stand on
    them. Markers shared with other lineages (CD56 on neuroendocrine cells,
    PD-L1 on tumour cells) are the exceptions, and say so in `lineage`."""
    import re

    import yaml

    from plexora.ai import vocabulary

    path = Path(vocabulary.__file__).parent / "knowledge" / "markers.yaml"
    entries = yaml.safe_load(path.read_text(encoding="utf-8"))["markers"]
    immune = re.compile(r"\b(T|B|NK) cells?\b|macrophage|monocyte|myeloid|dendritic|"
                        r"neutrophil|regulatory T", re.I)
    shared = re.compile(r"tumou?r|neuro|neural|melano|epitheli|any lineage", re.I)
    missing = []
    for entry in entries:
        lineage = entry.get("lineage") or ""
        if entry["canonical"] == "CD45" or not immune.search(lineage) \
                or shared.search(lineage):
            continue
        relations = {p["marker"]: p["relation"] for p in entry.get("partners") or []}
        if relations.get("CD45") != "subset":
            missing.append(entry["canonical"])
    assert not missing, missing
    assert vocabulary.canonical("CD57") == "CD57"


def test_a_membrane_marker_that_follows_the_nucleus_is_not_nuclear_bleed():
    from plexora.plugins.gating.server.autogate import profile as profmod

    rng = np.random.default_rng(2)
    n = 6000
    dna = rng.lognormal(7.0, 0.3, n).astype(np.float32)
    marker = (dna * 0.02 * rng.lognormal(0.0, 0.1, n)).astype(np.float32)
    marker[: n // 5] *= 20
    ds = FakeData({"DNA": dna, "CD45": marker})
    unknown = profmod.profile_marker(ds, "CD45", compartment=None)
    membrane = profmod.profile_marker(ds, "CD45", compartment="membrane")
    assert "nuclear_bleed" in unknown["flags"]
    assert "nuclear_bleed" not in membrane["flags"]


def test_a_conditional_gate_is_fitted_among_the_partners_positives():
    from plexora.plugins.gating.server.autogate import bivariate

    rng = np.random.default_rng(4)
    n = 20_000
    immune = rng.random(n) < 0.3
    cd45 = np.where(immune, rng.normal(7.5, 0.3, n), rng.normal(4.0, 0.3, n))
    real = immune & (rng.random(n) < 0.3)
    cd57 = np.where(real, rng.normal(7.0, 0.3, n), rng.normal(4.0, 0.3, n))
    # Outside the partner the stain is a broad off-target smear reaching the
    # positives' level: no plain gate separates it, a gate among CD45+ does.
    off = ~immune & (rng.random(n) < 0.25)
    cd57[off] = rng.normal(6.0, 0.8, int(off.sum()))
    ds = FakeData({"CD45": np.expm1(cd45), "CD57": np.expm1(cd57)})
    result = bivariate.within_partner(ds, "CD57", "CD45", float(np.expm1(6.0)))
    assert result["ok"] and result["method"] == "gmm_within"
    low = np.log1p(result["low"])
    assert 4.5 < low < 6.8
    called = np.log1p(np.expm1(cd57)) > low
    assert result["n_positive_within"] == int((called & immune).sum())
    assert result["n_positive_outside"] == int((called & ~immune).sum()) > 0
    few = bivariate.within_partner(ds, "CD57", "CD45", float(np.expm1(20.0)))
    assert not few["ok"] and "reason" in few


def test_flip_cells_of_a_conditional_gate_stay_inside_the_partner():
    from plexora.plugins.gating.server.autogate import sampler

    rng = np.random.default_rng(5)
    n = 8000
    immune = rng.random(n) < 0.4
    cd45 = np.where(immune, 7.5, 4.0) + rng.normal(0, 0.2, n)
    cd57 = rng.normal(5.0, 1.0, n)
    ds = FakeData({"CD45": np.expm1(cd45), "CD57": np.expm1(cd57)})
    within = {"marker": "CD45", "gate": float(np.expm1(6.0))}
    lows = [float(np.expm1(5.0)), float(np.expm1(5.5)), float(np.expm1(6.0))]
    plain = sampler.delta_cells(ds, "CD57", lows)
    inside = sampler.delta_cells(ds, "CD57", lows, within=within)
    ids = set(np.flatnonzero(immune) + 1)          # FakeData ids run from one
    for interval in inside["intervals"]:
        assert {c["cell_id"] for c in interval["cells"]} <= ids
    assert all(a["n_flip"] < b["n_flip"]
               for a, b in zip(inside["intervals"], plain["intervals"]))
    v = np.expm1(cd57).astype(np.float32)
    assert inside["n_positive_at"][0] == int(((v > np.float32(lows[0])) & immune).sum())


def test_t4_confidence_is_measured_in_the_steps_the_candidates_took():
    from plexora.plugins.gating.server.autogate import engine

    base = {"path": "t4", "ai_confidence": 0.8, "metrics": {"d": 2.0}, "flags": []}
    # One step into the positives is three background sds: moderate, not low.
    assert engine.confidence_for({**base, "delta_step_sd": 1.0, "delta_bg_sd": 3.0}) == \
        "moderate"
    assert engine.confidence_for({**base, "delta_bg_sd": 3.0}) == "low"
    # A gate at a partner's negative control stands on the control.
    assert engine.confidence_for({**base, "delta_step_sd": 2.5, "chosen_step": "ctrl:CD45"}) \
        == "high"


def test_remarks_cap_confidence_but_their_number_never_lowers_it():
    """Two technical remarks on a confident look are a moderate gate, not a
    low one: a low gate is no reference, so counting remarks would decide
    which partner the next markers lean on (lsp11385_2: CD3e went low on two
    flags and CD8a moved to CD45's evidence)."""
    from plexora.plugins.gating.server.autogate import engine

    for path, extra in (("t2", {}), ("t4", {"delta_step_sd": 1.0})):
        base = {"path": path, "ai_confidence": 0.8, "metrics": {"d": 2.0}, "flags": [],
                **extra}
        one = engine.confidence_for({**base, "artifact_flags": ["segmentation"]})
        two = engine.confidence_for({**base, "artifact_flags": ["segmentation", "edge"]})
        assert one == two == "moderate", (path, one, two)


def test_packets_are_sent_lean():
    from plexora.plugins.gating.server.autogate import packets

    packet = {"kind": "t2_confirm", "answer_schema": {"big": "x" * 500}, "answer_with": "…",
              "evidence": {"partners": [{"contradiction": 0.123456789, "control": None,
                                         "quadrants": {"both": 3}}],
                           "candidate": {"low": 6.788859540491426}, "within_allowed": []},
              "progress": {"units_done": 1, "units_total": 9, "by_state": {"a": 1}},
              "images": [{"role": "r", "estimated_vision_tokens": 700}]}
    packets.lean(packet, {"reading": "once", "engine": "v1"})
    ev = packet["evidence"]
    assert ev["partners"][0]["contradiction"] == 0.1235 and "control" not in ev["partners"][0]
    assert ev["candidate"]["low"] == 6.788859540491426      # gate values stay exact
    assert "within_allowed" not in ev and "answer_with" not in packet
    assert packet["answer_schema"] == {"see": "reading_guide.answer_schemas.t2_confirm"}
    assert packet["progress"] == {"units_done": 1, "units_total": 9}
    assert packets.guide_version() == packets.guide_version()
    # Each engine's guide holds its own looks' schemas and every shared one;
    # together they hold every kind.
    kinds = set(__import__("plexora.plugins.gating.server.autogate.answers",
                           fromlist=["KINDS"]).KINDS)
    guides = {name: set(packets.reading_guide(name)["answer_schemas"])
              for name in packets.ENGINE_ONLY}
    assert set().union(*guides.values()) == kinds
    for name, parts in packets.ENGINE_ONLY.items():
        assert set(parts["kinds"]) <= guides[name]
        assert not set(parts["kinds"]) & set().union(
            *(g for other, g in guides.items() if other != name))
    assert packets.guide_version("v1") != packets.guide_version("v2")
    # The engines' first names give the same guide.
    assert packets.guide_version("classic") == packets.guide_version("v1")
    assert packets.guide_version("candidates") == packets.guide_version("v2")
    assert packets.guide_version() == packets.guide_version("v2")


# -- determinism: the lattice, row placement, the memo --------------------------------


def _lattice(points):
    return {"points": [{"id": p, "low": v, "sources": [p] + list(extra)}
                       for p, v, *extra in points]}


def test_a_chain_runs_nearest_first_and_stops_at_an_anchor():
    from plexora.plugins.gating.server.autogate import lattice

    lat = _lattice([("down:1sd", 4.0), ("gmm", 5.0), ("up:0.5sd", 5.5),
                    ("ctrl:CD45", 6.0), ("up:1sd", 6.5), ("edge:high", 7.0)])
    up = lattice.chain(lat, 5.0, "up")
    assert [p["id"] for p in up] == ["up:0.5sd", "ctrl:CD45"]      # never past a control
    down = lattice.chain(lat, 5.0, "down")
    assert [p["id"] for p in down] == ["down:1sd"]
    wide = _lattice([("gmm", 0.0)] + [(f"up:{i}sd", float(i)) for i in range(1, 8)]
                    + [("ctrl:CD3", 9.0)])
    capped = lattice.chain(wide, 0.0, "up", max_points=4)
    # The nearest points, not three and a far anchor: LSP11321 FOXP3's last
    # row ran from 6.13 to its MART1 control at 6.97 and could not be judged.
    assert [p["id"] for p in capped] == ["up:1sd", "up:2sd", "up:3sd", "up:4sd"]
    assert lattice.chain(wide, 7.0, "up")[-1]["id"] == "ctrl:CD3"   # a later round gets there
    assert lattice.chain(lat, 7.0, "up") == []


def test_a_capped_chain_without_an_anchor_keeps_the_nearest_points():
    """Live run lsp11385: the cap kept the farthest point, so one row spanned
    24k cells and the ceiling between was never shown."""
    from plexora.plugins.gating.server.autogate import lattice

    lat = _lattice([("score", 7.27), ("otsu", 7.24), ("down:0.5sd", 7.2),
                    ("ceiling", 7.05), ("onset", 6.57)])
    down = lattice.chain(lat, 7.277, "down", max_points=4)
    assert [p["id"] for p in down] == ["score", "otsu", "down:0.5sd", "ceiling"]


def test_a_wide_mixed_row_is_split_into_lattice_points(tmp_path):
    """A row too wide for one word is split evenly in the fit space, into
    snapped points joined to the lattice; a narrow one is left alone."""
    from plexora.agent import AgentSession
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import lattice
    from plexora.plugins.gating.server.autogate import profile as profmod

    make_gating_project(tmp_path)
    ds = AgentSession().data("gsynth")
    gmm = model.fit_for(ds, "CD8")["gate"]
    lat = lattice.build(ds, "CD8", gmm=gmm)
    col = profmod.column(ds, "CD8")
    a = float(gmm)
    b = float(col.from_fit(col.to_fit(a) + 3.0))
    before = lat["fingerprint"]
    assert lattice.split(lat, ds, "CD8", a, b, core_sd=2.0) == []        # 1.5 sds: kept
    added = lattice.split(lat, ds, "CD8", a, b, core_sd=0.5)            # 6 sds
    assert 1 <= len(added) <= lattice.MAX_CHAIN - 1
    assert all(a < p["low"] < b and p["id"].startswith("split:") for p in added)
    lows = [p["low"] for p in lat["points"]]
    assert lows == sorted(lows) and all(p in lat["points"] for p in added)
    assert lat["fingerprint"] != before
    again = lattice.build(ds, "CD8", gmm=gmm)
    assert [p["low"] for p in lattice.split(again, ds, "CD8", a, b, core_sd=0.5)] == \
        [p["low"] for p in added]                                         # the same points
    up = [p["low"] for p in lattice.chain(lat, a, "up", max_points=100)]
    assert [v for v in up if v in {p["low"] for p in added}] == [p["low"] for p in added]


def test_a_marker_is_absent_only_when_no_judged_region_shows_clear_positives():
    from plexora.plugins.gating.server.autogate import absence

    assert absence.absent({"r1": "none", "r2": "few", "r3": "none", "r4": "cannot_tell"})
    # Sparse and weak regions are chosen to have few positives: one clear
    # region anywhere is a population.
    assert not absence.absent({"r1": "clear", "r2": "none", "r3": "none", "r4": "few"})
    assert not absence.absent({"r1": "none", "r2": "cannot_tell", "r3": "cannot_tell"})
    assert not absence.absent({})


def test_an_anchor_at_the_gate_and_a_point_on_it_do_not_stop_a_chain():
    from plexora.plugins.gating.server.autogate import lattice

    lat = _lattice([("bio:CD45", 6.52), ("bio:CD3e", 6.54), ("up:1sd", 6.6)])
    assert [p["id"] for p in lattice.chain(lat, 6.5186, "up")] == ["bio:CD3e"]
    lat = _lattice([("score", 5.99), ("up:0.5sd", 6.05)])
    assert [p["id"] for p in lattice.chain(lat, 5.98999, "up")] == ["up:0.5sd"]


def test_a_trailing_capital_l_folds_but_never_a_ligand():
    from plexora.ai import vocabulary

    assert vocabulary.canonical("CD20L") == "CD20"
    assert vocabulary.canonical("HLADRL") == vocabulary.canonical("HLADR")
    assert vocabulary.canonical("CD40L") is None


def test_an_ome_channel_name_matches_its_table_column():
    """lsp11385_2 re-imported with the OME names: `03_CD45_ArgoFluor520` is the
    image channel of column `CD45`. A spelling the old rules resolved never
    changes, and two channels naming one marker match neither."""
    from plexora.ai import vocabulary

    channels = ["01_Nucleus_Hoechst", "02_AF1", "03_CD45_ArgoFluor520",
                "04_Ki-67_ArgoFluor555L", "07_CD20_ArgoFluor660L", "09_CD163_ArgoFluor602",
                "01_Nucleus_Hoechst"]
    assert vocabulary.match_name("CD45", channels) == "03_CD45_ArgoFluor520"
    assert vocabulary.match_name("Ki67", channels) == "04_Ki-67_ArgoFluor555L"
    assert vocabulary.match_name("CD20L", channels) == "07_CD20_ArgoFluor660L"
    assert vocabulary.match_name("09_CD163_ArgoFluor602", ["CD163", "CD68"]) == "CD163"
    assert vocabulary.match_name("Nucleus", channels) is None
    assert vocabulary.match_name("CD8a", channels) is None
    assert vocabulary.canonical("02_AF1") == vocabulary.AUTOFLUORESCENCE
    assert vocabulary.canonical("CD3_1_AF488") == "CD3"


def test_rows_place_the_gate_deterministically():
    from plexora.plugins.gating.server.autogate import lattice

    chain = [{"id": "c1", "low": 1.0}, {"id": "c2", "low": 2.0}, {"id": "c3", "low": 3.0}]
    place = lattice.place
    # Up: past cells that are not positive themselves, unstained or a
    # neighbour's light alike.
    assert place(chain, {"i1": "unstained", "i2": "neighbour_only",
                         "i3": "own_stain"}, "up")[0]["id"] == "c2"
    assert place(chain, {"i1": "own_stain", "i2": "own_stain",
                         "i3": "mixed"}, "down")[0]["id"] == "c2"
    # The first row says the opposite of moving: turn back (once, the engine's
    # rule); a mixed first row is the boundary; anything else keeps.
    assert place(chain, {"i1": "own_stain"}, "up")[:3:2] == (None, "reverse")
    assert place(chain, {"i1": "neighbour_only"}, "down")[:3:2] == (None, "reverse")
    assert place(chain, {"i1": "mixed"}, "up")[:3:2] == (None, "mixed")
    assert place(chain, {"i1": "cannot_tell"}, "up")[:3:2] == (None, "keep")
    # A later row cannot pull the gate past an earlier one that said stop.
    assert place(chain, {"i1": "own_stain", "i2": "unstained",
                         "i3": "unstained"}, "up")[0] is None


def test_the_rows_decide_the_direction_never_the_agent():
    """`verdicts.direction_from_rows`, rule by rule (lsp11385_2's pilot: rows
    near the gate never show the clear positives, so all-negative rows say
    raise, and only a look that saw no stain anywhere says no positives)."""
    from plexora.plugins.gating.server.autogate.verdicts import direction_from_rows

    def d(below, near, above, pattern="cytoplasmic"):
        rows = {k: v for k, v in (("below", below), ("near", near), ("above", above)) if v}
        return direction_from_rows(rows, pattern)[0]

    assert d("unstained", "neighbour_only", "neighbour_only") == "up"         # CD68
    assert d("unstained", "unstained", "unstained", pattern="none") == "no_positives"
    assert d("own_stain", "neighbour_only", "neighbour_only") == "cannot_tell"
    assert d("unstained", "unstained", "mixed") == "up"
    assert d("own_stain", "own_stain", "own_stain") == "down"
    assert d("mixed", "own_stain", "own_stain") == "down"                     # CD4
    assert d("neighbour_only", "neighbour_only", "own_stain") == "about_right"   # CD3e
    assert d("unstained", "unstained", "own_stain") == "up"
    assert d("unstained", None, "own_stain") == "about_right"
    assert d("cannot_tell", "cannot_tell", "cannot_tell") == "cannot_tell"


def test_a_lattice_is_a_function_of_its_inputs(tmp_path):
    from plexora.agent import AgentSession
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import lattice

    make_gating_project(tmp_path)
    ds = AgentSession().data("gsynth")
    gmm = model.fit_for(ds, "CD8")["gate"]
    ref = {"marker": "CD3", "relation": "subset",
           "gate": float(np.percentile(np.asarray(ds.table.columns(["CD3"])["CD3"]), 60))}
    one = lattice.build(ds, "CD8", gmm=gmm, references=[ref])
    two = lattice.build(AgentSession().data("gsynth"), "CD8", gmm=gmm, references=[ref])
    assert one["fingerprint"] == two["fingerprint"] and one["points"] == two["points"]
    lows = [p["low"] for p in one["points"]]
    assert lows == sorted(lows) and len(set(lows)) == len(lows)       # merged when they snap
    desc = model._description(ds)["CD8"]
    for p in one["points"]:
        assert model.snap_to_grid(p["low"], desc["max"], desc)[0] == p["low"]
    ids = {s for p in one["points"] for s in p["sources"]}
    assert "gmm" in ids and any(s.startswith("up:") for s in ids)
    assert lattice.point(one, "gmm")["low"] == pytest.approx(
        model.snap_to_grid(gmm, desc["max"], desc)[0])


def test_the_memo_key_ignores_ids_and_charges_but_not_evidence(tmp_path):
    from plexora.plugins.gating.server.autogate import memo

    packet = {"kind": "t2_confirm", "units": [{"project": "p", "marker": "CD3"}],
              "question": "q", "evidence": {"candidate": {"low": 1.0}},
              "packet_id": "pk_0001", "session_id": "s1", "budget": {"x": 1}}
    images = [(b"abc", "webp")]
    same = {**packet, "packet_id": "pk_0009", "session_id": "s2", "budget": {"x": 2}}
    assert memo.key(packet, images) == memo.key(same, images)
    moved = {**packet, "evidence": {"candidate": {"low": 1.01}}}
    assert memo.key(packet, images) != memo.key(moved, images)
    assert memo.key(packet, images) != memo.key(packet, [(b"abd", "webp")])
    memo.put("p", "k1", "agent", {"kind": "t2_confirm"})
    memo.put("p", "k1", "other", {"kind": "t3_biological"})
    assert memo.get("p", "k1", "agent")["answer"] == {"kind": "t2_confirm"}
    assert memo.get("p", "k1", "nobody") is None
    assert memo.forget("p", agent="other") == 1 and memo.get("p", "k1", "other") is None


def test_confidence_is_three_words_with_fixed_numbers():
    from pydantic import ValidationError

    from plexora.plugins.gating.server.autogate import answers, schemas

    fields = {"kind": "t2_confirm", "pattern": "membrane",
              "rows": {"below": "unstained", "above": "own_stain"}}
    assert answers.T2Answer(**fields, confidence="sure").confidence == "sure"
    with pytest.raises(ValidationError):
        answers.T2Answer(**fields, confidence=0.9)
    e = schemas.ENGINE
    words = schemas.AI_CONFIDENCE
    assert words["sure"] >= e["high_ai"] > words["fairly_sure"] >= e["t2_min_confidence"] \
        > words["unsure"]


# -- mixed rows split, whole-image disagreement kept (2026-10-18) --------------------


class _StubEngine:
    """Just what the transitions below call."""

    def __init__(self, unit, *, split=(), rounds=True, points=(), **options):
        self.record = {"units": {f"{unit['project']}::{unit['marker']}": unit}}
        self.options = options
        self.split = list(split)
        self.rounds = rounds
        self.points = [{"id": f"p:{v:g}", "low": v, "sources": ["gmm"]} for v in points]
        self.finalized = []
        self.settled = []

    def rounds_left(self, unit):
        return self.rounds

    def lattice_for(self, unit):
        return {"points": self.points}

    def settle(self, unit):
        self.settled.append(unit["state"])

    def split_row(self, unit, a, b):
        return [{"id": f"split:{v:g}", "low": v} for v in self.split if min(a, b) < v < max(a, b)]

    def finalize(self, unit, *, method):
        self.finalized.append(method)
        unit["state"] = "accepted_low_confidence"


def _t4_unit():
    return {"project": "p", "marker": "FOXP3", "candidate": 6.04, "direction": "up",
            "state": "awaiting_t4", "rounds": 1}


def test_a_walk_that_stops_at_a_wide_mixed_row_looks_inside_it():
    """LSP11321 FOXP3: three rows passed, the fourth (6.13 to 6.97) read
    mixed and the gate stopped at 6.13; the person's is 6.93."""
    from plexora.plugins.gating.server.autogate import transitions
    from plexora.plugins.gating.server.autogate.answers import T4Answer

    unit = _t4_unit()
    unit["candidate_steps"] = {"c1": "up:0.5sd", "c2": "gmm", "c3": "up:1sd", "c4": "up:2sd"}
    chain = [{"id": "c1", "low": 6.08}, {"id": "c2", "low": 6.09}, {"id": "c3", "low": 6.13},
             {"id": "c4", "low": 6.97}]
    words = T4Answer(intervals={"i1": "neighbour_only", "i2": "neighbour_only",
                                "i3": "neighbour_only", "i4": "mixed"}, confidence="sure")
    engine = _StubEngine(unit, split=(6.34, 6.55, 6.76), split_mixed=True)
    out = transitions._split_mixed(engine, unit, chain, words, chain[2], 3)
    assert out["split"] == [6.34, 6.55, 6.76]
    assert unit["candidate"] == 6.13 and unit["state"] == "awaiting_t4"
    assert unit["chosen_step"] == "up:1sd" and unit["t4_splits"][0]["row"] == "i4"
    # A narrow row, the switch off, or no round left: the walk stops as before.
    for engine in (_StubEngine(_t4_unit(), split=(), split_mixed=True),
                   _StubEngine(_t4_unit(), split=(6.5,), split_mixed=False),
                   _StubEngine(_t4_unit(), split=(6.5,), rounds=False, split_mixed=True)):
        assert transitions._split_mixed(engine, _t4_unit(), chain, words, chain[2], 3) is None
    # A first row that is mixed is split from the current gate.
    first = T4Answer(intervals={"i1": "mixed", "i2": "own_stain", "i3": "own_stain",
                                "i4": "own_stain"}, confidence="sure")
    unit = _t4_unit()
    out = transitions._split_mixed(_StubEngine(unit, split=(6.06,), split_mixed=True), unit,
                                   chain, first, None, 0)
    assert out["chosen"] == "split" and unit["candidate"] == 6.04


def test_a_whole_image_check_does_not_overrule_rows_that_judged_the_cells():
    """LSP11321 CD45: the walk ended at 7.76 (the person's 7.8), the
    whole-image check said too low, and the gate went to review -- after
    which 13 markers could not lean on it."""
    from plexora.plugins.gating.server.autogate import transitions
    from plexora.plugins.gating.server.autogate.answers import ConfirmAnswer

    unit = {"project": "p", "marker": "CD45", "candidate": 7.76, "state": "regression_confirm",
            "judged": [[7.76, 7.93, "own_stain"]], "directions": ["down"]}
    packet = {"units": [{"project": "p", "marker": "CD45"}]}
    engine = _StubEngine(unit, whole_image_moves=False, whole_image_reopen=False)
    out = transitions.apply_regression(engine, packet, ConfirmAnswer(verdict="too_low"))
    assert out["kept"] and unit["candidate"] == 7.76 and engine.finalized
    assert unit["whole_image_disagreed"] == "too_low"
    assert "whole_image_disagreed" in unit["look_flags"]


# -- whole-image "too low" reopens the walk; a lone clear region is looked at again --


def _reopened_unit():
    """p193 CD31: the walk stopped at 4.94 under a mixed row, the whole image
    said too low; the person's gate is 5.79."""
    return {"project": "p", "marker": "CD31", "candidate": 4.94, "state": "regression_confirm",
            "judged": [[4.86, 4.94, "neighbour_only"], [4.94, 5.10, "mixed"]],
            "directions": ["up"], "rounds": 1}


def test_a_whole_image_too_low_after_the_walk_looks_up_once_more():
    from plexora.plugins.gating.server.autogate import transitions
    from plexora.plugins.gating.server.autogate.answers import ConfirmAnswer

    packet = {"units": [{"project": "p", "marker": "CD31"}]}
    unit = _reopened_unit()
    engine = _StubEngine(unit, points=(5.10, 5.30, 5.60), whole_image_moves=False,
                         whole_image_reopen=True)
    out = transitions.apply_regression(engine, packet, ConfirmAnswer(verdict="too_low"))
    assert out["reopened"] and unit["state"] == "awaiting_t4" and unit["direction"] == "up"
    assert unit["whole_image_reopened"] == {"at": 4.94} and not engine.finalized
    # Once only: the second "too low" keeps the gate, at low confidence.
    unit["state"] = "regression_confirm"
    out = transitions.apply_regression(engine, packet, ConfirmAnswer(verdict="too_low"))
    assert out["kept"] and unit["whole_image_disagreed"] == "too_low" and engine.finalized
    # Off, nothing above, no round left, or "too high": as before.
    for engine in (_StubEngine(_reopened_unit(), points=(5.1,), whole_image_moves=False,
                               whole_image_reopen=False),
                   _StubEngine(_reopened_unit(), points=(), whole_image_moves=False,
                               whole_image_reopen=True),
                   _StubEngine(_reopened_unit(), points=(5.1,), rounds=False,
                               whole_image_moves=False, whole_image_reopen=True)):
        unit = engine.record["units"]["p::CD31"]
        out = transitions.apply_regression(engine, packet, ConfirmAnswer(verdict="too_low"))
        assert out["kept"] and "whole_image_reopened" not in unit
    unit = _reopened_unit()
    engine = _StubEngine(unit, points=(5.1,), whole_image_moves=False, whole_image_reopen=True)
    out = transitions.apply_regression(engine, packet, ConfirmAnswer(verdict="too_high"))
    assert out["kept"] and "whole_image_reopened" not in unit


def _reopened_t4(points):
    unit = _reopened_unit()
    unit.update(state="awaiting_t4", direction="up", whole_image_reopened={"at": 4.94},
                candidates={f"c{i + 1}": v for i, v in enumerate(points)},
                candidate_steps={f"c{i + 1}": f"p:{v:g}" for i, v in enumerate(points)})
    return unit


def test_the_reopened_look_passes_a_first_mixed_row_and_stops_at_own_stain():
    from plexora.plugins.gating.server.autogate import transitions
    from plexora.plugins.gating.server.autogate.answers import T4Answer

    packet = {"units": [{"project": "p", "marker": "CD31"}], "evidence": {}}
    unit = _reopened_t4((5.10, 5.30, 5.60))
    engine = _StubEngine(unit, split_mixed=False)
    out = transitions.apply_t4(engine, packet, T4Answer(
        intervals={"i1": "mixed", "i2": "neighbour_only", "i3": "own_stain"},
        confidence="fairly_sure"))
    assert unit["candidate"] == 5.30 and out["rows_passed"] == 2
    assert unit["whole_image_reopened"] == {"at": 4.94, "looked": "mixed", "to": 5.30}
    assert unit["state"] == "awaiting_regression" and engine.settled
    # Only the reopened look: the next mixed first row stops the walk as before.
    unit["state"], unit["direction"] = "awaiting_t4", "up"
    unit["candidates"] = {"c1": 5.60}
    unit["candidate_steps"] = {"c1": "p:5.6"}
    out = transitions.apply_t4(_StubEngine(unit, split_mixed=False), packet,
                               T4Answer(intervals={"i1": "mixed"}, confidence="fairly_sure"))
    assert out["chosen"] == "keep" and unit["candidate"] == 5.30


def test_a_reopened_look_that_moves_nothing_keeps_the_gate_and_never_turns():
    from plexora.plugins.gating.server.autogate import transitions
    from plexora.plugins.gating.server.autogate.answers import T4Answer

    packet = {"units": [{"project": "p", "marker": "CD31"}], "evidence": {}}
    unit = _reopened_t4((5.10, 5.30))
    engine = _StubEngine(unit, split_mixed=False)
    out = transitions.apply_t4(engine, packet, T4Answer(
        intervals={"i1": "own_stain", "i2": "own_stain"}, confidence="sure"))
    assert out["reopened"] and unit["candidate"] == 4.94 and engine.finalized
    assert unit["whole_image_disagreed"] == "too_low" and "t4_reversed" not in unit


def test_a_lone_clear_region_is_one_clear_among_judged_others():
    from plexora.plugins.gating.server.autogate import absence

    assert absence.lone_clear({"r1": "few", "r2": "few", "r3": "few", "r4": "clear"}) == "r4"
    assert absence.lone_clear({"r1": "clear", "r2": "clear", "r3": "none"}) is None
    assert absence.lone_clear({"r1": "clear", "r2": "cannot_tell", "r3": "none"}) is None
    assert absence.lone_clear({"r1": "none", "r2": "none"}) is None
    box = absence.surroundings({"x": 10, "y": 900, "width": 100, "height": 100}, (1000, 1000))
    assert box == {"x": 0, "y": 700, "width": 300, "height": 300}
    assert absence.surroundings({"x": 0, "y": 0, "width": 100, "height": 100},
                                (200, 500))["width"] == 200


def _absence_unit():
    return {"project": "p", "marker": "PDL1", "candidate": 6.98, "state": "awaiting_absence"}


def test_a_lone_clear_region_is_looked_at_again_before_it_keeps_the_marker_open():
    """LSP11761 PD-L1 (no positives to the person): regions few, few, few
    and one clear beside an edge flag kept it open."""
    from plexora.plugins.gating.server.autogate import transitions
    from plexora.plugins.gating.server.autogate.answers import AbsenceAnswer

    packet = {"units": [{"project": "p", "marker": "PDL1"}],
              "allowed": {"regions": ["r1", "r2", "r3", "r4"]}}
    close = {"units": [{"project": "p", "marker": "PDL1"}], "allowed": {"regions": ["r1"]}}
    unit = _absence_unit()
    engine = _StubEngine(unit, absence_confirm=True)
    out = transitions.apply_absence(engine, packet, AbsenceAnswer(
        regions={"r1": "few", "r2": "few", "r3": "few", "r4": "clear"}))
    assert out["close_look"] == "r4" and unit["absence_confirming"] == "r4"
    assert unit["state"] == "awaiting_absence" and len(unit["absence_rounds"]) == 1
    # The close look takes it back: the picture says absent, so another round.
    out = transitions.apply_absence(engine, close, AbsenceAnswer(regions={"r1": "none"}))
    assert out["again"] and unit["state"] == "awaiting_absence"
    assert unit["absence_rounds"][0]["words"]["r4"] == "none"
    assert unit["absence_rounds"][0]["close_look"] == {"region": "r4", "word": "none"}
    # A close look that still sees cells keeps the marker open.
    unit = _absence_unit()
    engine = _StubEngine(unit, absence_confirm=True)
    transitions.apply_absence(engine, packet, AbsenceAnswer(
        regions={"r1": "few", "r2": "none", "r3": "few", "r4": "clear"}))
    out = transitions.apply_absence(engine, close, AbsenceAnswer(regions={"r1": "clear"}))
    assert out["absent"] is False and unit["state"] == "awaiting_t2"
    assert unit["absence"]["clear"] == ["1:r4"]
    # Switched off, a lone clear region keeps the marker open at once.
    unit = _absence_unit()
    out = transitions.apply_absence(_StubEngine(unit, absence_confirm=False), packet,
                                    AbsenceAnswer(regions={"r1": "few", "r2": "few",
                                                           "r3": "few", "r4": "clear"}))
    assert out["absent"] is False and "absence_confirming" not in unit

