// Remote work on the website: saved servers are connected from the page
// itself (docs/internal/WEB_REMOTE_RELAY.md; tests/browser/remote.mjs drives a
// whole connection), and a node started on the server by hand pairs in one
// click.
//
// Checks, in one Chrome against the dev server (tools/browser_build/serve.py)
// and a real data node (`plexora node serve --dynamic --pair <site>`):
//   quit        File has no Quit (no process to stop)
//   remotes     Settings > Remote servers has the saved servers and the
//               presets, and says what connecting from a web page does (no
//               connection is asked for by drawing it)
//   dialog      the Connect dialog's presets are the presets, not a notice;
//               Google Cloud is its form (plan L9), which on a build with no
//               Google OAuth client says sign-in is not set up
//   toast       any other caller of a desktop-bound route gets the notice in the corner
//   about       Help > About says "website" without asking /desktop/info
//   pair        the node's printed link pairs it: the fragment is cleared
//               from the address bar, the node is listed reachable in
//               Settings > Data nodes, and Python reached it (urllib3 in the
//               worker, the `nodes` stage)
//   image       a project whose image is on the paired node opens, its tiles
//               read through the node
// Screenshots go to --shots (default: a temp folder), one per surface.
//
//   node tests/browser/connect.mjs [--shots DIR] [--image PATH]
// Prints JSON with `ok`.
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { appFrame, launch, startServer, PYTHON, REPO } from "./lib.mjs";

const args = process.argv.slice(2);
const option = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const shots = option("--shots", mkdtempSync(path.join(tmpdir(), "pxconnect-shots-")));
const image = option("--image", "C:/plexora-bench/browser_node/node_fixtures/pyr8_lzw.ome.tif");
mkdirSync(shots, { recursive: true });

//: The cells of the mixed project, written here and picked in the page: a
//: table that lives in the browser beside an image on the node.
const scratch = mkdtempSync(path.join(tmpdir(), "pxconnect-table-"));
const table = path.join(scratch, "cells.csv");
{
    const rows = ["CellID,X_centroid,Y_centroid,Area,Channel_0,Channel_1"];
    for (let i = 1; i <= 400; i += 1) {
        rows.push(`${i},${(i * 37) % 2000 + 20},${(i * 53) % 1400 + 20},${50 + (i % 30)},${(i * 7) % 255},${(i * 13) % 255}`);
    }
    writeFileSync(table, rows.join("\n") + "\n");
}

function freePort() {
    return new Promise((resolve) => {
        const probe = net.createServer();
        probe.listen(0, "127.0.0.1", () => {
            const { port } = probe.address();
            probe.close(() => resolve(port));
        });
    });
}

//: A real node, paired with `site`: its printed link (the token is in it and
//: goes nowhere else -- never into the record).
async function startNode(site) {
    const port = await freePort();
    const home = mkdtempSync(path.join(tmpdir(), "pxconnect-node-"));
    const child = spawn(PYTHON, ["-m", "plexora", "node", "serve", "--port", String(port), "--dynamic",
                                 "--serve", `image:fixture=${image}`, "--pair", site],
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
    return { port, link, endpoint: `http://127.0.0.1:${port}`,
             stop: () => { child.kill(); try { rmSync(home, { recursive: true, force: true }); } catch { /* held */ } } };
}

//: The first visit's question, answered "Keep in browser" when it comes.
async function keepInBrowser(frame) {
    await frame.waitForFunction(() => {
        const keep = [...document.querySelectorAll(".plx-confirm .plx-button")]
            .find((button) => button.textContent.trim() === "Keep in browser");
        if (keep) keep.click();
        return Boolean(keep);
    }, null, { timeout: 15000, polling: 200 }).catch(() => null);
}

//: The app's page at `route`, through the app's own navigation.
async function visit(page, route) {
    const frame = await appFrame(page);
    await frame.evaluate((to) => { window.location.href = to; }, route);
    await page.waitForTimeout(500);
    return appFrame(page);
}

const server = await startServer(["--site", `${REPO}/plexora/client/dist/static`]);
const site = `http://127.0.0.1:${server.port}`;
const node = await startNode(site);
const browser = await launch({});
const out = { shots };
const requests = [];
//: Where the run is, on stderr (the record is stdout).
const step = (name) => console.error(`${new Date().toISOString().slice(11, 19)} ${name}`);
try {
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1280, height: 860 });
    page.on("request", (request) => {
        const url = new URL(request.url());
        if (url.origin === site) requests.push(`${request.method()} ${url.pathname}`);
    });
    await page.goto(`${site}/`);
    let frame = await appFrame(page);
    await keepInBrowser(frame);

    step("quit");
    // -- quit ---------------------------------------------------------------
    out.quit = await frame.evaluate(() => ({ quit: Boolean(document.getElementById("nav_quit")),
                                             website: document.documentElement.classList.contains("is-website") }));

    step("remotes");
    // -- remotes ------------------------------------------------------------
    frame = await visit(page, "/settings#remotes");
    await frame.waitForSelector("#settings_remote_catalogue .connect-recipe", { timeout: 60000 });
    await frame.evaluate(() => document.querySelector('[data-section="remotes"]')?.click());
    await page.waitForTimeout(300);
    out.remotes = await frame.evaluate(() => ({
        lead: document.querySelector("#settings_panel_remotes .settings-panel-lead")?.textContent || "",
        handoff: Boolean(document.querySelector("#settings_panel_remotes .plx-handoff")),
        serverList: Boolean(document.getElementById("settings_remotes_list")),
        presets: document.querySelectorAll("#settings_remote_catalogue .connect-recipe").length,
        pairButton: Boolean(document.getElementById("settings_node_pair")),
    }));
    out.remotes.asked = requests.filter((line) => /\/settings\/remotes\/[^/]+\/(connect|status)/.test(line)).length;
    await (await frame.$("#settings_panel_remotes")).screenshot({ path: path.join(shots, "settings-remotes.png") });

    step("dialog");
    // -- dialog -------------------------------------------------------------
    const dialog = async (options, shot) => {
        await frame.evaluate((how) => { window.__opened = window.PlexoraConnectionModal.open(how); }, options);
        await frame.waitForSelector("dialog.connect-modal[open] .connect-recipe", { timeout: 15000 });
        const seen = await frame.evaluate(() => {
            const box = document.querySelector("dialog.connect-modal[open]");
            return { title: box.querySelector(".connect-modal-title").textContent,
                     handoff: box.classList.contains("is-handoff"),
                     presets: box.querySelectorAll(".connect-recipe").length };
        });
        await page.screenshot({ path: path.join(shots, shot) });
        await frame.evaluate(() => document.querySelector("dialog.connect-modal[open]")?.close());
        seen.result = await frame.evaluate(() => window.__opened);
        return seen;
    };
    out.dialog = await dialog({ view: "recipes" }, "connect-dialog-ssh.png");
    out.gcloud = await frame.evaluate(async () => {
        window.PlexoraConnectionModal.open({ view: "recipe", recipe: "gcloud" });
        const until = Date.now() + 30000;
        let box = null;
        let said = "";
        while (Date.now() < until) {
            box = document.querySelector("dialog.connect-modal[open]");
            said = box?.querySelector(".connect-gcloud-identity")?.textContent || "";
            if (/not set up/.test(said)) break;
            await new Promise((resolve) => setTimeout(resolve, 200));
        }
        const seen = { handoff: Boolean(box && box.classList.contains("is-handoff")),
                       title: box?.querySelector(".connect-modal-title")?.textContent || "",
                       identity: said };
        box?.close();
        return seen;
    });
    await page.screenshot({ path: path.join(shots, "connect-dialog-gcloud.png") });

    step("toast");
    // -- toast --------------------------------------------------------------
    out.toast = await frame.evaluate(async () => {
        // A route whose notice is not the one already drawn inline on this
        // page, so the toast
        // says it.
        const answer = await fetch("/settings/data");
        const body = await answer.json();
        const seen = await new Promise((resolve) => {
            const until = Date.now() + 5000;
            const look = () => {
                const toast = document.querySelector(".plx-toast");
                if (toast || Date.now() > until) {
                    resolve(toast ? { title: toast.querySelector(".plx-toast-title").textContent,
                                      actions: [...toast.querySelectorAll(".plx-toast-action")].map((b) => b.textContent) }
                                  : null);
                } else setTimeout(look, 100);
            };
            look();
        });
        return { status: answer.status, error: body.error, handoff: body.handoff, toast: seen };
    });
    await page.screenshot({ path: path.join(shots, "handoff-toast.png") });

    step("about");
    // -- about --------------------------------------------------------------
    const before = requests.length;
    out.about = await frame.evaluate(async () => {
        window.PlexoraHelpMenu.about();
        await new Promise((resolve) => setTimeout(resolve, 600));
        const table = document.querySelector(".plx-about-table");
        const rows = table ? Object.fromEntries([...table.querySelectorAll("tr")]
            .map((row) => [row.querySelector("th").textContent, row.querySelector("td").textContent])) : {};
        document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
        return { runningAs: rows["Running as"], version: rows.Plexora };
    });
    out.about.askedDesktopInfo = requests.slice(before).some((line) => line.endsWith("/desktop/info"));

    step("pair dialog");
    // -- pair, in the app: Settings > Data nodes > Pair a Node, the printed
    // link pasted ---------------------------------------------------------------
    frame = await visit(page, "/settings#nodes");
    await frame.evaluate(() => document.querySelector('[data-section="nodes"]')?.click());
    await frame.click("#settings_node_pair");
    await frame.waitForSelector(".plx-confirm .plx-handoff-link", { timeout: 15000 });
    out.dialogPair = await frame.evaluate(() => {
        const box = document.querySelector(".plx-confirm");
        const buttons = [...box.querySelectorAll("button")].filter((button) => button.offsetParent !== null);
        return { title: box.querySelector("h2, h3, .plx-confirm-title")?.textContent || "",
                 command: box.querySelector(".plx-handoff-command code")?.textContent || "",
                 buttons: buttons.map((button) => button.textContent.trim()).filter(Boolean) };
    });
    await page.screenshot({ path: path.join(shots, "pair-node-dialog.png") });
    await frame.fill(".plx-confirm .plx-handoff-link", node.link);
    await frame.evaluate(() => [...document.querySelectorAll(".plx-confirm button")]
        .find((button) => button.textContent.trim() === "Pair").click());
    out.dialogPair.toast = await frame.waitForFunction(() => {
        const toast = document.querySelector(".plx-toast-title");
        return toast && /Paired with|Could not pair|not a pairing/.test(toast.textContent) ? toast.textContent : null;
    }, null, { timeout: 90000, polling: 250 }).then((handle) => handle.jsonValue(), () => null);
    // The list on screen takes the new node without a reload.
    out.dialogPair.listed = await frame.waitForFunction(() => document.querySelector(".settings-node-card .settings-node-state")
        ?.textContent || null, null, { timeout: 30000, polling: 250 }).then((handle) => handle.jsonValue(), () => null);

    step("pair link");
    // -- pair, by the link the node printed -------------------------------------
    await page.goto(node.link);
    out.pair = { addressAfter: null };
    out.pair.addressAfter = await page.evaluate(() => location.href);
    frame = await appFrame(page);
    await keepInBrowser(frame);
    out.pair.toast = await frame.waitForFunction(() => {
        const toast = document.querySelector(".plx-toast-title");
        return toast && /Paired with|Could not pair|not complete/.test(toast.textContent) ? toast.textContent : null;
    }, null, { timeout: 90000, polling: 250 }).then((handle) => handle.jsonValue(), () => null);
    out.pair.addressNow = await page.evaluate(() => location.href);
    out.pair.tokenInAddress = [out.pair.addressAfter, out.pair.addressNow].some((where) => /[#&]t=/.test(where || ""));
    await page.screenshot({ path: path.join(shots, "paired-toast.png") });
    frame = await visit(page, "/settings#nodes");
    await frame.evaluate(() => document.querySelector('[data-section="nodes"]')?.click());
    await frame.waitForSelector(".settings-node-card", { timeout: 60000 });
    out.pair.nodes = await frame.evaluate(() => [...document.querySelectorAll(".settings-node-card")].map((card) => ({
        name: card.querySelector(".settings-field-label")?.textContent,
        state: card.querySelector(".settings-node-state")?.textContent,
        address: card.querySelector(".settings-path")?.textContent,
    })));
    await (await frame.$("#settings_panel_nodes")).screenshot({ path: path.join(shots, "settings-nodes-paired.png") });

    step("image");
    // -- image: a mixed project -- its image on the paired node, its cells in
    // this browser (a CSV picked here) -------------------------------------------
    const nodeName = (out.pair.nodes || []).find((entry) => entry.address === node.endpoint)?.name;
    out.image = await frame.evaluate(async ({ name, file }) => {
        const answer = await fetch("/import/sample", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ paths: [file], node: name, name: "mixed_node" }) });
        const body = await answer.json().catch(() => ({}));
        return { status: answer.status, error: body.error || null, name: body.name || null };
    }, { name: nodeName, file: image });
    // A pick needs a click (the browser's rule for a file chooser): a button
    // of the test's own, whose click asks the page's picker for one file.
    await frame.evaluate(() => {
        window.parent.showOpenFilePicker = undefined;
        window.showOpenFilePicker = undefined;
        const button = document.createElement("button");
        button.id = "test_pick_table";
        button.textContent = "pick";
        button.style.cssText = "position:fixed;left:0;top:0;z-index:99999";
        button.addEventListener("click", () => {
            window.__picked = window.PlexoraPicks.pick({ mode: "file", multiple: false });
        });
        document.body.appendChild(button);
    });
    const chooser = page.waitForEvent("filechooser", { timeout: 30000 });
    await frame.click("#test_pick_table");
    await (await chooser).setFiles(table);
    const picked = await frame.evaluate(() => window.__picked);
    out.image.picked = Array.isArray(picked) ? picked[0] : picked;
    out.image.table = await frame.evaluate(async (paths) => {
        const answer = await fetch("/import/layers", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ sample: "mixed_node", paths }) });
        const body = await answer.json().catch(() => ({}));
        return { status: answer.status, error: body.error || null };
    }, [out.image.picked]);
    const fromNode = [];
    browser.context.on("response", (response) => {
        if (response.url().startsWith(node.endpoint)) fromNode.push(response.status());
    });
    frame = await visit(page, "/mixed_node");
    await page.waitForTimeout(8000);
    out.image.viewer = await frame.evaluate(async () => {
        const ask = async (route) => (await fetch(route)).json().catch(() => ({}));
        const resources = await ask("/project/mixed_node/resources");
        const description = await ask("/get_database_description?datasource=mixed_node");
        const routing = await ask("/resource_routing?datasource=mixed_node");
        // What the record keeps: where each part lives -- never a token (a
        // direct route's query and health address carry the node's).
        const route = (routing.routes || {}).image || {};
        return { canvas: Boolean(document.querySelector("#container canvas, .openseadragon-canvas canvas")),
                 parts: (resources.resources || []).filter((entry) => entry.present).map((entry) => ({
                     kind: entry.kind, provider: entry.provider, node: entry.node,
                     inBrowser: String(entry.path || "").startsWith("/picked/") })),
                 columns: Object.keys(description),
                 direct: { endpoint: route.endpoint || null, node: route.node || null } };
    });
    out.image.python = await page.evaluate(() => (window.PlexoraPython.recent() || [])
        .filter((row) => /generate|tile|channel/.test(row.path)).slice(-40)
        .map((row) => `${row.status} ${row.path}`));
    out.image.direct = { responses: fromNode.length, ok: fromNode.filter((status) => status === 200).length };
    await page.screenshot({ path: path.join(shots, "mixed-node-image.png") });

    step("open desktop");
    // -- Open Desktop App with no project, last and in a tab of its own: the
    // app is asked to come forward (`plexora://open`), and with none to answer
    // (a headless browser) the download is offered. The unanswered external
    // link leaves headless Chrome's tab deaf to further real clicks.
    const last = await browser.context.newPage();
    await last.goto(`${site}/settings#remotes`);
    const lastFrame = await appFrame(last);
    // The data folder's notice (a desktop app setting) still offers the app.
    await lastFrame.evaluate(() => fetch("/settings/data").catch(() => null));
    await lastFrame.waitForSelector('.plx-toast [data-handoff-action="open_desktop"]', { timeout: 60000 });
    await lastFrame.click('.plx-toast [data-handoff-action="open_desktop"]');
    out.remotes.openDesktop = await lastFrame.waitForFunction(() => {
        const toast = document.querySelector(".plx-toast-title");
        return toast && /desktop app/i.test(toast.textContent) ? toast.textContent : null;
    }, null, { timeout: 10000, polling: 200 }).then((handle) => handle.jsonValue(), () => null);
} finally {
    await browser.close();
    node.stop();
    server.stop();
}
const named = (out.pair.nodes || []).find((entry) => entry.address === node.endpoint);
out.ok = Boolean(
    out.quit && out.quit.website && !out.quit.quit
    && !out.remotes.handoff && out.remotes.serverList && out.remotes.presets > 0
    && /Nothing is installed on this computer/.test(out.remotes.lead)
    && out.remotes.pairButton && out.remotes.asked === 0
    && !out.dialog.handoff && out.dialog.presets > 0
    && !out.gcloud.handoff && out.gcloud.title === "Connect to Google Cloud"
    && /Google sign-in is not set up on this site/.test(out.gcloud.identity)
    && out.toast.status === 501 && out.toast.toast && out.toast.toast.title === "A desktop app setting"
    && out.about.runningAs === "website" && !out.about.askedDesktopInfo
    && out.remotes.openDesktop === "Get the desktop app"
    && out.dialogPair.command === `plexora node serve --dynamic --pair ${site}`
    && /^Paired with/.test(out.dialogPair.toast || "") && out.dialogPair.listed === "Reachable"
    && /^Paired with/.test(out.pair.toast || "") && !out.pair.tokenInAddress
    && named && named.state === "Reachable"
    && out.image.status === 200 && out.image.table.status === 200 && out.image.viewer.canvas
    && out.image.viewer.parts.some((part) => part.kind === "image" && part.provider === "node" && part.node === named.name)
    && out.image.viewer.parts.some((part) => part.kind === "table" && part.inBrowser)
    && out.image.viewer.columns.includes("Channel_1")
    && out.image.viewer.direct.endpoint === node.endpoint && out.image.direct.ok > 0);
console.log(JSON.stringify(out, null, 1));
process.exit(out.ok ? 0 : 1);
