// A user's first image on the website: the home card's Browse, a file from
// this computer, Plexora's own import, and the viewer drawing it. The File
// System Access picker is a native dialog no driver can answer, so the test
// takes the page's other path (<input type=file>) by hiding it.
//
//   node tests/browser/import.mjs --file PATH [--shots DIR] [--headed]
import path from "node:path";
import { REPO, appFrame, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const file = opt("--file", null);
const shots = opt("--shots", null);
const site = opt("--site", path.join(REPO, "plexora", "client", "dist", "static"));
if (!file) throw new Error("--file PATH: the image to open");

const server = await startServer(["--site", site]);
const browser = await launch({ headed: args.includes("--headed") });
const out = {};
try {
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    const errors = [];
    const failed = [];
    page.on("pageerror", (error) => errors.push(`pageerror: ${error}`));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text().slice(0, 300)); });
    page.on("response", (response) => { if (response.status() >= 400) failed.push(`${response.status()} ${response.url()}`); });
    const requests = [];
    page.on("request", (request) => requests.push(`${request.method()} ${request.url().replace(/^https?:\/\/[^/]+/, "")}`));
    const started = Date.now();
    if (args.includes("--verbose")) {
        // Every console line of the page and its workers, timed, on stderr.
        const say = (where, text) => process.stderr.write(`${((Date.now() - started) / 1000).toFixed(1)}s ${where} ${text.slice(0, 400)}\n`);
        page.on("console", (message) => say(`[${message.type()}]`, message.text()));
        page.on("worker", (worker) => worker.on("console", (message) => say("[worker]", message.text())));
        page.on("crash", () => say("[crash]", "the page crashed"));
    }
    await page.goto(`http://127.0.0.1:${server.port}/`);
    const frame = await appFrame(page);
    await frame.waitForSelector("#quick_view_go", { timeout: 120000 });
    out.homeMs = Date.now() - started;
    // The first visit asks where projects go, where this browser can write
    // folders: this driver keeps them in the browser. Where it cannot, it says
    // they stay in the browser: "Keep them" asks the browser to keep them.
    out.firstVisit = await frame.waitForFunction(() => {
        const keep = [...document.querySelectorAll(".plx-confirm .plx-button")]
            .find((button) => ["Keep in browser", "Keep them"].includes(button.textContent.trim()));
        if (!keep) return false;
        keep.click();
        return keep.textContent.trim();
    }, null, { timeout: 8000, polling: 200 }).then((answer) => answer.jsonValue(), () => null);
    out.store = await page.evaluate(() => window.PlexoraStore.current());
    out.persisted = await page.evaluate(() => navigator.storage.persisted()).catch((error) => String(error));
    await page.evaluate(() => { window.showOpenFilePicker = undefined; window.showDirectoryPicker = undefined; });
    const chooser = page.waitForEvent("filechooser", { timeout: 30000 });
    await frame.click("#quick_view_go");
    const picked = Date.now();
    await (await chooser).setFiles(file);
    // An image that opens as it is goes straight to its page. One that needs
    // a pyramidized copy opens Import sample first: press Import, then accept
    // the copy (out.pressed says which were pressed).
    out.pressed = [];
    const goneAt = Date.now() + Number(opt("--import-timeout", "900")) * 1000;
    let moved = false;
    while (!moved && Date.now() < goneAt) {
        moved = await frame.evaluate(() => location.pathname !== "/" && window.PlexoraShellFramed
                                     && !document.getElementById("plexora-boot")).catch(() => false);
        if (moved) break;
        const pressed = await frame.evaluate(() => {
            const visible = (node) => node && !node.disabled && node.offsetParent !== null;
            const confirm = document.querySelector(".plx-confirm .plx-button-primary");
            if (visible(confirm)) { confirm.click(); return `confirm: ${confirm.textContent.trim()}`; }
            const go = document.querySelector('[data-role="go"]');
            if (visible(go) && !go.dataset.pressedByTest) {
                go.dataset.pressedByTest = "1";
                go.click();
                return `import: ${go.textContent.trim()}`;
            }
            return null;
        }).catch(() => null);
        if (pressed) out.pressed.push({ at: Date.now() - picked, pressed });
        await page.waitForTimeout(500);
    }
    if (!moved) {
        out.stuck = await frame.evaluate(() => ({
            input: document.getElementById("quick_view_path_input")?.value,
            at: location.pathname,
            framed: Boolean(window.PlexoraShellFramed),
            booting: Boolean(document.getElementById("plexora-boot")),
            text: document.body.innerText.slice(0, 800),
        }));
        out.errors = errors.slice(0, 20);
        out.failed = failed.slice(0, 20);
        out.requests = requests.slice(-30);
        if (shots) await page.screenshot({ path: path.join(shots, "import-stuck.png") });
        throw Object.assign(new Error("never left the home page"), { out });
    }
    out.address = await frame.evaluate(() => location.pathname);
    out.tabAddress = await page.evaluate(() => location.pathname);
    out.importMs = Date.now() - picked;
    // The viewer is up when OpenSeadragon has drawn tiles of every channel on.
    await frame.waitForFunction(() => {
        const viewer = window.__plexora?.seaDragonViewer?.viewer;
        if (!viewer || !viewer.world || !viewer.world.getItemCount()) return false;
        for (let i = 0; i < viewer.world.getItemCount(); i += 1) {
            if (!viewer.world.getItemAt(i).getFullyLoaded()) return false;
        }
        return true;
    }, null, { timeout: 180000, polling: 250 }).catch(() => { out.viewerTimedOut = true; });
    out.viewerMs = Date.now() - picked;
    out.state = await frame.evaluate(() => ({
        title: document.title,
        items: window.__plexora?.seaDragonViewer?.viewer?.world?.getItemCount() ?? null,
        picks: null,
    }));
    out.pyramids = await page.evaluate(() => (window.PlexoraPyramid ? window.PlexoraPyramid.builds() : null));
    out.picks = await page.evaluate(async () => (await window.PlexoraPicks.list())
        .map((p) => ({ id: p.id, kind: p.kind, files: (p.files || []).length })));
    if (shots) await page.screenshot({ path: path.join(shots, "import-viewer.png") });
    // What Python answered while the viewer opened, by route.
    const recent = await page.evaluate(() => window.PlexoraPython.recent());
    const byRoute = {};
    for (const r of recent) {
        const key = r.path.replace(/\/generated\/data\/.*/, "/generated/data/*").replace(/\/\d+_\d+\.png$/, "/tile");
        const row = byRoute[key] || (byRoute[key] = { n: 0, ms: 0, py: 0, max: 0 });
        row.n += 1; row.ms += r.ms; row.py += r.pyMs || 0; row.max = Math.max(row.max, r.ms);
    }
    out.python = Object.fromEntries(Object.entries(byRoute).sort((a, b) => b[1].ms - a[1].ms).slice(0, 25));
    out.pythonTotal = { requests: recent.length, ms: recent.reduce((a, r) => a + r.ms, 0),
                        py: recent.reduce((a, r) => a + (r.pyMs || 0), 0) };
    out.steps = await page.evaluate(() => window.PlexoraPython.steps());
    out.errors = errors.slice(0, 20);
    out.failed = failed.slice(0, 20);
} catch (error) {
    console.log(JSON.stringify(error.out || { error: String(error) }, null, 1));
    process.exitCode = 1;
} finally {
    await browser.close();
    server.stop();
}
if (!process.exitCode) console.log(JSON.stringify(out, null, 1));
