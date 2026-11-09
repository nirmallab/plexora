// The pace of the website's pyramid builds (client node/pace.js,
// workers/pyramidWorker.js, services/browserPyramid.js `pace`, and the two
// signals it reads: services/pythonClient.js `busy`, services/browserNode.js
// `busy`). Prints JSON; tests/test_pyramid_pace.py asserts on it.
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = path.join(REPO, "plexora", "client", "src", "js");
const { pacer } = await import(path.join(JS, "node", "pace.js"));
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = {};

// -- node/pace.js ---------------------------------------------------------------
async function most(pace, n, ms = 20) {
    let running = 0;
    let peak = 0;
    const order = [];
    const jobs = [];
    for (let i = 0; i < n; i += 1) {
        jobs.push(pace.run(async () => {
            running += 1;
            peak = Math.max(peak, running);
            order.push(i);
            await wait(ms);
            running -= 1;
            return i;
        }));
    }
    return { jobs, peak: () => peak, order };
}
{
    const free = await most(pacer(), 6);
    const answers = await Promise.all(free.jobs);
    out.unlimited = { peak: free.peak(), answers };
}
{
    const pace = pacer();
    pace.set(1);
    const one = await most(pace, 5);
    await Promise.all(one.jobs);
    out.one = { peak: one.peak(), order: one.order, activeAfter: pace.active };
}
{
    // Raised while work waits: the waiting work starts at once.
    const pace = pacer(1);
    const run = await most(pace, 6, 60);
    await wait(10);
    const before = pace.active;
    pace.set(3);
    await wait(10);
    const raised = pace.active;
    await Promise.all(run.jobs);
    out.raised = { before, raised, peak: run.peak() };
}
{
    // Lowered while work runs: what runs ends, nothing new starts past the limit.
    const pace = pacer();
    const run = await most(pace, 6, 60);
    await wait(10);
    const before = pace.active;
    pace.set(1);
    await wait(70);
    const lowered = pace.active;
    await Promise.all(run.jobs);
    out.lowered = { before, lowered };
}
{
    // A failing piece of work gives its turn back.
    const pace = pacer(1);
    const failed = pace.run(async () => { throw new Error("no"); }).catch((e) => e.message);
    const next = pace.run(async () => "next");
    out.failure = { failed: await failed, next: await next, active: pace.active };
}

// -- services/pythonClient.js `busy` -----------------------------------------------
{
    const posted = [];
    class StandInWorker {
        constructor() { this.onmessage = null; }
        postMessage(message) { posted.push(message); }
    }
    const reply = (op, result = {}) => {
        const message = posted.find((m) => m.op === op && !m.answered);
        message.answered = true;
        workerOf.onmessage({ data: { id: message.id, ok: true, result } });
    };
    let workerOf = null;
    const scope = { location: { href: "http://site.test/", origin: "http://site.test" } };
    globalThis.Worker = class extends StandInWorker {
        constructor(...args) { super(...args); workerOf = this; }
    };
    new Function("window", readFileSync(path.join(JS, "services", "pythonClient.js"), "utf8")
        .replace(/\}\)\(typeof window !== "undefined" \? window : globalThis\);\s*$/, "})(window);"))(scope);
    const python = scope.PlexoraPython;
    const started = python.start({ base: "/py/" });
    await wait(0);
    reply("boot", { version: "test" });
    await started;
    const busy = {};
    busy.idle = python.busy();
    const spec = python.nodeSpec("p");
    await wait(10);
    busy.justSent = python.busy();
    await wait(70);
    busy.after80 = python.busy();
    busy.after80ButAsked200 = python.busy(200);
    reply("nodeSpec", { served: true });
    await spec;
    busy.answered = python.busy();
    // The page's bookkeeping and its answers to Python: never waited on.
    const heap = python.heap();
    await wait(80);
    busy.heapPending = python.busy();
    reply("heap", {});
    await heap;
    out.pythonBusy = busy;
}

// -- services/browserPyramid.js `pace` ---------------------------------------------
{
    const messages = [];
    let worker = null;
    globalThis.Worker = class {
        constructor() { worker = this; this.onmessage = null; this.onerror = null; }
        postMessage(message) { messages.push(message); }
        terminate() { this.terminated = true; }
    };
    const answerers = {};
    let pythonBusy = false;
    let nodeBusy = false;
    const scope = {
        navigator: { hardwareConcurrency: 10 },
        PlexoraPython: { answers: (kind, fn) => { answerers[kind] = fn; }, busy: () => pythonBusy },
        PlexoraNode: { busy: () => nodeBusy },
        PlexoraPicks: { fileFor: () => ({ size: 1 }), adopt() {} },
    };
    new Function("window", readFileSync(path.join(JS, "services", "browserPyramid.js"), "utf8"))(scope);
    const paces = () => messages.filter((m) => m.op === "pace").map((m) => m.hands);
    const { value } = await answerers.pyramid({ path: "/picked/a/cell.ome.tif", key: "k", name: "cell.labels.pyramid.ome.tiff",
                                                kind: "labels", shape: [1, 10, 10], dtype: "uint32", factors: [2],
                                                tile: [256, 256], description: "" });
    const steps = {};
    steps.build = messages[0] && { op: messages[0].op, workers: messages[0].workers };
    await wait(250);
    steps.idle = paces();
    pythonBusy = true;
    await wait(250);
    steps.pythonBusy = paces();
    // The background job's own look (it waits): no request is holding out.
    await answerers.pyramid_status({ id: value.id, wait: 50 });
    await wait(150);
    steps.jobLooked = paces();
    pythonBusy = false;
    await wait(250);
    steps.free = paces();
    nodeBusy = true;
    await wait(250);
    steps.nodeBusy = paces();
    // A request holding out for the build (an import's look, no wait): every hand.
    await answerers.pyramid_status({ id: value.id, wait: 0 });
    steps.awaited = paces();
    await wait(500);
    steps.stillAwaited = paces();
    await wait(800);
    steps.awaitedEnded = paces();
    worker.onmessage({ data: { done: true, file: { size: 5 }, timings: {} } });
    const sentBefore = messages.length;
    nodeBusy = false;
    await wait(300);
    steps.afterDone = { more: messages.length - sentBefore, terminated: Boolean(worker.terminated) };
    steps.listed = Object.keys((await scope.PlexoraPyramid.builds())[0]).sort();
    out.builder = steps;
}

console.log(JSON.stringify(out));
process.exit(0);
