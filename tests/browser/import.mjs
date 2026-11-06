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
    await page.goto(`http://127.0.0.1:${server.port}/`);
    const frame = await appFrame(page);
    await frame.waitForSelector("#quick_view_go", { timeout: 120000 });
    out.homeMs = Date.now() - started;
    await page.evaluate(() => { window.showOpenFilePicker = undefined; window.showDirectoryPicker = undefined; });
    const chooser = page.waitForEvent("filechooser", { timeout: 30000 });
    await frame.click("#quick_view_go");
    const picked = Date.now();
    await (await chooser).setFiles(file);
    const moved = await frame.waitForFunction(() => location.pathname !== "/" && window.PlexoraShellFramed
                                                  && !document.getElementById("plexora-boot"),
                                              null, { timeout: 90000 })
        .then(() => true, () => false);
    if (!moved) {
        out.stuck = await frame.evaluate(() => ({
            input: document.getElementById("quick_view_path_input")?.value,
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
    out.picks = await page.evaluate(async () => (await window.PlexoraPicks.list())
        .map((p) => ({ id: p.id, kind: p.kind, files: (p.files || []).length })));
    if (shots) await page.screenshot({ path: path.join(shots, "import-viewer.png") });
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
