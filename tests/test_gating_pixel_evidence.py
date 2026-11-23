"""Pixel evidence (Stage 1 of docs/internal/bench/PIXEL_EVIDENCE.md): the
neighbour-referenced contrast, its null, the self-lit shares and the
exclusive background, on synthetic tables. The module lives beside the
offline tool (tools/bench_gates/pixel_signals.py), not in the engine: no
role passed its pre-registered test (2026-11-23)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

_PATH = Path(__file__).resolve().parents[1] / "tools" / "bench_gates" / "pixel_signals.py"
_SPEC = importlib.util.spec_from_file_location("pixel_signals", _PATH)
pe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pe)


def _lattice(n_side, spacing=10.0, jitter=0.0, seed=0):
    rng = np.random.default_rng(seed)
    g = np.arange(n_side) * spacing
    xy = np.stack(np.meshgrid(g, g), axis=-1).reshape(-1, 2).astype(np.float64)
    return xy + rng.normal(0, jitter, xy.shape)


def test_cell_diameter_is_the_median_area_s_disk():
    area = np.full(100, np.pi * 25.0)
    assert abs(pe.cell_diameter(area) - 10.0) < 1e-9
    assert pe.cell_diameter(None) is None


def test_an_isolated_cell_is_not_scored():
    xy = np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0], [500.0, 500.0]])
    idx, count = pe.neighbours(xy, 8.0)       # reach 12: the diagonal (14.1) is out
    assert count.tolist() == [2, 1, 1, 0]
    c = pe.contrast(np.array([1.0, 2.0, 3.0, 9.0]), idx)
    assert np.isnan(c[3])
    assert c[0] == 1.0 - 3.0


def test_neighbours_leave_the_cell_itself_out():
    xy = _lattice(5)
    idx, count = pe.neighbours(xy, 10.0)
    assert not (idx == np.arange(len(xy))[:, None]).any()
    # An interior cell of a square lattice: 4 at one spacing, 4 at sqrt 2.
    assert count[12] == 8


def test_disk_means_read_a_disk_and_leave_the_edge_out():
    plane = np.zeros((50, 50), dtype=np.uint16)
    plane[20:31, 20:31] = 100
    out = pe.disk_means(plane, np.array([25.0, 1.0]), np.array([25.0, 1.0]), 3.0)
    assert out[0] == 100.0
    assert np.isnan(out[1])


def test_storey_share_is_zero_under_the_null_and_one_far_from_it():
    rng = np.random.default_rng(1)
    share, _se, n = pe.storey_share(rng.uniform(0, 1, 4000))
    assert n == 4000 and share < 0.05
    share, _se, _n = pe.storey_share(np.full(100, 0.001))
    assert share == 1.0


def _scene(n_side=60, positives=(), seed=0):
    """A lattice of background cells (log values about 5) with planted
    positive cells at `positives` [(rows, level)]."""
    rng = np.random.default_rng(seed)
    xy = _lattice(n_side, jitter=1.0, seed=seed)
    values = rng.normal(5.0, 0.15, len(xy))
    for rows, level in positives:
        values[rows] = rng.normal(level, 0.1, len(rows))
    return xy, values


def test_scattered_positives_put_the_pixel_gate_between_the_populations():
    rng = np.random.default_rng(2)
    xy, values = _scene()
    rows = rng.choice(len(xy), 300, replace=False)
    values[rows] = rng.normal(6.5, 0.1, rows.size)
    idx, count = pe.neighbours(xy, 10.0)
    c = pe.contrast(values, idx)
    bounds = [round(5.0 + 0.1 * k, 2) for k in range(1, 15)]
    out = pe.signal_a(values, c, count, 5.0, bounds)
    assert out["validity"]["valid"], out["validity"]
    assert 5.4 <= out["gate"] <= 6.4


def test_a_packed_uniform_field_is_a_no_call_never_a_failed_stain():
    xy, values = _scene()
    values[:] = 7.0 + np.random.default_rng(3).normal(0, 0.01, len(values))
    values[:200] = 5.0
    idx, count = pe.neighbours(xy, 10.0)
    c = pe.contrast(values, idx)
    out = pe.signal_a(values, c, count, 6.0, [6.5, 6.8, 6.9, 7.0])
    assert out["gate"] is None
    assert not out["validity"]["valid"]
    assert all("fail" not in r for r in out["validity"]["reasons"])


def test_a_rare_marker_without_enough_standing_cells_is_a_no_call():
    rng = np.random.default_rng(4)
    xy, values = _scene()
    rows = rng.choice(len(xy), 8, replace=False)
    values[rows] = 7.0
    idx, count = pe.neighbours(xy, 10.0)
    c = pe.contrast(values, idx)
    out = pe.signal_a(values, c, count, 5.0, [5.3, 5.6, 6.0, 6.5])
    assert out["gate"] is None


def test_the_pixel_gate_is_one_of_the_candidates():
    rng = np.random.default_rng(5)
    xy, values = _scene()
    rows = rng.choice(len(xy), 400, replace=False)
    values[rows] = rng.normal(6.8, 0.2, rows.size)
    idx, count = pe.neighbours(xy, 10.0)
    c = pe.contrast(values, idx)
    bounds = [5.2, 5.5, 5.9, 6.1, 6.4, 6.6]
    out = pe.signal_a(values, c, count, 5.0, bounds)
    assert out["candidate"] in bounds


def test_tile_agreement_reports_each_tile():
    rng = np.random.default_rng(6)
    xy, values = _scene()
    rows = rng.choice(len(xy), 600, replace=False)
    values[rows] = rng.normal(6.5, 0.1, rows.size)
    idx, count = pe.neighbours(xy, 10.0)
    c = pe.contrast(values, idx)
    bounds = [round(5.0 + 0.1 * k, 2) for k in range(1, 15)]
    a = pe.signal_a(values, c, count, 5.0, bounds)
    t = pe.tile_agreement(values, a["p"], xy, bounds)
    assert len(t["tiles"]) == 9


def test_exclusive_background_ignores_cells_next_to_bright_neighbours():
    rng = np.random.default_rng(7)
    n = 2000
    partner = np.r_[np.full(1000, 8.0), np.full(1000, 4.0)]
    values = rng.normal(5.0, 0.1, n)
    nb = rng.normal(5.0, 0.1, n)
    # Half the partner's positives sit beside bright cells and carry spill.
    values[:500] += 1.5
    nb[:500] += 2.0
    out = pe.exclusive_background(values, nb, [(partner, 6.0)])
    assert out["validity"]["valid"]
    assert out["value"] < 5.5
    assert pe.exclusive_background(values, nb, [(partner, 9.0)])["value"] is None


def test_peak_onset_finds_the_rise_of_local_peaks():
    rng = np.random.default_rng(8)
    xy, values = _scene()
    rows = rng.choice(len(xy), 500, replace=False)
    values[rows] = rng.normal(6.5, 0.1, rows.size)
    idx, _count = pe.neighbours(xy, 10.0)
    c = pe.contrast(values, idx)
    bounds = [round(5.0 + 0.1 * k, 2) for k in range(1, 15)]
    out = pe.peak_onset(values, c, bounds, 5.0)
    # On independent noise the share of local peaks rises with a cell's own
    # value inside the background too, so the onset lands early: the
    # comparator's known bias (a real image's haze flattens that baseline).
    assert out["onset"] is not None and out["onset"] < 6.5
    assert out["plateau"] > out["baseline"]
