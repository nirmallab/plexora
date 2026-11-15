// The website's import (client services/bundleWeb.js runImport): a Stop
// pressed while `POST /projects/import` is on its way is not lost. The page's
// job has no Python job id yet, so `cancel` has nothing to tell Python; the
// job used to be acknowledged as stopped and then run to completion once the
// start answered (audit lead sc-bundles-10). Now the Stop is passed on as soon
// as the id arrives.
//
// tests/test_bundle_web_import_stop.py runs this and pins its lines.
//
//   node tests/js/bundle_web_import_stop_probe.mjs
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
        const status = await transport.status("import", job);
        if (["ready", "failed", "cancelled"].includes(status.status)) return status;
        await tick();
    }
    throw new Error("the job never ended");
}

await check("a Stop pressed while the import starts stops Python's import", async () => {
    calls.length = 0;
    let answer;
    const starting = new Promise((resolve) => { answer = resolve; });
    let stopped = false;
    routes = {
        "POST /projects/import/preview": async () => [200, { placements: [] }],
        "POST /projects/import": async () => { await starting; return [202, { job: "py1", status: "pending", progress: 0 }]; },
        "POST /projects/import/py1/cancel": async () => { stopped = true; return [200, { cancelled: true, job: "py1" }]; },
        "GET /projects/import/py1": async () => [200, stopped
            ? { job: "py1", status: "cancelled", progress: 0 }
            : { job: "py1", status: "ready", progress: 100, result: { projects: [{ name: "p1" }] } }],
    };
    const first = await transport.startImport({ source: { path: "/data/p1.plexora" } });
    for (let i = 0; i < 200 && !calls.some((call) => call[0] === "POST" && call[1] === "/projects/import"); i += 1) await tick();
    // Stop pressed while Python is starting the import: no Python job id yet.
    const acknowledged = await transport.cancel("import", first.job);
    assert.equal(acknowledged.cancelled, true);
    answer();
    const status = await until(first.job);
    assert.ok(stopped, "Python was never told to stop; calls: "
              + JSON.stringify(calls.map((call) => `${call[0]} ${call[1]}`)));
    assert.equal(status.status, "cancelled");
});

console.log(`${results.length} checks passed`);
