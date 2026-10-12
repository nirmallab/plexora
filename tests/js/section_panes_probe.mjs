/**
 * The section panes show the newest section, in step with the plane.
 *
 * views/sectionPanes.js runs here against a fake OpenSeadragon whose items
 * load when told to, and a clock this probe owns. Pinned:
 *
 * - a section handover superseded by a newer one never fades up, never fires
 *   its deadline, and drops whatever of it is still arriving; the pane ends
 *   on the newest token with only its items;
 * - a deferred handover (a navigation revision's) keeps the old section on
 *   screen until its commit runs, and a superseded revision drops it;
 * - a handover whose tiles never arrive still hands over at the deadline,
 *   and says so (`timeouts`);
 * - the z-line marks the plane the PICTURE shows (the revision committed), not
 *   the one asked for a moment ago;
 * - a cut that moved less than one screen pixel keeps the section it has.
 *
 * Run directly:  node tests/js/section_panes_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora/client/src/js");

const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}

// -- a clock and a small DOM -----------------------------------------------------

const clock = { now: 0, timers: [] };
function setTimeoutFake(fn, ms) {
    const timer = { fn, at: clock.now + (ms || 0), id: clock.timers.length + 1 };
    clock.timers.push(timer);
    return timer.id;
}
function clearTimeoutFake(id) {
    const timer = clock.timers.find((t) => t.id === id);
    if (timer) timer.cancelled = true;
}
function advance(ms) {
    clock.now += ms;
    for (const timer of clock.timers) {
        if (!timer.cancelled && !timer.done && timer.at <= clock.now) {
            timer.done = true;
            timer.fn();
        }
    }
}

function element() {
    return {
        style: { setProperty() {} }, hidden: false, children: [],
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        setAttribute() {}, appendChild(c) { return c; }, append() {}, remove() {},
        addEventListener() {}, getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }),
        getContext: () => null,
    };
}

const box = {
    console,
    setTimeout: setTimeoutFake,
    clearTimeout: clearTimeoutFake,
    requestAnimationFrame: () => 0,
    cancelAnimationFrame: () => {},
    performance: { now: () => clock.now },
    document: { createElement: () => element(), getElementById: () => null },
    addEventListener() {},
};
box.window = box;
box.globalThis = box;
runInContext(readFileSync(join(JS, "views/sectionPanes.js"), "utf8"), createContext(box));
const { Pane, SectionPanes } = box.PlexoraSectionPanes;

/** An OSD world whose items load when the probe says so. */
function fakeOsd() {
    const items = [];
    const world = {
        items,
        getItemCount: () => items.length,
        getItemAt: (i) => items[i],
        getIndexOfItem: (item) => items.indexOf(item),
        removeItem(item) {
            const at = items.indexOf(item);
            if (at >= 0) items.splice(at, 1);
            item.removed = true;
        },
    };
    const pendingAdds = [];
    const osd = {
        world,
        pendingAdds,
        forceRedraw() {},
        addTiledImage(options) {
            const handlers = new Set();
            const item = {
                token: options.tileSource.view,
                opacity: options.opacity,
                loaded: false,
                setOpacity(v) { item.opacity = v; },
                getFullyLoaded: () => item.loaded,
                addHandler(name, fn) { if (name === "fully-loaded-change") handlers.add(fn); },
                removeHandler(name, fn) { handlers.delete(fn); },
                ready() { item.loaded = true; for (const fn of [...handlers]) fn(); },
            };
            // Arrives later, when the probe lands it.
            pendingAdds.push(() => {
                items.push(item);
                options.success({ item });
                return item;
            });
        },
        land() { return pendingAdds.splice(0).map((add) => add()); },
    };
    return osd;
}

function makePane(channels = 2) {
    const owner = {
        timeouts: 0,
        superseded: 0,
        config: { tileWidth: 256, tileHeight: 256, imageData: [{}, {}, {}] },
        channels: () => Array.from({ length: channels }, (_, i) => [i, { url: `/t/${i}/` }]),
        channelCount: () => channels,
        isHd: () => false,
    };
    const pane = new Pane(owner, "xz");
    pane.osd = fakeOsd();
    pane.geometry = { width: 1000, height: 40, level_shapes: [[40, 1000]] };
    pane.align = () => {};
    pane.redraw = () => {};
    return { pane, owner, osd: pane.osd };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

// -- a superseded handover never lands -------------------------------------------

{
    const { pane, osd } = makePane();
    pane.show("xzy10");
    osd.land().forEach((item) => item.ready());
    await tick();
    check("a first section shows", pane.token === "xzy10" && osd.world.items.every((i) => i.opacity === 1));

    pane.show("xzy20");                 // a pan...
    const twenty = osd.land();          // ...whose items arrive...
    pane.show("xzy30");                 // ...and is overtaken before they load
    twenty.forEach((item) => item.ready());
    await tick();
    check("an overtaken section never fades up",
        twenty.every((item) => item.opacity === 0 && item.removed), JSON.stringify(twenty.map((i) => [i.opacity, i.removed])));
    const thirty = osd.land();
    thirty.forEach((item) => item.ready());
    await tick();
    check("the newest lands, and only its items are left",
        pane.token === "xzy30" && osd.world.items.length === 2
        && osd.world.items.every((i) => i.token === "xzy30" && i.opacity === 1));
    const before = osd.world.items.slice();
    advance(10000);
    check("the overtaken one's deadline never fires over it",
        pane.token === "xzy30" && osd.world.items.length === 2
        && osd.world.items.every((i, k) => i === before[k]));
}

{
    // A superseded one whose items arrive only after the newer one landed.
    const { pane, osd } = makePane();
    pane.show("xzy10");
    osd.land().forEach((item) => item.ready());
    pane.show("xzy20");
    const lateAdds = osd.pendingAdds.splice(0);
    pane.show("xzy30");
    osd.land().forEach((item) => item.ready());
    await tick();
    const late = lateAdds.map((add) => add());
    check("items of an overtaken section that arrive afterwards are dropped on arrival",
        late.every((item) => item.removed) && pane.token === "xzy30" && osd.world.items.length === 2);
}

// -- deferred: held until the revision commits ------------------------------------

{
    const { pane, osd } = makePane();
    pane.show("t0xzy10");
    osd.land().forEach((item) => item.ready());
    const promise = pane.show("t1xzy10", { defer: true });
    osd.land().forEach((item) => item.ready());
    const commit = await promise;
    check("a deferred section is ready but not shown: the old one stays",
        typeof commit === "function" && pane.token === "t0xzy10"
        && osd.world.items.filter((i) => i.opacity === 1).every((i) => i.token === "t0xzy10"));
    commit();
    check("until its commit, which shows it and drops the old",
        pane.token === "t1xzy10" && osd.world.items.length === 2
        && osd.world.items.every((i) => i.token === "t1xzy10" && i.opacity === 1));
}

{
    const { pane, osd } = makePane();
    pane.show("t0xzy10");
    osd.land().forEach((item) => item.ready());
    const listeners = [];
    const rev = { superseded: false, onSupersede(fn) { listeners.push(fn); } };
    const promise = pane.show("t2xzy10", { defer: true, rev });
    const fresh = osd.land();
    rev.superseded = true;
    listeners.forEach((fn) => fn(rev));
    const result = await promise;
    check("a superseded revision's section is dropped and never shown",
        result === null && fresh.every((item) => item.removed) && pane.token === "t0xzy10");
}

// -- the deadline -------------------------------------------------------------------

{
    const { pane, osd, owner } = makePane();
    pane.show("xzy10");
    osd.land().forEach((item) => item.ready());
    pane.show("xzy11");
    const fresh = osd.land();
    fresh[0].ready();                    // the other never loads
    advance(6001);
    check("a section whose tiles never all arrive still hands over at the deadline",
        pane.token === "xzy11" && fresh.every((item) => item.opacity === 1) && owner.timeouts === 1);
}

// -- the z-line follows the picture -------------------------------------------------

{
    const requested = { z: 20, t: 0, layout: "sections", mode: "plane" };
    const viewState = { depth: 40, frames: 1, info: { width: 1000, height: 800 },
                        get: () => ({ ...requested }) };
    const panes = new SectionPanes({ viewState, imageViewer: null, viewerManager: null, config: {} });
    check("before any revision the z-line marks the plane asked for", panes.zRow(40) === 20.5);
    const commit = panes.prepare({ state: { z: 12, t: 0 } });
    check("a z step needs nothing loaded: its commit is ready at once", typeof commit === "function");
    commit();
    requested.z = 25;                    // the hand is already further on
    check("the z-line marks the plane the picture shows, not the one asked for since",
        panes.zRow(40) === 12.5);
}

// -- a cut that moved less than a screen pixel keeps its section --------------------

{
    const viewState = { depth: 40, frames: 1, info: { width: 1000, height: 800 }, get: () => ({ t: 0 }) };
    const panes = new SectionPanes({ viewState, imageViewer: null, viewerManager: null, config: {} });
    panes.panes = { xz: { wanted: () => "xzy100" }, yz: { wanted: () => "yzx200" } };
    const zoomedOut = panes.tokens({ cx: 201.7, cy: 101.2, scale: 0.25 });
    check("zoomed out (4 image px a screen px), a 1-2 px move keeps both sections",
        zoomedOut.xz === "xzy100" && zoomedOut.yz === "yzx200");
    const moved = panes.tokens({ cx: 207, cy: 106, scale: 0.25 });
    check("a move past a screen pixel takes the new cut", moved.xz === "xzy106" && moved.yz === "yzx207");
    const zoomedIn = panes.tokens({ cx: 201.7, cy: 101.2, scale: 4 });
    check("zoomed in, every image row is its own section", zoomedIn.xz === "xzy101" && zoomedIn.yz === "yzx202");
}

// -- a channel switched on comes in (services/channelFade.js) ----------------------

{
    runInContext(readFileSync(join(JS, "services/channelFade.js"), "utf8"), box);
    const Fade = box.PlexoraChannelFade;
    let frames = [];
    box.requestAnimationFrame = (fn) => { frames.push(fn); return frames.length; };
    const run = (ms) => {
        for (let t = 0; t < ms; t += 16) {
            advance(16);
            const due = frames;
            frames = [];
            for (const fn of due) fn(clock.now);
        }
    };
    const { pane, owner, osd } = makePane(1);
    pane.show("xzy10");
    const first = osd.land();
    check("a pane's first section starts from nothing", first.every((item) => item.opacity === 0));
    first.forEach((item) => item.ready());
    await tick();
    run(Fade.FADE_MS + 64);
    check("and comes in, to full", first.every((item) => item.opacity === 1));

    // A second channel switched on: the pane rebuilds every channel's item.
    owner.channels = () => [0, 1].map((i) => [i, { url: `/t/${i}/` }]);
    owner.channelCount = () => 2;
    pane.token = "";
    pane.show("xzy10");
    const [kept, added] = osd.land();
    kept.ready();
    added.ready();
    await tick();
    check("the channel already there hands over in full; the new one starts from nothing",
          kept.opacity === 1 && added.opacity === 0, JSON.stringify([kept.opacity, added.opacity]));
    run(Fade.FADE_MS / 2);
    check("and comes in", added.opacity > 0 && added.opacity < 1, String(added.opacity));
    run(Fade.FADE_MS);
    check("to full", added.opacity === 1 && first.every((item) => item.removed));
    box.requestAnimationFrame = () => 0;
}

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
