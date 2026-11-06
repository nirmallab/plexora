// node bench/run.mjs --poc P0|P1|P1V|... --fixture S|M|L [--headed] [--out DIR] [--note TEXT] [--browser firefox|webkit]
//
// Starts the static origin (serve_static.py), the Plexora data node(s) a POC
// needs as its Python ground truth, and Chrome on the GPU; hands the page the
// fixture files through its file input (setInputFiles: file-backed Files, as
// a user's pick); calls window.POC.<fn>; writes one record:
//   docs/internal/bench/browser_node/<poc>-<fixture>-<date>[-<label>].json
// with the schema {poc, date, commit, machine, browser, arm, state, fixture,
// metrics, checks, thresholds, verdict, notes}. bench/verdict.py fills the
// verdict from thresholds.json.
import { execFileSync, execSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { launchChrome, POC_ROOT, REPO, startNode, startStatic, stopAll } from "./procs.mjs";
import { HANDLERS } from "./pocs.mjs";

function parseArgs(argv) {
    const out = { headed: false, out: path.join(REPO, "docs", "internal", "bench", "browser_node"), label: "" };
    for (let i = 0; i < argv.length; i += 1) {
        const a = argv[i];
        const next = () => argv[++i];
        if (a === "--poc") out.poc = next();
        else if (a === "--fixture") out.fixture = next();
        else if (a === "--headed") out.headed = true;
        else if (a === "--out") out.out = next();
        else if (a === "--label") out.label = next();
        else if (a === "--note") out.note = next();
        else if (a === "--params") out.params = JSON.parse(next());
        else if (a === "--browser") out.browser = next();
    }
    if (!out.poc) throw new Error("--poc is required");
    return out;
}

function loadFixtures() {
    const file = process.env.POC_FIXTURES || path.join(POC_ROOT, "bench", "fixtures.json");
    return JSON.parse(readFileSync(file, "utf8"));
}

/** Size, mtime and name of every fixture file: reader coverage is per file. */
export function fingerprint(fixture) {
    const out = {};
    for (const [key, value] of Object.entries(fixture)) {
        if (typeof value !== "string" || !existsSync(value)) continue;
        const s = statSync(value);
        out[key] = { name: path.basename(value), bytes: s.size, mtime: s.mtime.toISOString() };
    }
    return out;
}

function commit() {
    try {
        const sha = execSync("git rev-parse --short HEAD", { cwd: REPO }).toString().trim();
        const dirty = execSync("git status --porcelain -- tools/poc_browser_node", { cwd: REPO }).toString().trim();
        return dirty ? `${sha}-dirty` : sha;
    } catch (e) {
        return "unknown";
    }
}

/** Whole-machine CPU %, three one-second samples (Windows); what else was running. */
function cpuSample() {
    if (process.platform !== "win32") return null;
    try {
        const counter = String.raw`\Processor(_Total)\% Processor Time`;
        const script = `(Get-Counter '${counter}' -SampleInterval 1 -MaxSamples 3).CounterSamples | ForEach-Object { [math]::Round($_.CookedValue,1) }`;
        return execFileSync("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
                            ["-NoProfile", "-Command", script], { timeout: 20000 })
            .toString().trim().split(/\s+/).map(Number);
    } catch (e) {
        return null;
    }
}

function machine() {
    return {
        cpuPercentBefore: cpuSample(),
        platform: `${os.platform()} ${os.release()}`,
        cpu: os.cpus()[0]?.model, cores: os.cpus().length,
        memoryGB: Math.round(os.totalmem() / 2 ** 30),
        load: os.loadavg(),
        hostname: os.hostname(),
    };
}

async function main() {
    const args = parseArgs(process.argv.slice(2));
    const handler = HANDLERS[args.poc];
    if (!handler) throw new Error(`unknown POC ${args.poc}; have ${Object.keys(HANDLERS).join(", ")}`);
    const fixtures = loadFixtures();
    const fixture = fixtures[args.fixture] || {};
    const ctx = {
        args, fixture, fixtureName: args.fixture, startStatic, startNode,
        // --browser firefox|webkit runs a POC in Playwright's build of that engine.
        launchChrome: (o = {}) => launchChrome({ engine: args.browser || "chromium", ...o }),
        log: (...m) => console.log(`[${args.poc}]`, ...m),
    };
    // The local date: a run that crosses midnight UTC is still that evening's run.
    const now = new Date();
    const date = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
    let result;
    try {
        result = await handler(ctx);
    } finally {
        stopAll();
    }
    const record = {
        poc: args.poc,
        date,
        commit: commit(),
        machine: machine(),
        browser: result.browser || null,
        arm: result.arm || null,
        state: result.state || null,
        fixture: { name: args.fixture, files: fingerprint(fixture), ...(result.fixtureInfo || {}) },
        metrics: result.metrics || {},
        checks: result.checks || {},
        thresholds: null,
        verdict: null,
        notes: [args.note, ...(result.notes || [])].filter(Boolean),
        logs: (result.logs || []).slice(-60),
    };
    mkdirSync(args.out, { recursive: true });
    const name = `${args.poc}-${args.fixture || "none"}-${date}${args.label ? "-" + args.label : ""}.json`;
    const file = path.join(args.out, name);
    writeFileSync(file, JSON.stringify(record, null, 1) + "\n");
    console.log(`wrote ${file}`);
}

main().catch((error) => {
    console.error(error);
    stopAll();
    process.exit(1);
});
