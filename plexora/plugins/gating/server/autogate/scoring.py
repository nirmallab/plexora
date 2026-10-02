"""Candidate thresholds scored on biology, space and shape -- before any look.

A look can only judge the cells it is shown, and the cells it is shown are
chosen around the starting gate: start a hard marker at a mixture gate that
sits in the background (CD11c, CD68, a continuous PD-L1) and every collage is
background cells, from which no direction can recover the real gate. So the
start must already be about right, and this module is what makes it so.

**Candidates** (all in the fit space, returned raw):

- `gmm` -- the Auto (mixture) gate, as profiled.
- `onset` -- where background stops explaining the cells: the null is a
  Gaussian fitted to the background peak's core (`background`), and the
  onset is the first intensity above the mode where the local
  false-discovery rate (null / observed) falls below `onset_lfdr`. The lowest
  defensible gate.
- `ceiling` -- the noise ceiling: the background mode plus `ceiling_sd` core
  sds, past which no background cell should be. Hand gates sit near it on
  markers with and without a valley, so it is the distribution's candidate
  for a continuous marker (HLA-ABC, B2M, PD-L1): no valley, but a background
  to rise out of. The background is the tallest peak, or a lower peak well
  clear below it when most cells are positive.
- `bio:<P>` -- for a partner P that the marker's positives should also be
  positive for (subset or co-expressed), the share of P+ cells along the
  marker's intensity rises from a baseline to a plateau as background gives
  way to the real population. A logistic fit to that curve; the candidate is
  x0 + `bio_k`·s, where the curve has done most of its rise (the cells above
  are as P+ as the true positives get). Re-fitted with P's gate nudged both
  ways: a candidate whose elasticity (its movement per unit of P's gate
  movement) is high follows P's gate rather than this marker's population
  (CD4 inside CD45: nearly the same cells) and is `robust: False` -- it
  does not lead.
- `anti:<Q>` -- for an exclusive partner Q, the same curve falls; where it
  has finished falling is shown as evidence (Q's spill or doublets above it
  cannot be fixed by thresholding), never used to clip.
- `sens:<P>` -- for a partner whose positives should be positive for this
  marker (CD8 for CD3), the marker's 5th percentile among P+ cells: a gate
  above it drops true positives. Evidence only.
- `children` -- the evidence running up the hierarchy (`hierarchy`): when
  several reliably gated children agree on where their positives sit on
  this marker's axis (the `sens:` values of CD3, CD20 and CD163 on CD45),
  their median says where this marker's positives begin. It leads only when
  the marker's own evidence is weak (no robust partner curve, a poorly
  separated or degenerate mixture); otherwise it is shown beside the rest.

**Which partners.** Only those the hierarchy selects (`hierarchy.select`):
never one whose gate failed or is under review, one of low reliability
only when nothing better of its kind exists -- and then it can lead only
when no reliable partner can, at a lower weight.

**The proposal.** Partner evidence leads when there is robust `bio:` evidence
(the strictest robust partner, since the positives must satisfy every
relation), nudged a third of the way toward
the Auto gate when the two are within two background sds. Without it, a
weighted mean of the Auto gate (weighted by the mixture's separation, and
little for a continuous marker) and the noise ceiling.

**Scores** (each in [0, 1], reported per candidate, so the look that follows
knows what each candidate trades): `distribution` (1 - local fdr: how surely
cells at the gate are above background), `coexpression` (how far the
partner curves have risen), `anti` (how far the exclusive curves have
fallen), `retained` (of the cells in excess over the background null, the
share the gate keeps -- the sensitivity side), `region` (agreement with the
same estimate made separately in each tile of the tissue) and `morphology`
(cells just above the gate are not systematically larger or smaller than
those just below -- merged or fragment cells). `score` combines them; it
ranks candidates for the reader and never overrides the proposal.

Pure over (data, marker, partner gates, options): deterministic, no
sampling, cached on the handle set.
"""

from __future__ import annotations

import numpy as np

#: Bumped whenever the candidates or the proposal change meaning.
VERSION = "2"

#: The candidate families, by id prefix (`bio:CD45`, `anti:SOX10`, `sens:CD8`).
FAMILIES = ("gmm", "onset", "ceiling", "bio", "anti", "sens", "children", "score")

#: [cal] calibrated on hand-gated multiplexed images (melanoma CyCIF), where
#: the hand gates sit near x0 + 2s of a lineage partner's curve.
PARAMS = {
    "onset_lfdr": 0.05,          # the onset's local false-discovery rate
    "ceiling_sd": 6.0,           # the noise ceiling: mode + this many core sds
    "background_peak": 0.25,     # a lower peak this tall (of the tallest) ...
    "background_separation_sd": 8.0,  # ... and this many of its sds below it is the background
    "bio_k": 2.0,                # x0 + k*s of a partner curve
    "bio_min_rise": 0.3,         # a partner curve must rise at least this much
    "anti_min_fall": 0.15,       # an exclusive curve must fall at least this much
    "robust_shift_sd": 0.5,      # partner gate nudged by this many of its bg sds
    "robust_max_elasticity": 0.75,  # a bio candidate moving more than this per unit of
                                    # partner-gate movement is not robust
    "nudge_within_sd": 2.0,      # the proposal moves toward the Auto gate within this
    "min_bin": 40,               # cells per bin of a partner curve
    "bins": 120,                 # bins across the marker's body
    "coexpressed_past_ceiling_sd": 6.0,  # a co-expressed curve this far past the ceiling
                                         # follows a subpopulation; it does not lead
    "degenerate_fraction": 0.9,  # an Auto gate calling more than this is not a background split
    "bio_min_fraction": 0.005,   # a partner candidate calling fewer cells than this does not lead
    "sens_quantile": 0.05,
    "sens_min_cells": 100,
    "children_min": 2,           # reliable children that must agree ...
    "children_agree_sd": 2.0,    # ... within this many background sds
    "low_lead_weight": 1.0,      # a low-reliability partner's lead (a reliable one: 2.0)
    "tiles": 3,                  # region check: tiles x tiles over the tissue
    "tile_min_cells": 1500,
    "weights": {"distribution": 1.0, "coexpression": 2.0, "anti": 1.0},
    "mix": {"purity": 0.45, "retained": 0.35, "region": 0.1, "morphology": 0.1},
}


def _logistic(x, b, a, x0, s):
    return b + (a - b) / (1.0 + np.exp(-(x - x0) / s))


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -50, 50)))


def _binned(x, y, lo, hi, width, min_n):
    edges = np.arange(lo, hi + width, width)
    if edges.size < 3:
        return None
    index = np.digitize(x, edges) - 1
    ok = (index >= 0) & (index < edges.size - 1)
    n = np.bincount(index[ok], minlength=edges.size - 1)
    total = np.bincount(index[ok], weights=y[ok].astype(np.float64), minlength=edges.size - 1)
    keep = n >= min_n
    centres = (edges[:-1] + edges[1:]) / 2
    return centres[keep], total[keep] / n[keep], n[keep]


def partner_curve(x, positive, lo, hi, width, *, min_n=None):
    """{b, a, x0, s, rise} of a logistic fitted to P(partner+ | marker = x),
    or None when it cannot be fitted. `rise` = a - b (negative: falling)."""
    from scipy.optimize import curve_fit

    min_n = PARAMS["min_bin"] if min_n is None else min_n
    binned = _binned(x, positive, lo, hi, width, min_n)
    if binned is None or binned[0].size < 8:
        return None
    c, r, n = binned
    half = (r.min() + r.max()) / 2
    p0 = [float(r[:3].mean()), float(r[-3:].mean()),
          float(c[int(np.argmin(np.abs(r - half)))]), max(width * 3, 1e-3)]
    try:
        (b, a, x0, s), _ = curve_fit(_logistic, c, r, p0=p0, sigma=1 / np.sqrt(n),
                                     maxfev=20000,
                                     bounds=([0, 0, lo, width / 4], [1, 1, hi, (hi - lo)]))
    except (RuntimeError, ValueError):
        return None
    return {"b": float(b), "a": float(a), "x0": float(x0), "s": float(s),
            "rise": float(a - b)}


def background(body):
    """{mode, sd, onset, ceiling, centres, lfdr, excess} of a fit-space body,
    or None for a constant one.

    The null is a Gaussian fitted to the background peak's core: its height,
    and an sd from the narrower half-width at half-maximum of the two sides
    -- a long low tail (unstained or damaged cells) or a shoulder of real
    positives would bias any wider fit. `lfdr` = null / observed per bin;
    `onset` is the first bin above the mode where it drops below
    `onset_lfdr` (background stops explaining the cells); `ceiling` is mode +
    `ceiling_sd` core sds (no background cell reaches it)."""
    from scipy.ndimage import gaussian_filter1d

    lo, hi = (float(v) for v in np.quantile(body, [0.001, 0.999]))
    if not hi > lo:
        return None
    counts, edges = np.histogram(body, bins=400, range=(lo, hi))
    centres = (edges[:-1] + edges[1:]) / 2
    f = gaussian_filter1d(counts.astype(np.float64), 3)
    step = float(centres[1] - centres[0])

    def core_sd(j):
        # The narrower half-width at half-maximum: a long low tail widens the
        # left side, a shoulder of real positives the right.
        left = np.flatnonzero((centres < centres[j]) & (f < f[j] / 2))
        right = np.flatnonzero((centres > centres[j]) & (f < f[j] / 2))
        widths = [centres[j] - centres[left[-1]]] if left.size else []
        widths += [centres[right[0]] - centres[j]] if right.size else []
        return max(float(min(widths)) / 1.1774 if widths else 5 * step, step)

    # The background is the tallest peak -- unless a lower peak stands well
    # clear below it: where most cells are positive (HLA-ABC, B2M) the
    # tallest peak is the positives, and the cells without the marker are a
    # smaller peak far below.
    top = int(np.argmax(f))
    interior = np.flatnonzero((f[1:-1] >= f[:-2]) & (f[1:-1] > f[2:])) + 1
    lower = [int(j) for j in interior
             if f[j] >= PARAMS["background_peak"] * f[top]
             and centres[top] - centres[j] > PARAMS["background_separation_sd"] * core_sd(j)]
    i = lower[0] if lower else top
    mode = float(centres[i])
    sd = core_sd(i)
    null = f[i] * np.exp(-0.5 * ((centres - mode) / sd) ** 2)
    null[:i + 1] = f[:i + 1]
    lfdr = np.clip(null / np.maximum(f, 1e-12), 0.0, 1.0)
    excess = np.clip(f - null, 0.0, None)
    excess[:i + 1] = 0.0
    above = np.flatnonzero((centres > mode) & (lfdr < PARAMS["onset_lfdr"]))
    onset = float(centres[above[0]]) if above.size else None
    ceiling = mode + PARAMS["ceiling_sd"] * sd
    return {"mode": mode, "sd": sd, "onset": onset,
            "ceiling": ceiling if ceiling < hi else None,
            "centres": centres, "lfdr": lfdr, "excess": excess}


def _partner_sd(ds, partner, col_p):
    from plexora.plugins.gating.server.autogate import profile as profmod

    fit = profmod.fit_for(ds, partner)
    if fit is not None:
        sd = profmod.pools(fit).get("sd_bg")
        if sd:
            return float(sd)
    body = col_p.body if col_p.body.size else col_p.fit
    if not body.size:
        return 0.1
    q = np.quantile(body, [0.01, 0.99])
    return float(q[1] - q[0]) / 20 or 0.1


def _tiles(ds, eligible, n_tiles):
    """Row-aligned tile index (or -1) over the eligible cells, by position
    quantiles, so every tile holds about as many cells."""
    from plexora.plugins.gating.server.autogate import cells as cellmod

    c = cellmod.cells(ds)
    ok = eligible & c.valid
    tile = np.full(c.n, -1, dtype=np.int64)
    if ok.sum() < n_tiles * n_tiles:
        return tile
    qx = np.quantile(c.xs[ok], np.linspace(0, 1, n_tiles + 1)[1:-1])
    qy = np.quantile(c.ys[ok], np.linspace(0, 1, n_tiles + 1)[1:-1])
    tx = np.searchsorted(qx, c.xs[ok], side="right")
    ty = np.searchsorted(qy, c.ys[ok], side="right")
    tile[ok] = ty * n_tiles + tx
    return tile


def score(ds, marker, *, gmm=None, separation_d=None, partners=(), continuous=False) -> dict:
    """{version, candidates, proposal, proposal_source, leading, background,
    region, notes} for one marker. `partners` are dicts {marker, relation,
    gate, direction ("forward": the marker's own relation to the partner;
    "reverse": the partner's relation to the marker), confidence}."""
    from plexora.agent import cell_exclusions
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import profile as profmod

    key = tuple(sorted((p["marker"], p["relation"], p.get("direction", "forward"),
                        round(float(p["gate"]), 6), p.get("gate_confidence") or "")
                       for p in partners))
    qc = cell_exclusions.fingerprint(cell_exclusions.current(ds))
    gmm_key = None if gmm is None else round(float(gmm), 6)

    def compute():
        return _score(ds, marker, gmm=gmm, separation_d=separation_d, partners=partners,
                      continuous=continuous, cellmod=cellmod, profmod=profmod)

    return ds.cached(("autogate.scoring", VERSION, marker, gmm_key, bool(continuous), key,
                      profmod.fingerprint(ds), qc), compute)


def _score(ds, marker, *, gmm, separation_d, partners, continuous, cellmod, profmod):
    col = profmod.column(ds, marker)
    body = col.body if col.body.size else col.fit
    out = {"version": VERSION, "marker": marker, "candidates": [], "proposal": None,
           "proposal_source": None, "leading": None, "notes": []}
    if body.size < 200:
        out["notes"].append("too few cells to score candidates")
        return out
    bg = background(body)
    if bg is None:
        out["notes"].append("a constant column")
        return out
    mode, sd, onset, ceiling = bg["mode"], bg["sd"], bg["onset"], bg["ceiling"]
    centres, lfdr, excess = bg["centres"], bg["lfdr"], bg["excess"]
    lo, hi = (float(v) for v in np.quantile(body, [0.01, 0.998]))
    width = max((hi - lo) / PARAMS["bins"], 1e-6)
    x = col.to_fit(cellmod.values(ds, marker))
    finite = np.isfinite(x)
    out["background"] = {"mode": float(col.from_fit(mode)), "sd_fit": sd,
                         "onset": None if onset is None else float(col.from_fit(onset)),
                         "ceiling": None if ceiling is None else float(col.from_fit(ceiling))}
    raw = []   # (id, family, fit value, extra)
    g_fit = None if gmm is None else float(col.to_fit(gmm))
    if g_fit is not None:
        raw.append(("gmm", "distribution", g_fit, {}))
    if onset is not None:
        raw.append(("onset", "distribution", onset, {}))
    if ceiling is not None:
        raw.append(("ceiling", "distribution", ceiling, {}))
    bio, anti, sens = [], [], []
    for partner in partners:
        name, relation = partner["marker"], partner["relation"]
        direction = partner.get("direction", "forward")
        if name == marker:
            continue
        try:
            col_p = profmod.column(ds, name)
            y = col_p.to_fit(cellmod.values(ds, name))
        except Exception:
            continue
        both = finite & np.isfinite(y)
        if both.sum() < 500:
            continue
        g_p = float(col_p.to_fit(partner["gate"]))
        xb, yb = x[both], y[both]
        if direction == "forward" and relation in ("subset", "coexpressed"):
            curve = partner_curve(xb, yb > g_p, lo, hi, width)
            if curve is None or curve["rise"] < PARAMS["bio_min_rise"]:
                continue
            value = curve["x0"] + PARAMS["bio_k"] * curve["s"]
            shift = PARAMS["robust_shift_sd"] * _partner_sd(ds, name, col_p)
            moved = []
            for sign in (-1, 1):
                alt = partner_curve(xb, yb > g_p + sign * shift, lo, hi, width)
                if alt is not None and alt["rise"] >= PARAMS["bio_min_rise"]:
                    moved.append(alt["x0"] + PARAMS["bio_k"] * alt["s"])
            spread = (max(moved) - min(moved)) if len(moved) == 2 else None
            # Elasticity: how far the candidate moves per unit the partner's
            # gate moves. Near 1, it tracks the partner's gate, not this
            # marker's population.
            elasticity = None if spread is None else spread / (2 * shift)
            robust = elasticity is not None and elasticity <= PARAMS["robust_max_elasticity"]
            # Past the marker's own population (CTLA4 against CD3: the share of
            # CD3+ cells keeps rising into the last few cells) it cannot lead.
            beyond = (col.fraction(float(col.from_fit(value))) or 0.0) < \
                PARAMS["bio_min_fraction"]
            # A co-expressed partner covers only part of the positives, so its
            # curve can follow a bright subpopulation (CD3+ among CD4-bright
            # cells: T helpers over macrophages) far past where background
            # ends; there it does not lead. A subset relation holds for every
            # positive, so its curve may.
            past_ceiling = (relation == "coexpressed" and ceiling is not None
                            and value > ceiling + PARAMS["coexpressed_past_ceiling_sd"] * sd)
            robust = robust and not beyond and not past_ceiling
            entry = {"partner": name, "relation": relation, "curve": curve,
                     "robust": bool(robust), "reliable": _reliable(partner),
                     "elasticity": None if elasticity is None else float(elasticity),
                     "beyond_population": bool(beyond),
                     "past_ceiling": bool(past_ceiling),
                     "confidence": partner.get("confidence")}
            bio.append((value, entry))
            raw.append((f"bio:{name}", "coexpression", value, entry))
        elif relation == "exclusive":
            curve = partner_curve(xb, yb > g_p, lo, hi, width)
            if curve is None or curve["rise"] > -PARAMS["anti_min_fall"]:
                continue
            q05, q995 = np.quantile(xb, [0.05, 0.995])
            if not (q05 <= curve["x0"] <= q995):
                continue
            value = curve["x0"] + PARAMS["bio_k"] * curve["s"]
            entry = {"partner": name, "relation": relation, "curve": curve}
            anti.append((value, entry))
            raw.append((f"anti:{name}", "anti", value, entry))
        elif direction == "reverse" and relation == "subset":
            inside = xb[yb > g_p]
            if inside.size < PARAMS["sens_min_cells"]:
                continue
            value = float(np.quantile(inside, PARAMS["sens_quantile"]))
            entry = {"partner": name, "relation": "superset", "n_partner_positive":
                     int(inside.size), "reliable": _reliable(partner)}
            sens.append((value, entry))
            raw.append((f"sens:{name}", "sensitivity", value, entry))

    # The children's consensus: where reliably gated subsets of this marker
    # have their positives on its axis.
    kids = [v for v, e in sens if e["reliable"]]
    children = None
    if len(kids) >= PARAMS["children_min"] \
            and max(kids) - min(kids) <= PARAMS["children_agree_sd"] * sd:
        children = float(np.median(kids))
        raw.append(("children", "sensitivity", children,
                    {"relation": "children", "n_children": len(kids)}))

    # -- the proposal --
    groups = []
    robust = [(v, e) for v, e in bio if e["robust"]]
    # A reliable partner leads before a low-reliability one ever does.
    trusted = [(v, e) for v, e in robust if e["reliable"]] or robust
    leading = None
    if trusted:
        # The strictest robust partner leads: the positives must satisfy every
        # relation, and a lineage-specific co-expressed partner (CD163 for
        # CD68) is often stricter than the pan-lineage subset one (CD45).
        value, lead = max(trusted, key=lambda item: item[0])
        leading = lead["partner"]
        groups.append(("coexpression", value,
                       2.0 if lead["reliable"] else PARAMS["low_lead_weight"]))
        if not lead["reliable"]:
            out["notes"].append(f"led by {leading}, whose gate is of low reliability "
                                "(no reliable partner curve): down-weighted")
    d = separation_d or 0.0
    degenerate = gmm is not None and (col.fraction(gmm) or 0.0) > PARAMS["degenerate_fraction"]
    if children is not None and not trusted and (d < profmod.THRESHOLDS["weak_d"] or degenerate
                                                     or continuous):
        # Nothing of the marker's own says where its positives begin; its
        # reliable children agree on it.
        groups.append(("children", children, 2.0))
        out["notes"].append("the marker's own distribution is weak: its reliably gated "
                            "children's positives place the gate")
    w_gmm = 2.0 if d >= 2.5 else (1.0 if d >= 1.5 else 0.4)
    if continuous:
        w_gmm = min(w_gmm, 0.4)
    if degenerate:
        # A mixture that calls nearly every cell positive split off a low
        # tail, not a background (PCNA's unstained cells).
        w_gmm = 0.1
        out["notes"].append("the Auto gate calls nearly every cell positive; it barely counts")
    if g_fit is not None:
        groups.append(("gmm", g_fit, w_gmm))
    # The noise ceiling, or the onset when the background is too broad for a
    # ceiling inside the data.
    bound = ("ceiling", ceiling) if ceiling is not None else ("onset", onset)
    if bound[1] is not None:
        groups.append((bound[0], bound[1], 0.5 if groups and groups[0][0] in (
            "coexpression", "children") else 2.0))
    if not groups:
        out["notes"].append("no candidate: no mixture, no onset and no partner curve")
        return out
    if groups[0][0] in ("coexpression", "children"):
        values = np.array([g[1] for g in groups])
        weights = np.array([g[2] for g in groups])
        order = np.argsort(values, kind="stable")
        cumulative = np.cumsum(weights[order])
        chosen = int(order[np.searchsorted(cumulative, cumulative[-1] / 2)])
        proposal = float(values[chosen])
        source = groups[chosen][0]
        if source == "children":
            leading = "children"
        elif source != "coexpression":
            leading = None
        if g_fit is not None and abs(g_fit - proposal) < PARAMS["nudge_within_sd"] * sd:
            proposal = (2 * proposal + g_fit) / 3
    else:
        weights = np.array([g[2] for g in groups])
        proposal = float((weights * np.array([g[1] for g in groups])).sum() / weights.sum())
        source = "+".join(g[0] for g in groups)
    raw.append(("score", "proposal", proposal, {}))

    # -- scores per candidate --
    total_excess = float(excess.sum()) or 1.0
    tail_excess = np.cumsum(excess[::-1])[::-1]
    curves_up = [e["curve"] for _v, e in bio if e["robust"]] or [e["curve"] for _v, e in bio]
    curves_down = [e["curve"] for _v, e in anti]
    region = _region(ds, marker, x, finite, bio, partners, lo, hi, width, col, cellmod,
                     profmod)
    area = cellmod.cells(ds).area
    w = PARAMS["weights"]
    mix = PARAMS["mix"]
    for point_id, family, value, extra in raw:
        comp = {}
        j = int(np.clip(np.searchsorted(centres, value), 0, centres.size - 1))
        comp["distribution"] = float(1.0 - lfdr[j]) if value > mode else 0.0
        if curves_up:
            comp["coexpression"] = float(np.mean([_sigmoid((value - c["x0"]) / c["s"])
                                                  for c in curves_up]))
        if curves_down:
            comp["anti"] = float(np.mean([_sigmoid((value - c["x0"]) / c["s"])
                                          for c in curves_down]))
        comp["retained"] = float(tail_excess[j] / total_excess)
        if region and region.get("median") is not None:
            spread = max(region["iqr"] / 1.35, 0.5 * sd)
            comp["region"] = float(np.exp(-0.5 * ((value - region["median"]) / spread) ** 2))
        if area is not None:
            ratio = _area_ratio(x, finite, area, value, 0.5 * sd)
            if ratio is not None:
                comp["area_ratio"] = ratio
                comp["morphology"] = float(np.exp(-(np.log(ratio) ** 2) / (2 * 0.2 ** 2)))
        purity_parts = [(comp[k], w[k]) for k in ("distribution", "coexpression", "anti")
                        if k in comp]
        purity = sum(v * wt for v, wt in purity_parts) / sum(wt for _v, wt in purity_parts)
        total = mix["purity"] * purity + mix["retained"] * comp["retained"]
        weight = mix["purity"] + mix["retained"]
        for k in ("region", "morphology"):
            if k in comp:
                total += mix[k] * comp[k]
                weight += mix[k]
        low = float(col.from_fit(value))
        entry = {"id": point_id, "family": family, "low": low, "fit": float(value),
                 "fraction": col.fraction(low), "components": comp,
                 "score": float(total / weight)}
        if family == "coexpression":
            entry.update(robust=extra["robust"], relation=extra["relation"],
                         elasticity=extra["elasticity"],
                         rise=extra["curve"]["rise"])
        elif family in ("anti", "sensitivity"):
            entry["relation"] = extra["relation"]
            if extra.get("n_children"):
                entry["n_children"] = extra["n_children"]
        out["candidates"].append(entry)
    out["candidates"].sort(key=lambda c: c["fit"])
    out["proposal"] = float(col.from_fit(proposal))
    out["proposal_source"] = source
    out["leading"] = leading
    out["region"] = None if not region else {
        "tiles": region["tiles"], "basis": region["basis"],
        "median": None if region["median"] is None else float(col.from_fit(region["median"])),
        "iqr_fit": region["iqr"]}
    if bio and not robust:
        out["notes"].append("every partner curve follows its partner's gate (the two "
                            "populations are nearly the same cells): partner evidence "
                            "does not lead")
    return out


def _region(ds, marker, x, finite, bio, partners, lo, hi, width, col, cellmod, profmod):
    """The leading estimate made again in each tile: the robust partner curve
    with the most rise, else the noise ceiling. {tiles, basis, median, iqr} or
    None."""
    eligible = cellmod.eligible(ds, marker)
    if eligible.shape[0] != x.shape[0]:
        return None
    tile = _tiles(ds, eligible & finite, PARAMS["tiles"])
    lead = max(((e["curve"]["rise"], e["partner"]) for _v, e in bio if e["robust"]),
               default=None)
    y = None
    if lead is not None:
        name = lead[1]
        gate = next(float(p["gate"]) for p in partners if p["marker"] == name)
        col_p = profmod.column(ds, name)
        y = col_p.to_fit(cellmod.values(ds, name)) > col_p.to_fit(gate)
    estimates = []
    for t in range(PARAMS["tiles"] ** 2):
        rows = (tile == t) & finite
        if rows.sum() < PARAMS["tile_min_cells"]:
            continue
        if y is not None:
            curve = partner_curve(x[rows], y[rows], lo, hi, width * 2, min_n=20)
            if curve is not None and curve["rise"] >= PARAMS["bio_min_rise"]:
                estimates.append(curve["x0"] + PARAMS["bio_k"] * curve["s"])
        else:
            found = background(np.sort(x[rows]))
            if found is not None and found["ceiling"] is not None:
                estimates.append(found["ceiling"])
    if len(estimates) < 3:
        return None
    q1, med, q3 = np.quantile(estimates, [0.25, 0.5, 0.75])
    return {"tiles": len(estimates), "basis": f"bio:{lead[1]}" if lead else "ceiling",
            "median": float(med), "iqr": float(q3 - q1)}


def _area_ratio(x, finite, area, value, half):
    """Median area of cells just above `value` over those just below."""
    ok = finite & np.isfinite(area)
    above = ok & (x > value) & (x <= value + half)
    below = ok & (x <= value) & (x > value - half)
    if above.sum() < 30 or below.sum() < 30:
        return None
    a, b = float(np.median(area[above])), float(np.median(area[below]))
    return a / b if b > 0 else None


def evidence_partners(context, marker, gated) -> list:
    """The gated markers the hierarchy selects as evidence for `marker`
    (`hierarchy.relations`, `hierarchy.select`), in either direction and
    including the relations the tree implies -- the scoring's partners,
    wider than the two references a look plots. `gated` is {marker: (gate,
    grade)}, grade one of `hierarchy.GRADES` (or pending); a failed or
    pending one is never returned.

    Forward: the marker's own partners and markers that name it co-expressed
    or exclusive. Reverse: markers that are a subset of this one (CD8 of CD3)
    -- the `sens:` bounds and the `children` consensus. A relation stated at
    low confidence counts only as an exclusion."""
    from plexora.plugins.gating.server.autogate import hierarchy

    rels = [r for r in hierarchy.relations(context, marker)
            if r["confidence"] != "low" or r["relation"] == "exclusive"]
    grades = {m: g for m, (_gate, g) in gated.items()}
    out = []
    for r in hierarchy.select(rels, grades)["used"]:
        gate = gated[r["marker"]][0]
        if gate is None:
            continue
        out.append({"marker": r["marker"], "relation": r["relation"],
                    "direction": r["direction"], "gate": float(gate),
                    "confidence": r["confidence"], "gate_confidence": r["grade"],
                    "weight": r["weight"], "derived": r["derived"],
                    "down_weighted": r["down_weighted"]})
    return out


def _reliable(partner):
    """A partner whose gate is moderate or better -- or one passed without
    a grade (a caller that vouches for its gates)."""
    return partner.get("gate_confidence") in (None, "high", "moderate")
