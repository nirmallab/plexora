// Spike: Plexora's Python serving tiles in the website's worker. A synthetic
// 2-channel 2048x2048 uint16 pyramid is written and registered in the worker,
// then tiles are asked for through the page's fetch path, HD (16-bit PNG) and
// the default (8-bit WebP). Prints JSON: per-tile milliseconds.
//
//   node tests/browser/spike_tiles.mjs [--py 0.0.36]
import { launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const py = args.includes("--py") ? args[args.indexOf("--py") + 1] : "0.0.36";
const server = await startServer();
const browser = await launch();
let out;
try {
    const page = await browser.context.newPage();
    await page.goto(`http://127.0.0.1:${server.port}/tests/browser/harness/index.html?py=${py}`);
    await page.evaluate(() => window.harness.ready);
    out = await page.evaluate(async () => {
        const h = window.harness;
        const made = await h.run(`
import json, time
import numpy as np
import tifffile
from plexora.server.utils import image_pyramid
t = time.perf_counter()
rng = np.random.default_rng(0)
yy, xx = np.mgrid[0:2048, 0:2048]
image = np.stack([((np.sin(xx / 40) + np.cos(yy / 55)) * 9000 + 20000).astype(np.uint16),
                  rng.integers(0, 4000, (2048, 2048), dtype=np.uint16)])
tifffile.imwrite("/data/spike.tif", image, metadata={"axes": "CYX"})
written = time.perf_counter() - t
import plexora
t = time.perf_counter()
plexora.register_image_datasource("spike", "/data/spike.tif")
registered = time.perf_counter() - t
from plexora.server.models.project import Project
p = Project.load("spike")
json.dumps({"written_s": round(written, 2), "registered_s": round(registered, 2),
            "levels": p.image.max_level, "tile": p.image.tile_width, "pyramid": str(p.image.pyramid),
            "channels": list(p.image.channel_names)})`);
        const info = JSON.parse(made);
        const times = { hd: [], webp: [] };
        const statuses = {};
        const level = info.levels;
        for (const [quality, query] of [["hd", "?q=hd"], ["webp", ""]]) {
            for (let i = 0; i < 6; i += 1) {
                const tile = `${i % 2}_${Math.floor(i / 2) % 2}`;
                const channel = encodeURIComponent(info.channels[i % 2]);
                const answer = await h.ask(`/generated/data/spike/${channel}/${level}/${tile}.png${query}`);
                times[quality].push(Math.round(answer.ms));
                if (answer.status >= 400 && !times.error) times.error = answer.text.slice(-1500);
                statuses[`${quality}:${answer.status}:${answer.type}`] = (statuses[`${quality}:${answer.status}:${answer.type}`] || 0) + 1;
            }
        }
        const direct = await h.run(`
import traceback
from plexora.server.models import data_model
try:
    data_model.encode_tile("spike", "Channel 1", "${level}", "0_0", "hd")
    _r = "ok"
except Exception:
    _r = traceback.format_exc()[-2500:]
_r`);
        return { info, times, statuses, direct };
    });
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out));
