/**
 * Video Studio's burned-in scale bar against the zoom a 2-D frame is really
 * drawn at, upright and turned.
 *
 *     node tests/js/video_studio_scalebar_rotation_probe.mjs
 *
 * A 2-D frame is put on screen by videoSceneState.js applyViewport: it turns
 * the viewer to `viewport.degrees` and hands the output-aspect rectangle of
 * the view (videoCamera2d.js rect) to services/viewerScene.js fitRegion,
 * which fits the TURNED extent of that rectangle to the screen. The scale bar
 * (videoOverlays.js micronsPerPixel / draw) must describe that same zoom: its
 * µm per output pixel is pixel_size / scaleOf(viewer), and the bar it draws,
 * measured at that zoom, is as long in µm as its label says. Turned 90 or 270
 * degrees, a wide output shows the rectangle's HEIGHT across its width, so
 * the zoom differs from the upright one by the output's aspect ratio.
 *
 * Runs the real viewerScene.js, videoCamera2d.js, videoSceneState.js and
 * videoOverlays.js against a stand-in OpenSeadragon viewer with
 * viewer_scene_probe.mjs's arithmetic (one tiled image across the world's
 * unit width), its container the output's size.
 *
 * Reports {checked, failures} as JSON on stderr.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SCENE = join(REPO, "plexora/client/src/js/services/viewerScene.js");
const STATIC = join(REPO, "plexora/plugins/video_studio/static");
const FILES = ["videoEasing.js", "videoInterp.js", "videoSchema.js", "videoCamera3d.js", "videoCamera2d.js",
               "videoAnimation.js", "videoSceneState.js", "videoOverlays.js"];

const failures = [];
let checked = 0;
function check(name, condition, detail) {
    checked += 1;
    if (!condition) failures.push(detail === undefined ? { name } : { name, detail });
}
const close = (a, b, relative = 1e-6) => Math.abs(a - b) <= relative * Math.max(Math.abs(a), Math.abs(b));

class Point {
    constructor(x, y) { this.x = x; this.y = y; }
    minus(p) { return new Point(this.x - p.x, this.y - p.y); }
}

class Rect {
    constructor(x, y, width, height) {
        this.x = x; this.y = y; this.width = width; this.height = height;
    }
}

/** An ImageViewer-shaped stand-in whose OSD turn follows its viewTransform. */
function makeViewer({ imageW = 8000, cw, ch }) {
    const container = new Point(cw, ch);
    const item = {
        imageToViewportCoordinates(x, y) {
            if (x instanceof Point) return new Point(x.x / imageW, x.y / imageW);
            return new Point(x / imageW, y / imageW);
        },
        viewportToImageCoordinates(p) { return new Point(p.x * imageW, p.y * imageW); },
        viewportToImageRectangle(r) {
            return new Rect(r.x * imageW, r.y * imageW, r.width * imageW, r.height * imageW);
        },
        imageToViewportZoom(imageZoom) { return imageZoom * imageW / container.x; },
        viewportToImageZoom(viewportZoom) { return viewportZoom * container.x / imageW; },
    };
    const state = { center: new Point(0.5, 0.3), zoom: 1 };
    let transform = { degrees: 0, flipH: false, flipV: false };
    const viewport = {
        getContainerSize: () => new Point(container.x, container.y),
        getRotation: () => transform.degrees,
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
    };
    return {
        viewer: { viewport, world: { getItemAt: (i) => (i === 0 ? item : null), getItemCount: () => 1 } },
        config: { extraZoomLevels: 0 },
        viewTransform: {
            get: () => ({ ...transform }),
            set(next) { transform = { ...transform, ...next }; },
        },
    };
}

/** A 2-D context that draws nothing. */
function nullContext() {
    return new Proxy({}, {
        get(target, key) {
            if (key in target) return target[key];
            if (key === "measureText") return () => ({ width: 0 });
            return () => undefined;
        },
        set(target, key, value) { target[key] = value; return true; },
    });
}

const context = createContext({ console, Math, JSON, Promise, setTimeout, Date, Array, Object, Number,
                                String, Boolean, Error, Proxy, OpenSeadragon: { Point, Rect } });
context.window = context;
context.globalThis = context;
runInContext(readFileSync(SCENE, "utf8"), context, { filename: SCENE });
for (const file of FILES) runInContext(readFileSync(join(STATIC, file), "utf8"), context, { filename: file });
const ViewerScene = context.PlexoraViewerScene;
const Scene = context.PlexoraVideoSceneState;
const Overlays = context.PlexoraVideoOverlays;
check("the modules load", Boolean(ViewerScene && Scene && Overlays && Overlays.micronsPerPixel));

const PIXEL_SIZE = 0.5;
const facts = { pixel_size: PIXEL_SIZE, image_size: [8000, 6000], depth: 1, frames: 1 };

for (const [width, height] of [[1920, 1080], [1080, 1920], [1000, 1000]]) {
    for (const degrees of [0, 90, 180, 270, 30]) {
        const label = `${width}x${height} turned ${degrees}`;
        const iv = makeViewer({ cw: width, ch: height });
        const env = { iv, config: iv.config };
        const viewport = { cx: 4000, cy: 3000, w: 1000, degrees, flip_h: false, flip_v: false };
        Scene.applyViewport(env, viewport, width / height);
        // Screen (= output) pixels per full-resolution image pixel, as drawn.
        const scale = ViewerScene.scaleOf(iv);
        const truth = PIXEL_SIZE / scale;
        const state = { view: { mode: "plane" }, viewport };
        const size = { width, height };
        const said = Overlays.micronsPerPixel(state, facts, size);
        check(`${label}: µm per output pixel is the zoom the frame is drawn at`, close(said, truth),
              { micronsPerPixel: said, drawn_at: truth, ratio: said / truth });
        const drawn = Overlays.draw(nullContext(), size, state, facts, { scalebar: true }, {});
        const bar = drawn.scalebar;
        const spans = bar ? bar.pixels * truth : NaN;
        check(`${label}: the bar spans the length its label says`, bar && close(spans, bar.um),
              { label_um: bar && bar.um, spans_um: spans });
    }
}

process.stderr.write(JSON.stringify({ checked, failures }));
process.exit(failures.length ? 1 : 0);
