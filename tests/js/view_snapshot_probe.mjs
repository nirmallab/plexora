/**
 * "What was on screen", captured and put back: services/viewSnapshot.js, and
 * the `immediately` option it hands to services/viewerScene.js.
 *
 * A region drawn by hand or with magic select remembers the view it was drawn
 * under, and magic select decides from the same record whether a second click
 * may reuse the first click's encoded picture. What is worth pinning is where
 * that record could quietly lie:
 *
 *   - a channel whose window cannot be converted to raw yet recorded with a
 *     byte pair mislabelled as raw (it must be recorded without a range);
 *   - more than eight channels, or hidden and disabled ones;
 *   - `sameView` calling a different picture the same -- another sample, HD
 *     mode, a colour or a window changed, a real zoom -- or calling a nudge of
 *     the view a different one (every cache miss is a slow encode);
 *   - a region click that jumps instead of animating, because the snapshot's
 *     restore lost `{immediately: false}` on its way to OpenSeadragon;
 *   - restoring HD mode by firing a change event that was not needed (it
 *     reloads every channel).
 *
 * The real scripts in a vm realm with a stand-in viewer, sidebar and
 * PlexoraViewerScene (and, for the last section, a stand-in OpenSeadragon
 * viewport that records its calls).
 *
 * Run directly: `node tests/js/view_snapshot_probe.mjs`
 * Prints `ok  <check>` per check held; exit 1 if any did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SERVICES = join(REPO, "plexora", "client", "src", "js", "services");

// -- the realm ------------------------------------------------------------

class Event {
    constructor(type, init) { this.type = type; this.bubbles = Boolean(init?.bubbles); }
}

const hdBox = {
    checked: false,
    events: [],
    dispatchEvent(event) { this.events.push(event.type); return true; },
};

const sceneCalls = [];
const scene = {
    box: { x: 1000, y: 2000, w: 800.123, h: 600.456 },
    scale: 0.2500049,
    currentViewport() { return { ...this.box }; },
    scaleOf() { return this.scale; },
    restoreViewport(...args) { sceneCalls.push(args); return true; },
};

const window = {
    __plexora: null,
    PlexoraViewerScene: scene,
    flaskVariables: { datasource: "sample-A" },
};
const document = { getElementById: (id) => (id === "viewer_controls_hd" ? hdBox : null) };
const context = createContext({ window, document, Event, console, Date, URLSearchParams });
runInContext(readFileSync(join(SERVICES, "viewSnapshot.js"), "utf8"), context,
             { filename: "viewSnapshot.js" });
const Snap = window.PlexoraViewSnapshot;

/** A channel panel shaped like viewerSidebar: slots, raw windows, HD mode. */
function panel({ hd = false, unconvertible = [] } = {}) {
    const slots = [
        { name: "DNA", enabled: true, visible: true, colorHex: "#0000FF", range: [1, 2] },
        { name: "CD3", enabled: true, colorHex: "#ff0000" },
        { name: "off", enabled: false, colorHex: "#00ff00" },
        { name: "hidden", enabled: true, visible: false, colorHex: "#00ff00" },
        { name: "", enabled: true, colorHex: "#00ff00" },
        { name: "CD8", enabled: true, colorHex: "not-a-colour" },
        { name: "CD20", enabled: true, colorHex: "#ffffff" },
        { name: "PanCK", enabled: true, colorHex: "#ffff00" },
        { name: "Ki67", enabled: true, colorHex: "#ff00ff" },
        { name: "CD68", enabled: true, colorHex: "#00ffff" },
        { name: "SMA", enabled: true, colorHex: "#888888" },
        { name: "ninth", enabled: true, colorHex: "#111111" },
    ];
    return {
        channelSlots: slots,
        isHdMode: () => hd,
        quantWindow: (name) => (unconvertible.includes(name) ? null : { low: 0, high: 1 }),
        toRawRangeForSlot: (slot) => [100.12345, 5000 + slot.name.length],
    };
}

const viewer = { viewer: {} };

/** Equal values across the vm's realm (its objects have other prototypes). */
function same(actual, expected) {
    assert.equal(JSON.stringify(actual), JSON.stringify(expected));
}

const failures = [];
function check(name, fn) {
    try {
        fn();
        console.log(`ok  ${name}`);
    } catch (error) {
        failures.push(name);
        console.log(`FAIL  ${name}\n      ${String(error && error.message || error).split("\n").join("\n      ")}`);
    }
}

// -- capture ----------------------------------------------------------------

check("capture records the sample, viewport, zoom, HD mode and channels, rounded", () => {
    const shot = Snap.capture({ viewer, panel: panel(), sample: "sample-A" });
    same(Object.keys(shot), ["version", "sample", "viewport", "zoom", "hd_mode", "channels",
                             "captured_at"]);
    assert.equal(shot.version, Snap.VERSION);
    assert.equal(shot.sample, "sample-A");
    same(shot.viewport, { x: 1000, y: 2000, width: 800.12, height: 600.46 });
    assert.equal(shot.zoom, 0.25);
    assert.equal(shot.hd_mode, false);
    assert.equal(typeof shot.captured_at, "string");
    same(shot.channels[0], { name: "DNA", visible: true, color: "#0000FF",
                             range: [100.123, 5003] });
});

check("only enabled, visible, named channels, at most eight, a bad colour left off", () => {
    const shot = Snap.capture({ viewer, panel: panel() });
    same(shot.channels.map((c) => c.name),
         ["DNA", "CD3", "CD8", "CD20", "PanCK", "Ki67", "CD68", "SMA"]);
    assert.equal(shot.channels.length, 8);
    assert.equal("color" in shot.channels[2], false);
});

check("the sample falls back to the page's datasource", () => {
    assert.equal(Snap.capture({ viewer, panel: panel() }).sample, "sample-A");
});

check("a window that cannot be converted to raw is recorded without a range", () => {
    const shot = Snap.capture({ viewer, panel: panel({ unconvertible: ["CD3"] }) });
    const cd3 = shot.channels.find((c) => c.name === "CD3");
    same(cd3, { name: "CD3", visible: true, color: "#ff0000" });
    assert.ok(shot.channels.find((c) => c.name === "DNA").range);
});

check("...but in HD mode the slot's window is raw already and is kept", () => {
    const shot = Snap.capture({ viewer, panel: panel({ hd: true, unconvertible: ["CD3"] }) });
    assert.equal(shot.hd_mode, true);
    same(shot.channels.find((c) => c.name === "CD3").range, [100.123, 5003]);
});

check("HD mode is read off the checkbox when the panel cannot say", () => {
    const bare = { channelSlots: [] };
    hdBox.checked = true;
    assert.equal(Snap.capture({ viewer, panel: bare }).hd_mode, true);
    hdBox.checked = false;
    assert.equal(Snap.capture({ viewer, panel: bare }).hd_mode, false);
});

check("with no viewer open there is no viewport or zoom, and nothing throws", () => {
    const shot = Snap.capture({ panel: panel() });
    assert.equal(shot.viewport, null);
    assert.equal(shot.zoom, null);
});

// -- forStorage ---------------------------------------------------------------

check("forStorage spells out width and height and drops the version", () => {
    const stored = Snap.forStorage({ version: 1, sample: "s", viewport: { x: 1, y: 2, w: 30, h: 40 },
                                     zoom: 0.5, hd_mode: true, channels: [{ name: "DNA" }],
                                     captured_at: "t" });
    same(stored, { channels: [{ name: "DNA" }], sample: "s",
                   viewport: { x: 1, y: 2, width: 30, height: 40 }, zoom: 0.5, hd_mode: true,
                   captured_at: "t" });
    assert.equal("version" in stored, false);
    assert.equal(Snap.forStorage(null), null);
});

// -- sameView -----------------------------------------------------------------

const BASE = {
    sample: "s", hd_mode: false,
    viewport: { x: 1000, y: 1000, width: 1000, height: 800 },
    channels: [{ name: "DNA", color: "#0000ff", range: [10, 900] }],
};
const variant = (change) => ({ ...JSON.parse(JSON.stringify(BASE)), ...change });

check("sameView: a 0.5 % pan is the same picture", () => {
    assert.equal(Snap.sameView(BASE, variant({ viewport: { x: 1005, y: 1004, width: 1000,
                                                             height: 800 } })), true);
});

check("sameView: a 15 % zoom is not", () => {
    assert.equal(Snap.sameView(BASE, variant({ viewport: { x: 1000, y: 1000, width: 1150,
                                                             height: 920 } })), false);
});

check("sameView: a changed window, colour, HD mode or sample is not", () => {
    assert.equal(Snap.sameView(BASE, variant({ channels: [{ name: "DNA", color: "#0000ff",
                                                              range: [10, 950] }] })), false);
    assert.equal(Snap.sameView(BASE, variant({ channels: [{ name: "DNA", color: "#ff0000",
                                                              range: [10, 900] }] })), false);
    assert.equal(Snap.sameView(BASE, variant({ hd_mode: true })), false);
    assert.equal(Snap.sameView(BASE, variant({ sample: "t" })), false);
});

check("sameView: colour case does not matter, and w/h spellings compare", () => {
    assert.equal(Snap.sameView(BASE, variant({ channels: [{ name: "DNA", color: "#0000FF",
                                                              range: [10, 900] }],
                                               viewport: { x: 1000, y: 1000, w: 1000, h: 800 } })),
                 true);
    assert.equal(Snap.sameView(BASE, null), false);
});

check("key is equal for equal views and differs across channels", () => {
    assert.equal(Snap.key(BASE), Snap.key(variant({})));
    assert.notEqual(Snap.key(BASE), Snap.key(variant({ channels: [] })));
    assert.equal(Snap.key(null), "");
});

// -- contains -----------------------------------------------------------------

check("contains: every point inside the viewport, edges included", () => {
    assert.equal(Snap.contains(BASE, [{ x: 1000, y: 1000 }, { x: 2000, y: 1800 },
                                      { x: 1500, y: 1400 }]), true);
    assert.equal(Snap.contains(BASE, [{ x: 1500, y: 1400 }, { x: 2001, y: 1400 }]), false);
    assert.equal(Snap.contains({}, [{ x: 0, y: 0 }]), false);
});

// -- restoring ----------------------------------------------------------------

check("restoreViewport animates by default: {immediately: false} reaches the scene", () => {
    sceneCalls.length = 0;
    const done = Snap.restoreViewport({ viewport: { x: 10, y: 20, width: 300, height: 200 } },
                                      { viewer });
    assert.equal(done, true);
    assert.equal(sceneCalls.length, 1);
    const [imageViewer, config, box, options] = sceneCalls[0];
    assert.equal(imageViewer, viewer);
    assert.equal(config, undefined);
    same(box, { x: 10, y: 20, w: 300, h: 200 });
    same(options, { immediately: false });
});

check("...and immediately when asked; a degenerate viewport moves nothing", () => {
    sceneCalls.length = 0;
    Snap.restoreViewport({ viewport: { x: 0, y: 0, w: 5, h: 5 } }, { viewer, immediately: true });
    same(sceneCalls[0][3], { immediately: true });
    sceneCalls.length = 0;
    assert.equal(Snap.restoreViewport({ viewport: { x: 0, y: 0, width: 0, height: 5 } },
                                      { viewer }), false);
    assert.equal(Snap.restoreViewport({}, { viewer }), false);
    assert.equal(sceneCalls.length, 0);
});

check("restoreHdMode flips the checkbox and fires one change only when it differs", () => {
    hdBox.checked = false;
    hdBox.events = [];
    assert.equal(Snap.restoreHdMode({ hd_mode: false }), false);
    same(hdBox.events, []);
    assert.equal(Snap.restoreHdMode({ hd_mode: true }), true);
    assert.equal(hdBox.checked, true);
    same(hdBox.events, ["change"]);
    assert.equal(Snap.restoreHdMode({ hd_mode: true }), false);
    assert.equal(Snap.restoreHdMode({}), false);
    same(hdBox.events, ["change"]);
});

// -- viewerScene.restoreViewport's `immediately` --------------------------------

class Point { constructor(x, y) { this.x = x; this.y = y; } }
class Rect { constructor(x, y, width, height) { Object.assign(this, { x, y, width, height }); } }

function recordingViewer() {
    const calls = [];
    const item = {
        imageToViewportCoordinates: (x, y) => new Point(x / 1000, y / 1000),
        imageToViewportRectangle: (r) => new Rect(r.x / 1000, r.y / 1000, r.width / 1000,
                                                  r.height / 1000),
    };
    const viewport = {
        getAspectRatio: () => 4 / 3,
        panTo: (...args) => calls.push(["panTo", args[args.length - 1]]),
        zoomTo: (...args) => calls.push(["zoomTo", args[args.length - 1]]),
        fitBounds: (...args) => calls.push(["fitBounds", args[args.length - 1]]),
    };
    return { calls, imageViewer: { viewer: { viewport, world: { getItemAt: () => item } },
                                   config: {} } };
}

const sceneContext = createContext({ Math, Number, Object, Boolean, JSON, Error, console,
                                     OpenSeadragon: { Point, Rect } });
runInContext(readFileSync(join(SERVICES, "viewerScene.js"), "utf8"), sceneContext,
             { filename: "viewerScene.js" });
const RealScene = sceneContext.PlexoraViewerScene;
const UPRIGHT = { x: 100, y: 100, w: 400, h: 300 };
const TURNED = { ...UPRIGHT, orientation: { degrees: 90, flip_h: false, flip_v: false,
                                            frame_w: 300, frame_h: 400 } };

check("viewerScene.restoreViewport jumps by default (fitBounds told true)", () => {
    const { calls, imageViewer } = recordingViewer();
    assert.equal(RealScene.restoreViewport(imageViewer, null, UPRIGHT), true);
    same(calls, [["fitBounds", true]]);
});

check("...and animates with {immediately: false}, upright and turned", () => {
    let { calls, imageViewer } = recordingViewer();
    RealScene.restoreViewport(imageViewer, null, UPRIGHT, { immediately: false });
    same(calls, [["fitBounds", false]]);
    ({ calls, imageViewer } = recordingViewer());
    RealScene.restoreViewport(imageViewer, null, TURNED, { immediately: false });
    same(calls, [["panTo", false], ["zoomTo", false]]);
    ({ calls, imageViewer } = recordingViewer());
    RealScene.restoreViewport(imageViewer, null, TURNED);
    same(calls, [["panTo", true], ["zoomTo", true]]);
});

process.exit(failures.length ? 1 : 0);
