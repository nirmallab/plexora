// Kept answers (client services/keptAnswers.js) in node, with Cache Storage,
// the route table, Python and the picks stood in. Prints JSON; the pytest
// wrapper is tests/test_kept_answers.py.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(here, "..", "..", "plexora", "client", "src", "js", "services",
                                      "keptAnswers.js"), "utf8");

// -- Cache Storage --------------------------------------------------------------
const stores = new Map();
const caches = {
    async open(name) {
        if (!stores.has(name)) stores.set(name, new Map());
        const store = stores.get(name);
        // A key is a URL or a Request (`keys()` gives requests).
        const k = (key) => (typeof key === "string" ? key : key.url);
        return {
            async match(key) {
                const hit = store.get(k(key));
                return hit ? new Response(hit.bytes.slice(), { status: 200, headers: hit.headers }) : undefined;
            },
            async put(key, response) {
                store.set(k(key), { bytes: new Uint8Array(await response.arrayBuffer()),
                                         headers: [...response.headers.entries()] });
            },
            async delete(key) { return store.delete(k(key)); },
            async keys() { return [...store.keys()].map((url) => ({ url })); },
        };
    },
};

// -- the route table (services/backend.js `classify`), the rows used here ------------
const ROWS = [
    [/^\/config$/, "serve_config"], [/^\/get_channel_names$/, "get_channel_names"],
    [/^\/get_saved_channel_list$/, "get_saved_channel_list"], [/^\/view_transform\/[^/]+$/, "get_view_transform"],
    [/^\/resource_routing$/, "resource_routing"], [/^\/get_all_cells\/.+$/, "get_all_cells"],
    [/^\/save_channel_list$/, "save_channel_list"], [/^\/get_channel_gmm$/, "get_channel_gmm"],
    [/^\/[^/]+$/, "image_viewer"],
];
const classify = (url) => {
    const pathname = new URL(String(url), "http://site.test/").pathname;
    const row = ROWS.find(([pattern]) => pattern.test(pathname));
    return row ? { endpoint: row[1] } : null;
};

// -- Python: answers by route, and what it was asked ------------------------------------
const answers = new Map();
const asked = [];
const python = {
    async fetch(url, init) {
        const where = new URL(String(url), "http://site.test/");
        const route = where.pathname + where.search;
        asked.push(route);
        // An answer may be a promise: one Python takes its time over.
        const body = await (answers.get(route) ?? answers.get(where.pathname));
        const status = body === undefined ? 404 : 200;
        const bytes = new TextEncoder().encode(body === undefined ? "{}" : body);
        // As services/pythonClient.js `send` does: every answer observed.
        if (python.observer) python.observer.observe(where.href, "GET", status, [["Content-Type", "application/json"]], bytes, []);
        return new Response(bytes, { status, headers: { "Content-Type": "application/json" } });
    },
    async nodeSpec(name) { asked.push(`spec:${name}`); return specs.get(name) || null; },
};
const specs = new Map();

// -- the picks --------------------------------------------------------------------
const reopened = [];
const files = new Map();
const picks = {
    async reopen(ids) {
        reopened.push(...ids);
        for (const id of ids) if (id === "abc") files.set("/picked/abc/image.ome.tif", { size: 1 });
        return { opened: ids };
    },
    fileFor: (p) => files.get(p) || null,
    filesUnder: () => [],
};
let revalidated = 0;

const window = {
    location: { href: "http://site.test/image", origin: "http://site.test" },
    caches, performance, setTimeout, clearTimeout,
    PlexoraBackend: { classify },
    PlexoraPython: python,
    PlexoraPicks: picks,
    PlexoraNode: { revalidate: () => { revalidated += 1; } },
};
const warnings = [];
const console = { warn: (...a) => warnings.push(a.join(" ")), log() {}, error() {} };
new Function("window", "console", source)(window, console);
const K = window.PlexoraKept;

const enc = (text) => new TextEncoder().encode(text);
const json = (value) => enc(JSON.stringify(value));
const text = async (response) => (response ? new TextDecoder().decode(await response.arrayBuffer()) : null);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const out = {};

// Normalising: cache-busters out, the query sorted, the project found.
K.configure({ stamp: "1.0|py/1/" });
const at = (endpoint, url) => { const w = K.locate(endpoint, url); return { normal: w.normal, project: w.project }; };
out.locate = {
    config: at("serve_config", "/config?t=1794194494735"),
    names: at("get_channel_names", "/get_channel_names?shortNames=true&datasource=image"),
    transform: at("get_view_transform", "/view_transform/my%20image"),
    page: at("image_viewer", "/my%20image"),
};

// Kept: a kept route's 200; not another route's, nor a failure, a fragment,
// a part of a page, or a routing answer that names a node.
const page = `<!doctype html><html><script type="application/json" id="plexora-variables">{"datasource": "image"}</script></html>`;
const html = [["Content-Type", "text/html; charset=utf-8"]];
const type = [["Content-Type", "application/json"]];
K.observe("http://site.test/image", "GET", 200, html, enc(page), [["Accept", "text/html"]]);
K.observe("http://site.test/image", "GET", 200, html, enc("<div>fragment</div>"), [["X-Plexora-Fragment", "1"]]);
K.observe("http://site.test/config?t=1", "GET", 200, type, json({ image: { name: "image", lastOpenedAt: "a" } }));
K.observe("http://site.test/get_channel_names?datasource=image", "GET", 200, type, json(["DAPI"]));
K.observe("http://site.test/get_saved_channel_list?datasource=image", "GET", 200, type, json([{ channel: "DAPI", r: 35 }]));
K.observe("http://site.test/view_transform/image", "GET", 200, type, json({ degrees: 0 }));
K.observe("http://site.test/get_all_cells/float32/?datasource=image", "GET", 200, type, json([1, 2]));
K.observe("http://site.test/resource_routing?datasource=image", "GET", 200, type, json({ routes: { image: "http://node" } }));
K.observe("http://site.test/get_channel_names?datasource=other", "GET", 200, type, json(["X"]));
K.observe("http://site.test/get_channel_names?datasource=other", "GET", 500, type, json({}));
await wait(50);
const keys = [...(stores.get("plexora-kept") || new Map()).keys()].map((k) => decodeURIComponent(new URL(k).pathname));
out.keptKeys = keys.sort();
out.keptPage = await text(await (await caches.open("plexora-kept")).match(K.locate("image_viewer", "/image").key));

// Before Python is up: kept GETs answered, and recorded against the page.
let resolveStart;
const started = new Promise((resolve) => { resolveStart = resolve; });
K.starting(started);
const framePage = {};
const otherPage = {};
const req = (url, method = "GET") => ({ url: `http://site.test${url}`, method });
out.waiting = K.waiting();
const config = await K.answer(req("/config?t=999"), framePage);
out.config = { body: await text(config), kept: config.headers.get("X-Plexora-Kept") };
out.names = await text(await K.answer(req("/get_channel_names?datasource=image"), framePage));
out.saved = await text(await K.answer(req("/get_saved_channel_list?datasource=image"), framePage));
out.cells = await K.answer(req("/get_all_cells/float32/?datasource=image"), framePage);
out.pageByFetch = await K.answer(req("/image"), framePage);
out.post = await K.answer(req("/save_channel_list", "POST"), framePage);
out.otherClearedBefore = await K.cleared(otherPage);

// The spec: the kept one given while Python starts.
specs.set("image", { served: true, path: "/picked/abc/image.ome.tif", tile: [512, 512] });
const fromPython = await K.spec("image", () => python.nodeSpec("image"));  // waiting, none kept: Python's
await wait(20);
const kept = await K.spec("image", async () => { throw new Error("Python asked while a spec is kept"); });
out.spec = { fromPython: Boolean(fromPython), kept: kept && kept.path };

// The page shown at once: page, spec, and the pick the spec names.
out.early = await K.early("/image");
out.reopened = reopened.slice();
out.earlyOther = await K.early("/other");
out.earlyQuery = await K.early("/image?tool=gating");
out.earlyOtherQuery = await K.early("/image?view=slab");

// Python up, every answer the same but for /config's lastOpenedAt: the
// writes go.
answers.set("/config", JSON.stringify({ image: { name: "image", lastOpenedAt: "b" } }));
answers.set("/get_channel_names?datasource=image", JSON.stringify(["DAPI"]));
answers.set("/get_saved_channel_list?datasource=image", JSON.stringify([{ channel: "DAPI", r: 35 }]));
resolveStart();
await started;
out.waitingAfter = K.waiting();
out.cleared = await K.cleared(framePage);
out.checked = await K.checked(framePage);
out.revalidated = revalidated;
out.afterUp = await K.answer(req("/config"), framePage);
out.asked = asked.slice();

// A second start, with what was kept no longer so: the page's writes refused.
const second = {};
let resolveSecond;
const started2 = new Promise((resolve) => { resolveSecond = resolve; });
// A new page of the host (a reload): a fresh copy of the service, the same
// Cache Storage.
const window2 = { ...window, PlexoraKept: undefined };
new Function("window", "console", source)(window2, console);
const K2 = window2.PlexoraKept;
K2.configure({ stamp: "1.0|py/1/" });
python.observer = K2;
K2.starting(started2);
await K2.answer(req("/get_saved_channel_list?datasource=image"), second);
answers.set("/get_saved_channel_list?datasource=image", JSON.stringify([{ channel: "DAPI", r: 255 }]));
resolveSecond();
await started2;
out.changedCleared = await K2.cleared(second);
out.changedChecked = await K2.checked(second);
out.report = K2.report();
out.rekept = await text(await (await caches.open("plexora-kept")).match(
    K2.locate("get_saved_channel_list", "/get_saved_channel_list?datasource=image").key));

// A write that went through: the project's state asked again once quiet.
asked.length = 0;
answers.set("/view_transform/image", JSON.stringify({ degrees: 90 }));
answers.set("/image", page);
K2.observe("http://site.test/image", "GET", 200, html, enc(page), []);  // the project, as last shown
await wait(20);
K2.observe("http://site.test/view_transform/image", "PUT", 200, type, json({ success: true }));
await wait(3500);
out.recaptured = asked.slice().sort();

// The mask's pyramid built after the spec was kept (`labels.pending` then,
// served now): the build finishing, not a change. A mask the page was served
// that is no longer so: a change.
const specCase = async (keptSpec, freshSpec) => {
    specs.set("image", keptSpec);
    await K.spec("image", () => python.nodeSpec("image"));  // Python up in K: kept as given
    await wait(20);
    const w = { ...window, PlexoraKept: undefined };
    new Function("window", "console", source)(w, console);
    const k = w.PlexoraKept;
    k.configure({ stamp: "1.0|py/1/" });
    let go;
    const up = new Promise((resolve) => { go = resolve; });
    k.starting(up);
    const p = {};
    await k.answer(req("/get_channel_names?datasource=image"), p);
    await k.spec("image", async () => { throw new Error("Python asked while a spec is kept"); });
    specs.set("image", freshSpec);
    go();
    await up;
    return k.checked(p);
};
const image = { served: true, path: "/picked/abc/image.ome.tif", tile: [512, 512] };
const labels = { served: true, path: "/derived/k/cell.labels.pyramid.ome.tiff", levels: [[0, 10, 10]] };
out.specPending = await specCase({ ...image, labels: { served: false, pending: true, reason: "the mask is being prepared" } },
                                 { ...image, labels });
// The project loaded twice in the run that kept the spec (the mask attached),
// once in this one: the same files.
out.specGeneration = await specCase({ ...image, generation: 2, labels }, { ...image, generation: 1, labels });
out.specMoved = await specCase({ ...image, labels }, { ...image, labels: { ...labels, path: "/derived/j/other.tiff" } });
// The check answers at the first change; the rest is still asked, and the
// whole of it logged, a moment later.
await wait(20);
out.specWarnings = warnings.filter((w) => w.includes("spec:image"));
for (const w of out.specWarnings) warnings.splice(warnings.indexOf(w), 1);

// Answers made from the pixels are asked last, and the first answer found
// changed answers the check at once: a fit that takes seconds on a Python
// just started does not hold up what a cheap answer has already settled.
{
    const gmm = "/get_channel_gmm?channel=DAPI&datasource=image";
    const names = "/get_channel_names?datasource=image";
    K.observe(`http://site.test${gmm}`, "GET", 200, type, json({ means: [1, 2] }));
    K.observe(`http://site.test${names}`, "GET", 200, type, json(["DAPI"]));
    await wait(20);
    const w = { ...window, PlexoraKept: undefined };
    new Function("window", "console", source)(w, console);
    const k = w.PlexoraKept;
    k.configure({ stamp: "1.0|py/1/" });
    let go;
    const up = new Promise((resolve) => { go = resolve; });
    k.starting(up);
    const p = {};
    await k.answer(req(gmm), p);      // given first
    await k.answer(req(names), p);
    let fitted;
    answers.set(gmm, new Promise((resolve) => { fitted = () => resolve(JSON.stringify({ means: [1, 2] })); }));
    answers.set(names, JSON.stringify(["DNA"]));
    asked.length = 0;
    go();
    await up;
    const early = await Promise.race([k.checked(p), wait(500).then(() => "held by the fit")]);
    out.pixelsLast = { asked: asked.slice(), early, cleared: await Promise.race([k.cleared(p), wait(500).then(() => "held")]) };
    fitted();
    await wait(20);
    out.pixelsLast.results = k.report().results;
    for (const w of warnings.filter((w) => w.includes("DNA") || w.includes(names))) warnings.splice(warnings.indexOf(w), 1);
}

// Auto-contrast moving on by itself: channels calibrated since, not a change;
// a kept channel's window no longer so, a change.
const contrast = (channels, extra = {}) => json({ state: "computing", rev: 1, channels, calibrated: Object.keys(channels), ...extra });
out.contrastMovedOn = K.same("get_display_contrast", contrast({ A: { lo: 1, hi: 9 } }),
                             contrast({ A: { lo: 1, hi: 9 }, B: { lo: 2, hi: 7 } }, { state: "ready", rev: 3 }));
out.contrastChanged = K.same("get_display_contrast", contrast({ A: { lo: 1, hi: 9 } }),
                             contrast({ A: { lo: 1, hi: 8 } }, { state: "ready", rev: 3 }));

// Another version's answers are not this one's.
const window3 = { ...window, PlexoraKept: undefined };
new Function("window", "console", source)(window3, console);
window3.PlexoraKept.configure({ stamp: "2.0|py/2/" });
window3.PlexoraKept.starting(new Promise(() => {}));
await wait(50);
out.otherVersion = await window3.PlexoraKept.answer(req("/get_channel_names?datasource=image"), {});
out.purged = [...stores.get("plexora-kept").keys()].filter((k) => !k.includes("v=2.0")).length;

out.equalVolatile = K.same("serve_config", json({ a: { lastOpenedAt: "x", n: 1 } }), json({ a: { n: 1, lastOpenedAt: "y" } }));
out.unequal = K.same("get_channel_names", json(["A"]), json(["B"]));
out.warnings = warnings;
console.log = globalThis.console.log;
globalThis.console.log(JSON.stringify(out));
