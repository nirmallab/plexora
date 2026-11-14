// A reload of the site in Chrome or Edge, where the tab's Python is a
// SharedWorker kept across it (client workers/pythonWorker.js `attach`,
// `leftBehind`, `pageNow`, `drain`, its `onmessage`): a write the page before
// had queued while Python was busy must still reach Python once the new page
// connects (services/backend.js: "a write the page made as it left still
// lands"), and its answer must not reach the new page, whose ids are its own.
// Python is stood in for by OPS.request; each page by a port that records what
// it is posted. Prints JSON; tests/test_tab_python_reload_keeps_writes.py
// asserts on it.
//
//   node tests/js/tab_python_reload_keeps_writes_probe.mjs   (from the repository root)
import assert from "node:assert/strict";
import fs from "node:fs";

const source = fs.readFileSync("plexora/client/src/js/workers/pythonWorker.js", "utf8");
const pick = (pattern, name) => {
    const found = source.match(pattern);
    assert.ok(found, `pythonWorker.js has no ${name}`);
    return found[0];
};
const code = [
    pick(/\nfunction attach\(port\) \{[\s\S]*?\n\}\n/, "attach(port)"),
    pick(/\nfunction leftBehind\(number\) \{[\s\S]*?\n\}\n/, "leftBehind(number)"),
    pick(/\nconst pageNow = [^\n]*\n/, "pageNow"),
    pick(/\nasync function drain\(\) \{[\s\S]*?\n\}\n/, "drain()"),
    pick(/\nconst turns = new MessageChannel\(\);[\s\S]*?\nasync function letIn\(\) \{[\s\S]*?\n\}\n/, "letIn()"),
    pick(/\nself\.onmessage = \(\{ data \}\) => \{[\s\S]*?\n\};\n/, "self.onmessage"),
    pick(/\nfunction ahead\(message\) \{[\s\S]*?\n\}\n/, "ahead(message)"),
    pick(/\nconst isRead = [\s\S]*?;\n/, "isRead"),
].join("");

//: The worker's state around these pieces, at rest, as a tab's worker.
const PREAMBLE = `
let page = null; let pageNumber = 0; const unsent = []; let goneTimer = null; let booted = true;
function watchPage() {} const asks = new Map();
const queue = []; let busy = false; let idling = 0; let store = null; let lastPull = 0; let lastQuiet = 0;
const PULL_STALE_MS = 10000; async function pullNow() { return false; }
let fatalTold = false; let fatalWith = null; const AFTER_FATAL = new Set(["boot", "heap", "trace", "state"]);
let current = null; let inStep = false;
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

//: One page of the tab: its port, and what the worker posted to it.
function pagePort(name) {
    const got = [];
    return { name, got, onmessage: null, onmessageerror: null, start() {},
             postMessage(message) { got.push(message); } };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const request = (path, method = "GET") => ({ method, path, query: "", headers: [], body: null });

// The worker as a tab's: what it posts goes to the page answered now.
const asked = [];
let release;
const held = new Promise((resolve) => { release = resolve; });
const OPS = {
    request: async (args) => {
        asked.push(`${args.method} ${args.path}`);
        if (args.path === "/stats/slow") await held;   // Python busy as the page leaves
        return { status: 200, path: args.path };
    },
    state: () => ({}),
};
const self = {};
const make = new Function("self", "OPS", "MessageChannel",
                          `${PREAMBLE}${code}
                           self.postMessage = (message, transfer) => { if (page) page.postMessage(message, transfer || []); };
                           return { attach, queue };`);
const made = make(self, OPS, TaskChannel);

// The first page: a slow read Python is running, then the user's edit saved.
const first = pagePort("first");
made.attach(first);
first.onmessage({ data: { id: 1, op: "request", args: request("/stats/slow") } });
await tick();
first.onmessage({ data: { id: 2, op: "request", args: request("/gates/CD3", "PUT") } });
first.onmessage({ data: { id: 3, op: "request", args: request("/stats/other") } });
const queuedBefore = made.queue.map((m) => `${m.args.method} ${m.args.path}`);

// The user reloads the site: the page it brings connects to the same worker.
const second = pagePort("second");
made.attach(second);
const queuedAfter = made.queue.map((m) => `${m.args.method} ${m.args.path}`);

// Python finishes the slow read; whatever is still queued runs.
release();
for (let i = 0; i < 60; i += 1) await tick();

console.log(JSON.stringify({
    queuedBefore,
    queuedAfter,
    asked,
    // Answers with an id the second page was posted: ids 1-3 were the first page's.
    answersToSecond: second.got.filter((m) => m && m.id !== undefined).map((m) => m.id),
}));
