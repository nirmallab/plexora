// The website's smoke test (web.yml's smoke job): correctness only, never
// timing. The static build at the root and the Python bundle under /py/, as
// Pages and the assets bucket serve them (tools/browser_build/serve.py), in
// a headless Chromium:
//
//   boot     the page is cross-origin isolated, the app's first page is
//            written, and the website's Python reaches ready without an error;
//   open     an image picked through the home page's Browse (the page's
//            <input type=file> path: no driver can answer the native picker)
//            opens, and the viewer draws every channel's tiles;
//   policy   no `securitypolicyviolation` in any frame, no page error, no
//            failed request on the site's own origin.
//
// It also counts the shell: the bytes the first visit took from the site's
// own origin outside /py/ (the bundle is the assets bucket's), as served and
// gzipped (Pages compresses text on the fly; gzip is the larger estimate).
// tools/browser_build/budget.py --shell holds it to its bar.
//
//   node tests/browser/smoke.mjs --site DIR --fixture FILE [--py DIR]
//        [--swiftshader] [--shell-out FILE] [--shots DIR] [--headed]
import { writeFileSync } from "node:fs";
import path from "node:path";
import { gzipSync } from "node:zlib";
import { SWIFTSHADER, appFrame, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const site = opt("--site", null);
const fixture = opt("--fixture", null);
const pyRoot = opt("--py", null);
const shots = opt("--shots", null);
const shellOut = opt("--shell-out", null);
const timeout = Number(opt("--timeout", "300")) * 1000;
if (!site || !fixture) throw new Error("--site DIR (static_build's output) and --fixture FILE are required");

const server = await startServer(["--site", site, ...(pyRoot ? ["--py", pyRoot] : [])]);
const origin = `http://127.0.0.1:${server.port}`;
const browser = await launch({ headed: args.includes("--headed"),
                               args: args.includes("--swiftshader") ? SWIFTSHADER : [] });
const out = { origin, checks: {}, failed: [] };
const fail = (check, why) => out.failed.push(`${check}: ${why}`);
const violations = [];
const errors = [];
const failedRequests = [];
const shell = { requests: 0, bytes: 0, gzipBytes: 0, files: [] };
let countingShell = true;
const counted = new Set();
const pending = [];

try {
    // Every frame reports its policy violations here (the app runs in an
    // iframe): a binding reaches every frame of the context.
    await browser.context.exposeBinding("plexoraSmokeViolation", (_source, violation) => {
        violations.push(violation);
    });
    await browser.context.addInitScript(() => {
        document.addEventListener("securitypolicyviolation", (event) => {
            window.plexoraSmokeViolation({ directive: event.violatedDirective, blocked: event.blockedURI,
                                           source: event.sourceFile, line: event.lineNumber,
                                           frame: location.pathname });
        });
    });
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    page.on("pageerror", (error) => errors.push(`pageerror: ${String(error).slice(0, 300)}`));
    page.on("console", (message) => {
        if (message.type() === "error") errors.push(message.text().slice(0, 300));
    });
    page.on("requestfailed", (request) => {
        if (request.url().startsWith(origin)) {
            failedRequests.push(`${request.failure()?.errorText} ${request.url().slice(origin.length)}`);
        }
    });
    page.on("response", (response) => {
        const url = response.url();
        if (!url.startsWith(origin)) return;
        const where = new URL(url).pathname;
        if (response.status() >= 400) failedRequests.push(`${response.status()} ${where}`);
        // A response the service worker made is Python's answer (a route),
        // not a file of the site.
        if (!countingShell || where.startsWith("/py/") || response.status() !== 200
            || response.fromServiceWorker() || counted.has(url)) return;
        // Once per address: a file asked for again comes from the HTTP cache.
        counted.add(url);
        pending.push(response.body().then((body) => {
            shell.requests += 1;
            shell.bytes += body.length;
            shell.gzipBytes += gzipSync(body, { level: 6 }).length;
            shell.files.push([where, body.length]);
        }, () => { /* a body the page did not keep (a redirect, a stream) */ }));
    });

    // boot
    const opened = Date.now();
    await page.goto(`${origin}/`);
    const frame = await appFrame(page, { timeout });
    const ready = await page.evaluate(() => window.PlexoraPython.ready.then(
        (info) => ({ error: (info && info.error) || null, stages: info && info.stages }),
        (error) => ({ error: String(error) })));
    out.checks.boot = {
        isolated: await page.evaluate(() => window.crossOriginIsolated),
        ready,
        // Which GPU draws (on a runner: SwiftShader, or no WebGL at all).
        webgl: await frame.evaluate(() => {
            const gl = document.createElement("canvas").getContext("webgl2");
            if (!gl) return null;
            const info = gl.getExtension("WEBGL_debug_renderer_info");
            return info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : "webgl2";
        }),
        seconds: Math.round((Date.now() - opened) / 100) / 10,
    };
    if (!out.checks.boot.isolated) fail("boot", "the page is not cross-origin isolated");
    if (ready.error) fail("boot", `Python: ${ready.error}`);
    await frame.waitForSelector("#quick_view_go", { timeout });
    // A browser that can write folders is asked once where projects are
    // kept (services/shell.js); the smoke keeps them in the browser.
    const keep = frame.locator(".plx-confirm button", { hasText: "Keep in browser" });
    out.checks.boot.asked = await keep.waitFor({ state: "visible", timeout: 15000 }).then(async () => {
        await keep.click();
        await keep.waitFor({ state: "detached", timeout: 15000 });
        return true;
    }, () => false);
    await Promise.all(pending.splice(0));
    countingShell = false;
    shell.files.sort((a, b) => b[1] - a[1]);
    out.checks.shell = { requests: shell.requests, bytes: shell.bytes, gzipBytes: shell.gzipBytes,
                         largest: shell.files.slice(0, 10) };
    if (shots) await page.screenshot({ path: path.join(shots, "smoke-home.png") });

    // open
    const picked = Date.now();
    await page.evaluate(() => { window.showOpenFilePicker = undefined; window.showDirectoryPicker = undefined; });
    const chooser = page.waitForEvent("filechooser", { timeout: 30000 });
    chooser.catch(() => {}); // awaited below; a click that hangs must not crash the run first
    await frame.click("#quick_view_go", { timeout: 30000 });
    await (await chooser).setFiles(fixture);
    // An image that opens as it is goes straight to its page; one that needs
    // a copy opens Import sample first (import.mjs presses the same buttons).
    const pressed = [];
    let moved = false;
    while (!moved && Date.now() - picked < timeout) {
        moved = await frame.evaluate(() => location.pathname !== "/" && window.PlexoraShellFramed
                                     && !document.getElementById("plexora-boot")).catch(() => false);
        if (moved) break;
        const press = await frame.evaluate(() => {
            const visible = (node) => node && !node.disabled && node.offsetParent !== null;
            const confirm = document.querySelector(".plx-confirm .plx-button-primary");
            if (visible(confirm)) { confirm.click(); return `confirm: ${confirm.textContent.trim()}`; }
            const go = document.querySelector('[data-role="go"]');
            if (visible(go) && !go.dataset.pressedBySmoke) {
                go.dataset.pressedBySmoke = "1";
                go.click();
                return `import: ${go.textContent.trim()}`;
            }
            return null;
        }).catch(() => null);
        if (press) pressed.push(press);
        await page.waitForTimeout(500);
    }
    out.checks.open = { pressed, address: moved ? await frame.evaluate(() => location.pathname) : null };
    if (!moved) {
        fail("open", "the image never left the home page");
    } else {
        const drawn = await frame.waitForFunction(() => {
            const viewer = window.__plexora?.seaDragonViewer?.viewer;
            if (!viewer || !viewer.world || !viewer.world.getItemCount()) return false;
            for (let i = 0; i < viewer.world.getItemCount(); i += 1) {
                if (!viewer.world.getItemAt(i).getFullyLoaded()) return false;
            }
            return true;
        }, null, { timeout, polling: 250 }).then(() => true, () => false);
        out.checks.open.items = await frame.evaluate(
            () => window.__plexora?.seaDragonViewer?.viewer?.world?.getItemCount() ?? null);
        out.checks.open.drawn = drawn;
        if (!drawn) fail("open", "the viewer never drew every channel's tiles");
    }
    if (shots) await page.screenshot({ path: path.join(shots, "smoke-viewer.png") });
} catch (error) {
    fail("run", String(error && error.stack ? error.stack : error).slice(0, 2000));
} finally {
    await browser.close().catch(() => {});
    server.stop();
}

// policy
out.checks.policy = { violations, errors: errors.slice(0, 20), failedRequests: failedRequests.slice(0, 20) };
if (violations.length) fail("policy", `${violations.length} securitypolicyviolation(s): ${JSON.stringify(violations.slice(0, 3))}`);
if (failedRequests.length) fail("policy", `failed requests on the site's origin: ${failedRequests.slice(0, 5).join("; ")}`);
if (errors.some((line) => line.startsWith("pageerror"))) fail("policy", errors.find((line) => line.startsWith("pageerror")));
if (shellOut) {
    writeFileSync(shellOut, JSON.stringify({ requests: shell.requests, bytes: shell.bytes,
                                             gzipBytes: shell.gzipBytes, files: shell.files }, null, 1));
}
console.log(JSON.stringify(out, null, 1));
process.exitCode = out.failed.length ? 1 : 0;
