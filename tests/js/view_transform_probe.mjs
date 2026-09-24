/**
 * Runs the real services/viewTransform.js against a stand-in OpenSeadragon
 * viewport whose arithmetic is OSD 6.1's own (Viewport.pixelFromPoint,
 * pointFromPixel, pixelFromPointNoRotate) and whose drawing is CanvasDrawer's
 * (mirror about the centre line, then turn about the centre).
 *
 *     node tests/js/view_transform_probe.mjs [--source <path>]
 *
 * What it holds:
 *   - the mapping from our screen-axis flips onto OSD's one horizontal flip,
 *     checked as MATRICES at several angles, not as a table copied from the
 *     code -- a table is only as right as whoever wrote both halves;
 *   - that every helper agrees with what the drawer actually puts on screen,
 *     both ways round;
 *   - the service's lifecycle: one save per burst, none for a boot adopt.
 *
 * Reports {checked, failures} as JSON on stderr.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import path from "node:path";

const args = process.argv.slice(2);
const sourceIndex = args.indexOf("--source");
const SOURCE = sourceIndex >= 0
    ? args[sourceIndex + 1]
    : path.resolve("plexora/client/src/js/services/viewTransform.js");

const failures = [];
let checked = 0;

function check(name, condition, detail) {
    checked += 1;
    if (!condition) failures.push(detail === undefined ? { name } : { name, detail });
}

function near(a, b, tolerance = 1e-6) {
    return Math.abs(a - b) <= tolerance;
}

// -- OSD stand-ins ------------------------------------------------------

class Point {
    constructor(x, y) { this.x = x; this.y = y; }
    minus(p) { return new Point(this.x - p.x, this.y - p.y); }
    plus(p) { return new Point(this.x + p.x, this.y + p.y); }
    times(f) { return new Point(this.x * f, this.y * f); }
    divide(f) { return new Point(this.x / f, this.y / f); }
    /** OSD's Point.rotate: about `pivot`, degrees, y down. */
    rotate(degrees, pivot = new Point(0, 0)) {
        const r = degrees * Math.PI / 180;
        const cos = Math.cos(r);
        const sin = Math.sin(r);
        const dx = this.x - pivot.x;
        const dy = this.y - pivot.y;
        return new Point(pivot.x + dx * cos - dy * sin, pivot.y + dx * sin + dy * cos);
    }
}

class Rect {
    constructor(x, y, width, height) { Object.assign(this, { x, y, width, height }); }
    getCenter() { return new Point(this.x + this.width / 2, this.y + this.height / 2); }
    getTopLeft() { return new Point(this.x, this.y); }
}

/** A viewport of W x H css px showing viewport-space bounds `bounds`. */
function makeViewer({ W = 800, H = 500, degrees = 0, flipped = false } = {}) {
    const calls = { panTo: [], zoomTo: [], setRotation: [], setFlip: [], goHome: 0, events: [] };
    const bounds = new Rect(0.1, 0.05, 0.8, 0.5);
    const viewport = {
        rotation: degrees,
        flipped,
        homeFillsViewer: true,
        getContainerSize() { return new Point(W, H); },
        getRotation() { return this.rotation; },
        getFlip() { return this.flipped; },
        setFlip(state) { calls.setFlip.push(state); this.flipped = state; return this; },
        setRotation(d, immediately) { calls.setRotation.push([d, !!immediately]); this.rotation = d; return this; },
        getCenter() { return bounds.getCenter(); },
        getAspectRatio() { return W / H; },
        // OSD 6.1, verbatim in shape.
        pixelFromPointNoRotate(point) {
            return point.minus(bounds.getTopLeft()).times(W / bounds.width);
        },
        pixelFromPoint(point) {
            return this.pixelFromPointNoRotate(point.rotate(this.rotation, this.getCenter()));
        },
        pointFromPixelNoRotate(pixel) {
            return pixel.divide(W / bounds.width).plus(bounds.getTopLeft());
        },
        pointFromPixel(pixel) {
            return this.pointFromPixelNoRotate(pixel).rotate(-this.rotation, this.getCenter());
        },
        goHome() { calls.goHome += 1; return this; },
        panTo(center, immediately) { calls.panTo.push([center, !!immediately]); return this; },
        zoomTo(zoom, ref, immediately) { calls.zoomTo.push([zoom, !!immediately]); return this; },
    };
    const home = new Rect(0, 0, 1, 0.5);
    const viewer = {
        viewport,
        calls,
        world: { getItemCount: () => 1, getHomeBounds: () => home },
        raiseEvent(name, data) { calls.events.push([name, data]); },
    };
    return viewer;
}

/**
 * Where CanvasDrawer puts a viewport point: placed unrotated, then the whole
 * context mirrored about the centre line (when flipped) and turned about the
 * centre. The context transform is F . R, so the point is R'd first.
 */
function drawnAt(viewer, point) {
    const vp = viewer.viewport;
    const { x: W, y: H } = vp.getContainerSize();
    const c = new Point(W / 2, H / 2);
    let p = vp.pixelFromPointNoRotate(point).rotate(vp.getRotation(), c);
    if (vp.getFlip()) p = new Point(W - p.x, p.y);
    return p;
}

// -- a 2-D affine recorder for orientContext -----------------------------

class RecordingContext {
    constructor() { this.m = [1, 0, 0, 1, 0, 0]; this.ops = []; }
    _mul(a, b, c, d, e, f) {
        const [A, B, C, D, E, F] = this.m;
        this.m = [A * a + C * b, B * a + D * b, A * c + C * d, B * c + D * d,
                  A * e + C * f + E, B * e + D * f + F];
    }
    translate(x, y) { this.ops.push("translate"); this._mul(1, 0, 0, 1, x, y); }
    scale(x, y) { this.ops.push("scale"); this._mul(x, 0, 0, y, 0, 0); }
    rotate(r) { this.ops.push("rotate"); this._mul(Math.cos(r), Math.sin(r), -Math.sin(r), Math.cos(r), 0, 0); }
    apply(p) {
        const [a, b, c, d, e, f] = this.m;
        return new Point(a * p.x + c * p.y + e, b * p.x + d * p.y + f);
    }
}

// -- load the source ----------------------------------------------------

const timers = [];
const fetches = [];
const context = createContext({
    console,
    Math, Number, Object, Promise, JSON, Set, Map, Array, Error, String, Boolean,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatched: [],
    setTimeout(fn, ms) { timers.push({ fn, ms, live: true }); return timers.length; },
    clearTimeout(id) { if (timers[id - 1]) timers[id - 1].live = false; },
    fetch(url, init) { fetches.push({ url, init }); return Promise.resolve({ ok: true }); },
    OpenSeadragon: { Point, Rect },
});
context.window = context;
context.globalThis = context;
context.dispatchEvent = (event) => context.dispatched.push(event);
runInContext(readFileSync(SOURCE, "utf-8"), context, { filename: SOURCE });
const VT = context.PlexoraViewTransform;
check("the service is exposed on window", typeof VT === "function");

function runTimers() {
    for (const timer of timers) {
        if (timer.live) { timer.live = false; timer.fn(); }
    }
}

// -- normalize ----------------------------------------------------------

for (const [input, expected] of [[0, 0], [360, 0], [-0, 0], [-90, 270], [725, 5], [12.5, 12.5],
                                  [-1e-13, 0], [NaN, 0], [Infinity, 0]]) {
    const got = VT.normalize(input);
    check(`normalize(${input}) is ${expected}`, Object.is(got, expected), { got });
}

// -- the flip mapping, as matrices ---------------------------------------

/** Our state as a 2x2 linear map about the centre: Fh^h . Fv^v . R(d). */
function ourMatrix({ degrees, flipH, flipV }) {
    const r = degrees * Math.PI / 180;
    let m = [Math.cos(r), Math.sin(r), -Math.sin(r), Math.cos(r)]; // column-major a b c d
    if (flipV) m = [m[0], -m[1], m[2], -m[3]];
    if (flipH) m = [-m[0], m[1], -m[2], m[3]];
    return m;
}

/** What OSD draws for (flipped, degrees): F . R. */
function osdMatrix({ flipped, degrees }) {
    return ourMatrix({ degrees, flipH: flipped, flipV: false });
}

for (const degrees of [0, 30, 90, 135, 180, 270, 359]) {
    for (const flipH of [false, true]) {
        for (const flipV of [false, true]) {
            const state = { degrees, flipH, flipV };
            const osd = VT.osdStateFor(state);
            const ours = ourMatrix(state);
            const theirs = osdMatrix(osd);
            check(`OSD draws what the state means at ${degrees}deg H=${flipH} V=${flipV}`,
                ours.every((v, i) => near(v, theirs[i], 1e-9)), { osd, ours, theirs });
            check(`OSD degrees stay in one turn at ${degrees} H=${flipH} V=${flipV}`,
                osd.degrees >= 0 && osd.degrees < 360, osd);
        }
    }
}

// A flip is on the SCREEN's axis: "horizontally" swaps left and right as
// seen, whatever the angle -- so the screen x of a point is negated about the
// centre and its y is untouched.
{
    const unflipped = ourMatrix({ degrees: 90, flipH: false, flipV: false });
    const flipped = ourMatrix({ degrees: 90, flipH: true, flipV: false });
    check("a horizontal flip mirrors screen x at 90 degrees",
        near(flipped[0], -unflipped[0]) && near(flipped[2], -unflipped[2])
        && near(flipped[1], unflipped[1]) && near(flipped[3], unflipped[3]));
}

// -- helpers agree with the drawer ----------------------------------------

const samples = [new Point(0.3, 0.2), new Point(0.75, 0.41), new Point(0.5, 0.3)];
for (const degrees of [0, 90, 33]) {
    for (const flipped of [false, true]) {
        const viewer = makeViewer({ degrees, flipped });
        for (const point of samples) {
            const drawn = drawnAt(viewer, point);
            const pixel = VT.pixelFromPoint(viewer, point);
            check(`pixelFromPoint lands where the drawer draws (${degrees}, flip=${flipped})`,
                near(pixel.x, drawn.x, 1e-6) && near(pixel.y, drawn.y, 1e-6), { pixel, drawn });
            const back = VT.pointFromPixel(viewer, drawn);
            check(`pointFromPixel undoes the drawer (${degrees}, flip=${flipped})`,
                near(back.x, point.x, 1e-9) && near(back.y, point.y, 1e-9), { back, point });

            const ctx = new RecordingContext();
            VT.orientContext(ctx, viewer);
            const oriented = ctx.apply(viewer.viewport.pixelFromPointNoRotate(point));
            check(`orientContext is the drawer's own transform (${degrees}, flip=${flipped})`,
                near(oriented.x, drawn.x, 1e-6) && near(oriented.y, drawn.y, 1e-6),
                { oriented, drawn, ops: ctx.ops });
        }
    }
}
{
    const ctx = new RecordingContext();
    VT.orientContext(ctx, makeViewer());
    check("orientContext does nothing to an upright, unmirrored view", ctx.ops.length === 0, ctx.ops);
}

// Image rect -> screen box, with a trivial item (image px == viewport units * 1000).
{
    const item = {
        imageToViewportCoordinates: (x, y) => new Point(x / 1000, y / 1000),
        viewportToImageCoordinates: (p) => new Point(p.x * 1000, p.y * 1000),
    };
    const viewer = makeViewer({ degrees: 90 });
    const rect = { x: 300, y: 200, width: 200, height: 100 };
    const box = VT.screenBoxOfImageRect(viewer, item, rect);
    // A 90-degree turn swaps the box's sides: 200 x 100 image px at 1000
    // px per unit and W/bounds.width = 1000 css px per unit is 100 wide, 200 high.
    check("screenBoxOfImageRect swaps sides at 90 degrees",
        near(box.width, 100, 1e-6) && near(box.height, 200, 1e-6), box);
    const back = VT.imageBoxOfScreenRect(viewer, item, box);
    check("imageBoxOfScreenRect inverts it",
        near(back.x, 300, 1e-6) && near(back.y, 200, 1e-6)
        && near(back.width, 200, 1e-6) && near(back.height, 100, 1e-6), back);
}

// -- the service --------------------------------------------------------

{
    const viewer = makeViewer();
    fetches.length = 0;
    const service = new VT(viewer, { datasource: "alpha" });
    const seen = [];
    const unsubscribe = service.subscribe((state) => seen.push(state));

    service.set({ degrees: 10 });
    service.set({ degrees: 20 });
    service.set({ degrees: 45 });
    check("a burst of changes does not save until it settles", fetches.length === 0, fetches.length);
    runTimers();
    check("a burst of changes is one save", fetches.length === 1, fetches.length);
    check("and it saves the last state",
        fetches[0] && JSON.parse(fetches[0].init.body).degrees === 45, fetches[0]);
    check("with PUT to the datasource's route",
        fetches[0]?.init?.method === "PUT" && fetches[0].url === "/view_transform/alpha", fetches[0]);
    check("every change reached the subscriber", seen.length === 3, seen.length);
    check("and the page event fired for each",
        context.dispatched.filter((e) => e.type === "plexora:view-transform-changed").length >= 3);

    service.set({ flipH: true });
    check("a flip leaves the angle alone", service.get().degrees === 45 && service.get().flipH);
    check("and is applied at once, never sprung",
        viewer.calls.setRotation.at(-1)?.[1] === true, viewer.calls.setRotation.at(-1));
    service.set({ flipV: true });
    check("a second flip leaves the first and the angle alone",
        service.get().flipH && service.get().flipV && service.get().degrees === 45, service.get());
    check("both flips are OSD's no-flip plus half a turn",
        viewer.viewport.flipped === false && near(viewer.viewport.rotation, 225), viewer.viewport);

    service.set({ degrees: 90 }, { immediately: false });
    check("a quick-select turn may animate", viewer.calls.setRotation.at(-1)?.[1] === false);

    service.set({ flipH: false }, { immediately: false });
    check("a flip asked to animate is applied at once anyway",
        viewer.calls.setRotation.at(-1)?.[1] === true, viewer.calls.setRotation.at(-1));
    service.set({ flipH: true }, { immediately: false });

    const before = seen.length;
    service.set({ degrees: 90 });
    check("setting the same state is not a change", seen.length === before);

    unsubscribe();
    service.set({ degrees: 180 });
    check("an unsubscribed listener hears nothing more", seen.length === before);

    runTimers();
    fetches.length = 0;
    service.adopt({ degrees: 270, flipH: false, flipV: true });
    runTimers();
    check("adopting a saved state never saves it back", fetches.length === 0, fetches.length);
    check("adopt applies it", service.get().degrees === 270 && service.get().flipV);

    service.reset();
    check("reset turns upright and keeps the flips",
        service.get().degrees === 0 && service.get().flipV, service.get());
}

// -- home ---------------------------------------------------------------

{
    const viewer = makeViewer({ W: 800, H: 400 });
    const service = new VT(viewer, { datasource: "" });
    viewer.viewport.goHome(true);
    check("upright, home is OSD's own", viewer.calls.goHome === 1 && viewer.calls.zoomTo.length === 0);

    service.set({ degrees: 90 });
    viewer.viewport.goHome(true);
    check("turned, home is not OSD's", viewer.calls.goHome === 1);
    // Home bounds 1 x 0.5 turned 90 degrees is a 0.5 x 1 box; the viewer is
    // 2:1, so the box is taller than the view. Fill matches the WIDTH: the
    // view is 0.5 wide.
    const [zoom] = viewer.calls.zoomTo.at(-1) || [];
    check("turned by a right angle, home still fills", near(zoom, 1 / 0.5, 1e-9), { zoom });
    check("home raises OSD's home event", viewer.calls.events.some(([n]) => n === "home"));

    viewer.viewport.homeFillsViewer = false;
    viewer.viewport.goHome(true);
    const [contain] = viewer.calls.zoomTo.at(-1) || [];
    // Contain: the box's height 1 must fit a view of height width/2, so the
    // view is 2 wide.
    check("and contains when the viewer asks for contain", near(contain, 1 / 2, 1e-9), { contain });

    viewer.viewport.homeFillsViewer = true;
    service.set({ degrees: 45 });
    viewer.viewport.goHome(true);
    const [tilted] = viewer.calls.zoomTo.at(-1) || [];
    const box = Math.SQRT1_2 * 1.5;
    // At 45 degrees the box is square, (1 + 0.5) / sqrt 2 on a side, and the
    // fit is contain: the height has to fit a view half as tall as it is wide.
    check("at an odd angle home contains", near(tilted, 1 / (box * 2), 1e-9), { tilted });

    const twice = new VT(viewer, { datasource: "" });
    check("the home wrap is installed once per viewport", viewer.viewport.__plexoraHomeWrapped === true && !!twice);
}

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.exitCode = failures.length ? 1 : 0;
