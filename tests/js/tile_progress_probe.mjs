/**
 * The navbar's bottom-edge bar (services/tileProgress.js): how far the view
 * has sharpened, from OpenSeadragon's own pass over each image's levels.
 *
 *   - THE FINEST LEVEL OF A PASS IS THE MEASURE. OSD walks the levels from
 *     the one the view is sharp at down to the coarse ones it shows first;
 *     only the first level's tiles count, and only those that exist.
 *   - FORWARD ONLY WHILE THE VIEW STANDS STILL; a moved view is measured as
 *     it is.
 *   - NOTHING FOR A QUICK LOAD, and gone once every image is complete.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/tileProgress.js");

let checks = 0;
function check(label, fn) {
    fn();
    checks += 1;
    console.log("  ok  " + label);
}

function emitter(extra = {}) {
    const handlers = {};
    return {
        ...extra,
        addHandler(name, fn) { (handlers[name] ||= []).push(fn); },
        raise(name, event = {}) { (handlers[name] || []).forEach((fn) => fn(event)); },
    };
}

/** A page with the bar's mount, a controllable clock, and frames run by hand. */
function boot() {
    const timers = new Map();
    let nextId = 1;
    let now = 0;
    let frames = [];
    const fill = { className: "nav-load-bar-fill", style: {}, offsetWidth: 0 };
    const classes = new Set();
    const bar = {
        hidden: true,
        attributes: {},
        classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c) },
        querySelector: (selector) => (selector === ".nav-load-bar-fill" ? fill : null),
        setAttribute(name, value) { this.attributes[name] = value; },
    };
    const context = {
        console: { log() {}, error() {}, warn() {} },
        Math, Number, Boolean, Set, WeakMap, String,
        document: { getElementById: (id) => (id === "tile_progress" ? bar : null) },
        requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
    };
    context.window = context;
    context.setTimeout = (fn, ms) => {
        const id = nextId++;
        timers.set(id, { fn, at: now + (ms || 0) });
        return id;
    };
    context.clearTimeout = (id) => timers.delete(id);
    createContext(context);
    runInContext(readFileSync(SOURCE, "utf8"), context);

    const items = [];
    const world = emitter({ getItemCount: () => items.length, getItemAt: (i) => items[i] });
    const viewer = emitter({ world });
    let time = 1000;
    const page = {
        api: context.PlexoraTileProgress,
        bar, fill, viewer,
        width: () => (bar.hidden ? null : fill.style.width),
        /** An image, loading until `complete(true)`. */
        image() {
            const item = emitter({ loaded: false, getFullyLoaded() { return this.loaded; } });
            items.push(item);
            world.raise("add-item", { item });
            return item;
        },
        complete(item) {
            item.loaded = true;
            item.raise("fully-loaded-change", { fullyLoaded: true });
        },
        /** One OSD pass over `item`: `levels` finest first, each [level, [loaded flags]]. */
        pass(item, levels) {
            time += 16;
            for (const [level, tiles] of levels) {
                viewer.raise("update-level", { tiledImage: item, level, currenttime: time });
                tiles.forEach((loaded) => viewer.raise("update-tile", {
                    tiledImage: item, tile: { level, loaded: loaded !== null && loaded, exists: loaded !== null },
                }));
            }
        },
        /** Run the frames asked for. */
        frame() {
            const due = frames;
            frames = [];
            due.forEach((fn) => fn());
        },
        tick(ms) {
            now += ms;
            [...timers.entries()].filter(([, t]) => t.at <= now)
                .forEach(([id, t]) => { timers.delete(id); t.fn(); });
        },
    };
    page.api.watch(viewer);
    page.frame();
    return page;
}

console.log("tile progress");

check("a load that finishes inside the delay draws nothing", () => {
    const p = boot();
    const item = p.image();
    p.pass(item, [[5, [false, false]], [2, [true]]]);
    p.frame();
    p.tick(100);
    p.complete(item);
    p.frame();
    p.tick(1000);
    assert.equal(p.bar.hidden, true);
});

check("the bar is the share of the finest level's tiles that have arrived", () => {
    const p = boot();
    const item = p.image();
    // Level 5 is the view's; level 2's coarse tiles (all in) do not count,
    // and a tile that does not exist is no part of the view.
    p.pass(item, [[5, [true, false, false, false, null]], [2, [true, true]]]);
    p.frame();
    p.tick(250);
    assert.equal(p.width(), "25.00%");
    assert.equal(p.bar.attributes["aria-valuenow"], "25");
});

check("two images count together", () => {
    const p = boot();
    const a = p.image();
    const b = p.image();
    p.pass(a, [[5, [true, true, false, false]]]);
    p.pass(b, [[5, [false, false, false, false]]]);
    p.frame();
    p.tick(250);
    assert.equal(p.width(), "25.00%");
    p.complete(a);
    p.frame();
    assert.equal(p.width(), "50.00%", "a complete image counts whole");
});

check("it never moves back while the view stands still", () => {
    const p = boot();
    const item = p.image();
    p.pass(item, [[5, [true, true, false, false]]]);
    p.frame();
    p.tick(250);
    p.pass(item, [[5, [true, false, false, false]]]);
    p.frame();
    assert.equal(p.width(), "50.00%");
});

check("a moved view is measured as it is", () => {
    const p = boot();
    const item = p.image();
    p.pass(item, [[5, [true, true, false, false]]]);
    p.frame();
    p.tick(250);
    p.viewer.raise("viewport-change");
    p.pass(item, [[6, [true, false, false, false, false, false, false, false]]]);
    p.frame();
    assert.equal(p.width(), "12.50%");
});

check("full, then gone once every image is complete", () => {
    const p = boot();
    const item = p.image();
    p.pass(item, [[5, [true, false]]]);
    p.frame();
    p.tick(250);
    assert.equal(p.width(), "50.00%");
    p.complete(item);
    p.frame();
    assert.equal(p.width(), "100.00%");
    p.tick(250);
    assert.equal(p.bar.classList.contains("is-done"), true, "fading");
    p.tick(400);
    assert.equal(p.bar.hidden, true);
    assert.equal(p.fill.style.width, "0.00%", "empty for the next load");
});

check("a new load during the fade takes the bar back", () => {
    const p = boot();
    const first = p.image();
    p.pass(first, [[5, [false]]]);
    p.frame();
    p.tick(250);
    p.complete(first);
    p.frame();
    p.tick(250);
    const second = p.image();
    p.pass(second, [[5, [true, false, false, false]]]);
    p.frame();
    p.tick(250);
    assert.equal(p.bar.hidden, false);
    assert.equal(p.bar.classList.contains("is-done"), false);
    p.tick(1000);
    assert.equal(p.bar.hidden, false, "the old fade's timer does not hide it");
});

console.log(`\n${checks} checks passed`);
