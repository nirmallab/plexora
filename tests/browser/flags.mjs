// The site's switches as a visitor meets them (services/shell.js `readFlags`;
// tools/browser_build/publish.py flags): for each case the flags file is
// written (or removed) and the site opened in a fresh profile. Prints, per
// case, what the loader said, whether the app's frame came up, how many
// requests reached the Python bundle, whether the preview mark was drawn, and
// -- where the app came up -- what Python's magic select says about its model.
//
//   node tests/browser/flags.mjs --site DIR --flags-dir DIR
//
// `--site` is a static build made with `--flags /fx/flags.json`; `--flags-dir`
// the folder served at /fx/ that this writes flags.json into.
import { rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { appFrame, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name) => args[args.indexOf(name) + 1];
const site = opt("--site");
const dir = opt("--flags-dir");

const CASES = [
    ["absent", null],
    ["paused", { viewer: "desktop-only", message: "Back at noon." }],
    ["stale", { min_site_version: "99.0.0" }],
    ["preview", { viewer: "preview", models: { mobilesam: { enabled: false } } }],
    ["broken", "{not json"],
];

const SAM = [
    "import json",
    "from plexora.vision import sam_backend",
    "json.dumps(sam_backend.executor().status())",
].join("\n");

const server = await startServer(["--site", site, "--tree", `fx=${dir}`]);
const origin = `http://127.0.0.1:${server.port}`;
const out = { cases: {} };
try {
    for (const [name, flags] of CASES) {
        const file = path.join(dir, "flags.json");
        if (flags === null) rmSync(file, { force: true });
        else writeFileSync(file, typeof flags === "string" ? flags : JSON.stringify(flags));
        const browser = await launch({});
        const row = {};
        try {
            const page = await browser.context.newPage();
            let python = 0;
            page.on("request", (request) => { if (new URL(request.url()).pathname.startsWith("/py/")) python += 1; });
            await page.goto(`${origin}/?pydebug`);
            const stopped = await page.waitForSelector(".plexora-boot-actions button", { timeout: 15000 })
                .then(() => true).catch(() => false);
            if (stopped) {
                row.said = await page.textContent("#plexora-boot-status");
                row.button = await page.textContent(".plexora-boot-actions button");
                await page.waitForTimeout(1500);
                row.app = Boolean(await page.$("#plexora-app"));
            } else {
                await appFrame(page);
                await page.evaluate(() => window.PlexoraPython.ready);
                row.app = true;
                row.mark = await page.evaluate(() => document.getElementById("plexora-preview-mark")?.textContent || null);
                const said = await page.evaluate((code) => window.PlexoraPython.run(code), SAM);
                row.sam = JSON.parse(String(said).replace(/^'|'$/g, ""));
            }
            row.python_requests = python;
        } catch (error) {
            row.error = String(error).slice(0, 500);
        } finally {
            await browser.close();
        }
        out.cases[name] = row;
    }
} finally {
    server.stop();
}
console.log(JSON.stringify(out));
