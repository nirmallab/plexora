// survey.mjs - a picture of the whole image, for picking waypoints.
//
//   node survey.mjs --base http://127.0.0.1:PORT --dataset NAME --out survey.json
//                   [--params '{"z0": 128, "channel": 0}']
//
// Opens the project at plane z0 with one channel, goes home, waits for it to
// load fully (tourpage.js), then captures the viewer and records how screen
// pixels map onto image pixels, so waypoints.py can find tissue.
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Browser } from "../bench3d/drive.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));

const args = process.argv.slice(2);
const opt = (name, fallback = null) => {
    const at = args.indexOf(name);
    return at >= 0 ? args[at + 1] : fallback;
};
const base = opt("--base");
const dataset = opt("--dataset");
const out = opt("--out");
const params = JSON.parse(opt("--params", "{}"));

const browser = new Browser({ headed: false });
await browser.launch();
try {
    await browser.send("Page.addScriptToEvaluateOnNewDocument",
                       { source: readFileSync(path.join(HERE, "tourpage.js"), "utf8") });
    await browser.navigate(`${base}/${encodeURIComponent(dataset)}?z=${params.z0 ?? 128}`);
    await browser.waitFor(() => Boolean(window.__rb && window.__bench?.k?.booted?.()), 300000);
    const info = await browser.eval(async (channel) => {
        __bench.start();
        const k = __bench.k;
        k.setChannels([channel]);
        k.viewer().viewport.goHome(true);
        const r = await window.__rb.waitStep(performance.now(), 300000);
        await k.sleep(500);
        const v = k.viewer();
        const item = k.channelItems(v.world).find((i) => i.getOpacity() > 0);
        const el = v.element.getBoundingClientRect();
        const corner = (x, y) => {
            const p = item.viewerElementToImageCoordinates(new OpenSeadragon.Point(x, y));
            return [p.x, p.y];
        };
        return { full: r.full, selector: v.element.id ? `#${v.element.id}` : "#openseadragon_wrapper",
                 element: { x: el.left, y: el.top, width: el.width, height: el.height },
                 topLeft: corner(0, 0), bottomRight: corner(el.width, el.height),
                 size: window.__rb.imageSize() };
    }, params.channel ?? 0);
    const shot = await browser.shot(info.selector);
    writeFileSync(out, JSON.stringify({ dataset, params, info, png: shot }));
} finally {
    await browser.close();
}
process.exit(0);
