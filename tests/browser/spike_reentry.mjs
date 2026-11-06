// Spike S-1: can the worker answer a request while Python is suspended inside
// a job (a poll sleeping through JSPI, waiting on the user)? Prints JSON.
//
//   node tests/browser/spike_reentry.mjs [--py 0.0.36]
import { launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const py = args.includes("--py") ? args[args.indexOf("--py") + 1] : "0.0.36";
const server = await startServer();
const browser = await launch();
const out = { py };
try {
    const page = await browser.context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    await page.goto(`http://127.0.0.1:${server.port}/tests/browser/harness/index.html?py=${py}`);
    out.info = await page.evaluate(() => window.harness.ready);
    out.result = await page.evaluate(async () => {
        const h = window.harness;
        await h.run(`
import time
from plexora import _work
_spike = {"start": time.monotonic()}
def _sleeper():
    _work.wait(3.0)
    _spike["woke"] = time.monotonic() - _spike["start"]
_work.spawn(_sleeper, name="spike-sleeper")
"done"`);
        // Let the worker start the job and fall asleep in it.
        await new Promise((r) => setTimeout(r, 300));
        const during = await h.ask("/health");
        const projects = await h.ask("/projects");
        await new Promise((r) => setTimeout(r, 3500));
        const woke = await h.run("_spike.get('woke')");
        const after = await h.ask("/health");
        return { duringStatus: during.status, duringMs: during.ms, projectsStatus: projects.status,
                 projectsMs: projects.ms, woke, afterStatus: after.status, afterMs: after.ms };
    });
    out.errors = errors;
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out));
