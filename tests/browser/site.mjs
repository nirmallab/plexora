// The website as a visitor gets it: the static build at the root, the loader,
// Plexora's Python in the page, and the page the address asks for. Prints
// JSON (what loaded, how long it took, every console error and failed
// request); --shots DIR saves a screenshot of each address.
//
//   node tests/browser/site.mjs [--paths /,/settings] [--shots DIR] [--headed]
import path from "node:path";
import { REPO, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const paths = opt("--paths", "/,/settings,/open_project").split(",");
const shots = opt("--shots", null);
const site = opt("--site", path.join(REPO, "plexora", "client", "dist", "static"));

const server = await startServer(["--site", site]);
const browser = await launch({ headed: args.includes("--headed") });
const out = { pages: {} };
try {
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    let errors = [];
    let failed = [];
    page.on("pageerror", (error) => errors.push(`pageerror: ${error}`));
    page.on("console", (message) => {
        if (message.type() === "error") errors.push(message.text().slice(0, 300));
    });
    page.on("response", (response) => {
        if (response.status() >= 400) failed.push(`${response.status()} ${response.url()}`);
    });
    for (const address of paths) {
        errors = [];
        failed = [];
        const started = Date.now();
        await page.goto(`http://127.0.0.1:${server.port}${address}`);
        await page.waitForFunction(() => !document.getElementById("plexora-boot")
                                         || document.querySelector(".plexora-boot-failed"),
                                   null, { timeout: 120000 });
        const written = Date.now() - started;
        await page.waitForTimeout(2500);
        const state = await page.evaluate(() => ({
            title: document.title,
            failedBoot: document.querySelector(".plexora-boot-failed")?.textContent || null,
            kind: window.PlexoraBackend && window.PlexoraBackend.kind,
            controlled: Boolean(navigator.serviceWorker.controller),
            bodyClasses: document.body ? document.body.className : null,
            text: document.body ? document.body.innerText.slice(0, 400) : null,
        }));
        if (shots) {
            const name = address === "/" ? "home" : address.replace(/[^a-z0-9]+/gi, "_");
            await page.screenshot({ path: path.join(shots, `site-${name}.png`) });
        }
        out.pages[address] = { writtenMs: written, ...state, errors: errors.slice(0, 15),
                               failed: failed.slice(0, 15) };
    }
} finally {
    await browser.close();
    server.stop();
}
console.log(JSON.stringify(out, null, 1));
