// The website's export (client services/bundleWeb.js) gives Python back the
// plan's work folder however the export ends. `POST /projects/export/plan`
// writes every database snapshot, entry and capture into a folder of
// Python's in-memory filesystem (bundles.plan_for_page) and hands back its
// `work_dir`; an export that is stopped, or that fails, after the plan must
// still tell Python, which removes the folder (bundle_routes._remove_work_dir).
// Otherwise every attempt leaves a copy of each project's database in the
// tab's memory until the tab is closed.
//
// tests/test_bundle_web_work_dir_release.py runs this and pins its lines.
//
//   node tests/js/bundle_web_work_dir_probe.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..");
const JS = path.join(REPO, "plexora", "client", "src", "js");

const tick = () => new Promise((resolve) => setTimeout(resolve, 5));

// -- Python, as the page's worker answers -------------------------------------

const calls = [];
let routes = {};
let stores = async () => ({ "/data": { kind: "folder", handle: null } });
const python = {
    async fetch(url, init) {
        const method = (init && init.method) || "GET";
        const body = init && init.body ? JSON.parse(init.body) : undefined;
        calls.push([method, url, body]);
        const handler = routes[`${method} ${url}`];
        if (!handler) return new Response(JSON.stringify({ error: `no route ${method} ${url}` }), { status: 404 });
        const [status, answer] = await handler(body, url);
        return new Response(JSON.stringify(answer), { status });
    },
    async flush() { calls.push(["flush"]); return true; },
    async bundleStores() { return stores(); },
    async readFile() { throw new Error("ENOENT"); },
    async pull() { return {}; },
};

const host = { PlexoraPython: python, PlexoraPicks: { fileFor: () => null } };
host.parent = host;
const frame = {
    parent: host,
    document: { createElement: () => ({ style: {}, click() {}, remove() {} }), body: { appendChild() {} } },
    URL: { createObjectURL: () => "blob:fake", revokeObjectURL() {} },
    setTimeout: () => 0,
    PlexoraProjectExport: { _internal: { createServerTransport: () => ({
        kind: "server", status: async () => { throw new Error("not the server's"); }, cancel: async () => ({}) }) } },
};

const source = readFileSync(path.join(JS, "services", "bundleWeb.js"), "utf8");
vm.runInThisContext(`(function (window) {${source}\n})`)(host);
vm.runInThisContext(`(function (window) {${source}\n})`)(frame);
const transport = frame.PlexoraBundleTransport;
assert.equal(transport.kind, "website");

const results = [];
async function check(name, run) {
    try {
        await run();
    } catch (error) {
        process.exitCode = 1;
        console.log(`not ok - ${name}: ${error.message}`);
        return;
    }
    results.push(name);
    console.log(`ok - ${name}`);
}

async function until(job) {
    for (let i = 0; i < 2000; i += 1) {
        const status = await transport.status("export", job);
        if (["ready", "failed", "cancelled"].includes(status.status)) return status;
        await tick();
    }
    throw new Error("the job never ended");
}

function planWith(workDir) {
    return {
        format: 2, reserve: 4096, work_dir: workDir, entries: { p1: { title: "P1" } }, stamps: { p1: "s" },
        manifest: { format: 2, projects: [], media: [], captures: [], datasets: [], figures: [], left_out: [] },
        members: [{ bundle_path: "projects/p1/state/p1.db", source: `${workDir}/00000.db`, size: 22 }],
    };
}

//: Every call to Python after the plan that names the plan's work folder.
function released(workDir) {
    const at = calls.findIndex((call) => call[1] === "/projects/export/plan");
    return calls.slice(at + 1).filter((call) => call[2] && call[2].work_dir === workDir);
}

await check("a stopped export gives Python back its plan's work folder", async () => {
    calls.length = 0;
    const workDir = "/tmp/plexora-bundle-stopped";
    let answer;
    const planned = new Promise((resolve) => { answer = resolve; });
    routes = { "POST /projects/export/plan": async () => { await planned; return [200, planWith(workDir)]; } };
    const first = await transport.startExport({ ids: ["p1"], include: {}, dest: null });
    for (let i = 0; i < 200 && !calls.some((call) => call[1] === "/projects/export/plan"); i += 1) await tick();
    // Stop pressed while Python plans: the plan (and its folder) still arrives.
    await transport.cancel("export", first.job);
    answer();
    const status = await until(first.job);
    assert.equal(status.status, "cancelled");
    for (let i = 0; i < 50; i += 1) await tick();
    assert.ok(released(workDir).length > 0,
              `Python was never told to remove ${workDir}; calls after the plan: `
              + JSON.stringify(calls.map((call) => call[0] === "POST" ? call[1] : call[0])));
});

await check("a failed export gives Python back its plan's work folder", async () => {
    calls.length = 0;
    const workDir = "/tmp/plexora-bundle-failed";
    routes = { "POST /projects/export/plan": async () => [200, planWith(workDir)] };
    stores = async () => { throw new Error("The project store is not there."); };
    const first = await transport.startExport({ ids: ["p1"], include: {}, dest: null });
    const status = await until(first.job);
    assert.equal(status.status, "failed");
    assert.equal(status.error, "The project store is not there.");
    for (let i = 0; i < 50; i += 1) await tick();
    assert.ok(released(workDir).length > 0,
              `Python was never told to remove ${workDir}; calls after the plan: `
              + JSON.stringify(calls.map((call) => call[0] === "POST" ? call[1] : call[0])));
});

console.log(`${results.length} checks passed`);
