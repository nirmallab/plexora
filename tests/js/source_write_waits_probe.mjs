/**
 * A save into a picked file Python wrote (services/pythonClient.js `save`,
 * services/sourceWrites.js) can wait on the user: "Save into <file>?". The
 * answer of the request that wrote it waits for that save; the answers of
 * other requests must not -- a tile, a table read, a status poll would hang
 * behind a question nobody has answered yet.
 *
 * Run in Node over the real module, its worker stood in for: the worker
 * posts a `sourceWrite` event during one request (as workers/pythonWorker.js
 * `sourceWrite` does, naming the request it ran in) and answers it; then
 * answers a second request. Prints JSON; tests/test_source_write_waits.py
 * asserts on it.
 *
 *   node tests/js/source_write_waits_probe.mjs
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(path.join(REPO, "plexora", "client", "src", "js", "services", "pythonClient.js"), "utf8");
const tick = () => new Promise((resolve) => setImmediate(resolve));
const settle = async (n = 20) => { for (let i = 0; i < n; i += 1) await tick(); };

const workers = [];
class Worker {
    constructor(url) { this.url = String(url); this.posted = []; this.onmessage = null; workers.push(this); }
    postMessage(message) { this.posted.push(message); }
    addEventListener() {}
    removeEventListener() {}
    terminate() {}
}
const window = {
    location: { href: "https://viewer.example/", origin: "https://viewer.example", reload() {} },
    navigator: {},
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    crypto: { randomUUID: () => "0123456789abcdef-0123" },
    crossOriginIsolated: false,
    document: null,
};
const context = vm.createContext({
    window, Worker, Blob, URL, URLSearchParams, Headers, Response, Request, console: { ...console, error() {}, warn() {} },
    setTimeout, clearTimeout, setInterval, clearInterval, performance, Date,
    Promise, Map, Set, Math, Number, String, Object, Array, Error, JSON, AbortController,
    TextEncoder, TextDecoder, Uint8Array, Int32Array, ArrayBuffer, DOMException, Atomics, WebAssembly: {},
    globalThis: undefined,
});
context.globalThis = context;
vm.runInContext(SOURCE.replace(/\(typeof window !== "undefined" \? window : globalThis\)\s*;?\s*$/, "(window);"), context);
const python = window.PlexoraPython;

let release = null;
python.saveWith(() => new Promise((resolve) => { release = resolve; }));
python.start({ base: "/py/", root: "/" });
await settle();
const worker = workers[0];
const say = (data) => worker.onmessage({ data });
const sentOf = (path) => worker.posted.find((m) => m.op === "request" && m.args.path === path);
const answer = (id) => ({ id, ok: true, result: { status: 200, headers: [["Content-Type", "text/plain"]],
                                                 body: new TextEncoder().encode("ok"), stream: null } });
say({ id: worker.posted.find((m) => m.op === "boot").id, ok: true, result: { version: "probe" } });
await settle();

const out = { writerAnswered: false, otherAnswered: false, writerAfterSave: false };
python.fetch("/roi/columns", { method: "POST", body: "{}" }).then(() => { out.writerAnswered = true; });
await settle();
const writer = sentOf("/roi/columns");
// Python saves into the picked table while it answers that request.
say({ event: { kind: "sourceWrite", source: "/picked/p1/cells.csv", file: null, request: writer.id } });
say(answer(writer.id));
await settle();
out.writerBeforeSave = out.writerAnswered;

python.fetch("/status").then(() => { out.otherAnswered = true; });
await settle();
say(answer(sentOf("/status").id));
await settle();
out.otherBeforeSave = out.otherAnswered;

release && release({});
await settle();
out.writerAfterSave = out.writerAnswered;
console.log(JSON.stringify(out));
process.exit(0);
