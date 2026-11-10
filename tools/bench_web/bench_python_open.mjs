// Python's open of picked TIFFs in the website's own worker -- tifffile and the
// channel series, as the image provider opens a file -- with the read ahead
// (plexora/browser/tiff_ahead.py) and without it, alternating, one file a trial:
// the OS keeps what was read, so a trial that read what another had is no trial.
// Give files of one kind not read lately (a share's, picked where they lie).
//
//   node tools/bench_web/bench_python_open.mjs --site <static_build out> --py <build.py out>/py
//        [--ahead-first] FILE [FILE ...]
//
// Each line: the file, whether it was read ahead, Python's open in ms, and the
// walk's rounds and pieces. Needs a build of the checkout under test (the Python
// bundle and the static site), served by tools/browser_build/serve.py.
import { launch, seedBrowserStore, startServer } from "../../tests/browser/lib.mjs";

const args = process.argv.slice(2);
const opt = (name) => (args.includes(name) ? args[args.indexOf(name) + 1] : null);
const site = opt("--site");
const pyRoot = opt("--py");
const files = args.filter((arg, i) => !arg.startsWith("--") && !["--site", "--py"].includes(args[i - 1]));
if (!site || !pyRoot || !files.length) throw new Error("--site DIR --py DIR FILE [FILE ...]");

const server = await startServer(["--site", site, "--py", pyRoot]);
const origin = `http://127.0.0.1:${server.port}`;
const browser = await launch({});
try {
    await seedBrowserStore(browser.context, origin);
    const page = await browser.context.newPage();
    page.on("pageerror", (error) => console.error("pageerror:", String(error).slice(0, 300)));
    await page.goto(`${origin}/?pydebug`);
    await page.waitForFunction(() => window.PlexoraPython !== undefined, null, { timeout: 180000 });
    await page.evaluate(() => {
        const input = document.createElement("input");
        input.type = "file";
        input.multiple = true;
        input.id = "bench_files";
        document.body.appendChild(input);
    });
    await page.setInputFiles("#bench_files", files);
    for (let i = 0; i < files.length; i += 1) {
        const ahead = (i % 2 === 1) !== args.includes("--ahead-first");
        const [path] = await page.evaluate((k) => window.PlexoraPython.mount(`/picked/bench_open${k}`,
            [document.getElementById("bench_files").files[k]]), i);
        const code = `
import json, time, tifffile
from plexora.browser import tiff_ahead
from plexora.server.utils import tiff_series
real = tiff_ahead.walk
if not ${ahead ? "True" : "False"}:
    tiff_ahead.walk = lambda fh, size, fetch, piece: {"rounds": 0, "pieces": 0, "ifds": 0}
before = len(tiff_ahead.walks)
began = time.perf_counter()
try:
    tif = tifffile.TiffFile(${JSON.stringify(path)}, is_ome=False)
    series = tiff_series.channel_series(tif)
    ms = (time.perf_counter() - began) * 1000
    levels = len(series.levels)
    tif.close()
finally:
    tiff_ahead.walk = real
json.dumps({"ms": round(ms), "walk": list(tiff_ahead.walks)[before:], "levels": levels})`;
        const out = JSON.parse(await page.evaluate((c) => window.PlexoraPython.run(c), code));
        const walk = out.walk[0] || {};
        console.log(JSON.stringify({ file: files[i].split(/[\\/]/).pop(), ahead, ms: out.ms, levels: out.levels,
                                     rounds: walk.rounds ?? 0, pieces: walk.pieces ?? 0 }));
    }
} finally {
    await browser.close();
    server.stop();
}
