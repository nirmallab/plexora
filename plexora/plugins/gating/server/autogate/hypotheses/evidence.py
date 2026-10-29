"""What the labelled cells say about the candidate gates.

A look labels cells blind -- `own_stain`, `not_own_stain`, `uncertain`,
`artifact` -- with no value, call or gate in the picture. Each labelled cell
falls in one interval between neighbouring candidates (`candidates.build`).
Per interval, the own-stain fraction among the cells read either way is the
evidence; a Wilson interval says how sure it is.

A candidate gate is scored by the cells it would get wrong: below it, the
interval's own-stain share of its cells is called negative; above it, the
rest is called positive. Fractions are made non-decreasing in value first
(pool-adjacent-violators, weighted by the cells read): a reader's noise
should not make a brighter interval look less stained than a dimmer one. An
interval with no reading counts one half either way, so it never favours a
candidate. The leader is the candidate with the fewest cells wrong.

The leader is *decisive* against a rival when the own-stain share of the
cells between the two -- each interval weighted by the cells it holds, not
by the cells read from it -- has a Wilson interval wholly on the leader's
side of one half, at the sample size the weighting leaves (`between`). A
first look shows every interval the same number of cells, so pooling the
reads as they come would let a sliver of forty cells outvote an interval of
a thousand. When some cells were picked by the classifier (`calls`,
`strata`), each interval is split by the classifier's call and each part
weighted by its share of the interval's cells: an enriched pick is read for
what it is, never as the interval.
Rivals within `tolerance` (fit units; the user's own retest noise) need not
be beaten. Nothing here averages candidates: the gate is always one of them.

That is the `wilson` rule (VERSION 5). The `posterior` rule (VERSION 6)
decides on the same evidence differently (`posterior`, `support`): each
read interval's share is drawn from Beta(own + 1/2, not + 1/2), pooled
monotone per draw, and the candidate with the fewest cells wrong in each
draw counted, so every candidate gets P(it is the best gate). The losses
of candidates in a sparse tail differ by a few dozen cells, so two or three
reads swing a point estimate across 0.3 or more (lsp11385_2, three runs,
2026-10-21); the posterior says when they are that close. The plateau is
every candidate with P of at least `tau`; the leader is the plateau member
nearest the scored start, so flat evidence keeps the start and decisive
evidence moves it. A leader is resolved when P(the best gate lies within
the tolerance of it, or changes fewer than `material` cells) reaches `rho`.
Symmetric shrinkage of the shares could not do this: a Beta(a, a) mean
never crosses one half, so it never changes which side a share is on.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict

import numpy as np

OWN = "own_stain"
NOT_OWN = "not_own_stain"
LABELS = (OWN, NOT_OWN, "uncertain", "artifact")

#: [cal] the two-sided normal quantile of the Wilson intervals (95 %).
Z = 1.96

#: [cal] draws of the posterior over candidates: P(c* = j) moves by about
#: 0.025 between two seeds at P = 1/2.
DRAWS = 400


def wilson(k, n, z=Z) -> tuple:
    """(low, high) of the Wilson score interval of k successes in n."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def interval_of(value, bounds) -> int | None:
    """Index k with bounds[k] < value <= bounds[k + 1], or None outside."""
    for k in range(len(bounds) - 1):
        if bounds[k] < value <= bounds[k + 1]:
            return k
    return None


def _stratum(cell, calls, key=None):
    if key is not None:
        value = cell.get(key)
        return None if value is None else int(value)
    if calls is None:
        return None
    return calls.get(str(int(cell["cell_id"])), calls.get(int(cell["cell_id"])))


def _tally():
    return {"own": 0, "not": 0, "other": 0, "uncertain": 0, "artifact": 0}


def counts(bounds, observations, calls=None, key=None) -> list:
    """Per interval: own, not_own and other labels of the valid observations'
    cells (sentinels excluded), in total and per stratum (`by`: the
    classifier's call of each cell when `calls` is given, the cell's `key`
    field when that is, else one). `other` is `uncertain` plus `artifact`,
    each also counted on its own."""
    out = [dict(_tally(), by={}) for _ in range(max(0, len(bounds) - 1))]
    for obs in observations:
        if not obs.get("valid", True):
            continue
        for cell in obs.get("cells") or ():
            if cell.get("sentinel"):
                continue
            k = interval_of(float(cell["value"]), bounds)
            if k is None:
                continue
            label = cell.get("label")
            kind = "own" if label == OWN else "not" if label == NOT_OWN else "other"
            part = out[k]["by"].setdefault(str(_stratum(cell, calls, key)), _tally())
            for tally in (out[k], part):
                tally[kind] += 1
                if kind == "other":
                    tally["artifact" if label == "artifact" else "uncertain"] += 1
    return out


def _read(part) -> int:
    return int((part or {}).get("own", 0) + (part or {}).get("not", 0))


def _strata(entry, sizes, missing) -> dict:
    """The strata an interval's share is weighted over: all of `sizes`, or
    (`missing="pooled"`) only those read, so a stratum nobody read takes the
    share of those that were instead of one half."""
    sizes = {s: n for s, n in (sizes or {}).items() if n}
    if missing == "pooled":
        sizes = {s: n for s, n in sizes.items() if _read(entry["by"].get(str(s)))}
    return sizes


def estimate(entry, sizes=None, missing="half") -> tuple:
    """(own share, effective cells read) of one interval: the share in each
    stratum weighted by the stratum's cells (`sizes`, {stratum: cells}), a
    stratum never read counting one half as if read once (`missing="half"`)
    or left out (`"pooled"`); without `sizes`, the plain share. (None, 0)
    when nothing was read either way."""
    read = entry["own"] + entry["not"]
    if not read:
        return None, 0.0
    sizes = _strata(entry, sizes, missing)
    if not sizes:
        return entry["own"] / read, float(read)
    total = float(sum(sizes.values())) or 1.0
    share, inverse = 0.0, 0.0
    for stratum, size in sizes.items():
        w = size / total
        part = entry["by"].get(str(stratum)) or {}
        n = part.get("own", 0) + part.get("not", 0)
        share += w * (part["own"] / n if n else 0.5)
        inverse += w * w / max(n, 1)
    return share, (1.0 / inverse if inverse else 0.0)


def monotone(fractions, weights) -> list:
    """Pool-adjacent-violators: the non-decreasing sequence closest to
    `fractions` in weighted squares. Entries with weight 0 are left None."""
    blocks = []      # [value, weight, [indices]]
    for i, (f, w) in enumerate(zip(fractions, weights)):
        if not w:
            continue
        blocks.append([float(f), float(w), [i]])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, i2 = blocks.pop()
            v1, w1, i1 = blocks.pop()
            blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, i1 + i2])
    out = [None] * len(fractions)
    for value, _w, indices in blocks:
        for i in indices:
            out[i] = value
    return out


def fractions(table, strata=None, missing="half") -> list:
    """Each interval's own-stain share (None unread), made non-decreasing,
    each weighted by its effective cells read."""
    pairs = [estimate(entry, None if strata is None else strata[k], missing)
             for k, entry in enumerate(table)]
    return monotone([0.0 if f is None else f for f, _n in pairs], [n for _f, n in pairs])


def losses(n_cells, fitted) -> list:
    """Cells each candidate gate would get wrong (candidate j sits at the
    lower bound of interval j; j == len(intervals) is above them all)."""
    shares = [0.5 if f is None else f for f in fitted]
    out = []
    for j in range(len(n_cells) + 1):
        below = sum(n * s for n, s in zip(n_cells[:j], shares[:j]))
        above = sum(n * (1.0 - s) for n, s in zip(n_cells[j:], shares[j:]))
        out.append(below + above)
    return out


def between(table, n_cells, a, b, strata=None, missing="half") -> tuple:
    """(own share, effective cells read) of the cells between candidates
    a < b: each interval's share weighted by the cells it holds, an
    interval never read counting one half as if read once."""
    total = float(sum(n_cells[a:b]))
    if not total:
        return 0.5, 0.0
    share, inverse = 0.0, 0.0
    for k in range(a, b):
        if not n_cells[k]:
            continue
        w = n_cells[k] / total
        f, n = estimate(table[k], None if strata is None else strata[k], missing)
        share += w * (0.5 if f is None else f)
        inverse += w * w / max(n, 1.0)
    return share, (1.0 / inverse if inverse else 0.0)


#: A rival that changes fewer cells than this never blocks, however few
#: positives the two candidates have (`support`'s `min_between_share`).
MIN_MATERIAL = 3


def material(min_between, share, positives) -> int:
    """Cells a rival's call must change to block: `min_between`, or fewer
    when that is more than `share` of the positives of the larger of the two
    candidates (a rare population's whole extent can be under twenty cells,
    and whether it exists is exactly what a rival above it disputes)."""
    if not share:
        return int(min_between)
    return min(int(min_between), max(MIN_MATERIAL, math.ceil(share * positives)))


# -- the posterior over candidates (the `posterior` rule) -----------------------------------


def _uniforms(seed, k, part, draws):
    """The uniforms behind interval k's (stratum `part`'s) share draws: fixed
    by the seed and the interval, so a hypothetical read changes only that
    interval's draws (common random numbers) and a replay draws the same."""
    return np.random.default_rng([int(seed) & 0xFFFFFFFF, int(k), int(part)]).random(draws)


def _beta(uniforms, own, not_own):
    from scipy.stats import beta

    return beta.ppf(uniforms, own + 0.5, not_own + 0.5)


#: Share draws already made, by (seed, interval, stratum, draws, own, not
#: own): a draw is a pure function of them, and a look's worth asks for the
#: same ones again and again (`look_gains` per count, `look_gain` per
#: simulation) -- each a few hundred quantiles of a Beta. Read only; bounded.
_DRAWN: OrderedDict = OrderedDict()
_DRAWN_LIMIT = 4096
_DRAWN_LOCK = threading.Lock()


def _beta_draws(seed, k, part, draws, own, not_own):
    """`_beta(_uniforms(seed, k, part, draws), own, not_own)`, remembered."""
    key = (int(seed) & 0xFFFFFFFF, int(k), int(part), int(draws), float(own), float(not_own))
    with _DRAWN_LOCK:
        found = _DRAWN.get(key)
        if found is not None:
            _DRAWN.move_to_end(key)
            return found
    found = _beta(_uniforms(seed, k, part, draws), float(own), float(not_own))
    found.flags.writeable = False
    with _DRAWN_LOCK:
        _DRAWN[key] = found
        while len(_DRAWN) > _DRAWN_LIMIT:
            _DRAWN.popitem(last=False)
    return found


def isotonic(shares, weights):
    """The weighted non-decreasing fit of every row of `shares` (draws x k,
    every weight above zero), all rows at once: f_i is the largest, over
    a <= i, of the smallest mean of a..b over b >= i (the max-min formula of
    isotonic regression; the same fit `monotone` pools to)."""
    shares = np.asarray(shares, dtype=np.float64)
    weights = np.asarray(weights, dtype=np.float64)
    d, k = shares.shape
    if not k:
        return shares
    c = np.concatenate([np.zeros((d, 1)), np.cumsum(shares * weights, axis=1)], axis=1)
    w = np.concatenate([[0.0], np.cumsum(weights)])
    upper = np.triu(np.ones((k, k), dtype=bool))            # [a, b]: a <= b
    den = np.where(upper, w[None, 1:] - w[:-1, None], 1.0)
    mean = np.where(upper, (c[:, None, 1:] - c[:, :-1, None]) / den, np.inf)
    suffix = np.minimum.accumulate(mean[:, :, ::-1], axis=2)[:, :, ::-1]
    return np.where(upper, suffix, -np.inf).max(axis=1)


def _choose(shares, weights, n_cells, eligible):
    """(P(c* = j) per candidate, mean loss per candidate) over draws of the
    interval shares: monotone per draw (read intervals only; an unread one
    stays one half), the candidate with the fewest cells wrong counted."""
    d = shares.shape[0]
    k = len(n_cells)
    read = np.asarray(weights) > 0
    fitted = np.full((d, k), 0.5)
    if read.any():
        fitted[:, read] = isotonic(shares[:, read], np.asarray(weights)[read])
    n = np.asarray(n_cells, dtype=np.float64)
    zeros = np.zeros((d, 1))
    below = np.concatenate([zeros, np.cumsum(fitted * n, axis=1)], axis=1)
    negatives = (1.0 - fitted) * n
    above = negatives.sum(axis=1, keepdims=True) - \
        np.concatenate([zeros, np.cumsum(negatives, axis=1)], axis=1)
    loss = below + above
    if eligible is not None:
        loss[:, ~np.asarray(eligible, dtype=bool)] = np.inf
    # Candidates tied in a draw (only an unread interval between them) share
    # it: an interval nobody read favours no candidate.
    tied = loss <= loss.min(axis=1, keepdims=True) + 1e-9
    p = (tied / tied.sum(axis=1, keepdims=True)).mean(axis=0)
    return p, loss.mean(axis=0)


def _draws(table, n_cells, strata, missing, seed, draws):
    """(shares, weights): each interval's share drawn per stratum read (one
    half where nothing was read) and weighted by the stratum's cells, and
    its effective cells read."""
    k = len(n_cells)
    shares = np.full((draws, k), 0.5)
    weights = np.zeros(k)
    for i, entry in enumerate(table):
        f, n_eff = estimate(entry, None if strata is None else strata[i], missing)
        if f is None:
            continue
        weights[i] = n_eff
        sizes = _strata(entry, None if strata is None else strata[i], missing)
        if not sizes:
            shares[:, i] = _beta_draws(seed, i, 0, draws, entry["own"], entry["not"])
            continue
        total = float(sum(sizes.values()))
        mix = np.zeros(draws)
        for j, (stratum, size) in enumerate(sorted(sizes.items(), key=lambda kv: str(kv[0]))):
            part = entry["by"].get(str(stratum)) or {}
            mix += size / total * _beta_draws(seed, i, j + 1, draws,
                                              part.get("own", 0), part.get("not", 0))
        shares[:, i] = mix
    return shares, weights


def posterior(table, n_cells, *, eligible=None, strata=None, missing="half", seed=0,
              draws=DRAWS) -> tuple:
    """(P(c* = j), mean loss) for every candidate j: the candidate with the
    fewest cells wrong, counted over `draws` draws of the interval shares
    (`_draws`, `_choose`). Ineligible candidates are never chosen."""
    shares, weights = _draws(table, n_cells, strata, missing, seed, draws)
    return _choose(shares, weights, n_cells, eligible)


def equivalence(fits, n_cells, positives, tolerance, min_between, share):
    """[i][j]: candidates i and j are one gate in effect -- within the
    tolerance (fit units), or their calls differ on fewer cells than
    `material`."""
    m = len(fits)
    out = np.zeros((m, m), dtype=bool)
    cum = np.concatenate([[0.0], np.cumsum(np.asarray(n_cells, dtype=np.float64))])
    for i in range(m):
        for j in range(m):
            a, b = sorted((i, j))
            close = abs(fits[i] - fits[j]) <= tolerance + 1e-9
            changed = cum[b] - cum[a]
            out[i, j] = close or changed < material(min_between, share,
                                                    max(positives[i], positives[j]))
    return out


def plateau_leader(p, fits, prior, eligible, tau, same=None, rho=None) -> tuple:
    """(leader, plateau): the plateau is every eligible candidate with
    P >= `tau`, and the most probable one always; the leader is the plateau
    member nearest the prior by value (ties to the lower).

    With `same` (`equivalence`) and `rho`, a plateau member the reads put
    within the tolerance with P >= `rho` leads instead (the nearest the prior
    of those): the prior only breaks ties the reads leave, and a member that
    would resolve is not passed over for one they all but rule out."""
    p = np.asarray(p)
    allowed = [j for j in range(len(p)) if eligible is None or eligible[j]]
    best = max(allowed, key=lambda j: (p[j], -j))
    plateau = sorted({j for j in allowed if p[j] >= tau - 1e-12} | {best})
    target = fits[prior]
    pool = plateau
    if same is not None and rho is not None:
        pool = [j for j in plateau if float(same[j] @ p) >= rho - 1e-12] or plateau
    leader = min(pool, key=lambda j: (abs(fits[j] - target), j))
    return leader, plateau


def monotone_fit(table) -> dict:
    """How far the reads are from one non-decreasing share: the binomial
    deviance of the pooled fit against each interval's own share, its
    degrees of freedom (intervals read minus pooled blocks) and a chi-square
    p (conservative for an order-restricted fit). A record, never a
    decision: a reader's slips and a neighbour's spill both break the order."""
    from scipy.stats import chi2

    own = [float(c["own"]) for c in table]
    read = [float(c["own"] + c["not"]) for c in table]
    raw = [o / r if r else 0.0 for o, r in zip(own, read)]
    fitted = monotone(raw, read)
    deviance, blocks, last = 0.0, 0, None
    for o, r, p, f in zip(own, read, raw, fitted):
        if not r:
            continue
        if last is None or abs(f - last) > 1e-12:
            blocks += 1
        last = f
        f = min(max(f, 1e-9), 1 - 1e-9)
        if o:
            deviance += 2 * o * math.log(p / f)
        if r - o:
            deviance += 2 * (r - o) * math.log((1 - p) / (1 - f))
    df = sum(1 for r in read if r) - blocks
    pvalue = float(chi2.sf(deviance, df)) if df > 0 else 1.0
    return {"deviance": round(deviance, 3), "df": int(df), "p": round(pvalue, 4)}


def support(bounds, n_cells, observations, *, prior, to_fit, tolerance,
            resolve_within=None, min_between=0, min_between_share=0.0,
            n_above=0, calls=None, strata=None, rule="wilson", posterior_opts=None) -> dict:
    """The leader among the candidates `bounds` (sorted table values) and,
    for every rival outside `tolerance`, whether the leader beats it.

    `prior` is the candidate index a tie goes to (the scored start).
    `resolved` asks the leader to beat every rival farther than
    `resolve_within` (default `tolerance`): a rival nearer than that may stay
    undecided, since where the stain truly starts can lie between two close
    candidates, and the cells between them are then mixed by nature. A
    rival whose call differs from the leader's on fewer than `min_between`
    cells is the same gate in effect and never blocks -- unless those cells
    are more than `min_between_share` of the larger candidate's positives
    (`material`); `n_above` is the cells above the highest bound, positive
    under every candidate. `calls` ({cell id: 0/1}) and `strata` (per
    interval {0: cells, 1: cells}) post-stratify by a classifier's calls.

    `rule="posterior"` decides by the posterior over candidates instead
    (`posterior_opts`: `tau`, `rho`, `draws`, `seed`, `eligible` per bound,
    `key` -- a cell field to stratify on, with `strata` its sizes per
    interval; `leader` -- "prior" or "resolving", `plateau_leader`): the
    leader is the plateau member nearest the prior, resolved when P(within
    the tolerance of it, or the same gate in effect) >= `rho`.
    The rivals' Wilson intervals are still reported."""
    resolve_within = tolerance if resolve_within is None else resolve_within
    opts = dict(posterior_opts or {})
    key = opts.get("key") if rule == "posterior" else None
    missing = "pooled" if key is not None else "half"
    table = counts(bounds, observations, calls if strata is not None and key is None else None,
                   key=key if strata is not None else None)
    fitted = fractions(table, strata, missing)
    loss = losses(n_cells, fitted)
    fits = [float(to_fit(b)) for b in bounds]
    positives = [int(sum(n_cells[j:])) + int(n_above) for j in range(len(bounds))]
    block = None
    if rule == "posterior":
        eligible = opts.get("eligible")
        seed = int(opts.get("seed") or 0)
        p, mean_loss = posterior(table, n_cells, eligible=eligible, strata=strata,
                                 missing=missing, seed=seed,
                                 draws=int(opts.get("draws") or DRAWS))
        same = equivalence(fits, n_cells, positives, tolerance, min_between,
                           min_between_share)
        resolving = opts.get("leader") == "resolving"
        leader, plateau = plateau_leader(p, fits, prior, eligible, float(opts.get("tau", 0.1)),
                                         same=same if resolving else None,
                                         rho=float(opts.get("rho", 0.8)) if resolving else None)
        within = float(same[leader] @ p)
        best = min(mean_loss[j] for j in plateau)
        block = {"p": [round(float(v), 4) for v in p], "within": round(within, 4),
                 "max_p": round(float(p.max()), 4), "seed": seed,
                 "draws": int(opts.get("draws") or DRAWS),
                 "plateau": [{"index": j, "low": float(bounds[j]), "fit": round(fits[j], 6),
                              "p": round(float(p[j]), 4),
                              "loss_gap": round(float(mean_loss[j] - best), 2)}
                             for j in plateau],
                 "non_monotone": monotone_fit(table)}
        if eligible is not None and not all(eligible):
            block["ineligible"] = [float(bounds[j]) for j, ok in enumerate(eligible) if not ok]
    else:
        best = min(loss)
        tied = [j for j, value in enumerate(loss) if value <= best + 1e-9]
        leader = min(tied, key=lambda j: (abs(j - prior), j))
    rivals = []
    for j in range(len(bounds)):
        if j == leader or abs(fits[j] - fits[leader]) <= tolerance + 1e-9:
            continue
        a, b = sorted((leader, j))
        own = sum(c["own"] for c in table[a:b])
        read = sum(c["own"] + c["not"] for c in table[a:b])
        share, n_eff = between(table, n_cells, a, b, strata, missing)
        low, high = wilson(share * n_eff, n_eff)
        # The leader calls the cells between them positive when it is the
        # lower of the two, negative when it is the higher.
        decisive = low > 0.5 if leader < j else high < 0.5
        rivals.append({"index": j, "low": float(bounds[j]), "own": own, "read": read,
                       "share": round(share, 4), "n_eff": round(n_eff, 2),
                       "ci": [round(low, 4), round(high, 4)], "decisive": bool(decisive),
                       "n_between": int(sum(n_cells[a:b])),
                       "material": material(min_between, min_between_share,
                                            max(positives[leader], positives[j])),
                       "distance": round(abs(fits[j] - fits[leader]), 4),
                       **({"p": block["p"][j]} if block else {})})
    labelled = sum(c["own"] + c["not"] + c["other"] for c in table)
    other = sum(c["other"] for c in table)
    if block is not None:
        resolved = block["within"] >= float(opts.get("rho", 0.8)) - 1e-12
        # While unresolved, the plateau's members that are another gate in
        # effect are what the looks must tell apart.
        in_plateau = {m["index"] for m in block["plateau"]}
        blocking = [] if resolved else \
            [r for r in rivals if r["index"] in in_plateau and r["n_between"] >= r["material"]]
    else:
        blocking = [r for r in rivals if not r["decisive"]
                    and r["distance"] > resolve_within + 1e-9
                    and r["n_between"] >= r["material"]]
        resolved = not blocking
    out = {"leader": leader, "low": float(bounds[leader]), "positives": positives[leader],
           "loss": [round(v, 3) for v in loss],
           "fractions": [None if f is None else round(f, 4) for f in fitted],
           "table": [{k: c[k] for k in ("own", "not", "other", "uncertain", "artifact")}
                     for c in table],
           "rivals": rivals,
           "undecided": [r for r in rivals if not r["decisive"]],
           "blocking": blocking, "resolved": resolved,
           "labelled": labelled, "other_share": (other / labelled) if labelled else None,
           "rule": rule}
    if block is not None:
        out["posterior"] = block
    if key is not None:
        out["strata_reads"] = _strata_reads(table)
    return out


def _strata_reads(table) -> dict:
    """Labels per stratum over the range, and each stratum's uncertain share."""
    out = {}
    for entry in table:
        for stratum, part in entry["by"].items():
            total = out.setdefault(stratum, _tally())
            for name in total:
                total[name] += int(part.get(name, 0))
    for total in out.values():
        labelled = total["own"] + total["not"] + total["other"]
        total["uncertain_share"] = round(total["other"] / labelled, 4) if labelled else None
    return out


# -- what a look could still change (the `voi` sampling) ------------------------------------


def _pooled(table, n_cells, seed, draws):
    """Unstratified draws (one Beta per interval) and plain reads as weights."""
    return _draws(table, n_cells, None, "half", seed, draws)


def look_gains(table, n_cells, available, *, eligible, same, seed=0, draws=DRAWS,
               batch=8, definite=1.0) -> tuple:
    """(best, gains): the best P(within the tolerance) over candidates now,
    and per interval the expected rise of it after `batch` more cells read
    there (fewer when fewer are unshown; times `definite`, the share of
    cells read either way so far), clipped at zero -- 0 for an interval with
    nothing unshown. P(within) of one fixed leader cannot rise in
    expectation (a posterior probability is a martingale); a look's worth
    is that it can make some candidate surer, which the best of them
    measures. The interval's other draws are held fixed (common random
    numbers), so a gain is not draw noise."""
    from scipy.stats import betabinom

    shares, weights = _pooled(table, n_cells, seed, draws)
    p, _ = _choose(shares, weights, n_cells, eligible)
    best = float((same @ p).max())
    gains = [0.0] * len(n_cells)
    for k, free in enumerate(available):
        m = int(round(min(batch, free) * definite))
        if m <= 0:
            continue
        own, not_own = float(table[k]["own"]), float(table[k]["not"])
        pmf = betabinom.pmf(np.arange(m + 1), m, own + 0.5, not_own + 0.5)
        keep_share, keep_weight = shares[:, k].copy(), weights[k]
        expected = 0.0
        for x in range(m + 1):
            shares[:, k] = _beta_draws(seed, k, 0, draws, own + x, not_own + m - x)
            weights[k] = own + not_own + m
            q, _ = _choose(shares, weights, n_cells, eligible)
            expected += float(pmf[x]) * float((same @ q).max())
        shares[:, k], weights[k] = keep_share, keep_weight
        gains[k] = max(0.0, expected - best)
    return best, gains


def look_gain(table, n_cells, picks, *, eligible, same, seed=0, draws=DRAWS,
              simulations=200, definite=1.0) -> float:
    """The expected rise of the best P(within the tolerance) after a whole
    look reading `picks` cells per interval: looks simulated from the
    posterior predictive (a share drawn per interval, its reads binomial),
    clipped at zero."""
    shares, weights = _pooled(table, n_cells, seed, draws)
    p, _ = _choose(shares, weights, n_cells, eligible)
    best = float((same @ p).max())
    rng = np.random.default_rng([int(seed) & 0xFFFFFFFF, 7919])
    chosen = [(k, int(round(n * definite))) for k, n in enumerate(picks)]
    chosen = [(k, m) for k, m in chosen if m > 0]
    if not chosen:
        return 0.0
    base = {k: (shares[:, k].copy(), weights[k]) for k, _m in chosen}
    total = 0.0
    for _ in range(int(simulations)):
        for k, m in chosen:
            own, not_own = float(table[k]["own"]), float(table[k]["not"])
            x = int(rng.binomial(m, rng.beta(own + 0.5, not_own + 0.5)))
            shares[:, k] = _beta_draws(seed, k, 0, draws, own + x, not_own + m - x)
            weights[k] = own + not_own + m
        q, _ = _choose(shares, weights, n_cells, eligible)
        total += float((same @ q).max())
    for k, (column, weight) in base.items():
        shares[:, k], weights[k] = column, weight
    return max(0.0, total / int(simulations) - best)
