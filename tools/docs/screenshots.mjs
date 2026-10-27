#!/usr/bin/env node
// Captures the documentation's screenshots from a running Plexora.
//
//   node tools/docs/screenshots.mjs --base http://127.0.0.1:8791 [--only a,b] [--headed]
//
// The shots are data, in tools/docs/shots.mjs: a page, the steps that set it
// up, and the file it becomes under website/public/media/. Every shot renders
// at 1280x800 CSS pixels and is saved at 2000x1250, so the interface reads at
// the size the docs column shows it and <Media highlights> coordinates are in
// that 2000x1250 space.
//
// Run it against a Plexora that is (1) unlicensed, so no paid control appears
// (BIOCOGNIA_DIR pointing at an empty folder does that), and (2) started with
// --data-dir on a copy of the demo projects shots.mjs names, never your own
// data root: opening a project writes to it, and (3) with --no-token, as no
// viewer token is sent. CONTRIBUTING_DOCS.md has the
// whole recipe. Chrome is driven over the DevTools protocol with Node 22's
// built-in WebSocket; nothing to install.
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { SHOTS } from "./shots.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.resolve(HERE, "..", "..", "website", "public", "media");
const CHROME = process.env.PLEXORA_DOCS_CHROME || {
    darwin: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    win32: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
}[process.platform] || "google-chrome";
const VIEW = { width: 1280, height: 800, scale: 2000 / 1280 };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function parseArgs(argv) {
    const out = { headed: false, only: null, base: "" };
    for (let i = 0; i < argv.length; i += 1) {
        if (argv[i] === "--base") out.base = argv[++i].replace(/\/$/, "");
        else if (argv[i] === "--only") out.only = new Set(argv[++i].split(","));
        else if (argv[i] === "--headed") out.headed = true;
    }
    if (!out.base) throw new Error("--base http://127.0.0.1:PORT is required");
    return out;
}

export class Page {
    constructor() {
        this.id = 0;
        this.pending = new Map();
        this.inflight = new Set();
        this.lastNet = Date.now();
    }

    async launch(headed) {
        const port = 9700 + Math.floor(Math.random() * 200);
        this.profile = mkdtempSync(path.join(tmpdir(), "plx-docs-"));
        const args = [
            `--remote-debugging-port=${port}`, `--user-data-dir=${this.profile}`,
            "--use-angle=" + (process.platform === "darwin" ? "metal" : "swiftshader"),
            "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
            "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
            "--force-color-profile=srgb", "--font-render-hinting=none",
            `--window-size=${VIEW.width},${VIEW.height}`, "about:blank",
        ];
        if (!headed) args.unshift("--headless=new");
        this.chrome = spawn(CHROME, args, { stdio: "ignore" });
        let ws = null;
        for (let i = 0; i < 100 && !ws; i += 1) {
            try {
                const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
                const target = list.find((t) => t.type === "page");
                if (target) ws = new WebSocket(target.webSocketDebuggerUrl);
            } catch { /* not up yet */ }
            if (!ws) await sleep(150);
        }
        if (!ws) throw new Error(`Chrome did not start (${CHROME})`);
        await new Promise((resolve) => ws.addEventListener("open", resolve));
        this.ws = ws;
        ws.addEventListener("message", (event) => this.onMessage(JSON.parse(event.data)));
        await this.send("Page.enable");
        await this.send("Runtime.enable");
        await this.send("Network.enable");
        await this.send("Emulation.setDeviceMetricsOverride", {
            width: VIEW.width, height: VIEW.height, deviceScaleFactor: VIEW.scale, mobile: false,
        });
        await this.send("Emulation.setEmulatedMedia", {
            features: [{ name: "prefers-reduced-motion", value: "reduce" }],
        });
    }

    onMessage(msg) {
        if (msg.id && this.pending.has(msg.id)) {
            const { resolve, reject } = this.pending.get(msg.id);
            this.pending.delete(msg.id);
            if (msg.error) reject(new Error(msg.error.message));
            else resolve(msg.result);
            return;
        }
        const p = msg.params || {};
        if (msg.method === "Network.requestWillBeSent") this.inflight.add(p.requestId);
        if (msg.method === "Network.loadingFinished" || msg.method === "Network.loadingFailed") {
            this.inflight.delete(p.requestId);
        }
        if (msg.method?.startsWith("Network.")) this.lastNet = Date.now();
        if (msg.method === "Runtime.exceptionThrown") {
            console.warn("  page error:", p.exceptionDetails?.exception?.description?.split("\n")[0]);
        }
    }

    /** One DevTools call. A page that stops answering fails the shot after a
     * minute rather than hanging the run. */
    send(method, params = {}) {
        const id = ++this.id;
        this.ws.send(JSON.stringify({ id, method, params }));
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                this.pending.delete(id);
                reject(new Error(`${method} got no answer in 60 s`));
            }, 60000);
            this.pending.set(id, {
                resolve: (v) => { clearTimeout(timer); resolve(v); },
                reject: (e) => { clearTimeout(timer); reject(e); },
            });
        });
    }

    async eval(expression) {
        const r = await this.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
        if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || expression);
        return r.result.value;
    }

    async goto(url) {
        const loaded = new Promise((resolve) => {
            const onLoad = (event) => {
                if (JSON.parse(event.data).method === "Page.loadEventFired") {
                    this.ws.removeEventListener("message", onLoad);
                    resolve();
                }
            };
            this.ws.addEventListener("message", onLoad);
        });
        await this.send("Page.navigate", { url });
        await loaded;
    }

    /** Until no request has been pending or finished for `quiet` ms. */
    async settle(quiet = 900, limit = 30000) {
        const start = Date.now();
        while (Date.now() - start < limit) {
            if (this.inflight.size === 0 && Date.now() - this.lastNet > quiet) return;
            await sleep(100);
        }
        console.warn(`  still loading after ${limit} ms; capturing anyway`);
    }

    async waitFor(expression, limit = 20000) {
        const start = Date.now();
        while (Date.now() - start < limit) {
            if (await this.eval(`Boolean(${expression})`).catch(() => false)) return;
            await sleep(150);
        }
        throw new Error(`timed out waiting for ${expression}`);
    }

    async point(selector) {
        const box = await this.eval(`(() => {
            const el = document.querySelector(${JSON.stringify(selector)});
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
        })()`);
        if (!box) throw new Error(`no element matches ${selector}`);
        return box;
    }

    async click(selector) {
        const { x, y } = await this.point(selector);
        await this.mouse(x, y);
    }

    async mouse(x, y) {
        await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
        await this.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
        await this.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
    }

    /** Press at the first point, move through the rest, release at the last. */
    async drag(points) {
        const [[x0, y0], ...rest] = points;
        await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: x0, y: y0 });
        await this.send("Input.dispatchMouseEvent", { type: "mousePressed", x: x0, y: y0, button: "left", clickCount: 1 });
        for (const [x, y] of rest) {
            await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "left", buttons: 1 });
            await sleep(16);
        }
        const [x1, y1] = points[points.length - 1];
        await this.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: x1, y: y1, button: "left", clickCount: 1 });
    }

    async hover(selector) {
        const { x, y } = await this.point(selector);
        await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    }

    async key(key, modifiers = 0) {
        const code = key.length === 1 ? `Key${key.toUpperCase()}` : key;
        const text = key.length === 1 && !(modifiers & ~8) ? key : undefined;
        await this.send("Input.dispatchKeyEvent", { type: "keyDown", key, code, modifiers, text });
        await this.send("Input.dispatchKeyEvent", { type: "keyUp", key, code, modifiers });
    }

    async type(text) {
        await this.send("Input.insertText", { text });
    }

    async shot(file, clip) {
        const params = { format: "webp", quality: 72, captureBeyondViewport: false };
        if (clip) params.clip = { scale: 1, ...clip };
        const { data } = await this.send("Page.captureScreenshot", params);
        mkdirSync(path.dirname(file), { recursive: true });
        writeFileSync(file, Buffer.from(data, "base64"));
    }

    close() {
        try { this.chrome.kill(); } catch { /* gone */ }
        try { rmSync(this.profile, { recursive: true, force: true }); } catch { /* in use */ }
    }
}

/** `{data}` in a step's strings is the folder of public demo files. */
function withData(step) {
    const text = JSON.stringify(step);
    if (!text.includes("{data}")) return step;
    const data = process.env.PLEXORA_DOCS_DATA;
    if (!data) throw new Error("set PLEXORA_DOCS_DATA to the folder of demo files this shot picks");
    return JSON.parse(text.replaceAll("{data}", data.replace(/[\\"]/g, "\\$&")));
}

async function runStep(page, base, shot, raw) {
    const step = withData(raw);
    if (step.wait) await sleep(step.wait);
    else if (step.goto) {
        await page.goto(`${base}${step.goto}`);
        await page.settle(900, shot.limit);
    }
    else if (step.if) {
        // Steps that make state (a figure's panels) run only while it is missing.
        if (await page.eval(`Boolean(${step.if})`)) {
            for (const inner of step.then) await runStep(page, base, shot, inner);
        }
    }
    else if (step.waitFor) await page.waitFor(step.waitFor, step.limit);
    else if (step.click) await page.click(step.click);
    else if (step.at) await page.mouse(...step.at);
    else if (step.move) await page.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: step.move[0], y: step.move[1] });
    else if (step.drag) await page.drag(step.drag);
    else if (step.hover) await page.hover(step.hover);
    else if (step.key) await page.key(step.key, step.modifiers || 0);
    else if (step.type) await page.type(step.type);
    else if (step.eval) await page.eval(step.eval);
    else if (step.settle) await page.settle(step.settle, shot.limit);
    else throw new Error(`unknown step ${JSON.stringify(step)}`);
}

export async function run(page, base, shot) {
    await page.goto(`${base}${shot.url}`);
    await page.settle(900, shot.limit);
    for (const step of shot.steps || []) await runStep(page, base, shot, step);
    await page.settle(shot.quiet ?? 1200, shot.limit);
    await sleep(shot.after ?? 400);
    await page.shot(path.join(OUT, `${shot.name}.webp`), shot.clip);
}

async function main() {
    const args = parseArgs(process.argv.slice(2));
    const todo = SHOTS.filter((s) => !s.manual && (!args.only || args.only.has(s.name)));
    const page = new Page();
    let failed = 0;
    try {
        await page.launch(args.headed);
        for (const shot of todo) {
            process.stdout.write(`${shot.name} ... `);
            try {
                await run(page, args.base, shot);
                console.log("ok");
            } catch (error) {
                failed += 1;
                console.log(`FAILED: ${error.message}`);
            }
        }
    } finally {
        page.close();
    }
    console.log(`${todo.length - failed}/${todo.length} captured into ${path.relative(process.cwd(), OUT)}`);
    process.exit(failed ? 1 : 0);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
