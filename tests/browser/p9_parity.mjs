// The gating session in the product's Python worker, against CPython: P9's
// oracle session (tools/poc_browser_node/page/py/p9_run.py) on its fixture,
// run inside the website's worker exactly as built, nothing stubbed but the
// licence (the suite's throwaway issuer, as P9 and truth_p9.py use).
// Writes the dump to --out; compare it with bench/compare_p9.py against
// truth_p9.py's run of the same fixture on the same checkout.
//
//   node tests/browser/p9_parity.mjs --fixture P9S --out dump.json [--py 0.0.36]
import { writeFileSync } from "node:fs";
import path from "node:path";
import { REPO, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const py = opt("--py", "0.0.36");
const fixture = opt("--fixture", "P9S");
const out = opt("--out", "p9-parity.json");
const bench = process.env.PLEXORA_BENCH_HOME || "C:/plexora-bench";
const fixtureDir = path.join(bench, "browser_node", "cache", `p9-${fixture}`);
const pyDir = path.join(REPO, "tools", "poc_browser_node", "page", "py");

const server = await startServer(["--tree", `fixture=${fixtureDir}`, "--tree", `p9py=${pyDir}`]);
const browser = await launch();
let result;
try {
    const page = await browser.context.newPage();
    const logs = [];
    page.on("console", (message) => logs.push(`${message.type()}: ${message.text()}`.slice(0, 300)));
    await page.goto(`http://127.0.0.1:${server.port}/tests/browser/harness/index.html?py=${py}`);
    result = await page.evaluate(async () => {
        const h = window.harness;
        const info = await h.ready;
        const text = async (url) => (await fetch(url)).text();
        const names = ["cells.csv", "image.ome.tif", "mask.tif"];
        const files = await Promise.all(names.map(async (name) => new File(
            [await (await fetch(`/fixture/root/_gsynth_files/${name}`)).blob()], name)));
        await PlexoraPython.mount("/picked/gsynth", files);
        const sources = { "p9_run.py": await text("/p9py/p9_run.py"),
                          "p9_oracle.py": await text("/p9py/p9_oracle.py") };
        const config = await text("/fixture/config.browser.json");
        const fixtureInfo = await text("/fixture/info.json");
        const started = performance.now();
        const dump = await h.run(`
import json, os, shutil, sys
SOURCES = json.loads(${JSON.stringify(JSON.stringify(sources))})
os.makedirs("/tmp/p9", exist_ok=True)
for name, source in SOURCES.items():
    with open("/tmp/p9/" + name, "w", encoding="utf-8") as handle:
        handle.write(source)
sys.path.insert(0, "/tmp/p9")
os.environ["BIOCOGNIA_OFFLINE"] = "1"
if not hasattr(os, "link"):
    def _link(src, dst):
        raise OSError(38, "link() is not available here")
    os.link = _link
from biocognia.testing import Issuer
from plexora.licensing import LICENSING, PRODUCT
class _Patch:
    setattr = staticmethod(setattr)
_issuer = Issuer(_Patch(), PRODUCT)
_issuer.install(LICENSING, _issuer.issue(PRODUCT, ent=["ai"]))
os.makedirs("/data/_gsynth_files", exist_ok=True)
for name in ("cells.csv", "image.ome.tif", "mask.tif"):
    shutil.copy("/picked/gsynth/" + name, "/data/_gsynth_files/" + name)
with open("/data/config.json", "w", encoding="utf-8") as handle:
    handle.write(${JSON.stringify(config)})
import p9_run
_dump = p9_run.session("/data", json.loads(${JSON.stringify(fixtureInfo)}))
json.dumps(_dump)`);
        return { boot: info, wall: performance.now() - started, dump: JSON.parse(dump),
                 heap: await h.heap(), stages: h.stages };
    });
    result.logs = logs.slice(-40);
} finally {
    await browser.close();
    server.stop();
}
writeFileSync(out, JSON.stringify(result));
console.log(JSON.stringify({ out, wall: Math.round(result.wall), packets: result.dump.summary.packets,
                             units: result.dump.units, stages: result.stages,
                             heapMiB: Math.round(result.heap / 2 ** 20) }));
