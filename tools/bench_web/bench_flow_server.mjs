// The same flow as bench_flow_web.mjs on a local Plexora server (the Python
// webserver, viewed in Chrome): import the fixture by path, open, first
// channel lit, four channels on (eye), reload; brightness sampled from
// screenshots.
//
//   node tools/bench_web/bench_flow_server.mjs --repo CHECKOUT --fixture DIR
//        [--files image,mask,table] [--pause MS] [--snap] [--out FILE] [--shots DIR]
import path from "node:path";
import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { INIT, launch, levelWatch, levelsSeen, post } from "./benchlib.mjs";

const args = process.argv.slice(2);
const opt = (n, f) => (args.includes(n) ? args[args.indexOf(n) + 1] : f);
const repo = opt("--repo");
const fixture = opt("--fixture");
const names = opt("--files", "image.ome.tif,mask.labels.pyramid.ome.tiff,cells.csv").split(",");
const outFile = opt("--out");
const shots = opt("--shots", ".");
const T0 = Date.now();
const say = (m) => process.stderr.write(`${((Date.now() - T0) / 1000).toFixed(1)}s ${m}\n`);
const out = { repo, failed: [], samples: [], steps: {} };
const data = mkdtempSync(path.join(tmpdir(), "pxserver-"));
const PYTHONPATH = [".", "../spatialagents/src", "../spatialbridge/src", "../biocognia-markers/src",
                    "../biocognia-intelligence/src"].join(":");
const server = await new Promise((resolve, reject) => {
    const child = spawn(process.env.PLEXORA_PYTHON, ["-u", "-m", "plexora", "--no-browser", "--port", "0", "--data-dir", data],
        { cwd: repo, env: { ...process.env, PYTHONPATH, PLEXORA_DATA_PATH: data, PLEXORA_TELEMETRY: "off", PYTHONUNBUFFERED: "1" },
          stdio: ["ignore", "pipe", "pipe"] });
    let buf = "";
    const on = (c) => {
        buf += c;
        const m = buf.match(/Serving Plexora at (\S+)/);
        if (m) resolve({ url: m[1], stop: () => child.kill("SIGTERM") });
    };
    child.stdout.on("data", on);
    child.stderr.on("data", on);
    child.on("exit", (c) => reject(new Error(`exit ${c} ${buf.slice(-500)}`)));
});
say(`server ${server.url}`);
const browser = await launch();
const page = await browser.context.newPage();
page.on("response", (r) => { if (r.status() >= 400) out.failed.push({ t: Date.now() - T0, url: r.url().slice(0, 140), status: r.status() }); });
await page.addInitScript(INIT);
await page.setViewportSize({ width: 1440, height: 900 });

async function brightness(label) {
    try {
        const png = await page.screenshot({ clip: { x: 400, y: 60, width: 1020, height: 820 } });
        if (label) writeFileSync(path.join(shots, `${label}.png`), png);
        return await page.evaluate(async (b64) => {
            const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
            const bmp = await createImageBitmap(new Blob([bytes], { type: "image/png" }));
            const c = new OffscreenCanvas(bmp.width, bmp.height);
            const g = c.getContext("2d");
            g.drawImage(bmp, 0, 0);
            const d = g.getImageData(0, 0, bmp.width, bmp.height).data;
            let s = 0, lit = 0;
            for (let i = 0; i < d.length; i += 16) { const v = (d[i] + d[i + 1] + d[i + 2]) / 3; s += v; if (v > 24) lit++; }
            return { mean: +(s / (d.length / 16)).toFixed(2), lit: +(lit / (d.length / 16)).toFixed(4) };
        }, png.toString("base64"));
    } catch (e) { return { error: String(e).slice(0, 100) }; }
}
const sample = async (phase) => { const row = { t: Date.now() - T0, phase, b: await brightness() }; out.samples.push(row); return row; };
const stats = () => page.evaluate(() => performance.getEntriesByType("resource")
    .filter((e) => /get_image_channel_stats|get_channel_gmm|display_contrast/.test(e.name))
    .map((e) => ({ p: new URL(e.name).pathname, q: new URL(e.name).searchParams.get("channel"),
                   start: Math.round(e.startTime), ms: Math.round(e.duration) })));

try {
    await page.goto(server.url);
    await page.waitForLoadState("load");
    const paths = names.map((n) => realpathSync(path.join(fixture, n)));
    let t = Date.now();
    await post(page, "import/inspect", { paths });
    const imported = await post(page, "import/sample", { paths, answers: {} });
    const name = imported.body?.name;
    out.steps.import = { ms: Date.now() - t, name };
    say(`imported ${name} in ${Date.now() - t} ms`);
    const base = server.url.replace(/\?token=.*/, "").replace(/\/$/, "");
    t = Date.now();
    await page.goto(`${base}/${name}`);
    await page.waitForFunction(() => document.querySelector(".channel-slot-eye"), null, { timeout: 120000, polling: 50 });
    for (let i = 0; i < 60; i++) {
        const r = await sample("open"); if (args.includes("--snap") && i % 2 === 0) await brightness(`s_open_${String(i).padStart(2, "0")}`);
        if (r.b?.lit > 0.05) { out.steps.openLitMs = Date.now() - t; break; }
        await page.waitForTimeout(500);
    }
    say(`open: lit after ${out.steps.openLitMs ?? "never"} ms`);
    await brightness("1_open");
  if (opt("--pause", null)) await page.waitForTimeout(Number(opt("--pause")));
    const since = await page.evaluate(() => performance.now());
    const eyes = await page.$$(".channel-slot-eye");
    await levelWatch(page).catch(() => null);
    let turned = 0;
    for (const e of eyes) {
        if ((await e.getAttribute("aria-pressed")) === "false" && turned < 4) { await e.click(); turned++; await page.waitForTimeout(150); }
    }
    say(`turned on ${turned}`);
    for (let end = Date.now() + 40000; Date.now() < end;) { await sample("channels"); await page.waitForTimeout(1000); }
    await brightness("2_channels");
    out.steps.levels = await levelsSeen(page).catch((e) => String(e));
    say(`levels: fit ${out.steps.levels?.fitMs} ms, calibrated ${out.steps.levels?.calibratedMs} ms after the first click`);
    out.steps.channelRequests = (await stats()).filter((r) => r.start >= since - 500)
        .map((r) => ({ ...r, start: r.start - Math.round(since) }));
    say(`channel stats: ${JSON.stringify(out.steps.channelRequests.filter((r) => r.p.includes("stats")).map((r) => [r.q, r.start + r.ms]))}`);
    t = Date.now();
    await page.reload();
    await page.waitForFunction(() => document.querySelector(".channel-slot-eye"), null, { timeout: 120000, polling: 50 });
    for (let i = 0; i < 60; i++) {
        const r = await sample("reload");
        if (r.b?.lit > 0.05) { out.steps.reloadLitMs = Date.now() - t; break; }
        await page.waitForTimeout(250);
    }
    await page.waitForTimeout(8000);
    await sample("reload-settled");
    await brightness("3_reload");
    say(`reload lit ${out.steps.reloadLitMs}`);
} catch (e) {
    out.fatal = String(e).slice(0, 400);
    say("FATAL " + out.fatal);
} finally {
    writeFileSync(outFile, JSON.stringify(out, null, 1));
    await browser.context.close();
    server.stop();
}
