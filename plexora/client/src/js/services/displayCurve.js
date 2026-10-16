/**
 * displayCurve.js - one display curve for every surface that draws a channel.
 *
 * The JavaScript twin of plexora/server/utils/display_curve.py and of
 * frag.glsl's range_clamp: a raw value to a brightness in [0, 1], anchored
 * on a window [black, white], either
 *
 *   linear: t = clamp((x - black) / (white - black), 0, 1), or
 *   asinh:  t = clamp(asinh((x - black) / c) / asinh((white - black) / c), 0, 1)
 *
 * with a cofactor c > 0 in the same units as the window. Near black asinh is
 * linear with slope 1/c, far above it logarithmic: a dim cell shows above
 * the background while a bright one keeps its structure short of white.
 *
 * Every surface works in its own normalised units (bytes / 255 over a
 * quantization window, raw / 65535 in HD, a texture's 0..1); the cofactor
 * only scales with them (`normalize`), so one float per channel is enough.
 * tests/golden/display_curve_vectors.json pins this file, the shader and the
 * server to the same numbers.
 *
 * A curve is `null` (linear) or `{kind: "asinh", cofactor}` in RAW units.
 */
(function (global) {
    "use strict";

    //: The Log toggle's cofactor for a channel that has none of its own: the
    //: window's span divided by this (the server's render default too).
    const DEFAULT_ASINH_SPAN = 20;

    /** The curve as {kind: "asinh", cofactor} or null for linear. */
    function normalise(curve) {
        if (!curve || typeof curve !== "object") return null;
        const kind = curve.kind === undefined ? "asinh" : curve.kind;
        const cofactor = Number(curve.cofactor);
        if (kind !== "asinh" || !Number.isFinite(cofactor) || cofactor <= 0) return null;
        return { kind: "asinh", cofactor };
    }

    /**
     * A brightness in [0, 1]. `cofactor` in the window's own units; 0 (or
     * anything not positive) is linear -- the arithmetic the shader does.
     */
    function transfer(value, low, high, cofactor) {
        const span = high - low;
        if (!(span > 0)) return 0;
        let t;
        if (cofactor > 0) {
            const top = Math.asinh(span / cofactor);
            t = top > 0 ? Math.asinh((value - low) / cofactor) / top : 0;
        } else {
            t = (value - low) / span;
        }
        return Math.min(1, Math.max(0, t));
    }

    /** `transfer` for a raw value against a raw window and a curve object. */
    function level(value, window, curve) {
        const shape = normalise(curve);
        return transfer(Number(value), Number(window[0]), Number(window[1]),
                        shape ? shape.cofactor : 0);
    }

    /** The raw value drawn at brightness t under a raw window and curve. */
    function inverse(t, window, curve) {
        const low = Number(window[0]);
        const high = Number(window[1]);
        const f = Math.min(1, Math.max(0, Number(t)));
        const shape = normalise(curve);
        if (!shape) return low + f * (high - low);
        const c = shape.cofactor;
        return low + c * Math.sinh(f * Math.asinh((high - low) / c));
    }

    /**
     * A raw cofactor in the normalised units of a surface whose value is
     * (raw - offset) / scale: c / scale. 0 for a linear curve or an unknown
     * scale (drawn linear rather than wrong).
     */
    function normalize(curve, scale) {
        const shape = normalise(curve);
        const s = Number(scale);
        if (!shape || !(s > 0)) return 0;
        return shape.cofactor / s;
    }

    /** The Log toggle's curve for a raw window: span / DEFAULT_ASINH_SPAN. */
    function seeded(window) {
        const span = Number(window[1]) - Number(window[0]);
        return span > 0 ? { kind: "asinh", cofactor: span / DEFAULT_ASINH_SPAN } : null;
    }

    /** Whether two curves draw the same (both linear, or the same cofactor). */
    function same(a, b) {
        const x = normalise(a);
        const y = normalise(b);
        if (!x || !y) return !x && !y;
        return Math.abs(x.cofactor - y.cofactor) <= 1e-9 * Math.max(1, Math.abs(x.cofactor));
    }

    const DisplayCurve = { normalise, transfer, level, inverse, normalize, seeded, same,
                           DEFAULT_ASINH_SPAN };
    global.PlexoraDisplayCurve = DisplayCurve;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = DisplayCurve;
    }
})(typeof window !== "undefined" ? window : globalThis);
