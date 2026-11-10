// A project made in the desktop app, opened on the website: its image is
// named by a path on the disk, which a web page cannot open. The loader asks
// once for the folder that holds it ("Find them..."); the page finds the file
// there by its identity, Python records where, and the viewer draws it.
//
//   node tests/browser/find_files.mjs --entry ENTRY.json --image IMAGE
//
// ENTRY.json: {name, entry} -- a config.json entry as the desktop wrote it in
// the website's library (tests/test_browser_find_files.py makes one). The
// folder picker is stood in by a folder of the profile's storage holding a
// copy of IMAGE (served by the test server; a copy keeps the bytes, not the time).
// Where the browser has no folder picker (Firefox, Safari) the loader asks for
// the file itself ("Locate file…"), answered here with IMAGE.
// Prints JSON with `ok`.
import path from "node:path";
import { readFileSync } from "node:fs";
import { appFrame, canWriteFolders, launch, seedBrowserStore, startServer, REPO } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name) => args[args.indexOf(name) + 1];
const { name, entry } = JSON.parse(readFileSync(opt("--entry"), "utf-8"));
const image = opt("--image");

const server = await startServer(["--site", `${REPO}/plexora/client/dist/static`,
                                  "--tree", `fixture=${path.dirname(image)}`]);
const browser = await launch({});
const out = { name };
try {
    const page = await browser.context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    const origin = `http://127.0.0.1:${server.port}`;
    out.folders = await canWriteFolders(browser.context, origin);
    if (out.folders) {
        await page.addInitScript(() => {
            window.showDirectoryPicker = async () => {
                const root = await navigator.storage.getDirectory();
                return root.getDirectoryHandle("found-here", { create: true });
            };
        });
    }
    await seedBrowserStore(browser.context, origin);
    await page.goto(`${origin}/?pydebug`);
    let frame = await appFrame(page);
    // The desktop's project, in the website's store; the image's copy in the
    // folder the picker will give.
    if (out.folders) await page.evaluate(async ([fileName, url]) => {
        const root = await navigator.storage.getDirectory();
        const dir = await root.getDirectoryHandle("found-here", { create: true });
        const writable = await (await dir.getFileHandle(fileName, { create: true })).createWritable();
        await writable.write(await (await fetch(url)).blob());
        await writable.close();
    }, [path.basename(image), `/fixture/${path.basename(image)}`]);
    await page.evaluate(([projectName, projectEntry]) => window.PlexoraPython.run([
        "import json, pathlib",
        "p = pathlib.Path('/data/config.json')",
        "config = json.loads(p.read_text()) if p.exists() else {}",
        `config[${JSON.stringify(projectName)}] = json.loads(${JSON.stringify(JSON.stringify(projectEntry))})`,
        "p.write_text(json.dumps(config))",
    ].join("\n")), [name, entry]);
    await page.evaluate(() => window.PlexoraPython.flush());

    // Open it: the loader asks; "Find them..." is the answer -- or, with no
    // folder picker, "Locate file...", a click the browser lets open its file
    // dialog, answered with the image.
    await page.goto(`${origin}/${encodeURIComponent(name)}?pydebug`);
    if (out.folders) {
        out.asked = await page.waitForFunction(() => {
            const frameEl = document.getElementById("plexora-app");
            const inner = frameEl && frameEl.contentDocument;
            const button = inner && [...inner.querySelectorAll(".plexora-boot-actions button")]
                .find((b) => b.textContent.trim() === "Find them…");
            if (button) button.click();
            return Boolean(button);
        }, null, { timeout: 60000, polling: 200 }).then(() => true, () => false);
    } else {
        const loader = await (await page.waitForSelector("#plexora-app", { timeout: 60000 })).contentFrame();
        const locate = loader.locator(".plexora-boot-actions button", { hasText: "Locate file" });
        out.asked = await locate.waitFor({ timeout: 60000 }).then(() => true, () => false);
        if (out.asked) {
            const chooser = page.waitForEvent("filechooser", { timeout: 30000 });
            await locate.click();
            await (await chooser).setFiles(image);
        }
    }
    frame = await appFrame(page);
    out.drawn = await frame.waitForFunction(() => {
        const viewer = window.__plexora?.seaDragonViewer?.viewer;
        if (!viewer || !viewer.world || !viewer.world.getItemCount()) return false;
        for (let i = 0; i < viewer.world.getItemCount(); i += 1) {
            if (!viewer.world.getItemAt(i).getFullyLoaded()) return false;
        }
        return true;
    }, null, { timeout: 120000, polling: 250 }).then(() => true, () => false);
    out.recorded = await page.evaluate((projectName) => window.PlexoraPython.run([
        "import json, pathlib",
        `entry = json.loads(pathlib.Path('/data/config.json').read_text())[${JSON.stringify(projectName)}]`,
        "[info.get('at', {}).get('website') for info in entry.get('sources', {}).values()]",
    ].join("\n")), name);
    out.errors = errors.slice(0, 10);
} finally {
    await browser.close();
    server.stop();
}
out.ok = Boolean(out.asked && out.drawn && String(out.recorded).includes("/picked/"));
console.log(JSON.stringify(out, null, 1));
process.exit(out.ok ? 0 : 1);
