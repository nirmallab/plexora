// services/fitHelpers.js: the website's pool of helper Pythons for contrast
// fits, with a stand-in Worker. Run by tests/test_fit_helpers.py.
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(here, "..", "..", "plexora", "client", "src", "js", "services", "fitHelpers.js"), "utf8");

let failures = 0;
const check = (ok, what) => { if (!ok) { failures++; console.log("FAIL " + what); } else console.log("ok   " + what); };
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function load({ cores = 10, memory = 8 } = {}) {
    const workers = [];
    class Worker {
        constructor(url, options) {
            this.url = String(url); this.options = options; this.posted = []; this.terminated = false;
            workers.push(this);
        }
        postMessage(message, transfer) { this.posted.push({ message, transfer }); }
        terminate() { this.terminated = true; }
        reply(data) { this.onmessage({ data }); }
    }
    let answer = null;
    let clock = 1000;
    let resolveReady;
    const ready = new Promise((resolve) => { resolveReady = resolve; });
    const context = createContext({
        URL, Promise, Uint8Array, Map, Object, Number, String, Math, console, setTimeout,
        navigator: { hardwareConcurrency: cores, deviceMemory: memory },
        location: { href: "https://viewer.example/" },
        performance: { now: () => clock },
        Worker,
        PlexoraPython: {
            answers: (kind, fn) => { if (kind === "fit") answer = fn; },
            ready,
            bundle: () => ({ base: "https://assets.example/py/0.0.47/", root: "/" }),
        },
    });
    context.window = context;
    runInContext(source, context);
    return { context, workers, ask: (args) => answer(args).value, resolveReady,
             tick: (ms) => { clock += ms; } };
}

// How many.
{
    const { context } = load();
    const size = context.PlexoraFitHelpers.poolSize;
    check(size({ hardwareConcurrency: 10, deviceMemory: 8 }) === 2, "eight cores or more: two helpers");
    check(size({ hardwareConcurrency: 4 }) === 1, "four cores: one");
    check(size({ hardwareConcurrency: 2 }) === 0, "two cores: none");
    check(size({ hardwareConcurrency: 16, deviceMemory: 2 }) === 0, "under 4 GB: none");
}

// One at a time, from the bundle Python was started from.
{
    const { workers, ask, resolveReady } = load();
    check(ask({ op: "ready" }).ready === false, "nothing ready before Python is");
    check(ask({ op: "start", values: { dtype: "float64", data: new Uint8Array(16) }, code: { data: new Uint8Array(4) }, code_kind: "pyc", code_id: "c1" }) === null,
          "no helper: Python fits for itself");
    resolveReady({});
    await flush(); await flush();
    check(workers.length === 1, "the first helper loads once Python is ready");
    check(workers[0].url === "https://viewer.example/client/src/js/workers/fitWorker.js?base="
          + encodeURIComponent("https://assets.example/py/0.0.47/"), "from the same bundle");
    check(workers[0].options.type === "module", "a module worker");
    workers[0].reply({ op: "ready" });
    check(workers.length === 2, "the second loads when the first is ready");
    check(ask({ op: "ready" }).ready === true, "ready once one is");

    // A fit: handed to an idle helper, its bytes moved there.
    const values = new Uint8Array(new Float64Array([1, 2, 3]).buffer);
    const started = ask({ op: "start", values: { dtype: "float64", shape: [3], data: values }, code: { data: new Uint8Array([7, 7]) }, code_kind: "pyc", code_id: "c1" });
    check(typeof started.id === "string", "a fit is started");
    const sent = workers[0].posted.find((p) => p.message.op === "fit");
    check(sent && sent.message.id === started.id && sent.message.code[0] === 7 && sent.message.codeKind === "pyc"
          && sent.message.codeId === "c1", "the helper is sent the fit and the code");
    check(sent && sent.transfer.length === 2 && sent.transfer[0] === sent.message.values.buffer, "its values moved, not copied");
    check(ask({ op: "result", id: started.id }).pending === true, "pending while it is made");
    // A second fit waits for a free helper (the second is not ready yet).
    const second = ask({ op: "start", values: { dtype: "float64", data: new Uint8Array(8) }, code: { data: new Uint8Array(4) }, code_kind: "pyc", code_id: "c1" });
    check(ask({ op: "result", id: second.id }).pending === true, "queued while every helper is busy");
    workers[0].reply({ op: "done", id: started.id, params: "00ff" });
    check(ask({ op: "result", id: started.id }).params === "00ff", "the fit, once made");
    check(ask({ op: "result", id: started.id }).gone === true, "handed out once");
    check(workers[0].posted.some((p) => p.message.op === "fit" && p.message.id === second.id), "the queued fit goes to the freed helper");
    check(ask({ op: "result", id: "fit-404" }).gone === true, "a job this page does not know (a reload)");
    check(ask({ op: "start", values: { dtype: "float32", data: new Uint8Array(8) }, code: { data: new Uint8Array(4) }, code_kind: "pyc", code_id: "c1" }) === null, "only float64 values");
}

// A helper that fails: its fit fails (Python fits for itself), not replaced.
{
    const { workers, ask, resolveReady } = load({ cores: 4 });
    resolveReady({});
    await flush(); await flush();
    workers[0].reply({ op: "ready" });
    check(workers.length === 1, "four cores: one helper");
    const started = ask({ op: "start", values: { dtype: "float64", data: new Uint8Array(8) }, code: { data: new Uint8Array(4) }, code_kind: "pyc", code_id: "c1" });
    workers[0].onerror(new Error("boom"));
    check(workers[0].terminated, "a failed helper is let go");
    check(typeof ask({ op: "result", id: started.id }).failed === "string", "its fit fails");
    check(ask({ op: "ready" }).ready === false && workers.length === 1, "and is not replaced");
}

// A helper that cannot load: the next one is tried, and none is ready.
{
    const { workers, ask, resolveReady } = load();
    resolveReady({});
    await flush(); await flush();
    workers[0].reply({ op: "failed", error: "no scikit-learn" });
    check(workers[0].terminated && ask({ op: "ready" }).ready === false, "a helper that did not load is let go");
}

// A result nobody came back for is dropped after a minute.
{
    const { workers, ask, resolveReady, tick } = load({ cores: 4 });
    resolveReady({});
    await flush(); await flush();
    workers[0].reply({ op: "ready" });
    const started = ask({ op: "start", values: { dtype: "float64", data: new Uint8Array(8) }, code: { data: new Uint8Array(4) }, code_kind: "pyc", code_id: "c1" });
    workers[0].reply({ op: "done", id: started.id, params: "aa" });
    tick(61000);
    ask({ op: "start", values: { dtype: "float64", data: new Uint8Array(8) }, code: { data: new Uint8Array(4) }, code_kind: "pyc", code_id: "c1" });
    check(ask({ op: "result", id: started.id }).gone === true, "an old result is dropped");
}

// Python that failed to start: no helpers.
{
    const { workers, resolveReady } = load();
    resolveReady({ error: "no Python" });
    await flush(); await flush();
    check(workers.length === 0, "no Python, no helpers");
}

console.log(failures ? `${failures} failed` : "fit helpers: all ok");
process.exit(failures ? 1 : 0);
