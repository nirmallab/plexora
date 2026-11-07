// An .h5ad cell table on the website, end to end: an image (and a mask, if
// given) and the table picked from this computer, Plexora's own import
// (/import/inspect, /import/sample), the viewer's table load
// (get_table_status ready) and the cell features that read it -- the marker
// list and its statistics, the cell nearest a point, colouring by a marker
// and by an obs column (Cell Explorer) -- each checked against anndata's
// reading of the same file in CPython (tests/browser/spike_h5ad.py --facts).
// The HDF5 stages (`hdf5`: h5py; `h5ad`: anndata, pandas) must arrive with
// the table, not at boot. Picks are file-backed Files (setInputFiles), mounted and adopted as
// the picker does; the picker itself is import.mjs's.
// Prints JSON; exits 1 when a check fails.
//
//   node tests/browser/h5ad.mjs --image PATH --table PATH.h5ad [--mask PATH]
//                               [--marker CD45] [--obs phenotype] [--shots DIR]
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { PYTHON, REPO, appFrame, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const image = opt("--image", null);
const mask = opt("--mask", null);
const table = opt("--table", null);
const obsColumn = opt("--obs", "phenotype");
//: COLUMN=VALUE: the image the table is, answered as the Import dialog's
//: question asks it of a table that spans several.
const subset = opt("--subset", null);
const subsetArgs = subset ? ["--subset", subset] : [];
const shots = opt("--shots", null);
const site = opt("--site", path.join(REPO, "plexora", "client", "dist", "static"));
if (!image || !table) throw new Error("--image PATH --table PATH.h5ad");

const facts = spawnSync(PYTHON, [path.join(REPO, "tests", "browser", "spike_h5ad.py"), "--facts",
                                 ...(opt("--marker", null) ? ["--marker", opt("--marker")] : []),
                                 "--obs", obsColumn, ...subsetArgs, table],
                        { cwd: REPO, encoding: "utf8", maxBuffer: 64 << 20, env: { ...process.env, PYTHONPATH: REPO } });
if (facts.status !== 0) throw new Error(`facts: ${facts.stderr.slice(-2000)}`);
const truth = Object.values(JSON.parse(facts.stdout.trim().split("\n").pop()))[0];
const marker = truth.marker;

// The same import on the desktop: this checkout's app in CPython, on a data
// root of its own. The website's project record must be the desktop's.
const deskRoot = mkdtempSync(path.join(tmpdir(), "pxh5-desk-"));
const deskRun = spawnSync(PYTHON, [path.join(REPO, "tests", "browser", "spike_h5ad.py"), "--desktop",
                                   ...subsetArgs, image, ...(mask ? [mask] : []), table],
                          { cwd: REPO, encoding: "utf8", maxBuffer: 64 << 20,
                            env: { ...process.env, PYTHONPATH: REPO, PLEXORA_DATA_PATH: deskRoot,
                                   PLEXORA_TELEMETRY: "off" } });
try { rmSync(deskRoot, { recursive: true, force: true }); } catch { /* Windows may hold it */ }
if (deskRun.status !== 0) throw new Error(`desktop: ${deskRun.stderr.slice(-2000)}`);
const desk = JSON.parse(deskRun.stdout.trim().split("\n").pop());
const picked = [image, mask, table].filter(Boolean).map((p) => path.basename(p));
const baseName = (value) => value.split(/[\\/]/).pop();
//: A record with every path to a picked file as the file's name, keys sorted.
const comparable = (value) => JSON.stringify(value, (key, v) => {
    if (typeof v === "string" && /[\\/]/.test(v) && picked.includes(baseName(v))) return baseName(v);
    if (v && typeof v === "object" && !Array.isArray(v)) {
        return Object.fromEntries(Object.entries(v).sort(([a], [b]) => a.localeCompare(b)));
    }
    return v;
});

const out = { image: path.basename(image), table: path.basename(table), mask: mask && path.basename(mask),
              rows: truth.rows, marker, checks: {} };
const check = (name, ok, detail) => { out.checks[name] = { ok: Boolean(ok), ...(detail === undefined ? {} : { detail }) }; };
const near = (a, b, tolerance = 1e-4) => Math.abs(a - b) <= tolerance * Math.max(1, Math.abs(b));

const server = await startServer(["--site", site]);
const browser = await launch({ headed: args.includes("--headed") });
const errors = [];
const failed = [];
try {
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    page.on("pageerror", (error) => errors.push(`pageerror: ${error}`.slice(0, 300)));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text().slice(0, 300)); });
    page.on("response", (response) => {
        if (response.status() >= 400 && !response.url().includes("/update/check")) {
            failed.push(`${response.status()} ${response.url().replace(/^https?:\/\/[^/]+/, "")}`);
        }
    });
    // Every stage the worker loads, from the moment it starts.
    await page.addInitScript(() => {
        window.__stages = [];
        const watch = () => {
            if (window.PlexoraPython && window.PlexoraPython.on) {
                window.PlexoraPython.on("stage", (event) => window.__stages.push({ ...event, at: Math.round(performance.now()) }));
            } else setTimeout(watch, 5);
        };
        watch();
    });
    const started = Date.now();
    await page.goto(`http://127.0.0.1:${server.port}/?pydebug`);
    let frame = await appFrame(page);
    out.homeMs = Date.now() - started;
    // The first visit asks where projects go (where this Chrome can write
    // folders): this driver keeps them in the browser, as import.mjs does.
    out.firstVisit = await frame.waitForFunction(() => {
        const keep = [...document.querySelectorAll(".plx-confirm .plx-button")]
            .find((button) => button.textContent.trim() === "Keep in browser");
        if (!keep) return false;
        keep.click();
        return true;
    }, null, { timeout: 5000, polling: 200 }).then(() => "kept in browser", () => null);
    out.stagesAtBoot = await page.evaluate(() => window.__stages.map((s) => s.stage));
    check("no h5ad stage at boot", !out.stagesAtBoot.includes("h5ad"), out.stagesAtBoot);

    // The pick: file-backed Files, mounted and adopted as PlexoraPicks.pick does.
    await page.evaluate(() => {
        const input = document.createElement("input");
        input.type = "file"; input.multiple = true; input.id = "h5ad-pick"; input.hidden = true;
        document.body.append(input);
    });
    await page.setInputFiles("#h5ad-pick", [image, ...(mask ? [mask] : []), table]);
    const paths = await page.evaluate(async () => {
        const files = [...document.getElementById("h5ad-pick").files];
        const mounted = await window.PlexoraPython.mount("/picked/h5adtest", files);
        mounted.forEach((p, i) => window.PlexoraPicks.adopt(p, files[i]));
        return mounted;
    });

    const post = (route, body) => frame.evaluate(async ([r, b]) => {
        const t = performance.now();
        const response = await fetch(r, { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify(b) });
        return { status: response.status, ms: Math.round(performance.now() - t), body: await response.json() };
    }, [route, body]);
    const inspected = await post("import/inspect", { paths });
    const samples = inspected.body.samples || [];
    const layers = samples.flatMap((s) => s.layers || []);
    const tableLayer = layers.find((l) => l.kind === "table");
    out.inspect = { status: inspected.status, ms: inspected.ms, samples: samples.length,
                    layers: layers.map((l) => `${l.kind}:${l.label}`), table: tableLayer?.render?.detail || tableLayer?.detail,
                    questions: samples.flatMap((s) => (s.questions || []).map((q) => q.id || q.key)),
                    warnings: [...(inspected.body.warnings || []), ...samples.flatMap((s) => s.warnings || [])] };
    out.stagesAfterInspect = await page.evaluate(() => window.__stages);
    check("inspect: one sample with the table", inspected.status === 200 && samples.length === 1 && tableLayer,
          out.inspect);
    check("h5ad stage loaded by the import", out.stagesAfterInspect.some((s) => s.stage === "h5ad"),
          out.stagesAfterInspect);

    // The table's image question answered as the dialog does
    // (spike_h5ad.answers_for); every other question takes its default.
    const answers = {};
    if (subset) {
        const value = subset.split("=").slice(1).join("=");
        for (const question of samples.flatMap((s) => s.questions || [])) {
            if (String(question.id).startsWith("subset:")
                && (question.options || []).some((o) => o.value === value)) answers[question.id] = value;
        }
    }
    out.answers = answers;
    const imported = await post("import/sample", { paths, answers });
    const name = imported.body.name;
    out.importSample = { status: imported.status, ms: imported.ms, name,
                         error: name ? undefined : JSON.stringify(imported.body).slice(0, 600) };
    check("import/sample made the project", imported.status === 200 && name, out.importSample);
    if (!name) throw new Error("import failed");

    // The viewer, and its table.
    const opened = Date.now();
    await frame.evaluate((n) => { location.href = `/${n}`; }, name);
    frame = await appFrame(page);
    const get = (route) => frame.evaluate(async (r) => {
        const t = performance.now();
        const response = await fetch(r);
        const type = response.headers.get("Content-Type") || "";
        const headers = Object.fromEntries([...response.headers.entries()].filter(([k]) => k.startsWith("x-")));
        const body = type.includes("json") ? await response.json() : { bytes: (await response.arrayBuffer()).byteLength };
        return { status: response.status, ms: Math.round(performance.now() - t), headers, body };
    }, route);
    const q = `datasource=${encodeURIComponent(name)}`;
    let status = null;
    for (let i = 0; i < 2400; i += 1) {
        status = (await get(`get_table_status?${q}`)).body;
        if (status.status === "ready" || status.status === "error" || status.error) break;
        await page.waitForTimeout(250);
    }
    out.tableReadyMs = Date.now() - opened;
    out.tableStatus = status;
    await frame.waitForFunction(() => {
        const viewer = window.__plexora?.seaDragonViewer?.viewer;
        if (!viewer || !viewer.world || !viewer.world.getItemCount()) return false;
        for (let i = 0; i < viewer.world.getItemCount(); i += 1) {
            if (!viewer.world.getItemAt(i).getFullyLoaded()) return false;
        }
        return true;
    }, null, { timeout: 180000, polling: 250 }).catch(() => { out.viewerTimedOut = true; });
    out.viewerMs = Date.now() - opened;
    out.loadStatus = (await get(`get_load_status?${q}`)).body;
    check("table ready", status && status.status === "ready", status);

    const described = await get(`get_database_description?${q}`);
    const markers = Object.keys(described.body || {}).filter((k) => truth.markers.includes(k));
    const stats = described.body?.[marker];
    out.description = { status: described.status, ms: described.ms, markers: markers.length,
                        [marker]: stats && { mean: stats.mean, count: stats.count } };
    check("every marker described", markers.length === truth.markers.length,
          { got: markers.length, want: truth.markers.length });
    check(`${marker} mean as anndata reads it`, stats && near(stats.mean, truth.mean) && stats.count === truth.rows,
          { got: stats && stats.mean, want: truth.mean });

    const nearest = await get(`get_nearest_cell?point_x=${truth.first.x}&point_y=${truth.first.y}&${q}`);
    out.nearest = { status: nearest.status, ms: nearest.ms, body: JSON.stringify(nearest.body).slice(0, 400) };
    const cell = Array.isArray(nearest.body) ? nearest.body[0] : nearest.body;
    check("nearest cell is the first cell, with its marker value",
          nearest.status === 200 && cell && near(Number(cell[marker]), truth.first.value)
          && near(Number(cell.X ?? cell.x), truth.first.x),
          { got: cell && { value: cell[marker], x: cell.X ?? cell.x }, want: truth.first });

    const variables = await get(`plugins/cell_explorer/api/variables?${q}`);
    const names = (variables.body.variables || []).map((v) => v.name || v.column);
    out.variables = { status: variables.status, ms: variables.ms, names: names.slice(0, 20) };
    check(`Cell Explorer offers ${obsColumn}`, names.includes(obsColumn), out.variables);
    const byObs = await get(`plugins/cell_explorer/api/values?${q}&column=${encodeURIComponent(obsColumn)}`);
    out.colourByObs = { status: byObs.status, ms: byObs.ms, ...byObs.headers, bytes: byObs.body.bytes };
    check(`colour by ${obsColumn}`, byObs.status === 200 && Number(byObs.headers["x-cell-count"]) === truth.rows
          && byObs.headers["x-value-kind"] === "categorical", out.colourByObs);
    // What the viewer colours cells by a marker with (dataLayer.getAllCells):
    // every cell's id, position and value, rows of four float32.
    out.colourByMarker = await frame.evaluate(async (r) => {
        const t = performance.now();
        const response = await fetch(r);
        const values = new Float32Array(await response.arrayBuffer());
        let sum = 0;
        for (let i = 3; i < values.length; i += 4) sum += values[i];
        return { status: response.status, ms: Math.round(performance.now() - t), rows: values.length / 4,
                 first: values[3], mean: sum / (values.length / 4) };
    }, `get_all_cells/float/?${q}&start_keys=id,X,Y,${encodeURIComponent(marker)}`);
    check(`colour by ${marker}: every cell's value`, out.colourByMarker.status === 200
          && out.colourByMarker.rows === truth.rows && near(out.colourByMarker.first, truth.first.value)
          && near(out.colourByMarker.mean, truth.mean), { got: out.colourByMarker, want: { first: truth.first.value, mean: truth.mean } });

    if (shots) {
        await frame.evaluate(([n, c]) => { location.href = `/${n}?tool=cell_explorer&color_by=${c}`; }, [name, obsColumn]);
        frame = await appFrame(page);
        await page.waitForTimeout(3000);
        await frame.evaluate(() => {
            const loader = window.PlexoraToolLoader;
            if (loader && !loader.isToolOpen?.("cell_explorer")) loader.toggleTool?.("cell_explorer");
        });
        await page.waitForTimeout(3000);
        await frame.getByText("Filled", { exact: true }).click().catch(() => {});
        await page.waitForTimeout(4000);
        await page.screenshot({ path: path.join(shots, "h5ad-colour-by.png") });
    }
    // Parity with the desktop's import of the same files.
    const config = (await get("config")).body?.[name] || {};
    const requirements = (await get(`${encodeURIComponent(name)}/tools/cell_explorer/requirements`)).body;
    // `segmentationPending` says whether the mask's pyramid is still being
    // built at the moment of asking: timing, not the project.
    const differs = (a, b) => Object.keys({ ...a, ...b }).filter((k) => k !== "segmentationPending"
                                                                && comparable(a?.[k]) !== comparable(b?.[k]));
    out.parity = { name: [name, desk.name], dataset: differs(config.dataset || {}, desk.dataset || {}),
                   roles: comparable(config.roles ?? null) === comparable(desk.roles ?? null),
                   requirements: differs(requirements || {}, desk.requirements || {}) };
    check("the project record is the desktop's", name === desk.name && !out.parity.dataset.length
          && out.parity.roles, out.parity);
    check("Cell Explorer's requirements are the desktop's", !out.parity.requirements.length, out.parity);
    out.desktopTimings = desk.timings;
    out.tableSrc = config.dataset?.src;
    out.heapBytes = await page.evaluate(() => window.PlexoraPython.heap());
    out.stages = await page.evaluate(() => window.__stages);
    out.python = await page.evaluate(() => window.PlexoraPython.recent()
        .filter((r) => !/\/generated\//.test(r.path)).sort((a, b) => b.ms - a.ms).slice(0, 12)
        .map((r) => ({ path: r.path.slice(0, 80), ms: Math.round(r.ms), py: r.pyMs })));
} catch (error) {
    out.error = String(error.message || error).slice(0, 1000);
} finally {
    out.errors = errors.slice(0, 20);
    out.failed = failed.slice(0, 20);
    await browser.close();
    server.stop();
}
out.ok = !out.error && Object.values(out.checks).every((c) => c.ok);
console.log(JSON.stringify(out, null, 1));
if (!out.ok) process.exitCode = 1;
