/**
 * The dataset's thumbnail grid (views/datasetStrip.js), run for real against a
 * DOM stand-in.
 *
 * The decisions worth fencing, all of which fail quietly:
 *
 *   - IT KEEPS OFF THE REST OF THE CANVAS. fit() is arithmetic over boxes; a
 *     wrong sign puts thumbnails over the channel names or the caption, and
 *     nothing errors.
 *   - DISMISSING IT NEVER NAVIGATES, and choosing the sample already on screen
 *     does not reload it.
 *   - ITS KEY HANDLER TAKES ESCAPE ONLY. It listens in the capture phase, so a
 *     careless one would swallow B and N, and the walk would stop working
 *     whenever the grid was open.
 *   - IT LEAVES NOTHING BEHIND: document listeners, a window listener and an
 *     observer, all released on close.
 *   - THE SEARCH ONLY NARROWS. It hides tiles and refits; picking from it goes
 *     through the same choice as a click, and its first Escape clears rather
 *     than closes.
 *
 * Run directly: `node tests/js/dataset_strip_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/views/datasetStrip.js");

let checks = 0;
function check(label, fn) {
    fn();
    checks += 1;
    console.log("  ok  " + label);
}

function box(top, left, width, height) {
    return { top, left, width, height, right: left + width, bottom: top + height };
}

function element(tag = "div", doc = null) {
    const node = {
        tagName: tag.toUpperCase(),
        className: "",
        children: [],
        attributes: {},
        listeners: {},
        style: {},
        textContent: "",
        title: "",
        value: "",
        parentNode: null,
        rect: box(0, 0, 0, 0),
        scrollLeft: 0, scrollWidth: 0, clientWidth: 0, offsetLeft: 0, offsetWidth: 0,
        appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
        insertBefore(child, before) {
            child.parentNode = this;
            const at = this.children.indexOf(before);
            if (at === -1) this.children.push(child); else this.children.splice(at, 0, child);
            return child;
        },
        removeChild(child) {
            this.children = this.children.filter((c) => c !== child);
            child.parentNode = null;
        },
        setAttribute(name, value) { this.attributes[name] = String(value); },
        getAttribute(name) { return this.attributes[name]; },
        addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); },
        contains(other) {
            for (let at = other; at; at = at.parentNode) if (at === this) return true;
            return false;
        },
        getBoundingClientRect() { return this.rect; },
        focus() { if (doc) doc.activeElement = this; },
        fire(name, event = {}) {
            for (const fn of this.listeners[name] || []) fn({ target: this, ...event });
        },
    };
    return node;
}

function find(node, predicate) {
    if (!node) return null;
    if (predicate(node)) return node;
    for (const child of node.children || []) {
        const hit = find(child, predicate);
        if (hit) return hit;
    }
    return null;
}

const hasClass = (node, cls) => String(node.className).split(" ").includes(cls);

/**
 * @param wrapper false for a page with no canvas.
 * @param furniture id -> rect for core's own canvas furniture.
 * @param marked rects of plugin chrome carrying data-viewer-furniture.
 * @param observer true to give the page a ResizeObserver.
 */
function boot({ wrapper = true, furniture = {}, marked = [], observer = false } = {}) {
    const docListeners = {};
    const winListeners = {};
    const observers = [];
    const doc = { activeElement: null };
    const host = wrapper ? element("div", doc) : null;
    const byId = {};
    if (host) {
        host.rect = box(0, 0, 1200, 800);
        for (const [id, rect] of Object.entries(furniture)) {
            const el = element("div", doc);
            el.rect = rect;
            host.appendChild(el);
            byId[id] = el;
        }
        const markedEls = marked.map((rect) => {
            const el = element("div", doc);
            el.rect = rect;
            el.setAttribute("data-viewer-furniture", "");
            host.appendChild(el);
            return el;
        });
        host.querySelectorAll = (sel) => (sel === "[data-viewer-furniture]" ? markedEls : []);
        host.querySelector = () => null;
    }
    const chip = element("nav", doc);
    chip.rect = box(12, 1038, 150, 28);
    const anchor = element("button", doc);
    anchor.rect = box(14, 1130, 30, 22);
    chip.appendChild(anchor);
    if (host) host.appendChild(chip);

    Object.assign(doc, {
        getElementById: (id) => (id === "openseadragon_wrapper" ? host : byId[id] || null),
        createElement: (tag) => element(tag, doc),
        addEventListener: (name, fn, capture) => {
            (docListeners[name] ||= []).push({ fn, capture: !!capture });
        },
        removeEventListener: (name, fn) => {
            docListeners[name] = (docListeners[name] || []).filter((l) => l.fn !== fn);
        },
    });
    const context = {
        console, JSON, Math, Object, Array, String, Number, Boolean,
        encodeURIComponent,
        document: doc,
        plexoraUrl: (path) => "/" + String(path || "").replace(/^\/+/, ""),
        addEventListener: (name, fn) => { (winListeners[name] ||= []).push(fn); },
        removeEventListener: (name, fn) => {
            winListeners[name] = (winListeners[name] || []).filter((f) => f !== fn);
        },
    };
    if (observer) {
        context.ResizeObserver = class {
            constructor(fn) { this.fn = fn; this.watched = []; this.live = true; observers.push(this); }
            observe(el) { this.watched.push(el); }
            disconnect() { this.live = false; this.watched = []; }
        };
    }
    context.window = context;
    createContext(context);
    runInContext(readFileSync(SOURCE, "utf8"), context);
    const strip = context.PlexoraDatasetStrip;
    return {
        strip, host, chip, anchor, doc, docListeners, winListeners, observers, byId,
        root: () => (host ? find(host, (n) => hasClass(n, "dataset-nav-strip")) : null),
        part: (cls) => (host ? find(host, (n) => hasClass(n, cls)) : null),
        tiles: () => {
            const grid = host && find(host, (n) => hasClass(n, "dataset-nav-strip-grid"));
            return grid ? grid.children : [];
        },
        fireDoc: (name, event) => {
            for (const l of (docListeners[name] || []).slice()) l.fn(event);
        },
        fireWin: (name, event = {}) => {
            for (const fn of (winListeners[name] || []).slice()) fn(event);
        },
    };
}

const SAMPLES = ["s1", "s2", "s3", "s4", "s5"];
function openOn(t, { members = SAMPLES, current = "s2", picked = [] } = {}) {
    return t.strip.open({
        anchor: t.anchor, chip: t.chip, members, current, label: "Cohort",
        onPick: (name) => picked.push([name, t.strip.isOpen()]),
    });
}

const S = boot().strip._sizes;
const HOST = { width: 1200, height: 800 };
const CHIP = box(12, 1038, 150, 28);           // bottom 40, right 1188
const TILE = boot().strip._tileFor(150);
const rowsFor = (height) => Math.floor(
    (height - 2 * S.PAD - 2 * S.BORDER - S.HEAD + S.CELL_GAP) / (TILE.height + S.CELL_GAP));
const fit = (obstacles, count = 60) => boot().strip.fit({ host: HOST, anchor: CHIP, obstacles, count });

console.log("dataset strip");

// ---------------------------------------------------------------- fit()

check("it hangs under the chip at exactly the chip's width", () => {
    const placed = fit([]);
    assert.equal(placed.top, 40 + S.GAP);
    assert.equal(placed.right, 1200 - 1188);
    assert.equal(placed.width, 150, "key caps and all");
});

check("one thumbnail per row fills it, at 4:3", () => {
    const { tile } = fit([]);
    assert.equal(S.COLUMNS, 1);
    assert.equal(tile.width + 2 * S.PAD + 2 * S.BORDER, 150);
    assert.equal(tile.height, Math.round(tile.width * 3 / 4));
    const wider = boot().strip.fit({ host: HOST, anchor: box(12, 1010, 178, 28), obstacles: [], count: 4 });
    assert.equal(wider.width, 178, "a wider counter, a wider strip");
    assert.equal(wider.tile.width, TILE.width + 28);
});

check("the caption and the expand button leave it alone", () => {
    const placed = fit([box(12, 12, 300, 60), box(8, 8, 40, 40)]);
    assert.equal(placed.right, 12);
    assert.equal(placed.width, 150);
});

check("a plugin dock moves it left, at the same width", () => {
    const placed = fit([box(12, 1030, 158, 400)]);
    assert.equal(placed.right, 1200 - (1030 - S.GAP));
    assert.equal(placed.width, 150);
});

check("the search row costs exactly its height and the gap under it", () => {
    assert.equal(S.HEAD, S.SEARCH + S.CELL_GAP);
    assert.equal(S.SEARCH, 22, "viewer.css .dataset-nav-strip-search says 22px");
    const roomy = { width: 1200, height: 44 + 3 * (TILE.height + S.CELL_GAP) + 2 * S.PAD + S.MARGIN };
    const placed = boot().strip.fit({ host: roomy, anchor: CHIP, obstacles: [], count: 3 });
    assert.equal(placed.rows, 2, "three rows' room less the search holds two");
});

check("a dock level with the search counts as beside it", () => {
    const dock = box(44 + S.HEAD + TILE.height - 2, 1011, 177, 400);
    assert.equal(fit([dock], 14).right, 1200 - (1011 - S.GAP));
});

check("a dock stacked under the chip puts it beside the dock, full height", () => {
    // Figure Builder's dock starts 6px under the chip, level with the first row.
    const dock = box(46, 1011, 177, 400);
    const placed = fit([dock], 14);
    assert.equal(placed.right, 1200 - (1011 - S.GAP));
    assert.equal(placed.width, 150);
    assert.equal(placed.rows, Math.min(14, rowsFor(800 - S.MARGIN - 44)), "not capped by it");
});

check("the legend under the chip caps the rows, and keeps a gap", () => {
    const legend = box(500, 1000, 188, 288);
    const placed = fit([legend]);
    const top = 40 + S.GAP;
    // 60 samples are more than one column, so the scrollbar's gutter is paid for.
    assert.equal(placed.rows, rowsFor(500 - S.GAP - top - S.SCROLLBAR));
    const bottom = top + S.HEAD + placed.rows * (TILE.height + S.CELL_GAP) - S.CELL_GAP
        + 2 * S.PAD + 2 * S.BORDER + S.SCROLLBAR;
    assert.ok(bottom <= 500 - S.GAP, `bottom ${bottom} clears the legend`);
});

check("something low that is not under it costs it nothing", () => {
    const lens = box(560, 12, 220, 228);
    const placed = fit([lens], 400);
    assert.equal(placed.rows, rowsFor(800 - S.MARGIN - 44 - S.SCROLLBAR));
});

check("rows never exceed the members and never fall below one", () => {
    assert.equal(fit([], 1).rows, 1);
    assert.equal(fit([], 0).rows, 1);
    assert.equal(fit([box(44 + S.HEAD + TILE.height + 10, 900, 288, 500)], 20).rows, 1,
        "a legend just below the first row");
});

check("a column holds what fits, and the rest start a column beside it", () => {
    const tallest = rowsFor(800 - S.MARGIN - 44);
    assert.equal(fit([], 3).rows, 3, "three that fit are one column of three");
    assert.equal(fit([], tallest).rows, tallest, "a column exactly full does not scroll");
    assert.equal(fit([], 400).rows, rowsFor(800 - S.MARGIN - 44 - S.SCROLLBAR),
        "past one column, every column is full");
});

check("rows leave room for the scrollbar only when it is drawn", () => {
    const tall = { width: 1200,
        height: 44 + S.HEAD + 2 * (TILE.height + S.CELL_GAP) + 2 * S.PAD + S.MARGIN };
    const few = boot().strip.fit({ host: tall, anchor: CHIP, obstacles: [], count: 2 });
    assert.equal(few.rows, 2, "two fit in one column, no scrollbar, two rows");
    const many = boot().strip.fit({ host: tall, anchor: CHIP, obstacles: [], count: 400 });
    assert.equal(many.rows, 1, "the scrollbar's gutter costs the second row");
});

// ---------------------------------------------------------------- obstacles

check("a hidden legend is not an obstacle", () => {
    const t = boot({ furniture: {
        viewer_channel_legend: box(0, 0, 0, 0),
        viewer_mini_map: box(560, 12, 220, 228),
    } });
    const found = t.strip._obstacles(t.host, t.chip, null);
    assert.equal(found.length, 1);
    assert.equal(found[0].top, 560);
});

check("plugin chrome is found by its attribute, and the chip never is", () => {
    const t = boot({ marked: [box(12, 1030, 158, 400)] });
    const found = t.strip._obstacles(t.host, t.chip, null);
    assert.deepEqual(JSON.parse(JSON.stringify(found)),
        [{ top: 12, bottom: 412, left: 1030, right: 1188 }]);
});

// ---------------------------------------------------------------- the grid

check("nothing is built until the counter is pressed", () => {
    const t = boot();
    assert.equal(t.root(), null);
    assert.equal(t.strip.isOpen(), false);
});

check("opening builds one tile per member, in the dataset's own order", () => {
    const t = boot();
    openOn(t, { members: ["zebra", "apple", "mango"], current: "apple" });
    assert.deepEqual(t.tiles().map((n) => n.attributes["data-sample"]), ["zebra", "apple", "mango"]);
    assert.equal(t.root().attributes["aria-label"], "Samples in Cohort");
});

check("each tile carries the sample's thumbnail and its name as text", () => {
    const t = boot();
    openOn(t, { members: ["core 1/a"], current: "x" });
    const [item] = t.tiles();
    const img = find(item, (n) => n.tagName === "IMG");
    assert.equal(img.src, "/project_thumbnail/core%201%2Fa");
    assert.equal(img.loading, "lazy");
    assert.equal(img.alt, "");
    const name = find(item, (n) => hasClass(n, "dataset-nav-strip-name"));
    assert.equal(name.textContent, "core 1/a");
    assert.equal(item.title, "core 1/a");
});

check("a name is text, never markup", () => {
    const t = boot();
    openOn(t, { members: ["<b>x</b>"], current: "y" });
    const name = find(t.tiles()[0], (n) => hasClass(n, "dataset-nav-strip-name"));
    assert.equal(name.textContent, "<b>x</b>");
    assert.equal(name.innerHTML, undefined);
});

check("the current sample is marked, and choosing it only closes", () => {
    const t = boot();
    const picked = [];
    openOn(t, { picked });
    const here = t.tiles()[1];
    assert.ok(hasClass(here, "is-current"));
    assert.equal(here.attributes["aria-current"], "true");
    assert.equal(t.tiles().filter((n) => hasClass(n, "is-current")).length, 1);
    here.fire("click");
    assert.equal(t.strip.isOpen(), false);
    assert.equal(picked.length, 0, "no reload of the page already on screen");
});

check("choosing another tile closes the strip, then hands the name back", () => {
    const t = boot();
    const picked = [];
    openOn(t, { picked });
    t.tiles()[3].fire("click");
    assert.deepEqual(picked, [["s4", false]]);
    assert.equal(t.root(), null);
});

check("a thumbnail that will not load falls back to an icon", () => {
    const t = boot();
    openOn(t);
    const item = t.tiles()[0];
    find(item, (n) => n.tagName === "IMG").fire("error");
    find(item, (n) => n.tagName === "IMG").fire("error");
    assert.ok(hasClass(item, "is-missing"));
    const fallbacks = item.children.filter((n) => hasClass(n, "dataset-nav-strip-fallback"));
    assert.equal(fallbacks.length, 1, "once, however often it fails");
    assert.equal(fallbacks[0].children[0].className, "fas fa-image");
    assert.ok(hasClass(item.children[item.children.length - 1], "dataset-nav-strip-name"),
        "the name stays on top");
});

check("the counter says it is expanded while the strip is open", () => {
    const t = boot();
    openOn(t);
    assert.equal(t.anchor.attributes["aria-expanded"], "true");
    t.strip.close();
    assert.equal(t.anchor.attributes["aria-expanded"], "false");
});

check("it is placed from measurement, inline", () => {
    const t = boot();
    openOn(t);
    const root = t.root();
    assert.equal(root.style.top, (40 + S.GAP) + "px");
    assert.equal(root.style.right, "12px");
    const grid = find(root, (n) => hasClass(n, "dataset-nav-strip-grid"));
    assert.equal(root.style.width, "150px", "the chip's width");
    assert.equal(grid.style.gridAutoColumns, TILE.width + "px");
    assert.equal(grid.style.gridTemplateRows, `repeat(5, ${TILE.height}px)`, "five in one column");
});

check("the current tile has focus when it opens", () => {
    const t = boot();
    openOn(t);
    assert.equal(t.doc.activeElement, t.tiles()[1]);
});

// ---------------------------------------------------------------- dismissal

check("Escape shuts it and goes no further", () => {
    const t = boot();
    openOn(t);
    assert.ok(t.docListeners.keydown.every((l) => l.capture), "ahead of the page's own keys");
    let stopped = false;
    t.fireDoc("keydown", { key: "Escape", stopPropagation: () => { stopped = true; }, preventDefault() {} });
    assert.equal(t.strip.isOpen(), false);
    assert.equal(stopped, true);
    assert.equal(t.doc.activeElement, t.anchor, "focus back on the counter");
});

check("B, N, PageUp and PageDown pass straight through", () => {
    const t = boot();
    openOn(t);
    for (const key of ["b", "n", "B", "PageUp", "PageDown"]) {
        let stopped = false;
        t.fireDoc("keydown", { key, stopPropagation: () => { stopped = true; }, preventDefault() {} });
        assert.equal(stopped, false, key);
    }
    assert.equal(t.strip.isOpen(), true);
});

check("a press outside shuts it; a press inside does not", () => {
    const t = boot();
    openOn(t);
    t.fireDoc("pointerdown", { target: t.tiles()[0] });
    assert.equal(t.strip.isOpen(), true);
    t.fireDoc("pointerdown", { target: t.host });
    assert.equal(t.strip.isOpen(), false);
});

check("a press on the counter is left to the counter", () => {
    const t = boot();
    openOn(t);
    t.fireDoc("pointerdown", { target: t.anchor });
    assert.equal(t.strip.isOpen(), true, "its click is the toggle");
});

check("one strip at a time", () => {
    const t = boot();
    openOn(t);
    openOn(t);
    const strips = t.host.children.filter((n) => hasClass(n, "dataset-nav-strip"));
    assert.equal(strips.length, 1);
    assert.equal(t.docListeners.keydown.length, 1);
});

check("a page routed over the viewer closes it", () => {
    const t = boot();
    openOn(t);
    t.fireWin("plexora:viewer-hidden");
    assert.equal(t.strip.isOpen(), false);
});

check("a resize refits rather than closes", () => {
    const t = boot();
    openOn(t);
    t.host.rect = box(0, 0, 900, 800);
    t.fireWin("resize");
    assert.equal(t.strip.isOpen(), true);
    assert.equal(t.root().style.right, (900 - 1188) + "px");
});

check("the wrapper and the furniture are watched while open and released on close", () => {
    const t = boot({ observer: true, furniture: { viewer_channel_legend: box(500, 1000, 188, 288) } });
    openOn(t);
    const [watcher] = t.observers;
    assert.ok(watcher.watched.includes(t.host));
    assert.ok(watcher.watched.includes(t.byId.viewer_channel_legend));
    t.byId.viewer_channel_legend.rect = box(200, 1000, 188, 588);
    watcher.fn();
    const grid = find(t.root(), (n) => hasClass(n, "dataset-nav-strip-grid"));
    assert.equal(grid.style.gridTemplateRows, `repeat(${rowsFor(200 - S.GAP - 44)}, ${TILE.height}px)`,
        "a legend that grew took rows");
    t.strip.close();
    assert.equal(watcher.live, false);
    assert.equal((t.docListeners.keydown || []).length, 0);
    assert.equal((t.docListeners.pointerdown || []).length, 0);
    assert.equal((t.winListeners.resize || []).length, 0);
    assert.equal((t.winListeners["plexora:viewer-hidden"] || []).length, 0);
});

check("a page with no ResizeObserver still opens", () => {
    const t = boot({ observer: false });
    assert.equal(openOn(t), true);
    assert.equal(t.strip.isOpen(), true);
});

check("arrows walk the grid: down one, right one column", () => {
    const t = boot();
    openOn(t, { members: Array.from({ length: 12 }, (_, i) => "m" + i), current: "m0" });
    const grid = find(t.root(), (n) => hasClass(n, "dataset-nav-strip-grid"));
    const rows = Number(/repeat\((\d+)/.exec(grid.style.gridTemplateRows)[1]);
    const press = (key, from) => {
        let stopped = false;
        for (const fn of grid.listeners.keydown) {
            fn({ key, target: from, preventDefault() {}, stopPropagation: () => { stopped = true; } });
        }
        return stopped;
    };
    const tiles = t.tiles();
    assert.equal(press("ArrowDown", tiles[0]), true);
    assert.equal(t.doc.activeElement, tiles[1]);
    press("ArrowRight", tiles[1]);
    assert.equal(t.doc.activeElement, tiles[Math.min(1 + rows, 11)]);
    press("End", tiles[0]);
    assert.equal(t.doc.activeElement, tiles[11]);
    assert.equal(press("n", tiles[0]), false, "a letter is not the grid's");
});

check("a vertical wheel scrolls it sideways only when there is overflow", () => {
    const t = boot();
    openOn(t);
    const grid = find(t.root(), (n) => hasClass(n, "dataset-nav-strip-grid"));
    let prevented = false;
    const wheel = (deltaY) => grid.listeners.wheel[0]({ deltaY, preventDefault: () => { prevented = true; } });
    grid.scrollWidth = 200; grid.clientWidth = 200;
    wheel(50);
    assert.equal(grid.scrollLeft, 0);
    assert.equal(prevented, false, "nothing to scroll, the page keeps its wheel");
    grid.scrollWidth = 900;
    wheel(50);
    assert.equal(grid.scrollLeft, 50);
    assert.equal(prevented, true);
});

// ---------------------------------------------------------------- search

const shown = (t) => t.tiles().filter((n) => n.style.display !== "none")
    .map((n) => n.attributes["data-sample"]);
const typeInto = (t, text) => {
    const input = t.part("dataset-nav-strip-input");
    input.value = text;
    input.fire("input");
    return input;
};
const pressIn = (node, key) => {
    let stopped = false;
    for (const fn of node.listeners.keydown || []) {
        fn({ key, target: node, preventDefault() {}, stopPropagation: () => { stopped = true; } });
    }
    return stopped;
};

check("a search row sits over the grid, and the current tile keeps focus", () => {
    const t = boot();
    openOn(t);
    const [search, grid] = t.root().children;
    assert.ok(hasClass(search, "dataset-nav-strip-search"));
    assert.ok(hasClass(grid, "dataset-nav-strip-grid"));
    const input = t.part("dataset-nav-strip-input");
    assert.equal(input.placeholder, "Search samples");
    assert.equal(input.attributes["aria-label"], "Search samples");
    assert.equal(t.doc.activeElement, t.tiles()[1], "so B and N still walk");
});

check("typing keeps the tiles that hold every word, in any case", () => {
    const t = boot();
    openOn(t, { members: ["Tonsil core 1", "Tonsil core 2", "Lung core 1", "lung 7"], current: "lung 7" });
    typeInto(t, "LUNG");
    assert.deepEqual(shown(t), ["Lung core 1", "lung 7"]);
    typeInto(t, "core   1");
    assert.deepEqual(shown(t), ["Tonsil core 1", "Lung core 1"]);
    typeInto(t, "  ");
    assert.equal(shown(t).length, 4, "blank is everything");
    assert.equal(t.tiles().length, 4, "hidden, never removed");
});

check("a title is searched as well as the name", () => {
    const t = boot();
    t.strip.open({ anchor: t.anchor, chip: t.chip, members: SAMPLES, current: "s1",
                   titles: { s3: "Patient 42 biopsy" } });
    typeInto(t, "42");
    assert.deepEqual(shown(t), ["s3"]);
});

check("the grid refits to what is shown", () => {
    const t = boot();
    openOn(t);
    const grid = t.part("dataset-nav-strip-grid");
    typeInto(t, "s4");
    assert.equal(grid.style.gridTemplateRows, `repeat(1, ${TILE.height}px)`);
    typeInto(t, "");
    assert.equal(grid.style.gridTemplateRows, `repeat(5, ${TILE.height}px)`);
});

check("nothing matching hides the grid and says so in one line", () => {
    const t = boot();
    openOn(t);
    typeInto(t, "zzz");
    const empty = t.part("dataset-nav-strip-empty");
    assert.equal(t.part("dataset-nav-strip-grid").style.display, "none");
    assert.equal(empty.style.display, "");
    assert.equal(empty.textContent, "No sample matches");
    typeInto(t, "s");
    assert.equal(t.part("dataset-nav-strip-grid").style.display, "");
    assert.equal(empty.style.display, "none");
});

check("Enter picks the first match; on the current sample it only closes", () => {
    let t = boot();
    let picked = [];
    openOn(t, { picked });
    pressIn(typeInto(t, "s4"), "Enter");
    assert.deepEqual(picked, [["s4", false]]);
    t = boot();
    picked = [];
    openOn(t, { picked });
    pressIn(typeInto(t, "s2"), "Enter");
    assert.equal(t.strip.isOpen(), false);
    assert.equal(picked.length, 0, "no reload of the page already on screen");
    t = boot();
    picked = [];
    openOn(t, { picked });
    pressIn(typeInto(t, "zzz"), "Enter");
    assert.equal(t.strip.isOpen(), true, "nothing to pick");
});

check("ArrowDown from the search reaches the first match; the arrows walk only matches", () => {
    const t = boot();
    openOn(t, { members: Array.from({ length: 12 }, (_, i) => "m" + i), current: "m0" });
    const input = typeInto(t, "m1");
    const byName = (name) => t.tiles().find((n) => n.attributes["data-sample"] === name);
    assert.equal(pressIn(input, "ArrowDown"), true);
    assert.equal(t.doc.activeElement, byName("m1"));
    const grid = t.part("dataset-nav-strip-grid");
    const gridKey = (key, from) => {
        for (const fn of grid.listeners.keydown) {
            fn({ key, target: from, preventDefault() {}, stopPropagation() {} });
        }
    };
    gridKey("ArrowDown", byName("m1"));
    assert.equal(t.doc.activeElement, byName("m10"), "past the hidden m2");
    gridKey("ArrowUp", byName("m1"));
    assert.equal(t.doc.activeElement, input, "up off the top is the search");
});

check("Escape clears a search first and closes second", () => {
    const t = boot();
    openOn(t);
    const input = typeInto(t, "s4");
    const escape = () => t.fireDoc("keydown", { key: "Escape", stopPropagation() {}, preventDefault() {} });
    escape();
    assert.equal(t.strip.isOpen(), true);
    assert.equal(input.value, "");
    assert.equal(shown(t).length, 5);
    assert.ok(!hasClass(t.part("dataset-nav-strip-search"), "has-query"));
    escape();
    assert.equal(t.strip.isOpen(), false);
});

check("the clear button is drawn only with a query, and empties it", () => {
    const t = boot();
    openOn(t);
    const search = t.part("dataset-nav-strip-search");
    assert.ok(!hasClass(search, "has-query"));
    const input = typeInto(t, "s3");
    assert.ok(hasClass(search, "has-query"));
    t.part("dataset-nav-strip-clear").fire("click");
    assert.equal(input.value, "");
    assert.equal(shown(t).length, 5);
    assert.equal(t.doc.activeElement, input, "back in the field to type again");
});

check("a page with no canvas opens nothing", () => {
    const t = boot({ wrapper: false });
    assert.equal(openOn(t), false);
    assert.equal(t.strip.isOpen(), false);
    t.strip.close();
});

console.log(`\n${checks} checks passed`);
