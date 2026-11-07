// A project as one file on the website: File > Export Project downloads its
// `.plexora` (the project's own work, never its images), and opening that file
// again (File > Open Project File..., the same route) gives a second project
// that draws, its image the same picked file. The menu items are checked to be
// there; the file goes through the routes they use.
//
//   node tests/browser/bundle.mjs --file <tif>
// Prints JSON with `ok`.
import { appFrame, launch, startServer, REPO } from "./lib.mjs";

const args = process.argv.slice(2);
const file = args[args.indexOf("--file") + 1];

const drawn = () => {
    const viewer = window.__plexora?.seaDragonViewer?.viewer;
    if (!viewer || !viewer.world || !viewer.world.getItemCount()) return false;
    for (let i = 0; i < viewer.world.getItemCount(); i += 1) if (!viewer.world.getItemAt(i).getFullyLoaded()) return false;
    return true;
};

const server = await startServer(["--site", `${REPO}/plexora/client/dist/static`]);
const browser = await launch({});
const out = {};
try {
    const page = await browser.context.newPage();
    const origin = `http://127.0.0.1:${server.port}`;
    await page.goto(`${origin}/`);
    let frame = await appFrame(page);
    await frame.waitForSelector("#quick_view_go", { timeout: 120000 });
    await frame.waitForFunction(() => {
        const keep = [...document.querySelectorAll(".plx-confirm .plx-button")]
            .find((button) => button.textContent.trim() === "Keep in browser");
        if (keep) keep.click();
        return Boolean(keep);
    }, null, { timeout: 8000, polling: 200 }).catch(() => null);
    await page.evaluate(() => { window.showOpenFilePicker = undefined; window.showDirectoryPicker = undefined; });
    const chooser = page.waitForEvent("filechooser", { timeout: 30000 });
    await frame.click("#quick_view_go");
    await (await chooser).setFiles(file);
    const until = Date.now() + 300000;
    while (Date.now() < until && !(await frame.evaluate(() => location.pathname !== "/").catch(() => false))) {
        await frame.evaluate(() => {
            const confirm = [...document.querySelectorAll(".plx-confirm .plx-button-primary")].find((n) => n.offsetParent !== null);
            if (confirm) { confirm.click(); return; }
            const go = document.querySelector('[data-role="go"]');
            if (go && !go.disabled && go.offsetParent !== null && !go.dataset.pressed) { go.dataset.pressed = "1"; go.click(); }
        }).catch(() => null);
        await page.waitForTimeout(400);
    }
    frame = await appFrame(page);
    out.project = await frame.evaluate(() => location.pathname.slice(1));
    out.menu = await frame.evaluate(() => ({
        export: document.getElementById("nav_export_project")?.getAttribute("href"),
        open: Boolean(document.getElementById("nav_open_bundle")),
    }));
    // Export, then open the file again.
    out.bundle = await frame.evaluate(async (name) => {
        const exported = await fetch(`/project/${encodeURIComponent(name)}/bundle`);
        const bytes = await exported.arrayBuffer();
        const opened = await fetch("/projects/bundle", { method: "POST", body: bytes,
                                                        headers: { "Content-Type": "application/zip" } });
        return { status: exported.status, kb: Math.round(bytes.byteLength / 1024),
                 disposition: exported.headers.get("Content-Disposition"), opened: await opened.json() };
    }, out.project);
    // Within the app's frame, as the menu does: the page's Python and its
    // picks stay (a reload would ask again for a file picked with a plain input).
    await frame.evaluate((name) => { location.href = `/${encodeURIComponent(name)}`; }, out.bundle.opened.project);
    await page.waitForTimeout(500);
    frame = await appFrame(page);
    out.drawn = await frame.waitForFunction(drawn, null, { timeout: 120000, polling: 250 }).then(() => true, () => false);
} finally {
    await browser.close();
    server.stop();
}
out.ok = Boolean(out.menu.export && out.menu.open && out.bundle.status === 200
                 && out.bundle.opened.project && out.bundle.opened.project !== out.project && out.drawn);
console.log(JSON.stringify(out, null, 1));
process.exit(out.ok ? 0 : 1);
