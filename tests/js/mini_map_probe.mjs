/**
 * Runs the real miniMap.js against a hand-rolled DOM/OpenSeadragon stand-in.
 *
 * Three things live here that nothing else in the suite can see: the colour
 * arithmetic (which has to agree with frag.glsl, in a second language), the
 * circle geometry (wrong geometry still draws a map, it just points at the
 * wrong place), and the lifecycle (a handler that outlives a collapse costs
 * per-frame work forever and is invisible until someone profiles).
 *
 *     node tests/js/mini_map_probe.mjs [--source <path>]
 *
 * Reports {checked, failures} as JSON on stderr so diagnostics never mix with
 * output.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import path from "node:path";

const args = process.argv.slice(2);
const sourceIndex = args.indexOf("--source");
const SOURCE = sourceIndex >= 0
    ? args[sourceIndex + 1]
    : path.resolve("plexora/client/src/js/views/miniMap.js");

const failures = [];
let checked = 0;

function check(name, condition, detail) {
    checked += 1;
    if (!condition) {
        failures.push(detail === undefined ? { name } : { name, detail });
    }
}

function near(a, b, tolerance = 1e-6) {
    return Math.abs(a - b) <= tolerance;
}

// -- the stand-ins ------------------------------------------------------

class FakeClassList {
    constructor() { this._set = new Set(); }
    add(name) { this._set.add(name); }
    remove(name) { this._set.delete(name); }
    contains(name) { return this._set.has(name); }
    toggle(name, force) {
        const on = force === undefined ? !this._set.has(name) : Boolean(force);
        if (on) this._set.add(name); else this._set.delete(name);
        return on;
    }
}

class FakeElement {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.textContent = "";
        this.style = {};
        this.children = [];
        this.classList = new FakeClassList();
        this.attributes = {};
        this.listeners = {};
        this.offsetWidth = 0;
        this.offsetHeight = 0;
        this._rect = { left: 0, top: 0, width: 0, height: 0 };
        this.width = undefined;
        this.height = undefined;
        this._captured = new Set();
    }
    appendChild(child) { this.children.push(child); return child; }
    setAttribute(name, value) { this.attributes[name] = value; }
    getAttribute(name) { return this.attributes[name]; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    dispatch(type, event) { (this.listeners[type] || []).forEach((fn) => fn(event)); }
    getBoundingClientRect() { return this._rect; }
    setPointerCapture(id) { this._captured.add(id); }
    hasPointerCapture(id) { return this._captured.has(id); }
    releasePointerCapture(id) { this._captured.delete(id); }
    getContext() { return this._context || (this._context = makeContext2d(this)); }
}

function makeContext2d(owner) {
    return {
        owner,
        fillStyle: "",
        imageSmoothingEnabled: false,
        imageSmoothingQuality: "",
        fills: [],
        draws: [],
        puts: [],
        fillRect(...a) { this.fills.push(a); },
        drawImage(...a) { this.draws.push(a); },
        putImageData(image) { this.puts.push(image); owner._lastPut = image; },
        getImageData(x, y, w, h) {
            return owner._imageDataToReturn || { data: new Uint8ClampedArray(w * h * 4), width: w, height: h };
        },
    };
}

class FakeImageData {
    constructor(width, height) {
        this.width = width;
        this.height = height;
        this.data = new Uint8ClampedArray(width * height * 4);
    }
}

function makeHarness(options = {}) {
    const width = options.width ?? 1000;
    const height = options.height ?? 500;
    const size = options.size ?? 220;

    const wrapper = new FakeElement("div");
    const created = [];
    const byId = { openseadragon_wrapper: wrapper };

    const viewerHandlers = {};
    const panCalls = [];
    const zoomCalls = [];
    let constraintCalls = 0;
    let bounds = { x: 0, y: 0, width: 1, height: height / width };

    const viewer = {
        addHandler(name, fn) { (viewerHandlers[name] ||= []).push(fn); },
        removeHandler(name, fn) {
            viewerHandlers[name] = (viewerHandlers[name] || []).filter((f) => f !== fn);
        },
        raise(name) { (viewerHandlers[name] || []).forEach((fn) => fn()); },
        viewport: {
            getBounds() { return bounds; },
            panTo(point, immediately) { panCalls.push({ point, immediately }); },
            zoomBy(factor, reference) { zoomCalls.push({ factor, reference }); },
            applyConstraints() { constraintCalls += 1; },
        },
    };

    const slots = options.slots || [];
    const imageViewer = {
        viewer,
        config: {
            width,
            height,
            imageData: options.imageData
                || slots.map((s) => ({ fullname: s.name, name: s.name })),
        },
        dataLayer: { getFullChannelName: (n) => n },
        getActiveLegendChannels: () => slots.filter((s) => s.enabled !== false),
    };

    const fetchCalls = [];
    const held = [];
    const sandbox = {
        console: { warn() {}, log() {}, error() {} },
        Math,
        Number,
        Array,
        Object,
        Set,
        Map,
        Promise,
        Float32Array,
        Uint8Array,
        Uint8ClampedArray,
        JSON,
        ImageData: FakeImageData,
        encodeURIComponent,
        setTimeout,
        datasource: "proj",
        plexoraUrl: (p) => `/${p}`,
        toFloatColor: (c) => [c.r / 255, c.g / 255, c.b / 255],
        d3: { color: (hex) => sandbox.__colors[hex] || null },
        __colors: options.colors || {},
        OpenSeadragon: { Point: function Point(x, y) { this.x = x; this.y = y; } },
        createImageBitmap: async () => ({ width: 4, height: 4, close() {} }),
        fetch: async (url) => {
            fetchCalls.push(url);
            const failFirst = options.failFirstFetchOnly && fetchCalls.length === 1;
            // Per channel, so a test can mix statuses -- which is the only way
            // to see whether the note reasons over all of them or just one.
            const named = options.failByName && options.failByName[String(url).split("/").pop()];
            if (named) {
                return { ok: false, status: named };
            }
            if (options.failFetch || failFirst) {
                return { ok: false, status: options.failStatus ?? 500 };
            }
            // Held open until releaseFetch(), so a test can look at the map
            // while one channel has already failed and another is still in
            // flight -- the window the note must stay quiet through.
            if (options.hangFetch) {
                await new Promise((resolve) => held.push(resolve));
            }
            return { ok: true, status: 200, blob: async () => ({}) };
        },
        document: {
            getElementById: (id) => byId[id] || null,
            createElement: (tag) => {
                const el = new FakeElement(tag);
                created.push(el);
                if (tag === "canvas") {
                    // Whatever the mini-map reads back from a decoded overview
                    // bitmap; one grey value per pixel, in the red byte.
                    const grey = options.grey || [0, 0, 0, 0, 0, 0, 0, 0,
                        0, 0, 0, 0, 0, 0, 0, 0];
                    const data = new Uint8ClampedArray(grey.length * 4);
                    grey.forEach((v, i) => { data[i * 4] = v; data[i * 4 + 1] = v; data[i * 4 + 2] = v; });
                    el._imageDataToReturn = { data, width: 4, height: 4 };
                }
                return el;
            },
        },
        window: {
            devicePixelRatio: options.dpr ?? 1,
            listeners: {},
            addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
            __plexora: { viewerSidebar: options.sidebar || null },
            flaskVariables: { datasource: "proj" },
        },
    };
    sandbox.window.window = sandbox.window;
    sandbox.globalThis = sandbox;

    const context = createContext(sandbox);
    runInContext(readFileSync(SOURCE, "utf-8"), context, { filename: SOURCE });
    runInContext("globalThis.__MiniMap = MiniMap;", context);

    const map = new sandbox.__MiniMap(imageViewer);
    // The stage's layout box is constant in both states by design, so the
    // harness can set it once.
    map.stage.offsetWidth = size;
    map.stage.offsetHeight = size;
    map.stage._rect = { left: 0, top: 0, width: size, height: size };

    return {
        map, viewer, viewerHandlers, panCalls, zoomCalls, fetchCalls, sandbox,
        get constraintCalls() { return constraintCalls; },
        setBounds(next) { bounds = next; },
        // The server comes back -- a restarted waitress, or a transient error
        // that stops recurring.
        recover() { options.failFetch = false; options.failFirstFetchOnly = false; },
        releaseFetch() { held.splice(0).forEach((resolve) => resolve()); },
        slots,
    };
}

const WHITE = { r: 255, g: 255, b: 255 };
const RED = { r: 255, g: 0, b: 0 };

// -- geometry -----------------------------------------------------------

{
    // A landscape image: the long side spans the full diameter, and the short
    // side is centred, so the corners -- background on any slide -- are what
    // border-radius clips.
    const h = makeHarness({ width: 1000, height: 500, size: 220 });
    h.map.expand();
    const g = h.map.geom;
    check("landscape: long side spans the diameter", near(g.drawWidth, 220));
    check("landscape: short side keeps the aspect", near(g.drawHeight, 110));
    check("landscape: image is centred vertically", near(g.offsetY, 55));
    check("landscape: no horizontal offset", near(g.offsetX, 0));

    const p = makeHarness({ width: 500, height: 1000, size: 220 });
    p.map.expand();
    check("portrait: long side spans the diameter", near(p.map.geom.drawHeight, 220));
    check("portrait: image is centred horizontally", near(p.map.geom.offsetX, 55));

    const sq = makeHarness({ width: 800, height: 800, size: 220 });
    sq.map.expand();
    check("square: fills the diameter both ways",
        near(sq.map.geom.drawWidth, 220) && near(sq.map.geom.drawHeight, 220));
}

{
    // The indicator has to be the viewport rectangle, and it has to be right
    // with ZERO channels on -- there is no world item to ask in that state,
    // which is why this does not go through viewportToImageRectangle.
    const h = makeHarness({ width: 1000, height: 500, size: 220, slots: [] });
    h.map.expand();
    const g = h.map.geom;

    h.setBounds({ x: 0, y: 0, width: 1, height: 0.5 });
    h.map._syncIndicator();
    check("home view: indicator covers the whole drawn image",
        near(parseFloat(h.map.indicator.style.width), g.drawWidth)
        && near(parseFloat(h.map.indicator.style.height), g.drawHeight),
        h.map.indicator.style);

    h.setBounds({ x: 0.25, y: 0.125, width: 0.5, height: 0.25 });
    h.map._syncIndicator();
    check("quarter view: indicator is a quarter, centred",
        near(parseFloat(h.map.indicator.style.left), g.offsetX + 0.25 * g.drawWidth)
        && near(parseFloat(h.map.indicator.style.top), g.offsetY + 0.25 * g.drawHeight)
        && near(parseFloat(h.map.indicator.style.width), 0.5 * g.drawWidth),
        h.map.indicator.style);

    // Zoomed further out than the image: OSD reports bounds beyond [0, 1] and
    // the indicator has to read "you can see everything", not spill into the
    // dead space around the image.
    h.setBounds({ x: -0.5, y: -0.25, width: 2, height: 1 });
    h.map._syncIndicator();
    check("zoomed out past the image: indicator is clamped to the image",
        near(parseFloat(h.map.indicator.style.left), g.offsetX)
        && near(parseFloat(h.map.indicator.style.width), g.drawWidth),
        h.map.indicator.style);
}

{
    // A drag maps back through the same transform it drew with: grab the
    // rectangle, move it n px, and the viewport centre moves n/drawWidth.
    const h = makeHarness({ width: 1000, height: 500, size: 220 });
    h.map.expand();
    h.setBounds({ x: 0.25, y: 0.125, width: 0.5, height: 0.25 });
    h.map._syncIndicator();

    const g = h.map.geom;
    const start = { clientX: g.offsetX + 0.5 * g.drawWidth, clientY: g.offsetY + 0.5 * g.drawHeight };
    h.map.stage.dispatch("pointerdown", {
        button: 0, pointerId: 7, clientX: start.clientX, clientY: start.clientY,
        target: h.map.indicator, preventDefault() {},
    });
    check("grabbing the rectangle does not itself pan", h.panCalls.length === 0, h.panCalls);

    h.map.stage.dispatch("pointermove", {
        pointerId: 7, clientX: start.clientX + 22, clientY: start.clientY,
    });
    const panned = h.panCalls[h.panCalls.length - 1];
    check("dragging pans by the dragged distance",
        panned && near(panned.point.x, 0.5 + 22 / g.drawWidth, 1e-9), panned && panned.point);
    check("a drag pans immediately rather than easing", panned && panned.immediately === true);

    h.map.stage.dispatch("pointerup", { pointerId: 7 });
    check("the gesture reapplies OSD's constraints when it ends", h.constraintCalls >= 1);

    // Clicking bare map recentres there.
    h.panCalls.length = 0;
    h.map.stage.dispatch("pointerdown", {
        button: 0, pointerId: 8,
        clientX: g.offsetX + 0.75 * g.drawWidth, clientY: g.offsetY + 0.25 * g.drawHeight,
        target: h.map.canvas, preventDefault() {},
    });
    const click = h.panCalls[0];
    check("clicking the map recentres on that point",
        click && near(click.point.x, 0.75) && near(click.point.y, 0.25 * g.aspect),
        click && click.point);

    // A pan target outside the image is clamped, not passed through.
    h.panCalls.length = 0;
    h.map._panToNormalized(1.8, -0.4, false);
    const clamped = h.panCalls[0];
    check("a pan target outside the image is clamped",
        clamped && near(clamped.point.x, 1) && near(clamped.point.y, 0), clamped && clamped.point);
}

{
    // The wheel is the one gesture that may change zoom, and it zooms about
    // the point under the cursor.
    const h = makeHarness({ width: 1000, height: 500, size: 220 });
    h.map.expand();
    const g = h.map.geom;
    h.map.stage.dispatch("wheel", {
        deltaY: -100,
        clientX: g.offsetX + 0.25 * g.drawWidth,
        clientY: g.offsetY + 0.5 * g.drawHeight,
        preventDefault() {},
    });
    const zoom = h.zoomCalls[0];
    check("the wheel zooms in on a negative delta", zoom && zoom.factor > 1, zoom && zoom.factor);
    check("the wheel zooms about the cursor",
        zoom && near(zoom.reference.x, 0.25) && near(zoom.reference.y, 0.5 * g.aspect),
        zoom && zoom.reference);
    check("the wheel does not scroll the page too", h.zoomCalls.length === 1);
}

// -- colour -------------------------------------------------------------

/** One composited pixel, read back out of the ImageData the map produced. */
async function composite(options) {
    const h = makeHarness(options);
    h.map.expand();
    await h.map._loading;
    const image = h.map._image;
    return {
        harness: h,
        pixel: (i) => [image.data[i * 4], image.data[i * 4 + 1], image.data[i * 4 + 2], image.data[i * 4 + 3]],
    };
}

{
    // The reference value, computed the way frag.glsl does it:
    //   t = clamp((byte/255 - lo) / (hi - lo)) ; rgb = colour * t * 0.9
    const grey = [0, 128, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    const { pixel } = await composite({
        width: 1000, height: 500,
        slots: [{ index: 0, name: "A", colorHex: "#ff0000", range: [0, 255], enabled: true }],
        colors: { "#ff0000": RED },
        grey,
    });
    const expected = Math.round((128 / 255) * 0.9 * 255);
    check("full range: a mid byte lands at the shader's value",
        pixel(1)[0] === expected, { got: pixel(1), expected });
    check("full range: red channel only", pixel(1)[1] === 0 && pixel(1)[2] === 0, pixel(1));
    check("a zero byte stays black", pixel(0)[0] === 0, pixel(0));
    check("the top of the range reaches the alpha ceiling",
        pixel(2)[0] === Math.round(0.9 * 255), pixel(2));
    check("the composite is opaque", pixel(1)[3] === 255, pixel(1));
}

{
    // A narrow window clips below its floor and stretches what is left.
    const grey = [64, 128, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    const { pixel } = await composite({
        width: 1000, height: 500,
        slots: [{ index: 0, name: "A", colorHex: "#ffffff", range: [128, 255], enabled: true }],
        colors: { "#ffffff": WHITE },
        grey,
    });
    check("below the window clips to black", pixel(0)[0] === 0, pixel(0));
    check("the window floor is the new black", pixel(1)[0] === 0, pixel(1));
    check("the window ceiling is the new white",
        pixel(2)[0] === Math.round(0.9 * 255), pixel(2));
}

{
    // Two channels add and saturate, which is what compositeOperation
    // "lighter" does in the main viewer.
    const grey = [255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    const { pixel } = await composite({
        width: 1000, height: 500,
        slots: [
            { index: 0, name: "A", colorHex: "#ff0000", range: [0, 255], enabled: true },
            { index: 1, name: "B", colorHex: "#00ff00", range: [0, 255], enabled: true },
        ],
        colors: { "#ff0000": RED, "#00ff00": { r: 0, g: 255, b: 0 } },
        grey,
    });
    check("two channels compose into both their colours",
        pixel(0)[0] === Math.round(0.9 * 255) && pixel(0)[1] === Math.round(0.9 * 255),
        pixel(0));
}

{
    // A zero-width window. GLSL's clamp absorbs the 0/0, but in JS it is NaN,
    // and NaN added into the shared accumulator poisons the pixel for EVERY
    // other channel too -- a Uint8ClampedArray then stores it as 0, so a
    // slider dragged to a single value blacks out the whole map with nothing
    // in the console to say why. Two channels are what make that visible: one
    // pinned to a zero-width window, one perfectly ordinary.
    const grey = [200, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    const { pixel } = await composite({
        width: 1000, height: 500,
        slots: [
            { index: 0, name: "A", colorHex: "#ff0000", range: [200, 200], enabled: true },
            { index: 1, name: "B", colorHex: "#00ff00", range: [0, 255], enabled: true },
        ],
        colors: { "#ff0000": RED, "#00ff00": { r: 0, g: 255, b: 0 } },
        grey,
    });
    check("a zero-width window stays finite at its floor",
        Number.isFinite(pixel(0)[0]) && pixel(0)[0] === 0, pixel(0));
    check("a zero-width window does not black out the channels beside it",
        pixel(0)[1] === Math.round((200 / 255) * 0.9 * 255), pixel(0));
    check("a zero-width window still lights everything above it",
        pixel(1)[0] === Math.round(0.9 * 255), pixel(1));
}

{
    // HD mode: slot.range arrives in raw 16-bit units, while these bytes were
    // quantized against [qmin, qmax]. Converting through the same window has
    // to land on the same pixel the equivalent byte range gives.
    // 200 sits strictly inside the window below. A byte ON the floor would be
    // black either way, and the comparison would pass on broken code.
    const grey = [200, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    const sidebar = {
        isHdMode: () => true,
        quantWindow: () => ({ qmin: 0, qmax: 4000 }),
        rawToByteRange: ([lo, hi], packet) => {
            const span = Math.max(packet.qmax - packet.qmin, 1);
            const toByte = (v) => Math.min(255, Math.max(0, Math.round(((v - packet.qmin) / span) * 255)));
            return [toByte(lo), toByte(hi)];
        },
    };
    // 2000 raw of a 0..4000 window is byte 128.
    const hd = await composite({
        width: 1000, height: 500,
        slots: [{ index: 0, name: "A", colorHex: "#ffffff", range: [2000, 4000], enabled: true }],
        colors: { "#ffffff": WHITE },
        grey, sidebar,
    });
    const plain = await composite({
        width: 1000, height: 500,
        slots: [{ index: 0, name: "A", colorHex: "#ffffff", range: [128, 255], enabled: true }],
        colors: { "#ffffff": WHITE },
        grey,
    });
    check("HD ranges convert into the byte domain the pixels are in",
        hd.pixel(0)[0] === plain.pixel(0)[0], { hd: hd.pixel(0), plain: plain.pixel(0) });
    check("the HD sample is actually inside its window",
        plain.pixel(0)[0] > 0 && plain.pixel(0)[0] < 255, plain.pixel(0));

    // A channel whose stats have not landed has no window to convert through.
    const noWindow = await composite({
        width: 1000, height: 500,
        slots: [{ index: 0, name: "A", colorHex: "#ffffff", range: [2000, 4000], enabled: true }],
        colors: { "#ffffff": WHITE },
        grey,
        sidebar: { isHdMode: () => true, quantWindow: () => null },
    });
    check("a channel with no quantization window yet is drawn, not blacked out",
        noWindow.pixel(0)[0] > 0, noWindow.pixel(0));
}

// -- lifecycle ----------------------------------------------------------

{
    const h = makeHarness({
        width: 1000, height: 500,
        slots: [{ index: 0, name: "A", colorHex: "#ffffff", range: [0, 255], enabled: true }],
        colors: { "#ffffff": WHITE },
    });

    check("collapsed: nothing is fetched", h.fetchCalls.length === 0, h.fetchCalls);
    check("collapsed: the canvas has no backing store",
        h.map.canvas.width === 0 && h.map.canvas.height === 0);

    h.map.invalidate({ refetch: true });
    check("collapsed: invalidate does not fetch", h.fetchCalls.length === 0, h.fetchCalls);

    // Handler count must not grow with expand/collapse cycles -- the whole
    // reason they are registered once and guarded rather than added/removed.
    const handlerCount = () =>
        Object.values(h.viewerHandlers).reduce((n, list) => n + list.length, 0);
    const before = handlerCount();
    for (let i = 0; i < 10; i += 1) {
        h.map.expand();
        h.map.collapse();
    }
    check("ten open/close cycles register no extra handlers",
        handlerCount() === before, { before, after: handlerCount() });

    // The animation handler exists from construction; raising it while
    // collapsed must do nothing observable. Checked after the cycles above,
    // because the geometry it would write only exists once the lens has been
    // opened -- which is also the state a leaked handler would be running in.
    h.map.indicator.style.left = "sentinel";
    h.viewer.raise("animation");
    h.viewer.raise("animation-finish");
    check("collapsed: an animation frame writes nothing",
        h.map.indicator.style.left === "sentinel", h.map.indicator.style.left);
}

{
    const slots = [
        { index: 0, name: "A", colorHex: "#ffffff", range: [0, 255], enabled: true },
        { index: 1, name: "B", colorHex: "#ffffff", range: [0, 255], enabled: true },
    ];
    const h = makeHarness({
        width: 1000, height: 500, slots, colors: { "#ffffff": WHITE },
        grey: [255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    });

    h.map.expand();
    await h.map._loading;
    check("expanding fetches one overview per active channel",
        h.fetchCalls.length === 2, h.fetchCalls);
    check("the overview url names the channel",
        h.fetchCalls[0].includes("generated/overview/proj/A"), h.fetchCalls[0]);

    h.map.collapse();
    h.map.expand();
    await h.map._loading;
    check("reopening refetches nothing", h.fetchCalls.length === 2, h.fetchCalls);

    // A colour change is served from cache.
    slots[0].colorHex = "#ff0000";
    h.sandbox.__colors["#ff0000"] = RED;
    h.map.invalidate();
    check("a colour change costs no request", h.fetchCalls.length === 2, h.fetchCalls);
    // Two channels are on and only A changed, so the map should now be red
    // plus white: red saturates while green keeps only B's contribution. If
    // the colour had been captured at fetch time the pixel would still be
    // neutral.
    check("a colour change is read at draw time, not captured at fetch time",
        h.map._image.data[0] > h.map._image.data[1]
        && h.map._image.data[1] === Math.round(0.9 * 255)
        && h.map._image.data[1] === h.map._image.data[2],
        Array.from(h.map._image.data.slice(0, 3)));

    // Adding a third channel fetches only the new one.
    slots.push({ index: 2, name: "C", colorHex: "#ffffff", range: [0, 255], enabled: true });
    h.map.imageViewer.config.imageData.push({ fullname: "C", name: "C" });
    await h.map._ensureData();
    check("adding a channel fetches only the new one",
        h.fetchCalls.length === 3, h.fetchCalls);

    // Removing one drops it from the composite without touching the cache.
    slots.pop();
    h.map.invalidate();
    check("removing a channel needs no request", h.fetchCalls.length === 3, h.fetchCalls);
}

{
    // A channel whose overview will not load must not take the map down.
    const h = makeHarness({
        width: 1000, height: 500,
        slots: [{ index: 0, name: "A", colorHex: "#ffffff", range: [0, 255], enabled: true }],
        colors: { "#ffffff": WHITE },
        failFetch: true,
    });
    h.map.expand();
    let threw = false;
    try {
        await h.map._loading;
    } catch (e) {
        threw = true;
    }
    check("a failing overview does not reject the rebuild", threw === false);
    check("a failing overview is not cached as if it worked", h.map._gray.size === 0);
    // A black circle reads as dark tissue. Nothing but this note distinguishes
    // "the overview failed" from "this slide is dim" for the person looking.
    check("a total failure says so instead of drawing nothing",
        h.map.note.textContent.length > 0 && h.map.root.classList.contains("has-note"),
        h.map.note.textContent);
    check("a non-404 failure does not blame the server version",
        !/restart/i.test(h.map.note.textContent), h.map.note.textContent);
}

{
    // 404 is the one failure with a single cause in practice: a waitress
    // process older than the route (no reloader, routes bound at import).
    const h = makeHarness({
        width: 1000, height: 500,
        slots: [{ index: 0, name: "A", colorHex: "#ffffff", range: [0, 255], enabled: true }],
        colors: { "#ffffff": WHITE },
        failFetch: true,
        failStatus: 404,
    });
    h.map.expand();
    await h.map._loading;
    check("404 tells the user to restart the server",
        /restart the Plexora server/i.test(h.map.note.textContent), h.map.note.textContent);

    // The indicator is hidden by CSS on .has-note; the class is the contract.
    check("the has-note class is what hides the indicator",
        h.map.root.classList.contains("has-note"));
}

{
    // Mixed statuses: one channel 404s, another 500s. "Restart the server" is
    // only true when the route is missing for everything -- a 500 in the pile
    // means something else is wrong and a restart will not fix it. This is
    // also the case a single last-error field cannot get right, since which
    // status survives is decided by whichever request finishes last.
    const h = makeHarness({
        width: 1000, height: 500,
        slots: [
            { index: 0, name: "A", colorHex: "#ffffff", range: [0, 255], enabled: true },
            { index: 1, name: "B", colorHex: "#ffffff", range: [0, 255], enabled: true },
        ],
        colors: { "#ffffff": WHITE },
        failByName: { A: 500, B: 404 },
    });
    h.map.expand();
    await h.map._loading;
    check("both failures are recorded, with their own statuses",
        h.map._failed.get(0) === 500 && h.map._failed.get(1) === 404,
        [...h.map._failed.entries()]);
    check("a 404 mixed with a 500 does not blame the server version",
        h.map.note.textContent.length > 0 && !/restart/i.test(h.map.note.textContent),
        h.map.note.textContent);
}

{
    // One channel failing while another works is a missing colour, not a
    // broken map -- and must stay silent.
    const h = makeHarness({
        width: 1000, height: 500,
        slots: [
            { index: 0, name: "A", colorHex: "#ffffff", range: [0, 255], enabled: true },
            { index: 1, name: "B", colorHex: "#ffffff", range: [0, 255], enabled: true },
        ],
        colors: { "#ffffff": WHITE },
        failFirstFetchOnly: true,
    });
    h.map.expand();
    await h.map._loading;
    check("a partial failure caches what did load", h.map._gray.size === 1, h.map._gray.size);
    check("a partial failure stays silent",
        h.map.note.textContent === "" && !h.map.root.classList.contains("has-note"),
        h.map.note.textContent);
}

{
    // Recovery: the server comes back and the map fills in. The note must not
    // outlive the condition it describes.
    const h = makeHarness({
        width: 1000, height: 500,
        slots: [{ index: 0, name: "A", colorHex: "#ffffff", range: [0, 255], enabled: true }],
        colors: { "#ffffff": WHITE },
        failFetch: true,
        failStatus: 404,
    });
    h.map.expand();
    await h.map._loading;
    check("the note is up before recovery", h.map.root.classList.contains("has-note"));
    h.recover();
    h.map.invalidate({ refetch: true });
    await h.map._loading;
    check("the note clears once an overview loads",
        h.map.note.textContent === "" && !h.map.root.classList.contains("has-note"),
        h.map.note.textContent);
}

{
    // Mid-load: one channel has already failed while another is still in
    // flight. Declaring failure here would flash an error over every slow
    // load that ends up working.
    const h = makeHarness({
        width: 1000, height: 500,
        slots: [
            { index: 0, name: "A", colorHex: "#ffffff", range: [0, 255], enabled: true },
            { index: 1, name: "B", colorHex: "#ffffff", range: [0, 255], enabled: true },
        ],
        colors: { "#ffffff": WHITE },
        failFirstFetchOnly: true,
        hangFetch: true,
    });
    h.map.expand();
    await new Promise((resolve) => setTimeout(resolve, 0));
    check("A has failed and B is still in flight",
        h.map._failed.size === 1 && h.map._pending.size === 1,
        { failed: h.map._failed.size, pending: h.map._pending.size });
    check("no note while another channel is still loading",
        h.map.note.textContent === "", h.map.note.textContent);

    h.releaseFetch();
    await h.map._loading;
    check("and none once the survivor lands",
        h.map.note.textContent === "" && h.map._gray.size === 1,
        h.map.note.textContent);
}

// -- report -------------------------------------------------------------

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.exit(failures.length ? 1 : 0);
