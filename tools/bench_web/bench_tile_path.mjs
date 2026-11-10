// The website's tile path on a real file, without the rest of the app: a checkout's own
// views/tileFetch.js (the scheduler, `local` and `localGroup` set as main.js sets them on the
// website) and services/browserNode.js (`use`d with the spec Python would give, the file picked
// through an <input>), its dist/tiffWorker_bundle.js, in headless Chrome. OSD is played by a
// loop that, every 16 ms, starts each channel's item's next tiles nearest the view's centre
// first -- as many as the item's `_currentMaxTilesPerFrame` (1 for a planar image, x10 after
// it is made and halved each update, raised by the scheduler where it groups), its items kept
// for the whole run. A view is done when all its tiles are delivered. A file on a share is
// read where it is (`setInputFiles` takes a path of any size), which is the point: the
// fixture benches link their files into the browser's own storage.
//
//   node tools/bench_web/bench_tile_path.mjs --file <tiff> [--checkout <repo>] [--channels 4,7,11,13]
//        [--regions "0.4,0.6;0.7,0.3"] [--label x] [--detail] [--out file.jsonl]
//
// Compare two checkouts by alternating them (A B B A ...), each on a region and channels no
// run read before: the OS keeps what was read, and a share's speed drifts within the hour.
// Each line: the open, then every view's ms (home, zoom to L3..L0 about the region, a pan right
// and one down at full resolution); --detail adds what the pool's workers were asked.
import http from "node:http";
import { readFileSync, existsSync, appendFileSync } from "node:fs";
import path from "node:path";
import { launch, REPO } from "./benchlib.mjs";

const args = process.argv.slice(2);
const opt = (n, f) => (args.includes(n) ? args[args.indexOf(n) + 1] : f);
const checkout = opt("--checkout", REPO);
const file = opt("--file");
const channels = opt("--channels", "4,7,11,13").split(",").map(Number);
const regions = opt("--regions", "0.4,0.6").split(";").map((r) => r.split(",").map(Number));
const label = opt("--label", path.basename(checkout));
const record = opt("--out", null);
const detail = args.includes("--detail");

const PAGE = `<!doctype html><meta charset=utf-8><input id=f type=file>
<script>
// Every op the pool sends a worker, timed from post to answer, and how many tiles it read.
window.opLog = [];
const NativeWorker = window.Worker;
window.Worker = class extends NativeWorker {
  constructor(...a) {
    super(...a);
    this._sent = new Map();
    this.addEventListener("message", (e) => {
      const s = e.data && this._sent.get(e.data.id);
      if (!s) return;
      this._sent.delete(e.data.id);
      const r = e.data.result || {};
      opLog.push({ op: s.op, ms: performance.now() - s.t, n: s.n });
    });
  }
  postMessage(m, t) {
    if (m && m.id !== undefined) this._sent.set(m.id, { op: m.op, t: performance.now(), n: m.count || 1 });
    return super.postMessage(m, t);
  }
};
</script>
<script src="/client/src/js/views/tileFetch.js"></script>
<script src="/client/src/js/services/browserNode.js"></script>
<script>
// The spec node_spec.describe gives on the website, from the file's own geometry.
window.specOf = async (file) => {
  const w = new NativeWorker("/client/dist/tiffWorker_bundle.js");
  const g = await new Promise((resolve, reject) => {
    w.onmessage = (e) => (e.data.ok ? resolve(e.data.result.geometry) : reject(new Error(e.data.error)));
    w.postMessage({ id: 1, op: "open", file, size: file.size });
  });
  w.terminate();
  const levels = g.levels.map((l) => [g.numChannels, l.height, l.width]);
  return { served: true, name: "bench", generation: 1, tile: [1024, 1024], path: "/picked/bench/" + file.name,
           format: "ome-tiff", levels, dtype: g.bitsPerSample === 8 ? "uint8" : "uint16",
           channels: levels.length ? Array.from({ length: g.numChannels }, (_, c) => "ch" + c) : [],
           labels: { served: false, reason: "none" } };
};
window.setUp = async () => {
  const file = document.getElementById("f").files[0];
  const spec = await specOf(file);
  PlexoraNode.use({ spec: async () => spec, files: async () => (p) => (p === spec.path ? { file } : null) });
  const scheduler = new PlexoraTileFetch.TileFetchScheduler({ makeContext: () => ({}) });
  scheduler.local = (request) => PlexoraNode.decoded(request.url, { raster: request.raster });
  if (PlexoraNode.group) scheduler.localGroup = PlexoraNode.group;
  window.scheduler = scheduler;
  const t0 = performance.now();
  const entry = await PlexoraNode.project(spec.name);
  return { spec, refused: entry.image.refused || null, openMs: performance.now() - t0 };
};
window.items = new Map();
const itemOf = (channel) => {
  if (!items.has(channel)) {
    const item = { getOpacity: () => 1, source: { plexoraOwner: "main" }, maxTilesPerFrame: 1 };
    item._currentMaxTilesPerFrame = item.maxTilesPerFrame * 10;
    items.set(channel, item);
  }
  return items.get(channel);
};
window.view = (spec, perChannel, maxLevel) => new Promise((resolve) => {
  const t0 = performance.now();
  const its = perChannel.map((tiles) => itemOf(tiles[0].channel));
  const lines = perChannel.map((tiles) => tiles.slice());
  let left = perChannel.reduce((n, t) => n + t.length, 0);
  const total = left;
  const done = [];
  let failed = 0;
  const stats0 = { ...scheduler.stats };
  const log0 = opLog.length;
  const finish = (ok) => {
    done.push(performance.now() - t0);
    if (!ok) failed += 1;
    left -= 1;
    if (left) return;
    clearInterval(timer);
    const ops = opLog.slice(log0).filter((o) => o.op === "tile" || o.op === "tiles");
    const mean = (xs) => (xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : 0);
    resolve({ ms: performance.now() - t0, first: Math.min(...done), tiles: total, failed,
              groups: (scheduler.stats.localGroups || 0) - (stats0.localGroups || 0),
              ops: ops.length, opMs: mean(ops.map((o) => o.ms)), runLen: mean(ops.map((o) => o.n)),
              index: opLog.slice(log0).filter((o) => o.op === "index").length });
  };
  const frame = () => {
    lines.forEach((line, c) => {
      const item = its[c];
      for (let k = 0; k < item._currentMaxTilesPerFrame && line.length; k += 1) {
        const t = line.shift();
        const url = "/generated/data/" + spec.name + "/" + spec.channels[t.channel] + "_" + t.channel + "/"
          + t.level + "/" + t.tx + "_" + t.ty + ".png?q=hd";
        scheduler.start({ src: url, source: item.source, userData: {}, tries: 1,
                          tile: { level: maxLevel - t.level, squaredDistance: t.d, tiledImage: item },
                          finish: () => finish(true), fail: () => finish(false) });
      }
      if (item._currentMaxTilesPerFrame > item.maxTilesPerFrame) {
        item._currentMaxTilesPerFrame = Math.max(Math.ceil(item._currentMaxTilesPerFrame / 2), item.maxTilesPerFrame);
      }
    });
  };
  frame();
  const timer = setInterval(frame, 16);
});
</script>`;

const root = path.join(checkout, "plexora", "client");
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://x");
  const p = url.pathname.startsWith("/client/") ? path.join(root, url.pathname.slice("/client/".length)) : null;
  if (p && existsSync(p)) { res.writeHead(200, { "Content-Type": "text/javascript", "Cache-Control": "no-store" }); res.end(readFileSync(p)); return; }
  if (url.pathname === "/") { res.writeHead(200, { "Content-Type": "text/html" }); res.end(PAGE); return; }
  res.writeHead(404); res.end();
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const browser = await launch();
const page = await browser.context.newPage();
page.on("pageerror", (e) => console.error("pageerror:", String(e).slice(0, 300)));
await page.goto(`http://127.0.0.1:${server.address().port}/`);
await page.setInputFiles("#f", [file]);
const { spec, refused, openMs } = await page.evaluate(() => window.setUp());
if (refused) { console.error("refused:", refused); process.exit(2); }

const T = spec.tile[0];
const maxLevel = spec.levels.length - 1;
// The view's tiles at `level` about (fx, fy): 3x2 file tiles (a 1440x900 window at that
// level's own scale), or the whole level when it is that small; nearest the centre first.
const tilesAt = (level, fx, fy, nx, ny) => {
  const [, H, W] = spec.levels[level];
  const mx = Math.ceil(W / T), my = Math.ceil(H / T);
  const x0 = Math.max(0, Math.min(mx - nx, Math.floor(fx * mx - nx / 2)));
  const y0 = Math.max(0, Math.min(my - ny, Math.floor(fy * my - ny / 2)));
  const cx = x0 + nx / 2 - 0.5, cy = y0 + ny / 2 - 0.5;
  const out = [];
  for (let ty = y0; ty < Math.min(my, y0 + ny); ty += 1) for (let tx = x0; tx < Math.min(mx, x0 + nx); tx += 1) {
    out.push({ level, tx, ty, d: (tx - cx) ** 2 + (ty - cy) ** 2 });
  }
  return out.sort((a, b) => a.d - b.d);
};
const home = spec.levels.findIndex(([, H, W]) => W <= 2 * 1060 && H <= 2 * 820);
for (const [rx, ry] of regions) {
  const steps = [["home", tilesAt(home, 0.5, 0.5, 99, 99)],
                 ...[3, 2, 1, 0].filter((l) => l < spec.levels.length).map((l) => [`L${l}`, tilesAt(l, rx, ry, 3, 2)]),
                 ["right", tilesAt(0, rx + 0.06, ry, 3, 2)], ["down", tilesAt(0, rx + 0.06, ry + 0.05, 3, 2)]];
  const rows = [];
  for (const [name, tiles] of steps) {
    const perChannel = channels.map((channel) => tiles.map((t) => ({ ...t, channel })));
    rows.push({ name, ...(await page.evaluate(([s, p, m]) => window.view(s, p, m), [spec, perChannel, maxLevel])) });
  }
  const total = rows.reduce((s, r) => s + r.ms, 0);
  console.log(`${label.padEnd(10)} ${path.basename(file).slice(0, 12)} ch ${channels.join(",").padEnd(11)} at ${rx},${ry}  open ${openMs.toFixed(0)}  `
              + rows.map((r) => `${r.name} ${r.ms.toFixed(0)}${r.failed ? `(${r.failed} failed)` : ""}`).join("  ") + `  | total ${total.toFixed(0)} ms`);
  if (detail) {
    for (const r of rows) {
      console.log(`    ${r.name.padEnd(6)} ${r.ms.toFixed(0).padStart(6)} ms  first tile ${r.first.toFixed(0).padStart(5)}  groups ${r.groups}  `
                  + `reads ${r.ops} x ${r.runLen.toFixed(1)} tiles, ${r.opMs.toFixed(0)} ms each  levels parsed ${r.index}`);
    }
  }
  if (record) appendFileSync(record, JSON.stringify({ label, file, channels, region: [rx, ry], openMs, rows, total, at: new Date().toISOString() }) + "\n");
}
await browser.close();
server.close();
