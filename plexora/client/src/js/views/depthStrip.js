/**
 * depthStrip.js - the Z strip: moving through a volume without leaving the image.
 *
 * ONE PIECE OF FURNITURE, and only on a volume. A planar image never builds
 * it, binds no key and changes no tile address (services/viewState.js says
 * why). On a z-stack it is a narrow column on the right edge of the canvas:
 *
 *     [plane] [projection] [3-D]   what the picture is
 *     [sections]                   XZ and YZ beside the plane
 *     [play]                       step through the planes; in 3-D, turn it
 *     ┊ rail ┊                     z 0 at the top, as a section's rows are
 *     12 / 40                      what is drawn, in numbers
 *     [⚙]                          the settings for what is drawn now
 *
 * EVERY CONTROL IS ONE THAT DOES SOMETHING NOW. Which of them shows, and what
 * each does, is declared in services/viewControls.js and resolved on every
 * paint: in 3-D the plane under the view is frozen, so the Sections button
 * goes and the rail is the 3-D view's cut along Z (services/inspectState.js):
 * the planes drawn -- dragged as a projection's band is, which crops the
 * stack -- a Z crop's or cutaway's range, or a Z section's or slab's plane
 * as the thumb. Play turns the stack instead of stepping it. A control that
 * does nothing in a mode is hidden there, never left disabled.
 *
 * What the picture is in words ("z 12/40 · 24.0 µm", "MIP z 8–20 · 13
 * planes", viewState.label) is drawn into every capture (imageViewer's
 * renderCurrentViewCanvas), so a projection can never pass for a plane in
 * one; on screen the readout and the Depth card say it.
 *
 * The cog opens the settings that apply to the mode on screen
 * (views/depthSettings.js); everything less frequent lives in the Depth card
 * (views/depthTools.js).
 *
 * Keys, only on a volume and never while typing: `]` / `[` one plane (in 3-D
 * and in a fixed projection, the range one plane), Shift ten, Alt to the last
 * / first; `.` / `,` one frame. Shift+wheel over the image does what `]` / `[`
 * do. No bare letter: every one is already somebody's.
 */
(function (global) {
    "use strict";

    const PLAY_FPS = 6;
    const TICK_EVERY = 5;

    //: What Play says it will do, and what it says while doing it, per kind
    //: (viewControls.js "play" config).
    const PLAY_WORDS = {
        planes: ["Step through the planes", "Stop stepping"],
        band: ["Slide the range through the stack", "Stop sliding"],
        frames: ["Play the frames", "Stop"],
        rotate: ["Rotate the stack (Esc stops)", "Stop rotating"],
    };

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

    function isTyping() {
        if (global.PlexoraShortcuts?.isTyping) return global.PlexoraShortcuts.isTyping();
        const active = document.activeElement;
        if (!active) return false;
        const tag = active.tagName;
        return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || active.isContentEditable;
    }

    class DepthStrip {
        constructor(viewState, { viewer = null, openCard = null, volume = null,
                                 sections = null, extras = null } = {}) {
            this.viewState = viewState;
            this.viewer = viewer;
            this.openCard = openCard;
            //: The volume renderer's controller (views/volumeRenderer.js), or
            //: null where this browser cannot draw one -- then no 3-D button.
            this.volume = volume;
            this.volumeSupported = Boolean(volume);
            //: The orthogonal sections' controller (views/sectionPanes.js),
            //: or null for a stack too thin to cut.
            this.sections = sections;
            //: What the page has, for the controls' context (main.js
            //: coreExtras): shared with the settings popover, so the two can
            //: never disagree about what is offered.
            this.extras = typeof extras === "function" ? extras : null;
            this.root = null;
            //: control id (services/viewControls.js) -> its node.
            this.nodes = {};
            //: The interval stepping planes, a range or frames, or null; and
            //: which of those it steps. Turning in 3-D is the renderer's.
            this.playing = null;
            this.playingKind = null;
            //: What Play does now, and what the rail does (resolved in paint).
            this.playKind = "planes";
            this.railConfig = { plane: true, band: false, bandMove: false };
            this.drag = null;
            //: The plane the pointer rests on along the rail, or null.
            this.hoveredZ = null;
            this.hoverOn = true;
            this.settings = null;
            this._unsubscribe = null;
            this._onKey = (event) => this.onKey(event);
            this._onRepaint = () => this.paint();
        }

        mount() {
            const vs = this.viewState;
            if (!vs?.enabled() || this.root) return false;
            const wrapper = document.getElementById("openseadragon_wrapper");
            if (!wrapper) return false;
            wrapper.classList.add("has-depth-strip");
            this.root = this.build();
            wrapper.appendChild(this.root);
            // A band down the right edge of the canvas layout: 12px of margin,
            // the 36px strip, 8px of air. Nothing docked is placed under it.
            this._band = global.PlexoraCanvasLayout?.reserve?.("depth-strip",
                { edge: "right", size: 56, order: 0 }) || null;
            this._unsubscribe = vs.subscribe(() => this.paint());
            global.addEventListener("plexora:view-changed", this._onSettled = () => this.paintLoading(false));
            // What changes which controls are offered, beside the view state:
            // a control registered (a plugin's), the prefs, Play's turn
            // stopped by a press, a surface arriving, the 3-D view built.
            for (const name of REPAINT_ON) global.addEventListener(name, this._onRepaint);
            document.addEventListener("keydown", this._onKey);
            this.bindWheel();
            this.paint();
            return true;
        }

        destroy() {
            this.stop({ persist: false });
            this.endHover();
            this.settings?.close?.();
            this._unsubscribe?.();
            document.removeEventListener("keydown", this._onKey);
            global.removeEventListener("plexora:view-changed", this._onSettled);
            for (const name of REPAINT_ON) global.removeEventListener(name, this._onRepaint);
            this.root?.remove();
            this._band?.();
            this._band = null;
            document.getElementById("openseadragon_wrapper")?.classList.remove("has-depth-strip");
            this.root = null;
        }

        /** What the page has (`extras`), over what the strip knows itself. */
        pageExtras() {
            return {
                cutSupported: Boolean(this.cutter()),
                volumeSupported: this.volumeSupported,
                sectionsSupported: Boolean(this.sections),
                rotating: Boolean(this.volume?.rotating),
                prefs: global.PlexoraViewerPrefs || null,
                openCard: typeof this.openCard === "function" ? this.openCard : null,
                ...(this.extras ? this.extras() : {}),
            };
        }

        /** The controls' context now (services/viewControls.js), or null on
         *  a page without the registry. Under a hand on the cut, what it
         *  previews (services/inspectState.js). */
        context() {
            const controls = global.PlexoraViewControls;
            if (!controls) return null;
            const vs = this.viewState;
            const cutter = this.cutter();
            const source = cutter?.previewing ? {
                get: () => cutter.effective(), depth: vs.depth, frames: vs.frames, color: vs.color,
                zStep: () => vs.zStep?.() } : vs;
            return controls.context(source, () => this.pageExtras());
        }

        /** The 3-D view's cut (services/inspectState.js), for this stack. */
        cutter() {
            const cutter = global.PlexoraInspect;
            return cutter && cutter.viewState === this.viewState ? cutter : null;
        }

        // -- building ------------------------------------------------------

        build() {
            const vs = this.viewState;
            const root = el("div", "viewer-depth-strip", {
                id: "viewer_depth_strip", role: "group", "aria-label": "Depth",
            });
            this.modes = el("div", "depth-strip-modes", { role: "radiogroup",
                                                          "aria-label": "Show" });
            this.modeButtons = {};
            const offered = [
                ["plane", "square", "One plane"],
                ["slab", "layer-group", "Projection of a range of planes"],
                ["volume", "cube", "The stack in 3-D"],
            ];
            for (const [mode, glyph, title] of offered) {
                if (mode === "volume" && !this.volumeSupported) continue;
                const button = el("button", "depth-strip-button", {
                    type: "button", role: "radio", title, "aria-label": title,
                    "data-depth-mode": mode,
                });
                button.appendChild(icon(glyph));
                button.addEventListener("click", () => this.chooseMode(mode));
                this.modes.appendChild(button);
                this.modeButtons[mode] = button;
                this.nodes[`mode.${mode}`] = button;
            }

            this.sectionsButton = el("button", "depth-strip-button", {
                type: "button", title: "Show XZ and YZ sections through the centre of the view",
                "aria-label": "Sections", "aria-pressed": "false",
            });
            this.sectionsButton.appendChild(icon("table-cells-large"));
            this.sectionsButton.addEventListener("click", () => {
                const on = vs.get().layout === "sections";
                vs.set({ layout: on ? "single" : "sections" });
            });
            this.nodes.sections = this.sectionsButton;

            this.playButton = el("button", "depth-strip-button", {
                type: "button", title: PLAY_WORDS.planes[0],
                "aria-label": PLAY_WORDS.planes[0], "aria-pressed": "false",
            });
            this.playButton.appendChild(icon("play"));
            this.playButton._glyph = "play";
            this.playButton.addEventListener("click", () => (this.isPlaying() ? this.stop() : this.play()));
            this.nodes.play = this.playButton;

            this.rail = this.buildRail();
            this.nodes.rail = this.rail;

            // What is drawn, in numbers: read, not pressed -- the settings
            // are the cog's.
            this.readout = el("div", "depth-strip-readout", {
                role: "status", "aria-live": "polite", title: "What is drawn",
            });
            this.readoutTop = el("span", "depth-strip-readout-top");
            this.readoutBottom = el("span", "depth-strip-readout-bottom");
            this.readout.append(this.readoutTop, this.readoutBottom);
            this.nodes.readout = this.readout;

            this.cog = el("button", "depth-strip-button is-small depth-strip-settings", {
                type: "button", title: "Depth settings", "aria-label": "Depth settings",
                "aria-haspopup": "dialog", "aria-expanded": "false",
            });
            this.cog.appendChild(icon("gear"));
            this.cog.addEventListener("click", (event) => {
                event.stopPropagation();
                this.openSettings();
            });
            this.nodes.cog = this.cog;

            root.append(this.modes, this.sectionsButton, this.playButton, this.rail, this.readout,
                        this.cog);
            // A time series: which frame, and a step either way. Below the
            // planes' readout because z is what the rail moves; `.` and `,`
            // do the same from the keyboard.
            if (vs.frames > 1) {
                this.frame = el("div", "depth-strip-frame", { role: "group",
                    "aria-label": "Frame" });
                this.framePrev = el("button", "depth-strip-button is-small", {
                    type: "button", title: "Previous frame (,)", "aria-label": "Previous frame" });
                this.framePrev.appendChild(icon("backward-step"));
                this.framePrev.addEventListener("click", () => vs.stepTime(-1));
                this.frameText = el("span", "depth-strip-frame-text");
                this.frameNext = el("button", "depth-strip-button is-small", {
                    type: "button", title: "Next frame (.)", "aria-label": "Next frame" });
                this.frameNext.appendChild(icon("forward-step"));
                this.frameNext.addEventListener("click", () => vs.stepTime(1));
                this.frame.append(this.framePrev, this.frameText, this.frameNext);
                root.append(this.frame);
                this.nodes.frame = this.frame;
            }
            return root;
        }

        buildRail() {
            const vs = this.viewState;
            const rail = el("div", "depth-strip-rail", {
                role: "slider", tabindex: "0", "aria-orientation": "vertical",
                "aria-label": "Plane", "aria-valuemin": "0",
                "aria-valuemax": String(Math.max(0, vs.depth - 1)),
            });
            const track = el("div", "depth-strip-track");
            for (let z = 0; z < vs.depth; z += TICK_EVERY) {
                const tick = el("span", "depth-strip-tick");
                tick.style.top = `${this.fraction(z) * 100}%`;
                track.appendChild(tick);
            }
            this.band = el("div", "depth-strip-band");
            this.handleTop = el("span", "depth-strip-handle is-top", { "data-handle": "z0" });
            this.handleBottom = el("span", "depth-strip-handle is-bottom", { "data-handle": "z1" });
            this.band.append(this.handleTop, this.handleBottom);
            // Where a click would land: a mark on the track and the plane's
            // number beside the strip, while the pointer is over the rail
            // and not dragging. Resting there also starts that plane loading
            // (`plexora:plane-hover`, planeMotion.NeighbourPrefetch), so the
            // click it is about to get is often a flip.
            this.hoverMark = el("div", "depth-strip-hover", { "aria-hidden": "true" });
            this.hoverLabel = el("span", "depth-strip-hover-label");
            this.hoverMark.appendChild(this.hoverLabel);
            this.thumb = el("div", "depth-strip-thumb");
            track.append(this.band, this.hoverMark, this.thumb);
            rail.appendChild(track);
            this.track = track;
            this.nodes.band = this.band;
            this.nodes.hover = this.hoverMark;
            this.nodes.thumb = this.thumb;
            rail.addEventListener("pointermove", (event) => this.hover(event));
            rail.addEventListener("pointerleave", () => this.endHover());
            rail.addEventListener("pointerdown", (event) => this.startDrag(event));
            rail.addEventListener("keydown", (event) => {
                const steps = { ArrowUp: -1, ArrowDown: 1, PageUp: -10, PageDown: 10 };
                if (event.key in steps) {
                    event.preventDefault();
                    this.viewState.step(steps[event.key]);
                } else if (event.key === "Home" || event.key === "End") {
                    event.preventDefault();
                    this.toEnd(event.key === "Home" ? -1 : 1);
                }
            });
            rail.addEventListener("wheel", (event) => {
                event.preventDefault();
                const delta = Math.abs(event.deltaY) >= Math.abs(event.deltaX)
                    ? event.deltaY : event.deltaX;
                if (delta) this.viewState.step(delta > 0 ? 1 : -1);
            }, { passive: false });
            return rail;
        }

        // -- painting --------------------------------------------------------

        fraction(z) {
            const top = Math.max(1, this.viewState.depth - 1);
            return Math.max(0, Math.min(1, z / top));
        }

        /** Said once, the first time a projection is chosen: a projection
         *  looks like a plane. */
        noticeProjection(state) {
            if (state.mode !== "slab" || this._noticed) return;
            this._noticed = true;
            let seen = false;
            try {
                seen = global.localStorage?.getItem("plexora.depth.projectionNotice") === "1";
                global.localStorage?.setItem("plexora.depth.projectionNotice", "1");
            } catch (error) {
                seen = false;
            }
            if (!seen) {
                global.PlexoraToast?.show?.({
                    title: "A projection combines planes. Captures and figures record the "
                        + "range and the operation.",
                });
            }
        }

        /**
         * Show the controls that do something now (services/viewControls.js)
         * and nothing else, and say what each does. Never changes the view
         * state: a mode change only repaints, and a plane stepper whose kind
         * no longer applies (3-D entered while stepping) just stops.
         */
        applyControls() {
            const controls = global.PlexoraViewControls;
            const ctx = this.context();
            const resolved = controls && ctx ? controls.resolve(ctx, { surface: "strip" }) : null;
            const shown = resolved ? new Map(resolved.map((entry) => [entry.id, entry.config]))
                : new Map(Object.keys(this.nodes).map((id) => [id, {}]));
            for (const [id, node] of Object.entries(this.nodes)) {
                if (node) node.hidden = !shown.has(id);
            }
            this.modes.hidden = !Object.keys(this.modeButtons).some((mode) => shown.has(`mode.${mode}`));
            const state = this.viewState.get();
            this.playKind = shown.get("play")?.kind
                || (state.mode === "volume" ? "rotate" : "planes");
            this.railConfig = { plane: true, band: state.mode !== "plane", bandMove: false,
                                ...(shown.get("rail") || {}) };
            this.hoverOn = shown.has("hover");
            if (!this.hoverOn) this.endHover();
            if (this.playing && this.playingKind !== this.playKind) this.stop({ persist: false });
            return ctx;
        }

        paint() {
            const vs = this.viewState;
            const cutter = this.cutter();
            const state = cutter?.previewing ? cutter.effective() : vs.get();
            if (!state || !this.root) return;
            this.noticeProjection(state);
            const ctx = this.applyControls();
            for (const [mode, button] of Object.entries(this.modeButtons)) {
                const on = state.mode === mode;
                button.classList.toggle("is-active", on);
                button.setAttribute("aria-checked", on ? "true" : "false");
            }
            const sectionsOn = state.layout === "sections" && state.mode !== "volume";
            this.sectionsButton?.classList.toggle("is-active", sectionsOn);
            this.sectionsButton?.setAttribute("aria-pressed", sectionsOn ? "true" : "false");
            const volume = state.mode === "volume";
            const slab = state.mode === "slab";
            // In 3-D the band is the planes the cut keeps along Z (a range of
            // its own, viewControls keptPlanes), drawn and dragged as a fixed
            // projection's is; the thumb a Z section's or slab's plane.
            const range = volume ? [ctx?.keptZ0 ?? 0, ctx?.keptZ1 ?? vs.depth - 1]
                : [state.slab.z0, state.slab.z1];
            this.root.classList.toggle("is-slab", slab || volume);
            this.root.classList.toggle("is-volume", volume);
            // While the rail is dragged the thumb is the hand's, between planes
            // as much as on them (moveDrag); it settles on the plane after.
            if (this.drag?.kind !== "z" && this.drag?.kind !== "cut-plane") {
                this.thumb.style.top = `${this.fraction(state.z) * 100}%`;
            }
            if (slab || volume) {
                const top = this.fraction(range[0]);
                const bottom = this.fraction(range[1]);
                this.band.style.top = `${top * 100}%`;
                this.band.style.height = `${Math.max(0, bottom - top) * 100}%`;
            }
            this.band.classList.toggle("is-movable", Boolean(this.railConfig.bandMove));
            // A Z cutaway's band has one end that moves: the cut.
            const cutEnd = volume && ctx?.cut === "cutaway"
                ? (state.inspect?.cutaway?.z?.keep === "lo" ? "z1" : "z0") : null;
            this.handleTop.hidden = Boolean(cutEnd) && cutEnd !== "z0";
            this.handleBottom.hidden = Boolean(cutEnd) && cutEnd !== "z1";
            if (volume) {
                const plane = this.railConfig.plane;
                this.rail.setAttribute("aria-label", plane ? "Z section in 3-D" : "Planes drawn in 3-D");
                this.rail.setAttribute("aria-valuenow", String(plane ? state.z : range[0]));
                this.rail.setAttribute("aria-valuetext", plane ? `z ${state.z}` : `planes ${range[0]}–${range[1]}`);
            } else {
                this.rail.setAttribute("aria-label", "Plane");
                this.rail.setAttribute("aria-valuenow", String(state.z));
                this.rail.setAttribute("aria-valuetext", vs.label());
            }
            if (volume) {
                this.readoutTop.textContent = "3-D";
                this.readoutBottom.textContent = this.cutReadout(ctx, state, range);
            } else if (slab) {
                const op = state.slab.op === "pct" ? `p${state.slab.q}`
                    : (state.slab.op === "max" ? "MIP" : state.slab.op);
                this.readoutTop.textContent = op;
                this.readoutBottom.textContent = `${state.slab.z0}–${state.slab.z1}`;
            } else {
                this.readoutTop.textContent = String(state.z);
                this.readoutBottom.textContent = `/ ${vs.depth}`;
            }
            this.readoutBottom.classList.toggle(
                "is-long", this.readoutBottom.textContent.length > 4);
            if (this.frameText) {
                this.frameText.textContent = `t ${state.t + 1}`;
                this.frameText.title = `Frame ${state.t + 1} of ${vs.frames}`;
                this.framePrev.disabled = state.t <= 0;
                this.frameNext.disabled = state.t >= vs.frames - 1;
            }
            this.paintPlay();
            this.paintLoading(vs.spec() !== vs.appliedSpec());
            // The popover follows the mode, open (depthSettings.js).
            this.settings?.refresh?.();
        }

        /** The readout's second line in 3-D: what is cut along Z. */
        cutReadout(ctx, state, range) {
            const cut = ctx?.cut || "full";
            if (cut === "full") return "all";
            if (!ctx?.cutAxes?.z) return { cutaway: "cut", section: "sect.", slab: "slab", crop: "crop" }[cut] || "";
            if (cut === "section" || (cut === "slab" && ctx.slabComposite === "single")) return `z ${state.z}`;
            return `${range[0]}–${range[1]}`;
        }

        paintLoading(on) {
            // The thumb sits on the plane asked for as soon as it is asked
            // for; while that plane is still on its way it breathes
            // (viewer.css), so a far jump reads as under way, not ignored.
            this.root?.classList.toggle("is-loading", Boolean(on));
        }

        /** Play's glyph and words for what it does now, and whether it is. */
        paintPlay() {
            const button = this.playButton;
            if (!button) return;
            const words = PLAY_WORDS[this.playKind] || PLAY_WORDS.planes;
            const on = this.isPlaying();
            const label = on ? words[1] : words[0];
            button.setAttribute("aria-pressed", on ? "true" : "false");
            button.setAttribute("aria-label", label);
            button.title = label;
            const glyph = on ? "pause" : "play";
            if (button._glyph !== glyph) {
                button.replaceChildren(on ? icon("pause") : icon("play"));
                button._glyph = glyph;
            }
        }

        /** The pointer over the rail: mark the plane a click would choose. */
        hover(event) {
            if (this.drag || event.pointerType === "touch" || !this.hoverOn) {
                this.endHover();
                return;
            }
            const vs = this.viewState;
            if (vs.depth < 2) return;
            const z = this.zAt(event.clientY);
            this.hoverMark.style.top = `${this.fraction(z) * 100}%`;
            this.hoverLabel.textContent = `z ${z}`;
            this.rail.classList.add("is-hovering");
            if (z === this.hoveredZ) return;
            this.hoveredZ = z;
            global.dispatchEvent?.(new CustomEvent("plexora:plane-hover", { detail: { z } }));
        }

        endHover() {
            this.rail?.classList.remove("is-hovering");
            if (this.hoveredZ === null || this.hoveredZ === undefined) return;
            this.hoveredZ = null;
            global.dispatchEvent?.(new CustomEvent("plexora:plane-hover", { detail: { z: null } }));
        }

        // -- interaction -----------------------------------------------------

        /** Switch what the picture is (viewControls.transition: entering 3-D
         *  from a slab that follows the plane opens the whole stack). */
        chooseMode(mode) {
            const vs = this.viewState;
            const controls = global.PlexoraViewControls;
            vs.set(controls ? controls.transition(controls.context(vs), mode) : { mode });
        }

        /** To the first (-1) or last (1) plane -- in a fixed projection the
         *  range moved there whole, in 3-D what is cut along Z. */
        toEnd(sign) {
            const vs = this.viewState;
            const state = vs.get();
            if (state.mode === "volume" || (state.mode === "slab" && !state.slab.follow)) {
                vs.step(sign * vs.depth);
            } else {
                vs.set({ z: sign > 0 ? vs.depth - 1 : 0 });
            }
        }

        zAt(clientY) {
            return Math.round(this.zExact(clientY));
        }

        /** Where the pointer is along the rail, in planes, unrounded. */
        zExact(clientY) {
            const box = this.track.getBoundingClientRect();
            const fraction = box.height > 0 ? (clientY - box.top) / box.height : 0;
            const top = this.viewState.depth - 1;
            return Math.max(0, Math.min(top, fraction * top));
        }

        /** The thumb under the pointer, between planes as much as on them. */
        followThumb(clientY) {
            this.thumb.style.top = `${this.fraction(this.zExact(clientY)) * 100}%`;
        }

        /**
         * A press on the rail: a band end resizes the range, a press inside
         * a range that can move moves it, and anywhere else picks the plane --
         * except in 3-D, where the plane is not what is drawn and a press
         * outside the clip range does nothing (viewControls "rail" config).
         */
        startDrag(event) {
            if (event.button !== 0) return;
            event.preventDefault();
            this.rail.focus?.({ preventScroll: true });
            const vs = this.viewState;
            const state = vs.get();
            const config = this.railConfig;
            const handle = event.target?.dataset?.handle;
            const z = this.zAt(event.clientY);
            if (state.mode === "volume") {
                if (!this.startCutDrag(event, state, z, handle)) return;
                this.listenDrag();
                return;
            }
            const inBand = z >= state.slab.z0 && z <= state.slab.z1;
            if (config.band && handle) {
                this.drag = { kind: handle };
            } else if (config.bandMove && inBand) {
                this.drag = { kind: "band", from: z, z0: state.slab.z0, z1: state.slab.z1 };
            } else if (config.plane) {
                this.drag = { kind: "z" };
                this.rail.classList.add("is-dragging");
                this.rail.classList.remove("is-hovering");
                this.followThumb(event.clientY);
                vs.set({ z });
            } else {
                return;
            }
            this.listenDrag();
        }

        /** Follow the pointer until it is let go (moveDrag), then commit a
         *  cut previewed under it as one change. */
        listenDrag() {
            const move = (e) => this.moveDrag(e);
            const up = () => {
                const kind = this.drag?.kind || "";
                this.drag = null;
                this.rail.classList.remove("is-dragging");
                global.removeEventListener("pointermove", move);
                global.removeEventListener("pointerup", up);
                // One drag, one undo entry.
                if (kind.startsWith("cut-")) this.cutter()?.commit();
                // Onto the plane on screen (a short glide, viewer.css).
                if (kind === "z" || kind === "cut-plane") this.paint();
            };
            global.addEventListener("pointermove", move);
            global.addEventListener("pointerup", up);
        }

        /**
         * A press on the rail in 3-D (viewControls "rail" config): on a Z
         * section's or slab's rail it picks the plane; on the planes drawn a
         * band end resizes them -- the whole stack becomes a Z crop -- and the
         * band moves; a Z cutaway's one end moves its cut. Anything else
         * along the rail is not the cut's, and does nothing. Returns whether
         * a drag began.
         */
        startCutDrag(event, state, z, handle) {
            const config = this.railConfig;
            const ctx = this.context();
            const kept = [ctx?.keptZ0 ?? 0, ctx?.keptZ1 ?? this.viewState.depth - 1];
            if (config.plane) {
                this.drag = { kind: "cut-plane" };
                this.rail.classList.add("is-dragging");
                this.rail.classList.remove("is-hovering");
                this.followThumb(event.clientY);
                this.cut({ z });
            } else if (config.band && handle) {
                this.drag = { kind: `cut-${handle}`, cut: config.cut, kept, axes: state.inspect?.axes };
            } else if (config.bandMove && z >= kept[0] && z <= kept[1]) {
                this.drag = { kind: "cut-band", from: z, kept, cut: config.cut, axes: state.inspect?.axes };
            } else {
                return false;
            }
            return true;
        }

        /** Preview a cut under the hand (services/inspectState.js); a page
         *  without the cut controller sets it outright. */
        cut(patch) {
            const cutter = this.cutter();
            if (cutter) cutter.preview(patch);
            else this.viewState.set(patch);
        }

        /** A Z crop of planes `[z0, z1]`: the whole stack becomes one, keeping
         *  no crop of another axis it may have kept from before. */
        cropZ(drag, z0, z1) {
            const axes = drag.cut === "full" ? { x: false, y: false, z: true } : { z: true };
            this.cut({ inspect: { mode: "crop", axes, crop: { z: [z0, z1] } } });
        }

        moveDrag(event) {
            const drag = this.drag;
            if (!drag) return;
            const vs = this.viewState;
            const state = vs.get();
            const z = this.zAt(event.clientY);
            if (drag.kind === "cut-plane") {
                this.followThumb(event.clientY);
                this.cut({ z });
            } else if (drag.kind === "cut-z0" || drag.kind === "cut-z1") {
                if (drag.cut === "cutaway") {
                    this.cut({ inspect: { cutaway: { z: { at: z } } } });
                } else {
                    let [z0, z1] = drag.kept;
                    if (drag.kind === "cut-z0") z0 = Math.min(z, z1);
                    else z1 = Math.max(z, z0);
                    this.cropZ(drag, z0, z1);
                }
            } else if (drag.kind === "cut-band") {
                const width = drag.kept[1] - drag.kept[0];
                const start = Math.max(0, Math.min(vs.depth - 1 - width, drag.kept[0] + (z - drag.from)));
                this.cropZ(drag, start, start + width);
            } else if (drag.kind === "z") {
                this.followThumb(event.clientY);
                if (z !== state.z) vs.set({ z });
            } else if (drag.kind === "z0" || drag.kind === "z1") {
                let { z0, z1 } = state.slab;
                if (drag.kind === "z0") z0 = Math.min(z, z1);
                else z1 = Math.max(z, z0);
                if (z0 !== state.slab.z0 || z1 !== state.slab.z1) {
                    vs.set({ z: Math.round((z0 + z1) / 2), slab: { z0, z1, thickness: z1 - z0 + 1 } });
                }
            } else if (drag.kind === "band") {
                const width = drag.z1 - drag.z0;
                const top = vs.depth - 1;
                const start = Math.max(0, Math.min(top - width, drag.z0 + (z - drag.from)));
                if (start !== state.slab.z0) {
                    vs.set({ slab: { z0: start, z1: start + width } });
                }
            }
        }

        /** The cog: the settings for what is drawn now (depthSettings.js). */
        openSettings() {
            const Settings = global.PlexoraDepthSettings;
            if (!Settings) return;
            if (!this.settings) this.settings = new Settings({ strip: this });
            this.settings.toggle(this.cog);
        }

        /** Whether Play is running: stepping, or turning the stack. */
        isPlaying() {
            return Boolean(this.playing) || (this.playKind === "rotate" && Boolean(this.volume?.rotating));
        }

        get rotating() {
            return Boolean(this.volume?.rotating);
        }

        /**
         * Play, as the mode has it (viewControls "play" config): step through
         * the planes; slide a fixed projection's range; play the frames of a
         * single-plane series; or in 3-D turn the stack, at the speed and
         * about the axis of the settings (services/viewerPrefs.js `rotate`).
         */
        play() {
            const vs = this.viewState;
            if (this.isPlaying()) return;
            const kind = this.playKind;
            if (kind === "rotate") {
                this.volume?.rotate?.(true, global.PlexoraViewerPrefs?.rotate?.() || {});
                this.paintPlay();
                return;
            }
            this.playingKind = kind;
            this.playing = setInterval(() => {
                const state = vs.get();
                if (kind === "frames") {
                    vs.set({ t: (state.t + 1) % vs.frames }, { save: false });
                } else if (kind === "band") {
                    const width = state.slab.z1 - state.slab.z0;
                    const start = state.slab.z1 >= vs.depth - 1 ? 0 : state.slab.z0 + 1;
                    vs.set({ slab: { z0: start, z1: start + width } }, { save: false });
                } else {
                    vs.set({ z: (state.z + 1) % vs.depth }, { save: false });
                }
            }, 1000 / PLAY_FPS);
            this.paintPlay();
        }

        /** Stop Play. `persist: false` when it stops because the mode moved
         *  on: nobody chose the plane it stopped on. */
        stop({ persist = true } = {}) {
            if (this.volume?.rotating) this.volume.rotate?.(false);
            if (this.playing) {
                clearInterval(this.playing);
                this.playing = null;
                this.playingKind = null;
                // The plane playback stopped on is the one to reopen on.
                if (persist) this.viewState.persist();
            }
            this.paintPlay();
        }

        onKey(event) {
            if (event.defaultPrevented || isTyping() || document.querySelector("dialog[open]")) {
                return;
            }
            if (event.metaKey || event.ctrlKey) return;
            const vs = this.viewState;
            if (event.code === "BracketRight" || event.code === "BracketLeft") {
                if (vs.depth < 2) return;
                event.preventDefault();
                const sign = event.code === "BracketRight" ? 1 : -1;
                if (event.altKey) {
                    this.toEnd(sign);
                } else {
                    // A plane; in 3-D and in a fixed projection, the range
                    // (viewState.step).
                    vs.step(sign * (event.shiftKey ? 10 : 1));
                }
            } else if ((event.code === "Period" || event.code === "Comma") && vs.frames > 1
                       && !event.altKey) {
                event.preventDefault();
                vs.stepTime(event.code === "Period" ? 1 : -1);
            }
        }

        /** Shift+wheel over the image does what `]` and `[` do. */
        bindWheel() {
            const viewer = this.viewer;
            if (!viewer?.addHandler || this.viewState.depth < 2) return;
            viewer.addHandler("canvas-scroll", (event) => {
                const original = event.originalEvent;
                if (!event.shift && !original?.shiftKey) return;
                event.preventDefaultAction = true;
                if (original?.preventDefault) original.preventDefault();
                const dy = original ? (Math.abs(original.deltaY) >= Math.abs(original.deltaX)
                    ? original.deltaY : original.deltaX) : -event.scroll;
                if (dy) this.viewState.step(dy > 0 ? 1 : -1);
            });
        }
    }

    //: Events after which the strip's controls are resolved again.
    const REPAINT_ON = ["plexora:viewer-prefs-changed", "plexora:view-controls-changed",
                        "plexora:volume-rotate", "plexora:volume-mounted",
                        "plexora:surface-loaded", "plexora:inspect-preview", "plexora:inspect-changed"];

    DepthStrip.PLAY_WORDS = PLAY_WORDS;
    global.PlexoraDepthStrip = DepthStrip;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = DepthStrip;
    }
})(typeof window !== "undefined" ? window : globalThis);
