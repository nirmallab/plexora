// The local Python webserver as the reference: process start -> "Serving"
// line, then a fresh Chrome's navigation -> the home page's button.
//   node bench_server_startup.mjs --data DIR --runs 2
import { chromium, CHROME, PY, REPO, PYTHONPATH } from "./benchlib.mjs";
import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const args = process.argv.slice(2);
const opt = (n, f) => (args.includes(n) ? args[args.indexOf(n) + 1] : f);
const runs = Number(opt("--runs", "2"));
const data = opt("--data", mkdtempSync(path.join(tmpdir(), "pxdata-")));
if (!existsSync(data)) mkdirSync(data, { recursive: true });

function startServer() {
    return new Promise((resolve, reject) => {
        const t0 = Date.now();
        const child = spawn(PY, ["-u", "-m", "plexora", "--no-browser", "--port", "0", "--data-dir", data],
                            { cwd: REPO, env: { ...process.env, PYTHONPATH, PLEXORA_DATA_PATH: data, PLEXORA_TELEMETRY: "off", PYTHONUNBUFFERED: "1" },
                              stdio: ["ignore", "pipe", "pipe"] });
        let buffer = "";
        const onData = (chunk) => {
            buffer += chunk;
            const m = buffer.match(/Serving Plexora at (\S+)/);
            if (m) resolve({ url: m[1], servingMs: Date.now() - t0, stop: () => child.kill("SIGTERM"), log: () => buffer });
        };
        child.stdout.on("data", onData);
        child.stderr.on("data", onData);
        child.on("exit", (code) => reject(new Error(`server exited ${code}: ${buffer.slice(-800)}`)));
        setTimeout(() => reject(new Error(`server did not announce: ${buffer.slice(-800)}`)), 120000);
    });
}

for (let i = 0; i < runs; i += 1) {
    const server = await startServer();
    const out = { label: `server#${i}`, servingMs: server.servingMs };
    const profile = mkdtempSync(path.join(tmpdir(), "pxbench-srv-"));
    const ctx = await chromium.launchPersistentContext(profile, { executablePath: CHROME, headless: true,
        args: ["--no-first-run", "--no-default-browser-check"] });
    const page = await ctx.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    // health first: the server is listening (the token swap happens on the first page)
    const t0 = Date.now();
    await page.goto(server.url);
    await page.waitForSelector("#quick_view_go", { timeout: 60000 });
    out.homeMs = Date.now() - t0;
    const nav = await page.evaluate(() => { const e = performance.getEntriesByType("navigation")[0]; return e ? { ttfb: e.responseStart, dcl: e.domContentLoadedEventEnd, load: e.loadEventEnd } : null; });
    out.hostNav = nav;
    // reload
    const t1 = Date.now();
    await page.reload();
    await page.waitForSelector("#quick_view_go", { timeout: 60000 });
    out.reloadMs = Date.now() - t1;
    // second tab (warm server, warm cache)
    const page2 = await ctx.newPage();
    const t2 = Date.now();
    await page2.goto(server.url.replace(/\?token=.*/, ""));
    await page2.waitForSelector("#quick_view_go", { timeout: 60000 }).catch(() => null);
    out.secondTabMs = Date.now() - t2;
    await ctx.close();
    server.stop();
    console.log(JSON.stringify(out));
    await new Promise((r) => setTimeout(r, 1500));
}
