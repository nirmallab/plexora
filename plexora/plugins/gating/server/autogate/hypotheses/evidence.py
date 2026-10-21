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
"""

from __future__ import annotations

import math

OWN = "own_stain"
NOT_OWN = "not_own_stain"
LABELS = (OWN, NOT_OWN, "uncertain", "artifact")

#: [cal] the two-sided normal quantile of the Wilson intervals (95 %).
Z = 1.96


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


def _stratum(cell, calls):
    if calls is None:
        return None
    return calls.get(str(int(cell["cell_id"])), calls.get(int(cell["cell_id"])))


def counts(bounds, observations, calls=None) -> list:
    """Per interval: own, not_own and other labels of the valid observations'
    cells (sentinels excluded), in total and per stratum (`by`: the
    classifier's call of each cell when `calls` is given, else one)."""
    out = [{"own": 0, "not": 0, "other": 0, "by": {}} for _ in range(max(0, len(bounds) - 1))]
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
            key = "own" if label == OWN else "not" if label == NOT_OWN else "other"
            out[k][key] += 1
            part = out[k]["by"].setdefault(str(_stratum(cell, calls)),
                                           {"own": 0, "not": 0, "other": 0})
            part[key] += 1
    return out


def estimate(entry, sizes=None) -> tuple:
    """(own share, effective cells read) of one interval: the share in each
    stratum weighted by the stratum's cells (`sizes`, {stratum: cells}), a
    stratum never read counting one half as if read once; without `sizes`,
    the plain share. (None, 0) when nothing was read either way."""
    read = entry["own"] + entry["not"]
    if not read:
        return None, 0.0
    if not sizes:
        return entry["own"] / read, float(read)
    total = float(sum(sizes.values())) or 1.0
    share, inverse = 0.0, 0.0
    for stratum, size in sizes.items():
        if not size:
            continue
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


def fractions(table, strata=None) -> list:
    """Each interval's own-stain share (None unread), made non-decreasing,
    each weighted by its effective cells read."""
    pairs = [estimate(entry, None if strata is None else strata[k])
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


def between(table, n_cells, a, b, strata=None) -> tuple:
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
        f, n = estimate(table[k], None if strata is None else strata[k])
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


def support(bounds, n_cells, observations, *, prior, to_fit, tolerance,
            resolve_within=None, min_between=0, min_between_share=0.0,
            n_above=0, calls=None, strata=None) -> dict:
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
    interval {0: cells, 1: cells}) post-stratify by a classifier's calls."""
    resolve_within = tolerance if resolve_within is None else resolve_within
    table = counts(bounds, observations, calls if strata is not None else None)
    fitted = fractions(table, strata)
    loss = losses(n_cells, fitted)
    best = min(loss)
    tied = [j for j, value in enumerate(loss) if value <= best + 1e-9]
    leader = min(tied, key=lambda j: (abs(j - prior), j))
    fits = [float(to_fit(b)) for b in bounds]
    positives = [int(sum(n_cells[j:])) + int(n_above) for j in range(len(bounds))]
    rivals = []
    for j in range(len(bounds)):
        if j == leader or abs(fits[j] - fits[leader]) <= tolerance + 1e-9:
            continue
        a, b = sorted((leader, j))
        own = sum(c["own"] for c in table[a:b])
        read = sum(c["own"] + c["not"] for c in table[a:b])
        share, n_eff = between(table, n_cells, a, b, strata)
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
                       "distance": round(abs(fits[j] - fits[leader]), 4)})
    labelled = sum(c["own"] + c["not"] + c["other"] for c in table)
    other = sum(c["other"] for c in table)
    blocking = [r for r in rivals if not r["decisive"]
                and r["distance"] > resolve_within + 1e-9
                and r["n_between"] >= r["material"]]
    return {"leader": leader, "low": float(bounds[leader]), "positives": positives[leader],
            "loss": [round(v, 3) for v in loss],
            "fractions": [None if f is None else round(f, 4) for f in fitted],
            "table": [{k: c[k] for k in ("own", "not", "other")} for c in table],
            "rivals": rivals,
            "undecided": [r for r in rivals if not r["decisive"]],
            "blocking": blocking, "resolved": not blocking,
            "labelled": labelled, "other_share": (other / labelled) if labelled else None}
