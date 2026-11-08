// Remote servers from the website, end to end on this computer
// (docs/internal/WEB_REMOTE_RELAY.md): nothing installed for the browser, a
// password and a Duo-style code typed in the page, a data node started
// detached, and a connection no relay restart or tab reload ends.
//
// Runs, in one Chrome:
//   - the relay Worker (relay/, `wrangler dev --local`);
//   - an SSH server that signs in as O2 does (tests/browser/ssh_server.py);
//   - the static site built to use that relay, with this checkout's Python
//     (tools/browser_build/build.py's py/, through serve.py).
// Checks:
//   connect    a saved server connects: host key, password and code answered
//              through the status Python serves; Connected within the limit
//   node       the node is on the map at its room's address and answers
//              through the relay (/remote_health, /list_dir)
//   reload     a reloaded tab reconnects to the same node with no question
//   restart    the relay is stopped and started again: the node answers
//              again by itself, with no question
//   disconnect Disconnect stops the node
//   files      a files-only server (SFTP, nothing started over there): signed
//              in, listed, a file fetched whole and an OME-TIFF imported and
//              drawn through the page's SFTP connection
//   files-restart / files-reload
//              the relay restarted, then the tab reloaded: the page signs in
//              again by itself (the questions asked again) and the same bytes
//              come back
//
//   node tests/browser/remote.mjs [--shots DIR] [--headed]
// Prints JSON with `ok`. Needs asyncssh (`pip install asyncssh`) for the SSH
// server and relay/node_modules (`cd relay && npx npm@11 install`).
import { execFileSync, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { appFrame, launch, PYTHON, REPO, startServer } from "./lib.mjs";

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const work = mkdtempSync(path.join(tmpdir(), "pxremote-"));
const shots = opt("--shots", path.join(work, "shots"));
mkdirSync(shots, { recursive: true });
const NAME = "test-server";
const FILES = "test-files";
const results = { ok: false, steps: {}, shots };
const children = [];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const freePort = () => new Promise((resolve) => {
    const socket = net.createServer();
    socket.listen(0, "127.0.0.1", () => {
        const { port } = socket.address();
        socket.close(() => resolve(port));
    });
});

function spawnLogged(command, argv, options, label) {
    const child = spawn(command, argv, { ...options, stdio: ["ignore", "pipe", "pipe"] });
    children.push(child);
    child.log = [];
    const keep = (chunk) => {
        for (const text of String(chunk).split("\n")) if (text.trim()) child.log.push(`[${label}] ${text}`);
        if (child.log.length > 400) child.log.splice(0, child.log.length - 400);
    };
    child.stdout.on("data", keep);
    child.stderr.on("data", keep);
    return child;
}

async function startRelay(port, sshPort) {
    const child = spawnLogged("npx", [
        "wrangler", "dev", "--local", "--ip", "127.0.0.1", "--port", String(port),
        "--var", "ALLOWED_ORIGINS:http://127.0.0.1:*,http://localhost:*",
        "--var", "ALLOW_PRIVATE_HOSTS:1",
        "--var", `SSH_PORTS:22,${sshPort}`,
        "--var", "FEATURES_URL:",
    ], { cwd: path.join(REPO, "relay"), env: { ...process.env, CI: "1" } }, "relay");
    const until = Date.now() + 90000;
    while (Date.now() < until) {
        try {
            const answer = await fetch(`http://127.0.0.1:${port}/v1/hello`);
            if (answer.ok) return child;
        } catch (error) { /* not yet */ }
        await sleep(500);
    }
    throw new Error(`the relay did not start:\n${child.log.slice(-20).join("\n")}`);
}

function stop(child) {
    return new Promise((resolve) => {
        if (!child || child.exitCode !== null) return resolve();
        child.once("exit", () => resolve());
        child.kill("SIGINT");
        setTimeout(() => { try { child.kill("SIGKILL"); } catch (error) { /* gone */ } }, 5000);
    });
}

function startSsh(port, home) {
    return new Promise((resolve, reject) => {
        const child = spawn(PYTHON, [path.join(REPO, "tests", "browser", "ssh_server.py"),
                                     "--port", String(port), "--home", home],
                            { cwd: REPO, stdio: ["ignore", "pipe", "pipe"] });
        child.log = [];
        child.stderr.on("data", (chunk) => {
            for (const text of String(chunk).split("\n")) if (text.trim()) child.log.push(text.slice(0, 300));
            if (child.log.length > 300) child.log.splice(0, child.log.length - 300);
        });
        sshChild = child;
        children.push(child);
        let buffer = "";
        child.stdout.on("data", (chunk) => {
            buffer += chunk;
            if (buffer.includes("\n")) {
                try { resolve({ child, ...JSON.parse(buffer.split("\n")[0]) }); }
                catch (error) { reject(error); }
            }
        });
        child.on("exit", (code) => reject(new Error(`ssh_server.py exited ${code}`)));
    });
}

//: A request the page's own app would make, answered as it would be.
function call(frame, route, init = {}) {
    return frame.evaluate(async ([where, options]) => {
        const response = await window.PlexoraBackend.fetch(where, options);
        const text = await response.text();
        let body = null;
        try { body = JSON.parse(text); } catch (error) { body = text; }
        return { status: response.status, body };
    }, [route, init]);
}

const post = (frame, route, body) => call(frame, route, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
});

async function waitConnected(frame, ssh, { answers = true, limit = 240000, name = NAME } = {}) {
    const started = Date.now();
    const asked = [];
    let last = null;
    while (Date.now() - started < limit) {
        const status = await call(frame, `/settings/remotes/${name}/status?kind=node`);
        last = status.body;
        if (last && last.state === "connected" && (last.node || last.files)) {
            return { seconds: (Date.now() - started) / 1000, asked, last };
        }
        if (last && last.state === "failed") {
            const deep = await call(frame, `/settings/remotes/${name}/status?kind=node&log=200`);
            throw new Error(`failed: ${last.error}\n${((deep.body && deep.body.log) || last.log || []).join("\n")}`);
        }
        if (last && last.prompt) {
            if (!answers) throw new Error(`asked again: ${last.prompt.text}`);
            const text = last.prompt.text;
            const answer = /authenticity/i.test(text) ? "yes" : /password/i.test(text) ? ssh.password
                : /passcode|option/i.test(text) ? ssh.code : null;
            if (answer === null) throw new Error(`an unexpected question: ${text}`);
            asked.push(text.split("\n").pop());
            await post(frame, `/settings/remotes/${name}/answer?kind=node`, { id: last.prompt.id, answer });
        }
        await sleep(400);
    }
    throw new Error(`not connected in ${limit / 1000} s: ${JSON.stringify(last)}`);
}

async function nodeAnswers(frame, { limit = 120000 } = {}) {
    const started = Date.now();
    let last = null;
    while (Date.now() - started < limit) {
        const health = await call(frame, "/remote_health");
        last = health.body;
        const entry = ((last && last.health) || {})[NAME];
        if (entry && entry.state === "healthy") {
            return { seconds: (Date.now() - started) / 1000, entry };
        }
        await sleep(1000);
    }
    const status = await call(frame, `/settings/remotes/${NAME}/status?kind=node&log=60`);
    const page = await frame.evaluate(() => {
        try { return localStorage.getItem("plexora.webRemote.rooms"); } catch (error) { return String(error); }
    }).catch(() => null);
    throw new Error(`the node did not answer: ${JSON.stringify(last).slice(0, 600)}\n`
                    + `status: ${JSON.stringify(status.body).slice(0, 3000)}\nrooms: ${page}`);
}

//: A file fetched whole through the page's fetch path: its length and SHA-256.
function fetchDigest(frame, body) {
    return frame.evaluate(async (asked) => {
        const response = await window.PlexoraBackend.fetch("/fetch_file", {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(asked) });
        const bytes = await response.arrayBuffer();
        if (!response.ok) return { status: response.status, error: new TextDecoder().decode(bytes).slice(0, 400) };
        const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
        return { status: response.status, length: bytes.byteLength,
                 sha256: Array.from(digest, (b) => b.toString(16).padStart(2, "0")).join("") };
    }, body);
}

//: One tile of a project's channel, through the page's fetch path.
function tile(frame, route) {
    return frame.evaluate(async (where) => {
        const response = await window.PlexoraBackend.fetch(where);
        const bytes = await response.arrayBuffer();
        return { status: response.status, type: response.headers.get("Content-Type"), length: bytes.byteLength,
                 error: response.ok ? null : new TextDecoder().decode(bytes).slice(0, 400) };
    }, route);
}

let sshChild = null;
let browser = null;
let server = null;
let relay = null;
try {
    const relayPort = await freePort();
    const sshPort = await freePort();
    relay = await startRelay(relayPort, sshPort);
    const ssh = await startSsh(sshPort, path.join(work, "ssh-home"));
    const site = path.join(work, "site");
    execFileSync(PYTHON, ["-m", "plexora.client.static_build", "--out", site, "--no-home",
                          "--relay", `ws://127.0.0.1:${relayPort}`], { cwd: REPO, stdio: "ignore" });
    server = await startServer(["--site", site]);
    const origin = `http://127.0.0.1:${server.port}`;
    browser = await launch({ headed: args.includes("--headed") });
    const page = await browser.context.newPage();
    const consoleErrors = [];
    page.on("console", (message) => {
        if (message.type() === "error" || /ssh|relay/i.test(message.text())) consoleErrors.push(message.text());
    });
    await page.goto(`${origin}/settings#remotes`);
    let frame = await appFrame(page);

    // Saved as the preset form saves one: the address, and the environment
    // Plexora runs from over there (this checkout, through ssh_server.py).
    const saved = await post(frame, "/settings/remotes", {
        name: NAME, target: `${ssh.user}@127.0.0.1:${sshPort}`,
        remote_command: path.dirname(path.dirname(PYTHON)),
    });
    if (saved.status !== 200) throw new Error(`could not save the server: ${JSON.stringify(saved.body)}`);

    const began = await post(frame, `/settings/remotes/${NAME}/connect?kind=node`);
    if (began.status !== 202) throw new Error(`connect answered ${began.status}: ${JSON.stringify(began.body)}`);
    const up = await waitConnected(frame, ssh);
    results.steps.connect = { ok: true, seconds: up.seconds, asked: up.asked };

    const places = await call(frame, "/data_places");
    const place = (places.body.places || []).find((item) => item.id === NAME) || {};
    const health = await nodeAnswers(frame);
    const listed = await post(frame, "/list_dir", { node: NAME, path: ssh.home });
    results.steps.node = { ok: place.node === NAME && listed.status === 200,
                           place: { state: place.state, node: place.node }, health: health.entry,
                           listed: listed.status, entries: Array.isArray(listed.body?.entries)
                               ? listed.body.entries.length : listed.body };
    await page.screenshot({ path: path.join(shots, "connected.png") });

    // A reloaded tab: the same node, through the room this browser kept.
    await page.reload();
    frame = await appFrame(page);
    const again = await waitConnected(frame, ssh, { answers: false, limit: 90000 });
    const healthAgain = await nodeAnswers(frame);
    results.steps.reload = { ok: true, seconds: again.seconds, health: healthAgain.entry };

    // The relay goes away and comes back, as a Cloudflare restart would.
    await stop(relay);
    await sleep(3000);
    relay = await startRelay(relayPort, sshPort);
    const back = await nodeAnswers(frame, { limit: 150000 });
    const stillConnected = await call(frame, `/settings/remotes/${NAME}/status?kind=node`);
    results.steps.restart = { ok: stillConnected.body.state === "connected" && !stillConnected.body.prompt,
                              seconds: back.seconds, state: stillConnected.body.state };
    await page.screenshot({ path: path.join(shots, "after-restart.png") });

    const ended = await post(frame, `/settings/remotes/${NAME}/disconnect?kind=node`);
    await sleep(4000);
    const running = execFileSync("ps", ["-axo", "command"]).toString()
        .split("\n").filter((line) => line.includes("--relay-file") && line.includes(ssh.home));
    results.steps.disconnect = { ok: ended.status === 200 && running.length === 0, running };

    // -- A files-only server: the page's own SSH connection, held, read
    // through its SFTP side. Nothing is started over there.
    execFileSync(PYTHON, ["-c", "import sys; from pathlib import Path; "
                          + "from tests.brightfield_fixtures import write_planar_fluorescence; "
                          + "write_planar_fluorescence(Path(sys.argv[1]))",
                          path.join(ssh.home, "panel.ome.tif")], { cwd: REPO, stdio: "ignore" });
    const local = readFileSync(path.join(ssh.home, "panel.ome.tif"));
    const localSha = createHash("sha256").update(local).digest("hex");
    const savedFiles = await post(frame, "/settings/remotes", {
        name: FILES, target: `${ssh.user}@127.0.0.1:${sshPort}`, use_srun: false, srun: "",
        install: false, bind_node: false, files: { via: "sftp" },
    });
    if (savedFiles.status !== 200) throw new Error(`could not save the files server: ${JSON.stringify(savedFiles.body)}`);
    const opened = await post(frame, `/settings/remotes/${FILES}/connect?kind=node`);
    if (opened.status !== 202) throw new Error(`files connect answered ${opened.status}: ${JSON.stringify(opened.body)}`);
    const filesUp = await waitConnected(frame, ssh, { name: FILES });
    const filesPlaces = await call(frame, "/data_places");
    const filesPlace = (filesPlaces.body.places || []).find((item) => item.id === FILES) || {};
    const base = filesPlace.files && filesPlace.files.base;
    const folder = await post(frame, "/list_dir", { node: base, path: "" });
    const entry = ((folder.body && folder.body.entries) || []).find((item) => item.name === "panel.ome.tif");
    const whole = await fetchDigest(frame, { node: base, path: "panel.ome.tif" });
    let drawn = null;
    if (entry) {
        const inspected = await post(frame, "/import/inspect", { paths: [entry.path] });
        const sample = await post(frame, "/import/sample", { paths: [entry.path], answers: {} });
        const project = sample.body && sample.body.name;
        const channels = project ? await call(frame, `/get_channel_names?datasource=${encodeURIComponent(project)}`) : null;
        // A channel's tiles are at `<name>_<index>` (datasource.py's `src`).
        const names = channels && Array.isArray(channels.body) ? channels.body : [];
        const key = names.length > 1 ? `${names[1]}_1` : `${names[0] || "channel"}_0`;
        const shown = project ? await tile(frame, `/generated/data/${encodeURIComponent(project)}/${encodeURIComponent(key)}/0/0_0.png`) : null;
        drawn = { inspected: inspected.status, imported: sample.status, project,
                  error: project ? null : JSON.stringify(sample.body).slice(0, 600),
                  channels: channels && channels.body, tile: shown };
    }
    results.steps.files = {
        ok: Boolean(base) && folder.status === 200 && Boolean(entry) && whole.sha256 === localSha
            && Boolean(drawn && drawn.tile && drawn.tile.status === 200 && drawn.tile.length > 0),
        seconds: filesUp.seconds, asked: filesUp.asked, base, home: filesPlace.files && filesPlace.files.home,
        listed: folder.status, fetched: { ...whole, expected: local.length }, drawn,
    };
    await page.screenshot({ path: path.join(shots, "files-connected.png") });

    // The relay restarts under a held SSH connection: the page signs in again.
    await stop(relay);
    await sleep(3000);
    relay = await startRelay(relayPort, sshPort);
    const filesBack = await waitConnected(frame, ssh, { name: FILES, limit: 240000 });
    const afterRestart = await fetchDigest(frame, { node: base, path: "panel.ome.tif" });
    results.steps["files-restart"] = { ok: afterRestart.sha256 === localSha, seconds: filesBack.seconds,
                                       asked: filesBack.asked, fetched: afterRestart };

    // A reloaded tab: no SSH connection survives it; the page signs in again.
    await page.reload();
    frame = await appFrame(page);
    const filesReloaded = await waitConnected(frame, ssh, { name: FILES, limit: 240000 });
    const afterReload = await fetchDigest(frame, { node: base, path: "panel.ome.tif" });
    results.steps["files-reload"] = { ok: afterReload.sha256 === localSha, seconds: filesReloaded.seconds,
                                      asked: filesReloaded.asked, fetched: afterReload };
    const filesEnded = await post(frame, `/settings/remotes/${FILES}/disconnect?kind=node`);
    const afterEnd = await fetchDigest(frame, { node: base, path: "panel.ome.tif" });
    results.steps["files-disconnect"] = { ok: filesEnded.status === 200 && afterEnd.status === 503,
                                          fetched: afterEnd };
    results.consoleErrors = consoleErrors.slice(0, 20);
    results.ok = Object.values(results.steps).every((step) => step.ok);
} catch (error) {
    results.error = String(error && error.stack || error);
    results.relayLog = relay ? relay.log.filter((line) => !/explorer|Useful routes|curl|OpenAPI|Binding|env\./.test(line)).slice(-30) : [];
    results.sshLog = sshChild ? sshChild.log.filter((line) => /auth|error|disconnect|clos|lost|exception|Traceback|kbdint/i.test(line)).slice(-40) : [];
} finally {
    if (browser) await browser.close().catch(() => null);
    if (server) server.stop();
    for (const child of children) await stop(child);
    writeFileSync(path.join(work, "result.json"), JSON.stringify(results, null, 1));
    console.log(JSON.stringify(results, null, 1));
}
