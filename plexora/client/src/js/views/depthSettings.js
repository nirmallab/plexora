/**
 * depthSettings.js - the depth strip's cog: the settings for what is drawn now.
 *
 * A small popover beside the strip (views/depthStrip.js) holding only what
 * applies to the mode on screen, as services/viewControls.js resolves it for
 * the "popover" surface:
 *
 *     one plane    Show: one plane or a projection's operation
 *     projection   the operation, its percentile, the planes combined, Follow
 *     3-D          MIP / Additive, the view, how Play turns the stack, the
 *                  grid, the surfaces (the cut is the strip's Cut and Crop,
 *                  and the panel they open, views/volumeDock.js)
 *
 * and the Depth card at its foot for everything less frequent. Nothing in it
 * is permanent furniture: it opens on the cog and closes on the next click
 * elsewhere, Escape, a resize or a scroll outside it. Open, it follows the
 * view: a mode changed by the strip, a key or an agent rebuilds its rows in
 * place, keeping focus on a control that is still there; values move with
 * the state but a field being typed in is never overwritten.
 *
 * Where each value lives is unchanged by it: the projection, the clip range
 * and the composite in the view state (per image, services/viewState.js);
 * how Play turns and the grid in the viewer prefs (per user,
 * services/viewerPrefs.js). A value set in one mode is there when that mode
 * comes back.
 *
 * A plugin's popover row: `ctx.view.controls.register(entry, {row})` with
 * `row(ctx, config, popover) -> {node, update(ctx, config)}` (main.js).
 *
 * Classic script, `window.PlexoraDepthSettings`.
 */
(function (global) {
    "use strict";

    //: The heading over each group of rows; "" for none.
    const GROUP_TITLES = {
        projection: "Projection", volume: "3-D view", clip: "Cut",
        rotate: "Play turns the stack", grid: "Grid", surfaces: "Surfaces",
        plugins: "", footer: "",
    };
    const OPS = [["plane", "One plane"], ["max", "Maximum (MIP)"], ["mean", "Mean"],
                 ["min", "Minimum"], ["std", "Std. deviation"], ["med", "Median"],
                 ["pct", "Percentile"]];
    const AXES = [["screen", "The screen's vertical"], ["x", "The image's X axis"],
                  ["y", "The image's Y axis"], ["z", "The image's Z axis"]];
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

    /** A labelled row: the label left, the control right. */
    function field(label, control, { id = null, title = null } = {}) {
        const row = el("div", "plx-popover-field", { title });
        const name = el(control.tagName === "INPUT" || control.tagName === "SELECT" ? "label" : "span",
                        "plx-popover-label", { text: label });
        if (id && control.id === id) name.setAttribute("for", id);
        row.append(name, control);
        return row;
    }

    /** One of a few, as pressed buttons: `[[value, text, icon?, title?]]`. */
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
        const node = el("select", "depth-panel-select plx-popover-select", { id });
        for (const [value, text] of choices) node.appendChild(el("option", null, { value, text }));
        node.addEventListener("change", () => onChange(node.value));
        return node;
    }

    function number(id, { min = 0, max = 0, label = "" } = {}) {
        return el("input", "depth-panel-number is-short", {
            id, type: "number", min: String(min), max: String(max), step: "1",
            inputmode: "numeric", "aria-label": label,
        });
    }

    function toggle(label, onChange, title = null) {
        const node = el("label", "depth-panel-toggle plx-popover-toggle", { title });
        const input = el("input", null, { type: "checkbox" });
        input.addEventListener("change", () => onChange(input.checked));
        node.append(input, el("span", null, { text: label }));
        return { node, input };
    }

    function button(text, onClick, { title = null, glyph = null, className = "" } = {}) {
        const node = el("button", `plx-popover-button ${className}`.trim(), { type: "button", title });
        if (glyph) node.appendChild(icon(glyph));
        if (text) node.appendChild(el("span", null, { text }));
        if (!text && title) node.setAttribute("aria-label", title);
        node.addEventListener("click", onClick);
        return node;
    }

    /** "13 planes · 26.0 µm" for planes z0..z1. */
    function planesNote(vs, z0, z1) {
        const planes = z1 - z0 + 1;
        const step = vs.zStep?.();
        const thick = step ? ` · ${(planes * step.value).toFixed(planes * step.value < 10 ? 1 : 0)} ${step.unit || "µm"}` : "";
        return `${planes} plane${planes === 1 ? "" : "s"}${thick}`;
    }

    /** A z0..z1 pair of number fields, committed on change, with a note. */
    function rangeRow(popover, prefix, label, commit) {
        const vs = popover.viewState;
        const top = Math.max(0, vs.depth - 1);
        const z0 = number(`${prefix}_z0`, { max: top, label: `${label}: first plane` });
        const z1 = number(`${prefix}_z1`, { max: top, label: `${label}: last plane` });
        const note = el("span", "depth-panel-unit plx-popover-note");
        const apply = () => {
            const state = vs.get();
            const a = Math.max(0, Math.min(top, int(z0.value, state.slab.z0)));
            const b = Math.max(0, Math.min(top, int(z1.value, state.slab.z1)));
            commit(Math.min(a, b), Math.max(a, b));
        };
        z0.addEventListener("change", apply);
        z1.addEventListener("change", apply);
        const pair = el("div", "plx-popover-range");
        pair.append(z0, el("span", "depth-panel-unit", { text: "–" }), z1);
        const node = el("div", "plx-popover-stack");
        node.append(field(label, pair), note);
        return {
            node,
            update(ctx) {
                if (!editing(z0)) z0.value = String(ctx.z0);
                if (!editing(z1)) z1.value = String(ctx.z1);
                note.textContent = planesNote(vs, ctx.z0, ctx.z1);
            },
        };
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
            const { node, input } = toggle("Follow the plane",
                (on) => vs.set({ slab: { follow: on } }),
                "Keep the projection centred on the current plane, at the same thickness");
            return { node, update(now) { input.checked = Boolean(now.follow); } };
        },
        "volume.composite"(ctx, config, popover) {
            const vs = popover.viewState;
            const choice = segmented("Composite", [
                ["mip", "MIP", null, "Maximum intensity along each ray"],
                ["additive", "Additive", null, "Emission and absorption: nearer tissue covers what is behind"],
            ], (value) => vs.set({ volume: { composite: value } }));
            return {
                node: field("Composite", choice.node),
                update() { choice.set(vs.get().volume?.composite === "additive" ? "additive" : "mip"); },
            };
        },
        "volume.view"(ctx, config, popover) {
            const preset = (name) => popover.strip.volume?.renderer?.preset?.(name);
            const row = el("div", "plx-popover-actions", { role: "group", "aria-label": "View" });
            for (const [name, glyph, title] of [["xy", "arrows-to-dot", "Looking down onto XY"],
                                                ["xz", "grip-lines", "From the side, XZ"],
                                                ["yz", "grip-lines-vertical", "From the side, YZ"],
                                                ["iso", "cube", "Three-quarter view"]]) {
                row.appendChild(button("", () => preset(name), { title, glyph, className: "is-glyph" }));
            }
            return { node: field("View", row), update() {} };
        },
        "rotate.speed"(ctx, config, popover) {
            const prefs = ctx.prefs;
            const limits = global.PlexoraVolumeRenderer?.MOTION || {};
            const min = limits.ROTATE_MIN_DPS || 5;
            const max = limits.ROTATE_MAX_DPS || 90;
            const note = el("span", "depth-panel-unit plx-popover-note");
            const say = (speed) => {
                note.textContent = `${Math.round(360 / Math.max(1, speed))} s a turn · turns with Motion off too`;
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
            const node = el("div", "plx-popover-stack");
            node.append(field("Speed", holder), note);
            return {
                node,
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
        "rotate.axis"(ctx, config, popover) {
            const prefs = ctx.prefs;
            const control = select("depth_settings_axis", AXES, (axis) => {
                prefs.set({ rotate: { axis } });
                popover.retune();
            });
            return {
                node: field("About", control, { id: control.id }),
                update() { if (!editing(control)) control.value = prefs.rotate().axis; },
            };
        },
        "rotate.direction"(ctx, config, popover) {
            const prefs = ctx.prefs;
            const choice = segmented("Direction", [
                [1, "", "rotate-right", "Clockwise, seen from above"],
                [-1, "", "rotate-left", "Anticlockwise, seen from above"],
            ], (direction) => {
                prefs.set({ rotate: { direction } });
                popover.retune();
            });
            return { node: field("Direction", choice.node), update() { choice.set(prefs.rotate().direction); } };
        },
        "grid.on"(ctx, config, popover) {
            const prefs = ctx.prefs;
            const { node, input } = toggle("Grid", (on) => prefs.set({ grid: { on } }),
                                           "Lines on the far walls of the box, behind the tissue");
            return { node, update() { input.checked = Boolean(prefs.grid?.()?.on); } };
        },
        "grid.options"(ctx, config, popover) {
            const prefs = ctx.prefs;
            const helpers = global.PlexoraVolumeRenderer?.grid;
            const set = (patch) => prefs.set({ grid: patch });
            const faces = segmented("Where", [
                ["walls", "Walls", null, "On the far walls"],
                ["floor", "Floor", null, "Only on the far face across z"],
            ], (value) => set({ faces: value }));
            const spacingText = el("span", "plx-popover-value");
            const finer = button("", () => step(-1), { glyph: "minus", className: "is-glyph" });
            const coarser = button("", () => step(1), { glyph: "plus", className: "is-glyph" });
            const auto = button("", () => set({ step: "auto" }), { glyph: "rotate-left", className: "is-glyph" });
            const spacing = el("div", "plx-popover-actions");
            spacing.append(spacingText, finer, coarser, auto);
            const strengthText = el("span", "plx-popover-value");
            const fainter = button("", () => strength(-1), { glyph: "minus", className: "is-glyph",
                                                              title: "Fainter" });
            const stronger = button("", () => strength(1), { glyph: "plus", className: "is-glyph",
                                                              title: "Stronger" });
            const lines = el("div", "plx-popover-actions");
            lines.append(strengthText, fainter, stronger);
            const labels = toggle("Axis labels", (on) => set({ labels: on }),
                                  "Numbers along three edges of the box");
            const axes = () => popover.strip.volume?.renderer?.gridAxes?.(prefs.grid().step) || null;
            const step = (direction) => {
                const now = axes();
                if (!now || !helpers) return;
                set({ step: helpers.niceNeighbour(now.step[0], direction) });
            };
            const at = () => {
                const opacity = prefs.grid().opacity;
                let index = 0;
                GRID_STRENGTHS.forEach(([value], i) => {
                    if (Math.abs(value - opacity) < Math.abs(GRID_STRENGTHS[index][0] - opacity)) index = i;
                });
                return index;
            };
            const strength = (direction) => {
                const next = GRID_STRENGTHS[Math.max(0, Math.min(GRID_STRENGTHS.length - 1, at() + direction))];
                set({ opacity: next[0] });
            };
            const node = el("div", "plx-popover-stack");
            node.append(field("Where", faces.node), field("Spacing", spacing), field("Lines", lines), labels.node);
            return {
                node,
                update() {
                    const grid = prefs.grid();
                    faces.set(grid.faces);
                    labels.input.checked = Boolean(grid.labels);
                    const now = axes();
                    if (now && helpers) {
                        const unit = now.units[0];
                        const say = (value) => `${helpers.formatTick(value)} ${unit}`;
                        const down = helpers.niceNeighbour(now.step[0], -1);
                        const up = helpers.niceNeighbour(now.step[0], 1);
                        spacingText.textContent = `${say(now.step[0])}${grid.step === "auto" ? " (auto)" : ""}`;
                        finer.title = `Finer: ${say(down)}`;
                        finer.setAttribute("aria-label", finer.title);
                        // Finer than the grid allows draws the same lines.
                        finer.disabled = popover.strip.volume.renderer.gridAxes(down).step[0] >= now.step[0];
                        coarser.title = `Coarser: ${say(up)}`;
                        coarser.setAttribute("aria-label", coarser.title);
                        auto.title = `Automatic: ${say(now.auto)}`;
                        auto.setAttribute("aria-label", auto.title);
                        auto.disabled = grid.step === "auto";
                    } else {
                        spacingText.textContent = grid.step === "auto" ? "auto" : String(grid.step);
                    }
                    const index = at();
                    strengthText.textContent = GRID_STRENGTHS[index][1];
                    fainter.disabled = index <= 0;
                    stronger.disabled = index >= GRID_STRENGTHS.length - 1;
                },
            };
        },
        "surfaces.show"(ctx, config, popover) {
            const surfaces = global.PlexoraSurfaceLayers;
            const choice = segmented("Show", [
                ["both", "Both", null, "Volume and surfaces"],
                ["volume", "Volume", null, "Volume only"],
                ["surfaces", "Surfaces", null, "Surfaces only"],
            ], (value) => surfaces?.setShow?.({ volume: value !== "surfaces", surfaces: value !== "volume" }));
            return {
                node: field("Show", choice.node),
                update() {
                    const show = surfaces?.shown?.() || { volume: true, surfaces: true };
                    choice.set(show.volume && show.surfaces ? "both" : (show.volume ? "volume" : "surfaces"));
                },
            };
        },
        "surfaces.light"(ctx, config, popover) {
            const surfaces = global.PlexoraSurfaceLayers;
            const choice = segmented("Light", [
                ["left", "", "arrow-left", "Light from the left"],
                ["above", "", "arrow-up", "Light from above"],
                ["right", "", "arrow-right", "Light from the right"],
                ["front", "", "circle-dot", "Light from the front"],
            ], (name) => {
                surfaces?.setLight?.(name);
                popover.refresh({ force: true });
            });
            return { node: field("Light", choice.node), update() { choice.set(surfaces?.lightName?.()); } };
        },
        "surfaces.new"(ctx, config, popover) {
            const node = button("New surface…", () => {
                popover.close({ refocus: false });
                global.PlexoraSurfaceStudio?.open?.();
            }, { title: "Surface Studio (S)", className: "is-link" });
            return { node, update() {} };
        },
        "surfaces.import"(ctx, config, popover) {
            const node = button("Import mesh…", () => {
                popover.close({ refocus: false });
                const input = document.createElement("input");
                input.type = "file";
                input.accept = ".ply,.obj,.stl,.glb,.zip";
                input.addEventListener("change", () => {
                    const file = input.files?.[0];
                    if (!file) return;
                    global.PlexoraSurfaceLayers?.importMesh?.(file).catch((error) => global.PlexoraToast?.show?.(
                        { title: error.message, tone: "warning" }));
                });
                input.click();
            }, { title: "PLY, OBJ, STL, GLB, or a zip Plexora exported", className: "is-link" });
            return { node, update() {} };
        },
        card(ctx, config, popover) {
            const node = button("Depth card…", () => {
                popover.close({ refocus: false });
                ctx.openCard?.();
            }, { title: "Everything else about depth: spacing, motion, section sizes", className: "is-link" });
            return { node, update() {} };
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
                // nothing else (the 3-D view's Escape stops a turn).
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
                    section = el("div", "plx-popover-group", { "data-group": group, role: "group" });
                    const title = GROUP_TITLES[group] ?? "";
                    if (title) {
                        const heading = el("div", "plx-popover-heading", { text: title });
                        section.appendChild(heading);
                        section.setAttribute("aria-label", title);
                    }
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
