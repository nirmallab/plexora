/**
 * viewTransform.js - how the viewer turns and mirrors the image.
 *
 * ONE STATE, `{degrees, flipH, flipV}`, owned here and nowhere else. Core's
 * Rotate and Flip tools (views/viewTransformTools.js) are its only writers;
 * everything that draws or picks in screen space reads it through the helpers
 * at the bottom, never through OpenSeadragon's viewport directly.
 *
 * WHAT THE STATE MEANS. Turn the image clockwise by `degrees`, then mirror the
 * VIEW on the screen's own axes:
 *
 *     screen = Fh^flipH . Fv^flipV . R(degrees)
 *
 * "Flip horizontally" therefore always swaps left and right as the user sees
 * it, at any angle, and pressing a flip never changes the stored angle or the
 * other flip. The cost: while exactly one flip is on, a larger angle turns the
 * picture counter-clockwise on screen, because the mirror reverses it.
 *
 * HOW IT REACHES OPENSEADRAGON. Through the viewport (`setFlip`,
 * `setRotation`), never per tiled image, so every channel, the mask, every
 * registered layer and the Visium HD bins turn together for free. OSD 6.1's
 * canvas drawer mirrors its output context about the canvas centre first and
 * then rotates about the same centre (CanvasDrawer.draw -> _flip, then
 * _setRotations per item), so what it draws is
 *
 *     screen = F . R(osdDegrees)
 *
 * with ONE flip, horizontal. `osdStateFor` maps ours onto that, using
 * Fv = Fh . R(180) and Fh . Fv = R(180):
 *
 *     none -> (no flip, d)      H   -> (flip, d)
 *     V    -> (flip, d + 180)   H+V -> (no flip, d + 180)
 *
 * WHAT OSD DOES NOT DO FOR US, and so what the helpers are for:
 *   - `viewport.pointFromPixel` undoes rotation but not flip. OSD pre-mirrors
 *     positions only for its own `canvas-click` event, which nothing in
 *     Plexora uses. `pointFromPixel` below mirrors first.
 *   - Anything drawn on core's overlay canvas (canvas-overlay-hd.js), the
 *     transcript points and the mini-map lens are drawn by us, so they have
 *     to be oriented by us: `orientContext` is the drawer's own composition.
 *   - Home is not rotation-aware: `getHomeZoom` fills from the UNROTATED
 *     content aspect, so a 90-degree view of a wide image went home with side
 *     margins and a cropped height. The constructor wraps `viewport.goHome`.
 *
 * Saved to `/view_transform/<datasource>` (the per-datasource database),
 * debounced, so a slider drag is one request. `adopt()` applies a saved state
 * without saving it back, which is how boot uses it.
 *
 * Classic script, loaded from base.html before imageViewer.js, exposed as
 * `window.PlexoraViewTransform` so the webpacked viewerManager.js can reach
 * the helpers lazily without an import.
 */
(function (global) {
    "use strict";

    const SAVE_DELAY_MS = 400;
    const IDENTITY = Object.freeze({ degrees: 0, flipH: false, flipV: false });

    /** Positive modulo 360; -0 and 360 both read as 0. */
    function normalize(degrees) {
        const value = Number(degrees);
        if (!Number.isFinite(value)) return 0;
        const turned = ((value % 360) + 360) % 360;
        // Float noise at the seam: -1e-13 comes out as 359.9999999999999, a
        // full turn that would never compare equal to the 0 it means.
        return 360 - turned < 1e-9 || Object.is(turned, -0) ? 0 : turned;
    }

    function clean(state) {
        return {
            degrees: normalize(state?.degrees ?? 0),
            flipH: !!state?.flipH,
            flipV: !!state?.flipV,
        };
    }

    /** Our state as OSD's one horizontal flip plus a rotation. See the header. */
    function osdStateFor(state) {
        const { degrees, flipH, flipV } = clean(state);
        return {
            flipped: flipH !== flipV,
            degrees: normalize(flipV ? degrees + 180 : degrees),
        };
    }

    function isIdentity(state) {
        const { degrees, flipH, flipV } = clean(state);
        return degrees === 0 && !flipH && !flipV;
    }

    // ---- Screen-space helpers -------------------------------------------------
    //
    // Statics taking a viewer, so anything with an OSD viewer can use them --
    // the overlay host, the plugins' pickers, the webpacked viewerManager.js --
    // whether or not it can see the service instance.

    function containerSize(viewer) {
        const size = viewer.viewport.getContainerSize();
        return { width: size.x, height: size.y };
    }

    function point(x, y) {
        return global.OpenSeadragon ? new global.OpenSeadragon.Point(x, y) : { x, y };
    }

    /** A pixel in the viewer element -> viewport coordinates, flip included. */
    function pointFromPixel(viewer, pixel, current = true) {
        const viewport = viewer.viewport;
        let unflipped = pixel;
        if (viewport.getFlip?.()) {
            unflipped = point(containerSize(viewer).width - pixel.x, pixel.y);
        }
        return viewport.pointFromPixel(unflipped, current);
    }

    /** Viewport coordinates -> a pixel in the viewer element, flip included. */
    function pixelFromPoint(viewer, viewportPoint, current = true) {
        const viewport = viewer.viewport;
        const pixel = viewport.pixelFromPoint(viewportPoint, current);
        if (!viewport.getFlip?.()) return pixel;
        return point(containerSize(viewer).width - pixel.x, pixel.y);
    }

    /** An image-pixel point -> a pixel in the viewer element. */
    function imageToScreen(viewer, item, x, y) {
        return pixelFromPoint(viewer, item.imageToViewportCoordinates(x, y, true));
    }

    /** A pixel in the viewer element -> image pixels of `item`. */
    function screenToImage(viewer, item, pixel) {
        return item.viewportToImageCoordinates(pointFromPixel(viewer, pixel));
    }

    /**
     * Orient a 2-D context the way the drawer orients the tiles: mirror about
     * the vertical centre line when flipped, then rotate about the centre. In
     * CSS pixels -- a caller that scaled for devicePixelRatio does that first.
     * After this, draw at `viewport.pixelFromPointNoRotate(...)` positions.
     */
    function orientContext(context, viewer) {
        const viewport = viewer.viewport;
        const { width, height } = containerSize(viewer);
        if (viewport.getFlip?.()) {
            context.translate(width, 0);
            context.scale(-1, 1);
        }
        const degrees = viewport.getRotation(true) % 360;
        if (degrees !== 0) {
            const cx = width / 2;
            const cy = height / 2;
            context.translate(cx, cy);
            context.rotate(degrees * Math.PI / 180);
            context.translate(-cx, -cy);
        }
    }

    /**
     * The axis-aligned screen box of an image-space rectangle: the bounding box
     * of its four projected corners, which under a rotation is larger than the
     * rectangle itself. `{x, y, width, height}` in image pixels.
     */
    function screenBoxOfImageRect(viewer, item, rect) {
        const corners = [
            [rect.x, rect.y],
            [rect.x + rect.width, rect.y],
            [rect.x, rect.y + rect.height],
            [rect.x + rect.width, rect.y + rect.height],
        ].map(([x, y]) => imageToScreen(viewer, item, x, y));
        const xs = corners.map((p) => p.x);
        const ys = corners.map((p) => p.y);
        const left = Math.min(...xs);
        const top = Math.min(...ys);
        return { x: left, y: top, width: Math.max(...xs) - left, height: Math.max(...ys) - top };
    }

    /** The image-pixel bounding box of a screen rectangle. Same idea, inverted. */
    function imageBoxOfScreenRect(viewer, item, rect) {
        const corners = [
            [rect.x, rect.y],
            [rect.x + rect.width, rect.y],
            [rect.x, rect.y + rect.height],
            [rect.x + rect.width, rect.y + rect.height],
        ].map(([x, y]) => screenToImage(viewer, item, point(x, y)));
        const xs = corners.map((p) => p.x);
        const ys = corners.map((p) => p.y);
        const left = Math.min(...xs);
        const top = Math.min(...ys);
        return { x: left, y: top, width: Math.max(...xs) - left, height: Math.max(...ys) - top };
    }

    // ---- The service ----------------------------------------------------------

    class ViewTransform {
        constructor(viewer, { datasource = "", fetchImpl, saveDelay = SAVE_DELAY_MS } = {}) {
            this.viewer = viewer;
            this.datasource = datasource;
            this.state = { ...IDENTITY };
            this._subscribers = new Set();
            this._saveTimer = null;
            this._saveDelay = saveDelay;
            this._fetch = fetchImpl || ((...args) => global.fetch(...args));
            this._wrapGoHome();
        }

        get() {
            return { ...this.state };
        }

        /**
         * Change part of the state. `immediately: false` lets OSD's spring
         * animate the turn (the quick-select buttons); a slider drag passes
         * true. A flip always applies immediately -- mirroring is instant in
         * OSD, and animating the 180 degrees a vertical flip adds would spin
         * the picture half a turn behind a mirror that already happened.
         */
        set(patch, { immediately = true, save = true } = {}) {
            const next = clean({ ...this.state, ...patch });
            const prev = this.state;
            if (next.degrees === prev.degrees && next.flipH === prev.flipH
                && next.flipV === prev.flipV) {
                return this.get();
            }
            const flipChanged = next.flipH !== prev.flipH || next.flipV !== prev.flipV;
            this.state = next;
            this.apply({ immediately: immediately || flipChanged });
            this._notify();
            if (save) this._scheduleSave();
            return this.get();
        }

        reset() {
            return this.set({ degrees: 0 }, { immediately: false });
        }

        /** Apply a saved state without saving it back (boot). */
        adopt(saved) {
            return this.set(clean(saved), { immediately: true, save: false });
        }

        subscribe(fn) {
            this._subscribers.add(fn);
            return () => this._subscribers.delete(fn);
        }

        /** Push the state onto OSD. Safe on an empty world. */
        apply({ immediately = true } = {}) {
            const viewport = this.viewer?.viewport;
            if (!viewport) return;
            const { flipped, degrees } = osdStateFor(this.state);
            viewport.setFlip?.(flipped);
            // OSD's rotateTo animates the SHORT way round from wherever the
            // spring is now, so 350 -> 10 turns 20 degrees, not 340.
            viewport.setRotation?.(degrees, immediately);
        }

        /** Write any pending change now (page leaving, tests). */
        flush() {
            if (this._saveTimer === null) return Promise.resolve();
            clearTimeout(this._saveTimer);
            this._saveTimer = null;
            return this._save();
        }

        _notify() {
            const state = this.get();
            for (const fn of [...this._subscribers]) {
                try {
                    fn(state);
                } catch (error) {
                    console.error("viewTransform: subscriber failed", error);
                }
            }
            try {
                global.dispatchEvent?.(new CustomEvent("plexora:view-transform-changed",
                    { detail: state }));
            } catch (error) {
                // No CustomEvent in a bare test runtime; subscribers were told.
            }
        }

        _scheduleSave() {
            if (!this.datasource) return;
            if (this._saveTimer !== null) clearTimeout(this._saveTimer);
            this._saveTimer = setTimeout(() => {
                this._saveTimer = null;
                this._save();
            }, this._saveDelay);
        }

        _save() {
            const url = typeof global.plexoraUrl === "function"
                ? global.plexoraUrl(`view_transform/${encodeURIComponent(this.datasource)}`)
                : `/view_transform/${encodeURIComponent(this.datasource)}`;
            return Promise.resolve(this._fetch(url, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(this.state),
            })).catch((error) => {
                // Never fatal: the view is already turned; only the next
                // load will not remember it.
                console.error("viewTransform: could not save", error);
            });
        }

        /**
         * Home that knows about rotation. OSD's own fits the UNROTATED content
         * aspect; here the rotated bounding box is fitted instead. At right
         * angles `homeFillsViewer` still fills, as it does upright; at any
         * other angle the fit is contain, because "fill" of a tilted image
         * would crop a corner of it off whichever way it was chosen.
         */
        _wrapGoHome() {
            const viewport = this.viewer?.viewport;
            if (!viewport || typeof viewport.goHome !== "function" || viewport.__plexoraHomeWrapped) {
                return;
            }
            const original = viewport.goHome.bind(viewport);
            const viewer = this.viewer;
            viewport.goHome = function (immediately) {
                const degrees = normalize(viewport.getRotation());
                if (degrees === 0 || !viewer.world?.getItemCount?.()) {
                    return original(immediately);
                }
                viewer.raiseEvent?.("home", { immediately });
                const home = viewer.world.getHomeBounds();
                const radians = degrees * Math.PI / 180;
                const cos = Math.abs(Math.cos(radians));
                const sin = Math.abs(Math.sin(radians));
                const boxWidth = home.width * cos + home.height * sin;
                const boxHeight = home.width * sin + home.height * cos;
                const aspect = viewport.getAspectRatio();
                const rightAngle = degrees % 90 === 0;
                const wider = boxWidth / boxHeight >= aspect;
                const fill = viewport.homeFillsViewer && rightAngle;
                const viewWidth = (wider !== fill) ? boxWidth : boxHeight * aspect;
                viewport.panTo(home.getCenter(), immediately);
                viewport.zoomTo(1 / viewWidth, null, immediately);
                return viewport;
            };
            viewport.__plexoraHomeWrapped = true;
        }
    }

    ViewTransform.IDENTITY = IDENTITY;
    ViewTransform.normalize = normalize;
    ViewTransform.osdStateFor = osdStateFor;
    ViewTransform.isIdentity = isIdentity;
    ViewTransform.pointFromPixel = pointFromPixel;
    ViewTransform.pixelFromPoint = pixelFromPoint;
    ViewTransform.imageToScreen = imageToScreen;
    ViewTransform.screenToImage = screenToImage;
    ViewTransform.orientContext = orientContext;
    ViewTransform.screenBoxOfImageRect = screenBoxOfImageRect;
    ViewTransform.imageBoxOfScreenRect = imageBoxOfScreenRect;

    global.PlexoraViewTransform = ViewTransform;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = ViewTransform;
    }
})(typeof window !== "undefined" ? window : globalThis);
