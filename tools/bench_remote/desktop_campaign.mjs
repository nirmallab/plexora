// desktop_campaign.mjs - the remote benchmark's tour in the Plexora desktop
// app's own window, Browser rendering off against on (plan Phase 6 step 1).
//
//   node desktop_campaign.mjs --exe <plexora-desktop.exe> --home C:\plexora-bench-desk
//        --dataset lsp11385 --order off,on,on,off --out campaign.json
//        --params '{"passes": ["C1", "C2"], "channels": [0, 1, 2],
//                   "waypoints": {"a": [x, y], "b": [x, y], "c": [x, y]}}'
//        [--python C:/Users/aj/.conda/envs/plexora/python.exe] [--cwd C:\pxdesk]
//
// One block per entry of --order, each a fresh app: the shell is started with
// the setting written into a settings file of the bench home's own (platformdirs'
// WIN_PD_OVERRIDE_LOCAL_APPDATA, never the user's), a WebView2 profile of its own
// (WEBVIEW2_USER_DATA_FOLDER) and remote debugging on --port, a developer
// interpreter (PLEXORA_DESKTOP_PYTHON) and the bench data root; tour.mjs then
// drives its window over CDP (--cdp), the renderer's peak working set is read
// (Win32_Process PeakWorkingSetSize of the profile's renderer processes), the
// machine's CPU load is sampled before the block, and the app is ended (its
// process tree). C1 empties the WebView's HTTP cache; the OS file cache is not
// dropped (no admin here): every block reads the image with Windows' cache as
// the blocks before it left it, which the ABBA order shares out.
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { sleep } from "../bench3d/drive.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const POWERSHELL = `${process.env.WINDIR || "C:\\Windows"}\\System32\\WindowsPowerShell\\v1.0\\powershell.exe`;

function parseArgs(argv) {
    const out = { order: ["off", "on", "on", "off"], port: 9333, params: {},
                  python: "C:/Users/aj/.conda/envs/plexora/python.exe", cwd: "C:\\pxdesk" };
    for (let i = 0; i < argv.length; i += 1) {
        const arg = argv[i];
        const next = () => argv[++i];
        if (arg === "--exe") out.exe = next();
        else if (arg === "--home") out.home = next();
        else if (arg === "--dataset") out.dataset = next();
        else if (arg === "--order") out.order = next().split(",");
        else if (arg === "--out") out.out = next();
        else if (arg === "--params") out.params = JSON.parse(next());
        else if (arg === "--port") out.port = Number(next());
        else if (arg === "--python") out.python = next();
        else if (arg === "--cwd") out.cwd = next();
    }
    return out;
}

function ps(command) {
    const run = spawnSync(POWERSHELL, ["-NoProfile", "-NonInteractive", "-Command", command],
                          { encoding: "utf8" });
    return (run.stdout || "").trim();
}

function cpuLoad() {
    const text = ps("(Get-Counter '\\Processor(_Total)\\% Processor Time' -SampleInterval 1 -MaxSamples 3)"
                    + ".CounterSamples | ForEach-Object { [math]::Round($_.CookedValue, 1) }");
    return text.split(/\s+/).map(Number).filter(Number.isFinite);
}

/** Peak working sets (GB) of this profile's WebView2 processes, by type. */
function webviewPeaks(profile) {
    const needle = profile.replace(/'/g, "''");
    const text = ps("Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | "
        + `Where-Object { $_.CommandLine -like '*${needle}*' } | `
        + "ForEach-Object { $t = if ($_.CommandLine -match '--type=([a-z-]+)') { $Matches[1] } else { 'browser' }; "
        + "'{0} {1} {2}' -f $t, $_.PeakWorkingSetSize, $_.WorkingSetSize }");
    const peaks = {};
    for (const line of text.split(/\r?\n/).filter(Boolean)) {
        const [type, peakKb, nowBytes] = line.trim().split(/\s+/);
        const peakGb = Number(peakKb) / 1024 / 1024;
        const entry = (peaks[type] ||= { n: 0, peakGb: 0, nowGb: 0 });
        entry.n += 1;
        entry.peakGb = Math.max(entry.peakGb, +peakGb.toFixed(3));
        entry.nowGb = Math.max(entry.nowGb, +(Number(nowBytes) / 1024 ** 3).toFixed(3));
    }
    return peaks;
}

function writeSetting(home, value) {
    const file = path.join(home, "appdata", "plexora", "settings.json");
    mkdirSync(path.dirname(file), { recursive: true });
    let data = {};
    try { data = JSON.parse(readFileSync(file, "utf8")); } catch (error) { data = {}; }
    data.browser_rendering = value === "on" ? "auto" : "off";
    writeFileSync(file, JSON.stringify(data, null, 1));
}

async function waitForPage(port, timeoutMs = 180000) {
    const until = Date.now() + timeoutMs;
    while (Date.now() < until) {
        try {
            const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
            const page = list.find((t) => t.type === "page" && /^http:\/\/127\.0\.0\.1:\d+\//.test(t.url));
            if (page) return new URL(page.url).origin;
        } catch (error) { /* not up yet */ }
        await sleep(500);
    }
    throw new Error("the app's window did not open its page");
}

async function portFree(port, timeoutMs = 30000) {
    const until = Date.now() + timeoutMs;
    while (Date.now() < until) {
        try {
            await fetch(`http://127.0.0.1:${port}/json`);
        } catch (error) {
            return true;
        }
        await sleep(500);
    }
    return false;
}

function runTour(opts, origin, out) {
    return new Promise((resolve) => {
        const child = spawn(process.execPath, [path.join(HERE, "tour.mjs"), "--cdp", `http://127.0.0.1:${opts.port}`,
                                               "--base", origin, "--dataset", opts.dataset,
                                               "--params", JSON.stringify(opts.params), "--out", out],
                            { stdio: ["ignore", "inherit", "inherit"] });
        child.on("exit", (code) => resolve(code));
    });
}

async function block(opts, index, arm) {
    const home = opts.home;
    const profile = path.join(home, "webview");
    writeSetting(home, arm);
    const load = cpuLoad();
    const env = {
        ...process.env,
        PLEXORA_DESKTOP_PYTHON: opts.python,
        PLEXORA_DESKTOP_CWD: opts.cwd,
        PLEXORA_DATA_PATH: path.join(home, "data"),
        PLEXORA_TELEMETRY: "off",
        WIN_PD_OVERRIDE_LOCAL_APPDATA: path.join(home, "appdata"),
        WEBVIEW2_USER_DATA_FOLDER: profile,
        WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${opts.port}`,
    };
    const shell = spawn(opts.exe, [], { env, cwd: opts.cwd, detached: true, stdio: "ignore" });
    shell.unref();
    process.stderr.write(`block ${index} (${arm}): shell ${shell.pid}, cpu ${load.join("/")}%\n`);
    let tour = null;
    let peaks = null;
    let error = null;
    try {
        const origin = await waitForPage(opts.port);
        // The server answers before the page has drawn its first view.
        await sleep(3000);
        const out = path.join(path.dirname(opts.out), `tour-${index}-${arm}.json`);
        const code = await runTour(opts, origin, out);
        tour = existsSync(out) ? JSON.parse(readFileSync(out, "utf8")) : null;
        if (code !== 0) error = `tour.mjs exited ${code}`;
        peaks = webviewPeaks(profile);
    } catch (caught) {
        error = String(caught?.stack || caught);
    } finally {
        spawnSync("taskkill", ["/PID", String(shell.pid), "/T", "/F"], { stdio: "ignore" });
        await portFree(opts.port);
        await sleep(2000);
    }
    return { index, arm, setting: arm === "on" ? "auto" : "off", cpuBefore: load, peaks, error, tour };
}

async function main() {
    const opts = parseArgs(process.argv.slice(2));
    if (!opts.exe || !opts.home || !opts.dataset || !opts.out) {
        throw new Error("--exe, --home, --dataset and --out are needed");
    }
    mkdirSync(path.dirname(opts.out), { recursive: true });
    const record = { dataset: opts.dataset, order: opts.order, params: opts.params,
                     started: new Date().toISOString(), blocks: [] };
    for (const [i, arm] of opts.order.entries()) {
        record.blocks.push(await block(opts, i, arm));
        writeFileSync(opts.out, JSON.stringify(record));
    }
    record.cpuAfter = cpuLoad();
    record.finished = new Date().toISOString();
    writeFileSync(opts.out, JSON.stringify(record));
    process.exit(record.blocks.some((b) => b.error) ? 2 : 0);
}

main().catch((error) => {
    console.error(error);
    process.exit(1);
});
