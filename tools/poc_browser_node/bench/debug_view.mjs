// node bench/debug_view.mjs <arm> <fixture> <shot.png> -- open one arm, wait, screenshot, dump tile state.
import path from "node:path";
import { readFileSync } from "node:fs";
import { launchChrome, startNode, startStatic, stopAll, POC_ROOT } from "./procs.mjs";
const [arm = "browser", fx = "S", shot = "shot.png", view = ""] = process.argv.slice(2);
const fixture = JSON.parse(readFileSync(path.join(POC_ROOT, "bench", "fixtures.json"), "utf8"))[fx];
try {
    const stat = await startStatic();
    const b = await launchChrome();
    await b.page.goto(`${stat.origin}/poc/index.html`);
    await b.page.waitForFunction(() => document.getElementById("status").textContent === "ready");
    await b.page.setInputFiles("#picker", [fixture.image]);
    if (view) await b.page.evaluate((v) => { window.__view = v; }, JSON.parse(view));
    const node = await startNode({ serve: [`image:img=${fixture.image}`], allowOrigins: [stat.origin] });
    const out = await b.page.evaluate(async ({ arm, node, file }) => {
        const L = window.POC.lib;
        const a = await L.openArm({ arm, node, image: { file }, channels: [
            { index: 0, color: "#0000ff", range: [458 / 65535, 2925 / 65535] }, { index: 1, color: "#00ff00", range: [0, 0.05] }] });
        window.__arm = a;
        let full = await L.waitFull(a.viewer.viewer, { timeoutMs: 20000 });
        if (window.__view) { L.goTo(a.viewer.viewer, window.__view); full = await L.waitFull(a.viewer.viewer, { timeoutMs: 60000 }); }
        const items = a.viewer.items.map((it) => {
            let tiles = 0, withArray = 0;
            for (const lvl of Object.values(it.tilesMatrix)) for (const col of Object.values(lvl)) for (const t of Object.values(col)) {
                tiles += 1; if (t._array) withArray += 1;
            }
            return { tiles, withArray, fully: it.getFullyLoaded() };
        });
        const c = a.viewer.viewer.drawer.canvas;
        const px = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
        let nb = 0; for (let i = 0; i < px.length; i += 4) if (px[i] || px[i + 1] || px[i + 2]) nb++;
        return { full, items, canvas: [c.width, c.height], nonBlack: nb, geometry: a.geometry, glArgs: Object.keys(a.viewer.renderer.gl_arguments || {}) };
    }, { arm, node: { endpoint: node.endpoint, token: node.token, imageId: "img" }, file: path.basename(fixture.image) });
    console.log(JSON.stringify(out, null, 1).slice(0, 3000));
    await b.page.screenshot({ path: shot });
    console.log(b.logs.slice(-25).join("\n"));
    await b.close();
} finally { stopAll(); }
