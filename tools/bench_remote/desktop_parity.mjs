// desktop_parity.mjs - the desktop app's Browser rendering against its Python
// route, tile by tile, in the app's own window (plan Phase 6 step 1).
//
//   node desktop_parity.mjs --dataset NAME --out parity.json
//        [--cdp http://127.0.0.1:9333] [--channels 0,1,2,10,39] [--per-level 3]
//
// The app runs with WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=
// --remote-debugging-port=9333 and Settings > Advanced > Browser rendering on,
// with the project open (or this opens it). For each sampled tile -- channels
// x every level x `per-level` positions (the first, the middle and the last,
// an edge tile), and the mask at every level the page serves -- the page's
// answer (`PlexoraNode.decoded`: the pixels its scheduler hands the GPU, or the
// mask's RGBA PNG) is compared with the server's (`fetch` of the same URL,
// decoded as the viewer decodes it) sample for sample, and both are hashed
// (FNV-1a over the decoded bytes). Any difference fails the run.
import { writeFileSync } from "node:fs";
import { Browser } from "../bench3d/drive.mjs";

function parseArgs(argv) {
    const out = { cdp: "http://127.0.0.1:9333", channels: [0, 1, 2, 10, 39], perLevel: 3 };
    for (let i = 0; i < argv.length; i += 1) {
        const arg = argv[i];
        const next = () => argv[++i];
        if (arg === "--dataset") out.dataset = next();
        else if (arg === "--out") out.out = next();
        else if (arg === "--cdp") out.cdp = next();
        else if (arg === "--channels") out.channels = next().split(",").map(Number);
        else if (arg === "--per-level") out.perLevel = Number(next());
    }
    return out;
}

/** Page side: compare the sampled tiles. */
async function compare(dataset, channels, perLevel) {
    const fnv = (bytes) => {
        let h = 0x811c9dc5;
        for (let i = 0; i < bytes.length; i += 1) h = Math.imul(h ^ bytes[i], 0x01000193) >>> 0;
        return h.toString(16).padStart(8, "0");
    };
    const node = window.PlexoraNode;
    if (!node || typeof node.decoded !== "function") return { error: "the browser node is not on this page" };
    const entry = await node.project(dataset);
    if (!entry.spec) return { error: "no spec" };
    const spec = entry.spec;
    const config = window.__plexora.seaDragonViewer.config;
    const labelIndex = (config.layers || []).find((layer) => layer.kind === "labels")?.channelIndex ?? -1;
    const [tw, th] = spec.tile;
    const picks = (count) => {
        const n = Math.max(1, count);
        const out = [0, Math.floor((n - 1) / 2), n - 1];
        return [...new Set(out)].slice(0, perLevel);
    };
    const cases = [];
    const imageData = config.imageData || [];
    for (const channel of channels) {
        // The entry whose address names image channel `channel` (`_<n>`).
        const found = imageData.find((d, i) => i !== labelIndex
            && new RegExp(`_${channel}/?$`).test(String(d.src || "")));
        if (!found) continue;
        spec.levels.forEach(([, h, w], level) => {
            const xs = picks(Math.ceil(w / tw));
            const ys = picks(Math.ceil(h / th));
            for (let k = 0; k < xs.length; k += 1) {
                cases.push({ kind: "channel", channel, level,
                             url: `${found.src}${level}/${xs[k]}_${ys[k]}.png?q=hd` });
            }
        });
    }
    if (labelIndex >= 0 && spec.labels && spec.labels.served) {
        const src = imageData[labelIndex].src;
        for (const [level, h, w] of spec.labels.levels) {
            const xs = picks(Math.ceil(w / tw));
            const ys = picks(Math.ceil(h / th));
            for (let k = 0; k < xs.length; k += 1) {
                cases.push({ kind: "labels", level, url: `${src}${level}/${xs[k]}_${ys[k]}.png?v=parity` });
            }
        }
    }
    const results = [];
    for (const c of cases) {
        const raster = c.kind === "labels";
        const t0 = performance.now();
        const local = await node.decoded(c.url, { raster });
        const t1 = performance.now();
        const response = await fetch(c.url, { cache: "no-store" });
        const body = await response.arrayBuffer();
        const t2 = performance.now();
        const record = { ...c, status: response.status, localMs: +(t1 - t0).toFixed(1),
                         serverMs: +(t2 - t1).toFixed(1) };
        if (!local) { results.push({ ...record, verdict: "declined" }); continue; }
        if (!response.ok) { results.push({ ...record, verdict: "server-failed" }); continue; }
        const server = window.UPNG.decode(body);
        let a;
        let b;
        if (raster) {
            const mine = window.UPNG.decode(local.raster);
            a = new Uint8Array(mine.data.buffer, mine.data.byteOffset, 4 * mine.width * mine.height);
            b = new Uint8Array(server.data.buffer, server.data.byteOffset, 4 * server.width * server.height);
            record.size = [mine.width, mine.height, server.width, server.height];
        } else {
            a = local.array;
            b = new Uint8Array(server.data.buffer, server.data.byteOffset, 2 * server.width * server.height);
            record.size = [local.width, local.height, server.width, server.height];
            record.serverKind = [server.ctype, server.depth];
        }
        let differing = 0;
        if (a.length !== b.length) differing = -1;
        else for (let i = 0; i < a.length; i += 1) if (a[i] !== b[i]) differing += 1;
        results.push({ ...record, bytes: a.length, localHash: fnv(a), serverHash: fnv(b), differing,
                       verdict: differing === 0 ? "identical" : "DIFFERENT" });
    }
    return { spec: { levels: spec.levels, dtype: spec.dtype, tile: spec.tile,
                     labels: spec.labels && { levels: spec.labels.levels, dtype: spec.labels.dtype } },
             results };
}

async function main() {
    const opts = parseArgs(process.argv.slice(2));
    const browser = new Browser();
    await browser.attach(opts.cdp, { prefix: "http://127.0.0.1" });
    const origin = await browser.eval("location.origin");
    const here = await browser.eval("location.pathname");
    if (here !== `/${opts.dataset}`) {
        await browser.navigate(`${origin}/${encodeURIComponent(opts.dataset)}`);
    }
    const ok = await browser.waitFor(() => Boolean(window.__plexora?.seaDragonViewer?.config
        && window.PlexoraNode), 180000);
    if (!ok) throw new Error("the viewer did not open with the browser node on the page");
    const out = await browser.eval(compare, opts.dataset, opts.channels, opts.perLevel);
    const results = out.results || [];
    const summary = {
        tiles: results.length,
        identical: results.filter((r) => r.verdict === "identical").length,
        different: results.filter((r) => r.verdict === "DIFFERENT").length,
        declined: results.filter((r) => r.verdict === "declined").length,
        serverFailed: results.filter((r) => r.verdict === "server-failed").length,
        channelTiles: results.filter((r) => r.kind === "channel").length,
        labelTiles: results.filter((r) => r.kind === "labels").length,
    };
    const record = { dataset: opts.dataset, when: new Date().toISOString(), summary, ...out };
    if (opts.out) writeFileSync(opts.out, JSON.stringify(record, null, 1));
    console.log(JSON.stringify(summary));
    await browser.close();
    process.exit(out.error || summary.different || summary.serverFailed ? 2 : 0);
}

main().catch((error) => {
    console.error(error);
    process.exit(1);
});
