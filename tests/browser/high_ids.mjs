// Cell ids past 2^24 on the website (the envelope's mask-id cell): float32's
// last exact integer is 16,777,216, so an id that passes through a float32
// anywhere -- a label tile, the cell table, the nearest-cell pick, a gate --
// comes back as its neighbour. The fixture is a small project whose ids are
// all shifted (tools/bench_envelope/high_ids.py: `hi24` straddles 2^24, `hi32`
// sits near 2^32); it is imported as project.mjs imports one, the cells drawn
// as outlines, and then:
//   - every id in the label tiles the viewer fetched, decoded as it decodes
//     them (each id's high byte was dropped until 2026-11),
//   - /get_all_cells/integer/ (the viewer's ids) against the table's,
//   - /get_nearest_cell at the centre of each cell either side of the pivot,
//   - the gating route's /get_gated_cell_ids for a gate, against the table,
//   - that gate on a plugin's cell layer: the same cells, drawn on the CPU,
//     with no id-indexed table built (one as long as the largest id).
//
//   node tests/browser/high_ids.mjs --dir FIXTURE_DIR [--site DIR] [--shots DIR] [--headed]
// Prints JSON with `ok` and the checks. Without --site it builds a static
// site from this tree first (static_build), as selfcheck.mjs does.
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { PYTHON, REPO, appFrame, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const dir = opt("--dir", null);
const shots = opt("--shots", null);
if (!dir) throw new Error("--dir FIXTURE_DIR (image.ome.tif, mask.tif, cells.csv)");
let site = opt("--site", null);
if (!site) {
    site = mkdtempSync(path.join(tmpdir(), "pxhigh-site-"));
    execFileSync(PYTHON, ["-m", "plexora.client.static_build", "--out", site],
                 { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO }, stdio: ["ignore", "ignore", "inherit"] });
}

const table = readFileSync(path.join(dir, "cells.csv"), "utf-8").trim().split(/\r?\n/);
const header = table[0].split(",");
const rows = table.slice(1).map((line) => line.split(","));
const col = (name) => header.indexOf(name);
const ids = rows.map((row) => Number(row[col("CellID")]));
const offset = Math.min(...ids) - 1;
// The cells either side of 2^24 (or of the offset, where 2^24 is not crossed).
const pivot = ids.some((id) => id >= 2 ** 24) && ids.some((id) => id < 2 ** 24) ? 2 ** 24 : offset + 128;
const probes = rows.filter((row) => Math.abs(Number(row[col("CellID")]) - pivot) <= 2)
    .map((row) => ({ id: Number(row[col("CellID")]), x: Number(row[col("X_centroid")]), y: Number(row[col("Y_centroid")]) }));
const marker = "CD3";
const cut = rows.map((row) => Number(row[col(marker)])).sort((a, b) => a - b)[Math.floor(rows.length / 2)];
const expectedGated = new Set(rows.filter((row) => Number(row[col(marker)]) > cut).map((row) => Number(row[col("CellID")])));

const out = { dir, offset, pivot, checks: [], errors: [], failed: [] };
const check = (name, ok, detail = {}) => {
    out.checks.push({ name, ok: Boolean(ok), ...detail });
    console.error(`${ok ? "ok  " : "FAIL"} ${name}`);
};

const server = await startServer(["--site", site, "--tree", `fixture=${dir}`]);
const browser = await launch({ headed: args.includes("--headed") });
const labelUrls = new Set();
try {
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    page.on("pageerror", (error) => out.errors.push(`pageerror: ${error}`.slice(0, 300)));
    page.on("console", (message) => { if (message.type() === "error") out.errors.push(message.text().slice(0, 300)); });
    page.on("response", (response) => {
        const url = response.url();
        const tile = url.match(/\/generated\/data\/[^/]+\/([^/]+)\/\d+\/\d+_\d+\.png(?:\?(.*))?$/);
        if (tile && response.status() === 200 && !/_\d+$/.test(tile[1])) labelUrls.add(url.replace(/^https?:\/\/[^/]+/, ""));
        if (/generated/.test(url) && (out.generated ||= []).length < 12) out.generated.push(url.replace(/^https?:\/\/[^/]+/, ""));
        if (response.status() >= 400 && !url.includes("/update/check")) {
            out.failed.push(`${response.status()} ${url.replace(/^https?:\/\/[^/]+/, "")}`);
        }
    });
    await page.goto(`http://127.0.0.1:${server.port}/`);
    let frame = await appFrame(page);
    const paths = await page.evaluate(async () => {
        const names = ["image.ome.tif", "mask.tif", "cells.csv"];
        const picked = await Promise.all(names.map(async (n) => new File([await (await fetch(`/fixture/${n}`)).blob()], n)));
        const mounted = await window.PlexoraPython.mount("/picked/fixture", picked);
        mounted.forEach((p, i) => window.PlexoraPicks.adopt(p, picked[i]));
        return mounted;
    });
    const imported = await frame.evaluate(async (picks) => {
        const response = await fetch("import/sample", { method: "POST", headers: { "Content-Type": "application/json" },
                                                         body: JSON.stringify({ paths: picks }) });
        return { status: response.status, body: await response.json() };
    }, paths);
    const name = imported.body && imported.body.name;
    check("the project imports", imported.status === 200 && name, { status: imported.status });
    if (!name) throw new Error(JSON.stringify(imported.body).slice(0, 300));

    await frame.evaluate((n) => { location.href = `/${n}`; }, name);
    frame = await appFrame(page);
    await frame.waitForFunction(() => {
        const viewer = window.__plexora?.seaDragonViewer?.viewer;
        if (!viewer || !viewer.world || viewer.world.getItemCount() < 2) return false;
        for (let i = 0; i < viewer.world.getItemCount(); i += 1) {
            if (!viewer.world.getItemAt(i).getFullyLoaded()) return false;
        }
        return true;
    }, null, { timeout: 180000, polling: 250 });
    // A project opens with its cells off ("None"): outlines draw the mask.
    const outlines = async () => {
        await frame.click('[data-cell-mode="outlines"]');
        const until = Date.now() + 60000;
        while (!labelUrls.size && Date.now() < until) await frame.waitForTimeout(250);
        await frame.waitForTimeout(1500);
    };
    await outlines();
    if (shots) await page.screenshot({ path: path.join(shots, `high-ids-${path.basename(dir)}.png`) });

    out.labelRenderer = await frame.evaluate(() => window.__plexora.seaDragonViewer.labelRenderer);

    const decoded = await frame.evaluate(async (urls) => {
        const seen = new Set();
        for (const url of urls) {
            const response = await fetch(url.replace(/^\//, ""));
            const tile = window.PlexoraTileDecode.decodeLabelTile(new Uint8Array(await response.arrayBuffer()));
            const words = new Uint32Array(tile.data.buffer, tile.data.byteOffset, tile.width * tile.height);
            for (const id of words) if (id) seen.add(id);
        }
        return [...seen];
    }, [...labelUrls]);
    const known = new Set(ids);
    const strangers = decoded.filter((id) => !known.has(id));
    check("every id in the label tiles is a cell of the table", labelUrls.size > 0 && decoded.length > 0 && !strangers.length,
          { tiles: labelUrls.size, ids: decoded.length, strangers: strangers.slice(0, 5) });

    const viewerIds = await frame.evaluate(async (ds) => {
        const response = await fetch(`get_all_cells/integer/?${new URLSearchParams({ start_keys: "CellID", datasource: ds })}`);
        return [...new Uint32Array(await response.arrayBuffer())];
    }, name);
    const sameIds = viewerIds.length === ids.length && viewerIds.every((id, i) => id === ids[i]);
    check("the viewer's ids (/get_all_cells/integer/) are the table's, exactly", sameIds,
          { count: viewerIds.length, first: viewerIds.slice(0, 3) });

    const picks = await frame.evaluate(async ([ds, cells]) => Promise.all(cells.map(async (cell) => {
        const response = await fetch(`get_nearest_cell?${new URLSearchParams({ point_x: cell.x, point_y: cell.y, datasource: ds })}`);
        const body = await response.json();
        return { asked: cell.id, got: body && (body.CellID ?? body.id ?? body.cell_id), body: JSON.stringify(body).slice(0, 160) };
    })), [name, probes]);
    out.picks = picks;
    check("the nearest-cell pick names each cell either side of the pivot", picks.length >= 4
          && picks.every((pick) => Number(pick.got) === pick.asked), { picks: picks.map((p) => [p.asked, p.got]) });

    const gated = await frame.evaluate(async ([ds, key, low]) => {
        const response = await fetch(`plugins/gating/get_gated_cell_ids?${new URLSearchParams({
            filter: JSON.stringify({ [key]: [low, 1e12] }), start_keys: "CellID", datasource: ds })}`);
        const body = await response.json();
        return Array.isArray(body) ? body.map((row) => Number(row.CellID ?? row.id)) : body;
    }, [name, marker, cut]);
    const gatedOk = Array.isArray(gated) && gated.length === expectedGated.size && gated.every((id) => expectedGated.has(id));
    check(`a gate (${marker} > ${cut}) selects exactly the table's cells`, gatedOk,
          { got: Array.isArray(gated) ? gated.length : gated, expected: expectedGated.size });

    // The same gate drawn on a plugin's cell layer, registered as the gating
    // tool registers its own (the tool's card itself is a licensed one) with
    // the gating route as its provider. The layer's id tables (one entry per
    // id up to the largest) stop at 2^24, so past it the viewer takes the ids
    // from the server and draws on the CPU -- without first allocating a
    // table as long as the largest id (4 GB for ids near 2^32).
    const drawn = await frame.evaluate(async ([ds, key, low]) => {
        const viewer = window.__plexora.seaDragonViewer;
        const provider = {
            localRangeGate: true,
            async getSelectedIds(filter) {
                const response = await fetch(`plugins/gating/get_gated_cell_ids?${new URLSearchParams({
                    filter: JSON.stringify(filter), start_keys: "CellID", datasource: ds })}`);
                return new Set((await response.json()).map((row) => Number(row.CellID ?? row.id)));
            },
            supportsColorCoding: () => false,
            getColorCodedRanges: () => [],
        };
        viewer.registerCellLayer("probe", provider, { mode: "outlines", makeActive: true });
        await viewer.updateSegmentationFilter({ [key]: [low, 1e12] }, false, "probe");
        await new Promise((resolve) => setTimeout(resolve, 2000));
        const layer = viewer._cellStack.get("probe");
        return { renderer: viewer.labelRenderer, ids: layer.filterIds ? [...layer.filterIds] : null,
                 mask: layer.gateMask ? layer.gateMask.length : null,
                 heapMb: performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1e6) : null };
    }, [name, marker, cut]);
    out.gateDrawn = { renderer: drawn.renderer, ids: drawn.ids && drawn.ids.length, mask: drawn.mask, heapMb: drawn.heapMb };
    const drawnOk = drawn.ids && drawn.ids.length === expectedGated.size && drawn.ids.every((id) => expectedGated.has(id));
    check("a plugin's cell layer keeps exactly the gate's cells", drawnOk,
          { ids: drawn.ids && drawn.ids.length, mask: drawn.mask });
    check("and draws them on the CPU, with no id table built", drawn.renderer === "cpu" && drawn.mask === null,
          { renderer: drawn.renderer, before: out.labelRenderer, heapMb: drawn.heapMb });
    if (shots) await page.screenshot({ path: path.join(shots, `high-ids-${path.basename(dir)}-gate.png`) });

    // The viewer's session check after a reload answers 404 on the website
    // (agent/v1/viewer/sessions/<old id>): not this test's subject.
    const failed = out.failed.filter((line) => !/\/agent\/v1\/viewer\/sessions\//.test(line));
    const errors = out.errors.filter((line) => !/status of 404/.test(line) || failed.length);
    check("no page errors, no failed requests", !errors.length && !failed.length, { errors, failed });
} catch (error) {
    out.checks.push({ name: "the run finished", ok: false, error: String(error.message || error).slice(0, 500) });
} finally {
    await browser.close();
    server.stop();
}
out.ok = out.checks.every((c) => c.ok);
console.log(JSON.stringify(out, null, 1));
process.exitCode = out.ok ? 0 : 1;
