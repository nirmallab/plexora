/**
 * depthSettings.js - the depth strip's cog: the settings for what is drawn now.
 *
 * A small popover beside the strip (views/depthStrip.js) holding only what
 * applies to the mode on screen, as services/viewControls.js resolves it for
 * the "popover" surface:
 *
 *     one plane    Show: one plane or a projection's operation; Motion
 *     projection   the operation, its percentile, the planes combined,
 *                  Follow; Motion
 *     3-D          Render (MIP / Additive), Motion; how Play turns the stack;
 *                  the grid; the surfaces (the cut is the strip's Cut and
 *                  Crop, and the panel they open, views/volumeDock.js)
 *
 * ONE LINE A SETTING. A row is a quiet label in a gutter and its controls
 * beside it; a hairline between groups is the only heading (a group's name
 * is its aria-label), and what a control does is its tooltip, not a note
 * under it. Controls that mean nothing in the state on screen -- the grid's
 * options while it is off -- are hidden, not greyed.
 *
 * Nothing in it is permanent furniture: it opens on the cog and closes on the
 * next click elsewhere, Escape, a resize or a scroll outside it. Open, it
 * follows the view: a mode changed by the strip, a key or an agent rebuilds
 * its rows in place, keeping focus on a control that is still there; values
 * move with the state but a field being typed in is never overwritten.
 *
 * Where each value lives is unchanged by it: the projection and the
 * composite in the view state (per image, services/viewState.js); Motion, how
 * Play turns and the grid in the viewer prefs (per user,
 * services/viewerPrefs.js). A value set in one mode is there when that mode
 * comes back. What is set less often -- an exact plane, the z step, a
 * projection added as a layer -- is the Depth card's (views/depthTools.js).
 *
 * A plugin's popover row: `ctx.view.controls.register(entry, {row})` with
 * `row(ctx, config, popover) -> {node, update(ctx, config)}` (main.js).
 *
 * Classic script, `window.PlexoraDepthSettings`.
 */
(function (global) {
    "use strict";

    //: Each group's name, for assistive tech: on screen a hairline parts
    //: the groups and the rows' labels say the rest.
    const GROUP_NAMES = {
        projection: "Projection", volume: "View", rotate: "Play turns the stack",
        grid: "Grid", surfaces: "Surfaces",
    };
    const OPS = [["plane", "One plane"], ["max", "Maximum (MIP)"], ["mean", "Mean"],
                 ["min", "Minimum"], ["std", "Std. deviation"], ["med", "Median"],
                 ["pct", "Percentile"]];
    //: What Play turns the stack about, and which way: [value, text, glyph, title].
    const AXES = [["screen", "", "arrows-up-down", "About the screen's vertical"],
                  ["x", "X", null, "About the image's X axis"],
                  ["y", "Y", null, "About the image's Y axis"],
                  ["z", "Z", null, "About the image's Z axis"]];
    const DIRECTIONS = [[1, "", "rotate-right", "Clockwise, seen from above"],
                        [-1, "", "rotate-left", "Anticlockwise, seen from above"]];
    //: The grid's line strengths, faintest first (the 3-D view's grid).
    const GRID_STRENGTHS = [[0.18, "faint"], [0.32, "medium"], [0.5, "strong"]];
    const GAP = 8;

    function el(tag, className, attrs = {}) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        for (const [key, value] of Object.entries(attrs)) {
            if (value === null || value === undefined) continue;
            if (key === "text") node.textContent = value;
            else node.setAttribute(key, value);
        }
        return node;
    }

    function icon(name) {
        return el("span", `fas fa-${name}`, { "aria-hidden": "true" });
    }

    function portal(node) {
        if (typeof PopoverPortal !== "undefined") PopoverPortal.attach(node);
        else document.documentElement.appendChild(node);
    }

    function unportal(node) {
        if (typeof PopoverPortal !== "undefined") PopoverPortal.detach(node);
        else node.remove();
    }

    function int(value, fallback) {
        const number = Math.round(Number(value));
        return Number.isFinite(number) ? number : fallback;
    }

    /** True while `node` (or something in it) is being typed in. */
    function editing(node) {
        const active = document.activeElement;
        return Boolean(active) && (active === node || Boolean(node?.contains?.(active)))
            && (active.tagName === "INPUT" || active.tagName === "SELECT");
    }

    /** A row: the label in the gutter, the controls beside it. The label
     *  names the control `id` where there is one. */
    function field(label, controls, { id = null, title = null } = {}) {
        const row = el("div", "plx-popover-field", { title });
        const cell = el("div", "plx-popover-controls");
        cell.append(...[controls].flat());
        row.append(el(id ? "label" : "span", "plx-popover-label", { text: label, for: id }), cell);
        return row;
    }

    /** One of a few, as pressed buttons: `[[value, text, glyph?, title?]]`. */
    function segmented(label, choices, onPick) {
        const group = el("div", "plx-popover-segmented", { role: "radiogroup", "aria-label": label });
        const buttons = new Map();
        for (const [value, text, glyph, title] of choices) {
            const button = el("button", "plx-popover-segment", {
                type: "button", role: "radio", "aria-checked": "false",
                title: title || text, "aria-label": title || text,
            });
            if (glyph) button.appendChild(icon(glyph));
            if (text && !glyph) button.textContent = text;
            button.addEventListener("click", () => onPick(value));
            group.appendChild(button);
            buttons.set(value, button);
        }
        return {
            node: group,
            buttons,
            set(value) {
                for (const [key, button] of buttons) {
                    const on = key === value;
                    button.classList.toggle("is-active", on);
                    button.setAttribute("aria-checked", on ? "true" : "false");
                }
            },
        };
    }

    function select(id, choices, onChange) {
        const node = el("select", "plx-popover-select", { id });
        for (const [value, text] of choices) node.appendChild(el("option", null, { value, text }));
        node.addEventListener("change", () => onChange(node.value));
        return node;
    }

    function number(id, { min = 0, max = 0, label = "" } = {}) {
        return el("input", "plx-popover-number", {
            id, type: "number", min: String(min), max: String(max), step: "1",
            inputmode: "numeric", "aria-label": label,
        });
    }

    /** An on/off switch (DESIGN.md's 28x16, `.channel-toggle-switch`). */
    function switchInput(id, label, onChange, title = null) {
        const input = el("input", "channel-toggle-switch", {
            id, type: "checkbox", role: "switch", "aria-label": label, title });
        input.addEventListener("change", () => onChange(input.checked));
        return input;
    }

    function button(text, onClick, { title = null, glyph = null, className = "" } = {}) {
        const node = el("button", `plx-popover-button ${className}`.trim(), { type: "button", title });
        if (glyph) node.appendChild(icon(glyph));
        if (text) node.appendChild(el("span", null, { text }));
        if (!text && title) node.setAttribute("aria-label", title);
        node.addEventListener("click", onClick);
        return node;
    }

    /** A glyph button's name, said on hover and to assistive tech. */
    function name(node, text) {
        node.title = text;
        node.setAttribute("aria-label", text);
    }

    function press(node, on) {
        node.setAttribute("aria-pressed", on ? "true" : "false");
    }

    /** "13 planes · 26.0 µm" for planes z0..z1. */
    function planesNote(vs, z0, z1) {
        const planes = z1 - z0 + 1;
        const step = vs.zStep?.();
        const thick = step ? ` · ${(planes * step.value).toFixed(planes * step.value < 10 ? 1 : 0)} ${step.unit || "µm"}` : "";
        return `${planes} plane${planes === 1 ? "" : "s"}${thick}`;
    }

    /** A z0..z1 pair of number fields, committed on change, and what they
     *  come to after them. */
    function rangeRow(popover, prefix, label, commit) {
        const vs = popover.viewState;
        const top = Math.max(0, vs.depth - 1);
        const z0 = number(`${prefix}_z0`, { max: top, label: `${label}: first plane` });
        const z1 = number(`${prefix}_z1`, { max: top, label: `${label}: last plane` });
        const note = el("span", "plx-popover-note");
        const apply = () => {
            const state = vs.get();
            const a = Math.max(0, Math.min(top, int(z0.value, state.slab.z0)));
            const b = Math.max(0, Math.min(top, int(z1.value, state.slab.z1)));
            commit(Math.min(a, b), Math.max(a, b));
        };
        z0.addEventListener("change", apply);
        z1.addEventListener("change", apply);
        return {
            node: field(label, [z0, el("span", "plx-popover-note", { text: "–", "aria-hidden": "true" }), z1, note]),
            update(ctx) {
                if (!editing(z0)) z0.value = String(ctx.z0);
                if (!editing(z1)) z1.value = String(ctx.z1);
                note.textContent = planesNote(vs, ctx.z0, ctx.z1);
                note.title = note.textContent;
            },
        };
    }

    /** The index in GRID_STRENGTHS nearest `opacity`. */
    function strengthAt(opacity) {
        let index = 0;
        GRID_STRENGTHS.forEach(([value], i) => {
            if (Math.abs(value - opacity) < Math.abs(GRID_STRENGTHS[index][0] - opacity)) index = i;
        });
        return index;
    }

    /**
     * The rows, by control id: `(ctx, config, popover) -> {node, update}`.
     * `update(ctx, config)` moves a row's values with the state.
     */
    const ROWS = {
        "projection.op"(ctx, config, popover) {
            const vs = popover.viewState;
            const control = select("depth_settings_op", OPS, (value) => {
                if (value === "plane") vs.set({ mode: "plane" });
                else if (value === "pct") vs.set({ mode: "slab", slab: { op: "pct", q: vs.get().slab.q ?? 95 } });
                else vs.set({ mode: "slab", slab: { op: value } });
            });
            return {
                node: field("Show", control, { id: control.id }),
                update(now, cfg) { if (!editing(control)) control.value = cfg.value; },
            };
        },
        "projection.percentile"(ctx, config, popover) {
            const vs = popover.viewState;
            const input = number("depth_settings_q", { max: 100, label: "Percentile" });
            input.addEventListener("change", () => vs.set({
                mode: "slab", slab: { op: "pct", q: Math.max(0, Math.min(100, int(input.value, 95))) } }));
            return {
                node: field("Percentile", input, { id: input.id }),
                update() { if (!editing(input)) input.value = String(vs.get().slab.q ?? 95); },
            };
        },
        "projection.range"(ctx, config, popover) {
            const vs = popover.viewState;
            return rangeRow(popover, "depth_settings_slab", "Planes", (z0, z1) => vs.set({
                mode: "slab", z: Math.round((z0 + z1) / 2),
                slab: { z0, z1, thickness: z1 - z0 + 1, follow: false } }));
        },
        "projection.follow"(ctx, config, popover) {
            const vs = popover.viewState;
            const input = switchInput("depth_settings_follow", "Follow the plane",
                (on) => vs.set({ slab: { follow: on } }),
                "Keep the projection centred on the current plane, at the same thickness");
            return {
                node: field("Follow", input, { id: input.id }),
                update(now) { input.checked = Boolean(now.follow); },
            };
        },
        "volume.composite"(ctx, config, popover) {
            const vs = popover.viewState;
            const choice = segmented("Render", [
                ["mip", "MIP", null, "Maximum intensity along each ray"],
                ["additive", "Additive", null, "Emission and absorption: nearer tissue covers what is behind"],
            ], (value) => vs.set({ volume: { composite: value } }));
            return {
                node: field("Render", choice.node),
                update() { choice.set(vs.get().volume?.composite === "additive" ? "additive" : "mip"); },
            };
        },
        // The Area tool's square (views/areaTool.js): its side on screen,
        // so a zoom changes how much tissue it takes, not its size.
        "area.size"(ctx, config, popover) {
            const prefs = ctx.prefs;
            const input = number("depth_settings_area", { min: 64, max: 4096,
                                                          label: "Area side on screen, pixels" });
            input.addEventListener("change", () => {
                const value = Number(input.value);
                if (!(value > 0)) return;
                prefs.set({ area: { screen: Math.round(value) } });
            });
            const unit = el("span", "plx-popover-unit", { text: "px on screen" });
            return {
                node: field("Area", [input, unit], { id: input.id }),
                update() {
                    if (editing(input)) return;
                    const area = prefs.area?.() || { screen: 320 };
                    input.value = String(area.screen);
                    input.title = "The side of the Area tool's square on screen: zoom in to take less "
                        + "tissue, out to take more (V places it)";
                },
            };
        },
        "view.motion"(ctx, config, popover) {
            const prefs = ctx.prefs;
            const input = switchInput("depth_settings_motion", "Motion",
                (on) => prefs.set({ motion: on ? "on" : "off" }));
            return {
                node: field("Motion", input, { id: input.id }),
                update() {
                    // The preference, not smoothMotion(): an export holding
                    // motion off for a while has not turned it off.
                    const system = Boolean(prefs.systemReducedMotion?.());
                    input.disabled = system;
                    input.checked = !system && prefs.get?.().motion !== "off";
                    input.title = system ? "Off: the system asks for reduced motion"
                        : "Planes fade into each other, and the 3-D view coasts after a drag. "
                          + "For every image; Play turns either way.";
                },
            };
        },
        "rotate.axis"(ctx, config, popover) {
            const prefs = ctx.prefs;
            const turn = (patch) => {
                prefs.set({ rotate: patch });
                popover.retune();
            };
            const axis = segmented("About", AXES, (value) => turn({ axis: value }));
            for (const key of ["x", "y", "z"]) axis.buttons.get(key).classList.add("is-axis", `is-${key}`);
            const direction = segmented("Direction", DIRECTIONS, (value) => turn({ direction: value }));
            direction.node.classList.add("is-end");
            return {
                node: field("Turn", [axis.node, direction.node]),
                update() {
                    const rotate = prefs.rotate();
                    axis.set(rotate.axis);
                    direction.set(rotate.direction);
                },
            };
        },
        "rotate.speed"(ctx, config, popover) {
            const prefs = ctx.prefs;
            const limits = global.PlexoraVolumeRenderer?.MOTION || {};
            const min = limits.ROTATE_MIN_DPS || 5;
            const max = limits.ROTATE_MAX_DPS || 90;
            let row = null;
            // What the old note under the slider said, now its tooltip.
            const say = (speed) => {
                if (row) row.title = `${Math.round(360 / Math.max(1, speed))} s a turn`;
            };
            const apply = (speed, save) => {
                prefs.set({ rotate: { speed } }, { save });
                popover.retune();
                say(speed);
            };
            let slider = null;
            let input = null;
            const holder = el("div", "plx-popover-slider");
            if (typeof global.PlexoraSlider === "function") {
                slider = new global.PlexoraSlider(holder, {
                    min, max, step: 5, value: prefs.rotate().speed, unit: "°/s", integer: true,
                    ariaLabel: "Turning speed, degrees a second",
                    onInput: (value) => apply(value, false),
                    onChange: (value) => apply(value, true),
                });
                popover.sliders.push(slider);
            } else {
                input = el("input", null, { type: "range", min: String(min), max: String(max), step: "5",
                                            "aria-label": "Turning speed, degrees a second" });
                input.addEventListener("input", () => apply(Number(input.value), false));
                input.addEventListener("change", () => apply(Number(input.value), true));
                holder.appendChild(input);
            }
            row = field("Speed", holder);
            return {
                node: row,
                update() {
                    const speed = prefs.rotate().speed;
                    // Silent: set() otherwise reports the value as the user's,
                    // which saves it and repaints this row again.
                    if (slider && !editing(holder) && slider.get?.() !== speed) slider.set?.(speed, { silent: true });
                    if (input && !editing(input)) input.value = String(speed);
                    say(speed);
                },
            };
        },
        "grid.on"(ctx, config, popover) {
            const prefs = ctx.prefs;
            const set = (patch) => prefs.set({ grid: patch });
            const input = switchInput("depth_settings_grid", "Grid", (on) => set({ on }),
                                      "Lines on the far walls of the box, behind the tissue");
            const faces = segmented("Where", [
                ["walls", "Walls", null, "On the far walls"],
                ["floor", "Floor", null, "Only on the far face across z"],
            ], (value) => set({ faces: value }));
            const labels = button("", () => set({ labels: !prefs.grid().labels }), {
                glyph: "ruler-combined", className: "is-glyph is-end",
                title: "Axis labels: numbers along three edges of the box" });
            return {
                node: field("Grid", [input, faces.node, labels], { id: input.id }),
                update() {
                    const grid = prefs.grid();
                    input.checked = Boolean(grid.on);
                    faces.node.hidden = !grid.on;
                    labels.hidden = !grid.on;
                    faces.set(grid.faces);
                    press(labels, grid.labels);
                },
            };
        },
        "grid.options"(ctx, config, popover) {
            const prefs = ctx.prefs;
            const helpers = global.PlexoraVolumeRenderer?.grid;
            const set = (patch) => prefs.set({ grid: patch });
            const axes = () => popover.strip.volume?.renderer?.gridAxes?.(prefs.grid().step) || null;
            const step = (direction) => {
                const now = axes();
                if (now && helpers) set({ step: helpers.niceNeighbour(now.step[0], direction) });
            };
            const spacing = el("span", "plx-popover-value");
            const finer = button("", () => step(-1), { glyph: "minus", className: "is-glyph", title: "Finer grid" });
            const coarser = button("", () => step(1), { glyph: "plus", className: "is-glyph", title: "Coarser grid" });
            // Auto on: the spacing follows the box. Off: it stays where auto
            // had it, so letting go changes nothing on screen.
            const auto = button("Auto", () => {
                const now = axes();
                if (prefs.grid().step !== "auto") set({ step: "auto" });
                else if (now) set({ step: now.step[0] });
            }, { title: "Automatic spacing" });
            const steps = el("span", "plx-popover-actions");
            steps.append(finer, coarser);
            const strength = segmented("Line strength", GRID_STRENGTHS.map(([value, word]) => [
                value, "", null, `${word[0].toUpperCase()}${word.slice(1)} lines`]), (value) => set({ opacity: value }));
            GRID_STRENGTHS.forEach(([value], index) => strength.buttons.get(value).appendChild(
                el("span", "plx-popover-line", { "data-strength": String(index), "aria-hidden": "true" })));
            strength.node.classList.add("is-end");
            return {
                node: field("Lines", [spacing, steps, auto, strength.node]),
                update() {
                    const grid = prefs.grid();
                    press(auto, grid.step === "auto");
                    const now = axes();
                    if (now && helpers) {
                        const unit = now.units[0];
                        const say = (value) => `${helpers.formatTick(value)} ${unit}`;
                        const down = helpers.niceNeighbour(now.step[0], -1);
                        spacing.textContent = say(now.step[0]);
                        name(finer, `Finer grid: ${say(down)}`);
                        name(coarser, `Coarser grid: ${say(helpers.niceNeighbour(now.step[0], 1))}`);
                        auto.title = `Automatic spacing: ${say(now.auto)}`;
                        // Finer than the grid allows draws the same lines.
                        finer.disabled = popover.strip.volume.renderer.gridAxes(down).step[0] >= now.step[0];
                    } else {
                        spacing.textContent = grid.step === "auto" ? "auto" : String(grid.step);
                    }
                    strength.set(GRID_STRENGTHS[strengthAt(grid.opacity)][0]);
                },
            };
        },
        surfaces(ctx, config, popover) {
            const layers = global.PlexoraSurfaceLayers;
            const show = segmented("Show", [
                ["both", "Both", null, "Volume and surfaces"],
                ["volume", "Volume", null, "Volume only"],
                ["surfaces", "Surfaces", null, "Surfaces only"],
            ], (value) => layers?.setShow?.({ volume: value !== "surfaces", surfaces: value !== "volume" }));
            const make = button("", () => {
                popover.close({ refocus: false });
                global.PlexoraSurfaceStudio?.open?.();
            }, { glyph: "cubes", className: "is-glyph", title: "New surface: Surface Studio (S)" });
            const load = button("", () => {
                popover.close({ refocus: false });
                const input = document.createElement("input");
                input.type = "file";
                input.accept = ".ply,.obj,.stl,.glb,.zip";
                input.addEventListener("change", () => {
                    const file = input.files?.[0];
                    if (!file) return;
                    layers?.importMesh?.(file).catch((error) => global.PlexoraToast?.show?.(
                        { title: error.message, tone: "warning" }));
                });
                input.click();
            }, { glyph: "file-arrow-up", className: "is-glyph",
                 title: "Import a mesh: PLY, OBJ, STL, GLB, or a zip Plexora exported" });
            const actions = el("span", "plx-popover-actions is-end");
            actions.append(make, load);
            return {
                node: field("Surfaces", [show.node, actions]),
                update(now, cfg = {}) {
                    show.node.hidden = !cfg.show;
                    make.hidden = !(cfg.make && cfg.studio);
                    load.hidden = !cfg.make;
                    const shown = layers?.shown?.() || { volume: true, surfaces: true };
                    show.set(shown.volume && shown.surfaces ? "both" : (shown.volume ? "volume" : "surfaces"));
                },
            };
        },
        "surfaces.light"(ctx, config, popover) {
            const layers = global.PlexoraSurfaceLayers;
            const choice = segmented("Light", [
                ["left", "", "arrow-left", "Light from the left"],
                ["above", "", "arrow-up", "Light from above"],
                ["right", "", "arrow-right", "Light from the right"],
                ["front", "", "circle-dot", "Light from the front"],
            ], (light) => {
                layers?.setLight?.(light);
                choice.set(layers?.lightName?.() ?? light);
            });
            return { node: field("Light", choice.node), update() { choice.set(layers?.lightName?.()); } };
        },
    };

    //: Rows plugins added (defineRow), beside core's.
    const extraRows = new Map();

    class DepthSettings {
        constructor({ strip }) {
            this.strip = strip;
            this.el = null;
            this.anchor = null;
            this.rows = new Map();          // id -> {node, update}
            this.sliders = [];
            this.signature = "";
            this._teardown = null;
        }

        get viewState() {
            return this.strip.viewState;
        }

        isOpen() {
            return Boolean(this.el);
        }

        toggle(anchor) {
            if (this.el) this.close();
            else this.open(anchor);
        }

        open(anchor) {
            if (this.el) return;
            this.anchor = anchor;
            const node = el("div", "plx-menu plx-popover depth-settings", {
                role: "dialog", "aria-label": "Depth settings" });
            // A click inside is the popover's own business.
            node.addEventListener("click", (event) => event.stopPropagation());
            this.el = node;
            this.render();
            portal(node);
            this.place();
            anchor?.setAttribute?.("aria-expanded", "true");
            const onKey = (event) => {
                // Capture, and stopped: Escape here shuts the popover and
                // nothing else (the 3-D view's Escape, which stops a turn,
                // stands aside while a popover is open: volumeRenderer.js).
                if (event.key !== "Escape") return;
                event.stopPropagation();
                event.preventDefault?.();
                this.close();
            };
            const onDocumentClick = (event) => {
                // The strip is where the mode changes: the popover follows
                // it rather than closing (the cog toggles it itself).
                if (this.strip.root?.contains?.(event?.target)) return;
                this.close({ refocus: false });
            };
            const onScroll = (event) => { if (!node.contains(event.target)) this.close({ refocus: false }); };
            const onResize = () => this.close({ refocus: false });
            // Next tick, or the click that opened this would shut it.
            const timer = setTimeout(() => document.addEventListener("click", onDocumentClick), 0);
            document.addEventListener("keydown", onKey, true);
            document.addEventListener("scroll", onScroll, true);
            global.addEventListener("resize", onResize);
            this._teardown = () => {
                clearTimeout(timer);
                document.removeEventListener("click", onDocumentClick);
                document.removeEventListener("keydown", onKey, true);
                document.removeEventListener("scroll", onScroll, true);
                global.removeEventListener("resize", onResize);
            };
            this.focusFirst();
        }

        close({ refocus = true } = {}) {
            if (!this.el) return;
            this._teardown?.();
            this._teardown = null;
            this.destroyRows();
            unportal(this.el);
            this.el = null;
            this.anchor?.setAttribute?.("aria-expanded", "false");
            if (refocus) this.anchor?.focus?.();
        }

        destroyRows() {
            for (const slider of this.sliders) slider.destroy?.();
            this.sliders = [];
            this.rows.clear();
        }

        /** What the popover offers now (services/viewControls.js). */
        resolved() {
            const controls = global.PlexoraViewControls;
            const ctx = this.strip.context();
            if (!controls || !ctx) return { ctx, entries: [] };
            const entries = controls.resolve(ctx, { surface: "popover" })
                .filter((entry) => ROWS[entry.id] || extraRows.has(entry.id));
            return { ctx, entries };
        }

        /** Build every row for the mode on screen, grouped. */
        render() {
            const node = this.el;
            if (!node) return;
            const { ctx, entries } = this.resolved();
            this.destroyRows();
            node.replaceChildren();
            this.signature = entries.map((entry) => entry.id).join(",");
            let group = null;
            let section = null;
            for (const entry of entries) {
                if (entry.group !== group) {
                    group = entry.group;
                    section = el("div", "plx-popover-group", {
                        "data-group": group, role: "group", "aria-label": GROUP_NAMES[group] || null });
                    node.appendChild(section);
                }
                const builder = ROWS[entry.id] || extraRows.get(entry.id);
                let row = null;
                try {
                    row = builder(ctx, entry.config, this);
                } catch (error) {
                    console.error(`depthSettings: the "${entry.id}" row failed`, error);
                }
                if (!row?.node) continue;
                row.node.setAttribute("data-control", entry.id);
                section.appendChild(row.node);
                this.rows.set(entry.id, row);
                row.update?.(ctx, entry.config);
            }
            if (!entries.length) {
                node.appendChild(el("p", "plx-popover-empty", { text: "Nothing to set for this view." }));
            }
        }

        /**
         * Follow the view: rows rebuilt in place when what is offered changed
         * (a mode change), values moved otherwise. Focus stays on a control
         * that survives.
         */
        refresh({ force = false } = {}) {
            if (!this.el) return;
            const { ctx, entries } = this.resolved();
            const signature = entries.map((entry) => entry.id).join(",");
            if (force || signature !== this.signature) {
                const active = document.activeElement;
                const holder = active?.closest?.("[data-control]");
                const id = holder?.getAttribute?.("data-control") || null;
                const within = id && this.el.contains(active);
                this.render();
                this.place();
                if (within) {
                    const again = this.el.querySelector?.(`[data-control="${id}"]`);
                    (again?.querySelector?.("input, select, button") || again)?.focus?.();
                }
                return;
            }
            const configs = new Map(entries.map((entry) => [entry.id, entry.config]));
            for (const [id, row] of this.rows) row.update?.(ctx, configs.get(id) || {});
        }

        /** Play's turn, re-tuned to the prefs as they now are, if it runs. */
        retune() {
            const volume = this.strip.volume;
            if (volume?.rotating) volume.rotate(true, global.PlexoraViewerPrefs?.rotate?.() || {});
        }

        /** Left of the strip -- and of the cut panel open beside it
         *  (views/volumeDock.js) -- level with the cog, inside the window;
         *  below or above the cog where there is no room to its left. */
        place() {
            const node = this.el;
            const anchor = this.anchor;
            if (!node || !anchor?.getBoundingClientRect) return;
            const box = anchor.getBoundingClientRect();
            const strip = this.strip.root?.getBoundingClientRect?.() || box;
            const panel = this.strip.root?.querySelector?.(".volume-dock.is-open")?.getBoundingClientRect?.();
            const edge = panel ? Math.min(strip.left, panel.left) : strip.left;
            node.style.position = "fixed";
            const width = node.offsetWidth || 260;
            const height = node.offsetHeight || 0;
            const viewW = global.innerWidth || 0;
            const viewH = global.innerHeight || 0;
            let left = edge - GAP - width;
            let top = box.top + box.height / 2 - height / 2;
            if (left < GAP) {
                left = Math.max(GAP, Math.min(box.right - width, viewW - width - GAP));
                top = box.bottom + 4 + height > viewH - GAP && box.top - 4 - height > GAP
                    ? box.top - 4 - height : box.bottom + 4;
            }
            top = Math.max(GAP, Math.min(top, viewH - height - GAP));
            node.style.left = `${Math.round(left)}px`;
            node.style.top = `${Math.round(top)}px`;
        }

        focusFirst() {
            const target = this.el?.querySelector?.(
                "select:not([disabled]), input:not([disabled]), button:not([disabled])");
            target?.focus?.();
        }
    }

    /** A plugin's popover row for its registered entry `id`. */
    function defineRow(id, builder) {
        if (typeof builder !== "function") return () => {};
        extraRows.set(String(id), builder);
        return () => {
            if (extraRows.get(String(id)) === builder) extraRows.delete(String(id));
        };
    }

    DepthSettings.ROWS = ROWS;
    DepthSettings.defineRow = defineRow;
    DepthSettings.GRID_STRENGTHS = GRID_STRENGTHS;
    global.PlexoraDepthSettings = DepthSettings;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = DepthSettings;
    }
})(typeof window !== "undefined" ? window : globalThis);
