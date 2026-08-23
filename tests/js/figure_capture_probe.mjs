/**
 * The arithmetic a captured panel is only as good as.
 *
 * Everything here is pure and nothing else in the suite executes any of it: the
 * Python tests render HTML and stop, and `node --check` sees syntax only. So a
 * capture that stores screen coordinates instead of image coordinates, a
 * Shift-square that is square on screen but not in the image, or a preview crop
 * that is off by a device-pixel-ratio would all ship with a green suite -- and
 * the symptom would be a figure that exports at the wrong region, or at the
 * resolution of the monitor it was built on.
 *
 * The viewer is synthetic and deliberately simple: a 4000x3000 image drawn into
 * an 800x600 element, so every expected number below can be worked out by hand
 * and checked by a reader.
 *
 * Run directly:
 *   node tests/js/figure_capture_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
const SCRIPTS = ["figureSchema.js", "figureSceneSnapshot.js", "figureCaptureTool.js",
    "figureCaptureBoxes.js", "figureCaptureDock.js"];

const IMAGE_WIDTH = 4000;
const IMAGE_HEIGHT = 3000;
const ELEMENT_WIDTH = 800;
const ELEMENT_HEIGHT = 600;

const problems = [];
const drawCalls = [];
//: How far the whole projection is slid sideways. Zero unless a section is
//: asking what happens when the viewer moves under a locked frame.
let screenShift = 0;

function canvasStub(backingWidth, backingHeight, cssRect) {
    return {
        width: backingWidth,
        height: backingHeight,
        getBoundingClientRect: () => cssRect,
        getContext: () => ({
            save() {}, restore() {}, fillRect() {}, strokeRect() {}, setLineDash() {},
            drawImage(...args) { drawCalls.push(args.slice(1)); },
        }),
        toBlob(callback) { callback({ type: "image/webp", size: 128 }); },
    };
}

/** The two canvases a real viewer stacks: the tile drawer at device
 *  resolution, and an overlay at CSS resolution. Their backing stores differ,
 *  which is the case the crop has to get right. */
const DRAWER = canvasStub(1600, 1200, { left: 0, top: 0, width: 800, height: 600 });
const OVERLAY = canvasStub(800, 600, { left: 0, top: 0, width: 800, height: 600 });

const CONTAINER = {
    style: {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
    querySelectorAll: () => [DRAWER, OVERLAY],
};

/** A viewer whose mapping is one multiplication, so the expected numbers below
 *  can be read straight off the fixture. */
function syntheticViewer(useGetImagePixel) {
    const item = {
        source: useGetImagePixel ? {
            // Already full-resolution, extraZoomLevels included -- the path
            // viewerManager installs on every channel.
            getImagePixel: (_item, position) => [
                (position.x / ELEMENT_WIDTH) * IMAGE_WIDTH,
                (position.y / ELEMENT_HEIGHT) * IMAGE_HEIGHT,
            ],
        } : {},
        viewportToImageCoordinates: (point) => ({
            x: point.x * IMAGE_WIDTH,
            y: point.y * IMAGE_HEIGHT,
        }),
        imageToViewportRectangle: (rect) => ({
            getTopLeft: () => ({ x: rect.x / IMAGE_WIDTH, y: rect.y / IMAGE_HEIGHT }),
            getBottomRight: () => ({
                x: (rect.x + rect.width) / IMAGE_WIDTH,
                y: (rect.y + rect.height) / IMAGE_HEIGHT,
            }),
        }),
    };
    return {
        world: { getItemAt: () => item },
        viewport: {
            pointFromPixel: (position) => ({
                x: position.x / ELEMENT_WIDTH, y: position.y / ELEMENT_HEIGHT,
            }),
            pixelFromPoint: (point) => ({
                x: point.x * ELEMENT_WIDTH + screenShift, y: point.y * ELEMENT_HEIGHT,
            }),
        },
        canvas: CONTAINER,
        addHandler() {}, removeHandler() {},
    };
}

/** What `ctx.viewer` actually is: core's ImageViewer, which OWNS the
 *  OpenSeadragon instance at `.viewer`. The capture tool reaches through to the
 *  latter for coordinates; the snapshot asks the former about layers. Getting
 *  the two the wrong way round is a real mistake and this fixture is where it
 *  shows up. */
function syntheticImageViewer(useGetImagePixel) {
    return {
        viewer: syntheticViewer(useGetImagePixel),
        cellLayers: () => [
            { name: "cell_explorer", mode: "filled", opacity: 0.7, visible: true },
        ],
        show_scalebar: true,
    };
}

function browserGlobals() {
    const globals = {
        console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
        Date, Promise, Error, TypeError, Infinity, URLSearchParams,
        setTimeout: () => 1, clearTimeout: () => {},
        requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
        CustomEvent: class CustomEvent {
            constructor(type, init) { this.type = type; this.detail = (init || {}).detail; }
        },
        OpenSeadragon: {
            Rect: class Rect {
                constructor(x, y, width, height) {
                    Object.assign(this, { x, y, width, height });
                }
            },
            // viewport.pointFromPixel does arithmetic through the point's own
            // methods, so the tool has to hand it a real one -- a bare {x, y}
            // throws, and only on the longhand path, which is the one no
            // developer's machine takes.
            Point: class Point {
                constructor(x, y) { this.x = x; this.y = y; }
            },
        },
        document: {
            readyState: "complete",
            // Settable, because the capture shortcut has to stand down while
            // the user is typing and that is the only way to say they are.
            activeElement: null,
            // The one core element the snapshot reads directly.
            getElementById: (id) => (id === "viewer_controls_hd" ? { checked: true } : null),
            createElement: (tag) => (tag === "canvas"
                ? canvasStub(0, 0, { left: 0, top: 0, width: 0, height: 0 })
                : { style: {}, appendChild() {}, remove() {} }),
            addEventListener() {}, removeEventListener() {},
        },
    };
    globals.window = {
        devicePixelRatio: 2,
        crypto: { randomUUID: () => "0123456789abcdef0123456789abcdef" },
        addEventListener() {}, removeEventListener() {},
        // Dispatched by FigureScene.pluginStates; one plugin answers, so the
        // opaque-state contract is exercised rather than assumed.
        dispatchEvent: (event) => {
            if (event.type === "plexora:figure-capture-state") {
                event.detail.contribute("roi", {
                    version: "test", state: { enabled: true }, legend: [{ label: "Tumor" }],
                });
            }
            return true;
        },
        __plexora: {
            viewerSidebar: {
                channelSlots: [
                    { name: "DNA", enabled: true, color: { r: 0, g: 0, b: 255 }, range: [0, 255] },
                    { name: "CD8", enabled: true, color: { r: 255, g: 0, b: 0 }, range: [4, 200] },
                    { name: "CD3", enabled: false, color: { r: 0, g: 255, b: 0 }, range: [0, 255] },
                    { name: "", enabled: true, color: { r: 1, g: 1, b: 1 }, range: [0, 1] },
                ],
                // Byte-domain slider values converted back to raw 16-bit, which
                // is the only form the scene is allowed to store.
                toRawRangeForSlot: (slot) => [slot.range[0] * 257, slot.range[1] * 257],
            },
        },
    };
    globals.crypto = globals.window.crypto;
    return globals;
}

const ctx = createContext(browserGlobals());
for (const name of SCRIPTS) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}

// The plugin context a real capture tool is handed.
ctx.__ctx = {
    config: {
        width: IMAGE_WIDTH,
        height: IMAGE_HEIGHT,
        extraZoomLevels: 0,
        imageData: [
            { name: "DNA", fullname: "DNA_full", src: "/generated/data/demo/demo_0/" },
            { name: "CD8", fullname: "CD8_full", src: "/generated/data/demo/demo_1/" },
            { name: "CD3", fullname: "CD3_full", src: "/generated/data/demo/demo_2/" },
        ],
    },
    dataLayer: { getFullChannelName: (short) => short + "_full" },
    viewer: syntheticImageViewer(true),
};
ctx.__syntheticImageViewer = syntheticImageViewer;
// Slides the whole projection sideways, which is what a viewer that is still
// settling -- or one the user has panned -- does to every region on screen.
ctx.__shift = (pixels) => { screenShift = pixels; };

function check(label, actual, expected) {
    const a = JSON.stringify(actual);
    const b = JSON.stringify(expected);
    if (a !== b) problems.push(`${label}: expected ${b}, got ${a}`);
}

function run(expression) {
    return runInContext(`(() => { ${expression} })()`, ctx);
}

// -- screen -> full-resolution image pixels ------------------------------

const viaSource = run(`
    const tool = new FigureCaptureTool(__ctx, {});
    return tool.toImage({ x: 400, y: 300 });
`);
check("toImage through the tile source", viaSource, [2000, 1500]);

// The longhand path, for a source that predates getImagePixel -- and with
// extraZoomLevels set, which is the divide that is easy to leave out and
// impossible to notice while it is zero.
const viaLonghand = run(`
    const config = { ...__ctx.config, extraZoomLevels: 1 };
    const tool = new FigureCaptureTool(
        { ...__ctx, config, viewer: __syntheticImageViewer(false) }, {});
    return tool.toImage({ x: 400, y: 300 });
`);
check("toImage longhand honours extraZoomLevels", viaLonghand, [1000, 750]);

// -- the rectangle -------------------------------------------------------

const rects = run(`
    const tool = new FigureCaptureTool(__ctx, {});
    return {
        forward: tool.rectBetween([100, 200], [300, 500], false),
        backward: tool.rectBetween([300, 500], [100, 200], false),
        square: tool.rectBetween([100, 200], [300, 500], true),
        squareUpLeft: tool.rectBetween([300, 500], [100, 200], true),
    };
`);
check("a forward drag", rects.forward, { x: 100, y: 200, w: 200, h: 300 });
// A drag up and to the left describes the same rectangle -- the anchor is a
// corner, not the origin.
check("a backward drag", rects.backward, { x: 100, y: 200, w: 200, h: 300 });
// Shift squares in IMAGE pixels, taking the longer side, so a row of square
// panels really is the same physical field.
check("Shift squares in image pixels", rects.square, { x: 100, y: 200, w: 300, h: 300 });
check("Shift squares up and to the left", rects.squareUpLeft,
    { x: 0, y: 200, w: 300, h: 300 });

const clamped = run(`
    const tool = new FigureCaptureTool(__ctx, {});
    return {
        inside: tool.clamp({ x: 100, y: 100, w: 200, h: 200 }),
        overhang: tool.clamp({ x: 3900, y: 2900, w: 400, h: 400 }),
        negative: tool.clamp({ x: -50, y: -50, w: 100, h: 100 }),
    };
`);
check("a capture inside the image is untouched", clamped.inside,
    { x: 100, y: 100, w: 200, h: 200 });
check("a capture running off the edge is trimmed", clamped.overhang,
    { x: 3900, y: 2900, w: 100, h: 100 });
check("a capture starting off the edge is pulled in", clamped.negative,
    { x: 0, y: 0, w: 100, h: 100 });

// -- image rectangle -> the pixels it occupies on screen -----------------

const screenRect = run(`
    const tool = new FigureCaptureTool(__ctx, {});
    return tool.toScreenRect({ x: 1000, y: 750, w: 2000, h: 1500 });
`);
check("toScreenRect", screenRect, { x: 200, y: 150, width: 400, height: 300 });

// -- the preview crop ----------------------------------------------------

drawCalls.length = 0;
const preview = run(`
    const tool = new FigureCaptureTool(__ctx, {});
    const canvas = tool.grabPreview({ x: 100, y: 50, width: 200, height: 150 });
    return { width: canvas.width, height: canvas.height };
`);
// devicePixelRatio 2, and well under the preview edge cap, so the output is the
// crop at device resolution.
check("preview size", preview, { width: 400, height: 300 });
// Each canvas is cropped through its OWN backing scale: the drawer is 2x, the
// overlay 1x. A shared devicePixelRatio would put the overlay in the wrong
// place on exactly the setups where an overlay matters.
check("the drawer is cropped at its own 2x backing scale",
    drawCalls[0], [200, 100, 400, 300, 0, 0, 400, 300]);
check("the overlay is cropped at its own 1x backing scale",
    drawCalls[1], [100, 50, 200, 150, 0, 0, 400, 300]);

// -- the scene snapshot --------------------------------------------------

const scene = run(`
    return FigureScene.capture(__ctx, "src_1", { x: 1000, y: 750, w: 2000, h: 1500 });
`);
check("the viewport is stored in full-resolution image pixels",
    scene.viewport, { x: 1000, y: 750, w: 2000, h: 1500 });
// Only enabled slots with a marker, identified by the URL key rather than by a
// name the user can change.
check("channels are keyed by their stable URL key",
    scene.channels.map((c) => c.key), ["demo_0", "demo_1"]);
check("the name at capture rides along for the legend",
    scene.channels.map((c) => c.fullname_at_capture), ["DNA_full", "CD8_full"]);
// Raw 16-bit, whatever domain the slider was in.
check("windows are stored in raw 16-bit units",
    scene.channels.map((c) => c.window), [[0, 65535], [1028, 51400]]);
check("channel colours are stored", scene.channels[1].color, { r: 255, g: 0, b: 0 });
check("core overlays are recorded", scene.core_overlays, {
    cell_layers: [{ name: "cell_explorer", mode: "filled", opacity: 0.7, visible: true, z: 0 }],
    hd_tiles: true,
    scalebar_visible: true,
});
check("a plugin's state and legend are carried opaquely",
    scene.plugins, { roi: { version: "test", state: { enabled: true }, legend: [{ label: "Tumor" }] } });
check("the snapshot is versioned", scene.snapshot_version, 1);

// Nothing about the UI. A snapshot that recorded which panel was expanded would
// make two identical captures compare unequal and restore a layout the user has
// since rearranged.
const sceneKeys = Object.keys(scene).sort();
check("the snapshot holds only what changes the rendering", sceneKeys,
    ["captured_at", "channels", "core_overlays", "plugins", "snapshot_version",
     "source_id", "viewport"]);

// -- the viewfinder ------------------------------------------------------
//
// The frame is the feature: one rectangle, several shots, so a row of panels
// really is the same field seen in different places. All of it is arithmetic,
// and every mistake in it produces a set of panels that quietly disagree about
// size -- which is the defect this replaced a per-panel drag to avoid.

const boxes = run(`
    const bounds = { width: 800, height: 600 };
    return {
        preset: FigureCaptureTool.defaultBox(bounds),
        pushedIn: FigureCaptureTool.clampBox({ x: 700, y: 560, width: 300, height: 200 }, bounds),
        tiny: FigureCaptureTool.clampBox({ x: 10, y: 10, width: 2, height: 2 }, bounds),
        moved: FigureCaptureTool.moveBox({ x: 100, y: 100, width: 200, height: 150 }, 40, -30, bounds),
        stopped: FigureCaptureTool.moveBox({ x: 100, y: 100, width: 200, height: 150 }, 900, 900, bounds),
        resized: FigureCaptureTool.resizeBox({ x: 100, y: 100, width: 200, height: 150 }, "nw", 20, 10, bounds),
        floored: FigureCaptureTool.resizeBox({ x: 100, y: 100, width: 200, height: 150 }, "nw", 400, 400, bounds),
    };
`);
// 46% of the shorter side, 4:3, centred.
check("capture mode opens with a frame in the middle", boxes.preset,
    { x: 262, y: 197, width: 276, height: 207 });
// Fully inside, never merely overlapping: the shutter and the handles are ON
// the frame, and a frame half off the edge is one the user can see and cannot
// reach.
check("a frame is pushed back inside the viewer", boxes.pushedIn,
    { x: 500, y: 400, width: 300, height: 200 });
check("a frame cannot be smaller than a frame", boxes.tiny,
    { x: 10, y: 10, width: 12, height: 12 });
check("a frame moves by the drag", boxes.moved,
    { x: 140, y: 70, width: 200, height: 150 });
check("a frame dragged off the edge stops at it", boxes.stopped,
    { x: 600, y: 450, width: 200, height: 150 });
// The opposite corner is the anchor and does not move, which is what makes a
// resize feel like a resize rather than like a nudge.
check("resizing from the north-west holds the south-east corner", boxes.resized,
    { x: 120, y: 110, width: 180, height: 140 });
check("a corner dragged past its anchor stops rather than inverting", boxes.floored,
    { x: 288, y: 238, width: 12, height: 12 });

const framed = run(`
    const tool = new FigureCaptureTool(__ctx, {});
    return {
        rect: tool.imageRectFor({ x: 200, y: 150, width: 400, height: 300 }),
        overhang: tool.imageRectFor({ x: -100, y: -100, width: 400, height: 300 }),
    };
`);
check("the frame becomes a region in full-resolution image pixels", framed.rect,
    { x: 1000, y: 750, w: 2000, h: 1500 });
check("a frame hanging off the image is pulled in", framed.overhang,
    { x: 0, y: 0, w: 2000, h: 1500 });

// The property the whole design rests on. The frame is held in SCREEN pixels,
// so panning the image underneath it moves WHICH part is captured and not HOW
// MUCH -- four panels from four places, all the same size. Held in image
// pixels instead, the frame would travel with the image and the second capture
// would be of the first one's region.
const panned = run(`
    const shifted = __syntheticImageViewer(true);
    shifted.viewer.world.getItemAt().source.getImagePixel =
        (_item, position) => [position.x * 5 + 500, position.y * 5];
    const tool = new FigureCaptureTool({ ...__ctx, viewer: shifted }, {});
    return tool.imageRectFor({ x: 200, y: 150, width: 400, height: 300 });
`);
check("the same frame over a panned image captures the same amount of it",
    { w: panned.w, h: panned.h }, { w: framed.rect.w, h: framed.rect.h });
check("...of a different part of it", { x: panned.x, y: panned.y }, { x: 1500, y: 750 });

// -- the two one-key shortcuts -------------------------------------------
//
// C opens capture mode and S takes the shot. Both are bare letters, and the
// shutter's is a bare letter for a reason worth pinning: it used to be Enter,
// which activates whatever button has focus -- and by the time anyone presses
// it the focus is on the last thing they clicked, so the same keystroke fired
// the shutter, or the mode toggle, or both, depending on where their hand had
// last been. A letter only ever reaches the document handler.

const shortcut = run(`
    const test = (init) => FigureCaptureDock.isShortcut(init);
    const shot = (init) => FigureCaptureTool.isShootKey(init);
    document.activeElement = null;
    const answer = {
        plain: test({ key: "c" }),
        upper: test({ key: "C" }),
        chord: test({ key: "c", metaKey: true }),
        other: test({ key: "v" }),
        shoot: shot({ key: "s" }),
        shootUpper: shot({ key: "S" }),
        shootChord: shot({ key: "s", metaKey: true }),
        shootEnter: shot({ key: "Enter" }),
        distinct: FigureCaptureTool.SHOOT_KEY !== FigureCaptureDock.SHORTCUT,
        crossed: shot({ key: FigureCaptureDock.SHORTCUT }),
    };
    document.activeElement = { tagName: "INPUT" };
    answer.typing = test({ key: "c" });
    answer.shootTyping = shot({ key: "s" });
    document.activeElement = null;
    return answer;
`);
check("c toggles capture mode", shortcut.plain, true);
check("so does C", shortcut.upper, true);
// Cmd-C is copy and always will be.
check("a modifier chord is not the shortcut", shortcut.chord, false);
check("another letter is not the shortcut", shortcut.other, false);
// Otherwise naming a figure "cell cores" toggles capture mode four times.
check("nothing fires while the user is typing", shortcut.typing, false);

check("s takes the shot", shortcut.shoot, true);
check("so does S", shortcut.shootUpper, true);
// Cmd-S is save. And Enter is what this replaced, so it must not still work:
// leaving it bound would keep the flaky path alive next to the reliable one.
check("a modifier chord does not take the shot", shortcut.shootChord, false);
check("Enter no longer takes the shot", shortcut.shootEnter, false);
// Otherwise naming a figure "cross sections" takes three photographs.
check("nothing is taken while the user is typing", shortcut.shootTyping, false);
// One key, one job: a letter that did both would leave capture mode and take a
// shot on its way out, in an order nothing on screen explains.
check("the two shortcuts are different keys", shortcut.distinct, true);
check("...and neither answers to the other's", shortcut.crossed, false);

// -- how much room the dock gets -----------------------------------------
//
// The strip grows with every capture and the channel legend sits in the same
// corner below it, so the dock takes the room that is free above the legend
// rather than a fixed slice of the window -- which would be wrong on every
// window except the one it was chosen on. Pure arithmetic, checked here.

const room = run(`
    const D = FigureCaptureDock;
    return {
        clear: D.roomFor(900, null),
        // A legend 120px tall in a 900px viewer sits 48px off the bottom, so
        // its top is at 732.
        below: D.roomFor(900, 732),
        // More channels, a taller legend, less room -- with no code change.
        taller: D.roomFor(900, 600),
        squeezed: D.roomFor(400, 180),
        floor: D.MIN_HEIGHT,
        gap: D.GAP,
        margin: D.MARGIN,
    };
`);
check("with nothing below it the dock has the viewer's height", room.clear, 876);
check("a legend below it stops the dock above the legend", room.below, 710);
check("...and a taller legend leaves less room", room.taller, 578);
// Something has to give when neither fits, and it is not the orb: a dock
// squeezed below this is one nobody can capture with, which is worse than a
// dock overlapping a legend in the one case where nothing else fits.
check("the dock is never squeezed below a usable height", room.squeezed, room.floor);
// The clearance is real space rather than a coincidence of rounding: the dock
// has to stop visibly short of the legend, not touch it.
check("it keeps a gap rather than butting up against it",
    room.below + room.gap + room.margin, 732);


// -- the capture boxes ---------------------------------------------------
//
// The other half of the pair, and the exact opposite of the viewfinder: a box
// records a region OF THE IMAGE, so it has to travel with the image while the
// frame stays put. The two live in the same corner of the same screen and mean
// opposite things, and either one held the other way round looks fine until the
// moment it matters.

const marks = run(`
    const bounds = { width: 800, height: 600 };
    return {
        onScreen: FigureCaptureBoxes.placement({ x: 200, y: 150, width: 400, height: 300 }, bounds),
        straddling: FigureCaptureBoxes.placement({ x: -100, y: -50, width: 400, height: 300 }, bounds),
        offLeft: FigureCaptureBoxes.placement({ x: -500, y: 0, width: 400, height: 300 }, bounds),
        offRight: FigureCaptureBoxes.placement({ x: 900, y: 0, width: 400, height: 300 }, bounds),
        pinprick: FigureCaptureBoxes.placement({ x: 10, y: 10, width: 0.2, height: 0.2 }, bounds),
    };
`);
check("a box in view is placed where it is", marks.onScreen,
    { left: 200, top: 150, width: 400, height: 300 });
// Kept whole and left to the container's overflow to cut: trimming the
// rectangle here would move the outline off the region it marks.
check("a box half off the edge keeps its shape", marks.straddling,
    { left: -100, top: -50, width: 400, height: 300 });
// #openseadragon_wrapper does not clip, so a box that is nowhere has to be
// drawn nowhere -- a div a mile wide would otherwise be laid over the sidebar.
check("a box off to the left is not drawn at all", marks.offLeft, null);
check("a box off to the right is not drawn at all", marks.offRight, null);
// Zoomed far out, a whole field is a couple of pixels. It stays a mark rather
// than collapsing to nothing.
check("a box smaller than a pixel is still a mark", marks.pinprick,
    { left: 10, top: 10, width: 1, height: 1 });

// Going back to a capture and taking another one has to land on the SAME image
// pixels -- that is the whole claim behind "change the channels and capture the
// same field again". So aiming the frame at a region and asking what region the
// frame is over must give the region back, unchanged.
const aimed = run(`
    const tool = new FigureCaptureTool(__ctx, {});
    const region = { x: 1000, y: 750, w: 2000, h: 1500 };
    tool.arm();
    const landed = tool.aimAt(region);
    return { landed, box: tool.box, back: tool.imageRectFor(tool.box) };
`);
check("the frame lands on the region it was aimed at", aimed.box,
    { x: 200, y: 150, width: 400, height: 300 });
check("...and the region it is over is the one it was given", aimed.back,
    { x: 1000, y: 750, w: 2000, h: 1500 });

// Going back to a capture does not slam the viewer into it edge to edge. The
// region gets about half the window, so what surrounds it -- and the capture's
// own outline, which is how the user knows they have arrived somewhere they
// have been -- are still on screen.
const context = run(`
    return {
        framing: FigureCaptureBoxes.contextRect({ x: 1000, y: 750, w: 2000, h: 1500 }),
        factor: FigureCaptureBoxes.CONTEXT,
    };
`);
check("going back frames the region with room around it", context.framing,
    { x: 0, y: 0, w: 4000, h: 3000 });
// Grown about its centre, not its corner: about the corner the capture would
// arrive in the corner of the window instead of the middle of it.
check("...centred on the region, not hung off it",
    { cx: context.framing.x + context.framing.w / 2,
      cy: context.framing.y + context.framing.h / 2 },
    { cx: 2000, cy: 1500 });

// -- the pinned frame ----------------------------------------------------
//
// Selecting a capture locks the frame onto it, and while it is locked the
// shutter takes THAT region rather than reading a fresh one off the screen.
// Without the lock the second capture of a field lands a pixel or two from the
// first -- close enough to look right and wrong in the file, which is the worst
// of the available outcomes.

const pinned = run(`
    const region = { x: 1000, y: 750, w: 2000, h: 1500 };
    const elsewhere = { x: 0, y: 0, w: 400, h: 300 };
    const tool = new FigureCaptureTool(__ctx, {});
    tool.arm();
    const answer = {};

    // Locks what the frame is already on...
    tool.aimAt(region);
    answer.took = tool.pinTo(region, "Capture 1");
    answer.region = tool.pinned;
    // ...and shoots the stored numbers rather than the screen's reading of them.
    answer.shot = tool.imageRectFor(tool.box);
    answer.pinnedShot = tool.clamp(tool.pinned);

    // ...but never moves the frame to do it: a frame the user set up somewhere
    // else stays where they put it, unlocked.
    const free = new FigureCaptureTool(__ctx, {});
    free.arm();
    free.setBox({ x: 40, y: 40, width: 120, height: 90 });
    answer.refused = free.pinTo(elsewhere, "Capture 2");
    answer.frameKept = free.box;

    // Aiming somewhere else lets go, and says so.
    let released = 0;
    const watched = new FigureCaptureTool(__ctx, { onUnpin: () => { released += 1; } });
    watched.arm();
    watched.aimAt(region);
    watched.pinTo(region, "Capture 1");
    watched.setBox({ x: 10, y: 10, width: 100, height: 100 });
    answer.released = released;
    answer.stillPinned = watched.pinned;
    return answer;
`);
check("a frame already on the region locks onto it", pinned.took, true);
check("the lock holds the region in image pixels", pinned.region,
    { x: 1000, y: 750, w: 2000, h: 1500 });
// The two readings agree here because nothing has moved -- which is exactly
// why taking the shot from the pin costs nothing and guarantees everything.
check("what the pin shoots is the region it was given", pinned.pinnedShot, pinned.shot);
check("a frame that is not on the region does not lock", pinned.refused, false);
check("...and is left exactly where the user put it", pinned.frameKept,
    { x: 40, y: 40, width: 120, height: 90 });
// Aiming the frame somewhere else is the user saying they want a new region,
// and the strip and the boxes have to stop claiming otherwise.
check("aiming somewhere else lets go of the capture", pinned.stillPinned, null);
check("...and announces it once", pinned.released, 1);

// -- the frame follows the region it is locked to ------------------------
//
// The lock is on a REGION, and the frame is only how that region is shown. So
// when the viewer moves the frame goes with it, and lets go only when the
// region can no longer be shown as a frame at all. That is what makes going
// back to a capture survive the movement nobody asked for -- OpenSeadragon is
// still settling for a while after it says it has stopped -- and it is what
// keeps a following frame from being drawn outside the viewer, since
// #openseadragon_wrapper does not clip.

const following = run(`
    const region = { x: 1000, y: 750, w: 2000, h: 1500 };
    let released = 0;
    const tool = new FigureCaptureTool(__ctx, { onUnpin: () => { released += 1; } });
    tool.arm();

    // One call aims AND locks, from a single reading of where the region is.
    const answer = { locked: tool.lockOn(region, "Capture 1"),
        box: tool.box, pinned: tool.pinned };

    // The viewer settles a few pixels after it said it had finished. The frame
    // goes with it rather than treating three pixels as "the user left".
    __shift(3);
    tool.checkPin();
    answer.nudged = { box: tool.box, pinned: tool.pinned, released };

    // Navigating away: the region cannot be framed inside the viewer any more,
    // so the lock goes -- and the frame stays where it last sat.
    __shift(700);
    tool.checkPin();
    answer.gone = { box: tool.box, pinned: tool.pinned, released };
    __shift(0);
    return answer;
`);
check("one call puts the frame on the region and locks it there",
    { locked: following.locked, box: following.box },
    { locked: true, box: { x: 200, y: 150, width: 400, height: 300 } });
// The failure this replaced: aimAt then pinTo read where the region was twice,
// a moment apart, and the viewer had moved between the two readings -- so the
// lock was refused on the capture the user had just clicked.
check("a few pixels of settling does not cost the lock",
    { pinned: following.nudged.pinned, released: following.nudged.released },
    { pinned: { x: 1000, y: 750, w: 2000, h: 1500 }, released: 0 });
check("...the frame follows the region instead", following.nudged.box,
    { x: 203, y: 150, width: 400, height: 300 });
check("a region that can no longer be framed lets go", following.gone.pinned, null);
check("...and says so once", following.gone.released, 1);
// Left where it last sat, not dragged to the edge: "one frame, four places" is
// what the frame is for the moment it stops being locked to anything.
check("...leaving the frame where it was", following.gone.box,
    { x: 203, y: 150, width: 400, height: 300 });


// -- one shot per arming -------------------------------------------------
//
// Capture mode changes what a drag on the image does, so it lasts exactly as
// long as the thing it is for. The shot ends it, and the user comes back with
// C when they want another -- rather than going off to adjust the picture with
// the gesture for that still redrawing a viewfinder.

const spent = run(`
    const region = { x: 1000, y: 750, w: 2000, h: 1500 };
    const taken = [];
    const tool = new FigureCaptureTool(__ctx, { onCapture: (rect) => taken.push(rect) });
    tool.arm();
    tool.aimAt(region);
    tool.pinTo(region, "Capture 1");

    const answer = { shot: tool.shoot(), count: taken.length, armed: tool.active };
    // A second press with the mode off takes nothing: no second identical panel
    // from a key held a moment too long.
    tool.shoot();
    answer.after = taken.length;
    // Arming again comes back to the SAME region, still locked -- so
    // capture, adjust, capture is one keystroke a lap and lands on one region.
    tool.arm();
    answer.again = { armed: tool.active, box: tool.box, pinned: tool.pinned };
    return answer;
`);
check("the shutter takes the region it was aimed at", spent.shot,
    { x: 1000, y: 750, w: 2000, h: 1500 });
check("...once", spent.count, 1);
check("and the shot ends capture mode", spent.armed, false);
check("a second press with the mode off takes nothing", spent.after, 1);
// disarm() keeps the frame's geometry and the pin on purpose: losing them here
// would make every capture after the first a fresh freehand rectangle.
check("arming again brings the frame back", spent.again,
    { armed: true, box: { x: 200, y: 150, width: 400, height: 300 },
      pinned: { x: 1000, y: 750, w: 2000, h: 1500 } });

console.error(JSON.stringify(
    { problems, drawCalls, scene, boxes, framed, shortcut, room, marks, aimed,
      context, pinned, following, spent },
    null, 2));
process.exit(problems.length ? 1 : 0);
