/**
 * One navigation revision, drawn by every surface in the same frame
 * (services/viewState.js).
 *
 * The bug this pins: the plane, the section panes' z-line, a 3-D label
 * volume's outlines and the 3-D view each followed the view on a clock of
 * their own -- the z-line moved the moment a key was pressed while the plane
 * it marked arrived 70-170 ms later, outlines were taken down before the next
 * plane's were read. Here fake surfaces get ready at different times, and the
 * properties a user sees are pinned:
 *
 * - every surface ready in time commits in ONE task (nothing else runs
 *   between the first commit and the last), the main plane deciding when;
 * - a surface still preparing SYNC_WAIT_MS after the plane commits late under
 *   the same revision -- and never over a newer revision that already reached
 *   it (its half-built work is discarded instead);
 * - a surface that never answers holds nothing up;
 * - a surface with `hold` keeps the plane waiting for it (the contrast a
 *   projection is levelled to), up to its limit;
 * - going back to the plane on screen supersedes the revision in flight: its
 *   signal aborts and it commits nothing; so does one stuck loading while a
 *   newer one waits, and one a jump far from it leaves behind (a step to the
 *   next plane, or a fast drag, lets it land);
 * - revision ids only grow, and view-changed says which one is on screen.
 *
 * Run directly:  node tests/js/nav_revision_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const source = readFileSync(join(REPO, "plexora/client/src/js/services/viewState.js"), "utf8");

const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}

const events = [];
const box = {
    console,
    setTimeout,
    clearTimeout,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    URLSearchParams,
    AbortController,
    DOMException,
    performance,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: (event) => { events.push(event); return true; },
    location: { search: "" },
};
box.window = box;
box.globalThis = box;
runInContext(source, createContext(box));
const ViewState = box.PlexoraViewState;
const { SYNC_WAIT_MS, SUPERSEDE_AFTER_MS } = ViewState;

const VOLUME = { volumetric: true, depth: 40, frames: 3, width: 2000, height: 1500, default_view: "z10" };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function volume() {
    const vs = new ViewState("vol", {
        saveDelay: 100000,
        fetchImpl: async (url, options) => {
            if (options?.method === "PUT") return { ok: true, json: async () => ({}) };
            if (url.includes("volume/vol/info")) return { ok: true, json: async () => VOLUME };
            return { ok: true, json: async () => ({}) };
        },
    });
    await vs.load({ search: "" });
    return vs;
}

//: Everything that happens, in order: commits, and a tick every millisecond
//: (a macrotask that would land between two commits in different tasks).
const log = [];
let ticking = true;
(function tick() {
    if (!ticking) return;
    log.push("tick");
    setTimeout(tick, 1);
})();

/** A surface ready after `ms` (null: never), logging its commits as `name:rev`. */
function surface(name, ms, { aborted = [], discarded = [], commit = true } = {}) {
    return {
        prepare(rev) {
            return new Promise((resolve, reject) => {
                rev.signal?.addEventListener?.("abort", () => {
                    aborted.push(`${name}:${rev.spec}`);
                    reject(ViewState.abortError());
                });
                const answer = () => {
                    if (!commit) return resolve(null);
                    const run = () => log.push(`${name}:${rev.spec}`);
                    run.discard = () => discarded.push(`${name}:${rev.spec}`);
                    resolve(run);
                };
                if (ms === null) return;
                if (typeof ms === "function") ms(rev).then(answer);
                else setTimeout(answer, ms);
            });
        },
    };
}

function contiguous(names) {
    const at = names.map((name) => log.indexOf(name));
    if (at.some((i) => i < 0)) return false;
    const lo = Math.min(...at);
    const hi = Math.max(...at);
    return log.slice(lo, hi + 1).every((entry) => entry !== "tick");
}

// -- every surface ready in time commits in one task ---------------------------------

{
    const vs = await volume();
    vs.registerSurface("main", { order: 20, ...surface("main", 40) });
    vs.registerSurface("panes", { order: 40, ...surface("panes", 5) });
    vs.registerSurface("labels", { order: 30, ...surface("labels", 70) });
    vs.registerSurface("volume", { order: 50, ...surface("volume", 0) });
    vs.set({ z: 11 });
    check("the request is announced at once, the picture not yet",
        events.some((e) => e.type === "plexora:view-requested") && vs.appliedSpec() === "z10");
    await vs.settled();
    const names = ["main:z11", "panes:z11", "labels:z11", "volume:z11"];
    check("the plane, the panes, the outlines and the 3-D view commit in ONE task",
        contiguous(names), log.slice(-12).join(","));
    const order = names.map((name) => log.indexOf(name));
    check("in surface order (lower first)",
        order[0] < order[2] && order[2] < order[1] && order[1] < order[3]);
    check("and the picture is the revision asked for", vs.appliedSpec() === "z11"
        && vs.revision()?.spec === "z11");
    const changed = events.filter((e) => e.type === "plexora:view-changed").pop();
    check("view-changed names the revision on screen", changed?.detail?.rev === vs.revision().id);
}

// -- a jump far away supersedes at once; a near step waits ---------------------------

{
    const vs = await volume();
    const aborted = [];
    const slow = { z11: 600 };
    vs.registerSurface("main", { order: 20, ...surface("main", (rev) => sleep(slow[rev.spec] ?? 20), { aborted }) });
    const from = log.length;
    const t0 = performance.now();
    vs.set({ z: 11 });
    await sleep(120);                      // the hand still: the next request is a jump
    vs.set({ z: 35 });
    await vs.settled();
    const took = performance.now() - t0;
    check("a jump far from the plane in flight supersedes it at once, not after a second",
        aborted.includes("main:z11") && log.slice(from).includes("main:z35")
        && !log.slice(from).includes("main:z11")
        && took < 600, `${took.toFixed(0)} ms; aborted ${aborted.join()}`);
}

{
    const vs = await volume();
    const aborted = [];
    const slow = { z11: 300 };
    vs.registerSurface("main", { order: 20, ...surface("main", (rev) => sleep(slow[rev.spec] ?? 20), { aborted }) });
    const from = log.length;
    vs.set({ z: 11 });
    await sleep(120);
    vs.set({ z: 12 });                     // the next plane: the one in flight is on the way
    await vs.settled();
    check("a step to the next plane lets the one in flight land first",
        !aborted.includes("main:z11") && log.slice(from).indexOf("main:z11") >= 0
        && log.slice(from).indexOf("main:z11") < log.slice(from).indexOf("main:z12"), aborted.join());
}

{
    const vs = await volume();
    const aborted = [];
    const slow = { z11: 300 };
    vs.registerSurface("main", { order: 20, ...surface("main", (rev) => sleep(slow[rev.spec] ?? 20), { aborted }) });
    const from = log.length;
    vs.set({ z: 11 });
    await sleep(20);                       // a fast drag down the rail: no pause
    vs.set({ z: 20 });
    await vs.settled();
    check("a fast drag is a scrub, not a jump: planes still land on the way",
        !aborted.includes("main:z11") && log.slice(from).includes("main:z11"), aborted.join());
}

// -- a slow surface commits late, never over a newer revision --------------------------

{
    const vs = await volume();
    const discarded = [];
    let slowFor = { z11: 400, z12: 10 };
    vs.registerSurface("main", { order: 20, ...surface("main", 10) });
    vs.registerSurface("slow", { order: 30, ...surface("slow", (rev) => sleep(slowFor[rev.spec] ?? 10), { discarded }) });
    const t0 = performance.now();
    vs.set({ z: 11 });
    await vs.settled();
    const committedAt = performance.now() - t0;
    check("the plane does not wait for a surface still preparing past SYNC_WAIT_MS",
        log.includes("main:z11") && !log.includes("slow:z11") && committedAt < 10 + SYNC_WAIT_MS + 150,
        `${committedAt.toFixed(0)} ms`);
    vs.set({ z: 12 });
    await vs.settled();
    await sleep(450);
    check("the next revision reached the slow surface first",
        log.includes("slow:z12") && log.indexOf("slow:z12") < log.length);
    check("so the older one's late work is discarded, never drawn over it",
        !log.includes("slow:z11") && discarded.includes("slow:z11"), JSON.stringify(discarded));
}

{
    const vs = await volume();
    vs.registerSurface("main", { order: 20, ...surface("main", 5) });
    vs.registerSurface("late", { order: 30, ...surface("late", SYNC_WAIT_MS + 120) });
    vs.set({ z: 14 });
    await vs.settled();
    await sleep(SYNC_WAIT_MS + 200);
    check("a late surface with nothing newer commits under its own revision",
        log.includes("late:z14") && log.indexOf("main:z14") < log.indexOf("late:z14"));
}

// -- a surface that never answers holds nothing up ---------------------------------

{
    const vs = await volume();
    vs.registerSurface("main", { order: 20, ...surface("main", 5) });
    vs.registerSurface("stuck", { order: 30, ...surface("stuck", null) });
    vs.set({ z: 15 });
    await vs.settled();
    vs.set({ z: 16 });
    await vs.settled();
    check("a surface that never answers does not hold the plane or the next revision",
        log.includes("main:z15") && log.includes("main:z16") && vs.appliedSpec() === "z16");
}

// -- a held surface: the plane waits for it, up to its limit ------------------------------

{
    const vs = await volume();
    vs.registerSurface("main", { order: 20, ...surface("main", 5) });
    vs.registerSurface("ranges", { order: 10, hold: 1500, ...surface("ranges", 300) });
    vs.set({ mode: "slab", slab: { z0: 5, z1: 15, op: "max" } });
    await vs.settled();
    check("a projection's tiles wait for its contrast and show with it, in one task",
        contiguous(["ranges:z5-15max", "main:z5-15max"])
        && log.indexOf("ranges:z5-15max") < log.indexOf("main:z5-15max"));
}

{
    const vs = await volume();
    vs.registerSurface("main", { order: 20, ...surface("main", 5) });
    vs.registerSurface("ranges", { order: 10, hold: 100, ...surface("ranges", 600) });
    const t0 = performance.now();
    vs.set({ z: 30 });
    await vs.settled();
    const at = performance.now() - t0;
    check("but only up to its hold", log.includes("main:z30") && at < 100 + 150,
        `${at.toFixed(0)} ms`);
}

// -- superseded: back to the plane on screen ------------------------------------------

{
    const vs = await volume();
    const aborted = [];
    vs.registerSurface("main", { order: 20, ...surface("main", 300, { aborted }) });
    vs.set({ z: 17 });
    await sleep(40);
    vs.set({ z: 10 });
    await vs.settled();
    await sleep(20);
    check("going back to the plane on screen aborts the revision in flight",
        aborted.includes("main:z17") && !log.includes("main:z17"), JSON.stringify(aborted));
    check("and the picture never left it", vs.appliedSpec() === "z10");
    const changed = events.filter((e) => e.type === "plexora:view-changed").pop();
    check("and it is announced as shown", changed?.detail?.spec === "z10");
}

// -- superseded: stuck behind a slow load while a newer one waits -----------------------

{
    const vs = await volume();
    const aborted = [];
    const slowly = (rev) => sleep(rev.spec === "z21" ? SUPERSEDE_AFTER_MS + 2000 : 20);
    vs.registerSurface("main", { order: 20, ...surface("main", slowly, { aborted }) });
    const t0 = performance.now();
    vs.set({ z: 21 });
    await sleep(50);
    vs.set({ z: 22 });
    await vs.settled();
    const at = performance.now() - t0;
    check("a revision stuck loading past SUPERSEDE_AFTER_MS gives way to the newest",
        aborted.includes("main:z21") && log.includes("main:z22") && !log.includes("main:z21")
        && at < SUPERSEDE_AFTER_MS + 400, `${at.toFixed(0)} ms`);
}

// -- a scrub: few revisions, ids only grow, nothing older after newer -----------------

{
    const vs = await volume();
    const seen = [];
    vs.registerSurface("main", { order: 20, prepare: (rev) => sleep(25).then(() => () => seen.push([rev.id, rev.state.z])) });
    for (let i = 0; i < 30; i += 1) {
        vs.step(1);
        await sleep(5);
    }
    await vs.settled();
    const zs = seen.map(([, z]) => z);
    const ids = seen.map(([id]) => id);
    check("a scrub of thirty steps is a few revisions", seen.length <= 10, `${seen.length}`);
    check("ids only grow, and no plane older than one shown is shown after it",
        ids.every((id, i) => i === 0 || id > ids[i - 1]) && zs.every((z, i) => i === 0 || z > zs[i - 1]));
    check("and the last is where the hand stopped", zs[zs.length - 1] === 39 && vs.appliedSpec() === "z39");
}

// -- in 3-D, the hidden plane's tiles stay where they were ------------------------------

{
    const vs = await volume();
    const specs = [];
    vs.registerSurface("main", { order: 20, prepare: (rev) => { specs.push(rev.spec); return () => {}; } });
    vs.set({ mode: "volume" });
    await vs.settled();
    vs.set({ z: 25 });
    await vs.settled();
    vs.set({ slab: { z0: 3, z1: 30 } });
    await vs.settled();
    check("moving z or the clip in 3-D does not swap the hidden plane's tiles",
        specs.every((spec) => spec === "z10"), JSON.stringify(specs));
    vs.set({ mode: "plane" });
    await vs.settled();
    check("and leaving 3-D swaps once, to where z is now", specs[specs.length - 1] === "z25");
}

// -- with no surface, the applier is applied as before ------------------------------

{
    const vs = await volume();
    const applied = [];
    vs.applier = async (spec) => { applied.push(spec); };
    vs.set({ z: 33 });
    await vs.settled();
    check("with no surfaces the applier alone shows a change, as it always did",
        applied.join() === "z33" && vs.appliedSpec() === "z33");
}

ticking = false;
if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
process.exit(0);
