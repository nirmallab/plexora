/**
 * areaTool.js - the Area tool: a square on the image that opens as 3-D.
 *
 * On a z-stack, from a plane or a projection: arm the tool (the depth
 * strip's Area button), and a dashed square follows the pointer over the
 * image. A click -- or V with the pointer there -- PLACES it, as a way into
 * the Crop tool: the view goes 3-D over the whole stack, cropped to the
 * square in x and y (from a fixed projection, to its planes in z too), with
 * the Crop panel out. Nothing is fetched for it: the crop is the shader's
 * (services/inspectState.js), so its grips, O (whole tissue / the crop) and
 * the rest of the Crop tool work on it at once, and a finer detail of the
 * part kept comes in once the camera rests (views/volumeRenderer.js).
 * The 3-D view opens at the plane's magnification, centred on the square
 * (VolumeRenderer.frameRegion), and turns about the crop's middle.
 *
 *     the square    `area` of the viewer prefs (services/viewerPrefs.js):
 *                   its side ON SCREEN. Zoom in and it takes less tissue,
 *                   out and it takes more; the wheel zooms and a drag pans
 *                   while it is armed, the square keeping its size, and a
 *                   readout under it says how much tissue it takes (microns
 *                   where the image states its spacing, else pixels)
 *
 * The mode buttons always mean the whole stack (services/viewControls.js
 * `transition`: the crop is hidden, kept for O), and Back -- B -- returns to
 * the view the square was placed from (services/viewHistory.js), where the
 * square is drawn again, solid, while the tool stays armed.
 *
 * The tool stays armed after placing, so V can go back and forth; Esc, or
 * the button again, disarms it and lets the placed square go. It never takes
 * a click it did not arm for: with the tool off, the image's own gestures
 * are untouched.
 *
 * Classic script, `window.PlexoraAreaTool`.
 */
(function (global) {
    "use strict";

    const OVERLAY_ID = "area-tool";
    const DASH = [8, 6];

    function G() {
        return global.PlexoraInspectGeometry || null;
    }

    function typing() {
        if (global.PlexoraShortcuts?.isTyping) return global.PlexoraShortcuts.isTyping();
        const active = document.activeElement;
        if (!active) return false;
        const tag = active.tagName;
        return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || active.isContentEditable;
    }

    function announce(armed) {
        try {
            global.dispatchEvent?.(new CustomEvent("plexora:area-tool", { detail: { armed } }));
        } catch (error) {
            // No CustomEvent in a bare runtime.
        }
    }

    class AreaTool {
        constructor({ viewState, imageViewer = null, prefs = null, volume = null } = {}) {
            this.viewState = viewState;
            this.imageViewer = imageViewer;
            //: The 3-D view's controller (views/volumeRenderer.js attach), to
            //: open at the plane's magnification.
            this.volume = volume;
            //: The square last placed, `{x, y, width, height}` image pixels:
            //: drawn on the plane while the tool is armed (after Back).
            this.placed = null;
            this.prefs = prefs || global.PlexoraViewerPrefs || null;
            this._armed = false;
            //: The image point under the pointer while armed on the plane,
            //: `{x, y}` full-resolution pixels, or null.
            this.hover = null;
            //: Where the pointer is on the canvas (element pixels), so a
            //: zoom or a pan under a still pointer moves the square with it.
            this.pointer = null;
            this.overlay = null;
            this._off = [];
        }

        get viewer() {
            return this.imageViewer?.viewer || null;
        }

        attach() {
            const viewer = this.viewer;
            if (this.imageViewer?.addOverlay) {
                this.overlay = this.imageViewer.addOverlay({
                    id: OVERLAY_ID, kind: "shapes",
                    draw: ({ context, px }) => this.draw(context, px),
                });
            }
            if (viewer && global.OpenSeadragon?.MouseTracker) {
                // A hover is an observation, not a gesture: OSD's canvas-*
                // events do not report a plain move.
                this._tracker = new global.OpenSeadragon.MouseTracker({
                    element: viewer.canvas,
                    moveHandler: (event) => this.moved(event.position),
                    leaveHandler: () => this.left(),
                });
                this._off.push(() => this._tracker?.destroy?.());
                const click = (event) => {
                    if (!this._armed || !event.quick || !this.onPlane()) return;
                    const point = this.imagePoint(event.position);
                    if (!point) return;
                    // OSD's own click zooms; a click to place is not that.
                    event.preventDefaultAction = true;
                    this.place(point);
                };
                viewer.addHandler("canvas-click", click);
                this._off.push(() => viewer.removeHandler("canvas-click", click));
            }
            if (typeof this.imageViewer?.onViewportChange === "function") {
                this._off.push(this.imageViewer.onViewportChange(() => this.zoomed()));
            }
            const onKey = (event) => {
                if (event.key !== "Escape" || !this._armed || typing()) return;
                if (document.querySelector("dialog[open]") || document.querySelector(".plx-popover")) return;
                // In 3-D, Esc is the view's own (a lock, a turn).
                if (!this.onPlane()) return;
                event.preventDefault();
                this.disarm();
            };
            document.addEventListener("keydown", onKey);
            this._off.push(() => document.removeEventListener("keydown", onKey));
            this._off.push(this.viewState.subscribe(() => this.repaint()));
            return this;
        }

        destroy() {
            for (const off of this._off.splice(0)) off?.();
            if (this.overlay) this.imageViewer?.removeOverlay?.(OVERLAY_ID);
            this.overlay = null;
        }

        // -- arming --------------------------------------------------------------

        armed() {
            return this._armed;
        }

        arm() {
            if (this._armed) return;
            this._armed = true;
            this.cursor();
            announce(true);
            this.repaint();
        }

        disarm() {
            if (!this._armed) return;
            this._armed = false;
            this.hover = null;
            this.placed = null;
            this.cursor();
            announce(false);
            this.repaint();
        }

        toggle() {
            if (this._armed) this.disarm();
            else this.arm();
            return this._armed;
        }

        cursor() {
            const canvas = this.viewer?.canvas;
            if (canvas?.style) canvas.style.cursor = this._armed ? "crosshair" : "";
        }

        // -- geometry ----------------------------------------------------------------

        onPlane() {
            const mode = this.viewState.get()?.mode;
            return mode === "plane" || mode === "slab";
        }

        dims() {
            const g = G();
            const vs = this.viewState;
            return g ? g.dims(vs.info, vs.depth)
                : { width: Number(vs.info?.width) || 1, height: Number(vs.info?.height) || 1,
                    depth: vs.depth || 1, area: null };
        }

        /** The square's side in full-resolution pixels: the prefs' side on
         *  screen over how many screen pixels an image pixel is drawn at now
         *  (viewerScene.scaleOf), so it covers the same part of the screen
         *  at every zoom. */
        sizePx() {
            const pref = this.prefs?.area?.() || { screen: 320 };
            const screen = Number(pref.screen) > 0 ? Number(pref.screen) : 320;
            const scale = global.PlexoraViewerScene?.scaleOf?.(this.imageViewer);
            return Math.max(1, Math.round(screen / (scale > 0 ? scale : 1)));
        }

        /** How much tissue the square takes now, in words: "150 µm", or
         *  "300 px" where the image states no spacing. */
        sizeLabel(side = this.sizePx()) {
            const g = G();
            const space = g?.spacing?.(this.viewState.info) || null;
            return g?.label ? g.label("x", side, space) : `${Math.round(side)} px`;
        }

        /** The square centred on image point (`x`, `y`), inside the image. */
        squareAt(x, y) {
            const g = G();
            if (!g) return null;
            return g.squareAt(x, y, this.sizePx(), this.dims());
        }

        /** Element pixels -> full-resolution image pixels, or null. */
        imagePoint(position) {
            const viewer = this.viewer;
            const item = viewer?.world?.getItemAt?.(0);
            if (!item || !position) return null;
            const transform = global.PlexoraViewTransform?.screenToImage;
            const p = transform ? transform(viewer, item, position)
                : item.viewportToImageCoordinates(viewer.viewport.pointFromPixel(position));
            return p && Number.isFinite(p.x) && Number.isFinite(p.y) ? { x: p.x, y: p.y } : null;
        }

        // -- the pointer -------------------------------------------------------------

        moved(position) {
            if (!this._armed || !this.onPlane()) return;
            this.pointer = position ? { x: position.x, y: position.y } : null;
            this.hover = this.imagePoint(position);
            this.repaint();
        }

        left() {
            this.pointer = null;
            if (!this.hover) return;
            this.hover = null;
            this.repaint();
        }

        /** The view zoomed or panned under a still pointer: the square is
         *  where the pointer is now, at its size for the new zoom. */
        zoomed() {
            if (!this._armed || !this.pointer || !this.onPlane()) return;
            // OpenSeadragon's own point: its conversions call its methods.
            const Point = global.OpenSeadragon?.Point;
            const at = Point ? new Point(this.pointer.x, this.pointer.y) : this.pointer;
            this.hover = this.imagePoint(at);
            this.repaint();
        }

        /** The Crop tool's cut for `square`: x and y the square's, z the
         *  fixed projection's planes, else every plane. */
        cropFor(square) {
            const vs = this.viewState;
            const band = typeof vs._bandCrop === "function" ? vs._bandCrop() : null;
            const inspect = {
                mode: "crop", axes: { x: true, y: true, z: Boolean(band) },
                crop: { x: [square.x, square.x + square.width], y: [square.y, square.y + square.height] },
            };
            if (band) inspect.crop.z = band.crop.z.slice();
            return inspect;
        }

        /**
         * Place the square at `point` (`{x, y}`, image pixels; the hover by
         * default): 3-D over the whole stack, cropped to it, in one change
         * of the view state (one undo entry with the cut, one step of Back),
         * at the plane's magnification, the Crop panel out.
         */
        place(point = this.hover) {
            if (!point) return null;
            const square = this.squareAt(point.x, point.y);
            if (!square) return null;
            const vs = this.viewState;
            // Read before the plane goes: what an image pixel is on screen now.
            const perPixel = global.PlexoraViewerScene?.scaleOf?.(this.imageViewer) || 0;
            // Where the user was, for Back (services/viewHistory.js).
            global.PlexoraViewHistory?.push?.();
            this.placed = { ...square };
            this.volume?.frameNext?.({
                box: [square.x, square.y, square.x + square.width, square.y + square.height],
                perPixel, height: this.viewer?.canvas?.clientHeight || 0,
            });
            const inspect = this.cropFor(square);
            const extra = { mode: "volume", volume: { area: null } };
            const cutter = global.PlexoraInspect;
            const out = cutter?.viewState === vs && typeof cutter.set === "function"
                ? cutter.set({ inspect }, { extra })
                : vs.set({ ...extra, inspect });
            global.PlexoraVolumeDock?.open?.();
            this.hover = null;
            this.repaint();
            return out;
        }

        // -- drawing -----------------------------------------------------------------

        repaint() {
            this.overlay?.invalidate?.();
        }

        draw(context, px = 1) {
            if (!context || !this.onPlane()) return;
            context.save();
            // The square last placed (Back came here from it): solid, quiet.
            const placed = this._armed ? this.placed : null;
            if (placed) {
                context.setLineDash([]);
                context.lineWidth = 3 * px;
                context.strokeStyle = "rgba(0, 0, 0, 0.45)";
                context.strokeRect(placed.x, placed.y, placed.width, placed.height);
                context.lineWidth = 1.5 * px;
                context.strokeStyle = "rgba(255, 255, 255, 0.85)";
                context.strokeRect(placed.x, placed.y, placed.width, placed.height);
            }
            const square = this._armed && this.hover ? this.squareAt(this.hover.x, this.hover.y) : null;
            if (square) {
                context.setLineDash(DASH.map((v) => v * px));
                context.lineWidth = 2 * px;
                context.strokeStyle = "rgba(0, 0, 0, 0.55)";
                context.strokeRect(square.x, square.y, square.width, square.height);
                context.lineDashOffset = DASH[0] * px;
                context.strokeStyle = "rgba(255, 255, 255, 0.95)";
                context.strokeRect(square.x, square.y, square.width, square.height);
                // How much tissue it takes, under its middle.
                if (typeof context.fillText === "function") {
                    context.setLineDash([]);
                    context.font = `${12 * px}px system-ui, sans-serif`;
                    context.textAlign = "center";
                    context.textBaseline = "top";
                    const text = this.sizeLabel(Math.max(square.width, square.height));
                    const x = square.x + square.width / 2;
                    const y = square.y + square.height + 6 * px;
                    context.lineWidth = 3 * px;
                    context.strokeStyle = "rgba(0, 0, 0, 0.6)";
                    context.strokeText(text, x, y);
                    context.fillStyle = "rgba(255, 255, 255, 0.95)";
                    context.fillText(text, x, y);
                }
            }
            context.restore();
        }
    }

    function attach(options = {}) {
        const tool = new AreaTool(options);
        if (!options.viewState?.enabled?.()) return null;
        tool.attach();
        api.instance = tool;
        return tool;
    }

    const api = {
        AreaTool,
        attach,
        instance: null,
        armed: () => Boolean(api.instance?.armed()),
        arm: () => api.instance?.arm(),
        disarm: () => api.instance?.disarm(),
        toggle: () => Boolean(api.instance?.toggle()),
        /** The image point under the pointer while armed on the plane, or null. */
        hover: () => api.instance?.hover || null,
        /** The square last placed, while the tool is armed, or null. */
        placed: () => (api.instance?.armed() ? api.instance.placed : null),
        place: (point) => api.instance?.place(point) || null,
        sizePx: () => api.instance?.sizePx() || null,
    };
    global.PlexoraAreaTool = api;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    }
})(typeof window !== "undefined" ? window : globalThis);
