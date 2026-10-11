"""`plexora ai bench cellcall`: how fast the cell model reads a project.

Gating's cell model (`autogate/cellcall.py`) reads every cell near a gate --
crops from the pyramid, tensors, an onnxruntime ensemble -- about a minute a
marker. This times it on a real project at the gates it holds (the Auto gate
where a marker has none), for each number of workers asked for, and reports
wall time, cells a second and the process's peak resident memory.

Nothing is written to the project or to its stored runs: the runs go to a
temporary folder and are thrown away. The model is the configured one
(`cellcall_weights.ensure`, or the unpinned `PLEXORA_CELLCALL_MODEL_FILE`).
"""

from __future__ import annotations

import json
import resource
import sys
import tempfile
import time
from pathlib import Path


def _peak_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Bytes on macOS, kilobytes on Linux.
    return round(peak / (1 << 20) if sys.platform == "darwin" else peak / 1024, 1)


def run(project, *, markers=None, workers=(0,), band_halves=None, max_cells=None) -> list:
    """Rows {workers, marker, n_run, seconds, cells_per_s, peak_mb} -- a
    `workers` of 0 is the default split (`cellcall.default_workers`)."""
    from plexora.agent import AgentSession, registry
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import cellcall, cellcall_weights

    registry.discover(["gating"])
    session = AgentSession(table_limit=2)
    rows = []
    saved = cellcall._cache_dir
    try:
        ds = session.data(project)
        active = {m: float(low) for m, (low, _high) in model.active_gates(ds).items()}
        wanted = list(markers or active or ds.table.markers)
        gates = {}
        for marker in wanted:
            if marker in active:
                gates[marker] = active[marker]
            else:
                fit = model.fit_for(ds, marker)
                if fit is not None:
                    gates[marker] = float(fit["gate"])
        path = cellcall_weights.ensure()
        with tempfile.TemporaryDirectory(prefix="plexora-cellcall-bench-") as folder:
            cellcall._cache_dir = lambda d: Path(folder) / d.name
            for count in workers:
                pool = cellcall.Pool(path, workers=int(count) or None)
                for marker, low in gates.items():
                    started = time.perf_counter()
                    try:
                        found = cellcall.run(session, ds, marker, low, gates=gates, model=pool,
                                             refresh=True, band_halves=band_halves,
                                             max_cells=max_cells)
                    except cellcall.Unavailable as exc:
                        rows.append({"workers": pool.workers, "marker": marker,
                                     "unavailable": str(exc)})
                        continue
                    seconds = time.perf_counter() - started
                    n = int(found["info"]["n_run"])
                    rows.append({"workers": pool.workers, "marker": marker, "n_run": n,
                                 "seconds": round(seconds, 1),
                                 "cells_per_s": round(n / seconds, 1) if seconds else None,
                                 "peak_mb": _peak_mb(), "model": found["info"]["model"]})
                    print(f"workers {pool.workers}  {marker:10} {n:7d} cells  "
                          f"{seconds:6.1f} s  {n / max(seconds, 1e-9):7.1f} cells/s  "
                          f"peak {_peak_mb():.0f} MB", flush=True)
    finally:
        cellcall._cache_dir = saved
        session.close()
    return rows


def bench_command(*, project, markers=None, workers="0", band_halves=None, max_cells=None,
                  out=None) -> int:
    counts = [int(w) for w in str(workers).split(",") if w.strip()] or [0]
    rows = run(project, markers=markers, workers=counts, band_halves=band_halves,
               max_cells=max_cells)
    by = {}
    for row in rows:
        if "n_run" in row:
            entry = by.setdefault(row["workers"], [0, 0.0])
            entry[0] += row["n_run"]
            entry[1] += row["seconds"]
    for count, (n, seconds) in sorted(by.items()):
        print(f"workers {count}: {n} cells in {seconds:.0f} s ({n / max(seconds, 1e-9):.0f} "
              f"cells/s)")
    if out:
        folder = Path(out)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "cellcall_bench.json").write_text(json.dumps(rows, indent=1))
    return 0
