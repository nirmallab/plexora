"""`plexora ai bench spatial`: what the spatial evidence layer costs.

Gating's spatial evidence (`autogate/spatial.py`) is a fast pass over every
cell and a targeted pass over the few cells a look shows. This times both
and measures the fast pass's memory, on synthetic tables large enough to
matter -- clustered cells (dense Gaussian clusters over a sparse field) at
the sizes given -- and on a synthetic image for the targeted pass, in both
evidence modes that read pixels (A with the mask, B with centroids only).

Timing and memory are measured in separate passes (tracemalloc slows what
it watches). Nothing here touches a project of the user's: the image is
built in a temporary data directory.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import numpy as np


class _Table:
    def __init__(self, columns, frame):
        self._columns = columns
        self._frame = frame
        self.markers = list(columns)
        self.metadata_columns = []
        self.log_transformed = False
        self.is_local = True
        self.available = False
        self.expression_fingerprint = ""

    def columns(self, names):
        return {name: self._columns[name] for name in names}

    def geometry(self):
        return self._frame


class _Data:
    """Just enough of a project's handle set for the table side of gating."""

    def __init__(self, columns, xs, ys, name):
        import polars as pl

        from plexora.api.dataset import DatasetSchema

        n = xs.shape[0]
        frame = pl.DataFrame({"CellID": np.arange(1, n + 1), "X": xs, "Y": ys})
        self.name = name
        self.table = _Table(columns, frame)
        self.schema = DatasetSchema(cell_id="CellID", x="X", y="Y")
        self.project = SimpleNamespace(name=name, resources={}, image=SimpleNamespace(
            pixel_size=None, is_blank=True, src=None, real_channels=()))
        self._cache = {}

    def cached(self, key, compute):
        if key not in self._cache:
            self._cache[key] = compute()
        return self._cache[key]


def clustered(n, markers, *, seed=0, clusters=None):
    """(xs, ys, {marker: values}): `n` cells, two thirds in dense Gaussian
    clusters over a sparse uniform field; each marker positive on a share of
    the cells, more often inside clusters."""
    rng = np.random.default_rng(seed)
    side = float(np.sqrt(n) * 18.0)
    k = clusters or max(4, n // 20_000)
    centres = rng.uniform(0.1 * side, 0.9 * side, (k, 2))
    n_in = (2 * n) // 3
    which = rng.integers(0, k, n_in)
    inside = centres[which] + rng.normal(0, side / (6 * np.sqrt(k)), (n_in, 2))
    outside = rng.uniform(0, side, (n - n_in, 2))
    points = np.vstack([inside, outside])
    clustered_ = np.r_[np.ones(n_in, bool), np.zeros(n - n_in, bool)]
    values = {}
    for index, marker in enumerate(markers):
        share = np.where(clustered_, 0.35, 0.1) * (1 + 0.2 * index)
        positive = rng.random(n) < share
        logs = np.where(positive, rng.normal(7.0, 0.5, n), rng.normal(4.0, 0.5, n))
        values[marker] = np.expm1(logs).astype(np.float32)
    return points[:, 0], points[:, 1], values


def _warm(data, markers):
    """The marker's column and mixture, and the cell geometry: the profile's
    (paid by every session before any spatial evidence, switch on or off),
    not the feature layer's -- outside both the timing and the trace."""
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import profile as profmod

    for marker in markers:
        profmod.column(data, marker)
        profmod.fit_for(data, marker)
    cellmod.cells(data)


def _time_fast_pass(data, markers):
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import spatial

    started = time.perf_counter()
    radius, _basis = spatial.radius_for(data)
    cellmod.grid(data, radius)
    cellmod.neighbour_density(data, radius)
    spatial.density_class(data, radius)
    graph = time.perf_counter() - started
    per_marker = []
    for marker in markers:
        started = time.perf_counter()
        col = data.table.columns([marker])[marker]
        low = float(np.expm1(5.5)) if np.nanmax(col) > 0 else 0.0
        spatial.spatial_summary(data, marker, low, mode="C")
        per_marker.append(time.perf_counter() - started)
    return graph, per_marker


def fast_pass(n, n_markers=4, *, seed=0) -> dict:
    """{cells, graph_s, per_marker_s (median, max), peak_mb}: the neighbour
    graph and density once, then the per-cell features and the summary for
    each marker; the memory peak in a second pass. Both start after the
    profile is warm (`_warm`): its cost is every session's, not this
    layer's."""
    from plexora.plugins.gating.server.autogate import spatial

    markers = [f"M{i + 1}" for i in range(int(n_markers))]
    xs, ys, values = clustered(int(n), markers, seed=seed)
    spatial.clear_cache()
    data = _Data(values, xs, ys, f"bench_{n}_t")
    _warm(data, markers)
    graph, per_marker = _time_fast_pass(data, markers)
    spatial.clear_cache()
    data = _Data(values, xs, ys, f"bench_{n}_m")
    _warm(data, markers)
    tracemalloc.start()
    _time_fast_pass(data, markers)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    spatial.clear_cache()
    return {"cells": int(n), "markers": len(markers), "graph_s": round(graph, 3),
            "per_marker_s_median": round(float(np.median(per_marker)), 3),
            "per_marker_s_max": round(float(np.max(per_marker)), 3),
            "peak_mb": round(peak / 2 ** 20, 1),
            "bytes_per_cell": round(peak / max(1, n), 1)}


def targeted_pass(counts, *, seed=0) -> list:
    """[{mode, cells, seconds, reads}] for the targeted pass on crops of a
    synthetic image (the `heterogeneous` spatial scene), modes A and B."""
    from plexora import paths
    from plexora.agent import AgentSession
    from plexora.agent.evidence import crops as cropmod
    from plexora.agent.render import resolve_channel
    from plexora.ai import bench_data
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import spatial
    from plexora.server.utils import source_image

    rows = []
    with tempfile.TemporaryDirectory(prefix="plexora-bench-spatial-") as root:
        previous = os.environ.get("PLEXORA_DATA_PATH")
        os.environ["PLEXORA_DATA_PATH"] = root
        paths.reset()
        try:
            bench_data.register_spatial(root, "timing", scenario="heterogeneous", size=2048,
                                        seed=seed, markers=("CD3", "CD8", "CD4"))
            session = AgentSession(table_limit=2)
            ds = session.data("timing")
            c = cellmod.cells(ds)
            _i, found = resolve_channel("CD3", list(ds.project.image.real_channels))
            key = source_image.channel_key(found)
            feats = spatial.per_cell(ds, "CD3")
            gate = spatial.at_gate(ds, "CD3", feats, float(np.expm1(5.5)), mode="A")
            rng = np.random.default_rng(seed)
            for n in counts:
                picked = rng.choice(np.flatnonzero(c.valid), size=min(int(n), int(c.valid.sum())),
                                    replace=False)
                cells = [{"cell_id": int(c.ids[r]), "x": float(c.xs[r]), "y": float(c.ys[r]),
                          **{k: v for k, v in spatial.cell_record(feats, gate, int(r)).items()
                             if k == "brightest"}} for r in picked]
                for mode in ("A", "B"):
                    started = time.perf_counter()
                    crops, info = cropmod.read_cell_crops(session, ds.project, cells,
                                                          channels={"CD3": key}, crop_px=48,
                                                          tile_px=48, mask=mode == "A")
                    read = time.perf_counter() - started
                    found_ = {}
                    for first in range(0, len(cells), int(spatial.SPATIAL["max_targeted"])):
                        part = cells[first:first + int(spatial.SPATIAL["max_targeted"])]
                        found_.update(spatial.targeted_from_crops(
                            crops, "CD3", part, mode=mode, compartment="membrane",
                            radius_px=9.0))
                    rows.append({"mode": mode, "cells": len(cells), "read_s": round(read, 3),
                                 "targeted_s": round(time.perf_counter() - started - read, 3),
                                 "reads": int(info["reads"]), "measured": len(found_)})
            session.close()
        finally:
            if previous is None:
                os.environ.pop("PLEXORA_DATA_PATH", None)
            else:
                os.environ["PLEXORA_DATA_PATH"] = previous
            paths.reset()
    return rows


def markdown(fast, targeted) -> str:
    lines = ["# Spatial evidence: cost", "",
             "Fast pass (all cells; clustered synthetic tables): the neighbour graph and "
             "density once per image, then per marker the features and the summary.", "",
             "| cells | markers | graph s | per marker s (median) | per marker s (max) | "
             "peak MB | bytes/cell |", "|---|---|---|---|---|---|---|"]
    for row in fast:
        lines.append(f"| {row['cells']:,} | {row['markers']} | {row['graph_s']} | "
                     f"{row['per_marker_s_median']} | {row['per_marker_s_max']} | "
                     f"{row['peak_mb']} | {row['bytes_per_cell']} |")
    if targeted:
        lines += ["", "Targeted pass (selected cells; a 2048 px synthetic image):", "",
                  "| mode | cells | crop reads | read s | targeted s |", "|---|---|---|---|---|"]
        for row in targeted:
            lines.append(f"| {row['mode']} | {row['cells']} | {row['reads']} | "
                         f"{row['read_s']} | {row['targeted_s']} |")
    return "\n".join(lines) + "\n"


def bench_command(*, sizes=(200_000, 1_000_000), markers=4, targeted=(50, 200), out=None,
                  seed=0, emit=print) -> int:
    from plexora.server.utils import jit

    fast = [fast_pass(n, markers, seed=seed) for n in sizes]
    found = targeted_pass(targeted, seed=seed) if targeted else []
    started = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    if out is None:
        from plexora import paths

        out = paths.agent_root() / "bench" / f"spatial_{started}"
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps({"fast": fast, "targeted": found,
                                                  "numba": bool(jit.enabled())}, indent=1),
                                      encoding="utf-8")
    text = markdown(fast, found)
    (out / "summary.md").write_text(text, encoding="utf-8")
    emit(text.rstrip())
    emit(f"\nWritten: {out / 'results.json'}")
    return 0
