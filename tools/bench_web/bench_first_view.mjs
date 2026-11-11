// Open an image in Plexora the way a person does, in a brand-new Chrome
// profile (no cache, no history, no site data), with a visible window:
//   website: load the site, wait for the home page, drag the file onto it (a
//            browser-level drop, so the page gets a real file handle), answer
//            the first-use questions if they come
//   local:   load the local server's page, paste the path, press Load
// then reach for the image at once: zoom in, three more channels on, zoom to
// full resolution, pan. Times every step from navigation, takes a screenshot
// at each, and samples how lit the viewer is (from screenshots). Records the
// first view's own marks (services/firstView.js `__plexoraFirstView`).
// docs/internal/bench/web/WEBSITE_FIRST_VIEW_2026-11-10.md has the results.
//
//   PLEXORA_PLAYWRIGHT_MODULES=<node_modules with playwright-core> \
//   node tools/bench_web/bench_first_view.mjs --mode web|local --file PATH [--url URL]
//        [--label L] [--out DIR] [--limit S] [--headless] [--no-interact]
import path from "node:path";
import { pathToFileURL } from "node:url";
import { mkdtempSync, mkdirSync, writeFileSync, appendFileSync } from "node:fs";
import { tmpdir } from "node:os";

const args = process.argv.slice(2);
const opt = (n, f) => (args.includes(n) ? args[args.indexOf(n) + 1] : f);
const mode = opt("--mode", "web");
const file = opt("--file");
const url = opt("--url", "https://viewer.plexoraapp.com/");
const label = opt("--label", `${mode}-${path.basename(file)}`);
const outDir = opt("--out", "runs");
const limitMs = Number(opt("--limit", "900")) * 1000;
const headless = args.includes("--headless");
mkdirSync(path.join(outDir, label), { recursive: true });
const shot = (name) => path.join(outDir, label, `${name}.png`);

const { chromium } = await import(pathToFileURL(path.join(process.env.PLEXORA_PLAYWRIGHT_MODULES, "playwright-core", "index.mjs")).href);
const profile = mkdtempSync(path.join(process.env.PLEXORA_BENCH_WORK || tmpdir(), "pxfirst-"));
const ctx = await chromium.launchPersistentContext(profile, {
    executablePath: process.env.PLEXORA_CHROME || "C:/Program Files/Google/Chrome/Application/chrome.exe",
    headless, viewport: { width: 1440, height: 900 },
    args: ["--no-first-run", "--no-default-browser-check", "--window-size=1460,1000"],
});
const page = ctx.pages()[0] || await ctx.newPage();
const T0 = Date.now();
const ms = () => Date.now() - T0;
const steps = {};
const log = [];
const say = (m) => { const line = `${(ms() / 1000).toFixed(1)}s ${m}`; log.push(line); process.stderr.write(line + "\n"); };
const mark = async (name, take = true) => { steps[name] = ms(); say(name); if (take) await page.screenshot({ path: shot(`${String(Object.keys(steps).length).padStart(2, "0")}_${name}`) }).catch(() => null); };
page.on("pageerror", (e) => say(`pageerror ${String(e).slice(0, 160)}`));
// Every app request and frame navigation, on the run's clock.
const reqs = [];
const started = new Map();
const INTEREST = /import\/|check_path|check_file|config|volume\/|stats|gmm|view_state|init_database|get_channel_names|node_spec|\/generated\/|tile|display_contrast|datasets|get_load_status/;
page.on("request", (r) => { if (INTEREST.test(r.url())) started.set(r, ms()); });
page.on("requestfinished", (r) => { if (started.has(r)) reqs.push({ s: started.get(r), e: ms(), u: r.url().replace(/^https?:\/\/[^/]+/, "").replace(/token=[^&]+/, "").slice(0, 90) }); });
page.on("requestfailed", (r) => { if (started.has(r)) reqs.push({ s: started.get(r), e: ms(), u: "FAILED " + r.url().replace(/^https?:\/\/[^/]+/, "").slice(0, 80) }); });
page.on("framenavigated", (fr) => { if (fr !== page.mainFrame()) reqs.push({ s: ms(), e: ms(), u: "NAV " + fr.url().replace(/^https?:\/\/[^/]+/, "").replace(/token=[^&]+/, "").slice(0, 80) }); });
page.on("console", (m) => { if (m.type() === "error" && !/cloudflareinsights|Content Security/.test(m.text())) say(`console.error ${m.text().slice(0, 160)}`); });

// How lit the viewer area is, from a screenshot decoded in the page.
async function lit() {
    try {
        const png = await page.screenshot({ clip: { x: 420, y: 80, width: 980, height: 780 } });
        return await page.evaluate(async (b64) => {
            const blob = await (await fetch(`data:image/png;base64,${b64}`)).blob();
            const bmp = await createImageBitmap(blob);
            const c = new OffscreenCanvas(bmp.width / 4, bmp.height / 4);
            const g = c.getContext("2d"); g.drawImage(bmp, 0, 0, c.width, c.height);
            const d = g.getImageData(0, 0, c.width, c.height).data;
            let on = 0; for (let i = 0; i < d.length; i += 4) if (d[i] + d[i + 1] + d[i + 2] > 120) on++;
            return on / (d.length / 4);
        }, png.toString("base64"));
    } catch (e) { return -1; }
}

const frame = async () => {
    // The website's app runs in a frame; the local server's page is the app.
    const el = mode === "web" ? await page.waitForSelector("#plexora-app", { timeout: 5000 }).catch(() => null)
        : await page.$("#plexora-app").catch(() => null);
    return el ? el.contentFrame() : page.mainFrame();
};

await page.goto(url);
await mark("navigated", false);
// Home ready: the field is there and the loader gone.
let f = null;
for (;;) {
    f = await frame();
    const ready = await f.evaluate(() => Boolean(document.getElementById("quick_view_go")) && !document.getElementById("plexora-boot")
        && getComputedStyle(document.getElementById("quick_view_card")).visibility !== "hidden").catch(() => false);
    const veiled = await page.evaluate(() => document.getElementById("plexora-app")?.classList.contains("is-veiled") ?? false).catch(() => false);
    if (ready && !veiled) break;
    if (ms() > 180000) throw new Error("home never came");
    await page.waitForTimeout(100);
}
await mark("home_visible");
// "Starting" chip: the website gates the page's requests until Python is up.
if (mode === "web") {
    for (;;) {
        const s = await f.evaluate(() => document.querySelector("#app_status .app-status-label")?.textContent || "").catch(() => "");
        if (!/Starting/i.test(s)) break;
        if (!steps.starting_chip_shown) await mark("starting_chip_shown");
        await page.waitForTimeout(100);
    }
    await mark("python_ready_chip_gone");
    steps.phases = await page.evaluate(async () => { const r = await window.PlexoraShellStarted; const t0 = performance.timeOrigin;
        return Object.fromEntries(Object.entries(r.phases).map(([k, v]) => [k, Math.round(v - t0)])); }).catch(() => null);
}
await page.waitForTimeout(1500); // a person reads the page

// Give the image.
if (mode === "web") {
    const box = await (await f.$("#quick_view_landing")).boundingBox();
    const x = box.x + box.width / 2, y = box.y + box.height / 2;
    const cdp = await ctx.newCDPSession(page);
    const data = { items: [], files: [file], dragOperationsMask: 1 };
    await cdp.send("Input.dispatchDragEvent", { type: "dragEnter", x, y, data });
    await cdp.send("Input.dispatchDragEvent", { type: "dragOver", x, y, data });
    await page.waitForTimeout(400);
    await cdp.send("Input.dispatchDragEvent", { type: "drop", x, y, data });
    await mark("dropped");
} else {
    await f.click("#quick_view_path_input");
    await f.fill("#quick_view_path_input", file);
    await page.waitForTimeout(300);
    await f.click("#quick_view_go");
    await mark("load_pressed");
}

// Then whatever comes: the storage question, the import dialog, the viewer.
let lastUrl = "";
let lastLit = 0;
let seen = new Set();
const deadline = T0 + limitMs;
while (Date.now() < deadline) {
    f = await frame();
    const state = await f.evaluate(() => {
        const vis = (el) => el && el.getClientRects().length && getComputedStyle(el).visibility !== "hidden";
        const onb = document.querySelector("dialog.plx-onb[open]");
        const dialogs = [...document.querySelectorAll("dialog[open], .modal.show, [role=dialog]")].filter(vis)
            .map((d) => (d.id || d.className || "dialog").toString().slice(0, 40) + ": " + (d.innerText || "").replace(/\s+/g, " ").slice(0, 140));
        const v = window.__plexora?.seaDragonViewer?.viewer;
        let items = 0, loaded = 0, drawnTiles = 0;
        if (v && v.world) { items = v.world.getItemCount(); for (let i = 0; i < items; i++) { const it = v.world.getItemAt(i); if (it.getFullyLoaded()) loaded++; drawnTiles += (it.lastDrawn || []).length; } }
        const busy = document.getElementById("quick_view_go_busy");
        const status = document.getElementById("quick_view_status")?.innerText || "";
        const chip = document.querySelector("#app_status .app-status-label")?.textContent || "";
        return { url: location.pathname, onb: Boolean(onb), dialogs, items, loaded, drawnTiles, busy: vis(busy) ? 1 : 0, status: status.slice(0, 120), chip };
    }).catch((e) => ({ error: String(e).slice(0, 80) }));
    if (state.error) { await page.waitForTimeout(200); continue; }
    const key = (k) => { if (!seen.has(k)) { seen.add(k); return true; } return false; };
    if (state.url !== lastUrl) { lastUrl = state.url; await mark(`url ${state.url}`.replace(/[^\w ]/g, "_")); }
    if (state.status && key("status:" + state.status)) say(`status: ${state.status}`);
    if (state.chip && key("chip:" + state.chip)) say(`chip: ${state.chip}`);
    for (const d of state.dialogs) if (key("dialog:" + d.slice(0, 60))) { say(`dialog: ${d}`); await page.screenshot({ path: shot(`dialog_${seen.size}`) }); }
    if (state.onb && !steps.onboarding_asked) {
        await mark("onboarding_asked");
        // A person: "I'm new", "Use browser storage", "Continue to my image".
        const live = (s) => `dialog.plx-onb .plx-onb-screen:not(.is-leaving) ${s}`;
        for (const sel of ['[data-choice="new"]', '[data-choice="browser"]']) {
            const node = await f.waitForSelector(live(sel), { state: "visible", timeout: 30000 }).catch(() => null);
            if (node) { await page.waitForTimeout(600); await node.click(); }
        }
        const go = await f.waitForSelector(`dialog.plx-onb .plx-onb-screen:not(.is-leaving) button:has-text("Continue")`,
                                           { state: "visible", timeout: 30000 }).catch(() => null);
        if (go) { await page.waitForTimeout(600); await go.click(); }
        await f.waitForSelector("dialog.plx-onb[open]", { state: "detached", timeout: 30000 }).catch(() => null);
        await mark("onboarding_answered");
        steps.onboarding_ms = steps.onboarding_answered - steps.onboarding_asked;
    }
    // An import dialog asking to confirm: press its primary button, as a person would.
    if (!state.onb && state.dialogs.some((d) => /import/i.test(d)) && !steps.import_confirmed) {
        const btn = await f.$("dialog[open] .btn-primary:not([disabled]), dialog[open] button.is-primary:not([disabled])");
        if (btn) { await page.waitForTimeout(800); await btn.click().catch(() => null); await mark("import_confirmed"); }
    }
    if (!steps.preview_shown) {
        const fv = await page.evaluate(() => window.__plexoraFirstView || null).catch(() => null);
        if (fv && fv.shownMs != null) { await mark("preview_shown"); steps.preview = fv; }
    }
    if (state.items > 0 && !steps.viewer_item) await mark("viewer_item");
    if (state.drawnTiles > 0 && !steps.first_tile_drawn) await mark("first_tile_drawn");
    if (state.items > 0) {
        const l = await lit();
        if (l > 0.01 && !steps.first_channel_visible) { await mark("first_channel_visible"); }
        lastLit = l;
        if (state.loaded === state.items && state.items > 0 && !steps.view_fully_loaded) await mark("view_fully_loaded");
        if (steps.first_channel_visible && steps.view_fully_loaded) break;
    }
    await page.waitForTimeout(250);
}
// How the first view ended, or why it never showed (`failed`, `endedBy`).
steps.firstView = await page.evaluate(() => window.__plexoraFirstView || null).catch(() => null);
// The person reaches for the image at once: zoom in, three more channels on,
// then zoom to full resolution, then pan -- each timed until the viewer has
// stopped moving and every channel shown is fully drawn.
const settledIn = async (label, limit = 180000) => {
    const f2 = await frame();
    const t0 = ms();
    await page.waitForTimeout(250);
    // Polled with `evaluate` (the site's CSP refuses waitForFunction's eval).
    const drawn = () => f2.evaluate(() => {
        const v = window.__plexora?.seaDragonViewer?.viewer;
        if (!v || !v.world || !v.world.getItemCount()) return false;
        if (typeof v.isAnimating === "function" && v.isAnimating()) return false;
        for (let i = 0; i < v.world.getItemCount(); i += 1) {
            const it = v.world.getItemAt(i);
            if (it.getOpacity() > 0 && !it.getFullyLoaded()) return false;
        }
        return true;
    }).catch(() => false);
    let ok = false;
    for (const end = Date.now() + limit; Date.now() < end;) {
        if (await drawn()) { ok = true; break; }
        await page.waitForTimeout(100);
    }
    steps[label] = ok ? ms() - t0 : `>${limit}`;
    say(`${label} ${steps[label]}`);
    await page.screenshot({ path: shot(label) }).catch(() => null);
};
if (steps.view_fully_loaded && !args.includes("--no-interact")) {
    const f2 = await frame();
    const box = await f2.evaluate(() => {
        const el = document.querySelector(".openseadragon-container");
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    }).catch(() => null);
    const off = mode === "web" ? await page.evaluate(() => {
        const r = document.getElementById("plexora-app")?.getBoundingClientRect();
        return r ? { x: r.left, y: r.top } : { x: 0, y: 0 };
    }) : { x: 0, y: 0 };
    if (box) {
        const cx = box.x + off.x + 40, cy = box.y + off.y + 30;
        await page.mouse.move(cx, cy);
        for (let i = 0; i < 4; i += 1) { await page.mouse.wheel(0, -500); await page.waitForTimeout(60); }
        await settledIn("zoom_in_ms");
        const eyes = await f2.$$(".channel-slot-eye");
        let turned = 0;
        for (const e of eyes) {
            const on = await e.getAttribute("aria-pressed");
            if (on === "false" && turned < 3) { await e.click().catch(() => null); turned += 1; await page.waitForTimeout(120); }
        }
        steps.channels_turned = turned;
        await settledIn("channels_on_ms");
        await page.mouse.move(cx, cy);
        for (let i = 0; i < 8; i += 1) { await page.mouse.wheel(0, -600); await page.waitForTimeout(60); }
        await settledIn("zoom_full_ms");
        await page.mouse.move(cx - 200, cy);
        await page.mouse.down();
        await page.mouse.move(cx + 200, cy + 60, { steps: 8 });
        await page.mouse.up();
        await settledIn("pan_ms");
    }
}
// What Python was asked meanwhile (website: the page's own record).
const recent = mode === "web" ? await page.evaluate(() => window.PlexoraPython?.recent?.() || []).catch(() => []) : [];
const pySteps = mode === "web" ? await page.evaluate(() => window.PlexoraPython?.steps?.() || []).catch(() => []) : [];
f = await frame();
const resources = await f.evaluate(() => performance.getEntriesByType("resource")
    .filter((e) => !/\.(js|css|svg|woff2?|png|json)(\?|$)/.test(e.name) || /import|config|channel|tile|stats/.test(e.name))
    .map((e) => ({ n: e.name.replace(location.origin, "").slice(0, 90), s: Math.round(e.startTime), d: Math.round(e.duration) }))
    .filter((e) => e.d > 100).slice(-60)).catch(() => []);
await page.screenshot({ path: shot("99_end") });
const out = { label, mode, file, url, steps, lastLit: +lastLit.toFixed(3), totalMs: ms(), timedOut: Date.now() >= deadline,
              slowPython: recent.filter((r) => r.ms > 300).map((r) => ({ path: r.path, ms: r.ms, status: r.status })),
              pySteps: pySteps.filter((s) => s.ms > 300), slowResources: resources, requests: reqs.sort((a, b) => a.s - b.s), log };
writeFileSync(path.join(outDir, label, "result.json"), JSON.stringify(out, null, 1));
appendFileSync(path.join(outDir, "results.jsonl"), JSON.stringify({ label, steps, totalMs: out.totalMs, timedOut: out.timedOut }) + "\n");
console.log(JSON.stringify({ label, steps, timedOut: out.timedOut, slowPython: out.slowPython.slice(0, 25) }));
await ctx.close();
