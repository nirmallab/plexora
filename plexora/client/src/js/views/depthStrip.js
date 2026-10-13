/**
 * depthStrip.js - the Z strip: moving through a volume without leaving the image.
 *
 * ONE PIECE OF FURNITURE, and only on a volume. A planar image never builds
 * it, binds no key and changes no tile address (services/viewState.js says
 * why). On a z-stack it is a narrow column on the right edge of the canvas:
 *
 *     [plane] [projection]      what the picture is
 *     [play]                    step through the planes
 *     ┊ rail ┊                  z 0 at the top, as a section's rows are
 *     [12 / 40]                 the readout; click it for the operation
 *
 * What the picture is in words ("z 12/40 · 24.0 µm", "MIP z 8–20 · 13
 * planes", viewState.label) is not on the canvas -- the caption is the
 * project's name alone -- but it is drawn into every capture (imageViewer's
 * renderCurrentViewCanvas), so a projection can never pass for a plane in
 * one, and the rail's readout and the Depth card say it on screen.
 *
 * Everything less frequent lives in the Depth card (views/depthTools.js).
 *
 * Keys, only on a volume and never while typing: `]` / `[` one plane,
 * Shift ten, Alt to the last / first; `.` / `,` one frame. Shift+wheel over
 * the image scrubs z. No bare letter: every one is already somebody's.
 */
(function (global) {
    "use strict";

    const PLAY_FPS = 6;
    const TICK_EVERY = 5;
    //: The 3-D grid's strengths, as the menu names them: its lines' opacity.
    const GRID_STRENGTHS = [[0.18, "faint"], [0.32, "medium"], [0.5, "strong"]];

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
                                 sections = null } = {}) {
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
            this.root = null;
            this.playing = null;
            this.drag = null;
            //: The plane the pointer rests on along the rail, or null.
            this.hoveredZ = null;
            this._unsubscribe = null;
            this._onKey = (event) => this.onKey(event);
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
            document.addEventListener("keydown", this._onKey);
            this.bindWheel();
            this.paint();
            return true;
        }

        destroy() {
            this.stop();
            this.endHover();
            this._unsubscribe?.();
            document.removeEventListener("keydown", this._onKey);
            global.removeEventListener("plexora:view-changed", this._onSettled);
            this.root?.remove();
            this._band?.();
            this._band = null;
            document.getElementById("openseadragon_wrapper")?.classList.remove("has-depth-strip");
            this.root = null;
        }

        // -- building ------------------------------------------------------

        build() {
            const vs = this.viewState;
            const root = el("div", "viewer-depth-strip", {
                id: "viewer_depth_strip", role: "group", "aria-label": "Depth",
            });
            const modes = el("div", "depth-strip-modes", { role: "radiogroup",
                                                           "aria-label": "Show" });
            this.modeButtons = {};
            const offered = [
                ["plane", "square", "One plane"],
                ["slab", "layer-group", "Projection of a range of planes"],
            ];
            if (this.volumeSupported) offered.push(["volume", "cube", "The stack in 3-D"]);
            for (const [mode, glyph, title] of offered) {
                const button = el("button", "depth-strip-button", {
                    type: "button", role: "radio", title, "aria-label": title,
                    "data-depth-mode": mode,
                });
                button.appendChild(icon(glyph));
                button.addEventListener("click", () => this.chooseMode(mode));
                modes.appendChild(button);
                this.modeButtons[mode] = button;
            }
            // One plane at a time is all a colour stack offers.
            if (vs.depth < 2 || vs.color) modes.hidden = true;

            this.sectionsButton = el("button", "depth-strip-button", {
                type: "button", title: "Show XZ and YZ sections through the centre of the view",
                "aria-label": "Sections", "aria-pressed": "false",
            });
            this.sectionsButton.appendChild(icon("table-cells-large"));
            this.sectionsButton.addEventListener("click", () => {
                const state = vs.get();
                const on = state.layout === "sections";
                vs.set({ layout: on ? "single" : "sections",
                         mode: !on && state.mode === "volume" ? "plane" : state.mode });
            });
            if (!this.sections || vs.color) this.sectionsButton.hidden = true;

            this.playButton = el("button", "depth-strip-button", {
                type: "button", title: "Step through the planes",
                "aria-label": "Step through the planes", "aria-pressed": "false",
            });
            this.playButton.appendChild(icon("play"));
            this.playButton.addEventListener("click", () => (this.playing ? this.stop() : this.play()));
            if (vs.depth < 2 && vs.frames < 2) this.playButton.hidden = true;

            this.rail = this.buildRail();

            this.readout = el("button", "depth-strip-readout", {
                type: "button", title: "Projection and depth options",
                "aria-haspopup": "menu",
            });
            this.readoutTop = el("span", "depth-strip-readout-top");
            this.readoutBottom = el("span", "depth-strip-readout-bottom");
            this.readout.append(this.readoutTop, this.readoutBottom);
            this.readout.addEventListener("click", (event) => {
                event.stopPropagation();
                this.openOptions();
            });

            root.append(modes, this.sectionsButton, this.playButton, this.rail, this.readout);
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
                    this.viewState.set({ z: event.key === "Home" ? 0 : vs.depth - 1 });
                }
            });
            rail.addEventListener("wheel", (event) => {
                event.preventDefault();
                const delta = Math.abs(event.deltaY) >= Math.abs(event.deltaX)
                    ? event.deltaY : event.deltaX;
                if (delta) this.viewState.step(delta > 0 ? 1 : -1);
            }, { passive: false });
            if (vs.depth < 2) rail.hidden = true;
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

        paint() {
            const vs = this.viewState;
            const state = vs.get();
            if (!state || !this.root) return;
            this.noticeProjection(state);
            for (const [mode, button] of Object.entries(this.modeButtons)) {
                const on = state.mode === mode;
                button.classList.toggle("is-active", on);
                button.setAttribute("aria-checked", on ? "true" : "false");
            }
            const sectionsOn = state.layout === "sections" && state.mode !== "volume";
            this.sectionsButton?.classList.toggle("is-active", sectionsOn);
            this.sectionsButton?.setAttribute("aria-pressed", sectionsOn ? "true" : "false");
            const volume = state.mode === "volume";
            // In 3-D the band is the clip range, so it is drawn and dragged
            // exactly as a projection's is.
            const slab = state.mode === "slab" || volume;
            this.root.classList.toggle("is-slab", slab);
            this.root.classList.toggle("is-volume", volume);
            // While the rail is dragged the thumb is the hand's, between planes
            // as much as on them (moveDrag); it settles on the plane after.
            if (this.drag?.kind !== "z") this.thumb.style.top = `${this.fraction(state.z) * 100}%`;
            this.band.hidden = !slab;
            if (slab) {
                const top = this.fraction(state.slab.z0);
                const bottom = this.fraction(state.slab.z1);
                this.band.style.top = `${top * 100}%`;
                this.band.style.height = `${Math.max(0, bottom - top) * 100}%`;
            }
            this.rail.setAttribute("aria-valuenow", String(state.z));
            this.rail.setAttribute("aria-valuetext", vs.label());
            if (volume) {
                this.readoutTop.textContent = "3-D";
                this.readoutBottom.textContent = `${state.slab.z0}–${state.slab.z1}`;
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
            this.paintLoading(vs.spec() !== vs.appliedSpec());
        }

        paintLoading(on) {
            // The thumb sits on the plane asked for as soon as it is asked
            // for; while that plane is still on its way it breathes
            // (viewer.css), so a far jump reads as under way, not ignored.
            this.root?.classList.toggle("is-loading", Boolean(on));
        }

        /** The pointer over the rail: mark the plane a click would choose. */
        hover(event) {
            if (this.drag || event.pointerType === "touch") {
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

        /**
         * Switch what the picture is. Entering 3-D with the slab still at its
         * default (centred, following z) opens the whole stack: in 3-D the
         * band is the clip range, and a first look should show everything.
         */
        chooseMode(mode) {
            const vs = this.viewState;
            const state = vs.get();
            if (mode === "volume" && state.mode !== "volume" && state.slab.follow) {
                vs.set({ mode, slab: { z0: 0, z1: vs.depth - 1, follow: false,
                                       thickness: vs.depth } });
                return;
            }
            vs.set({ mode });
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

        startDrag(event) {
            if (event.button !== 0) return;
            event.preventDefault();
            this.rail.focus({ preventScroll: true });
            const vs = this.viewState;
            const state = vs.get();
            const handle = event.target?.dataset?.handle;
            const z = this.zAt(event.clientY);
            if (state.mode === "slab" && handle) {
                this.drag = { kind: handle };
            } else if (state.mode === "slab" && z >= state.slab.z0 && z <= state.slab.z1
                       && !state.slab.follow) {
                this.drag = { kind: "band", from: z, z0: state.slab.z0, z1: state.slab.z1 };
            } else {
                this.drag = { kind: "z" };
                this.rail.classList.add("is-dragging");
                this.rail.classList.remove("is-hovering");
                this.followThumb(event.clientY);
                vs.set({ z });
            }
            const move = (e) => this.moveDrag(e);
            const up = () => {
                const following = this.drag?.kind === "z";
                this.drag = null;
                this.rail.classList.remove("is-dragging");
                global.removeEventListener("pointermove", move);
                global.removeEventListener("pointerup", up);
                // Onto the plane on screen (a short glide, viewer.css).
                if (following) this.paint();
            };
            global.addEventListener("pointermove", move);
            global.addEventListener("pointerup", up);
        }

        moveDrag(event) {
            const drag = this.drag;
            if (!drag) return;
            const vs = this.viewState;
            const state = vs.get();
            const z = this.zAt(event.clientY);
            if (drag.kind === "z") {
                this.followThumb(event.clientY);
                if (z !== state.z) vs.set({ z });
            } else if (drag.kind === "z0" || drag.kind === "z1") {
                let { z0, z1 } = state.slab;
                if (drag.kind === "z0") z0 = Math.min(z, z1);
                else z1 = Math.max(z, z0);
                if (z0 !== state.slab.z0 || z1 !== state.slab.z1) {
                    vs.set({ z: Math.round((z0 + z1) / 2),
                             slab: { z0, z1, thickness: z1 - z0 + 1 } });
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

        openOptions() {
            const vs = this.viewState;
            const state = vs.get();
            if (state.mode === "volume") {
                this.openVolumeOptions(state);
                return;
            }
            const items = [];
            const ops = [["max", "Maximum (MIP)"], ["mean", "Mean"], ["min", "Minimum"],
                         ["std", "Std. deviation"], ["med", "Median"]];
            for (const [op, label] of ops) {
                items.push({
                    label, checked: state.mode === "slab" && state.slab.op === op,
                    onSelect: () => vs.set({ mode: "slab", slab: { op } }),
                });
            }
            items.push({
                label: "95th percentile",
                checked: state.mode === "slab" && state.slab.op === "pct",
                onSelect: () => vs.set({ mode: "slab", slab: { op: "pct", q: 95 } }),
            });
            items.push({ separator: true });
            items.push({
                label: "Follow the plane",
                checked: Boolean(state.slab.follow),
                title: "Keep the projection centred on the current plane",
                onSelect: () => vs.set({ slab: { follow: !state.slab.follow } }),
            });
            items.push({
                label: "One plane",
                checked: state.mode === "plane",
                onSelect: () => vs.set({ mode: "plane" }),
            });
            if (typeof this.openCard === "function") {
                items.push({ separator: true });
                items.push({ label: "Depth options…", onSelect: () => this.openCard() });
            }
            global.PlexoraMenu?.open?.(this.readout, items);
        }

        openVolumeOptions(state) {
            const vs = this.viewState;
            const renderer = this.volume?.renderer;
            const composite = state.volume?.composite === "additive" ? "additive" : "mip";
            const items = [
                { label: "Maximum intensity (MIP)", checked: composite === "mip",
                  onSelect: () => vs.set({ volume: { composite: "mip" } }) },
                { label: "Additive", checked: composite === "additive",
                  onSelect: () => vs.set({ volume: { composite: "additive" } }) },
                { separator: true },
                { label: "View",
                  actions: [
                      { icon: "fas fa-arrows-to-dot", title: "Looking down onto XY",
                        onSelect: () => renderer?.preset("xy") },
                      { icon: "fas fa-grip-lines", title: "From the side, XZ",
                        onSelect: () => renderer?.preset("xz") },
                      { icon: "fas fa-grip-lines-vertical", title: "From the side, YZ",
                        onSelect: () => renderer?.preset("yz") },
                      { icon: "fas fa-cube", title: "Three-quarter view",
                        onSelect: () => renderer?.preset("iso") },
                  ] },
                { label: "All planes",
                  onSelect: () => vs.set({ slab: { z0: 0, z1: vs.depth - 1, follow: false,
                                                    thickness: vs.depth } }) },
                ...this.gridItems(renderer),
            ];
            // Surfaces (services/surfaceLayers.js): what the 3-D view shows,
            // once there is a surface, and a new one.
            const surfaces = global.PlexoraSurfaceLayers;
            if (surfaces) {
                items.push({ separator: true });
                if (surfaces.surfaceLayers().length) {
                    const show = surfaces.shown();
                    items.push(
                        { label: "Volume and surfaces", checked: show.volume && show.surfaces,
                          onSelect: () => surfaces.setShow({ volume: true, surfaces: true }) },
                        { label: "Volume only", checked: show.volume && !show.surfaces,
                          onSelect: () => surfaces.setShow({ volume: true, surfaces: false }) },
                        { label: "Surfaces only", checked: !show.volume && show.surfaces,
                          onSelect: () => surfaces.setShow({ volume: false, surfaces: true }) });
                }
                items.push({ label: "New surface…", title: "Surface Studio (S)",
                             onSelect: () => global.PlexoraSurfaceStudio?.open() });
                items.push({ label: "Import mesh…", title: "PLY, OBJ, STL, GLB, or a zip Plexora exported",
                             onSelect: () => {
                                 const input = document.createElement("input");
                                 input.type = "file";
                                 input.accept = ".ply,.obj,.stl,.glb,.zip";
                                 input.addEventListener("change", () => {
                                     const file = input.files?.[0];
                                     if (!file) return;
                                     surfaces.importMesh(file).catch((error) => global.PlexoraToast?.show?.(
                                         { title: error.message, tone: "warning" }));
                                 });
                                 input.click();
                             } });
                if (surfaces.surfaceLayers().length) {
                    const current = surfaces.lightName();
                    items.push({ label: "Light",
                                 actions: [["left", "arrow-left", "Light from the left"],
                                           ["above", "arrow-up", "Light from above"],
                                           ["right", "arrow-right", "Light from the right"],
                                           ["front", "circle-dot", "Light from the front"]]
                                     .map(([name, glyph, title]) => ({
                                         icon: `fas fa-${glyph}`,
                                         title: current === name ? `${title} (now)` : title,
                                         onSelect: () => surfaces.setLight(name) })) });
                }
            }
            items.push({ separator: true },
                       { label: "Back to one plane", onSelect: () => vs.set({ mode: "plane" }) });
            global.PlexoraMenu?.open?.(this.readout, items);
        }

        /**
         * The 3-D view's reference grid (views/volumeRenderer.js), a per-user
         * preference (services/viewerPrefs.js `grid`): the switch, and while
         * it is on where it is drawn, its spacing, its strength and its
         * numbers. A spacing or strength step opens the menu again, so it
         * can be stepped more than once.
         */
        gridItems(renderer) {
            const prefs = global.PlexoraViewerPrefs;
            const helpers = global.PlexoraVolumeRenderer?.grid;
            if (!prefs?.grid || !helpers) return [];
            const grid = prefs.grid();
            const set = (patch) => prefs.set({ grid: patch });
            const again = () => this.openVolumeOptions(this.viewState.get());
            const items = [
                { separator: true },
                { label: "Grid", checked: grid.on,
                  title: "Lines on the far walls of the box, behind the tissue",
                  onSelect: () => set({ on: !grid.on }) },
            ];
            if (!grid.on) return items;
            items.push(
                { label: "On the walls", checked: grid.faces === "walls",
                  onSelect: () => set({ faces: "walls" }) },
                { label: "On the floor", checked: grid.faces === "floor",
                  title: "Only on the far face across z",
                  onSelect: () => set({ faces: "floor" }) });
            const axes = renderer?.gridAxes?.(grid.step) || null;
            if (axes) {
                const unit = axes.units[0];
                const step = axes.step[0];
                const say = (value) => `${helpers.formatTick(value)} ${unit}`;
                const finer = helpers.niceNeighbour(step, -1);
                const coarser = helpers.niceNeighbour(step, 1);
                // Finer than the grid allows draws the same lines (gridAxes).
                const finest = renderer.gridAxes(finer).step[0] >= step;
                items.push({
                    label: `Spacing: ${say(step)}${grid.step === "auto" ? " (auto)" : ""}`,
                    actions: [
                        { icon: "fas fa-minus", title: `Finer: ${say(finer)}`, disabled: finest,
                          onSelect: () => { set({ step: finer }); again(); } },
                        { icon: "fas fa-plus", title: `Coarser: ${say(coarser)}`,
                          onSelect: () => { set({ step: coarser }); again(); } },
                        { icon: "fas fa-rotate-left", title: `Automatic: ${say(axes.auto)}`,
                          disabled: grid.step === "auto",
                          onSelect: () => { set({ step: "auto" }); again(); } },
                    ] });
            }
            const strengths = GRID_STRENGTHS;
            let at = 0;
            strengths.forEach(([value], index) => {
                if (Math.abs(value - grid.opacity) < Math.abs(strengths[at][0] - grid.opacity)) at = index;
            });
            items.push({
                label: `Lines: ${strengths[at][1]}`,
                actions: [
                    { icon: "fas fa-minus", title: at > 0 ? `Fainter: ${strengths[at - 1][1]}` : "Fainter",
                      disabled: at <= 0,
                      onSelect: () => { set({ opacity: strengths[at - 1][0] }); again(); } },
                    { icon: "fas fa-plus",
                      title: at < strengths.length - 1 ? `Stronger: ${strengths[at + 1][1]}` : "Stronger",
                      disabled: at >= strengths.length - 1,
                      onSelect: () => { set({ opacity: strengths[at + 1][0] }); again(); } },
                ] },
            { label: "Axis labels", checked: grid.labels,
              title: "Numbers along three edges of the box",
              onSelect: () => set({ labels: !grid.labels }) });
            return items;
        }

        play() {
            const vs = this.viewState;
            if (this.playing) return;
            this.playButton.setAttribute("aria-pressed", "true");
            this.playButton.replaceChildren(icon("pause"));
            const byTime = vs.depth < 2 && vs.frames > 1;
            this.playing = setInterval(() => {
                const state = vs.get();
                if (byTime) {
                    vs.set({ t: (state.t + 1) % vs.frames }, { save: false });
                } else {
                    vs.set({ z: (state.z + 1) % vs.depth }, { save: false });
                }
            }, 1000 / PLAY_FPS);
        }

        stop() {
            if (!this.playing) return;
            clearInterval(this.playing);
            this.playing = null;
            this.playButton?.setAttribute("aria-pressed", "false");
            this.playButton?.replaceChildren(icon("play"));
            // The plane playback stopped on is the one to reopen on.
            this.viewState.persist();
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
                    vs.set({ z: sign > 0 ? vs.depth - 1 : 0 });
                } else {
                    vs.step(sign * (event.shiftKey ? 10 : 1));
                }
            } else if ((event.code === "Period" || event.code === "Comma") && vs.frames > 1
                       && !event.altKey) {
                event.preventDefault();
                vs.stepTime(event.code === "Period" ? 1 : -1);
            }
        }

        /** Shift+wheel over the image scrubs z instead of zooming. */
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

    global.PlexoraDepthStrip = DepthStrip;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = DepthStrip;
    }
})(typeof window !== "undefined" ? window : globalThis);
