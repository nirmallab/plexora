"""Segmentation QC's one framework: DNA peaks against the mask's labels, on
the labels' adjacency graph.

1. **Scale** -- the nuclear size comes from the DNA, by scale selection
   (`nuclear_sigma`), never from the labels: a whole-cell mask's labels are
   twice a nucleus across, and smoothing at their size erases the gaps
   between packed nuclei.
2. **Peaks** -- robust DNA maxima, found once: a difference of Gaussians at
   the nuclear scale over log intensity, local maxima above one global floor
   (`tau`, from the sizing sample, so no tile decides its own threshold).
   Only then is each peak given the label under it. Brightness, DNA mass and
   the DNA along a boundary are read on a finer plane (half the scale).
3. **Graph** -- the labels' boundary adjacency, from right / down neighbour
   pairs (every boundary counted once, by the tile owning its first pixel):
   shared boundary length and the brightest DNA along it. Sparse by
   construction; nothing is all-pairs.
4. **Context** -- each cell's 12 nearest neighbours: the local object size,
   how many neighbours have two peaks too, and the DNA mass of a typical
   nucleus there, so a dense lymph node is judged against itself, not
   against a sparse stroma.
5. **Scores** -- under-segmentation per label (two well-separated, similar,
   deeply separated peaks in one label that is large for its neighbourhood).
   Over-segmentation per edge, as a product of necessary conditions: the cut
   runs through DNA as bright as the stronger side's nucleus, the weaker
   side has no nucleus of its own, it holds a real share of the DNA, the two
   together hold one nucleus's worth (not two), and the cut is a real side
   of it. A dim nucleus beside a bright one is not evidence. A nucleus too
   big for the pair test (one large nucleus cut into several labels) is the
   blob rule's (`big_scores`): a coarse DNA blob with no nuclear-scale
   structure inside, crossed by label lines. Ambiguous evidence scores low:
   the overlay should be right more than it is complete.

Tiles are only for I/O and memory. Each is read with a halo wider than a
label, peaks are detected over the whole haloed tile, and a label's peak
statistics are taken in the tile that owns its strongest peak -- every label
whose peaks all fall in the haloed tile is therefore judged exactly as if the
image were one array.
"""

from __future__ import annotations

import numpy as np

#: How far apart two peaks are compared, in samples along the segment.
SEGMENT_SAMPLES = 9
#: The neighbourhood of a cell.
NEIGHBOURS = 12
#: The scale-selection ladder (px): 1.5 * 2 ** (k / 2), k = -1..8. Its ends
#: cannot be maxima in scale, so 1.06 px is there for 1.5 px to be found.
LADDER = tuple(1.5 * 2 ** (k / 2) for k in range(-1, 9))
#: The big-nucleus blob scales, as multiples of the run's sigma.
COARSE = (2.0, 3.5)
#: A boundary shorter than this (px at the run level) is a touch, not a cut.
MIN_CUT_PX = 4.0
#: Status codes and words.
PASS, UNDER, OVER, AMBIGUOUS = 0, 1, 2, 3
STATUS_WORDS = ("pass", "under_segmented", "over_segmented", "ambiguous")


def ramp(value, low, high):
    """0 below `low`, 1 above `high`, linear between."""
    return np.clip((np.asarray(value, dtype=np.float64) - low) / (high - low), 0.0, 1.0)


def smooth_planes(dna, sigma):
    """(s, dog): the log DNA smoothed at the nuclear scale, and its difference
    of Gaussians (a nucleus-sized blob detector)."""
    from scipy import ndimage

    logged = np.log1p(np.maximum(np.asarray(dna, dtype=np.float32), 0))
    s = ndimage.gaussian_filter(logged, sigma)
    wide = ndimage.gaussian_filter(logged, 1.6 * sigma)
    return s.astype(np.float32), (s - wide).astype(np.float32)


def tile_planes(dna, sigma, fine, coarse=COARSE):
    """The planes a tile is judged on: `fine` (the log DNA smoothed at
    `fine`, where brightness, mass and boundaries are read), `dog` (the
    nuclear-scale blob detector) and `coarse` [(sigma_c, dog_c)] (the
    big-nucleus detectors)."""
    from scipy import ndimage

    logged = np.log1p(np.maximum(np.asarray(dna, dtype=np.float32), 0))
    dog = (ndimage.gaussian_filter(logged, sigma)
           - ndimage.gaussian_filter(logged, 1.6 * sigma)).astype(np.float32)
    smooth = ndimage.gaussian_filter(logged, fine).astype(np.float32)
    wide = []
    for factor in coarse:
        sc = factor * sigma
        wide.append((sc, (ndimage.gaussian_filter(logged, sc)
                          - ndimage.gaussian_filter(logged, 1.6 * sc)).astype(np.float32)))
    return smooth, dog, wide


def nuclear_sigma(blocks, ladder=LADDER, min_peaks=50):
    """(sigma, n labels voting, histogram) by scale selection over (dna,
    labels) blocks: the scale-space maxima of the DoG ladder (in space and
    scale, on a label), one vote per label -- its strongest maximum's scale
    -- and the scale most labels vote for (the ladder's mode, refined by a
    parabola through its two neighbours in log scale). One vote per label,
    not per maximum: chromatin texture has many weak fine-scale maxima in
    every nucleus and would outvote the nuclei. The mode, not the median:
    clumps of small nuclei are blobs at coarse scales too. sigma is None when
    fewer than `min_peaks` labels vote (an empty or unstained sample: the
    caller falls back to the labels' size)."""
    from scipy import ndimage

    votes, strengths = [], []
    for dna, labels in blocks:
        logged = np.log1p(np.maximum(np.asarray(dna, dtype=np.float32), 0))
        stack = np.stack([ndimage.gaussian_filter(logged, s)
                          - ndimage.gaussian_filter(logged, 1.6 * s) for s in ladder])
        top = ndimage.maximum_filter(stack, size=(3, 5, 5), mode="nearest")
        hit = (stack == top) & (labels[None] > 0) & (stack > 0)
        # The ends of the ladder cannot be maxima in scale.
        hit[0] = False
        hit[-1] = False
        k, ys, xs = np.nonzero(hit)
        strength = stack[k, ys, xs]
        label = labels[ys, xs].astype(np.int64)
        order = np.lexsort((xs, ys, k, -strength, label))
        label, k, strength = label[order], k[order], strength[order]
        first = np.r_[True, label[1:] != label[:-1]] if label.size else np.zeros(0, bool)
        votes.append(k[first])
        strengths.append(strength[first])
    if not votes:
        return None, 0, [0] * len(ladder)
    k = np.concatenate(votes)
    strength = np.concatenate(strengths)
    if k.size:
        # A label whose best is a fifth of the typical one has no nucleus.
        k = k[strength >= 0.2 * float(np.median(strength))]
    histogram = np.bincount(k, minlength=len(ladder))
    if k.size < min_peaks:
        return None, int(k.size), histogram.tolist()
    # argmax takes the first of a tie: the finer scale.
    mode = int(np.argmax(histogram))
    offset = 0.0
    if 0 < mode < len(ladder) - 1:
        a, b, c = (float(v) for v in histogram[mode - 1:mode + 2])
        if a - 2 * b + c < 0:
            offset = 0.5 * (a - c) / (a - 2 * b + c)
    step = np.log(ladder[1] / ladder[0])
    sigma = float(ladder[mode] * np.exp(offset * step))
    return sigma, int(k.size), histogram.tolist()


def noise_floor(strengths, floor=0.05, fraction=0.2):
    """tau: a fifth of the typical nuclear peak in the sizing sample (the DoG
    maxima that fall on labels), and never below `floor` (5 % contrast in log
    intensity). Relative on purpose: the DoG's spread over a whole field is
    the tissue's structure, not its noise, and a threshold taken from it
    would drown the nuclei it is meant to find."""
    values = np.concatenate([np.asarray(v, dtype=np.float64).ravel() for v in strengths])         if strengths else np.zeros(0)
    values = values[values > 0]
    if not values.size:
        return floor
    return max(floor, fraction * float(np.median(values)))


def tile_peaks(dog, labels, tau, radius):
    """(y, x, strength) of the local maxima of `dog` at or above `tau` that
    fall on a label (anywhere, with `labels` None), over the whole tile (halo
    included)."""
    from scipy import ndimage

    size = 2 * int(round(radius)) + 1
    top = ndimage.maximum_filter(dog, size=size, mode="nearest")
    hit = (dog == top) & (dog >= tau)
    if labels is not None:
        hit &= labels > 0
    ys, xs = np.nonzero(hit)
    return ys, xs, dog[ys, xs]


def label_peaks(ys, xs, strength, labels, s, robust_ratio):
    """Per label in the tile, from its peaks: the strongest (p1), the
    strongest other robust one (p2), their separation and the depth of the
    valley between them. Returns a dict of arrays keyed by label, sorted by
    label; every array is aligned with `label`."""
    from scipy import ndimage

    if ys.size == 0:
        empty = np.zeros(0)
        return {"label": np.zeros(0, dtype=np.int64), "n": np.zeros(0, dtype=np.int32),
                "p1": empty, "p2": empty, "y1": empty, "x1": empty, "sep2": empty,
                "depth2": empty}
    label = labels[ys, xs].astype(np.int64)
    # Label, then strongest first, then position: deterministic.
    order = np.lexsort((xs, ys, -strength, label))
    label, ys, xs, strength = label[order], ys[order], xs[order], strength[order]
    starts = np.flatnonzero(np.r_[True, label[1:] != label[:-1]])
    counts = np.diff(np.r_[starts, label.size])
    p1 = strength[starts]
    robust = strength >= robust_ratio * np.repeat(p1, counts)
    n = np.add.reduceat(robust.astype(np.int32), starts)
    has2 = counts >= 2
    second = starts + 1
    p2 = np.where(has2, strength[np.minimum(second, label.size - 1)], 0.0)
    y1, x1 = ys[starts].astype(np.float64), xs[starts].astype(np.float64)
    y2 = np.where(has2, ys[np.minimum(second, label.size - 1)], ys[starts]).astype(np.float64)
    x2 = np.where(has2, xs[np.minimum(second, label.size - 1)], xs[starts]).astype(np.float64)
    sep2 = np.hypot(y2 - y1, x2 - x1)
    depth2 = np.zeros(starts.size)
    pairs = np.flatnonzero(has2)
    if pairs.size:
        t = np.linspace(0.0, 1.0, SEGMENT_SAMPLES)
        sy = y1[pairs, None] + (y2[pairs] - y1[pairs])[:, None] * t[None, :]
        sx = x1[pairs, None] + (x2[pairs] - x1[pairs])[:, None] * t[None, :]
        along = ndimage.map_coordinates(s, [sy.ravel(), sx.ravel()], order=1,
                                        mode="nearest").reshape(sy.shape)
        linear = np.expm1(np.maximum(along, 0.0))
        ends = np.minimum(linear[:, 0], linear[:, -1])
        valley = linear.min(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            depth2[pairs] = np.clip(1.0 - valley / np.maximum(ends, 1e-6), 0.0, 1.0)
    return {"label": label[starts], "n": n.astype(np.int32), "p1": p1.astype(np.float64),
            "p2": p2.astype(np.float64), "y1": y1, "x1": x1, "sep2": sep2, "depth2": depth2}


def blob_candidates(coarse, dog, labels, tau, window):
    """Big-nucleus candidates in a tile: maxima of a coarse DoG (blob radius
    R = 1.28 * sqrt(2) * sigma_c, suppressed within half a radius) on a
    label, at or above `tau`, with **no nuclear-scale structure inside** --
    the run-scale DoG's maximum within 0.8 R is under half the coarse
    response (a clump of small nuclei is a coarse blob too, but one full of
    small ones). Only candidates whose centre lies in the tile's interior
    `window` are returned (so each is found once): (y, x, R) in tile pixels."""
    from scipy import ndimage

    iy0, iy1, ix0, ix1 = window
    out_y, out_x, out_r = [], [], []
    for sc, dog_c in coarse:
        radius = 1.28 * np.sqrt(2.0) * sc
        size = 2 * int(round(0.5 * radius)) + 1
        top = ndimage.maximum_filter(dog_c, size=size, mode="nearest")
        hit = (dog_c == top) & (dog_c >= tau) & (labels > 0)
        ys, xs = np.nonzero(hit)
        inside = (ys >= iy0) & (ys < iy1) & (xs >= ix0) & (xs < ix1)
        ys, xs = ys[inside], xs[inside]
        if not ys.size:
            continue
        inner = ndimage.maximum_filter(dog, size=2 * int(round(0.8 * radius)) + 1,
                                       mode="nearest")
        plain = inner[ys, xs] < 0.5 * dog_c[ys, xs]
        out_y.append(ys[plain])
        out_x.append(xs[plain])
        out_r.append(np.full(int(plain.sum()), radius))
    if not out_y:
        z = np.zeros(0)
        return z, z, z
    return (np.concatenate(out_y).astype(np.float64), np.concatenate(out_x).astype(np.float64),
            np.concatenate(out_r))


def tile_background(fine, window):
    """The tile's DNA background (linear): the 10th percentile of the smoothed
    DNA over the interior window. Masses are only compared between
    neighbours, which share a tile or nearly, so a per-tile level is enough."""
    iy0, iy1, ix0, ix1 = window
    part = fine[iy0:iy1, ix0:ix1]
    if not part.size:
        return 0.0
    return float(np.expm1(np.percentile(part, 10)))


class EdgeAccumulator:
    """Label-label boundary pairs, reduced to one row per edge as they come:
    (lo, hi) -> pixel pairs, and the brightest DNA of any pair (order
    independent, so tiling never changes it)."""

    def __init__(self, compact_every=2_000_000):
        self.parts = []
        self.pending = 0
        self.compact_every = compact_every
        self.reduced = None

    def add(self, lo, hi, value, *_midpoints):
        if lo.size == 0:
            return
        self.parts.append((lo, hi, np.ones(lo.size), np.asarray(value, dtype=np.float64)))
        self.pending += lo.size
        if self.pending >= self.compact_every:
            self.compact()

    def compact(self):
        parts = self.parts + ([self.reduced] if self.reduced is not None else [])
        self.parts, self.pending = [], 0
        if not parts:
            return
        lo = np.concatenate([p[0] for p in parts])
        hi = np.concatenate([p[1] for p in parts])
        m = int(max(hi.max(), 1)) + 1
        key = lo * m + hi
        unique, inverse = np.unique(key, return_inverse=True)
        inverse = inverse.ravel()
        length = np.bincount(inverse, weights=np.concatenate([p[2] for p in parts]),
                             minlength=unique.size)
        value = np.concatenate([p[3] for p in parts])
        order = np.argsort(inverse, kind="stable")
        starts = np.flatnonzero(np.r_[True, inverse[order][1:] != inverse[order][:-1]])
        top = np.maximum.reduceat(value[order], starts)
        self.reduced = (unique // m, unique % m, length, top)

    def result(self):
        self.compact()
        if self.reduced is None:
            z = np.zeros(0)
            return {"a": np.zeros(0, dtype=np.int64), "b": np.zeros(0, dtype=np.int64),
                    "length": z, "max": z}
        a, b, length, top = self.reduced
        return {"a": a.astype(np.int64), "b": b.astype(np.int64), "length": length,
                "max": top}


def _median_where(values, keep):
    """Per row, the median of `values` where `keep` (NaN where none is):
    nanmedian without its all-NaN warning, by sorting the rest to the end."""
    ranked = np.sort(np.where(keep, values, np.inf), axis=1)
    counts = keep.sum(axis=1)
    rows = np.arange(values.shape[0])
    lo = ranked[rows, np.maximum(counts - 1, 0) // 2]
    hi = ranked[rows, np.maximum(counts, 1) // 2]
    return np.where(counts > 0, 0.5 * (lo + hi), np.nan)


def context(cy, cx, area, n_peaks, p1, excess, k=NEIGHBOURS):
    """Per cell, over its 12 nearest neighbours: the median area, the share
    with two robust peaks, and, over those that have a peak, the median DNA
    mass (`mass_local`, a typical whole nucleus there; over all of them where
    none has a peak) and the median peak strength (`peak_local`, 0 where none
    has a peak)."""
    from scipy.spatial import cKDTree

    n = cy.size
    if n == 0:
        z = np.zeros(0)
        return {"area_local": z, "multi_local": z, "mass_local": z, "peak_local": z}
    kk = min(k + 1, n)
    points = np.column_stack([cx, cy])
    _dist, index = cKDTree(points).query(points, k=kk)
    index = np.atleast_2d(index).reshape(n, kk)
    hood = index[:, 1:] if kk > 1 else index
    area_local = np.median(area[hood], axis=1)
    multi_local = (n_peaks[hood] >= 2).mean(axis=1)
    peaked = p1[hood] > 0
    mass_local = _median_where(excess[hood], peaked)
    mass_local = np.where(np.isnan(mass_local), np.median(excess[hood], axis=1), mass_local)
    peak_local = np.nan_to_num(_median_where(p1[hood], peaked))
    return {"area_local": area_local, "multi_local": multi_local, "mass_local": mass_local,
            "peak_local": peak_local}


def under_scores(cells, hood):
    """A label holding two well-separated, similar, deeply separated nuclei,
    large for its neighbourhood; damped where two-peak labels are common (a
    region whose nuclei are lobed or textured says so by being common). The
    valley is necessary, not just weighed: at the nuclear scale an elongated
    nucleus has two maxima along its length with no dip between them."""
    d_local = 2.0 * np.sqrt(np.maximum(hood["area_local"], 1.0) / np.pi)
    with np.errstate(divide="ignore", invalid="ignore"):
        sep = ramp(cells["sep2"] / d_local, 0.6, 1.0)
        similar = ramp(np.where(cells["p1"] > 0, cells["p2"] / cells["p1"], 0.0), 0.35, 0.7)
        large = ramp(cells["area"] / np.maximum(hood["area_local"], 1.0), 1.2, 2.0)
    deep = ramp(cells["depth2"], 0.15, 0.5)
    score = (cells["n_peaks"] >= 2) * ramp(cells["depth2"], 0.15, 0.35) * \
        (0.30 * sep + 0.30 * deep + 0.20 * similar + 0.20 * large)
    return score * (1.0 - 0.5 * ramp(hood["multi_local"], 0.25, 0.6))


def over_scores(cells, hood, edges, index_of, d_nuc):
    """Per edge, the evidence that its weaker side is a fragment of the
    stronger side's nucleus; per cell, the strongest such edge. Returns
    (over score per cell, partner id per cell or 0).

    `strong` is the side with a peak when only one has, else the brighter.
    Each factor is a necessary condition, so any one failing clears the edge:

    - the stronger side has a nucleus, and the cut is more than a touch;
    - the brightest DNA on the cut is most of the stronger side's brightest:
      the cut runs through a nucleus, not through the cytoplasm between two
      (a dim nucleus beside a bright one is not a fragment of it);
    - not two nuclei, one each (both sides have a peak, a nucleus apart);
    - the weaker side holds a real share of the pair's DNA mass (a rim that
      caught a nucleus's glow is not a piece of it);
    - the two together hold about one nucleus's DNA for the neighbourhood,
      not two;
    - and, softening only, the cut is a real side of the weaker label."""
    n = cells["label"].size
    over = np.zeros(n)
    partner = np.zeros(n, dtype=np.int64)
    if edges["a"].size == 0 or n == 0:
        return over, partner
    ia, ib = index_of(edges["a"]), index_of(edges["b"])
    keep = (ia >= 0) & (ib >= 0)
    ia, ib = ia[keep], ib[keep]
    e_max, e_len = edges["max"][keep], edges["length"][keep]
    has = cells["p1"] > 0
    max_dna, excess = cells["max_dna"], cells["excess"]
    one = has[ia] ^ has[ib]
    a_strong = np.where(one, has[ia], max_dna[ia] >= max_dna[ib])
    strong = np.where(a_strong, ia, ib)
    weak = np.where(a_strong, ib, ia)
    gate = has[strong] & (e_len >= MIN_CUT_PX)
    cross = np.expm1(e_max) / np.maximum(np.expm1(max_dna[strong]), 1e-6)
    sep = np.hypot(cells["y1"][ia] - cells["y1"][ib], cells["x1"][ia] - cells["x1"][ib])
    two = has[weak] * ramp(sep / max(float(d_nuc), 1e-6), 0.6, 0.9)
    pair_mass = excess[weak] + excess[strong]
    share = excess[weak] / np.maximum(pair_mass, 1e-6)
    pair = pair_mass / np.maximum(hood["mass_local"][weak], 1e-6)
    cut = e_len / np.maximum(cells["perimeter"][weak], 1.0)
    score = (gate * ramp(cross, 0.35, 0.70) * (1.0 - two) * ramp(share, 0.15, 0.35)
             * (1.0 - ramp(pair, 1.15, 1.60)) * (0.6 + 0.4 * ramp(cut, 0.12, 0.35)))
    # Strongest edge per weak cell: sort by (weak, score) and keep the last.
    order = np.lexsort((score, weak))
    last = np.r_[weak[order][1:] != weak[order][:-1], True]
    chosen = order[last]
    over[weak[chosen]] = score[chosen]
    partner[weak[chosen]] = cells["label"][strong[chosen]]
    return over, partner


def big_scores(cells, hood, blobs):
    """The blob rule: per cell, the evidence that it is a piece of a big
    nucleus cut into several labels. `blobs` = (y, x, R) of the big-nucleus
    candidates (`blob_candidates`, global level pixels). A blob's members are
    the labels whose centroid lies within 0.9 R; the member nearest the
    centre is its home, and every other member is a piece of it when it holds
    a real share of the blob's DNA, is nearly as bright as the home, and has
    no nucleus-strength peak of its own. A piece of one big nucleus shows at
    the nuclear scale only as the weak maxima of its rim or its texture (well
    under half its neighbours' peaks); a band of packed nuclei (a gland's
    wall) is a coarse blob too, but each of its labels has a peak as strong
    as any nucleus around it. Returns (score per cell, home id per cell or 0,
    how many blobs had a piece)."""
    from scipy.spatial import cKDTree

    n = cells["label"].size
    score = np.zeros(n)
    partner = np.zeros(n, dtype=np.int64)
    by, bx, br = blobs
    if n < 2 or not np.size(by):
        return score, partner, 0
    tree = cKDTree(np.column_stack([cells["cy"], cells["cx"]]))
    excess, bright = cells["excess"], np.expm1(cells["max_dna"])
    own = cells["p1"] / np.maximum(hood["peak_local"], 1e-9)
    weak_peak = 1.0 - ramp(np.where(hood["peak_local"] > 0, own, 0.0), 0.45, 0.65)
    found = 0
    # Blobs in position order, and a later blob wins a cell only on a strictly
    # higher score: deterministic whatever order the tiles found them in.
    for j in np.lexsort((bx, by)):
        members = np.sort(np.asarray(tree.query_ball_point([by[j], bx[j]], 0.9 * br[j]),
                                     dtype=np.int64))
        if members.size < 2:
            continue
        home = members[np.argmin(np.hypot(cells["cy"][members] - by[j],
                                          cells["cx"][members] - bx[j]))]
        mass = float(excess[members].sum())
        if mass <= 0:
            continue
        others = members[members != home]
        s = ramp(excess[others] / mass, 0.06, 0.16) * \
            ramp(bright[others] / max(float(bright[home]), 1e-6), 0.5, 0.8) * weak_peak[others]
        better = s > score[others]
        score[others[better]] = s[better]
        partner[others[better]] = cells["label"][home]
        found += int((s >= 0.6).any())
    return score, partner, found


def status_of(under, over, flag=0.6, unsure=0.4, over_flag=None, over_unsure=None):
    """Per cell: a call at or above its side's threshold, ambiguous in the band
    below one (or when both sides are called). `flag` / `unsure` are the under
    side's, and the over side's too unless `over_flag` / `over_unsure` say
    otherwise: the two scores are built differently, so one number need not
    mean the same evidence for both."""
    over_flag = flag if over_flag is None else over_flag
    over_unsure = unsure if over_unsure is None else over_unsure
    is_under = under >= flag
    is_over = over >= over_flag
    status = np.full(under.shape, PASS, dtype=np.uint8)
    status[((under >= unsure) & ~is_under) | ((over >= over_unsure) & ~is_over)] = AMBIGUOUS
    status[is_under] = UNDER
    status[is_over] = OVER
    status[is_under & is_over] = AMBIGUOUS
    return status
