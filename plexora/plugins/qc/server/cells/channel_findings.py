"""Two findings about one marker that are no region of the tissue.

- **Rim enrichment**: a marker brighter in the tissue's rim -- the band of
  `schemas.TISSUE["feather_um"]` just inside the glass, the Background ROI's
  own width -- than in its interior. Edge artifacts (drying, antibody pooling
  at the coverslip's edge) look like this; so does a real epithelial edge,
  which is why the rim cells are only warned (`schemas.RIM_REASON`) and the
  finding is dismissable. The test is the region rule every signal-raising
  finding passes (`calls.signal_test`): the rim's cells must stand out of the
  interior's tail, not just shift.
- **Broad background**: a channel whose negative cells are spread wider than
  the panel's other channels' -- the lower half of its log distribution
  (median minus 16th percentile) a robust z of `DETECT["background_z"]` above
  the panel's. A finding only: a warning on every cell would take the marker
  out of every fit. Calibrated on the panel itself, so no per-marker number.

Closed form on stored data (the table's values, the tissue mask the scan
stored), deterministic, no mixture fitted.
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.qc.server import schemas


def tissue_rows(tissue, xs, ys) -> tuple:
    """(inside, rim): row-aligned masks of the cells inside the tissue and of
    those within the feather of the glass; (None, None) without a tissue
    mask, or one that is all tissue (no glass to measure from)."""
    from scipy import ndimage

    from plexora.plugins.qc.server import tissue as tissue_rules

    mask = None if tissue is None else tissue.get("mask")
    if mask is None or xs is None or ys is None:
        return None, None
    mask = np.asarray(mask, dtype=bool)
    if not mask.any() or mask.all():
        return None, None
    factor = float(tissue.get("factor") or 1.0)
    feather = tissue_rules.feather_px_at(tissue.get("pixel_um"), factor)
    band = mask & (ndimage.distance_transform_edt(mask) <= feather)
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    ok = np.isfinite(xs) & np.isfinite(ys)
    iy = np.clip(np.floor(np.where(ok, ys, 0) / factor).astype(np.int64), 0, mask.shape[0] - 1)
    ix = np.clip(np.floor(np.where(ok, xs, 0) / factor).astype(np.int64), 0, mask.shape[1] - 1)
    inside = ok & mask[iy, ix]
    rim = ok & band[iy, ix]
    return inside, rim


def rim_finding(values, inside, rim, marker, quantile):
    """(finding, flagged rows mask) for one marker's log values: the signal
    test of the rim's cells against the interior's; None when either side is
    too small to test."""
    from plexora.plugins.qc.server.cells.calls import signal_test

    finite = np.isfinite(values)
    rim_rows = np.flatnonzero(rim & finite)
    interior = inside & ~rim & finite
    if rim_rows.size < schemas.MARKER_EVIDENCE["min_cells"] \
            or int(interior.sum()) < schemas.MARKER_EVIDENCE["min_reference"]:
        return None, None
    test = signal_test(values[rim_rows], values[interior], marker, quantile)
    chosen = rim_rows[test.pop("_flag")]
    flagged = np.zeros(values.shape[0], dtype=bool)
    flagged[chosen] = True
    finding = {k: v for k, v in test.items() if k != "why"}
    if test.get("why"):
        finding["why"] = test["why"]
    finding.update(n_rim=int(rim_rows.size), n_flagged=int(chosen.size),
                   band_um=float(schemas.TISSUE["feather_um"]))
    return finding, flagged


def background_findings(values_by_marker, inside) -> dict:
    """{marker: {bg_spread, panel_median, z, flagged}} over the panel, or {}
    with fewer than `MARKER_EVIDENCE["min_cells"]` markers to calibrate on."""
    from plexora.plugins.qc.server.detectors.classical import DETECT

    spreads = {}
    for marker, values in values_by_marker.items():
        keep = np.isfinite(values) if inside is None else (inside & np.isfinite(values))
        if int(keep.sum()) < schemas.MARKER_EVIDENCE["min_reference"]:
            continue
        q16, q50 = np.quantile(values[keep], [0.16, 0.5])
        spreads[marker] = float(q50 - q16)
    if len(spreads) < schemas.MARKER_EVIDENCE["min_cells"]:
        return {}
    panel = np.asarray(list(spreads.values()), dtype=np.float64)
    median = float(np.median(panel))
    scale = 1.4826 * float(np.median(np.abs(panel - median)))
    out = {}
    for marker, spread in sorted(spreads.items()):
        z = (spread - median) / scale if scale > 0 else 0.0
        out[marker] = {"bg_spread": round(spread, 4), "panel_median": round(median, 4),
                       "z": round(float(z), 3), "flagged": bool(z >= DETECT["background_z"])}
    return out
