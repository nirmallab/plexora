// The app a data node serves, behind a portal (plan Phase 4, OOD): `plexora
// node serve --app` serves the website's static build at /app/, and Open
// OnDemand reaches it at /rnode/<host>/<port>/app/, stripping the prefix
// before the node sees the request. This stands in for the portal -- a proxy
// that does exactly that strip -- and opens the app through it:
//   boot     the app starts under the prefix (its loader, the service worker,
//            Python), BASE is the prefix, and the first page is the app's
//   links    a page Python rendered links under the prefix (Settings), and
//            following one stays under it
//   pair     the node's own link (page and endpoint both under the portal)
//            pairs it with this page, same-origin: Settings > Data nodes
//            lists it reachable
// The Python bundle comes from the dev server (tools/browser_build/serve.py)
// at the portal's /py/, as the assets bucket would serve it.
//
//   node tests/browser/ood.mjs [--shots DIR]
// Prints JSON with `ok`.
import { spawn } from "node:child_process";
import http from "node:http";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { appFrame, launch, seedBrowserStore, startServer, PYTHON, REPO } from "./lib.mjs";

const args = process.argv.slice(2);
const option = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const shots = option("--shots", mkdtempSync(path.join(tmpdir(), "pxood-shots-")));
const image = option("--image", "C:/plexora-bench/browser_node/node_fixtures/pyr8_lzw.ome.tif");
mkdirSync(shots, { recursive: true });

function freePort() {
    return new Promise((resolve) => {
        const probe = net.createServer();
        probe.listen(0, "127.0.0.1", () => {
            const { port } = probe.address();
            probe.close(() => resolve(port));
        });
    });
}

//: The portal: `/rnode/127.0.0.1/<node>/...` to the node with the prefix
//: stripped (OOD's rnode), `/py/...` to the dev server (the bucket).
function startPortal(port, nodePort, devPort) {
    const prefix = `/rnode/127.0.0.1/${nodePort}`;
    const server = http.createServer((request, response) => {
        let target = null;
        if (request.url.startsWith(prefix + "/") || request.url === prefix) {
            target = { port: nodePort, path: request.url.slice(prefix.length) || "/" };
        } else if (request.url.startsWith("/py/")) {
            target = { port: devPort, path: request.url };
        }
        if (!target) {
            response.writeHead(404, { "Content-Type": "text/plain" });
            response.end("not behind this portal\n");
            return;
        }
        const upstream = http.request({ host: "127.0.0.1", port: target.port, path: target.path,
                                        method: request.method,
                                        headers: { ...request.headers, host: `127.0.0.1:${target.port}` } },
                                      (answer) => {
            response.writeHead(answer.statusCode, answer.headers);
            answer.pipe(response);
        });
        upstream.on("error", (error) => { response.writeHead(502); response.end(String(error)); });
        request.pipe(upstream);
    });
    return new Promise((resolve) => server.listen(port, "127.0.0.1", () => resolve({ prefix, stop: () => server.close() })));
}

async function startNode(port, portalOrigin, prefix) {
    const home = mkdtempSync(path.join(tmpdir(), "pxood-node-"));
    const child = spawn(PYTHON, ["-m", "plexora", "node", "serve", "--port", String(port), "--dynamic", "--app",
                                 "--serve", `image:fixture=${image}`,
                                 "--pair", `${portalOrigin}${prefix}/app`,
                                 "--browser-endpoint", `${portalOrigin}${prefix}`],
                        { cwd: REPO, env: { ...process.env, PLEXORA_DATA_PATH: home, PLEXORA_TELEMETRY: "off" },
                          stdio: ["ignore", "pipe", "pipe"] });
    let text = "";
    const link = await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("the node printed no pairing link")), 90000);
        child.stdout.on("data", (chunk) => {
            text += chunk;
            const found = /(\S+\/connect#v=1&e=\S+&t=\S+)/.exec(text);
            if (found) { clearTimeout(timer); resolve(found[1]); }
        });
        child.stderr.on("data", () => {});
        child.on("exit", (code) => reject(new Error(`the node exited ${code}`)));
    });
    return { link, stop: () => { child.kill(); try { rmSync(home, { recursive: true, force: true }); } catch { /* held */ } } };
}

const dev = await startServer([]);
const nodePort = await freePort();
const portalPort = await freePort();
const portalOrigin = `http://127.0.0.1:${portalPort}`;
const portal = await startPortal(portalPort, nodePort, dev.port);
const node = await startNode(nodePort, portalOrigin, portal.prefix);
const base = `${portal.prefix}/app`;
const browser = await launch({});
const out = { shots, base };
const failed = [];
try {
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1280, height: 860 });
    page.on("response", (response) => {
        const url = new URL(response.url());
        if (url.origin === portalOrigin && response.status() >= 400) failed.push(`${response.status()} ${url.pathname}`);
    });
    await seedBrowserStore(browser.context, `${portalOrigin}${base}`);
    await page.goto(`${portalOrigin}${base}/`);
    let frame = await appFrame(page, { timeout: 180000 });
    out.boot = await frame.evaluate(() => ({
        base: window.PLEXORA_SITE_BASE, baseUrl: window.flaskVariables?.base_url,
        at: location.pathname, home: Boolean(document.getElementById("quick_view_go")),
        isolated: window.crossOriginIsolated,
    }));
    out.boot.controller = await page.evaluate(() => navigator.serviceWorker.controller?.scriptURL || null);
    await page.screenshot({ path: path.join(shots, "ood-home.png") });

    // -- links ---------------------------------------------------------------
    out.links = await frame.evaluate(() => ({
        settings: document.getElementById("nav_settings")?.getAttribute("href"),
        scripts: [...document.querySelectorAll("script[src]")].map((node) => node.getAttribute("src"))
            .filter((src) => src.startsWith("/")).slice(0, 3),
    }));
    await frame.evaluate(() => document.getElementById("nav_settings").click());
    await page.waitForTimeout(1500);
    frame = await appFrame(page);
    await frame.waitForSelector("#settings_panel_nodes", { state: "attached", timeout: 60000 });
    out.links.after = await frame.evaluate(() => location.pathname);
    out.links.address = await page.evaluate(() => location.pathname);

    // -- pair -----------------------------------------------------------------
    out.pair = { link: node.link.replace(/t=[^&]+/, "t=…") };
    await page.goto(node.link);
    out.pair.address = await page.evaluate(() => location.href);
    frame = await appFrame(page, { timeout: 180000 });
    out.pair.toast = await frame.waitForFunction(() => {
        const toast = document.querySelector(".plx-toast-title");
        return toast && /Paired with|Could not pair|not complete/.test(toast.textContent) ? toast.textContent : null;
    }, null, { timeout: 90000, polling: 250 }).then((handle) => handle.jsonValue(), () => null);
    await frame.evaluate((to) => { window.location.href = to; }, `${base}/settings#nodes`);
    await page.waitForTimeout(1500);
    frame = await appFrame(page);
    await frame.evaluate(() => document.querySelector('[data-section="nodes"]')?.click());
    await frame.waitForSelector(".settings-node-card", { timeout: 60000 }).catch(() => null);
    out.pair.nodes = await frame.evaluate(() => [...document.querySelectorAll(".settings-node-card")].map((card) => ({
        name: card.querySelector(".settings-field-label")?.textContent,
        state: card.querySelector(".settings-node-state")?.textContent,
        address: card.querySelector(".settings-path")?.textContent,
    })));
    await page.screenshot({ path: path.join(shots, "ood-settings-nodes.png") });
} finally {
    await browser.close();
    node.stop();
    portal.stop();
    dev.stop();
}
out.failed = failed.slice(0, 20);
const paired = (out.pair.nodes || []).find((entry) => entry.address === `${portalOrigin}${portal.prefix}`);
out.ok = Boolean(out.boot.base === base && out.boot.baseUrl === base && out.boot.home && out.boot.isolated
                 && out.boot.at.startsWith(base) && (out.boot.controller || "").includes(`${base}/client/`)
                 && out.links.settings === `${base}/settings` && out.links.scripts.every((src) => src.startsWith(base))
                 && out.links.after === `${base}/settings` && out.links.address === `${base}/settings`
                 && !/[#&]t=/.test(out.pair.address) && /^Paired with/.test(out.pair.toast || "")
                 && paired && paired.state === "Reachable");
console.log(JSON.stringify(out, null, 1));
process.exit(out.ok ? 0 : 1);
