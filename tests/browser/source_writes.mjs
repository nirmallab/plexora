// Saving into the user's own files on the website (plan Phase 4, source
// write-back; plexora/server/utils/source_writes.py, services/sourceWrites.js),
// held to the bytes the desktop writes.
//
// Three projects, each an image and a cell table picked on the website and
// imported (/import/inspect, /import/sample), then saved into as a user saves:
//
// - CSV, picked with a handle. Settings > Data > "Save into your files" off:
//   the ROI panel's cell columns (/plugins/roi/api/map_to_cells) are refused
//   and the file is untouched. The box ticked in Settings: the same save from a
//   click writes the file through its handle, the browser's prompt asked once,
//   from that click.
// - .h5ad, picked with a handle: Save Gates to Source File
//   (/plugins/gating/save_gates_to_anndata) from no click -- the page asks
//   "Save into cells.h5ad?" first, and its Save is the click the browser asks from.
// - CSV picked through `<input type=file>` (no handle): the save comes back
//   as a download, with one notice.
//
// The picker is stood in by files in the profile's own storage (OPFS
// "picks/"), writable as a granted file is; Chrome's write prompt is stood in
// on the handles' prototype: "prompt" until asked, and asked only with a
// user activation, as Chrome refuses one without (recorded in __prompts).
// Every saved file must be what Python staged (its sha256 in
// source_writes.delivered), and the desktop's routes on copies of the same
// files (tests/browser/source_writes_desktop.py) must have written the same
// bytes (the .h5ad: the same gates and data where HDF5's own bytes differ).
// Prints JSON {ok, checks}; exits 1 when a check fails.
//
//   node tests/browser/source_writes.mjs --image IMG --image2 IMG --image3 IMG
//        --csv CELLS.csv --h5ad CELLS.h5ad --steps STEPS.json [--headed]
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { PYTHON, REPO, appFrame, launch, seedBrowserStore, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback = null) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const [image, image2, image3, csv, h5ad, stepsFile] = ["--image", "--image2", "--image3", "--csv", "--h5ad",
                                                       "--steps"].map((name) => opt(name));
if (!image || !image2 || !image3 || !csv || !h5ad || !stepsFile) {
    throw new Error("--image --image2 --image3 --csv --h5ad --steps");
}
const steps = JSON.parse(readFileSync(stepsFile, "utf-8"));
const site = opt("--site", path.join(REPO, "plexora", "client", "dist", "static"));
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");

const out = { checks: {} };
const check = (name, ok, detail) => { out.checks[name] = { ok: Boolean(ok), ...(detail === undefined ? {} : { detail }) }; };

// The desktop: this checkout's app in CPython, on copies, a data root of its own.
const work = mkdtempSync(path.join(tmpdir(), "pxwb-desk-"));
const deskRun = spawnSync(PYTHON, [path.join(REPO, "tests", "browser", "source_writes_desktop.py"),
                                   "--steps", stepsFile, "--out", path.join(work, "files"),
                                   "--image", image, "--image2", image2, "--csv", csv, "--h5ad", h5ad],
                          { cwd: REPO, encoding: "utf8", maxBuffer: 64 << 20,
                            env: { ...process.env, PYTHONPATH: REPO, PLEXORA_DATA_PATH: path.join(work, "root"),
                                   PLEXORA_TELEMETRY: "off" } });
if (deskRun.status !== 0) throw new Error(`desktop: ${deskRun.stderr.slice(-3000)}`);
const desk = JSON.parse(deskRun.stdout.trim().split("\n").pop());
out.desktop = { csv: { sha256: desk.csv.sha256, mapped: desk.csv.mapped.success },
                h5ad: { sha256: desk.h5ad.sha256, saved: desk.h5ad.saved.success } };

const STUB = () => {
    // Chrome's write prompt, stood in: a handle may write once asked, and it
    // is asked only from a user activation (Chrome throws without one).
    window.__prompts = [];
    window.__refusedWithoutClick = 0;
    const granted = new Set();
    const proto = window.FileSystemHandle && window.FileSystemHandle.prototype;
    if (proto) {
        proto.queryPermission = async function ({ mode } = {}) {
            return mode === "readwrite" && !granted.has(this.name) ? "prompt" : "granted";
        };
        proto.requestPermission = async function ({ mode } = {}) {
            if (mode !== "readwrite" || granted.has(this.name)) return "granted";
            // Chrome's rule: a click within its activation's lifespan (5 s). The
            // headless build here keeps `isActive` set long after, so the driver
            // stamps each click it makes (`__lastClick`) and that is held to 5 s.
            const fresh = Date.now() - (window.__lastClick || 0) <= 5000;
            if (!navigator.userActivation || !navigator.userActivation.isActive || !fresh) {
                window.__refusedWithoutClick += 1;
                throw new DOMException("User activation is required to request permissions.", "SecurityError");
            }
            window.__prompts.push(this.name);
            granted.add(this.name);
            return "granted";
        };
    }
    // The file picker: the files named in __nextPick, from the profile's storage.
    window.showOpenFilePicker = async () => {
        const root = await navigator.storage.getDirectory();
        const dir = await root.getDirectoryHandle("picks", { create: true });
        return Promise.all((window.__nextPick || []).map((name) => dir.getFileHandle(name)));
    };
};

const server = await startServer(["--site", site, "--tree", `fixture=${path.dirname(image)}`]);
const browser = await launch({ headed: args.includes("--headed") });
const errors = [];
const downloads = [];
try {
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    page.on("pageerror", (error) => errors.push(`pageerror: ${error}`.slice(0, 300)));
    page.on("console", (message) => {
        if (message.type() === "error" || /\[python\]/.test(message.text())) errors.push(message.text().slice(0, 600));
    });
    page.on("download", (download) => downloads.push(download));
    await page.addInitScript(STUB);
    const origin = `http://127.0.0.1:${server.port}`;
    await seedBrowserStore(browser.context, origin);
    await page.goto(`${origin}/?pydebug`);
    let frame = await appFrame(page);

    // The files the picker will hand over, in the profile's storage.
    for (const file of [image, image2, csv, h5ad]) {
        await page.evaluate(async ([name, url]) => {
            const root = await navigator.storage.getDirectory();
            const dir = await root.getDirectoryHandle("picks", { create: true });
            const writable = await (await dir.getFileHandle(name, { create: true })).createWritable();
            await writable.write(await (await fetch(url)).blob());
            await writable.close();
        }, [path.basename(file), `/fixture/${path.basename(file)}`]);
    }
    const stored = (name) => page.evaluate(async (fileName) => {
        const root = await navigator.storage.getDirectory();
        const file = await (await (await root.getDirectoryHandle("picks")).getFileHandle(fileName)).getFile();
        return Array.from(new Uint8Array(await file.arrayBuffer()));
    }, name).then((bytes) => Buffer.from(bytes));
    const delivered = async () => JSON.parse(await page.evaluate(() => window.PlexoraPython.run(
        "import json\nfrom plexora.server.utils import source_writes\njson.dumps(source_writes.delivered)")));
    const outcomes = () => page.evaluate(() => window.PlexoraSourceWrites.outcomes());
    //: A click the driver is about to make, stamped where the prompt's stand-in reads it.
    const clicked = () => page.evaluate(() => { window.__lastClick = Date.now(); });

    const post = (route, body) => frame.evaluate(async ([r, b]) => {
        const response = await fetch(r, { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify(b) });
        return { status: response.status, body: await response.json().catch(() => null) };
    }, [route, body]);
    const get = (route) => frame.evaluate(async (r) => (await fetch(r)).json(), route);

    // A project from picked paths, opened, its table loaded.
    async function project(paths) {
        const inspected = await post("import/inspect", { paths });
        if (inspected.status !== 200) throw new Error(`inspect: ${JSON.stringify(inspected.body).slice(0, 500)}`);
        const imported = await post("import/sample", { paths, answers: {} });
        const name = imported.body && imported.body.name;
        if (!name) throw new Error(`import: ${JSON.stringify(imported.body).slice(0, 500)}`);
        await frame.evaluate((n) => { location.href = `/${n}`; }, name);
        frame = await appFrame(page);
        let status = null;
        for (let i = 0; i < 1200; i += 1) {
            status = await get(`get_table_status?datasource=${encodeURIComponent(name)}`);
            if (status.status === "ready" || status.error) break;
            await page.waitForTimeout(250);
        }
        if (!status || status.status !== "ready") throw new Error(`table: ${JSON.stringify(status)}`);
        return name;
    }
    async function regions(name) {
        const { revision } = await get(`plugins/roi/api/state?datasource=${encodeURIComponent(name)}`);
        const made = await post("plugins/roi/api/import", { datasource: name, document: steps.regions,
                                                             base_revision: revision });
        if (!made.body || !made.body.success) throw new Error(`regions: ${JSON.stringify(made.body).slice(0, 500)}`);
    }

    // -- 1. CSV with a handle ---------------------------------------------------------------
    const csvName = path.basename(csv);
    const csvBefore = await stored(csvName);
    const csvPaths = await page.evaluate(async (names) => {
        window.__nextPick = names;
        return window.PlexoraPicks.pick({ multiple: true });
    }, [path.basename(image), csvName]);
    const csvProject = await project(csvPaths);
    await regions(csvProject);
    const refused = await post("plugins/roi/api/map_to_cells", { datasource: csvProject, name: "rois" });
    if (!refused.body) {
        // Not an answer the panel can show: Python's own account of why.
        out.why = await page.evaluate((n) => window.PlexoraPython.run([
            "import traceback, plexora",
            "app = plexora.app",
            "app.config['PROPAGATE_EXCEPTIONS'] = True",
            "try:",
            "    answer = app.test_client().post('/plugins/roi/api/map_to_cells', json={'datasource': "
                + JSON.stringify(n) + ", 'name': 'rois'})",
            "    out = answer.get_data(as_text=True)[:2000]",
            "except BaseException:",
            "    out = traceback.format_exc()",
            "finally:",
            "    app.config['PROPAGATE_EXCEPTIONS'] = None",
            "out"].join("\n")), csvProject).catch((error) => String(error));
    }
    check("off: refused, file untouched",
          !(refused.body && refused.body.success) && /Settings › Data/.test(JSON.stringify(refused.body))
          && sha(await stored(csvName)) === sha(csvBefore) && (await outcomes()).length === 0,
          { answer: refused.body });

    // The setting, ticked where the user ticks it: Settings > Data.
    await frame.evaluate(() => { location.href = "/settings#data"; });
    frame = await appFrame(page);
    await frame.waitForSelector("#settings_web_writes:not([hidden]) #settings_web_writes_on", { timeout: 30000 });
    out.settingBefore = await frame.evaluate(() => document.getElementById("settings_web_writes_on").checked);
    await clicked();
    await frame.click("#settings_web_writes_on");
    out.setting = await page.evaluate(() => window.PlexoraSourceWrites.allowed());
    out.settingSays = await frame.evaluate(() => document.getElementById("settings_web_writes_meta").textContent);
    await frame.evaluate((n) => { location.href = `/${n}`; }, csvProject);
    frame = await appFrame(page);

    // The save, from a click (the ROI panel's own button is a click too).
    await frame.evaluate((n) => {
        const button = document.createElement("button");
        button.id = "wb-save";
        button.textContent = "Save";
        button.style.cssText = "position:fixed;left:8px;top:8px;z-index:100000";
        button.onclick = async () => {
            const response = await fetch("plugins/roi/api/map_to_cells", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ datasource: n, name: "rois" }) });
            const text = await response.text();
            try {
                window.__wbAnswer = JSON.parse(text);
            } catch (error) {
                window.__wbAnswer = { success: false, status: response.status, text: text.slice(0, 3000) };
            }
        };
        document.body.append(button);
    }, csvProject);
    await clicked();
    await frame.click("#wb-save");
    // The answer -- or, where the click no longer counted by the time the
    // browser was asked, the page's question (which this check fails on).
    await frame.waitForFunction(() => window.__wbAnswer || document.querySelector(".plx-confirm"),
                                null, { timeout: 120000 }).catch(async (error) => {
        const state = { prompts: await page.evaluate(() => window.__prompts.slice()),
                        refused: await page.evaluate(() => window.__refusedWithoutClick),
                        worker: await page.evaluate(() => window.PlexoraPython.state()),
                        recent: await page.evaluate(() => window.PlexoraPython.recent().slice(-4)),
                        outcomes: await outcomes(), errors: errors.slice(-8) };
        throw new Error(`the click's save did not finish: ${JSON.stringify(state).slice(0, 4000)}`);
    });
    if (await frame.evaluate(() => !window.__wbAnswer)) {
        out.csvClickState = { prompts: await page.evaluate(() => window.__prompts.slice()),
                              refused: await page.evaluate(() => window.__refusedWithoutClick),
                              activation: await page.evaluate(() => navigator.userActivation
                                  && { active: navigator.userActivation.isActive,
                                       been: navigator.userActivation.hasBeenActive }),
                              python: await page.evaluate(() => window.PlexoraPython.recent().slice(-3)) };
        await frame.locator(".plx-confirm .plx-button", { hasText: "Save" }).last().click();
        await frame.waitForFunction(() => window.__wbAnswer, null, { timeout: 120000 });
    }
    const csvAnswer = await frame.evaluate(() => window.__wbAnswer);
    const asked = await frame.evaluate(() => Boolean(document.querySelector(".plx-confirm")));
    const csvAfter = await stored(csvName);
    const csvOutcome = (await outcomes()).at(-1);
    const prompts = await page.evaluate(() => window.__prompts.slice());
    const csvStaged = (await delivered()).at(-1);
    check("csv: replaced from the click that saved",
          csvAnswer.success && csvOutcome && csvOutcome.how === "replaced" && !asked,
          { answer: csvAnswer, outcome: csvOutcome, asked });
    check("csv: the browser asked once", prompts.filter((n) => n === csvName).length === 1, prompts);
    check("csv: bytes are what Python staged",
          csvStaged && csvStaged.sha256 === sha(csvAfter) && csvStaged.bytes === csvAfter.length,
          { staged: csvStaged, saved: sha(csvAfter) });
    check("csv: bytes are the desktop's", sha(csvAfter) === desk.csv.sha256 && sha(csvAfter) !== sha(csvBefore),
          { website: sha(csvAfter), desktop: desk.csv.sha256 });
    // Python reads, at the picked path, the file it wrote.
    const reread = await page.evaluate((p) => window.PlexoraPython.run(
        `import hashlib\nhashlib.sha256(open(${JSON.stringify(p)}, 'rb').read()).hexdigest()`),
        csvPaths.find((p) => p.endsWith(csvName)));
    out.reread = reread;

    // -- 2. .h5ad with a handle, from no click: the page asks first --------------------------
    const h5Name = path.basename(h5ad);
    const h5Before = await stored(h5Name);
    const h5Paths = await page.evaluate(async (names) => {
        window.__nextPick = names;
        return window.PlexoraPicks.pick({ multiple: true });
    }, [path.basename(image2), h5Name]);
    const h5Project = await project(h5Paths);
    const listed = await post("plugins/gating/save_gating_list",
                              { datasource: h5Project, filter: steps.gates, channels: steps.gates });
    if (!listed.body || !listed.body.success) throw new Error(`gates: ${JSON.stringify(listed.body)}`);
    // No click for the browser to ask from: the last one is over 5 s old.
    const sinceClick = await page.evaluate(() => Date.now() - (window.__lastClick || 0));
    if (sinceClick < 6000) await page.waitForTimeout(6000 - sinceClick);
    const refusedBefore = await page.evaluate(() => window.__refusedWithoutClick);
    const saving = frame.evaluate(async (n) => {
        const response = await fetch("plugins/gating/save_gates_to_anndata", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ datasource: n }) });
        return response.json();
    }, h5Project);
    const question = frame.locator(".plx-confirm .plx-button", { hasText: "Save" }).last();
    const settled = saving.then((answer) => ({ answer }), (error) => ({ error: String(error) }));
    const first = await Promise.race([question.waitFor({ timeout: 120000 }).then(() => null), settled]);
    if (first) {
        throw new Error(`no question before the save ended: ${JSON.stringify({
            first, outcomes: await outcomes(), prompts: await page.evaluate(() => window.__prompts.slice()),
            refused: await page.evaluate(() => window.__refusedWithoutClick) }).slice(0, 3000)}`);
    }
    out.question = await frame.evaluate(() => document.querySelector(".plx-confirm .plx-dialog-title")?.textContent);
    await clicked();
    await question.click();
    const h5Answer = await saving;
    const h5After = await stored(h5Name);
    const h5Outcome = (await outcomes()).at(-1);
    const h5Staged = (await delivered()).at(-1);
    const prompts2 = await page.evaluate(() => window.__prompts.slice());
    check("h5ad: asked first, then replaced",
          h5Answer.success && h5Outcome && h5Outcome.how === "replaced"
          && out.question === `Save into ${h5Name}?`
          && (await page.evaluate(() => window.__refusedWithoutClick)) > refusedBefore
          && prompts2.filter((n) => n === h5Name).length === 1,
          { answer: h5Answer, outcome: h5Outcome, question: out.question, prompts: prompts2 });
    check("h5ad: bytes are what Python staged",
          h5Staged && h5Staged.sha256 === sha(h5After) && sha(h5After) !== sha(h5Before),
          { staged: h5Staged, saved: sha(h5After) });
    // The desktop's file beside the website's: the same bytes, or (HDF5
    // libraries differ between Pyodide and CPython) the same contents.
    const h5Website = path.join(work, "website-cells.h5ad");
    writeFileSync(h5Website, h5After);
    const compared = spawnSync(PYTHON, [path.join(REPO, "tests", "browser", "source_writes_desktop.py"),
                                        "--compare", h5Website, desk.h5ad.path],
                               { cwd: REPO, encoding: "utf8", env: { ...process.env, PYTHONPATH: REPO } });
    const same = compared.status === 0 ? JSON.parse(compared.stdout.trim().split("\n").pop()) : { error: compared.stderr.slice(-1500) };
    out.h5adCompared = same;
    check("h5ad: what the desktop writes", same.same_bytes || same.same_contents, same);

    // -- 3. CSV with no handle: a download ---------------------------------------------------
    await page.evaluate(() => {
        const input = document.createElement("input");
        input.type = "file"; input.multiple = true; input.id = "wb-pick"; input.hidden = true;
        document.body.append(input);
    });
    await page.setInputFiles("#wb-pick", [image3, csv]);
    const dlPaths = await page.evaluate(async () => {
        const files = [...document.getElementById("wb-pick").files];
        const mounted = await window.PlexoraPython.mount("/picked/wbinput", files);
        mounted.forEach((p, i) => window.PlexoraPicks.adopt(p, files[i]));
        return mounted;
    });
    const dlProject = await project(dlPaths);
    await regions(dlProject);
    const before = downloads.length;
    const dlAnswer = await post("plugins/roi/api/map_to_cells", { datasource: dlProject, name: "rois" });
    for (let i = 0; i < 100 && downloads.length === before; i += 1) await page.waitForTimeout(100);
    const download = downloads[before];
    const dlBytes = download ? readFileSync(await download.path()) : Buffer.alloc(0);
    const toast = await frame.evaluate(() => [...document.querySelectorAll(".plx-toast")]
        .map((node) => node.textContent.replace(/\s+/g, " ").trim()));
    const dlOutcome = (await outcomes()).at(-1);
    check("download: no handle, one notice",
          dlAnswer.body && dlAnswer.body.success && dlOutcome && dlOutcome.how === "downloaded"
          && download && download.suggestedFilename() === csvName
          && toast.length === 1 && /downloaded/.test(toast[0]),
          { answer: dlAnswer.body, outcome: dlOutcome, file: download && download.suggestedFilename(), toast });
    check("download: bytes are the desktop's", sha(dlBytes) === desk.csv.sha256,
          { website: sha(dlBytes), desktop: desk.csv.sha256 });
    check("Python reads what it wrote", reread === sha(csvAfter), { reread, saved: sha(csvAfter) });
} catch (error) {
    out.failed = String(error && error.stack || error).slice(0, 4000);
} finally {
    out.errors = errors.slice(0, 20);
    await browser.close();
    server.stop();
    try { rmSync(work, { recursive: true, force: true }); } catch { /* Windows may hold it */ }
}
out.ok = Object.values(out.checks).every((c) => c.ok);
console.log(JSON.stringify(out, null, 1));
process.exit(out.ok ? 0 : 1);
