"""The deterministic pre-scan: maps that point at the artifacts painted in.

Every test builds a synthetic scene (`plexora.ai.qc_scenes`) with known
artifacts and checks the scan's maps -- not a detector's verdict -- light up
where the truth says, stay quiet elsewhere, and are the same bytes on a rerun.
"""

import numpy as np
import pytest

from plexora.agent import AgentSession
from plexora.plugins.qc.server import scan
from tests.qc_fixtures import SCAN_PARAMS, make_qc_project


def _truth_on_grid(result, mask):
    """A full-resolution truth mask, as the fraction of each map cell it covers."""
    grid = result.grid
    ny, nx = grid["shape"]
    s = grid["cell_full_px"]
    out = np.zeros((ny, nx))
    for iy in range(ny):
        for ix in range(nx):
            y0, x0 = int(iy * s), int(ix * s)
            block = mask[y0:int(y0 + s), x0:int(x0 + s)]
            out[iy, ix] = block.mean() if block.size else 0
    return out


def _region(info, name):
    return next(r for r in info["truth"]["regions"] if r["name"] == name)


@pytest.fixture
def scanned(tmp_path):
    def run(artifacts, **kwargs):
        info = make_qc_project(tmp_path, artifacts=artifacts, **kwargs)
        session = AgentSession()
        result, reused = scan.load_or_run(session, info["name"], params=SCAN_PARAMS)
        return info, result, reused, session
    return run


def test_the_grid_is_sized_in_microns_and_read_at_a_coarse_level(scanned):
    info, result, reused, _ = scanned(())
    grid = result.grid
    assert not reused
    assert grid["cell_um"] == pytest.approx(25.0, rel=0.3)
    assert grid["shape"][0] >= 30
    assert grid["cell_level_px"] >= scan.MAP_MIN_LEVEL_PX
    tissue = result.shared("tissue_fraction")
    assert tissue.shape == tuple(grid["shape"])
    # The glass margin is off tissue, the centre on it.
    assert tissue[0, 0] < 0.2 and tissue[grid["shape"][0] // 2, grid["shape"][1] // 2] > 0.9
    assert result.meta["cycles"]["method"] == "nuclear_repeats"
    assert [c["channels"] for c in result.meta["cycles"]["cycles"]] == \
        [["DNA_1", "CD3", "CD8"], ["DNA_2", "CD20"]]


def test_a_rerun_reuses_the_stored_scan_and_is_the_same_bytes(scanned, tmp_path):
    info, first, _, session = scanned(("blur_local",))
    scan._MEMORY.clear()
    again, reused = scan.load_or_run(session, info["name"], params=SCAN_PARAMS)
    assert reused
    assert set(again.maps) == set(first.maps)
    for key in first.maps:
        np.testing.assert_array_equal(np.nan_to_num(first.maps[key]),
                                      np.nan_to_num(again.maps[key]))
    # And a fresh run from nothing draws the same maps.
    scan._MEMORY.clear()
    fp, context = scan.plan(session, info["name"], params=SCAN_PARAMS)
    fresh = scan.run(session, info["name"], fp, context)
    for key in first.maps:
        np.testing.assert_array_equal(np.nan_to_num(first.maps[key]),
                                      np.nan_to_num(fresh.maps[key]))


def test_a_blurred_field_drops_relative_focus_only_where_it_is(scanned):
    info, result, _, _ = scanned(("blur_local",))
    truth = _truth_on_grid(result, _region(info, "blur_local")["mask"]) > 0.8
    tissue = result.tissue(core=True)
    focus = result.map("DNA_2", "focus_rel")
    inside = np.nanmedian(focus[truth])
    outside = np.nanmedian(focus[tissue & ~truth])
    assert inside < 0.5 * outside, (inside, outside)
    # The first cycle's DNA was not blurred.
    first = result.map("DNA_1", "focus_rel")
    assert np.nanmedian(first[truth]) > 0.7 * np.nanmedian(first[tissue & ~truth])


def test_saturation_and_aggregates_light_their_own_maps(scanned):
    info, result, _, _ = scanned(("saturation", "aggregates"))
    sat_truth = _truth_on_grid(result, _region(info, "saturation")["mask"]) > 0.8
    sat = result.map("CD3", "saturation")
    assert np.nanmin(sat[sat_truth]) > 0.5
    assert np.nanmax(sat[~(_truth_on_grid(result, _region(info, "saturation")["mask"]) > 0)]) \
        < 0.01
    agg_truth = _truth_on_grid(result, _region(info, "aggregates")["mask"]) > 0.6
    compact = result.map("CD8", "bright_compact")
    assert np.nanmean(compact[agg_truth]) > 5 * np.nanmean(compact[~agg_truth] + 1e-6)


def test_tile_seams_are_found_on_their_lines(scanned):
    info, result, _, _ = scanned(("tile_seams",))
    seams = result.channel("CD8")["summary"]["seams"]
    assert seams["columns"] or seams["rows"]
    clean = result.channel("CD3")["summary"]["seams"]
    assert not clean["columns"] and not clean["rows"]


def _round_core(n=40, fade_cells=3):
    """A round core on the map grid: its tissue fraction (partial along the
    rim), and a stain that fades toward the rim, as a punched core's does."""
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float64)
    depth = (n / 2 - 2) - np.hypot(xx - n / 2 + 0.5, yy - n / 2 + 0.5)
    fraction = np.clip(depth + 0.5, 0, 1)
    stain = np.full((n, n), 5.0) - 1.2 * np.clip(1 - depth / fade_cells, 0, 1)
    rng = np.random.default_rng(1)
    stain = stain + rng.normal(0, 0.01, size=stain.shape)
    return fraction, stain


def test_a_round_cores_rim_is_not_a_seam():
    """The rim steps along a curve that meets every row and column near the
    edge: measured between interior cells only, and on lines spanning most of
    the interior, it is no seam (before, it was one in every channel)."""
    fraction, stain = _round_core()
    seam, found = scan._seam_map(stain.astype(np.float32), fraction >= 0.25,
                                 core=fraction >= 0.9)
    assert not found["columns"] and not found["rows"], found
    assert not (seam >= 4.0).any()


def test_a_straight_seam_across_a_round_core_is_still_found():
    fraction, stain = _round_core()
    stain[:, 26:] += 0.3            # one tile brighter from column 26: a straight step
    _seam, found = scan._seam_map(stain.astype(np.float32), fraction >= 0.25,
                                  core=fraction >= 0.9)
    assert [c["index"] for c in found["columns"]] == [25], found
    assert not found["rows"]


def test_a_round_scene_has_no_seam_candidate(tmp_path):
    from plexora.plugins.qc.server import candidates as cand
    from plexora.plugins.qc.server.detectors import DetectorContext, run_all

    scan._MEMORY.clear()
    info = make_qc_project(tmp_path, artifacts=(), shape="round")
    result, _ = scan.load_or_run(AgentSession(), info["name"], params=SCAN_PARAMS)
    for channel in result.channels:
        seams = channel["summary"]["seams"]
        assert not seams["columns"] and not seams["rows"], (channel["name"], seams)
    raw, _ = run_all(DetectorContext(result, project=info["name"]))
    built = cand.build(raw, result, project=info["name"])
    assert not [c for c in built["ranked"] if c.class_hint == "stitching_or_tile_seam"]


def test_cycle_dropout_is_tissue_lost_in_the_second_cycle(scanned):
    info, result, _, _ = scanned(("cycle_dropout",))
    cross = result.meta["cross_cycle"]
    assert cross["available"]
    loss = result.shared("tissue_loss:c2")
    truth = _truth_on_grid(result, _region(info, "cycle_dropout")["mask"]) > 0.8
    assert np.nanmean(loss[truth]) > 0.5
    assert np.nanmean(loss[result.tissue(core=True) & ~truth]) < 0.05


def test_an_empty_channel_is_flagged(scanned):
    info, result, _, _ = scanned(("empty_channel",))
    assert "empty_channel" in result.channel("CD20")["flags"]
    assert "empty_channel" not in result.channel("CD3")["flags"]
