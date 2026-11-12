// The website's Python queue (client workers/pythonWorker.js `drain`,
// `ahead`, `letIn`, its `onmessage`) when the user leaves a page that still
// has Python busy: the page asked for goes ahead of the reads waiting, never
// ahead of a write; the page's message is read in between two requests, though
// Python's answers go on as microtasks; a request dropped while Python ran it
// is not asked again; `later` puts the reads waiting behind what comes next.
// Python is stood in for by OPS.request (a JSPI answer: a microtask), the
// worker's MessageChannel by setTimeout(0) (one queue of tasks, in order).
// Prints JSON; tests/test_python_queue.py asserts on it.
//
//   node tests/js/python_queue_probe.mjs   (from the repository root)
import assert from "node:assert/strict";
import fs from "node:fs";

const source = fs.readFileSync("plexora/client/src/js/workers/pythonWorker.js", "utf8");
const pick = (pattern, name) => {
    const found = source.match(pattern);
    assert.ok(found, `pythonWorker.js has no ${name}`);
    return found[0];
};
const code = [
    pick(/\nasync function drain\(\) \{[\s\S]*?\n\}\n/, "drain()"),
    pick(/\nconst turns = new MessageChannel\(\);[\s\S]*?\nasync function letIn\(\) \{[\s\S]*?\n\}\n/, "letIn()"),
    pick(/\nself\.onmessage = \(\{ data \}\) => \{[\s\S]*?\n\};\n/, "self.onmessage"),
    pick(/\nfunction ahead\(message\) \{[\s\S]*?\n\}\n/, "ahead(message)"),
    pick(/\nconst isRead = [\s\S]*?;\n/, "isRead"),
].join("");

//: What the worker's state around these pieces is, at rest.
const PREAMBLE = `
const queue = []; let busy = false; let idling = 0; let store = null; let lastPull = 0; let lastQuiet = 0;
const PULL_STALE_MS = 10000; async function pullNow() { return false; }
let fatalTold = false; let fatalWith = null; const AFTER_FATAL = new Set(["boot", "heap", "trace", "state"]);
const pageNow = () => true; let current = null; let inStep = false;
const retrying = new Map(); const running = new Set(); const abandoned = new Set();
let debug = false; const traced = []; function fatal() {}
let pendingWork = 0; let lastAnswer = 0; let lastRequest = 0; let dueInMs = 0;
function step() {} function idlePull() {}
let inFlight = null; function warm() {} function answered() { return true; }
const cancelledJobs = new Set(); function quit() {}
`;

//: A task after those already queued, as a MessagePort's message is.
class TaskChannel {
    constructor() {
        this.port1 = { onmessage: null };
        this.port2 = { postMessage: (data) => setTimeout(() => this.port1.onmessage({ data }), 0) };
    }
}

//: A fresh worker: `OPS.request` is Python. Returns the page's side.
function worker(python) {
    const posted = [];
    const self = { postMessage: (message) => posted.push(message) };
    const OPS = { request: python, state: () => ({}) };
    const make = new Function("self", "OPS", "MessageChannel",
                              `${PREAMBLE}${code}; return { onmessage: self.onmessage, queue, retrying };`);
    const made = make(self, OPS, TaskChannel);
    const send = (id, op, args) => made.onmessage({ data: { id, op, args } });
    return { ...made, posted, send };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const request = (path, method = "GET", extra = {}) => ({ method, path, query: "", headers: [], body: null, ...extra });
//: Python's answer to a request, in the order answered: its path.
const answeredPaths = (made, ran) => made.posted.filter((m) => m.ok).map((m) => ran.get(m.id));
const out = {};

// -- the page asked for, while the page left still has reads waiting -----------
{
    const ran = new Map();
    // Python answers at once, as a microtask: no message gets in on its own.
    const made = worker(async (args) => { await null; return { status: 200, path: args.path }; });
    for (let i = 1; i <= 6; i += 1) {
        ran.set(i, `/stats/${i}`);
        made.send(i, "request", request(`/stats/${i}`));
    }
    // Posted by the page while Python answers the first.
    ran.set(7, "/");
    setTimeout(() => made.send(7, "request", request("/", "GET", { first: true })), 0);
    for (let i = 0; i < 40 && made.posted.length < 7; i += 1) await tick();
    out.first = answeredPaths(made, ran);
}

// -- never ahead of a write, nor of a mount ------------------------------------
{
    const ran = new Map();
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    const made = worker(async (args) => {
        if (args.path === "/stats/1") await held;   // running when the rest arrive
        return { status: 200 };
    });
    const asks = [[1, "request", request("/stats/1")], [2, "request", request("/stats/2")],
                  [3, "request", request("/save", "POST")], [4, "request", request("/stats/4")],
                  [5, "request", request("/stats/5")]];
    for (const [id, op, args] of asks) { ran.set(id, args.path); made.send(id, op, args); }
    ran.set(6, "/"); made.send(6, "request", request("/", "GET", { first: true }));
    ran.set(7, "/settings"); made.send(7, "request", request("/settings", "GET", { first: true }));
    out.queuedBehindWrite = made.queue.map((m) => m.args.path);
    release();
    for (let i = 0; i < 40 && made.posted.length < 7; i += 1) await tick();
    out.behindWrite = answeredPaths(made, ran);
}

// -- a request dropped while Python ran it, whose route says "again later" -----
{
    const asked = [];
    const made = worker(async (args) => {
        asked.push(args.path);
        await wait(20);                              // Python waits on the page
        return { retry: 10 };
    });
    made.send(1, "request", request("/stats/1"));
    await wait(5);
    made.send(null, "cancel", { id: 1 });
    await wait(80);
    out.droppedWhileRunning = { asked: asked.length, retrying: made.retrying.size,
                                aborted: made.posted.filter((m) => m.aborted).map((m) => m.id) };
}

// -- a retry the page dropped while it waited ----------------------------------
{
    const asked = [];
    const made = worker(async (args) => { asked.push(args.path); await null; return { retry: 30 }; });
    made.send(1, "request", request("/stats/1"));
    await wait(5);
    const waiting = made.retrying.size;
    made.send(null, "cancel", { id: 1 });
    await wait(60);
    out.droppedRetry = { waiting, asked: asked.length, retrying: made.retrying.size,
                         aborted: made.posted.filter((m) => m.aborted).map((m) => m.id) };
}

// -- `later`: a page shown over the viewer, whose reads wait -------------------
{
    const ran = new Map();
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    const retried = new Map();
    const made = worker(async (args) => {
        if (args.path === "/stats/1") await held;
        if (args.path === "/stats/retry") {
            const n = (retried.get(args.path) || 0) + 1;
            retried.set(args.path, n);
            if (n === 1) return { retry: 5 };
        }
        return { status: 200 };
    });
    for (const [id, path] of [[1, "/stats/1"], [2, "/stats/2"], [3, "/stats/retry"], [4, "/save"]]) {
        ran.set(id, path);
        made.send(id, "request", request(path, path === "/save" ? "POST" : "GET"));
    }
    made.send(null, "later", {});
    out.late = made.queue.map((m) => `${m.args.path}${m.late ? " (late)" : ""}`);
    ran.set(5, "/home-thumb"); made.send(5, "request", request("/home-thumb"));
    release();
    for (let i = 0; i < 80 && made.posted.length < 5; i += 1) await tick();
    out.later = answeredPaths(made, ran);
}

console.log(JSON.stringify(out));
