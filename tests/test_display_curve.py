"""How bright a marker is drawn: one curve everywhere, and the rule that sets it.

- `display_curve`: linear is the arithmetic every renderer used before curves
  existed, byte for byte; asinh becomes it as the cofactor grows; the canvas
  (services/displayCurve.js, frag.glsl) draws the same numbers.
- `calibration.rule_from`: black under the background, the background's
  noise p99 drawn dim, the bright cells' structure (the titre) drawn bright
  with room above it; a marker with no bright cells is never stretched to
  white.
- `cell_window.measure`: what the cells' pixels say -- the titre from the
  brightest cells, and a stained negatives band re-anchored on the low band.
- `calibration.pick_column`: code picks the column from a vision check's two
  lists, the numbers' vetoes first.
"""

import itertools
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# -- the curve ------------------------------------------------------------------


def test_linear_is_the_old_arithmetic_exactly():
    from plexora.server.utils import display_curve

    rng = np.random.default_rng(0)
    plane = rng.integers(0, 65535, (64, 64)).astype(np.uint16)
    low, high = 1234.5, 23456.0
    old = np.clip((np.asarray(plane).astype(np.float32) - low) / (high - low), 0.0, 1.0)
    new = display_curve.transfer(plane, (low, high), None)
    assert new.dtype == np.float32
    assert np.array_equal(old, new)
    assert np.array_equal(old, display_curve.transfer(plane, (low, high), {"kind": "linear"}))


def test_asinh_becomes_linear_as_the_cofactor_grows():
    from plexora.server.utils import display_curve

    x = np.linspace(-100, 5000, 400)
    window = (100.0, 4100.0)
    linear = display_curve.transfer(x, window)
    near = display_curve.transfer(x, window, {"kind": "asinh", "cofactor": 1e7})
    assert np.max(np.abs(near - linear)) < 1e-4
    log_like = display_curve.transfer(x, window, {"kind": "asinh", "cofactor": 50.0})
    dim = (x > 150) & (x < 600)
    assert np.all(log_like[dim] > linear[dim])          # dim values lifted
    assert np.all(np.diff(log_like) >= 0)               # monotone
    assert log_like[x <= 100].max() == 0.0 and log_like[x >= 4100].min() == 1.0


def test_inverse_and_level_agree_with_transfer():
    from plexora.server.utils import display_curve

    window = (40.0, 9000.0)
    for curve in (None, {"kind": "asinh", "cofactor": 120.0}):
        for t in np.linspace(0, 1, 21):
            x = display_curve.inverse(t, window, curve)
            assert display_curve.level(x, window, curve) == pytest.approx(t, abs=1e-9)
            assert float(display_curve.transfer(np.array([x]), window, curve)[0]) == \
                pytest.approx(t, abs=1e-5)


def test_white_for_draws_a_value_at_a_level():
    from plexora.server.utils import display_curve

    for curve in (None, {"kind": "asinh", "cofactor": 30.0}):
        white = display_curve.white_for(100.0, 700.0, 0.35, curve)
        assert display_curve.level(700.0, (100.0, white), curve) == pytest.approx(0.35)


def test_solve_places_two_values_at_two_levels():
    from plexora.server.utils import display_curve

    curve, white_gap = display_curve.solve(20.0, 1500.0, 0.12, 0.85)
    assert curve is not None
    assert display_curve.level(20.0, (0.0, white_gap), curve) == pytest.approx(0.12, abs=1e-6)
    assert display_curve.level(1500.0, (0.0, white_gap), curve) == pytest.approx(0.85,
                                                                                  abs=1e-6)
    # Already bright enough at the low end in a linear window: linear.
    curve, white_gap = display_curve.solve(500.0, 1500.0, 0.12, 0.85)
    assert curve is None and white_gap == pytest.approx(1500.0 / 0.85)


def test_the_golden_vectors_are_the_servers():
    from plexora.server.utils import display_curve

    golden = json.loads((REPO_ROOT / "tests" / "golden" / "display_curve_vectors.json")
                        .read_text(encoding="utf-8"))
    assert len(golden["cases"]) > 100
    for case in golden["cases"]:
        curve = None if case["cofactor"] is None else {"kind": "asinh",
                                                       "cofactor": case["cofactor"]}
        assert display_curve.level(case["x"], case["window"], curve) == \
            pytest.approx(case["t"], abs=1e-12)


def test_the_canvas_draws_the_same_curve():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(REPO_ROOT / "tests" / "js" / "display_curve_probe.mjs")],
                          capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "checks passed" in proc.stdout, proc.stdout
    assert proc.stdout.count("ok - ") >= 15, proc.stdout


def test_blend_plane_draws_through_the_curve():
    from plexora.server.utils import source_image

    plane = np.array([[100.0, 300.0, 4100.0]], dtype=np.float32)
    white = {"r": 255, "g": 255, "b": 255}
    linear = source_image.finish(source_image.blend_plane(
        np.zeros((1, 3, 3), np.float32), plane, (100.0, 4100.0), white))
    log_like = source_image.finish(source_image.blend_plane(
        np.zeros((1, 3, 3), np.float32), plane, (100.0, 4100.0), white,
        curve={"kind": "asinh", "cofactor": 40.0}))
    assert linear[0, 0, 0] == log_like[0, 0, 0] == 0
    assert log_like[0, 1, 0] > linear[0, 1, 0]
    assert linear[0, 2, 0] == log_like[0, 2, 0]


# -- the rule -----------------------------------------------------------------------


def _measured(black=90.0, background=100.0, noise=160.0, titre=3000.0):
    return {"black": black, "background": background, "noise_p99": noise, "titre": titre}


def test_the_rule_draws_background_faint_and_structure_bright():
    from plexora.agent.evidence import calibration as cal
    from plexora.server.utils import display_curve

    measured = _measured()
    rule = cal.rule_from(measured)
    window, curve = rule["window"], rule["curve"]
    assert window[0] == 90.0                    # black under the background
    assert curve is not None                    # a log-like curve for this spread
    noise = display_curve.level(measured["noise_p99"], window, curve)
    titre = display_curve.level(measured["titre"], window, curve)
    background = display_curve.level(measured["background"], window, curve)
    assert noise == pytest.approx(cal.NOISE_LEVEL, abs=1e-6)
    assert titre == pytest.approx(cal.TITRE_LEVEL, abs=1e-6)
    # A faint floor: not black, and darker than the noise top.
    assert 0.0 < background < noise
    assert window[1] > measured["titre"]        # room above the brightest structure


def test_a_marker_without_bright_cells_is_not_stretched_to_white():
    from plexora.agent.evidence import calibration as cal
    from plexora.server.utils import display_curve

    for titre in (None, 170.0):
        measured = _measured(titre=titre)
        rule = cal.rule_from(measured)
        assert rule["floored"]
        noise = display_curve.level(measured["noise_p99"], rule["window"], rule["curve"])
        assert noise <= cal.TITRE_LEVEL / cal.TITRE_FLOOR_GAPS + 1e-6
    measured = _measured()
    assert not cal.rule_from(measured)["floored"]


def test_the_ladder_scales_every_level_by_its_gain():
    from plexora.agent.evidence import calibration as cal
    from plexora.server.utils import display_curve

    for measured in (_measured(), _measured(noise=1100.0)):     # asinh, then linear
        rule = cal.rule_from(measured)
        columns = cal.columns_for(rule)
        assert [c["id"] for c in columns] == list(cal.COLUMN_IDS)
        assert columns[cal.COLUMN_IDS.index(cal.RULE_COLUMN)]["white"] == \
            pytest.approx(rule["window"][1])
        whites = [c["white"] for c in columns]
        assert whites == sorted(whites, reverse=True)       # c1 darkest
        for column in columns:
            for x in (120.0, 600.0, 2000.0):
                base = display_curve.level(x, rule["window"], rule["curve"])
                here = display_curve.level(x, (rule["window"][0], column["white"]),
                                           rule["curve"])
                assert here == pytest.approx(min(1.0, base * column["gain"]), abs=1e-6)


def test_a_choice_is_a_gain_over_the_rule():
    from plexora.agent.evidence import calibration as cal

    rule = cal.rule_from(_measured())
    entry = {"role": "marker", "window": list(rule["window"]), "curve": rule["curve"],
             "rule": rule, "columns": cal.columns_for(rule),
             "rule_low": cal.rule_from(_measured(black=20.0, background=30.0, noise=60.0))}
    chosen = cal.apply_choice(entry, {"column": "c2", "gain": 0.8, "chosen_by": "vision"})
    assert chosen["window"][1] == pytest.approx(entry["columns"][1]["white"])
    assert chosen["chosen_by"] == "vision"
    low = cal.apply_choice(entry, {"column": "c3", "gain": 1.0, "band": "low",
                                   "chosen_by": "vision:reanchored"})
    assert low["window"] == pytest.approx(entry["rule_low"]["window"])


# -- the cells' measurements ----------------------------------------------------------


def _crops(n, background, sd, *, ring=None, seed=0, start=0):
    from plexora.agent.evidence import crops

    rng = np.random.default_rng(seed)
    made, cells = {}, []
    yy, xx = np.mgrid[:32, :32]
    distance = np.hypot(yy - 15.5, xx - 15.5)
    for index in range(n):
        cell_id = start + index
        plane = rng.normal(background, sd, (32, 32)).astype(np.float32)
        if ring is not None:
            plane[(distance > 6) & (distance < 8)] += ring       # a membrane ring
        crop = crops.Crop(cell_id, 15.5, 15.5, (0, 0, 32, 32), 0)
        crop.planes["M"] = plane
        made[cell_id] = crop
        cells.append({"cell_id": cell_id, "x": 15.5, "y": 15.5, "value": float(index)})
    return made, cells


def test_the_titre_is_the_bright_cells_structure():
    from plexora.agent.evidence import cell_window

    negatives, neg_cells = _crops(24, 100.0, 20.0, seed=1)
    bright, bright_cells = _crops(16, 100.0, 20.0, ring=2000.0, seed=2, start=100)
    measured = cell_window.measure({**negatives, **bright}, "M",
                                   {"negatives": neg_cells, "bright": bright_cells},
                                   radius_px=9.0)
    assert measured["background"] == pytest.approx(100.0, abs=3.0)
    assert measured["black"] < measured["background"] < measured["noise_p99"]
    assert measured["titre"] == pytest.approx(2100.0, rel=0.05)     # the ring, not the disc
    assert not measured["reanchored"]
    assert len(measured["cells"]["bright"]) == 2
    assert measured["cells"]["negative"][0]["cell_id"] in {c["cell_id"] for c in neg_cells}


def test_a_stained_negatives_band_is_reanchored_on_the_low_band():
    from plexora.agent.evidence import cell_window

    # A marker on most cells: the p30-50 band is itself positive.
    stained, neg_cells = _crops(24, 900.0, 40.0, seed=3)
    low, low_cells = _crops(12, 100.0, 20.0, seed=4, start=200)
    bright, bright_cells = _crops(16, 900.0, 40.0, ring=3000.0, seed=5, start=300)
    measured = cell_window.measure({**stained, **low, **bright}, "M",
                                   {"negatives": neg_cells, "low": low_cells,
                                    "bright": bright_cells}, radius_px=9.0)
    assert measured["reanchored"] and measured["band"] == "low"
    assert measured["background"] == pytest.approx(100.0, abs=3.0)


def test_a_flat_background_is_not_reanchored():
    from plexora.agent.evidence import cell_window

    negatives, neg_cells = _crops(24, 45.0, 0.0, seed=6)
    for crop in negatives.values():
        crop.planes["M"] += 0.5                  # a hair above the low band
    low, low_cells = _crops(12, 45.0, 0.0, seed=7, start=200)
    bright, bright_cells = _crops(16, 45.0, 0.0, ring=1500.0, seed=8, start=300)
    measured = cell_window.measure({**negatives, **low, **bright}, "M",
                                   {"negatives": neg_cells, "low": low_cells,
                                    "bright": bright_cells}, radius_px=9.0)
    assert not measured["reanchored"]
    assert "low_band" in measured


def test_display_cells_are_seeded_and_banded():
    from plexora.agent.evidence import cell_window

    rng = np.random.default_rng(1)
    values = rng.gamma(2.0, 100.0, 4000)
    ids = np.arange(4000)
    xs, ys = rng.uniform(0, 1000, 4000), rng.uniform(0, 1000, 4000)
    a, _ = cell_window.select_display_cells(values, ids, xs, ys, seed=3)
    b, _ = cell_window.select_display_cells(values, ids, xs, ys, seed=3)
    assert a == b
    spec = cell_window.CELL_WINDOW
    for band, pct, take in (("low", "low_pct", "n_low"), ("near", "near_pct", "n_near"),
                            ("bright", "bright_pct", "n_bright")):
        lo, hi = np.percentile(values, spec[pct])
        assert len(a[band]) == spec[take]
        assert all(lo - 1e-6 <= c["value"] <= hi + 1e-6 for c in a[band]), band


# -- the vision check's pick -----------------------------------------------------------


def test_pick_column_over_every_answer():
    """Every pair of lists: the pick is a column the numbers allow; in both
    lists when they meet, nearest the rule's column; the rule when the
    pattern is seen nowhere; a re-anchor (or the darkest) when no column's
    background is clear."""
    from plexora.agent.evidence import calibration as cal

    rule = cal.rule_from(_measured())
    columns = cal.columns_for(rule)
    ids = list(cal.COLUMN_IDS)
    subsets = [list(s) for r in range(6) for s in itertools.combinations(ids, r)]
    levels = {"c5": {"negative_level": 0.5, "clipped_share": 0.2, "bright_level": 1.0}}
    rule_at = ids.index(cal.RULE_COLUMN)
    for background, structure in itertools.product(subsets, subsets):
        for low_band in (False, True):
            pick = cal.pick_column(columns, background, structure, levels=levels,
                                   has_low_band=low_band)
            assert pick["column"] in ids
            assert pick["column"] != "c5" or pick["chosen_by"].startswith("rule")
            both = [c for c in background if c in structure and c != "c5"]
            clear = [c for c in background if c != "c5"]
            seen = [c for c in structure if c != "c5"]
            if both:
                assert pick["chosen_by"] == "vision" and pick["column"] in both
                best = min(abs(ids.index(c) - rule_at) for c in both)
                assert abs(ids.index(pick["column"]) - rule_at) == best
            elif not clear:
                assert pick["chosen_by"] == ("vision:reanchored" if low_band else "vision:dark")
                assert pick["band"] == ("low" if low_band else "negatives")
            elif not seen:
                assert pick == {**pick, "column": cal.RULE_COLUMN,
                                "chosen_by": "rule:pattern_not_seen"}
            else:
                assert pick["chosen_by"] == "vision:conflict" and pick["column"] in clear
            assert pick["gain"] == columns[ids.index(pick["column"])]["gain"]


def test_every_column_vetoed_keeps_the_rule():
    from plexora.agent.evidence import calibration as cal

    columns = cal.columns_for(cal.rule_from(_measured()))
    levels = {c: {"negative_level": 0.9} for c in cal.COLUMN_IDS}
    pick = cal.pick_column(columns, list(cal.COLUMN_IDS), list(cal.COLUMN_IDS), levels=levels)
    assert pick["column"] == cal.RULE_COLUMN and pick["chosen_by"] == "rule:vetoed"


def test_the_viewer_applies_the_gating_contrast_only_where_nobody_set_one():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(REPO_ROOT / "tests" / "js" / "gating_contrast_probe.mjs")],
                          capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "checks passed" in proc.stdout, proc.stdout
