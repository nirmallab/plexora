// drive.mjs - run benchmark scenarios against a Plexora viewer in Chrome.
//
//   node drive.mjs --base http://127.0.0.1:PORT --dataset NAME --scenario z_steps
//                  [--reps 7] [--headed] [--out result.json] [--cache on|off]
//
// Chrome is launched with the GPU (Metal through ANGLE): SwiftShader frames are
// 5-10x slower and would make every number meaningless, so a run whose WebGL
// renderer is software refuses to measure. The page gets probes.js before any
// of its own scripts; scenarios (scenarios.mjs) drive it through `ctx` and
// return {metric: [values...]}. The network is recorded through CDP: requests
// by kind, bytes, status, disk-cache hits, aborts and the server's
// Server-Timing phases.
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { SCENARIOS } from "./scenarios.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CHROME = process.env.PLEXORA_BENCH_CHROME
    || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function parseArgs(argv) {
    const out = { reps: 7, headed: false, cache: "on", out: "" };
    for (let i = 0; i < argv.length; i += 1) {
        const arg = argv[i];
        const next = () => argv[++i];
        if (arg === "--base") out.base = next();
        else if (arg === "--dataset") out.dataset = next();
        else if (arg === "--scenario") out.scenario = next();
        else if (arg === "--reps") out.reps = Number(next());
        else if (arg === "--headed") out.headed = true;
        else if (arg === "--out") out.out = next();
        else if (arg === "--cache") out.cache = next();
        else if (arg === "--params") out.params = JSON.parse(next());
    }
    return out;
}

function kindOf(address) {
    if (/\/volume\/[^/]+\/level/.test(address)) return "level";
    if (/\/volume\/[^/]+\/bricks?\b/.test(address)) return "brick";
    if (/view=(t\d+)?(xzy|yzx)/.test(address)) return "section";
    if (/\/generated\/data\//.test(address)) return "tile";
    if (/\.glsl(\?|$)/.test(address)) return "shader";
    if (/\/(stats|gmm|quantization)/.test(address)) return "stats";
    if (/\/volume\//.test(address)) return "volume";
    return "other";
}

function parseServerTiming(value) {
    const out = {};
    for (const part of String(value || "").split(",")) {
        const [name, ...params] = part.trim().split(";");
        if (!name) continue;
        for (const param of params) {
            const [key, raw] = param.split("=");
            if (key.trim() === "dur") out[name.trim()] = Number(raw);
            if (key.trim() === "desc") out[name.trim()] = raw;
        }
    }
    return out;
}

export class Browser {
    constructor({ headed = false } = {}) {
        this.headed = headed;
        this.id = 0;
        this.pending = new Map();
        this.logs = [];
        this.requests = new Map();
        this.net = [];
    }

    async launch() {
        const port = 9300 + Math.floor(Math.random() * 600);
        this.profile = mkdtempSync(path.join(tmpdir(), "plx-bench-"));
        const args = [
            `--remote-debugging-port=${port}`, `--user-data-dir=${this.profile}`,
            "--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist",
            "--window-size=1440,900", "--no-first-run", "--no-default-browser-check",
            "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows", "--enable-precise-memory-info",
            "about:blank",
        ];
        if (!this.headed) args.unshift("--headless=new");
        this.chrome = spawn(CHROME, args, { stdio: "ignore" });
        let ws = null;
        for (let i = 0; i < 80 && !ws; i += 1) {
            try {
                const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
                const page = list.find((target) => target.type === "page");
                if (page) ws = new WebSocket(page.webSocketDebuggerUrl);
            } catch (error) { /* not up yet */ }
            if (!ws) await sleep(200);
        }
        if (!ws) throw new Error("Chrome did not start");
        await new Promise((resolve) => ws.addEventListener("open", resolve));
        this.ws = ws;
        ws.addEventListener("message", (event) => this.onMessage(JSON.parse(event.data)));
        await this.send("Runtime.enable");
        await this.send("Page.enable");
        await this.send("Network.enable", { maxTotalBufferSize: 0, maxResourceBufferSize: 0 });
        await this.send("Performance.enable");
        await this.send("Emulation.setDeviceMetricsOverride",
                        { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
        for (const name of ["probes.js", "pagekit.js"]) {
            const source = readFileSync(path.join(HERE, name), "utf8");
            await this.send("Page.addScriptToEvaluateOnNewDocument", { source });
        }
        return this;
    }

    onMessage(msg) {
        if (msg.id && this.pending.has(msg.id)) {
            this.pending.get(msg.id)(msg);
            this.pending.delete(msg.id);
            return;
        }
        const p = msg.params || {};
        switch (msg.method) {
        case "Runtime.consoleAPICalled":
            this.logs.push(`[${p.type}] ` + (p.args || []).map((a) => a.value ?? a.description ?? "").join(" "));
            break;
        case "Runtime.exceptionThrown":
            this.logs.push("[exception] " + (p.exceptionDetails?.exception?.description || p.exceptionDetails?.text));
            break;
        case "Network.requestWillBeSent": {
            // `wall` (epoch seconds) is comparable with the page's
            // performance.timeOrigin + now(); `seen` is when this process heard of it.
            const record = { id: p.requestId, url: p.request.url, kind: kindOf(p.request.url),
                             start: p.timestamp, wall: p.wallTime ?? null, seen: Date.now(),
                             status: 0, bytes: 0, disk: false, timing: null,
                             failed: false, canceled: false, end: null };
            this.requests.set(p.requestId, record);
            this.net.push(record);
            break;
        }
        case "Network.requestServedFromCache": {
            const record = this.requests.get(p.requestId);
            if (record) record.disk = true;
            break;
        }
        case "Network.responseReceived": {
            const record = this.requests.get(p.requestId);
            if (!record) break;
            const headers = Object.fromEntries(Object.entries(p.response.headers || {})
                .map(([k, v]) => [k.toLowerCase(), v]));
            record.status = p.response.status;
            record.disk = record.disk || Boolean(p.response.fromDiskCache || p.response.fromMemoryCache);
            record.timing = parseServerTiming(headers["server-timing"]);
            record.ttfb = p.response.timing ? p.response.timing.receiveHeadersEnd : null;
            record.queued = p.response.timing ? p.response.timing.sendStart : null;
            break;
        }
        case "Network.loadingFinished": {
            const record = this.requests.get(p.requestId);
            if (record) { record.bytes = p.encodedDataLength; record.end = p.timestamp; }
            break;
        }
        case "Network.loadingFailed": {
            const record = this.requests.get(p.requestId);
            if (record) { record.failed = true; record.canceled = Boolean(p.canceled); record.end = p.timestamp; }
            break;
        }
        default:
        }
    }

    send(method, params = {}) {
        return new Promise((resolve) => {
            this.id += 1;
            this.pending.set(this.id, resolve);
            this.ws.send(JSON.stringify({ id: this.id, method, params }));
        });
    }

    /** Evaluate an expression (or a function with JSON arguments) in the page. */
    async eval(expr, ...args) {
        const expression = typeof expr === "function"
            ? `(${expr.toString()})(...${JSON.stringify(args)})` : expr;
        const result = await this.send("Runtime.evaluate",
                                       { expression, awaitPromise: true, returnByValue: true });
        if (result.result?.exceptionDetails) {
            const detail = result.result.exceptionDetails;
            throw new Error(`page: ${detail.exception?.description || detail.text}`);
        }
        return result.result?.result?.value;
    }

    async waitFor(expr, timeout = 60000, ...args) {
        const until = Date.now() + timeout;
        while (Date.now() < until) {
            try {
                if (await this.eval(expr, ...args)) return true;
            } catch (error) { /* the page is navigating */ }
            await sleep(100);
        }
        return false;
    }

    async navigate(address) {
        this.requests.clear();
        // Requests of the page left behind never finish: netIdle looks past them.
        this.navMark = this.net.length;
        await this.send("Page.navigate", { url: address });
    }

    /** Wait until no tile, section, level or brick request of this page is in
     *  flight and none has started for `quietMs` (at most `ms`). False on time-out. */
    async netIdle(quietMs = 300, ms = 8000) {
        const kinds = new Set(["tile", "section", "level", "brick"]);
        const end = Date.now() + ms;
        while (Date.now() < end) {
            const live = this.net.slice(this.navMark || 0).filter((r) => kinds.has(r.kind));
            const pending = live.some((r) => r.end === null);
            const last = live.reduce((most, r) => Math.max(most, r.seen || 0), 0);
            if (!pending && Date.now() - last >= quietMs) return true;
            await sleep(25);
        }
        return false;
    }

    /** A PNG (base64) of the element `selector` as composited on screen, or null. */
    async shot(selector = "#openseadragon_wrapper") {
        const clip = await this.eval((sel) => {
            const node = document.querySelector(sel);
            if (!node) return null;
            const r = node.getBoundingClientRect();
            return { x: r.left, y: r.top, width: r.width, height: r.height, scale: 1 };
        }, selector);
        if (!clip || !(clip.width > 0) || !(clip.height > 0)) return null;
        const shot = await this.send("Page.captureScreenshot", { format: "png", clip });
        return shot.result?.data || null;
    }

    async setCache(enabled) {
        await this.send("Network.setCacheDisabled", { cacheDisabled: !enabled });
    }

    async clearCache() {
        await this.send("Network.clearBrowserCache");
    }

    /** Real mouse input: [[type, x, y, delayAfterMs], ...]. */
    async mouse(events) {
        let held = false;
        for (const [type, x, y, delay = 0] of events) {
            const kind = { press: "mousePressed", release: "mouseReleased" }[type] || "mouseMoved";
            if (type === "press") held = true;
            await this.send("Input.dispatchMouseEvent", {
                type: kind, x, y, button: type === "move" && !held ? "none" : "left",
                buttons: held ? 1 : 0, clickCount: type === "press" || type === "release" ? 1 : 0 });
            if (type === "release") held = false;
            if (delay) await sleep(delay);
        }
    }

    async wheel(x, y, deltaY) {
        await this.send("Input.dispatchMouseEvent", { type: "mouseWheel", x, y, deltaX: 0, deltaY });
    }

    async key(key, { code = key, keyCode = 0, repeat = false, up = true } = {}) {
        const base = { key, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode };
        await this.send("Input.dispatchKeyEvent", { type: "keyDown", ...base, autoRepeat: repeat,
                                                     text: key.length === 1 ? key : undefined });
        if (up) await this.send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
    }

    /** Requests seen since `mark` (an index into this.net). */
    netSince(mark) {
        return this.net.slice(mark);
    }

    async metrics() {
        const result = await this.send("Performance.getMetrics");
        return Object.fromEntries((result.result?.metrics || []).map((m) => [m.name, m.value]));
    }

    async renderer() {
        return this.eval(() => {
            const gl = document.createElement("canvas").getContext("webgl2");
            const info = gl && gl.getExtension("WEBGL_debug_renderer_info");
            return info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : (gl ? gl.getParameter(gl.RENDERER) : "none");
        });
    }

    close() {
        try { this.ws?.close(); } catch (error) { /* gone */ }
        try { this.chrome?.kill(); } catch (error) { /* gone */ }
        try { rmSync(this.profile, { recursive: true, force: true }); } catch (error) { /* in use */ }
    }
}

/** Summarise network records by kind. */
export function netSummary(records) {
    const out = {};
    for (const r of records) {
        const k = (out[r.kind] ||= { n: 0, bytes: 0, disk: 0, s304: 0, canceled: 0, failed: 0,
                                    phases: {} });
        k.n += 1;
        k.bytes += r.bytes || 0;
        if (r.disk) k.disk += 1;
        if (r.status === 304) k.s304 += 1;
        if (r.canceled) k.canceled += 1;
        else if (r.failed) k.failed += 1;
        for (const [name, value] of Object.entries(r.timing || {})) {
            if (typeof value === "number") (k.phases[name] ||= []).push(value);
        }
    }
    return out;
}

async function main() {
    const opts = parseArgs(process.argv.slice(2));
    const scenario = SCENARIOS[opts.scenario];
    if (!scenario) throw new Error(`no scenario ${opts.scenario}; have ${Object.keys(SCENARIOS)}`);
    const browser = await new Browser({ headed: opts.headed }).launch();
    const result = { scenario: opts.scenario, dataset: opts.dataset, reps: opts.reps,
                     headed: opts.headed, started: new Date().toISOString() };
    try {
        result.renderer = await browser.renderer();
        if (/swiftshader|llvmpipe|software/i.test(result.renderer || "")) {
            throw new Error(`software WebGL (${result.renderer}): refusing to measure`);
        }
        await browser.setCache(opts.cache !== "off");
        const ctx = { browser, base: opts.base, dataset: opts.dataset, reps: opts.reps,
                      headed: opts.headed, params: opts.params || {}, log: [] };
        const out = await scenario.run(ctx);
        Object.assign(result, out);
        result.log = ctx.log;
    } catch (error) {
        result.error = String(error?.stack || error);
    } finally {
        result.console = browser.logs.filter((line) => /error|exception|warn/i.test(line)).slice(-30);
        browser.close();
    }
    const text = JSON.stringify(result, null, 1);
    if (opts.out) writeFileSync(opts.out, text);
    else process.stdout.write(text + "\n");
    process.exit(result.error ? 2 : 0);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
    main().catch((error) => {
        console.error(error);
        process.exit(1);
    });
}
