// Choosing a file or a folder on the website, in Chrome, end to end: Import
// Sample's Browse opens the explorer (services/localFiles.js), empty on a
// first visit; "Choose folder…" keeps a folder and opens it; an OME-Zarr in it
// is a dataset row and is imported whole; the next Browse opens where the last
// pick came from, with the folder in the sidebar; a folder the browser stops
// granting opens on Reconnect; a file dropped on the dialog is mounted, not
// uploaded. Then the Firefox and Safari shape (no folder handles): Browse asks
// File or Folder under the button, and the folder half takes an OME-Zarr.
//
// The browser's own dialogs are native and no driver can answer them, so
// `showDirectoryPicker` and `showOpenFilePicker` hand back folders and files in
// the test profile's own storage (OPFS test-folders/...), copied there from
// the fixtures; everything after the picker is the product's.
//
//   node tests/browser/website_pick.mjs --site DIR --fixtures DIR [--shots DIR] [--headed]
//
// --fixtures is node_fixtures.py's and smoke_fixture.py's folder (it needs
// smoke.ome.tif and zarr_v3_sharded.ome.zarr). --site is a static build
// (static_build --out): with `--py https://assets.plexoraapp.com/py/<v>/` for
// a released bundle whose Python is this checkout's.
import { readdirSync, statSync } from "node:fs";
import path from "node:path";
import { appFrame, launch, seedBrowserStore, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const site = opt("--site", null);
const fixtures = opt("--fixtures", null);
const shots = opt("--shots", null);
const headed = args.includes("--headed");
if (!site || !fixtures) throw new Error("--site DIR --fixtures DIR");

//: Folders standing in for the browser's dialogs: each dialog hands back the
//: next name queued in `__nextFolders` / `__nextFiles` (paths under
//: OPFS test-folders/), or cancels. `__lapsed` makes every handle ask again.
const STUB = () => {
    window.__nextFolders = [];
    window.__nextFiles = [];
    window.__asked = [];
    const at = async (where) => {
        let dir = await (await navigator.storage.getDirectory()).getDirectoryHandle("test-folders");
        const parts = where.split("/");
        for (const part of parts.slice(0, -1)) dir = await dir.getDirectoryHandle(part);
        return { dir, last: parts[parts.length - 1] };
    };
    const cancelled = () => Object.assign(new Error("cancelled"), { name: "AbortError" });
    window.showDirectoryPicker = async (options = {}) => {
        window.__asked.push({ dialog: "folder", id: options.id || "", startIn: Boolean(options.startIn) });
        const next = window.__nextFolders.shift();
        if (!next) throw cancelled();
        const { dir, last } = await at(next);
        return dir.getDirectoryHandle(last);
    };
    window.showOpenFilePicker = async (options = {}) => {
        window.__asked.push({ dialog: "file", id: options.id || "", startIn: Boolean(options.startIn),
                              types: (options.types || []).length });
        const next = window.__nextFiles.shift();
        if (!next) throw cancelled();
        const { dir, last } = await at(next);
        return [await dir.getFileHandle(last)];
    };
    const query = FileSystemHandle.prototype.queryPermission;
    const request = FileSystemHandle.prototype.requestPermission;
    FileSystemHandle.prototype.queryPermission = function (options) {
        return window.top.__lapsed ? Promise.resolve("prompt") : query.call(this, options);
    };
    FileSystemHandle.prototype.requestPermission = function (options) {
        window.top.__asked.push({ dialog: "permission" });
        window.top.__lapsed = false;
        return request.call(this, options);
    };
};

//: Firefox and Safari, as far as picking goes: no handles at all.
const NO_HANDLES = () => {
    window.showDirectoryPicker = undefined;
    window.showOpenFilePicker = undefined;
};

/** Every file under a fixture, as {path in the test folder, url}. */
function filesOf(local, inside) {
    const out = [];
    const walk = (dir, rel) => {
        for (const name of readdirSync(dir)) {
            const full = path.join(dir, name);
            if (statSync(full).isDirectory()) walk(full, `${rel}/${name}`);
            else out.push({ path: `${rel}/${name}`, url: `/fixtures/${path.relative(fixtures, full).split(path.sep).join("/")}` });
        }
    };
    if (statSync(local).isDirectory()) walk(local, `${inside}/${path.basename(local)}`);
    else out.push({ path: `${inside}/${path.basename(local)}`, url: `/fixtures/${path.basename(local)}` });
    return out;
}

const out = { checks: [], failed: [] };
const check = (label, ok, detail = null) => {
    (ok ? out.checks : out.failed).push(detail === null ? label : `${label}: ${JSON.stringify(detail)}`);
    process.stderr.write(`${ok ? "  ok  " : "FAIL  "}${label}${ok || detail === null ? "" : ` -- ${JSON.stringify(detail)}`}\n`);
};

async function shot(page, name) {
    if (shots) await page.screenshot({ path: path.join(shots, `website_pick_${name}.png`) });
}

const server = await startServer(["--site", site, "--tree", `fixtures=${fixtures}`]);
const origin = `http://127.0.0.1:${server.port}`;

async function openSite({ stub }) {
    const browser = await launch({ headed });
    await seedBrowserStore(browser.context, origin);
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 960 });
    await page.addInitScript(stub);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const requests = [];
    page.on("request", (request) => requests.push(request.url().replace(origin, "")));
    await page.goto(`${origin}/`);
    const frame = await appFrame(page);
    await frame.waitForSelector("#quick_view_go", { timeout: 180000 });
    return { browser, page, frame, errors, requests };
}

/** Import Sample, opened as File > Import Sample opens it, at its pick state. */
async function importDialog(frame) {
    await frame.evaluate(() => window.PlexoraImportSample.open());
    await frame.waitForSelector(".plx-import-pick .plx-import-browse", { state: "visible", timeout: 60000 });
}

/** The names on the import dialog's sample cards, once one is drawn. */
async function cardNames(frame, count, timeout = 120000) {
    const cards = ".plx-import-sample:not(.plx-import-skeleton)";
    await frame.waitForFunction(([n, sel]) => document.querySelectorAll(sel).length >= n,
                                [count, cards], { timeout, polling: 250 });
    return frame.evaluate((sel) => [...document.querySelectorAll(sel)]
        .map((card) => card.innerText.split("\n").filter(Boolean).slice(0, 2).join(" ").trim()), cards);
}

const rowsOf = (frame) => frame.evaluate(() => [...document.querySelectorAll(".plx-explorer-row")]
    .map((row) => ({ name: row.querySelector(".plx-explorer-name")?.textContent,
                     store: row.classList.contains("is-store"), muted: row.classList.contains("is-muted") })));

async function rowNamed(frame, name) {
    await frame.waitForFunction((n) => [...document.querySelectorAll(".plx-explorer-name")]
        .some((el) => el.textContent === n), name, { timeout: 30000 });
    return frame.evaluateHandle((n) => [...document.querySelectorAll(".plx-explorer-row")]
        .find((row) => row.querySelector(".plx-explorer-name")?.textContent === n), name);
}

try {
    // -- Chrome: the explorer over the folders given ---------------------------------------
    {
        const { browser, page, frame, errors, requests } = await openSite({ stub: STUB });
        const files = [...filesOf(path.join(fixtures, "smoke.ome.tif"), "Lab data"),
                       ...filesOf(path.join(fixtures, "zarr_v3_sharded.ome.zarr"), "Lab data")];
        await page.evaluate(async (list) => {
            const top = await (await navigator.storage.getDirectory()).getDirectoryHandle("test-folders", { create: true });
            const write = async (where, bytes) => {
                const parts = where.split("/").filter(Boolean);
                let dir = top;
                for (const part of parts.slice(0, -1)) dir = await dir.getDirectoryHandle(part, { create: true });
                const writable = await (await dir.getFileHandle(parts[parts.length - 1], { create: true })).createWritable();
                await writable.write(bytes);
                await writable.close();
            };
            for (const { path: where, url } of list) await write(where, await (await fetch(url)).arrayBuffer());
            await write("Lab data/Xenium_run/experiment.xenium", "{}");
            await write("Lab data/Xenium_run/transcripts.parquet", "x");
            await write("Lab data/notes.txt", "notes");
        }, files);

        await importDialog(frame);
        await frame.click(".plx-import-pick .plx-import-browse");
        await frame.waitForSelector(".plx-explorer-panel.is-welcome", { state: "visible", timeout: 30000 });
        const first = await frame.evaluate(() => ({
            title: document.querySelector(".plx-explorer-panel-title")?.textContent,
            buttons: [...document.querySelectorAll(".plx-explorer-choose .plx-explorer-system-label")].map((b) => b.textContent),
            system: [...document.querySelectorAll(".plx-explorer-system:not(.plx-explorer-choose)")].every((b) => b.hidden),
        }));
        check("a first visit's Browse opens the explorer on a drop area and the browser's two dialogs",
              first.title === "Drop a file or folder here" && first.buttons.join("|") === "Choose file…|Choose folder…"
              && first.system, first);
        await shot(page, "1_first_visit");

        await page.evaluate(() => { window.__nextFolders.push("Lab data"); });
        await frame.click('.plx-explorer-choose[data-kind="directory"]');
        await rowNamed(frame, "zarr_v3_sharded.ome.zarr");
        const rows = await rowsOf(frame);
        const row = (name) => rows.find((r) => r.name === name) || {};
        check("Choose folder… keeps a plain folder and opens it, its datasets marked",
              row("zarr_v3_sharded.ome.zarr").store && row("Xenium_run").store
              && row("smoke.ome.tif").name && !row("smoke.ome.tif").muted, rows);
        const sidebar = await frame.evaluate(() => [...document.querySelectorAll(".plx-explorer-place-label")].map((l) => l.textContent));
        check("...and the sidebar lists it under Folders, with Add folder…, and no Home",
              sidebar.includes("Lab data") && sidebar.includes("Add folder…") && !sidebar.includes("Home"), sidebar);
        await shot(page, "2_folder_given");

        const zarr = await rowNamed(frame, "zarr_v3_sharded.ome.zarr");
        await zarr.click({ position: { x: 60, y: 12 } });
        await frame.click(".plx-explorer-primary");
        await frame.waitForSelector(".plx-explorer-window", { state: "detached", timeout: 30000 });
        const cards = await cardNames(frame, 1);
        check("an OME-Zarr chosen in it is imported whole: its sample card is drawn", cards.length >= 1, cards);
        const picks = await page.evaluate(() => window.PlexoraPicks.list());
        check("...mounted where it lies and remembered with where it came from",
              picks.some((p) => p.kind === "directory" && p.name === "zarr_v3_sharded.ome.zarr" && /^\/local\//.test(p.from)),
              picks.map((p) => ({ kind: p.kind, name: p.name, from: p.from })));
        await shot(page, "3_zarr_card");

        // A card's Browse opens beside its image: the folder the OME-Zarr was
        // chosen in, not inside the store, and not where the explorer was
        // last left (Xenium_run, below).
        await frame.click('.plx-import-card-actions button:has-text("Add segmentation mask")');
        await frame.waitForSelector(".plx-import-compact-browse", { state: "visible", timeout: 30000 });
        await frame.click(".plx-import-compact-browse");
        await rowNamed(frame, "Xenium_run");
        await frame.evaluate(() => [...document.querySelectorAll(".plx-explorer-row")]
            .find((r) => r.querySelector(".plx-explorer-name")?.textContent === "Xenium_run")
            .querySelector(".plx-explorer-enter").click());
        await rowNamed(frame, "experiment.xenium");
        await frame.click(".plx-explorer-cancel");
        await frame.waitForSelector(".plx-explorer-window", { state: "detached", timeout: 30000 });
        await frame.click(".plx-import-compact-browse");
        await rowNamed(frame, "zarr_v3_sharded.ome.zarr");
        const beside = await frame.evaluate(() => [...document.querySelectorAll(".plx-explorer-crumb")]
            .map((c) => c.textContent));
        check("a card's Browse for its mask opens beside its image, not where the explorer was left",
              beside[beside.length - 1] === "Lab data", beside);
        await frame.click(".plx-explorer-cancel");
        await frame.waitForSelector(".plx-explorer-window", { state: "detached", timeout: 30000 });

        // The next Browse: where the last pick was chosen, with the folder in Recent.
        await frame.evaluate(() => window.PlexoraImportSample.close());
        await frame.waitForTimeout(300);
        await importDialog(frame);
        await frame.click(".plx-import-pick .plx-import-browse");
        await rowNamed(frame, "smoke.ome.tif");
        const again = await frame.evaluate(() => ({
            crumbs: [...document.querySelectorAll(".plx-explorer-crumb")].map((c) => c.textContent),
            places: [...document.querySelectorAll(".plx-explorer-place-label")].map((l) => l.textContent),
        }));
        check("the next Browse opens in the folder the last pick came from, Recent naming it",
              again.crumbs.includes("Lab data") && again.places.filter((p) => p === "Lab data").length >= 2, again);
        await shot(page, "4_returning");

        // Reconnect: the browser stops granting the folder.
        await frame.click(".plx-explorer-cancel");
        await page.evaluate(() => { window.__lapsed = true; });
        await frame.click(".plx-import-pick .plx-import-browse");
        await frame.waitForSelector(".plx-explorer-panel.is-reconnect", { state: "visible", timeout: 30000 });
        const lapsed = await frame.evaluate(() => ({
            text: document.querySelector(".plx-explorer-panel-title")?.textContent,
            caption: document.querySelector(".plx-explorer-place.is-lapsed .plx-explorer-place-caption")?.textContent,
        }));
        check("a folder the browser stopped granting opens on Reconnect, the sidebar marking it",
              /permission/.test(lapsed.text || "") && lapsed.caption === "Reconnect", lapsed);
        await shot(page, "5_reconnect");
        await frame.click(".plx-explorer-panel.is-reconnect .plx-button-primary");
        await rowNamed(frame, "smoke.ome.tif");
        const asked = await page.evaluate(() => window.__asked.filter((a) => a.dialog === "permission").length);
        check("...and Reconnect asks the browser from the click, then lists it", asked === 1, asked);

        // A file chosen by double-click: the second sample.
        const tif = await rowNamed(frame, "smoke.ome.tif");
        await tif.dblclick({ position: { x: 60, y: 12 } });
        await frame.waitForSelector(".plx-explorer-window", { state: "detached", timeout: 30000 });
        const two = await cardNames(frame, 1).catch(() => []);
        check("a file double-clicked is chosen and read for the import", two.some((c) => /smoke/.test(c)), two);
        await shot(page, "6_two_samples");

        // A file dropped on the dialog: mounted, not uploaded.
        await frame.evaluate(() => window.PlexoraImportSample.close());
        await frame.waitForTimeout(300);
        await importDialog(frame);
        const before = requests.length;
        await frame.evaluate(async () => {
            const bytes = await (await fetch("/fixtures/smoke.ome.tif")).arrayBuffer();
            const transfer = new DataTransfer();
            transfer.items.add(new File([bytes], "dropped.ome.tif", { type: "image/tiff" }));
            const target = document.querySelector(".plx-import-pick");
            target.dispatchEvent(new DragEvent("dragover", { dataTransfer: transfer, bubbles: true, cancelable: true }));
            target.dispatchEvent(new DragEvent("drop", { dataTransfer: transfer, bubbles: true, cancelable: true }));
        });
        const dropped = await cardNames(frame, 1).catch(() => []);
        const uploads = requests.slice(before).filter((url) => url.includes("upload_data_file"));
        check("a file dropped on the dialog is mounted where it lies, never uploaded",
              dropped.length >= 1 && uploads.length === 0, { dropped, uploads });
        check("no page errors on the way", errors.length === 0, errors.slice(0, 5));
        await browser.close();
    }

    // -- Firefox and Safari's shape: no folder handles -----------------------------------------
    {
        const { browser, page, frame, errors } = await openSite({ stub: NO_HANDLES });
        await importDialog(frame);
        await frame.click(".plx-import-pick .plx-import-browse");
        await frame.waitForSelector(".browse-kind-split.is-floating", { state: "visible", timeout: 30000 });
        const menu = await frame.evaluate(() => [...document.querySelectorAll(".browse-kind-split.is-floating .browse-kind-half")]
            .map((half) => half.innerText.replace(/\s+/g, " ").trim()));
        check("with no folder handles, Browse asks File or Folder under the button, a sample's examples on it",
              menu.length === 2 && /^File/.test(menu[0]) && /^Folder/.test(menu[1]) && /\.zarr/.test(menu[1]), menu);
        await shot(page, "7_firefox_menu");
        const chooser = page.waitForEvent("filechooser", { timeout: 30000 });
        await frame.click(".browse-kind-split.is-floating .browse-kind-half.is-directory");
        await (await chooser).setFiles(path.join(fixtures, "zarr_v3_sharded.ome.zarr"));
        const cards = await cardNames(frame, 1).catch(() => []);
        check("...and the folder half takes an OME-Zarr folder into the import", cards.length >= 1, cards);
        check("no page errors on the way (no handles)", errors.length === 0, errors.slice(0, 5));
        await shot(page, "8_firefox_zarr");
        await browser.close();
    }
} finally {
    server.stop();
}

console.log(JSON.stringify(out, null, 1));
if (out.failed.length) process.exitCode = 1;
