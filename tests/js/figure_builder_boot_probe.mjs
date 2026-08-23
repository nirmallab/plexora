/**
 * Does the Figure Builder plugin's client actually come up?
 *
 * A plugin's client is the list of plain <script> tags named by
 * `PLUGIN.scripts` (plexora/api/plugin.py), and nothing in the Python suite ever
 * runs them: pytest renders the panel's HTML and stops there. So the whole
 * client can be broken -- a file missing from the tuple, a class that throws the
 * moment it is constructed, a registration that never happens -- and every
 * server-side test still passes, with the failure appearing only as a panel that
 * renders and does nothing.
 *
 * This plugin adds a wrinkle the others do not have: two of its files SELF-BOOT
 * at the bottom, because they drive pages rather than tool panels. Those boots
 * run in this probe too, against a DOM where their root elements do not exist --
 * which is the case that matters, since every file loads on all three pages
 * and each controller has to stand down politely on the two that are not its
 * own. A controller that assumed its DOM would take the viewer's panel down with
 * it.
 *
 * What this checks, in the order it matters:
 *   1. every declared file parses and runs;
 *   2. each one defines the global the others reach for;
 *   3. the self-booting page controllers no-op when their page is absent;
 *   4. exactly one plugin registers, under the right name;
 *   5. a sidebar controller can be built from a real plugin context;
 *   6. the mm/label arithmetic the canvas depends on is right, since it is
 *      pure and nothing else in the suite executes it.
 *
 * The list is passed in by the Python test, which reads it off the descriptor,
 * so this probes what the server will really send.
 *
 * Run directly:
 *   node tests/js/figure_builder_boot_probe.mjs figureBuilderApi.js ...
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
const SCRIPTS = process.argv.slice(2);

const registered = [];
const requests = [];

/** A browser stand-in no wider than what these files touch at load time. */
function browserGlobals() {
    const node = () => ({
        style: {}, dataset: {}, files: [],
        classList: { add() {}, remove() {}, toggle() {} },
        append() {}, appendChild() {}, remove() {}, setAttribute() {},
        getAttribute: () => null, addEventListener() {}, click() {},
    });
    const timers = new Map();
    let nextTimer = 1;
    let clockNow = 0;
    const globals = {
        console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
        Date, Promise, Error, TypeError, Uint8Array, Infinity, URLSearchParams,
        // A clock the probe drives by hand. Going back to a capture waits for
        // the viewer to be QUIET rather than for it to say it has finished
        // moving, so a setTimeout that never fires would leave the landing
        // hanging -- and a real one would make the test wait out a debounce.
        setTimeout: (fn, ms) => {
            const id = nextTimer;
            nextTimer += 1;
            timers.set(id, { fn: fn, at: clockNow + (ms || 0) });
            return id;
        },
        clearTimeout: (id) => { timers.delete(id); },
        //: Advance the clock, running whatever comes due in the order it does.
        __tick: (ms) => {
            clockNow += ms;
            for (;;) {
                const due = Array.from(timers.entries())
                    .filter(([, t]) => t.at <= clockNow)
                    .sort((a, b) => a[1].at - b[1].at);
                if (!due.length) return;
                for (const [id, timer] of due) {
                    timers.delete(id);
                    timer.fn();
                }
            }
        },
        requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
        fetch: async (url) => {
            requests.push(String(url));
            return { ok: true, status: 200, json: async () => ({}) };
        },
        Blob: class Blob {},
        URL: { createObjectURL: () => "blob:x", revokeObjectURL: () => {} },
        CustomEvent: class CustomEvent {
            constructor(type, init) { this.type = type; this.detail = (init || {}).detail; }
        },
        OpenSeadragon: {
            Rect: class Rect {
                constructor(x, y, width, height) {
                    Object.assign(this, { x, y, width, height });
                }
            },
            Point: class Point {
                constructor(x, y) { this.x = x; this.y = y; }
            },
        },
        document: {
            // readyState is deliberately not "loading": the self-booting files
            // then run their boot immediately, in this probe, which is the
            // whole point of checking them here.
            readyState: "complete",
            getElementById: () => null,
            querySelectorAll: () => [],
            querySelector: () => null,
            createElement: () => node(),
            addEventListener() {}, removeEventListener() {},
            body: node(),
            get activeElement() { return null; },
            title: "",
        },
    };
    globals.window = {
        Plexora: { registerPlugin: (definition) => registered.push(definition) },
        // Distinct on every call, unlike a constant: ids that collide are a
        // real failure mode -- a batch of captures whose panels all share one
        // id is a batch the server rejects -- and a stub that never varies
        // cannot see it.
        crypto: {
            randomUUID: (() => {
                let n = 0;
                return () => (n += 1).toString(16).padEnd(32, "0");
            })(),
        },
        PlexoraStatus: { begin: () => ({ done() {}, fail() {} }), track: async (l, p) => p },
        localStorage: {
            getItem: () => null, setItem() {}, removeItem() {},
        },
        plexoraUrl: (path) => "/" + String(path || "").replace(/^\/+/, ""),
        addEventListener() {}, removeEventListener() {},
        alert() {}, confirm: () => false, prompt: () => null,
        location: { href: "" },
    };
    // The files read `window.crypto` and bare `document`/`fetch` alike, which is
    // what a classic script sees in a browser.
    globals.crypto = globals.window.crypto;
    globals.plexoraUrl = globals.window.plexoraUrl;
    return globals;
}

const ctx = createContext(browserGlobals());
const problems = [];
const loaded = [];

for (const name of SCRIPTS) {
    try {
        runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
        loaded.push(name);
    } catch (error) {
        problems.push(`${name} failed to load: ${error.message}`);
        break;   // everything after it would fail for the same reason
    }
}

const report = { order: SCRIPTS, loaded, registered: [], problems };

if (!problems.length) {
    // Loading without throwing is not the same as working: a file can define
    // its class fine and still refer to a name that does not exist yet when the
    // class is actually used.
    runInContext(
        "globalThis.__names = { FigureBuilderApi: typeof FigureBuilderApi,"
        + " FigureSchema: typeof FigureSchema,"
        + " FigureScene: typeof FigureScene,"
        + " FigureCaptureTool: typeof FigureCaptureTool,"
        + " FigureCaptureBoxes: typeof FigureCaptureBoxes,"
        + " FigureCaptureDock: typeof FigureCaptureDock,"
        + " FigureDocumentState: typeof FigureDocumentState,"
        + " FigureCanvas: typeof FigureCanvas,"
        + " FigureExportUi: typeof FigureExportUi,"
        + " FigureLibrary: typeof FigureLibrary,"
        + " FigureWorkspace: typeof FigureWorkspace,"
        + " FigureBuilderSidebarController: typeof FigureBuilderSidebarController };",
        ctx);
    for (const [name, kind] of Object.entries(ctx.__names)) {
        if (kind === "undefined") problems.push(`${name} was never defined`);
    }

    // The page controllers already ran their boot when their file loaded, with
    // no root element present. Reaching here at all means neither threw; this
    // pins that they answered "not my page" rather than half-initialising.
    try {
        runInContext(
            "globalThis.__booted = { library: FigureLibrary.boot(), workspace: FigureWorkspace.boot() };",
            ctx);
        if (ctx.__booted.library !== null || ctx.__booted.workspace !== null) {
            problems.push("a page controller booted on a page that is not its own");
        }
    } catch (error) {
        problems.push(`a page controller threw when its page was absent: ${error.message}`);
    }

    if (registered.length !== 1) {
        problems.push(`expected exactly one plugin registration, got ${registered.length}`);
    } else if (registered[0].name !== "figure_builder") {
        problems.push(`registered under the wrong name: ${registered[0].name}`);
    } else if (registered[0].ownsCellLayer !== false) {
        problems.push("figure_builder must not claim the cell layer");
    }

    try {
        runInContext(
            "globalThis.__built = (() => {"
            + " const c = new FigureBuilderSidebarController({ url: (p) => '/' + p,"
            + "   datasource: 'demo', config: { width: 100, height: 100 },"
            + "   viewer: null, onCleanup() {} });"
            + " c.setup();"
            + " return { datasource: c.datasource, figureId: c.figureId,"
            + "          hasApi: typeof c.api.listFigures === 'function' }; })();",
            ctx);
    } catch (error) {
        problems.push(`a controller could not be built: ${error.message}`);
    }

    // Capture mode does not need a figure. Asked as a behaviour rather than
    // read off the source, because "the button is enabled" and "the tool arms"
    // are different claims and only the second one is the feature.
    try {
        runInContext(
            "globalThis.__armed = (() => {"
            + " const osd = { addHandler() {}, removeHandler() {}, canvas: { style: {},"
            + "   getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }) } };"
            + " const c = new FigureBuilderSidebarController({ url: (p) => '/' + p,"
            + "   datasource: 'demo', config: { width: 4000, height: 3000 },"
            + "   viewer: { viewer: osd }, onCleanup() {} });"
            + " c.toggleCapture();"
            + " const withNoFigure = c.capture.active && c.figureId === null;"
            + " c.toggleCapture();"
            + " const offAgain = c.capture.active === false;"
            + " c.editing = { panelId: 'pnl_1' };"
            + " c.toggleCapture();"
            + " const whileEditing = c.capture.active;"
            + " return { withNoFigure, offAgain, whileEditing }; })();",
            ctx);
        const armed = ctx.__armed;
        if (!armed.withNoFigure) {
            problems.push("capture mode refused to arm without a figure");
        }
        if (!armed.offAgain) problems.push("the toggle did not stand capture mode down");
        // The viewer is showing a panel's borrowed scene while an edit session
        // runs. A capture taken then is a panel of somebody else's view, and
        // nothing on screen would say so.
        if (armed.whileEditing) {
            problems.push("capture mode armed while a panel's view was loaded");
        }
        report.armed = armed;
    } catch (error) {
        problems.push(`capture mode threw: ${error.message}`);
    }

    // Going back to a capture. Two claims, and the second one is the whole
    // reason the first is useful: the viewer moves to the region, and NOTHING
    // about the rendering is put back. A "go back" that restored the captured
    // channels would make "same field, different rendering" -- two panels of
    // one region under two colour schemes -- impossible to reach by the obvious
    // route, and would silently rewrite the user's colours while it did it.
    try {
        runInContext(
            "globalThis.__selection = (() => {"
            + " const fitted = []; let restores = 0; let shift = 0;"
            + " const handlers = {};"
            + " const item = { source: { getImagePixel: (_i, p) => [p.x * 5, p.y * 5] },"
            + "   imageToViewportRectangle: (r) => ({ r, getTopLeft: () => ({ x: r.x / 4000, y: r.y / 3000 }),"
            + "     getBottomRight: () => ({ x: (r.x + r.width) / 4000, y: (r.y + r.height) / 3000 }) }) };"
            + " const osd = { addHandler(n, f) { (handlers[n] = handlers[n] || []).push(f); },"
            + "   removeHandler(n, f) { handlers[n] = (handlers[n] || []).filter((h) => h !== f); },"
            + "   world: { getItemAt: () => item },"
            + "   viewport: { fitBounds: (b) => fitted.push(b.r),"
            + "     pixelFromPoint: (p) => ({ x: p.x * 800 + shift, y: p.y * 600 }) },"
            + "   canvas: { style: {}, getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }) } };"
            + " const fire = (n) => (handlers[n] || []).slice().forEach((fn) => fn());"
            // How the viewer really arrives, and the whole point of this
            // fixture: it says `animation-finish` and then MOVES AGAIN, because
            // OpenSeadragon pulls the viewport back inside its constraints
            // afterwards. Landing the frame on the first of those two arrivals
            // put it three pixels off the region and the lock -- which is what
            // the selection is -- broke before the user could see it was made.
            + " const arrive = () => { shift = 40; fire('animation');"
            + "   shift = 3; fire('animation-finish');"
            + "   shift = 0; fire('animation'); };"
            + " const c = new FigureBuilderSidebarController({ url: (p) => '/' + p,"
            + "   datasource: 'demo', config: { width: 4000, height: 3000 },"
            + "   viewer: { viewer: osd }, onCleanup() {} });"
            + " const restore = FigureScene.restore;"
            + " FigureScene.restore = async () => { restores += 1; return null; };"
            + " c.captures = [{ id: 'cap_1', panelId: null, url: null,"
            + "     scene: { viewport: { x: 1000, y: 750, w: 2000, h: 1500 } } },"
            + "   { id: 'cap_2', panelId: 'pnl_9', url: null,"
            + "     scene: { viewport: { x: 0, y: 0, w: 400, h: 300 } } }];"
            // NOT armed first. Clicking a capture is what arms it -- everything
            // going back to a region does is invisible with the mode off.
            + " const armedBefore = c.capture.active;"
            + " c.selectCapture('cap_1');"
            // Armed and highlighted the instant it is clicked, before the
            // viewer has gone anywhere: the click has to look like it did
            // something while the flight is still in the air.
            + " const atOnce = { armed: c.capture.active, selected: c.selected };"
            + " arrive(); __tick(200);"
            + " const answer = { armedBefore, atOnce, armedAfter: c.capture.active,"
            + "   selected: c.selected, fitted: fitted.length, restores,"
            + "   frame: c.capture.box, pinned: c.capture.pinned, unattached: c.unattached(),"
            + "   framing: fitted[0] ? { x: fitted[0].x, y: fitted[0].y,"
            + "     w: fitted[0].width, h: fitted[0].height } : null };"
            + " c.selectCapture('no_such_capture');"
            + " answer.stillSelected = c.selected;"
            // A nudge -- the viewer's own settling, a small pan. The region is
            // still somewhere a frame can sit, so the frame follows it and the
            // capture stays selected.
            + " shift = 120; fire('animation');"
            + " answer.afterNudge = { pinned: c.capture.pinned, selected: c.selected,"
            + "   frame: c.capture.box };"
            // Navigating away. The region can no longer be framed inside the
            // viewer, so the lock goes and takes the selection with it.
            + " shift = 700; fire('animation');"
            + " answer.afterMoving = { pinned: c.capture.pinned, selected: c.selected };"
            + " FigureScene.restore = restore;"
            + " return answer; })();",
            ctx);
        const selection = ctx.__selection;
        // Clicking a capture arms the shutter. Without this the click moves the
        // viewer and leaves nothing on screen saying why -- no frame, no lock,
        // and the one control that would show either still switched off.
        if (selection.armedBefore !== false) {
            problems.push("the fixture started with capture mode already on");
        }
        if (selection.armedAfter !== true) {
            problems.push("selecting a capture did not turn capture mode on");
        }
        // Both of them the instant the thumbnail is clicked, while the viewer is
        // still flying: a click whose only visible effect arrives half a second
        // later reads as a click that missed.
        if (selection.atOnce.armed !== true || selection.atOnce.selected !== "cap_1") {
            problems.push(`clicking a capture did not take effect at once: `
                + JSON.stringify(selection.atOnce));
        }
        // And it is still selected once the viewer has finished arriving --
        // including the second, smaller arrival after `animation-finish`, which
        // used to break the lock and take the selection with it.
        if (selection.selected !== "cap_1") {
            problems.push(`selecting a capture did not select it: ${selection.selected}`);
        }
        if (selection.fitted !== 1) {
            problems.push(`the viewer was not put back over the region (${selection.fitted} moves)`);
        }
        if (selection.restores !== 0) {
            problems.push("going back to a capture restored its rendering state");
        }
        // The frame lands ON the region, which is what makes the next capture
        // of it pixel-for-pixel concordant with the first.
        const frame = selection.frame || {};
        if (frame.x !== 200 || frame.y !== 150 || frame.width !== 400 || frame.height !== 300) {
            problems.push(`the frame did not land on the region: ${JSON.stringify(frame)}`);
        }
        // A strip and a canvas that disagree about what exists is worse than
        // either answer, so an id that is not in the strip selects nothing
        // rather than clearing what was.
        if (selection.stillSelected !== "cap_1") {
            problems.push("an unknown capture id changed the selection");
        }
        if (selection.unattached !== 1) {
            problems.push(`the unattached count is wrong: ${selection.unattached}`);
        }
        // With room around it, not filling the window: the capture is 2000x1500
        // and the viewer is put over 4000x3000 of the slide centred on it.
        const framing = selection.framing || {};
        if (framing.x !== 0 || framing.y !== 0 || framing.w !== 4000 || framing.h !== 3000) {
            problems.push(`going back did not leave room around the region: ${JSON.stringify(framing)}`);
        }
        // The lock is what makes the next shot the SAME region rather than a
        // fresh reading off the screen -- a pixel or two out, and only in the
        // file.
        const pin = selection.pinned || {};
        if (pin.x !== 1000 || pin.y !== 750 || pin.w !== 2000 || pin.h !== 1500) {
            problems.push(`the frame did not lock onto the region: ${JSON.stringify(pin)}`);
        }
        // A viewer that moves a little does NOT cost the user the capture they
        // just clicked. The lock is on a region and the frame is how that region
        // is shown, so the frame follows it -- which is also what makes the lock
        // survive the movement nobody asked for: OpenSeadragon's own settling
        // after a flight, a nudge, a resize.
        const nudged = selection.afterNudge || {};
        if (!nudged.pinned || nudged.selected !== "cap_1") {
            problems.push(`a nudge cost the user the capture: ${JSON.stringify(nudged)}`);
        }
        if (!nudged.frame || nudged.frame.x !== 320) {
            problems.push(`the frame did not follow the region: ${JSON.stringify(nudged.frame)}`);
        }
        // And it lets go once the region can no longer be framed at all, taking
        // the strip's active item and the box's highlight with it: all three
        // mean "the shutter will take this one", so they cannot disagree. That
        // is also what keeps a following frame inside the viewer, since
        // #openseadragon_wrapper does not clip.
        if (selection.afterMoving.pinned !== null) {
            problems.push("the frame stayed locked after the viewer moved");
        }
        if (selection.afterMoving.selected !== null) {
            problems.push("the selection outlived the lock it was showing");
        }
        report.selection = selection;
    } catch (error) {
        problems.push(`selecting a capture threw: ${error.message}`);
    }

    // "Figure Canvas" leaves the viewer for the figure's own page. Two claims,
    // and the second is the one that can lose an hour's work: unattached
    // captures are MEMORY, and a navigation ends the memory -- so everything
    // waiting has to be written into the figure BEFORE the page changes, and a
    // write that fails has to stop the navigation rather than carry the
    // captures off the page.
    try {
        runInContext(
            "globalThis.__canvas = (() => {"
            + " const osd = { addHandler() {}, removeHandler() {}, canvas: { style: {},"
            + "   getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }) } };"
            + " const build = () => { const c = new FigureBuilderSidebarController({"
            + "     url: (p) => '/' + p, datasource: 'demo', config: { width: 4000, height: 3000 },"
            + "     viewer: { viewer: osd }, onCleanup() {} });"
            + "   c.figureId = 'fig_abc';"
            + "   c.state = { document: { panels: {}, pages: [{ page_id: 'pg_1' }] } };"
            + "   return c; };"
            + " const order = [];"
            + " const good = build();"
            + " good.attachCaptures = () => { order.push('attached'); return Promise.resolve(true); };"
            + " const bad = build();"
            + " bad.attachCaptures = () => Promise.resolve(false);"
            + " window.location.href = '';"
            + " return good.goToCanvas().then(() => {"
            + "   order.push('href:' + window.location.href);"
            + "   window.location.href = '';"
            + "   return bad.goToCanvas(); }).then(() => ({ order,"
            + "     blockedHref: window.location.href, said: bad.failure })); })();",
            ctx);
        const canvas = await ctx.__canvas;
        if (canvas.order[0] !== "attached") {
            problems.push("the canvas was opened without saving the waiting captures first");
        }
        if (canvas.order[1] !== "href:/plugins/figure_builder/figure/fig_abc") {
            problems.push(`"Figure Canvas" did not go to the figure's page: ${canvas.order[1]}`);
        }
        // A pane could afford to open anyway. A navigation cannot: the strip is
        // the only copy of an unattached capture.
        if (canvas.blockedHref !== "") {
            problems.push("captures that could not be saved were carried off the page anyway");
        }
        if (!canvas.said) {
            problems.push("the canvas refused to open and said nothing about why");
        }
        report.canvas = canvas;
    } catch (error) {
        problems.push(`opening the canvas threw: ${error.message}`);
    }

    // A capture becomes a panel in exactly one place, and a strip full of them
    // becomes one batch -- which is what makes a burst of captures one undo
    // step rather than six. Pure, and nothing else in the suite runs it.
    try {
        runInContext(
            "globalThis.__panels = (() => {"
            + " const source = { source_id: 'src_1', pixel_size: { value: 0.325 } };"
            + " const captures = [1, 2, 3].map((n) => ({ id: 'cap_' + n,"
            + "   scene: { source_id: '', viewport: { x: n, y: 0, w: 10, h: 8 } } }));"
            + " const panels = captures.map((c) =>"
            + "   FigureBuilderSidebarController.panelFor(c, source));"
            + " return { ids: panels.map((p) => p.panel_id),"
            + "   sources: panels.map((p) => p.scene.source_id),"
            + "   placed: panels.filter((p) => p.placement !== null).length,"
            + "   scalebars: panels.every((p) => p.scalebar.visible === true),"
            + "   uncalibrated: FigureBuilderSidebarController.panelFor(captures[0],"
            + "     { source_id: 'src_2', pixel_size: null }).scalebar.visible }; })();",
            ctx);
        const panels = ctx.__panels;
        if (new Set(panels.ids).size !== 3) {
            problems.push(`captures did not get distinct panel ids: ${panels.ids}`);
        }
        if (panels.sources.some((id) => id !== "src_1")) {
            problems.push("a panel's scene was not joined to the figure's source");
        }
        // Composition is a different sitting from exploration: a capture that
        // placed itself on a page would make every capture a layout decision.
        if (panels.placed !== 0) {
            problems.push("a capture placed itself on a page instead of the tray");
        }
        // A bar drawn from an assumed pixel size looks exactly like one that is
        // right, so an uncalibrated source gets none at all.
        if (!panels.scalebars || panels.uncalibrated !== false) {
            problems.push("scale bars did not follow the source's calibration");
        }
        report.panels = panels;
    } catch (error) {
        problems.push(`panelFor threw: ${error.message}`);
    }

    // Pure arithmetic the canvas and the export both depend on, and which
    // nothing else in the suite ever executes. A label sequence that goes
    // A..Z,BA instead of A..Z,AA is wrong in a way no server test can see.
    try {
        runInContext(
            "globalThis.__math = {"
            + " labels: [0, 1, 25, 26, 27].map((i) => FigureSchema.labelFor(i, 'A')),"
            + " lower: FigureSchema.labelFor(0, 'a'),"
            + " numbered: FigureSchema.labelFor(2, 'A1'),"
            + " noScale: FigureSchema.physicalWidthUm({ pixel_size: null }, { w: 100 }),"
            + " width: FigureSchema.physicalWidthUm({ pixel_size: { value: 0.325 } }, { w: 4000 }),"
            + " bar: FigureSchema.scaleBarLength(1300),"
            + " escaped: FigureSchema.escapeHtml('<img src=x onerror=1>') };",
            ctx);
        const math = ctx.__math;
        if (JSON.stringify(math.labels) !== JSON.stringify(["A", "B", "Z", "AA", "AB"])) {
            problems.push(`panel labels are wrong: ${JSON.stringify(math.labels)}`);
        }
        if (math.lower !== "a" || math.numbered !== "A3") {
            problems.push(`label styles are wrong: ${math.lower} / ${math.numbered}`);
        }
        if (math.noScale !== null) {
            problems.push("an uncalibrated source produced a physical width");
        }
        if (Math.abs(math.width - 1300) > 1e-6) {
            problems.push(`physical width is wrong: ${math.width}`);
        }
        if (math.bar !== 250) {
            problems.push(`scale bar length is wrong: ${math.bar}`);
        }
        if (math.escaped.includes("<")) {
            problems.push("escapeHtml let markup through");
        }
        report.math = math;
    } catch (error) {
        problems.push(`figure arithmetic threw: ${error.message}`);
    }
}

report.registered = registered.map((d) => ({
    name: d.name, ownsCellLayer: d.ownsCellLayer, hooks: Object.keys(d),
}));
report.controller = ctx.__built || null;
report.requests = requests;
report.problems = problems;

console.error(JSON.stringify(report, null, 2));
process.exit(problems.length ? 1 : 0);
