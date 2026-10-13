/**
 * inspectGeometry.js - what part of a stack the 3-D view keeps, as numbers.
 *
 * The 3-D view can be CUT (the view state's `inspect`, services/viewState.js):
 *
 *     full      the whole stack
 *     cutaway   a side taken off along each active axis; with two or three
 *               axes, the corner where every one of them is on its removed
 *               side (a cut-away corner, the rest kept)
 *     section   a plane through the stack per active axis, one voxel thick,
 *               in its real intensities
 *     slab      a thick slice per active axis, its voxels combined along each
 *               ray (single, maximum, mean, minimum, sum)
 *     crop      a range kept along each active axis, the box they make
 *
 * over any of X, Y and Z. Every number is in IMAGE units: full-resolution
 * pixels across x and y (boundaries, so a crop of [0, width] is the whole
 * row), plane indices along z. Section and slab positions are the view's own
 * plane and its sections (`state.z`, `state.section.x/y`), which is why the
 * plane a 2-D view returns to is the Z section the 3-D view showed.
 *
 * THIS FILE ONLY COMPUTES. `resolve()` turns the state into a box, a hole and
 * a set of planes in continuous image coordinates (plane k spans [k, k+1));
 * `toTexture()` turns that into the [0,1] numbers the renderer's uniforms
 * take, relative to the area its textures cover. The renderer, the surfaces,
 * the dock, the depth strip, the handles, the section panes, the captions and
 * the agent all ask here, so they cannot disagree about what is cut.
 *
 * Lengths are said in a unit the image states: microns only when the image
 * or its user gave the spacing (`metadata` or `manual`), else pixels and
 * planes. A length is never invented.
 *
 * Classic script, `window.PlexoraInspectGeometry`.
 */
(function (global) {
    "use strict";

    const MODES = ["full", "cutaway", "section", "slab", "crop"];
    const AXES = ["x", "y", "z"];
    const COMPOSITES = ["single", "max", "mean", "min", "sum"];
    const KEEPS = ["lo", "hi"];
    const TRUSTED = ["metadata", "manual"];
    //: What the renderer's `u_cut` says: nothing beyond the box, planes,
    //: slabs combined along the ray, or the cut faces drawn solid.
    const CUT = { none: 0, planes: 1, slabs: 2, faces: 3 };
    //: `u_slab_op` for the combining slab composites.
    const OPS = { max: 1, mean: 2, min: 3, sum: 4 };
    //: The surrounding volume drawn behind a section or slab, by default.
    const CONTEXT = 0.15;

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

    /** The defaults for a stack of `d` (dims) and spacing `space`. */
    function defaults(d, space = null) {
        const zThick = Math.max(1, Math.round(d.depth / 10));
        const thickness = thicknessFor(zThick, "z", d, space);
        return {
            mode: "full",
            axes: { x: false, y: false, z: true },
            crop: { x: [0, d.width], y: [0, d.height], z: [0, d.depth - 1] },
            cutaway: {
                x: { at: Math.round(d.width / 2), keep: "hi" },
                y: { at: Math.round(d.height / 2), keep: "hi" },
                z: { at: Math.floor(d.depth / 2), keep: "hi" },
            },
            slab: { thickness, composite: "max" },
            opacity: 1,
            context: CONTEXT,
            face: false,
            linkPanes: false,
        };
    }

    /**
     * One thickness for every axis: `value` in `axis`'s units (planes along
     * z, pixels across), the same physical length along the others where the
     * spacing is stated, the same number of voxels where it is not.
     */
    function thicknessFor(value, axis, d, space = null) {
        const from = stepOf(axis, space);
        const out = {};
        for (const to of AXES) {
            const step = stepOf(to, space);
            let voxels = value;
            if (to !== axis && from && step && unitsAgree(from, step)) {
                voxels = (value * from.value) / step.value;
            }
            const top = to === "z" ? d.depth : (to === "x" ? d.width : d.height);
            out[to] = clamp(Math.round(voxels), 1, Math.max(1, top));
        }
        return out;
    }

    function unitsAgree(a, b) {
        return (a.unit || "µm") === (b.unit || "µm");
    }

    /**
     * Merge `patch` over `base`, one level into `crop`, `cutaway`, `slab` and
     * `axes` (and one more into a cutaway axis and the thickness), so a patch
     * to `crop.x` keeps `crop.y`.
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
            } else if (key === "cutaway" && isObject(value)) {
                const held = { ...(out.cutaway || {}) };
                for (const [axis, entry] of Object.entries(value)) {
                    held[axis] = isObject(entry) ? { ...(held[axis] || {}), ...entry } : entry;
                }
                out.cutaway = held;
            } else if (key === "slab" && isObject(value)) {
                const held = { ...(out.slab || {}), ...copy(value) };
                if (isObject(value.thickness)) {
                    held.thickness = { ...(out.slab?.thickness || {}), ...value.thickness };
                }
                out.slab = held;
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

    /**
     * `raw` repaired over the defaults: a known mode, axes as booleans (at
     * least one on), every position inside the stack, ranges ordered and at
     * least a voxel wide, thicknesses of a voxel or more.
     */
    function clean(raw, d, space = null) {
        const base = defaults(d, space);
        const src = merge(base, isObject(raw) ? raw : {});
        const out = {};
        out.mode = MODES.includes(src.mode) ? src.mode : "full";
        out.axes = {};
        for (const axis of AXES) out.axes[axis] = src.axes?.[axis] === true;
        if (!AXES.some((axis) => out.axes[axis])) out.axes.z = true;
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
        const thick = isObject(src.slab?.thickness) ? src.slab.thickness : {};
        out.slab = {
            thickness: {
                x: clamp(int(thick.x, base.slab.thickness.x), 1, Math.max(1, Math.round(d.width))),
                y: clamp(int(thick.y, base.slab.thickness.y), 1, Math.max(1, Math.round(d.height))),
                z: clamp(int(thick.z, base.slab.thickness.z), 1, d.depth),
            },
            composite: COMPOSITES.includes(src.slab?.composite) ? src.slab.composite : "max",
        };
        out.opacity = clamp(num(src.opacity, 1), 0.05, 1);
        out.context = clamp(num(src.context, CONTEXT), 0, 1);
        out.face = src.face === true;
        out.linkPanes = src.linkPanes === true;
        return out;
    }

    /** Where the section and slab planes are: the view's plane and sections. */
    function positions(state, d) {
        const section = state?.section || {};
        return {
            x: clamp(num(section.x, Math.floor(d.width / 2)), 0, d.width - 1),
            y: clamp(num(section.y, Math.floor(d.height / 2)), 0, d.height - 1),
            z: clamp(int(state?.z, Math.floor(d.depth / 2)), 0, d.depth - 1),
        };
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
     *     planes  [{axis, at, lo, hi}]  each section's plane (lo = hi = at)
     *             or slab's interval, at its centre `at`
     *     cut     what is drawn beyond the box (CUT), op (OPS), context,
     *             opacity, face
     *
     * `pos` is `positions(state, d)`.
     */
    function resolve(inspect, pos, d) {
        const mode = inspect?.mode || "full";
        const axes = active(inspect);
        const box = whole(d);
        const out = { mode, axes: mode === "full" ? [] : axes, box, hole: null, planes: [],
                      cut: CUT.none, op: 0, context: 1, opacity: 1, face: false };
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
        } else if (mode === "section" || mode === "slab") {
            const single = mode === "section" || inspect.slab?.composite === "single";
            for (const axis of axes) {
                const at = pos[axis] + 0.5;
                const half = single ? 0 : inspect.slab.thickness[axis] / 2;
                const top = box[axis][1];
                out.planes.push({ axis, at, lo: clamp(at - half, 0, top), hi: clamp(at + half, 0, top) });
            }
            out.cut = axes.length ? (single ? CUT.planes : CUT.slabs) : CUT.none;
            out.op = single ? 0 : OPS[inspect.slab.composite] || OPS.max;
            out.context = inspect.context;
            out.opacity = mode === "section" ? inspect.opacity : 1;
        }
        if ((mode === "crop" || mode === "cutaway") && inspect.face && axes.length) {
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
        const out = {
            lo, hi,
            hole: null,
            plane: [0.5, 0.5, 0.5],
            planeOn: [0, 0, 0],
            half: [0, 0, 0],
            cut: resolved.cut,
            op: resolved.op,
            context: resolved.context,
            opacity: resolved.opacity,
            face: resolved.face ? 1 : 0,
        };
        if (resolved.hole) {
            const [hlo, hhi] = pair(resolved.hole);
            out.hole = { lo: hlo, hi: hhi };
        }
        for (const plane of resolved.planes) {
            const i = AXES.indexOf(plane.axis);
            // The middle of the interval drawn -- not `at`: a slab cut short
            // by the stack's edge is not centred on its plane any more.
            const lo = t(plane.axis, plane.lo);
            const hi = t(plane.axis, plane.hi);
            out.plane[i] = (lo + hi) / 2;
            out.planeOn[i] = 1;
            out.half[i] = Math.max(0, (hi - lo) / 2);
        }
        return out;
    }

    /** Whether two `toTexture` answers draw the same. */
    function sameTexture(a, b) {
        return JSON.stringify(a || null) === JSON.stringify(b || null);
    }

    /** The kept z planes, `[z0, z1]` inclusive, of a resolution: what the
     *  contrast is levelled over and the strip's band shows. */
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

    /** A thickness `voxels` thick along `axis`, in words. */
    function thicknessLabel(axis, voxels, space = null) {
        const step = stepOf(axis, space);
        const unit = axis === "z" ? (voxels === 1 ? "plane" : "planes") : "px";
        return step ? `${fixed(voxels * step.value)} ${step.unit}` : `${voxels} ${unit}`;
    }

    const MODE_WORDS = { full: "", cutaway: "cutaway", section: "section", slab: "slab", crop: "crop" };

    /**
     * The cut in a few words, for the caption, a capture and the agent:
     * "" for the whole stack, else e.g. "crop X 120–310 µm, Z planes 4–9",
     * "section Z z 12 · 24.0 µm", "slab X 512 px ×16 px (mean)".
     */
    function describe(inspect, pos, d, space = null) {
        const mode = inspect?.mode || "full";
        if (mode === "full") return "";
        const parts = active(inspect).map((axis) => {
            const name = axis.toUpperCase();
            if (mode === "crop") return `${name} ${label(axis, inspect.crop[axis], space)}`;
            if (mode === "cutaway") {
                const { at, keep } = inspect.cutaway[axis];
                return `${name} ${keep === "hi" ? "from" : "to"} ${label(axis, at, space)}`;
            }
            const where = label(axis, pos[axis], space);
            if (mode === "section" || inspect.slab.composite === "single") return `${name} ${where}`;
            return `${name} ${where} × ${thicknessLabel(axis, inspect.slab.thickness[axis], space)}`;
        });
        const op = mode === "slab" && inspect.slab.composite !== "single"
            ? ` (${inspect.slab.composite})` : "";
        return `${MODE_WORDS[mode]} ${parts.join(", ")}${op}`.trim();
    }

    global.PlexoraInspectGeometry = {
        MODES, AXES, COMPOSITES, CUT, OPS, CONTEXT,
        dims, spacing, defaults, thicknessFor, merge, clean, positions, active, resolve,
        toTexture, sameTexture, keptPlanes, label, thicknessLabel, describe,
    };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraInspectGeometry;
    }
})(typeof window !== "undefined" ? window : globalThis);
