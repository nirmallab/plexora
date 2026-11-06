// Debug aid: a picked file mounted in the worker, then /import/inspect and
// /import/sample asked through the page's fetch path; prints status and body.
//
//   node tests/browser/spike_import.mjs --file PATH [--py 0.0.36]
import { readFileSync } from "node:fs";
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
        const inspect = await post("/import/inspect", { paths: [mounted] });
        const sample = await post("/import/sample", { paths: [mounted] });
        return { mounted, inspect: { status: inspect.status, ms: Math.round(inspect.ms), body: inspect.text.slice(-3000) },
                 sample: { status: sample.status, ms: Math.round(sample.ms), body: sample.text.slice(-3000) } };
    }, path.basename(file));
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out, null, 1));
