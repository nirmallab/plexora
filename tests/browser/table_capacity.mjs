// A large cell table on the website (the envelope's table cells): a project
// from tools/bench_envelope/big_table.py (image.ome.tif + cells.csv) imported
// as the Import dialog imports one, opened, and its whole table handed to
// Python in the worker -- what an AI session or a dataset gate needs. Records
// whether the "Large for a browser tab" notice showed on open (it shows past
// envelope.json's table.worker_values), how long the table took to be ready
// and to reach Python, the worker's WebAssembly memory before and after, and
// whether Python, the tab and the viewer were still answering afterwards.
//
//   node tests/browser/table_capacity.mjs --dir FIXTURE_DIR [--site DIR] [--headed]
// Prints JSON. Measures; asserts nothing (envelope records are made from it).
import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { PYTHON, REPO, appFrame, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const dir = opt("--dir", null);
if (!dir) throw new Error("--dir FIXTURE_DIR (image.ome.tif, cells.csv)");
let site = opt("--site", null);
if (!site) {
    site = mkdtempSync(path.join(tmpdir(), "pxtable-site-"));
    execFileSync(PYTHON, ["-m", "plexora.client.static_build", "--out", site],
                 { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO }, stdio: ["ignore", "ignore", "inherit"] });
}

const out = { dir, at: {}, errors: [] };
const t0 = Date.now();
const mark = (name) => { out.at[name] = Date.now() - t0; };
const server = await startServer(["--site", site, "--tree", `fixture=${dir}`]);
const browser = await launch({ headed: args.includes("--headed") });
let crashed = false;
let page = null;
let frame = null;
try {
    page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    page.on("crash", () => { crashed = true; });
    page.on("pageerror", (error) => out.errors.push(`pageerror: ${error}`.slice(0, 300)));
    page.on("console", (message) => {
        const text = message.text();
        if (message.type() === "error") out.errors.push(text.slice(0, 300));
        else if (/abort|fatal|memory|out of|RangeError|pyodide/i.test(text)) (out.console ||= []).push(text.slice(0, 300));
    });
    await page.goto(`http://127.0.0.1:${server.port}/?pydebug`);
    frame = await appFrame(page);
    await frame.waitForSelector("#quick_view_go", { timeout: 120000 });
    const heap = () => page.evaluate(() => window.PlexoraPython.heap());
    out.heapBootMib = Math.round(await heap() / 2 ** 20);
    const paths = await page.evaluate(async () => {
        const names = ["image.ome.tif", "cells.csv"];
        const picked = await Promise.all(names.map(async (n) => new File([await (await fetch(`/fixture/${n}`)).blob()], n)));
        const mounted = await window.PlexoraPython.mount("/picked/fixture", picked);
        mounted.forEach((p, i) => window.PlexoraPicks.adopt(p, picked[i]));
        return mounted;
    });
    mark("mounted");
    const imported = await frame.evaluate(async (picks) => {
        const response = await fetch("import/sample", { method: "POST", headers: { "Content-Type": "application/json" },
                                                         body: JSON.stringify({ paths: picks }) });
        const text = await response.text();
        try { return { status: response.status, body: JSON.parse(text) }; }
        catch (error) { return { status: response.status, body: { error: text.slice(-800) } }; }
    }, paths);
    mark("imported");
    out.importStatus = imported.status;
    const name = imported.body && imported.body.name;
    if (!name) throw new Error(`import: ${JSON.stringify(imported.body).slice(0, 400)}`);
    out.project = name;

    await frame.evaluate((n) => { location.href = `/${n}`; }, name);
    frame = await appFrame(page);
    // Past envelope.json's table limit the open is held by the notice ("Large
    // for a browser tab"); the driver answers "Continue here", as a user who
    // keeps going would, and records that it was asked.
    out.notice = null;
    const full = () => frame.evaluate(() => {
        const viewer = window.__plexora?.seaDragonViewer?.viewer;
        if (!viewer || !viewer.world || !viewer.world.getItemCount()) return false;
        for (let i = 0; i < viewer.world.getItemCount(); i += 1) if (!viewer.world.getItemAt(i).getFullyLoaded()) return false;
        return true;
    }).catch(() => false);
    const answerNotice = () => frame.evaluate(() => {
        if (!document.body.innerText.includes("Large for a browser tab")) return null;
        const text = [...document.querySelectorAll("*")].find((node) => node.children.length === 0
            && /need about/.test(node.textContent || ""));
        const button = [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "Continue here");
        if (button) button.click();
        return { note: text ? text.textContent.trim() : null, answered: Boolean(button) };
    }).catch(() => null);
    const viewerUntil = Date.now() + Number(opt("--viewer-wait", "600")) * 1000;
    let viewerFull = false;
    while (Date.now() < viewerUntil && !(viewerFull = await full())) {
        if (!out.notice) {
            const asked = await answerNotice();
            if (asked) { out.notice = asked; mark("noticeAnswered"); }
        }
        await page.waitForTimeout(500);
    }
    if (!viewerFull) throw new Error("the viewer did not finish drawing in 10 minutes");
    mark("viewer");
    out.table = await frame.evaluate(async (n) => {
        for (let i = 0; i < 3600; i += 1) {
            const status = await (await fetch(`get_table_status?datasource=${encodeURIComponent(n)}`)).json();
            if (status.status === "ready" || status.status === "error" || status.status === "failed") return status;
            await new Promise((resolve) => setTimeout(resolve, 250));
        }
        return { status: "timed out" };
    }, name);
    mark("tableReady");
    out.heapTableMib = Math.round(await heap() / 2 ** 20);
    // Within the limit the notice never shows; it is judged up to 15 s after
    // the table's size is known, so look a little longer before saying so.
    const noticeUntil = Date.now() + 25000;
    while (!out.notice && Date.now() < noticeUntil) {
        const asked = await answerNotice();
        if (asked) { out.notice = asked; mark("noticeAnswered"); break; }
        await page.waitForTimeout(500);
    }
    out.notice = out.notice || false;

    const handOver = await page.evaluate((n) => window.PlexoraPython.run(`
import json, time
t = time.perf_counter()
try:
    from plexora import api
    frame = api.project_data(${JSON.stringify(n)}).table.frame()
    answer = {"rows": frame.height, "columns": frame.width, "s": round(time.perf_counter() - t, 2),
              "frame_mb": round(frame.estimated_size() / 1e6)}
except BaseException as error:
    answer = {"error": repr(error)[:400], "s": round(time.perf_counter() - t, 2)}
json.dumps(answer)
`), name).catch((error) => JSON.stringify({ error: `run: ${String(error.message || error).slice(0, 300)}` }));
    out.handOver = JSON.parse(handOver);
    mark("handedOver");
    out.heapAfterMib = Math.round(await heap().catch(() => 0) / 2 ** 20);
    // Still answering: Python (a gate on the routes) and the viewer.
    out.afterwards = await frame.evaluate(async (n) => {
        const response = await fetch(`plugins/gating/get_gated_cell_ids?${new URLSearchParams({
            filter: JSON.stringify({ CD3: [1000, 1e12] }), start_keys: "CellID", datasource: n })}`);
        const body = await response.json().catch(() => null);
        return { status: response.status, gated: Array.isArray(body) ? body.length : null };
    }, name).catch((error) => ({ error: String(error.message || error).slice(0, 300) }));
    mark("afterwards");
    out.jsHeapMb = await page.evaluate(() => (performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1e6) : null));
} catch (error) {
    out.error = String(error.message || error).slice(0, 600);
    // What Python was doing when it stopped, and what the page says.
    if (page && !crashed) {
        out.python = await page.evaluate(async () => ({
            state: await Promise.race([window.PlexoraPython.state(), new Promise((r) => setTimeout(() => r("no answer in 5 s"), 5000))]),
            heapMib: Math.round((await Promise.race([window.PlexoraPython.heap(), new Promise((r) => setTimeout(() => r(0), 5000))])) / 2 ** 20),
            recent: window.PlexoraPython.recent().slice(-8).map((r) => ({ path: r.path, status: r.status, ms: r.ms, py: r.pyMs })),
            tasks: window.PlexoraPython.tasks().slice(-6),
        })).catch((caught) => String(caught).slice(0, 300));
        out.text = frame ? await frame.evaluate(() => document.body.innerText.slice(0, 600)).catch(() => null) : null;
        // Python failed for good: the page says so (handoffNotice's "stopped").
        out.stopped = frame ? await frame.evaluate(async () => {
            for (let i = 0; i < 40; i += 1) {
                const text = document.body.innerText;
                if (/ran out of memory|stopped in this tab/.test(text)) {
                    return { title: /ran out of memory/.test(text) ? "This tab ran out of memory" : "Plexora stopped in this tab",
                             buttons: [...document.querySelectorAll("button")].map((b) => b.textContent.trim())
                                 .filter((label) => ["Open in Desktop App", "Reload"].includes(label)) };
                }
                await new Promise((r) => setTimeout(r, 250));
            }
            return null;
        }).catch(() => null) : null;
        const within = (promise, ms) => Promise.race([promise, new Promise((r) => setTimeout(() => r("no answer"), ms))]);
        out.stillAnswering = {
            python: await within(page.evaluate(() => window.PlexoraPython.run("1 + 1")).catch((e) => `error: ${String(e).slice(0, 200)}`), 10000),
            table: frame ? await within(frame.evaluate(async (n) => (await fetch(`get_table_status?datasource=${n}`)).json(), out.project).catch((e) => String(e).slice(0, 200)), 10000) : null,
            load: frame ? await within(frame.evaluate(async (n) => (await fetch(`get_load_status?datasource=${n}`)).json(), out.project).catch((e) => String(e).slice(0, 200)), 10000) : null,
            // The same question to the app itself, inside Python (no bridge),
            // and through the page's Python client (the bridge, no relay).
            direct: await within(page.evaluate((n) => window.PlexoraPython.run(`
import json
from plexora import app
r = app.test_client().get("/get_table_status?datasource=${n}")
json.dumps({"status": r.status_code, "type": r.headers.get("Content-Type"), "encoding": r.headers.get("Content-Encoding"), "head": r.get_data()[:120].decode("latin-1")})
`), out.project).catch((e) => `error: ${String(e).slice(0, 200)}`), 20000),
            client: await within(page.evaluate(async (n) => {
                const r = await window.PlexoraPython.fetch(`/get_table_status?datasource=${n}`);
                const bytes = new Uint8Array(await r.arrayBuffer());
                return { status: r.status, headers: [...r.headers.entries()], bytes: bytes.length,
                         head: [...bytes.slice(0, 24)].map((b) => b.toString(16).padStart(2, "0")).join("") };
            }, out.project).catch((e) => `error: ${String(e).slice(0, 200)}`), 20000),
            recent: await page.evaluate(() => window.PlexoraPython.recent().filter((r) => !/health|viewer\/sessions/.test(r.path)).slice(-12)
                .map((r) => ({ path: r.path, status: r.status, bytes: r.bytes, ms: r.ms, py: r.pyMs }))).catch(() => null),
        };
    }
} finally {
    out.crashed = crashed;
    await browser.close().catch(() => {});
    server.stop();
}
console.log(JSON.stringify(out, null, 1));
