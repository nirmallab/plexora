// tour.mjs - one block of the remote-access benchmark: a fixed tour of one
// project, in one fresh Chrome, once per pass.
//
//   node tour.mjs --base http://127.0.0.1:PORT --dataset NAME --out result.json
//                 --params '{"passes": ["C1", "C2s", "C2"], "z0": 128, "channels": [0, 4, 7],
//                            "waypoints": {"a": [x, y], "b": [x, y], "c": [x, y]},
//                            "timeout": 120000, "volume": true}'
//
// Passes: C1 starts from an empty browser cache (Chrome's profile is new); C2s
// clears the browser cache but finds the servers as C1 left them; C2 keeps
// everything. Every pass opens the project from a fresh navigation with the
// page's own storage cleared (saved view state, the routing verdict), so each
// starts from the same view.
//
// Every step is timed from the page clock (see tourpage.js for "full"); each
// request is attributed to the step during which Chrome first saw it, and
// bytes are counted once the pass has ended (a request still in flight when
// its step ended is counted with that step). Step windows are recorded in
// epoch ms so the caller can line its own samplers (nettop, /proc) up with
// them. Writes the project would make (`save_channel_list`, view state) are
// answered locally and never reach the server.
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Browser, netSummary, sleep } from "../bench3d/drive.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));

function parseArgs(argv) {
    const out = { headed: false, params: {} };
    for (let i = 0; i < argv.length; i += 1) {
        const arg = argv[i];
        const next = () => argv[++i];
        if (arg === "--base") out.base = next();
        else if (arg === "--dataset") out.dataset = next();
        else if (arg === "--out") out.out = next();
        else if (arg === "--headed") out.headed = true;
        else if (arg === "--params") out.params = JSON.parse(next());
    }
    return out;
}

/** Writes answered here: the project must look the same to every pass. */
const BLOCKED = [/\/save_channel_list\b/, /\/save_view_state\b/, /\/viewer_prefs\b/];

async function blockWrites(browser) {
    await browser.send("Fetch.enable", { patterns: [{ urlPattern: "*", requestStage: "Request" }] });
    browser.ws.addEventListener("message", async (event) => {
        const msg = JSON.parse(event.data);
        if (msg.method !== "Fetch.requestPaused") return;
        const { requestId, request } = msg.params;
        const write = request.method !== "GET" && BLOCKED.some((re) => re.test(request.url));
        if (write) {
            await browser.send("Fetch.fulfillRequest", {
                requestId, responseCode: 200,
                responseHeaders: [{ name: "Content-Type", value: "application/json" }],
                body: Buffer.from("{}").toString("base64") });
        } else {
            await browser.send("Fetch.continueRequest", { requestId });
        }
    });
}

class Tour {
    constructor(browser, opts) {
        this.browser = browser;
        this.opts = opts;
        this.params = opts.params;
        this.timeout = this.params.timeout || 120000;
        this.steps = [];
    }

    /** Run `action` (page-side, given args) and wait for the view it leaves. */
    async step(name, action, ...args) {
        const wallFrom = Date.now();
        const result = await this.browser.eval(async (body, ms, argv) => {
            const fn = eval(`(${body})`);
            window.__rb.hook();
            const u0 = window.__rb.updates;
            const t0 = performance.now();
            await fn(...argv);
            const r = await window.__rb.waitStep(t0, ms, u0);
            return { ...r, t0 };
        }, action.toString(), this.timeout, args);
        const record = { step: name, wallFrom, wallTo: Date.now(), ...result };
        this.steps.push(record);
        this.browser.log?.(name, result.full);
        return record;
    }

    async open(z0) {
        const { browser } = this;
        const wallFrom = Date.now();
        const query = z0 === null || z0 === undefined ? "" : `?z=${z0}`;
        await browser.navigate(`${this.opts.base}/${encodeURIComponent(this.opts.dataset)}${query}`);
        const ok = await browser.waitFor(() => Boolean(window.__rb && window.__bench?.k), 60000);
        if (!ok) throw new Error("no page");
        // The page clock starts at navigation (performance.timeOrigin).
        const result = await browser.eval(async (ms) => {
            __bench.start();
            const r = await window.__rb.waitStep(0, ms, 0);
            return { ...r, t0: 0, origin: performance.timeOrigin };
        }, Math.max(this.timeout, 300000));
        const record = { step: "open", wallFrom, wallTo: Date.now(), ...result };
        this.steps.push(record);
        this.browser.log?.("open", result.full);
        return record;
    }
}

/** Attribute requests to steps by when Chrome first saw them. */
function attribute(records, steps) {
    const out = steps.map(() => []);
    for (const r of records) {
        let index = -1;
        for (let i = 0; i < steps.length; i += 1) {
            if (r.seen >= steps[i].wallFrom) index = i;
        }
        if (index >= 0) out[index].push(r);
    }
    return out;
}

function firstRequest(records, kinds, since) {
    const hits = records.filter((r) => kinds.includes(r.kind) && r.seen >= since);
    return hits.length ? Math.min(...hits.map((r) => r.seen)) - since : null;
}

async function runPass(browser, opts, pass) {
    const p = opts.params;
    if (pass !== "C2") await browser.clearCache();
    const mark = browser.net.length;
    const tour = new Tour(browser, opts);
    const passFrom = Date.now();
    const open = await tour.open(p.z0);
    const info = await browser.eval(() => {
        const k = __bench.k;
        return { slots: k.slots().filter((s) => s.name).map((s) => s.index), enabled: k.enabled(),
                 depth: k.vs()?.depth || 1, spec: k.vs()?.spec?.() || "",
                 hd: Boolean(k.P().seaDragonViewer?.viewerManagerVMain?.isHdMode?.()),
                 size: window.__rb.imageSize(), state: window.__rb.state(),
                 routing: window.PlexoraRouting ? "present" : "absent" };
    });
    open.firstTileRequest = firstRequest(browser.netSince(mark), ["tile", "batch"], open.wallFrom);
    if (p.openOnly) return await finishPass(browser, tour, mark, pass, passFrom, info);
    const W = p.waypoints;
    const z0 = p.z0;
    const channels = p.channels || info.enabled;
    if (JSON.stringify([...channels].sort()) !== JSON.stringify([...info.enabled].sort())) {
        await tour.step("channels", (wanted) => __bench.k.setChannels(wanted), channels);
    }
    if (p.hd === false) {
        await tour.step("fast", () => __plexora.seaDragonViewer.viewerManagerVMain.setHdMode(false));
    }
    await tour.step("zoom_L2_a", (at) => __rb.place(0.25, at), W.a);
    await tour.step("zoom_L0_a", (at) => __rb.place(1, at), W.a);
    await tour.step("pan_L0_b", (at) => __rb.place(1, at), W.b);
    await tour.step("home", () => __bench.k.viewer().viewport.goHome(true));
    await tour.step("revisit_L0_a", (at) => __rb.place(1, at), W.a);
    await tour.step("jump_L0_c", (at) => __rb.place(1, at), W.c);
    await tour.step("z_plus1", (z) => __bench.k.vs().set({ z }), z0 + 1);
    await tour.step("z_plus8", (z) => __bench.k.vs().set({ z }), z0 + 9);
    await tour.step("z_plus50", (z) => __bench.k.vs().set({ z }), z0 + 59);
    // Animated: OpenSeadragon's own spring, as a drag or a scroll would move it.
    await tour.step("anim_zoom_L2", (at) => {
        const v = __bench.k.viewer();
        const item = __bench.k.channelItems(v.world).find((i) => i.getOpacity() > 0);
        v.viewport.panTo(item.imageToViewportCoordinates(at[0], at[1]), true);
        v.viewport.zoomTo(item.imageToViewportZoom(0.25));
    }, W.b);
    await tour.step("anim_pan_L2", (at) => {
        const v = __bench.k.viewer();
        const item = __bench.k.channelItems(v.world).find((i) => i.getOpacity() > 0);
        v.viewport.panTo(item.imageToViewportCoordinates(at[0], at[1]));
    }, W.c);
    let volume = null;
    if (p.volume) {
        const vfrom = Date.now();
        volume = await browser.eval(async (ms) => {
            const k = __bench.k;
            const t0 = performance.now();
            k.vs().set({ mode: "volume", layout: "single" });
            const end = t0 + ms;
            let status = null;
            while (performance.now() < end) {
                const r = k.P().volumeView?.renderer;
                status = r?.status || null;
                if (r && ["unavailable", "error"].includes(r.status)) break;
                const ready = r && (typeof r.settled === "function" ? r.settled()
                    : (r.status === "ready" && !r._loads?.size));
                if (ready && r.status === "ready") return { full: performance.now() - t0, status };
                await k.sleep(10);
            }
            return { full: null, status };
        }, p.volumeTimeout || 180000);
        volume.wallFrom = vfrom;
        volume.wallTo = Date.now();
        volume.step = "volume";
        tour.steps.push(volume);
        browser.log?.("volume", volume.full);
        await browser.eval(() => { __bench.k.vs().set({ mode: "plane" }); return true; });
    }
    return await finishPass(browser, tour, mark, pass, passFrom, info);
}

async function finishPass(browser, tour, mark, pass, passFrom, info) {
    const passTo = Date.now();
    // Frame statistics over each step's own window, then stop recording.
    const F = await browser.eval(() => __bench.period(500));
    for (const s of tour.steps) {
        if (s.step === "open" || s.step === "volume" || s.t0 === undefined) continue;
        const span = s.full ?? (s.wallTo - s.wallFrom);
        s.frames = await browser.eval((t0, t1, f) => {
            const st = __bench.k.frameStats(t0, t1, f);
            const lt = (__bench.longTasks || []).filter(([t]) => t >= t0 && t <= t1).length;
            return { ...st, longTasks: lt };
        }, s.t0, s.t0 + span, F);
    }
    await browser.eval(() => __bench.stop());
    // Let what is still in flight land before counting bytes.
    await browser.netIdle(500, 20000);
    const records = browser.netSince(mark);
    const groups = attribute(records, tour.steps);
    tour.steps.forEach((s, i) => { s.net = netSummary(groups[i]); });
    // Storage cleared for the next pass (saved view state, routing verdict).
    await browser.eval(() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) { /* none */ } return true; });
    return { pass, passFrom, passTo, info, F, steps: tour.steps, net: netSummary(records),
             requests: records.length };
}

let stdinLines = null;
/** The next line the caller writes to stdin. */
function nextLine() {
    if (!stdinLines) {
        stdinLines = { buffer: "", waiters: [] };
        process.stdin.setEncoding("utf8");
        process.stdin.on("data", (chunk) => {
            stdinLines.buffer += chunk;
            let at;
            while ((at = stdinLines.buffer.indexOf("\n")) >= 0) {
                const line = stdinLines.buffer.slice(0, at);
                stdinLines.buffer = stdinLines.buffer.slice(at + 1);
                stdinLines.waiters.shift()?.(line);
            }
        });
    }
    return new Promise((resolve) => stdinLines.waiters.push(resolve));
}

async function main() {
    const opts = parseArgs(process.argv.slice(2));
    const browser = new Browser({ headed: opts.headed });
    try {
        await browser.launch();
    } catch (error) {
        await browser.close();
        throw error;
    }
    const result = { dataset: opts.dataset, params: opts.params, started: new Date().toISOString(),
                     passes: [] };
    browser.log = (name, full) => process.stderr.write(`  ${name}: ${full === null ? "TIMEOUT" : Math.round(full) + " ms"}\n`);
    try {
        await browser.send("Page.addScriptToEvaluateOnNewDocument",
                           { source: readFileSync(path.join(HERE, "tourpage.js"), "utf8") });
        await blockWrites(browser);
        result.renderer = await browser.renderer();
        if (/swiftshader|llvmpipe|software/i.test(result.renderer || "")) {
            throw new Error(`software WebGL (${result.renderer}): refusing to measure`);
        }
        await browser.setCache(true);
        const passes = opts.params.passes || ["C1"];
        for (const [i, pass] of passes.entries()) {
            process.stderr.write(`pass ${pass}\n`);
            result.passes.push(await runPass(browser, opts, pass));
            writeFileSync(opts.out, JSON.stringify(result));
            // The caller reads its own counters between passes: say so, and
            // wait for its go-ahead (a line on stdin) before the next one.
            if (opts.params.handshake && i < passes.length - 1) {
                process.stdout.write(`PASS_DONE ${pass}\n`);
                await nextLine();
            }
        }
    } catch (error) {
        result.error = String(error?.stack || error);
    }
    result.console = browser.logs.filter((line) => /error|exception|warn/i.test(line)).slice(-40);
    writeFileSync(opts.out, JSON.stringify(result));
    await browser.close();
    process.exit(result.error ? 2 : 0);
}


main().catch((error) => {
    console.error(error);
    process.exit(1);
});
