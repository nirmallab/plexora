// Spike: does a long background task let requests in at its checkpoints?
// A task busy for 3 s in 50 ms pieces, calling _work.checkpoint() between
// them; /health asked every 100 ms meanwhile. Prints each answer's latency.
//
//   node tests/browser/spike_checkpoint.mjs [--py 0.0.36]
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
        await h.run(`
import time
from plexora import _work
_cp = {"yields": 0}
def _busy():
    end = time.monotonic() + 3.0
    while time.monotonic() < end:
        piece = time.monotonic() + 0.05
        while time.monotonic() < piece:
            pass
        before = _work._last_yield
        _work.checkpoint()
        if _work._last_yield != before:
            _cp["yields"] += 1
_work.spawn(_busy, name="busy")
"queued"`);
        const latencies = [];
        const started = performance.now();
        while (performance.now() - started < 3500) {
            const t = performance.now();
            await h.ask("/health");
            latencies.push(Math.round(performance.now() - t));
            await new Promise((r) => setTimeout(r, 100));
        }
        const yields = await h.run("_cp['yields']");
        const trace = await PlexoraPython.trace();
        return { latencies, yields, trace };
    });
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out));
