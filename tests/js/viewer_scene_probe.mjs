/**
 * Runs the real services/viewerScene.js against a stand-in OpenSeadragon
 * viewer whose arithmetic is OSD's own for one tiled image laid across the
 * world's unit width: an item `W` source pixels wide spans viewport x 0..1, so
 * a viewport point is a source pixel divided by W, and at viewport zoom `z`
 * one source pixel is `z * containerWidth / W` screen pixels
 * (TiledImage.viewportToImageZoom).
 *
 *     node tests/js/viewer_scene_probe.mjs
 *
 * What it holds, all in FULL-RESOLUTION image pixels:
 *   - fitRegion frames the region -- centred, contained, and at exactly the
 *     zoom the tighter axis allows -- upright, turned 90 and 30 degrees, and
 *     through `extraZoomLevels` (source pixels 2^n per image pixel);
 *   - panTo / zoomTo / scaleOf agree with each other and with currentViewport;
 *   - every conversion goes through `referenceItem()`, not world item 0;
 *   - restoreViewport is the inverse of currentViewport, and currentViewport
 *     under a turned view reports the orientation record;
 *   - bad input is refused rather than turned into NaN.
 *
 * Reports {checked, failures} as JSON on stderr.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/viewerScene.js");

const failures = [];
let checked = 0;
function check(name, condition, detail) {
    checked += 1;
    if (!condition) failures.push(detail === undefined ? { name } : { name, detail });
}
const near = (a, b, tolerance = 1e-6) => Math.abs(a - b) <= tolerance;

// -- OSD stand-ins ------------------------------------------------------

class Point {
    constructor(x, y) { this.x = x; this.y = y; }
    minus(p) { return new Point(this.x - p.x, this.y - p.y); }
}

class Rect {
    constructor(x, y, width, height) {
        this.x = x; this.y = y; this.width = width; this.height = height;
    }
}

/** One tiled image, `sourceW` x `sourceH` source pixels, offset by `left`
 *  viewport units (a registered layer sitting beside the reference). */
function item(sourceW, sourceH, container, left = 0) {
    return {
        source: { dimensions: new Point(sourceW, sourceH) },
        imageToViewportCoordinates(x, y) {
            if (x instanceof Point) return new Point(left + x.x / sourceW, x.y / sourceW);
            return new Point(left + x / sourceW, y / sourceW);
        },
        viewportToImageCoordinates(p) {
            return new Point((p.x - left) * sourceW, p.y * sourceW);
        },
        imageToViewportRectangle(rect) {
            return new Rect(left + rect.x / sourceW, rect.y / sourceW,
                rect.width / sourceW, rect.height / sourceW);
        },
        viewportToImageRectangle(rect) {
            return new Rect((rect.x - left) * sourceW, rect.y * sourceW,
                rect.width * sourceW, rect.height * sourceW);
        },
        imageToViewportZoom(imageZoom) { return imageZoom * sourceW / container.x; },
        viewportToImageZoom(viewportZoom) { return viewportZoom * container.x / sourceW; },
    };
}

/**
 * An ImageViewer-shaped object: `.viewer` (OSD), `.config`, optionally
 * `.referenceItem()` and `.viewTransform`.
 */
function makeViewer({ imageW = 4000, imageH = 3000, cw = 800, ch = 600, rotation = 0,
                      extra = 0, decoy = false, transform = null } = {}) {
    const container = new Point(cw, ch);
    const scale = 2 ** extra;
    const reference = item(imageW * scale, imageH * scale, container, decoy ? 0.25 : 0);
    const items = decoy ? [item(imageW * scale, imageH * scale, container, 0), reference] : [reference];
    const state = { center: new Point(0.5, 0.375), zoom: 1 };
    const viewport = {
        getContainerSize: () => new Point(container.x, container.y),
        getRotation: () => rotation,
        getAspectRatio: () => cw / ch,
        getZoom: () => state.zoom,
        getCenter: () => new Point(state.center.x, state.center.y),
        panTo(p) { state.center = new Point(p.x, p.y); },
        zoomTo(z) { state.zoom = z; },
        getBounds() {
            const w = 1 / state.zoom;
            const h = w * ch / cw;
            return new Rect(state.center.x - w / 2, state.center.y - h / 2, w, h);
        },
        getBoundsNoRotate() { return this.getBounds(); },
        fitBounds(rect) {
            state.center = new Point(rect.x + rect.width / 2, rect.y + rect.height / 2);
            state.zoom = Math.min(1 / rect.width, (ch / cw) / rect.height);
        },
    };
    const viewer = {
        viewer: { viewport, world: { getItemAt: (i) => items[i] || null, getItemCount: () => items.length } },
        config: { extraZoomLevels: extra },
        state,
    };
    if (decoy) viewer.referenceItem = () => reference;
    if (transform) {
        let current = { ...transform };
        viewer.viewTransform = { get: () => ({ ...current }), set: (next) => { current = { ...next }; } };
    }
    return viewer;
}

const context = createContext({ Math, Number, Object, Boolean, JSON, Error, console,
                                OpenSeadragon: { Point, Rect } });
runInContext(readFileSync(SOURCE, "utf8"), context, { filename: SOURCE });
const S = context.PlexoraViewerScene;
check("the script defines PlexoraViewerScene", Boolean(S));

/** The screen extent of a full-resolution region at the current scale. */
function onScreen(viewer, region, degrees) {
    const r = degrees * Math.PI / 180;
    const scale = S.scaleOf(viewer);
    const w = (region.width * Math.abs(Math.cos(r)) + region.height * Math.abs(Math.sin(r))) * scale;
    const h = (region.width * Math.abs(Math.sin(r)) + region.height * Math.abs(Math.cos(r))) * scale;
    return { w, h };
}

// -- fitRegion ------------------------------------------------------------

for (const [label, options, region, expectedScale] of [
    ["upright, same aspect", {}, { x: 1000, y: 500, width: 400, height: 300 }, 2],
    ["upright, wide strip", {}, { x: 0, y: 0, width: 800, height: 100 }, 1],
    ["upright, tall strip", {}, { x: 100, y: 100, width: 100, height: 1200 }, 0.5],
    ["extraZoomLevels 1", { extra: 1 }, { x: 1000, y: 500, width: 400, height: 300 }, 2],
    ["extraZoomLevels 2", { extra: 2 }, { x: 1000, y: 500, width: 1600, height: 1200 }, 0.5],
    ["turned 90", { rotation: 90 }, { x: 1000, y: 500, width: 400, height: 300 }, 1.5],
    ["turned 270", { rotation: 270 }, { x: 1000, y: 500, width: 400, height: 300 }, 1.5],
    ["turned 30", { rotation: 30 }, { x: 200, y: 200, width: 400, height: 300 },
        Math.min(800 / (400 * Math.cos(Math.PI / 6) + 300 * 0.5),
                 600 / (400 * 0.5 + 300 * Math.cos(Math.PI / 6)))],
]) {
    const viewer = makeViewer(options);
    S.fitRegion(viewer, region);
    const scale = S.scaleOf(viewer);
    check(`fitRegion ${label}: the zoom the tighter axis allows`, near(scale, expectedScale, 1e-9),
        { scale, expectedScale });
    const extent = onScreen(viewer, region, options.rotation || 0);
    check(`fitRegion ${label}: contained`, extent.w <= 800 + 1e-6 && extent.h <= 600 + 1e-6, extent);
    check(`fitRegion ${label}: fills one axis`, near(extent.w, 800, 1e-6) || near(extent.h, 600, 1e-6),
        extent);
    if (!options.rotation) {
        const view = S.currentViewport(viewer);
        check(`fitRegion ${label}: centred on the region`,
            near(view.x + view.w / 2, region.x + region.width / 2, 1e-6)
            && near(view.y + view.h / 2, region.y + region.height / 2, 1e-6), { view, region });
    } else {
        const centre = viewer.viewer.world.getItemAt(0)
            .viewportToImageCoordinates(viewer.viewer.viewport.getCenter());
        const factor = 2 ** (options.extra || 0);
        check(`fitRegion ${label}: centred on the region`,
            near(centre.x / factor, region.x + region.width / 2, 1e-6)
            && near(centre.y / factor, region.y + region.height / 2, 1e-6), { centre, region });
    }
}

{
    const viewer = makeViewer();
    S.fitRegion(viewer, { x: 10, y: 20, w: 400, h: 300 });
    check("fitRegion takes w/h as well as width/height", near(S.scaleOf(viewer), 2, 1e-9));
    for (const bad of [{ x: 0, y: 0, width: 0, height: 10 }, { x: "a", y: 0, width: 5, height: 5 },
                       { x: 0, y: 0, width: -3, height: 5 }, { x: 0, y: 0 }]) {
        let threw = false;
        try { S.fitRegion(viewer, bad); } catch (error) { threw = true; }
        check(`fitRegion refuses ${JSON.stringify(bad)}`, threw);
    }
    check("fitRegion without a region is false", S.fitRegion(viewer, null) === false);
}

// -- pan, zoom, scale ---------------------------------------------------

{
    const viewer = makeViewer({ extra: 1 });
    S.zoomTo(viewer, 0.5);
    check("zoomTo then scaleOf round-trips (extraZoomLevels 1)", near(S.scaleOf(viewer), 0.5, 1e-9),
        { scale: S.scaleOf(viewer) });
    S.panTo(viewer, 1234, 567);
    const view = S.currentViewport(viewer);
    check("panTo centres the view on that image pixel",
        near(view.x + view.w / 2, 1234, 1e-6) && near(view.y + view.h / 2, 567, 1e-6), view);
    check("at scale 0.5 an 800 px screen spans 1600 image pixels", near(view.w, 1600, 1e-6)
        && near(view.h, 1200, 1e-6), view);
    let threw = false;
    try { S.zoomTo(viewer, 0); } catch (error) { threw = true; }
    check("zoomTo refuses a non-positive scale", threw);
    threw = false;
    try { S.panTo(viewer, NaN, 3); } catch (error) { threw = true; }
    check("panTo refuses a non-numeric point", threw);
    check("the helpers are no-ops with no viewer", S.panTo(null, 1, 1) === false
        && S.zoomTo({}, 1) === false && S.scaleOf(null) === null);
    const empty = S.currentViewport({ viewer: { viewport: {}, world: { getItemAt: () => null } } });
    check("currentViewport with nothing open is a unit box, never NaN",
        JSON.stringify(empty) === JSON.stringify({ x: 0, y: 0, w: 1, h: 1 }), empty);
}

// -- the reference item ---------------------------------------------------

{
    // Item 0 is a layer sitting a quarter of the world to the LEFT of the
    // reference; converting through it would put the view 1000 px off.
    const viewer = makeViewer({ decoy: true });
    S.fitRegion(viewer, { x: 1000, y: 500, width: 400, height: 300 });
    const view = S.currentViewport(viewer);
    check("conversions go through referenceItem(), not world item 0",
        near(view.x + view.w / 2, 1200, 1e-6) && near(view.y + view.h / 2, 650, 1e-6), view);
    check("referenceItem prefers the viewer's own answer",
        S.referenceItem(viewer) === viewer.referenceItem());
}

// -- restore and orientation ---------------------------------------------

{
    const viewer = makeViewer({ extra: 1, transform: { degrees: 0, flipH: false, flipV: false } });
    const target = { x: 400, y: 300, w: 800, h: 600 };
    check("restoreViewport moves the view", S.restoreViewport(viewer, null, target) === true);
    const back = S.currentViewport(viewer);
    check("restoreViewport is currentViewport's inverse",
        ["x", "y", "w", "h"].every((key) => near(back[key], target[key], 1e-6)), back);
    check("an upright restore leaves the view upright", viewer.viewTransform.get().degrees === 0);

    const turned = makeViewer({ transform: { degrees: 90, flipH: true, flipV: false } });
    S.zoomTo(turned, 1);
    S.panTo(turned, 2000, 1500);
    const framed = S.currentViewport(turned);
    check("a turned view reports its orientation", Boolean(framed.orientation)
        && framed.orientation.degrees === 90 && framed.orientation.flip_h === true
        && near(framed.orientation.frame_w, 800, 1e-6) && near(framed.orientation.frame_h, 600, 1e-6),
        framed);
    check("the box around a quarter-turned frame swaps its sides",
        near(framed.w, 600, 1e-6) && near(framed.h, 800, 1e-6), framed);
    check("orientationOf reads the record back", S.orientationOf(framed).degrees === 90);
    check("orientationOf is null for an upright viewport", S.orientationOf({ x: 0, y: 0, w: 1, h: 1 }) === null);
    check("normalizeDegrees folds -90 and 720", S.normalizeDegrees(-90) === 270
        && S.normalizeDegrees(720) === 0 && S.normalizeDegrees("x") === 0);
    check("fromViewTransform is null when upright",
        S.fromViewTransform({ degrees: 360, flipH: false, flipV: false }) === null);
}

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.stdout.write(failures.length ? `${failures.length} of ${checked} checks failed\n`
                                     : `all ${checked} checks passed\n`);
process.exitCode = failures.length ? 1 : 0;
