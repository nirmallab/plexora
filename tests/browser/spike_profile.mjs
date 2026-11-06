// Debug aid: cProfile inside the website's worker. A picked file is imported,
// then each named call is profiled in the worker; prints the top entries.
//
//   node tests/browser/spike_profile.mjs --file PATH [--py 0.0.36]
import path from "node:path";
import { launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const py = opt("--py", "0.0.36");
const file = opt("--file", null);
const server = await startServer(["--tree", `pick=${path.dirname(file)}`]);
const browser = await launch();
let out;
try {
    const page = await browser.context.newPage();
    await page.goto(`http://127.0.0.1:${server.port}/tests/browser/harness/index.html?py=${py}`);
    await page.evaluate(() => window.harness.ready);
    out = await page.evaluate(async (name) => {
        const blob = await (await fetch(`/pick/${name}`)).blob();
        const [mounted] = await PlexoraPython.mount("/picked/test", [new File([blob], name)]);
        const post = (route, body) => window.harness.ask(route, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        const sample = JSON.parse((await post("/import/sample", { paths: [mounted] })).text);
        const ds = sample.name;
        const profile = async (label, code) => window.harness.run(`
import cProfile, pstats, io, time
_pr = cProfile.Profile()
_t = time.perf_counter()
_pr.enable()
${code}
_pr.disable()
_s = io.StringIO()
pstats.Stats(_pr, stream=_s).sort_stats("cumulative").print_stats(28)
f"${label}: {time.perf_counter() - _t:.2f} s\\n" + _s.getvalue()[-6000:]`);
        const load = await profile("load", `
from plexora.server.models import data_model
data_model.ensure_loaded("${ds}")`);
        const info = await profile("volume info", `
from plexora import app
with app.test_client() as _c:
    _c.get("/volume/${ds}/info")`);
        const warm = await profile("warm", `
from plexora.server.models import data_model
data_model._warm_datasource_caches("${ds}")`);
        return { ds, load, info, warm };
    }, path.basename(file));
} finally {
    await browser.close();
    server.stop();
}
console.log(out.load);
console.log(out.info);
console.log(out.warm);
