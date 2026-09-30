"""The cell modules' deterministic half, inside the session's bulk pass.

Each planned module checks it can run here (the columns it needs), measures,
and proposes cutoffs under every preset. A module with nothing beyond even the
strictest cutoff is decided without a look, and so is one with nothing beyond
its proposed (standard) cutoffs and almost nothing near them; channel
outliers are kept for the markers with the most extremes (at most
`MAX_OUTLIER_MARKERS`) among markers that have a positive population to be
extreme against. Whether the extremes cluster in space is computed here as
context for the agent's look; it is not evidence of an artifact and decides
nothing.
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.qc.server import strictness
from plexora.plugins.qc.server.cells import modules as cell_modules


#: [cal] a module with no cell beyond its proposed cutoffs is accepted without
#: a look when fewer than this many cells sit within half a step inside them
#: (less than one collage row to judge from).
AUTO_ACCEPT_NEAR = 6


def _nothing_to_show(at_standard):
    return all(side["beyond"] == 0 and side["near"] < AUTO_ACCEPT_NEAR
               for side in at_standard.values())


def _summary(values):
    finite = values[np.isfinite(values)]
    if not finite.size:
        return {"n": 0}
    q = np.percentile(finite, [1, 5, 25, 50, 75, 95, 99])
    return {"n": int(finite.size), "quantiles": {f"p{p}": float(v) for p, v in
                                                  zip((1, 5, 25, 50, 75, 95, 99), q)}}


def run(call, session_id):
    from plexora.plugins.qc.server.engine import engine_for

    with engine_for(call, session_id, save=False) as engine:
        project = engine.project
        pending = [dict(u) for u in engine.units_of("cells") if u["state"] == "pending"]
        scan_meta = {"cycles": ((engine.record.get("scan") or {}).get(project) or {}).get(
            "cycles")}
    if not pending:
        return
    ds = call.session.data(project)
    frame = ds.table.geometry()
    xs = frame[ds.schema.x].to_numpy().astype(np.float64)
    ys = frame[ds.schema.y].to_numpy().astype(np.float64)
    strict = strictness.thresholds("strict")
    prepared = {}
    outlier_counts = {}
    for unit in pending:
        module = cell_modules.module(unit["module"])
        ok, why = module.available(ds, scan_meta)
        if not ok:
            prepared[unit["module"]] = {"close": ("skipped_not_applicable", why)}
            continue
        meas = module.measure(ds, scan_meta)
        cutoffs = {preset: module.cutoffs(meas, strictness.thresholds(preset))
                   for preset in ("lenient", "standard", "strict")}
        outlier = isinstance(module, cell_modules.ChannelOutlier)
        if outlier:
            flagged = module.extremes(meas, cutoffs["strict"])
        else:
            ex, wa = module.calls(meas, cutoffs["strict"], {}, strict)
            flagged = np.zeros(len(xs), dtype=bool)
            for mask in list(ex.values()) + list(wa.values()):
                flagged |= np.asarray(mask, dtype=bool)
        values = meas[cell_modules.value_key(meas)]
        entry = {"summary": {k: _summary(v) for k, v in meas.items() if k.startswith("m_")},
                 "proposals": {p: cell_modules.public(c) for p, c in cutoffs.items()},
                 "flagged_strict": int(flagged.sum()),
                 "column": meas.get("_column"),
                 "at_standard": {side: dict(zip(("beyond", "near"), cell_modules.beyond_and_near(
                     values, cutoffs["standard"], side)))
                     for side in cell_modules.sides_of(unit["module"])}}
        if outlier:
            if cutoffs["standard"].get("reference") == "none":
                prepared[unit["module"]] = {"close": ("skipped_not_applicable",
                                                      cutoffs["standard"]["why"])}
                continue
            bright = module.extremes(meas, cutoffs["standard"])
            clustered, enrichment, box = cell_modules.clustered(xs, ys, bright)
            entry["clustered"] = {"clustered": clustered, "enrichment": enrichment, "box": box}
            outlier_counts[unit["module"]] = int(bright.sum())
        prepared[unit["module"]] = entry
    # Outliers: the busiest markers only.
    ranked = sorted((m for m, c in outlier_counts.items()
                     if c >= cell_modules.MIN_EXTREMES), key=lambda m: -outlier_counts[m])
    keep = set(ranked[:cell_modules.MAX_OUTLIER_MARKERS])
    with engine_for(call, session_id) as engine:
        for unit in engine.units_of("cells"):
            entry = prepared.get(unit["module"])
            if entry is None or unit["state"] != "pending":
                continue
            if "close" in entry:
                engine.close(unit, *entry["close"])
                continue
            unit.update(summary=entry["summary"], proposals=entry["proposals"],
                        column=entry.get("column"), flagged_strict=entry["flagged_strict"])
            decision = {}
            if "clustered" in entry:
                decision["clustered"] = bool(entry["clustered"]["clustered"])
                unit["cluster"] = entry["clustered"]
                if unit["module"] not in keep:
                    engine.close(unit, "skipped_not_applicable",
                                 "too few extreme cells to judge" if outlier_counts.get(
                                     unit["module"], 0) < cell_modules.MIN_EXTREMES
                                 else "other markers had more extremes")
                    continue
            unit["decision"] = decision
            if entry["flagged_strict"] == 0:
                engine.close(unit, "decided", "no cell is beyond even the strictest cutoff")
                continue
            unit["at_standard"] = entry["at_standard"]
            if _nothing_to_show(entry["at_standard"]):
                # A look could only show an empty "beyond" row and a handful
                # of kept cells: the one answer it could draw is "too
                # lenient", on evidence too thin to move a cutoff by. Accepted
                # as proposed, and said so (a stricter preset's own cutoff
                # still applies to the cells beyond it).
                unit["auto_accepted"] = entry["at_standard"]
                engine.close(unit, "decided", "no cell beyond either proposed cutoff and "
                                              f"fewer than {AUTO_ACCEPT_NEAR} within half a "
                                              "step of it: accepted without a look")
                continue
            unit["state"] = "awaiting_look"
