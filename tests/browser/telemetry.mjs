// The website sends the desktop's anonymous usage counts, under the same
// rules (plexora/browser/telemetry.py): a build that names the service starts
// telemetry in the worker, in the default mode; the uploader registers and
// posts from the site's origin through the page's fetch (a CORS preflight for
// the token and the gzip, as the Worker's backend/src/cors.ts admits), and the
// client block says runtime/launch_mode/deployment/install_kind "web"; what it
// keeps -- the browser's install id and the token in the queue -- is in the
// site's IndexedDB, so the next visit is the same install and does not
// register again; and a browser sending Global Privacy Control is off, as
// DO_NOT_TRACK is on a desktop. The service is tests/browser/mock_telemetry.py.
//
//   node tests/browser/telemetry.mjs [--headed]
// Prints JSON with `ok` and the checks.
import { execFileSync, spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { appFrame, launch, PYTHON, REPO, seedBrowserStore, startServer } from "./lib.mjs";

const headed = process.argv.includes("--headed");

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

const out = { checks: [], errors: [] };
const check = (name, ok, detail = {}) => {
    out.checks.push({ name, ok: Boolean(ok), ...detail });
    console.error(`${ok ? "ok  " : "FAIL"} ${name}`);
};

const site = mkdtempSync(path.join(tmpdir(), "pxtel-site-"));
const mock = await startMock();
const service = `http://127.0.0.1:${mock.port}`;
const requests = async () => (await (await fetch(`${service}/_requests`)).json()).requests;
execFileSync(PYTHON, ["-m", "plexora.client.static_build", "--out", site, "--telemetry", service],
             { cwd: REPO, env: { ...process.env, PYTHONPATH: REPO }, stdio: ["ignore", "ignore", "inherit"] });
const server = await startServer(["--site", site]);
const origin = `http://127.0.0.1:${server.port}`;

//: The boot's telemetry answer, from the host page.
const booted = (page) => page.evaluate(async () => (await window.PlexoraPython.ready).info.telemetry || null);
//: Settings' "Send now": one upload of everything queued, open hour included.
const sendNow = (frame) => frame.evaluate(async () => {
    const answer = await fetch(plexoraUrl("telemetry/send"), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    return (await answer.json()).outcome;
});
//: What the site keeps in IndexedDB for telemetry: the files' names and the install id.
const kept = (page) => page.evaluate(() => new Promise((resolve, reject) => {
    const request = indexedDB.open("plexora-telemetry", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("files");
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
        const db = request.result;
        const tx = db.transaction("files", "readonly");
        const keys = tx.objectStore("files").getAllKeys();
        const prefs = tx.objectStore("files").get("prefs.json");
        tx.oncomplete = () => {
            db.close();
            const text = prefs.result ? new TextDecoder().decode(prefs.result) : "{}";
            resolve({ keys: keys.result.map(String).sort(), install: JSON.parse(text).install_id || null });
        };
    };
}));

const browser = await launch({ headed });
let other = null;
try {
    const page = await browser.context.newPage();
    page.on("pageerror", (error) => out.errors.push(`pageerror: ${String(error).slice(0, 300)}`));
    await seedBrowserStore(browser.context, origin);
    await page.goto(`${origin}/?pydebug`);
    let frame = await appFrame(page);
    const first = await booted(page);
    check("a build naming the service starts telemetry, in the default mode, keeping the queue",
          first && first.enabled === true && first.mode === "anonymous" && first.source === "default"
          && first.kept === true, { first });

    out.firstSend = await sendNow(frame);
    let seen = await requests();
    const register = seen.filter((r) => r.method === "POST" && r.path === "/v1/telemetry/register");
    const events = seen.filter((r) => r.method === "POST" && r.path === "/v1/telemetry/events");
    const preflights = seen.filter((r) => r.method === "OPTIONS");
    check("the first upload registers and sends from the site's origin", out.firstSend === "sent"
          && register.length === 1 && events.length === 1 && events[0].origin === origin
          && register[0].origin === origin, { outcome: out.firstSend, paths: seen.map((r) => `${r.method} ${r.path}`) });
    const asked = (preflights.find((r) => r.path === "/v1/telemetry/events") || {}).asked || "";
    check("the upload is preflighted for the token and the gzip (what the Worker's CORS admits)",
          /authorization/i.test(asked) && /content-encoding/i.test(asked) && !/user-agent/i.test(asked),
          { asked });
    const client = events[0]?.body?.client || {};
    check("the client block says web", client.runtime === "web" && client.launch_mode === "web"
          && client.deployment === "web" && client.install_kind === "web" && client.mode === "anonymous",
          { client: { runtime: client.runtime, launch_mode: client.launch_mode, deployment: client.deployment,
                      install_kind: client.install_kind, os: client.os, python: client.python } });
    check("the batch is gzipped, carries the token and some counts", events[0]?.encoding === "gzip"
          && events[0]?.authorization === true && (events[0]?.body?.events || []).length > 0,
          { types: [...new Set((events[0]?.body?.events || []).map((e) => e.type))] });
    // A tab being hidden: telemetry's turn (the counts into the kept queue,
    // the files saved); the open hour stays for later, as on a desktop.
    const turn = await page.evaluate(() => window.PlexoraPython.telemetry({ hidden: true }));
    check("a hidden tab's turn queues and keeps what was counted",
          turn && turn.enabled === true && turn.kept === true && ["nothing", "flushed"].includes(turn.outcome)
          && Array.isArray(turn.saved), { turn });
    const store = await kept(page);
    check("the install id and the queue are kept in the site's IndexedDB",
          store.keys.includes("prefs.json") && store.keys.includes("queue.sqlite")
          && store.install === client.install_id, { keys: store.keys });

    // The next visit: the same install, and the token from the kept queue.
    await page.reload();
    frame = await appFrame(page);
    const again = await booted(page);
    out.secondSend = await sendNow(frame);
    seen = await requests();
    const later = seen.filter((r) => r.method === "POST" && r.path === "/v1/telemetry/events");
    check("a second visit is the same install and does not register again",
          again && again.kept === true && out.secondSend === "sent"
          && seen.filter((r) => r.method === "POST" && r.path === "/v1/telemetry/register").length === 1
          && later.length === 2 && later[1].body?.client?.install_id === client.install_id,
          { outcome: out.secondSend });

    // Global Privacy Control (Do Not Track): off, as DO_NOT_TRACK on a desktop.
    other = await launch({ headed });
    await other.context.addInitScript(() => {
        Object.defineProperty(Navigator.prototype, "globalPrivacyControl", { get: () => true, configurable: true });
    });
    await seedBrowserStore(other.context, origin);
    const quiet = await other.context.newPage();
    await quiet.goto(`${origin}/?pydebug`);
    const quietFrame = await appFrame(quiet);
    const declined = await booted(quiet);
    const before = (await requests()).length;
    const quietSend = await sendNow(quietFrame);
    check("Global Privacy Control turns it off and nothing is sent",
          declined && declined.enabled === false && declined.source === "do_not_track"
          && quietSend === "off" && (await requests()).length === before, { declined, quietSend });
} catch (error) {
    out.errors.push(String(error?.stack || error).slice(0, 2000));
} finally {
    await browser.close().catch(() => null);
    if (other) await other.close().catch(() => null);
    server.stop();
    mock.stop();
}
out.ok = out.errors.length === 0 && out.checks.length > 0 && out.checks.every((c) => c.ok);
console.log(JSON.stringify(out, null, 1));
process.exit(out.ok ? 0 : 1);
