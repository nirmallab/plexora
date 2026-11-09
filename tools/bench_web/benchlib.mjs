// Shared page-side measurements for both arms (website frame / server page).
// Where things are, from the environment (nothing of one machine in the tree):
//   PLEXORA_PLAYWRIGHT_MODULES  a node_modules holding playwright-core (as tests/browser/lib.mjs)
//   PLEXORA_CHROME              the Chrome to drive (default: the platform's usual place)
//   PLEXORA_PYTHON              the Python with Plexora's dependencies (default: python3)
//   PLEXORA_BENCH_WORK          where fixtures, sites, data roots and out/ go (default: <tmp>/plexora-bench-web)
import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const modules = process.env.PLEXORA_PLAYWRIGHT_MODULES;
export const { chromium } = await import(modules
    ? pathToFileURL(path.join(modules, "playwright-core", "index.mjs")).href : "playwright-core");
// A DevTools message too long to be a string (over 512 MB) ends Playwright's
// connection (ERR_STRING_TOO_LONG). Its first and last bytes are printed
// instead, and Playwright is handed an empty message, so the run goes on and
// says what it was.
{
    const LIMIT = 0x1fffffe8;
    const native = Buffer.prototype.utf8Slice;
    Buffer.prototype.utf8Slice = function (start = 0, end = this.length) {
        if (end - start > LIMIT) {
            const head = this.subarray(start, start + 800).toString("latin1");
            const tail = this.subarray(end - 300, end).toString("latin1");
            process.stderr.write(`HUGE DEVTOOLS MESSAGE ${end - start} bytes\n  head: ${head}\n  tail: ${tail}\n`);
            return "{}";
        }
        return native.call(this, start, end);
    };
}

export const CHROME = process.env.PLEXORA_CHROME || ({
    darwin: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    win32: "C:/Program Files/Google/Chrome/Application/chrome.exe",
}[process.platform] || "/usr/bin/google-chrome");
export const PY = process.env.PLEXORA_PYTHON || "python3";
export const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
export const PYTHONPATH = ["spatialagents/src", "spatialbridge/src", "biocognia-markers/src", "biocognia-intelligence/src"]
    .map((p) => path.join(REPO, "..", p)).concat([REPO]).join(":");
export const S = process.env.PLEXORA_BENCH_WORK || path.join(tmpdir(), "plexora-bench-web");
mkdirSync(path.join(S, "out"), { recursive: true });

export async function launch(profile = mkdtempSync(path.join(tmpdir(), "pxbench-"))) {
    const context = await chromium.launchPersistentContext(profile, {
        executablePath: CHROME, headless: true,
        args: ["--no-first-run", "--no-default-browser-check", "--disable-background-timer-throttling",
               "--disable-renderer-backgrounding", "--enable-unsafe-webgpu"],
    });
    return { context, profile, close: () => context.close() };
}

// Every frame: a resource observer for tiles and scripts, and rAF frame timing.
export const INIT = () => {
    // A console message over 512 MB ends Playwright's connection
    // (ERR_STRING_TOO_LONG). Strings over 1 MB are cut here, and where each
    // came from is kept in window.__bigLogs.
    const CAP = 1 << 20;
    const huge = (a) => (typeof a === "string" && a.length > CAP) || (a instanceof Error && String(a.message).length > CAP);
    for (const level of ["log", "warn", "error", "info", "debug"]) {
        const original = console[level].bind(console);
        console[level] = (...args) => {
            if (args.some(huge)) {
                const text = String(args.find(huge) instanceof Error ? args.find(huge).message : args.find(huge));
                (window.__bigLogs = window.__bigLogs || []).push({ level, length: text.length, head: text.slice(0, 300),
                                                                   stack: String(new Error().stack).slice(0, 1500) });
                args = args.map((a) => (huge(a) ? `${String(a instanceof Error ? a.message : a).slice(0, 2000)}... (${text.length} chars)` : a));
            }
            return original(...args);
        };
    }
    // The same for an uncaught error or rejection (reported to DevTools as
    // an exception, not a log): a huge one is kept here and not reported.
    const keepHuge = (kind, value, event) => {
        const text = value instanceof Error ? `${value.message}\n${value.stack || ""}` : String(value);
        if (text.length <= CAP) return;
        (window.__bigLogs = window.__bigLogs || []).push({ level: kind, length: text.length, head: text.slice(0, 300),
                                                           stack: value instanceof Error ? String(value.stack).slice(0, 1500) : null });
        event.preventDefault();
    };
    window.addEventListener("error", (event) => keepHuge("error-event", event.error || event.message, event), true);
    window.addEventListener("unhandledrejection", (event) => keepHuge("rejection", event.reason, event), true);
    // What DevTools carries whole besides a log: a request's body, a data:
    // URL, a history entry's URL. One over 1 MB is announced in one short
    // console line ("BENCH-BIG ...") before the event that would carry it.
    const announce = (kind, size, what) => original_log(`BENCH-BIG ${kind} ${size} ${String(what).slice(0, 160)} @ ${String(new Error().stack).split("\n").slice(2, 9).join(" | ").slice(0, 900)}`);
    const original_log = console.log;
    const sizeOf = (body) => (body == null ? 0 : typeof body === "string" ? body.length : body.byteLength || body.size || 0);
    const nativeFetch = window.fetch;
    window.fetch = function (input, init) {
        const size = sizeOf(init && init.body);
        if (size > CAP) announce("fetch-body", size, typeof input === "string" ? input : input && input.url);
        const url = typeof input === "string" ? input : (input && input.url) || "";
        if (url.length > CAP) announce("fetch-url", url.length, url.slice(0, 80));
        return nativeFetch.apply(this, arguments);
    };
    const nativeOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) { this.__benchUrl = String(url); if (this.__benchUrl.length > CAP) announce("xhr-url", this.__benchUrl.length, this.__benchUrl.slice(0, 80)); return nativeOpen.apply(this, arguments); };
    const nativeSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function (body) { const size = sizeOf(body); if (size > CAP) announce("xhr-body", size, this.__benchUrl); return nativeSend.apply(this, arguments); };
    const srcOf = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, "src");
    Object.defineProperty(HTMLImageElement.prototype, "src", { configurable: true, enumerable: true, get() { return srcOf.get.call(this); },
        set(value) { if (typeof value === "string" && value.length > CAP) announce("img-src", value.length, value.slice(0, 60)); srcOf.set.call(this, value); } });
    const nativeSetAttribute = Element.prototype.setAttribute;
    Element.prototype.setAttribute = function (name, value) { if (typeof value === "string" && value.length > CAP) announce(`attr-${name}`, value.length, value.slice(0, 60)); return nativeSetAttribute.apply(this, arguments); };
    for (const method of ["pushState", "replaceState"]) {
        const native = history[method];
        history[method] = function (state, title, url) { const u = url == null ? "" : String(url); if (u.length > CAP) announce(method, u.length, u.slice(0, 80)); return native.apply(this, arguments); };
    }
    window.__res = [];
    try {
        new PerformanceObserver((list) => {
            for (const e of list.getEntries()) {
                if (/\/generated\/|\/tools\/|\.js(\?|$)|\.css(\?|$)|\/plugins\/|\/get_|\/init_database|\/config|\/resource_/.test(e.name)) {
                    window.__res.push({ n: e.name.replace(/^https?:\/\/[^/]+/, "").slice(0, 160), s: Math.round(e.startTime),
                                        d: Math.round(e.duration), w: Math.round(e.workerStart || 0), b: e.transferSize || 0, t: e.initiatorType });
                }
            }
        }).observe({ type: "resource", buffered: true });
    } catch {}
    window.__frames = null;
    window.__recordFrames = (ms) => new Promise((resolve) => {
        const ts = []; let last = performance.now(); const end = last + ms;
        const tick = (t) => { ts.push(t - last); last = t; if (t < end) requestAnimationFrame(tick); else resolve(ts); };
        requestAnimationFrame(tick);
    });
};

/** The console cap of INIT, for a worker (`page.on("worker")`): its own
 *  console is outside the page's. */
export const WORKER_INIT = () => {
    const CAP = 1 << 20;
    for (const level of ["log", "warn", "error", "info", "debug"]) {
        const original = console[level].bind(console);
        console[level] = (...args) => {
            const big = args.find((a) => typeof a === "string" && a.length > CAP);
            if (big !== undefined) {
                (self.__bigLogs = self.__bigLogs || []).push({ level, length: big.length, head: big.slice(0, 300), stack: String(new Error().stack).slice(0, 1500) });
                args = args.map((a) => (typeof a === "string" && a.length > CAP ? `${a.slice(0, 2000)}... (${a.length} chars)` : a));
            }
            return original(...args);
        };
    }
    return self.location.href;
};

export const viewerDrawn = () => {
    // Tiles the page reads itself (tileFetch.local, the website's browser
    // node) never reach the network or Resource Timing: timed here instead,
    // from the first poll that finds the viewer.
    const fetcher = window.__plexora?.seaDragonViewer?.tileFetch;
    if (fetcher && typeof fetcher.local === "function" && !fetcher.local.__timed) {
        const local = fetcher.local;
        window.__localTiles = window.__localTiles || [];
        fetcher.local = async (request) => {
            const s = performance.now();
            const answered = await local(request);
            if (answered) window.__localTiles.push({ s: Math.round(s), d: Math.round(performance.now() - s) });
            return answered;
        };
        fetcher.local.__timed = true;
    }
    const viewer = window.__plexora?.seaDragonViewer?.viewer;
    if (!viewer || !viewer.world || !viewer.world.getItemCount()) return false;
    for (let i = 0; i < viewer.world.getItemCount(); i += 1) {
        const item = viewer.world.getItemAt(i);
        if (item.getOpacity() > 0 && !item.getFullyLoaded()) return false;
    }
    return true;
};

export async function waitViewer(frame, timeout = 120000) {
    const t = Date.now();
    await frame.waitForFunction(viewerDrawn, null, { timeout, polling: 50 });
    return Date.now() - t;
}

/** Tile resource entries since `since` (ms on the frame clock) split by served-by. */
export const tileStats = (since = 0) => {
    const tiles = window.__res.filter((e) => e.n.includes("/generated/") && e.s >= since);
    const local = (window.__localTiles || []).filter((e) => e.s >= since);
    const d = tiles.map((e) => e.d).concat(local.map((e) => e.d)).sort((a, b) => a - b);
    const q = (p) => (d.length ? d[Math.min(d.length - 1, Math.floor(p * d.length))] : null);
    return { n: d.length, p50: q(0.5), p95: q(0.95), max: d[d.length - 1] ?? null, sumMs: d.reduce((a, b) => a + b, 0),
             viaWorker: tiles.filter((e) => e.w > 0).length, local: local.length };
};

/** A tour step: place the view, wait until drawn and settled; frame intervals meanwhile. */
export async function tourStep(frame, label, placer) {
    process.stderr.write(`tour ${label}...\n`);
    const t0 = Date.now();
    const since = await frame.evaluate(() => performance.now());
    const framesP = frame.evaluate((ms) => window.__recordFrames(ms), 1500);
    await frame.evaluate(placer);
    // settle: OSD animation done + all items fully loaded + 2 consecutive polls
    await frame.waitForFunction(() => {
        const v = window.__plexora?.seaDragonViewer?.viewer;
        if (!v) return false;
        const animating = v.viewport && (v.viewport.zoomSpring.current.value !== v.viewport.zoomSpring.target.value
            || v.viewport.centerSpringX.current.value !== v.viewport.centerSpringX.target.value);
        return !animating && (() => {
            for (let i = 0; i < v.world.getItemCount(); i += 1) { const it = v.world.getItemAt(i); if (it.getOpacity() > 0 && !it.getFullyLoaded()) return false; }
            return true;
        })();
    }, null, { timeout: 60000, polling: 30 }).catch(() => null);
    const fullMs = Date.now() - t0;
    const frames = await framesP;
    const sorted = frames.slice().sort((a, b) => a - b);
    const tiles = await frame.evaluate(tileStats, since);
    return { label, fullMs, frameP50: sorted[Math.floor(sorted.length / 2)], frameP95: sorted[Math.floor(sorted.length * 0.95)],
             frameMax: Math.max(...sorted), long: frames.filter((f) => f > 50).length, tiles };
}

export async function tour(frame) {
    const out = [];
    const place = (zoom, cx, cy, immediate) => `(() => {
        const v = window.__plexora.seaDragonViewer.viewer; const item = v.world.getItemAt(0);
        const w = item.source.width, h = item.source.height;
        const pt = item.imageToViewportCoordinates(w * ${cx}, h * ${cy});
        v.viewport.zoomTo(item.imageToViewportZoom(${zoom}), pt, ${immediate}); v.viewport.panTo(pt, ${immediate});
    })()`;
    out.push(await tourStep(frame, "zoom_quarter_centre", place(0.25, 0.5, 0.5, true)));
    out.push(await tourStep(frame, "zoom_full_centre", place(1, 0.5, 0.5, true)));
    out.push(await tourStep(frame, "pan_full_b", place(1, 0.3, 0.3, true)));
    out.push(await tourStep(frame, "pan_full_c", place(1, 0.7, 0.6, true)));
    out.push(await tourStep(frame, "anim_zoom_out", place(0.25, 0.5, 0.5, false)));
    out.push(await tourStep(frame, "anim_pan", place(0.25, 0.3, 0.4, false)));
    // `void`: goHome returns the viewport, and an evaluate returning it sends
    // OpenSeadragon's whole object graph back (587 MB on LSP11385: Playwright's
    // ERR_STRING_TOO_LONG, which once ended the local server's arm here too).
    out.push(await tourStep(frame, "fit", `void window.__plexora.seaDragonViewer.viewer.viewport.goHome(true)`));
    return out;
}

/** Python requests recorded by the website's client (host window) since index `from`. */
export const pyRecent = async (host) => host.evaluate(() => (window.PlexoraPython ? window.PlexoraPython.recent() : []));
export function byRoute(recent) {
    const out = {};
    for (const r of recent) {
        const key = r.path.replace(/\/generated\/data\/.*/, "/generated/data/*").replace(/\/[^/]+\/tools\/([^/]+)\/panel/, "/<ds>/tools/$1/panel")
            .replace(/^\/(image)$/, "/<page>").replace(/\/agent\/v1\/viewer\/sessions\/[^/]+\//, "/agent/v1/viewer/sessions/<id>/");
        const row = out[key] || (out[key] = { n: 0, ms: 0, py: 0, max: 0 });
        row.n += 1; row.ms += r.ms; row.py += r.pyMs || 0; row.max = Math.max(row.max, r.ms);
    }
    return Object.fromEntries(Object.entries(out).sort((a, b) => b[1].ms - a[1].ms));
}

/** Open every tool of the viewer's sidebar in turn; time each. */
export async function openTools(frame, host, only = null) {
    let names = await frame.evaluate(() => [...new Set([...document.querySelectorAll("[data-tool]")].map((b) => b.dataset.tool).filter(Boolean))]);
    if (only) names = names.filter((n) => only.includes(n));
    const out = [];
    for (const name of names) {
        const before = host ? (await pyRecent(host)).length : 0;
        const since = await frame.evaluate(() => performance.now());
        const t0 = Date.now();
        process.stderr.write(`tool ${name}...\n`);
        const result = await frame.evaluate(async (n) => {
            // A tool whose prerequisites are missing opens a requirements dialog
            // and waits for a person: record what it asked and close it.
            const asked = [];
            // Answer it with its defaults (Save/Continue); if it is still up
            // 4 s later (a question only a person can answer), use its X --
            // the only two paths that settle `collect`'s promise.
            let seenAt = 0;
            const watcher = setInterval(() => {
                const dialog = document.querySelector("dialog.requirements-modal[open]");
                if (!dialog) { seenAt = 0; return; }
                if (!seenAt) {
                    seenAt = performance.now();
                    asked.push(dialog.querySelector(".requirements-title")?.textContent.trim() + " / " + (dialog.querySelector(".requirements-body")?.innerText || "").slice(0, 160).replace(/\s+/g, " "));
                    dialog.querySelector('[data-action="save"]')?.click();
                } else if (performance.now() - seenAt > 4000) {
                    asked.push("closed");
                    dialog.querySelector('[data-action="close"]')?.click();
                    seenAt = 0;
                }
            }, 100);
            const timeout = new Promise((r) => setTimeout(() => r("timeout 25s"), 25000));
            try {
                const out = await Promise.race([window.PlexoraToolLoader.openTool(n).then((r) => (r && r.skipped ? `skipped: ${r.skipped}` : "ok")), timeout]);
                return asked.length ? `${out} (asked: ${asked.join("; ")})` : out;
            } catch (e) { return `error: ${String(e).slice(0, 200)}`; } finally { clearInterval(watcher); }
        }, name).catch((e) => `evaluate: ${String(e).slice(0, 200)}`);
        // the panel mounted and visible
        await frame.waitForFunction(() => {
            const slot = document.getElementById("tool_panel_slot") || document.getElementById("tool_panel_legacy_slot");
            return slot && slot.children.length && !slot.classList.contains("tool-panel-hidden");
        }, null, { timeout: 5000, polling: 30 }).catch(() => null);
        await frame.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
        const ms = Date.now() - t0;
        const res = await frame.evaluate((s) => window.__res.filter((e) => e.s >= s), since);
        const scripts = res.filter((e) => /\.js(\?|$)/.test(e.n)).length;
        const panel = res.filter((e) => e.n.includes("/tools/")).map((e) => ({ n: e.n.slice(0, 90), d: e.d }));
        let python = null;
        if (host) { const recent = (await pyRecent(host)).slice(before); python = { n: recent.length, ms: recent.reduce((a, r) => a + r.ms, 0), py: recent.reduce((a, r) => a + (r.pyMs || 0), 0), routes: byRoute(recent) }; }
        const requests = res.length;
        // Every request but a file, by route, as the page timed it: the
        // server's arm has no Python client to ask (`python` is the website's).
        const fetched = {};
        for (const e of res.filter((x) => !/\.(js|mjs|css|png|svg|woff2?|json)(\?|$)/.test(x.n))) {
            const route = e.n.replace(/^https?:\/\/[^/]+/, "").split("?")[0].slice(0, 80);
            const row = (fetched[route] ||= { n: 0, ms: 0, max: 0 });
            row.n += 1; row.ms += Math.round(e.d); row.max = Math.max(row.max, Math.round(e.d));
        }
        out.push({ tool: name, ms, result, scripts, requests, panel, python, fetched });
        await frame.waitForTimeout(400);
    }
    return out;
}

export function startStatic(site, extra = []) {
    return new Promise((resolve, reject) => {
        const child = spawn(PY, [path.join(REPO, "tools", "browser_build", "serve.py"), "--site", site, ...extra],
                            { cwd: REPO, stdio: ["ignore", "pipe", "inherit"] });
        let buffer = "";
        child.stdout.on("data", (chunk) => {
            buffer += chunk;
            if (buffer.includes("\n")) { try { resolve({ port: JSON.parse(buffer.split("\n")[0]).port, stop: () => child.kill() }); } catch (e) { reject(e); } }
        });
        child.on("exit", (code) => reject(new Error(`serve.py exited ${code}`)));
    });
}

export function startServer(data) {
    return new Promise((resolve, reject) => {
        const t0 = Date.now();
        const child = spawn(PY, ["-u", "-m", "plexora", "--no-browser", "--port", "0", "--data-dir", data],
                            { cwd: REPO, env: { ...process.env, PYTHONPATH, PLEXORA_DATA_PATH: data, PLEXORA_TELEMETRY: "off", PYTHONUNBUFFERED: "1" },
                              stdio: ["ignore", "pipe", "pipe"] });
        let buffer = "";
        const onData = (chunk) => {
            buffer += chunk;
            const m = buffer.match(/Serving Plexora at (\S+)/);
            if (m) resolve({ url: m[1], servingMs: Date.now() - t0, stop: () => child.kill("SIGTERM"), log: () => buffer });
        };
        child.stdout.on("data", onData); child.stderr.on("data", onData);
        child.on("exit", (code) => reject(new Error(`server exited ${code}: ${buffer.slice(-800)}`)));
        setTimeout(() => reject(new Error(`server did not announce: ${buffer.slice(-800)}`)), 120000);
    });
}

export const post = (frame, route, body) => frame.evaluate(async ([r, b]) => {
    const t = performance.now();
    const response = await fetch(r, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
    const text = await response.text();
    let json = null; try { json = JSON.parse(text); } catch {}
    return { status: response.status, ms: Math.round(performance.now() - t), body: json, text: json ? undefined : text.slice(0, 300) };
}, [route, body]);

export const get = (frame, route) => frame.evaluate(async (r) => {
    const t = performance.now();
    const response = await fetch(r);
    const type = response.headers.get("Content-Type") || "";
    const body = type.includes("json") ? await response.json() : { bytes: (await response.arrayBuffer()).byteLength };
    return { status: response.status, ms: Math.round(performance.now() - t), body };
}, route);

export async function pollReady(frame, name, timeout = 120000) {
    const t0 = Date.now();
    const q = `datasource=${encodeURIComponent(name)}`;
    let table = null, seg = null;
    while (Date.now() - t0 < timeout) {
        table = (await get(frame, `get_table_status?${q}`)).body;
        seg = (await get(frame, `get_segmentation_status?${q}`)).body;
        const tableOk = !table || table.status === "ready" || table.state === "ready" || table.ready === true || table.status === "none";
        const segOk = !seg || ["ready", "none", "missing"].includes(seg.status) || seg.ready === true;
        if (tableOk && segOk) break;
        await new Promise((r) => setTimeout(r, 300));
    }
    return { ms: Date.now() - t0, table, seg };
}
