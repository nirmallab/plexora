"""The spatial evidence layer, table-side: neighbours, density, background,
spill-over, the summary, the case table -- against hand-made truth.

What these pin is the module's own promises (`autogate/spatial.py`): the
kernel agrees with a k-d tree, density strata are QC-aware terciles,
proximity alone is never a verdict, a positive region is never background,
units that differ are refused, a weak cell is never a negative one, and
nothing here needs an image.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

from tests.autogate_fixtures import FakeData


# -- the kernel -----------------------------------------------------------------------


def test_neighbour_values_match_a_kd_tree():
    from scipy.spatial import cKDTree

    from plexora.plugins.gating.server.autogate import kernels

    rng = np.random.default_rng(5)
    n = 6000
    xs, ys = rng.uniform(0, 1500, n), rng.uniform(0, 1000, n)
    values = rng.normal(0, 1, n)
    values[rng.random(n) < 0.1] = np.nan          # QC's left-out cells
    flags = (rng.random(n) < 0.2) & np.isfinite(values)
    grid = kernels.Grid(xs, ys, 40.0)
    out = kernels.neighbour_values(grid, values, flags)
    tree = cKDTree(np.c_[xs, ys])
    for i in rng.choice(n, 200, replace=False):
        hits = [j for j in tree.query_ball_point([xs[i], ys[i]], 40.0) if j != i]
        valued = [j for j in hits if np.isfinite(values[j])]
        flagged = [j for j in valued if flags[j]]
        assert out["n"][i] == len(hits)
        assert out["n_valued"][i] == len(valued)
        assert out["n_flagged"][i] == len(flagged)
        if valued:
            assert out["max"][i] == pytest.approx(max(values[j] for j in valued))
            assert values[out["argmax"][i]] == pytest.approx(out["max"][i])
        else:
            assert np.isnan(out["max"][i]) and out["argmax"][i] == -1
        d = {j: np.hypot(xs[j] - xs[i], ys[j] - ys[i]) for j in hits}
        nearest = min((d[j] for j in flagged), default=np.inf)
        assert out["nearest_flagged"][i] == pytest.approx(nearest)
        assert out["nearest_any"][i] == pytest.approx(min(d.values(), default=np.inf))
    some = rng.choice(n, 50, replace=False)
    part = kernels.neighbour_values(grid, values, flags, query=some)
    assert np.array_equal(part["n"], out["n"][some])
    with pytest.raises(ValueError):
        kernels.neighbour_values(grid, values, flags, radius=500.0)


def test_neighbour_values_give_the_same_answers_without_numba():
    repo = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(f"""
        import sys, json
        sys.path.insert(0, {str(repo)!r})
        import numpy as np
        from plexora.server.utils import jit
        from plexora.plugins.gating.server.autogate import kernels
        assert not jit.enabled()
        rng = np.random.default_rng(4)
        xs, ys = rng.uniform(0, 400, 700), rng.uniform(0, 400, 700)
        v = rng.normal(0, 1, 700); v[::9] = np.nan
        out = kernels.neighbour_values(kernels.Grid(xs, ys, 25.0), v, v > 0.5)
        print(json.dumps([int(out["n"].sum()), int(out["n_flagged"].sum()),
                          float(np.nansum(out["max"])),
                          float(np.sum(out["nearest_any"][np.isfinite(out["nearest_any"])]))]))
    """)
    env = {**os.environ, "PLEXORA_NO_NUMBA": "1"}
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          env=env, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    plain = json.loads(done.stdout.strip().splitlines()[-1])
    from plexora.plugins.gating.server.autogate import kernels

    rng = np.random.default_rng(4)
    xs, ys = rng.uniform(0, 400, 700), rng.uniform(0, 400, 700)
    v = rng.normal(0, 1, 700)
    v[::9] = np.nan
    out = kernels.neighbour_values(kernels.Grid(xs, ys, 25.0), v, v > 0.5)
    assert plain[:2] == [int(out["n"].sum()), int(out["n_flagged"].sum())]
    assert plain[2] == pytest.approx(float(np.nansum(out["max"])))
    assert plain[3] == pytest.approx(float(np.sum(out["nearest_any"][
        np.isfinite(out["nearest_any"])])))


# -- scenes ---------------------------------------------------------------------------


def _two_regions(n_dense=1600, n_sparse=400, seed=0):
    """Positions: a dense block on the left, a sparse one on the right (px)."""
    rng = np.random.default_rng(seed)
    side_d = int(np.sqrt(n_dense))
    side_s = int(np.sqrt(n_sparse))
    gx, gy = np.meshgrid(np.arange(side_d), np.arange(side_d))
    dense = np.c_[gx.ravel() * 20.0, gy.ravel() * 20.0]
    gx, gy = np.meshgrid(np.arange(side_s), np.arange(side_s))
    sparse = np.c_[2000 + gx.ravel() * 60.0, gy.ravel() * 60.0]
    pts = np.vstack([dense, sparse]) + rng.uniform(-2, 2, (len(dense) + len(sparse), 2))
    region = np.array(["dense"] * len(dense) + ["sparse"] * len(sparse))
    return pts[:, 0], pts[:, 1], region


def _bimodal(n, fraction, rng, *, bg=4.0, pos=7.0, sd=0.3):
    truth = rng.random(n) < fraction
    logs = np.where(truth, rng.normal(pos, sd, n), rng.normal(bg, sd, n))
    return np.expm1(logs).astype(np.float32), truth


def test_density_strata_are_terciles_of_the_cells_qc_kept():
    from plexora.agent import cell_exclusions
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import spatial

    xs, ys, region = _two_regions()
    rng = np.random.default_rng(1)
    values, _ = _bimodal(xs.size, 0.3, rng)
    ds = FakeData({"M": values}, xs=xs, ys=ys)
    classes, counts, info = spatial.density_class(ds, 60.0)
    assert info["reason"] is None and sum(info["counts"].values()) == xs.size
    # A third of the cells are sparse: the lowest tercile is the sparse block
    # and the dense block's own edge.
    assert (classes[region == "dense"] >= 1).mean() > 0.75
    assert (classes[region == "sparse"] == 0).mean() > 0.9
    feats = spatial.per_cell(ds, "M")
    assert info["dense_rule"] == "otsu"
    assert (feats.dense_env == (region == "dense")).mean() > 0.9
    # QC leaves most dense cells out: the boundaries move, the physical
    # neighbour counts do not.
    c = cellmod.cells(ds)
    out_ids = c.ids[region == "dense"][::2]
    record = cell_exclusions.make_record(out_ids, fingerprint="q1")
    with cell_exclusions.using(record):
        qc_classes, qc_counts, qc_info = spatial.density_class(ds, 60.0)
    assert np.array_equal(qc_counts, counts)
    assert qc_info["boundaries"] != info["boundaries"] or qc_info["counts"] != info["counts"]
    assert (qc_classes[np.isin(c.ids, out_ids)] == -1).all()


def test_too_few_cells_are_all_intermediate_and_say_so():
    from plexora.plugins.gating.server.autogate import spatial

    rng = np.random.default_rng(2)
    values, _ = _bimodal(120, 0.3, rng)
    ds = FakeData({"M": values}, extent=500.0)
    classes, _counts, info = spatial.density_class(ds, 60.0)
    assert set(classes.tolist()) == {1} and "fewer than" in info["reason"]


def _spill_scene(*, spill, seed=3):
    """A dense field: strong positives scattered; their neighbours are lifted
    toward the gate when `spill`, else the near-gate cells are dim positives
    spread anywhere."""
    from plexora.plugins.gating.server.autogate import kernels

    rng = np.random.default_rng(seed)
    side = 60
    gx, gy = np.meshgrid(np.arange(side), np.arange(side))
    xs = gx.ravel() * 20.0 + rng.uniform(-2, 2, side * side)
    ys = gy.ravel() * 20.0 + rng.uniform(-2, 2, side * side)
    n = xs.size
    strong = rng.random(n) < 0.15
    logs = np.where(strong, rng.normal(7.0, 0.25, n), rng.normal(4.0, 0.25, n))
    if spill:
        grid = kernels.Grid(xs, ys, 25.0)
        _n, touching = kernels.neighbour_counts(grid, flags=strong, radius=25.0)
        lifted = ~strong & (touching > 0) & (rng.random(n) < 0.6)
        logs[lifted] = rng.normal(5.4, 0.2, lifted.sum())
    else:
        dim = ~strong & (rng.random(n) < 0.1)
        logs[dim] = rng.normal(5.4, 0.2, dim.sum())
    return FakeData({"M": np.expm1(logs).astype(np.float32)}, xs=xs, ys=ys), strong


def test_proximity_alone_is_a_candidate_never_a_verdict():
    from plexora.plugins.gating.server.autogate import spatial

    ds, _strong = _spill_scene(spill=True)
    feats = spatial.per_cell(ds, "M")
    for mode, word in (("C", "candidate"), ("A", "untested"), ("B", "untested")):
        gate = spatial.at_gate(ds, "M", feats, float(np.expm1(5.2)), mode=mode)
        states = {spatial.schemas.SPILL_STATES[s] for s in np.unique(gate.spill)}
        assert states <= {"none", word}
        assert word in states
        assert "supported" not in states       # only the pixels can say that
    # A cell far below the gate is never a spill candidate, however bright
    # its neighbour: the band is the gate's.
    gate = spatial.at_gate(ds, "M", feats, float(np.expm1(5.2)), mode="C")
    far = feats.vf < gate.g - 2 * gate.h
    assert (gate.spill[far] == spatial.SPILL["none"]).all()


def test_spill_excess_separates_spill_from_clustered_biology():
    from plexora.plugins.gating.server.autogate import spatial

    lifted, _ = _spill_scene(spill=True)
    dim, _ = _spill_scene(spill=False)
    low = float(np.expm1(5.4))
    with_spill = spatial.spatial_summary(lifted, "M", low, mode="C")
    without = spatial.spatial_summary(dim, "M", low, mode="C")
    assert with_spill["neighbours"]["spill_excess"] > 0.15
    assert without["neighbours"]["spill_excess"] < with_spill["neighbours"]["spill_excess"]
    assert "spill" in with_spill["risk"]["reasons"]
    text = json.dumps(with_spill)
    assert len(text) < 4096 and "NaN" not in text


def test_summary_without_positives_is_none_not_nan():
    from plexora.plugins.gating.server.autogate import spatial

    rng = np.random.default_rng(6)
    values = np.expm1(rng.normal(4.0, 0.2, 2500)).astype(np.float32)
    ds = FakeData({"M": values}, extent=2000.0)
    summary = spatial.spatial_summary(ds, "M", float(values.max()), mode="C")
    json.dumps(summary, allow_nan=False)
    assert summary["neighbours"]["isolated_positive_share"] is None


# -- background ------------------------------------------------------------------------


def _map_from_plane(plane, t=8):
    from plexora.plugins.gating.server.autogate import spatial

    q, n = spatial._tile_quantiles(plane.astype(np.float32), None, t, (0.125, 0.25, 0.375))
    ok = n >= 16
    bg = q[0.25]
    noise = np.maximum((q[0.375] - q[0.125]) / 1.349, 1e-3)
    return {"bg": bg, "noise": noise, "filled": ~ok, "level": 0, "tile_px": float(t),
            "tissue": "all pixels", "info": spatial.structure_of(bg, ok)}


def test_a_flat_background_is_flat_and_a_gradient_rises():
    rng = np.random.default_rng(7)
    flat = 50 + rng.normal(0, 2, (256, 256))
    xx = np.tile(np.arange(256, dtype=np.float64), (256, 1))
    ramp = 50 + 2.0 * xx + rng.normal(0, 2, (256, 256))
    assert _map_from_plane(flat)["info"]["structure"] == "flat"
    ramped = _map_from_plane(ramp)
    assert ramped["info"]["structure"] in ("smooth", "patchy")
    assert ramped["bg"][:, -1].mean() > ramped["bg"][:, 0].mean() + 300


def test_cells_read_their_tile_and_units_are_checked():
    from plexora.plugins.gating.server.autogate import spatial

    rng = np.random.default_rng(8)
    n = 3000
    xs, ys = rng.uniform(0, 255, n), rng.uniform(0, 255, n)
    values, _ = _bimodal(n, 0.2, rng, bg=np.log1p(55), pos=np.log1p(900), sd=0.1)
    bgmap = _map_from_plane(40 + 0.2 * np.tile(np.arange(256.0), (256, 1)))
    ds = FakeData({"M": values}, xs=xs, ys=ys)
    feats = spatial.per_cell(ds, "M", bgmap=bgmap)
    order = np.argsort(xs)
    assert feats.local_bg[order[-100:]].mean() > feats.local_bg[order[:100]].mean()
    gate = spatial.at_gate(ds, "M", feats, float(np.expm1(5.5)), mode="A", bgmap=bgmap)
    assert gate.snr["available"], gate.snr
    # The same cells a thousand times smaller: not the image's units.
    small = FakeData({"M": values / 1000.0}, xs=xs, ys=ys, name="small")
    feats_s = spatial.per_cell(small, "M", bgmap=bgmap)
    refused = spatial.at_gate(small, "M", feats_s, float(np.expm1(5.5)) / 1000.0, mode="A",
                              bgmap=bgmap)
    assert not refused.snr["available"] and "units" in refused.snr["reason"]
    # z-scores: no intensity at all.
    z = FakeData({"M": (np.log1p(values) - 5) / 1.0}, xs=xs, ys=ys, name="z")
    feats_z = spatial.per_cell(z, "M", bgmap=bgmap)
    refused = spatial.at_gate(z, "M", feats_z, 0.0, mode="A", bgmap=bgmap)
    assert not refused.snr["available"]


def test_a_tile_of_positives_is_never_background():
    from plexora.plugins.gating.server.autogate import spatial

    rng = np.random.default_rng(9)
    n = 4000
    xs, ys = rng.uniform(0, 255, n), rng.uniform(0, 255, n)
    positive = (xs < 64) & (ys < 64)
    logs = np.where(positive, rng.normal(np.log1p(900), 0.1, n), rng.normal(np.log1p(55), 0.1,
                                                                             n))
    ds = FakeData({"M": np.expm1(logs).astype(np.float32)}, xs=xs, ys=ys)
    bgmap = _map_from_plane(50 + rng.normal(0, 2, (256, 256)), t=16)
    feats = spatial.per_cell(ds, "M", bgmap=bgmap)
    gate = spatial.at_gate(ds, "M", feats, float(np.expm1(5.5)), mode="A", bgmap=bgmap)
    inside = positive & feats.eligible
    assert gate.unreliable[inside].mean() > 0.8
    assert (gate.snr_class[gate.unreliable] == spatial.SNR_UNKNOWN).all()


def test_the_map_survives_a_round_trip():
    from plexora.plugins.gating.server.autogate import spatial

    bgmap = _map_from_plane(50 + 2.0 * np.tile(np.arange(256.0), (256, 1)))
    encoded = spatial.encode_map(bgmap)
    json.dumps(encoded)
    back = spatial.decode_map(encoded)
    assert np.allclose(back["bg"], bgmap["bg"], rtol=1e-3)
    assert np.array_equal(back["filled"], bgmap["filled"])
    assert back["fingerprint"] == encoded["fingerprint"] == spatial.map_fingerprint(back)


# -- the case table ------------------------------------------------------------------


def test_the_case_table():
    from plexora.plugins.gating.server.autogate import verdicts

    case = verdicts.spatial_case
    assert case({"below": "unstained", "near": "mixed", "above": "own_stain"}) is None
    assert case({"reference": "unstained", "clear": "own_stain"}, side="above")[0] == "E"
    assert case({"bright": "neighbour_only", "clear": "own_stain", "reference": "own_stain"},
                side="above")[:2] == ("B", "hold_flag_spill")
    assert case({"bright": "mixed", "clear": "mixed", "reference": "own_stain"},
                summary={"spill_share_near_gate": 0.4}, side="above")[0] == "B"
    assert case({"low_snr": "unstained", "clear": "own_stain", "reference": "own_stain"},
                side="above")[:2] == ("C", "hold_investigate_background")
    assert case({"background": "own_stain", "clear": "own_stain", "reference": "own_stain"},
                side="above")[0] == "C"
    # Unstained everywhere, beside a neighbour or not: the gate is too low.
    assert case({"clear": "unstained", "dense_clear": "unstained", "low_snr": "unstained",
                 "bright": "neighbour_only", "reference": "own_stain"},
                side="above")[0] == "A"
    assert case({"clear": "unstained", "dense_clear": "unstained", "low_snr": "unstained",
                 "bright": "unstained", "reference": "own_stain"},
                side="above")[:2] == ("A", "move_up")
    assert case({"clear": "own_stain", "dense_clear": "own_stain", "reference": "own_stain"},
                side="above")[1] == "about_right"
    assert case({"clear": "own_stain", "dense_clear": "own_stain", "reference": "own_stain"},
                side="below")[:2] == ("D", "move_down")
    assert case({"clear": "unstained", "reference": "own_stain"}, side="below")[1] == \
        "about_right"
    found = case({"clear": "own_stain", "dense_clear": "unstained", "reference": "own_stain"},
                 side="above")
    assert found[0] == "E" and "density_disagreement" in found[3]
    # An explanation alone never moves the gate.
    assert case({"clear": "mixed", "reference": "own_stain"},
                ["threshold_error"], side="above")[1] == "provisional_review"
    found = case({"clear": "cannot_tell", "reference": "own_stain"},
                 ["possible_segmentation_error"], side="above")
    assert found[0] == "E" and "segmentation_suspected" in found[3]


def test_every_explanation_word_has_a_decision_rule():
    """Each word either joins a trigger or a case rule, or is what the case
    table falls back on -- never one nobody reads."""
    import inspect

    from plexora.plugins.gating.server.autogate import schemas, verdicts

    text = inspect.getsource(verdicts)
    for word in schemas.EXPLANATIONS:
        assert word in text or word == "insufficient_evidence", word
    used = set(verdicts.SPILL_WORDS + verdicts.BACKGROUND_WORDS + verdicts.WEAK_WORDS
               + verdicts.SEGMENTATION_WORDS)
    assert used | {"threshold_error", "insufficient_evidence"} == set(schemas.EXPLANATIONS)


def test_a_raise_only_neighbours_argue_for_is_compared_first():
    from plexora.plugins.gating.server.autogate import verdicts

    trigger = verdicts.contrast_trigger
    rows = {"below": "unstained", "near": "neighbour_only", "above": "mixed"}
    assert trigger("up", rows, (), {"spill_share_near_gate": 0.3}) == "above"
    assert trigger("up", rows, (), {"spill_share_near_gate": 0.12}) == "above"
    # Words without numbers, or numbers without words: no contrast.
    assert trigger("up", rows, (), {"spill_share_near_gate": 0.02, "risk": "low"}) is None
    assert trigger("up", {"below": "unstained", "near": "unstained", "above": "unstained"},
                   (), {"spill_share_near_gate": 0.5, "risk": "high"}) is None
    assert trigger("up", rows, ["threshold_error"], {"spill_share_near_gate": 0.12}) is None
    assert trigger("down", {"below": "own_stain"}, (), {"risk": "moderate"}) == "below"
    assert trigger("down", {"below": "own_stain"}, (), {"risk": "low",
                                                       "spill_share_near_gate": 0.0}) is None
    assert trigger("up", {"near": "unstained", "above": "unstained"},
                   ["possible_local_background"], {"snr_pf_ratio": 2.5}) == "above"


def test_environments_are_allocated_deterministically():
    from plexora.plugins.gating.server.autogate import sampler

    counts = {"sparse/high": 50, "dense/high": 10, "dense/low": 1}
    take = sampler.allocate(6, counts)
    assert take == sampler.allocate(6, dict(reversed(list(counts.items()))))
    assert sum(take.values()) == 6 and take["dense/low"] == 1
    assert take["sparse/high"] >= take["dense/high"] >= 1
    assert sampler.allocate(6, {"sparse": 2, "dense": 1}) == {"sparse": 2, "dense": 1}
    assert sampler.allocate(6, {}) == {}


def test_t1_audit_reads_numbers_only():
    from plexora.plugins.gating.server.autogate import spatial

    clean = {"risk": {"level": "low"}, "density_pf": {"sparse": 0.2, "dense": 0.25},
             "density_counts": {"sparse": 900, "dense": 900}, "spill_excess": 0.01}
    assert spatial.t1_audit(clean) == []
    dense = {**clean, "density_pf": {"sparse": 0.05, "dense": 0.3}}
    assert spatial.t1_audit(dense)
    thin = {**dense, "density_counts": {"sparse": 50, "dense": 900}}
    assert spatial.t1_audit(thin) == []
    assert spatial.t1_audit({**clean, "risk": {"level": "high"}})


# -- the targeted pass ----------------------------------------------------------------


def _crop(plane, labels, cell_id=1, level=0, x=None, y=None):
    from plexora.agent.evidence.crops import Crop

    side = plane.shape[0]
    x = side / 2 if x is None else x
    y = side / 2 if y is None else y
    crop = Crop(cell_id, x, y, (0, 0, side, side), level)
    crop.planes = {"M": plane.astype(np.float32)}
    crop.labels = labels
    return crop


def _two_cells(stain):
    """A 40x40 crop: cell 1 at the centre, cell 2 touching it on the right."""
    yy, xx = np.mgrid[:40, :40]
    labels = np.zeros((40, 40), dtype=np.uint32)
    labels[np.hypot(xx - 20, yy - 20) <= 8] = 1
    labels[(np.hypot(xx - 36, yy - 20) <= 8) & (labels == 0)] = 2
    plane = np.full((40, 40), 50.0) + np.random.default_rng(0).normal(0, 1, (40, 40))
    own = labels == 1
    d = np.hypot(xx - 20, yy - 20)
    if stain == "spill":
        plane[labels == 2] = 900.0
        plane[own & (xx >= 25)] = 400.0         # the neighbour's light, on one side
    elif stain == "ring":
        plane[labels == 2] = 900.0
        plane[own & (d > 6)] = 600.0             # its own membrane, all round
    elif stain == "polarised":
        plane[labels == 2] = 900.0
        plane[own & (d > 6)] = 250.0
        plane[own & (d > 6) & (xx >= 25)] = 900.0
    return plane, labels


def test_the_targeted_pass_reads_where_the_stain_is():
    from plexora.plugins.gating.server.autogate import spatial

    cells = [{"cell_id": 1, "x": 20.0, "y": 20.0}]
    spill = spatial.targeted_from_crops({1: _crop(*_two_cells("spill"))}, "M", cells,
                                        mode="A", compartment="membrane", radius_px=8)
    assert spill[1]["spill"] == "supported" and spill[1]["brightest_adjacent"] == 2
    ring = spatial.targeted_from_crops({1: _crop(*_two_cells("ring"))}, "M", cells,
                                       mode="A", compartment="membrane", radius_px=8)
    assert ring[1]["spill"] == "unsupported" and ring[1]["compartment_reading"] == "expected"
    # A membrane stain brighter toward the neighbour, but above background on
    # the far side too: polarisation, not spill.
    polar = spatial.targeted_from_crops({1: _crop(*_two_cells("polarised"))}, "M", cells,
                                        mode="A", compartment="membrane", radius_px=8)
    assert polar[1]["spill"] == "unsupported"


def test_without_a_mask_the_numbers_are_estimates():
    from plexora.plugins.gating.server.autogate import spatial

    plane, _labels = _two_cells("spill")
    cells = [{"cell_id": 1, "x": 20.0, "y": 20.0, "brightest": {"x": 36.0, "y": 20.0}}]
    found = spatial.targeted_from_crops({1: _crop(plane, None)}, "M", cells, mode="B",
                                        compartment="membrane", radius_px=8)
    record = found[1]
    assert record["centroid_estimate"] is True and record["support_strength"] == "approximate"
    assert "rim_over_core" not in record and "core_mean" not in record
    assert record["toward_over_away"] > 1.3


def test_no_correction_is_offered_until_one_is_validated():
    from plexora.agent.errors import AgentError
    from plexora.plugins.gating.server.autogate import spatial

    assert spatial.CORRECTIONS == {}
    ds = FakeData({"M": np.ones(10, dtype=np.float32)})
    with pytest.raises(AgentError):
        spatial.corrected_values(ds, "M", "unmixing")
