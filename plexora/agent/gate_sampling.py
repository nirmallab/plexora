"""Choosing where to look when checking a gate.

A gate is judged by looking at the tissue, and where one looks decides what one
concludes: a field full of bright positives says nothing about whether the
threshold is right, and neither does an empty one. So fields are chosen by
what they can show -- a clearly negative neighbourhood, a clearly positive
one, and above all the borderline cells either side of the threshold, where a
gate is right or wrong -- plus the places a gate most often misbehaves: dense
tissue, sparse tissue, a bright cell on its own, the edge of the section.

Deterministic by construction. The candidates are the squares of a fixed grid
(half-field steps, so every field is grid-snapped and two runs can only ever
pick the same squares), scored from counts, ordered by score with ties broken
by position, and de-duplicated by overlap. `seed` matters only when a very
large table is subsampled for scoring, and it is recorded.

Only ids, coordinates and the one marker column are read.
"""

from __future__ import annotations

import numpy as np

from plexora.agent.errors import AgentError
from plexora.agent.limits import MAX_FIELDS, MAX_PER_CLASS, SAMPLING_CELL_CAP

CLASSES = ("clear_negative", "clear_positive", "borderline", "high_density",
           "low_density", "bright_isolated", "edge")
DEFAULT_CLASSES = ("clear_negative", "clear_positive", "borderline")

#: Half-width of the default borderline band, in percentile points of the
#: marker's distribution either side of the gate.
BAND_PERCENTILE = 2.5

#: Two fields overlapping more than this (intersection over union) are one.
MAX_IOU = 0.25

WHY = {
    "clear_negative": "cells well below the gate: what negative looks like here",
    "clear_positive": "cells well above the gate: what positive looks like here",
    "borderline": "the most cells near the threshold, where the gate is decided",
    "high_density": "the densest tissue, where outlines and spill-over mislead most",
    "low_density": "sparse tissue, where background is easiest to judge",
    "bright_isolated": "the brightest cell with few neighbours: real signal or artefact",
    "edge": "the edge of the section, where staining and segmentation often fail",
}


def _band(values, low, band):
    """(band_low, band_high, how) around the gate."""
    finite = values[np.isfinite(values)]
    if band is not None:
        return float(band[0]), float(band[1]), "explicit"
    at = float((finite <= low).mean()) * 100 if finite.size else 50.0
    lo_p = max(0.0, at - BAND_PERCENTILE)
    hi_p = min(100.0, at + BAND_PERCENTILE)
    band_low, band_high = np.percentile(finite, [lo_p, hi_p])
    if not band_low < band_high:
        span = max(abs(low) * 0.05, 1e-6)
        band_low, band_high = low - span, low + span
    return float(band_low), float(band_high), f"±{BAND_PERCENTILE}% percentile around the gate"


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def field_stats(xs, ys, values, ids, box, low, high, band_low, band_high):
    inside = (xs >= box[0]) & (xs < box[2]) & (ys >= box[1]) & (ys < box[3])
    v = values[inside]
    finite = np.isfinite(v)
    positive = finite & (v > low) & (v < high)
    near = finite & (v >= band_low) & (v <= band_high)
    return inside, {
        "cells": int(inside.sum()),
        "positives": int(positive.sum()),
        "positive_fraction": float(positive.sum() / inside.sum()) if inside.any() else None,
        "borderline_below": int((near & ~positive).sum()),
        "borderline_above": int((near & positive).sum()),
    }


def sample_gate_validation_regions(data, marker, low, high, *, field_px, image_size,
                                   n_per_class=2, classes=DEFAULT_CLASSES, seed=0,
                                   band=None, max_fields=MAX_FIELDS):
    """Fields to check a gate in: `{fields: [...], band, grid, ...}`.

    `data` is a provider-backed handle set; `field_px` the side of a square
    field in full-resolution pixels; `image_size` (width, height).
    """
    from plexora.server.utils.label_overlay import cell_ids

    unknown = [c for c in classes if c not in CLASSES]
    if unknown:
        raise AgentError("invalid_input", f"unknown field classes {unknown}",
                         detail={"classes": list(CLASSES)})
    n_per_class = max(1, min(int(n_per_class), MAX_PER_CLASS))
    max_fields = max(1, min(int(max_fields), MAX_FIELDS))
    schema = data.schema
    frame = data.table.geometry()
    if frame is None or schema is None or schema.x not in frame.columns \
            or schema.y not in frame.columns:
        raise AgentError("precondition_missing", "sampling fields needs cell coordinates",
                         detail={"missing": [{"key": "role:x", "label": "X column"},
                                             {"key": "role:y", "label": "Y column"}]})
    ids_all, keep = cell_ids(frame, schema.cell_id)
    xs = frame[schema.x].to_numpy().astype(np.float64)[keep]
    ys = frame[schema.y].to_numpy().astype(np.float64)[keep]
    values = np.asarray(data.table.columns([marker])[marker], dtype=np.float64)[keep]
    ids = ids_all
    ok = np.isfinite(xs) & np.isfinite(ys)
    xs, ys, values, ids = xs[ok], ys[ok], values[ok], ids[ok]
    if not len(xs):
        raise AgentError("precondition_missing", "no cells with coordinates to sample")

    band_low, band_high, band_how = _band(values, low, band)
    width, height = image_size
    side = float(max(8.0, min(field_px, width, height)))
    step = side / 2.0

    # Score on a subsample of a huge table; the stats below use every cell.
    scored = np.arange(len(xs))
    subsampled = False
    if len(xs) > SAMPLING_CELL_CAP:
        rng = np.random.default_rng(seed)
        scored = np.sort(rng.choice(len(xs), SAMPLING_CELL_CAP, replace=False))
        subsampled = True
    sx, sy, sv = xs[scored], ys[scored], values[scored]
    nx = max(1, int(np.ceil(width / step)))
    ny = max(1, int(np.ceil(height / step)))
    bx = np.clip((sx // step).astype(int), 0, nx - 1)
    by = np.clip((sy // step).astype(int), 0, ny - 1)

    finite = np.isfinite(sv)
    pos = finite & (sv > low) & (sv < high)
    near = finite & (sv >= band_low) & (sv <= band_high)
    far_neg = finite & (sv < band_low)
    far_pos = pos & (sv > band_high)

    def grid(weights):
        out = np.zeros((ny, nx), dtype=np.float64)
        np.add.at(out, (by, bx), weights)
        return out

    counts = grid(np.ones_like(sx))
    g_pos, g_near = grid(pos), grid(near)
    g_farneg, g_farpos = grid(far_neg), grid(far_pos)
    brightest = np.full((ny, nx), -np.inf)
    np.maximum.at(brightest, (by, bx), np.where(finite, sv, -np.inf))

    def window(g):
        """Sums over 2x2 blocks of bins: one per candidate field."""
        padded = np.pad(g, ((0, 1), (0, 1)))
        return padded[:-1, :-1] + padded[1:, :-1] + padded[:-1, 1:] + padded[1:, 1:]

    w_count, w_pos, w_near = window(counts), window(g_pos), window(g_near)
    w_farneg, w_farpos = window(g_farneg), window(g_farpos)
    padded_b = np.pad(brightest, ((0, 1), (0, 1)), constant_values=-np.inf)
    w_bright = np.maximum.reduce([padded_b[:-1, :-1], padded_b[1:, :-1],
                                  padded_b[:-1, 1:], padded_b[1:, 1:]])
    # Only windows wholly inside the image. A window hanging off the right or
    # bottom edge would have to be shifted back in to be drawn, and then it
    # would no longer be the window that was scored.
    inside_x = np.arange(nx) * step + side <= width + 1e-6
    inside_y = np.arange(ny) * step + side <= height + 1e-6
    occupied = (w_count > 0) & inside_y[:, None] & inside_x[None, :]
    density_rank = np.zeros_like(w_count)
    if occupied.any():
        order = np.argsort(w_count[occupied], kind="stable")
        ranks = np.empty(order.size)
        ranks[order] = np.arange(order.size) / max(1, order.size - 1)
        density_rank[occupied] = ranks

    def box_of(iy, ix):
        x0 = min(ix * step, max(0.0, width - side))
        y0 = min(iy * step, max(0.0, height - side))
        return (float(x0), float(y0), float(x0 + side), float(y0 + side))

    scores = {
        "clear_negative": np.where(w_pos == 0, w_farneg, -1) - 0.5 * w_near,
        "clear_positive": w_farpos - 0.5 * w_near - 0.25 * (w_count - w_pos),
        "borderline": w_near,
        "high_density": w_count,
        "low_density": np.where(w_count >= 2, -w_count, -np.inf),
        "bright_isolated": np.where(occupied, w_bright - 1e-3 * w_count * np.nanmax(
            np.abs(sv[finite])) if finite.any() else 0, -np.inf),
    }
    edge = np.zeros_like(w_count, dtype=bool)
    edge[0, :] = edge[:, 0] = True
    last_y = int(np.flatnonzero(inside_y)[-1]) if inside_y.any() else 0
    last_x = int(np.flatnonzero(inside_x)[-1]) if inside_x.any() else 0
    edge[last_y:, :] = True
    edge[:, last_x:] = True
    scores["edge"] = np.where(edge & occupied, w_count, -np.inf)

    chosen = []
    for cls in classes:
        score = scores[cls]
        candidates = [(float(score[iy, ix]), iy, ix)
                      for iy in range(ny) for ix in range(nx)
                      if occupied[iy, ix] and np.isfinite(score[iy, ix]) and score[iy, ix] > 0
                      or (cls == "low_density" and occupied[iy, ix]
                          and np.isfinite(score[iy, ix]))]
        # Highest score first; ties by position, so the order is total.
        candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
        taken = 0
        for value, iy, ix in candidates:
            if taken >= n_per_class or len(chosen) >= max_fields:
                break
            box = box_of(iy, ix)
            if any(_iou(box, other["box"]) > MAX_IOU for other in chosen):
                continue
            chosen.append({"class": cls, "box": box, "score": value,
                           "density_percentile": float(density_rank[iy, ix] * 100)})
            taken += 1

    fields = []
    for index, field in enumerate(chosen):
        x0, y0, x1, y1 = field["box"]
        _, stats = field_stats(xs, ys, values, ids, field["box"], low, high,
                               band_low, band_high)
        fields.append({
            "field_id": f"f{index + 1}",
            "class": field["class"],
            "why": WHY[field["class"]],
            "bounds": {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0},
            "center": [(x0 + x1) / 2, (y0 + y1) / 2],
            **stats,
            "density_percentile": round(field["density_percentile"], 1),
        })
    missing = [cls for cls in classes if not any(f["class"] == cls for f in fields)]
    finite_all = np.isfinite(values)
    positive_all = finite_all & (values > low) & (values < high)
    return {
        "marker": marker,
        "gate": {"low": float(low), "high": float(high)},
        "band": {"low": band_low, "high": band_high, "how": band_how},
        "field_px": side,
        "grid_step_px": step,
        "classes": list(classes),
        "classes_without_field": missing,
        "fields": fields,
        "dataset": {"cells": int(len(values)), "positives": int(positive_all.sum()),
                    "positive_fraction": float(positive_all.sum() / max(1, finite_all.sum()))},
        "seed": int(seed),
        "subsampled_for_scoring": subsampled,
    }
