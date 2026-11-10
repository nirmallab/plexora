// A pyramid that does not fit in what the browser lets the site keep:
// Firefox allows about 10 GB per site and refuses a write past it
// (QuotaExceededError). The site's storage is filled to within --room MB of
// the quota first (a file in OPFS, written by a worker), then a flat image is
// imported: the import must stop with a sentence that says so, not hang.
//
//   node tests/browser/quota.mjs --file <flat tif> [--room 50] [--browser firefox]
// Prints JSON with `ok`: the import failed within --timeout seconds and said
// the browser has no room.
import { appFrame, launch, seedBrowserStore, startServer, REPO } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const file = opt("--file", null);
const room = Number(opt("--room", "50")) * 1e6;
const timeout = Number(opt("--timeout", "300")) * 1000;
if (!file) throw new Error("--file PATH: a flat image whose pyramid is larger than --room");

const FILL = `
self.onmessage = async ({ data: { bytes } }) => {
    try {
        const root = await navigator.storage.getDirectory();
        const handle = await (await root.getFileHandle("quota-filler.bin", { create: true })).createSyncAccessHandle();
        const chunk = new Uint8Array(64 << 20);
        let at = handle.getSize();
        while (at < bytes) {
            const n = Math.min(chunk.length, bytes - at);
            handle.write(n === chunk.length ? chunk : chunk.subarray(0, n), { at });
            at += n;
        }
        handle.flush();
        handle.close();
        postMessage({ ok: at });
    } catch (error) {
        postMessage({ error: String(error && (error.name + ": " + error.message)) });
    }
};`;

const server = await startServer(["--site", `${REPO}/plexora/client/dist/static`]);
const browser = await launch({});
const out = {};
try {
    const page = await browser.context.newPage();
    const logs = [];
    page.on("console", (message) => {
        if (!/try_table|threadpoolctl|longtask|WebGL/.test(message.text())) logs.push(message.text().slice(0, 300));
    });
    const origin = `http://127.0.0.1:${server.port}`;
    await seedBrowserStore(browser.context, origin);
    await page.goto(`${origin}/`);
    const frame = await appFrame(page);
    await frame.waitForSelector("#quick_view_go", { timeout: 120000 });
    // Fill the site's storage to within `room` of its quota.
    const filled = Date.now();
    out.before = await page.evaluate(() => navigator.storage.estimate());
    out.fill = await page.evaluate(async ([source, leave]) => {
        const { quota, usage } = await navigator.storage.estimate();
        const bytes = Math.max(0, Math.floor(quota - usage - leave));
        const worker = new Worker(URL.createObjectURL(new Blob([source], { type: "text/javascript" })));
        const answer = await new Promise((resolve) => {
            worker.onmessage = ({ data }) => resolve(data);
            worker.postMessage({ bytes });
        });
        worker.terminate();
        return { asked: bytes, ...answer };
    }, [FILL, room]);
    out.fillMs = Date.now() - filled;
    out.after = await page.evaluate(() => navigator.storage.estimate());
    // Import the image: Import sample, then the pyramid's question.
    const chooser = page.waitForEvent("filechooser", { timeout: 30000 });
    await frame.click("#quick_view_go");
    const picked = Date.now();
    await (await chooser).setFiles(file);
    out.pressed = [];
    let said = null;
    while (Date.now() - picked < timeout && !said) {
        const step = await frame.evaluate(() => {
            const visible = (node) => node && !node.disabled && node.offsetParent !== null;
            const text = document.body.innerText;
            const found = /no room left[^\n]*/.exec(text) || /could not build[^\n]*/.exec(text);
            if (found) return { said: found[0] };
            if (location.pathname !== "/") return { moved: location.pathname };
            const confirm = document.querySelector(".plx-confirm .plx-button-primary");
            if (visible(confirm)) { confirm.click(); return { pressed: confirm.textContent.trim() }; }
            const go = document.querySelector('[data-role="go"]');
            if (visible(go) && !go.dataset.pressedByTest) {
                go.dataset.pressedByTest = "1";
                go.click();
                return { pressed: go.textContent.trim() };
            }
            return null;
        }).catch(() => null);
        if (step && step.pressed) out.pressed.push(step.pressed);
        if (step && step.moved) { out.moved = step.moved; break; }
        if (step && step.said) said = step.said;
        await page.waitForTimeout(500);
    }
    out.saidMs = Date.now() - picked;
    out.said = said;
    out.logs = logs.slice(-25);
    out.text = await frame.evaluate(() => document.body.innerText.slice(-600)).catch((error) => String(error));
    out.worker = await page.evaluate(() => window.PlexoraPython.state()).catch((error) => String(error));
    out.builds = await page.evaluate(() => window.PlexoraPyramid.builds()
        .map(({ state, error, tiles, total, where }) => ({ state, error, tiles, total, where })));
    // Leave the profile as it was asked of the disk.
    await page.evaluate(async () => {
        const root = await navigator.storage.getDirectory();
        await root.removeEntry("quota-filler.bin").catch(() => {});
    });
} finally {
    await browser.close();
    server.stop();
}
out.ok = Boolean(out.said && /no room left/.test(out.said) && out.saidMs < timeout);
console.log(JSON.stringify(out, null, 1));
process.exit(out.ok ? 0 : 1);
