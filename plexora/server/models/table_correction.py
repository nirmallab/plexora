"""A cell table read the way its mask and image need it, without touching the file.

`models/consistency.py` says when a table and its mask or image disagree. Two of
those disagreements are a property of how the table was WRITTEN rather than of
which sample it is, and both are common enough to fix in place:

- **cell ids one off the mask's labels** -- a pipeline wrote the row's position
  (0, 1, 2 ...) where the mask's label (1, 2, 3 ...) belongs;
- **coordinates in other units** -- microns, or the pixels of a different
  resolution, where the image's own pixels belong.

A correction is a small record on the project's table spec (`DataSpec.
correction`) and is applied every time the table is READ, on whichever machine
reads it (`apply`, called by the table providers): the file is never written.
That is the whole contract, and the reason it is safe to offer behind one
button -- undoing it is deleting the record.

**What it changes.** The x/y (and z) role columns are multiplied in place. The
cell id is NOT rewritten in place: a flat table's cell id is often the
positional `id` column the adapter manufactures, and rows are looked up by it.
The corrected id goes into a column of its own (`mask_label`) and the cell-id
role points there, so everything that addresses the mask by the role reads the
corrected value and everything that addresses a row by `id` is undisturbed.

**How the correction is found** (`propose`). Not guessed from the numbers'
ranges alone: a table in microns at 0.07 um/px and one at 0.14 um/px are both
"in the corner" of the image, and only one of them is right. Candidate
corrections are tried against the mask itself -- a sample of cells, each looked
up at the position the candidate puts it -- and the one under which the most
cells sit on their own label wins. Without a mask there is nothing to check
against; then only the coordinates are corrected, from the image's size and its
stated pixel size, and the record says it was estimated.
"""

from __future__ import annotations

import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import numpy as np

#: The findings a correction can answer (consistency.py's codes).
FIXABLE = ("ids_below_one", "cells_in_a_corner", "cells_outside_image")
#: The finding that says a correction is in force.
APPLIED_CODE = "table_corrected"
#: Where the corrected cell id is written, unless the table has a column of
#: that name already.
ID_COLUMN = "mask_label"
#: Cells looked up per candidate: a first pass over SCREEN of them for every
#: candidate, then all SAMPLE for the best two.
SAMPLE = 64
SCREEN = 16
#: The share of looked-up cells that must sit on their own label for a
#: correction to be applied.
MIN_MATCH = 0.5
#: Half-width of the window a cell is looked up in, in pixels (and planes): a
#: centroid on the edge of its cell, or one pixel out from rounding, still
#: counts as on it.
WINDOW = 1
#: Coordinates may reach this far past the image edge and still be inside it.
EDGE_TOLERANCE = 1.005
#: A candidate under which the farthest cell reaches less than this share of
#: the image (or the stack) is not tried: it is the corner the table is already
#: in, made smaller, and on a node every candidate costs a request per cell.
MIN_REACH = 0.2
#: Multiples of the stated pixel size tried as the table's unit -- a table
#: measured at half or twice the image's resolution is the usual way the stated
#: size is the wrong one.
PIXEL_SIZE_MULTIPLES = (1.0, 0.5, 2.0, 0.25, 4.0)
#: Downsampling factors tried as "the table is in the pixels of another level".
LEVEL_FACTORS = (2.0, 4.0, 8.0, 16.0, 0.5, 0.25)
#: The unit of the one candidate that is not a named unit: the scale at which
#: the cells exactly fill the image.
_FILL = "estimated from the image's size"


class CorrectionError(ValueError):
    """The recorded correction cannot be applied to this table as read."""


# -- the record ------------------------------------------------------------


def is_active(correction: Mapping[str, Any] | None) -> bool:
    """Whether `correction` changes anything a reader would see."""
    if not correction:
        return False
    return bool(_id_part(correction)) or bool(_scales(correction))


def _id_part(correction) -> dict | None:
    part = (correction or {}).get("cell_id") or None
    if not part or not int(part.get("offset") or 0) or not part.get("from"):
        return None
    return dict(part)


def _scales(correction) -> dict:
    scale = (correction or {}).get("scale") or {}
    return {axis: float(value) for axis, value in scale.items()
            if axis in ("x", "y", "z") and value and float(value) != 1.0}


def signature(correction: Mapping[str, Any] | None) -> str | None:
    """A short stable token for what `correction` does to the values, for the
    keys of caches built from the corrected table. None when it does nothing,
    so a project without one keeps the keys it always had."""
    if not is_active(correction):
        return None
    part = _id_part(correction)
    body = {"cell_id": [part["from"], int(part["offset"]), part.get("column") or ID_COLUMN]
            if part else None,
            "scale": {axis: round(value, 9) for axis, value in sorted(_scales(correction).items())}}
    return hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def raw_cell_id_column(roles, correction) -> str | None:
    """The column the table's own cell ids are in, before any correction."""
    part = _id_part(correction)
    if part and roles.cell_id == (part.get("column") or ID_COLUMN):
        return part["from"]
    return roles.cell_id


# -- applying it -----------------------------------------------------------


def apply(normalized, spec):
    """`normalized` (an adapter's NormalizedDatasource) as `spec`'s correction
    says to read it. The same object when there is nothing to do."""
    correction = getattr(spec, "correction", None)
    if not is_active(correction):
        return normalized
    columns = {"x": normalized.x_column or spec.roles.x,
               "y": normalized.y_column or spec.roles.y,
               "z": spec.roles.z}
    table = apply_frame(normalized.table, correction, columns)
    return replace(normalized, table=table)


def apply_frame(table, correction, columns: Mapping[str, str | None]):
    """`table` (polars) with `correction` applied; `columns` names the x/y/z
    columns to scale."""
    import polars as pl

    exprs = []
    for axis, factor in _scales(correction).items():
        name = columns.get(axis)
        if name and name in table.columns:
            exprs.append((pl.col(name).cast(pl.Float64) * factor).alias(name))
    part = _id_part(correction)
    if part:
        source, target = part["from"], part.get("column") or ID_COLUMN
        if source not in table.columns:
            raise CorrectionError(
                f"this project's table correction reads cell ids from {source!r}, "
                "which the table no longer has; undo the correction and fix it again")
        exprs.append((pl.col(source).cast(pl.Float64).round(0).cast(pl.Int64)
                      + int(part["offset"])).alias(target))
    return table.with_columns(exprs) if exprs else table


def corrected_project(project, correction: Mapping[str, Any]):
    """`project` with `correction` recorded on its table, any earlier one
    replaced, and the cell-id role pointed at the corrected ids."""
    spec = project.dataset
    roles = _uncorrected_roles(spec.roles, spec.correction)
    part = _id_part(correction)
    if part:
        roles = replace(roles, cell_id=part.get("column") or ID_COLUMN)
    return project.patch(dataset=replace(spec, roles=roles, correction=dict(correction)))


def uncorrected_project(project):
    """`project` read exactly as its file says again."""
    spec = project.dataset
    if not spec or not spec.correction:
        return project
    return project.patch(dataset=replace(
        spec, roles=_uncorrected_roles(spec.roles, spec.correction), correction={}))


def _uncorrected_roles(roles, correction):
    """The cell-id role back on the file's own column -- only if it still
    points at ours: somebody who has since chosen another column keeps it."""
    part = _id_part(correction)
    if part and roles.cell_id == (part.get("column") or ID_COLUMN):
        return replace(roles, cell_id=part["from"])
    return roles


# -- saying it ---------------------------------------------------------------


def describe(correction: Mapping[str, Any]) -> str:
    """What `correction` does, as the clauses of one sentence."""
    clauses = []
    part = _id_part(correction)
    if part:
        offset = int(part["offset"])
        clauses.append(f"cell ids {'+' if offset > 0 else '−'}{abs(offset)} to match the mask's "
                       "labels")
    scales = _scales(correction)
    xy = scales.get("x")
    if xy:
        unit = (correction.get("units") or {}).get("xy")
        clauses.append(f"x/y ×{_number(xy)}" + (f" ({unit})" if unit else ""))
    if scales.get("z"):
        unit = (correction.get("units") or {}).get("z")
        clauses.append(f"z ×{_number(scales['z'])}" + (f" ({unit})" if unit else ""))
    return "; ".join(clauses)


def _number(value: float) -> str:
    text = f"{value:.4g}"
    return text


def applied_finding(correction: Mapping[str, Any]) -> dict:
    """The note that a correction is in force, with what checked it."""
    checked = correction.get("checked") or {}
    evidence = ""
    if checked.get("cells"):
        evidence = (f" {checked['matched']:,} of {checked['cells']:,} sampled cells sit on "
                    "their own mask label.")
    elif correction.get("estimated"):
        evidence = " Estimated from the image's size; there is no mask to check it against."
    return {
        "code": APPLIED_CODE,
        "level": "info",
        "message": (f"Corrected in Plexora: {describe(correction)}.{evidence} "
                    "Your table file is unchanged."),
        "undo": True,
    }


# -- finding one -------------------------------------------------------------


def propose(frame, roles, *, width: int, height: int, depth: int = 1,
            pixel_size: float | None = None, z_step: float | None = None,
            read_window: Callable[[int, int], np.ndarray] | None = None,
            current: Mapping[str, Any] | None = None, reasons=(), workers: int = 1,
            sample: int = SAMPLE, seed: int = 0) -> dict:
    """The correction that puts this table's cells on its mask.

    `frame` is the table as currently read (polars), `roles` its column roles
    and `current` the correction in force, if any -- candidates are always
    built from the file's own values. `read_window(x, y, plane)` returns the
    mask's labels in a (2*WINDOW+1)-pixel square around pixel (x, y) -- for a
    label volume, on plane `plane`, or on a few planes through the stack when
    `plane` is None (a table with no z); a 2-D mask ignores `plane`. None when
    there is no mask.
    `pixel_size`/`z_step` are the image's stated microns per pixel and plane.

    Returns `{"correction": dict | None, "message": str}`. A None correction
    with a message is an answer too: the table already lines up, or nothing
    tried makes it.
    """
    ids, x, y, z = _raw_values(frame, roles, current)
    if x is None or y is None:
        return {"correction": None,
                "message": "The table has no x/y columns to place its cells with."}
    has_z = z is not None and depth > 1
    xy_scales = _xy_candidates(x, y, width, height, pixel_size)
    z_scales = _z_candidates(z, depth, z_step) if has_z else [(1.0, None)]
    id_source = raw_cell_id_column(roles, current)

    if read_window is None:
        return _estimate(xy_scales, z_scales, x, y, width, height, reasons)
    if ids is None:
        return {"correction": None,
                "message": "The table has no numeric cell id column to match against the mask."}

    finite = np.isfinite(ids) & np.isfinite(x) & np.isfinite(y)
    if has_z:
        finite &= np.isfinite(z)
    rows = np.flatnonzero(finite)
    if rows.size == 0:
        return {"correction": None, "message": "No cell in the table has a position."}
    rng = np.random.default_rng(seed)
    rows = rng.permutation(rows)[:sample]
    offsets = _offset_candidates(ids[np.isfinite(ids)])

    windows: dict[tuple, np.ndarray | None] = {}

    def fetch(points):
        wanted = [p for p in dict.fromkeys(points) if p not in windows]
        if not wanted:
            return
        if workers > 1 and len(wanted) > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                found = list(pool.map(lambda p: _safe_read(read_window, *p), wanted))
        else:
            found = [_safe_read(read_window, *p) for p in wanted]
        windows.update(zip(wanted, found))

    def probe(r, s, sz):
        """Where row `r` lands under scales `s`/`sz`: (x, y, plane) or None.
        One plane per look-up, not a column: a label volume is stored a plane
        to a chunk, so a column through 200 planes is 200 chunks read for one
        cell, and the plane the table's z names is the one that answers."""
        point = _pixel(x[r] * s, y[r] * s, width, height)
        if point is None:
            return None
        if not has_z:
            return point + (None,)
        plane = int(math.floor(z[r] * sz))
        return point + (plane,) if 0 <= plane < depth else None

    def score(s, sz, chosen):
        """{offset: matched} for scale `s` (and z scale `sz`) over `chosen` rows."""
        points = [probe(r, s, sz) for r in chosen]
        fetch([p for p in points if p is not None])
        out = {o: 0 for o in offsets}
        for r, point in zip(chosen, points):
            window = windows.get(point) if point is not None else None
            if window is None:
                continue
            present = set(np.unique(window).tolist())
            for o in offsets:
                if int(round(ids[r])) + o in present:
                    out[o] += 1
        return out

    combos = [(s, sz) for s, _ in xy_scales for sz, _ in z_scales]
    screen = rows[:min(SCREEN, rows.size)]
    # Every look-up the screen needs, in one batch: over a tunnel to a node it
    # is the round trips that cost, and a batch per candidate is forty of them.
    fetch([point for s, sz in combos for r in screen
           if (point := probe(r, s, sz)) is not None])
    screened = []
    for order, (s, sz) in enumerate(combos):
        hits = score(s, sz, screen)
        best_o = max(offsets, key=lambda o: (hits[o], o == 0))
        screened.append((hits[best_o], -order, s, sz))
    screened.sort(reverse=True)
    finalists = screened[:2] + [entry for entry in screened
                                 if entry[2] == 1.0 and entry[3] in (1.0, None)][:1]
    fetch([point for _, _, s, sz in finalists for r in rows
           if (point := probe(r, s, sz)) is not None])
    results = []
    for _, neg_order, s, sz in finalists:
        hits = score(s, sz, rows)
        for o in offsets:
            identity = s == 1.0 and sz in (1.0, None) and o == 0
            results.append((hits[o], identity, neg_order, s, sz, o))
    results.sort(key=lambda entry: (entry[0], entry[1], entry[2]), reverse=True)
    matched, identity, _, s, sz, o = results[0]
    baseline = max((entry[0] for entry in results if entry[1]), default=0)
    cells = int(rows.size)

    lines_up = matched >= MIN_MATCH * cells
    if identity and lines_up:
        return {"correction": None,
                "message": (f"The table already lines up with the mask: {matched:,} of "
                            f"{cells:,} sampled cells sit on their own label, and no "
                            "correction tried does better.")}
    if identity or not lines_up or matched <= baseline:
        return {"correction": None,
                "message": (f"No correction tried puts the cells on their mask labels "
                            f"(the best matched {matched:,} of {cells:,} sampled cells). "
                            "The table may belong to a different mask or image.")}

    correction = _record(s, sz, o, id_source, frame, xy_scales, z_scales, reasons)
    correction["checked"] = {"cells": cells, "matched": int(matched), "before": int(baseline)}
    return {"correction": correction,
            "message": (f"Corrected in Plexora: {describe(correction)}. {matched:,} of "
                        f"{cells:,} sampled cells now sit on their own mask label "
                        f"({baseline:,} did before). Your table file is unchanged.")}


def _estimate(xy_scales, z_scales, x, y, width, height, reasons):
    """No mask: the coordinates only, from the image's size."""
    if not set(reasons) & {"cells_in_a_corner", "cells_outside_image"}:
        return {"correction": None,
                "message": "There is no mask to check a correction against."}
    fitting = [(s, unit) for s, unit in xy_scales if s != 1.0]
    if not fitting:
        return {"correction": None,
                "message": "No scale puts the cells inside the image."}
    # A named unit (the stated pixel size, a pyramid level) over the bare
    # "fills the image" estimate, when one comes close to filling it: cells
    # rarely reach the very edge, so the named one is the likelier truth.
    fill = max(s for s, _ in fitting)
    named = [(s, unit) for s, unit in fitting if unit != _FILL]
    s, unit = (max(named, key=lambda entry: entry[0])
               if named and max(s for s, _ in named) >= 0.9 * fill
               else max(fitting, key=lambda entry: entry[0]))
    sz, z_unit = next(((value, label) for value, label in z_scales[1:]
                       if label and "says" not in label), (1.0, None))
    correction = _record(s, sz, 0, None, None, [(s, unit)], [(sz, z_unit)], reasons)
    correction["estimated"] = True
    return {"correction": correction,
            "message": (f"Corrected in Plexora: {describe(correction)}. Estimated from the "
                        "image's size; there is no mask to check it against. Your table "
                        "file is unchanged.")}


def _record(s, sz, o, id_source, frame, xy_scales, z_scales, reasons):
    correction: dict[str, Any] = {"version": 1}
    if o:
        column = ID_COLUMN
        if frame is not None and column in frame.columns:
            column = f"{ID_COLUMN}_plexora"
        correction["cell_id"] = {"from": id_source, "offset": int(o), "column": column}
    scale, units = {}, {}
    if s != 1.0:
        scale["x"] = scale["y"] = float(s)
        unit = dict(xy_scales).get(s)
        if unit:
            units["xy"] = unit
    if sz not in (None, 1.0):
        scale["z"] = float(sz)
        unit = dict(z_scales).get(sz)
        if unit:
            units["z"] = unit
    if scale:
        correction["scale"] = scale
    if units:
        correction["units"] = units
    correction["reasons"] = [code for code in reasons if code in FIXABLE]
    correction["created"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return correction


def _raw_values(frame, roles, current):
    """(ids, x, y, z) as numpy float arrays, as the FILE has them."""
    import polars as pl

    scales = _scales(current)

    def column(name, factor=1.0):
        if not name or name not in frame.columns:
            return None
        try:
            values = frame[name].cast(pl.Float64).to_numpy().astype(np.float64)
        except Exception:
            return None
        return values / factor

    id_column = raw_cell_id_column(roles, current)
    return (column(id_column), column(roles.x, scales.get("x", 1.0)),
            column(roles.y, scales.get("y", 1.0)), column(roles.z, scales.get("z", 1.0)))


def _xy_candidates(x, y, width, height, pixel_size):
    """[(scale, unit sentence)], identity first: the order breaks ties."""
    out = [(1.0, None)]
    reach = max(np.nanmax(x) / max(width, 1), np.nanmax(y) / max(height, 1))
    if not np.isfinite(reach) or reach <= 0:
        return out
    bound = EDGE_TOLERANCE / reach
    if pixel_size:
        for k in PIXEL_SIZE_MULTIPLES:
            um = pixel_size * k
            out.append((1.0 / um, f"microns at {um:.4g} µm per pixel"
                        + ("" if k == 1.0 else f"; the image says {pixel_size:.4g}")))
    for factor in LEVEL_FACTORS:
        out.append((factor, f"pixels of a {factor:g}× coarser level" if factor > 1
                    else f"pixels of a {1 / factor:g}× finer level"))
    out.append((bound / EDGE_TOLERANCE, _FILL))
    return _dedupe([(s, unit) for s, unit in out
                    if s == 1.0 or (s <= bound and s * reach >= MIN_REACH)])


def _z_candidates(z, depth, z_step):
    out = [(1.0, None)]
    reach = np.nanmax(z) if z is not None and np.isfinite(z).any() else 0
    if reach <= 0:
        return out
    bound = depth * 1.02 / reach
    if z_step:
        for k in PIXEL_SIZE_MULTIPLES:
            um = z_step * k
            out.append((1.0 / um, f"microns at {um:.4g} µm per plane"
                        + ("" if k == 1.0 else f"; the image says {z_step:.4g}")))
    return _dedupe([(s, unit) for s, unit in out
                    if s == 1.0 or (s <= bound and s * reach >= MIN_REACH * depth)])


def _dedupe(candidates):
    kept = []
    for s, unit in candidates:
        if all(abs(s - other) > 0.005 * other for other, _ in kept):
            kept.append((s, unit))
    return kept


def _offset_candidates(ids):
    lowest = int(np.nanmin(ids)) if ids.size else 0
    offsets = {0, 1, -1, 1 - lowest}
    return sorted(o for o in offsets if lowest + o >= 0)


def _pixel(px, py, width, height):
    if not (np.isfinite(px) and np.isfinite(py)):
        return None
    col, row = int(math.floor(px)), int(math.floor(py))
    if not (0 <= col < width and 0 <= row < height):
        return None
    return col, row


def _safe_read(read_window, col, row, plane=None):
    try:
        window = read_window(col, row, plane)
    except Exception:
        return None
    return None if window is None else np.asarray(window)


# -- reading the mask --------------------------------------------------------


def mask_window_reader(project, providers=None):
    """`(read_window, workers)` for `project`'s mask, or `(None, 1)` without one.

    A label volume is read on the one plane asked for -- a cell is many planes
    deep, and on disk a plane is a chunk -- or, for a table with no z, on a few
    planes spread through the stack; a 2-D mask as a small square. A mask on a data node is read over the wire, so its
    reads go eight at a time.
    """
    from plexora.server.volume import labels as volume_labels

    if not project.segmentation.available:
        return None, 1
    on_node = project.resource("segmentation") is not None
    workers = 8 if on_node else 1
    volume = volume_labels.project_label_volume(project)
    if volume is not None:
        depth, height, width = volume.depth, volume.height, volume.width
        spread = sorted({depth // 4, depth // 2, (3 * depth) // 4})

        def read_planes(col, row, plane=None):
            rows = slice(max(0, row - WINDOW), min(height, row + WINDOW + 1))
            cols = slice(max(0, col - WINDOW), min(width, col + WINDOW + 1))
            if plane is None:
                return np.stack([np.asarray(volume.read(0, z, z, rows, cols))[0]
                                 for z in spread])
            plane = max(0, min(depth - 1, int(plane)))
            return np.asarray(volume.read(0, plane, plane, rows, cols))

        return read_planes, workers
    segmentation = getattr(providers, "segmentation", None)
    if segmentation is None:
        return None, 1

    def read_square(col, row, plane=None):
        box = (col - WINDOW, row - WINDOW, col + WINDOW + 1, row + WINDOW + 1)
        return np.asarray(segmentation.read_region(0, box))

    return read_square, workers
