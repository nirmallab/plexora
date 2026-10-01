"""Cell-QC packets: the cells either side of a proposed cutoff, shown.

A number is never trusted on its own (CyLinter's lesson): each module's
packet shows collages of the cells just beyond each cutoff, far beyond it, and
just inside it, and asks per side whether the cutoff is right, too aggressive
(it flags real cells), too lenient (artifacts pass), or flags biology
(`not_artifact`: that side then warns and never excludes). An adjustment moves
the cutoff by one step (`modules.step_size`: at least a MAD, and at least a
quarter of the cutoff's distance from the median) and shows it again (at most
`cell_rounds` looks); the answer is stored as that adjustment, not as a
number.

Modules are judged together: `next_group` puts up to `cell_batch` modules
awaiting a look into one `cell_modules` packet -- one strata collage holding
every module's rows (each row labelled with its module, drawn in its own
marker), and the cycle-stability quadrants beside it -- answered per module
with the same fields a single module's packet takes. A side with nothing
beyond its cutoff and almost nothing near it is not drawn (and is accepted):
the look would have nothing to show there.
"""

from __future__ import annotations

import numpy as np

from plexora.agent.errors import AgentError
from plexora.plugins.qc.server import schemas, strictness
from plexora.plugins.qc.server.cells import modules as cell_modules

PER_ROW = 6
#: Collage rows per image of a combined packet (a module is never split).
ROWS_PER_IMAGE = 12
#: A side is drawn when cells are beyond its cutoff, or at least this many
#: sit within half a step inside it (the same bar `cells.bulk` accepts by).
NEAR_TO_SHOW = 6
COMBINED_KIND = "cell_modules"
#: A Segmentation QC collage crop, in nuclear diameters.
SEG_CROP_NUCLEI = 4.0


def _measure(engine, unit):
    ds = engine.call.session.data(unit["project"])
    scan_meta = {"cycles": ((engine.record.get("scan") or {}).get(unit["project"]) or {}).get(
        "cycles")}
    module = cell_modules.module(unit["module"])
    return ds, module, module.measure(ds, scan_meta)


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


def _positions(engine, ds):
    """(xs, ys, full-row cell ids) of the table, once per engine."""
    cached = getattr(engine, "_cell_positions", None)
    if cached is not None and cached[0] == ds.name:
        return cached[1]
    from plexora.server.utils.label_overlay import cell_ids

    frame = ds.table.geometry()
    ids, keep = cell_ids(frame, ds.schema.cell_id if ds.schema else None)
    xs = frame[ds.schema.x].to_numpy().astype(np.float64)
    ys = frame[ds.schema.y].to_numpy().astype(np.float64)
    full_ids = np.zeros(len(xs), dtype=np.int64)
    full_ids[keep] = ids
    engine._cell_positions = (ds.name, (xs, ys, full_ids))
    return xs, ys, full_ids


def view(engine, unit):
    """What a look at one module would show: its cutoffs, the rows of every
    side worth drawing, and how it is drawn. Memoised on the engine (the
    grouping and the builder of one packet read the same view)."""
    views = engine.__dict__.setdefault("_cell_views", {})
    decision = unit.get("decision") or {}
    key = (unit["module"], repr(sorted((k, repr(v)) for k, v in decision.items())))
    if key in views:
        return views[key]
    ds, module, meas = _measure(engine, unit)
    table = strictness.thresholds("standard")
    cutoffs = module.cutoffs(meas, table, decision)
    values = meas[cell_modules.value_key(meas)]
    xs, ys, full_ids = _positions(engine, ds)
    channels = [c.get("fullname") or c.get("name") for c in ds.project.image.real_channels]
    kind = schemas.CELL_KINDS[unit["module"].split(":", 1)[0]]
    column = meas.get("_column")
    marker = column if column in channels else None
    if marker is None:
        from plexora.agent import presets

        marker = presets.nuclear_channel(channels) or (channels[0] if channels else None)
    layout = "quadrants" if kind == "cycle_stability" else "strata"
    quadrants = {}
    if layout == "quadrants":
        first, last = (meas.get("_columns") or [None, None])[:2]
        quadrants = {"a": first if first in channels else marker,
                     "b": last if last in channels else marker}
    sides, counts, near, rows = [], {}, {}, {}
    for side in cell_modules.sides_of(unit["module"]):
        side_rows, n_beyond = _rows_for(values, cutoffs, xs, ys, full_ids, side)
        counts[side] = n_beyond
        near[side] = cell_modules.beyond_and_near(values, cutoffs, side)[1]
        if not any(r["cells"] for r in side_rows):
            continue
        if n_beyond == 0 and near[side] < NEAR_TO_SHOW:
            continue            # nothing beyond, almost nothing near: nothing to judge
        sides.append(side)
        rows[side] = side_rows
    # Segmentation QC's cells are judged on the DNA with the mask's outlines,
    # in a crop a few nuclei wide: a merge or a split is a neighbourhood.
    crop_um = None
    if kind == "cell_segmentation" and meas.get("_d_nucleus_um"):
        crop_um = SEG_CROP_NUCLEI * float(meas["_d_nucleus_um"])
    out = {"ds": ds, "unit": unit, "kind": kind, "layout": layout, "marker": marker,
           "crop_um": crop_um,
           "quadrants": quadrants, "column": column, "cutoffs": cutoffs, "sides": sides,
           "rows": rows, "beyond": counts, "near": near,
           "n_cells": int(np.isfinite(values).sum()), "decision": decision}
    views[key] = out
    return out


def _n_rows(v):
    return sum(len(v["rows"][s]) for s in v["sides"])


def _pack(views):
    """[[view, ...] per image] of a combined packet, or None when it does not
    fit in the images a packet may carry. Cycle stability (quadrants: its
    own panels) is an image of its own; strata modules share images, whole."""
    quad = [v for v in views if v["layout"] == "quadrants"]
    strata = [v for v in views if v["layout"] != "quadrants"]
    if len(quad) > 1:
        return None
    images = []
    for v in strata:
        rows = _n_rows(v)
        if images and sum(_n_rows(o) for o in images[-1]) + rows <= ROWS_PER_IMAGE:
            images[-1].append(v)
        else:
            images.append([v])
    images += [[v] for v in quad]
    return images if len(images) <= 2 else None


def next_group(engine, project):
    """(kind, units) of the next cell look: one module (its own kind) or up
    to `cell_batch` together (`cell_modules`); (None, []) when none waits.
    A module whose look would show nothing is closed here, accepted."""
    waiting = []
    for unit in engine.units_of("cells", project):
        if engine.check_user_edit(unit) or unit["state"] != "awaiting_look":
            continue
        v = view(engine, unit)
        if not v["sides"]:
            unit["shown_sides"] = []
            engine.close(unit, "decided", "nothing beyond the cutoffs and almost nothing near "
                                          "them: kept as they are")
            continue
        waiting.append((unit, v))
    if not waiting:
        return None, []
    batch = int(schemas.ENGINE["cell_batch"])
    group = []
    for unit, v in waiting:
        if len(group) >= batch:
            break
        if _pack([g[1] for g in group] + [v]) is None:
            continue
        kind = COMBINED_KIND if group else v["kind"]
        if not engine.wants(unit, kind):
            continue
        group.append((unit, v))
    if not group:
        return None, []
    if len(group) == 1:
        return group[0][1]["kind"], [group[0][0]]
    return COMBINED_KIND, [u for u, _v in group]


def _proposal(cutoffs):
    out = {"low": cutoffs["low"], "high": cutoffs["high"], "median": cutoffs["median"],
           "mad": cutoffs["mad"], "step": cutoffs.get("step")}
    if cutoffs.get("reference"):
        out["reference"] = cutoffs["reference"]
        out["n_positive"] = cutoffs.get("n_positive")
    return cell_modules.public(out)


#: What each kind of module asks of the cells shown, per side.
ASKS = {
    "cell_intensity": {
        "low": "are these debris, empty or out-of-plane nuclei (artifact) or real cells?",
        "high": "are these clumped nuclei or saturated spots (artifact), or real cells with "
                "dense chromatin (not_artifact)? This side only ever warns."},
    "cell_area": {
        "low": "are these fragments or debris (artifact) or small real cells?",
        "high": "are these several cells merged into one object (artifact) or single large "
                "cells (not_artifact)?"},
    "cycle_stability": {
        "low": "is the nucleus gone or moved in the last cycle (artifact: the cell was "
               "lost during cycling)?",
        "high": "is the last cycle's nucleus a different cell or misregistered? This side "
                "only ever warns."},
    "seg_under": {
        "high": "does each object hold two or more nuclei (artifact: a merge the mask should "
                "have split) -- or one nucleus, a dividing cell, or dense tissue where cells "
                "touch (not_artifact)?"},
    "seg_over": {
        "high": "is one nucleus cut across two or more outlines (artifact: a split) -- or "
                "are these separate nuclei, small cells like lymphocytes (not_artifact)?"},
    "seg_size": {
        "low": "are these fragments, debris or a sliver of a nucleus (artifact) or small "
               "real cells? A small cell excludes only under a preset that allows size "
               "alone.",
        "high": "are these several cells merged into one outline (artifact), or single "
                "large cells -- macrophages, tumour cells (not_artifact)? A large cell "
                "excludes only where its DNA also says two nuclei."},
    "seg_shape": {
        "low": "are these outlines drawn wrong -- ragged, leaking into the background "
               "(artifact) -- or elongated real cells (not_artifact)? Shape only ever "
               "warns."},
    "channel_outlier": {
        "high": "is the {marker} signal on these cells an artifact -- aggregate specks, a "
                "saturated blob, debris lying on the cell -- rather than the brightest real "
                "positive cells? The 'just inside' row IS the brightest real-looking "
                "positives: answer not_artifact if the cells beyond look like them. An "
                "accepted answer marks {marker} unreliable in these cells; it never removes "
                "a cell."},
}


def _asks(v):
    asks = ASKS.get(v["unit"]["module"]) or ASKS.get(v["kind"]) or {}
    marker = v.get("marker") or v.get("column") or "the marker"
    return {side: asks[side].format(marker=marker) for side in v["sides"] if side in asks}


def _module_evidence(v):
    unit, decision = v["unit"], v["decision"]
    return {"module": unit["module"], "column": v["column"], "marker": v["marker"],
            "space": v["cutoffs"].get("space"), "proposal": _proposal(v["cutoffs"]),
            "beyond": v["beyond"], "near": v["near"], "n_cells": v["n_cells"],
            "sides_shown": list(v["sides"]),
            "round": int(unit.get("rounds") or 0) + 1,
            "offsets": {s: (decision.get(s) or {}).get("offset_steps", 0)
                        for s in ("low", "high")},
            "asks": _asks(v),
            "cluster": unit.get("cluster"), "summary": unit.get("summary")}


def _render(engine, v, rows, title):
    from plexora.agent.evidence import collage

    return collage.render_collage(
        engine.call.session, v["ds"], layout=v["layout"], rows=rows, marker=v["marker"],
        fmt=engine.options["image_format"], pixel=engine.pixel_for(v["unit"]["project"]),
        title=title, crop_um=v.get("crop_um"), **v["quadrants"])


def _record(units, rendered):
    art = rendered.get("artifact")
    if art:
        for unit in units:
            unit.setdefault("artifacts", []).append(art["id"])


def _image(rendered, caption):
    size = rendered["manifest"]["size"]
    return ((rendered["image"], rendered["format"], (int(size[0]), int(size[1]))),
            {"role": "cell_collage", "caption": caption,
             "artifact_id": (rendered.get("artifact") or {}).get("id")})


def build(engine, units):
    """One module's packet: a collage per side worth drawing."""
    unit = units[0]
    v = view(engine, unit)
    images, meta = [], []
    unit["shown_sides"] = list(v["sides"])
    for side in v["sides"]:
        try:
            rendered = _render(engine, v, v["rows"][side],
                               f"{unit['module']} - {side} side: cells beyond and inside the "
                               "proposed cutoff")
        except AgentError:
            continue
        _record([unit], rendered)
        image, info = _image(rendered, f"{side} side")
        images.append(image)
        meta.append(info)
    if not images:
        engine.close(unit, "decided", "no cells could be drawn beside the cutoffs; kept as "
                                      "proposed")
        return None
    evidence = _module_evidence(v)
    evidence.update(rows=["far beyond", "just beyond", "just inside (kept)"], guide=["cells"])
    asks = "; ".join(f"{side}: {text}" for side, text in _asks(v).items())
    packet = {"question": (f"{unit['module']}: {asks} Per side shown: accept (the cells "
                           "beyond are artifacts and the cutoff is right), too_aggressive, "
                           "too_lenient, not_artifact (these extremes are biology) or "
                           "cannot_tell. Only a side you judge an artifact can exclude."),
              "evidence": evidence,
              "allowed": ["accept", "too_aggressive", "too_lenient", "not_artifact",
                          "cannot_tell"],
              "_image_meta": meta}
    return packet, images[:2]


def _short(name):
    return {"counterstain_intensity": "counterstain", "segmentation_area": "area",
            "cycle_stability": "cycle", "seg_under": "merged", "seg_over": "split",
            "seg_size": "size", "seg_shape": "shape"}.get(
        name, name.replace("channel_outlier:", "outlier "))


def build_many(engine, units):
    """Several modules in one packet: every module's rows in one strata
    collage (split over two images when long), cycle stability's quadrants
    in an image of its own; answered per module."""
    views = [view(engine, u) for u in units]
    images_plan = _pack(views)
    if images_plan is None:          # the grouping guarantees a fit; never guess
        raise AgentError("internal_error", "the cell modules of this packet do not fit "
                                           "in two images")
    images, meta = [], []
    for group in images_plan:
        rows = []
        for v in group:
            v["unit"]["shown_sides"] = list(v["sides"])
            short = _short(v["unit"]["module"])
            for side in v["sides"]:
                for row in v["rows"][side]:
                    rows.append({**row, "label": f"{short} | {row['label']}",
                                 "key": f"{v['unit']['module']}|{row['label']}",
                                 **({"marker": v["marker"]} if v["layout"] != "quadrants"
                                    and v["marker"] else {})})
        names = ", ".join(v["unit"]["module"] for v in group)
        rendered = _render(engine, group[0], rows,
                           f"{names[:90]} - cells beyond and inside each proposed cutoff")
        _record([v["unit"] for v in group], rendered)
        image, info = _image(rendered, names[:80])
        images.append(image)
        meta.append(info)
    packet = {"question": ("For every module below, answer the question in its evidence "
                           "(`modules.<name>.asks`, per side shown). Rows are labelled "
                           "`module | side: row`. Answer `modules`, one entry per module name, "
                           "each with low/high (only the sides shown count): accept (the "
                           "cells beyond are artifacts and the cutoff is right), "
                           "too_aggressive, too_lenient, not_artifact (biology) or "
                           "cannot_tell. Only a side you judge an artifact can exclude."),
              "evidence": {"modules": {v["unit"]["module"]: _module_evidence(v) for v in views},
                           "rows": ["far beyond", "just beyond", "just inside (kept)"],
                           "guide": ["cells"]},
              "allowed": ["accept", "too_aggressive", "too_lenient", "not_artifact",
                          "cannot_tell"],
              "_image_meta": meta}
    return packet, images


def _apply_one(engine, unit, answer, notes=""):
    """One module's verdict (a single packet's answer, or one entry of a
    combined packet's `modules`)."""
    if notes:
        unit.setdefault("notes", []).append(notes)
    decision = unit.setdefault("decision", {})
    every = cell_modules.sides_of(unit["module"])
    shown = unit.get("shown_sides")
    sides = [s for s in every if shown is None or s in shown]
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
    for side in every:
        if side not in sides:
            # Not drawn: nothing was beyond it to judge; the proposal stands.
            decision.setdefault(side, {"offset_steps": 0, "veto": False}).setdefault(
                "verdict", "not_shown")
    if unit["module"].startswith("channel_outlier:"):
        decision["artifact"] = answer.high in ("accept", "too_lenient", "too_aggressive")
    if answer.pattern:
        decision["pattern"] = answer.pattern
    decision["confidence"] = answer.confidence
    unit["rounds"] = int(unit.get("rounds") or 0) + 1
    if sides and unsure == len(sides):
        decision["manual_review"] = True
        engine.close(unit, "manual_review_recommended",
                     "the agent could not tell from the cells: warn only")
    elif moved and unit["rounds"] < schemas.ENGINE["cell_rounds"]:
        unit["state"] = "awaiting_look"
    else:
        engine.close(unit, "decided", "cutoffs judged on the cells beside them")
    return {"unit": unit["id"], "type": "cells", "state": unit["state"],
            "decision": {k: v for k, v in decision.items() if k in ("low", "high")}}


def _units(engine, packet):
    return [engine.record["units"][engine.unit_key_of(ref)] for ref in packet["units"]
            if engine.unit_key_of(ref) in engine.record["units"]]


def apply(engine, packet, answer):
    units = _units(engine, packet)
    if answer.kind != COMBINED_KIND:
        return _apply_one(engine, units[0], answer, answer.notes)
    by_name = {u["module"]: u for u in units}
    missing = [m for m in by_name if m not in answer.modules]
    unknown = [m for m in answer.modules if m not in by_name]
    if missing or unknown:
        raise AgentError("invalid_input", "answer every module of this packet, by name",
                         detail={"missing": missing, "unknown": unknown,
                                 "allowed": list(by_name)})
    outcomes = {}
    for name, verdict in answer.modules.items():
        unit = by_name[name]
        if unit["state"] in schemas.TERMINAL_STATES:
            continue
        outcomes[name] = _apply_one(engine, unit, verdict, answer.notes if not outcomes
                                    else "")
    return {"state": "judged", "modules": outcomes}
