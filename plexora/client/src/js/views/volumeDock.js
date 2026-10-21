/**
 * volumeDock.js - the cut panel: cutting the 3-D view open, beside the strip.
 *
 * In 3-D the depth strip (views/depthStrip.js) offers two tools, Cut (a side
 * taken off, `inspect.mode` "cutaway") and Crop (a box kept). Choosing one
 * opens this panel to the strip's LEFT, as an extension of it -- the same
 * ground, border and type -- sliding out from under it:
 *
 *     Cut                 ›  ×      the tool; collapse; close
 *     (X)[◐]   [faces] [eye]        the axes down the left: a Cut is along
 *     (Y)      [reset] [undo]       ONE (a radio), and flips the side it
 *     (Z)                           keeps; the actions beside
 *     [ Smooth  |  Blocks ]         how voxels are read
 *     ✋ ─────●──────               how strongly the handles are drawn
 *     [O] Show/Hide                 the cut shown or hidden
 *     [+] Save area                 the area, cut and camera kept by name,
 *     [▣ Area 1 ×] ...              each a thumbnail that restores it
 *
 * Crop is the same panel, its axis chips toggles (any of X, Y and Z) filling
 * the flip column, and "Crop to area" among the actions while the crop is
 * narrower than the area: the area becomes the crop, fetched finer. The cut
 * itself is made in the view, with the handles (views/volumeHandles.js); the
 * panel has no position sliders.
 *
 * ONE TOOL, ONE STATE. The tool and its settings are the view state's
 * (`inspect`, services/inspectGeometry.js) and change only through
 * services/inspectState.js, so the panel, the handles, the keys and the
 * agent never disagree. Collapsing the panel (›, Esc, the tool's button
 * again) leaves the cut as it is; × closes it and goes back to the whole
 * stack. `O` (or Show/Hide) hides the cut and shows it again, settings kept,
 * while the panel stays out, so the cut can be flicked on and off while
 * looking; a control used while the cut is hidden shows it again. Switching
 * tools updates the panel in place.
 *
 * WHAT SHOWS IS THE REGISTRY'S. Every control is an entry of
 * services/viewControls.js on the "dock" surface: the cut faces and Voxels
 * need the volume drawn, the handles' controls the viewer prefs. A control
 * that does nothing now is hidden, never disabled.
 *
 * The 3-D view's keys (X, Y, Z, Esc) work while the pointer is over the
 * panel, as over the view itself; Cmd/Ctrl+Z and Shift+Cmd/Ctrl+Z undo and
 * redo a cut there.
 *
 * Classic script, `window.PlexoraVolumeDock`.
 */
(function (global) {
    "use strict";

    const AXES = ["x", "y", "z"];
    const TOOL_NAMES = { cutaway: "Cut", crop: "Crop" };
    const AXIS_HINT = {
        x: "X (across the plane, left to right)",
        y: "Y (across the plane, top to bottom)",
        z: "Z (through the planes)",
    };
    //: The key that shows and hides the cut (the panel's Show/Hide and the
    //: depth strip's tooltips say so).
    const TOGGLE_KEY = "o";
    const OPEN_KEY = "plexora.cutPanel.open";

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

    /** A button's words, said once for the tooltip and the screen reader. */
    function say(node, title) {
        node.title = title;
        node.setAttribute("aria-label", title);
    }

    function readOpen() {
        try {
            return global.localStorage?.getItem(OPEN_KEY) === "1";
        } catch (error) {
            return false;
        }
    }

    function writeOpen(open) {
        try {
            global.localStorage?.setItem(OPEN_KEY, open ? "1" : "0");
        } catch (error) {
            // A private window: the panel waits for a tool next time.
        }
    }

    function typing() {
        if (global.PlexoraShortcuts?.isTyping) return global.PlexoraShortcuts.isTyping();
        const active = document.activeElement;
        const tag = active?.tagName;
        return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || Boolean(active?.isContentEditable);
    }

    /** "Ctrl+Z" or "⌘Z", as the menus print it. */
    function chord(spec, fallback) {
        return global.PlexoraShortcuts?.format?.(spec) || fallback;
    }

    class VolumeDock {
        constructor({ viewState, volume = null, cutter = null, extras = null, strip = null } = {}) {
            this.viewState = viewState;
            //: The volume renderer's controller (views/volumeRenderer.js attach).
            this.volume = volume;
            this.cutter = cutter;
            //: The depth strip the panel opens beside (views/depthStrip.js).
            this.strip = strip;
            this.extras = typeof extras === "function" ? extras : null;
            this.G = global.PlexoraInspectGeometry;
            this.root = null;
            //: Whether the panel is out in 3-D: opened by a tool, kept while
            //: its cut is hidden, across a tool switch and a reload; only a
            //: collapse or × puts it away.
            this.open = readOpen();
            this.controls = new Map();      // control id -> {node, update(ctx, state)}
            this._off = [];
        }

        get renderer() {
            return this.volume?.renderer || null;
        }

        /** The state the panel shows: a preview under a hand included. */
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
                savedAreasSupported: Boolean(this.saved()),
                volumeSupported: Boolean(this.volume),
                prefs: global.PlexoraViewerPrefs || null,
                ...(this.extras ? this.extras() : {}),
            }));
        }

        // -- building --------------------------------------------------------

        mount() {
            const host = this.strip?.root;
            if (this.root || !this.cutter || !this.G || !host) return false;
            const root = el("section", "volume-dock", { role: "region", "aria-label": "Cut the 3-D view" });
            root.inert = true;
            const head = el("div", "volume-dock-head");
            this.title = el("span", "volume-dock-title", { text: "Cut" });
            this.refining = el("span", "volume-dock-refining", { title: "Fetching the voxels the cut shows…",
                                                                 "aria-hidden": "true" });
            this.refining.hidden = true;
            const spacer = el("span", "volume-dock-spacer");
            this.collapseButton = button("volume-dock-head-button",
                                         { title: "Collapse the panel (the cut stays as it is)",
                                           glyph: "chevron-right" });
            this.collapseButton.addEventListener("click", () => this.collapse({ focus: true }));
            this.closeButton = button("volume-dock-head-button",
                                      { title: "Close: back to the whole stack", glyph: "xmark" });
            this.closeButton.addEventListener("click", () => this.close());
            head.append(this.title, this.refining, spacer, this.collapseButton, this.closeButton);
            const grid = el("div", "volume-dock-grid");
            const axes = this.axesControl();
            const actions = el("div", "volume-dock-actions", { role: "group", "aria-label": "Cut actions" });
            const made = {
                "cut.axes": axes,
                "cut.face": this.faceControl(),
                "cut.handles": this.handlesControl(),
                "cut.reset": this.resetControl(),
                "cut.toArea": this.toAreaControl(),
                "cut.history": this.undoControl(),
                "cut.interpolation": this.interpolationControl(),
                "cut.handleOpacity": this.handleOpacityControl(),
                "cut.toggle": this.toggleControl(),
                "area.save": this.saveAreaControl(),
                "area.list": this.areaListControl(),
            };
            for (const [id, control] of Object.entries(made)) {
                control.node.setAttribute("data-control", id);
                this.controls.set(id, control);
            }
            actions.append(...["cut.face", "cut.handles", "cut.reset", "cut.toArea", "cut.history"]
                .map((id) => made[id].node));
            grid.append(axes.node, actions);
            root.append(head, grid, made["cut.interpolation"].node, made["cut.handleOpacity"].node,
                        made["area.save"].node, made["area.list"].node, made["cut.toggle"].node);
            root.addEventListener("keydown", (event) => {
                if (event.key !== "Escape") return;
                event.preventDefault();
                event.stopPropagation();
                this.collapse({ focus: true });
            });
            this.root = root;
            host.appendChild(root);
            const repaint = () => this.paint();
            this._off.push(this.viewState.subscribe(repaint));
            for (const name of ["plexora:inspect-preview", "plexora:inspect-changed", "plexora:viewer-prefs-changed",
                                "plexora:view-controls-changed", "plexora:volume-mounted", "plexora:surface-loaded",
                                "plexora:volume-progress", "plexora:saved-areas"]) {
                global.addEventListener(name, repaint);
                this._off.push(() => global.removeEventListener(name, repaint));
            }
            const keys = (event) => this.onKey(event);
            document.addEventListener("keydown", keys, true);
            this._off.push(() => document.removeEventListener("keydown", keys, true));
            this.paint();
            return true;
        }

        destroy() {
            for (const off of this._off.splice(0)) off?.();
            clearTimeout(this._refiningTimer);
            this.root?.remove();
            this.strip?.root?.classList?.remove("has-cut-panel");
            this.root = null;
        }

        /** X, Y and Z down the left: in a Crop each a chip that turns its
         *  axis on or off (the last one stays on); in a Cut a radio, one axis
         *  at a time, with a flip beside it for the side it keeps. */
        axesControl() {
            const node = el("div", "volume-dock-axes", { role: "group", "aria-label": "Axes" });
            const rows = {};
            for (const axis of AXES) {
                const row = el("div", `volume-dock-axis is-${axis}`);
                const chip = button("volume-dock-chip", { title: AXIS_HINT[axis], text: axis.toUpperCase(),
                                                          pressed: false });
                chip.addEventListener("click", () => this.toggleAxis(axis));
                const flip = button("volume-dock-flip", { title: "Keep the other side",
                                                          glyph: "circle-half-stroke" });
                flip.addEventListener("click", () => this.flip(axis));
                row.append(chip, flip);
                node.appendChild(row);
                rows[axis] = { row, chip, flip };
            }
            return {
                node,
                update: (ctx, state, config) => {
                    const inspect = state.inspect;
                    const [verb, verbing] = ctx.cutTool === "crop" ? ["Crop", "cropping"] : ["Cut", "cutting"];
                    const single = Boolean(config.single);
                    node.classList.toggle("has-flip", Boolean(config.flip));
                    node.setAttribute("role", single ? "radiogroup" : "group");
                    for (const axis of AXES) {
                        const { chip, flip } = rows[axis];
                        const on = Boolean(ctx.cutAxes[axis]);
                        chip.classList.toggle("is-active", on);
                        if (single) {
                            chip.setAttribute("role", "radio");
                            chip.setAttribute("aria-checked", on ? "true" : "false");
                            chip.removeAttribute("aria-pressed");
                            say(chip, on ? `${verb} along ${AXIS_HINT[axis]}` : `${verb} along ${AXIS_HINT[axis]} instead`);
                        } else {
                            chip.removeAttribute("role");
                            chip.removeAttribute("aria-checked");
                            chip.setAttribute("aria-pressed", on ? "true" : "false");
                            const last = on && AXES.filter((a) => ctx.cutAxes[a]).length === 1;
                            say(chip, !on ? `${verb} along ${AXIS_HINT[axis]} too`
                                : (last ? `${verb} along ${AXIS_HINT[axis]}: one axis stays on`
                                    : `Stop ${verbing} along ${AXIS_HINT[axis]}`));
                        }
                        // A flip only beside an axis a Cut cuts; its place is
                        // kept, so the chips never shift.
                        flip.hidden = !config.flip;
                        flip.classList.toggle("is-idle", !on);
                        flip.disabled = !on;
                        if (config.flip && on) {
                            const far = inspect.cutaway[axis].keep === "hi";
                            // The glyph's full half is the side kept: its
                            // left as drawn, mirrored for the far (high) side.
                            flip.classList.toggle("is-flipped", far);
                            say(flip, `${axis.toUpperCase()}: keeping the ${far ? "far" : "near"} side. `
                                + `Keep the ${far ? "near" : "far"} side instead`);
                        }
                    }
                },
            };
        }

        /** A Cut: along `axis` alone. A Crop: `axis` on or off; the last
         *  one stays on. */
        toggleAxis(axis) {
            const inspect = this.viewState.get().inspect;
            const tool = inspect.mode === "full" ? inspect.tool : inspect.mode;
            if (tool === "cutaway") {
                if (inspect.axes[axis] && inspect.mode !== "full") return;
                this.change({ axes: Object.fromEntries(AXES.map((a) => [a, a === axis])) });
                return;
            }
            const axes = { ...inspect.axes };
            const on = !axes[axis];
            if (!on && AXES.filter((a) => axes[a]).length <= 1) return;
            axes[axis] = on;
            this.change({ axes });
        }

        /** A Cut's kept side along `axis`, the other way. */
        flip(axis) {
            const keep = this.viewState.get().inspect.cutaway[axis].keep;
            this.change({ cutaway: { [axis]: { keep: keep === "hi" ? "lo" : "hi" } } });
        }

        /** One change of the cut from the panel (one undo entry). Made while
         *  the cut is hidden, it shows the cut again, changed. */
        change(inspect) {
            const now = this.viewState.get().inspect;
            this.cutter.set({ inspect: now?.mode === "full" ? { mode: now.tool, ...inspect } : inspect });
        }

        faceControl() {
            const node = button("volume-dock-action", { title: "Solid cut faces", glyph: "fill", pressed: false });
            node.addEventListener("click", () => {
                this.change({ face: !this.viewState.get().inspect.face });
            });
            return {
                node,
                update: (ctx, state) => {
                    const on = Boolean(state.inspect.face);
                    node.classList.toggle("is-active", on);
                    node.setAttribute("aria-pressed", on ? "true" : "false");
                    say(node, on ? "Solid cut faces: on. Click to see through the cut"
                        : "Solid cut faces: off. Click to draw the voxels on the cut solid");
                },
            };
        }

        handlesControl() {
            const prefs = () => global.PlexoraViewerPrefs;
            const node = button("volume-dock-action", { title: "Hide the handles", glyph: "eye", pressed: true });
            node.addEventListener("click", () => {
                const on = prefs()?.handles?.().visible !== false;
                prefs()?.set?.({ handles: { visible: !on } });
            });
            return {
                node,
                update: () => {
                    const on = prefs()?.handles?.().visible !== false;
                    node.setAttribute("aria-pressed", on ? "true" : "false");
                    node.classList.toggle("is-active", on);
                    say(node, on ? "Handles: shown in the view. Click to hide them"
                        : "Handles: hidden. Click to show them");
                    if (node._glyph !== on) {
                        node.replaceChildren(icon(on ? "eye" : "eye-slash"));
                        node._glyph = on;
                    }
                },
            };
        }

        resetControl() {
            const node = button("volume-dock-action", { title: "Reset the cut", glyph: "rotate" });
            node.addEventListener("click", () => this.cutter.reset("mode"));
            return {
                node,
                update: (ctx) => {
                    say(node, `Reset: put the ${ctx.cutTool === "crop" ? "crop" : "cut"} back where it starts`
                        + " (the axes stay)");
                },
            };
        }

        /** "Crop to area": the area becomes the crop's x and y, fetched at
         *  the finest level it allows and framed (inspectState.cropToArea). */
        toAreaControl() {
            const node = button("volume-dock-action", { title: "Crop to area", glyph: "expand" });
            node.addEventListener("click", () => this.cutter.cropToArea?.());
            return {
                node,
                update: () => {
                    say(node, "Crop to area: draw only the crop, fetched finer, filling the view");
                },
            };
        }

        /** The saved areas (services/savedAreas.js), when the page has them. */
        saved() {
            const api = global.PlexoraSavedAreas;
            const saved = api?.instance;
            return saved && saved.viewState === this.viewState ? saved : null;
        }

        /** "Save area": the area, the cut and the camera, kept by name. */
        saveAreaControl() {
            const node = button("volume-dock-save", { title: "Save area", glyph: "bookmark", text: "Save area" });
            node.addEventListener("click", () => this.saved()?.save());
            return {
                node,
                update: () => say(node, "Save area: keep the area, the cut and the camera, to come back to"),
            };
        }

        /** The saved areas: a thumbnail and a name each; a click restores,
         *  × forgets, a double-click on the name renames. */
        areaListControl() {
            const node = el("ul", "volume-dock-areas", { "aria-label": "Saved areas" });
            const rename = (entry, label) => {
                const input = el("input", "volume-dock-area-rename", { type: "text", value: entry.name,
                                                                        "aria-label": "Name" });
                input.maxLength = 64;
                let done = false;
                const finish = (keep) => {
                    if (done) return;
                    done = true;
                    if (keep) this.saved()?.rename(entry.id, input.value);
                    this.paint();
                };
                input.addEventListener("keydown", (event) => {
                    event.stopPropagation();
                    if (event.key === "Enter") finish(true);
                    else if (event.key === "Escape") finish(false);
                });
                input.addEventListener("blur", () => finish(true));
                label.replaceWith(input);
                input.focus();
                input.select?.();
            };
            return {
                node,
                update: () => {
                    const saved = this.saved();
                    const list = saved ? saved.list() : [];
                    const key = JSON.stringify(list.map((entry) => [entry.id, entry.name, Boolean(saved.thumb(entry.id))]));
                    if (node._key === key) return;
                    node._key = key;
                    node.replaceChildren();
                    for (const entry of list.slice().reverse()) {
                        const item = el("li", "volume-dock-area");
                        const open = el("button", "volume-dock-area-open", { type: "button" });
                        say(open, `Go back to ${entry.name}`);
                        const url = saved.thumb(entry.id);
                        const picture = url ? el("img", "volume-dock-area-thumb", { src: url, alt: "" })
                            : el("span", "volume-dock-area-thumb is-empty", { "aria-hidden": "true" });
                        const label = el("span", "volume-dock-area-name", { text: entry.name });
                        open.append(picture, label);
                        open.addEventListener("click", () => saved.restore(entry.id));
                        open.addEventListener("dblclick", (event) => {
                            event.preventDefault();
                            rename(entry, label);
                        });
                        const remove = button("volume-dock-area-remove", { title: `Forget ${entry.name}`,
                                                                            glyph: "xmark" });
                        remove.addEventListener("click", () => saved.remove(entry.id));
                        item.append(open, remove);
                        node.appendChild(item);
                    }
                },
            };
        }

        undoControl() {
            const node = button("volume-dock-action", { title: "Undo", glyph: "arrow-rotate-left" });
            node.addEventListener("click", () => this.cutter.undo());
            return {
                node,
                update: () => {
                    node.disabled = !this.cutter.canUndo();
                    say(node, `Undo the last change to the cut (${chord("mod+z", "Ctrl+Z")}; `
                        + `${chord("mod+shift+z", "Shift+Ctrl+Z")} redoes)`);
                },
            };
        }

        /** How voxels are read, across the panel's width. */
        interpolationControl() {
            const node = el("div", "plx-popover-segmented volume-dock-segmented",
                            { role: "radiogroup", "aria-label": "Voxels" });
            const choices = [["linear", "Smooth", "Voxels: read between them smoothly"],
                             ["nearest", "Blocks", "Voxels: show them as the blocks they are"]];
            const buttons = new Map();
            for (const [value, text, title] of choices) {
                const segment = el("button", "plx-popover-segment", { type: "button", role: "radio", title,
                                                                      "aria-label": title, text });
                segment.addEventListener("click", () => this.viewState.set({ volume: { interpolation: value } }));
                node.appendChild(segment);
                buttons.set(value, segment);
            }
            return {
                node,
                update: (ctx, state) => {
                    const now = state.volume?.interpolation === "nearest" ? "nearest" : "linear";
                    for (const [value, segment] of buttons) {
                        segment.classList.toggle("is-active", value === now);
                        segment.setAttribute("aria-checked", value === now ? "true" : "false");
                    }
                },
            };
        }

        /** How strongly the handles are drawn (a viewer pref). */
        handleOpacityControl() {
            const node = el("div", "volume-dock-handles", { title: "Handles: how strongly they are drawn" });
            node.appendChild(icon("hand"));
            const holder = el("div", "volume-dock-slider");
            node.appendChild(holder);
            const prefs = global.PlexoraViewerPrefs;
            let slider = null;
            if (typeof global.PlexoraSlider === "function" && prefs?.handles) {
                slider = new global.PlexoraSlider(holder, {
                    min: 0.15, max: 1, step: 0.05, field: false, ariaLabel: "Handle strength",
                    onInput: (value) => prefs.set({ handles: { opacity: value } }, { save: false }),
                    onChange: (value) => prefs.set({ handles: { opacity: value } }),
                });
            }
            return {
                node,
                update: () => {
                    const value = prefs?.handles?.().opacity;
                    if (slider && Number.isFinite(value) && Math.abs(slider.get() - value) > 1e-6) {
                        slider.set(value, { silent: true });
                    }
                },
            };
        }

        /** `O` as a button, under the handles: the cut shown or hidden, the
         *  panel left out. */
        toggleControl() {
            const key = TOGGLE_KEY.toUpperCase();
            const node = el("button", "volume-dock-toggle", { type: "button", "aria-pressed": "true" });
            node.append(el("kbd", "plx-kbd", { text: key }), el("span", null, { text: "Show/Hide" }));
            node.addEventListener("click", () => this.cutter.toggle());
            return {
                node,
                update: (ctx) => {
                    const on = ctx.cut !== "full";
                    node.setAttribute("aria-pressed", on ? "true" : "false");
                    say(node, on ? `Show/Hide (${key}): the cut is shown. Hide it, the panel stays`
                        : `Show/Hide (${key}): the cut is hidden. Show it again`);
                },
            };
        }

        // -- opening and closing ---------------------------------------------

        /** The strip's Cut or Crop pressed: that tool on and the panel out;
         *  pressed again while it cuts, the panel folds in or out, the cut
         *  as it is. Pressed while its cut is hidden, it shows it. */
        pick(tool) {
            if (!this.G.TOOLS.includes(tool)) return;
            const inspect = this.viewState.get().inspect;
            if (inspect?.mode === tool) {
                this.setOpen(!this.open);
                return;
            }
            this.cutter.pick(tool);
            this.setOpen(true);
        }

        /** Bring the panel out (the tool stays as it is). */
        show() {
            this.setOpen(true);
        }

        /** Fold the panel into the strip; the tool and its settings stay. */
        collapse({ focus = false } = {}) {
            const inside = this.root?.contains?.(document.activeElement);
            this.setOpen(false);
            if (focus || inside) this.focusTool();
        }

        /** × : the panel closed and the whole stack back. */
        close() {
            const inside = this.root?.contains?.(document.activeElement);
            this.setOpen(false);
            this.cutter.reset("full");
            if (inside) this.focusTool();
        }

        setOpen(open) {
            this.open = Boolean(open);
            writeOpen(this.open);
            this.paint();
        }

        /** Focus back on the tool's button in the strip. */
        focusTool() {
            const inspect = this.viewState.get().inspect;
            const tool = inspect?.mode === "full" ? inspect?.tool : inspect?.mode;
            this.strip?.nodes?.[`tool.${tool || "cutaway"}`]?.focus?.({ preventScroll: true });
        }

        // -- painting ------------------------------------------------------

        /** Out in 3-D while the panel is open, for the tool last chosen,
         *  its cut shown or hidden (`is-off`); every control the registry
         *  resolves now, with the state's values. */
        paint() {
            if (!this.root) return;
            const controls = global.PlexoraViewControls;
            const ctx = this.context();
            const state = this.state();
            const resolved = controls && ctx ? controls.resolve(ctx, { surface: "dock" }) : [];
            const shown = new Map(resolved.map((entry) => [entry.id, entry.config]));
            const out = this.open && shown.size > 0 && state?.mode === "volume";
            this.root.classList.toggle("is-open", out);
            this.root.inert = !out;
            this.root.setAttribute("aria-hidden", out ? "false" : "true");
            this.strip?.root?.classList?.toggle("has-cut-panel", out);
            if (!out) {
                this.paintRefining(false);
                return;
            }
            this.root.classList.toggle("is-off", ctx.cut === "full");
            this.root.dataset.tool = ctx.cutTool;
            this.title.textContent = TOOL_NAMES[ctx.cutTool] || "Cut";
            for (const [id, control] of this.controls) {
                control.node.hidden = !shown.has(id);
                if (!control.node.hidden) control.update?.(ctx, state, shown.get(id) || {});
            }
            this.paintRefining();
        }

        /** The dot that says the voxels the cut shows are still on their way
         *  (the base level is drawn meanwhile; volumeRenderer.resolved). */
        paintRefining(allowed = true) {
            const renderer = this.renderer;
            const busy = allowed && Boolean(renderer && renderer.status === "ready"
                && typeof renderer.resolved === "function" && !renderer.resolved());
            this.refining.hidden = !busy;
            clearTimeout(this._refiningTimer);
            if (busy) this._refiningTimer = setTimeout(() => this.paintRefining(), 400);
        }

        /**
         * `O`: the cut shown or hidden (the panel as it is), anywhere the
         * 3-D view is on screen and nobody is typing. Cmd/Ctrl+Z,
         * Shift+Cmd/Ctrl+Z: undo and redo a cut, while the user is working
         * in the 3-D view or this panel.
         */
        onKey(event) {
            if (event.defaultPrevented || !this.viewState.isVolume?.()) return;
            const key = String(event.key || "").toLowerCase();
            if (key === TOGGLE_KEY && !(event.metaKey || event.ctrlKey || event.altKey || event.shiftKey)) {
                const canvas = this.renderer?.canvas;
                if (typing() || !canvas || canvas.hidden || document.querySelector?.("dialog[open]")
                    || global.PlexoraConfirm?.modalOpen?.() || !this.cutter.enabled) return;
                event.preventDefault();
                event.stopPropagation();
                if (!event.repeat) this.cutter.toggle();
                return;
            }
            if (!(event.metaKey || event.ctrlKey) || event.altKey || key !== "z") return;
            if (typing()) return;
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

    /** Build the panel beside `strip` for this stack's 3-D view; null where
     *  it cannot be. */
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
        TOGGLE_KEY,
        pick(tool) { api.instance?.pick(tool); },
        open() { api.instance?.show(); },
        collapse() { api.instance?.collapse(); },
        close() { api.instance?.close(); },
        get root() { return api.instance?.root || null; },
    };
    global.PlexoraVolumeDock = api;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    }
})(typeof window !== "undefined" ? window : globalThis);
