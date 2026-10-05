"""The outline of a registration region: the nuclei it holds, not its map cells.

A cycle out of register has no edge of its own to trace -- both DNA stains
are ordinary nuclei; they only disagree with each other -- so the Registration
Check's region is a stack of ~6.5 um map cells. The outline a reader wants is
the cells the problem reaches. Inside the region's envelope, every nucleus of
the REFERENCE cycle (the one the masks were drawn on) is read in both cycles,
after the comparison's global shift is taken out, and called once:

- `lost`      the reference has it, the comparison (almost) none
              (`LOST_RATIO`): tissue gone in that cycle, never a shift;
- `displaced` a share of its pixels at least `DISPLACED_SHARE` disagree
              between the two stains (the Registration panel's own pixel
              measure, `registration.lit_of`) -- a shift of about a quarter
              of a nucleus or more, read relative to each nucleus's size;
- `aligned`   the rest.

A registration region keeps its displaced nuclei, a one-cycle (tissue-loss)
region its lost ones, so the two can never claim the same nucleus. The kept
nuclei are joined across about one nucleus radius, holes filled, clipped to
the envelope (grown by that radius) and the tissue, and specks under
`MIN_NUCLEI` nuclei dropped: the outline follows the affected cells.

Nuclei are the segmentation mask's labels when there is a mask (the cells the
calls are made on), else the reference's own nuclear blobs. A region too large
to read, or holding too few nuclei to say anything, keeps its map outline
(`None` is returned and the caller says why).
"""

from __future__ import annotations

import math

import numpy as np

VERSION = "1"

#: [cal] A nucleus is displaced when this share of its pixels disagree.
DISPLACED_SHARE = 0.35
#: [cal] ... lost when the comparison holds under this share of the
#: reference's stain over it.
LOST_RATIO = 0.25
#: [cal] A reference nucleus must reach this normalised stain (mean over its
#: pixels) to be read at all: below it there is nothing to compare.
PRESENT = 0.25
#: [cal] A part of the outline must hold at least this many nuclei.
MIN_NUCLEI = 3
#: Fewer kept nuclei than this in the whole region and the map outline stays.
MIN_KEPT = 3
#: Nuclei a few pixels across at least, or the per-nucleus read is noise.
NUCLEUS_MIN_PX = 5.0
#: A nucleus diameter when nothing measured one (microns).
DEFAULT_NUCLEUS_UM = 8.0
#: The crop budget (pixels per plane).
MAX_PIXELS = 6_000_000

KEPT = {"registration": "displaced", "one_cycle": "lost"}


def nucleus_um(project) -> float:
    """A nucleus diameter for this image: Segmentation QC's, when it ran."""
    try:
        from plexora.plugins.qc.server.segqc import run as segqc

        summary = segqc.current(project) or {}
        value = summary.get("d_nucleus_um")
        if value:
            return float(value)
    except Exception:  # noqa: BLE001 -- a default is always safe here
        pass
    return DEFAULT_NUCLEUS_UM


def _level(source, bounds, pixel_um, d_um, pad_full):
    """(level, factor, um, box) of the finest level where the padded envelope
    fits the budget, among those where a nucleus is at least
    `NUCLEUS_MIN_PX` across; None when none fits."""
    base_um = float(pixel_um) if pixel_um else 0.5
    full_w = source.level_shape(0)[1]
    rows = []
    for level in range(max(1, int(source.levels))):
        lh, lw = source.level_shape(level)
        rows.append((full_w / max(1, lw), level, int(lh), int(lw)))
    rows.sort()
    x0, y0, x1, y1 = bounds
    for factor, level, lh, lw in rows:
        if d_um / (base_um * factor) < NUCLEUS_MIN_PX:
            break
        box = [max(0, int(math.floor((x0 - pad_full) / factor))),
               max(0, int(math.floor((y0 - pad_full) / factor))),
               min(lw, int(math.ceil((x1 + pad_full) / factor))),
               min(lh, int(math.ceil((y1 + pad_full) / factor)))]
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        if (box[2] - box[0]) * (box[3] - box[1]) <= MAX_PIXELS:
            return level, float(factor), base_um * factor, box
    return None


def _labels_from_mask(record, crop_box, factor, shape):
    """The segmentation mask's labels over the crop (nearest-resampled to
    it), or None without a readable mask."""
    import cv2

    from plexora.agent import render

    provider, _status, _why, _loc = render._mask_for(record)
    if provider is None:
        return None
    extra = int(getattr(record.segmentation, "extra_levels", 0) or 0)
    level = max(0, int(round(math.log2(max(1.0, factor)))))
    div = 2 ** level
    x0, y0, x1, y1 = crop_box
    box = (int(math.floor(x0 * factor / div)), int(math.floor(y0 * factor / div)),
           int(math.ceil(x1 * factor / div)), int(math.ceil(y1 * factor / div)))
    labels = np.asarray(provider.read_region(level + extra, box,
                                             max_pixels=(box[2] - box[0]) * (box[3] - box[1])))
    if labels.ndim > 2:
        labels = labels.reshape(labels.shape[-2:])
    if labels.shape != shape:
        labels = cv2.resize(labels.astype(np.int32), (shape[1], shape[0]),
                            interpolation=cv2.INTER_NEAREST)
    return labels.astype(np.int64)


def _labels_from_stain(a, d_px):
    """The reference's own nuclear blobs: thresholded, split where they touch
    (distance transform peaks a nucleus apart)."""
    from scipy import ndimage

    from plexora.plugins.qc.server import registration

    on = a >= registration.DENSE_NUCLEUS
    if not on.any():
        return np.zeros(a.shape, dtype=np.int64)
    distance = ndimage.distance_transform_edt(on)
    size = max(3, int(round(d_px * 0.6)) | 1)
    peaks = (distance == ndimage.maximum_filter(distance, size=size)) & \
        (distance >= max(1.0, d_px * 0.2))
    markers, _n = ndimage.label(peaks)
    try:
        from skimage.segmentation import watershed

        return watershed(-distance, markers, mask=on).astype(np.int64)
    except Exception:  # noqa: BLE001 -- connected blobs are a coarser but honest answer
        labels, _n = ndimage.label(on)
        return labels.astype(np.int64)


def classify(a, b, labels):
    """{label: (state, ref_mean, cmp_mean, share)} of every nucleus present in
    the reference: `lost`, `displaced` or `aligned`."""
    from scipy import ndimage

    from plexora.plugins.qc.server import registration

    a_s, b_s, lit = registration.lit_of(a, b)
    wrong = (lit >= registration.DENSE_LIT).astype(np.float64)
    ids = np.unique(labels)
    ids = ids[ids > 0]
    if not ids.size:
        return {}
    ref = ndimage.mean(a_s, labels, ids)
    cmp_ = ndimage.mean(b_s, labels, ids)
    share = ndimage.mean(wrong, labels, ids)
    out = {}
    for label, r, c, s in zip(ids.tolist(), ref, cmp_, share):
        if r < PRESENT:
            continue
        if c < LOST_RATIO * r:
            state = "lost"
        elif s >= DISPLACED_SHARE:
            state = "displaced"
        else:
            state = "aligned"
        out[int(label)] = (state, float(r), float(c), float(s))
    return out


def trace(session, project, unit, envelope, scan, source, *, pixel_um, shift_full=(0.0, 0.0)):
    """(geometry, record) of a registration or one-cycle region's nuclei, or
    (None, record) when the map outline should stay (record says why)."""
    from shapely.geometry import shape as to_shape

    from plexora.plugins.qc.server import polygons, refine, registration

    kind = "one_cycle" if unit.get("one_cycle") else "registration"
    want = KEPT[kind]
    reference = unit.get("reference")
    comparison = unit.get("channel")
    if (unit.get("one_cycle") or {}).get("lost_in") == "reference":
        # The reference lost the nuclei: there is no reference nucleus to
        # outline, and no cell was segmented there. The map outline stays.
        return None, {"status": "map", "method": "nuclei", "reason": "the reference cycle "
                      "lost these nuclei: no cell was drawn there to outline"}
    if not reference or not comparison:
        return None, {"status": "map", "method": "nuclei",
                      "reason": "the region does not name its two DNA channels"}
    d_um = nucleus_um(project)
    bounds = to_shape(envelope).bounds
    base_um = float(pixel_um) if pixel_um else 0.5
    pad_full = 2.0 * d_um / base_um + float(np.hypot(*shift_full))
    chosen = _level(source, bounds, pixel_um, d_um, pad_full)
    if chosen is None:
        return None, {"status": "map", "method": "nuclei",
                      "reason": "the region is too large to read nucleus by nucleus"}
    level, factor, um, box = chosen
    crop = refine._Crop(scan, box, factor, um)
    shape = crop.shape
    a_raw = refine._read_plane(source, scan, reference, level, box, shape)
    # The comparison read where its nuclei landed: the global shift taken out.
    dy, dx = (int(round(float(v) / factor)) for v in shift_full)
    cbox = [box[0] - dx, box[1] - dy, box[2] - dx, box[3] - dy]
    b_raw = _read_shifted(source, scan, comparison, level, cbox, shape)
    windows = []
    for name, plane in ((reference, a_raw), (comparison, b_raw)):
        logged = np.log1p(np.maximum(plane, 0))
        tissue = logged[logged > 0]
        lo = float(np.median(tissue)) if tissue.size else 0.0
        hi = float(np.percentile(tissue, 99.7)) if tissue.size else 1.0
        windows.append((lo, max(hi, lo + 1e-3)))
    a = registration.normalised(a_raw, windows[0])
    b = registration.normalised(b_raw, windows[1])
    d_px = d_um / um
    record_ = session.project(project)
    labels = None
    source_of = "stain"
    if getattr(record_.segmentation, "available", False):
        try:
            labels = _labels_from_mask(record_, box, factor, shape)
            source_of = "mask"
        except Exception:  # noqa: BLE001 -- the stain still gives nuclei
            labels = None
    if labels is None:
        labels = _labels_from_stain(a, d_px)
    states = classify(a, b, labels)
    inside = refine._rasterise(envelope, crop)
    # Only nuclei the envelope holds (by their centre) are this region's.
    from scipy import ndimage

    ids = np.array(sorted(states), dtype=np.int64)
    counts = {s: 0 for s in ("lost", "displaced", "aligned")}
    kept = []
    if ids.size:
        centres = ndimage.center_of_mass(np.ones(shape), labels, ids)
        for label, (cy, cx) in zip(ids.tolist(), centres):
            iy, ix = int(min(shape[0] - 1, max(0, cy))), int(min(shape[1] - 1, max(0, cx)))
            if not inside[iy, ix]:
                continue
            state = states[label][0]
            counts[state] += 1
            if state == want:
                kept.append(label)
    record = {"status": "refined", "method": "nuclei", "version": VERSION,
              "nuclei_from": source_of, "nuclei": counts, "kept": len(kept),
              "level": int(level), "um_per_px": round(um, 4),
              "nucleus_um": round(d_um, 2), "displaced_share": DISPLACED_SHARE,
              "lost_ratio": LOST_RATIO, "shift_removed_px": [float(v) for v in shift_full]}
    if len(kept) < MIN_KEPT:
        record.update(status="map", reason=f"only {len(kept)} {want} nuclei inside the "
                      "region: its map outline stays")
        return None, record
    mask = np.isin(labels, kept)
    radius = max(1, int(round(d_px / 2.0)))
    mask = refine._closing(mask, radius)
    mask = ndimage.binary_fill_holes(mask)
    grown = refine._dilate(inside, radius)
    mask &= grown & refine._tissue(crop)
    nucleus_area = math.pi * (d_px / 2.0) ** 2
    mask = refine._drop_small(mask, MIN_NUCLEI * nucleus_area)
    if not mask.any():
        record.update(status="map", reason="the kept nuclei form no part of "
                      f"{MIN_NUCLEI} nuclei or more")
        return None, record
    geometry = refine.mask_to_polygon(mask, (box[0], box[1]), factor,
                                      simplify_px=max(0.5, radius / 3.0),
                                      min_area_px=MIN_NUCLEI * nucleus_area * factor ** 2)
    if geometry is None:
        record.update(status="map", reason="the kept nuclei could not be outlined")
        return None, record
    record["kept_fraction"] = round(polygons.area_of(geometry)
                                    / max(1e-9, polygons.area_of(envelope)), 4)
    return geometry, record


def _read_shifted(source, scan, name, level, box, shape):
    """A plane read at `box`, which may run off the image: the part outside is
    zeros (no nucleus), the part inside where it belongs."""
    from plexora.plugins.qc.server import refine

    lh, lw = source.level_shape(level)
    x0, y0, x1, y1 = box
    cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(lw, x1), min(lh, y1)
    out = np.zeros(shape, dtype=np.float32)
    if cx1 <= cx0 or cy1 <= cy0:
        return out
    part = refine._read_plane(source, scan, name, level, [cx0, cy0, cx1, cy1],
                              (cy1 - cy0, cx1 - cx0))
    oy, ox = cy0 - y0, cx0 - x0
    h = min(part.shape[0], shape[0] - oy)
    w = min(part.shape[1], shape[1] - ox)
    out[oy:oy + h, ox:ox + w] = part[:h, :w]
    return out
