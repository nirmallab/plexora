/**
 * Video Studio end to end in Chrome, against a running scratch server: the
 * driver half of plexora/plugins/video_studio/tests/test_video_studio_browser.py.
 *
 *     node tests/js/video_studio_export.mjs BASE DATASET
 *
 * Opens the dataset, opens Video Studio from its launcher, and runs:
 *   sweep     a z-sweep, one plane per frame, as PNG frames on the server
 *   orbit     a 16-frame 360° loop, twice -- the two exports must be identical
 *   cancel    an export stopped after three frames
 * recording the viewer's state before and after each. Prints one JSON object.
 * Skips (prints {"skip": reason}) when Chrome or WebGL2 is missing.
 */
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CHROME = process.env.PLEXORA_BENCH_CHROME
    || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const [base, dataset] = process.argv.slice(2);
if (!existsSync(CHROME)) {
    console.log(JSON.stringify({ skip: "Chrome is not installed" }));
    process.exit(0);
}
const { Browser, sleep } = await import(path.join(HERE, "..", "..", "tools", "bench3d", "drive.mjs"));
const browser = new Browser({ headed: false });
await browser.launch();
const out = {};

const digest = () => browser.eval(() => JSON.stringify(window.PlexoraVideoViewerState.digest(
    window.PlexoraVideoViewerState.snapshot(window.PlexoraVideoStudio.controller.env))));

async function exportWith(preset, params, settings) {
    await browser.eval(async (name) => {
        const studio = window.PlexoraVideoStudio;
        if (studio.state().preset !== name) await studio.choosePreset(name);
        studio.controller.preview.pause();
        await studio.controller.preview.end();
    }, preset);
    await browser.eval((p) => window.PlexoraVideoStudio.setParams(p), params);
    await sleep(500);
    const before = await digest();
    const result = await browser.eval((s) => window.PlexoraVideoStudio.routes(s).then((routes) => {
        const route = routes.find((r) => r.format === s.format && r.available && r.route === "server");
        return window.PlexoraVideoStudio.exportNow(s, route)
            .then((res) => ({ ok: true, exportId: res.exportId, bytes: res.bytes }))
            .catch((e) => ({ ok: false, error: String(e && (e.name + ": " + e.message)) }));
    }), settings);
    await sleep(1000);
    const after = await digest();
    const state = await browser.eval(() => window.PlexoraVideoStudio.state());
    return { ...result, videoId: state.saved.videoId, restored: before === after,
             cover: await browser.eval(() => Boolean(document.querySelector(".vs-render-cover"))) };
}

try {
    await browser.send("Browser.setDownloadBehavior", { behavior: "deny" });
    await browser.send("Page.navigate", { url: `${base}/${dataset}` });
    const up = await browser.waitFor("Boolean(window.__plexora && document.querySelector('#viewer_tool_launchers'))", 90000);
    if (!up) throw new Error("the viewer did not load");
    const webgl2 = await browser.eval(() => Boolean(document.createElement("canvas").getContext("webgl2")));
    if (!webgl2) {
        console.log(JSON.stringify({ skip: "no WebGL2 in this Chrome" }));
        process.exit(0);
    }
    await sleep(2000);
    out.launcher = await browser.eval(() => document.querySelector("#viewer_tool_launchers [data-tool=video_studio]")?.title || null);
    await browser.eval(() => document.querySelector("#viewer_tool_launchers [data-tool=video_studio]").click());
    out.ready = await browser.waitFor("Boolean(window.PlexoraVideoStudio && window.PlexoraVideoStudio.ready())", 60000);
    const small = { width: 320, height: 180, fps: 30, format: "png", quality: "good",
                    overlays: { scalebar: true, label: true } };
    out.sweep = await exportWith("z_sweep", { ms_per_plane: 1000 / 30, hold_ends_s: 0 }, small);
    const orbit = { duration_s: 16 / 30, loop: true, axis: "y" };
    out.orbitA = await exportWith("orbit", orbit, small);
    out.orbitB = await exportWith("orbit", orbit, small);
    // Cancel: stop the export after three frames.
    await browser.eval((p) => window.PlexoraVideoStudio.setParams(p), { duration_s: 4 });
    await sleep(400);
    const cancelled = browser.eval((s) => window.PlexoraVideoStudio.routes(s).then((routes) => {
        const route = routes.find((r) => r.format === s.format && r.available && r.route === "server");
        // A stopped export resolves to null (the bar says "Export stopped").
        return window.PlexoraVideoStudio.exportNow(s, route).then((res) => ({ ok: Boolean(res), stopped: res === null }))
            .catch((e) => ({ ok: false, error: String(e && e.name) }));
    }), small);
    const t0 = Date.now();
    await browser.waitFor("window.PlexoraVideoStudio.state().exporting && "
                          + "(window.PlexoraVideoStudio.state().progress || {}).done >= 3", 60000);
    await browser.eval(() => window.PlexoraVideoStudio.cancel());
    const stopped = await cancelled;
    out.cancel = { ...stopped, seconds: (Date.now() - t0) / 1000,
                   cover: await browser.eval(() => Boolean(document.querySelector(".vs-render-cover"))),
                   videoId: await browser.eval(() => window.PlexoraVideoStudio.state().saved.videoId) };
    out.console_errors = browser.logs.filter((l) => /\[(error|exception)\]/.test(l)).slice(0, 5);
} catch (error) {
    out.error = String(error.stack || error);
} finally {
    browser.close();
}
console.log(JSON.stringify(out));
process.exit(0);
