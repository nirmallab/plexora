/**
 * inspectGeometry.js - what part of a stack the 3-D view keeps, as numbers.
 *
 * The 3-D view can be CUT (the view state's `inspect`, services/viewState.js)
 * with one of two tools, over any of X, Y and Z:
 *
 *     cutaway   "Cut": a side taken off along each active axis; with two or
 *               three axes, the corner where every one of them is on its
 *               removed side (a cut-away corner, the rest kept)
 *     crop      a range kept along each active axis, the box they make
 *
 * `mode` is the tool that is on, or "full" for the whole stack. `tool` is the
 * tool last chosen, kept while it is off, so turning it back on (the depth
 * strip's key, views/volumeDock.js) brings back exactly what was cut. Each
 * tool keeps its own axes: `axes` are the current tool's, `axesBy` what each
 * had when it was last left (services/inspectState.js swaps them), so a Cut
 * starts on X and a Crop on X, Y and Z.
 *
 * Every number is in IMAGE units: full-resolution pixels across x and y
 * (boundaries, so a crop of [0, width] is the whole row), plane indices along
 * z.
 *
 * THIS FILE ONLY COMPUTES. `resolve()` turns the state into a box and a hole
 * in continuous image coordinates (plane k spans [k, k+1)); `toTexture()`
 * turns that into the [0,1] numbers the renderer's uniforms take, relative to
 * the area its textures cover. The renderer, the surfaces, the dock, the
 * handles, the section panes, the captions and the agent all ask here, so
 * they cannot disagree about what is cut.
 *
 * Lengths are said in a unit the image states: microns only when the image
 * or its user gave the spacing (`metadata` or `manual`), else pixels and
 * planes. A length is never invented.
 *
 * Classic script, `window.PlexoraInspectGeometry`.
 */
(function (global) {
    "use strict";

    const MODES = ["full", "cutaway", "crop"];
    //: The tools a cut is made with: every mode but the whole stack.
    const TOOLS = ["cutaway", "crop"];
    const AXES = ["x", "y", "z"];
    const KEEPS = ["lo", "hi"];
    const TRUSTED = ["metadata", "manual"];
    //: What the renderer's `u_cut` says: nothing beyond the box, or the cut
    //: faces drawn solid.
    const CUT = { none: 0, faces: 1 };
    //: The axes each tool starts with.
    const TOOL_AXES = {
        cutaway: { x: true, y: false, z: false },
        crop: { x: true, y: true, z: true },
    };

    function clamp(value, low, high) {
        return Math.max(low, Math.min(high, value));
    }

    function num(value, fallback) {
        const number = Number(value);
        return Number.isFinite(number) ? number : fallback;
    }

    function int(value, fallback) {
        const number = Math.round(Number(value));
        return Number.isFinite(number) ? number : fallback;
    }

    function copy(value) {
        return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
    }

    function isObject(value) {
        return Boolean(value) && typeof value === "object" && !Array.isArray(value);
    }

    /** `{width, height, depth}` of the stack (`info` from /volume/<ds>/info). */
    function dims(info = {}, depth = null) {
        return {
            width: Math.max(1, num(info?.width, 1)),
            height: Math.max(1, num(info?.height, 1)),
            depth: Math.max(1, int(depth ?? info?.depth, 1)),
        };
    }

    /** The spacing the image STATES, per axis: `{value, unit}` or null. An
     *  `assumed` or missing step is null: it is drawing, not a measurement. */
    function spacing(info = {}) {
        const out = {};
        for (const axis of AXES) {
            const entry = info?.spacing?.[axis];
            const value = Number(entry?.value);
            const trusted = !entry?.source || TRUSTED.includes(entry.source);
            out[axis] = value > 0 && trusted ? { value, unit: entry.unit || "µm" } : null;
        }
        // y is x unless stated: the plane's pixels are square unless said.
        if (!out.y && out.x) out.y = { ...out.x };
        return out;
    }

    /** One plane, one pixel, in the unit `axis` is measured in, or null. */
    function stepOf(axis, space) {
        return space?.[axis] || null;
    }

    /** The defaults for a stack of `d` (dims). */
    function defaults(d) {
        return {
            mode: "full",
            tool: "cutaway",
            axes: { ...TOOL_AXES.cutaway },
            axesBy: { cutaway: { ...TOOL_AXES.cutaway }, crop: { ...TOOL_AXES.crop } },
            crop: { x: [0, d.width], y: [0, d.height], z: [0, d.depth - 1] },
            cutaway: {
                x: { at: Math.round(d.width / 2), keep: "hi" },
                y: { at: Math.round(d.height / 2), keep: "hi" },
                z: { at: Math.floor(d.depth / 2), keep: "hi" },
            },
            face: false,
        };
    }

    /**
     * Merge `patch` over `base`, one level into `crop`, `cutaway`, `axes` and
     * `axesBy` (and one more into a cutaway axis and a tool's axes), so a
     * patch to `crop.x` keeps `crop.y`.
     */
    function merge(base, patch) {
        const out = copy(base) || {};
        if (!isObject(patch)) return out;
        for (const [key, value] of Object.entries(patch)) {
            if (value === undefined) continue;
            if (key === "axes" && isObject(value)) {
                out.axes = { ...(out.axes || {}), ...value };
            } else if (key === "crop" && isObject(value)) {
                out.crop = { ...(out.crop || {}), ...copy(value) };
            } else if ((key === "cutaway" || key === "axesBy") && isObject(value)) {
                const held = { ...(out[key] || {}) };
                for (const [name, entry] of Object.entries(value)) {
                    held[name] = isObject(entry) ? { ...(held[name] || {}), ...entry } : entry;
                }
                out[key] = held;
            } else {
                out[key] = copy(value);
            }
        }
        return out;
    }

    function range(raw, low, high, fallback, { integer = false, gap = 0 } = {}) {
        const pair = Array.isArray(raw) && raw.length === 2 ? raw : fallback;
        const read = integer ? int : num;
        let a = clamp(read(pair[0], fallback[0]), low, high);
        let b = clamp(read(pair[1], fallback[1]), low, high);
        if (b < a) [a, b] = [b, a];
        if (b - a < gap) {
            b = Math.min(high, a + gap);
            a = Math.max(low, b - gap);
        }
        return [a, b];
    }

    /** Axes as booleans, at least one on (`fallback`'s where none is). */
    function cleanAxes(raw, fallback) {
        const out = {};
        for (const axis of AXES) out[axis] = raw?.[axis] === true;
        return AXES.some((axis) => out[axis]) ? out : { ...fallback };
    }

    /**
     * `raw` repaired over the defaults: a known mode and tool, axes as
     * booleans (at least one on), every position inside the stack, ranges
     * ordered and at least a voxel wide. A cut saved as a section or slab
     * (gone since 2026-10-13) opens as the whole stack.
     */
    function clean(raw, d) {
        const base = defaults(d);
        const src = merge(base, isObject(raw) ? raw : {});
        const out = {};
        out.mode = MODES.includes(src.mode) ? src.mode : "full";
        out.tool = TOOLS.includes(out.mode) ? out.mode : (TOOLS.includes(src.tool) ? src.tool : "cutaway");
        out.axes = cleanAxes(src.axes, TOOL_AXES[out.tool]);
        out.axesBy = {};
        for (const tool of TOOLS) out.axesBy[tool] = cleanAxes(src.axesBy?.[tool], TOOL_AXES[tool]);
        out.crop = {
            x: range(src.crop?.x, 0, d.width, base.crop.x, { gap: 1 }),
            y: range(src.crop?.y, 0, d.height, base.crop.y, { gap: 1 }),
            z: range(src.crop?.z, 0, d.depth - 1, base.crop.z, { integer: true }),
        };
        out.cutaway = {};
        for (const axis of AXES) {
            const entry = isObject(src.cutaway?.[axis]) ? src.cutaway[axis] : {};
            const top = axis === "z" ? d.depth - 1 : (axis === "x" ? d.width : d.height);
            const read = axis === "z" ? int : num;
            out.cutaway[axis] = {
                at: clamp(read(entry.at, base.cutaway[axis].at), 0, top),
                keep: KEEPS.includes(entry.keep) ? entry.keep : "hi",
            };
        }
        out.face = src.face === true;
        return out;
    }

    /** The active axes, x before y before z. */
    function active(inspect) {
        return AXES.filter((axis) => inspect?.axes?.[axis]);
    }

    function whole(d) {
        return { x: [0, d.width], y: [0, d.height], z: [0, d.depth] };
    }

    /**
     * What `inspect` keeps, in continuous image coordinates:
     *
     *     box     {x, y, z: [lo, hi]}  what is drawn at all (crop, a
     *             one-axis cutaway)
     *     hole    {x, y, z: [lo, hi]} | null  the corner a cutaway over two
     *             or three axes takes out of the box
     *     cut     what is drawn beyond the box (CUT), face
     */
    function resolve(inspect, d) {
        const mode = inspect?.mode || "full";
        const axes = active(inspect);
        const box = whole(d);
        const out = { mode, axes: mode === "full" ? [] : axes, box, hole: null, cut: CUT.none, face: false };
        if (mode === "crop") {
            for (const axis of axes) {
                const [lo, hi] = inspect.crop[axis];
                box[axis] = axis === "z" ? [lo, hi + 1] : [lo, hi];
            }
        } else if (mode === "cutaway") {
            const kept = {};
            for (const axis of axes) {
                const { at, keep } = inspect.cutaway[axis];
                // Along z the cut is between planes: keeping "hi" keeps
                // plane `at` and those after it, "lo" plane `at` and before.
                const top = axis === "z" ? d.depth : box[axis][1];
                kept[axis] = keep === "hi" ? [at, top] : [0, axis === "z" ? at + 1 : at];
            }
            if (axes.length === 1) {
                box[axes[0]] = kept[axes[0]];
            } else if (axes.length > 1) {
                // The removed side of each axis, together: one corner.
                const hole = whole(d);
                for (const axis of axes) {
                    const [lo, hi] = kept[axis];
                    hole[axis] = lo > 0 ? [0, lo] : [hi, box[axis][1]];
                }
                out.hole = hole;
            }
        }
        if (mode !== "full" && inspect.face && axes.length) {
            out.face = true;
            out.cut = CUT.faces;
        }
        return out;
    }

    /**
     * `resolved` in the renderer's texture space: x and y relative to
     * `covered` ([x0, y0, x1, y1], the area the textures hold, full-resolution
     * pixels), z to the whole stack. Ranges come back as `[lo, hi]` vec3s.
     */
    function toTexture(resolved, covered, depth) {
        const c = covered && covered.length === 4 ? covered : [0, 0, 1, 1];
        const spanX = Math.max(1e-9, c[2] - c[0]);
        const spanY = Math.max(1e-9, c[3] - c[1]);
        const D = Math.max(1, depth);
        const t = (axis, v) => {
            const raw = axis === "x" ? (v - c[0]) / spanX : (axis === "y" ? (v - c[1]) / spanY : v / D);
            return clamp(raw, 0, 1);
        };
        const pair = (ranges) => [AXES.map((axis) => t(axis, ranges[axis][0])),
                                  AXES.map((axis) => t(axis, ranges[axis][1]))];
        const [lo, hi] = pair(resolved.box);
        const out = { lo, hi, hole: null, cut: resolved.cut, face: resolved.face ? 1 : 0 };
        if (resolved.hole) {
            const [hlo, hhi] = pair(resolved.hole);
            out.hole = { lo: hlo, hi: hhi };
        }
        return out;
    }

    /** Whether two `toTexture` answers draw the same. */
    function sameTexture(a, b) {
        return JSON.stringify(a || null) === JSON.stringify(b || null);
    }

    /** The kept z planes, `[z0, z1]` inclusive, of a resolution: what the
     *  contrast is levelled over. */
    function keptPlanes(resolved, d) {
        const [lo, hi] = resolved.box.z;
        return [clamp(Math.floor(lo), 0, d.depth - 1), clamp(Math.ceil(hi) - 1, 0, d.depth - 1)];
    }

    function fixed(value) {
        const abs = Math.abs(value);
        return abs >= 100 ? value.toFixed(0) : (abs >= 10 ? value.toFixed(1) : value.toFixed(2));
    }

    /**
     * `axis` at `value` (a number or `[lo, hi]`) in words: "120–310 µm",
     * "x 512 px", "planes 4–9 · 12.0 µm". z values are plane indices.
     */
    function label(axis, value, space = null) {
        const step = stepOf(axis, space);
        const pair = Array.isArray(value);
        if (axis === "z") {
            if (pair) {
                const planes = value[1] - value[0] + 1;
                const thick = step ? ` · ${fixed(planes * step.value)} ${step.unit}` : "";
                return `planes ${value[0]}–${value[1]}${thick}`;
            }
            return step ? `z ${value} · ${fixed(value * step.value)} ${step.unit}` : `z ${value}`;
        }
        if (step) {
            return pair ? `${fixed(value[0] * step.value)}–${fixed(value[1] * step.value)} ${step.unit}`
                : `${fixed(value * step.value)} ${step.unit}`;
        }
        return pair ? `${Math.round(value[0])}–${Math.round(value[1])} px` : `${Math.round(value)} px`;
    }

    const MODE_WORDS = { full: "", cutaway: "cutaway", crop: "crop" };

    /**
     * The cut in a few words, for the caption, a capture and the agent:
     * "" for the whole stack, else e.g. "crop X 120–310 µm, Z planes 4–9",
     * "cutaway X from 512 px".
     */
    function describe(inspect, d, space = null) {
        const mode = inspect?.mode || "full";
        if (mode === "full") return "";
        const parts = active(inspect).map((axis) => {
            const name = axis.toUpperCase();
            if (mode === "crop") return `${name} ${label(axis, inspect.crop[axis], space)}`;
            const { at, keep } = inspect.cutaway[axis];
            return `${name} ${keep === "hi" ? "from" : "to"} ${label(axis, at, space)}`;
        });
        return `${MODE_WORDS[mode] || mode} ${parts.join(", ")}`.trim();
    }

    global.PlexoraInspectGeometry = {
        MODES, TOOLS, TOOL_AXES, AXES, CUT,
        dims, spacing, defaults, merge, clean, active, resolve,
        toTexture, sameTexture, keptPlanes, label, describe,
    };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraInspectGeometry;
    }
})(typeof window !== "undefined" ? window : globalThis);
