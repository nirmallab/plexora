/**
 * Channel statistics the browser node owes the viewer, with the server's
 * rules (plexora/server/models/data_model.py, providers/local.py):
 *
 *  - overviewOf: providers/local.volume_overview -- the smallest level with
 *    both sides >= 200, block-mean-pooled toward 200 when a side exceeds 400
 *    (skimage block_reduce: the far edges padded with zeros).
 *  - statsOf: data_model.channel_stats_of -- a 50-bin density histogram of
 *    log(v > 0), image_min/max, and the p50 / p99.5 hint window.
 *  - gmmOf: data_model.channel_gmm_of -- a 3-component Gaussian mixture on
 *    log(v > 0) (sklearn GaussianMixture(3, max_iter=1000, tol=1e-6), k-means
 *    initialised), vmin at the crossover of the two upper components, vmax at
 *    mean + 2 sd of the top one.
 *
 * sklearn's fit is randomly initialised, so the server's own answer moves from
 * run to run; the parity check is a tolerance, not equality.
 */

export function chooseOverviewLevel(levels) {
    for (let i = levels.length - 1; i >= 0; i -= 1) {
        if (levels[i].width >= 200 && levels[i].height >= 200) return i;
    }
    return 0;
}

/** Block-mean pooling of a (height x width) plane, as skimage pads it. */
export function overviewOf(values, width, height) {
    if (width <= 400 && height <= 400) return { data: Float64Array.from(values), width, height };
    const reduce = Math.max(1, Math.min(Math.floor(height / 200), Math.floor(width / 200)));
    const ow = Math.ceil(width / reduce);
    const oh = Math.ceil(height / reduce);
    const sums = new Float64Array(ow * oh);
    for (let y = 0; y < height; y += 1) {
        const row = y * width;
        const orow = Math.floor(y / reduce) * ow;
        for (let x = 0; x < width; x += 1) sums[orow + Math.floor(x / reduce)] += values[row + x];
    }
    const block = reduce * reduce;   // zero padding counts toward the mean
    for (let i = 0; i < sums.length; i += 1) sums[i] /= block;
    return { data: sums, width: ow, height: oh };
}

function logPositive(values) {
    let n = 0;
    for (let i = 0; i < values.length; i += 1) if (values[i] > 0) n += 1;
    const out = new Float64Array(n);
    let k = 0;
    for (let i = 0; i < values.length; i += 1) if (values[i] > 0) out[k++] = Math.log(values[i]);
    return out;
}

/** numpy.percentile's default (linear) interpolation over a sorted copy. */
export function percentile(sorted, q) {
    if (!sorted.length) return NaN;
    const pos = (q / 100) * (sorted.length - 1);
    const lo = Math.floor(pos);
    const hi = Math.min(lo + 1, sorted.length - 1);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

/** numpy.linspace(start, stop, num): step * i + start, the last one exact. */
function linspace(start, stop, num) {
    const step = (stop - start) / (num - 1);
    const out = new Float64Array(num);
    for (let i = 0; i < num; i += 1) out[i] = i * step + start;
    out[num - 1] = stop;
    return out;
}

/**
 * numpy.histogram(values, bins, density=True) -> [{x: midpoint, y: density}],
 * with numpy's own uniform-bin assignment (lib/_histograms_impl.py): the bin
 * index from (v - first) * norm, then nudged against the linspace edges so a
 * value that rounding put on the wrong side of an edge goes where numpy puts
 * it. A plain floor((v - min) / width) disagrees with numpy for values on
 * an edge, which a table of measured areas has plenty of.
 */
export function histogram(values, bins = 50, lo = null, hi = null) {
    let first = lo, last = hi;
    if (first === null || last === null) {
        first = Infinity; last = -Infinity;
        for (let i = 0; i < values.length; i += 1) {
            const v = values[i];
            if (v < first) first = v;
            if (v > last) last = v;
        }
    }
    if (first === last) { first -= 0.5; last += 0.5; }
    const edges = linspace(first, last, bins + 1);
    const norm = bins / (last - first);
    const counts = new Float64Array(bins);
    let total = 0;
    for (let i = 0; i < values.length; i += 1) {
        const v = values[i];
        if (!(v >= first && v <= last)) continue;
        let b = Math.trunc((v - first) * norm);
        if (b === bins) b -= 1;
        if (v < edges[b]) b -= 1;
        else if (b !== bins - 1 && v >= edges[b + 1]) b += 1;
        counts[b] += 1;
        total += 1;
    }
    const out = [];
    for (let b = 0; b < bins; b += 1) {
        const width = edges[b + 1] - edges[b];
        out.push({ x: (edges[b + 1] + edges[b]) / 2, y: counts[b] / width / total });
    }
    return out;
}

export function statsOf(overview, qmin = null, qmax = null) {
    const logs = logPositive(overview);
    const sorted = Float64Array.from(logs).sort();
    return {
        image_histogram: histogram(logs, 50, sorted[0], sorted[sorted.length - 1]),
        image_min: Math.ceil(Math.exp(sorted[0])),
        image_max: Math.ceil(Math.exp(sorted[sorted.length - 1])),
        qmin, qmax,
        vmin_hint: Math.round(Math.exp(percentile(sorted, 50))),
        vmax_hint: Math.round(Math.exp(percentile(sorted, 99.5))),
    };
}

/** Deterministic RNG for the k-means++ start (sklearn's is random). */
function mulberry32(seed) {
    return () => {
        seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
        let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

function kmeans1d(x, k, rand) {
    // k-means++ seeding, then Lloyd to convergence.
    const centers = [x[Math.floor(rand() * x.length)]];
    const d2 = new Float64Array(x.length);
    while (centers.length < k) {
        let total = 0;
        for (let i = 0; i < x.length; i += 1) {
            let best = Infinity;
            for (const c of centers) best = Math.min(best, (x[i] - c) ** 2);
            d2[i] = best;
            total += best;
        }
        let r = rand() * total;
        let pick = x.length - 1;
        for (let i = 0; i < x.length; i += 1) { r -= d2[i]; if (r <= 0) { pick = i; break; } }
        centers.push(x[pick]);
    }
    const labels = new Int32Array(x.length);
    for (let iter = 0; iter < 300; iter += 1) {
        const sum = new Float64Array(k);
        const count = new Float64Array(k);
        for (let i = 0; i < x.length; i += 1) {
            let best = 0;
            for (let j = 1; j < k; j += 1) if (Math.abs(x[i] - centers[j]) < Math.abs(x[i] - centers[best])) best = j;
            labels[i] = best;
            sum[best] += x[i];
            count[best] += 1;
        }
        let moved = 0;
        for (let j = 0; j < k; j += 1) {
            const c = count[j] ? sum[j] / count[j] : centers[j];
            moved += Math.abs(c - centers[j]);
            centers[j] = c;
        }
        if (moved < 1e-10) break;
    }
    return labels;
}

/** EM for a 1-D Gaussian mixture, sklearn's defaults (reg_covar 1e-6). */
export function fitGmm(x, k = 3, { maxIter = 1000, tol = 1e-6, seed = 1 } = {}) {
    const n = x.length;
    const rand = mulberry32(seed);
    const labels = kmeans1d(x, k, rand);
    const resp = new Float64Array(n * k);
    for (let i = 0; i < n; i += 1) resp[i * k + labels[i]] = 1;
    const weights = new Float64Array(k);
    const means = new Float64Array(k);
    const vars = new Float64Array(k);
    const mstep = () => {
        for (let j = 0; j < k; j += 1) {
            let nk = 1e-15, s = 0;   // sklearn adds 10*eps to nk
            for (let i = 0; i < n; i += 1) { nk += resp[i * k + j]; s += resp[i * k + j] * x[i]; }
            const mu = s / nk;
            let v = 0;
            for (let i = 0; i < n; i += 1) v += resp[i * k + j] * (x[i] - mu) ** 2;
            weights[j] = nk / n;
            means[j] = mu;
            vars[j] = v / nk + 1e-6;
        }
    };
    mstep();
    let lower = -Infinity;
    let iterations = 0;
    let converged = false;
    for (let iter = 1; iter <= maxIter; iter += 1) {
        iterations = iter;
        let ll = 0;
        for (let i = 0; i < n; i += 1) {
            let max = -Infinity;
            const lp = new Float64Array(k);
            for (let j = 0; j < k; j += 1) {
                lp[j] = Math.log(weights[j]) - 0.5 * (Math.log(2 * Math.PI * vars[j]) + (x[i] - means[j]) ** 2 / vars[j]);
                if (lp[j] > max) max = lp[j];
            }
            let s = 0;
            for (let j = 0; j < k; j += 1) s += Math.exp(lp[j] - max);
            const lse = max + Math.log(s);
            ll += lse;
            for (let j = 0; j < k; j += 1) resp[i * k + j] = Math.exp(lp[j] - lse);
        }
        mstep();
        const mean = ll / n;
        if (Math.abs(mean - lower) < tol) { converged = true; lower = mean; break; }
        lower = mean;
    }
    return { weights: Array.from(weights), means: Array.from(means), vars: Array.from(vars),
             iterations, converged, lowerBound: lower };
}

const normPdf = (x, mu, sd) => Math.exp(-0.5 * ((x - mu) / sd) ** 2) / (sd * Math.sqrt(2 * Math.PI));

/** channel_gmm_of's vmin/vmax from the fit (the log-window part is left out). */
export function gmmOf(overview, options = {}) {
    const logs = logPositive(overview);
    let imin = Infinity, imax = -Infinity;
    for (let i = 0; i < overview.length; i += 1) {
        if (overview[i] < imin) imin = overview[i];
        if (overview[i] > imax) imax = overview[i];
    }
    const fit = fitGmm(logs, 3, options);
    const order = [0, 1, 2].sort((a, b) => fit.means[a] - fit.means[b]);
    const [, i1, i2] = order;
    const mean1 = fit.means[i1], mean2 = fit.means[i2];
    const std1 = Math.sqrt(fit.vars[i1]), std2 = Math.sqrt(fit.vars[i2]);
    let best = 0, bestDiff = Infinity;
    const xs = [];
    for (let i = 0; i < 50; i += 1) xs.push(mean1 + (mean2 - mean1) * (i / 49));
    xs.forEach((x, i) => {
        const d = Math.abs(normPdf(x, mean1, std1) * fit.weights[i1] - normPdf(x, mean2, std2) * fit.weights[i2]);
        if (d < bestDiff) { bestDiff = d; best = i; }
    });
    const lmax = mean2 + 2 * std2;
    let lmin = xs[best];
    if (lmin >= mean2) lmin = mean2 - 2 * std2;
    const vmin = Math.max(Math.exp(lmin), imin, 0);
    const vmax = Math.min(Math.exp(lmax), imax);
    return { vmin: Math.round(vmin), vmax: Math.round(vmax), fit, samples: logs.length };
}

/**
 * Floyd-Rivest selection: after it, a[k] is the k-th smallest of a[left..right],
 * everything before it no larger and everything after no smaller. Ported from
 * mourner/quickselect (ISC). Linear on average, against a sort's n log n.
 */
function quickselect(a, k, left, right) {
    while (right > left) {
        if (right - left > 600) {
            const n = right - left + 1;
            const m = k - left + 1;
            const z = Math.log(n);
            const s = 0.5 * Math.exp((2 * z) / 3);
            const sd = 0.5 * Math.sqrt((z * s * (n - s)) / n) * (m - n / 2 < 0 ? -1 : 1);
            const newLeft = Math.max(left, Math.floor(k - (m * s) / n + sd));
            const newRight = Math.min(right, Math.floor(k + ((n - m) * s) / n + sd));
            quickselect(a, k, newLeft, newRight);
        }
        const t = a[k];
        let i = left;
        let j = right;
        let tmp = a[left]; a[left] = a[k]; a[k] = tmp;
        if (a[right] > t) { tmp = a[left]; a[left] = a[right]; a[right] = tmp; }
        while (i < j) {
            tmp = a[i]; a[i] = a[j]; a[j] = tmp;
            i += 1; j -= 1;
            while (a[i] < t) i += 1;
            while (a[j] > t) j -= 1;
        }
        if (a[left] === t) { tmp = a[left]; a[left] = a[j]; a[j] = tmp; }
        else { j += 1; tmp = a[j]; a[j] = a[right]; a[right] = tmp; }
        if (j <= k) left = j + 1;
        if (k <= j) right = j - 1;
    }
}

/** numpy's linear percentiles of `finite` (which this reorders), for several q. */
export function percentiles(finite, qs) {
    const n = finite.length;
    const wanted = new Set();
    for (const q of qs) {
        const pos = (q / 100) * (n - 1);
        wanted.add(Math.floor(pos));
        wanted.add(Math.min(n - 1, Math.floor(pos) + 1));
    }
    const ks = [...wanted].sort((x, y) => x - y);
    let left = 0;
    // Each selection on what is right of the last: a[k] is final once
    // selected, and starting the next one AT k would let its first swap move it.
    for (const k of ks) { quickselect(finite, k, left, n - 1); left = k + 1; }
    return qs.map((q) => {
        const pos = (q / 100) * (n - 1);
        const lo = Math.floor(pos);
        const hi = Math.min(n - 1, lo + 1);
        return finite[lo] + (finite[hi] - finite[lo]) * (pos - lo);
    });
}
