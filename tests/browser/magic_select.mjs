// Magic select on the website, against the desktop's answers.
//
//   node tests/browser/magic_select.mjs --out DIR [--file TIF] [--devices webgpu,wasm]
//        [--site DIR] [--repeat N] [--shots DIR] [--headed]
//
// The desktop's side first (tests/browser/magic_select_truth.py truth, run
// here when DIR/cases.json is missing): a desktop project of the image and
// its answers to every case, on ONNX Runtime's CPU provider. Then the website,
// from a fresh profile, as a user: the image picked and imported (Plexora's
// own import, the page building its pyramid), magic select set up through
// the routes the viewer's client calls (`/segment/v1/status` says the model
// is missing, `/install` has the page fetch it -- served here from the model
// folder, as the assets bucket will -- and `/status` shows the progress
// until ready), and then, once per runtime (`--devices`; the page's WebGPU
// and WebAssembly), every case sent to `/segment/v1/segment` as the desktop's
// were, with what the model saw and made read back (PlexoraPython.run), a
// click made through the viewer's own client (PlexoraSegment.point on a view
// the viewer was moved to), and `--repeat` timed encodes and decodes.
// Finally the desktop replays the viewer's clicks and `compare` holds every
// website answer to the desktop's (its tolerances say why they are what they
// are). Prints one JSON record; exit 1 when a case is out of tolerance.
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { PYTHON, REPO, appFrame, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const out = opt("--out", null);
if (!out) throw new Error("--out DIR: where the desktop's answers are (or go) and the website's go");
const file = opt("--file", "C:/plexora-bench/browser_node/fixtures/flat/lsp11385-s3ch-raw.ome.tif");
const devices = opt("--devices", "webgpu,wasm").split(",").filter(Boolean);
const site = opt("--site", path.join(REPO, "plexora", "client", "dist", "static"));
const repeat = Number(opt("--repeat", "5"));
const shots = opt("--shots", null);
const TRUTH = path.join(REPO, "tests", "browser", "magic_select_truth.py");

function python(...rest) {
    const done = spawnSync(PYTHON, [TRUTH, ...rest], { cwd: REPO, encoding: "utf-8", maxBuffer: 1 << 26 });
    if (done.status !== 0 && rest[0] !== "compare") {
        throw new Error(`${rest[0]} failed: ${(done.stderr || "").slice(-3000)}`);
    }
    return { status: done.status, stdout: done.stdout };
}

mkdirSync(out, { recursive: true });
if (!existsSync(path.join(out, "cases.json"))) python("truth", "--file", file, "--out", out);
const spec = JSON.parse(readFileSync(path.join(out, "cases.json"), "utf-8"));
const truth = JSON.parse(readFileSync(path.join(out, "truth.json"), "utf-8"));
const desktop = Object.fromEntries(truth.records.map((record) => [record.name, record.answer]));

//: A case's request on this side: its token and starting mask are this
//: side's own answers, an outline used as a prompt the desktop's.
function resolve(cas, answers) {
    const body = JSON.parse(JSON.stringify(cas.request));
    if (cas.token_from) body.token = answers[cas.token_from].token;
    if (cas.prev_from) (body.options ||= {}).prev_key = answers[cas.prev_from].prev_key;
    if (cas.mask_from) (body.options ||= {}).mask_geometry = desktop[cas.mask_from].geometry;
    return body;
}

const server = await startServer(["--site", site, "--tree", `models/${spec.web_dir}=${spec.model_dir}`]);
const browser = await launch({ headed: args.includes("--headed") });
const result = { file, devices, setup: {}, runs: {} };
let failed = false;
try {
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(`pageerror: ${error}`));
    page.on("console", (message) => {
        if (message.type() === "error") errors.push(message.text().slice(0, 300));
    });
    await page.goto(`http://127.0.0.1:${server.port}/?pydebug`);
    const frame = await appFrame(page);
    await frame.waitForSelector("#quick_view_go", { timeout: 120000 });
    await frame.waitForFunction(() => {
        const keep = [...document.querySelectorAll(".plx-confirm .plx-button")]
            .find((button) => button.textContent.trim() === "Keep in browser");
        if (!keep) return false;
        keep.click();
        return true;
    }, null, { timeout: 5000, polling: 200 }).catch(() => null);

    // -- the image, as a user opens it (tests/browser/import.mjs) --
    await page.evaluate(() => { window.showOpenFilePicker = undefined; window.showDirectoryPicker = undefined; });
    const chooser = page.waitForEvent("filechooser", { timeout: 30000 });
    await frame.click("#quick_view_go");
    const picked = Date.now();
    await (await chooser).setFiles(file);
    let moved = false;
    while (!moved && Date.now() - picked < 900000) {
        moved = await frame.evaluate(() => location.pathname !== "/" && window.PlexoraShellFramed
                                     && !document.getElementById("plexora-boot")).catch(() => false);
        if (moved) break;
        await frame.evaluate(() => {
            const visible = (node) => node && !node.disabled && node.offsetParent !== null;
            const confirm = document.querySelector(".plx-confirm .plx-button-primary");
            if (visible(confirm)) { confirm.click(); return; }
            const go = document.querySelector('[data-role="go"]');
            if (visible(go) && !go.dataset.pressedByTest) { go.dataset.pressedByTest = "1"; go.click(); }
        }).catch(() => null);
        await page.waitForTimeout(500);
    }
    if (!moved) throw new Error("the image never opened");
    await frame.waitForFunction(() => {
        const viewer = window.__plexora?.seaDragonViewer?.viewer;
        return viewer && viewer.world && viewer.world.getItemCount() > 0
            && viewer.world.getItemAt(0).getFullyLoaded();
    }, null, { timeout: 180000, polling: 250 });
    result.importMs = Date.now() - picked;
    const project = await frame.evaluate(() => window.flaskVariables?.datasource);
    result.project = project;

    const routeOn = (target) => (where, body) => target.evaluate(async ({ where, body }) => {
        const t = performance.now();
        const response = await fetch(where, {
            method: body === undefined ? "GET" : "POST",
            headers: { "Content-Type": "application/json", Accept: "application/json" },
            body: body === undefined ? undefined : JSON.stringify(body) });
        const data = await response.json().catch(() => null);
        return { status: response.status, data, ms: Math.round(performance.now() - t) };
    }, { where, body });
    const route = routeOn(frame);
    const py = (code) => page.evaluate((code) => window.PlexoraPython.run(code), code);
    const dumpOf = async (answer, name, folder) => {
        const code = `TOKEN = ${JSON.stringify(answer.token)}\nPROJECT = ${JSON.stringify(project)}\n`
            + `PREV = ${JSON.stringify(answer.prev_key)}\n${spec.dump}`;
        const seen = JSON.parse(await py(code));
        mkdirSync(folder, { recursive: true });
        writeFileSync(path.join(folder, `${name}.npz`), Buffer.from(seen.npz, "base64"));
        delete seen.npz;
        return seen;
    };

    // -- the one-time setup, through the routes the viewer's client calls --
    const before = await route("/segment/v1/status");
    result.setup.before = { state: before.data.state, missing: before.data.weights?.missing,
                            device: before.data.device, runtime: before.data.runtime, ms: before.ms };
    const setupStarted = Date.now();
    const install = await route("/segment/v1/install", {});
    result.setup.install = { status: install.status, state: install.data.state, ms: install.ms };
    const states = [];
    let state = install.data;
    while (Date.now() - setupStarted < 300000) {
        state = (await route("/segment/v1/status")).data;
        const last = states[states.length - 1];
        const seen = { state: state.state, file: state.download?.file ?? null,
                       fraction: state.download?.fraction ?? null };
        if (!last || last.state !== seen.state || last.file !== seen.file) {
            states.push({ ...seen, at: Date.now() - setupStarted });
        }
        if (["ready", "error"].includes(state.state) && !state.download?.running) break;
        await page.waitForTimeout(250);
    }
    result.setup.states = states;
    result.setup.readyMs = Date.now() - setupStarted;
    // What else Python's worker ran meanwhile (background steps, requests).
    result.setup.python = await page.evaluate((since) => ({
        steps: window.PlexoraPython.steps().filter((s) => s.at >= since - 5000),
        requests: window.PlexoraPython.recent().filter((r) => r.path.startsWith("/segment/")).slice(-6),
    }), await page.evaluate(() => performance.now()) - (Date.now() - setupStarted));
    result.setup.after = { state: state.state, hint: state.hint, device: state.device,
                           download: state.download };
    if (state.state !== "ready") throw new Error(`setup ended ${state.state}: ${state.hint}`);
    if (shots) await page.screenshot({ path: path.join(shots, "magic-select-ready.png") });

    for (const device of devices) {
        const run = { device: null, records: [], ui: [], timing: { embed: [], decode: [] } };
        const folder = path.join(out, `web-${device}`);
        // This runtime from now on: the page's sessions and Python's dropped,
        // made again (timed) before the first case.
        await page.evaluate((device) => window.PlexoraOnnx.reset({ device }), device);
        await py("from plexora.vision import sam, segment\nsegment.forget()\nsam._BACKEND = None\n'ok'");
        const loadStarted = Date.now();
        await py("from plexora.vision import sam\nsam.session()\n'ok'");
        run.loadMs = Date.now() - loadStarted;
        const answers = {};
        for (const cas of spec.cases) {
            const body = resolve(cas, answers);
            const answer = await route("/segment/v1/segment", { datasource: project, ...body });
            if (answer.status !== 200) throw new Error(`${device} ${cas.name}: ${answer.status} ${JSON.stringify(answer.data)}`);
            answers[cas.name] = answer.data;
            const seen = await dumpOf(answer.data, cas.name, folder);
            run.records.push({ name: cas.name, body, answer: answer.data, requestMs: answer.ms, ...seen });
        }
        run.device = answers[spec.cases[0].name].device;
        // Timed: a new view's encode (the cache forgotten each time), then
        // decodes on it (a second click).
        const first = spec.cases.find((cas) => cas.name === "point");
        const second = spec.cases.find((cas) => cas.name === "grow");
        for (let i = 0; i < repeat; i += 1) {
            await py("from plexora.vision import segment\nsegment.forget()\n'ok'");
            const answer = await route("/segment/v1/segment", { datasource: project, ...resolve(first, {}) });
            run.timing.embed.push({ route: answer.data.timing.embed_ms, request: answer.ms });
            for (let j = 0; j < 2; j += 1) {
                const again = await route("/segment/v1/segment",
                                          { datasource: project, ...resolve(second, { point: answer.data }) });
                run.timing.decode.push({ route: again.data.timing.decode_ms, request: again.ms });
            }
        }
        run.ortTimes = await page.evaluate(() => window.PlexoraOnnx.times());

        // A click through the viewer's own client, on a view the viewer was
        // moved to: the request it sent is what the desktop replays.
        const target = first.request.points[0];
        await frame.evaluate(({ x, y }) => {
            const viewer = window.__plexora.seaDragonViewer.viewer;
            const item = viewer.world.getItemAt(0);
            viewer.viewport.fitBounds(item.imageToViewportRectangle(x - 300, y - 300, 600, 600), true);
            window.__msSent = [];
            if (!window.__msFetch) {
                window.__msFetch = window.fetch;
                window.fetch = (where, init) => {
                    if (String(where).includes("segment/v1/segment") && init && init.body) {
                        window.__msSent.push(JSON.parse(init.body));
                    }
                    return window.__msFetch(where, init);
                };
            }
        }, target);
        await page.waitForTimeout(1000);
        const click = await frame.evaluate(async ({ x, y, project }) => {
            window.PlexoraSegment.forget();
            const t = performance.now();
            const answer = await window.PlexoraSegment.point({ datasource: project,
                                                               points: [{ x, y, label: 1 }] });
            return { answer, ms: Math.round(performance.now() - t), body: window.__msSent.pop() };
        }, { ...target, project });
        if (!click.answer || click.answer.ok === false) throw new Error(`${device} ui click: ${JSON.stringify(click.answer)}`);
        const seen = await dumpOf(click.answer, "ui-click", folder);
        run.ui.push({ name: "click", body: click.body, answer: click.answer, requestMs: click.ms, ...seen });
        if (shots) await page.screenshot({ path: path.join(shots, `magic-select-${device}.png`) });
        writeFileSync(path.join(out, `web-${device}.json`), JSON.stringify(run, null, 1));
        result.runs[device] = {
            device: run.device, loadMs: run.loadMs,
            embedMs: run.records.filter((r) => r.answer.timing.embed_ms !== undefined)
                .map((r) => r.answer.timing.embed_ms),
            decodeMs: run.records.map((r) => r.answer.timing.decode_ms),
            repeated: run.timing,
            ort: summarize(run.ortTimes),
        };
    }

    // -- a later visit (same profile): the model comes from the browser's
    // storage, checked again, with no download --
    await page.goto(`http://127.0.0.1:${server.port}/?pydebug`);
    const home = await appFrame(page);
    const later = await routeOn(home)("/segment/v1/status");
    const loadStarted = Date.now();
    const loaded = await py("from plexora.vision import sam\nimport json\njson.dumps(sam.session().device)");
    const loadMs = Date.now() - loadStarted;
    const embedded = await py("import numpy as np\nfrom plexora.vision import sam\n"
        + "e = sam.embed(np.full((256, 256, 3), 128, np.uint8))\nstr(e.array.shape)");
    result.revisit = {
        state: later.data.state, download: later.data.download, missing: later.data.weights?.missing,
        loadMs, device: JSON.parse(loaded), embedded,
        stored: await page.evaluate(async (dir) => {
            let at = await navigator.storage.getDirectory();
            for (const part of ["plexora", "cache", "models", ...dir.split("/").filter(Boolean)]) {
                at = await at.getDirectoryHandle(part);
            }
            const out = {};
            for await (const [name, handle] of at.entries()) out[name] = (await handle.getFile()).size;
            return out;
        }, spec.web_dir),
    };
    if (later.data.state !== "ready" || later.data.download) failed = true;
    result.errors = errors.slice(0, 20);
} catch (error) {
    result.error = String(error && error.stack || error);
    failed = true;
} finally {
    await browser.close();
    server.stop();
}

function summarize(times) {
    const out = {};
    for (const op of ["embed", "decode"]) {
        const ms = times.filter((t) => t.op === op).map((t) => t.ms).sort((a, b) => a - b);
        if (ms.length) out[op] = { n: ms.length, median: ms[Math.floor(ms.length / 2)], min: ms[0], max: ms[ms.length - 1],
                                   ep: [...new Set(times.filter((t) => t.op === op).map((t) => t.ep))] };
    }
    return out;
}

if (!failed) {
    python("replay", "--out", out);
    const compared = python("compare", "--out", out);
    const report = JSON.parse(compared.stdout);
    result.parity = { ok: report.ok, tolerances: report.tolerances,
                      runs: Object.fromEntries(Object.entries(report.runs).map(([name, run]) => [name, {
                          ok: run.ok, worst: run.worst,
                          failing: Object.entries(run.cases).filter(([, c]) => !c.ok).map(([n, c]) => ({ n, ...c })) }])) };
    writeFileSync(path.join(out, "parity.json"), JSON.stringify(report, null, 1));
    failed = !report.ok;
}
console.log(JSON.stringify(result, null, 1));
process.exitCode = failed ? 1 : 0;
