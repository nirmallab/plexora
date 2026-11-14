/**
 * The website's Python worker failing to load (services/pythonClient.js
 * `spawn`, `start`): a module worker whose script (or one of its imports)
 * cannot be fetched or evaluated never posts a message; the browser fires the
 * worker's `error` event and nothing more. The page's start() must still
 * settle -- with an error the loader can show ("Plexora could not start") --
 * instead of leaving the loader spinning for good.
 *
 * Run in Node over the real module, the browser stood in for, with a fake
 * clock so any watchdog the page arms (a boot deadline, a heartbeat) runs
 * without waiting: five minutes are let pass after the error. Prints JSON;
 * tests/test_python_worker_dies.py holds it to the expectations.
 *
 *   node tests/js/python_worker_dies_probe.mjs
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(path.join(REPO, "plexora", "client", "src", "js", "services", "pythonClient.js"), "utf8");
const tick = () => new Promise((resolve) => setImmediate(resolve));

//: A clock the probe moves: setTimeout/setInterval run only when advanced past.
function fakeClock() {
    let now = 1000;
    let nextId = 1;
    const timers = new Map();
    const clock = {
        now: () => now,
        setTimeout(fn, ms = 0, ...args) {
            const id = nextId++;
            timers.set(id, { fn, at: now + Math.max(0, Number(ms) || 0), every: null, args });
            return id;
        },
        setInterval(fn, ms = 0, ...args) {
            const id = nextId++;
            const every = Math.max(1, Number(ms) || 0);
            timers.set(id, { fn, at: now + every, every, args });
            return id;
        },
        clearTimeout(id) { timers.delete(id); },
        clearInterval(id) { timers.delete(id); },
        async advance(ms) {
            const until = now + ms;
            for (let guard = 0; guard < 100000; guard += 1) {
                await tick();
                let due = null;
                for (const [id, timer] of timers) {
                    if (timer.at <= until && (!due || timer.at < due[1].at)) due = [id, timer];
                }
                if (!due) break;
                const [id, timer] = due;
                now = Math.max(now, timer.at);
                if (timer.every) timer.at = now + timer.every;
                else timers.delete(id);
                try { timer.fn(...timer.args); } catch (error) { /* as a browser would: logged, not fatal */ }
            }
            now = until;
            await tick();
        },
    };
    return clock;
}

function locks() {
    const held = new Set();
    return {
        request(name, options, callback) {
            if (held.has(name)) return new Promise(() => {});
            held.add(name);
            return Promise.resolve(callback({ name }));
        },
    };
}

//: A worker the page can make (`Worker` or `SharedWorker`) whose script never
//: runs: `fail()` fires its error event as the browser does on a failed load.
function deadWorkers(made) {
    const errorTarget = (target) => {
        target.listeners = [];
        target.addEventListener = (type, fn) => { if (type === "error") target.listeners.push(fn); };
        target.removeEventListener = (type, fn) => { target.listeners = target.listeners.filter((one) => one !== fn); };
        target.fail = () => {
            const event = { type: "error", message: "", filename: "", preventDefault() {} };
            if (typeof target.onerror === "function") target.onerror(event);
            for (const fn of target.listeners) fn(event);
        };
    };
    class Worker {
        constructor(url, options) {
            this.url = String(url);
            this.posted = [];
            this.onmessage = null;
            this.onerror = null;
            errorTarget(this);
            made.dedicated.push(this);
        }
        postMessage(message) { this.posted.push(message); }
        terminate() { this.terminated = true; }
    }
    class SharedWorker {
        constructor(url, options = {}) {
            this.url = String(url);
            this.options = { name: options.name, type: options.type, extendedLifetime: options.extendedLifetime };
            this.posted = [];
            this.onerror = null;
            errorTarget(this);
            const posted = this.posted;
            this.port = { postMessage: (message) => posted.push(message), start() {}, close() { this.closed = true; },
                          onmessage: null, onmessageerror: null, addEventListener() {}, removeEventListener() {} };
            made.shared.push(this);
        }
    }
    return { Worker, SharedWorker };
}

//: A page: a fresh copy of the module. `tab`: a browser that keeps a
//: SharedWorker across a reload (Chrome, Edge); else the page's own worker.
function page({ tab }) {
    const clock = fakeClock();
    const made = { shared: [], dedicated: [], reloads: 0 };
    const { Worker, SharedWorker } = deadWorkers(made);
    const storage = new Map();
    const window = {
        location: { href: "https://viewer.example/", origin: "https://viewer.example",
                    reload: () => { made.reloads += 1; } },
        navigator: { locks: locks() },
        sessionStorage: { getItem: (k) => (storage.has(k) ? storage.get(k) : null),
                          setItem: (k, v) => storage.set(k, String(v)), removeItem: (k) => storage.delete(k) },
        crypto: { randomUUID: () => "0123456789abcdef-0123" },
        crossOriginIsolated: false,
        document: null,
    };
    const context = vm.createContext({
        window, Worker, Blob, URL, console: { ...console, error() {}, warn() {} },
        setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
        setInterval: clock.setInterval, clearInterval: clock.clearInterval,
        performance: { now: clock.now }, Date: { now: clock.now },
        Promise, Map, Set, Math, Number, String, Object, Array, Error, JSON, AbortController,
        TextEncoder, TextDecoder, Uint8Array, Int32Array, DOMException, Atomics,
        WebAssembly: tab ? { Suspending: function Suspending() {} } : {},
        globalThis: undefined,
        ...(tab ? { SharedWorker } : {}),
    });
    context.globalThis = context;
    vm.runInContext(SOURCE.replace(/\(typeof window !== "undefined" \? window : globalThis\)\s*;?\s*$/, "(window);"), context);
    return { python: window.PlexoraPython, made, clock };
}

//: start() on a page whose worker fails to load; what start() came to after
//: five minutes.
async function failedLoad({ tab }) {
    const { python, made, clock } = page({ tab });
    let outcome = { settled: false };
    python.start({ base: "/py/", root: "/", tab }).then(
        (info) => { outcome = { settled: true, error: info && info.error ? String(info.error) : null }; },
        (error) => { outcome = { settled: true, error: String((error && error.message) || error) }; },
    );
    // The page's worker made (a tab's after its lock is taken).
    await clock.advance(50);
    const worker = tab
        ? made.shared.filter((one) => !String(one.options.name).startsWith("plexora-probe-")).pop()
        : made.dedicated[0];
    const posted = worker ? worker.posted.map((message) => message.op) : [];
    if (worker) worker.fail();
    await clock.advance(5 * 60 * 1000);
    return { made: Boolean(worker), posted, ...outcome, stopped: python.stopped() !== null,
             mode: python.mode(), reloads: made.reloads };
}

const out = {};
out.page = await failedLoad({ tab: false });
out.tab = await failedLoad({ tab: true });
console.log(JSON.stringify(out));
process.exit(0);
