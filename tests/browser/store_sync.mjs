// The website's project store shares its folder with the desktop app
// (Documents/Plexora/projects): what the other app writes there while a tab is
// open must survive the tab's write-back and reach the tab's Python, and the
// tab's own deletions must reach the folder -- but never delete what the other
// app changed (workers/storeSync.js). The folder is the test profile's own
// storage standing in for the granted one (no driver can answer the picker).
// Prints JSON with `ok` and each check; exits 1 when one fails.
//
//   node tests/browser/store_sync.mjs
import { appFrame, launch, startServer, REPO } from "./lib.mjs";

const STUB = () => {
    window.showDirectoryPicker = async () => {
        const root = await navigator.storage.getDirectory();
        const top = await root.getDirectoryHandle("test-folders", { create: true });
        return top.getDirectoryHandle("Documents", { create: true });
    };
};

//: In the page: the projects folder, as the other app would reach it.
const FOLDER = async () => {
    const root = await navigator.storage.getDirectory();
    const docs = await (await root.getDirectoryHandle("test-folders")).getDirectoryHandle("Documents");
    return (await docs.getDirectoryHandle("Plexora")).getDirectoryHandle("projects");
};

const server = await startServer(["--site", `${REPO}/plexora/client/dist/static`]);
const browser = await launch({});
const checks = {};
try {
    const page = await browser.context.newPage();
    await page.addInitScript(STUB);
    await page.goto(`http://127.0.0.1:${server.port}/?pydebug`);
    const frame = await appFrame(page);
    await (await frame.waitForSelector(".plx-confirm .plx-button-primary", { timeout: 20000 })).click();
    await page.waitForFunction(() => window.PlexoraStore.current().kind === "folder", null, { timeout: 30000 });
    const py = (code) => page.evaluate((c) => window.PlexoraPython.run(c), code);
    const inFolder = (rel) => page.evaluate(async ([folderFn, path]) => {
        // eslint-disable-next-line no-new-func
        let dir = await (new Function(`return (${folderFn})()`))();
        const parts = path.split("/");
        try {
            for (const part of parts.slice(0, -1)) dir = await dir.getDirectoryHandle(part);
            return await (await (await dir.getFileHandle(parts.at(-1))).getFile()).text();
        } catch (error) {
            return null;
        }
    }, [FOLDER.toString(), rel]);
    const otherAppWrites = (rel, text) => page.evaluate(async ([folderFn, path, body]) => {
        // eslint-disable-next-line no-new-func
        let dir = await (new Function(`return (${folderFn})()`))();
        const parts = path.split("/");
        for (const part of parts.slice(0, -1)) dir = await dir.getDirectoryHandle(part, { create: true });
        const writable = await (await dir.getFileHandle(parts.at(-1), { create: true })).createWritable();
        await writable.write(body);
        await writable.close();
    }, [FOLDER.toString(), rel, text]);

    // The website writes a file of its own; it reaches the folder.
    await py("import pathlib\npathlib.Path('/data/web_note.json').write_text('one')");
    await page.evaluate(() => window.PlexoraPython.flush());
    checks.websiteWriteReachesFolder = (await inFolder("web_note.json")) === "one";

    // The desktop app adds a project while the tab is open; the tab writes again.
    await otherAppWrites("desktop_made/desktop_made.db", "desktop");
    await py("import pathlib\npathlib.Path('/data/web_note.json').write_text('two')");
    await page.evaluate(() => window.PlexoraPython.flush());
    checks.otherAppsProjectSurvives = (await inFolder("desktop_made/desktop_made.db")) === "desktop";
    checks.websiteChangeWritten = (await inFolder("web_note.json")) === "two";

    // The tab reads the other app's project in.
    // Between requests, every two seconds: a request Python answers.
    await page.waitForTimeout(2200);
    await frame.evaluate(() => fetch("/projects"));
    checks.otherAppsProjectReadIn = (await py("import pathlib\np = pathlib.Path('/data/desktop_made/desktop_made.db')\np.read_text() if p.exists() else None")).replace(/'/g, "") === "desktop";

    // The tab deletes its own file: deleted in the folder.
    await py("import pathlib\npathlib.Path('/data/web_note.json').unlink()");
    await page.evaluate(() => window.PlexoraPython.flush());
    checks.websiteDeleteReachesFolder = (await inFolder("web_note.json")) === null;

    // The tab deletes a file the other app changed meanwhile: kept.
    await otherAppWrites("desktop_made/notes.txt", "v1");
    await page.evaluate(() => window.PlexoraPython.pull());
    await page.waitForTimeout(20);
    await otherAppWrites("desktop_made/notes.txt", "v2-desktop");
    await py("import pathlib\np = pathlib.Path('/data/desktop_made/notes.txt')\np.unlink() if p.exists() else None");
    await page.evaluate(() => window.PlexoraPython.flush());
    checks.otherAppsChangeNotDeleted = (await inFolder("desktop_made/notes.txt")) === "v2-desktop";
} finally {
    await browser.close();
    server.stop();
}
const ok = Object.values(checks).every(Boolean) && Object.keys(checks).length === 6;
console.log(JSON.stringify({ ok, checks }, null, 1));
process.exit(ok ? 0 : 1);
