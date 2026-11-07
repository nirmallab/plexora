// A whole project on the website -- image, mask and cell table from this
// computer -- and every tool opened on it: the feature-parity smoke test.
// The files are mounted as a pick would mount them (the picker itself is
// import.mjs's), imported as the Import Sample dialog imports them, and then
// the viewer and each tool are opened the way a user opens them (?tool=).
// Prints JSON: per step, how long, the console errors and failed requests;
// --shots DIR keeps a screenshot of each.
//
//   node tests/browser/project.mjs --dir FIXTURE_DIR [--files a,b,c] [--tools gating,qc]
import path from "node:path";
import { REPO, appFrame, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const dir = opt("--dir", "C:/plexora-bench/browser_node/cache/p9-P9S/root/_gsynth_files");
const files = opt("--files", "image.ome.tif,mask.tif,cells.csv").split(",");
const tools = opt("--tools", "gating,qc,roi,cell_explorer,figure_builder,video_studio").split(",").filter(Boolean);
const shots = opt("--shots", null);
const site = opt("--site", path.join(REPO, "plexora", "client", "dist", "static"));

const server = await startServer(["--site", site, "--tree", `fixture=${dir}`]);
const browser = await launch({ headed: args.includes("--headed") });
const served = {};
const out = { steps: {}, served };
let errors = [];
let failed = [];
const step = async (name, work, frame, page) => {
    errors = [];
    failed = [];
    const started = Date.now();
    let error = null;
    try { await work(); } catch (caught) { error = String(caught.message || caught).slice(0, 500); }
    const record = { ms: Date.now() - started, error, errors: errors.slice(0, 12), failed: failed.slice(0, 12) };
    if (error && page) {
        // What Python was doing: its last requests and steps, from the host.
        record.python = await page.evaluate(async () => ({
            recent: window.PlexoraPython.recent().slice(-6),
            steps: window.PlexoraPython.steps().slice(-6),
            tasks: window.PlexoraPython.tasks().slice(-8),
            now: Math.round(performance.now()),
            state: await window.PlexoraPython.state(),
        })).catch((caught) => String(caught));
    }
    if (frame) {
        record.text = await frame.evaluate(() => document.body ? document.body.innerText.slice(0, 300) : "")
            .catch(() => null);
    }
    if (shots && page) await page.screenshot({ path: path.join(shots, `project-${name}.png`) });
    out.steps[name] = record;
    return record;
};
try {
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    page.on("pageerror", (error) => errors.push(`pageerror: ${error}`.slice(0, 300)));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text().slice(0, 300)); });
    page.on("response", (response) => {
        const tile = response.url().match(/\/generated\/data\/.*\.png\?(.*)$/);
        if (tile && response.status() === 200) {
            const by = response.headers()["x-plexora-served-by"] || "python";
            const key = `${new URLSearchParams(tile[1]).get("q") || "8bit"}:${by}`;
            served[key] = (served[key] || 0) + 1;
        }
        if (response.status() >= 400 && !response.url().includes("/update/check")) {
            failed.push(`${response.status()} ${response.url().replace(/^https?:\/\/[^/]+/, "")}`);
        }
    });
    await page.goto(`http://127.0.0.1:${server.port}/`);
    let frame = await appFrame(page);
    let name = null;
    await step("import", async () => {
        const paths = await page.evaluate(async (names) => {
            const picked = await Promise.all(names.map(async (n) => new File(
                [await (await fetch(`/fixture/${n}`)).blob()], n)));
            const mounted = await window.PlexoraPython.mount("/picked/fixture", picked);
            // As a pick would leave them: the page's own readers can open them.
            mounted.forEach((p, i) => window.PlexoraPicks.adopt(p, picked[i]));
            return mounted;
        }, files);
        const result = await frame.evaluate(async (picks) => {
            const post = async (route, body) => {
                const response = await fetch(route, { method: "POST", headers: { "Content-Type": "application/json" },
                                                      body: JSON.stringify(body) });
                return { status: response.status, body: await response.json() };
            };
            const inspected = await post("import/inspect", { paths: picks });
            const imported = await post("import/sample", { paths: picks });
            return { inspected, imported };
        }, paths);
        out.inspect = { status: result.inspected.status,
                        layers: (result.inspected.body.samples || []).flatMap((s) => (s.layers || []).map((l) => l.kind)),
                        missing: (result.inspected.body.samples || []).flatMap((s) => s.missing || []),
                        questions: (result.inspected.body.samples || []).flatMap((s) => (s.questions || []).map((q) => q.key || q.id)) };
        out.importStatus = result.imported.status;
        name = result.imported.body.name;
        if (!name) throw new Error(`import failed: ${JSON.stringify(result.imported.body).slice(0, 300)}`);
    }, frame, page);
    const viewerFull = async () => frame.waitForFunction(() => {
        const viewer = window.__plexora?.seaDragonViewer?.viewer;
        if (!viewer || !viewer.world || !viewer.world.getItemCount()) return false;
        for (let i = 0; i < viewer.world.getItemCount(); i += 1) {
            if (!viewer.world.getItemAt(i).getFullyLoaded()) return false;
        }
        return true;
    }, null, { timeout: 120000, polling: 250 });
    if (name) {
        await step("viewer", async () => {
            await frame.evaluate((n) => { location.href = `/${n}`; }, name);
            frame = await appFrame(page);
            await viewerFull();
            out.loadStatus = await frame.evaluate(async () => (await (await fetch("get_load_status")).json()));
            out.tableStatus = await frame.evaluate(async () => (await (await fetch("get_table_status")).json()));
        }, frame, page);
        for (const tool of tools) {
            await step(`tool-${tool}`, async () => {
                await frame.evaluate(([n, t]) => { location.href = `/${n}?tool=${t}`; }, [name, tool]);
                frame = await appFrame(page);
                await viewerFull();
                await frame.waitForTimeout(2500);
            }, frame, page);
        }
    }
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out, null, 1));
