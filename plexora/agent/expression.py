"""Which expression matrix to gate, and whether to log1p it: decided from the
values where that is safe, asked where it is not.

A project whose `features` requirement nobody confirmed reads `X` with log1p
off by default -- which is right for a CSV already logged, wrong for raw
counts, and says nothing about a file that also carries a `log1p` layer. Before
a gating session profiles anything, each matrix the project can read is
sampled (as stored, never transformed) and classified here; `recommend` then
picks one only when the rule below settles it, and otherwise says `ask`.

The rule, in one line: a log-like matrix is read as it is (log1p off); raw
counts or intensities are read with log1p on; values are never transformed
twice.

A range alone cannot tell a CSV of log1p values from a raw export at a low
scale: both stay under `LOG_CEILING`. Their shape can (`shape`): raw
intensities are right-skewed and close to symmetric once logged, log1p values
are close to symmetric already and skew left when logged again -- and skewness
does not depend on scale. So an unconfirmed `X` in the log-like range is read
as it is when its shape says logged, with log1p on when it says raw, and is
only asked about when the shape is unclear (`ambiguous`: the harness puts the
numbers to a model before anyone is asked; `plexora.ai.harness.expression`).

Pure: numpy in, dicts out.
"""

from __future__ import annotations

import numpy as np

KINDS = ("raw_counts", "raw_intensity", "log_like", "scaled", "unknown")
CONFIDENCES = ("certain", "ask")
#: Layer names that say "log-transformed" (whole-word, case-insensitive).
LOG_NAMES = ("log", "log1p", "lognorm", "logcounts")
#: Cells sampled per matrix.
SAMPLE_CELLS = 2000
#: [cal] a non-negative matrix whose p99.9 stays below this reads as log-like
#: (log1p of a 16-bit intensity tops out near 11; of a 32-bit one near 22).
LOG_CEILING = 20.0
#: [cal] the shares the classes are cut at.
THRESHOLDS = {"integer_share": 0.99, "negative_share": 0.01, "p_high": 99.9}

#: [cal] The shape test: over a matrix's markers, the median of
#: skew(values) - |skew(log values)| on each marker's positive values. Synthetic
#: panels of background/positive mixtures (2026-10-15): raw intensities 2.1 to
#: 2.4 at any scale (x1, /200, /65535), log1p and log2 values -0.1 to 0.1, a 0-1
#: sigmoid rescale 0.2. At or above `raw` the matrix reads as raw, at or below
#: `logged` as logged; between, unclear.
SHAPE = {"raw": 1.5, "logged": 0.8, "min_values": 50, "min_markers": 3}

RULE = ("a log-like matrix is read as it is (log1p off); raw counts or intensities are read "
        "with log1p on; values are never transformed twice")


def classify(sample) -> dict:
    """{kind, n, min, p50, p99_9, max, integer_share, negative_share} for one
    matrix's sampled values (any shape; non-finite values are dropped)."""
    values = np.asarray(sample, dtype=np.float64).ravel()
    values = values[np.isfinite(values)]
    out = {"kind": "unknown", "n": int(values.size)}
    if not values.size:
        return out
    t = THRESHOLDS
    p50, p_high = np.percentile(values, [50, t["p_high"]])
    negative = float(np.mean(values < 0))
    integer = float(np.mean(np.mod(values, 1) == 0))
    out.update(min=float(values.min()), p50=float(p50), p99_9=float(p_high),
               max=float(values.max()), integer_share=integer, negative_share=negative)
    if negative > t["negative_share"]:
        out["kind"] = "scaled"
    elif negative == 0 and integer >= t["integer_share"] and float(values.max()) > 1:
        out["kind"] = "raw_counts"
    elif negative == 0 and p_high < LOG_CEILING:
        out["kind"] = "log_like"
    else:
        out["kind"] = "raw_intensity"
    return out


def _skew(values) -> float:
    spread = float(values.std())
    if spread <= 0:
        return 0.0
    return float(np.mean(((values - values.mean()) / spread) ** 3))


#: Markers listed one by one in a shape (`shape`'s `detail`): what a model
#: reading an unclear matrix is shown (`plexora.ai.harness.expression`).
DETAIL_MARKERS = 16


def shape(samples: dict) -> dict:
    """{says, d, skew_linear, skew_log, markers, detail} -- whether a
    matrix's per-marker distributions look raw or logged (`SHAPE`), whatever
    their scale. `says` is `raw`, `logged`, `unclear`, or None when fewer than
    `SHAPE["min_markers"]` markers have enough positive values to judge.
    `detail`: the first `DETAIL_MARKERS` markers' own numbers."""
    linear, logged, detail = [], [], []
    for marker, values in samples.items():
        values = np.asarray(values, dtype=np.float64).ravel()
        values = values[np.isfinite(values) & (values > 0)]
        if values.size < SHAPE["min_values"]:
            continue
        linear.append(_skew(values))
        logged.append(_skew(np.log(values)))
        if len(detail) < DETAIL_MARKERS:
            p1, p50, p99 = np.percentile(values, [1, 50, 99])
            detail.append({"marker": str(marker), "p1": float(f"{p1:.4g}"),
                           "p50": float(f"{p50:.4g}"), "p99": float(f"{p99:.4g}"),
                           "skew": round(linear[-1], 2), "skew_of_log": round(logged[-1], 2)})
    if len(linear) < SHAPE["min_markers"]:
        return {"says": None, "markers": len(linear)}
    linear, logged = np.asarray(linear), np.asarray(logged)
    d = float(np.median(linear - np.abs(logged)))
    says = "raw" if d >= SHAPE["raw"] else ("logged" if d <= SHAPE["logged"] else "unclear")
    return {"says": says, "d": round(d, 3), "skew_linear": round(float(np.median(linear)), 3),
            "skew_log": round(float(np.median(logged)), 3), "markers": int(linear.size),
            "detail": detail}


def classify_markers(samples: dict) -> dict:
    """One matrix's classification over all its sampled markers, plus the
    number of markers it read and their `shape`."""
    if not samples:
        return {"kind": "unknown", "n": 0, "markers": 0}
    pooled = np.concatenate([np.asarray(v, dtype=np.float64).ravel()
                             for v in samples.values()]) if samples else np.zeros(0)
    out = classify(pooled)
    out["markers"] = len(samples)
    if out["kind"] in ("log_like", "raw_intensity", "raw_counts"):
        out["shape"] = shape(samples)
    return out


def shape_says(option) -> str | None:
    """An option's shape verdict (`raw`, `logged`, `unclear`), or None."""
    return ((option.get("stats") or {}).get("shape") or {}).get("says")


def looks_logged(name) -> bool:
    """Whether a layer's name says it is log-transformed."""
    import re

    words = [w for w in re.split(r"[^a-z0-9]+", str(name or "").lower()) if w]
    return any(w in LOG_NAMES or w.startswith("log") for w in words)


def _layer(value):
    return value.split(":", 1)[1] if str(value).startswith("layer:") else None


def recommend(options, current, confirmed) -> dict:
    """{choice, confidence, why, options, ambiguous} over classified options
    (`[{value, label, kind, stats}]`). `choice` is `{features_layer,
    features_log}` or None; `confidence` is `certain` or `ask`; `ambiguous`
    (with `ask`) means the values were read and only their shape left it open
    -- what a model may be asked to read -- rather than that nothing could be
    read at all."""
    listing = [{"value": o["value"], "kind": o.get("kind"), "shape": shape_says(o)}
               for o in options]
    if confirmed:
        return {"choice": None, "confidence": "certain", "options": listing,
                "why": "the expression source was confirmed; it is used as it is"}
    layers = [o for o in options if _layer(o["value"])]
    # A layer in the log-like range whose shape says raw is a raw export at a
    # low scale, not a log.
    logged = [o for o in layers if o.get("kind") == "log_like" and shape_says(o) != "raw"]
    x = next((o for o in options if o["value"] == "X"), None)
    if len(logged) == 1:
        pick = logged[0]
        return {"choice": {"features_layer": pick["value"], "features_log": False},
                "confidence": "certain", "options": listing,
                "why": f"{pick['value']} is the one log-like matrix; it is read as it is "
                       "(log1p off)"}
    if len(logged) > 1:
        named = [o for o in logged if looks_logged(_layer(o["value"]))]
        if len(named) == 1:
            pick = named[0]
            return {"choice": {"features_layer": pick["value"], "features_log": False},
                    "confidence": "certain", "options": listing,
                    "why": f"{pick['value']} is the one log-like matrix named as a log; it is "
                           "read as it is (log1p off)"}
        return {"choice": None, "confidence": "ask", "options": listing,
                "why": f"{len(logged)} matrices look log-transformed; which one the panel was "
                       "normalised on cannot be told from the values"}
    if x is not None and x.get("kind") in ("raw_counts", "raw_intensity"):
        return {"choice": {"features_layer": "X", "features_log": True},
                "confidence": "certain", "options": listing,
                "why": f"X holds {x['kind'].replace('_', ' ')} and no layer is log-like; it is "
                       "read with log1p on"}
    if x is not None and x.get("kind") == "log_like":
        says = shape_says(x)
        if says == "logged":
            return {"choice": {"features_layer": "X", "features_log": False},
                    "confidence": "certain", "options": listing,
                    "why": "X's values are log-transformed already: under 20, and symmetric "
                           "where raw intensities are right-skewed; read as they are "
                           "(log1p off)"}
        if says == "raw":
            return {"choice": {"features_layer": "X", "features_log": True},
                    "confidence": "certain", "options": listing,
                    "why": "X's values are small but skewed like raw intensities (the shape "
                           "test does not depend on scale); read with log1p on"}
        return {"choice": None, "confidence": "ask", "options": listing,
                "ambiguous": says == "unclear",
                "why": "X's values are in the log range, but their shape does not say whether "
                       "they were logged; log1p on values already logged would transform "
                       "them twice"}
    if x is not None and x.get("kind") == "unknown":
        return {"choice": None, "confidence": "ask", "options": listing,
                "why": "X's values could not be read here, so nothing says whether they are "
                       "raw or logged"}
    return {"choice": None, "confidence": "ask", "options": listing,
            "why": "the values do not say which matrix holds the intensities (scaled or "
                   "unreadable)"}


def fallback_choice(options) -> dict:
    """What a non-interactive run takes when it must choose: the first
    log-like matrix as it is, else X with log1p on."""
    for option in options:
        if option.get("kind") == "log_like":
            return {"features_layer": option["value"], "features_log": False}
    return {"features_layer": "X", "features_log": True}
