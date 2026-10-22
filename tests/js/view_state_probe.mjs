/**
 * Where the viewer is in a volume (services/viewState.js), in node.
 *
 * The properties worth pinning are the ones a user feels and cannot see the
 * cause of: a planar image must get no view at all (every 2-D tile address
 * unchanged); a scrub must not queue one tile rebuild per plane; a slab that
 * follows z must keep its thickness at the ends of the stack; the URL wins
 * over what was saved; and the badge must say "MIP" for a projection. The
 * 3-D view's area: cleaned to the image, the cut kept inside it and moved
 * with it (unless the change says where the cut is), V's way into 3-D and
 * back, and the areas saved to come back to.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const source = readFileSync(join(REPO, "plexora/client/src/js/services/viewState.js"), "utf8");
const geometry = readFileSync(join(REPO, "plexora/client/src/js/services/inspectGeometry.js"), "utf8");

const events = [];
const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    URLSearchParams,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: (event) => { events.push(event); return true; },
    location: { search: "" },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
const context = createContext(sandbox);
runInContext(geometry, context);
runInContext(source, context);
const ViewState = sandbox.PlexoraViewState;
assert.ok(ViewState, "the service registers itself");

const VOLUME = {
    volumetric: true, depth: 40, frames: 1, width: 2000, height: 1500,
    default_view: "z20",
    spacing: { x: { value: 0.5, unit: "µm", source: "metadata" },
               y: { value: 0.5, unit: "µm", source: "metadata" },
               z: { value: 2, unit: "µm", source: "metadata" } },
};

function fakeFetch(routes, puts = []) {
    return async (url, options) => {
        if (options?.method === "PUT") {
            puts.push({ url, body: JSON.parse(options.body) });
            return { ok: true, json: async () => ({}) };
        }
        const key = Object.keys(routes).find((path) => url.includes(path));
        if (!key) return { ok: false, json: async () => null };
        return { ok: true, json: async () => routes[key] };
    };
}

const settle = (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms));
let checks = 0;
function check(condition, message) {
    assert.ok(condition, message);
    checks += 1;
}

// -- a planar image has no view state at all ------------------------------
{
    const vs = new ViewState("flat", { fetchImpl: fakeFetch({
        "volume/flat/info": { volumetric: false }, "view_state/flat": {} }) });
    await vs.load({ search: "?z=4" });
    check(!vs.enabled(), "a planar image is not a volume");
    check(vs.spec() === "", "and its tiles name no view");
    check(vs.label() === "", "and the badge says nothing");
    vs.set({ z: 3 });
    check(vs.get() === null, "and set() is a no-op on it");
}

// -- a volume opens on the default (middle) plane ---------------------------
{
    const vs = new ViewState("vol", { fetchImpl: fakeFetch({
        "volume/vol/info": VOLUME, "view_state/vol": {} }) });
    await vs.load({ search: "" });
    check(vs.enabled(), "a 40-plane stack is a volume");
    check(vs.get().z === 20 && vs.spec() === "z20", "it opens on the default plane");
    check(vs.label() === "z 20/40 · 40 µm", `the badge gives the plane and its depth (${vs.label()})`);
    check(!vs.isDerived(), "a plane is not a derived picture");
}

// -- the URL wins over what was saved, which wins over the default ----------
{
    const saved = { z: 7, mode: "plane" };
    const vs = new ViewState("vol", { fetchImpl: fakeFetch({
        "volume/vol/info": VOLUME, "view_state/vol": saved }) });
    await vs.load({ search: "" });
    check(vs.get().z === 7, "the saved plane is reopened");
    const linked = new ViewState("vol", { fetchImpl: fakeFetch({
        "volume/vol/info": VOLUME, "view_state/vol": saved }) });
    await linked.load({ search: "?view=z4-9mean" });
    check(linked.spec() === "z4-9mean", `a launch link's view wins (${linked.spec()})`);
    check(linked.isDerived(), "and a projection is a derived picture");
    check(linked.label().startsWith("Mean z 4–9 · 6 planes"), linked.label());
    const clipped = new ViewState("vol", { fetchImpl: fakeFetch({
        "volume/vol/info": VOLUME, "view_state/vol": { z: 999 } }) });
    await clipped.load({ search: "" });
    check(clipped.get().z === 39, "a saved plane past the end is clamped, not refused");
}

// -- a scrub coalesces into a few swaps -------------------------------------
{
    const puts = [];
    const vs = new ViewState("vol", { fetchImpl: fakeFetch({
        "volume/vol/info": VOLUME, "view_state/vol": {} }, puts), saveDelay: 5 });
    await vs.load({ search: "" });
    const applied = [];
    vs.applier = async (spec) => {
        applied.push(spec);
        await settle(15);
    };
    for (let i = 0; i < 50; i += 1) vs.step(i % 2 ? 1 : 1);
    await vs.settled();
    await settle(40);
    check(vs.get().z === 39, "fifty steps from 20 stop at the last plane");
    check(applied.length <= 3, `a scrub is a few swaps, not fifty (${applied.length})`);
    check(applied[applied.length - 1] === "z39", "and the last swap is the plane the user is on");
    check(vs.appliedSpec() === "z39", "and the applied plane is the current one");
    check(puts.length >= 1 && puts[puts.length - 1].body.z === 39,
        "the position is saved, debounced");
    check(events.some((e) => e.type === "plexora:view-changed"),
        "and the viewer is told when the picture shows it");
}

// -- a clipped following slab: cut at the ends, outlines from its own plane --
// The thresholding panel's slab (plugins/gating/static/gatingPlanes.js): the
// planes around z that exist, never a slab centred somewhere else, and a 3-D
// mask's outlines from z -- the plane the user is on -- not the slab's middle.
{
    const vs = new ViewState("clip", { fetchImpl: fakeFetch({
        "volume/clip/info": { ...VOLUME, segmentation: { ndim: 3 } }, "view_state/clip": {} }) });
    await vs.load({ search: "" });
    vs.set({ z: 20 });
    vs.set({ mode: "slab", slab: { thickness: 5, op: "mean", follow: true, clip: true } });
    check(vs.spec() === "z18-22mean", `centred on the plane (${vs.spec()})`);
    check(vs.labelSpec() === "z20", `the outlines are the plane's (${vs.labelSpec()})`);
    vs.set({ z: 1 });
    check(vs.spec() === "z0-3mean", `cut at the first plane, not slid down (${vs.spec()})`);
    check(vs.labelSpec() === "z1", `and the outlines stay the plane's (${vs.labelSpec()})`);
    vs.set({ z: 39 });
    check(vs.spec() === "z37-39mean", `cut at the last plane (${vs.spec()})`);
    const ahead = vs.movedTo(38);
    check(vs.spec(ahead) === "z36-39mean" && vs.labelSpec(ahead) === "z38"
          && vs.spec() === "z37-39mean", "movedTo says what a step would show, and changes nothing");
    vs.set({ slab: { z0: 4, z1: 9, follow: false } });
    check(vs.labelSpec() === "z6" && !vs.get().slab.clip,
          `a fixed slab keeps its middle plane's outlines, and does not clip (${vs.labelSpec()})`);
}

// -- a following slab keeps its thickness, even at the ends -----------------
{
    const vs = new ViewState("vol", { fetchImpl: fakeFetch({
        "volume/vol/info": VOLUME, "view_state/vol": {} }) });
    await vs.load({ search: "" });
    vs.set({ mode: "slab", slab: { z0: 15, z1: 25, op: "max", thickness: 11, follow: true } });
    check(vs.spec() === "z15-25max", `the slab is what was asked for (${vs.spec()})`);
    vs.set({ z: 2 });
    check(vs.spec() === "z0-10max", `near the top it keeps eleven planes (${vs.spec()})`);
    vs.set({ z: 39 });
    check(vs.spec() === "z29-39max", `and near the bottom too (${vs.spec()})`);
    vs.set({ slab: { follow: false } });
    vs.step(-5);
    check(vs.spec() === "z24-34max", `a fixed slab moves as a block (${vs.spec()})`);
    const before = events.length;
    vs.set({ slab: { op: "std" } });
    const classChange = events.slice(before).find((e) => e.type === "plexora:view-class-changed");
    check(classChange && classChange.detail.from === "volume" && classChange.detail.to === "std",
        "a standard deviation moves the tiles to their own window, and says so");
    vs.set({ slab: { op: "pct", q: 99.5 } });
    check(vs.spec() === "z24-34p99.5", `a percentile spells its q (${vs.spec()})`);
}

// -- in 3-D a step moves what is cut along Z, never the frozen plane ---------
{
    const vs = new ViewState("vol", { fetchImpl: fakeFetch({
        "volume/vol/info": VOLUME, "view_state/vol": {} }) });
    await vs.load({ search: "" });
    vs.set({ z: 12 });
    check(vs.get().inspect?.mode === "full", "the 3-D view starts whole");
    vs.set({ mode: "volume" });
    const spec = vs.spec();
    vs.step(3);
    check(vs.get().z === 12 && vs.get().inspect.mode === "full", "with nothing cut along Z a step moves nothing");
    vs.set({ inspect: { mode: "crop", axes: { x: false, z: true }, crop: { z: [5, 9] } } });
    vs.step(3);
    let state = vs.get();
    check(state.inspect.crop.z[0] === 8 && state.inspect.crop.z[1] === 12 && state.z === 12,
          `] in 3-D moves a Z crop whole and leaves the plane (${JSON.stringify(state.inspect.crop.z)}, z ${state.z})`);
    vs.step(-100);
    check(vs.get().inspect.crop.z[0] === 0 && vs.get().inspect.crop.z[1] === 4, "and stops at the top of the stack");
    check(vs.label().includes("crop Z planes 0–4"), `the caption says the cut (${vs.label()})`);
    vs.set({ inspect: { mode: "cutaway", axes: { x: false, z: true }, cutaway: { z: { at: 20 } } } });
    vs.step(2);
    check(vs.get().inspect.cutaway.z.at === 22, "a Z cutaway's cut moves");
    check(vs.get().inspect.crop.z[1] === 4, "and the crop it is not keeps its range");
    const key = vs.revisionKey();
    vs.set({ inspect: { mode: "crop", axes: { x: true, z: true } } });
    check(vs.revisionKey() === key, "a cut is not a revision: the 3-D view reads it every frame");
    vs.step(5);
    state = vs.get();
    check(state.z === 12, "a step in 3-D never moves the frozen plane");
    check(vs.spec() === spec, `the tiles under the 3-D view stay on the plane it was entered from (${vs.spec()})`);
    vs.set({ mode: "plane" });
    check(vs.spec() === "z12", "and leaving 3-D comes back to that plane");
    check(!("section" in vs.get()), "the view state keeps no 3-D section positions");
    vs.set({ mode: "volume", inspect: { crop: { x: [1900, 100] } } });
    check(vs.get().inspect.crop.x[0] === 100 && vs.get().inspect.crop.y[1] === 1500,
          "a patch to one axis keeps the others, repaired");
}

// -- a 3-D view saved with a clip range opens cut to those planes -------------
{
    const vs = new ViewState("vol", { fetchImpl: fakeFetch({
        "volume/vol/info": VOLUME,
        "view_state/vol": { mode: "volume", z: 12, slab: { z0: 5, z1: 9, follow: false } } }) });
    await vs.load({ search: "" });
    const state = vs.get();
    check(state.inspect.mode === "crop" && state.inspect.axes.z && state.inspect.crop.z.join() === "5,9",
          `the old clip range becomes a Z crop (${JSON.stringify(state.inspect)})`);
    const whole = new ViewState("vol", { fetchImpl: fakeFetch({
        "volume/vol/info": VOLUME,
        "view_state/vol": { mode: "volume", slab: { z0: 0, z1: 39, follow: false } } }) });
    await whole.load({ search: "" });
    check(whole.get().inspect.mode === "full", "and a range over every plane stays whole");
}

// -- a colour stack's planes are shown one at a time -------------------------
{
    const vs = new ViewState("hez", { fetchImpl: fakeFetch({
        "volume/hez/info": { ...VOLUME, color: true },
        "view_state/hez": { mode: "slab", layout: "sections" } }) });
    await vs.load({ search: "" });
    check(vs.color && vs.get().mode === "plane" && vs.get().layout === "single",
          "a saved projection or sections layout opens a colour stack on a plane");
    vs.set({ mode: "volume" });
    vs.set({ mode: "slab", layout: "sections" });
    check(vs.get().mode === "plane" && vs.get().layout === "single" && /^z\d+$/.test(vs.spec()),
          `nothing combines or cuts a colour stack's planes (${vs.spec()})`);
    vs.set({ z: 3 });
    check(vs.spec() === "z3", "its planes are stepped as any stack's");
}

// -- tokens parse the same grammar the server speaks -------------------------
{
    const parse = ViewState.parseToken;
    check(parse("z12").z === 12 && parse("z12").mode === "plane", "a plane token");
    const slab = parse("t2z19-4max");
    check(slab.t === 2 && slab.slab.z0 === 4 && slab.slab.z1 === 19, "a slab token, reordered");
    check(parse("z3-8p95").slab.op === "pct" && parse("z3-8p95").slab.q === 95, "a percentile");
    check(parse("nonsense") === null && parse("") === null, "anything else is not a view");
}

// -- the 3-D view's area ---------------------------------------------------------
{
    const puts = [];
    const vs = new ViewState("vol", { saveDelay: 5, fetchImpl: fakeFetch({
        "volume/vol/info": VOLUME, "view_state/vol": {} }, puts) });
    await vs.load({ search: "" });
    check(vs.get().volume.area === null, "no area: the 3-D view draws the whole stack");
    const key = vs.revisionKey();
    vs.set({ volume: { area: { x: 1900.4, y: -10, width: 300, height: 200 } } });
    check(JSON.stringify(vs.get().volume.area) === JSON.stringify({ x: 1900, y: 0, width: 100, height: 190 }),
          `an area is cut to the image and rounded out (${JSON.stringify(vs.get().volume.area)})`);
    check(vs.revisionKey() === key, "the area is no revision of the tiles");
    vs.set({ volume: { area: { x: 0, y: 0, width: 0, height: 10 } } });
    check(vs.get().volume.area === null, "an area of nothing is no area");

    vs.set({ volume: { area: { x: 100, y: 100, width: 400, height: 400 } },
             inspect: { mode: "crop", axes: { x: true, y: true, z: false }, crop: { x: [200, 300], y: [150, 250] },
                        cutaway: { x: { at: 250 } } } });
    check(JSON.stringify(vs.get().inspect.crop.x) === "[200,300]", "a crop inside the area is kept as it is");
    vs.set({ volume: { area: { x: 600, y: 50, width: 400, height: 400 } } });
    const moved = vs.get().inspect;
    check(JSON.stringify(moved.crop.x) === "[700,800]" && JSON.stringify(moved.crop.y) === "[100,200]"
          && moved.cutaway.x.at === 750, `the cut moves with the area (${JSON.stringify(moved.crop)})`);
    vs.set({ volume: { area: { x: 0, y: 0, width: 400, height: 400 } },
             inspect: { crop: { x: [10, 20], y: [10, 20] } } });
    check(JSON.stringify(vs.get().inspect.crop.x) === "[10,20]",
          "a change that says where the cut is is not moved with the area");
    vs.set({ volume: { area: null } });
    check(vs.get().volume.area === null && vs.get().inspect.mode === "full"
          && JSON.stringify(vs.get().inspect.crop.x) === "[0,2000]",
          `back to the whole stack the cut goes back to the whole stack's places (${JSON.stringify(vs.get().inspect.crop.x)})`);
    await settle(60);
    const body = puts.at(-1)?.body;
    check(body && "area" in body.volume && !("saved" in body.volume), "the area is saved, and nothing else of it");
}

// -- V: to 3-D and back to what it was entered from ------------------------------------
{
    const vs = new ViewState("vol", { fetchImpl: fakeFetch({
        "volume/vol/info": VOLUME, "view_state/vol": {} }) });
    await vs.load({ search: "" });
    vs.set({ mode: "slab", slab: { z0: 4, z1: 9, follow: false } });
    vs.toggleVolume();
    check(vs.get().mode === "volume", "V enters 3-D");
    const cut = vs.get().inspect;
    check(cut.mode === "crop" && cut.axes.z && JSON.stringify(cut.crop.z) === "[4,9]",
          `from a fixed projection, 3-D is cut to its planes (${JSON.stringify(cut.crop.z)})`);
    vs.toggleVolume();
    check(vs.get().mode === "slab", "V again goes back to the projection it came from");
    vs.set({ mode: "slab", slab: { follow: true } });
    vs.set({ inspect: { mode: "full" } });
    vs.toggleVolume();
    check(vs.get().mode === "volume" && vs.get().inspect.mode === "full", "a following projection is never cut");
    vs.set({ mode: "plane" });
    vs.toggleVolume();
    vs.toggleVolume();
    check(vs.get().mode === "plane", "from a plane, back to the plane");
    vs.enterVolume({ area: { x: 10, y: 10, width: 100, height: 100 } });
    check(vs.get().mode === "volume" && vs.get().volume.area?.width === 100, "the Area tool enters 3-D on its area");
    vs.set({ inspect: { mode: "crop", tool: "crop", crop: { x: [20, 60] } } });
    vs.toggleVolume();
    check(vs.get().mode === "plane" && vs.get().volume.area === null, "leaving 3-D lets the square go");
    const whole = vs.get().inspect;
    check(whole.mode === "full" && whole.tool === "crop" && whole.crop.x[0] === 0,
          `and the cut made inside it goes back to the whole stack's places, its tool kept (${JSON.stringify(whole.crop)})`);
    vs.toggleVolume();
    check(vs.get().mode === "volume" && vs.get().volume.area === null, "V again is the whole stack, not the square");
    vs.enterVolume({ area: { x: 10, y: 10, width: 100, height: 100 } });
    vs.enterVolume();
    check(vs.get().volume.area === null, "3-D entered with no square is the whole stack");
    vs.enterVolume({ area: { x: 10, y: 10, width: 100, height: 100 } });
    vs.set({ volume: { area: null }, inspect: { mode: "crop", crop: { x: [30, 40] } } });
    check(vs.get().inspect.mode === "crop" && JSON.stringify(vs.get().inspect.crop.x) === "[30,40]",
          "a patch that says where the cut is still says so as the square goes (a restore, Back)");
}

console.log(`view state probe OK (${checks} checks)`);
