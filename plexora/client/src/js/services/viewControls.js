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
 * The 3-D view's cut (the view state's `inspect`, services/inspectGeometry.js)
 * is part of the context too: `cut` (full, cutaway, section, slab, crop) and
 * its axes, and the 3-D dock (views/volumeDock.js) is the "dock" surface --
 * a row there shows only in the spatial modes, on the axes and for the
 * representation (volume, surfaces or both) it applies to.
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
        prefs: (ctx) => Boolean(ctx.prefs),
        card: (ctx) => typeof ctx.openCard === "function",
        grid: (ctx) => Boolean(ctx.grid),
        // The 3-D view can be cut (services/inspectState.js on the page).
        cut: (ctx) => Boolean(ctx.cutSupported),
        // The volume is drawn: not "surfaces only". A row about voxels --
        // how a slab combines them, the surroundings, the cut faces -- needs it.
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
        "cut.section": (ctx) => ctx.mode === "volume" && ctx.cut === "section",
        "cut.slab": (ctx) => ctx.mode === "volume" && ctx.cut === "slab",
        "cut.crop": (ctx) => ctx.mode === "volume" && ctx.cut === "crop",
        // Sections or slabs: planes through the stack, the rest dimmed.
        "cut.planes": (ctx) => ctx.mode === "volume" && (ctx.cut === "section" || ctx.cut === "slab"),
        // A crop or a cutaway: a box with sides taken off.
        "cut.box": (ctx) => ctx.mode === "volume" && (ctx.cut === "crop" || ctx.cut === "cutaway"),
        // A slab whose voxels combine (not a single plane).
        "cut.combines": (ctx) => ctx.mode === "volume" && ctx.cut === "slab" && ctx.slabComposite !== "single",
        "axis.x": (ctx) => ctx.mode === "volume" && ctx.cut !== "full" && Boolean(ctx.cutAxes?.x),
        "axis.y": (ctx) => ctx.mode === "volume" && ctx.cut !== "full" && Boolean(ctx.cutAxes?.y),
        "axis.z": (ctx) => ctx.mode === "volume" && ctx.cut !== "full" && Boolean(ctx.cutAxes?.z),
        // In 3-D, nothing on the rail to pick: the plane is the Z section's
        // only in a section or slab along Z.
        "cut.noPlane": (ctx) => ctx.mode === "volume" && !railPlane(ctx),
        // In 3-D, no range on the rail: the whole stack, a Z crop or cutaway,
        // or a combining Z slab have one.
        "cut.noBand": (ctx) => ctx.mode === "volume" && !railBand(ctx),
    };

    /** In 3-D, whether a press on the rail moves the Z section or slab. */
    function railPlane(ctx) {
        return (ctx.cut === "section" || ctx.cut === "slab") && Boolean(ctx.cutAxes?.z);
    }

    /** In 3-D, whether the rail shows a range: the planes kept. */
    function railBand(ctx) {
        if (ctx.cut === "full") return true;
        if (!ctx.cutAxes?.z) return false;
        return ctx.cut === "crop" || ctx.cut === "cutaway"
            || (ctx.cut === "slab" && ctx.slabComposite !== "single");
    }

    /** Where each group sits, strip top to bottom, popover top to bottom. */
    const GROUPS = {
        mode: 10, sections: 20, play: 30, rail: 40, readout: 50, cog: 60, frame: 70,
        projection: 100, volume: 110, clip: 120, rotate: 130, grid: 140, surfaces: 150,
        // The 3-D dock, top to bottom (views/volumeDock.js).
        cut: 200, axes: 210, positions: 220, slab: 230, look: 240, tools: 250, advanced: 260,
        plugins: 800, footer: 900,
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
        // rail and the popover's planes row.
        const kept = keptPlanes(inspect, state, depth);
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
            cutAxes: { x: inspect.axes?.x === true, y: inspect.axes?.y === true,
                       z: inspect.axes?.z !== false },
            slabComposite: inspect.slab?.composite || "max",
            keptZ0: kept[0],
            keptZ1: kept[1],
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
            openCard: null,
            ...more,
        };
    }

    const CUTS = ["full", "cutaway", "section", "slab", "crop"];

    /** `[z0, z1]`: the planes a Z crop or cutaway keeps, every plane else. */
    function keptPlanes(inspect, state, depth) {
        const top = depth - 1;
        if (!inspect.axes?.z) return [0, top];
        if (inspect.mode === "crop" && Array.isArray(inspect.crop?.z)) return inspect.crop.z.slice();
        if (inspect.mode === "cutaway" && inspect.cutaway?.z) {
            const { at, keep } = inspect.cutaway.z;
            return keep === "lo" ? [0, at] : [at, top];
        }
        if (inspect.mode === "slab" && inspect.slab?.composite !== "single") {
            const half = (inspect.slab?.thickness?.z || 1) / 2;
            const z = Number.isFinite(state.z) ? state.z : 0;
            return [Math.max(0, Math.floor(z + 0.5 - half)), Math.min(top, Math.ceil(z + 0.5 + half) - 1)];
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
        { id: "sections", surfaces: ["strip"], modes: ["plane", "slab"],
          needs: ["depth", "sections", "colourless"], group: "sections",
          doc: "XZ and YZ sections beside the plane. Not in 3-D, which is its own view." },
        { id: "play", surfaces: ["strip"], needs: ["depthOrFrames"], group: "play",
          config: (ctx) => ({
              kind: ctx.mode === "volume" ? "rotate"
                  : (ctx.depth < 2 ? "frames" : (ctx.mode === "slab" && !ctx.follow ? "band" : "planes")),
          }),
          doc: "Step through the planes (a fixed projection: slide its range; 3-D: turn the stack)." },
        { id: "rail", surfaces: ["strip"], needs: ["depth"], group: "rail", order: 10,
          config: (ctx) => (ctx.mode === "volume" ? {
              // In 3-D the rail is the cut along Z: a press moves a Z
              // section or slab; the whole stack's range, or a Z crop's, is
              // resized (a crop it becomes) and moved; a Z cutaway's one cut
              // moves (`cut`). Nothing else along Z is the rail's.
              plane: railPlane(ctx),
              band: railBand(ctx) && ctx.cut !== "slab",
              bandMove: ctx.cut === "full" || ctx.cut === "crop",
              cut: ctx.cut,
          } : {
              plane: true,
              band: ctx.mode !== "plane",
              bandMove: ctx.mode === "slab" && !ctx.follow,
          }),
          doc: "The planes, z 0 at the top: the plane on screen, the range drawn, or in 3-D the cut along Z." },
        { id: "band", surfaces: ["strip"], modes: ["slab", "volume"], needs: ["depth"], group: "rail",
          order: 20, excludes: ["cut.noBand"],
          doc: "The projection's range; in 3-D, the planes the cut keeps along Z." },
        { id: "thumb", surfaces: ["strip"], needs: ["depth"], group: "rail", order: 30, excludes: ["cut.noPlane"],
          doc: "The plane on screen; in 3-D, a Z section's or slab's plane." },
        { id: "hover", surfaces: ["strip"], needs: ["depth"], group: "rail", order: 40, excludes: ["cut.noPlane"],
          doc: "The plane a click would choose, which starts loading under the pointer." },
        { id: "readout", surfaces: ["strip"], needs: ["depth"], group: "readout",
          doc: "What is drawn, in numbers." },
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
          group: "volume", order: 10, doc: "Maximum intensity or additive." },
        { id: "volume.view", surfaces: ["popover"], modes: ["volume"], needs: ["webgl2"],
          group: "volume", order: 20, doc: "Look along an axis, or the three-quarter view." },
        { id: "volume.clip", surfaces: ["popover"], modes: ["volume"], needs: ["depth", "cut"],
          group: "clip", order: 10, doc: "How the 3-D view is cut, and the 3-D dock that cuts it." },
        { id: "rotate.speed", surfaces: ["popover"], modes: ["volume"], needs: ["webgl2", "prefs"],
          group: "rotate", order: 10, doc: "How fast Play turns the stack." },
        { id: "rotate.axis", surfaces: ["popover"], modes: ["volume"], needs: ["webgl2", "prefs"],
          group: "rotate", order: 20, doc: "What Play turns the stack about." },
        { id: "rotate.direction", surfaces: ["popover"], modes: ["volume"], needs: ["webgl2", "prefs"],
          group: "rotate", order: 30, doc: "Which way it turns." },
        { id: "grid.on", surfaces: ["popover"], modes: ["volume"], needs: ["webgl2", "grid"],
          group: "grid", order: 10, doc: "A reference grid on the box's far walls." },
        { id: "grid.options", surfaces: ["popover"], modes: ["volume"], needs: ["webgl2", "grid"],
          when: ["gridOn"], group: "grid", order: 20, doc: "Where the grid is drawn, its spacing, strength and numbers." },
        { id: "surfaces.show", surfaces: ["popover"], modes: ["volume"], needs: ["surfaces", "surfacesPresent"],
          group: "surfaces", order: 10, doc: "Volume, surfaces, or both." },
        { id: "surfaces.light", surfaces: ["popover"], modes: ["volume"], needs: ["surfaces", "surfacesPresent"],
          group: "surfaces", order: 20, doc: "Where the light on the surfaces comes from." },
        { id: "surfaces.new", surfaces: ["popover"], modes: ["volume"],
          needs: ["surfaceStudio", "surfaceMaking"],
          group: "surfaces", order: 30, doc: "Make a surface (Surface Studio)." },
        { id: "surfaces.import", surfaces: ["popover"], modes: ["volume"],
          needs: ["surfaces", "surfaceMaking"],
          group: "surfaces", order: 40, doc: "Import a mesh." },
        { id: "card", surfaces: ["popover"], needs: ["card"], group: "footer",
          doc: "The Depth card, for everything less frequent." },
        { id: "card.projection", surfaces: ["card"], modes: ["plane", "slab"], needs: ["depth", "colourless"],
          group: "projection", doc: "The Depth card's projection rows: the operation, the planes, Follow." },

        // The 3-D dock (views/volumeDock.js): cutting the stack.
        { id: "cut.mode", surfaces: ["dock"], modes: ["volume"], needs: ["cut"], group: "cut",
          doc: "The whole stack, a cutaway, sections, slabs or a crop." },
        { id: "cut.axes", surfaces: ["dock"], modes: ["volume"], needs: ["cut"], when: ["cut.on"],
          group: "axes", doc: "Which of X, Y and Z are cut." },
        ...["x", "y", "z"].map((axis, i) => ({
            id: `cut.${axis}`, surfaces: ["dock"], modes: ["volume"], needs: ["cut"], when: [`axis.${axis}`],
            group: "positions", order: 10 + i,
            config: (ctx) => ({
                // A crop keeps a range; a cutaway moves one cut and flips
                // the side kept; a section or slab moves its plane.
                kind: ctx.cut === "crop" ? "range" : (ctx.cut === "cutaway" ? "cut" : "plane"),
            }),
            doc: `Where the stack is cut along ${axis.toUpperCase()}.` })),
        { id: "cut.combine", surfaces: ["dock"], modes: ["volume"], needs: ["cut", "volumeDrawn"],
          when: ["cut.slab"], group: "slab", order: 10,
          doc: "How a slab's voxels combine along each ray: one plane, maximum, mean, minimum or sum." },
        { id: "cut.thickness", surfaces: ["dock"], modes: ["volume"], needs: ["cut"], when: ["cut.combines"],
          group: "slab", order: 20, doc: "How thick the slabs are." },
        { id: "cut.opacity", surfaces: ["dock"], modes: ["volume"], needs: ["cut"], when: ["cut.section"],
          group: "look", order: 10, doc: "How opaque the section planes are." },
        { id: "cut.context", surfaces: ["dock"], modes: ["volume"], needs: ["cut", "volumeDrawn"],
          when: ["cut.planes"], group: "look", order: 20,
          doc: "The rest of the stack, dimmed, around the sections or slabs." },
        { id: "cut.face", surfaces: ["dock"], modes: ["volume"], needs: ["cut", "volumeDrawn"],
          when: ["cut.box"], group: "look", order: 30,
          doc: "Draw the voxels on the cut faces solid." },
        { id: "cut.handles", surfaces: ["dock"], modes: ["volume"], needs: ["cut", "prefs"], when: ["cut.on"],
          group: "tools", order: 10, doc: "Handles in the view to drag the cuts." },
        { id: "cut.reset", surfaces: ["dock"], modes: ["volume"], needs: ["cut"], when: ["cut.on"],
          group: "tools", order: 20, doc: "Put an axis, the mode or every cut back." },
        { id: "cut.history", surfaces: ["dock"], modes: ["volume"], needs: ["cut"], group: "tools", order: 30,
          doc: "Undo and redo a cut." },
        { id: "cut.interpolation", surfaces: ["dock"], modes: ["volume"], needs: ["cut", "volumeDrawn"],
          group: "advanced", order: 10, doc: "Read voxels smoothly, or as the blocks they are." },
        { id: "cut.handleOpacity", surfaces: ["dock"], modes: ["volume"], needs: ["cut", "prefs"],
          when: ["cut.on"], group: "advanced", order: 20, doc: "How strongly the handles are drawn." },
        { id: "cut.link", surfaces: ["dock"], modes: ["volume"], needs: ["cut", "sections"], when: ["cut.planes"],
          group: "advanced", order: 30,
          doc: "The XZ and YZ panes cut where the X and Y sections are, not at the view's centre." },
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
