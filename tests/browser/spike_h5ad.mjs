// Spike S-4: an .h5ad the user picked, read by h5py in the website's Python
// worker (Pyodide, the file mounted with WORKERFS as a pick is), against
// CPython's h5py on the same file. Both run tests/browser/spike_h5ad.py: P4's
// reads (truth_p4.py, every answer hashed) and the adapter's table load. The
// h5wasm side is P4's (tools/poc_browser_node: node bench/run.mjs --poc P4).
// Prints JSON; --out FILE writes it too.
//
//   node tests/browser/spike_h5ad.mjs [--dir FIXTURE_DIR] [--files a.h5ad,b.h5ad]
//                                     [--repeat 3] [--adapter-only] [--py 0.0.36] [--out FILE]
import { spawnSync } from "node:child_process";
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { PYTHON, REPO, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const dir = opt("--dir", "C:/plexora-bench/browser_node/fixtures/p4-s");
const names = (opt("--files", null)?.split(",")
    || readdirSync(dir).filter((n) => n.endsWith(".h5ad")).sort());
const files = names.map((n) => path.join(dir, n));
const repeat = Number(opt("--repeat", "3"));
//: Only the adapter's table load (no P4 reads): the heap one table takes.
const adapterOnly = args.includes("--adapter-only");
const py = opt("--py", "0.0.36");
const truthFile = path.join(REPO, "tools", "poc_browser_node", "bench", "truth_p4.py");
const spikeFile = path.join(REPO, "tests", "browser", "spike_h5ad.py");

const out = { dir, files: names, repeat };

// CPython first: it also warms the OS file cache for the page's reads, as P4.
const cpython = spawnSync(PYTHON, [spikeFile, "--truth", truthFile, "--repeat", String(repeat),
                                   ...(adapterOnly ? ["--adapter-only"] : []), ...files],
                          { cwd: REPO, encoding: "utf8", maxBuffer: 256 * 1024 * 1024,
                            env: { ...process.env, PYTHONPATH: REPO } });
if (cpython.status !== 0) throw new Error(`CPython side failed: ${cpython.stderr.slice(-3000)}`);
const truth = JSON.parse(cpython.stdout.trim().split("\n").pop());
out.cpython = truth.environment;

function peakMemory(profile) {
    const needle = path.basename(profile).replace(/'/g, "''");
    const script = [
        `$procs = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object { $_.CommandLine -like '*${needle}*' }`,
        "$out = foreach ($p in $procs) { $g = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue;",
        "  if ($g) { $kind = if ($p.CommandLine -match '--type=([a-z-]+)') { $matches[1] } else { 'browser' };",
        "    [pscustomobject]@{ kind = $kind; peak = $g.PeakWorkingSet64 } } }",
        "$out | ConvertTo-Json -Compress",
    ].join("\n");
    const run = spawnSync("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
                          ["-NoProfile", "-Command", script], { encoding: "utf8", timeout: 30000 });
    const byKind = {};
    try {
        for (const row of [].concat(JSON.parse(run.stdout.trim()))) {
            byKind[row.kind] = Math.max(byKind[row.kind] || 0, row.peak);
        }
    } catch { return null; }
    return byKind;
}

const pyString = (text) => JSON.stringify(text);
const spikeSource = readFileSync(spikeFile, "utf8");
const truthSource = readFileSync(truthFile, "utf8");
const preamble = `
import json, sys, types
if "spike_h5ad" not in sys.modules:
    _m = types.ModuleType("spike_h5ad")
    exec(compile(${pyString(spikeSource)}, "spike_h5ad.py", "exec"), _m.__dict__)
    sys.modules["spike_h5ad"] = _m
import spike_h5ad
`;

const server = await startServer();
const browser = await launch();
try {
    const page = await browser.context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error).slice(0, 300)));
    await page.goto(`http://127.0.0.1:${server.port}/tests/browser/harness/index.html?py=${py}`);
    const info = await page.evaluate(() => window.harness.ready);
    if (info.error) throw new Error(`boot: ${info.error}`);
    out.readyMs = Math.round(await page.evaluate(() => window.harness.marks.ready));
    // The files as a pick hands them over: File objects backed by the disk.
    await page.evaluate(() => {
        const input = document.createElement("input");
        input.type = "file"; input.multiple = true; input.id = "s4";
        document.body.append(input);
    });
    await page.setInputFiles("#s4", files);
    out.mounted = await page.evaluate(() => window.PlexoraPython.mount("/picked/s4",
                                                                       [...document.getElementById("s4").files]));
    out.heapBoot = await page.evaluate(() => window.harness.heap());
    const run = async (code) => JSON.parse(await page.evaluate((c) => window.harness.run(c), preamble + code));
    let started = Date.now();
    out.pyodide = await run("json.dumps(spike_h5ad.environment())");
    out.firstImportMs = Date.now() - started;
    out.stages = await page.evaluate(() => window.harness.stages);
    out.heapStage = await page.evaluate(() => window.harness.heap());
    const truthText = pyString(truthSource);
    out.rows = [];
    out.adapter = [];
    for (const [index, name] of names.entries()) {
        const where = `/picked/s4/${name}`;
        const mine = adapterOnly ? { ops: {} }
            : await run(`json.dumps(spike_h5ad.reads(${truthText}, ${pyString(where)}, ${repeat}))`);
        const ref = truth.files[files[index]].reads || { ops: {} };
        if (mine.error) out.rows.push({ file: name, op: "*", state: "unsupported", error: mine.error });
        for (const [op, theirs] of Object.entries(ref.ops)) {
            const got = mine.ops?.[op];
            if (!got) continue;
            const failed = JSON.stringify(got.sha256).includes("\"error\"");
            out.rows.push({
                file: name, layout: ref.layout, codec: ref.codec, op,
                state: failed ? "unsupported" : JSON.stringify(got.sha256) === JSON.stringify(theirs.sha256) ? "ok" : "differs",
                error: failed ? JSON.stringify(got.sha256).slice(0, 300) : undefined,
                ms: got.ms == null ? null : Math.round(got.ms * 10) / 10,
                cpythonMs: Math.round(theirs.ms * 10) / 10,
                ratio: got.ms == null ? null : Math.round((got.ms / Math.max(theirs.ms, 0.05)) * 10) / 10,
            });
        }
        started = Date.now();
        let loaded;
        try {
            loaded = await run(`json.dumps(spike_h5ad.adapter(${pyString(where)}, ${repeat}))`);
        } catch (error) {
            loaded = { error: String(error.message || error).slice(-600) };
        }
        const theirs = truth.files[files[index]].adapter;
        out.adapter.push({
            file: name, state: loaded.error ? "error" : loaded.sha256 === theirs.sha256 ? "ok" : "differs",
            error: loaded.error, ms: loaded.ms == null ? null : Math.round(loaded.ms),
            cpythonMs: theirs.ms == null ? null : Math.round(theirs.ms),
            ratio: loaded.ms == null ? null : Math.round((loaded.ms / theirs.ms) * 10) / 10,
            rows: loaded.rows, features: loaded.features,
        });
        out[`heapAfter:${name}`] = await page.evaluate(() => window.harness.heap());
    }
    out.peakWorkingSet = peakMemory(browser.profile);
    out.errors = errors;
} finally {
    await browser.close();
    server.stop();
}
out.summary = {
    reads: out.rows.length,
    ok: out.rows.filter((r) => r.state === "ok").length,
    notOk: out.rows.filter((r) => r.state !== "ok").map((r) => `${r.file} ${r.op}: ${r.state}`),
    adapterOk: out.adapter.filter((r) => r.state === "ok").length,
};
const text = JSON.stringify(out, null, 1);
if (opt("--out", null)) writeFileSync(opt("--out", null), text + "\n");
console.log(text);
