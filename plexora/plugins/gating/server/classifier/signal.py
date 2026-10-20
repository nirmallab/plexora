"""How well a marker separates, and the bands either side of a gate.

What a gating session's layer needs to know before it weighs the
classifier's opinion against a gate (rules.py): whether the stain is
strong enough for a model to see it at all (`grade`), and which cells sit
just either side of the gate -- the cells the layer is asked about, and so
never the cells it learns from (`band`).

Both read the mixture fit Thresholding's Auto uses (`model.fit_for`), in the
space it was fitted, so the panel, a session and the bench measure the same
thing. The band's unit is the borderline band of `capabilities.fit_band`:
half the narrower sd of the two components either side of the boundary
(`mixture.pools`'s `sd_edge`), as the session's own samplers use it.
"""

from __future__ import annotations

import numpy as np

from plexora.plugins.gating.server.classifier import rules

#: Half-bands either side of the gate that a session's picks stay out of and
#: its push reads (`[cal]`, the same as `schemas.ENGINE["classifier_band_halves"]`).
BAND_HALVES = 2.0


def _fit(ds, marker):
    from plexora.plugins.gating.server import model

    try:
        return model.fit_for(ds, marker)
    except Exception:  # noqa: BLE001 -- no fit is an answer here
        return None


def _to_fit(value, fitted):
    value = float(value)
    return float(np.log1p(max(value, 0.0))) if fitted.get("fitted_in_log") else value


def _from_fit(value, fitted):
    return float(np.expm1(value)) if fitted.get("fitted_in_log") else float(value)


def separation(ds, marker) -> float | None:
    """Ashman's D of the background and positive pools of the mixture fit."""
    from plexora.plugins.gating.server import mixture

    fitted = _fit(ds, marker)
    if not fitted or len(fitted.get("means") or ()) < 2:
        return None
    pools = mixture.pools(fitted)
    return rules.ashman_d([pools["mu_bg"], pools["mu_pos"]], [pools["sd_bg"], pools["sd_pos"]])


def band_half(ds, marker, gate) -> float | None:
    """Half the borderline band, in fit units; None without a fit."""
    from plexora.plugins.gating.server import mixture

    fitted = _fit(ds, marker)
    if not fitted or len(fitted.get("means") or ()) < 2:
        return None
    return 0.5 * float(mixture.pools(fitted)["sd_edge"])


def band(ds, marker, gate, halves=BAND_HALVES):
    """((below_low, gate, above_high) in the table's units, half band in fit
    units) -- the cells within `halves` half-bands either side of `gate` --
    or (None, None) without a fit."""
    fitted = _fit(ds, marker)
    h = band_half(ds, marker, gate)
    if fitted is None or h is None or not np.isfinite(h) or h <= 0:
        return None, None
    g = _to_fit(gate, fitted)
    return (_from_fit(g - halves * h, fitted), float(gate),
            _from_fit(g + halves * h, fitted)), h


def halves_apart(ds, marker, a, b) -> float | None:
    """How many half-bands lie between two values; None without a fit."""
    fitted = _fit(ds, marker)
    h = band_half(ds, marker, a)
    if fitted is None or h is None or not np.isfinite(h) or h <= 0:
        return None
    return abs(_to_fit(a, fitted) - _to_fit(b, fitted)) / h


def pixel_contrast(matrix, names, rows) -> float | None:
    """Median pixel contrast (`px_contrast`: the cell's mean over the tissue
    around it, signed log) of `rows`; None without pixel features."""
    names = list(names or ())
    if matrix is None or "px_contrast" not in names or rows is None or not len(rows):
        return None
    values = np.asarray(matrix[rows, names.index("px_contrast")], dtype=np.float64)
    values = values[np.isfinite(values)]
    return float(np.median(values)) if values.size else None


def assess(ds, marker, gate, values, called, matrix=None, names=None) -> dict:
    """{"d", "contrast", "grade"} at `gate`: the separation of the fit and
    the pixel contrast of the cells above the gate (`values` and `called`
    row aligned to the table)."""
    above = np.flatnonzero(called & (values > float(gate)))
    d = separation(ds, marker)
    contrast = pixel_contrast(matrix, names, above)
    return {"d": None if d is None else round(d, 3),
            "contrast": None if contrast is None else round(contrast, 3),
            "grade": rules.grade(d, contrast),
            "bars": {"low": dict(rules.SNR_LOW)}}
