// The website as a visitor gets it: the static build at the root, the loader,
// Plexora's Python in the page, and the page the address asks for. Prints
// JSON (what loaded, how long it took, every console error and failed
// request); --shots DIR saves a screenshot of each address. --url drives a
// deployed site instead of the local build; an address given twice is a
// return visit (the same profile, so the HTTP cache is warm).
//
//   node tests/browser/site.mjs [--paths /,/settings] [--url https://...] [--shots DIR] [--headed]
import path from "node:path";
import { REPO, appFrame, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const paths = opt("--paths", "/,/settings,/open_project").split(",");
const shots = opt("--shots", null);
const site = opt("--site", path.join(REPO, "plexora", "client", "dist", "static"));

const remote = opt("--url", null);
const server = remote ? null : await startServer(["--site", site]);
const origin = remote ? remote.replace(/\/$/, "") : `http://127.0.0.1:${server.port}`;
const browser = await launch({ headed: args.includes("--headed") });
const out = { origin, pages: {} };
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
    // What the site's Content-Security-Policy refused (or would refuse, while
    // it is report-only), from the page, its frames and its workers.
    let csp = [];
    const refused = (where) => (message) => {
        if (/Content Security Policy/i.test(message.text())) csp.push(`${where}${message.text().slice(0, 300)}`);
    };
    page.on("console", refused(""));
    page.on("worker", (worker) => worker.on("console", refused("worker: ")));
    for (const [visit, address] of paths.entries()) {
        errors = [];
        failed = [];
        csp = [];
        const started = Date.now();
        await page.goto(`${origin}${address}`);
        const failedBoot = await page.waitForSelector(".plexora-boot-failed", { timeout: 1500 })
            .then(() => true, () => false);
        const frame = failedBoot ? page.mainFrame() : await appFrame(page);
        const written = Date.now() - started;
        const ready = await page.evaluate(() => window.PlexoraPython && window.PlexoraPython.ready
            && window.PlexoraPython.ready.then((info) => ({ at: Math.round(performance.now()),
                                                            stages: info && info.stages })))
            .catch((error) => ({ failed: String(error) }));
        await page.waitForTimeout(2500);
        const tab = await page.evaluate(() => ({ address: location.pathname, title: document.title }));
        const state = await frame.evaluate(() => ({
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
        const key = paths.indexOf(address) === visit ? address : `${address} (visit ${visit + 1})`;
        out.pages[key] = { writtenMs: written, ready, tab, ...state, errors: errors.slice(0, 15),
                           failed: failed.slice(0, 15), csp: csp.slice(0, 15) };
    }
} finally {
    await browser.close();
    if (server) server.stop();
}
console.log(JSON.stringify(out, null, 1));
