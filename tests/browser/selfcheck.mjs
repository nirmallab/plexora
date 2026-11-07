// The website's self-check page (/selfcheck; client src/js/services/
// selfcheck.js) in a real browser: Run measures every step, each row shows a
// value and what it means here, nothing is left in the site's storage (the
// pyramid's copy is deleted, Python's scratch boot opens no project store),
// no Content-Security-Policy violation, and "Download results" saves the
// same results as a file.
//
//   node tests/browser/selfcheck.mjs [--headed] [--browser chromium|firefox|webkit]
//        [--url https://plexora-viewer-preview.pages.dev] [--save <file>]
// Prints JSON with `ok`, the checks, the results and the downloaded file.
// `--url` runs a deployed site's page (its first visit through the real CDN)
// instead of a static build served here; `--save` keeps the downloaded file
// (tools/bench_envelope/envelope_table.py add files it).
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { launch, PYTHON, REPO, startServer } from "./lib.mjs";

const headed = process.argv.includes("--headed");
const option = (name) => (process.argv.includes(name) ? process.argv[process.argv.indexOf(name) + 1] : null);
const out = { checks: [], errors: [] };
const check = (name, ok, detail = {}) => {
    out.checks.push({ name, ok: Boolean(ok), ...detail });
    console.error(`${ok ? "ok  " : "FAIL"} ${name}`);
};

let server = null;
let origin = option("--url");
if (origin) {
    origin = origin.replace(/\/+$/, "");
} else {
    const site = mkdtempSync(path.join(tmpdir(), "pxself-site-"));
    execFileSync(PYTHON, ["-m", "plexora.client.static_build", "--out", site],
                 { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO }, stdio: ["ignore", "ignore", "inherit"] });
    server = await startServer(["--site", site]);
    origin = `http://127.0.0.1:${server.port}`;
}
const browser = await launch({ headed });

let page = null;
try {
    page = await browser.context.newPage();
    const violations = [];
    await page.exposeFunction("__selfcheckViolation", (what) => violations.push(what));
    await page.addInitScript(() => {
        document.addEventListener("securitypolicyviolation",
                                  (event) => window.__selfcheckViolation(`${event.violatedDirective} ${event.blockedURI}`));
    });
    page.on("pageerror", (error) => out.errors.push(String(error && error.message || error)));
    page.on("console", (message) => { if (message.type() === "error") (out.console ||= []).push(message.text().slice(0, 300)); });
    await page.goto(`${origin}/selfcheck`);
    check("the address /selfcheck is the self-check page", await page.locator("#selfcheck-run").count() === 1);
    const buttons = await page.$$eval(".selfcheck-actions button", (nodes) => nodes.map((n) => ({
        text: n.textContent.trim(), top: Math.round(n.getBoundingClientRect().top), disabled: n.disabled })));
    check("two buttons on one row, Download off until a run", buttons.length === 2
          && buttons[0].top === buttons[1].top && buttons[1].disabled, { buttons });

    const t0 = Date.now();
    await page.click("#selfcheck-run");
    await page.waitForFunction(() => ["done", "failed"].includes(document.body.dataset.state), null, { timeout: 600000 });
    out.ms = Date.now() - t0;
    const results = await page.evaluate(() => window.PlexoraSelfcheck.results);
    out.results = results;
    const steps = results.steps || {};
    const names = ["browser", "storage", "download", "python", "pyramid", "memory", "gpu"];
    check("every step measured", names.every((name) => steps[name] && steps[name].ok),
          { failed: names.filter((name) => !(steps[name] && steps[name].ok)).map((name) => [name, steps[name]?.here]) });
    const rows = await page.$$eval("#selfcheck-results tr", (trs) => trs.map((tr) => [...tr.children].map((td) => td.textContent)));
    check("a row per step, each with a value and what it means", rows.length === names.length
          && rows.every(([label, value, here]) => label && value && value !== "Measuring…" && (here || label === "Storage")), { rows });
    const python = steps.python?.detail || {};
    check("Python started, each phase timed", python.ready_ms > 0 && python.core_ms > 0
          && python.core_ms <= python.packages_ms && python.packages_ms <= python.ready_ms, { python });
    const pyramid = steps.pyramid?.detail || {};
    check("the pyramid was built and its rate set beside the estimate", pyramid.bytes_per_second > 0
          && pyramid.estimate?.flat_limit_bytes > 0 && pyramid.measured?.flat_limit_bytes > 0
          && pyramid.written_bytes > 0, { pyramid });
    const download = steps.download?.detail || {};
    check("the download rate counts bytes as sent", download.bytes > 0 && download.bytes_per_second > 0
          && download.first_visit_bytes > 0, { download });
    check("WebAssembly memory measured", steps.memory?.detail?.wasm_bytes >= 1 << 30, { memory: steps.memory?.detail });

    const left = await page.evaluate(async () => {
        const list = async (dir) => { const names = []; for await (const [name] of dir.entries()) names.push(name); return names; };
        const root = await navigator.storage.getDirectory();
        const top = await list(root);
        let plexora = [];
        let derived = [];
        try {
            const p = await root.getDirectoryHandle("plexora");
            plexora = await list(p);
            if (plexora.includes("derived")) derived = await list(await p.getDirectoryHandle("derived"));
        } catch (error) { /* none */ }
        return { top, plexora, derived };
    });
    check("nothing left in the site's storage but the empty copies folder",
          left.derived.length === 0 && left.plexora.every((name) => name === "derived")
          && left.top.every((name) => name === "plexora"), { left });

    const [file] = await Promise.all([page.waitForEvent("download"), page.click("#selfcheck-save")]);
    const saved = JSON.parse(readFileSync(await file.path(), "utf-8"));
    if (option("--save")) copyFileSync(await file.path(), option("--save"));
    out.download = { name: file.suggestedFilename() };
    check("Download results saves the same results", /^plexora-selfcheck-\d{4}-\d\d-\d\d\.json$/.test(file.suggestedFilename())
          && saved.selfcheck === 1 && JSON.stringify(saved.steps) === JSON.stringify(results.steps));
    check("no Content-Security-Policy violation", violations.length === 0, { violations });
    check("no page errors", out.errors.length === 0, { errors: out.errors });
} catch (error) {
    out.errors.push(String(error && error.stack || error));
    // Where it stopped: each row as the page shows it.
    out.rows = page ? await page.$$eval("#selfcheck-results tr", (trs) => trs.map((tr) => tr.innerText)).catch(() => null) : null;
    check("the driver ran", false, { rows: out.rows });
} finally {
    await browser.close();
    if (server) server.stop();
}

out.ok = out.checks.length > 0 && out.checks.every((c) => c.ok);
console.log(JSON.stringify(out));
process.exit(out.ok ? 0 : 1);
