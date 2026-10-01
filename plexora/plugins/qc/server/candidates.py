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
#: into a local region (a local fold inside a shaded channel stays a fold),
#: but merged with the same class at the same place -- one seam seen in every
#: channel is one candidate carrying the channel list, and the scope question
#: settles which channels it affects, instead of a look per channel.
WHOLE = ("empty_or_failed_channel", "illumination_or_shading", "stitching_or_tile_seam",
         "cross_cycle_registration_error")
#: ...except a failed channel: its mask is the whole tissue in every channel,
#: so "the same place" says nothing, and a failed stain is a channel's own.
NEVER_MERGED = ("empty_or_failed_channel",)

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


def _box(mask):
    """(y0, y1, x0, x1), half-open, of a mask's true cells."""
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    return int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1


def _iou_within(a, b, box_a, box_b, size_a, size_b):
    """`_iou` of two masks, reading only where their boxes meet (the only
    place they can overlap)."""
    y0, y1 = max(box_a[0], box_b[0]), min(box_a[1], box_b[1])
    x0, x1 = max(box_a[2], box_b[2]), min(box_a[3], box_b[3])
    inter = int(np.logical_and(a[y0:y1, x0:x1], b[y0:y1, x0:x1]).sum())
    if not inter:
        return 0.0, 0.0
    return inter / (size_a + size_b - inter), inter / min(size_a, size_b)


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


def compatible(a, b) -> bool:
    """Whether two candidates may be one region: two local classes (a fold is
    bright, blurred and saturated at once), or the same whole-channel /
    grid-line class -- never a local one with a whole one."""
    if a.class_hint in NEVER_MERGED or b.class_hint in NEVER_MERGED:
        return False
    if a.class_hint in WHOLE or b.class_hint in WHOLE:
        return a.class_hint == b.class_hint
    return True


def merge(raw):
    """Union-find over candidates that are the same place, across channels
    and detectors, before any is shown (`compatible` says which may merge)."""
    items = [c for c in raw if c.mask.any()]
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # Only masks whose boxes meet can overlap: tested all at once per mask,
    # and the overlap measured inside the shared box. A 40-channel image's
    # aggregate detector alone raises thousands of specks, and every pair
    # over the whole grid was hours of numpy (the first live run hung here).
    boxes = np.array([_box(c.mask) for c in items], dtype=np.int64).reshape(-1, 4)
    sizes = [int(c.mask.sum()) for c in items]
    for i in range(len(items)):
        y0, y1, x0, x1 = boxes[i]
        rest = boxes[i + 1:]
        meets = np.flatnonzero((rest[:, 0] < y1) & (rest[:, 1] > y0)
                               & (rest[:, 2] < x1) & (rest[:, 3] > x0)) + i + 1
        for j in meets.tolist():
            if not compatible(items[i], items[j]):
                continue
            iou, contain = _iou_within(items[i].mask, items[j].mask, boxes[i], boxes[j],
                                       sizes[i], sizes[j])
            ratio = max(sizes[i], sizes[j]) / max(1, min(sizes[i], sizes[j]))
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
        # Strengths are comparable only within one detector: the lead's own.
        same = [m.strength for m in members
                if m.detector == lead.detector and m.strength is not None]
        lead.strength = max(same) if same else lead.strength
        lead.merged_from = [{"detector": m.detector, "class": m.class_hint,
                             "channels": list(m.channels), "severity": m.severity}
                            for m in members]
        out.append(lead)
    return out


def audit_channels(unit) -> list:
    """The channels whose audit rows show a candidate unit (every channel of a
    merged one; older records name only their lead)."""
    return list(unit.get("audit_channels") or [unit.get("audit_channel")])


def rank_key(candidate):
    ys, xs = np.nonzero(candidate.mask)
    y0 = int(ys.min()) if ys.size else 0
    x0 = int(xs.min()) if xs.size else 0
    return (-candidate.severity * float(np.sqrt(max(1, candidate.mask.sum()))), y0, x0,
            candidate.class_hint, candidate.detector)


def strength_of(candidate) -> float:
    """The candidate's unbounded strength, or its score when its detector
    gives none."""
    return float(candidate.score if candidate.strength is None else candidate.strength)


def cap_raw(raw, per_channel=None):
    """(kept, capped): at most `raw_per_channel` raw candidates per detector
    and channel, the strongest by `strength_of` (then the larger) -- before
    the merge, so a speck-rich channel neither slows it nor crowds the rest."""
    per_channel = per_channel or schemas.ENGINE["raw_per_channel"]
    groups = {}
    for index, candidate in enumerate(raw):
        lead = candidate.channels[0] if len(candidate.channels) == 1 else ""
        groups.setdefault((candidate.detector, lead), []).append(index)
    keep = set()
    for (_detector, lead), members in groups.items():
        if not lead or len(members) <= per_channel:
            keep.update(members)
            continue
        members.sort(key=lambda i: (-strength_of(raw[i]), -int(raw[i].mask.sum()), i))
        keep.update(members[:per_channel])
    return ([c for i, c in enumerate(raw) if i in keep],
            [c for i, c in enumerate(raw) if i not in keep])


def _bright_share(scan, channel, mask):
    """The share of `mask`'s cells where `channel` is bright -- compact
    specks as the aggregate detector measures them, or a diffuse patch as
    the diffuse detector does."""
    from plexora.plugins.qc.server.detectors.classical import DETECT
    from plexora.plugins.qc.server.scan import robust_z

    hit = np.zeros(mask.shape, dtype=bool)
    compact = scan.map(channel, "bright_compact")
    if compact is not None:
        compact = np.nan_to_num(compact)
        z = robust_z(compact, scan.tissue(), floor=DETECT["compact_fraction"])
        hit |= (compact >= DETECT["compact_fraction"]) & (np.nan_to_num(z) >= DETECT["compact_z"])
    diffuse = scan.map(channel, "bright_diffuse")
    if diffuse is not None:
        hit |= np.nan_to_num(diffuse) >= DETECT["diffuse_z"]
    return float(hit[mask].mean()) if mask.any() else 0.0


def dense_tissue(candidate, scan):
    """Why a merged aggregate group looks like dense real tissue rather than
    debris, or None (`ENGINE["dense_tissue_*"]`). Only a group the
    aggregate detector alone raised: a saturated plateau, a fold or a patch
    of blur in the same place keeps its full score."""
    from plexora.plugins.qc.server.class_rules import is_af_channel
    from plexora.plugins.qc.server.cycles import is_nuclear

    engine = schemas.ENGINE
    if candidate.class_hint != "debris_or_foreign_object" or not candidate.merged_from or \
            any(m["detector"] != "aggregate" for m in candidate.merged_from):
        return None
    markers = [c for c in candidate.channels if not is_nuclear(c) and not is_af_channel(c)]
    if len(markers) < engine["dense_tissue_min_markers"]:
        return None
    usable = [c["name"] for c in scan.channels
              if not ({"empty_channel", "near_zero_plane"} & set(c.get("flags") or ()))]
    af = [c for c in usable if is_af_channel(c)]
    if af:
        shares = {c: _bright_share(scan, c, candidate.mask) for c in af}
        if max(shares.values()) >= engine["dense_tissue_af_share"]:
            return None
        return (f"bright compact cells in {len(markers)} markers but not in the "
                f"autofluorescence channel{'s' if len(af) > 1 else ''} "
                f"({', '.join(af)}): dense stained tissue, not debris")
    stained = [c for c in usable if not is_nuclear(c) and not is_af_channel(c)]
    share = len(markers) / max(1, len(stained))
    if share >= engine["debris_marker_share"]:
        return None
    return (f"bright compact cells in {len(markers)} of {len(stained)} stained markers, "
            "not in (nearly) all: dense stained tissue, not debris")


def _rank_strength(candidates):
    """Each candidate's rank by `strength_of` among its detector's in this
    scan (1 = strongest), in its metrics with the count -- what the audit's
    "very strong" reads (`transitions._forced`), since scores saturate."""
    by_detector = {}
    for candidate in candidates:
        by_detector.setdefault(candidate.detector, []).append(candidate)
    for members in by_detector.values():
        members.sort(key=lambda c: -strength_of(c))
        for rank, candidate in enumerate(members, start=1):
            candidate.metrics["strength"] = float(f"{strength_of(candidate):.4g}")
            candidate.metrics["strength_rank"] = rank
            candidate.metrics["strength_of"] = len(members)


def build(raw, scan, *, project, per_channel=None, per_session=None,
          min_score=None) -> dict:
    """{ranked, residual, skipped_detectors} from raw detector candidates."""
    engine = schemas.ENGINE
    per_channel = per_channel or engine["candidates_per_channel"]
    per_session = per_session or engine["candidates_per_session"]
    min_score = engine["candidate_min_score"] if min_score is None else min_score
    shape = tuple(scan.grid["shape"])
    raw, capped = cap_raw(raw)
    cleaned = [cleanup(c, shape) for c in raw]
    merged = merge(cleaned)
    for candidate in merged:
        why = dense_tissue(candidate, scan)
        if why:
            candidate.score *= engine["dense_tissue_factor"]
            candidate.severity *= engine["dense_tissue_factor"]
            candidate.metrics["dense_tissue"] = why
    merged = [c for c in merged if c.score >= min_score and c.mask.any()]
    _rank_strength(merged)
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
    return {"ranked": ranked, "residual": summarise_residual(residual, scan, capped=capped)}


def summarise_residual(residual, scan, *, capped=()):
    """What was found but not pursued, per class and channel, for the report.
    `capped`: raw candidates dropped before the merge (`cap_raw`), counted in
    `n` and again in `capped` (their cells may overlap the kept ones')."""
    cell_um = scan.grid.get("cell_um")
    out = {}
    for candidate, was_capped in [(c, False) for c in residual] + [(c, True) for c in capped]:
        key = (candidate.class_hint, candidate.channels[0] if candidate.channels else "")
        entry = out.setdefault(key, {"class": key[0], "channel": key[1], "n": 0,
                                     "cells": 0})
        entry["n"] += 1
        entry["cells"] += int(candidate.mask.sum())
        if was_capped:
            entry["capped"] = entry.get("capped", 0) + 1
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
