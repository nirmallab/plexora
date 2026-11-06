// The processes a POC run needs: the static origin, Plexora data nodes, and
// Chrome on the real GPU. Everything started here is stopped by stopAll().
import { execFileSync, spawn, spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { randomBytes } from "node:crypto";
import { createServer } from "node:net";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const POC_ROOT = path.resolve(HERE, "..");
export const REPO = path.resolve(POC_ROOT, "..", "..");
export const PYTHON = process.env.PLEXORA_PYTHON || "C:/Users/aj/.conda/envs/plexora/python.exe";
export const CHROME = process.env.PLEXORA_BENCH_CHROME
    || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const started = [];

export async function freePort() {
    return new Promise((resolve, reject) => {
        const server = createServer();
        server.listen(0, "127.0.0.1", () => {
            const { port } = server.address();
            server.close(() => resolve(port));
        });
        server.on("error", reject);
    });
}

function kill(proc) {
    if (!proc || proc.exitCode !== null) return;
    if (process.platform === "win32") spawnSync("taskkill", ["/PID", String(proc.pid), "/T", "/F"], { stdio: "ignore" });
    else proc.kill();
}

/** serve_static.py; resolves {port, url, proc} once it prints its port. */
export async function startStatic({ coopCoep = null, rnode = false, port = 0, tls = null, pyodide = null,
                                    throttle = null } = {}) {
    // A fixed port when a test needs the SAME origin twice (OPFS and
    // IndexedDB are per origin, and the port is part of the origin).
    const args = [path.join(POC_ROOT, "serve_static.py"), "--port", String(port)];
    if (coopCoep) args.push("--coop-coep", coopCoep);
    if (rnode) args.push("--rnode");
    if (tls) args.push("--tls-cert", tls.cert, "--tls-key", tls.key);
    // P9: the Pyodide release and wheels (bench/p9_dist.py), and a shared link.
    if (pyodide) args.push("--pyodide", pyodide);
    if (throttle) args.push("--throttle", throttle);
    const proc = spawn(PYTHON, ["-I", ...args], { stdio: ["ignore", "pipe", "inherit"] });
    started.push(proc);
    const line = await new Promise((resolve, reject) => {
        let buffer = "";
        proc.stdout.on("data", (chunk) => {
            buffer += chunk;
            const nl = buffer.indexOf("\n");
            if (nl >= 0) resolve(buffer.slice(0, nl));
        });
        proc.on("exit", (code) => reject(new Error(`serve_static exited ${code}`)));
    });
    const listening = JSON.parse(line).port;
    return { port: listening, origin: `${tls ? "https" : "http"}://127.0.0.1:${listening}`, proc };
}

/**
 * `python -m plexora node serve`, as tests/node_harness.py starts one: its own
 * data root, a hex token, waited on until /health.
 */
export async function startNode({ serve, allowOrigins = [], token = null, port = null, env = {},
                                  dynamic = false, inlineWindows = false } = {}) {
    port ||= await freePort();
    token ||= randomBytes(8).toString("hex");
    const root = mkdtempSync(path.join(tmpdir(), "plexora-poc-node-"));
    const args = ["-m", "plexora", "node", "serve", "--port", String(port), "--token", token,
                  "--host", "127.0.0.1", "--exit-on-stdin-close"];
    for (const s of serve) args.push("--serve", s);
    for (const o of allowOrigins) args.push("--allow-origin", o);
    if (dynamic) args.push("--dynamic");
    const proc = spawn(PYTHON, args, {
        cwd: REPO,
        // Windows are scanned in the background, as in production, unless a
        // test needs the exact window on the first answer (node_harness does).
        env: { ...process.env, PLEXORA_DATA_PATH: root,
               ...(inlineWindows ? { PLEXORA_WINDOW_SCANS_INLINE: "1" } : {}), ...env },
        stdio: ["pipe", "ignore", "ignore"],
    });
    started.push(proc);
    const endpoint = `http://127.0.0.1:${port}`;
    const t0 = Date.now();
    for (;;) {
        if (proc.exitCode !== null) throw new Error(`node exited ${proc.exitCode}`);
        try {
            const r = await fetch(`${endpoint}/node/v1/health`, { headers: { "X-Plexora-Node-Token": token } });
            if (r.ok) break;
        } catch (e) { /* not up */ }
        if (Date.now() - t0 > 120000) throw new Error("node never became ready");
        await sleep(200);
    }
    return { endpoint, token, port, proc, root, readyMs: Date.now() - t0,
             stop: () => { kill(proc); try { rmSync(root, { recursive: true, force: true }); } catch (e) { /* busy */ } } };
}

/**
 * Chrome on the GPU via playwright-core; a fresh profile unless one is given.
 * `engine` "firefox" or "webkit" launches Playwright's own build of that
 * engine instead (P8: functional checks only; WebKit on Windows is the engine
 * Safari uses, not Safari), with none of Chrome's flags.
 */
export async function launchChrome({ headed = false, profile = null, unthrottled = true, extraArgs = [],
                                     engine = "chromium" } = {}) {
    const pw = await import("playwright-core");
    const args = engine !== "chromium" ? [] : [
        "--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist",
        "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows", "--enable-precise-memory-info",
        ...(unthrottled ? ["--disable-gpu-vsync", "--disable-frame-rate-limit"] : []),
        ...extraArgs,
    ];
    const userDataDir = profile || mkdtempSync(path.join(tmpdir(), `plx-poc-${engine}-`));
    const context = await pw[engine].launchPersistentContext(userDataDir, {
        ...(engine === "chromium" ? { executablePath: CHROME, args } : {}),
        headless: !headed,
        viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1,
    });
    const page = context.pages()[0] || await context.newPage();
    const logs = [];
    page.on("console", (m) => logs.push(`[${m.type()}] ${m.text()}`.slice(0, 500)));
    page.on("pageerror", (e) => logs.push(`[pageerror] ${String(e).slice(0, 500)}`));
    return {
        context, page, logs, args, userDataDir,
        close: async () => {
            await context.close();
            if (!profile) { try { rmSync(userDataDir, { recursive: true, force: true }); } catch (e) { /* busy */ } }
        },
    };
}

/**
 * Peak working set of each Chrome process of one profile (Windows), by kind
 * (renderer, gpu-process, utility, browser): the renderer's peak is the page,
 * its workers and their wasm heaps together -- what a tab costs at its worst.
 */
export function peakMemory(userDataDir) {
    if (process.platform !== "win32") return null;
    const needle = path.basename(userDataDir).replace(/'/g, "''");
    const script = [
        `$procs = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object { $_.CommandLine -like '*${needle}*' }`,
        "$out = foreach ($p in $procs) { $g = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue;",
        "  if ($g) { $kind = if ($p.CommandLine -match '--type=([a-z-]+)') { $matches[1] } else { 'browser' };",
        "    [pscustomobject]@{ kind = $kind; pid = $p.ProcessId; peak = $g.PeakWorkingSet64; private = $g.PrivateMemorySize64 } } }",
        "$out | ConvertTo-Json -Compress",
    ].join(String.fromCharCode(10));
    try {
        const text = execFileSync("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
                                  ["-NoProfile", "-Command", script], { timeout: 30000 }).toString().trim();
        if (!text) return null;
        const rows = [].concat(JSON.parse(text));
        const byKind = {};
        for (const r of rows) byKind[r.kind] = Math.max(byKind[r.kind] || 0, r.peak);
        return { byKind, processes: rows.length };
    } catch (e) {
        return { error: String(e).slice(0, 200) };
    }
}

export function stopAll() {
    for (const proc of started) kill(proc);
}
process.on("exit", stopAll);
process.on("SIGINT", () => { stopAll(); process.exit(130); });
