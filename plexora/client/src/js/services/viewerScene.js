/**
 * viewerScene.js -- where the viewer is looking, in FULL-RESOLUTION IMAGE
 * PIXELS, and how to put it somewhere else.
 *
 * Two callers needed the same answer and had one copy between them. Figure
 * Builder records a captured panel's region and puts it back later
 * (figureSceneSnapshot.js), and the agent bridge (agentBridge.js) reports the
 * viewport to an external agent and moves it on request. Both speak image
 * pixels of the full-resolution image, never OpenSeadragon's viewport units and
 * never screen pixels: an image-pixel region is stable under zoom, under HD mode,
 * under a different monitor and across a reload, and nothing else is. So the
 * conversions live here, once, and Figure Builder's FigureSchema/FigureScene
 * keep one-line delegates so their callers and their probes are unchanged.
 *
 * Three facts every function below has to respect, and each one was learnt
 * the hard way somewhere else in the tree:
 *
 *   **The reference item, not item 0.** A registered layer can sit at world
 *   index 0 with its own affine, and converting through it lands on the wrong
 *   slide by however far the two are apart. `ImageViewer.referenceItem()` is
 *   the item every screen <-> image conversion goes through; item 0 is only the
 *   fallback for a viewer that has no stack to ask (a test harness, a flat RGB
 *   quick view).
 *
 *   **`extraZoomLevels`.** The tile source may be served 2^n larger than the
 *   image, so OpenSeadragon's "image" coordinates are the full-resolution ones
 *   times `2 ** extraZoomLevels`. Forgetting the divide is silently wrong by a
 *   power of two, which is why it is `levelScale()` and nobody inlines it.
 *
 *   **Rotation.** Under core's Rotate & Flip the screen is a turned frame
 *   over the image, not a rectangle of it. `currentViewport` reports that as
 *   the figure format's `orientation` record; `fitRegion` sizes the zoom off
 *   the region's TURNED extent, so an axis-aligned box on a view turned 90
 *   degrees fits the screen rather than overflowing it.
 *
 * Pure apart from what it is handed: no globals are read except
 * `OpenSeadragon` (for Point/Rect) at call time, so a probe can run it in a
 * vm context with a stand-in viewer.
 */
(function (global) {
    "use strict";

    // -- orientation ------------------------------------------------------
    //
    // The figure format's orientation record (see figureSchema.js for the
    // long form): `{ degrees, flip_h, flip_v, frame_w, frame_h }`, where the
    // viewport's x/y/w/h is the axis-aligned box AROUND the turned frame and
    // frame_w/frame_h are the frame's own size along the screen. Absent on an
    // upright viewport.

    /** Positive modulo 360, with float noise at the seam read as 0. */
    function normalizeDegrees(value) {
        const number = Number(value);
        if (!Number.isFinite(number)) return 0;
        const turned = ((number % 360) + 360) % 360;
        return 360 - turned < 1e-9 ? 0 : turned;
    }

    /** The viewport's orientation, or null when it is upright. */
    function orientationOf(viewport) {
        const o = viewport && viewport.orientation;
        if (!o || typeof o !== "object") return null;
        const degrees = normalizeDegrees(o.degrees);
        if (!degrees && !o.flip_h && !o.flip_v) return null;
        return {
            degrees: degrees,
            flip_h: Boolean(o.flip_h),
            flip_v: Boolean(o.flip_v),
            frame_w: Number(o.frame_w) > 0 ? Number(o.frame_w) : viewport.w,
            frame_h: Number(o.frame_h) > 0 ? Number(o.frame_h) : viewport.h,
        };
    }

    /** The core viewer's transform state (`{degrees, flipH, flipV}`, see
     *  viewTransform.js), as an orientation, or null. */
    function fromViewTransform(state) {
        if (!state) return null;
        const degrees = normalizeDegrees(state.degrees);
        if (!degrees && !state.flipH && !state.flipV) return null;
        return { degrees: degrees, flip_h: Boolean(state.flipH), flip_v: Boolean(state.flipV) };
    }

    /** The frame's middle, in image pixels -- the box's middle, either way. */
    function frameCenter(viewport) {
        return { x: viewport.x + viewport.w / 2, y: viewport.y + viewport.h / 2 };
    }

    /**
     * A viewport for a frame of `frameW` x `frameH` image pixels centred on
     * (cx, cy), seen through `orientation`. The box is the frame's turned
     * extent; a flip changes no extent.
     */
    function orientedViewport(cx, cy, frameW, frameH, orientation) {
        const radians = orientation.degrees * Math.PI / 180;
        const cos = Math.abs(Math.cos(radians));
        const sin = Math.abs(Math.sin(radians));
        const w = frameW * cos + frameH * sin;
        const h = frameW * sin + frameH * cos;
        return {
            x: cx - w / 2, y: cy - h / 2, w: w, h: h,
            orientation: {
                degrees: orientation.degrees,
                flip_h: Boolean(orientation.flip_h),
                flip_v: Boolean(orientation.flip_v),
                frame_w: frameW, frame_h: frameH,
            },
        };
    }

    // -- the viewer ---------------------------------------------------------

    /** The OpenSeadragon viewer behind an ImageViewer (or RgbImageViewer). */
    function osdOf(imageViewer) {
        return (imageViewer && imageViewer.viewer) || null;
    }

    /** The world item image coordinates are read through. See the header. */
    function referenceItem(imageViewer) {
        if (imageViewer && typeof imageViewer.referenceItem === "function") {
            const item = imageViewer.referenceItem();
            if (item) return item;
        }
        const world = osdOf(imageViewer) && osdOf(imageViewer).world;
        return world && typeof world.getItemAt === "function" ? world.getItemAt(0) || null : null;
    }

    /** OpenSeadragon image pixels per full-resolution image pixel. */
    function levelScale(imageViewer, config) {
        const source = config || (imageViewer && imageViewer.config) || null;
        return 2 ** ((source && source.extraZoomLevels) || 0);
    }

    /** The viewer's turn, in degrees, as OpenSeadragon has it. */
    function rotationOf(imageViewer) {
        const viewport = osdOf(imageViewer) && osdOf(imageViewer).viewport;
        if (!viewport || typeof viewport.getRotation !== "function") return 0;
        return normalizeDegrees(viewport.getRotation());
    }

    /**
     * What the viewer is looking at right now, in full-resolution image pixels.
     *
     * `{x, y, w, h}` -- plus `orientation` when the view is turned or mirrored,
     * in which case the box is the one AROUND the turned frame (see the
     * orientation section above). `{0, 0, 1, 1}` when nothing is open yet, so a
     * caller that stores the answer never stores NaN.
     */
    function currentViewport(imageViewer, config) {
        const viewer = osdOf(imageViewer);
        const item = referenceItem(imageViewer);
        if (!viewer || !item) return { x: 0, y: 0, w: 1, h: 1 };
        const scale = levelScale(imageViewer, config);
        const transform = imageViewer.viewTransform && imageViewer.viewTransform.get
            ? imageViewer.viewTransform.get() : null;
        const orientation = fromViewTransform(transform);
        if (orientation) {
            // Turned or mirrored: the screen is a frame, not a rectangle of
            // the image -- its middle, its size along the screen, and how it
            // was turned. `getBoundsNoRotate` is that frame's unturned size
            // about the same centre, in viewport units.
            const view = viewer.viewport.getBoundsNoRotate(true);
            const middle = item.viewportToImageCoordinates(viewer.viewport.getCenter(true));
            const size = item.viewportToImageCoordinates(
                new OpenSeadragon.Point(view.width, view.height))
                .minus(item.viewportToImageCoordinates(new OpenSeadragon.Point(0, 0)));
            return orientedViewport(
                middle.x / scale, middle.y / scale,
                Math.max(1, size.x / scale), Math.max(1, size.y / scale), orientation);
        }
        const bounds = item.viewportToImageRectangle(viewer.viewport.getBounds(true));
        return {
            x: bounds.x / scale, y: bounds.y / scale,
            w: Math.max(1, bounds.width / scale), h: Math.max(1, bounds.height / scale),
        };
    }

    /**
     * Move the viewer to a recorded viewport (the figure format's), turning it
     * to the orientation the viewport was framed through -- or upright, when it
     * records none, because every viewport written before rotation existed was
     * framed upright.
     *
     * Immediately rather than animated: this is a jump to a recorded place,
     * and a two-second pan across a slide to get there is a two-second wait
     * that tells the user nothing. Returns whether anything was moved.
     */
    function restoreViewport(imageViewer, config, viewport) {
        const viewer = osdOf(imageViewer);
        const item = referenceItem(imageViewer);
        if (!viewer || !item || !viewport) return false;
        const scale = levelScale(imageViewer, config);
        const orientation = orientationOf(viewport);
        if (imageViewer.viewTransform) {
            imageViewer.viewTransform.set(orientation
                ? { degrees: orientation.degrees, flipH: orientation.flip_h, flipV: orientation.flip_v }
                : { degrees: 0, flipH: false, flipV: false }, { immediately: true });
        }
        if (orientation) {
            // Fit the FRAME: its middle to the middle of the viewer, and the
            // zoom at which its screen-axis size is contained. fitBounds would
            // fit the box around it, which on an odd angle is larger.
            const center = frameCenter(viewport);
            const origin = item.imageToViewportCoordinates(0, 0, true);
            const unit = item.imageToViewportCoordinates(1000 * scale, 0, true).x - origin.x;
            const frameW = orientation.frame_w * scale * unit / 1000;
            const frameH = orientation.frame_h * scale * unit / 1000;
            const width = Math.max(frameW, frameH * viewer.viewport.getAspectRatio());
            viewer.viewport.panTo(item.imageToViewportCoordinates(
                center.x * scale, center.y * scale, true), true);
            viewer.viewport.zoomTo(1 / width, null, true);
            return true;
        }
        const bounds = item.imageToViewportRectangle(new OpenSeadragon.Rect(
            viewport.x * scale, viewport.y * scale,
            viewport.w * scale, viewport.h * scale));
        viewer.viewport.fitBounds(bounds, true);
        return true;
    }

    // -- moving it, in full-resolution pixels ------------------------------
    //
    // Each takes `options.immediately` (default true: an agent that moves the
    // view and then captures it wants the view it asked for, not the first
    // frame of an animation towards it) and `options.config` (default the
    // viewer's own).

    function immediate(options) {
        return !(options && options.immediately === false);
    }

    /**
     * How many SCREEN pixels one full-resolution image pixel is drawn at right
     * now -- 1 is 100%, 0.25 is a quarter, 4 is magnified. Null with nothing
     * open. CSS pixels, the unit OpenSeadragon's container size is in.
     */
    function scaleOf(imageViewer, options) {
        const viewer = osdOf(imageViewer);
        const item = referenceItem(imageViewer);
        if (!viewer || !item) return null;
        const imageZoom = item.viewportToImageZoom(viewer.viewport.getZoom(true));
        return imageZoom * levelScale(imageViewer, options && options.config);
    }

    /** Centre the view on (x, y), full-resolution image pixels. */
    function panTo(imageViewer, x, y, options) {
        const viewer = osdOf(imageViewer);
        const item = referenceItem(imageViewer);
        if (!viewer || !item) return false;
        if (!Number.isFinite(Number(x)) || !Number.isFinite(Number(y))) {
            throw new Error("panTo needs a numeric x and y in image pixels");
        }
        const scale = levelScale(imageViewer, options && options.config);
        viewer.viewport.panTo(item.imageToViewportCoordinates(
            Number(x) * scale, Number(y) * scale, true), immediate(options));
        return true;
    }

    /**
     * Zoom so one full-resolution image pixel is `scale` screen pixels (see
     * scaleOf), about the current centre.
     */
    function zoomTo(imageViewer, scale, options) {
        const viewer = osdOf(imageViewer);
        const item = referenceItem(imageViewer);
        if (!viewer || !item) return false;
        const wanted = Number(scale);
        if (!(wanted > 0) || !Number.isFinite(wanted)) {
            throw new Error("zoomTo needs a positive scale (screen pixels per image pixel)");
        }
        const imageZoom = wanted / levelScale(imageViewer, options && options.config);
        viewer.viewport.zoomTo(item.imageToViewportZoom(imageZoom), null, immediate(options));
        return true;
    }

    /**
     * Frame `{x, y, width, height}` (full-resolution image pixels; `w`/`h`
     * accepted too) so the whole of it is on screen, centred.
     *
     * Not `viewport.fitBounds`: that fits the box in OpenSeadragon's unturned
     * frame, so on a view turned 90 degrees a wide region is fitted to the
     * screen's width and then drawn across its height. Here the zoom comes
     * from the region's TURNED extent against the container, and the centre
     * is panned to, which is right at every angle and says nothing about a
     * flip (a mirror changes no extent).
     */
    function fitRegion(imageViewer, region, options) {
        const viewer = osdOf(imageViewer);
        const item = referenceItem(imageViewer);
        if (!viewer || !item || !region) return false;
        const x = Number(region.x);
        const y = Number(region.y);
        const width = Number(region.width !== undefined ? region.width : region.w);
        const height = Number(region.height !== undefined ? region.height : region.h);
        if (![x, y, width, height].every(Number.isFinite) || !(width > 0) || !(height > 0)) {
            throw new Error("fitRegion needs x, y and a positive width and height in image pixels");
        }
        const radians = rotationOf(imageViewer) * Math.PI / 180;
        const cos = Math.abs(Math.cos(radians));
        const sin = Math.abs(Math.sin(radians));
        const screenW = width * cos + height * sin;
        const screenH = width * sin + height * cos;
        const container = viewer.viewport.getContainerSize();
        const scale = Math.min(container.x / screenW, container.y / screenH);
        panTo(imageViewer, x + width / 2, y + height / 2, options);
        zoomTo(imageViewer, scale, options);
        return true;
    }

    const ViewerScene = {
        normalizeDegrees: normalizeDegrees,
        orientationOf: orientationOf,
        fromViewTransform: fromViewTransform,
        frameCenter: frameCenter,
        orientedViewport: orientedViewport,
        referenceItem: referenceItem,
        levelScale: levelScale,
        rotationOf: rotationOf,
        currentViewport: currentViewport,
        restoreViewport: restoreViewport,
        scaleOf: scaleOf,
        panTo: panTo,
        zoomTo: zoomTo,
        fitRegion: fitRegion,
    };

    // On `window` for the page, and on the script's global as well: Figure
    // Builder's probes run these files in a vm context whose `window` is a
    // separate stand-in object, and FigureSchema's delegates name this by its
    // bare global. In a browser the two are the same object.
    global.PlexoraViewerScene = ViewerScene;
    if (typeof globalThis !== "undefined" && globalThis !== global) {
        globalThis.PlexoraViewerScene = ViewerScene;
    }
})(typeof window !== "undefined" ? window : globalThis);
