// Plexora AI's chat on the website, end to end in Chrome, against a stand-in
// platform and AI gateway (tests/browser/mock_platform.py: a throwaway
// signing key, the gateway's streamed wire, a scripted model):
//
//   1. The browser is licensed through Settings > Account (the device flow).
//   2. A project with a cell table is made in the website's own Python from
//      the test suite's fixture module, and opened.
//   3. The sidebar's AI button > "Chat with Plexora AI"; a message is sent.
//      The scripted model calls `set_gate` (through `call_tool`), then answers
//      in eight pieces a quarter of a second apart.
//   4. Checked: the panel's event stream was refused (406) and it polled; the
//      gateway was called from the worker with the page's token, as a chat
//      turn; the tool chip shows `set_gate` ok with Undo; the gate is set in
//      Python; the answer was drawn as it streamed (several lengths of the
//      line before the whole) and polls were answered by the page while the
//      worker ran the turn; Undo puts the gate back; no page error.
//
//   PLEXORA_BENCH_HOME=<bundle home> node tests/browser/ai_chat.mjs [--headed] [--shots DIR]
//
// Prints a JSON record of the checks: states and counts only, never a
// certificate, code, key or token.
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { appFrame, launch, startServer } from "./lib.mjs";
import { buildSite, connectLicence, py, shipFixtures, startMock } from "./ai_lib.mjs";

const args = process.argv.slice(2);
const headed = args.includes("--headed");
const shots = args.includes("--shots") ? args[args.indexOf("--shots") + 1] : null;
const PROJECT = "chatsynth";
const ANSWER = "The CD3 gate is set at the table's median, so about half of the cells read positive; "
    + "the sidebar shows it now, and Undo puts the old gate back.";

const out = { checks: [], errors: [] };
const check = (name, ok, detail = {}) => {
    out.checks.push({ name, ok: Boolean(ok), ...detail });
    console.error(`${ok ? "ok  " : "FAIL"} ${name}`);
};

const temp = mkdtempSync(path.join(tmpdir(), "pxai-chat-"));
const site = path.join(temp, "site");
const mock = await startMock(path.join(temp, "mock-platform"));
buildSite(site, path.join(temp, "data"), mock);
const server = await startServer(["--site", site]);
const browser = await launch({ headed });
const origin = `http://127.0.0.1:${server.port}`;

//: The fixture: make_gating_project's files, registered as a user's import
//: would register them (create_project), in the browser's project store.
const MAKE = `
import json
from tests.autogate_fixtures import make_gating_project
from plexora import datasets
import polars as pl
made = make_gating_project("/data/_chatfx", "src", grid=8, size=256)
folder = "/data/_chatfx/_src_files"
name = datasets.create_project(made["image_path"], name="${PROJECT}", segmentation=folder + "/mask.tif",
                               data=folder + "/cells.csv", cell_id="CellID", x="X_centroid", y="Y_centroid",
                               markers=["DNA", "CD3", "CD8", "CD20"], metadata=["Area"], exist_ok=True)
cd3 = pl.read_csv(folder + "/cells.csv")["CD3"]
json.dumps({"name": name, "cells": len(cd3), "median": round(float(cd3.median()), 3)})
`;

const GATE = `
import json
from plexora.agent import AgentSession, invoke
answer = invoke(AgentSession(), "get_gate", {"project": "${PROJECT}", "marker": "CD3"})
gate = (answer.get("result") or {}).get("gate") or {}
json.dumps({"ok": answer["ok"], "low": gate.get("low"), "error": answer.get("error")})
`;

try {
    const page = await browser.context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
    page.on("pageerror", (error) => out.errors.push(`pageerror: ${String(error).slice(0, 300)}`));
    page.on("crash", () => out.errors.push("the page crashed"));
    page.on("console", (message) => {
        if (message.type() === "error" || /\[python\]/.test(message.text())) {
            console.error(`[console ${message.type()}] ${message.text().slice(0, 400)}`);
        }
    });
    const events = [];
    page.on("response", async (response) => {
        const url = response.url();
        if (!/\/ai\/v1\/conversations\/[^/]+\/events/.test(url)) return;
        const record = { status: response.status(), at: Date.now(), stream: false, by: "python", events: 0 };
        try {
            const body = await response.json();
            record.by = body.answered_by || "python";
            record.events = (body.events || []).length;
        } catch (error) { record.stream = true; }
        events.push(record);
    });

    // 1. Licensed.
    await connectLicence(page, origin);
    console.error("licensed");

    // 2. The project, made in the worker and kept in the browser's store.
    await shipFixtures(page);
    const made = await py(page, MAKE);
    console.error(`made ${JSON.stringify(made)}`);
    await page.evaluate(() => window.PlexoraPython.flush());
    check("a project with a cell table is made in the website's Python", made.name === PROJECT && made.cells === 64,
          { cells: made.cells });
    await page.goto(`${origin}/${PROJECT}?pydebug`);
    const frame = await appFrame(page);
    await frame.waitForSelector("#openseadragon_wrapper", { timeout: 120000 });
    const before = await py(page, GATE);

    // 3. The chat.
    const low = made.median;
    await mock.brain([
        { text: "Setting the CD3 gate.", tool_uses: [{ name: "call_tool", input: {
            name: "set_gate", arguments: { project: PROJECT, marker: "CD3", low } } }] },
        { text: ANSWER, chunks: 8, delay_ms: 250 },
    ]);
    await frame.waitForSelector("#plexora_ai_button:not([hidden])", { timeout: 60000 });
    await frame.click("#plexora_ai_button");
    await frame.click('[data-action="ai-chat"]', { timeout: 30000 });
    await frame.waitForSelector(".plx-chat-panel:not([hidden]) .plx-chat-input", { timeout: 60000 });
    // What the answer's line said, each time it changed.
    await frame.evaluate(() => {
        window.__chatSeen = [];
        const log = document.querySelector(".plx-chat-log");
        new MutationObserver(() => {
            const lines = [...log.querySelectorAll(".plx-chat-assistant")];
            const last = lines[lines.length - 1];
            if (!last) return;
            const text = last.textContent;
            const seen = window.__chatSeen;
            if (!seen.length || seen[seen.length - 1].text !== text) seen.push({ text, at: performance.now() });
        }).observe(log, { childList: true, subtree: true, characterData: true });
    });
    const sentAt = Date.now();
    await frame.fill(".plx-chat-input", "Gate CD3 at the median, please.");
    await frame.press(".plx-chat-input", "Enter");
    await frame.waitForFunction((answer) => {
        const lines = [...document.querySelectorAll(".plx-chat-assistant")];
        return lines.some((line) => line.textContent === answer)
            && !document.querySelector(".plx-chat-panel")?.classList.contains("is-busy");
    }, ANSWER, { timeout: 120000, polling: 100 });
    const turnMs = Date.now() - sentAt;
    if (shots) {
        await page.waitForTimeout(400);
        await page.screenshot({ path: path.join(shots, "ai-chat.png") });
    }

    // 4. The checks.
    const asked = (await mock.requests()).filter((r) => r.path === "/ai/messages");
    check("the gateway was called from the worker with the page's token, as chat turns",
          asked.length === 2 && asked.every((r) => r.authorized && r.task === "plexora.chat.turn"
                                            && /^plexora\//.test(r.client || "") && r.idempotency)
          && asked[1].tool_results.length === 1 && asked[1].tool_results[0].ok,
          { calls: asked.length, origin: asked[0] && asked[0].origin,
            toolResult: asked[1] && asked[1].tool_results[0] && asked[1].tool_results[0].text.slice(0, 120) });
    const chip = await frame.evaluate(() => [...document.querySelectorAll(".plx-chat-tool")].map((node) => ({
        name: node.querySelector(".plx-chat-tool-name")?.textContent,
        status: node.querySelector(".plx-chat-tool-status")?.textContent,
        undo: Boolean(node.querySelector(".plx-chat-undo")) })));
    const after = await py(page, GATE);
    check("the tool call shows in the panel and the gate is set in Python",
          chip.some((c) => /call_tool|set_gate/.test(c.name || "") && c.status === "ok" && c.undo)
          && after.ok && Math.abs(after.low - low) < 1e-6 && before.low !== after.low,
          { chip, before: before.low, after: after.low });
    const seen = await frame.evaluate(() => window.__chatSeen);
    const partial = seen.filter((s) => s.text.length > 0 && s.text !== "Setting the CD3 gate." && s.text.length < ANSWER.length
                                    && ANSWER.startsWith(s.text));
    check("the answer was drawn as it streamed", partial.length >= 3 && seen[seen.length - 1].text === ANSWER,
          { lengths: seen.map((s) => s.text.length), spanMs: seen.length ? Math.round(seen[seen.length - 1].at - seen[0].at) : 0 });
    const refused = events.filter((e) => e.status === 406);
    const byPage = events.filter((e) => e.by === "page");
    check("the event stream was refused and the panel polled; polls were answered by the page while the turn ran",
          refused.length >= 1 && byPage.length >= 3 && byPage.some((e) => e.events > 0),
          { refused: refused.length, polls: events.length, byPage: byPage.length, turnMs });

    // Undo, from the chip.
    await frame.click(".plx-chat-undo");
    await frame.waitForFunction(() => [...document.querySelectorAll(".plx-chat-undo")]
        .some((node) => node.textContent === "Undone"), null, { timeout: 30000 });
    const undone = await py(page, GATE);
    check("Undo on the chip puts the gate back", undone.low === before.low, { low: undone.low });
} catch (error) {
    out.errors.push(String(error && error.stack || error).slice(0, 2000));
} finally {
    await browser.close();
    server.stop();
    mock.stop();
    rmSync(temp, { recursive: true, force: true });
}
out.ok = out.checks.length === 6 && out.checks.every((c) => c.ok) && out.errors.length === 0;
console.log(JSON.stringify(out, null, 1));
process.exit(out.ok ? 0 : 1);
