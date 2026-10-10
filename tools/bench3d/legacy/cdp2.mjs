// A tiny Chrome DevTools Protocol driver: launch headless Chrome, run steps.
// usage: node cdp.mjs <url> <steps.json> [outdir]
// steps: [{"eval": "js expr"}, {"wait": ms}, {"shot": "name.png"}, {"waitFor": "js expr", "timeout": ms}]
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const [, , target, stepsFile, outdir = "."] = process.argv;
const steps = JSON.parse(readFileSync(stepsFile, "utf8"));
const port = 9300 + Math.floor(Math.random() * 500);
const profile = mkdtempSync(path.join(tmpdir(), "cdp-"));
const chrome = spawn("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", [
    "--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    ...(process.env.CDP_GPU ? ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"]
                            : ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]),
    "--window-size=1440,900",
    "--no-first-run", "--no-default-browser-check", "about:blank"], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws;
for (let i = 0; i < 50; i += 1) {
    try {
        const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
        const page = list.find((t) => t.type === "page");
        if (page) { ws = new WebSocket(page.webSocketDebuggerUrl); break; }
    } catch (e) { /* not up yet */ }
    await sleep(200);
}
if (!ws) { console.error("no chrome"); chrome.kill(); process.exit(1); }
await new Promise((r) => ws.addEventListener("open", r));
let id = 0;
const pending = new Map();
const logs = [];
ws.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
    if (msg.method === "Runtime.consoleAPICalled") {
        logs.push(`[${msg.params.type}] ` + msg.params.args.map((a) => a.value ?? a.description ?? "").join(" "));
    }
    if (msg.method === "Runtime.exceptionThrown") {
        logs.push("[exception] " + (msg.params.exceptionDetails?.exception?.description || msg.params.exceptionDetails?.text));
    }
});
const send = (method, params = {}) => new Promise((resolve) => {
    id += 1; pending.set(id, resolve); ws.send(JSON.stringify({ id, method, params }));
});
await send("Runtime.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await send("Page.navigate", { url: target });
await sleep(1500);
const evaluate = async (expression) => {
    const result = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (result.result?.exceptionDetails) return { error: result.result.exceptionDetails.exception?.description };
    return result.result?.result?.value;
};
for (const step of steps) {
    if (step.eval) {
        const value = await evaluate(step.eval);
        console.log("eval:", step.label || step.eval.slice(0, 60), "=>", JSON.stringify(value)?.slice(0, 2000));
    } else if (step.wait) {
        await sleep(step.wait);
    } else if (step.waitFor) {
        const until = Date.now() + (step.timeout || 20000);
        let ok = false;
        while (Date.now() < until) {
            if (await evaluate(step.waitFor)) { ok = true; break; }
            await sleep(250);
        }
        console.log("waitFor:", step.waitFor.slice(0, 60), ok ? "ok" : "TIMEOUT");
    } else if (step.mouseFrom) {
        // Real input: [[type, x, y, delayAfterMs], ...] from a page expression.
        // type: press | move | release | hover (a move with no button held).
        const events = await evaluate(step.mouseFrom);
        if (!Array.isArray(events)) { console.log("mouseFrom: no events", JSON.stringify(events)); continue; }
        let held = false;
        for (const [type, x, y, delay = 0] of events) {
            const kind = { press: "mousePressed", release: "mouseReleased", press2: "mousePressed",
                           release2: "mouseReleased" }[type] || "mouseMoved";
            if (type === "press" || type === "press2") held = true;
            await send("Input.dispatchMouseEvent", {
                type: kind, x, y, button: type === "hover" || (type === "move" && !held) ? "none" : "left",
                buttons: held ? 1 : 0,
                clickCount: type.endsWith("2") ? 2 : (type === "press" || type === "release" ? 1 : 0) });
            if (type === "release" || type === "release2") held = false;
            if (delay) await sleep(delay);
        }
        console.log("mouse:", step.label || "", events.length, "events");
    } else if (step.navigate) {
        await send("Page.navigate", { url: step.navigate });
        await sleep(1500);
        console.log("navigate:", step.navigate);
    } else if (step.shot) {
        const shot = await send("Page.captureScreenshot", { format: "png" });
        writeFileSync(path.join(outdir, step.shot), Buffer.from(shot.result.data, "base64"));
        console.log("shot:", step.shot);
    }
}
console.log("--- console ---");
for (const line of logs.slice(-40)) console.log(line.slice(0, 400));
ws.close();
chrome.kill();
process.exit(0);
