/**
 * A 3-D mask's Outlines and Filled in the 3-D view: services/maskSurfaces.js.
 *
 * What is pinned: the surfaces are built only where they are drawn (3-D, a 3-D
 * mask, Surface Studio in the licence); the first build is a preview then the
 * standard build, posted once each however often the view is scrubbed; the
 * standard build replaces the preview the moment its mesh is in, with never a
 * frame drawing neither; Outlines is glass and Filled solid, coloured as the
 * cells are; a failure is reported once with the server's reason and retried
 * from the wait; a page that finds the surfaces made posts nothing.
 *
 * The service is run from source in a vm against stand-ins for the surface
 * layers (services/surfaceLayers.js), the view state and the wait.
 *
 * Run directly:  node tests/js/mask_surfaces_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/maskSurfaces.js");

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

const flush = async () => { for (let i = 0; i < 6; i += 1) await new Promise((r) => setTimeout(r, 0)); };

function boot({ licensed = true, labels3d = true, volume = true, mode = "none",
                server = [], createFails = null, lut = null } = {}) {
    const listeners = new Map();
    const win = {
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(fn);
        },
        dispatchEvent(event) {
            (listeners.get(event.type) || []).slice().forEach((fn) => fn(event));
            return true;
        },
    };
    const view = { mode: volume ? "volume" : "plane", t: 0 };
    //: Layers deleted (`DELETE /project/<ds>/layers/<id>`), and adopts after.
    const deleted = [];
    let adopts = 0;
    win.flaskVariables = { datasource: "demo" };
    win.__plexora = {
        layers: { unregister: (id) => { stack.delete(id); } },
        async adoptLayers() { adopts += 1; },
        viewState: {
            enabled: () => true,
            isVolume: () => view.mode === "volume",
            labelsAre3d: () => labels3d,
            get: () => ({ mode: view.mode, t: view.t }),
        },
        viewerControls: { mode },
    };
    win.PlexoraPaid = { allows: (entitlement) => licensed && entitlement === "volume:surfaces" };

    // -- the wait ----------------------------------------------------------
    const waits = [];
    win.PlexoraMaskSurfaceWait = {
        start: (opts) => waits.push(["start", opts || {}]),
        progress: (detail) => waits.push(["progress", detail]),
        ready: () => waits.push(["ready"]),
        failed: (error) => waits.push(["failed", error]),
    };

    // -- the surface layers --------------------------------------------------
    const stack = new Map();
    const posts = [];
    const styles = [];
    const loaded = new Set();
    //: Every moment a mask surface's visibility changed: [id, visible, both
    //: visible flags after it], to prove there is never a frame with neither.
    const frames = [];
    const surfacesShown = { surfaces: true };
    let next = 0;
    const record = (entry) => {
        const layer = { id: entry.id, kind: "surface", visible: entry.visible !== false, opacity: 1,
                        spec: { status: entry.status, surface: entry.spec, render: { ...(entry.render || {}) } } };
        stack.set(entry.id, layer);
        return layer;
    };
    for (const entry of server) record(entry);
    const api = {
        surfaceLayers: () => [...stack.values()],
        list: async () => ({ surfaces: server.map((entry) => ({
            id: entry.id, status: entry.status, spec: entry.spec, role: entry.spec.role,
            stale: Boolean(entry.stale) })) }),
        create: async (body) => {
            posts.push(body);
            if (createFails) throw new Error(createFails);
            const id = `surf-${body.quality}-${body.t}-${next += 1}`;
            const { render, label, replace, ...spec } = body;
            record({ id, status: "pending", spec, render });
            return { layer: id, status: "pending", created: true };
        },
        style(id, patch) {
            const layer = stack.get(id);
            if (!layer) return;
            styles.push([id, { ...patch }]);
            layer.spec.render = { ...layer.spec.render, ...patch };
            if (patch.visible !== undefined) {
                layer.visible = patch.visible;
                frames.push([id, patch.visible]);
            }
        },
        styleOf: (layer) => ({ ...layer.spec.render }),
        cellLut: () => lut,
        shown: () => ({ ...surfacesShown }),
        setShow: (show) => Object.assign(surfacesShown, show),
        renderer: { has: (id) => loaded.has(id), setStyle: (id) => styles.push([id, { lut: true }]) },
    };
    win.PlexoraSurfaceLayers = api;

    const fetch = async (url, options = {}) => {
        if (options.method === "DELETE") deleted.push(decodeURIComponent(String(url).split("/").pop()));
        return { ok: true, status: 200, json: async () => ({}) };
    };
    const context = createContext({
        console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map, Promise, Error, fetch,
        CustomEvent: class CustomEvent {
            constructor(type, init) { this.type = type; this.detail = init?.detail; }
        },
        window: win,
    });
    runInContext(readFileSync(SOURCE, "utf8"), context, { filename: "maskSurfaces.js" });
    const service = win.PlexoraMaskSurfaces;

    const emit = (type, detail) => win.dispatchEvent({ type, detail });
    return {
        service, posts, styles, waits, stack, frames, loaded, surfacesShown, view, deleted,
        get adopts() { return adopts; },
        setLut(value) { lut = value; emit("plexora:cell-lut-changed", { name: "tool" }); },
        setMode(value) {
            win.__plexora.viewerControls.mode = value;
            emit("plexora:cell-mode-changed", { mode: value, volume: view.mode === "volume" });
        },
        setView(value, t = view.t) {
            view.mode = value;
            view.t = t;
            emit("plexora:view-requested", {});
        },
        /** What main.js's poll does when a build lands: the status, then the event. */
        ready(id) {
            stack.get(id).spec.status = "ready";
            emit("plexora:layer-ready", { layer: id });
        },
        failed(id, error) {
            stack.get(id).spec.status = "failed";
            emit("plexora:layer-failed", { layer: id, error });
        },
        progress(id, progress) {
            emit("plexora:layer-progress", { layer: id, progress, message: "Extracting", stageLabel: "Extracting the surface" });
        },
        /** The renderer has the mesh: what surfaceLayers announces after `load`. */
        load(id) {
            loaded.add(id);
            emit("plexora:surface-loaded", { id });
        },
        /** Drawn: on the stack and not hidden (a deleted layer is not). */
        visible(id) { return Boolean(stack.get(id)) && stack.get(id).visible !== false; },
    };
}

// -- where it is built ---------------------------------------------------------

{
    const app = boot({ licensed: false, mode: "outlines" });
    app.service.init();
    await flush();
    check("without Surface Studio nothing is built", app.posts.length === 0, `${app.posts.length}`);
}

{
    const app = boot({ volume: false, mode: "outlines" });
    app.service.init();
    await flush();
    check("on a plane nothing is built: the plane draws the mask from its labels",
        app.posts.length === 0);
    app.setView("volume");
    await flush();
    check("the way into 3-D with Outlines on builds", app.posts.length === 1);
}

{
    const app = boot({ labels3d: false, mode: "outlines" });
    app.service.init();
    await flush();
    check("a 2-D mask is never built as surfaces", app.posts.length === 0);
}

// -- the first build: preview, then standard -------------------------------------

{
    const app = boot();
    app.service.init();
    await flush();
    check("choosing nothing builds nothing", app.posts.length === 0);
    app.setMode("outlines");
    await flush();
    check("Outlines in 3-D posts exactly one build", app.posts.length === 1, JSON.stringify(app.posts));
    const first = app.posts[0] || {};
    check("...the quick preview first",
        first.quality === "preview" && first.type === "label_surfaces" && first.t === 0);
    check("...marked as the mask's own", first.role === "mask");
    check("...looking as Outlines does: glass, see-through",
        first.render?.style === "glass" && first.render?.opacity === 0.35);
    check("...smoothed for display: the staircase taken out", first.smoothing === "strong");
    check("the first build opens the wait's modal",
        app.waits[0]?.[0] === "start" && app.waits[0]?.[1]?.modal !== false, JSON.stringify(app.waits[0]));

    app.setView("volume");
    app.setView("volume");
    app.setMode("filled");
    app.setMode("outlines");
    await flush();
    check("scrubbing and changing the look post nothing more", app.posts.length === 1, `${app.posts.length}`);

    const preview = [...app.stack.keys()][0];
    app.progress(preview, 50);
    const reading = app.waits.filter((w) => w[0] === "progress").pop()?.[1];
    check("the preview fills the first part of the wait's bar",
        reading?.progress === 15, JSON.stringify(reading));
    check("...naming the phase", /Preview/.test(reading?.stageLabel || ""), reading?.stageLabel);

    app.ready(preview);
    await flush();
    check("a landed preview is drawn", app.visible(preview));
    app.load(preview);
    check("...and the standard build is posted next",
        app.posts.length === 2 && app.posts[1].quality === "standard" && app.posts[1].role === "mask");
    const standard = [...app.stack.keys()].find((id) => id !== preview);
    check("...hidden until it lands", !app.visible(standard));

    app.progress(standard, 50);
    check("the standard build fills the rest of the bar",
        app.waits.filter((w) => w[0] === "progress").pop()?.[1]?.progress === 65);

    app.ready(standard);
    check("a landed standard build is made visible (which fetches its mesh)", app.visible(standard));
    check("...the preview kept up while it loads", app.visible(preview));
    check("...and the wait is over", app.waits.some((w) => w[0] === "ready"));

    const before = app.frames.length;
    app.load(standard);
    check("the frame its mesh is in, the preview goes", !app.visible(preview) && app.visible(standard));
    check("...in the same handler: never a state drawing neither",
        app.frames.slice(before).every(([id, on]) => on || id === preview));
    check("the service says what is shown",
        app.service.shownId() === standard && app.service.state() === "ready");
    await flush();
    check("the preview is then deleted: nothing will draw it again",
        app.deleted.includes(preview) && !app.stack.has(preview) && app.adopts >= 1,
        JSON.stringify(app.deleted));
    check("...and only the preview", app.deleted.length === 1 && app.stack.has(standard));

    app.setMode("filled");
    check("Filled draws the same surfaces solid",
        app.stack.get(standard).spec.render.style === "solid" && app.posts.length === 2);
    check("...without bringing the preview back", !app.visible(preview));
    app.setMode("none");
    check("None hides them", !app.visible(standard) && !app.visible(preview));
    check("...and nothing is reported as shown", app.service.shownId() === null);
    app.setMode("centroids");
    check("Centroids (a plane's mode) hides them too", !app.visible(standard));
    app.setMode("outlines");
    check("Outlines brings back only the standard build",
        app.visible(standard) && !app.visible(preview) && app.stack.get(standard).spec.render.style === "glass");

    const report = app.service.report();
    check("the agent's report names the build on screen",
        report.state === "ready" && report.shown === standard && report.standard === standard
        && report.allowed === true, JSON.stringify(report));
}

// -- colours --------------------------------------------------------------------------

{
    const app = boot({ mode: "filled", server: [
        { id: "surf-s", status: "ready", spec: { type: "label_surfaces", role: "mask", quality: "standard", t: 0, smoothing: "strong" } },
    ] });
    app.service.init();
    await flush();
    app.load("surf-s");
    check("with no tool colouring cells, a colour per cell",
        app.stack.get("surf-s").spec.render.colorBy === "id");
    app.setLut({ colors: new Uint8Array(8), maxId: 1 });
    check("a tool's colours by cell id are taken",
        app.stack.get("surf-s").spec.render.colorBy === "cells");
    const updates = app.styles.length;
    app.setLut({ colors: new Uint8Array(8), maxId: 1 });
    check("...and new colours redraw the table, nothing saved",
        app.styles.length === updates + 1 && app.styles.at(-1)[1].lut === true);
    app.setLut(null);
    check("colours taken away go back to a colour per cell",
        app.stack.get("surf-s").spec.render.colorBy === "id");
}

// -- coming back ------------------------------------------------------------------------

{
    const app = boot({ mode: "outlines", server: [
        { id: "surf-p", status: "ready", spec: { type: "label_surfaces", role: "mask", quality: "preview", t: 0, smoothing: "strong" }, visible: false },
        { id: "surf-s", status: "ready", spec: { type: "label_surfaces", role: "mask", quality: "standard", t: 0, smoothing: "strong" } },
    ] });
    app.service.init();
    await flush();
    check("surfaces made before are reused: nothing posted", app.posts.length === 0);
    check("...and the preview left from their first build is deleted",
        app.deleted.length === 1 && app.deleted[0] === "surf-p", JSON.stringify(app.deleted));
    check("...no wait opened", !app.waits.some((w) => w[0] === "start"));
    check("...the standard build drawn", app.visible("surf-s") && !app.visible("surf-p"));
    check("...never hidden and shown again while the server is asked (each is a save)",
        !app.frames.some(([id, on]) => id === "surf-s" && !on), JSON.stringify(app.frames));
}

{
    const app = boot({ mode: "outlines", server: [
        { id: "surf-p", status: "pending", spec: { type: "label_surfaces", role: "mask", quality: "preview", t: 0, smoothing: "strong" } },
    ] });
    app.service.init();
    await flush();
    check("a build already running is followed, not posted again", app.posts.length === 0);
    check("...in the chip only: it is not the first time it was asked for",
        app.waits[0]?.[0] === "start" && app.waits[0][1].modal === false, JSON.stringify(app.waits));
}

{
    const app = boot({ mode: "outlines", server: [
        { id: "surf-old", status: "ready", stale: true,
          spec: { type: "label_surfaces", role: "mask", quality: "standard", t: 0, smoothing: "strong" } },
        { id: "surf-medium", status: "ready",
          spec: { type: "label_surfaces", role: "mask", quality: "standard", t: 0, smoothing: "medium" } },
        { id: "surf-other", status: "ready",
          spec: { type: "label_surfaces", quality: "standard", t: 0 } },
    ] });
    app.service.init();
    await flush();
    check("a surface of a mask that changed since, or of an older recipe, is built again",
        app.posts.length === 1 && app.posts[0].quality === "preview");
    const preview = [...app.stack.keys()].find((id) => id.startsWith("surf-preview"));
    app.ready(preview);
    await flush();
    app.load(preview);
    const standard = [...app.stack.keys()].find((id) => id.startsWith("surf-standard"));
    app.ready(standard);
    app.load(standard);
    await flush();
    check("...and once the new one is drawn the superseded ones are deleted",
        app.deleted.includes("surf-old") && app.deleted.includes("surf-medium")
        && app.deleted.includes(preview), JSON.stringify(app.deleted));
    check("...never Surface Studio's own", !app.deleted.includes("surf-other"));
    check("...neither is drawn meanwhile, nor is Surface Studio's own taken for the mask's",
        !app.visible("surf-old") && !app.visible("surf-medium") && app.visible("surf-other"));
}

// -- failure ------------------------------------------------------------------------------

{
    const app = boot({ mode: "filled" });
    app.service.init();
    await flush();
    const preview = [...app.stack.keys()][0];
    app.failed(preview, "the mask could not be read");
    const failed = app.waits.filter((w) => w[0] === "failed");
    check("a failed build is reported once, with the server's reason",
        failed.length === 1 && failed[0][1] === "the mask could not be read", JSON.stringify(failed));
    check("...and the state says so", app.service.state() === "failed");
    await app.service.retry();
    check("Retry posts the failed build again, replacing it",
        app.posts.length === 2 && app.posts[1].quality === "preview" && app.posts[1].replace === true);
    check("...and reopens the wait", app.waits.at(-1)[0] === "start");
}

{
    const app = boot({ mode: "filled", createFails: "this mask has no objects" });
    app.service.init();
    await flush();
    check("a refused request says why, in the wait",
        app.waits.some((w) => w[0] === "failed" && w[1] === "this mask has no objects"),
        JSON.stringify(app.waits));
}

// -- frames ---------------------------------------------------------------------------------

{
    const app = boot({ mode: "outlines", server: [
        { id: "surf-s0", status: "ready", spec: { type: "label_surfaces", role: "mask", quality: "standard", t: 0, smoothing: "strong" } },
    ] });
    app.service.init();
    await flush();
    app.load("surf-s0");
    app.setView("volume", 1);
    await flush();
    check("another frame is its own build", app.posts.length === 1 && app.posts[0].t === 1);
    check("...and the last frame's surfaces are not drawn over it", !app.visible("surf-s0"));
}

{
    const app = boot({ mode: "none" });
    app.surfacesShown.surfaces = false;
    app.service.init();
    await flush();
    app.setMode("filled");
    await flush();
    check("choosing Filled with the view set to the volume alone shows surfaces again",
        app.surfacesShown.surfaces === true);
}

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
