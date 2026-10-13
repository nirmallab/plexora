"""How fast cell surfaces are made: a timing, not a correctness check.

Opt in with `PLEXORA_PERF=1` (and `-m perf -s` to see the numbers): 2,000
packed ellipsoids in a 48 x 1024 x 1024 label store, the store Plexora builds,
extracted in one pass. The budget is generous for a laptop; the printout is
what goes in docs/internal/3d/SURFACES.md.
"""

import os
import time

import numpy as np
import pytest

from plexora.server.surface import labels as label_surfaces
from plexora.server.surface import marching
from plexora.server.surface import mesh as mesh_mod
from plexora.server.utils import jit

pytestmark = [
    pytest.mark.perf,
    pytest.mark.skipif(not os.environ.get("PLEXORA_PERF"), reason="set PLEXORA_PERF=1 to time"),
    pytest.mark.skipif(not jit.enabled(), reason="timed with compiled kernels only"),
]


def _packed_cells(depth=48, size=1024, cells=2000, seed=3):
    rng = np.random.default_rng(seed)
    labels = np.zeros((depth, size, size), np.uint32)
    zz, yy, xx = np.ogrid[:depth, :size, :size]
    for label in range(1, cells + 1):
        c = rng.uniform([4, 12, 12], [depth - 4, size - 12, size - 12])
        r = rng.uniform([3, 6, 6], [6, 11, 11])
        z0, z1 = int(max(0, c[0] - r[0])), int(min(depth, c[0] + r[0] + 1))
        y0, y1 = int(max(0, c[1] - r[1])), int(min(size, c[1] + r[1] + 1))
        x0, x1 = int(max(0, c[2] - r[2])), int(min(size, c[2] + r[2] + 1))
        inside = (((zz[z0:z1] - c[0]) / r[0]) ** 2 + ((yy[:, y0:y1] - c[1]) / r[1]) ** 2
                  + ((xx[:, :, x0:x1] - c[2]) / r[2]) ** 2) <= 1.0
        box = labels[z0:z1, y0:y1, x0:x1]
        box[inside & (box == 0)] = label
    return labels


@pytest.mark.parametrize("threads", [1, 4])
def test_two_thousand_cells_in_one_pass(tmp_path, threads):
    from plexora.server.volume import labels as volume_labels

    array = _packed_cells()
    store = tmp_path / "cells.zarr"
    volume_labels.build_label_volume(array, store)
    volume = volume_labels.open_label_volume(store)
    jit.prime()
    started = time.perf_counter()
    ids, counts = label_surfaces.label_counts(volume)
    counted = time.perf_counter() - started
    grid = marching.Grid.of(array.shape)
    started = time.perf_counter()
    surface, report = label_surfaces.extract_labels(
        volume, 0, grid, smoothing=mesh_mod.Smoothing.preset("light"), threads=threads)
    took = time.perf_counter() - started
    voxels = array.size
    engine = report["engine"]
    print(f"\n{threads} thread(s): {surface.n_objects} cells, {surface.n_triangles / 1e6:.2f} M "
          f"triangles in {took:.2f} s (extract {engine['seconds_extract']:.2f} s, smooth+normals "
          f"{engine['seconds_finish']:.2f} s); {surface.n_objects / took:,.0f} cells/s, "
          f"{voxels / 1e6 / engine['seconds_extract']:,.0f} Mvox/s; coarse count "
          f"{counted:.2f} s for {len(ids)} ids")
    assert surface.n_objects == len(np.unique(array)) - 1
    assert took < 30
