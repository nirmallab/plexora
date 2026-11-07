// Where the website keeps what it makes: the first import asks where the
// projects go and where a flat image's pyramidized copy goes, and both are
// answered "a folder on this computer". The browser's folder picker is a
// native dialog no driver can answer, so `showDirectoryPicker` hands back
// folders in the test profile's own storage (OPFS test-folders/<projects |
// images>); everything after the picker is the product's. Then the page is
// reloaded: the projects must be read from that folder again and the copy
// mounted from it, and the project must open.
//
//   node tests/browser/places.mjs --file PATH [--close-pyramid] [--shots DIR] [--headed]
//
// --close-pyramid closes the pyramid's question instead of answering it: the
// copy then goes with the projects (Plexora's folder).
//
// Where the browser has no folder picker (Firefox, Safari: `--browser`), there
// is no folder to choose: the first visit says the projects stay in the
// browser ("Keep them" asks it to keep them), the pyramid's question takes its
// other form and the copy is kept in the site's storage (OPFS, else IndexedDB,
// else memory). The reload then asks for the picked file again ("Locate
// file…": the browser kept no handle to it), answered with the same file.
import path from "node:path";
import { REPO, appFrame, canWriteFolders, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const file = opt("--file", null);
const shots = opt("--shots", null);
const site = opt("--site", path.join(REPO, "plexora", "client", "dist", "static"));
if (!file) throw new Error("--file PATH: a flat image to import");

const STUB = () => {
    window.__folders = [];
    window.showDirectoryPicker = async (options = {}) => {
        const root = await navigator.storage.getDirectory();
        const top = await root.getDirectoryHandle("test-folders", { create: true });
        const name = options.id === "plexora-data" ? "Documents" : "images";
        window.__folders.push({ id: options.id, mode: options.mode, gave: name });
        return top.getDirectoryHandle(name, { create: true });
    };
    window.showOpenFilePicker = undefined; // the file comes through <input type=file>
};

const LIST = async () => {
    const walk = async (dir, prefix = "") => {
        const out = [];
        for await (const [name, handle] of dir.entries()) {
            if (handle.kind === "directory") out.push(...await walk(handle, `${prefix}${name}/`));
            else out.push(`${prefix}${name} ${(await handle.getFile()).size}`);
        }
        return out;
    };
    const root = await navigator.storage.getDirectory();
    const top = await root.getDirectoryHandle("test-folders", { create: true });
    return walk(top);
};

const viewerDrawn = () => {
    const viewer = window.__plexora?.seaDragonViewer?.viewer;
    if (!viewer || !viewer.world || !viewer.world.getItemCount()) return false;
    for (let i = 0; i < viewer.world.getItemCount(); i += 1) {
        if (!viewer.world.getItemAt(i).getFullyLoaded()) return false;
    }
    return true;
};

const server = await startServer(["--site", site]);
const browser = await launch({ headed: args.includes("--headed") });
const out = { pressed: [] };
try {
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(`pageerror: ${error}`));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text().slice(0, 300)); });
    const failed = [];
    page.on("response", (response) => { if (response.status() >= 400) failed.push(`${response.status()} ${response.url()}`); });
    const origin = `http://127.0.0.1:${server.port}`;
    out.canWriteFolders = await canWriteFolders(browser.context, origin);
    // Before the page's own scripts: headless Chrome has no folder picker,
    // and the page decides at load whether this browser can write folders.
    if (out.canWriteFolders) await page.addInitScript(STUB);
    await page.goto(`${origin}/?pydebug`);
    let frame = await appFrame(page);
    await frame.waitForSelector("#quick_view_go", { timeout: 120000 });
    await frame.evaluate(() => { window.showOpenFilePicker = undefined; });
    // The first visit asks where the projects go: a folder. (No folder to
    // choose: they stay in the browser, which is asked to keep them.)
    const primary = ".plx-confirm .plx-button-primary";
    out.firstVisit = await frame.waitForSelector(primary, { timeout: 15000 }).then(async (button) => {
        if (shots) {
            await page.waitForTimeout(300);
            await page.screenshot({ path: path.join(shots, "places-first-visit.png") });
        }
        const title = await frame.evaluate(() => document.querySelector(".plx-confirm .plx-dialog-title")?.textContent);
        await button.click();
        if (out.canWriteFolders) {
            await page.waitForFunction(() => window.PlexoraStore.current().kind === "folder", null, { timeout: 30000 });
        }
        return title;
    }, () => null);
    await page.waitForTimeout(1000);
    out.persisted = await page.evaluate(() => navigator.storage.persisted()).catch((error) => String(error));
    const chooser = page.waitForEvent("filechooser", { timeout: 30000 });
    await frame.click("#quick_view_go");
    const picked = Date.now();
    await (await chooser).setFiles(file);
    const until = Date.now() + 600000;
    let moved = false;
    while (!moved && Date.now() < until) {
        moved = await frame.evaluate(() => location.pathname !== "/" && !document.getElementById("plexora-boot"))
            .catch(() => false);
        if (moved) break;
        // Each question as the user sees it, before it is answered.
        const asking = await frame.evaluate(() => document.querySelector(".plx-confirm .plx-button-primary")?.offsetParent != null)
            .catch(() => false);
        if (asking && shots) {
            await page.waitForTimeout(300);
            await page.screenshot({ path: path.join(shots, `places-question-${out.pressed.length}.png`) });
        }
        const pressed = await frame.evaluate((closePyramid) => {
            const visible = (node) => node && !node.disabled && node.offsetParent !== null;
            const confirm = document.querySelector(".plx-confirm .plx-button-primary");
            const close = document.querySelector(".plx-confirm .plx-confirm-close");
            const asked = document.querySelector(".plx-confirm .plx-dialog-title")?.textContent.trim() || "";
            if (closePyramid && visible(close) && asked.includes("pyramid")) {
                close.click();
                return `${asked} -> closed`;
            }
            if (visible(confirm)) {
                const title = document.querySelector(".plx-confirm .plx-dialog-title")?.textContent.trim();
                confirm.click();
                return `${title} -> ${confirm.textContent.trim()}`;
            }
            const go = document.querySelector('[data-role="go"]');
            if (visible(go) && !go.dataset.pressedByTest) {
                go.dataset.pressedByTest = "1";
                go.click();
                return `import -> ${go.textContent.trim()}`;
            }
            return null;
        }, args.includes("--close-pyramid") && out.canWriteFolders).catch(() => null);
        if (pressed) out.pressed.push({ at: Date.now() - picked, pressed });
        await page.waitForTimeout(400);
    }
    out.importMs = Date.now() - picked;
    out.project = await frame.evaluate(() => location.pathname.slice(1));
    await frame.waitForFunction(viewerDrawn, null, { timeout: 180000, polling: 250 })
        .catch(() => { out.viewerTimedOut = true; });
    out.viewerMs = Date.now() - picked;
    out.asked = await page.evaluate(() => window.__folders);
    out.store = await page.evaluate(() => window.PlexoraStore.current());
    out.copies = await page.evaluate(() => window.PlexoraPyramid.builds()
        .map(({ where, place, path: p, state, bytes, note, ms, timings }) => ({ where, place, path: p, state, bytes, note, ms, timings })));
    if (out.canWriteFolders) out.folders = await page.evaluate(LIST);
    await page.waitForTimeout(3000);
    if (out.canWriteFolders) out.foldersLater = await page.evaluate(LIST);
    out.dataFiles = await page.evaluate(() => window.PlexoraPython.run([
        "import os",
        "found = [(os.path.join(d, f), os.path.getsize(os.path.join(d, f))) for d, _, fs in os.walk('/data') for f in fs]",
        "(len(found), sum(s for _, s in found), sorted(found, key=lambda x: -x[1])[:8])",
    ].join("\n")).catch((e) => String(e)));
    out.flushMs = await page.evaluate(async () => { const t = performance.now(); await window.PlexoraPython.flush(); return Math.round(performance.now() - t); });
    if (out.canWriteFolders) out.foldersFlushed = await page.evaluate(LIST);
    if (shots) await page.screenshot({ path: path.join(shots, "places-first.png") });

    // A later visit: no picker this time; the folder handles kept by the page.
    // (No handles where there is no folder picker: the loader asks for the
    // picked file again, a click that may open the browser's file dialog.)
    await page.goto(`${origin}/${out.project}?pydebug`);
    const reopened = Date.now();
    const loader = await (await page.waitForSelector("#plexora-app", { timeout: 120000 })).contentFrame();
    if (!out.canWriteFolders) {
        const locate = loader.locator(".plexora-boot-actions button", { hasText: "Locate file" });
        out.locateAsked = await locate.waitFor({ timeout: 30000 }).then(() => true, () => false);
        if (out.locateAsked) {
            const chooser = page.waitForEvent("filechooser", { timeout: 30000 });
            await locate.click();
            await (await chooser).setFiles(file);
        }
    } else {
        // The original was picked through <input type=file> (this driver
        // hides the file picker), so no handle was kept and the loader asks
        // for its folder ("Find them…", find_files.mjs's subject): skipped
        // here, the pyramidized copy is what the viewer reads.
        const skip = loader.locator(".plexora-boot-actions button", { hasText: "Skip" });
        out.findAsked = await skip.waitFor({ timeout: 20000 }).then(() => true, () => false);
        if (out.findAsked) await skip.click();
    }
    frame = await appFrame(page);
    await frame.waitForFunction(viewerDrawn, null, { timeout: 180000, polling: 250 })
        .catch(() => { out.reopenTimedOut = true; });
    out.reopenMs = Date.now() - reopened;
    out.reopenStore = await page.evaluate(() => window.PlexoraStore.current());
    out.reopenProjects = await frame.evaluate(async () => {
        const body = await (await fetch("/projects")).json();
        return (body.projects || body || []).map((p) => p.name || p).slice(0, 10);
    });
    out.reopenMounted = await page.evaluate(() => window.PlexoraPython.run(
        "import os; sorted(os.listdir('/data'))[:20], sorted(os.listdir('/derived'))").catch((e) => String(e)));
    out.waitingCopies = await page.evaluate(() => window.PlexoraPyramid.waiting());
    if (shots) await page.screenshot({ path: path.join(shots, "places-reopened.png") });
    out.errors = errors.slice(0, 20);
    out.failed = failed.slice(0, 20);
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out, null, 1));
