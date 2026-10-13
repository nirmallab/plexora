/**
 * volumeDock.js - the 3-D dock: cutting the stack open, on the canvas.
 *
 * A small panel in the canvas layout's top-right corner, under the
 * orientation gizmo, that is there only while the 3-D view is:
 *
 *     Cut  [full] [cutaway] [section] [slab] [crop]
 *     Axes [X] [Y] [Z]
 *     X    ----o====o----   100 – 600 µm      one row per axis cut
 *     ...  how a slab combines and how thick, a section's opacity, the
 *          surroundings, the cut faces
 *     [handles] [reset ▾]        [undo] [redo]
 *     ▸ More: how voxels are read, the handles' strength, the panes
 *
 * WHAT SHOWS IS THE REGISTRY'S. Every row is an entry of
 * services/viewControls.js on the "dock" surface, shown only in the spatial
 * modes, on the axes and for the representation it means something in --
 * a slab's Combine is not there over surfaces alone, a cut face not outside
 * Crop and Cutaway. A row that does nothing now is hidden, never disabled.
 *
 * WHAT IT CHANGES IS THE CUT'S. Every control goes through
 * services/inspectState.js: a slider PREVIEWS while it is dragged (the 3-D
 * view redraws from uniforms, nothing is saved) and commits once on release,
 * one undo entry. Settings of a mode are kept when another is chosen, and
 * come back with it. Lengths are in microns where the image states its
 * spacing, else pixels and planes (services/inspectGeometry.js).
 *
 * The 3-D view's keys (X, Y, Z, Esc) work while the pointer is over the
 * dock, as over the view itself; Cmd/Ctrl+Z and Shift+Cmd/Ctrl+Z undo and
 * redo a cut there.
 *
 * Classic script, `window.PlexoraVolumeDock`.
 */
(function (global) {
    "use strict";

    const MODES = [
        ["full", "cube", "The whole stack"],
        ["cutaway", "scissors", "Cutaway: take a side off"],
        ["section", "grip-lines", "Sections: planes through the stack"],
        ["slab", "layer-group", "Slabs: thick slices, combined along each ray"],
        ["crop", "crop-simple", "Crop: keep a box"],
    ];
    const MODE_NAMES = { full: "Whole stack", cutaway: "Cutaway", section: "Sections", slab: "Slabs", crop: "Crop" };
    const AXIS_HINT = {
        x: "X: across the plane, left to right -- an X section is a YZ plane",
        y: "Y: across the plane, top to bottom -- a Y section is an XZ plane",
        z: "Z: through the planes -- a Z section is an XY plane",
    };
    const COMBINE = [["single", "One plane"], ["max", "Maximum"], ["mean", "Mean"],
                     ["min", "Minimum"], ["sum", "Sum"]];
    const FOLD_KEY = "plexora.volumeDock.folded";

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

    function button(className, { title, glyph = null, text = null, pressed = null } = {}) {
        const node = el("button", className, { type: "button", title, "aria-label": title,
                                               "aria-pressed": pressed === null ? null : String(pressed) });
        if (glyph) node.appendChild(icon(glyph));
        if (text) node.appendChild(el("span", null, { text }));
        return node;
    }

    function readFolded() {
        try {
            return global.localStorage?.getItem(FOLD_KEY) === "1";
        } catch (error) {
            return false;
        }
    }

    function writeFolded(folded) {
        try {
            global.localStorage?.setItem(FOLD_KEY, folded ? "1" : "0");
        } catch (error) {
            // A private window: the dock simply opens unfolded next time.
        }
    }

    function typing() {
        if (global.PlexoraShortcuts?.isTyping) return global.PlexoraShortcuts.isTyping();
        const active = document.activeElement;
        const tag = active?.tagName;
        return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || Boolean(active?.isContentEditable);
    }

    class VolumeDock {
        constructor({ viewState, volume = null, cutter = null, extras = null } = {}) {
            this.viewState = viewState;
            //: The volume renderer's controller (views/volumeRenderer.js attach).
            this.volume = volume;
            this.cutter = cutter;
            this.extras = typeof extras === "function" ? extras : null;
            this.G = global.PlexoraInspectGeometry;
            this.root = null;
            this.rows = new Map();          // control id -> {node, update(ctx, state)}
            this.folded = readFolded();
            this._off = [];
        }

        get renderer() {
            return this.volume?.renderer || null;
        }

        dims() {
            return this.G.dims(this.viewState.info, this.viewState.depth);
        }

        spacing() {
            return this.G.spacing(this.viewState.info);
        }

        /** The state the dock shows: a preview under a hand included. */
        state() {
            return this.cutter?.previewing ? this.cutter.effective() : this.viewState.get();
        }

        context() {
            const controls = global.PlexoraViewControls;
            if (!controls) return null;
            const vs = this.viewState;
            const source = this.cutter?.previewing ? {
                get: () => this.cutter.effective(), depth: vs.depth, frames: vs.frames, color: vs.color,
                zStep: () => vs.zStep?.() } : vs;
            return controls.context(source, () => ({
                cutSupported: Boolean(this.cutter),
                volumeSupported: Boolean(this.volume),
                prefs: global.PlexoraViewerPrefs || null,
                ...(this.extras ? this.extras() : {}),
            }));
        }

        // -- building --------------------------------------------------------

        mount() {
            if (this.root || !this.cutter || !this.G) return false;
            const root = el("section", "volume-dock", { role: "region", "aria-label": "Cut the 3-D view",
                                                        "data-canvas-collapsible": "" });
            root.hidden = true;
            const head = el("div", "volume-dock-head");
            this.foldButton = button("volume-dock-fold", { title: "Fold the 3-D dock", glyph: "chevron-down" });
            this.foldButton.setAttribute("aria-expanded", "true");
            this.foldButton.addEventListener("click", () => this.setFolded(!this.folded));
            const title = el("span", "volume-dock-title", { text: "Cut" });
            this.summary = el("span", "volume-dock-summary", { "aria-live": "polite" });
            this.refining = el("span", "volume-dock-refining", { title: "Fetching the exact planes…",
                                                                 "aria-hidden": "true" });
            this.refining.hidden = true;
            head.append(this.foldButton, title, this.summary, this.refining);
            this.body = el("div", "volume-dock-body");
            this.main = el("div", "volume-dock-rows");
            this.more = el("details", "volume-dock-more");
            const moreSummary = el("summary", "volume-dock-more-summary", { text: "More" });
            this.moreRows = el("div", "volume-dock-rows");
            this.more.append(moreSummary, this.moreRows);
            this.body.append(this.main, this.more);
            root.append(head, this.body);
            this.root = root;
            this.build();
            const wrapper = document.getElementById("openseadragon_wrapper");
            const placed = global.PlexoraCanvasLayout?.place?.(root, {
                corner: "top-right", order: 20, id: "volume-dock",
                collapse: (collapsed) => this.setFolded(collapsed, { remember: false }) });
            if (!placed) wrapper?.appendChild(root);
            this.undock = typeof placed === "function" ? placed : null;
            const repaint = () => this.paint();
            this._off.push(this.viewState.subscribe(repaint));
            for (const name of ["plexora:inspect-preview", "plexora:inspect-changed", "plexora:viewer-prefs-changed",
                                "plexora:view-controls-changed", "plexora:volume-mounted", "plexora:surface-loaded",
                                "plexora:volume-progress"]) {
                global.addEventListener(name, repaint);
                this._off.push(() => global.removeEventListener(name, repaint));
            }
            const keys = (event) => this.onKey(event);
            document.addEventListener("keydown", keys, true);
            this._off.push(() => document.removeEventListener("keydown", keys, true));
            this.setFolded(this.folded, { remember: false });
            this.paint();
            return true;
        }

        destroy() {
            for (const off of this._off.splice(0)) off?.();
            this.undock?.();
            this.root?.remove();
            this.root = null;
        }

        /** Unfold and bring the dock into view (the popover's "Cut…"). */
        open() {
            this.setFolded(false);
            this.root?.querySelector?.(".volume-dock-mode .is-active")?.focus?.({ preventScroll: true });
        }

        setFolded(folded, { remember = true } = {}) {
            this.folded = Boolean(folded);
            if (remember) writeFolded(this.folded);
            if (!this.root) return;
            this.root.classList.toggle("is-folded", this.folded);
            this.body.hidden = this.folded;
            this.foldButton.setAttribute("aria-expanded", this.folded ? "false" : "true");
            this.foldButton.title = this.folded ? "Open the 3-D dock" : "Fold the 3-D dock";
            this.foldButton.setAttribute("aria-label", this.foldButton.title);
            this.foldButton.replaceChildren(icon(this.folded ? "chevron-right" : "chevron-down"));
        }

        /** Every row, once; `paint` shows the ones that apply. */
        build() {
            const make = {
                "cut.mode": () => this.modeRow(),
                "cut.axes": () => this.axesRow(),
                "cut.x": () => this.axisRow("x"),
                "cut.y": () => this.axisRow("y"),
                "cut.z": () => this.axisRow("z"),
                "cut.combine": () => this.combineRow(),
                "cut.thickness": () => this.thicknessRow(),
                "cut.opacity": () => this.fractionRow("opacity", "Opacity", "How opaque the section planes are",
                                                      0.05),
                "cut.context": () => this.fractionRow("context", "Around",
                                                      "The rest of the stack, dimmed, around the cut", 0),
                "cut.face": () => this.faceRow(),
                "cut.interpolation": () => this.interpolationRow(),
                "cut.handleOpacity": () => this.handleOpacityRow(),
                "cut.link": () => this.linkRow(),
            };
            for (const [id, build] of Object.entries(make)) {
                const row = build();
                row.node.setAttribute("data-control", id);
                this.rows.set(id, row);
                (["cut.interpolation", "cut.handleOpacity", "cut.link"].includes(id) ? this.moreRows : this.main)
                    .appendChild(row.node);
            }
            // The tools: handles, reset, undo and redo, on one line.
            const tools = this.toolsRow();
            this.main.appendChild(tools.node);
            this.rows.set("cut.tools", tools);
        }

        field(label, control, { title = null, id = null } = {}) {
            const row = el("div", "volume-dock-field", { title });
            const name = el(id ? "label" : "span", "volume-dock-label", { text: label, for: id });
            row.append(name, control);
            return row;
        }

        segmented(label, choices, onPick, { multi = false } = {}) {
            const group = el("div", "plx-popover-segmented volume-dock-segmented",
                             { role: multi ? "group" : "radiogroup", "aria-label": label });
            const buttons = new Map();
            for (const [value, text, glyph, title] of choices) {
                const node = el("button", "plx-popover-segment", {
                    type: "button", role: multi ? null : "radio", title: title || text,
                    "aria-label": title || text,
                });
                if (glyph) node.appendChild(icon(glyph));
                else node.textContent = text;
                node.addEventListener("click", () => onPick(value));
                group.appendChild(node);
                buttons.set(value, node);
            }
            return {
                node: group,
                set(on) {
                    for (const [value, node] of buttons) {
                        const lit = typeof on === "function" ? on(value) : on === value;
                        node.classList.toggle("is-active", lit);
                        node.setAttribute(multi ? "aria-pressed" : "aria-checked", lit ? "true" : "false");
                    }
                },
            };
        }

        modeRow() {
            const choice = this.segmented("Cut", MODES.map(([mode, glyph, title]) => [mode, MODE_NAMES[mode], glyph,
                                                                                      title]),
                                          (mode) => this.cutter.set({ inspect: { mode } }));
            choice.node.classList.add("volume-dock-mode");
            const node = el("div", "volume-dock-stack");
            node.append(choice.node);
            return { node, update(ctx) { choice.set(ctx.cut); } };
        }

        axesRow() {
            const choice = this.segmented("Axes", ["x", "y", "z"].map((axis) => [axis, axis.toUpperCase(), null,
                                                                                 AXIS_HINT[axis]]),
                                          (axis) => this.toggleAxis(axis), { multi: true });
            return {
                node: this.field("Axes", choice.node),
                update(ctx) { choice.set((axis) => Boolean(ctx.cutAxes[axis])); },
            };
        }

        /** An axis on or off; the last one stays on. */
        toggleAxis(axis) {
            const axes = { ...this.viewState.get().inspect.axes };
            const on = !axes[axis];
            if (!on && Object.values(axes).filter(Boolean).length <= 1) return;
            axes[axis] = on;
            this.cutter.set({ inspect: { axes } });
        }

        /** The range of image units an axis's controls may take: across x
         *  and y the area the 3-D view draws (it shows the area on screen
         *  when it was entered), along z every plane. */
        bounds(axis) {
            const d = this.dims();
            if (axis === "z") return [0, d.depth - 1];
            const covered = this.renderer?.covered;
            const i = axis === "x" ? 0 : 1;
            const whole = axis === "x" ? d.width : d.height;
            return covered ? [Math.max(0, covered[i]), Math.min(whole, covered[i + 2])] : [0, whole];
        }

        /** px <-> the unit a field is typed in (microns where stated). */
        units(axis) {
            const step = this.spacing()[axis];
            if (axis === "z") return { unit: "z", format: (v) => String(Math.round(v)), parse: (t) => Number(t) };
            if (!step) return { unit: "px", format: (v) => String(Math.round(v)), parse: (t) => Number(t) };
            const decimals = step.value * 10 < 1 ? 2 : 1;
            return { unit: step.unit, format: (v) => (v * step.value).toFixed(decimals),
                     parse: (t) => Number(t) / step.value };
        }

        axisRow(axis) {
            const node = el("div", "volume-dock-axis", { title: AXIS_HINT[axis] });
            const label = el("span", "volume-dock-label volume-dock-axis-name", { text: axis.toUpperCase() });
            const holder = el("div", "volume-dock-slider");
            const fields = el("div", "volume-dock-fields");
            const flip = button("plx-popover-button is-glyph volume-dock-flip",
                                { title: "Keep the other side", glyph: "arrows-left-right-to-line" });
            flip.addEventListener("click", () => {
                const keep = this.viewState.get().inspect.cutaway[axis].keep;
                this.cutter.set({ inspect: { cutaway: { [axis]: { keep: keep === "hi" ? "lo" : "hi" } } } });
            });
            const note = el("span", "volume-dock-note");
            const line = el("div", "volume-dock-axis-line");
            line.append(label, holder, flip);
            const under = el("div", "volume-dock-axis-under");
            under.append(fields, note);
            node.append(line, under);
            const row = { node, kind: null, slider: null };
            const patchFor = (kind, value) => {
                if (kind === "range") return { inspect: { crop: { [axis]: value } } };
                if (kind === "cut") return { inspect: { cutaway: { [axis]: { at: value } } } };
                return axis === "z" ? { z: value } : { section: { [axis]: value } };
            };
            const build = (kind) => {
                row.slider?.destroy?.();
                holder.replaceChildren();
                fields.replaceChildren();
                const [lo, hi] = this.bounds(axis);
                const units = this.units(axis);
                const Slider = global.PlexoraSlider;
                if (typeof Slider !== "function") return;
                row.kind = kind;
                row.bounds = `${lo},${hi}`;
                row.unit = units.unit;
                row.slider = new Slider(holder, {
                    mode: kind === "range" ? "range" : "single",
                    min: lo, max: axis === "z" ? hi : hi - (kind === "plane" ? 1 : 0), step: 1, integer: true,
                    // A pixel at least across; along z one plane is a crop.
                    minGap: kind === "range" && axis !== "z" ? 1 : 0,
                    unit: units.unit, format: units.format, parse: units.parse,
                    fieldsSlot: fields, fieldWidth: 54,
                    ariaLabel: `${axis.toUpperCase()} ${kind === "range" ? "range kept" : (kind === "cut" ? "cut" : "plane")}`,
                    onInput: (value) => this.cutter.preview(patchFor(kind, value)),
                    onChange: (value) => {
                        this.cutter.preview(patchFor(kind, value));
                        this.cutter.commit();
                    },
                });
            };
            row.update = (ctx, state) => {
                const kind = this.G && global.PlexoraViewControls
                    ? global.PlexoraViewControls.configOf(`cut.${axis}`, ctx).kind : "plane";
                const [lo, hi] = this.bounds(axis);
                // Built again for another kind, area or unit (a spacing typed
                // in the Depth card turns pixels into microns).
                if (kind !== row.kind || row.bounds !== `${lo},${hi}` || row.unit !== this.units(axis).unit) build(kind);
                flip.hidden = kind !== "cut";
                const inspect = state.inspect;
                let value;
                if (kind === "range") value = inspect.crop[axis].slice();
                else if (kind === "cut") value = inspect.cutaway[axis].at;
                else value = axis === "z" ? state.z : Math.round(state.section?.[axis] ?? 0);
                if (row.slider && JSON.stringify(row.slider.get()) !== JSON.stringify(value)) {
                    row.slider.set(value, { silent: true });
                }
                if (kind === "cut") {
                    flip.title = inspect.cutaway[axis].keep === "hi"
                        ? "Keeping the far side: keep the near side instead"
                        : "Keeping the near side: keep the far side instead";
                    flip.setAttribute("aria-label", flip.title);
                }
                note.textContent = this.axisNote(axis, kind, state);
            };
            return row;
        }

        /** What an axis row's fields do not say themselves: how many planes a
         *  Z range is and how thick, which side a cut keeps, how deep a Z
         *  plane is. Nothing where the fields say it all. */
        axisNote(axis, kind, state) {
            const space = this.spacing();
            if (kind === "range") {
                if (axis !== "z") return "";
                const [z0, z1] = state.inspect.crop.z;
                const planes = z1 - z0 + 1;
                const step = space.z;
                return `${planes} plane${planes === 1 ? "" : "s"}`
                    + (step ? ` · ${this.G.thicknessLabel("z", planes, space)}` : "");
            }
            if (kind === "cut") {
                return state.inspect.cutaway[axis].keep === "hi" ? "far side kept" : "near side kept";
            }
            if (axis === "z" && space.z) return `${(state.z * space.z.value).toFixed(1)} ${space.z.unit} deep`;
            return "";
        }

        combineRow() {
            const select = el("select", "plx-popover-select volume-dock-select", { id: "volume_dock_combine" });
            for (const [value, text] of COMBINE) select.appendChild(el("option", null, { value, text }));
            select.addEventListener("change", () => this.cutter.set({ inspect: { slab: { composite: select.value } } }));
            return {
                node: this.field("Combine", select, { id: select.id,
                    title: "How a slab's voxels combine along each ray" }),
                update(ctx, state) { if (document.activeElement !== select) select.value = state.inspect.slab.composite; },
            };
        }

        /** The axis a thickness is said along: Z when it is cut, else the
         *  first axis that is. */
        thicknessAxis(state) {
            const axes = state.inspect.axes;
            return axes.z ? "z" : (axes.x ? "x" : "y");
        }

        thicknessRow() {
            const holder = el("div", "volume-dock-slider");
            const note = el("span", "volume-dock-note");
            const node = el("div", "volume-dock-stack");
            node.append(this.field("Thick", holder, { title: "How thick the slabs are" }), note);
            const row = { node, axis: null, slider: null };
            const patchFor = (axis, value) => ({ inspect: { slab: {
                thickness: this.G.thicknessFor(value, axis, this.dims(), this.spacing()) } } });
            row.update = (ctx, state) => {
                const axis = this.thicknessAxis(state);
                const d = this.dims();
                const top = axis === "z" ? d.depth : Math.max(2, Math.round((axis === "x" ? d.width : d.height) / 2));
                if (axis !== row.axis && typeof global.PlexoraSlider === "function") {
                    row.slider?.destroy?.();
                    holder.replaceChildren();
                    row.axis = axis;
                    row.slider = new global.PlexoraSlider(holder, {
                        min: 1, max: top, step: 1, integer: true, field: false,
                        ariaLabel: "Slab thickness",
                        onInput: (value) => this.cutter.preview(patchFor(axis, value)),
                        onChange: (value) => {
                            this.cutter.preview(patchFor(axis, value));
                            this.cutter.commit();
                        },
                    });
                }
                const value = state.inspect.slab.thickness[axis];
                if (row.slider && row.slider.get() !== value) row.slider.set(value, { silent: true });
                note.textContent = this.G.thicknessLabel(axis, value, this.spacing());
            };
            return row;
        }

        fractionRow(key, label, title, min) {
            const holder = el("div", "volume-dock-slider");
            const row = { node: this.field(label, holder, { title }), slider: null };
            if (typeof global.PlexoraSlider === "function") {
                row.slider = new global.PlexoraSlider(holder, {
                    min, max: 1, step: 0.05, unit: "%", field: true, fieldWidth: 40,
                    format: (v) => String(Math.round(v * 100)), parse: (t) => Number(t) / 100,
                    ariaLabel: title,
                    onInput: (value) => this.cutter.preview({ inspect: { [key]: value } }),
                    onChange: (value) => {
                        this.cutter.preview({ inspect: { [key]: value } });
                        this.cutter.commit();
                    },
                });
            }
            row.update = (ctx, state) => {
                const value = state.inspect[key];
                if (row.slider && Math.abs(row.slider.get() - value) > 1e-6) row.slider.set(value, { silent: true });
            };
            return row;
        }

        toggle(label, title, onChange) {
            const node = el("label", "depth-panel-toggle plx-popover-toggle volume-dock-toggle", { title });
            const input = el("input", null, { type: "checkbox" });
            input.addEventListener("change", () => onChange(input.checked));
            node.append(input, el("span", null, { text: label }));
            return { node, input };
        }

        faceRow() {
            const { node, input } = this.toggle("Solid cut faces", "Draw the voxels on the cut faces solid, "
                + "not looked through", (on) => this.cutter.set({ inspect: { face: on } }));
            return { node, update(ctx, state) { input.checked = Boolean(state.inspect.face); } };
        }

        interpolationRow() {
            const choice = this.segmented("Voxels", [
                ["linear", "Smooth", null, "Read between voxels smoothly"],
                ["nearest", "Blocks", null, "Show voxels as the blocks they are"],
            ], (value) => this.viewState.set({ volume: { interpolation: value } }));
            return {
                node: this.field("Voxels", choice.node),
                update(ctx, state) { choice.set(state.volume?.interpolation === "nearest" ? "nearest" : "linear"); },
            };
        }

        handleOpacityRow() {
            const holder = el("div", "volume-dock-slider");
            const row = { node: this.field("Handles", holder, { title: "How strongly the handles are drawn" }),
                          slider: null };
            const prefs = global.PlexoraViewerPrefs;
            if (typeof global.PlexoraSlider === "function" && prefs?.handles) {
                row.slider = new global.PlexoraSlider(holder, {
                    min: 0.15, max: 1, step: 0.05, field: false, ariaLabel: "Handle strength",
                    onInput: (value) => prefs.set({ handles: { opacity: value } }, { save: false }),
                    onChange: (value) => prefs.set({ handles: { opacity: value } }),
                });
            }
            row.update = () => {
                const value = prefs?.handles?.().opacity;
                if (row.slider && Number.isFinite(value) && Math.abs(row.slider.get() - value) > 1e-6) {
                    row.slider.set(value, { silent: true });
                }
            };
            return row;
        }

        linkRow() {
            const { node, input } = this.toggle("Panes follow the sections",
                "Back in 2-D with sections on, the XZ and YZ panes cut where the Y and X sections are",
                (on) => this.cutter.set({ inspect: { linkPanes: on } }));
            return { node, update(ctx, state) { input.checked = Boolean(state.inspect.linkPanes); } };
        }

        toolsRow() {
            const node = el("div", "volume-dock-tools");
            const prefs = global.PlexoraViewerPrefs;
            const handles = button("plx-popover-button is-glyph", { title: "Show the handles", glyph: "eye",
                                                                    pressed: true });
            handles.addEventListener("click", () => {
                const on = prefs?.handles?.().visible !== false;
                prefs?.set?.({ handles: { visible: !on } });
            });
            const reset = button("plx-popover-button volume-dock-reset", { title: "Put cuts back", glyph: "rotate-left",
                                                                           text: "Reset" });
            reset.setAttribute("aria-haspopup", "menu");
            reset.addEventListener("click", () => this.openReset(reset));
            const spacer = el("span", "volume-dock-spacer");
            const undo = button("plx-popover-button is-glyph", { title: "Undo the last cut (Cmd/Ctrl+Z)",
                                                                 glyph: "arrow-rotate-left" });
            undo.addEventListener("click", () => this.cutter.undo());
            const redo = button("plx-popover-button is-glyph", { title: "Redo (Shift+Cmd/Ctrl+Z)",
                                                                 glyph: "arrow-rotate-right" });
            redo.addEventListener("click", () => this.cutter.redo());
            node.append(handles, reset, spacer, undo, redo);
            return {
                node,
                update: (ctx, state, shown) => {
                    handles.hidden = !shown.has("cut.handles");
                    reset.hidden = !shown.has("cut.reset");
                    undo.hidden = !shown.has("cut.history");
                    redo.hidden = !shown.has("cut.history");
                    const on = prefs?.handles?.().visible !== false;
                    handles.setAttribute("aria-pressed", on ? "true" : "false");
                    handles.title = on ? "Hide the handles" : "Show the handles";
                    handles.setAttribute("aria-label", handles.title);
                    handles.replaceChildren(icon(on ? "eye" : "eye-slash"));
                    undo.disabled = !this.cutter.canUndo();
                    redo.disabled = !this.cutter.canRedo();
                },
                hasAny: (shown) => ["cut.handles", "cut.reset", "cut.history"].some((id) => shown.has(id)),
            };
        }

        openReset(anchor) {
            const state = this.viewState.get();
            const inspect = state.inspect;
            const items = this.G.active(inspect).map((axis) => ({
                label: `${axis.toUpperCase()} only`, onSelect: () => this.cutter.reset("axis", { axis }) }));
            items.push(
                { label: `Every ${MODE_NAMES[inspect.mode].toLowerCase()} setting`, onSelect: () => this.cutter.reset("mode") },
                { label: "Every cut's settings", onSelect: () => this.cutter.reset("cut") },
                { separator: true },
                { label: "Back to the whole stack", onSelect: () => this.cutter.reset("full") });
            if (global.PlexoraMenu?.open) global.PlexoraMenu.open(anchor, items);
            else this.cutter.reset("full");
        }

        // -- painting ------------------------------------------------------

        /** Show the rows the registry resolves for the dock now, with the
         *  state's values; hide the dock where none apply (2-D). */
        paint() {
            if (!this.root) return;
            const controls = global.PlexoraViewControls;
            const ctx = this.context();
            const state = this.state();
            const shown = new Set(controls && ctx ? controls.resolve(ctx, { surface: "dock" }).map((e) => e.id) : []);
            const visible = shown.size > 0 && state?.mode === "volume";
            this.root.hidden = !visible;
            if (!visible) return;
            for (const [id, row] of this.rows) {
                if (id === "cut.tools") continue;
                row.node.hidden = !shown.has(id);
                if (!row.node.hidden) row.update?.(ctx, state, shown);
            }
            const tools = this.rows.get("cut.tools");
            tools.node.hidden = !tools.hasAny(shown);
            if (!tools.node.hidden) tools.update(ctx, state, shown);
            this.more.hidden = ![...this.moreRows.children].some((node) => !node.hidden);
            this.summary.textContent = ctx.cut === "full" ? "· whole stack"
                : `· ${MODE_NAMES[ctx.cut].toLowerCase()} ${this.G.active(state.inspect).join("+").toUpperCase()}`;
            this.paintRefining();
        }

        /** The dot that says the cut's exact planes are still on their way
         *  (the base level is drawn meanwhile; volumeRenderer.resolved). */
        paintRefining() {
            const renderer = this.renderer;
            const busy = Boolean(renderer && renderer.status === "ready" && typeof renderer.resolved === "function"
                && !renderer.resolved());
            this.refining.hidden = !busy;
            clearTimeout(this._refiningTimer);
            if (busy) this._refiningTimer = setTimeout(() => this.paintRefining(), 400);
        }

        /** Cmd/Ctrl+Z, Shift+Cmd/Ctrl+Z: undo and redo a cut, while the user
         *  is working in the 3-D view or this dock. */
        onKey(event) {
            if (!(event.metaKey || event.ctrlKey) || event.altKey) return;
            if (String(event.key || "").toLowerCase() !== "z") return;
            if (typing() || !this.viewState.isVolume?.()) return;
            const engaged = this.renderer?.engaged?.() || this.root?.matches?.(":hover")
                || this.root?.contains?.(document.activeElement);
            if (!engaged) return;
            const done = event.shiftKey ? this.cutter.redo() : this.cutter.undo();
            if (done) {
                event.preventDefault();
                event.stopPropagation();
            }
        }
    }

    /** Build the dock for this stack's 3-D view; null where it cannot be. */
    function attach(options = {}) {
        const dock = new VolumeDock(options);
        if (!dock.mount()) return null;
        api.instance = dock;
        return dock;
    }

    const api = {
        VolumeDock,
        attach,
        instance: null,
        open() { api.instance?.open(); },
        get root() { return api.instance?.root || null; },
    };
    global.PlexoraVolumeDock = api;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    }
})(typeof window !== "undefined" ? window : globalThis);
