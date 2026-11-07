// "Open in Desktop App" on the website, for a project kept in the browser:
// the projects move into Documents/Plexora/projects -- merged with what the
// desktop app keeps there, never over it -- and the desktop app is asked to
// open the project (`plexora://open?project=`). No desktop app answers in a
// headless browser, so the download is offered. The folder picker is stood
// in by a folder of the profile's storage.
//
//   node tests/browser/handoff.mjs --file <flat or pyramidal tif>
// Prints JSON with `ok`.
import { appFrame, launch, startServer, REPO } from "./lib.mjs";

const args = process.argv.slice(2);
const file = args[args.indexOf("--file") + 1];

const STUB = () => {
    window.showDirectoryPicker = async () => {
        const root = await navigator.storage.getDirectory();
        const top = await root.getDirectoryHandle("test-folders", { create: true });
        return top.getDirectoryHandle("Documents", { create: true });
    };
    window.showOpenFilePicker = undefined;
};
const LIST = async () => {
    const root = await navigator.storage.getDirectory();
    const projects = await (await (await (await root.getDirectoryHandle("test-folders")).getDirectoryHandle("Documents"))
        .getDirectoryHandle("Plexora")).getDirectoryHandle("projects");
    const config = JSON.parse(await (await (await projects.getFileHandle("config.json")).getFile()).text());
    return { names: Object.keys(config).sort(), desktopEntry: config.desktop_one };
};

const server = await startServer(["--site", `${REPO}/plexora/client/dist/static`]);
const browser = await launch({});
const out = {};
try {
    const page = await browser.context.newPage();
    await page.addInitScript(STUB);
    const origin = `http://127.0.0.1:${server.port}`;
    await page.goto(`${origin}/`);
    let frame = await appFrame(page);
    await frame.waitForSelector("#quick_view_go", { timeout: 120000 });
    // Projects kept in the browser.
    await frame.waitForFunction(() => {
        const keep = [...document.querySelectorAll(".plx-confirm .plx-button")]
            .find((button) => button.textContent.trim() === "Keep in browser");
        if (keep) keep.click();
        return Boolean(keep);
    }, null, { timeout: 8000, polling: 200 });
    // The desktop app already keeps a project in the folder.
    await page.evaluate(async () => {
        const root = await navigator.storage.getDirectory();
        const docs = await (await root.getDirectoryHandle("test-folders", { create: true }))
            .getDirectoryHandle("Documents", { create: true });
        const projects = await (await docs.getDirectoryHandle("Plexora", { create: true }))
            .getDirectoryHandle("projects", { create: true });
        const write = async (dir, name, body) => {
            const w = await (await dir.getFileHandle(name, { create: true })).createWritable();
            await w.write(body);
            await w.close();
        };
        await write(projects, "config.json", JSON.stringify({ desktop_one: { channelFile: "D:/data/one.tif" } }));
        await write(await projects.getDirectoryHandle("desktop_one", { create: true }), "desktop_one.db", "db");
    });
    // Import an image into the browser's store.
    await frame.evaluate(() => { window.showOpenFilePicker = undefined; });
    const chooser = page.waitForEvent("filechooser", { timeout: 30000 });
    await frame.click("#quick_view_go");
    await (await chooser).setFiles(file);
    const until = Date.now() + 300000;
    while (Date.now() < until) {
        if (await frame.evaluate(() => location.pathname !== "/").catch(() => false)) break;
        await frame.evaluate(() => {
            const confirm = [...document.querySelectorAll(".plx-confirm .plx-confirm-close, .plx-confirm .plx-button-primary")]
                .find((node) => node.offsetParent !== null);
            if (confirm) { confirm.click(); return; }
            const go = document.querySelector('[data-role="go"]');
            if (go && !go.disabled && go.offsetParent !== null && !go.dataset.pressed) { go.dataset.pressed = "1"; go.click(); }
        }).catch(() => null);
        await page.waitForTimeout(400);
    }
    frame = await appFrame(page);
    out.project = await frame.evaluate(() => location.pathname.slice(1));
    out.storeBefore = await page.evaluate(() => window.PlexoraStore.current().kind);
    // Open in Desktop App.
    out.page = await frame.evaluate(() => ({ at: location.pathname, settings: Boolean(document.getElementById("nav_settings")),
                                             desktop: Boolean(document.getElementById("nav_open_desktop")),
                                             website: document.documentElement.className }));
    if (!out.page.desktop) { console.log(JSON.stringify(out)); process.exit(1); }
    await frame.waitForSelector("#nav_open_desktop", { state: "attached", timeout: 30000 });
    out.menuShown = await frame.evaluate(() => getComputedStyle(document.getElementById("nav_open_desktop")).display !== "none");
    await frame.evaluate(() => document.getElementById("nav_open_desktop").click());
    out.offered = await frame.waitForFunction(() => [...document.querySelectorAll(".plexora-toast, .plx-toast, [class*='toast']")]
        .some((node) => /Get the desktop app/.test(node.textContent)), null, { timeout: 30000, polling: 250 })
        .then(() => true, () => false);
    out.storeAfter = await page.evaluate(() => window.PlexoraStore.current());
    out.folder = await page.evaluate(LIST);
} finally {
    await browser.close();
    server.stop();
}
out.ok = Boolean(out.menuShown && out.storeBefore === "browser" && out.storeAfter.kind === "folder"
                 && out.folder.names.includes("desktop_one") && out.folder.names.includes(out.project)
                 && out.folder.desktopEntry.channelFile === "D:/data/one.tif" && out.offered);
console.log(JSON.stringify(out, null, 1));
process.exit(out.ok ? 0 : 1);
