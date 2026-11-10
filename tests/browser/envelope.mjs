// The website's notices of what a tab does well, drawn by the real page
// (client src/js/services/handoffNotice.js, in the site's host page; the toast
// in the app's frame): a long batch, magic select's model with no WebGPU (the
// browser's adapter hidden here), and slow tiles -- each with a short title,
// one line and every button on one row, once per project per tab, and
// "Continue here" or the x remembered. The tile watcher is a PerformanceObserver
// the host makes in the frame's realm: the frame's tile requests reach it.
//
//   node tests/browser/envelope.mjs [--headed]
// Prints JSON with `ok` and the checks.
import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { appFrame, launch, PYTHON, REPO, seedBrowserStore, startServer } from "./lib.mjs";

const headed = process.argv.includes("--headed");
const shot = process.argv.includes("--shot") ? process.argv[process.argv.indexOf("--shot") + 1] : null;
const out = { checks: [], errors: [] };
const check = (name, ok, detail = {}) => {
    out.checks.push({ name, ok: Boolean(ok), ...detail });
    console.error(`${ok ? "ok  " : "FAIL"} ${name}`);
};

const site = mkdtempSync(path.join(tmpdir(), "pxenv-site-"));
execFileSync(PYTHON, ["-m", "plexora.client.static_build", "--out", site],
             { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO }, stdio: ["ignore", "ignore", "inherit"] });
const server = await startServer(["--site", site]);
const origin = `http://127.0.0.1:${server.port}`;
const browser = await launch({ headed });

//: The toast on screen in the frame: its words, its buttons, and whether
//: they sit on one row inside the card.
const toast = (frame) => frame.evaluate(() => {
    const card = document.querySelector(".plx-toast");
    if (!card) return null;
    const buttons = [...card.querySelectorAll(".plx-toast-actions button")];
    const box = card.getBoundingClientRect();
    return {
        title: card.querySelector(".plx-toast-title")?.textContent.trim(),
        note: card.querySelector(".plx-toast-note")?.textContent.trim() || null,
        buttons: buttons.map((b) => b.textContent.trim()),
        oneRow: buttons.length > 0 && new Set(buttons.map((b) => Math.round(b.getBoundingClientRect().top))).size === 1,
        inside: buttons.every((b) => b.getBoundingClientRect().right <= box.right + 0.5),
        width: Math.round(box.width),
    };
});
const press = (frame, label) => frame.evaluate((label) => {
    const button = [...document.querySelectorAll(".plx-toast-actions button")].find((b) => b.textContent.trim() === label);
    if (button) button.click();
    return Boolean(button);
}, label);
const until = async (fn, ms = 8000) => {
    const end = Date.now() + ms;
    let value = await fn();
    while (!value && Date.now() < end) {
        await new Promise((r) => setTimeout(r, 100));
        value = await fn();
    }
    return value;
};

try {
    // No WebGPU adapter in this browser, as on a machine without one.
    await browser.context.addInitScript(() => {
        Object.defineProperty(Navigator.prototype, "gpu", { get: () => undefined, configurable: true });
    });
    const page = await browser.context.newPage();
    page.on("pageerror", (error) => out.errors.push(`pageerror: ${String(error).slice(0, 300)}`));
    await seedBrowserStore(browser.context, origin);
    await page.goto(`${origin}/`);
    const frame = await appFrame(page);
    await page.waitForTimeout(300);
    await frame.evaluate(() => { for (const b of document.querySelectorAll(".plx-toast button[aria-label='Dismiss']")) b.click(); });

    // The tile watcher, made by the host in the frame's realm, sees the
    // frame's tile requests (answered or not, each is timed).
    await page.evaluate(() => window.PlexoraEnvelope.watchTiles(document.getElementById("plexora-app").contentWindow));
    await frame.evaluate(async () => {
        for (let i = 0; i < 60; i += 1) await fetch(plexoraUrl(`generated/data/envtest/DNA_1/0/${i}_0`)).catch(() => null);
    });
    const seen = await until(() => frame.evaluate(() => (window.__plexoraStall?.seen >= 60 ? window.__plexoraStall.seen : 0)));
    check("the host's watcher sees the frame's tile requests", seen >= 60, { seen });

    // A batch: 2000 images at 0.75 s each, before it runs.
    await page.evaluate(() => window.PlexoraEnvelope.onJob({ kind: "job", job: {
        job_id: "env-1", capability: "gating.apply_to_dataset", status: "running", project: "envtest",
        progress: { done: 0, total: 2000 }, started_at: new Date().toISOString() } }));
    const batch = await until(() => toast(frame));
    check("a long batch: title, one line, two buttons on one row", batch
          && batch.title === "A long run for a browser tab"
          && batch.note === "2000 images: about 25 min here, with this tab open."
          && JSON.stringify(batch.buttons) === JSON.stringify(["Desktop app", "Continue here"])
          && batch.oneRow && batch.inside, { batch });
    if (shot) await page.screenshot({ path: `${shot}-batch.png` });
    await press(frame, "Continue here");
    const kept = await page.evaluate(() => Object.keys(localStorage).filter((k) => k.startsWith("plexora-notice:")));
    check("Continue here is remembered for the project", kept.includes("plexora-notice:envtest:batch"), { kept });
    await page.evaluate(() => window.PlexoraEnvelope.onJob({ kind: "job", job: {
        job_id: "env-2", capability: "gating.apply_to_dataset", status: "running", project: "envtest",
        progress: { done: 0, total: 5000 }, started_at: new Date().toISOString() } }));
    await page.waitForTimeout(500);
    check("said once per project per tab", (await toast(frame)) === null);
    // Both end: tiles are timed again (a job holding the worker slows them for its own reason).
    for (const id of ["env-1", "env-2"]) {
        await page.evaluate((id) => window.PlexoraEnvelope.onJob({ kind: "job", job: {
            job_id: id, status: "done", progress: { done: 1, total: 1 } } }), id);
    }

    // The model, where the browser has no WebGPU adapter.
    const model = await page.evaluate(() => window.PlexoraEnvelope.beforeModel());
    const modelToast = await until(async () => { const t = await toast(frame); return t && t.title.startsWith("Magic") ? t : null; });
    check("magic select without WebGPU: the measured second a click, buttons on one row",
          model && model.verdict === "node" && modelToast && modelToast.title === "Magic select without a GPU here"
          && modelToast.note === "No WebGPU here: about 1 s a click (0.95 s measured)."
          && JSON.stringify(modelToast.buttons) === JSON.stringify(["Desktop app", "Continue here"])
          && modelToast.oneRow && modelToast.inside, { modelToast });
    if (shot) await page.screenshot({ path: `${shot}-model.png` });
    await press(frame, "Continue here");

    // Slow tiles: judged (the rolling p95 of the last 50 over 150 ms) but
    // never said -- no notice offers a node.
    const stallVerdict = await page.evaluate(async () => {
        const app = document.getElementById("plexora-app").contentWindow;
        let verdict = null;
        for (let i = 0; i < 60; i += 1) verdict = (await window.PlexoraEnvelope.tile(400, app)) || verdict;
        return verdict && verdict.verdict;
    });
    await page.waitForTimeout(1000);
    const stall = await toast(frame);
    check("slow tiles: judged, no notice", stallVerdict === "node" && stall === null, { stallVerdict, stall });
    const offers = await frame.evaluate(() => [...document.querySelectorAll(".plx-toast button")].map((b) => b.textContent.trim()));
    check("no notice offers a node", !offers.includes("Connect a node"), { offers });
} catch (error) {
    out.errors.push(String(error?.stack || error).slice(0, 2000));
} finally {
    await browser.close().catch(() => null);
    server.stop();
}
out.ok = out.errors.length === 0 && out.checks.length > 0 && out.checks.every((c) => c.ok);
console.log(JSON.stringify(out, null, 1));
process.exit(out.ok ? 0 : 1);
