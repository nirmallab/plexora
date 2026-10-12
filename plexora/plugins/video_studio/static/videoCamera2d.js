/**
 * videoCamera2d.js - moving a flat view: pan, zoom, rotate.
 *
 * A 2-D view is `{cx, cy, w, degrees, flip_h, flip_v}`: its centre and width in
 * full-resolution image pixels (the height follows from the output's aspect),
 * plus the view transform. Between two views:
 *
 *   - the width changes on a log scale, so a 4x zoom looks equally fast at its
 *     start and its end;
 *   - the centre moves so that the point both views agree on (the one at the
 *     same place on screen in each) stays still -- zooming into a region of the
 *     first view looks like zooming INTO it, not sliding past it. When that
 *     point lies far outside both views (a long pan with little zoom) the centre
 *     simply moves in a straight line instead;
 *   - rotation takes the shorter way; flips change at the keyframe.
 *
 * Classic script: `window.PlexoraVideoCamera2d`, and `module.exports`.
 */
(function (global) {
    "use strict";

    const Interp = global.PlexoraVideoInterp
        || (typeof require === "function" ? require("./videoInterp.js") : null);
    const Easing = global.PlexoraVideoEasing
        || (typeof require === "function" ? require("./videoEasing.js") : null);

    function copy(view) {
        return { cx: view.cx, cy: view.cy, w: view.w, degrees: view.degrees || 0,
                 flip_h: Boolean(view.flip_h), flip_v: Boolean(view.flip_v) };
    }

    /** The view `u` of the way from a to b (u already eased). */
    function between(a, b, u) {
        const w = Interp.logLerp(a.w, b.w, u);
        let cx;
        let cy;
        const dw = a.w - b.w;
        if (Math.abs(dw) > 1e-9 * Math.max(a.w, b.w)) {
            // The fixed point f: (f - c_a) / w_a = (f - c_b) / w_b.
            const fx = (a.cx * b.w - b.cx * a.w) / (b.w - a.w);
            const fy = (a.cy * b.w - b.cy * a.w) / (b.w - a.w);
            const far = Math.hypot(fx - a.cx, fy - a.cy) > 3 * Math.max(a.w, b.w);
            if (!far) {
                const k = (a.w - w) / dw;
                cx = a.cx + (b.cx - a.cx) * k;
                cy = a.cy + (b.cy - a.cy) * k;
            }
        }
        if (cx === undefined) {
            cx = Interp.lerp(a.cx, b.cx, u);
            cy = Interp.lerp(a.cy, b.cy, u);
        }
        return {
            cx, cy, w,
            degrees: Interp.angleLerp(a.degrees || 0, b.degrees || 0, u),
            flip_h: u >= 1 ? Boolean(b.flip_h) : Boolean(a.flip_h),
            flip_v: u >= 1 ? Boolean(b.flip_v) : Boolean(a.flip_v),
        };
    }

    /** A view on the path through `frames` (`[{t, value, ease, hold}]`). */
    function pathAt(frames, t) {
        if (!frames.length) return null;
        if (frames.length === 1 || t <= frames[0].t) return copy(frames[0].value);
        const last = frames[frames.length - 1];
        if (t >= last.t) return copy(last.value);
        let index = frames.findIndex((f, i) => i + 1 < frames.length && t < frames[i + 1].t);
        if (index < 0) index = frames.length - 2;
        const a = frames[index];
        const b = frames[index + 1];
        const start = a.t + (a.hold || 0);
        const span = b.t - start;
        const raw = span > 0 ? (t - start) / span : 1;
        return between(a.value, b.value, Easing.ease(a.ease, raw));
    }

    /** A pan from `view` by (dx, dy) image pixels over progress u. */
    function pan(view, params, u) {
        return { ...copy(view), cx: view.cx + (params.dx || 0) * u, cy: view.cy + (params.dy || 0) * u };
    }

    /** The image rectangle a view shows at output aspect `aspect` (w / h). */
    function rect(view, aspect) {
        const h = view.w / Math.max(1e-6, aspect);
        return { x: view.cx - view.w / 2, y: view.cy - h / 2, width: view.w, height: h };
    }

    /** The view showing image rectangle `r` whole at output aspect `aspect`. */
    function fit(r, aspect, base = {}) {
        const w = Math.max(r.width, r.height * aspect);
        return { ...copy({ degrees: 0, ...base, cx: r.x + r.width / 2, cy: r.y + r.height / 2, w }) };
    }

    const api = { copy, between, pathAt, pan, rect, fit };
    global.PlexoraVideoCamera2d = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
