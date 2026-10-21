/**
 * viewControls.js - which depth control is offered in which mode, declared once.
 *
 * Every control of the depth strip (views/depthStrip.js), of its settings
 * popover (views/depthSettings.js) and of any plugin that adds to them is an
 * ENTRY here: what it needs to mean anything, and in which modes. The strip
 * and the popover ask `resolve()` what to show and never decide it themselves,
 * so a control is never on screen where it does nothing -- the plane rail in
 * 3-D, where the plane is frozen; "follow the plane" over a single plane --
 * and a new 3-D tool takes part by declaring its entry
 * (docs/internal/3d/VIEW_CONTROLS.md, "Adding a control").
 *
 * An entry:
 *
 *     { id: "rotate.speed",            unique; a plugin's start "<plugin>."
 *       surfaces: ["popover"],         where it is drawn: "strip", "popover",
 *                                      or a plugin's own surface name
 *       modes: ["volume"],             "plane" | "slab" | "volume", or "*"
 *       needs: ["webgl2", "prefs"],    capabilities (CAPS), all must hold
 *       when: [],                      state flags (FLAGS) that must hold
 *       excludes: ["rotating"],        state flags, or ids, that hide it
 *       depends: [],                   ids that must be visible too
 *       config(ctx) { ... },           what it does in this context (an object)
 *       group: "rotate", order: 10,    where it sits among the others
 *       doc: "one line" }
 *
 * `excludes` and `depends` may name state flags, or entries that have no
 * `excludes` or `depends` of their own: resolving is two passes, never a
 * fixed point (checked at `register`). An unknown capability or flag throws
 * at `register`, so a typo fails at load, not as a control that never shows.
 *
 * The context (`context(viewState, extras)`) is built once per paint from the
 * view state and what the page has (`extras`: WebGL2, the section panes,
 * surfaces, the prefs, whether rotation is running). Resolving never changes
 * the view state. No DOM, no text, no icons: those belong to the views.
 *
 * The 3-D view's area (`volume.area`, placed with the Area tool,
 * views/areaTool.js) and its cut (the view state's `inspect`,
 * services/inspectGeometry.js) are part of the context too: `area`, `cut`
 * (full, cutaway, crop), the tool last chosen (`cutTool`) and its axes, and
 * whether a crop is narrower than the area (`cropNarrower`). The strip offers
 * the Area tool on a plane or a projection and the two cut tools in 3-D, and
 * the panel they open beside it (views/volumeDock.js) is the "dock" surface
 * -- its rows are the tool last chosen's, whether its cut is shown or hidden,
 * each for the representation (volume, surfaces or both) it applies to.
 *
 * Classic script, `window.PlexoraViewControls`.
 */
(function (global) {
    "use strict";

    const MODES = ["plane", "slab", "volume"];

    /** Capabilities: what the image and the page offer. */
    const CAPS = {
        depth: (ctx) => ctx.depth >= 2,
        frames: (ctx) => ctx.frames >= 2,
        depthOrFrames: (ctx) => ctx.depth >= 2 || ctx.frames >= 2,
        // A colour (RGB) stack is shown one plane at a time.
        colourless: (ctx) => !ctx.color,
        webgl2: (ctx) => Boolean(ctx.volumeSupported),
        sections: (ctx) => Boolean(ctx.sectionsSupported),
        zStep: (ctx) => Boolean(ctx.zStep),
        labels3d: (ctx) => Boolean(ctx.labelsAre3d),
        surfaces: (ctx) => Boolean(ctx.surfacesAvailable),
        surfacesPresent: (ctx) => ctx.surfaceCount > 0,
        surfaceStudio: (ctx) => Boolean(ctx.surfaceStudio),
        // Making a surface or importing a mesh is Plexora 3D; showing and
        // lighting the surfaces that exist is not (services/paidFeature.js).
        surfaceMaking: (ctx) => ctx.surfaceMaking !== false,
        // The popover's Surfaces row: surfaces to show, or one to make.
        surfaceRow: (ctx) => Boolean(ctx.surfacesAvailable) && (ctx.surfaceCount > 0 || ctx.surfaceMaking !== false),
        prefs: (ctx) => Boolean(ctx.prefs),
        grid: (ctx) => Boolean(ctx.grid),
        // The 3-D view can be cut (services/inspectState.js on the page).
        cut: (ctx) => Boolean(ctx.cutSupported),
        // Areas can be saved and restored (services/savedAreas.js on the page).
        savedAreas: (ctx) => Boolean(ctx.savedAreasSupported),
        // The volume is drawn: not "surfaces only". A row about voxels --
        // the cut faces, how voxels are read -- needs it.
        volumeDrawn: (ctx) => ctx.showVolume !== false,
    };

    /** State flags: what the user has chosen, for `when` and `excludes`. */
    const FLAGS = {
        "mode.plane": (ctx) => ctx.mode === "plane",
        "mode.slab": (ctx) => ctx.mode === "slab",
        "mode.volume": (ctx) => ctx.mode === "volume",
        following: (ctx) => ctx.mode === "slab" && ctx.follow,
        fixed: (ctx) => ctx.mode === "slab" && !ctx.follow,
        rotating: (ctx) => Boolean(ctx.rotating),
        sectionsOn: (ctx) => Boolean(ctx.sectionsOn),
        allPlanes: (ctx) => Boolean(ctx.allPlanes),
        percentile: (ctx) => ctx.mode === "slab" && ctx.op === "pct",
        gridOn: (ctx) => Boolean(ctx.gridOn),
        // The 3-D view's cut (only ever true in 3-D).
        "cut.full": (ctx) => ctx.mode === "volume" && ctx.cut === "full",
        "cut.on": (ctx) => ctx.mode === "volume" && ctx.cut !== "full",
        "cut.cutaway": (ctx) => ctx.mode === "volume" && ctx.cut === "cutaway",
        "cut.crop": (ctx) => ctx.mode === "volume" && ctx.cut === "crop",
        "axis.x": (ctx) => ctx.mode === "volume" && ctx.cut !== "full" && Boolean(ctx.cutAxes?.x),
        "axis.y": (ctx) => ctx.mode === "volume" && ctx.cut !== "full" && Boolean(ctx.cutAxes?.y),
        "axis.z": (ctx) => ctx.mode === "volume" && ctx.cut !== "full" && Boolean(ctx.cutAxes?.z),
        // A crop narrower than the area in x or y: "Crop to area" tightens it.
        "crop.narrower": (ctx) => ctx.mode === "volume" && ctx.cut === "crop" && Boolean(ctx.cropNarrower),
        // At least one area saved (services/savedAreas.js).
        "areas.saved": (ctx) => ctx.savedCount > 0,
    };

    /** Where each group sits, strip top to bottom, popover top to bottom. */
    const GROUPS = {
        mode: 10, sections: 20, cut: 25, play: 30, rail: 40, cog: 60, frame: 70,
        projection: 100, volume: 110, rotate: 130, grid: 140, surfaces: 150,
        // The cut panel beside the strip, top to bottom (views/volumeDock.js):
        // the axes, the actions to their right, then a row each.
        axes: 200, actions: 210, voxels: 220, handles: 230, areas: 235, toggle: 240,
        plugins: 800,
    };

    const entries = new Map();

    function event(name, detail) {
        try {
            global.dispatchEvent?.(new CustomEvent(name, { detail }));
        } catch (error) {
            // No CustomEvent in a bare runtime.
        }
    }

    function list(value) {
        if (value === undefined || value === null) return [];
        return Array.isArray(value) ? value : [value];
    }

    /**
     * Add an entry; returns the function that takes it away again. Throws on
     * a duplicate id, an unknown capability, flag or mode, or an `excludes` /
     * `depends` on an entry that has its own.
     */
    function register(entry, { announce = true } = {}) {
        const id = String(entry?.id || "");
        if (!id) throw new Error("viewControls: an entry needs an id");
        if (entries.has(id)) throw new Error(`viewControls: "${id}" is registered already`);
        const modes = entry.modes === "*" || entry.modes === undefined ? "*" : list(entry.modes);
        if (modes !== "*") {
            for (const mode of modes) {
                if (!MODES.includes(mode)) throw new Error(`viewControls: "${id}": no mode "${mode}"`);
            }
        }
        const needs = list(entry.needs);
        for (const cap of needs) {
            if (!CAPS[cap]) throw new Error(`viewControls: "${id}": no capability "${cap}"`);
        }
        const when = list(entry.when);
        for (const flag of when) {
            if (!FLAGS[flag]) throw new Error(`viewControls: "${id}": no state flag "${flag}"`);
        }
        const excludes = list(entry.excludes);
        const depends = list(entry.depends);
        for (const name of [...excludes, ...depends]) {
            if (FLAGS[name]) continue;
            const other = entries.get(name);
            if (!other) throw new Error(`viewControls: "${id}": no flag or entry "${name}"`);
            if (other.excludes.length || other.depends.length) {
                throw new Error(`viewControls: "${id}": "${name}" has excludes or depends of its own`);
            }
        }
        const held = {
            id,
            surfaces: list(entry.surfaces).length ? list(entry.surfaces) : ["strip"],
            modes, needs, when, excludes, depends,
            config: typeof entry.config === "function" ? entry.config : null,
            group: entry.group || "plugins",
            order: Number.isFinite(entry.order) ? entry.order : 50,
            doc: entry.doc || "",
        };
        entries.set(id, held);
        if (announce) event("plexora:view-controls-changed", { id, registered: true });
        return () => unregister(id);
    }

    function unregister(id) {
        if (!entries.delete(String(id))) return;
        event("plexora:view-controls-changed", { id: String(id), registered: false });
    }

    /** The context every entry is resolved against. `extras` is an object or
     *  a function returning one (the page's: main.js coreExtras). */
    function context(viewState, extras = {}) {
        const state = viewState?.get?.() || {};
        const more = typeof extras === "function" ? extras() : (extras || {});
        const depth = Math.max(1, Number(viewState?.depth) || 1);
        const slab = state.slab || {};
        const z0 = Number.isFinite(slab.z0) ? slab.z0 : 0;
        const z1 = Number.isFinite(slab.z1) ? slab.z1 : depth - 1;
        const inspect = state.inspect || {};
        // The planes the 3-D view keeps (a Z crop's or cutaway's), for the
        // popover's planes row.
        const kept = keptPlanes(inspect, state, depth);
        const info = viewState?.info || {};
        const width = Math.max(1, Number(info.width) || 1);
        const height = Math.max(1, Number(info.height) || 1);
        const area = state.volume?.area || null;
        // Lengths in microns only where the image states its spacing.
        const space = global.PlexoraInspectGeometry?.spacing?.(info) || {};
        return {
            mode: MODES.includes(state.mode) ? state.mode : "plane",
            layout: state.layout || "single",
            sectionsOn: state.layout === "sections" && state.mode !== "volume",
            follow: slab.follow !== false,
            op: slab.op || "max",
            z: Number.isFinite(state.z) ? state.z : 0,
            z0, z1,
            allPlanes: state.mode === "volume" ? kept[0] <= 0 && kept[1] >= depth - 1
                : z0 <= 0 && z1 >= depth - 1,
            cut: CUTS.includes(inspect.mode) ? inspect.mode : "full",
            cutTool: CUTS.includes(inspect.mode) && inspect.mode !== "full" ? inspect.mode
                : (CUTS.includes(inspect.tool) && inspect.tool !== "full" ? inspect.tool : "cutaway"),
            cutAxes: { x: inspect.axes?.x === true, y: inspect.axes?.y === true,
                       z: inspect.axes?.z === true },
            keptZ0: kept[0],
            keptZ1: kept[1],
            area,
            width,
            height,
            areaUnit: space.x ? "µm" : "px",
            cropNarrower: cropNarrower(inspect, area, width, height),
            savedCount: Array.isArray(state.volume?.saved) ? state.volume.saved.length : 0,
            savedAreasSupported: false,
            showVolume: state.volume?.show?.volume !== false,
            cutSupported: false,
            t: Number.isFinite(state.t) ? state.t : 0,
            depth,
            frames: Math.max(1, Number(viewState?.frames) || 1),
            color: Boolean(viewState?.color),
            zStep: viewState?.zStep?.() || null,
            labelsAre3d: false,
            volumeSupported: false,
            sectionsSupported: false,
            surfacesAvailable: false,
            surfaceCount: 0,
            surfaceStudio: false,
            rotating: false,
            grid: false,
            gridOn: false,
            prefs: null,
            ...more,
        };
    }

    const CUTS = ["full", "cutaway", "crop"];

    /** Whether a crop keeps less than `area` (or the image) across x or y. */
    function cropNarrower(inspect, area, width, height) {
        if (inspect.mode !== "crop" || !inspect.crop) return false;
        const whole = area ? { x: [area.x, area.x + area.width], y: [area.y, area.y + area.height] }
            : { x: [0, width], y: [0, height] };
        return ["x", "y"].some((axis) => {
            const range = inspect.crop[axis];
            if (!inspect.axes?.[axis] || !Array.isArray(range)) return false;
            return range[0] > whole[axis][0] + 0.5 || range[1] < whole[axis][1] - 0.5;
        });
    }

    /** `[z0, z1]`: the planes a Z crop or cutaway keeps, every plane else. */
    function keptPlanes(inspect, state, depth) {
        const top = depth - 1;
        if (!inspect.axes?.z) return [0, top];
        if (inspect.mode === "crop" && Array.isArray(inspect.crop?.z)) return inspect.crop.z.slice();
        if (inspect.mode === "cutaway" && inspect.cutaway?.z) {
            const { at, keep } = inspect.cutaway.z;
            return keep === "lo" ? [0, at] : [at, top];
        }
        return [0, top];
    }

    function inMode(held, ctx) {
        return held.modes === "*" || held.modes.includes(ctx.mode);
    }

    /** Pass one: modes, capabilities and `when`, wherever it is drawn. */
    function base(held, ctx) {
        return inMode(held, ctx) && held.needs.every((cap) => CAPS[cap](ctx))
            && held.when.every((flag) => FLAGS[flag](ctx));
    }

    function shown(held, ctx, first) {
        if (!first(held)) return false;
        for (const name of held.excludes) {
            if (FLAGS[name] ? FLAGS[name](ctx) : first(entries.get(name))) return false;
        }
        for (const name of held.depends) {
            if (FLAGS[name] ? !FLAGS[name](ctx) : !first(entries.get(name))) return false;
        }
        return true;
    }

    function passOne(ctx) {
        const memo = new Map();
        return (held) => {
            if (!held) return false;
            if (!memo.has(held.id)) memo.set(held.id, base(held, ctx));
            return memo.get(held.id);
        };
    }

    function configOf(id, ctx) {
        const held = entries.get(String(id));
        if (!held?.config) return {};
        try {
            return held.config(ctx) || {};
        } catch (error) {
            console.error(`viewControls: "${id}" config failed`, error);
            return {};
        }
    }

    /**
     * What to show on `surface` (all surfaces when omitted), in order:
     * `[{id, group, config}]`. Never changes the view state.
     */
    function resolve(ctx, { surface = null } = {}) {
        const first = passOne(ctx);
        const out = [];
        for (const held of entries.values()) {
            if (surface && !held.surfaces.includes(surface)) continue;
            if (!shown(held, ctx, first)) continue;
            out.push(held);
        }
        out.sort((a, b) => ((GROUPS[a.group] ?? 500) - (GROUPS[b.group] ?? 500))
            || (a.order - b.order) || a.id.localeCompare(b.id));
        return out.map((held) => ({ id: held.id, group: held.group, config: configOf(held.id, ctx) }));
    }

    /** Whether entry `id` is shown in `ctx`. False for an unknown id. */
    function visible(id, ctx) {
        const held = entries.get(String(id));
        return held ? shown(held, ctx, passOne(ctx)) : false;
    }

    /**
     * The view-state patch that switches to `mode` from `ctx`. The 3-D view
     * keeps its own cut (`inspect`), so the projection's range is left as it
     * was and comes back with the projection.
     */
    function transition(ctx, mode) {
        return { mode };
    }

    /** Call `fn` whenever an entry comes or goes; returns the undo. */
    function onChange(fn) {
        const handler = (e) => fn(e?.detail || {});
        global.addEventListener?.("plexora:view-controls-changed", handler);
        return () => global.removeEventListener?.("plexora:view-controls-changed", handler);
    }

    // -- core's own entries -------------------------------------------------
    //
    // The strip, top to bottom, then the popover's groups. The matrix these
    // make is in docs/internal/3d/VIEW_CONTROLS.md and pinned by
    // tests/js/view_controls_probe.mjs.

    const CORE = [
        { id: "mode.plane", surfaces: ["strip"], needs: ["depth", "colourless"], group: "mode", order: 10,
          doc: "Show one plane." },
        { id: "mode.slab", surfaces: ["strip"], needs: ["depth", "colourless"], group: "mode", order: 20,
          doc: "Show a projection of a range of planes." },
        { id: "mode.volume", surfaces: ["strip"], needs: ["depth", "colourless", "webgl2"], group: "mode",
          order: 30, doc: "Show the stack in 3-D." },
        // On a plane or a projection, a square to place: the 3-D view of that
        // area (views/areaTool.js). V opens it under the pointer.
        { id: "tool.area", surfaces: ["strip"], modes: ["plane", "slab"],
          needs: ["depth", "colourless", "webgl2"], group: "mode", order: 40,
          doc: "Area: place a square on the image and see that part of the stack in 3-D (V)." },
        // In 3-D, the two ways to cut the stack open; each opens the panel
        // beside the strip (views/volumeDock.js).
        { id: "tool.cutaway", surfaces: ["strip"], modes: ["volume"], needs: ["depth", "cut"], group: "cut",
          order: 10, doc: "Cut: take a side off the stack along X, Y or Z." },
        { id: "tool.crop", surfaces: ["strip"], modes: ["volume"], needs: ["depth", "cut"], group: "cut",
          order: 20, doc: "Crop: keep a box of the stack." },
        { id: "sections", surfaces: ["strip"], modes: ["plane", "slab"],
          needs: ["depth", "sections", "colourless"], group: "sections",
          doc: "XZ and YZ sections beside the plane. Not in 3-D, which is its own view." },
        { id: "play", surfaces: ["strip"], needs: ["depthOrFrames"], group: "play",
          config: (ctx) => ({
              kind: ctx.mode === "volume" ? "rotate"
                  : (ctx.depth < 2 ? "frames" : (ctx.mode === "slab" && !ctx.follow ? "band" : "planes")),
          }),
          doc: "Step through the planes (a fixed projection: slide its range; 3-D: turn the stack)." },
        // Not in 3-D: the cut there is the panel's and the handles' (views/volumeDock.js).
        { id: "rail", surfaces: ["strip"], modes: ["plane", "slab"], needs: ["depth"], group: "rail", order: 10,
          config: (ctx) => ({
              plane: true,
              band: ctx.mode !== "plane",
              bandMove: ctx.mode === "slab" && !ctx.follow,
          }),
          doc: "The planes, z 0 at the top: the plane on screen, or the range drawn." },
        { id: "band", surfaces: ["strip"], modes: ["slab"], needs: ["depth"], group: "rail", order: 20,
          doc: "The projection's range." },
        { id: "thumb", surfaces: ["strip"], modes: ["plane", "slab"], needs: ["depth"], group: "rail", order: 30,
          doc: "The plane on screen." },
        { id: "hover", surfaces: ["strip"], modes: ["plane", "slab"], needs: ["depth"], group: "rail", order: 40,
          doc: "The plane a click would choose, which starts loading under the pointer." },
        { id: "cog", surfaces: ["strip"], needs: ["depthOrFrames"], group: "cog",
          doc: "The settings that apply to what is drawn now." },
        { id: "frame", surfaces: ["strip"], needs: ["frames"], group: "frame",
          doc: "Which frame of a time series, and a step either way." },

        { id: "projection.op", surfaces: ["popover"], modes: ["plane", "slab"],
          needs: ["depth", "colourless"], group: "projection", order: 10,
          config: (ctx) => ({ value: ctx.mode === "plane" ? "plane" : ctx.op }),
          doc: "One plane, or how a range of planes is combined." },
        { id: "projection.percentile", surfaces: ["popover"], modes: ["slab"], when: ["percentile"],
          needs: ["depth", "colourless"], group: "projection", order: 20, doc: "The percentile." },
        { id: "projection.range", surfaces: ["popover"], modes: ["slab"], needs: ["depth", "colourless"],
          group: "projection", order: 30, doc: "The planes combined." },
        { id: "projection.follow", surfaces: ["popover"], modes: ["slab"], needs: ["depth", "colourless"],
          group: "projection", order: 40, doc: "Keep the range centred on the plane as it moves." },
        { id: "volume.composite", surfaces: ["popover"], modes: ["volume"], needs: ["webgl2"],
          group: "volume", order: 10, doc: "Render: maximum intensity or additive." },
        { id: "area.size", surfaces: ["popover"], modes: ["plane", "slab"],
          needs: ["depth", "colourless", "webgl2", "prefs"], group: "volume", order: 15,
          config: (ctx) => ({ unit: ctx.areaUnit }),
          doc: "The Area tool's square: its side, in microns where the image states its spacing." },
        // How the viewer moves, for every image: in every mode, as it is
        // the plane fades' and the 3-D view's coasting alike.
        { id: "view.motion", surfaces: ["popover"], needs: ["depthOrFrames", "prefs"],
          group: "volume", order: 20, doc: "Motion: planes fade into each other, the 3-D view coasts after a drag." },
        { id: "rotate.axis", surfaces: ["popover"], modes: ["volume"], needs: ["webgl2", "prefs"],
          group: "rotate", order: 10, doc: "What Play turns the stack about, and which way." },
        { id: "rotate.speed", surfaces: ["popover"], modes: ["volume"], needs: ["webgl2", "prefs"],
          group: "rotate", order: 20, doc: "How fast Play turns the stack." },
        { id: "grid.on", surfaces: ["popover"], modes: ["volume"], needs: ["webgl2", "grid"],
          group: "grid", order: 10, doc: "A reference grid on the box's far walls: where, and its numbers." },
        { id: "grid.options", surfaces: ["popover"], modes: ["volume"], needs: ["webgl2", "grid"],
          when: ["gridOn"], group: "grid", order: 20, doc: "The grid's spacing and line strength." },
        // One row: what is drawn (once there is a surface), and a new
        // surface or a mesh where Plexora 3D allows making one.
        { id: "surfaces", surfaces: ["popover"], modes: ["volume"], needs: ["surfaceRow"],
          group: "surfaces", order: 10,
          config: (ctx) => ({ show: ctx.surfaceCount > 0, make: ctx.surfaceMaking !== false,
                              studio: Boolean(ctx.surfaceStudio) }),
          doc: "Volume, surfaces or both; a new surface (Surface Studio) or an imported mesh." },
        { id: "surfaces.light", surfaces: ["popover"], modes: ["volume"], needs: ["surfaces", "surfacesPresent"],
          group: "surfaces", order: 20, doc: "Where the light on the surfaces comes from." },

        // The cut panel (views/volumeDock.js), for the tool last chosen,
        // whether its cut is shown or hidden (O): the panel stays out until
        // its ×. Its tools' own handles cut in the view (views/volumeHandles.js).
        { id: "cut.axes", surfaces: ["dock"], modes: ["volume"], needs: ["cut"],
          group: "axes",
          config: (ctx) => ({ flip: ctx.cutTool === "cutaway", single: ctx.cutTool === "cutaway" }),
          doc: "Which axes are cut: a Cut along one of X, Y or Z, and the side it keeps; a Crop along any." },
        { id: "cut.face", surfaces: ["dock"], modes: ["volume"], needs: ["cut", "volumeDrawn"],
          group: "actions", order: 10, doc: "Draw the voxels on the cut faces solid." },
        { id: "cut.handles", surfaces: ["dock"], modes: ["volume"], needs: ["cut", "prefs"],
          group: "actions", order: 20, doc: "Show or hide the handles that drag the cut in the view." },
        { id: "cut.reset", surfaces: ["dock"], modes: ["volume"], needs: ["cut"],
          group: "actions", order: 30, doc: "Put the tool's cut back where it starts." },
        { id: "cut.toArea", surfaces: ["dock"], modes: ["volume"], needs: ["cut"],
          when: ["crop.narrower"], group: "actions", order: 35,
          doc: "Crop to area: the area becomes the crop, fetched finer and framed." },
        { id: "cut.history", surfaces: ["dock"], modes: ["volume"], needs: ["cut"],
          group: "actions", order: 40, doc: "Undo the last change to the cut." },
        { id: "cut.interpolation", surfaces: ["dock"], modes: ["volume"], needs: ["cut", "volumeDrawn"],
          group: "voxels", doc: "Read voxels smoothly, or as the blocks they are." },
        { id: "cut.handleOpacity", surfaces: ["dock"], modes: ["volume"], needs: ["cut", "prefs"],
          group: "handles", doc: "How strongly the handles are drawn." },
        { id: "cut.toggle", surfaces: ["dock"], modes: ["volume"], needs: ["cut"],
          group: "toggle", doc: "Show or hide the cut (O), the panel left out." },
        // Areas kept to come back to (services/savedAreas.js): the area, the
        // cut and the camera, under a name, with a thumbnail.
        { id: "area.save", surfaces: ["dock"], modes: ["volume"], needs: ["savedAreas"],
          group: "areas", order: 10, doc: "Save what is shown: the area, the cut and the camera." },
        { id: "area.list", surfaces: ["dock"], modes: ["volume"], needs: ["savedAreas"],
          when: ["areas.saved"], group: "areas", order: 20,
          doc: "The areas saved: a click goes back to one, × forgets it, a double-click renames it." },
    ];
    for (const entry of CORE) register(entry, { announce: false });

    global.PlexoraViewControls = {
        MODES, CAPS, FLAGS, GROUPS, CUTS,
        register, unregister, context, resolve, visible, configOf, transition, onChange,
        /** Every entry, for the docs and the probe. */
        entries: () => [...entries.values()].map((held) => ({ ...held })),
    };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraViewControls;
    }
})(typeof window !== "undefined" ? window : globalThis);
