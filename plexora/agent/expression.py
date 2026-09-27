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
twice. `X` is never picked from its values alone -- a CSV of log1p values and
a low-scale raw export look the same -- so an unconfirmed `X` that looks
logged is asked about.

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


def classify_markers(samples: dict) -> dict:
    """One matrix's classification over all its sampled markers, plus the
    number of markers it read."""
    if not samples:
        return {"kind": "unknown", "n": 0, "markers": 0}
    pooled = np.concatenate([np.asarray(v, dtype=np.float64).ravel()
                             for v in samples.values()]) if samples else np.zeros(0)
    out = classify(pooled)
    out["markers"] = len(samples)
    return out


def looks_logged(name) -> bool:
    """Whether a layer's name says it is log-transformed."""
    import re

    words = [w for w in re.split(r"[^a-z0-9]+", str(name or "").lower()) if w]
    return any(w in LOG_NAMES or w.startswith("log") for w in words)


def _layer(value):
    return value.split(":", 1)[1] if str(value).startswith("layer:") else None


def recommend(options, current, confirmed) -> dict:
    """{choice, confidence, why, options} over classified options
    (`[{value, label, kind, stats}]`). `choice` is `{features_layer,
    features_log}` or None; `confidence` is `certain` or `ask`."""
    listing = [{"value": o["value"], "kind": o.get("kind")} for o in options]
    if confirmed:
        return {"choice": None, "confidence": "certain", "options": listing,
                "why": "the expression source was confirmed; it is used as it is"}
    layers = [o for o in options if _layer(o["value"])]
    logged = [o for o in layers if o.get("kind") == "log_like"]
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
        return {"choice": None, "confidence": "ask", "options": listing,
                "why": "X looks log-transformed but nothing records that it is; reading it "
                       "with log1p on would transform it twice"}
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
