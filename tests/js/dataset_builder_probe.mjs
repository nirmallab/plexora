/**
 * New Dataset's controller: the requests, the polling, and the order replies
 * are applied in.
 *
 * What can go wrong quietly here is ORDER. A correction posted while an
 * earlier one is still in flight must win, not be overwritten by the late
 * answer to the first; a refused correction must put the screen back the way
 * the server last agreed it was; a scan left behind a closed dialog must be
 * stopped, and a build must not be. The controller is run as written, with
 * a stand-in for fetch and a clock the probe turns by hand.
 *
 * Run directly:  node tests/js/dataset_builder_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/views/datasetBuilder.js");

const context = createContext({ window: {}, console, JSON, Promise, Set, Map });
context.window = context;
runInContext(readFileSync(SOURCE, "utf8"), context, { filename: SOURCE });
const builder = context.PlexoraDatasetBuilder;

// -- a clock turned by hand ------------------------------------------------

function clock() {
    let next = 1;
    const due = new Map();
    return {
        setTimeout(fn) { const id = next++; due.set(id, fn); return id; },
        clearTimeout(id) { due.delete(id); },
        pending: () => due.size,
        async fire() {
            const fns = Array.from(due.values());
            due.clear();
            // Not awaited: a callback may be waiting on a reply the probe is
            // deliberately holding back.
            fns.forEach((fn) => { fn(); });
            for (let i = 0; i < 5; i++) await settle();
        },
    };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

/** A server that answers from a script and records what it was asked. */
function server(answers) {
    const asked = [];
    const held = [];
    const request = (path, { method = "GET", body } = {}) => {
        asked.push({ path, method, body });
        const answer = answers(path, method, body, asked);
        if (answer && answer.hold) {
            return new Promise((resolve) => held.push(() => resolve(answer.reply)));
        }
        return Promise.resolve(answer || { ok: true, status: 200, body: {} });
    };
    return { asked, held, request };
}

const ok = (body) => ({ ok: true, status: 200, body });
//: Objects made inside the vm context are another realm's; compare as data.
const plain = (value) => JSON.parse(JSON.stringify(value));
const results = [];
async function check(name, fn) {
    await fn();
    results.push(name);
    console.log(`ok  ${name}`);
}

const plan = (extra = {}) => ({
    token: "scan-1", status: "ready", pending: false,
    projects: [{ id: "p:a", name: "A", status: "matched" }],
    unassigned: [], counts: { projects: 1 },
    overrides: {}, ...extra,
});

await check("a scan polls until it is no longer pending, then stops", async () => {
    const timers = clock();
    let polls = 0;
    const s = server((path, method) => {
        if (path === "datasets/scan" && method === "POST") return ok(plan({ pending: true, status: "pending" }));
        if (path === "datasets/scan/scan-1") {
            polls += 1;
            return ok(polls < 2 ? plan({ pending: true, status: "pending" }) : plan());
        }
    });
    const c = builder.createController({ request: s.request, timers });
    await c.start(["/data/cohort"]);
    assert.equal(c.state.phase, "scanning");
    assert.deepEqual(plain(s.asked[0].body), { paths: ["/data/cohort"] });
    await timers.fire();
    assert.equal(c.state.phase, "scanning");
    await timers.fire();
    assert.equal(c.state.phase, "plan");
    assert.equal(timers.pending(), 0);
    // Overrides from a fresh scan arrive as {}; every list is there anyway.
    assert.deepEqual(Object.keys(c.state.overrides).sort(),
                     ["assign", "detach", "exclude", "learn", "new_project", "rename"]);
});

await check("a folder on a data node names the node, and so does an added folder", async () => {
    const timers = clock();
    const s = server((path, method) => {
        if (path === "datasets/scan" && method === "POST") return ok(plan());
    });
    const c = builder.createController({ request: s.request, timers });
    await c.start(["/n/files/run"], "hms-o2");
    assert.deepEqual(plain(s.asked[0].body), { paths: ["/n/files/run"], node: "hms-o2" });
    await c.addFolder("/n/files/run2");
    assert.deepEqual(plain(s.asked[1].body),
                     { paths: ["/n/files/run2"], token: "scan-1", node: "hms-o2" });
    // On this computer, or over SFTP (whose address names its server), no node.
    await c.start(["sftp://me@transfer.example/n/files/run"]);
    assert.deepEqual(plain(s.asked[2].body), { paths: ["sftp://me@transfer.example/n/files/run"] });
});

await check("quick corrections go as one request, and a late answer is dropped", async () => {
    const timers = clock();
    let planPosts = 0;
    const s = server((path, method, body) => {
        if (path === "datasets/scan") return ok(plan());
        if (path.endsWith("/plan")) {
            planPosts += 1;
            const reply = ok(plan({ projects: [{ id: "p:a", name: `answer ${planPosts}`, status: "matched" }] }));
            return planPosts === 1 ? { hold: true, reply } : reply;
        }
    });
    const c = builder.createController({ request: s.request, timers });
    await c.start(["/d"]);
    c.change((o) => builder.edits.exclude(o, ["/d/x.tif"]));
    c.change((o) => builder.edits.rename(o, "p:a", "First"));
    assert.equal(c.state.planning, true);
    await timers.fire();
    assert.equal(planPosts, 1, "two edits within the debounce are one POST");
    assert.deepEqual(plain(s.asked.at(-1).body.overrides.exclude), ["/d/x.tif"]);
    assert.equal(s.asked.at(-1).body.overrides.rename["p:a"], "First");
    // A second correction while the first is still out.
    c.change((o) => builder.edits.rename(o, "p:a", "Second"));
    await timers.fire();
    assert.equal(c.state.scan.projects[0].name, "answer 2");
    s.held.shift()();          // the first answer lands last
    await settle();
    assert.equal(c.state.scan.projects[0].name, "answer 2", "the late answer is ignored");
    assert.equal(c.state.planning, false);
});

await check("a refused correction puts back what the server last agreed", async () => {
    const timers = clock();
    const s = server((path) => {
        if (path === "datasets/scan") return ok(plan());
        if (path.endsWith("/plan")) return { ok: false, status: 400, body: { error: "unknown slot" } };
    });
    const c = builder.createController({ request: s.request, timers });
    await c.start(["/d"]);
    c.change((o) => builder.edits.exclude(o, ["/d/x.tif"]));
    await timers.fire();
    assert.deepEqual(plain(c.state.overrides.exclude), []);
    assert.equal(c.state.error, "unknown slot");
});

await check("create posts the overrides and polls the build to its end", async () => {
    const timers = clock();
    let builds = 0;
    const s = server((path, method) => {
        if (path === "datasets/scan") return ok(plan());
        if (path.endsWith("/create") && method === "POST") return ok({ status: "pending", projects: [] });
        if (path.endsWith("/create")) {
            builds += 1;
            return ok(builds < 2 ? { status: "pending", projects: [{ id: "p:a", status: "registering" }] }
                : { status: "ready", projects: [{ id: "p:a", status: "failed", error: "disk" }],
                    counts: { failed: 1 }, dataset: { id: "d1", name: "Cohort" } });
        }
    });
    const c = builder.createController({ request: s.request, timers });
    await c.start(["/d"]);
    c.state.overrides.rename["p:a"] = "Named";
    await c.create({ name: "Cohort" });
    const posted = s.asked.find((a) => a.path.endsWith("/create") && a.method === "POST");
    assert.equal(posted.body.name, "Cohort");
    assert.equal(posted.body.overrides.rename["p:a"], "Named");
    assert.equal(c.state.phase, "building");
    await timers.fire();
    assert.equal(c.state.phase, "building");
    await timers.fire();
    assert.equal(c.state.phase, "done");
    assert.equal(c.state.build.projects[0].error, "disk");
    assert.equal(timers.pending(), 0);
});

await check("a taken name is reported, not built", async () => {
    const timers = clock();
    const s = server((path) => {
        if (path === "datasets/scan") return ok(plan());
        if (path.endsWith("/create")) return { ok: false, status: 409, body: { error: "There is already a dataset called Cohort.", taken: true } };
    });
    const c = builder.createController({ request: s.request, timers });
    await c.start(["/d"]);
    const reply = await c.create({ name: "Cohort" });
    assert.equal(reply.status, 409);
    assert.equal(c.state.phase, "plan");
    assert.match(c.state.error, /already a dataset/);
});

await check("closing stops a scan but leaves a build running", async () => {
    let timers = clock();
    let s = server((path) => path === "datasets/scan" ? ok(plan({ pending: true, status: "pending" })) : ok({}));
    let c = builder.createController({ request: s.request, timers });
    await c.start(["/d"]);
    c.close();
    assert.ok(s.asked.some((a) => a.path === "datasets/scan/scan-1/cancel"));
    assert.equal(timers.pending(), 0);

    timers = clock();
    s = server((path, method) => {
        if (path === "datasets/scan") return ok(plan());
        if (path.endsWith("/create") && method === "POST") return ok({ status: "pending" });
        return ok({ status: "pending" });
    });
    c = builder.createController({ request: s.request, timers });
    await c.start(["/d"]);
    await c.create({ name: "Cohort" });
    c.close();
    assert.ok(!s.asked.some((a) => a.path.endsWith("/cancel")), "a build is never cancelled by closing");
});

await check("the edits keep one placement per file", () => {
    const o = { assign: [], exclude: ["/a"], detach: ["/b"], new_project: [], rename: {}, learn: [] };
    builder.edits.assign(o, ["/a", "/b"], ["p:x"], "mask");
    assert.deepEqual(plain(o.exclude), []);
    assert.deepEqual(plain(o.detach), []);
    assert.equal(o.assign.length, 2);
    builder.edits.assign(o, ["/a"], ["p:y", "p:z"], "table");
    assert.deepEqual(plain(o.assign.filter((a) => a.path === "/a").map((a) => a.project)), ["p:y", "p:z"]);
    builder.edits.exclude(o, ["/a"]);
    assert.ok(!o.assign.some((a) => a.path === "/a"));
});

await check("names and paths are escaped in the table", () => {
    const html = builder.html.table({
        projects: [{ id: "p:x", name: "<img src=x onerror=1>", status: "matched",
                     image: { path: "/d/\"q\".tif", rel: "\"q\".tif", name: "\"q\".tif" },
                     mask: null, table: null, warnings: [] }],
    }, { phase: "plan", build: null, expanded: new Set(["p:x"]), selected: new Set(),
         busy: new Set(), filter: { text: "", status: "all" }, renaming: null, shared: new Set() });
    assert.ok(!html.includes("<img src=x"));
    assert.ok(html.includes("&lt;img src=x onerror=1&gt;"));
    assert.ok(!html.includes('"q".tif'));
});

console.log(`\n${results.length} checks passed`);
