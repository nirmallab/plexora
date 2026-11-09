// Website startup: cold (fresh profile), warm (same profile, new launch) and
// reload (same tab), against a site URL. Prints one JSON line per run.
//   node bench_startup.mjs --url https://viewer.plexoraapp.com --profile DIR --runs 2
import { chromium, CHROME, PY, REPO, PYTHONPATH } from "./benchlib.mjs";
import { mkdtempSync, rmSync, existsSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const args = process.argv.slice(2);
const opt = (n, f) => (args.includes(n) ? args[args.indexOf(n) + 1] : f);
const url = opt("--url", "https://viewer.plexoraapp.com/");
const runs = Number(opt("--runs", "2"));
const profileBase = opt("--profile", mkdtempSync(path.join(tmpdir(), "pxbench-")));

// Recorded inside the host page: Python's marks, the shell's started promise,
// and the frame's appearance, all on performance.now() of the host.
const INIT = () => {
    window.__bench = { marks: {}, started: null, frame: null, earlyHome: null, requests: 0 };
    const t = () => performance.now();
    const poll = setInterval(() => {
        const py = window.PlexoraPython;
        if (py && !window.__bench.hooked) {
            window.__bench.hooked = true;
            py.on("mark", (d) => { window.__bench.marks[d.mark] = d.at - performance.timeOrigin; });
        }
        if (window.PlexoraShellStarted && !window.__bench.startedHooked) {
            window.__bench.startedHooked = true;
            window.PlexoraShellStarted.then(() => { window.__bench.started = t(); }, () => { window.__bench.started = -1; });
        }
        const frame = document.getElementById("plexora-app");
        if (frame && window.__bench.frame === null) window.__bench.frame = t();
        if (window.__bench.started !== null && window.__bench.frame !== null && window.__bench.hooked) clearInterval(poll);
    }, 5);
};

async function measure(page, label, action) {
    const errors = [];
    const onErr = (e) => errors.push(String(e).slice(0, 200));
    page.on("pageerror", onErr);
    let bytes = 0; let count = 0;
    const onResp = async (r) => { count += 1; const l = r.headers()["content-length"]; if (l) bytes += Number(l); };
    page.on("response", onResp);
    const t0 = Date.now();
    await action();
    // The loader's line (first paint of the host).
    await page.waitForSelector("#plexora-boot-status", { timeout: 60000 }).catch(() => null);
    const loaderMs = Date.now() - t0;
    // The frame, and the home page inside it (early page or Python's).
    const frameEl = await page.waitForSelector("#plexora-app", { timeout: 120000 }).catch(() => null);
    const frameMs = Date.now() - t0;
    let homeMs = null; let homeInteractiveMs = null;
    if (frameEl) {
        const frame = await frameEl.contentFrame();
        await frame.waitForFunction(() => document.getElementById("quick_view_go") && !document.getElementById("plexora-boot"),
                                    null, { timeout: 120000, polling: 10 }).catch(() => null);
        homeMs = Date.now() - t0;
        // Unveiled: the frame's is-veiled class gone (the page faded in over the loader).
        await page.waitForFunction(() => { const f = document.getElementById("plexora-app"); return f && !f.classList.contains("is-veiled"); },
                                   null, { timeout: 120000, polling: 10 }).catch(() => null);
        homeInteractiveMs = Date.now() - t0;
    }
    // Python ready + shell started.
    await page.waitForFunction(() => window.__bench && window.__bench.started !== null, null, { timeout: 180000, polling: 20 }).catch(() => null);
    const startedMs = Date.now() - t0;
    const bench = await page.evaluate(() => window.__bench);
    const nav = await page.evaluate(() => { const e = performance.getEntriesByType("navigation")[0]; return e ? { dcl: e.domContentLoadedEventEnd, load: e.loadEventEnd, ttfb: e.responseStart } : null; });
    // Status chip in the frame says "Starting" while gated: when did it clear? (approximate by started)
    page.off("pageerror", onErr); page.off("response", onResp);
    const out = { label, url, loaderMs, frameMs, homeMs, homeInteractiveMs, startedMs,
                  hostNav: nav, marks: bench.marks, frameAt: bench.frame, startedAt: bench.started,
                  responses: count, bytesHeaderSum: bytes, errors: errors.slice(0, 5) };
    console.log(JSON.stringify(out));
    return out;
}

async function launch(profile) {
    return chromium.launchPersistentContext(profile, {
        executablePath: CHROME, headless: true,
        args: ["--no-first-run", "--no-default-browser-check", "--disable-background-timer-throttling", "--disable-renderer-backgrounding"],
    });
}

for (let i = 0; i < runs; i += 1) {
    const profile = path.join(profileBase, `p${i}`);
    if (existsSync(profile)) rmSync(profile, { recursive: true, force: true });
    mkdirSync(profile, { recursive: true });
    // cold
    let ctx = await launch(profile);
    let page = await ctx.newPage();
    await page.addInitScript(INIT);
    await page.setViewportSize({ width: 1440, height: 900 });
    await measure(page, `cold#${i}`, () => page.goto(url));
    // keep the first visit's dialogs out: answer "Keep in browser" if asked
    try {
        const frame = await (await page.$("#plexora-app")).contentFrame();
        await frame.waitForFunction(() => { const b = [...document.querySelectorAll(".plx-confirm .plx-button")].find((x) => ["Keep in browser", "Keep them", "Not now"].includes(x.textContent.trim())); if (b) { b.click(); return true; } return false; }, null, { timeout: 8000, polling: 200 }).catch(() => null);
    } catch {}
    await page.waitForTimeout(6000); // let the home page be kept for the next visit (keepHome waits 5 s)
    // reload (same tab)
    await measure(page, `reload#${i}`, () => page.reload());
    await page.waitForTimeout(1500);
    await ctx.close();
    // warm: same profile, new browser
    ctx = await launch(profile);
    page = await ctx.newPage();
    await page.addInitScript(INIT);
    await page.setViewportSize({ width: 1440, height: 900 });
    await measure(page, `warm#${i}`, () => page.goto(url));
    await ctx.close();
}
