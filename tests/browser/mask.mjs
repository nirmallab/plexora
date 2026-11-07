// A flat segmentation mask on the website: an image picked from the home
// card, its mask added with Import sample's "Add segmentation mask", both
// made into pyramids by the page (services/browserPyramid.js), then the
// viewer draws the image and the mask, and the mask's tiles come from the
// browser node rather than Python. As in import.mjs the native pickers are
// hidden, so each pick goes through <input type=file>.
//
//   node tests/browser/mask.mjs --file IMAGE --mask MASK [--shots DIR] [--headed]
//                               [--timeout S] [--trace] [--no-reopen]
//
// Then the project's address is opened again (unless --no-reopen): nothing
// may be built again, and the mask's tiles are still the page's.
//
// Prints JSON: when each step finished after the image was picked (`at`),
// each page build (`builds`: kind, where, began/ended after the pick), the
// tiles answered by who (`served`, as tests/browser/project.mjs counts them;
// `reopen.served` for the second visit), what the viewer made of the mask
// (`viewerOnReady`: drawn unasked when it lands while the viewer is open),
// Python's slow answers and long steps, console errors, failed requests and
// the traceback of any route that answered 500. --trace samples the Python
// worker every 300 ms until the mask is ready (busy, idling, queued, or
// `held` when it did not answer within 2 s).
import path from "node:path";
import { REPO, appFrame, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const file = opt("--file", null);
const mask = opt("--mask", null);
const shots = opt("--shots", null);
const site = opt("--site", path.join(REPO, "plexora", "client", "dist", "static"));
const timeoutS = Number(opt("--timeout", "900"));
if (!file || !mask) throw new Error("--file IMAGE --mask MASK");

const server = await startServer(["--site", site]);
const browser = await launch({ headed: args.includes("--headed") });
const served = {};
const out = { at: {}, served };
const errors = [];
const failed = [];
let picked = 0;
const mark = (name) => { out.at[name] = Date.now() - picked; };
try {
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    page.on("pageerror", (error) => errors.push(`pageerror: ${error}`.slice(0, 300)));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text().slice(0, 300)); });
    page.on("response", (response) => {
        const tile = response.url().match(/\/generated\/data\/[^/]+\/([^/]+)\/\d+\/\d+_\d+\.png(?:\?(.*))?$/);
        if (tile && response.status() === 200) {
            const by = response.headers()["x-plexora-served-by"] || "python";
            const kind = /_\d+$/.test(tile[1]) ? (new URLSearchParams(tile[2] || "").get("q") || "8bit") : "labels";
            served[`${kind}:${by}`] = (served[`${kind}:${by}`] || 0) + 1;
            if (kind === "labels" && !out.at.firstLabelTile) mark("firstLabelTile");
        }
        if (response.status() >= 400 && !response.url().includes("/update/check")) {
            const where = `${response.status()} ${response.url().replace(/^https?:\/\/[^/]+/, "")}`;
            response.text().then((body) => failed.push(`${where} ${body.slice(0, 300)}`),
                                 () => failed.push(where));
        }
    });
    await page.goto(`http://127.0.0.1:${server.port}/?pydebug`);
    let frame = await appFrame(page);
    await frame.waitForSelector("#quick_view_go", { timeout: 120000 });
    // The first visit asks where projects go ("Save projects on this
    // computer?"), in front of the home card: kept in the browser, as
    // import.mjs answers it (tests/browser/places.mjs takes the folder).
    const keepUntil = Date.now() + 5000;
    while (Date.now() < keepUntil) {
        const kept = await frame.evaluate(() => {
            const keep = [...document.querySelectorAll(".plx-confirm .plx-button")]
                .find((button) => button.textContent.trim() === "Keep in browser" && button.offsetParent !== null);
            if (keep) keep.click();
            return Boolean(keep);
        }).catch(() => false);
        if (kept) { out.firstVisit = "Keep in browser"; break; }
        await page.waitForTimeout(200);
    }
    await page.evaluate(() => { window.showOpenFilePicker = undefined; window.showDirectoryPicker = undefined; });
    if (shots) await page.screenshot({ path: path.join(shots, "mask-home.png") });

    // The image, from the home card.
    let chooser = page.waitForEvent("filechooser", { timeout: 30000 });
    await frame.click("#quick_view_go");
    picked = Date.now();
    const pickedAt = await page.evaluate(() => performance.now());
    await (await chooser).setFiles(file);

    // Import sample: the card's "Add segmentation mask", then its Browse.
    await frame.waitForSelector('button.plx-import-slot[data-slot="mask"]', { timeout: 120000 });
    mark("dialog");
    await frame.click('button.plx-import-slot[data-slot="mask"]');
    await frame.waitForSelector(".plx-import-inline-pick .plx-import-compact-browse", { timeout: 30000 });
    chooser = page.waitForEvent("filechooser", { timeout: 30000 });
    await frame.click(".plx-import-inline-pick .plx-import-compact-browse");
    await (await chooser).setFiles(mask);
    // The mask has landed on the card when its slot is gone and nothing is
    // being read.
    await frame.waitForFunction(() => !document.querySelector('button.plx-import-slot[data-slot="mask"]')
                                && !document.querySelector(".plx-import-inline-pick"), null,
                                { timeout: 120000, polling: 250 });
    mark("maskOnCard");
    out.card = await frame.evaluate(() => document.querySelector(".plx-import-body")?.innerText.slice(0, 600));
    if (shots) await page.screenshot({ path: path.join(shots, "mask-card.png") });

    // Import, then whatever the import asks (the image's pyramid question).
    out.pressed = [];
    const goneAt = Date.now() + timeoutS * 1000;
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
        await page.waitForTimeout(250);
    }
    if (!moved) {
        out.stuck = await frame.evaluate(() => document.body.innerText.slice(0, 800)).catch(() => null);
        if (shots) await page.screenshot({ path: path.join(shots, "mask-stuck.png") });
        throw Object.assign(new Error("never left the home page"), { out });
    }
    mark("imported");
    // --trace: what the Python worker is doing, every 300 ms, until the mask is
    // ready (busy, idling inside a job, requests queued behind it).
    const trace = [];
    let tracing = args.includes("--trace");
    const sample = async () => {
        while (tracing) {
            // null: the worker did not answer within 2 s (Python holds it).
            const state = await page.evaluate(() => window.PlexoraPython.state()).catch(() => null);
            const worker = state && state.worker;
            trace.push(worker ? { t: Date.now() - picked, busy: worker.busy, idling: worker.idling,
                                  queued: (worker.queued || []).length,
                                  current: worker.current && (worker.current.path || worker.current.op) }
                : { t: Date.now() - picked, held: true });
            await new Promise((r) => setTimeout(r, 300));
        }
    };
    const sampling = sample();
    frame = await appFrame(page);
    out.address = await frame.evaluate(() => location.pathname);
    // The mask's announcements on this page (main.js watchLayers), to tell a
    // mask taken on in place from one found on a reload.
    await frame.evaluate(() => {
        window.__maskTest = { events: [] };
        for (const what of ["ready", "failed"]) {
            window.addEventListener(`plexora:segmentation-${what}`,
                                    () => window.__maskTest.events.push([what, Math.round(performance.now())]));
        }
    });

    // The image drawn: every item OpenSeadragon has is fully loaded.
    const drawn = () => frame.waitForFunction(() => {
        const viewer = window.__plexora?.seaDragonViewer?.viewer;
        if (!viewer || !viewer.world || !viewer.world.getItemCount()) return false;
        for (let i = 0; i < viewer.world.getItemCount(); i += 1) {
            if (!viewer.world.getItemAt(i).getFullyLoaded()) return false;
        }
        return true;
    }, null, { timeout: 180000, polling: 250 });
    await drawn().catch(() => { out.imageTimedOut = true; });
    mark("imageDrawn");

    // The mask: its job done, the viewer takes it on in place and draws it.
    const name = decodeURIComponent(out.address.replace(/^\//, "").split("/")[0]);
    let status = null;
    while (Date.now() < goneAt) {
        status = await frame.evaluate(async (n) => (await fetch(`get_segmentation_status?datasource=${encodeURIComponent(n)}`))
            .json().catch(() => null), name).catch(() => null);
        if (status && ["ready", "error"].includes(status.status)) break;
        await page.waitForTimeout(500);
    }
    mark("maskReady");
    tracing = false;
    await sampling;
    if (trace.length) out.trace = trace;
    out.segmentation = status;
    // What the viewer made of it: whether it took the mask on and drew it
    // unasked (main.js adoptSegmentation), or the user has to choose a mode.
    const viewerState = () => frame.evaluate(() => ({
        mode: window.__plexora.viewerControls?.mode,
        userChose: window.__plexora.viewerControls?.userChose,
        segmentation: window.__plexora.seaDragonViewer?.config?.segmentation,
        status: window.__plexora.seaDragonViewer?.config?.segmentation_status,
        events: window.__maskTest ? window.__maskTest.events : "the page was reloaded",
        awaiting: Boolean(window.__plexora.seaDragonViewer?.cellLayerAwaitingMask),
        holder: window.__plexora.seaDragonViewer?.cellLayer?.pluginName || null,
    }));
    const labelsAt = Date.now() + 15000;
    while (!served["labels:browser"] && !served["labels:python"] && Date.now() < labelsAt) {
        await page.waitForTimeout(250);
    }
    out.viewerOnReady = await viewerState();
    if (!served["labels:browser"] && !served["labels:python"]) {
        // Not drawn unasked: draw it as a user would, with Outlines.
        out.clickedOutlines = true;
        await frame.click('[data-cell-mode="outlines"]');
        const clickedAt = Date.now() + 60000;
        while (!served["labels:browser"] && !served["labels:python"] && Date.now() < clickedAt) {
            await page.waitForTimeout(250);
        }
    }
    out.viewerAfter = await viewerState();
    await drawn().catch(() => { out.maskTimedOut = true; });
    mark("maskDrawn");
    // A step in, so finer mask levels are asked for too.
    await frame.evaluate(() => {
        const viewer = window.__plexora.seaDragonViewer.viewer;
        viewer.viewport.zoomBy(4);
        viewer.viewport.applyConstraints();
    });
    await page.waitForTimeout(500);
    await drawn().catch(() => { out.zoomTimedOut = true; });
    mark("zoomedDrawn");
    out.items = await frame.evaluate(() => window.__plexora?.seaDragonViewer?.viewer?.world?.getItemCount() ?? null);
    out.pyramids = await page.evaluate(() => (window.PlexoraPyramid ? window.PlexoraPyramid.builds() : null));
    // Each build's start and end after the pick, the times the page records.
    out.builds = (out.pyramids || []).map((b) => ({ kind: b.kind, where: b.where, tiles: b.total, bytes: b.bytes,
                                                     began: Math.round(b.began - pickedAt),
                                                     ended: Math.round(b.began - pickedAt + (b.ms || 0)),
                                                     ms: b.ms, buildMs: b.timings && b.timings.ms }));
    // Python's slow answers and long steps of background work, after the pick.
    out.slow = await page.evaluate((t0) => window.PlexoraPython.recent()
        .filter((r) => r.ms > 400 || r.status >= 500)
        .map((r) => ({ path: r.path, status: r.status, ms: r.ms, py: r.pyMs, at: Math.round(r.at - t0) })), pickedAt);
    out.tasks = await page.evaluate((t0) => window.PlexoraPython.tasks()
        .map((task) => ({ name: task.name, at: Math.round(task.at - t0) })), pickedAt);
    out.longSteps = await page.evaluate((t0) => window.PlexoraPython.steps()
        .filter((s) => s.ms > 200).map((s) => ({ ms: s.ms, names: s.names, at: Math.round(s.at - t0) })), pickedAt);
    out.node = await page.evaluate(async (n) => {
        const entry = await window.PlexoraNode.project(n);
        return { image: entry.image.refused || (entry.image.pool ? "served" : null),
                 labels: entry.labels.refused || (entry.labels.pool ? "served" : null),
                 labelsPath: entry.spec && entry.spec.labels && entry.spec.labels.path,
                 labelLevels: entry.spec && entry.spec.labels && entry.spec.labels.levels };
    }, name).catch((error) => String(error));
    if (shots) await page.screenshot({ path: path.join(shots, "mask-viewer.png") });
    // A later visit (--no-reopen skips it): the project's address opened
    // afresh, Python booted again. Nothing is built again -- the pyramids are
    // mounted from the browser's storage at boot -- and the mask's tiles are
    // still the page's.
    if (!args.includes("--no-reopen")) {
        out.served = { ...served };
        for (const key of Object.keys(served)) delete served[key];
        out.reopen = { served };
        const reopened = Date.now();
        await page.goto(`http://127.0.0.1:${server.port}/${encodeURIComponent(name)}?pydebug`);
        frame = await appFrame(page);
        await drawn().catch(() => { out.reopen.imageTimedOut = true; });
        out.reopen.imageMs = Date.now() - reopened;
        const seen = () => served["labels:browser"] || served["labels:python"];
        const until = Date.now() + 3000;
        while (!seen() && Date.now() < until) await page.waitForTimeout(250);
        if (!seen()) {
            // Nothing is drawn over the image on load (viewerControls.js).
            out.reopen.clickedOutlines = true;
            await frame.click('[data-cell-mode="outlines"]');
            const clickedAt = Date.now() + 60000;
            while (!seen() && Date.now() < clickedAt) await page.waitForTimeout(250);
        }
        await drawn().catch(() => { out.reopen.maskTimedOut = true; });
        out.reopen.maskMs = Date.now() - reopened;
        out.reopen.builds = await page.evaluate(() => window.PlexoraPyramid.builds().length);
        out.reopen.node = await page.evaluate(async (n) => {
            const entry = await window.PlexoraNode.project(n);
            return { image: entry.image.refused || (entry.image.pool ? "served" : null),
                     labels: entry.labels.refused || (entry.labels.pool ? "served" : null) };
        }, name).catch((error) => String(error));
        if (shots) await page.screenshot({ path: path.join(shots, "mask-reopened.png") });
    }
    // Each Python route that answered 500, asked again with the view called
    // directly, so its traceback comes back rather than the error page.
    const broken = [...new Set(failed.filter((f) => f.startsWith("500 ")).map((f) => f.split(" ")[1]))];
    out.tracebacks = {};
    for (const route of broken) {
        out.tracebacks[route] = await page.evaluate((url) => window.PlexoraPython.run([
            "import traceback",
            "from flask import request",
            "from plexora import app",
            "found = None",
            `with app.test_request_context(${JSON.stringify(url)}):`,
            "    endpoint, values = app.url_map.bind_to_environ(request.environ).match()",
            "    try:",
            "        app.view_functions[endpoint](**values)",
            "        found = 'answers now'",
            "    except Exception:",
            "        found = traceback.format_exc()[-1500:]",
            "found",
        ].join("\n")), route).catch((error) => String(error));
    }

    out.errors = errors.slice(0, 20);
    out.failed = failed.slice(0, 20);
} catch (error) {
    console.log(JSON.stringify({ ...(error.out || out), error: String(error), errors: errors.slice(0, 20),
                                 failed: failed.slice(0, 20) }, null, 1));
    process.exitCode = 1;
} finally {
    await browser.close();
    server.stop();
}
if (!process.exitCode) console.log(JSON.stringify(out, null, 1));
