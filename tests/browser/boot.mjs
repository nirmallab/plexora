// The website's Python, booted in Chrome: how long to ready, and the answers
// to a few routes through the fetch path the page will use. Prints one JSON
// line; tests/test_browser_python.py asserts on it.
//
//   node tests/browser/boot.mjs [--py 0.0.36] [--headed]
import { launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const py = args.includes("--py") ? args[args.indexOf("--py") + 1] : "0.0.36";
const headed = args.includes("--headed");
const PATHS = (process.env.PLEXORA_BOOT_PATHS || "/health,/projects,/config,/get_load_status,/license/status,/supported_data_formats,/settings")
    .split(",");

const server = await startServer();
const browser = await launch({ headed });
const out = { py, paths: {} };
try {
    const page = await browser.context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    const opened = Date.now();
    await page.goto(`http://127.0.0.1:${server.port}/tests/browser/harness/index.html?py=${py}`);
    out.isolated = await page.evaluate(() => window.harness.isolated);
    out.info = await page.evaluate(() => window.harness.ready);
    out.readyMs = Date.now() - opened;
    out.marks = await page.evaluate(() => window.harness.marks);
    for (const path of PATHS) {
        const answer = await page.evaluate((p) => window.harness.ask(p), path);
        out.paths[path] = { status: answer.status, ms: Math.round(answer.ms), type: answer.type,
                            head: answer.text.slice(0, 160) };
    }
    out.heap = await page.evaluate(() => window.harness.heap());
    out.stages = await page.evaluate(() => window.harness.stages);
    out.errors = errors.slice(0, 20);
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out));
