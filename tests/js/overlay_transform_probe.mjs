/**
 * Everything Plexora draws itself lands where OpenSeadragon draws the tiles,
 * on a turned or mirrored view.
 *
 *     node tests/js/overlay_transform_probe.mjs [--overlay <path>] [--points <path>]
 *
 * Two drawers, both of which used to place their origin with OSD's rotated
 * pixelFromPoint and never turn anything else -- so on a turned view a cell
 * outline or a transcript sat upright over tissue that was not:
 *
 *   - canvas-overlay-hd.js, core's one overlay canvas (selection, centroids,
 *     ROI shapes, the transcripts fallback all draw through it);
 *   - the transcript point renderer's placement(), fed to a vertex shader
 *     whose arithmetic is repeated here in JS.
 *
 * The reference is CanvasDrawer's own: a viewport point is placed unrotated,
 * then the context is mirrored about the centre line and turned about the
 * centre. Reports {checked, failures} as JSON on stderr.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const args = process.argv.slice(2);
const flag = (name, fallback) => {
    const at = args.indexOf(name);
    return at >= 0 ? args[at + 1] : fallback;
};
const OVERLAY = flag("--overlay", join(REPO, "plexora/client/external/openseadragon-bin-2.4.0/canvas-overlay-hd.js"));
const POINTS = flag("--points", join(REPO, "plexora/plugins/transcripts/static/transcriptPoints.js"));
const SERVICE = join(REPO, "plexora/client/src/js/services/viewTransform.js");

const failures = [];
let checked = 0;
function check(name, condition, detail) {
    checked += 1;
    if (!condition) failures.push(detail === undefined ? { name } : { name, detail });
}
const near = (a, b, tolerance = 1e-6) => Math.abs(a - b) <= tolerance;

class Point {
    constructor(x, y) { this.x = x; this.y = y; }
    minus(p) { return new Point(this.x - p.x, this.y - p.y); }
    plus(p) { return new Point(this.x + p.x, this.y + p.y); }
    times(f) { return new Point(this.x * f, this.y * f); }
    divide(f) { return new Point(this.x / f, this.y / f); }
    rotate(degrees, pivot = new Point(0, 0)) {
        const r = degrees * Math.PI / 180;
        const dx = this.x - pivot.x;
        const dy = this.y - pivot.y;
        return new Point(pivot.x + dx * Math.cos(r) - dy * Math.sin(r),
                         pivot.y + dx * Math.sin(r) + dy * Math.cos(r));
    }
}

const W = 900;
const H = 600;
const BOUNDS = { x: 0.2, y: 0.1, width: 0.6, height: 0.4 };
/** 1 viewport unit = 4000 image px. */
const IMAGE_PX = 4000;

function makeViewer(degrees, flipped) {
    const centre = new Point(BOUNDS.x + BOUNDS.width / 2, BOUNDS.y + BOUNDS.height / 2);
    const viewport = {
        getContainerSize: () => new Point(W, H),
        getRotation: () => degrees,
        getFlip: () => flipped,
        getZoom: () => 1 / BOUNDS.width,
        getCenter: () => centre,
        pixelFromPointNoRotate: (p) => p.minus(new Point(BOUNDS.x, BOUNDS.y)).times(W / BOUNDS.width),
        pixelFromPoint(p) { return this.pixelFromPointNoRotate(p.rotate(degrees, centre)); },
        pointFromPixel(p) {
            return p.divide(W / BOUNDS.width).plus(new Point(BOUNDS.x, BOUNDS.y)).rotate(-degrees, centre);
        },
    };
    const item = {
        // OSD: viewport zoom times the container width over the image width.
        viewportToImageZoom: (zoom) => zoom * W / IMAGE_PX,
        imageToViewportCoordinates: (x, y) => new Point(x / IMAGE_PX, y / IMAGE_PX),
    };
    const handlers = {};
    return {
        viewport,
        container: { clientWidth: W, clientHeight: H },
        canvas: { appendChild() {} },
        world: { getItemCount: () => 1, getItemAt: () => item },
        addHandler: (name, fn) => { (handlers[name] ||= []).push(fn); },
        handlers,
        item,
    };
}

/** Where the drawer puts an image pixel, in css px. */
function drawnAt(viewer, x, y) {
    const vp = viewer.viewport;
    const c = new Point(W / 2, H / 2);
    let p = vp.pixelFromPointNoRotate(viewer.item.imageToViewportCoordinates(x, y)).rotate(vp.getRotation(), c);
    if (vp.getFlip()) p = new Point(W - p.x, p.y);
    return p;
}

class RecordingContext {
    constructor() { this.m = [1, 0, 0, 1, 0, 0]; }
    _mul(a, b, c, d, e, f) {
        const [A, B, C, D, E, F] = this.m;
        this.m = [A * a + C * b, B * a + D * b, A * c + C * d, B * c + D * d,
                  A * e + C * f + E, B * e + D * f + F];
    }
    translate(x, y) { this._mul(1, 0, 0, 1, x, y); }
    scale(x, y) { this._mul(x, 0, 0, y, 0, 0); }
    rotate(r) { this._mul(Math.cos(r), Math.sin(r), -Math.sin(r), Math.cos(r), 0, 0); }
    setTransform(a, b, c, d, e, f) { this.m = [a, b, c, d, e, f]; }
    clearRect() {}
    apply(x, y) {
        const [a, b, c, d, e, f] = this.m;
        return new Point(a * x + c * y + e, b * x + d * y + f);
    }
}

function makeElement() {
    const context = new RecordingContext();
    return {
        style: {},
        setAttribute() {},
        appendChild() {},
        getContext: () => context,
        context,
    };
}

const sandbox = {
    console, Math, Number, Object, Array, JSON, Set, Map, Promise, String, Boolean,
    document: { createElement: () => makeElement() },
    devicePixelRatio: 2,
    OpenSeadragon: { Point },
    requestAnimationFrame: () => 0,
    setTimeout: () => 0,
    clearTimeout: () => {},
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
createContext(sandbox);
runInContext(readFileSync(SERVICE, "utf8"), sandbox, { filename: SERVICE });
runInContext(readFileSync(OVERLAY, "utf8"), sandbox, { filename: OVERLAY });
const CanvasOverlayHd = sandbox.OpenSeadragon.CanvasOverlayHd;
check("the overlay host loads", typeof CanvasOverlayHd === "function");

const probes = [[0, 0], [1200, 300], [2400, 1500], [3100, 800]];

for (const degrees of [0, 90, 30, 180]) {
    for (const flipped of [false, true]) {
        const viewer = makeViewer(degrees, flipped);
        let seen = null;
        const overlay = new CanvasOverlayHd(viewer, {
            onRedraw: (opts) => {
                // The matrix at draw time, and what an overlay drawing in
                // image pixels would hit -- divided back out of the device
                // scale the host applies first.
                seen = probes.map(([x, y]) => {
                    const device = opts.context.apply(x, y);
                    return new Point(device.x / sandbox.devicePixelRatio, device.y / sandbox.devicePixelRatio);
                });
            },
        });
        overlay.resize();
        overlay._updateCanvas();
        check(`the overlay drew (${degrees}, flip=${flipped})`, Array.isArray(seen));
        probes.forEach(([x, y], i) => {
            const want = drawnAt(viewer, x, y);
            const got = seen?.[i];
            check(`an overlay pixel lands on its tile pixel (${degrees}deg, flip=${flipped}, ${x},${y})`,
                got && near(got.x, want.x, 1e-6) && near(got.y, want.y, 1e-6), { got, want });
        });
    }
}

// -- transcript points ----------------------------------------------------

const pointsSource = readFileSync(POINTS, "utf8");
runInContext(`${pointsSource}\n;globalThis.__Renderer = TranscriptPointRenderer;`, sandbox, { filename: POINTS });
const Renderer = sandbox.__Renderer;
check("the transcript renderer loads", typeof Renderer === "function");

/** The vertex shader's placement, in JS: device px from image px. */
function shaderPlace(place, x, y) {
    let dx = place.originX + x * place.scale;
    let dy = place.originY + y * place.scale;
    const ox = dx - place.centerX;
    const oy = dy - place.centerY;
    dx = place.centerX + ox * place.cos - oy * place.sin;
    dy = place.centerY + ox * place.sin + oy * place.cos;
    if (place.flipped) dx = 2 * place.centerX - dx;
    return new Point(dx, dy);
}

check("the shader turns and mirrors about the centre",
    /uniform vec2 u_center;/.test(pointsSource) && /uniform vec2 u_rotation;/.test(pointsSource)
    && /uniform float u_flip;/.test(pointsSource)
    && pointsSource.includes("if (u_flip > 0.5) device.x = 2.0 * u_center.x - device.x;"));
check("...and is handed all three", ["u_center", "u_rotation", "u_flip"].every(
    (name) => pointsSource.includes(`gl.uniform${name === "u_flip" ? "1f" : "2f"}(this.uniform.${name}`)));

for (const degrees of [0, 90, 30]) {
    for (const flipped of [false, true]) {
        const viewer = makeViewer(degrees, flipped);
        const place = Renderer.prototype.placement.call({ viewer, anchorIndex: () => 0 });
        check(`placement answers (${degrees}, flip=${flipped})`, !!place);
        if (!place) continue;
        probes.forEach(([x, y]) => {
            const want = drawnAt(viewer, x, y);
            const got = shaderPlace(place, x, y);
            const ratio = sandbox.devicePixelRatio;
            check(`a transcript lands on its tile pixel (${degrees}deg, flip=${flipped}, ${x},${y})`,
                near(got.x / ratio, want.x, 1e-6) && near(got.y / ratio, want.y, 1e-6),
                { got: { x: got.x / ratio, y: got.y / ratio }, want });
        });
    }
}

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.exitCode = failures.length ? 1 : 0;
