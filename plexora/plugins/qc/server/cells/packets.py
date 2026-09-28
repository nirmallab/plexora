"""Cell-QC packets: the cells either side of a proposed cutoff, shown.

A number is never trusted on its own (CyLinter's lesson): each module's
packet shows collages of the cells just beyond each cutoff, far beyond it, and
just inside it, and asks per side whether the cutoff is right, too aggressive
(it flags real cells), too lenient (artifacts pass), or flags biology
(`not_artifact`: that side then warns and never excludes). An adjustment moves
the cutoff by `offset_step_mad` and shows it again (at most `cell_rounds`
looks); the answer is stored as that adjustment, not as a number.
"""

from __future__ import annotations

import numpy as np

from plexora.agent.errors import AgentError
from plexora.plugins.qc.server import schemas, strictness
from plexora.plugins.qc.server.cells import modules as cell_modules

PER_ROW = 6


def _measure(engine, unit):
    ds = engine.call.session.data(unit["project"])
    scan_meta = {"cycles": ((engine.record.get("scan") or {}).get(unit["project"]) or {}).get(
        "cycles")}
    module = cell_modules.module(unit["module"])
    return ds, module, module.measure(ds, scan_meta)


def _value_key(meas):
    return next(k for k in meas if k.startswith("m_"))


def _rows_for(values, cutoffs, xs, ys, ids, side):
    """Collage rows of one side: far beyond, just beyond, just inside."""
    finite = np.isfinite(values)
    cut = cutoffs[side]
    if side == "low":
        beyond = np.flatnonzero(finite & (values < cut))
        inside = np.flatnonzero(finite & (values >= cut))
        beyond = beyond[np.argsort(values[beyond])]              # most extreme first
        inside = inside[np.argsort(values[inside])]
        far, near = beyond[:PER_ROW], beyond[::-1][:PER_ROW]
        kept = inside[:PER_ROW]
    else:
        beyond = np.flatnonzero(finite & (values > cut))
        inside = np.flatnonzero(finite & (values <= cut))
        beyond = beyond[np.argsort(-values[beyond])]
        inside = inside[np.argsort(-values[inside])]
        far, near = beyond[:PER_ROW], beyond[::-1][:PER_ROW]
        kept = inside[:PER_ROW]

    def cells(index, call):
        return [{"cell_id": int(ids[i]), "x": float(xs[i]), "y": float(ys[i]),
                 "value": float(values[i]), "call": call} for i in index]

    rows = []
    if beyond.size:
        rows.append({"label": f"{side}: far beyond", "cells": cells(far, "positive")})
        if beyond.size > PER_ROW:
            rows.append({"label": f"{side}: just beyond", "cells": cells(near, "positive")})
    if inside.size:
        rows.append({"label": f"{side}: just inside (kept)", "cells": cells(kept, "negative")})
    return rows, int(beyond.size)


def build(engine, units):
    from plexora.agent.evidence import collage

    unit = units[0]
    project = unit["project"]
    ds, module, meas = _measure(engine, unit)
    decision = unit.get("decision") or {}
    table = strictness.thresholds("standard")
    cutoffs = module.cutoffs(meas, table, decision)
    values = meas[_value_key(meas)]
    frame = ds.table.geometry()
    from plexora.server.utils.label_overlay import cell_ids

    ids, keep = cell_ids(frame, ds.schema.cell_id if ds.schema else None)
    xs = frame[ds.schema.x].to_numpy().astype(np.float64)
    ys = frame[ds.schema.y].to_numpy().astype(np.float64)
    full_ids = np.zeros(len(xs), dtype=np.int64)
    full_ids[keep] = ids
    channels = [c.get("fullname") or c.get("name") for c in ds.project.image.real_channels]
    kind = schemas.CELL_KINDS[unit["module"].split(":", 1)[0]]
    column = meas.get("_column")
    marker = column if column in channels else None
    nuclear = next((c for c in channels if c == (meas.get("_columns") or [None])[0]), None)
    if marker is None:
        from plexora.agent import presets

        marker = presets.nuclear_channel(channels) or (channels[0] if channels else None)
    sides = ["low", "high"]
    if unit["module"].startswith("channel_outlier:"):
        sides = ["high"]
    images, meta, counts = [], [], {}
    pixel = engine.pixel_for(project)
    for side in sides:
        rows, n_beyond = _rows_for(values, cutoffs, xs, ys, full_ids, side)
        counts[side] = n_beyond
        if not any(r["cells"] for r in rows):
            continue
        layout = "quadrants" if kind == "cycle_stability" else "strata"
        kwargs = {}
        if layout == "quadrants":
            first, last = (meas.get("_columns") or [None, None])[:2]
            kwargs = {"a": first if first in channels else marker,
                      "b": last if last in channels else marker}
        try:
            rendered = collage.render_collage(
                engine.call.session, ds, layout=layout, rows=rows, marker=marker,
                fmt=engine.options["image_format"], pixel=pixel,
                title=f"{unit['module']} - {side} side: cells beyond and inside the "
                      "proposed cutoff", **kwargs)
        except AgentError:
            continue
        unit.setdefault("artifacts", []).extend(a["id"] for a in (rendered.get("artifact"),)
                                                if a)
        size = rendered["manifest"]["size"]
        images.append((rendered["image"], rendered["format"], (int(size[0]), int(size[1]))))
        meta.append({"role": "cell_collage", "caption": f"{side} side",
                     "artifact_id": (rendered.get("artifact") or {}).get("id")})
    if not images:
        engine.close(unit, "decided", "no cells could be drawn beside the cutoffs; kept as "
                                      "proposed")
        return None
    rows_text = ["far beyond", "just beyond", "just inside (kept)"]
    packet = {"question": (f"{unit['module']}: are the cells beyond each proposed cutoff "
                           "artifacts (debris, blur, merged or broken segments, lost cells) "
                           "and the ones inside it real? Per side: accept, too_aggressive, "
                           "too_lenient, not_artifact (these extremes are biology) or "
                           "cannot_tell."),
              "evidence": {"module": unit["module"], "column": column, "marker": marker,
                           "space": cutoffs.get("space"),
                           "proposal": {"low": cutoffs["low"], "high": cutoffs["high"],
                                        "median": cutoffs["median"], "mad": cutoffs["mad"]},
                           "beyond": counts, "n_cells": int(np.isfinite(values).sum()),
                           "round": int(unit.get("rounds") or 0) + 1,
                           "offsets": {s: (decision.get(s) or {}).get("offset_steps", 0)
                                       for s in ("low", "high")},
                           "cluster": unit.get("cluster"), "rows": rows_text,
                           "summary": unit.get("summary"),
                           "guide": ["cells"]},
              "allowed": ["accept", "too_aggressive", "too_lenient", "not_artifact",
                          "cannot_tell"],
              "_image_meta": meta}
    return packet, images[:2]


def apply(engine, packet, answer):
    ref = packet["units"][0]
    unit = engine.record["units"][engine.unit_key_of(ref)]
    if answer.notes:
        unit.setdefault("notes", []).append(answer.notes)
    decision = unit.setdefault("decision", {})
    sides = ("high",) if unit["module"].startswith("channel_outlier:") else ("low", "high")
    moved = False
    unsure = 0
    for side in sides:
        verdict = getattr(answer, side)
        entry = decision.setdefault(side, {"offset_steps": 0, "veto": False})
        entry["verdict"] = verdict
        if verdict == "too_aggressive":
            entry["offset_steps"] = max(-2, int(entry.get("offset_steps", 0)) - 1)
            moved = True
        elif verdict == "too_lenient":
            entry["offset_steps"] = min(2, int(entry.get("offset_steps", 0)) + 1)
            moved = True
        elif verdict == "not_artifact":
            entry["veto"] = True
        elif verdict == "cannot_tell":
            unsure += 1
    if unit["module"].startswith("channel_outlier:"):
        decision["artifact"] = answer.high in ("accept", "too_lenient", "too_aggressive")
    if answer.pattern:
        decision["pattern"] = answer.pattern
    decision["confidence"] = answer.confidence
    unit["rounds"] = int(unit.get("rounds") or 0) + 1
    if unsure == len(sides):
        decision["manual_review"] = True
        engine.close(unit, "manual_review_recommended",
                     "the agent could not tell from the cells: warn only")
    elif moved and unit["rounds"] < schemas.ENGINE["cell_rounds"]:
        unit["state"] = "awaiting_look"
    else:
        engine.close(unit, "decided", "cutoffs judged on the cells beside them")
    return {"unit": unit["id"], "type": "cells", "state": unit["state"],
            "decision": {k: v for k, v in decision.items() if k in ("low", "high")}}
