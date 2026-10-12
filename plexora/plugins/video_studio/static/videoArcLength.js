/**
 * videoArcLength.js - constant speed along a camera path.
 *
 * A path through keyframes parameterised by time moves fast where keyframes
 * are far apart and slow where they bunch up. With `speed: "arc"` a camera
 * track instead travels at constant SPATIAL speed: the path is sampled, its
 * cumulative length tabulated, and progress along the length is mapped back to
 * the curve parameter by binary search and linear interpolation in the table.
 *
 * `table(fn, samples)` samples `fn(s)` for s in [0, 1] (any dimension) and
 * returns `{length, at(fraction) -> s}`.
 *
 * Classic script: `window.PlexoraVideoArcLength`, and `module.exports`.
 */
(function (global) {
    "use strict";

    function table(fn, samples = 256) {
        const n = Math.max(2, Math.floor(samples));
        const params = new Float64Array(n + 1);
        const lengths = new Float64Array(n + 1);
        let prev = fn(0);
        for (let i = 1; i <= n; i += 1) {
            const s = i / n;
            const point = fn(s);
            let step = 0;
            for (let k = 0; k < point.length; k += 1) step += (point[k] - prev[k]) ** 2;
            params[i] = s;
            lengths[i] = lengths[i - 1] + Math.sqrt(step);
            prev = point;
        }
        const length = lengths[n];
        function at(fraction) {
            if (!(length > 0)) return Math.max(0, Math.min(1, fraction));
            const target = Math.max(0, Math.min(1, fraction)) * length;
            let lo = 0, hi = n;
            while (hi - lo > 1) {
                const mid = (lo + hi) >> 1;
                if (lengths[mid] < target) lo = mid; else hi = mid;
            }
            const span = lengths[hi] - lengths[lo];
            const local = span > 0 ? (target - lengths[lo]) / span : 0;
            return params[lo] + (params[hi] - params[lo]) * local;
        }
        return { length, at, lengths, params };
    }

    /** The coefficient of variation of step lengths when `fn` is sampled at
     *  equal `at(fraction)` steps -- 0 for perfectly constant speed. */
    function speedVariation(fn, arc, steps = 100) {
        const lengths = [];
        let prev = fn(arc.at(0));
        for (let i = 1; i <= steps; i += 1) {
            const point = fn(arc.at(i / steps));
            lengths.push(Math.hypot(...point.map((v, k) => v - prev[k])));
            prev = point;
        }
        const mean = lengths.reduce((a, b) => a + b, 0) / lengths.length;
        if (!(mean > 0)) return 0;
        const variance = lengths.reduce((a, b) => a + (b - mean) ** 2, 0) / lengths.length;
        return Math.sqrt(variance) / mean;
    }

    const api = { table, speedVariation };
    global.PlexoraVideoArcLength = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
