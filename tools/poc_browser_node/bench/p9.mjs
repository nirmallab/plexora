// P9: can Plexora's AI layer run unchanged in the browser? Pyodide in a
// worker beside the viewer, the gating session's Python code as it ships
// (page/py/p9_boot.py lists the few substitutions), against CPython.
//
//   node bench/run.mjs --poc P9 --fixture P9S [--params '{"arms": [...]}']
//
// Arms (all by default, in this order):
//   cold       fresh profile: boot to "ready", then the oracle session; the
//              dump is compared with CPython's (bench/truth_p9.py, cached)
//   return     the same profile and port, relaunched: boot to "ready" from
//              the browser's cache
//   throttled  fresh profile, every response over one 50 Mbps / 20 ms link
//              (serve_static.py --throttle): boot to "ready"
//   viewer     fresh profile per block, ABAB x 4: the viewer's open and pan
//              with the worker booting beside it and without
//   M          cold boot + session on the P9M fixture, compared likewise
//   L          the 1.26 M-cell Orion table read by polars in the worker
// Records: metrics.{boot, session, viewer, tableProbe, heap, python}, checks.
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync, mkdtempSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { POC_ROOT, PYTHON, freePort, peakMemory, sleep } from "./procs.mjs";

const BENCH = path.join(process.env.PLEXORA_BENCH_HOME || "C:/plexora-bench", "browser_node");
const CACHE = path.join(BENCH, "cache");
const DIST = path.join(BENCH, "pyodide", "314.0.7");
//: What the static origin serves as /pyodide/ and /wheels/: the release as
//: published, or a variant of it (`--params '{"dist": "pyc"}'`: bench/p9_dist.py --variant pyc).
let SERVED = DIST;
//: https with bench/selfsigned.py's certificate (`--params '{"tls": true}'`):
//: viewer.plexoraapp.com is https, and Chrome offers brotli only over https.
let TLS = null;
//: Leave p9_packages.json's `lazy` packages out of the boot (`--params '{"lazy": true}'`).
let LAZY = false;

function selfSignedCert() {
    const dir = path.join(CACHE, "tls");
    python([path.join(POC_ROOT, "bench", "selfsigned.py"), dir], "selfsigned.py");
    const cert = path.join(dir, "cert.pem");
    // Trusted by its key's hash rather than with --ignore-certificate-errors:
    // Chrome keeps no HTTP cache for an origin whose certificate it overrode,
    // and the return arm reads that cache.
    const spki = python(["-c", "import base64, hashlib, sys; from cryptography import x509; "
        + "from cryptography.hazmat.primitives import serialization as s; "
        + "c = x509.load_pem_x509_certificate(open(sys.argv[1], 'rb').read()); "
        + "print(base64.b64encode(hashlib.sha256(c.public_key().public_bytes(s.Encoding.DER, "
        + "s.PublicFormat.SubjectPublicKeyInfo)).digest()).decode())", cert], "spki").trim();
    return { cert, key: path.join(dir, "key.pem"), spki };
}
const SCRATCH = path.join(BENCH, "scratch", "p9");
const THROTTLE = "50:20";
const ARMS = ["cold", "return", "throttled", "viewer", "M", "L"];

function python(args, what) {
    const run = spawnSync(PYTHON, ["-I", ...args], { encoding: "utf8", maxBuffer: 1 << 26 });
    if (run.status !== 0) throw new Error(`${what} failed:\n${(run.stderr || "").slice(-3000)}`);
    return run.stdout;
}

/** The fixture folder (bench/p9_fixture.py), made once. */
function fixtureDir(name) {
    python([path.join(POC_ROOT, "bench", "p9_fixture.py"), "--fixture", name], "p9_fixture.py");
    return path.join(CACHE, `p9-${name}`);
}

/** CPython's reference dump (bench/truth_p9.py, 3 runs), kept while the fixture and plexora are unchanged. */
function truth(ctx, name, prototype = null) {
    const dir = fixtureDir(name);
    const file = path.join(CACHE, `truth-p9-${name}${prototype ? "-" + prototype : ""}.json`);
    const sources = createHash("sha256");
    for (const f of ["p9_boot.py", "p9_run.py", "p9_oracle.py"]) sources.update(readFileSync(path.join(POC_ROOT, "page", "py", f)));
    const key = JSON.stringify({ fixture: JSON.parse(readFileSync(path.join(dir, "key.json"), "utf8")),
                                 sources: sources.digest("hex"),
                                 wheel: JSON.parse(readFileSync(path.join(DIST, "wheels", "manifest.json"), "utf8"))
                                     .wheels.find((w) => w.file.startsWith("plexora-"))?.sha256 });
    const keyFile = `${file}.key`;
    if (!existsSync(file) || !existsSync(keyFile) || readFileSync(keyFile, "utf8") !== key) {
        ctx.log(`CPython reference for ${name} (3 runs)`);
        python([path.join(POC_ROOT, "bench", "truth_p9.py"), "--fixture", name, "--runs", "3", "--out", file,
                ...(prototype ? ["--prototype", prototype] : [])], "truth_p9.py");
        writeFileSync(keyFile, key);
    }
    return { file, record: JSON.parse(readFileSync(file, "utf8")) };
}

function fixtureFiles(dir) {
    const files = path.join(dir, "root", "_gsynth_files");
    return [path.join(files, "image.ome.tif"), path.join(files, "mask.tif"), path.join(files, "cells.csv"),
            path.join(dir, "config.browser.json"), path.join(dir, "info.json")];
}

async function openP9(ctx, { files = [], profile = null, port = 0, throttle = null } = {}) {
    const stat = await ctx.startStatic({ pyodide: SERVED, port, throttle, tls: TLS });
    const browser = await ctx.launchChrome({ headed: ctx.args.headed, profile,
                                             extraArgs: TLS ? [`--ignore-certificate-errors-spki-list=${TLS.spki}`] : [] });
    const t0 = Date.now();
    await browser.page.goto(`${stat.origin}/poc/index.html`);
    await browser.page.waitForFunction(() => window.POC && document.getElementById("status").textContent === "ready",
                                       null, { timeout: 120000 });
    if (files.length) await browser.page.setInputFiles("#picker", files);
    return { stat, browser, page: browser.page, port: stat.port, gotoMs: Date.now() - t0 };
}

async function close(o) {
    await o.browser.close();
    o.stat.proc.kill();
}

/** Boot, the session, the comparison: one fixture in one fresh profile. */
async function coldSession(ctx, fixture, { profile = null, port = 0, prototype = null } = {}) {
    const dir = fixtureDir(fixture);
    const ref = truth(ctx, fixture, prototype);
    const o = await openP9(ctx, { files: fixtureFiles(dir), profile, port });
    try {
        const boot = await o.page.evaluate((lazy) => window.POC.p9Boot({ lazy }), LAZY);
        ctx.log(`${fixture} cold ready at ${Math.round(boot.phases.ready)} ms, ${(boot.bytes.total / 1e6).toFixed(1)} MB`);
        const session = await o.page.evaluate((prototype) => window.POC.p9Session({ prototype }), prototype);
        mkdirSync(SCRATCH, { recursive: true });
        const dumpFile = path.join(SCRATCH, `run-${fixture}.json`);
        writeFileSync(dumpFile, JSON.stringify(session.dump));
        const compared = JSON.parse(python([path.join(POC_ROOT, "bench", "compare_p9.py"), ref.file, dumpFile],
                                           "compare_p9.py"));
        const memory = peakMemory(o.browser.userDataDir);
        const t = session.dump.summary.times;
        const py = ref.record.median;
        ctx.log(`${fixture} session ${t.total.toFixed(2)} s vs CPython ${py.total.toFixed(2)} s: ${compared.verdict}`);
        return {
            port: o.port, userDataDir: o.browser.userDataDir,
            boot, session: { wall: session.wall, times: t, cpython: py, cpythonRuns: ref.record.runs.map((r) => r.times),
                             ratio: t.total / py.total, packets: session.dump.summary.packets,
                             kinds: session.dump.summary.kinds, units: session.dump.units,
                             excess: session.dump.summary.excess, counters: session.dump.counters,
                             reads: session.reads },
            compare: compared, cpythonSelf: ref.record.self.map((s) => s.verdict),
            heap: { afterReady: boot.heap, afterSession: session.heap }, peakMemory: memory,
            versions: { pyodide: session.dump.versions, cpython: ref.record.dump.versions,
                        python: [session.dump.python, ref.record.dump.python] },
            logs: [...o.browser.logs.slice(-20), ...session.logs.slice(-20)],
        };
    } finally {
        await close(o);
    }
}

async function bootOnly(ctx, { profile = null, port = 0, throttle = null } = {}) {
    const o = await openP9(ctx, { profile, port, throttle });
    try {
        const boot = await o.page.evaluate((lazy) => window.POC.p9Boot({ lazy }), LAZY);
        return { boot, gotoMs: o.gotoMs };
    } finally {
        await close(o);
    }
}

/**
 * The viewer on fixtures.json's S image (P1's) with and without the worker
 * booting beside it, ABAB, a fresh profile each (both arms cold).
 */
async function viewerArm(ctx, blocks = 4) {
    const image = JSON.parse(readFileSync(path.join(POC_ROOT, "bench", "fixtures.json"), "utf8")).S.image;
    const rows = [];
    for (let b = 0; b < blocks; b += 1) {
        for (const worker of b % 2 === 0 ? [true, false] : [false, true]) {
            const o = await openP9(ctx, { files: [image] });
            try {
                const r = await o.page.evaluate(({ worker, image, lazy }) => window.POC.p9ViewerOpen({
                    worker, image, lazy,
                    channels: [0, 1, 2].map((index) => ({ index, color: window.POC.lib.PALETTE[index], range: [0, 0.2] })),
                }), { worker, image: path.basename(image), lazy: LAZY });
                rows.push({ block: b, ...r });
                ctx.log(`viewer block ${b} worker=${worker}: open ${Math.round(r.openMs)} ms, pan p95 ${r.pan.intervals.p95?.toFixed(1)} ms, long tasks ${r.longTasks.length}`);
            } finally {
                await close(o);
            }
        }
    }
    const of = (w) => rows.filter((r) => r.worker === w);
    const median = (v) => { const s = v.slice().sort((a, b) => a - b); return s.length ? s[Math.floor((s.length - 1) / 2)] : null; };
    const longMs = (r) => r.longTasks.reduce((a, t) => a + t.ms, 0);
    return {
        rows: rows.map((r) => ({ ...r, longTasks: { count: r.longTasks.length, ms: longMs(r), max: Math.max(0, ...r.longTasks.map((t) => t.ms)) } })),
        openRatio: median(of(true).map((r) => r.openMs)) / median(of(false).map((r) => r.openMs)),
        panP95: { worker: median(of(true).map((r) => r.pan.intervals.p95)), control: median(of(false).map((r) => r.pan.intervals.p95)) },
        longTasks: { worker: median(of(true).map((r) => r.longTasks.length)), control: median(of(false).map((r) => r.longTasks.length)),
                     workerMs: median(of(true).map(longMs)), controlMs: median(of(false).map(longMs)) },
        panDuringBoot: of(true).filter((r) => r.panEndedBeforeReady).length,
    };
}

/** L: the Orion table (1.26 M x 29) read by polars in the worker, against polars in CPython. */
async function tableProbe(ctx) {
    const L = JSON.parse(readFileSync(path.join(POC_ROOT, "bench", "fixtures.json"), "utf8")).L;
    const truthFile = path.join(CACHE, "truth-table-L.json");
    const cpython = existsSync(truthFile) ? JSON.parse(readFileSync(truthFile, "utf8")).polarsReadMs : null;
    const o = await openP9(ctx, { files: [L.table] });
    try {
        await o.page.evaluate((lazy) => window.POC.p9Boot({ lazy }), LAZY);
        const r = await o.page.evaluate((a) => window.POC.p9TableProbe(a),
                                        { name: path.basename(L.table), column: L.marker });
        ctx.log(`L: ${r.rows} x ${r.columns}: column store ${Math.round(r.storeMs)} ms, to Python ${Math.round(r.handMs)} ms, frame ${Math.round(r.frameMs)} ms (CPython read_csv ${Math.round(cpython)} ms), heap ${(r.heap / 2 ** 30).toFixed(2)} GiB`);
        const browserMs = r.storeMs + r.handMs + r.frameMs;
        return { ...r, browserMs, cpythonReadMs: cpython, ratio: cpython ? browserMs / cpython : null,
                 peakMemory: peakMemory(o.browser.userDataDir) };
    } catch (error) {
        return { error: String(error.message || error).slice(-1500) };
    } finally {
        await close(o);
    }
}

export async function P9(ctx) {
    const params = ctx.args.params || {};
    const arms = params.arms || ARMS;
    SERVED = params.dist ? `${DIST}-${params.dist}` : DIST;
    TLS = params.tls ? selfSignedCert() : null;
    LAZY = Boolean(params.lazy);
    const fixture = ctx.fixtureName?.startsWith("P9") ? ctx.fixtureName : "P9S";
    const metrics = { arms, dist: path.basename(SERVED), prototype: params.prototype || null, tls: Boolean(TLS), lazy: LAZY, throttle: { mbps: 50, rttMs: 20, how: "serve_static.py --throttle: one shared link, server side" } };
    const checks = {};
    const notes = [
        `Served: ${path.basename(SERVED)}${TLS ? " over https" : " over http"}${LAZY ? ", pyarrow fetched at the first Parquet call" : ""}.`,
        ...(params.prototype ? [`Prototype of a product change on both sides: ${params.prototype} (page/py/p9_boot.py).`] : []),
    ];
    const logs = [];
    const port = await freePort();
    let profile = null;
    if (arms.includes("cold")) {
        profile = mkdtempSync(path.join(tmpdir(), "plx-p9-"));
        const r = await coldSession(ctx, fixture, { profile, port, prototype: params.prototype });
        metrics.boot = { cold: r.boot };
        metrics.session = { [fixture]: r.session };
        metrics.heap = { [fixture]: r.heap };
        metrics.peakMemory = { [fixture]: r.peakMemory };
        metrics.python = r.versions;
        metrics.shims = r.boot.shims;
        metrics.shimCodeLines = Object.fromEntries(Object.entries(r.boot.shims).map(([k, v]) => [k, v.code]));
        checks[fixture] = { ...r.compare, cpythonSelf: r.cpythonSelf };
        logs.push(...r.logs);
    }
    if (arms.includes("return")) {
        if (!profile) throw new Error("the return arm follows the cold arm");
        metrics.boot.return = (await bootOnly(ctx, { profile, port })).boot;
        ctx.log(`return ready at ${Math.round(metrics.boot.return.phases.ready)} ms`);
    }
    if (profile) { try { rmSync(profile, { recursive: true, force: true }); } catch (e) { /* busy */ } }
    if (arms.includes("throttled")) {
        (metrics.boot ||= {}).throttled = (await bootOnly(ctx, { throttle: THROTTLE })).boot;
        ctx.log(`throttled ready at ${Math.round(metrics.boot.throttled.phases.ready)} ms`);
    }
    if (arms.includes("viewer")) metrics.viewer = await viewerArm(ctx, params.blocks || 4);
    if (arms.includes("M")) {
        const r = await coldSession(ctx, "P9M", { prototype: params.prototype });
        (metrics.session ||= {}).P9M = r.session;
        (metrics.heap ||= {}).P9M = r.heap;
        (metrics.peakMemory ||= {}).P9M = r.peakMemory;
        (metrics.boot ||= {}).coldM = r.boot;
        checks.P9M = { ...r.compare, cpythonSelf: r.cpythonSelf };
    }
    if (arms.includes("L")) metrics.tableProbe = await tableProbe(ctx);
    return { metrics, checks, notes, logs, browser: "chromium", arm: "pyodide-worker", state: "P9" };
}
