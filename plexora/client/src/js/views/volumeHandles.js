/**
 * volumeHandles.js - the 3-D view's cuts, outlined in the view and dragged there.
 *
 * Over the 3-D canvas, an SVG draws each face the cut puts in the stack (the
 * view state's `inspect`, services/inspectGeometry.js) as a thin outline in
 * its axis's colour -- the gizmo's -- with a GRIP at its centre:
 *
 *     crop      both faces along each axis cut, and a grip in the box's
 *               middle that moves the box whole
 *     cutaway   the cut along each axis
 *     section   each plane
 *     slab      each slab's middle plane (its two faces faint)
 *
 * DRAGGING A GRIP NEVER TURNS THE STACK. A press on a grip is taken before
 * the canvas sees it (stopped and captured, as the gizmo's are), so the
 * camera stays exactly where it was. The pointer's travel is measured along
 * the axis as the screen shows it at the grip -- one image unit along the
 * axis, projected through the camera at the grip's depth -- so a face under
 * the hand stays under the hand at any angle and any distance. While a grip
 * is dragged the cut is PREVIEWED (services/inspectState.js: uniforms in the
 * next frame, nothing saved) and committed once on release; Esc puts it back.
 * A grip double-clicked puts its axis back. Focused (Tab), the arrow keys
 * move it a voxel, Shift ten.
 *
 * Shown in the cut's modes while the viewer prefs say so (`handles`:
 * visible, opacity; views/volumeDock.js toggles them). Redrawn after every
 * frame the renderer draws (`addDrawListener`).
 *
 * Classic script, `window.PlexoraVolumeHandles`.
 */
(function (global) {
    "use strict";

    const SVG = "http://www.w3.org/2000/svg";
    const AXES = ["x", "y", "z"];
    const GRIP_R = 7;

    function svg(tag, attrs = {}) {
        const node = document.createElementNS(SVG, tag);
        for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
        return node;
    }

    function clamp(value, low, high) {
        return Math.max(low, Math.min(high, value));
    }

    class VolumeHandles {
        constructor({ viewState, volume = null, cutter = null } = {}) {
            this.viewState = viewState;
            this.volume = volume;
            this.cutter = cutter;
            this.G = global.PlexoraInspectGeometry;
            this.root = null;
            this.drag = null;
            //: grip key -> {group, disc, dot}; face key -> path. Made once,
            //: shown and placed per frame.
            this.grips = new Map();
            this.faces = new Map();
            this._off = [];
        }

        get renderer() {
            return this.volume?.renderer || null;
        }

        mount() {
            if (this.root || !this.cutter || !this.G) return false;
            const wrapper = document.getElementById("openseadragon_wrapper");
            if (!wrapper) return false;
            const root = svg("svg", { class: "volume-handles", "aria-label": "Cut handles", role: "group" });
            root.setAttribute("hidden", "");
            this.faceLayer = svg("g", { class: "handle-faces" });
            this.gripLayer = svg("g", { class: "handle-grips" });
            root.append(this.faceLayer, this.gripLayer);
            wrapper.appendChild(root);
            this.root = root;
            const repaint = () => this.paint();
            const hook = () => {
                const renderer = this.renderer;
                if (!renderer || renderer._handlesHooked) return;
                renderer._handlesHooked = true;
                renderer.handles = this;
                renderer.addDrawListener?.(repaint);
            };
            hook();
            for (const name of ["plexora:volume-mounted", "plexora:inspect-preview", "plexora:inspect-changed",
                                "plexora:viewer-prefs-changed", "plexora:canvas-layout"]) {
                const fn = name === "plexora:volume-mounted" ? () => { hook(); repaint(); } : repaint;
                global.addEventListener(name, fn);
                this._off.push(() => global.removeEventListener(name, fn));
            }
            this._off.push(this.viewState.subscribe(repaint));
            const onKey = (event) => {
                if (event.key === "Escape" && this.drag) {
                    event.preventDefault();
                    event.stopPropagation();
                    this.cancelDrag();
                }
            };
            document.addEventListener("keydown", onKey, true);
            this._off.push(() => document.removeEventListener("keydown", onKey, true));
            return true;
        }

        destroy() {
            for (const off of this._off.splice(0)) off?.();
            this.root?.remove();
            this.root = null;
        }

        prefs() {
            return global.PlexoraViewerPrefs?.handles?.() || { visible: true, opacity: 0.55 };
        }

        dims() {
            return this.G.dims(this.viewState.info, this.viewState.depth);
        }

        /** The state drawn now: a preview under a hand included. */
        state() {
            return this.cutter.previewing ? this.cutter.effective() : this.viewState.get();
        }

        // -- geometry ---------------------------------------------------------

        /** `[x0, y0, x1, y1]` full-resolution pixels the textures cover. */
        covered() {
            const d = this.dims();
            return this.renderer?.covered || [0, 0, d.width, d.height];
        }

        /** An image point (x, y pixels, z planes, continuous) on screen,
         *  `{x, y, w}` CSS pixels, or null behind the camera. */
        project(point, camera, extent, canvas) {
            const c = this.covered();
            const D = Math.max(1, this.viewState.depth);
            const t = [(point[0] - c[0]) / Math.max(1e-9, c[2] - c[0]),
                       (point[1] - c[1]) / Math.max(1e-9, c[3] - c[1]),
                       point[2] / D];
            const x = (t[0] - 0.5) * extent[0];
            const y = (t[1] - 0.5) * extent[1];
            const z = (t[2] - 0.5) * extent[2];
            const m = camera.forward;
            const w = m[3] * x + m[7] * y + m[11] * z + m[15];
            if (!(w > 1e-6)) return null;
            const nx = (m[0] * x + m[4] * y + m[8] * z + m[12]) / w;
            const ny = (m[1] * x + m[5] * y + m[9] * z + m[13]) / w;
            return { x: (nx * 0.5 + 0.5) * canvas.clientWidth, y: (0.5 - ny * 0.5) * canvas.clientHeight, w };
        }

        /**
         * What to draw: faces `{key, axis, at, span, faint}` (span: the
         * ranges of the other two axes) and grips `{key, axis, kind, at}` in
         * continuous image coordinates. `kind`: "lo" / "hi" (a crop's face),
         * "cut" (a cutaway's), "plane" (a section's or slab's), "body".
         */
        layout(state) {
            const inspect = state.inspect;
            const d = this.dims();
            const resolved = this.G.resolve(inspect, this.G.positions(state, d), d);
            const box = resolved.box;
            const faces = [];
            const grips = [];
            const other = (axis, ranges) => AXES.filter((a) => a !== axis).map((a) => [a, ranges[a]]);
            if (resolved.mode === "crop") {
                for (const axis of resolved.axes) {
                    for (const [kind, at] of [["lo", box[axis][0]], ["hi", box[axis][1]]]) {
                        faces.push({ key: `${axis}:${kind}`, axis, at, span: other(axis, box) });
                        grips.push({ key: `${axis}:${kind}`, axis, kind, at, span: other(axis, box) });
                    }
                }
                if (resolved.axes.length) {
                    grips.push({ key: "body", axis: null, kind: "body",
                                 at: null, centre: AXES.map((a) => (box[a][0] + box[a][1]) / 2) });
                }
            } else if (resolved.mode === "cutaway") {
                const region = resolved.hole || null;
                for (const axis of resolved.axes) {
                    const { at, keep } = inspect.cutaway[axis];
                    // Along z the cut is between planes (inspectGeometry).
                    const where = axis === "z" ? (keep === "hi" ? at : at + 1) : at;
                    const span = other(axis, region || box);
                    faces.push({ key: `${axis}:cut`, axis, at: where, span });
                    grips.push({ key: `${axis}:cut`, axis, kind: "cut", at: where, span });
                }
            } else if (resolved.mode === "section" || resolved.mode === "slab") {
                for (const plane of resolved.planes) {
                    const span = other(plane.axis, box);
                    faces.push({ key: `${plane.axis}:plane`, axis: plane.axis, at: plane.at, span });
                    if (plane.hi > plane.lo) {
                        faces.push({ key: `${plane.axis}:lo`, axis: plane.axis, at: plane.lo, span, faint: true });
                        faces.push({ key: `${plane.axis}:hi`, axis: plane.axis, at: plane.hi, span, faint: true });
                    }
                    grips.push({ key: `${plane.axis}:plane`, axis: plane.axis, kind: "plane", at: plane.at, span });
                }
            }
            return { faces, grips };
        }

        /** A face's four corners, an image point each. */
        corners(face) {
            const [[a, ra], [b, rb]] = face.span;
            const point = (va, vb) => {
                const p = [0, 0, 0];
                p[AXES.indexOf(face.axis)] = face.at;
                p[AXES.indexOf(a)] = va;
                p[AXES.indexOf(b)] = vb;
                return p;
            };
            return [point(ra[0], rb[0]), point(ra[1], rb[0]), point(ra[1], rb[1]), point(ra[0], rb[1])];
        }

        centre(grip) {
            if (grip.centre) return grip.centre;
            const [[a, ra], [b, rb]] = grip.span;
            const p = [0, 0, 0];
            p[AXES.indexOf(grip.axis)] = grip.at;
            p[AXES.indexOf(a)] = (ra[0] + ra[1]) / 2;
            p[AXES.indexOf(b)] = (rb[0] + rb[1]) / 2;
            return p;
        }

        // -- painting ------------------------------------------------------

        visible(state) {
            const renderer = this.renderer;
            return Boolean(renderer?.canvas && !renderer.canvas.hidden && state?.mode === "volume"
                && state.inspect && state.inspect.mode !== "full" && this.prefs().visible !== false);
        }

        paint() {
            if (!this.root) return;
            const state = this.state();
            if (!this.visible(state)) {
                this.root.setAttribute("hidden", "");
                return;
            }
            const renderer = this.renderer;
            const canvas = renderer.canvas;
            if (!canvas.clientWidth || !canvas.clientHeight || !canvas.height) return;
            const camera = renderer.cameraMatrices();
            const extent = renderer.extent();
            const { faces, grips } = this.layout(state);
            this.root.removeAttribute("hidden");
            const opacity = this.prefs().opacity ?? 0.55;
            this.faceLayer.setAttribute("opacity", String(opacity));
            this.gripLayer.setAttribute("opacity", String(Math.min(1, opacity + 0.35)));
            const seen = new Set();
            for (const face of faces) {
                const points = this.corners(face).map((p) => this.project(p, camera, extent, canvas));
                let path = this.faces.get(face.key);
                if (!path) {
                    path = svg("path", { class: "handle-face" });
                    this.faceLayer.appendChild(path);
                    this.faces.set(face.key, path);
                }
                seen.add(face.key);
                path.setAttribute("class", `handle-face is-${face.axis}${face.faint ? " is-faint" : ""}`);
                if (points.some((p) => !p)) {
                    path.setAttribute("d", "");
                    continue;
                }
                path.setAttribute("d", `M${points.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join("L")}Z`);
            }
            for (const [key, path] of this.faces) if (!seen.has(key)) path.setAttribute("d", "");
            const kept = new Set();
            for (const grip of grips) {
                const at = this.project(this.centre(grip), camera, extent, canvas);
                const node = this.gripNode(grip);
                kept.add(grip.key);
                node.group.removeAttribute("display");
                node.grip = grip;
                if (!at) {
                    node.at = null;
                    node.group.setAttribute("display", "none");
                    continue;
                }
                node.at = at;
                node.group.setAttribute("transform", `translate(${at.x.toFixed(1)} ${at.y.toFixed(1)})`);
            }
            for (const [key, node] of this.grips) {
                if (!kept.has(key)) node.group.setAttribute("display", "none");
            }
            // A crop's body grip gives way to a face's when they meet on
            // screen (looking straight along an axis puts both its faces'
            // grips at the box's middle): the faces are what a hand reaches for.
            const body = this.grips.get("body");
            if (body && kept.has("body") && body.at) {
                const crowded = [...this.grips.entries()].some(([key, node]) => key !== "body" && kept.has(key)
                    && node.at && Math.hypot(node.at.x - body.at.x, node.at.y - body.at.y) < GRIP_R * 2.5);
                if (crowded) body.group.setAttribute("display", "none");
            }
        }

        /** The grip for `grip.key`, made the first time it is drawn. */
        gripNode(grip) {
            let node = this.grips.get(grip.key);
            if (node) return node;
            const group = svg("g", { class: `handle-grip ${grip.axis ? `is-${grip.axis}` : "is-body"}`,
                                     tabindex: "0", role: "slider",
                                     "aria-label": this.gripLabel(grip) });
            const disc = svg("circle", { class: "grip-disc", r: GRIP_R });
            const dot = svg("circle", { class: "grip-dot", r: grip.kind === "body" ? 2 : 2.5 });
            group.append(disc, dot);
            this.gripLayer.appendChild(group);
            node = { group, disc, dot, grip };
            this.grips.set(grip.key, node);
            group.addEventListener("pointerdown", (event) => this.startDrag(event, node));
            // The canvas's own gestures -- a double-click to the three-quarter
            // view, the wheel's zoom -- are not the grip's.
            group.addEventListener("dblclick", (event) => {
                event.preventDefault();
                event.stopPropagation();
                if (node.grip.axis) this.cutter.reset("axis", { axis: node.grip.axis });
                else this.cutter.reset("mode");
            });
            group.addEventListener("wheel", (event) => event.stopPropagation(), { passive: true });
            group.addEventListener("keydown", (event) => this.onGripKey(event, node));
            return node;
        }

        gripLabel(grip) {
            if (grip.kind === "body") return "Move the crop";
            const name = grip.axis.toUpperCase();
            return { lo: `${name} crop, near face`, hi: `${name} crop, far face`, cut: `${name} cut`,
                     plane: `${name} section` }[grip.kind] || name;
        }

        // -- dragging ------------------------------------------------------------

        /**
         * The screen's measure of one image unit along `axis` at image point
         * `point`: `{x, y}` CSS pixels. Measured by projecting the point and
         * the point one unit further, so perspective is in it.
         */
        screenStep(axis, point) {
            const renderer = this.renderer;
            const camera = renderer.cameraMatrices();
            const extent = renderer.extent();
            const canvas = renderer.canvas;
            const i = AXES.indexOf(axis);
            const d = this.dims();
            const span = Math.max(1, [d.width, d.height, d.depth][i]) / 50;
            const ahead = point.slice();
            ahead[i] += span;
            const a = this.project(point, camera, extent, canvas);
            const b = this.project(ahead, camera, extent, canvas);
            if (!a || !b) return null;
            return { x: (b.x - a.x) / span, y: (b.y - a.y) / span };
        }

        startDrag(event, node) {
            if (event.button !== 0) return;
            // Taken before the canvas: a grip is never a turn of the stack.
            event.preventDefault();
            event.stopPropagation();
            node.group.setPointerCapture?.(event.pointerId);
            const renderer = this.renderer;
            renderer?.stopMotion?.();
            const grip = node.grip;
            const state = this.viewState.get();
            const centre = this.centre(grip);
            const axes = grip.kind === "body"
                ? this.G.active(state.inspect).map((axis) => ({ axis, step: this.screenStep(axis, centre) }))
                    .filter((entry) => entry.step && Math.hypot(entry.step.x, entry.step.y) > 1e-4)
                    .sort((a, b) => Math.hypot(b.step.x, b.step.y) - Math.hypot(a.step.x, a.step.y)).slice(0, 2)
                : [{ axis: grip.axis, step: this.screenStep(grip.axis, centre) }];
            if (!axes.length || axes.some((entry) => !entry.step)) return;
            this.drag = { node, grip, x: event.clientX, y: event.clientY, axes, start: state,
                          pointerId: event.pointerId };
            node.group.classList.add("is-dragging");
            if (renderer) renderer.interacting = true;
            const move = (e) => this.moveDrag(e);
            const up = (e) => this.endDrag(e, move, up);
            node.group.addEventListener("pointermove", move);
            node.group.addEventListener("pointerup", up);
            node.group.addEventListener("pointercancel", up);
            this.drag.release = () => {
                node.group.removeEventListener("pointermove", move);
                node.group.removeEventListener("pointerup", up);
                node.group.removeEventListener("pointercancel", up);
            };
        }

        /** Image units moved along each axis for a pointer travel `(dx, dy)`:
         *  along one axis its projection; along two (a crop's body) the pair
         *  whose screen steps add up to the travel. */
        travel(axes, dx, dy) {
            if (axes.length === 1) {
                const { step } = axes[0];
                const length = step.x * step.x + step.y * step.y;
                return { [axes[0].axis]: length > 1e-9 ? (dx * step.x + dy * step.y) / length : 0 };
            }
            const [a, b] = axes;
            const det = a.step.x * b.step.y - a.step.y * b.step.x;
            if (Math.abs(det) < 1e-6) return this.travel([a], dx, dy);
            return { [a.axis]: (dx * b.step.y - dy * b.step.x) / det,
                     [b.axis]: (a.step.x * dy - a.step.y * dx) / det };
        }

        moveDrag(event) {
            const drag = this.drag;
            if (!drag) return;
            event.stopPropagation();
            const moved = this.travel(drag.axes, event.clientX - drag.x, event.clientY - drag.y);
            const patch = this.patchFor(drag.grip, drag.start, moved);
            if (patch) this.cutter.preview(patch);
        }

        /** The cut moved by `moved` image units per axis from `state`. */
        patchFor(grip, state, moved) {
            const inspect = state.inspect;
            const d = this.dims();
            const bounds = (axis) => {
                if (axis === "z") return [0, d.depth - 1];
                const c = this.covered();
                const i = axis === "x" ? 0 : 1;
                return [Math.max(0, c[i]), Math.min(axis === "x" ? d.width : d.height, c[i + 2])];
            };
            const delta = (axis) => Math.round(moved[axis] || 0);
            if (grip.kind === "body") {
                const crop = {};
                for (const axis of Object.keys(moved)) {
                    const [lo, hi] = inspect.crop[axis];
                    const [min, max] = bounds(axis);
                    const width = hi - lo;
                    const start = clamp(lo + delta(axis), min, max - width);
                    crop[axis] = [start, start + width];
                }
                return { inspect: { crop } };
            }
            const axis = grip.axis;
            const [min, max] = bounds(axis);
            if (grip.kind === "lo" || grip.kind === "hi") {
                let [lo, hi] = inspect.crop[axis];
                const gap = axis === "z" ? 0 : 1;
                if (grip.kind === "lo") lo = clamp(lo + delta(axis), min, hi - gap);
                else hi = clamp(hi + delta(axis), lo + gap, max);
                return { inspect: { crop: { [axis]: [lo, hi] } } };
            }
            if (grip.kind === "cut") {
                const at = clamp(inspect.cutaway[axis].at + delta(axis), min, max);
                return { inspect: { cutaway: { [axis]: { at } } } };
            }
            if (axis === "z") return { z: clamp(state.z + delta("z"), 0, d.depth - 1) };
            const top = Math.max(min, max - 1);
            return { section: { [axis]: clamp(Math.round(state.section?.[axis] ?? 0) + delta(axis), min, top) } };
        }

        endDrag(event, move, up) {
            const drag = this.drag;
            if (!drag) return;
            event?.stopPropagation?.();
            this.finish();
            if (event?.type === "pointercancel") this.cutter.cancel();
            else this.cutter.commit();
        }

        cancelDrag() {
            if (!this.drag) return;
            this.finish();
            this.cutter.cancel();
        }

        finish() {
            const drag = this.drag;
            this.drag = null;
            drag.release?.();
            drag.node.group.classList.remove("is-dragging");
            try {
                drag.node.group.releasePointerCapture?.(drag.pointerId);
            } catch (error) {
                // Already released.
            }
            const renderer = this.renderer;
            if (renderer) {
                renderer.interacting = false;
                renderer.invalidate();
            }
        }

        /** Arrows move a focused grip a voxel along its axis (Shift ten). */
        onGripKey(event, node) {
            const grip = node.grip;
            const sign = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1 }[event.key];
            if (!sign) return;
            event.preventDefault();
            event.stopPropagation();
            const step = sign * (event.shiftKey ? 10 : 1);
            const state = this.viewState.get();
            const moved = grip.kind === "body"
                ? Object.fromEntries(this.G.active(state.inspect).slice(0, 1).map((axis) => [axis, step]))
                : { [grip.axis]: step };
            const patch = this.patchFor(grip, state, moved);
            if (patch) this.cutter.set(patch, { gesture: `key:${grip.key}` });
        }
    }

    function attach(options = {}) {
        const handles = new VolumeHandles(options);
        if (!handles.mount()) return null;
        api.instance = handles;
        return handles;
    }

    const api = { VolumeHandles, attach, instance: null, get root() { return api.instance?.root || null; } };
    global.PlexoraVolumeHandles = api;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    }
})(typeof window !== "undefined" ? window : globalThis);
