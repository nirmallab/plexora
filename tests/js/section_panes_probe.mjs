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
 * - a handover whose tiles never all arrive keeps the old section past any
 *   deadline and says so (`slow`); it lands once it covers the pane -- but
 *   never on a section whose every tile failed for good: that one says
 *   `error`, and lands after a retry, once a tile does;
 * - the z-line marks the plane the PICTURE shows (the revision committed), not
 *   the one asked for a moment ago;
 * - a cut that moved less than one screen pixel keeps the section it has;
 * - each pane on its own: XZ and YZ of one pan each reveal when they cover,
 *   the faster first, each label at its own pane's commit (pending until); a
 *   pan asked again shares what is loading; an older pan's late result never
 *   lands;
 * - a pan during a new frame's handover waits for it, at the new frame;
 * - a scrolled pane's own viewer reaches PANE_ROW_MARGIN_PX past both edges;
 * - each pane says how its loading goes (`plexora:surface-status`).
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
        style: { setProperty() {} }, hidden: false, children: [], dataset: {},
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        setAttribute() {}, appendChild(c) { return c; }, append() {}, remove() {},
        addEventListener() {}, getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }),
        getContext: () => null,
    };
}

//: Every `plexora:surface-status` the panes said, in order.
const statuses = [];
const box = {
    console,
    setTimeout: setTimeoutFake,
    clearTimeout: clearTimeoutFake,
    requestAnimationFrame: () => 0,
    cancelAnimationFrame: () => {},
    performance: { now: () => clock.now },
    document: { createElement: () => element(), getElementById: () => null },
    addEventListener() {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent(event) { if (event.type === "plexora:surface-status") statuses.push(event.detail); },
    OpenSeadragon: { Point: class { constructor(x, y) { this.x = x; this.y = y; } } },
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
    const osdHandlers = new Map();
    const osd = {
        world,
        pendingAdds,
        forceRedraw() {},
        addHandler(name, fn) {
            if (!osdHandlers.has(name)) osdHandlers.set(name, new Set());
            osdHandlers.get(name).add(fn);
        },
        removeHandler(name, fn) { osdHandlers.get(name)?.delete(fn); },
        raise(name, event = {}) { for (const fn of [...(osdHandlers.get(name) || [])]) fn(event); },
        listening: (name) => osdHandlers.get(name)?.size || 0,
        addTiledImage(options) {
            const handlers = new Set();
            // OSD's own state, as services/channelFade.js reads it: one tile
            // at level 0 covers the pane, four at level 2 are its resolution.
            const tile = () => ({ exists: true, loaded: false });
            const item = {
                token: options.tileSource.view,
                opacity: options.opacity,
                loaded: false,
                coverage: { 0: { 0: { 0: false } }, 2: { 0: { 0: false, 1: false }, 1: { 0: false, 1: false } } },
                tilesMatrix: { 0: { 0: { 0: tile() } },
                               2: { 0: { 0: tile(), 1: tile() }, 1: { 0: tile(), 1: tile() } } },
                setOpacity(v) { item.opacity = v; },
                getFullyLoaded: () => item.loaded,
                addHandler(name, fn) { if (name === "fully-loaded-change") handlers.add(fn); },
                removeHandler(name, fn) { handlers.delete(fn); },
                tiles() {
                    return Object.values(item.tilesMatrix)
                        .flatMap((level) => Object.values(level).flatMap((column) => Object.values(column)));
                },
                loadCoarse() { item.tilesMatrix[0][0][0].loaded = true; },
                /** Every tile but one at full resolution; that one failed for good. */
                breakOne() {
                    item.loadCoarse();
                    for (const [x, y] of [[0, 0], [0, 1], [1, 0]]) item.tilesMatrix[2][x][y].loaded = true;
                    Object.assign(item.tilesMatrix[2][1][1], { exists: false, _plexoraFailed: "final" });
                },
                ready() {
                    for (const one of item.tiles()) one.loaded = true;
                    item.loaded = true;
                    for (const fn of [...handlers]) fn();
                },
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
        slow: 0,
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

/** Both panes of one SectionPanes, each on a fake OSD of its own, 2 channels. */
function makePanes({ frames = 1 } = {}) {
    const viewState = { depth: 40, frames, info: { width: 1000, height: 800 },
                        get: () => ({ z: 20, t: 0 }) };
    const channelList = { currentChannels: { 0: { url: "/t/0/" }, 1: { url: "/t/1/" } } };
    const owner = new SectionPanes({ viewState, imageViewer: null, viewerManager: { channelList },
                                     config: { tileWidth: 256, tileHeight: 256, imageData: [{}, {}] } });
    const panes = { xz: new Pane(owner, "xz"), yz: new Pane(owner, "yz") };
    for (const pane of Object.values(panes)) {
        pane.osd = fakeOsd();
        pane.geometry = { width: 1000, height: 40, level_shapes: [[40, 1000]] };
        pane.align = () => {};
        pane.redraw = () => {};
    }
    owner.panes = panes;
    owner.enabled = true;
    return { owner, panes };
}

/** Animation frames the probe runs when it says (`flush`). */
function frameQueue() {
    let frames = [];
    box.requestAnimationFrame = (fn) => { frames.push(fn); return frames.length; };
    return {
        flush() {
            const due = frames;
            frames = [];
            for (const fn of due) fn(clock.now);
        },
        stop() { box.requestAnimationFrame = () => 0; },
    };
}

/** Let the panes' coverage polls (every COVER_POLL_MS) run for `ms`. */
function poll(ms) {
    for (let t = 0; t < ms; t += 20) advance(20);
}

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

// -- no deadline: the old section stays until the new one covers ----------------------

{
    const { pane, osd, owner } = makePane();
    pane.show("xzy10");
    const old = osd.land();
    old.forEach((item) => item.ready());
    // From here on the handover waits for coverage (services/channelFade.js).
    runInContext(readFileSync(join(JS, "services/channelFade.js"), "utf8"), box);
    const frames = frameQueue();
    statuses.length = 0;
    pane.show("xzy11");
    const fresh = osd.land();
    fresh[0].ready();                    // the other never loads
    poll(2001);
    frames.flush();
    check("a section whose tiles never all arrive keeps the old one at the deadline, and says slow",
        pane.token === "xzy10" && old.every((item) => !item.removed && item.opacity === 1)
        && fresh.every((item) => item.opacity === 0 && !item.removed) && owner.slow === 1
        && statuses.at(-1)?.phase === "slow" && statuses.at(-1)?.token === "xzy11",
        JSON.stringify({ token: pane.token, slow: owner.slow, status: statuses.at(-1)?.phase }));
    poll(6000);
    check("...past the old six-second deadline, too",
        pane.token === "xzy10" && old.every((item) => !item.removed) && owner.slow === 1);
    fresh[1].loadCoarse();               // one coarse tile covers the pane
    poll(200);
    check("it lands once it covers the pane, at the level that covers",
        pane.token === "xzy11" && old.every((item) => item.removed)
        && fresh.every((item) => item.opacity === 1) && [...pane.items.values()].every((i) => fresh.includes(i)));
    frames.stop();
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

// -- each pane reveals on its own, as it covers ------------------------------------

{
    const { owner, panes } = makePanes();
    const landAll = () => {
        for (const pane of Object.values(panes)) pane.osd.land().forEach((item) => item.ready());
    };
    const first = owner.showAt({ cx: 200, cy: 100, scale: 1 });
    landAll();
    await first;
    const settledTokens = `${panes.xz.token}|${panes.yz.token}`;

    const pan = owner.showAt({ cx: 260, cy: 150, scale: 1 });
    const again = owner.showAt({ cx: 260, cy: 150, scale: 1 });   // the hand let go there
    check("a pan asked again for the same cut shares the sections loading",
        panes.xz.osd.pendingAdds.length === 2 && panes.yz.osd.pendingAdds.length === 2,
        `${panes.xz.osd.pendingAdds.length} + ${panes.yz.osd.pendingAdds.length} item(s) asked for`);
    check("the label is pending while the pan's section is on its way, and names what is on screen",
        panes.xz.label.dataset.pending === "1" && panes.yz.label.dataset.pending === "1"
        && /y 100\b/.test(panes.xz.label.textContent) && /x 200\b/.test(panes.yz.label.textContent),
        `${panes.xz.label.textContent} / ${panes.yz.label.textContent}`);
    const xz = panes.xz.osd.land();
    const yz = panes.yz.osd.land();
    xz.forEach((item) => item.ready());
    await tick();
    check("XZ and YZ of one pan each reveal when they cover, the faster first",
        settledTokens === "xzy100|yzx200" && panes.xz.token === "xzy150" && panes.yz.token === "yzx200"
        && xz.every((item) => item.opacity === 1)
        && yz.every((item) => item.opacity === 0 && !item.removed),
        `${panes.xz.token}|${panes.yz.token}`);
    check("...each label at its own pane's commit: YZ's stays pending, naming what YZ shows",
        /y 150\b/.test(panes.xz.label.textContent) && !("pending" in panes.xz.label.dataset)
        && /x 200\b/.test(panes.yz.label.textContent) && panes.yz.label.dataset.pending === "1",
        `${panes.xz.label.textContent} / ${panes.yz.label.textContent}`);
    yz.forEach((item) => item.ready());
    const [older, newer] = await Promise.all([pan, again]);
    await tick();
    check("...and the slower lands in its turn",
        panes.xz.token === "xzy150" && panes.yz.token === "yzx260"
        && xz.every((item) => item.opacity === 1) && yz.every((item) => item.opacity === 1));
    check("the older pan's discard keeps what the newer one shares; only the newest reveals",
        older === false && newer === true && [...panes.xz.items.values()].every((i) => xz.includes(i)));
    check("the label names the new section once it is on screen, no longer pending",
        /y 150\b/.test(panes.xz.label.textContent) && /x 260\b/.test(panes.yz.label.textContent)
        && !("pending" in panes.xz.label.dataset) && !("pending" in panes.yz.label.dataset),
        `${panes.xz.label.textContent} / ${panes.yz.label.textContent}`);

    // A pane whose section did not change does not hold the other.
    const across = owner.showAt({ cx: 260, cy: 175, scale: 1 });
    check("...and a pane whose section is unchanged asks for nothing",
        panes.yz.osd.pendingAdds.length === 0 && !("pending" in panes.yz.label.dataset)
        && panes.xz.label.dataset.pending === "1");
    panes.xz.osd.land().forEach((item) => item.ready());
    check("nor holds the other back", (await across) === true && panes.xz.token === "xzy175");

    // A pan whose YZ covered and landed on its own, its XZ still on the way
    // when the hand moved on: that XZ, arriving after the newer one landed,
    // never lands over it.
    const late = owner.showAt({ cx: 300, cy: 200, scale: 1 });
    const lateAdds = panes.xz.osd.pendingAdds.splice(0);           // the older XZ, still coming
    panes.yz.osd.land().forEach((item) => item.ready());
    await tick();
    const yzOnItsOwn = panes.yz.token === "yzx300" && panes.xz.token === "xzy175";
    const newest = owner.showAt({ cx: 300, cy: 220, scale: 1 });   // on along y only
    const newXz = panes.xz.osd.land();
    newXz.forEach((item) => item.ready());
    const newestRevealed = await newest;
    const lateItems = lateAdds.map((add) => add());                // ...and the older XZ arrives
    lateItems.forEach((item) => item.ready());
    const lateRevealed = await late;
    await tick();
    check("an older pan's late result never lands",
        yzOnItsOwn && newestRevealed === true && lateRevealed === false
        && panes.xz.token === "xzy220" && panes.yz.token === "yzx300"
        && lateItems.every((item) => item.removed && item.opacity === 0)
        && [...panes.xz.items.values()].every((item) => newXz.includes(item))
        && /y 220\b/.test(panes.xz.label.textContent) && /x 300\b/.test(panes.yz.label.textContent)
        && !("pending" in panes.xz.label.dataset) && !("pending" in panes.yz.label.dataset),
        JSON.stringify({ xz: panes.xz.token, yz: panes.yz.token, newestRevealed, lateRevealed,
                         late: lateItems.map((item) => [item.opacity, Boolean(item.removed)]) }));
}

// -- a pan during a new frame's handover waits for it, at the new frame -----------

{
    const { owner, panes } = makePanes({ frames: 3 });
    owner.frame = { cx: 200, cy: 100, scale: 1 };
    const first = owner.showAt(owner.frame);
    for (const pane of Object.values(panes)) pane.osd.land().forEach((item) => item.ready());
    await first;
    const listeners = [];
    const rev = { id: 7, state: { z: 20, t: 1 }, superseded: false, scrub: false,
                  onSupersede(fn) { listeners.push(fn); } };
    const prepared = owner.prepare(rev);
    const revXz = panes.xz.osd.land();
    const revYz = panes.yz.osd.land();
    const pan = owner.showAt({ cx: 200, cy: 150, scale: 1 });
    check("a pan during a deferred new-t handover does not cancel it",
        revXz.every((item) => !item.removed) && panes.xz.loading?.rev === rev
        && panes.xz.loading?.token === "t1xzy100" && panes.yz.loading?.token === "t1yzx200"
        && owner.superseded === 0,
        JSON.stringify({ xz: panes.xz.loading?.token, superseded: owner.superseded }));
    check("...and cuts at the new frame: it is queued, not lost",
        panes.xz.after?.token === "t1xzy150" && panes.xz.wanted() === "t1xzy150"
        && panes.yz.after === null && panes.xz.osd.pendingAdds.length === 0,
        JSON.stringify({ after: panes.xz.after?.token }));
    revXz.forEach((item) => item.ready());
    revYz.forEach((item) => item.ready());
    const commit = await prepared;
    check("the frame's handover is the revision's to commit", typeof commit === "function"
        && panes.xz.token === "xzy100" && owner.pendingT === 1);
    commit();
    check("it lands; then the pan's section is asked for, at the new frame",
        panes.xz.token === "t1xzy100" && panes.yz.token === "t1yzx200" && owner.pendingT === null
        && panes.xz.loading?.token === "t1xzy150" && panes.xz.osd.pendingAdds.length === 2
        && panes.xz.label.dataset.pending === "1" && !("pending" in panes.yz.label.dataset));
    panes.xz.osd.land().forEach((item) => item.ready());
    check("and lands in its turn", (await pan) === true && panes.xz.token === "t1xzy150"
        && panes.yz.token === "t1yzx200" && !("pending" in panes.xz.label.dataset));
}

// -- a scrolled pane keeps rows loaded past its edges -------------------------------

{
    const viewState = { depth: 40, frames: 1, info: { width: 2000, height: 2000 }, get: () => ({ z: 20, t: 0 }) };
    const owner = new SectionPanes({ viewState, imageViewer: null, viewerManager: null, config: {} });
    const viewport = (sizes, centres) => ({
        resize: (p) => sizes.push([p.x, p.y]), zoomTo() {}, panTo: (p) => centres.push(p),
    });
    const setUp = (axis) => {
        const pane = new Pane(owner, axis);
        pane.geometry = { width: 2000, height: 100 };
        const sizes = [];
        const centres = [];
        pane.osd = { viewport: viewport(sizes, centres), world: { getItemCount: () => 0 }, forceRedraw() {} };
        pane.redraw = () => {};
        return { pane, sizes, centres };
    };
    owner.frame = { width: 1000, height: 800, scale: 6, x0: 0, x1: 1000 / 6, y0: 0, y1: 800 / 6 };
    const xz = setUp("xz");
    let view = xz.pane.layout();
    xz.pane.apply();
    const margin = view.margin;
    check("a scrolled pane's viewer is taller than the pane by its margin, moved up by one",
        view.size === 256 && view.whole === false && margin === 96
        && xz.sizes.at(-1)?.join() === `1000,${256 + 2 * 96}`
        && xz.pane.canvasHost.style.height === `${256 + 2 * 96}px`
        && xz.pane.canvasHost.style.transform === "translateY(-96px)",
        JSON.stringify({ size: view.size, margin, sizes: xz.sizes.at(-1), host: xz.pane.canvasHost.style }));
    check("...centred where it was, its z-line mid-pane",
        Math.abs(xz.centres.at(-1).y - (view.top + view.rows / 2) / 2000) < 1e-12
        && Math.abs(parseFloat(xz.pane.zline.style.top) - 128) < 1e-6, `${xz.pane.zline.style.top}`);
    const yz = setUp("yz");
    view = yz.pane.layout();
    yz.pane.apply();
    check("...as is a YZ pane, wider by its margin",
        view.size === 320 && view.margin === 96 && yz.sizes.at(-1)?.join() === `${320 + 2 * 96},800`
        && yz.pane.canvasHost.style.width === `${320 + 2 * 96}px`
        && yz.pane.canvasHost.style.transform === "translateX(-96px)",
        JSON.stringify({ sizes: yz.sizes.at(-1), host: yz.pane.canvasHost.style }));
    owner.frame.scale = 2;               // the whole stack fits: 200 px
    view = xz.pane.layout();
    xz.pane.apply();
    check("a whole pane has none",
        view.whole === true && view.margin === 0 && xz.sizes.at(-1)?.join() === "1000,200"
        && xz.pane.canvasHost.style.height === "200px" && xz.pane.canvasHost.style.transform === "");
}

// -- each pane says how its loading goes -------------------------------------------

{
    const frames = frameQueue();
    const { pane, osd, owner } = makePane();
    const retried = [];
    owner.grace = () => 0;               // the view moves: the level that covers
    // As tileFetch.retryFailed: a tile whose retries are spent is unloaded again.
    const retryFailed = (items) => {
        retried.push(items);
        let count = 0;
        for (const item of items) {
            for (const one of item.tiles()) {
                if (one._plexoraFailed !== "final") continue;
                delete one._plexoraFailed;
                Object.assign(one, { exists: true, loaded: false });
                count += 1;
            }
        }
        return count;
    };
    owner.imageViewer = { tileFetch: { attach: (source) => source, retryFailed } };
    pane.show("xzy10");
    osd.land().forEach((item) => item.ready());
    frames.flush();
    statuses.length = 0;
    const asked = clock.now;
    pane.show("xzy11");
    frames.flush();
    const fresh = osd.land();
    fresh.forEach((item) => item.loadCoarse());
    poll(60);
    frames.flush();
    // OSD raises tile-loaded on the VIEWER; the pane's also hosts the next
    // handover's hidden items, whose tiles say nothing about this one.
    osd.raise("tile-loaded", { tiledImage: {} });
    frames.flush();
    const beforeOwnTile = statuses.map((s) => s.phase).join();
    let decoded = null;
    osd.raise("tile-loaded", { tiledImage: fresh[0], promise: new Promise((done) => { decoded = done; }) });
    frames.flush();
    // OSD marks the tile loaded once its awaited handlers (the decode) are
    // done, and resolves the event's promise then.
    fresh[0].tilesMatrix[2][0][0].loaded = true;
    decoded();
    await tick();
    frames.flush();
    const afterDecode = statuses.at(-1);
    fresh.forEach((item) => item.ready());
    frames.flush();
    const phases = statuses.map((s) => s.phase);
    const preview = statuses.find((s) => s.phase === "preview");
    check("status goes fetching -> preview -> idle (refining between)",
        beforeOwnTile === "fetching,preview"
        && phases.join() === "fetching,preview,refining,refining,idle" && pane.token === "xzy11",
        `${beforeOwnTile} / ${phases.join()}`);
    check("...with the counts of the pane's level, summed over its channels, counted again once a tile decodes",
        preview?.surface === "xz" && preview.token === "xzy11" && preview.loaded === 0
        && preview.needed === 8 && preview.failed === 0 && preview.level === 2
        && preview.since === asked && typeof preview.retry === "function"
        && afterDecode.phase === "refining" && afterDecode.loaded === 1
        && statuses.at(-1).loaded === 8 && statuses.at(-1).needed === 8,
        JSON.stringify([preview, afterDecode]));
    check("...and once idle stops listening", osd.listening("tile-loaded") === 0
        && osd.listening("tile-load-failed") === 0);

    statuses.length = 0;
    pane.show("xzy12");
    frames.flush();
    const broken = osd.land();
    broken[0].breakOne();                // one tile failed for good
    broken[1].ready();
    poll(60);
    frames.flush();
    const error = statuses.at(-1);
    check("error with a final tile: it still hands over, a hole and not a wait",
        pane.token === "xzy12" && error?.phase === "error" && error.failed === 1
        && error.loaded === 7 && error.needed === 8 && osd.listening("tile-loaded") === 0,
        JSON.stringify(statuses.map((s) => [s.phase, s.loaded, s.failed])));
    const count = error?.retry?.();
    frames.flush();
    check("...whose retry asks the scheduler again for the pane's tiles, and listens again",
        count === 1 && retried.length === 1 && retried[0].length === 2
        && retried[0].every((item) => broken.includes(item))
        && statuses.at(-1).phase === "refining" && statuses.at(-1).failed === 0
        && osd.listening("tile-loaded") === 1 && osd.listening("tile-load-failed") === 1,
        JSON.stringify(statuses.at(-1)));
    frames.stop();
}

{
    // Every tile of the new section failed for good: never handed over (it
    // would be black where a section was). The old one stays, the status
    // says error, and a retry hands over once a tile lands.
    const frames = frameQueue();
    const { pane, osd, owner } = makePane();
    const retried = [];
    const retryFailed = (items) => {
        retried.push(items);
        let count = 0;
        for (const item of items) {
            for (const one of item.tiles()) {
                if (one._plexoraFailed !== "final") continue;
                delete one._plexoraFailed;
                Object.assign(one, { exists: true, loaded: false });
                count += 1;
            }
        }
        return count;
    };
    owner.imageViewer = { tileFetch: { attach: (source) => source, retryFailed } };
    pane.show("xzy10");
    const old = osd.land();
    old.forEach((item) => item.ready());
    frames.flush();
    statuses.length = 0;
    pane.show("xzy13");
    frames.flush();
    const fresh = osd.land();
    for (const item of fresh) {
        for (const one of item.tiles()) Object.assign(one, { loaded: false, exists: false, _plexoraFailed: "final" });
        item.loaded = true;              // OSD: nothing is loading any more
    }
    poll(200);
    frames.flush();
    const early = statuses.at(-1);
    poll(3000);
    frames.flush();
    const error = statuses.at(-1);
    check("a section whose every tile fails for good is never shown: the old one stays, the status says error",
        early?.phase === "error" && error?.phase === "error" && error.token === "xzy13"
        && typeof error.retry === "function" && pane.token === "xzy10"
        && old.every((item) => !item.removed && item.opacity === 1)
        && fresh.every((item) => !item.removed && item.opacity === 0),
        JSON.stringify({ early: early?.phase, error: error?.phase, token: pane.token }));
    const count = error.retry();
    frames.flush();
    poll(100);
    frames.flush();
    check("...its retry asks again for the waiting section's tiles; it waits again, not handed over",
        count === 10 && retried.length === 1 && fresh.every((item) => retried[0].includes(item))
        && statuses.at(-1).phase === "slow" && pane.token === "xzy10",
        JSON.stringify({ count, phase: statuses.at(-1).phase, token: pane.token }));
    fresh.forEach((item) => item.loadCoarse());
    poll(200);
    frames.flush();
    check("...and hands over once a tile lands, the old section gone",
        pane.token === "xzy13" && old.every((item) => item.removed)
        && fresh.every((item) => item.opacity === 1));
    frames.stop();
}

// -- the panes are 2-D only: nothing of the 3-D view's cut is left in them ----------
{
    check("no cut guides or pinning to 3-D sections",
          !("cutGuides" in box.PlexoraSectionPanes)
          && !("pinned" in box.PlexoraSectionPanes.SectionPanes.prototype)
          && !("bindCrosshair" in box.PlexoraSectionPanes.SectionPanes.prototype));
}

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
