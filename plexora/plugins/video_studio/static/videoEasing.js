/**
 * videoEasing.js - how a change between two keyframes is paced.
 *
 * Every easing maps progress u in [0, 1] to eased progress, with f(0) = 0 and
 * f(1) = 1, so a keyframe is always reached exactly at its time. The names are
 * the server's (server/schema.py EASINGS); a value can also be a CSS-style
 * cubic Bezier `[x1, y1, x2, y2]` or `{accel, decel}` for the cinematic
 * profile with custom ramps.
 *
 * `step` holds the starting value for the whole segment and lands on the next
 * keyframe only at its time -- what acquired planes and timepoints use, since
 * a plane between planes was never imaged.
 *
 * Classic script: `window.PlexoraVideoEasing`, and `module.exports` for the
 * node:vm probes.
 */
(function (global) {
    "use strict";

    const clamp01 = (u) => (u <= 0 ? 0 : u >= 1 ? 1 : u);

    /**
     * Constant acceleration for `accel` of the time, constant speed, constant
     * deceleration for the last `decel`: a camera move that starts and stops
     * like a dolly rather than a spring. The speed in the middle is whatever
     * makes the whole move cover exactly 1.
     */
    function cinematic(accel = 0.25, decel = 0.25) {
        const a = Math.max(0, Math.min(0.5, accel));
        const d = Math.max(0, Math.min(0.5, decel));
        const peak = 1 / (1 - a / 2 - d / 2);
        return (u) => {
            u = clamp01(u);
            if (a > 0 && u < a) return (peak * u * u) / (2 * a);
            if (d > 0 && u > 1 - d) return 1 - (peak * (1 - u) * (1 - u)) / (2 * d);
            return peak * (a / 2 + (u - a));
        };
    }

    /** CSS `cubic-bezier(x1, y1, x2, y2)`: solve x(s) = u, return y(s). */
    function bezier(x1, y1, x2, y2) {
        const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
        const cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
        const x = (s) => ((ax * s + bx) * s + cx) * s;
        const y = (s) => ((ay * s + by) * s + cy) * s;
        const dx = (s) => (3 * ax * s + 2 * bx) * s + cx;
        return (u) => {
            u = clamp01(u);
            if (u === 0 || u === 1) return u;
            let s = u;
            for (let i = 0; i < 8; i += 1) {
                const err = x(s) - u;
                if (Math.abs(err) < 1e-7) return y(s);
                const slope = dx(s);
                if (Math.abs(slope) < 1e-6) break;
                s -= err / slope;
            }
            // Bisection when Newton stalls (flat ends of steep curves).
            let lo = 0, hi = 1;
            s = u;
            for (let i = 0; i < 40; i += 1) {
                const value = x(s);
                if (Math.abs(value - u) < 1e-7) break;
                if (value < u) lo = s; else hi = s;
                s = (lo + hi) / 2;
            }
            return y(s);
        };
    }

    const EASINGS = {
        linear: (u) => clamp01(u),
        step: (u) => (u >= 1 ? 1 : 0),
        hold: (u) => (u >= 1 ? 1 : 0),
        quad_in: (u) => clamp01(u) ** 2,
        quad_out: (u) => { u = clamp01(u); return u * (2 - u); },
        quad_in_out: (u) => { u = clamp01(u); return u < 0.5 ? 2 * u * u : -1 + (4 - 2 * u) * u; },
        cubic_in: (u) => clamp01(u) ** 3,
        cubic_out: (u) => { u = clamp01(u) - 1; return u * u * u + 1; },
        cubic_in_out: (u) => {
            u = clamp01(u);
            return u < 0.5 ? 4 * u * u * u : (u - 1) * (2 * u - 2) * (2 * u - 2) + 1;
        },
        sine_in: (u) => 1 - Math.cos((clamp01(u) * Math.PI) / 2),
        sine_out: (u) => Math.sin((clamp01(u) * Math.PI) / 2),
        sine_in_out: (u) => -(Math.cos(Math.PI * clamp01(u)) - 1) / 2,
        expo_in: (u) => { u = clamp01(u); return u === 0 ? 0 : 2 ** (10 * u - 10); },
        expo_out: (u) => { u = clamp01(u); return u === 1 ? 1 : 1 - 2 ** (-10 * u); },
        expo_in_out: (u) => {
            u = clamp01(u);
            if (u === 0 || u === 1) return u;
            return u < 0.5 ? 2 ** (20 * u - 10) / 2 : (2 - 2 ** (-20 * u + 10)) / 2;
        },
        smootherstep: (u) => { u = clamp01(u); return u * u * u * (u * (6 * u - 15) + 10); },
        cinematic: cinematic(),
    };

    const NAMES = Object.keys(EASINGS).filter((name) => name !== "hold");

    /** The easing function for `spec` (a name, a Bezier array, or
     *  `{accel, decel}`); linear for anything unrecognised. */
    function resolve(spec) {
        if (typeof spec === "function") return spec;
        if (Array.isArray(spec) && spec.length === 4 && spec.every(Number.isFinite)) {
            return bezier(spec[0], spec[1], spec[2], spec[3]);
        }
        if (spec && typeof spec === "object" && ("accel" in spec || "decel" in spec)) {
            return cinematic(spec.accel, spec.decel);
        }
        return EASINGS[spec] || EASINGS.linear;
    }

    function ease(spec, u) {
        return resolve(spec)(u);
    }

    /** Whether `spec` holds its value until the segment ends. */
    function isStep(spec) {
        return spec === "step" || spec === "hold";
    }

    const api = { EASINGS, NAMES, resolve, ease, isStep, bezier, cinematic, clamp01 };
    global.PlexoraVideoEasing = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
