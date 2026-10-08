"""Timing and memory budgets for gating's spatial evidence layer.

A regression guard against minutes-long blowups, not a benchmark (that is
`plexora ai bench spatial`): the budgets are generous so a slower CI box does
not flake. Pinned: the neighbour graph is built once per image and reused by
every marker; the fast pass over every cell stays well under a few seconds a
marker at 200k cells; its traced memory stays bounded; and the targeted pass
reads nothing of its own -- only the crops, a read per tile touched.
"""

from __future__ import annotations

import pytest

from plexora.server.utils import jit

pytestmark = pytest.mark.skipif(not jit.enabled(), reason="the budgets assume numba")


def test_the_fast_pass_fits_its_budget_at_200k_cells():
    from plexora.ai import bench_spatial

    found = bench_spatial.fast_pass(200_000, 3)
    assert found["graph_s"] < 5.0, found
    assert found["per_marker_s_max"] < 6.0, found
    assert found["peak_mb"] < 400.0, found


def test_the_graph_is_built_once_per_image(monkeypatch):
    from plexora.ai import bench_spatial
    from plexora.plugins.gating.server.autogate import kernels, spatial

    built = []
    original = kernels.Grid.__init__

    def counting(self, *args, **kwargs):
        built.append(1)
        original(self, *args, **kwargs)

    monkeypatch.setattr(kernels.Grid, "__init__", counting)
    xs, ys, values = bench_spatial.clustered(20_000, ["M1", "M2", "M3"], seed=1)
    data = bench_spatial._Data(values, xs, ys, "once")
    spatial.clear_cache()
    for marker in ("M1", "M2", "M3"):
        spatial.spatial_summary(data, marker, 300.0, mode="C")
    assert len(built) == 1


def test_the_targeted_pass_reads_per_tile_not_per_cell():
    from plexora.ai import bench_spatial

    rows = bench_spatial.targeted_pass([200])
    for row in rows:
        assert row["measured"] == row["cells"]
        # One read per channel (and the mask) per pyramid tile touched: the
        # synthetic image's 128 px tiles hold ~1 of 200 random cells each, so
        # never more than that -- the pass itself reads nothing.
        assert row["reads"] <= 2 * row["cells"], row
        assert row["targeted_s"] < 10.0, row
