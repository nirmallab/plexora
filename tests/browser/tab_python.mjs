// One Python per tab, kept across a reload (services/pythonClient.js `spawn`,
// workers/pythonWorker.js `attach`, `watchPage`), in Chrome: the static build
// at the root and the Python bundle under /py/, as smoke.mjs serves them.
//
//   first    the tab's Python is a SharedWorker named for the tab;
//   reload   the page a reload brings finds it running (no second boot), and
//            Python answers at once;
//   copy     a tab whose sessionStorage was copied from a live one (Chrome's
//            Duplicate) gets a Python of its own;
//   closed   a tab's worker is gone once no page of it came back (PAGE_GONE_MS);
//   restart  Help › Restart Plexora in This Tab: a new Python, the old one gone.
//
//   node tests/browser/tab_python.mjs --site DIR [--py DIR] [--headed]
// Prints one JSON line; tests/test_tab_python.py asserts on it (when Chrome
// and a built site are at hand).
import { launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const site = opt("--site", null);
const pyRoot = opt("--py", null);
if (!site) throw new Error("--site DIR (static_build's output) is required");

const server = await startServer(["--site", site, ...(pyRoot ? ["--py", pyRoot] : [])]);
const origin = `http://127.0.0.1:${server.port}`;
const browser = await launch({ headed: args.includes("--headed") });
const out = { checks: {} };
const KEY = "plexora-python-tab";

async function started(page) {
    await page.waitForFunction(() => window.PlexoraShellStarted !== undefined, null, { timeout: 180000 });
    const t0 = Date.now();
    const info = await page.evaluate(async () => {
        const got = await window.PlexoraShellStarted;
        return { ready: got && got.phases ? Math.round(got.phases.ready || 0) : null, reattached: Boolean(got && got.reattached) };
    });
    return { ...info, waitedMs: Date.now() - t0 };
}
const health = (page) => page.evaluate(async () => {
    const t0 = performance.now();
    const answer = await fetch("/health");
    return { status: answer.status, ms: Math.round(performance.now() - t0) };
});

try {
    const page = await browser.context.newPage();
    const cdp = await browser.context.newCDPSession(page);
    const workers = async () => (await cdp.send("Target.getTargets")).targetInfos
        .filter((t) => t.type === "shared_worker" && /plexora-python:/.test(t.title || "")).map((t) => t.title).sort();
    await page.goto(`${origin}/`);
    const first = await started(page);
    const tab = await page.evaluate((key) => sessionStorage.getItem(key), KEY);
    out.checks.first = { mode: await page.evaluate(() => window.PlexoraPython.mode()), tab,
                         workers: await workers(), reattached: first.reattached, ready: first.ready };

    await page.reload();
    const again = await started(page);
    out.checks.reload = { tab: await page.evaluate((key) => sessionStorage.getItem(key), KEY), reattached: again.reattached,
                          sameBoot: again.ready === first.ready, health: await health(page), workers: await workers() };

    const copy = await browser.context.newPage();
    await copy.addInitScript(([key, value]) => { if (!sessionStorage.getItem(key)) sessionStorage.setItem(key, value); }, [KEY, tab]);
    await copy.goto(`${origin}/`);
    const copied = await started(copy);
    out.checks.copy = { tab: await copy.evaluate((key) => sessionStorage.getItem(key), KEY), reattached: copied.reattached,
                        workers: await workers() };
    await copy.close();
    const closedAt = Date.now();
    let gone = null;
    for (let i = 0; i < 40 && gone === null; i += 1) {
        await page.waitForTimeout(2000);
        if ((await workers()).length === 1) gone = Date.now() - closedAt;
    }
    out.checks.closed = { goneMs: gone, workers: await workers(), health: await health(page) };

    // Help › Restart, as the menu does it (the frame's click goes to this call).
    const reloaded = page.waitForNavigation({ timeout: 30000 });
    await page.evaluate(() => window.PlexoraPython.restart("test"));
    await reloaded;
    const restarted = await started(page);
    out.checks.restart = { tab: await page.evaluate((key) => sessionStorage.getItem(key), KEY), reattached: restarted.reattached,
                           workers: await workers(), health: await health(page) };
} catch (error) {
    out.fatal = String(error && error.stack || error).slice(0, 600);
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out));
