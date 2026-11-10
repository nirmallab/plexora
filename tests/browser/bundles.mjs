// A project as one `.plexora` file on the website, both ways, through the
// dialogs (views/projectExport.js, projectImport.js) and the website's
// transport (services/bundleWeb.js, workers/bundleWorker.js):
//   1. a project made from a picked image, with gates, exported -- the Save
//      dialog stood in by a file in the profile's own storage (OPFS);
//   2. a fresh browser profile imports that file (its file input);
//   3. the imported project has its gates;
//   4. its image, which stayed on the first "computer", is missing, and
//      opening the project asks for it ("Find them…", or "Locate file…" where
//      the browser has no folder picker).
// The file is checked on the way: its manifest is the first member, stored,
// and every member's CRC and sha256 read back (the worker's own reader, in Node).
//
//   node tests/browser/bundles.mjs --file <tif>
// Needs the static build (`plexora/client/dist/static`) and a Python bundle
// built from this checkout (tools/browser_build/build.py; serve.py `--py`).
// Prints JSON with `ok`.
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { appFrame, launch, seedBrowserStore, startServer, REPO } from "./lib.mjs";

const args = process.argv.slice(2);
const file = args[args.indexOf("--file") + 1];
const codec = await import(pathToFileURL(path.join(REPO, "plexora", "client", "src", "js", "workers", "bundleWorker.js")).href);
const GATES = '{"gates": {"CD3": 120}}';

//: A project made from a picked image, as a user makes one (bundle.mjs's way).
async function makeProject(page, origin) {
    let frame = await appFrame(page);
    await frame.waitForSelector("#quick_view_go", { timeout: 120000 });
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
    return frame.evaluate(() => decodeURIComponent(location.pathname.slice(1)));
}

const python = (lines) => lines.join("\n");

const server = await startServer(["--site", `${REPO}/plexora/client/dist/static`]);
const scratch = mkdtempSync(path.join(tmpdir(), "pxweb-bundles-"));
const out = {};
let first = await launch({});
let second = null;
try {
    // -- 1. export ------------------------------------------------------------------
    const origin = `http://127.0.0.1:${server.port}`;
    await seedBrowserStore(first.context, origin);
    const page = await first.context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    await page.goto(`${origin}/?pydebug`);
    out.project = await makeProject(page, origin);
    out.gatesSet = await page.evaluate((code) => window.PlexoraPython.run(code), python([
        "from plexora.api.store import store",
        `store(${JSON.stringify(out.project)}, "gating").put_state(${JSON.stringify(GATES)}.encode())`,
        `store(${JSON.stringify(out.project)}, "gating").get_state().decode()`,
    ])).catch((error) => String(error));
    await page.evaluate(() => window.PlexoraPython.flush());

    let frame = await appFrame(page);
    out.transport = await frame.evaluate(() => window.PlexoraBundleTransport && window.PlexoraBundleTransport.kind);
    // The Save dialog: a file in this profile's storage.
    await frame.evaluate(() => {
        window.showSaveFilePicker = async (options) => {
            window.__saveAsked = options;
            const root = await navigator.storage.getDirectory();
            const dir = await root.getDirectoryHandle("saved-here", { create: true });
            return dir.getFileHandle(options.suggestedName || "project.plexora", { create: true });
        };
    });
    await frame.evaluate((name) => window.PlexoraProjectExport.open({ names: [name] }), out.project);
    await frame.waitForSelector(".plx-bundle-export [data-act='export']:not([disabled])", { timeout: 60000 });
    await frame.click(".plx-bundle-export [data-act='export']");
    out.exported = await frame.waitForSelector(".plx-bundle-export .plx-bundle-done", { timeout: 300000 })
        .then(() => true, () => false);
    out.saveAsked = await frame.evaluate(() => window.__saveAsked && window.__saveAsked.suggestedName);
    // The file, out of the profile, for the second one.
    const encoded = await frame.evaluate(async (name) => {
        const root = await navigator.storage.getDirectory();
        const dir = await root.getDirectoryHandle("saved-here");
        const bytes = new Uint8Array(await (await (await dir.getFileHandle(name)).getFile()).arrayBuffer());
        let text = "";
        for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
        return btoa(text);
    }, out.saveAsked);
    const bundlePath = path.join(scratch, out.saveAsked);
    writeFileSync(bundlePath, Buffer.from(encoded, "base64"));
    const blob = new Blob([Buffer.from(encoded, "base64")]);
    const entries = await codec.readCentral(blob);
    out.bundle = { kb: Math.round(blob.size / 1024), members: entries.length, first: entries[0].name,
                   firstStored: entries[0].method === codec.STORED };
    const manifest = JSON.parse(new TextDecoder().decode(await codec.readMember(blob, entries[0])));
    const records = [];
    for (const project of manifest.projects) records.push(project.entry, ...project.files);
    let matched = 0;
    for (const record of records) {
        const entry = entries.find((item) => item.name === record.path);
        await codec.pumpMember(blob, entry, () => {}, { sha256: record.sha256 });
        matched += 1;
    }
    out.bundle.checked = matched;
    out.bundle.revision = manifest.projects[0].revision;
    out.bundle.imagesIncluded = (manifest.media || []).length;
    out.errorsFirst = errors.slice(0, 10);
    await first.close();
    first = null;

    // -- 2. a fresh profile imports it ------------------------------------------------
    second = await launch({});
    await seedBrowserStore(second.context, origin);
    const other = await second.context.newPage();
    const errorsSecond = [];
    other.on("pageerror", (error) => errorsSecond.push(String(error)));
    await other.goto(`${origin}/?pydebug`);
    frame = await appFrame(other);
    await other.evaluate(() => { window.showOpenFilePicker = undefined; });
    await frame.evaluate(() => window.PlexoraProjectImport.open({}));
    const chooser = other.waitForEvent("filechooser", { timeout: 30000 });
    await frame.click(".plx-bundle-import [data-act='pick']");
    await (await chooser).setFiles(bundlePath);
    await frame.waitForSelector(".plx-bundle-import [data-act='import']:not([disabled])", { timeout: 120000 });
    out.previewed = await frame.evaluate(() => [...document.querySelectorAll(".plx-bundle-import .plx-bundle-badge")]
        .map((badge) => badge.textContent.trim()));
    await frame.click(".plx-bundle-import [data-act='import']");
    out.imported = await frame.waitForSelector(".plx-bundle-import .plx-bundle-results", { timeout: 300000 })
        .then(() => true, () => false);
    out.target = await frame.evaluate(() => {
        const open = document.querySelector(".plx-bundle-import .plx-bundle-open");
        return open ? open.dataset.target : null;
    });
    out.missingOffered = await frame.evaluate(() => Boolean(document.querySelector(".plx-bundle-import [data-act='find']")));

    // -- 3. it has its gates ------------------------------------------------------------
    out.gates = await other.evaluate((code) => window.PlexoraPython.run(code), python([
        "from plexora.api.store import store",
        `(store(${JSON.stringify(out.target)}, "gating").get_state() or b"").decode()`,
    ])).catch((error) => String(error));

    // -- 4. its image is asked for ---------------------------------------------------------
    const folders = await other.evaluate(() => typeof window.showDirectoryPicker === "function");
    await other.goto(`${origin}/${encodeURIComponent(out.target)}?pydebug`);
    out.findAsked = await other.waitForFunction((label) => {
        const frameEl = document.getElementById("plexora-app");
        const inner = frameEl && frameEl.contentDocument;
        return Boolean(inner && [...inner.querySelectorAll(".plexora-boot-actions button")]
            .find((b) => b.textContent.trim().startsWith(label)));
    }, folders ? "Find them" : "Locate file", { timeout: 120000, polling: 250 }).then(() => true, () => false);
    out.errorsSecond = errorsSecond.slice(0, 10);
} finally {
    if (first) await first.close();
    if (second) await second.close();
    server.stop();
    rmSync(scratch, { recursive: true, force: true });
}
out.ok = Boolean(out.transport === "website" && out.exported && out.bundle && out.bundle.first === codec.MANIFEST
                 && out.bundle.firstStored && out.bundle.checked > 0 && out.bundle.revision
                 && out.imported && out.target && out.gates === GATES && out.findAsked);
console.log(JSON.stringify(out, null, 1));
process.exit(out.ok ? 0 : 1);
