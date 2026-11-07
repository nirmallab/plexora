// The website holds its own licence (plan D8), end to end in Chrome, against
// a stand-in platform (tests/browser/mock_platform.py: biocognia.testing's
// shapes and a throwaway signing key, with the CORS the platform sends a page):
//
//   1. On Free the page asks the platform nothing and keeps nothing.
//   2. Settings > Account > Connect this browser: the device flow through the
//      mock; Pro in Settings; in Python (the worker) the licence is active, a
//      Pro capability's guard passes and the files are where biocognia reads
//      them; IndexedDB keeps the certificate and the device secret, never the
//      code or the email; the account shows; the AI credits show (Python asked
//      the page for the AI token and called the stand-in gateway with it).
//   3. A second tab of the site is licensed from the same storage.
//   4. Reloaded with the platform unreachable: still Pro, Python unlocked, a
//      forced refresh fails silently, and nothing reached the platform.
//   5. Forget This Browser: the mock is told (deactivate, with this browser's
//      binding), IndexedDB and /bioc are cleared, both tabs read Free.
//
//   PLEXORA_BENCH_HOME=<bundle home> node tests/browser/license.mjs [--headed] [--shots DIR]
//
// Builds the site into a temp folder with --licence-api pointing at the mock
// and --licence-test-key trusting its key (a build publish.py refuses), serves
// it with tools/browser_build/serve.py (the Python bundle from
// PLEXORA_BENCH_HOME), and prints a JSON record of the checks: states and
// yes/no only, never a certificate, code, key, token or email.
import { execFileSync, spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { PYTHON, REPO, appFrame, launch, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const headed = args.includes("--headed");
const shots = args.includes("--shots") ? args[args.indexOf("--shots") + 1] : null;
const shoot = async (page, name) => {
    if (!shots) return;
    await page.waitForTimeout(400);
    await page.screenshot({ path: path.join(shots, `licence-${name}.png`) });
};

function startMock(home) {
    return new Promise((resolve, reject) => {
        const child = spawn(PYTHON, [path.join(REPO, "tests", "browser", "mock_platform.py"), "--bind-version", "2",
                                     "--home", home], { cwd: REPO, stdio: ["ignore", "pipe", "inherit"] });
        let buffer = "";
        child.stdout.on("data", (chunk) => {
            buffer += chunk;
            if (buffer.includes("\n")) {
                try { resolve({ ...JSON.parse(buffer.split("\n")[0]), stop: () => child.kill() }); }
                catch (error) { reject(error); }
            }
        });
        child.on("exit", (code) => reject(new Error(`mock_platform.py exited ${code}`)));
    });
}

const out = { checks: [], errors: [] };
const check = (name, ok, detail = {}) => {
    out.checks.push({ name, ok: Boolean(ok), ...detail });
    console.error(`${ok ? "ok  " : "FAIL"} ${name}`);
};

const site = mkdtempSync(path.join(tmpdir(), "pxlic-site-"));
const data = mkdtempSync(path.join(tmpdir(), "pxlic-data-"));
const mock = await startMock(path.join(data, "mock-platform"));
const api = `http://127.0.0.1:${mock.port}`;
const mockRequests = async () => (await (await fetch(`${api}/_requests`)).json()).requests;
execFileSync(PYTHON, ["-m", "plexora.client.static_build", "--out", site, "--licence-api", `${api}/v1`,
                      "--licence-test-key", `${mock.kid}=${mock.public_key}`],
             { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO, PLEXORA_DATA_PATH: data },
               stdio: ["ignore", "ignore", "inherit"] });
const server = await startServer(["--site", site]);
const browser = await launch({ headed });
const origin = `http://127.0.0.1:${server.port}`;

// The first visit asks where projects go; this test keeps them in the browser.
async function keepInBrowser(frame) {
    const until = Date.now() + 5000;
    while (Date.now() < until) {
        const clicked = await frame.evaluate(() => {
            const button = [...document.querySelectorAll(".plx-confirm .plx-button")]
                .find((node) => node.textContent.trim() === "Keep in browser");
            if (!button) return false;
            button.click();
            return true;
        }).catch(() => false);
        if (clicked) return true;
        await new Promise((resolve) => setTimeout(resolve, 200));
    }
    return false;
}

//: Python in the page's worker (?pydebug): its licence as biocognia resolves it.
const PYTHON_LICENCE = `
import json, os
from plexora import licensing
from plexora.licensing import guards
state = licensing.peek()
try:
    guards.check("ai:gating:session")
    guarded = "unlocked"
except Exception as exc:
    guarded = getattr(exc, "code", type(exc).__name__)
listing = sorted(os.listdir("/bioc")) + sorted("plexora/" + n for n in (os.listdir("/bioc/plexora") if os.path.isdir("/bioc/plexora") else []))
json.dumps({"state": state.state, "environment": state.environment_type, "volume": licensing.allows("volume"),
            "guard": guarded, "files": listing})
`;
const pythonLicence = async (page) => JSON.parse(await page.evaluate((code) => window.PlexoraPython.run(code),
                                                                     PYTHON_LICENCE));

//: What this browser keeps for the site's licence, read as the page reads it.
const kept = (page) => page.evaluate(() => new Promise((resolve, reject) => {
    const request = indexedDB.open("plexora-licence", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("kv");
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
        const db = request.result;
        const tx = db.transaction("kv", "readonly");
        const store = tx.objectStore("kv");
        const keys = store.getAllKeys();
        const values = store.getAll();
        tx.oncomplete = () => {
            db.close();
            resolve({ keys: keys.result.map(String).sort(), text: JSON.stringify(values.result) });
        };
    };
}));

const plan = (frame) => frame.evaluate(() => document.getElementById("settings_license_plan")?.textContent.trim());

try {
    const context = browser.context;
    const page = await context.newPage();
    // The portal page the device flow opens: no such site here; closed at once.
    page.on("popup", (opened) => { opened.close().catch(() => null); });
    page.on("pageerror", (error) => out.errors.push(`pageerror: ${String(error).slice(0, 300)}`));
    await page.goto(`${origin}/settings?pydebug#license`);
    let frame = await appFrame(page);
    out.firstVisitAsked = await keepInBrowser(frame);
    await frame.waitForSelector("#settings_license_connect", { timeout: 60000 });

    // 1. Free.
    await shoot(page, "free");
    const free = await kept(page);
    check("on Free the page asks the platform nothing and keeps nothing",
          (await mockRequests()).length === 0 && free.keys.length === 0 && (await plan(frame)) === "Free",
          { kept: free.keys });

    // 2. Connect this browser.
    await frame.click("#settings_license_connect");
    await frame.waitForFunction(() => document.getElementById("settings_license_plan")?.textContent.trim() === "Pro",
                                null, { timeout: 60000 });
    await shoot(page, "pro");
    const requests = await mockRequests();
    const start = requests.find((r) => r.path === "/activate/start");
    const polls = requests.filter((r) => r.path === "/activate/poll");
    const python = await pythonLicence(page);
    check("the device flow ran against the platform for a browser, and Python reads the licence as biocognia's own",
          start && start.kind === "browser" && /^plexora\//.test(start.client || "")
          && polls.length >= 2 && polls.every((p) => p.binding === start.binding)
          && python.state === "active" && python.environment === "browser" && python.volume === true
          && python.guard === "unlocked"
          && ["environment.json", "plexora/license.json"].every((name) => python.files.includes(name)),
          { python: { state: python.state, environment: python.environment, guard: python.guard, files: python.files },
            polls: polls.length });
    const held = await kept(page);
    check("IndexedDB keeps the certificate record and the device secret, never the code, key or email",
          JSON.stringify(held.keys) === JSON.stringify(["environment", "licence"])
          && held.text.includes("BIOC1.") && !held.text.includes("BIOC-TEST-0001")
          && !held.text.includes("ada@") && !held.text.includes("BIOCAI1"),
          { kept: held.keys });
    const account = await frame.waitForFunction(
        () => document.getElementById("settings_account_name")?.textContent.trim() === "Ada Lovelace", null,
        { timeout: 20000 }).then(() => true, () => false);
    check("the account shows, asked of the platform by the page and kept nowhere",
          account && !(await kept(page)).text.includes("ada@"));
    const credits = await frame.waitForFunction(() => {
        const count = document.getElementById("settings_account_credits_count")?.textContent.trim();
        return count && count !== "—" && !document.getElementById("settings_account_credits")?.hidden;
    }, null, { timeout: 30000 }).then(() => true, () => false);
    const tokenAsked = (await mockRequests()).filter((r) => r.path === "/ai/token").length;
    check("the AI credits show: Python asked the page for the AI token and called the gateway with it",
          credits && tokenAsked >= 1 && !(await kept(page)).text.includes("BIOCAI1"),
          { tokenExchanges: tokenAsked });

    // 3. A second tab.
    const second = await context.newPage();
    second.on("pageerror", (error) => out.errors.push(`tab 2 pageerror: ${String(error).slice(0, 300)}`));
    await second.goto(`${origin}/?pydebug`);
    await appFrame(second);
    const other = await pythonLicence(second);
    check("a second tab of the site is licensed from the same storage", other.state === "active",
          { state: other.state });

    // 4. Offline.
    await context.route(`${api}/**`, (route) => route.abort("internetdisconnected"));
    const asked = (await mockRequests()).length;
    await page.reload();
    frame = await appFrame(page);
    await frame.waitForFunction(() => document.getElementById("settings_license_plan")?.textContent.trim() === "Pro",
                                null, { timeout: 60000 });
    const offline = await pythonLicence(page);
    const refreshed = await page.evaluate(() => window.PlexoraLicense.heartbeat({ force: true }));
    const after = await pythonLicence(page);
    const paid = await frame.evaluate(() => Boolean(window.flaskVariables?.license?.paid));
    check("reloaded with the platform unreachable: still Pro, Python unlocked, a forced refresh fails silently",
          offline.state === "active" && offline.guard === "unlocked" && refreshed === "failed"
          && after.state === "active" && paid && (await mockRequests()).length === asked,
          { refresh: refreshed, state: after.state });
    await context.unroute(`${api}/**`);

    // 5. Forget this browser.
    await frame.click("#settings_license_forget");
    const confirm = await frame.waitForSelector(".plx-confirm .plx-button-danger", { timeout: 10000 });
    await shoot(page, "forget");
    await confirm.click();
    await frame.waitForFunction(() => document.getElementById("settings_license_plan")?.textContent.trim() === "Free",
                                null, { timeout: 30000 });
    const released = (await mockRequests()).filter((r) => r.path === "/deactivate");
    const cleared = await kept(page);
    const gone = await pythonLicence(page);
    let otherTab = null;
    for (let i = 0; i < 50; i += 1) {
        otherTab = await pythonLicence(second);
        if (otherTab.state === "free") break;
        await new Promise((resolve) => setTimeout(resolve, 200));
    }
    check("Forget released the device, cleared IndexedDB and /bioc, and both tabs read Free",
          released.length === 1 && released[0].binding === start.binding && cleared.keys.length === 0
          && gone.state === "free" && !gone.files.some((name) => name.endsWith(".json")) && gone.guard === "license_required"
          && otherTab.state === "free",
          { released: released.length, kept: cleared.keys, files: gone.files, otherTab: otherTab.state });
} catch (error) {
    out.errors.push(String(error && error.stack || error).slice(0, 2000));
} finally {
    await browser.close();
    server.stop();
    mock.stop();
    rmSync(site, { recursive: true, force: true });
    rmSync(data, { recursive: true, force: true });
}
out.ok = out.checks.length === 8 && out.checks.every((c) => c.ok) && out.errors.length === 0;
console.log(JSON.stringify(out, null, 1));
process.exit(out.ok ? 0 : 1);
