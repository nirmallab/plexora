/**
 * Magic select's client: services/segmentService.js.
 *
 * Both panels' magic tools are thin because everything they share is here,
 * so this is where a regression costs the most. What is pinned:
 *
 *   - `plan`, which decides what a click means (a new region, grow, carve,
 *     refine, or a refusal), for every kind it can answer;
 *   - the prompt bookkeeping of `Session`, its `seed` included;
 *   - the token: a second click on the same view sends the server's token
 *     AND the view and channels the first click used (sending the token with
 *     a nudged view would ask the server to decode against a picture it did
 *     not encode), and a click outside the encoded crop sends none; a refine
 *     of an existing region sends that region's outline (`mask_geometry`)
 *     only when it is not starting from the previous mask;
 *   - the one-time setup: a "model missing" answer starts ONE install and
 *     opens ONE setup modal that is updated in place, turns into "ready" and
 *     closes itself, replays the remembered click once, stays away once sent
 *     to the background -- and never names the model anywhere it shows text
 *     (people read "magic select" and nothing else);
 *   - the error modal, busy, unavailable (one PlexoraConfirm.tell), and the
 *     `?segment=stub` mode that never fetches.
 *
 * The real script in a vm realm per scenario (it keeps module state: the
 * token cache, the poll, the waiters, the modal), with a stubbed fetch
 * emulating the three `/segment/v1/...` routes, a hand-built document whose
 * <dialog> fires `close` like a browser's, and fake timers driven by hand.
 * The real viewSnapshot.js supplies `sameView`.
 *
 * Run directly: `node tests/js/segment_service_probe.mjs`
 * Prints `ok  <check>` per check held; exit 1 if any did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SERVICES = join(REPO, "plexora", "client", "src", "js", "services");
const SOURCES = ["viewSnapshot.js", "segmentService.js"]
    .map((name) => [name, readFileSync(join(SERVICES, name), "utf8")]);

/** A DOM element just big enough for the setup modal. A <dialog>'s close()
 *  fires `close` (synchronously here), as a browser's does. */
function element(tag = "div") {
    const node = {
        tagName: tag.toUpperCase(), className: "", children: [], parentNode: null,
        attributes: {}, dataset: {}, listeners: {}, textContent: "", hidden: false,
        value: 0, max: 0, type: "", open: false,
        setAttribute(key, value) { node.attributes[key] = String(value); },
        appendChild(child) {
            child.parentNode?.removeChild?.(child);
            child.parentNode = node;
            node.children.push(child);
            return child;
        },
        append(...kids) { kids.forEach((k) => node.appendChild(k)); },
        removeChild(child) {
            node.children = node.children.filter((c) => c !== child);
            child.parentNode = null;
            return child;
        },
        remove() { node.parentNode?.removeChild(node); },
        addEventListener(name, fn) { (node.listeners[name] ||= []).push(fn); },
        dispatch(name) { for (const fn of node.listeners[name] || []) fn({ type: name }); },
        click() { node.dispatch("click"); },
        showModal() { node.open = true; },
        close() {
            if (!node.open) return;
            node.open = false;
            node.dispatch("close");
        },
        querySelector(selector) {
            return all(node).slice(1).find((n) => matches(n, selector)) || null;
        },
    };
    return node;
}

function all(node) {
    return [node, ...node.children.flatMap(all)];
}

/** `tag`, `.class`, `tag.class` -- what this probe asks for. */
function matches(node, selector) {
    const [tag, ...classes] = selector.split(".");
    if (tag && node.tagName !== tag.toUpperCase()) return false;
    const own = String(node.className).split(/\s+/);
    return classes.every((c) => own.includes(c));
}

/** Every piece of text a person can read in the document. */
function visibleText(document) {
    return all(document.body).filter((n) => !n.hidden).map((n) => n.textContent)
        .filter(Boolean).join(" | ");
}

/**
 * A fresh realm. `routes(method, path, body)` answers fetch with
 * `{status, data}`; every request is recorded.
 */
function realm({ search = "", routes = () => ({ status: 404, data: {} }) } = {}) {
    const requests = [];
    const told = [];
    const intervals = [];
    const cleared = [];
    const timeouts = [];
    const document = { body: element("body"), createElement: (tag) => element(tag) };
    const window = {
        location: { search },
        PlexoraConfirm: { tell: (options) => { told.push(options); return Promise.resolve(); } },
        setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
        clearInterval: (id) => cleared.push(id),
        setTimeout: (fn, ms) => { timeouts.push({ fn, ms }); return timeouts.length; },
    };
    async function fetch(url, options = {}) {
        const body = options.body ? JSON.parse(options.body) : undefined;
        const method = options.method || "GET";
        requests.push({ url, method, body });
        const { status = 200, data = {} } = routes(method, url, body) || {};
        return { ok: status >= 200 && status < 300, status, json: async () => data };
    }
    const context = createContext({ window, document, fetch, console, Date, Math,
                                    URLSearchParams, Promise });
    for (const [name, text] of SOURCES) runInContext(text, context, { filename: name });
    const dialogs = () => document.body.children.filter((n) => n.tagName === "DIALOG");
    return { Segment: window.PlexoraSegment, Snap: window.PlexoraViewSnapshot, document,
             requests, told, intervals, cleared, timeouts, window, dialogs };
}

/** The setup modal's parts, by the classes it is built with. */
function parts(dialog) {
    const q = (s) => dialog.querySelector(s);
    const meta = q(".plx-segment-setup-meta");
    return { title: q("h2.plx-dialog-title"), note: q("p.plx-confirm-body"),
             bar: q("progress.plx-segment-setup-progress"), meta: meta ? meta.children : [],
             button: q("button") };
}

/** Let the stubbed fetch's promise chains run. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

/** Equal values across the vm's realm (its objects have other prototypes). */
function same(actual, expected) {
    assert.equal(JSON.stringify(actual), JSON.stringify(expected));
}

/** Never the model's name, anywhere a person reads. */
function namesNoModel(words) {
    assert.ok(!words.includes("SAM"), `"SAM" where people read: ${words}`);
    assert.ok(!/segment anything/i.test(words), `the model named where people read: ${words}`);
}

const failures = [];
async function check(name, fn) {
    try {
        await fn();
        console.log(`ok  ${name}`);
    } catch (error) {
        failures.push(name);
        console.log(`FAIL  ${name}\n      ${String(error && error.message || error).split("\n").join("\n      ")}`);
    }
}

const SNAPSHOT = {
    sample: "s1", hd_mode: false,
    viewport: { x: 1000, y: 1000, width: 1000, height: 800 },
    channels: [{ name: "DNA", visible: true, color: "#0000ff", range: [10, 900] }],
};
const nudged = { ...SNAPSHOT, viewport: { x: 1004, y: 1003, width: 1000, height: 800 } };
const ANSWER = {
    ok: true, token: "tok-1", prev_key: "pk-1",
    crop: { x: 1000, y: 1000, width: 1000, height: 800 },
    geometry: { type: "Polygon", coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]] },
    bbox: { x: 1400, y: 1300, width: 50, height: 40 },
    flags: { empty: false },
};

// -- plan -----------------------------------------------------------------------

await check("plan answers new, grow, carve, refine, locked, needCategory and needSelection", () => {
    const { Segment } = realm();
    const session = { roiId: "r1", bbox: { x: 0, y: 0, width: 100, height: 100 } };
    const selected = { id: "s1", bbox: { x: 500, y: 500, width: 100, height: 100 }, locked: false };
    const kinds = {
        new: Segment.plan({ point: { x: 50, y: 50 } }),
        needCategory: Segment.plan({ point: { x: 50, y: 50 }, canCreate: false }),
        needSelection: Segment.plan({ point: { x: 50, y: 50 }, shift: true }),
        grow: Segment.plan({ point: { x: 150, y: 50 }, session }),
        carve: Segment.plan({ point: { x: 150, y: 50 }, session, shift: true }),
        refine: Segment.plan({ point: { x: 550, y: 550 }, selected }),
        locked: Segment.plan({ point: { x: 550, y: 550 },
                               selected: { ...selected, locked: true } }),
    };
    for (const [kind, answer] of Object.entries(kinds)) assert.equal(answer.kind, kind, kind);
    assert.equal(kinds.grow.roiId, "r1");
    assert.equal(kinds.carve.roiId, "r1");
    assert.equal(kinds.refine.roiId, "s1");
    assert.equal(kinds.locked.roiId, "s1");
});

await check("...a Shift-click inside the selection carves it; far from the session starts anew", () => {
    const { Segment } = realm();
    const session = { roiId: "r1", bbox: { x: 0, y: 0, width: 100, height: 100 } };
    const selected = { id: "s1", bbox: { x: 500, y: 500, width: 100, height: 100 }, locked: false };
    same(Segment.plan({ point: { x: 550, y: 550 }, selected, shift: true }),
         { kind: "carve", roiId: "s1" });
    assert.equal(Segment.plan({ point: { x: 900, y: 900 }, session }).kind, "new");
    assert.equal(Segment.plan({ point: { x: 900, y: 900 }, session, shift: true }).kind,
                 "needSelection");
});

// -- Session --------------------------------------------------------------------

await check("strokePoints: a dab is one point, a long line at most eight, evenly along it", () => {
    const S = realm().Segment;
    same(S.strokePoints([[10, 10], [11, 10], [12, 11]], { spacing: 24 }), [{ x: 11, y: 10 }]);
    const line = [];
    for (let x = 0; x <= 700; x += 7) line.push([x, 50]);
    const points = S.strokePoints(line, { spacing: 24 });
    assert.equal(points.length, 8);
    assert.equal(points[0].x, 0);
    assert.equal(points[7].x, 700);
    for (let i = 1; i < points.length; i++) {
        assert.ok(Math.abs(points[i].x - points[i - 1].x - 100) < 1e-6, JSON.stringify(points));
        assert.equal(points[i].y, 50);
    }
    // Spaced by length along the line, not by input samples: an L-shape.
    const ell = S.strokePoints([[0, 0], [0, 50], [0, 100], [100, 100]], { spacing: 50 });
    same(ell, [{ x: 0, y: 0 }, { x: 0, y: 50 }, { x: 0, y: 100 }, { x: 50, y: 100 }, { x: 100, y: 100 }]);
    same(S.strokePoints([], { spacing: 5 }), []);
});

await check("Session.addMany is one step: an undo takes back the whole scribble", () => {
    const S = realm().Segment;
    const s = new S.Session();
    s.add({ x: 1, y: 1 }, 1);
    s.addMany([{ x: 2, y: 2 }, { x: 3, y: 3 }, { x: 4, y: 4 }], 0);
    assert.equal(s.points.length, 4);
    same(s.points.map((p) => p.label), [1, 0, 0, 0]);
    assert.equal(s.undo().length, 3);
    same(s.points, [{ x: 1, y: 1, label: 1 }]);
});

await check("Session keeps at most MAX_POINTS, dropping the oldest prompts whole", () => {
    const S = realm().Segment;
    assert.equal(S.MAX_POINTS, 32);
    const s = new S.Session();
    const line = (n, x) => Array.from({ length: n }, (_, i) => ({ x, y: i }));
    for (let k = 0; k < 5; k++) s.addMany(line(8, k), 1);
    assert.equal(s.points.length, 32);
    assert.equal(s.points[0].x, 1);   // the first scribble went, whole
    assert.equal(s.undo().length, 8);
    assert.equal(s.points.length, 24);
});

await check("Session adds labelled points, undoes them, knows when it refines, absorbs a box", () => {
    const { Segment } = realm();
    const s = new Segment.Session();
    s.add({ x: 1, y: 2 }, 1);
    assert.equal(Boolean(s.refining), false);
    s.add({ x: 3, y: 4 }, 0);
    same(s.points, [{ x: 1, y: 2, label: 1 }, { x: 3, y: 4, label: 0 }]);
    assert.equal(Boolean(s.refining), true);
    same(s.undo(), [{ x: 3, y: 4, label: 0 }]);
    assert.equal(s.points.length, 1);
    const boxed = new Segment.Session({ box: { x: 0, y: 0, width: 5, height: 5 } });
    assert.equal(Boolean(boxed.refining), false);
    boxed.add({ x: 1, y: 1 }, 1);
    assert.equal(Boolean(boxed.refining), true);
    s.absorb({ bbox: { x: 9, y: 9, width: 1, height: 1 } });
    same(s.bbox, { x: 9, y: 9, width: 1, height: 1 });
    assert.equal(s.undo() !== null && s.undo() === null, true);
    assert.equal(s.refining, false);
    assert.equal(s.seed, null);
    const outline = { type: "Polygon", coordinates: [[[0, 0], [9, 0], [9, 9], [0, 0]]] };
    assert.equal(new Segment.Session({ roiId: "r", seed: outline }).seed, outline);
});

await check("describeView turns a snapshot into the view and channels a request sends", () => {
    const { Segment } = realm();
    same(Segment.describeView({ viewport: { x: 1, y: 2, w: 3, h: 4 },
                                channels: [{ name: "DNA", visible: true, color: "#fff",
                                             range: [1, 2] }, { name: "CD3" }] }),
         { view: { x: 1, y: 2, width: 3, height: 4 },
           channels: [{ name: "DNA", color: "#fff", range: [1, 2] }, { name: "CD3" }] });
});

// -- the token --------------------------------------------------------------------

{
    const r = realm({ routes: (method, url) => (url === "/segment/v1/segment"
        ? { status: 200, data: ANSWER } : { status: 404, data: {} }) });
    const sent = () => r.requests.filter((q) => q.url === "/segment/v1/segment").map((q) => q.body);

    await check("the first click sends no token, with its own view and channels", async () => {
        const answer = await r.Segment.point({ datasource: "s1", snapshot: SNAPSHOT,
                                               points: [{ x: 1400, y: 1300, label: 1 }] });
        assert.equal(answer.token, "tok-1");
        const [body] = sent();
        assert.equal(body.token, undefined);
        same(body.view, { x: 1000, y: 1000, width: 1000, height: 800 });
        same(body.channels, [{ name: "DNA", color: "#0000ff", range: [10, 900] }]);
        same(body.points, [{ x: 1400, y: 1300, label: 1 }]);
    });

    await check("a second click inside the crop sends the token and the FIRST click's view", async () => {
        await r.Segment.point({ datasource: "s1", snapshot: nudged, usePrevious: true,
                                points: [{ x: 1400, y: 1300, label: 1 },
                                         { x: 1500, y: 1350, label: 0 }] });
        const body = sent()[1];
        assert.equal(body.token, "tok-1");
        same(body.view, { x: 1000, y: 1000, width: 1000, height: 800 });
        same(body.channels, [{ name: "DNA", color: "#0000ff", range: [10, 900] }]);
        same(body.options, { use_prev_mask: true, prev_key: "pk-1" });
    });

    await check("a click outside the encoded crop sends no token", async () => {
        await r.Segment.point({ datasource: "s1", snapshot: SNAPSHOT,
                                points: [{ x: 2500, y: 1300, label: 1 }] });
        const body = sent()[2];
        assert.equal(body.token, undefined);
        same(body.options, {});
    });

    await check("...nor does one after forget(), or on another sample", async () => {
        r.Segment.forget();
        await r.Segment.point({ datasource: "s1", snapshot: SNAPSHOT,
                                points: [{ x: 1400, y: 1300, label: 1 }] });
        assert.equal(sent()[3].token, undefined);
        await r.Segment.point({ datasource: "s2", snapshot: SNAPSHOT,
                                points: [{ x: 1400, y: 1300, label: 1 }] });
        assert.equal(sent()[4].token, undefined);
    });

    const outline = { type: "Polygon", coordinates: [[[1100, 1100], [1300, 1100],
                                                      [1300, 1300], [1100, 1100]]] };

    await check("a refine starting afresh sends the region's outline as mask_geometry", async () => {
        r.Segment.forget();
        await r.Segment.point({ datasource: "s1", snapshot: SNAPSHOT, maskGeometry: outline,
                                usePrevious: true, points: [{ x: 1200, y: 1200, label: 1 }] });
        same(sent()[5].options, { mask_geometry: outline });
    });

    await check("...but not once it can start from the previous mask", async () => {
        await r.Segment.point({ datasource: "s1", snapshot: SNAPSHOT, maskGeometry: outline,
                                usePrevious: true, points: [{ x: 1200, y: 1200, label: 1 },
                                                            { x: 1250, y: 1200, label: 0 }] });
        same(sent()[6].options, { use_prev_mask: true, prev_key: "pk-1" });
        await r.Segment.point({ datasource: "s1", snapshot: SNAPSHOT,
                                points: [{ x: 1200, y: 1200, label: 1 }] });
        same(sent()[7].options, {});
    });
}

// -- the one-time setup -------------------------------------------------------------

function setupRealm() {
    const r = realm({ routes: (method, url) => {
        if (url === "/segment/v1/segment") {
            return { status: 409, data: { ok: false, error: { code: "not_ready",
                                                              state: "weights_missing" } } };
        }
        if (url === "/segment/v1/install") {
            return { status: 202, data: { ok: true, state: "downloading",
                                          download: { done: 0, total: 400e6 } } };
        }
        if (url === "/segment/v1/status") return { status: 200, data: r.state };
        return { status: 404, data: {} };
    } });
    r.state = { state: "downloading", download: { done: 0, total: 400e6 } };
    return r;
}

{
    const r = setupRealm();
    let retried = 0;

    await check("a 'model missing' answer starts one install and is reported as setup", async () => {
        const answer = await r.Segment.point({ datasource: "s1", snapshot: SNAPSHOT,
                                               points: [{ x: 1400, y: 1300, label: 1 }],
                                               retry: () => { retried += 1; return true; } });
        same(answer, { ok: false, kind: "setup" });
        assert.equal(r.requests.filter((q) => q.url === "/segment/v1/install"
                                              && q.method === "POST").length, 1);
        assert.equal(r.intervals.length, 1);
    });

    await check("...and opens exactly one setup modal, which never names the model", () => {
        const dialogs = r.dialogs();
        assert.equal(dialogs.length, 1);
        const [dialog] = dialogs;
        assert.equal(dialog.className, "plx-dialog plx-confirm plx-segment-setup");
        assert.equal(dialog.open, true);
        assert.equal(dialog.dataset.state, "downloading");
        const p = parts(dialog);
        assert.equal(p.title.textContent, "Setting up magic select");
        assert.ok(p.note.textContent.length > 0);
        assert.equal(p.bar.hidden, false);
        assert.equal(p.bar.max, 100);
        assert.equal(p.meta[0].textContent, "0 %");
        assert.equal(p.meta[1].textContent, "0 MB of 400 MB");
        assert.equal(p.button.textContent, "Continue in background");
        namesNoModel(visibleText(r.document));
    });

    await check("polling while it downloads updates that modal in place", async () => {
        r.state = { state: "downloading", download: { done: 168e6, total: 400e6 } };
        await r.intervals[0].fn();
        assert.equal(r.dialogs().length, 1);
        const p = parts(r.dialogs()[0]);
        assert.equal(p.bar.value, 42);
        assert.equal(p.meta[0].textContent, "42 %");
        assert.equal(p.meta[1].textContent, "168 MB of 400 MB");
        assert.equal(retried, 0);
    });

    await check("ready: 'Magic select is ready', the click replays once, it closes after 1400 ms", async () => {
        r.state = { state: "ready", ready: true };
        await r.intervals[0].fn();
        same(r.cleared, [1]);
        assert.equal(retried, 1);
        const [dialog] = r.dialogs();
        assert.equal(dialog.dataset.state, "ready");
        const p = parts(dialog);
        assert.equal(p.title.textContent, "Magic select is ready");
        assert.equal(p.note.textContent, "Click an artifact to outline it.");
        assert.equal(p.bar.value, 100);
        namesNoModel(visibleText(r.document));
        const close = r.timeouts.find((t) => t.ms === 1400);
        assert.ok(close, "a 1400 ms close is scheduled");
        close.fn();
        assert.equal(dialog.open, false);
        assert.equal(r.dialogs().length, 0);
    });

    await check("...and nothing replays twice", async () => {
        await r.Segment.ensureReady();
        assert.equal(retried, 1);
        assert.equal(r.intervals.length, 1);
    });
}

await check("a click it could not replay is asked for again when ready", async () => {
    const r = setupRealm();
    r.state = { state: "weights_missing" };
    assert.equal(await r.Segment.ensureReady(() => false), false);
    r.state = { state: "ready" };
    await r.intervals[0].fn();
    assert.equal(parts(r.dialogs()[0]).note.textContent, "Click the artifact again.");
});

await check("'Continue in background' closes the modal and polling does not reopen it", async () => {
    const r = setupRealm();
    r.state = { state: "weights_missing" };
    await r.Segment.ensureReady();
    const [dialog] = r.dialogs();
    parts(dialog).button.click();
    assert.equal(dialog.open, false);
    assert.equal(r.dialogs().length, 0);
    r.state = { state: "downloading", download: { done: 1e6, total: 400e6 } };
    await r.intervals[0].fn();
    await r.intervals[0].fn();
    assert.equal(r.dialogs().length, 0, "the modal came back on the next poll");
});

await check("...until magic select is asked for again", async () => {
    const r = setupRealm();
    r.state = { state: "weights_missing" };
    await r.Segment.ensureReady();
    parts(r.dialogs()[0]).button.click();
    r.state = { state: "downloading", download: { done: 1e6, total: 400e6 } };
    await r.Segment.ensureReady();
    assert.equal(r.dialogs().length, 1);
});

await check("a failed install says it could not be set up, with Close and no bar", async () => {
    const r = realm({ routes: (method, url) => {
        if (url === "/segment/v1/status") return { status: 200, data: { state: "weights_missing" } };
        if (url === "/segment/v1/install") {
            return { status: 500, data: { ok: false, error: { code: "download_failed" } } };
        }
        return { status: 404, data: {} };
    } });
    assert.equal(await r.Segment.ensureReady(), false);
    const dialogs = r.dialogs();
    assert.equal(dialogs.length, 1);
    assert.equal(dialogs[0].dataset.state, "error");
    const p = parts(dialogs[0]);
    assert.equal(p.title.textContent, "Magic select could not be set up");
    assert.equal(p.button.textContent, "Close");
    assert.equal(p.bar.hidden, true);
    assert.equal(r.intervals.length, 0);
    namesNoModel(visibleText(r.document));
});

await check("a failed download keeps the server's reason (URL, HTTP status) out of the modal", async () => {
    const hint = "https://example.org/files/encoder.onnx answered HTTP 404";
    const r = realm({ routes: (method, url) => {
        if (url === "/segment/v1/status") return { status: 200, data: { state: "error", hint } };
        if (url === "/segment/v1/install") {
            return { status: 200, data: { ok: true, state: "error", hint } };
        }
        return { status: 404, data: {} };
    } });
    await r.Segment.ensureReady();
    if (r.intervals.length) await r.intervals[0].fn();
    assert.equal(r.dialogs()[0]?.dataset.state, "error");
    const text = visibleText(r.document);
    assert.ok(/connection/.test(text), text);
    assert.ok(!/https?:|HTTP|404/.test(text), text);
    namesNoModel(text);
});

await check("a download cancelled meanwhile closes the modal", async () => {
    const r = setupRealm();
    r.state = { state: "weights_missing" };
    await r.Segment.ensureReady();
    assert.equal(r.dialogs().length, 1);
    r.state = { state: "weights_missing" };
    await r.intervals[0].fn();
    assert.equal(r.dialogs().length, 0);
});

await check("a 429 is busy", async () => {
    const r = realm({ routes: () => ({ status: 429, data: { ok: false } }) });
    same(await r.Segment.point({ datasource: "s1", snapshot: SNAPSHOT,
                                 points: [{ x: 1400, y: 1300, label: 1 }] }),
         { ok: false, kind: "busy" });
});

await check("a server error is an error with the server's message", async () => {
    const r = realm({ routes: () => ({ status: 500, data: { ok: false,
                                                            error: { message: "Boom." } } }) });
    same(await r.Segment.point({ datasource: "s1", snapshot: SNAPSHOT,
                                 points: [{ x: 1400, y: 1300, label: 1 }] }),
         { ok: false, kind: "error", message: "Boom." });
});

await check("a server without the runtime says once that magic select is not available", async () => {
    const r = realm({ routes: (method, url) => (url === "/segment/v1/status"
        ? { status: 200, data: { state: "not_installed_runtime", hint: "Ask your admin." } }
        : { status: 404, data: {} }) });
    assert.equal(await r.Segment.ensureReady(), false);
    assert.equal(await r.Segment.ensureReady(), false);
    assert.equal(r.told.length, 1);
    assert.equal(r.told[0].title, "Magic select is not available on this server");
    same(r.told[0].body, ["Ask your admin."]);
    assert.equal(r.dialogs().length, 0);
    assert.equal(r.requests.filter((q) => q.url === "/segment/v1/install").length, 0);
    namesNoModel(`${r.told[0].title} ${r.told[0].body.join(" ")}`);
});

await check("?segment=stub answers a closed 24-gon and never fetches", async () => {
    const r = realm({ search: "?segment=stub" });
    assert.equal(r.Segment.stubbed(), true);
    assert.equal(await r.Segment.ensureReady(), true);
    const pending = r.Segment.point({ datasource: "s1",
                                      points: [{ x: 100, y: 100, label: 1 }] });
    await settle();
    assert.equal(r.timeouts.length, 1);
    r.timeouts[0].fn();
    const answer = await pending;
    assert.equal(answer.ok, true);
    assert.equal(answer.geometry.type, "Polygon");
    const ring = answer.geometry.coordinates[0];
    assert.equal(ring.length, 25);
    same(ring[0], ring[24]);
    assert.equal(r.requests.length, 0);
});

await check("KEY is E", () => {
    assert.equal(realm().Segment.KEY, "e");
});

await settle();
process.exit(failures.length ? 1 : 0);
