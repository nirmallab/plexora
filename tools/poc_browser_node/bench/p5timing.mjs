/**
 * P5T: the open and tour times of the two ways a person reaches the SAME
 * remote data node from this machine -- what the investigation's P5 gate
 * compares (open C1 within 1.2x the desktop node arm, navigation within 1.2x,
 * every step full by 10 s):
 *
 *  - desktop: a Plexora here holds the project, whose image is on the node;
 *    the product page draws it, taking its tiles from the node directly
 *    (direct routing). Driven by tools/bench_remote/tour.mjs, as the
 *    remote-access campaign drove its node arm.
 *  - browser: the static page p5t.html (page/src/p5t.js) holds everything and
 *    asks the node itself: the static-site layout.
 *
 * One node, one image, one Chrome binary, the same steps, waypoints and
 * completion rule. Blocks in ABBA order. Every arm starts cold (C1): the node
 * restarted with the image's pages dropped from its page cache, a new Chrome
 * profile and, for desktop, a new Plexora; then C2 warm in the same Chrome.
 * The node keeps its data root (quantization windows, the dynamic share), as
 * a returning user's does.
 *
 *   site "o2":    bench/o2/o2_daemon.py holds the run's only ssh login; `state`
 *                 is its directory (the token is read from its file).
 *   site "local": a node on this machine (a dry run of the whole harness).
 *
 *   node bench/run.mjs --poc P5T --fixture none --label o2 \
 *     --params '{"site":"o2","state":"DIR","image":"/n/scratch/.../NLU337.ome.tif","blocks":4}'
 *
 * Each arm's row is appended to `<work>/rows.jsonl` as it ends, and a rerun
 * with "resume": true skips the rows already there -- a driver that fails
 * halfway costs no new login.
 */
import { spawn, spawnSync } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { randomBytes } from "node:crypto";
import path from "node:path";
import { POC_ROOT, PYTHON, REPO, sleep } from "./procs.mjs";

const BENCH = path.join(process.env.PLEXORA_BENCH_HOME || "C:/plexora-bench", "browser_node");
const REMOTE = path.resolve(POC_ROOT, "..", "bench_remote");
const NAV = ["zoom_L2_a", "zoom_L0_a", "pan_L0_b", "home", "revisit_L0_a", "jump_L0_c", "anim_zoom_L2", "anim_pan_L2"];

function run(argv, opts = {}) {
    const done = spawnSync(argv[0], argv.slice(1), { encoding: "utf8", cwd: REPO, maxBuffer: 64 * 1024 * 1024, ...opts });
    if (done.status !== 0) throw new Error(`${path.basename(argv[1] || argv[0])} failed: ${(done.stderr || done.stdout || "").slice(-1500)}`);
    return done.stdout.trim().split(/\r?\n/).pop();
}

/** A node in an O2 job, through bench/o2/o2_daemon.py's one login. */
class DaemonSite {
    constructor(p) {
        this.state = p.state;
        const info = JSON.parse(readFileSync(path.join(p.state, "daemon.json"), "utf8"));
        this.base = `http://127.0.0.1:${info.port}`;
        this.key = info.key;
        this.imagePath = p.image;
    }

    async call(op, body = {}) {
        const r = await fetch(`${this.base}/${op}`, { method: "POST", headers: { "X-Key": this.key, "Content-Type": "application/json" },
                                                      body: JSON.stringify(body), signal: AbortSignal.timeout(900000) });
        const out = await r.json();
        if (!r.ok || out.error) throw new Error(`daemon ${op}: ${out.error || r.status}`);
        return out;
    }

    async start() {
        const s = await this.call("status");
        if (!s.alive || !s.endpoint) throw new Error(`the O2 session is not ready: ${JSON.stringify(s)}`);
        this.endpoint = s.endpoint;
        this.token = readFileSync(s.token_file, "utf8").trim();
        this.where = { job: s.job, host: s.host };
    }

    restart(origins) { return this.call("node_start", { origins }); }
    stop() { return this.call("node_stop"); }
}

/** A node on this machine: the harness's dry run. */
class LocalSite {
    constructor(p) {
        this.port = p.localNodePort || 8766;
        this.endpoint = `http://127.0.0.1:${this.port}`;
        this.token = randomBytes(16).toString("hex");
        this.root = path.join(BENCH, "scratch", "p5t-local-node");
        this.imagePath = p.image;
        this.proc = null;
        this.where = { host: "localhost" };
    }

    async start() { mkdirSync(this.root, { recursive: true }); }

    async stop() {
        if (this.proc && this.proc.exitCode === null) {
            spawnSync("taskkill", ["/PID", String(this.proc.pid), "/T", "/F"], { stdio: "ignore" });
            await new Promise((r) => this.proc.once("exit", r));
        }
        this.proc = null;
        return {};
    }

    async restart(origins) {
        await this.stop();
        const t0 = Date.now();
        const args = ["-m", "plexora", "node", "serve", "--host", "127.0.0.1", "--port", String(this.port),
                      "--node-id", "poc-o2", "--serve", `image:img=${this.imagePath}`, "--dynamic",
                      "--manifest", path.join(this.root, "manifest.json")];
        for (const o of origins) args.push("--allow-origin", o);
        this.proc = spawn(PYTHON, args, { cwd: REPO, stdio: "ignore",
            env: { ...process.env, PYTHONPATH: REPO, PLEXORA_DATA_PATH: this.root, PLEXORA_NODE_TOKEN: this.token, PLEXORA_TELEMETRY: "off" } });
        for (;;) {
            try {
                const r = await fetch(`${this.endpoint}/node/v1/health`, { headers: { "X-Plexora-Node-Token": this.token } });
                if (r.ok) break;
            } catch (e) { /* not yet */ }
            if (Date.now() - t0 > 120000) throw new Error("local node never answered");
            await sleep(200);
        }
        return { health_s: (Date.now() - t0) / 1000 };
    }
}

async function nodeGet(site, p) {
    const r = await fetch(`${site.endpoint}/node/v1/${p}`, { headers: { "X-Plexora-Node-Token": site.token } });
    if (!r.ok) throw new Error(`${p} -> ${r.status}`);
    return r.json();
}

/** /health round trips from this machine, outside any browser. */
async function rtt(site, n = 10) {
    const out = [];
    for (let i = 0; i < n; i += 1) {
        const t0 = performance.now();
        await fetch(`${site.endpoint}/node/v1/health`, { headers: { "X-Plexora-Node-Token": site.token } });
        out.push(performance.now() - t0);
        await sleep(50);
    }
    out.sort((a, b) => a - b);
    return { p50: out[n >> 1], min: out[0], max: out[n - 1] };
}

/** Wait until every channel's window on `ids` is the exact one (the node's
 *  background scan has landed); a primary that asked earlier would keep the
 *  provisional one for its whole life. */
async function bankWindows(site, ids, channels, timeoutMs = 1800000) {
    const t0 = Date.now();
    const out = {};
    for (const id of ids) {
        for (const c of channels) {
            for (;;) {
                const q = await nodeGet(site, `image/${encodeURIComponent(id)}/quantization?channel_index=${c}`);
                if (q.exact) { out[`${id}/${c}`] = Date.now() - t0; break; }
                if (Date.now() - t0 > timeoutMs) throw new Error(`window ${id}/${c} never exact`);
                await sleep(2000);
            }
        }
    }
    return out;
}

class Primary {
    constructor(data, port) { this.data = data; this.port = port; this.info = null; }
    get base() { return `http://127.0.0.1:${this.port}`; }
    start() {
        const t0 = Date.now();
        this.info = JSON.parse(run([PYTHON, path.resolve(POC_ROOT, "..", "bench3d", "serve.py"), "--data", this.data,
                                    "--port", String(this.port)]));
        return (Date.now() - t0) / 1000;
    }
    stop() {
        if (this.info) spawnSync(PYTHON, [path.resolve(POC_ROOT, "..", "bench3d", "serve.py"), "--stop", String(this.info.pid)]);
        this.info = null;
    }
}

function tour(primary, dataset, params, out, token) {
    return new Promise((resolve) => {
        const child = spawn(process.execPath, [path.join(REMOTE, "tour.mjs"), "--base", primary.base, "--dataset", dataset,
                                               "--out", out, "--params", JSON.stringify(params)],
                            { stdio: ["ignore", "ignore", "pipe"], cwd: REMOTE });
        let stderr = "";
        child.stderr.on("data", (d) => { stderr += d; });
        child.on("close", (status) => {
            let result = null;
            try { result = JSON.parse(readFileSync(out, "utf8").split(token).join("<token>")); } catch (e) { /* none */ }
            writeFileSync(out, JSON.stringify(result));
            resolve({ status, stderr: stderr.split(token).join("<token>"), result });
        });
    });
}

const stepRows = (steps) => (steps || []).filter((s) => !/^z_/.test(s.step))
    .map((s) => ({ step: s.step, full: s.full ?? null, firstPixel: s.firstPixel ?? null }));

async function desktopArm(ctx, p, site, primary, dataset, wp, work, block) {
    const row = { arm: "desktop" };
    primary.stop();
    row.primaryStartS = primary.start();
    const out = path.join(work, `tour-b${block}-${Date.now()}.json`);
    const t = await tour(primary, dataset, { passes: ["C1", "C2"], channels: p.slots, waypoints: wp, timeout: p.timeoutMs },
                         out, site.token);
    primary.stop();
    if (!t.result || t.result.error) throw new Error(`tour.mjs: ${t.result?.error || t.stderr.slice(-800)}`);
    row.renderer = t.result.renderer;
    for (const pass of t.result.passes) {
        const open = pass.steps.find((s) => s.step === "open");
        row[pass.pass] = {
            steps: stepRows(pass.steps), info: { enabled: pass.info.enabled, hd: pass.info.hd },
            openNet: open?.net?.byOrigin || null, firstTileRequestMs: open?.firstTileRequest ?? null,
            requests: pass.requests,
        };
    }
    return row;
}

async function browserArm(ctx, p, site, stat, wp, block) {
    const row = { arm: "browser" };
    const browser = await ctx.launchChrome({ unthrottled: false });
    try {
        const args = { node: { endpoint: site.endpoint, token: site.token, imageId: "img" },
                       open: p.openChannels, add: p.addChannels, waypoints: wp, size: p.size, timeoutMs: p.timeoutMs };
        const url = `${stat.origin}/poc/p5t.html#p5t=${encodeURIComponent(JSON.stringify(args))}`;
        for (const pass of ["C1", "C2"]) {
            if (pass === "C2") await browser.page.goto("about:blank");
            await browser.page.goto(url);
            await browser.page.waitForFunction(() => window.__p5t?.done, null, { timeout: 15 * 60000, polling: 250 });
            const r = await browser.page.evaluate(() => window.__p5t);
            if (r.error) throw new Error(`p5t.js: ${r.error}`);
            row.renderer ||= await browser.page.evaluate(() => {
                const gl = document.createElement("canvas").getContext("webgl2");
                const ext = gl?.getExtension("WEBGL_debug_renderer_info");
                return ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null;
            });
            row[pass] = { steps: stepRows(r.result.steps), open: r.result.steps[0], metadataRequests: r.result.metadataRequests,
                          pageLoadMs: r.result.pageLoadMs, tiles: r.result.tiles, canvas: r.result.canvas };
        }
        row.logs = browser.logs.filter((l) => !l.includes(site.token)).slice(-10);
    } finally {
        await browser.close();
    }
    return row;
}

const median = (v) => {
    const s = v.filter((x) => x != null).sort((a, b) => a - b);
    if (!s.length) return null;
    const m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const geo = (v) => (v.length ? Math.exp(v.reduce((a, x) => a + Math.log(x), 0) / v.length) : null);

/** Per pass: each step's median per arm, the per-block paired ratios browser / desktop. */
function summarise(rows, pass) {
    const blocks = [...new Set(rows.map((r) => r.block))].sort((a, b) => a - b);
    const pair = blocks.map((b) => ({ d: rows.find((r) => r.block === b && r.arm === "desktop"),
                                      w: rows.find((r) => r.block === b && r.arm === "browser") }))
        .filter((x) => x.d?.[pass] && x.w?.[pass]);
    const names = pair[0]?.d[pass].steps.map((s) => s.step) || [];
    const full = (row, step) => row[pass].steps.find((s) => s.step === step)?.full ?? null;
    const per = names.map((step) => {
        const ratios = pair.map(({ d, w }) => (full(d, step) > 0 && full(w, step) > 0 ? full(w, step) / full(d, step) : null))
            .filter((x) => x != null);
        return { step, desktop: median(pair.map(({ d }) => full(d, step))), browser: median(pair.map(({ w }) => full(w, step))),
                 ratioMedian: median(ratios), ratios };
    });
    // Navigation: per block, the geometric mean over the tour's steps of
    // browser / desktop; then the median over blocks.
    const navPerBlock = pair.map(({ d, w }) => geo(NAV.map((s) => (full(d, s) > 0 && full(w, s) > 0 ? full(w, s) / full(d, s) : null))
        .filter((x) => x != null)));
    const all = pair.flatMap(({ d, w }) => [...d[pass].steps, ...w[pass].steps]);
    return {
        blocks: pair.length, per, open: per.find((x) => x.step === "open"),
        navGeoRatioPerBlock: navPerBlock, navGeoRatioMedian: median(navPerBlock),
        stepsFull: all.filter((s) => s.full != null).length, steps: all.length,
        stepsFullBy10s: all.filter((s) => s.full != null && s.full <= 10000).length,
    };
}

export async function P5T(ctx) {
    const p = { site: "local", blocks: 4, primaryPort: 8661, pagePort: 8650, timeoutMs: 120000, size: [1060, 855],
                name: "p5t", first: "desktop", label: ctx.args.label || "local", ...(ctx.args.params || {}) };
    if (!p.image) throw new Error("P5T needs --params image (the image's path on the node)");
    const desktopOrigin = `http://127.0.0.1:${p.primaryPort}`;
    const pageOrigin = `http://127.0.0.1:${p.pagePort}`;
    const site = p.site === "o2" ? new DaemonSite(p) : new LocalSite(p);
    await site.start();
    const work = path.join(BENCH, "p5t", p.label);
    const rowsFile = path.join(work, "rows.jsonl");
    const setupFile = path.join(work, "setup.json");
    const data = path.join(work, "data");
    const primary = new Primary(data, p.primaryPort);
    const stat = await ctx.startStatic({ port: p.pagePort });
    const logs = [];
    let setup;
    try {
        if (p.resume && existsSync(setupFile)) {
            setup = JSON.parse(readFileSync(setupFile, "utf8"));
            site.firstStart = setup.firstStart;
        } else {
            rmSync(work, { recursive: true, force: true });
            mkdirSync(work, { recursive: true });
            setup = { firstStart: await site.restart([desktopOrigin, pageOrigin]) };
            ctx.log(`node up: ${JSON.stringify(setup.firstStart)}`);
            const geometry = await nodeGet(site, "image/img/geometry");
            setup.geometry = { width: geometry.width, height: geometry.height, levels: geometry.levels,
                               channels: geometry.num_channels, names: geometry.channel_names };
            const made = JSON.parse(run([PYTHON, path.join(POC_ROOT, "bench", "p5t_project.py"), "--data", data,
                                         "--node", "poc-o2", "--endpoint", site.endpoint, "--image", site.imagePath,
                                         "--name", p.name],
                                        { env: { ...process.env, PYTHONPATH: REPO, POC_NODE_TOKEN: site.token } }));
            setup.project = { name: made.name, channels: made.channels, resource: made.resources?.image?.resource_id };
            const all = Array.from({ length: geometry.num_channels }, (_, c) => c);
            setup.windowsExactMs = await bankWindows(site, ["img", setup.project.resource], all);
            ctx.log(`windows exact: ${JSON.stringify(setup.windowsExactMs)}`);
            // A picture of the image through the product, for waypoints on tissue.
            primary.start();
            const survey = path.join(work, "survey.json");
            run([process.execPath, path.join(REMOTE, "survey.mjs"), "--base", primary.base, "--dataset", p.name,
                 "--out", survey, "--params", JSON.stringify({ z0: 0, channel: 0 })], { cwd: REMOTE });
            run([PYTHON, path.join(REMOTE, "waypoints.py"), survey, "--sets", String(p.blocks), "--out",
                 path.join(work, "waypoints.json")]);
            // What the product opens with, and which image channel each slot is.
            const probe = await tour(primary, p.name, { passes: ["C1"], openOnly: true, timeout: p.timeoutMs },
                                     path.join(work, "probe.json"), site.token);
            primary.stop();
            if (!probe.result?.passes?.length) throw new Error(`probe: ${probe.result?.error || probe.stderr.slice(-800)}`);
            const info = probe.result.passes[0].info;
            const channelOf = Object.fromEntries(info.channelOf);
            const slots = p.channels || info.slots.slice(0, 3);
            if (!info.enabled.every((s) => slots.includes(s))) {
                throw new Error(`the product opens slots ${info.enabled}, not all among the tour's ${slots}`);
            }
            setup.slots = slots;
            setup.openChannels = info.enabled.map((s) => channelOf[s]);
            setup.addChannels = slots.filter((s) => !info.enabled.includes(s)).map((s) => channelOf[s]);
            setup.channelOf = info.channelOf;
            setup.probeOpenMs = probe.result.passes[0].steps[0].full;
            writeFileSync(setupFile, JSON.stringify(setup, null, 1));
        }
        Object.assign(p, { slots: setup.slots, openChannels: setup.openChannels, addChannels: setup.addChannels });
        const sets = JSON.parse(readFileSync(path.join(work, "waypoints.json"), "utf8")).sets;
        const done = existsSync(rowsFile)
            ? readFileSync(rowsFile, "utf8").split(/\r?\n/).filter(Boolean).map((l) => JSON.parse(l)) : [];
        const second = p.first === "desktop" ? "browser" : "desktop";
        for (let block = 0; block < p.blocks; block += 1) {
            const order = block % 2 === 0 ? [p.first, second] : [second, p.first];
            const set = sets[block % sets.length];
            const wp = { a: set.a, b: set.b, c: set.c };
            for (const arm of order) {
                if (done.some((r) => r.block === block && r.arm === arm && !r.error)) continue;
                ctx.log(`block ${block} ${arm}`);
                let row = { block, arm, order: order.indexOf(arm), waypoints: wp, started: new Date().toISOString() };
                try {
                    row.restart = await site.restart([desktopOrigin, pageOrigin]);
                    row.rttMs = await rtt(site);
                    Object.assign(row, arm === "desktop"
                        ? await desktopArm(ctx, p, site, primary, p.name, wp, work, block)
                        : await browserArm(ctx, p, site, stat, wp, block));
                } catch (error) {
                    row.error = String(error?.stack || error).split(site.token).join("<token>").slice(0, 2000);
                }
                row.finished = new Date().toISOString();
                appendFileSync(rowsFile, JSON.stringify(row) + "\n");
                ctx.log(`  ${row.error ? "ERROR " + row.error.slice(0, 300)
                    : ["C1", "C2"].map((pass) => `${pass} ${row[pass].steps.map((s) => (s.full == null ? "x" : Math.round(s.full))).join(" ")}`).join(" | ")}`);
                done.push(row);
            }
        }
    } finally {
        primary.stop();
        if (p.site === "local") await site.stop();
    }
    const rows = readFileSync(rowsFile, "utf8").split(/\r?\n/).filter(Boolean).map((l) => JSON.parse(l))
        .filter((r) => !r.error);
    const failed = readFileSync(rowsFile, "utf8").split(/\r?\n/).filter(Boolean).map((l) => JSON.parse(l))
        .filter((r) => r.error).map((r) => ({ block: r.block, arm: r.arm, error: r.error.slice(0, 400) }));
    const C1 = summarise(rows, "C1");
    const C2 = summarise(rows, "C2");
    const desktopRow = rows.find((r) => r.arm === "desktop");
    const browserRow = rows.find((r) => r.arm === "browser");
    return {
        browser: { renderer: browserRow?.renderer || desktopRow?.renderer || null },
        arm: "desktop (Plexora here + product page, tiles node-direct) vs browser (static page, everything node-direct)",
        state: "ABBA blocks; each arm C1 cold (node restarted, image pages dropped from its page cache, new Chrome profile, new Plexora for desktop) then C2 warm",
        metrics: {
            site: p.site, where: site.where, setup: { ...setup, firstStart: undefined }, firstStart: setup.firstStart,
            slots: setup.slots, openChannels: setup.openChannels, addChannels: setup.addChannels,
            C1, C2, rows, failed,
        },
        checks: {
            blocks: C1.blocks,
            openRatioC1: C1.open?.ratioMedian ?? null,
            navRatioC1: C1.navGeoRatioMedian,
            openRatioC2: C2.open?.ratioMedian ?? null,
            navRatioC2: C2.navGeoRatioMedian,
            everyStepFull: C1.stepsFull === C1.steps && C2.stepsFull === C2.steps,
            everyStepFullBy10s: C1.stepsFullBy10s === C1.steps && C2.stepsFullBy10s === C2.steps,
            armsFailed: failed.length,
        },
        logs,
    };
}
