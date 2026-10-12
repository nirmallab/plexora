/**
 * videoInterp.js - interpolation for camera paths and display values.
 *
 * Quaternions are [x, y, z, w], volumeCamera.js's convention. Two rules keep a
 * camera path from flipping:
 *
 *   1. Before interpolating, a list of keyframe orientations is UNWRAPPED:
 *      each is negated if that brings it closer to the one before
 *      (q and -q are the same rotation). After that, every slerp here goes
 *      the way the keyframes lead and never re-chooses "the short way" per
 *      segment -- which is what makes a path through four views turn the same
 *      direction throughout.
 *   2. Three or more orientations use SQUAD (spherical cubic), so the angular
 *      velocity is continuous through each keyframe instead of jerking.
 *
 * Distances interpolate in log space: zooming from 1000 to 100 passes 316 at
 * the midpoint, so the apparent speed of a zoom is constant.
 *
 * Classic script: `window.PlexoraVideoInterp`, and `module.exports`.
 */
(function (global) {
    "use strict";

    const lerp = (a, b, u) => a + (b - a) * u;

    function lerpVec(a, b, u) {
        return a.map((value, i) => lerp(value, b[i], u));
    }

    /** exp(lerp(log a, log b)): constant-ratio interpolation, for zoom. */
    function logLerp(a, b, u) {
        if (!(a > 0) || !(b > 0)) return lerp(a, b, u);
        return Math.exp(lerp(Math.log(a), Math.log(b), u));
    }

    /** Degrees along the shorter arc. */
    function angleLerp(a, b, u) {
        let delta = ((b - a) % 360 + 540) % 360 - 180;
        return (a + delta * u + 360) % 360;
    }

    // -- quaternions -----------------------------------------------------------

    const dot4 = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3];

    function normalize(q) {
        const n = Math.hypot(q[0], q[1], q[2], q[3]) || 1;
        return [q[0] / n, q[1] / n, q[2] / n, q[3] / n];
    }

    function multiply(a, b) {
        const [ax, ay, az, aw] = a;
        const [bx, by, bz, bw] = b;
        return [
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz,
        ];
    }

    const conjugate = (q) => [-q[0], -q[1], -q[2], q[3]];

    function axisAngle(axis, angle) {
        const n = Math.hypot(axis[0], axis[1], axis[2]);
        if (!n || !angle) return [0, 0, 0, 1];
        const s = Math.sin(angle / 2) / n;
        return [axis[0] * s, axis[1] * s, axis[2] * s, Math.cos(angle / 2)];
    }

    function rotate(q, v) {
        const [x, y, z, w] = q;
        const tx = 2 * (y * v[2] - z * v[1]);
        const ty = 2 * (z * v[0] - x * v[2]);
        const tz = 2 * (x * v[1] - y * v[0]);
        return [v[0] + w * tx + (y * tz - z * ty),
                v[1] + w * ty + (z * tx - x * tz),
                v[2] + w * tz + (x * ty - y * tx)];
    }

    /** Each orientation negated where that keeps it on the same side as the
     *  previous one. */
    function unwrapQuats(list) {
        const out = [];
        list.forEach((q, i) => {
            let next = normalize(q);
            if (i > 0 && dot4(out[i - 1], next) < 0) next = next.map((v) => -v);
            out.push(next);
        });
        return out;
    }

    /** Spherical interpolation that takes the path from a to b AS GIVEN (no
     *  re-flip to the short way: the list was unwrapped once, up front). */
    function slerpRaw(a, b, u) {
        let cos = Math.max(-1, Math.min(1, dot4(a, b)));
        if (cos > 0.9995) return normalize(lerpVec(a, b, u));
        const angle = Math.acos(cos);
        const sin = Math.sin(angle);
        const wa = Math.sin((1 - u) * angle) / sin;
        const wb = Math.sin(u * angle) / sin;
        return normalize(a.map((value, i) => value * wa + b[i] * wb));
    }

    function qlog(q) {
        const v = Math.hypot(q[0], q[1], q[2]);
        if (v < 1e-12) return [0, 0, 0, 0];
        const angle = Math.atan2(v, q[3]);
        const s = angle / v;
        return [q[0] * s, q[1] * s, q[2] * s, 0];
    }

    function qexp(q) {
        const angle = Math.hypot(q[0], q[1], q[2]);
        if (angle < 1e-12) return [0, 0, 0, 1];
        const s = Math.sin(angle) / angle;
        return [q[0] * s, q[1] * s, q[2] * s, Math.cos(angle)];
    }

    /** The squad control point for q[i] given its neighbours. */
    function squadControl(prev, q, next) {
        const inv = conjugate(q);
        const a = qlog(multiply(inv, next));
        const b = qlog(multiply(inv, prev));
        const sum = [-(a[0] + b[0]) / 4, -(a[1] + b[1]) / 4, -(a[2] + b[2]) / 4, 0];
        return normalize(multiply(q, qexp(sum)));
    }

    /** Squad between q[i] and q[i+1] of an unwrapped list, at u. */
    function squad(list, i, u) {
        const n = list.length;
        const q0 = list[i];
        const q1 = list[Math.min(n - 1, i + 1)];
        const prev = list[Math.max(0, i - 1)];
        const next = list[Math.min(n - 1, i + 2)];
        const s0 = squadControl(prev, q0, q1);
        const s1 = squadControl(q0, q1, next);
        return slerpRaw(slerpRaw(q0, q1, u), slerpRaw(s0, s1, u), 2 * u * (1 - u));
    }

    /** The angle between two orientations, radians. */
    function angleBetween(a, b) {
        return 2 * Math.acos(Math.min(1, Math.abs(dot4(a, b))));
    }

    // -- curves through points -----------------------------------------------

    /** Centripetal Catmull-Rom between p1 and p2 (p0, p3 the neighbours), at
     *  u: passes through every point without overshooting tight corners. */
    function catmullRom(p0, p1, p2, p3, u) {
        const alpha = 0.5;
        const dist = (a, b) => Math.max(1e-6, Math.hypot(...a.map((v, i) => v - b[i])) ** alpha);
        const t0 = 0;
        const t1 = t0 + dist(p0, p1);
        const t2 = t1 + dist(p1, p2);
        const t3 = t2 + dist(p2, p3);
        const t = lerp(t1, t2, u);
        const mix = (a, b, ta, tb) => a.map((v, i) => ((tb - t) * v + (t - ta) * b[i]) / (tb - ta));
        const a1 = mix(p0, p1, t0, t1);
        const a2 = mix(p1, p2, t1, t2);
        const a3 = mix(p2, p3, t2, t3);
        const b1 = mix(a1, a2, t0, t2);
        const b2 = mix(a2, a3, t1, t3);
        return mix(b1, b2, t1, t2);
    }

    /** Point i..i+1 of `points` at u, Catmull-Rom with the ends reflected. */
    function catmullAt(points, i, u) {
        const n = points.length;
        const p1 = points[i];
        const p2 = points[Math.min(n - 1, i + 1)];
        const p0 = i > 0 ? points[i - 1] : p1.map((v, k) => 2 * v - p2[k]);
        const p3 = i + 2 < n ? points[i + 2] : p2.map((v, k) => 2 * v - p1[k]);
        if (p1.every((v, k) => v === p2[k])) return p1.slice();
        return catmullRom(p0, p1, p2, p3, u);
    }

    const api = {
        lerp, lerpVec, logLerp, angleLerp, dot4, normalize, multiply, conjugate, axisAngle,
        rotate, unwrapQuats, slerpRaw, squad, squadControl, angleBetween, catmullRom, catmullAt,
    };
    global.PlexoraVideoInterp = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
