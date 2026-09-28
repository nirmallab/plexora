"""From detector output to the candidates a session pursues.

Detectors overlap: a fold is bright (diffuse), blurred (focus) and saturated
at once, in several channels. So raw candidates are cleaned, merged where they
are the same place, ranked, given an id that survives reruns (a content hash,
so the memo recognises the same candidate next time) and capped -- with what
was dropped summarised for the report rather than silently lost.
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.qc.server import schemas

#: Classes that describe a whole channel or a whole grid line: never merged
#: into a local region (a local fold inside a shaded channel stays a fold).
WHOLE = ("empty_or_failed_channel", "illumination_or_shading", "stitching_or_tile_seam",
         "cross_cycle_registration_error")

#: When merged candidates are equally severe, the more specific class leads.
PRIORITY = ("empty_or_failed_channel", "saturation_or_clipping", "cycle_specific_tissue_loss",
            "antibody_aggregate", "debris_or_foreign_object", "tissue_damage_or_detachment",
            "tissue_fold", "out_of_focus", "cross_cycle_registration_error",
            "stitching_or_tile_seam", "air_bubble_or_coverslip", "slide_or_tissue_edge",
            "autofluorescence", "illumination_or_shading", "excessive_background",
            "bleedthrough_or_crosstalk", "other_technical", "uncertain_manual_review")

MERGE_IOU = 0.5
MERGE_CONTAIN = 0.8
MERGE_SIZE_RATIO = 4.0


def _iou(a, b):
    inter = np.logical_and(a, b).sum()
    if not inter:
        return 0.0, 0.0
    union = np.logical_or(a, b).sum()
    return inter / union, inter / min(a.sum(), b.sum())


def _priority(klass):
    return PRIORITY.index(klass) if klass in PRIORITY else len(PRIORITY)


#: Classes whose masks are areas (opened to drop single-cell noise); speckle
#: classes (aggregates, saturation) are kept as found.
OPENED = ("out_of_focus", "tissue_fold", "autofluorescence", "excessive_background",
          "tissue_damage_or_detachment")


def cleanup(candidate, grid_shape):
    from scipy import ndimage

    if candidate.class_hint not in OPENED:
        return candidate
    mask = candidate.mask
    if min(grid_shape) >= 40 and mask.sum() > 4:
        opened = ndimage.binary_opening(mask, structure=np.ones((2, 2), dtype=bool))
        if opened.sum():
            mask = opened
    candidate.mask = mask
    return candidate


def merge(raw):
    """Union-find over local candidates that are the same place."""
    items = [c for c in raw if c.mask.any()]
    local = [i for i, c in enumerate(items) if c.class_hint not in WHOLE]
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a_index, i in enumerate(local):
        for j in local[a_index + 1:]:
            a, b = items[i].mask, items[j].mask
            iou, contain = _iou(a, b)
            ratio = max(a.sum(), b.sum()) / max(1, min(a.sum(), b.sum()))
            if iou >= MERGE_IOU or (contain >= MERGE_CONTAIN and ratio <= MERGE_SIZE_RATIO):
                parent[find(j)] = find(i)
    groups = {}
    for index in range(len(items)):
        groups.setdefault(find(index), []).append(items[index])
    out = []
    for members in groups.values():
        if len(members) == 1:
            out.append(members[0])
            continue
        members.sort(key=lambda c: (-round(c.severity, 2), _priority(c.class_hint),
                                    c.detector))
        lead = members[0]
        # The outline is the lead's, grown only by members that are the same
        # place (IoU); a member that merely contains it adds channels, not area.
        mask = lead.mask.copy()
        for member in members[1:]:
            if _iou(member.mask, lead.mask)[0] >= MERGE_IOU:
                mask |= member.mask
        channels = list(dict.fromkeys(ch for m in members for ch in m.channels))
        scopes = {m.scope_hint for m in members}
        if "all_channels" in scopes:
            scope = "all_channels"
        elif "cycles" in scopes or len({c for m in members for c in m.cycles}) > 1:
            scope = "cycles"
        elif "cycle" in scopes:
            scope = "cycle"
        else:
            scope = "channels" if len(channels) > 1 else "channel"
        alternatives = list(dict.fromkeys(
            [m.class_hint for m in members[1:] if m.class_hint != lead.class_hint]
            + [a for m in members for a in m.alternatives if a != lead.class_hint]))
        # Several marker channels bright in the same compact spots: debris,
        # not an antibody.
        klass = lead.class_hint
        if klass == "antibody_aggregate" and len(channels) >= 3:
            klass = "debris_or_foreign_object"
            alternatives = ["antibody_aggregate", *[a for a in alternatives
                                                    if a != "debris_or_foreign_object"]]
        lead.class_hint = klass
        lead.mask = mask
        lead.channels = tuple(channels)
        lead.scope_hint = scope
        lead.cycles = tuple(sorted({c for m in members for c in m.cycles if c}))
        lead.alternatives = alternatives
        lead.severity = max(m.severity for m in members)
        lead.score = max(m.score for m in members)
        lead.merged_from = [{"detector": m.detector, "class": m.class_hint,
                             "channels": list(m.channels), "severity": m.severity}
                            for m in members]
        out.append(lead)
    return out


def rank_key(candidate):
    ys, xs = np.nonzero(candidate.mask)
    y0 = int(ys.min()) if ys.size else 0
    x0 = int(xs.min()) if xs.size else 0
    return (-candidate.severity * float(np.sqrt(max(1, candidate.mask.sum()))), y0, x0,
            candidate.class_hint, candidate.detector)


def build(raw, scan, *, project, per_channel=None, per_session=None,
          min_score=None) -> dict:
    """{ranked, residual, skipped_detectors} from raw detector candidates."""
    engine = schemas.ENGINE
    per_channel = per_channel or engine["candidates_per_channel"]
    per_session = per_session or engine["candidates_per_session"]
    min_score = engine["candidate_min_score"] if min_score is None else min_score
    shape = tuple(scan.grid["shape"])
    cleaned = [cleanup(c, shape) for c in raw]
    merged = merge(cleaned)
    merged = [c for c in merged if c.score >= min_score and c.mask.any()]
    merged.sort(key=rank_key)
    for candidate in merged:
        candidate.make_id(project)
    # One id per region: a duplicate (two detectors, identical mask and
    # channels) keeps the first.
    seen, unique = set(), []
    for candidate in merged:
        if candidate.id in seen:
            continue
        seen.add(candidate.id)
        unique.append(candidate)
    counts, ranked, residual = {}, [], []
    for candidate in unique:
        lead = candidate.channels[0] if candidate.channels else ""
        if counts.get(lead, 0) >= per_channel or len(ranked) >= per_session:
            residual.append(candidate)
            continue
        counts[lead] = counts.get(lead, 0) + 1
        ranked.append(candidate)
    return {"ranked": ranked, "residual": summarise_residual(residual, scan)}


def summarise_residual(residual, scan):
    """What was found but not pursued, per class and channel, for the report."""
    cell_um = scan.grid.get("cell_um")
    out = {}
    for candidate in residual:
        key = (candidate.class_hint, candidate.channels[0] if candidate.channels else "")
        entry = out.setdefault(key, {"class": key[0], "channel": key[1], "n": 0,
                                     "cells": 0})
        entry["n"] += 1
        entry["cells"] += int(candidate.mask.sum())
    rows = []
    for entry in out.values():
        if cell_um:
            entry["area_mm2"] = round(entry["cells"] * cell_um * cell_um / 1e6, 4)
        rows.append(entry)
    return sorted(rows, key=lambda r: (-r["n"], r["class"]))


def area_fraction(mask, scan):
    tissue = scan.shared("tissue_fraction")
    if tissue is None:
        return float(mask.mean())
    total = float(tissue.sum())
    return float((tissue * mask).sum() / total) if total > 0 else 0.0


# -- masks in a session record ------------------------------------------------------


def encode_mask(mask) -> dict:
    """A map mask as JSON: its shape and its bits, base64."""
    import base64

    mask = np.asarray(mask, dtype=bool)
    return {"shape": list(mask.shape),
            "bits": base64.b64encode(np.packbits(mask, axis=None).tobytes()).decode("ascii")}


def decode_mask(encoded) -> np.ndarray:
    import base64

    shape = tuple(encoded["shape"])
    bits = np.frombuffer(base64.b64decode(encoded["bits"]), dtype=np.uint8)
    return np.unpackbits(bits, count=int(np.prod(shape))).astype(bool).reshape(shape)


def peak_of(candidate, scan):
    """The full-resolution centre of the candidate's strongest map cell."""
    channel, _sep, metric = (candidate.primary_metric or "").partition("::")
    values = scan.map(channel, metric) if metric else scan.shared(channel)
    mask = candidate.mask
    s = scan.grid["cell_full_px"]
    if values is None or not mask.any():
        ys, xs = np.nonzero(mask)
        if not ys.size:
            return None
        return [float((xs.mean() + 0.5) * s), float((ys.mean() + 0.5) * s)]
    score = np.where(mask, np.abs(np.nan_to_num(values - np.nanmedian(values))), -np.inf)
    iy, ix = np.unravel_index(int(np.argmax(score)), score.shape)
    return [float((ix + 0.5) * s), float((iy + 0.5) * s)]
