// The site's switches as a visitor meets them (services/shell.js `readFlags`;
// tools/browser_build/publish.py flags): for each case the flags file is
// written (or removed) and the site opened in a fresh profile. Prints, per
// case, what the loader said, whether the app's frame came up, how many
// requests reached the Python bundle, whether the preview mark was drawn, and
// -- where the app came up -- what Python's magic select says about its model.
//
// Then the count of browsers the loader turns away (services/refusalCount.js):
// an old Chrome on a build that names a telemetry service (built here,
// `--telemetry` pointing at tests/browser/mock_telemetry.py, the Worker's
// stand-in) -- counted once, not again on the next visit that day, and not at
// all with Do Not Track, Global Privacy Control, or "off" chosen earlier on
// the site. Every case also says what left the site's origin and whether the
// enforced Content-Security-Policy refused anything.
//
//   node tests/browser/flags.mjs --site DIR --flags-dir DIR
//
// `--site` is a static build made with `--flags /fx/flags.json` (and no
// `--telemetry`); `--flags-dir` the folder served at /fx/ that this writes
// flags.json into.
import { execFileSync, spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { appFrame, launch, PYTHON, REPO, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name) => args[args.indexOf(name) + 1];
const site = opt("--site");
const dir = opt("--flags-dir");

//: A Chrome too old for the website's Python (no JSPI, before 137): this
//: Chrome made to look like the Chrome 131 that crashed on a Mac.
const OLD_CHROME = () => {
    delete WebAssembly.Suspending;
    Object.defineProperty(Navigator.prototype, "userAgentData", { configurable: true, get: () => ({
        brands: [{ brand: "Google Chrome", version: "131" }, { brand: "Chromium", version: "131" }] }) });
};

//: Every Content-Security-Policy refusal on the page, kept for the driver.
const WATCH_CSP = () => {
    window.__cspRefused = [];
    document.addEventListener("securitypolicyviolation",
                              (event) => window.__cspRefused.push(`${event.violatedDirective} ${event.blockedURI}`));
};

const CASES = [
    ["absent", null],
    ["paused", { viewer: "desktop-only", message: "Back at noon." }],
    ["stale", { min_site_version: "99.0.0" }],
    ["preview", { viewer: "preview", models: { mobilesam: { enabled: false } } }],
    ["broken", "{not json"],
    ["oldChrome", null, OLD_CHROME],
];

const SAM = [
    "import json",
    "from plexora.vision import sam_backend",
    "json.dumps(sam_backend.executor().status())",
].join("\n");

//: Watches a page: requests to the Python bundle, requests that leave the
//: site's origin, and the site's scripts it loaded.
function watch(page, origin) {
    const seen = { python: 0, offsite: [], scripts: [] };
    page.on("request", (request) => {
        const url = new URL(request.url());
        if (url.origin !== origin) {
            if (!/^(data|blob|chrome-extension):/.test(url.protocol)) seen.offsite.push(`${request.method()} ${url.origin}${url.pathname}`);
            return;
        }
        if (url.pathname.startsWith("/py/")) seen.python += 1;
        if (url.pathname.endsWith(".js")) seen.scripts.push(url.pathname.split("/").pop());
    });
    return seen;
}

async function stoppedSaid(page) {
    const stopped = await page.waitForSelector(".plexora-boot-actions button", { timeout: 15000 })
        .then(() => true).catch(() => false);
    if (!stopped) return null;
    return {
        said: await page.textContent("#plexora-boot-status"),
        buttons: await page.$$eval(".plexora-boot-actions button", (nodes) => nodes.map((n) => n.textContent)),
    };
}

const out = { cases: {}, refusals: {} };
const server = await startServer(["--site", site, "--tree", `fx=${dir}`]);
const origin = `http://127.0.0.1:${server.port}`;
try {
    for (const [name, flags, pretend] of CASES) {
        const file = path.join(dir, "flags.json");
        if (flags === null) rmSync(file, { force: true });
        else writeFileSync(file, typeof flags === "string" ? flags : JSON.stringify(flags));
        const browser = await launch({});
        const row = {};
        try {
            const page = await browser.context.newPage();
            await page.addInitScript(WATCH_CSP);
            if (pretend) await page.addInitScript(pretend);
            const seen = watch(page, origin);
            await page.goto(`${origin}/?pydebug`);
            const stopped = await stoppedSaid(page);
            if (stopped) {
                row.said = stopped.said;
                row.button = stopped.buttons[0];
                row.buttons = stopped.buttons;
                await page.waitForTimeout(1500);
                row.app = Boolean(await page.$("#plexora-app"));
                row.csp = await page.evaluate(() => window.__cspRefused || null);
            } else {
                await appFrame(page);
                await page.evaluate(() => window.PlexoraPython.ready);
                row.app = true;
                row.mark = await page.evaluate(() => document.getElementById("plexora-preview-mark")?.textContent || null);
                const said = await page.evaluate((code) => window.PlexoraPython.run(code), SAM);
                row.sam = JSON.parse(String(said).replace(/^'|'$/g, ""));
            }
            row.python_requests = seen.python;
            row.offsite = seen.offsite;
            row.refusal_script = seen.scripts.includes("refusalCount.js");
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

// -- the count of refused browsers, on a build that names a service -------------

function startMock() {
    return new Promise((resolve, reject) => {
        const child = spawn(PYTHON, [path.join(REPO, "tests", "browser", "mock_telemetry.py")],
                            { cwd: REPO, stdio: ["ignore", "pipe", "inherit"] });
        let buffer = "";
        child.stdout.on("data", (chunk) => {
            buffer += chunk;
            if (buffer.includes("\n")) {
                try { resolve({ ...JSON.parse(buffer.split("\n")[0]), stop: () => child.kill() }); }
                catch (error) { reject(error); }
            }
        });
        child.on("exit", (code) => reject(new Error(`mock_telemetry.py exited ${code}`)));
    });
}

const mock = await startMock();
const service = `http://127.0.0.1:${mock.port}`;
const requests = async () => (await (await fetch(`${service}/_requests`)).json()).requests;
const posts = async (since) => (await requests()).slice(since).filter((r) => r.method === "POST");
const counted = mkdtempSync(path.join(tmpdir(), "pxrefused-site-"));
execFileSync(PYTHON, ["-m", "plexora.client.static_build", "--out", counted, "--telemetry", service],
             { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO }, stdio: ["ignore", "ignore", "inherit"] });
const countedServer = await startServer(["--site", counted]);
const siteOrigin = `http://127.0.0.1:${countedServer.port}`;

//: Waits until the service has had `n` more POSTs than `since` (or `ms` pass).
async function postsAfter(since, n, ms = 10000) {
    const until = Date.now() + ms;
    let got = await posts(since);
    while (got.length < n && Date.now() < until) {
        await new Promise((resolve) => setTimeout(resolve, 200));
        got = await posts(since);
    }
    return got;
}

//: The settings block the website's worker keeps (workers/telemetryStore.js),
//: written into the site's IndexedDB before the visit.
const choose = (block) => {
    return new Promise((resolve, reject) => {
        const request = indexedDB.open("plexora-telemetry", 1);
        request.onupgradeneeded = () => request.result.createObjectStore("files");
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
            const db = request.result;
            const tx = db.transaction("files", "readwrite");
            tx.objectStore("files").put(new TextEncoder().encode(JSON.stringify(block)), "prefs.json");
            tx.oncomplete = () => { db.close(); resolve(true); };
            tx.onerror = () => reject(tx.error);
        };
    });
};

const REFUSALS = [
    ["counted", []],
    ["dnt", [() => Object.defineProperty(Navigator.prototype, "doNotTrack", { configurable: true, get: () => "1" })]],
    ["gpc", [() => Object.defineProperty(Navigator.prototype, "globalPrivacyControl", { configurable: true, get: () => true })]],
    ["chosenOff", [], { mode: "off", notice_shown: true, install_id: "f".repeat(32) }],
];

try {
    for (const [name, extra, prefs] of REFUSALS) {
        const browser = await launch({});
        const row = {};
        try {
            const page = await browser.context.newPage();
            if (prefs) {
                await page.goto(`${siteOrigin}/client/shell.json`);
                await page.evaluate(choose, prefs);
            }
            await page.addInitScript(WATCH_CSP);
            await page.addInitScript(OLD_CHROME);
            for (const script of extra) await page.addInitScript(script);
            const seen = watch(page, siteOrigin);
            const before = (await requests()).length;
            await page.goto(`${siteOrigin}/`);
            const stopped = await stoppedSaid(page);
            row.said = stopped && stopped.said;
            row.buttons = stopped && stopped.buttons;
            const first = await postsAfter(before, 2, name === "counted" ? 10000 : 3000);
            row.posts = first.map((r) => `${r.method} ${r.path}`);
            row.origins = [...new Set(first.map((r) => r.origin))];
            row.register = (first.find((r) => r.path === "/v1/telemetry/register") || {}).body || null;
            const events = first.find((r) => r.path === "/v1/telemetry/events");
            row.batch = events ? events.body : null;
            row.authorization = events ? events.authorization : null;
            row.encoding = events ? events.encoding || null : null;
            row.all_requests = (await requests()).slice(before).length;
            row.csp = await page.evaluate(() => window.__cspRefused || null);
            row.databases = await page.evaluate(async () => (indexedDB.databases ? (await indexedDB.databases()).map((d) => d.name) : null));
            row.kept = await page.evaluate(() => { try { return JSON.parse(localStorage.getItem("plexora-refused")); } catch (e) { return "unreadable"; } });
            row.refusal_script = seen.scripts.includes("refusalCount.js");
            // The next visit that day, in the same browser.
            const again = (await requests()).length;
            await page.reload();
            await stoppedSaid(page);
            await page.waitForTimeout(3000);
            row.again_requests = (await requests()).slice(again).length;
            row.python_requests = seen.python;
            row.offsite = [...new Set(seen.offsite)];
        } catch (error) {
            row.error = String(error).slice(0, 500);
        } finally {
            await browser.close();
        }
        out.refusals[name] = row;
    }
    out.site_origin = siteOrigin;
} finally {
    countedServer.stop();
    mock.stop();
    rmSync(counted, { recursive: true, force: true });
}
console.log(JSON.stringify(out));
