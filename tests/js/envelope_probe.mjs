/**
 * The website's notices of what a browser tab does well
 * (client src/js/services/handoffNotice.js) against the shipped limits
 * (client/src/envelope.json): the open (`judge`, `check`), the batch
 * (`judgeBatch`, `onJob`), the model without WebGPU (`judgeModel`,
 * `beforeModel`) and the stall (`judgeStall`, `tile`). Prints JSON;
 * tests/test_envelope.py asserts on it.
 *
 * Run:  node tests/js/envelope_probe.mjs
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const client = path.join(REPO, "plexora", "client", "src");
const listeners = {};
// The host's Python client, before the script: it listens for job records.
globalThis.PlexoraPython = { on: (kind, fn) => { listeners[kind] = fn; } };
// A classic script: run it with globalThis as its window.
new Function(readFileSync(path.join(client, "js", "services", "handoffNotice.js"), "utf-8"))();
const limits = JSON.parse(readFileSync(path.join(client, "envelope.json"), "utf-8"));
const E = globalThis.PlexoraEnvelope;

const store = new Map();
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
globalThis.fetch = async () => ({ ok: true, json: async () => limits });
const toasts = [];
const said = (since) => toasts.slice(since).map((t) => ({ title: t.title, note: t.note,
                                                          actions: (t.actions || []).map((a) => (a.primary ? `*${a.label}` : a.label)) }));
const app = { fetch: async () => ({ ok: true, json: async () => ({ rows: 2_000_000, columns: 90 }) }),
              flaskVariables: { datasource: "big" },
              PlexoraToast: { show: (options) => toasts.push(options) } };
const ago = (seconds) => new Date(Date.now() - seconds * 1000).toISOString();
const job = (id, capability, done, total, startedSecondsAgo, status = "running") => ({
    kind: "job", job: { job_id: id, capability, status, project: "big", progress: { done, total },
                        started_at: ago(startedSecondsAgo) } });

// The open: shown once, "Continue here" remembered.
await E.check("big", app);
await E.check("big", app);          // once per project per tab
toasts[0]?.actions?.find((a) => a.label === "Continue here")?.onSelect();
const open = said(0);
const rememberedOpen = [...store.keys()];

// The batch: 500 images is within ten minutes, 1000 is not; a job's own pace.
const batchPrior = {
    fits: E.judgeBatch(job("a", "gating.apply_to_dataset", 0, 500, 0).job, limits),
    over: E.judgeBatch(job("b", "gating.apply_to_dataset", 0, 1000, 0).job, limits),
    unmeasured: E.judgeBatch(job("c", "qc.blur_run", 0, 1000, 0).job, limits),
};
const batchPace = {
    slow: E.judgeBatch(job("d", "qc.blur_run", 2, 200, 30).job, limits),     // 15 s a step: 50 min
    quick: E.judgeBatch(job("e", "qc.blur_run", 20, 200, 10).job, limits),   // 0.5 s a step
    finished: E.judgeBatch(job("f", "qc.blur_run", 200, 200, 600).job, limits),
};
let mark = toasts.length;
listeners.job(job("g", "gating.apply_to_dataset", 0, 500, 0));        // fits: nothing said
await new Promise((r) => setTimeout(r, 20));
const quietBatch = said(mark).length;
await E.onJob(job("h", "gating.apply_to_dataset", 0, 2000, 0));
await E.onJob(job("i", "gating.apply_to_dataset", 0, 3000, 0));      // once per project per tab
await E.onJob(job("g", "gating.apply_to_dataset", 500, 500, 0, "done"));
await E.onJob(job("h", "gating.apply_to_dataset", 0, 2000, 0, "done"));
await E.onJob(job("i", "gating.apply_to_dataset", 0, 3000, 0, "done"));
const batch = said(mark);

// The model: no WebGPU adapter here (node has no navigator.gpu).
const model = { gpu: E.judgeModel(true, limits), none: E.judgeModel(false, limits) };
mark = toasts.length;
await E.beforeModel();
await E.beforeModel();
const modelToasts = said(mark);

// The stall: the open's own tiles skipped, then the rolling p95 of 50.
const stallVerdicts = {
    fast: E.judgeStall(Array(50).fill(40), limits),
    slow: E.judgeStall([...Array(45).fill(300), ...Array(5).fill(20)], limits),
    short: E.judgeStall(Array(49).fill(900), limits),
    p95: E.p95([...Array(94).fill(10), ...Array(6).fill(500)]),
};
// While a job holds the worker, tiles are slow for that reason: not counted.
const stallApp = { flaskVariables: { datasource: "slides" }, PlexoraToast: app.PlexoraToast };
E.watchTiles(stallApp);
await E.onJob(job("j", "qc.blur_run", 0, 0, 0));
for (let i = 0; i < 120; i += 1) await E.tile(900, stallApp);
const duringJob = stallApp.__plexoraStall.seen;
await E.onJob(job("j", "qc.blur_run", 1, 1, 0, "done"));
mark = toasts.length;
let firstVerdictAt = null;
for (let i = 0; i < 100; i += 1) {
    const verdict = await E.tile(i < 50 ? 2000 : 400, stallApp);    // the first 50: the open's own
    if (verdict && verdict.verdict === "node" && firstVerdictAt === null) firstVerdictAt = i + 1;
}
const stall = said(mark);
toasts[toasts.length - 1]?.onDismiss?.("user");                     // the x: remembered 30 days
const rememberedStall = [...store.entries()].filter(([k]) => k.endsWith(":stall"))
    .map(([k, v]) => ({ key: k, days: Math.round((Number(v) - Date.now()) / 864e5) }));

// The build: a flat image's pyramid, before it starts. P7's 9.6 GB image, a
// 40 GB and an 80 GB one, on a 14-core computer (12 workers) and a 4-core one.
const flat = (y, x, extra = {}) => ({ shape: [2, y, x], dtype: "uint16", kind: "image", ...extra });
const builds = {
    p7: E.judgeBuild(flat(52923, 45381), limits, { cores: 14 }),
    forty: E.judgeBuild(flat(100000, 100000), limits, { cores: 14 }),
    eighty: E.judgeBuild(flat(100000, 200000), limits, { cores: 14 }),
    p7FourCores: E.judgeBuild(flat(52923, 45381), limits, { cores: 4 }),
    noRoom: E.judgeBuild(flat(52923, 45381), limits, { cores: 14, room: 3e9 }),
    mask: E.judgeBuild({ shape: [1, 100000, 200000], dtype: "uint32", kind: "labels" }, limits, { cores: 14 }),
    noShape: E.judgeBuild({ shape: [], dtype: "uint16" }, limits, { cores: 14 }),
};
Object.defineProperty(globalThis, "navigator", { configurable: true, value: {
    hardwareConcurrency: 14,
    storage: { estimate: async () => ({ quota: 10e9, usage: 6e9 }) } } });
mark = toasts.length;
const inFolder = await E.beforeBuild(flat(52923, 45381, { browser: false }));   // a folder: time only
const inBrowser = await E.beforeBuild(flat(52923, 45381, { browser: true }));   // 5.8 GB into 4 GB of room
await E.beforeBuild(flat(52923, 45381, { browser: true }));                     // once per project per tab
const buildToasts = said(mark);

console.log(JSON.stringify({
    builds, buildVerdicts: [inFolder.verdict, inBrowser.verdict], buildToasts,
    toasts: open.map(({ title, actions }) => ({ title, actions: actions.map((a) => a.replace(/^\*/, "")) })),
    remembered: rememberedOpen,
    orion: E.judge({ rows: 1_260_000, columns: 29 }, limits),
    wide: E.judge({ rows: 1_260_000, columns: 90 }, limits),
    orionOnFourGb: E.judge({ rows: 1_260_000, columns: 29 }, limits, { memoryGb: 4 }),
    noTable: E.judge({ rows: null, columns: null }, limits),
    batchPrior, batchPace, quietBatch, batch,
    model, modelToasts,
    stallVerdicts, duringJob, firstVerdictAt, stall, rememberedStall,
    limitsDated: ["image", "batch", "model", "stall"].every((k) => /^2026-\d\d-\d\d/.test(limits[k].measured || "")),
}));
