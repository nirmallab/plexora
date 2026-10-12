/**
 * Quick Surface (views/surfaceStudio.js) over the surface service
 * (services/surfaceLayers.js), with a stubbed page and server.
 *
 * Pinned:
 * - on a licence without Surface Studio the dock does not open and asks the
 *   server nothing (not granted, not shown); on a licence that lapsed while
 *   the page was open, the server's refusal opens the Paid modal with its
 *   own words;
 * - the slider starts at the server's suggestion, over the histogram's range;
 * - dragging asks for ONE preview after the debounce, and a newer preview
 *   aborts the one still in flight;
 * - what is posted is the isovalue in raw units, the channel and the choices
 *   made -- never a display window;
 * - creating takes the new layer on (adoptLayers) and closes the dock.
 *
 * Run directly:  node tests/js/surface_studio_probe.mjs
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
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// -- a page just big enough -------------------------------------------------------

class Element {
    constructor(tag) {
        this.tagName = String(tag).toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.attributes = {};
        this.dataset = {};
        this.style = { setProperty() {}, removeProperty() {} };
        this.listeners = {};
        this.textContent = "";
        this.value = "";
        this.hidden = false;
        this.className = "";
        this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
    }
    get options() { return this.children.filter((c) => c.tagName === "OPTION"); }
    get firstChild() { return this.children[0] || null; }
    appendChild(child) {
        child.parentNode?.children.splice(child.parentNode.children.indexOf(child), 1);
        child.parentNode = this;
        this.children.push(child);
        return child;
    }
    append(...nodes) { nodes.forEach((n) => this.appendChild(n)); }
    insertBefore(child) { return this.appendChild(child); }
    remove() {
        if (this.parentNode) this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
        this.parentNode = null;
    }
    contains(node) { return node === this || this.children.some((c) => c.contains(node)); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    removeEventListener() {}
    fire(type, extra = {}) { (this.listeners[type] || []).forEach((fn) => fn({ type, target: this, ...extra })); }
    getContext() { return null; }
    getBoundingClientRect() { return { left: 0, top: 0, width: 280, height: 56 }; }
    get clientWidth() { return 280; }
    get clientHeight() { return 56; }
    set innerHTML(_) {}
    find(predicate) {
        if (predicate(this)) return this;
        for (const child of this.children) {
            const found = child.find(predicate);
            if (found) return found;
        }
        return null;
    }
}

const wrapper = new Element("div");
const document = {
    createElement: (tag) => new Element(tag),
    getElementById: (id) => (id === "openseadragon_wrapper" ? wrapper : null),
    addEventListener() {},
    activeElement: null,
    querySelector: () => null,
};

// -- the server ---------------------------------------------------------------------

let licensed = false;
const requests = [];
const LICENSE = { code: "license_required", entitlement: "volume:surfaces", label: "Surface Studio",
                  summary: "Surfaces in the 3-D view.", state: "free" };
function respond(status, body, buffer = null) {
    return { ok: status < 400, status, headers: { get: () => null },
             json: async () => body, clone() { return this; },
             arrayBuffer: async () => buffer };
}
function emptyMesh(header) {
    const text = JSON.stringify({ ...header, arrays: [] });
    const bytes = new TextEncoder().encode(text);
    const out = new Uint8Array(8 + bytes.length);
    out.set([80, 88, 77, 83]);
    new DataView(out.buffer).setUint32(4, bytes.length, true);
    out.set(bytes, 8);
    return out.buffer;
}
function fetchImpl(url, options = {}) {
    const request = { url, options, aborted: false };
    requests.push(request);
    options.signal?.addEventListener?.("abort", () => { request.aborted = true; });
    if (!licensed && /surfaces(\/(suggest|preview|estimate))?(\?|$)/.test(url)
            && !(url.endsWith("/surfaces") && !options.method)) {
        return Promise.resolve(respond(403, { success: false, license: LICENSE }));
    }
    if (url.includes("/suggest")) {
        return Promise.resolve(respond(200, {
            default: 1240, thresholds: { otsu: 1240, triangle: 900, gmm: 1500 },
            histogram: { lo: 100, hi: 4000, counts: new Array(256).fill(3), log: true } }));
    }
    if (url.includes("/estimate")) {
        return Promise.resolve(respond(200, { ok: true, plan: { level: 1, voxels: 2e6 } }));
    }
    if (url.includes("/preview")) {
        return new Promise((resolve) => setTimeout(() => resolve(respond(200, null,
            emptyMesh({ preview: true, level: 2, triangles: 120, report: { pieces_kept: 3 },
                        contours: {} }))), 60));
    }
    if (url.endsWith("/surfaces") && options.method === "POST") {
        return Promise.resolve(respond(202, { layer: "surf-abc", status: "pending", created: true }));
    }
    return Promise.resolve(respond(404, {}));
}

// -- the page's globals ----------------------------------------------------------------

const explained = [];
let hinted = false;
let adopted = 0;
const viewState = {
    enabled: () => true, depth: 20, isVolume: () => true, labelsAre3d: () => false,
    get: () => ({ t: 0, z: 10, mode: "volume", slab: { z0: 0, z1: 19 }, volume: {} }),
    info: { spacing: { x: { value: 0.5 }, y: { value: 0.5 }, z: { value: 2, source: "metadata" } } },
    set() {},
};
const box = {
    console, setTimeout, clearTimeout, URLSearchParams, TextDecoder, TextEncoder, AbortController,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: () => true, addEventListener() {},
    document, fetch: fetchImpl, plexoraUrl: (path) => `/${path}`,
    flaskVariables: { datasource: "stack" },
    // The page's render-time hint (paidFeature.js `allows`).
    PlexoraPaid: { allows: (entitlement) => hinted && entitlement.startsWith("volume"),
                   explain: (license) => explained.push(license) },
    PlexoraToast: { show() {} },
    PlexoraCanvasLayout: { place: (node) => { wrapper.appendChild(node); return () => node.remove(); } },
    __plexora: {
        viewState,
        adoptLayers: async () => { adopted += 1; },
        seaDragonViewer: { config: { imageData: [{ fullname: "Area" }, { fullname: "DNA" },
                                                 { fullname: "CD31" }] },
                           layerStack: { layers: () => [], subscribe: () => () => {}, get: () => null } },
        viewerSidebar: { channelSlots: [{ name: "CD31", enabled: true }],
                         dataLayer: { getFullChannelName: (name) => name } },
    },
};
box.window = box;
box.globalThis = box;
const context = createContext(box);
// slider.js first, as base.html loads it: the threshold is a PlexoraSlider.
for (const file of ["views/slider.js", "views/surfaceRenderer.js", "services/surfaceLayers.js",
                   "views/surfaceStudio.js"]) {
    runInContext(readFileSync(join(JS, file), "utf8"), context);
}
const Studio = box.PlexoraSurfaceStudio;
box.PlexoraSurfaceLayers.init({ datasource: "stack", layerStack: box.__plexora.seaDragonViewer.layerStack });

// -- the licence ---------------------------------------------------------------------

const hidden = await Studio.open({ channel: "CD31" });
check("on Free the dock does not open and asks the server nothing",
      hidden === false && !Studio.isOpen() && requests.length === 0 && explained.length === 0);
// The page was told Paid; the server no longer agrees (a licence that lapsed).
hinted = true;
const refused = await Studio.open({ channel: "CD31" });
check("a licence that lapsed: the server refuses and the dock closes",
      refused === false && !Studio.isOpen());
check("and the Paid modal opens with the server's words",
      explained.length === 1 && explained[0].label === "Surface Studio");

// -- with the licence ------------------------------------------------------------------

licensed = true;
const opened = await Studio.open({ channel: "CD31" });
const state = Studio._state();
check("with the licence the dock opens on the channel asked for", opened === true && Studio.isOpen()
      && state.channel === "CD31");
check("the threshold starts at the suggestion, in raw units", state.iso === 1240
      && state.ui.slider.min === "100" && state.ui.slider.max === "4000");
await sleep(220);
let previews = requests.filter((r) => r.url.includes("/preview"));
check("one preview for the starting threshold", previews.length === 1
      && previews[0].url.includes("iso=1240") && previews[0].url.includes("channel=CD31"));
await sleep(100);

const before = requests.filter((r) => r.url.includes("/preview")).length;
for (const value of [1300, 1350, 1400]) {
    state.ui.slider.value = String(value);
    state.ui.slider.fire("input");
}
await sleep(40);
check("a drag asks nothing until it rests", requests.filter((r) => r.url.includes("/preview")).length === before);
await sleep(200);
previews = requests.filter((r) => r.url.includes("/preview")).slice(before);
check("then one preview, for where it rests", previews.length === 1 && previews[0].url.includes("iso=1400"),
      previews.map((r) => r.url).join(" "));
state.ui.slider.value = "1500";
state.ui.slider.fire("input");
await sleep(170);
check("a new resting place asks again", requests.filter((r) => r.url.includes("/preview"))
      .slice(before).some((r) => r.url.includes("iso=1500")));
state.ui.slider.value = "1600";
state.ui.slider.fire("input");
await sleep(170);
const all = requests.filter((r) => r.url.includes("/preview"));
check("the superseded request is aborted", all[all.length - 2].aborted === true,
      all.map((r) => `${r.url.split("iso=")[1]?.split("&")[0]}:${r.aborted}`).join(" "));
await sleep(120);
check("the dock says what the preview is", /Preview · level 2/.test(state.ui.status.textContent),
      state.ui.status.textContent);

// -- what is made ---------------------------------------------------------------------

const body = Studio._body();
check("what is posted is the raw isovalue, the channel and the choices",
      body.type === "isosurface" && body.channel === "CD31" && body.iso === 1600
      && body.smoothing === "light" && body.quality === "standard");
check("and no display window", !("window" in body) && !("range" in body) && !("w" in body));

state.ui.create.fire("click");
await sleep(30);
const posted = requests.find((r) => r.url.endsWith("/surfaces") && r.options.method === "POST");
check("create posts the request", posted && JSON.parse(posted.options.body).iso === 1600);
check("and takes the new layer on, closing the dock", adopted === 1 && !Studio.isOpen());

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
