// bench.mjs - Video Studio scenarios against a running (licensed) server.
//
//   node tools/bench_video/bench.mjs --base http://127.0.0.1:PORT --plan plan.json --out results.json
//        [--keep DIR] [--headed]
//
// A plan is {scenarios: [{name, dataset, preset, params, settings, route, preview_s?}]}.
// Each scenario opens the dataset in GPU Chrome (bench3d's Browser: Metal
// through ANGLE, refusing software GL), opens Video Studio, builds the preset,
// optionally measures preview frame pacing, exports through the chosen route,
// and records the export's own timings (from its manifest), the wall time,
// the bytes and whether the viewer came back exactly. Browser-encoded files
// are saved into --keep (never ~/Downloads).
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Browser, sleep } = await import(path.join(HERE, "..", "bench3d", "drive.mjs"));

function args(argv) {
    const out = { headed: false, keep: "" };
    for (let i = 0; i < argv.length; i += 1) {
        const a = argv[i];
        if (a === "--base") out.base = argv[++i];
        else if (a === "--plan") out.plan = argv[++i];
        else if (a === "--out") out.out = argv[++i];
        else if (a === "--keep") out.keep = argv[++i];
        else if (a === "--headed") out.headed = true;
    }
    return out;
}

const opts = args(process.argv.slice(2));
const plan = JSON.parse(readFileSync(opts.plan, "utf8"));
const results = { started_at: new Date().toISOString(), scenarios: [] };

for (const scenario of plan.scenarios) {
    const browser = new Browser({ headed: opts.headed });
    await browser.launch();
    const record = { name: scenario.name, dataset: scenario.dataset, preset: scenario.preset,
                     settings: scenario.settings, route: scenario.route };
    try {
        record.webgl = await browser.renderer?.().catch(() => null);
        if (opts.keep) {
            const dir = path.resolve(opts.keep, scenario.name);
            mkdirSync(dir, { recursive: true });
            await browser.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: dir });
            await browser.send("Page.setDownloadBehavior", { behavior: "allow", downloadPath: dir });
        } else {
            await browser.send("Browser.setDownloadBehavior", { behavior: "deny" });
        }
        await browser.send("Page.navigate", { url: `${opts.base}/${scenario.dataset}` });
        await browser.waitFor("Boolean(window.__plexora && document.querySelector('#viewer_tool_launchers'))", 120000);
        await sleep(2500);
        await browser.eval(() => document.querySelector("#viewer_tool_launchers [data-tool=video_studio]").click());
        await browser.waitFor("Boolean(window.PlexoraVideoStudio && window.PlexoraVideoStudio.ready())", 60000);
        await browser.eval((name) => window.PlexoraVideoStudio.choosePreset(name), scenario.preset);
        await sleep(400);
        await browser.eval(() => window.PlexoraVideoStudio.controller.preview.pause());
        if (scenario.params) await browser.eval((p) => window.PlexoraVideoStudio.setParams(p), scenario.params);
        if (scenario.preview_s) {
            record.preview = await browser.eval((seconds) => new Promise((resolve) => {
                const studio = window.PlexoraVideoStudio.controller;
                const gaps = [];
                let last = performance.now();
                let raf = 0;
                const tick = (now) => { gaps.push(now - last); last = now; raf = requestAnimationFrame(tick); };
                studio.preview.play();
                raf = requestAnimationFrame(tick);
                setTimeout(() => {
                    cancelAnimationFrame(raf);
                    studio.preview.pause();
                    gaps.sort((a, b) => a - b);
                    const pick = (q) => gaps[Math.min(gaps.length - 1, Math.floor(q * gaps.length))];
                    resolve({ frames: gaps.length, fps: Math.round((1000 * gaps.length) / (seconds * 1000)),
                              p50_ms: Math.round(pick(0.5) * 10) / 10, p95_ms: Math.round(pick(0.95) * 10) / 10 });
                }, seconds * 1000);
            }), scenario.preview_s);
        }
        await browser.eval(() => window.PlexoraVideoStudio.controller.preview.end());
        await sleep(600);
        const before = await browser.eval(() => JSON.stringify(window.PlexoraVideoViewerState.digest(
            window.PlexoraVideoViewerState.snapshot(window.PlexoraVideoStudio.controller.env))));
        const routes = await browser.eval((s) => window.PlexoraVideoStudio.routes(s), scenario.settings);
        const route = routes.find((r) => r.format === scenario.settings.format && r.available
                                      && (!scenario.route || r.route === scenario.route
                                          || r.writer === scenario.route));
        if (!route) throw new Error(`no route for ${scenario.settings.format}/${scenario.route}`);
        record.route_used = `${route.route}/${route.writer}`;
        const t0 = Date.now();
        const done = await browser.eval((s, r) => window.PlexoraVideoStudio.exportNow(s, r)
            .then((res) => ({ ok: true, name: res?.name, bytes: res?.bytes, kind: res?.kind,
                              exportId: res?.exportId || null, manifest: res?.manifest || null }))
            .catch((e) => ({ ok: false, error: String(e && (e.message || e)) })), scenario.settings, route);
        record.wall_s = (Date.now() - t0) / 1000;
        const { name: file, ...rest } = done;
        Object.assign(record, rest, { file });
        const state = await browser.eval(() => window.PlexoraVideoStudio.state());
        record.video_id = state.saved.videoId;
        if (!record.manifest && record.exportId && record.video_id) {
            const reply = await fetch(`${opts.base}/plugins/video_studio/api/videos/${record.video_id}`
                                      + `/exports/${record.exportId}/manifest`);
            if (reply.ok) record.manifest = (await reply.json()).manifest;
        }
        await sleep(1200);
        const after = await browser.eval(() => JSON.stringify(window.PlexoraVideoViewerState.digest(
            window.PlexoraVideoViewerState.snapshot(window.PlexoraVideoStudio.controller.env))));
        record.restored = before === after;
        record.console_errors = browser.logs.filter((l) => /\[(error|exception)\]/.test(l)).slice(0, 5);
    } catch (error) {
        record.ok = false;
        record.error = String(error.stack || error);
    } finally {
        browser.close();
    }
    if (record.manifest) {
        const m = record.manifest;
        record.frames = m.output?.frame_count;
        record.timing = m.timing;
        record.unresolved = (m.integrity?.unresolved_frames || []).length;
        record.verified = m.integrity?.acquired_verified;
        delete record.manifest;
    }
    console.log(JSON.stringify(record));
    results.scenarios.push(record);
}
writeFileSync(opts.out, JSON.stringify(results, null, 2));
process.exit(0);
