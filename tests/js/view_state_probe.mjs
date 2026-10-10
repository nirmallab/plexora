/**
 * Where the viewer is in a volume (services/viewState.js), in node.
 *
 * The properties worth pinning are the ones a user feels and cannot see the
 * cause of: a planar image must get no view at all (every 2-D tile address
 * unchanged); a scrub must not queue one tile rebuild per plane; a slab that
 * follows z must keep its thickness at the ends of the stack; the URL wins
 * over what was saved; and the badge must say "MIP" for a projection.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const source = readFileSync(join(REPO, "plexora/client/src/js/services/viewState.js"), "utf8");

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
runInContext(source, createContext(sandbox));
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

// -- tokens parse the same grammar the server speaks -------------------------
{
    const parse = ViewState.parseToken;
    check(parse("z12").z === 12 && parse("z12").mode === "plane", "a plane token");
    const slab = parse("t2z19-4max");
    check(slab.t === 2 && slab.slab.z0 === 4 && slab.slab.z1 === 19, "a slab token, reordered");
    check(parse("z3-8p95").slab.op === "pct" && parse("z3-8p95").slab.q === 95, "a percentile");
    check(parse("nonsense") === null && parse("") === null, "anything else is not a view");
}

console.log(`view state probe OK (${checks} checks)`);
