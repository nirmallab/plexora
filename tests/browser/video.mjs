// Video Studio's exports on the website and on the desktop, from the real
// panel in Chrome (plan L10). One small calibrated image (smoke_fixture.py) is
// opened in each arm, Video Studio's zoom preset is made from it, and each
// format is exported the way the Export dialog would export it -- the route it
// would pick, the panel's own render loop, the file saved as a user's
// download -- and kept for the checks (tests/test_browser_video.py):
//
//   website  the static site + the website's Python bundle (serve.py), licensed
//            through Settings > Account against the stand-in platform; the
//            image picked from this computer;
//   desktop  a CPython Plexora with a test licence (tools/bench_video/
//            serve_licensed.py) and the desktop's FFmpeg (PLEXORA_FFMPEG), the
//            image opened by its path.
//
// Before the exports the Export dialog is opened and its format list read:
// which formats it offers, how each would be written, and what a format it
// cannot write says instead (the website's hand-off notice for ProRes).
//
//   PLEXORA_BENCH_HOME=<bundle home> PLEXORA_FFMPEG=<ffmpeg> node tests/browser/video.mjs \
//       --out DIR [--arms website,desktop] [--formats mp4,webm,mov,gif,png] \
//       [--size 320x180] [--duration 1] [--fps 30] [--headed]
//
// Writes DIR/record.json and DIR/<arm>-<format>.<ext>; prints the record.
import { execFileSync, spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { PYTHON, REPO, appFrame, launch, startServer } from "./lib.mjs";
import { buildSite, connectLicence, keepInBrowser, startMock } from "./ai_lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const outDir = path.resolve(opt("--out", mkdtempSync(path.join(tmpdir(), "pxvid-out-"))));
const arms = opt("--arms", "website,desktop").split(",").filter(Boolean);
const formats = opt("--formats", "mp4,webm,mov,gif,png").split(",").filter(Boolean);
const [width, height] = opt("--size", "320x180").split("x").map(Number);
const duration = Number(opt("--duration", "1"));
const fps = Number(opt("--fps", "30"));
const headed = args.includes("--headed");
mkdirSync(outDir, { recursive: true });

const temp = mkdtempSync(path.join(tmpdir(), "pxvid-"));
const fixture = execFileSync(PYTHON, [path.join(REPO, "tests", "browser", "smoke_fixture.py"), path.join(temp, "fixture")],
                             { cwd: REPO, encoding: "utf-8" }).trim().split("\n").pop().trim();

const EXT = { mp4: "mp4", webm: "webm", mov: "mov", gif: "gif", png: "zip" };
const record = { size: [width, height], fps, duration_s: duration, fixture: path.basename(fixture), arms: {} };
const log = (text) => process.stderr.write(`${text}\n`);

/** The settings the Export dialog would hand the render loop for `format`. */
function settingsFor(format) {
    return { width, height, fps: format === "gif" ? 15 : fps, format, quality: "good",
             transparent: format === "mov", overlays: { scalebar: true, label: true, disclosure: false },
             attribution: "" };
}

function freePort() {
    return new Promise((resolve) => {
        const probe = net.createServer();
        probe.listen(0, "127.0.0.1", () => {
            const { port } = probe.address();
            probe.close(() => resolve(port));
        });
    });
}

/** The desktop: a CPython Plexora with a test licence and the desktop's FFmpeg. */
async function startDesktop() {
    const port = await freePort();
    const data = path.join(temp, "desktop-data");
    const licence = path.join(temp, "desktop-licence");
    mkdirSync(data, { recursive: true });
    mkdirSync(licence, { recursive: true });
    const child = spawn(PYTHON, [path.join(REPO, "tools", "bench_video", "serve_licensed.py"), REPO, data, String(port), licence],
                        { cwd: REPO, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, PYTHONPATH: REPO } });
    child.stderr.on("data", () => null);
    await new Promise((resolve, reject) => {
        let buffer = "";
        child.stdout.on("data", (chunk) => {
            buffer += chunk;
            if (buffer.includes("Serving on")) resolve();
        });
        child.on("exit", (code) => reject(new Error(`serve_licensed.py exited ${code}`)));
    });
    for (let i = 0; i < 120; i += 1) {
        try {
            const response = await fetch(`http://127.0.0.1:${port}/health`);
            if (response.status < 500) break;
        } catch { /* not yet */ }
        await new Promise((resolve) => setTimeout(resolve, 250));
    }
    return { origin: `http://127.0.0.1:${port}`, stop: () => child.kill() };
}

const viewerFull = (frame) => frame.waitForFunction(() => {
    const viewer = window.__plexora?.seaDragonViewer?.viewer;
    if (!viewer || !viewer.world || !viewer.world.getItemCount()) return false;
    for (let i = 0; i < viewer.world.getItemCount(); i += 1) {
        if (!viewer.world.getItemAt(i).getFullyLoaded()) return false;
    }
    return true;
}, null, { timeout: 120000, polling: 250 });

/** Import the fixture as the Import Sample dialog does; returns the project's name. */
async function importFixture(page, frame, arm) {
    let paths = [fixture];
    if (arm === "website") {
        await page.evaluate(() => { window.showOpenFilePicker = undefined; });
        await page.evaluate(() => {
            const input = document.createElement("input");
            input.type = "file";
            input.id = "pxvid-pick";
            input.style.display = "none";
            document.body.append(input);
        });
        await page.setInputFiles("#pxvid-pick", [fixture]);
        paths = await page.evaluate(async () => {
            const picked = [...document.getElementById("pxvid-pick").files];
            const mounted = await window.PlexoraPython.mount("/picked/pxvid", picked);
            mounted.forEach((p, i) => window.PlexoraPicks.adopt(p, picked[i]));
            return mounted;
        });
    }
    const result = await frame.evaluate(async (picks) => {
        const post = async (route, body) => {
            const response = await fetch(route, { method: "POST", headers: { "Content-Type": "application/json" },
                                                  body: JSON.stringify(body) });
            return { status: response.status, body: await response.json() };
        };
        const inspected = await post("import/inspect", { paths: picks });
        const imported = await post("import/sample", { paths: picks });
        return { inspected: inspected.status, imported };
    }, paths);
    const name = result.imported.body?.name;
    if (!name) throw new Error(`import failed: ${JSON.stringify(result).slice(0, 400)}`);
    return name;
}

/** The Export dialog's format list as a user sees it. */
async function readDialog(frame) {
    await frame.evaluate(() => { window.__pxvidDialog = window.PlexoraVideoStudio.controller.exportNow(); });
    await frame.waitForSelector("dialog.vs-dialog[open] .vs-format", { timeout: 30000 });
    await frame.waitForTimeout(300);
    const rows = await frame.evaluate(() => [...document.querySelectorAll("dialog.vs-dialog .vs-format")].map((node) => ({
        format: node.dataset.format,
        disabled: node.disabled,
        handoff: node.dataset.handoff === "1",
        name: node.querySelector(".vs-format-name")?.textContent || "",
        how: node.querySelector(".vs-format-how")?.textContent || "",
    })));
    // A format only the desktop app can write: choosing it shows the notice.
    let notice = null;
    if (rows.some((row) => row.handoff)) {
        await frame.click("dialog.vs-dialog .vs-format[data-handoff='1']");
        notice = await frame.waitForSelector("dialog.vs-dialog .plx-handoff", { timeout: 5000 })
            .then(() => frame.evaluate(() => {
                const box = document.querySelector("dialog.vs-dialog .plx-handoff");
                return { title: box.querySelector(".plx-handoff-title")?.textContent || "",
                         why: box.querySelector(".plx-handoff-why")?.textContent || "",
                         actions: [...box.querySelectorAll("[data-handoff-action]")].map((b) => b.dataset.handoffAction),
                         exportDisabled: document.querySelector("dialog.vs-dialog .vs-primary")?.disabled ?? null };
            }), () => null);
    }
    await frame.evaluate(() => [...document.querySelectorAll("dialog.vs-dialog .vs-button")]
        .find((b) => b.textContent.trim() === "Cancel")?.click());
    await frame.evaluate(() => window.__pxvidDialog);
    return { rows, notice };
}

/** One export through the panel, its file caught as the user's download. */
async function exportFormat(page, frame, arm, format) {
    const settings = settingsFor(format);
    const routes = await frame.evaluate((s) => window.PlexoraVideoStudio.routes(s), settings);
    const mine = routes.filter((r) => r.format === format);
    const route = mine.find((r) => r.available);
    const row = { format, routes: mine.map((r) => ({ route: r.route, writer: r.writer, available: r.available,
                                                     reason: r.reason || "" })) };
    if (!route) {
        row.ok = false;
        row.unavailable = true;
        row.reason = mine.map((r) => r.reason).filter(Boolean)[0] || "no route";
        return row;
    }
    row.route = { route: route.route, writer: route.writer, label: route.label };
    const file = path.join(outDir, `${arm}-${format}.${EXT[format]}`);
    const downloads = [];
    const onDownload = (download) => downloads.push(download);
    page.on("download", onDownload);
    const started = Date.now();
    try {
        const result = await frame.evaluate(async ([s, r]) => {
            const done = await window.PlexoraVideoStudio.exportNow(s, r);
            if (!done) return { ok: false, error: "stopped" };
            if (done.kind === "download" && done.url) {
                // The toast's Download, as a user presses it.
                const link = document.createElement("a");
                link.href = done.url;
                link.download = done.name || "";
                document.body.appendChild(link);
                link.click();
                link.remove();
            }
            return { ok: true, kind: done.kind, bytes: done.bytes, name: done.name || null,
                     encoder: done.encoder || null };
        }, [settings, route]);
        row.renderMs = Date.now() - started;
        Object.assign(row, result);
        for (let i = 0; i < 600 && !downloads.length; i += 1) await page.waitForTimeout(100);
        if (!downloads.length) throw new Error("no download arrived");
        await downloads[0].saveAs(file);
        row.ms = Date.now() - started;
        row.file = path.basename(file);
        row.suggested = downloads[0].suggestedFilename();
    } catch (error) {
        row.ok = false;
        row.error = String(error.message || error).slice(0, 600);
    } finally {
        page.off("download", onDownload);
    }
    return row;
}

async function runArm(arm) {
    const out = { exports: {}, errors: [] };
    record.arms[arm] = out;
    let server = null;
    let mock = null;
    let origin;
    if (arm === "website") {
        // Video Studio is Pro's (`plugin:video_studio`): the stand-in grants it.
        mock = await startMock(path.join(temp, "mock-platform"), ["--roots", "ai,volume,plugin:video_studio"]);
        const site = path.join(temp, "site");
        buildSite(site, path.join(temp, "site-data"), mock);
        server = await startServer(["--site", site]);
        origin = `http://127.0.0.1:${server.port}`;
    } else {
        server = await startDesktop();
        origin = server.origin;
    }
    const browser = await launch({ headed });
    let page = null;
    try {
        page = await browser.context.newPage();
        await page.setViewportSize({ width: 1440, height: 900 });
        page.on("pageerror", (error) => out.errors.push(`pageerror: ${String(error).slice(0, 300)}`));
        page.on("console", (message) => {
            if (message.type() === "error") out.errors.push(message.text().slice(0, 300));
        });
        let frame;
        if (arm === "website") {
            await connectLicence(page, origin);
            await page.goto(`${origin}/`);
            frame = await appFrame(page);
            await keepInBrowser(frame);
            await page.evaluate(() => window.PlexoraPython.ready);
        } else {
            await page.goto(`${origin}/`);
            frame = page.mainFrame();
            await frame.waitForLoadState("domcontentloaded");
        }
        log(`${arm}: up`);
        const name = await importFixture(page, frame, arm);
        out.project = name;
        log(`${arm}: imported ${name}`);
        await frame.evaluate((n) => { location.href = `/${encodeURIComponent(n)}?tool=video_studio`; }, name);
        if (arm === "website") frame = await appFrame(page);
        else await page.waitForLoadState("domcontentloaded");
        await viewerFull(frame);
        await frame.waitForFunction(() => window.PlexoraVideoStudio && window.PlexoraVideoStudio.ready(), null,
                                    { timeout: 90000, polling: 250 });
        await frame.evaluate(async (seconds) => {
            const studio = window.PlexoraVideoStudio;
            if (studio.state().preset !== "zoom") await studio.choosePreset("zoom");
            await studio.setParams({ duration_s: seconds, hold_end_s: 0, factor: 2 });
            studio.controller.preview.pause();
            await studio.controller.preview.end();
        }, duration);
        await frame.waitForTimeout(500);
        out.crossOriginIsolated = await frame.evaluate(() => Boolean(window.crossOriginIsolated));
        // What the video was made from: the image's facts and the scene's view.
        out.scene = await frame.evaluate(() => {
            const studio = window.PlexoraVideoStudio;
            const project = studio.state().project;
            const facts = studio.controller.facts || {};
            const viewer = window.__plexora?.seaDragonViewer?.viewer;
            const item = viewer?.world?.getItemAt(0);
            return { viewport: project.base?.viewport || null, mode: project.base?.view?.mode || null,
                     pixel_size: facts.pixel_size ?? null, image_size: facts.image_size || null,
                     contentSize: item ? [item.getContentSize().x, item.getContentSize().y] : null,
                     bounds: viewer ? viewer.viewport.getBounds() : null };
        });
        if (args.includes("--debug")) {
            log(`${arm}: scene ${JSON.stringify(out.scene)}`);
            const probe = await frame.evaluate(async () => {
                const studio = window.PlexoraVideoStudio;
                const env = studio.controller.env;
                const project = studio.state().project;
                const A = window.PlexoraVideoAnimation;
                const frames = A.frames(project, 30);
                const states = frames.map((f) => A.evaluate(project, f.t, { cache: {} }));
                const C = window.PlexoraVideoRenderCoordinator;
                const session = await C.open(env, { width: 320, height: 180, transparent: false, overlays: { scalebar: true } });
                const viewer = window.__plexora.seaDragonViewer.viewer;
                const out = { before: viewer.viewport.getBounds(), wrapper: env.wrapper.getBoundingClientRect().toJSON() };
                try {
                    await session.prefetch.prime(project, states);
                    const { canvas } = await C.frame(session, states[0], { timeoutMs: 20000, extra: {} });
                    out.after = viewer.viewport.getBounds();
                    out.zoom = viewer.viewport.getZoom();
                    out.state0 = states[0].view;
                    out.cam = states[0].camera2d || states[0].viewport || null;
                    const src = env.iv.exportSources();
                    out.drawer = src.drawer ? { w: src.drawer.width, h: src.drawer.height,
                                                rect: src.drawer.getBoundingClientRect().toJSON() } : null;
                    out.url = canvas.toDataURL("image/png");
                    out.drawerUrl = src.drawer.toDataURL("image/png");
                } catch (error) { out.error = String(error); }
                await C.close(session);
                return out;
            });
            const { url, drawerUrl, ...rest } = probe;
            log(`${arm}: probe ${JSON.stringify(rest)}`);
            if (url) writeFileSync(path.join(outDir, `${arm}-probe.png`), Buffer.from(url.split(",")[1], "base64"));
            if (drawerUrl) writeFileSync(path.join(outDir, `${arm}-probe-drawer.png`), Buffer.from(drawerUrl.split(",")[1], "base64"));
        }
        out.dialog = await readDialog(frame);
        log(`${arm}: dialog ${JSON.stringify(out.dialog.rows.map((r) => [r.format, r.disabled, r.how]))}`);
        for (const format of formats) {
            const row = await exportFormat(page, frame, arm, format);
            out.exports[format] = row;
            log(`${arm}: ${format} ${row.ok ? "ok" : "FAILED"} ${row.route ? row.route.route + "/" + row.route.writer : ""} `
                + `${row.ms || ""} ms ${row.bytes || ""} B ${row.error || row.reason || ""}`);
        }
        out.videos = await frame.evaluate(async () => {
            const studio = window.PlexoraVideoStudio;
            const id = studio.state().saved.videoId;
            const ds = studio.controller.env.datasource;
            const listed = await studio.controller.api.list(ds);
            return { id, count: (listed.videos || []).length };
        }).catch((error) => String(error));
    } catch (error) {
        out.error = String(error.stack || error).slice(0, 1500);
        log(`${arm}: ${out.error}`);
        if (page) {
            await page.screenshot({ path: path.join(outDir, `${arm}-failed.png`) }).catch(() => null);
            const framed = await page.$("#plexora-app");
            const frame = framed ? await framed.contentFrame() : page.mainFrame();
            out.where = await frame.evaluate(() => ({
                at: location.pathname + location.search,
                launchers: [...document.querySelectorAll("#viewer_tool_launchers [data-tool]")].map((n) => n.dataset.tool),
                studio: Boolean(window.PlexoraVideoStudio),
                text: document.body ? document.body.innerText.slice(0, 600) : "",
            })).catch((caught) => String(caught));
            log(`${arm}: ${JSON.stringify(out.where)}`);
        }
    } finally {
        await browser.close();
        server.stop();
        mock?.stop();
    }
}

for (const arm of arms) await runArm(arm);
writeFileSync(path.join(outDir, "record.json"), JSON.stringify(record, null, 1));
console.log(JSON.stringify(record, null, 1));
