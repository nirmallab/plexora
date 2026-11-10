/**
 * One Python per tab, kept across a reload (services/pythonClient.js
 * `spawn`, `tabName`, `restart`), run in Node over the real module with the
 * browser stood in for: a SharedWorker whose options say whether the browser
 * read `extendedLifetime`, Web Locks, sessionStorage. Each case is a fresh
 * copy of the module ("a page"). Prints JSON; tests/test_tab_python.py holds
 * it to the expectations.
 *
 *   node tests/js/tab_python_probe.mjs
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(path.join(REPO, "plexora", "client", "src", "js", "services", "pythonClient.js"), "utf8");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

//: Web Locks, one origin's: a request waits until the name is free, or its signal aborts.
function lockManager() {
    const held = new Map();   // name -> release()
    const queues = new Map(); // name -> [grant]
    const grantNext = (name) => {
        const queue = queues.get(name) || [];
        const next = queue.shift();
        if (next) next();
    };
    return {
        held,
        request(name, options, callback) {
            return new Promise((resolve, reject) => {
                const grant = () => {
                    let release;
                    const done = new Promise((r) => { release = r; });
                    held.set(name, release);
                    Promise.resolve(callback({ name })).then(resolve, reject);
                    done.then(() => { held.delete(name); grantNext(name); });
                };
                if (options.signal) {
                    options.signal.addEventListener("abort", () => {
                        const queue = queues.get(name) || [];
                        const at = queue.indexOf(grant);
                        if (at >= 0) { queue.splice(at, 1); reject(new Error("AbortError")); }
                    });
                }
                if (!held.has(name)) grant();
                else { if (!queues.has(name)) queues.set(name, []); queues.get(name).push(grant); }
            });
        },
        //: The page holding `name` goes away (its lock let go).
        drop(name) { const release = held.get(name); if (release) release(); },
    };
}

//: A browser: `reads` whether its SharedWorker reads `extendedLifetime`.
function browser({ reads = true, locks = lockManager(), storage = new Map() } = {}) {
    const made = { shared: [], dedicated: [], reloads: 0 };
    class SharedWorker {
        constructor(url, options = {}) {
            this.url = String(url);
            this.options = { name: options.name, type: options.type };
            if (reads) this.options.extendedLifetime = options.extendedLifetime;
            this.posted = [];
            const posted = this.posted;
            this.port = { postMessage: (message) => posted.push(message), start() {}, close() { this.closed = true; },
                          onmessage: null };
            made.shared.push(this);
        }
    }
    class Worker {
        constructor(url, options) {
            this.url = String(url);
            this.posted = [];
            made.dedicated.push(this);
        }
        postMessage(message) { this.posted.push(message); }
    }
    const window = {
        location: { href: "https://viewer.example/", origin: "https://viewer.example", reload: () => { made.reloads += 1; } },
        navigator: { locks },
        sessionStorage: { getItem: (k) => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)),
                          removeItem: (k) => storage.delete(k) },
        crypto: { randomUUID: () => `${Math.random().toString(16).slice(2)}-${Math.random().toString(16).slice(2)}` },
        crossOriginIsolated: false,
        document: null,
    };
    const context = vm.createContext({ window, SharedWorker, Worker, Blob, URL, setTimeout, clearTimeout, console, performance,
                                       Promise, Map, Set, Math, Number, String, Object, Array, Error, JSON, AbortController,
                                       TextEncoder, TextDecoder, Uint8Array, Int32Array, DOMException,
                                       WebAssembly: { Suspending: function Suspending() {} }, globalThis: undefined });
    context.globalThis = context;
    vm.runInContext(SOURCE.replace(/\(typeof window !== "undefined" \? window : globalThis\)\s*;?\s*$/, "(window);"), context);
    return { python: window.PlexoraPython, made, storage, locks, window };
}

const lastShared = (made) => made.shared.filter((w) => !String(w.options.name).startsWith("plexora-probe-")).pop() || null;
const out = {};

// Firefox, Safari: no SharedWorker kept across a reload -- the page's own worker.
{
    const page = browser({ reads: false });
    page.python.warm({ base: "/py/", root: "/", tab: true });
    await sleep(20);
    out.other = { mode: page.python.mode(), dedicated: page.made.dedicated.length, shared: lastShared(page.made) ? 1 : 0 };
}

// Chrome: the tab's worker, named for it; what was posted before it connected is sent then.
const locks = lockManager();
const storage = new Map();
let firstName = null;
{
    const page = browser({ locks, storage });
    page.python.warm({ base: "/py/", root: "/", tab: true });
    page.python.start({ base: "/py/", root: "/", tab: true });
    await sleep(30);
    const worker = lastShared(page.made);
    firstName = worker && worker.options.name;
    const boot = worker && worker.posted.find((m) => m.op === "boot");
    out.first = { mode: page.python.mode(), dedicated: page.made.dedicated.length, name: firstName,
                  extended: worker && worker.options.extendedLifetime, module: worker && worker.options.type,
                  stored: storage.get("plexora-python-tab"), held: locks.held.has(firstName),
                  ops: worker ? worker.posted.map((m) => m.op) : [],
                  bootShares: boot ? { cancels: boot.args.cancels, inFlight: boot.args.inFlight } : null };
    // A job stopped: by message, since there is no shared memory.
    out.first.cancel = page.python.cancel("job-7");
    out.first.cancelMessage = worker.posted.filter((m) => m.op === "cancelJob").map((m) => m.args.id);
    // The worker says it found no Python running (a first page).
    worker.port.onmessage({ data: { attached: { booted: false, page: 1, fatal: false } } });
    out.first.reattached = page.python.reattached();
    // The self-check's scratch boot never takes the tab's worker.
    const check = browser({ locks: lockManager() });
    check.python.start({ base: "/py/", root: "/", scratch: true, tab: true });
    await sleep(20);
    out.scratch = { mode: check.python.mode(), shared: lastShared(check.made) ? 1 : 0 };
    // The page reloads: its lock let go.
    locks.drop(firstName);
}

// The reload: the same id, the same worker's name; it says Python was running.
{
    const page = browser({ locks, storage });
    const t0 = Date.now();
    page.python.start({ base: "/py/", root: "/", tab: true });
    await sleep(30);
    const worker = lastShared(page.made);
    worker.port.onmessage({ data: { attached: { booted: true, page: 2, fatal: false } } });
    out.reload = { name: worker.options.name, same: worker.options.name === firstName, waitedMs: Date.now() - t0,
                   reattached: page.python.reattached() };
    // A copy of this tab (sessionStorage copied) while it lives: a new id, its own worker.
    const copyStorage = new Map(storage);
    const copy = browser({ locks, storage: copyStorage });
    const t1 = Date.now();
    copy.python.start({ base: "/py/", root: "/", tab: true });
    await sleep(1700);
    const copied = lastShared(copy.made);
    out.copy = { name: copied && copied.options.name, differs: Boolean(copied) && copied.options.name !== firstName,
                 kept: copyStorage.get("plexora-python-tab") !== storage.get("plexora-python-tab"),
                 waitedAtLeastMs: Date.now() - t1 };
    // Python failed for good in the tab: the page asks for a new one.
    worker.port.onmessage({ data: { id: worker.posted.find((m) => m.op === "boot").id, ok: true, result: { dead: true } } });
    await sleep(400);
    out.dead = { quit: worker.posted.some((m) => m.op === "quit"), stored: storage.has("plexora-python-tab"),
                 reloads: page.made.reloads };
}

console.log(JSON.stringify(out));
process.exit(0);
