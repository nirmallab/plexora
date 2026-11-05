// One handler per POC: what to start, what to call in the page, what to keep.
// Each returns {metrics, checks, browser, arm, state, notes, logs}.
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { POC_ROOT, PYTHON, freePort, peakMemory, sleep } from "./procs.mjs";

/** Static origin + Chrome + the POC page with `files` picked. */
export async function openPage(ctx, { files = [], staticOptions = {}, chrome = {}, pagePath = "poc/index.html",
                                       origin = null } = {}) {
    const stat = await ctx.startStatic(staticOptions);
    const browser = await ctx.launchChrome({ headed: ctx.args.headed, ...chrome });
    const base = origin ? origin.replace("PORT", String(stat.port)) : stat.origin;
    await browser.page.goto(`${base}/${pagePath}`);
    await browser.page.waitForFunction(() => window.POC && document.getElementById("status").textContent === "ready",
                                       null, { timeout: 60000 });
    if (files.length) await browser.page.setInputFiles("#picker", files);
    const env = await browser.page.evaluate(() => window.POC.env());
    if (!env.gpu || /swiftshader|llvmpipe|software/i.test(env.gpu)) {
        ctx.log(`WARNING: software WebGL (${env.gpu}); timings are not comparable`);
    }
    return { stat, browser, env, page: browser.page, origin: base };
}

const imageServe = (fixture) => [`image:img=${fixture.image}`];

/**
 * Channel windows (the node's own vmin/vmax hints) for a fixture, fetched once
 * and kept beside the bench's other scratch: both viewer arms draw with the
 * same windows, and no browser block needs a node running beside it.
 */
/** bench/truth_table.py's answers for a fixture's table, made once. */
const CACHE = path.join(process.env.PLEXORA_BENCH_HOME || "C:/plexora-bench", "browser_node", "cache");

function cachedTruth(ctx, fixture) {
    const dir = CACHE;
    mkdirSync(dir, { recursive: true });
    const file = path.join(dir, `truth-table-${ctx.fixtureName}.json`);
    if (!existsSync(file) || JSON.parse(readFileSync(file, "utf8")).csv !== fixture.table) {
        const r = fixture.tableRoles;
        const run = spawnSync(PYTHON, ["-I", path.join(POC_ROOT, "bench", "truth_table.py"), "--csv", fixture.table,
                                       "--cell-id", r.cell_id, "--x", r.x, "--y", r.y, "--out", file], { encoding: "utf8" });
        if (run.status !== 0) throw new Error(`truth_table.py failed: ${run.stderr}`);
        ctx.log(run.stdout.trim());
    }
    return JSON.parse(readFileSync(file, "utf8"));
}

/** POST the table's read spec, as the primary would (inspect's proposal + the roles). */
async function loadTableOnNode(node, id, roles) {
    const get = (p) => fetch(`${node.endpoint}/node/v1/${p}${p.includes("?") ? "&" : "?"}t=${node.token}`).then((r) => r.json());
    const inspected = await get(`table/${id}/inspect`);
    const spec = { ...(inspected.proposed || {}), type: inspected.data_type || "csv", src: "node",
                   roles: { ...(inspected.proposed?.roles || {}), ...roles } };
    const r = await fetch(`${node.endpoint}/node/v1/table/${id}/load`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-Plexora-Node-Token": node.token },
        body: JSON.stringify({ spec }) });
    if (!r.ok) throw new Error(`table load ${r.status}: ${await r.text()}`);
}

/** Python's own pyramid of a flat mask (bench/truth_mask.py), made once and timed. */
function cachedPythonPyramid(ctx, flat) {
    mkdirSync(CACHE, { recursive: true });
    const out = path.join(CACHE, `py-pyramid-${path.basename(flat).replace(/\.[^.]+$/, "")}.labels.pyramid.ome.tiff`);
    const record = `${out}.json`;
    if (existsSync(out) && existsSync(record)) return JSON.parse(readFileSync(record, "utf8"));
    const run = spawnSync(PYTHON, [path.join(POC_ROOT, "bench", "truth_mask.py"), "--mask", flat, "--out", out],
                          { encoding: "utf8", cwd: path.resolve(POC_ROOT, "..", "..") });
    if (run.status !== 0) throw new Error(`truth_mask.py failed: ${run.stderr.slice(-2000)}`);
    const lines = run.stdout.trim().split(/\r?\n/);
    const result = { ...JSON.parse(lines[lines.length - 1]), out };
    writeFileSync(record, JSON.stringify(result));
    ctx.log(`python pyramid: ${JSON.stringify(result)}`);
    return result;
}

/** bench/selfsigned.py's certificate for 127.0.0.1, made once. */
function selfSignedCert() {
    const dir = path.join(CACHE, "tls");
    const run = spawnSync(PYTHON, ["-I", path.join(POC_ROOT, "bench", "selfsigned.py"), dir], { encoding: "utf8" });
    if (run.status !== 0) throw new Error(`selfsigned.py failed: ${run.stderr}`);
    return { cert: path.join(dir, "cert.pem"), key: path.join(dir, "key.pem") };
}

async function cachedRanges(ctx, fixture, count) {
    const dir = CACHE;
    mkdirSync(dir, { recursive: true });
    const file = path.join(dir, `ranges-${ctx.fixtureName}.json`);
    if (existsSync(file)) {
        const kept = JSON.parse(readFileSync(file, "utf8"));
        if (kept.image === fixture.image && kept.ranges.length >= count) return kept.ranges.slice(0, count);
    }
    const node = await ctx.startNode({ serve: imageServe(fixture) });
    const ranges = await nodeRanges(node, count);
    node.stop();
    writeFileSync(file, JSON.stringify({ image: fixture.image, ranges }, null, 1));
    return ranges;
}

/** The channel windows the node itself would level with (vmin/vmax hints). */
async function nodeRanges(node, count) {
    const out = [];
    for (let c = 0; c < count; c += 1) {
        const r = await fetch(`${node.endpoint}/node/v1/image/img/stats?channel_index=${c}&t=${node.token}`);
        const s = await r.json();
        out.push([s.vmin_hint / 65535, Math.max(s.vmin_hint + 1, s.vmax_hint) / 65535]);
    }
    return out;
}

export const HANDLERS = {
    /** P0: the rendering layer from a static page; byte order; shader; picture identity. */
    async P0(ctx) {
        const { fixture } = ctx;
        const page = await openPage(ctx, { files: [fixture.image] });
        const node = await ctx.startNode({ serve: imageServe(fixture), allowOrigins: [page.origin] });
        const nodeArg = { endpoint: node.endpoint, token: node.token, imageId: "img" };
        const image = { file: path.basename(fixture.image) };
        const p0 = await page.page.evaluate((a) => window.POC.p0(a), { node: nodeArg, image });
        const ranges = await nodeRanges(node, 3);
        const channels = ranges.map((range, i) => ({ index: i, color: ["#0000ff", "#00ff00", "#ff0000"][i], range }));
        const pictures = await page.page.evaluate((a) => window.POC.compareArms(a), { node: nodeArg, image, channels });
        await page.browser.close();
        node.stop();
        // The static half: which client scripts assume the same-origin server.
        const auditFile = path.join(tmpdir(), `p0-audit-${process.pid}.json`);
        spawnSync(process.execPath, [path.join(POC_ROOT, "bench", "p0_audit.mjs"), "--out", auditFile], { stdio: "ignore" });
        const audit = JSON.parse(readFileSync(auditFile, "utf8"));
        return {
            browser: page.env,
            metrics: { nodeReadyMs: node.readyMs, pictures, audit },
            checks: {
                productModulesLoaded: Object.entries(p0.globals).filter(([, t]) => t !== "undefined").map(([k]) => k),
                productModulesMissing: Object.entries(p0.globals).filter(([, t]) => t === "undefined").map(([k]) => k),
                shims: p0.shims,
                byteOrder: p0.byteOrder,
                shader: p0.shader,
                base: p0.base,
                shaderUrl: p0.shaderUrl,
                picturesIdentical: pictures.every((p) => p.differing === 0 && p.sameSize && p.fullA && p.fullB && p.nonBlack > 0),
            },
            logs: page.browser.logs,
        };
    },

    /** P1 (tiles): open, per-tile latency and throughput by pool size, decoders, identity, auto-level. */
    async P1(ctx) {
        const { fixture } = ctx;
        const page = await openPage(ctx, { files: [fixture.image] });
        const node = await ctx.startNode({ serve: imageServe(fixture), allowOrigins: [page.origin] });
        const nodeArg = { endpoint: node.endpoint, token: node.token, imageId: "img" };
        const image = { file: path.basename(fixture.image) };
        const params = { poolSizes: [1, 2, 4, 8, page.env.hardwareConcurrency], count: 200, ...(ctx.args.params || {}) };
        const out = await page.page.evaluate((a) => window.POC.p1Tiles(a), { node: nodeArg, image, ...params });
        // The node's own cost for the same tiles, for the record (Server-Timing is
        // not needed: the node is local, so a fetch's time is its cost).
        await page.browser.close();
        node.stop();
        return {
            browser: page.env,
            arm: "browser",
            metrics: { geometry: out.geometry, open: out.open, bench: out.bench, decoders: out.decoders,
                       autoLevel: out.autoLevel },
            checks: { identity: out.identity },
            logs: page.browser.logs,
        };
    },

    /**
     * P1 (viewer): the same renderer fed by the node (today's direct route) and
     * by the browser node, ABBA, at 3/7/15 channels: open, home, an 11-step
     * tour to full, and a mid-zoom pan.
     */
    async P1V(ctx) {
        const { fixture } = ctx;
        const params = { channelCounts: [3, 7, 15], tour: 11, order: ["node", "browser", "browser", "node"],
                         warmup: true, ...(ctx.args.params || {}) };
        const maxCh = Math.max(...params.channelCounts);
        const channelRanges = await cachedRanges(ctx, fixture, maxCh);
        const blocks = [];
        let env = null;
        const logs = [];
        const order = params.warmup ? ["warmup", ...params.order] : params.order;
        for (const arm of order) {
            // A fresh browser profile per block: no tile, GPU or HTTP cache
            // carried over. A fresh node per node block. No node at all in a
            // browser block, so nothing else competes for the CPU.
            const page = await openPage(ctx, { files: [fixture.image] });
            env ||= page.env;
            const feed = arm === "warmup" ? "node" : arm;
            const node = feed === "node"
                ? await ctx.startNode({ serve: imageServe(fixture), allowOrigins: [page.origin] }) : null;
            const nodeArg = node ? { endpoint: node.endpoint, token: node.token, imageId: "img" }
                : { endpoint: "http://0.0.0.0:9", token: "none", imageId: "img" };
            ctx.log(`block ${arm}`);
            const out = await page.page.evaluate((a) => window.POC.p1Viewer(a), {
                arm: feed, node: nodeArg, image: { file: path.basename(fixture.image) },
                channelCounts: arm === "warmup" ? [maxCh] : params.channelCounts, tour: params.tour, channelRanges,
            });
            if (arm !== "warmup") blocks.push({ arm, nodeReadyMs: node?.readyMs ?? null, ...out });
            logs.push(...page.browser.logs.slice(-10));
            await page.browser.close();
            node?.stop();
        }
        return {
            browser: env, arm: "node,browser",
            state: "OS file cache warmed by one unrecorded node block; then a fresh Chrome profile and (node arm) a fresh node per block",
            metrics: { order: params.order, channelRanges, blocks }, checks: {}, logs,
        };
    },

    /**
     * P3: a CSV/Parquet table in DuckDB-Wasm -> Parquet in OPFS; the five
     * questions vs the node and vs Python truth; then the same profile
     * relaunched to time a reopen from OPFS (DuckDB start included).
     */
    async P3(ctx) {
        const { fixture } = ctx;
        const roles = fixture.tableRoles;
        const truth = cachedTruth(ctx, fixture);
        const profile = mkdtempSync(path.join(tmpdir(), "plx-poc-p3-"));
        const port = await freePort();
        const page = await openPage(ctx, { files: [fixture.table], chrome: { profile }, staticOptions: { port } });
        const node = await ctx.startNode({ serve: [`table:cells=${fixture.table}`], allowOrigins: [page.origin] });
        const args = { file: path.basename(fixture.table), roles,
                       node: { endpoint: node.endpoint, token: node.token, tableId: "cells" }, truth, cold: true,
                       ...(ctx.args.params || {}) };
        const first = await page.page.evaluate((a) => window.POC.p3(a), args);
        const peak = peakMemory(page.browser.userDataDir);
        const logs = [...page.browser.logs];
        await page.browser.close();
        node.stop();
        // Relaunch on the same profile: what a user reopening the page tomorrow pays.
        page.stat.proc.kill();
        const again = await openPage(ctx, { files: [fixture.table], chrome: { profile }, staticOptions: { port } });
        const reopen = await again.page.evaluate((a) => window.POC.p3Reopen(a),
                                                  { file: path.basename(fixture.table), roles, store: args.store || "columns" });
        logs.push(...again.browser.logs);
        await again.browser.close();
        try { rmSync(profile, { recursive: true, force: true }); } catch (e) { /* busy */ }
        const { nearest, rois, gates, ...truthSummary } = truth;
        return {
            browser: page.env, arm: "browser",
            state: "cold: OPFS cleared, fresh profile; reopen: same profile, Chrome relaunched",
            metrics: { ...first.timings, store: first.store, reopenMs: reopen.loadMs, reopenReused: reopen.reused, table: first.table,
                       memory: { ...first.memory, peakWorkingSetBytes: peak }, storage: first.storage,
                       python: { polarsReadMs: truthSummary.polarsReadMs, kdtreeBuildMs: truthSummary.kdtreeBuildMs,
                                 nearest200Ms: truthSummary.nearest200Ms, roi20Ms: truthSummary.roi20Ms } },
            checks: first.checks,
            fixtureInfo: { rows: truthSummary.rows, columns: truthSummary.columns },
            logs,
        };
    },

    /** P3b: where cold CSV ingest time goes (DuckDB variants vs a parallel parse). */
    async P3B(ctx) {
        const { fixture } = ctx;
        const page = await openPage(ctx, { files: [fixture.table] });
        const out = await page.page.evaluate((a) => window.POC.p3Ingest(a), {
            file: path.basename(fixture.table), roles: fixture.tableRoles, ...(ctx.args.params || {}),
        });
        const peak = peakMemory(page.browser.userDataDir);
        const logs = [...page.browser.logs];
        await page.browser.close();
        const { checks, ...metrics } = out;
        return { browser: page.env, arm: "browser", metrics: { ...metrics, peakWorkingSetBytes: peak }, checks, logs };
    },

    /**
     * P2: a label pyramid through the GPU cell layer, fed by the node (today)
     * and by geotiff.js (browser node), ABBA; a 40-tick gate drag each.
     */
    async P2(ctx) {
        const { fixture } = ctx;
        const roles = fixture.tableRoles;
        const marker = (ctx.args.params && ctx.args.params.marker) || fixture.marker || "CD45";
        const order = (ctx.args.params && ctx.args.params.order) || ["node", "browser", "browser", "node"];
        const files = [fixture.mask, fixture.table, ...(fixture.image ? [fixture.image] : [])];
        const blocks = [];
        let env = null;
        const logs = [];
        for (const arm of order) {
            const page = await openPage(ctx, { files });
            env ||= page.env;
            const serve = [`segmentation:seg=${fixture.mask}`, `table:cells=${fixture.table}`,
                           ...(fixture.image ? [`image:img=${fixture.image}`] : [])];
            const node = await ctx.startNode({ serve, allowOrigins: [page.origin] });
            await loadTableOnNode(node, "cells", roles);
            const channels = fixture.image ? (await cachedRanges(ctx, fixture, 2)).map((range, i) => ({ index: i, color: ["#0000ff", "#00ff00"][i], range })) : [];
            ctx.log(`block ${arm}`);
            const out = await page.page.evaluate((a) => window.POC.p2Viewer(a), {
                arm, image: fixture.image ? path.basename(fixture.image) : null, mask: path.basename(fixture.mask),
                node: { endpoint: node.endpoint, token: node.token, imageId: "img", segId: "seg", tableId: "cells" },
                channels, roles, marker, identityTiles: arm === "browser" && blocks.every((b) => b.arm !== "browser") ? 200 : 0,
            });
            blocks.push(out);
            logs.push(...page.browser.logs.slice(-10));
            await page.browser.close();
            node.stop();
        }
        const identity = blocks.find((b) => b.labelIdentity)?.labelIdentity || null;
        return { browser: env, arm: "node,browser", state: "fresh profile and node per block", metrics: { marker, blocks },
                 checks: { labelIdentity: identity, labelGpuActive: blocks.every((b) => b.labelGpu.active),
                           canvasesAgree: new Set(blocks.map((b) => b.canvas)).size === 1 }, logs };
    },

    /**
     * P2b: a flat mask -> label pyramid in a worker into OPFS; Python's own
     * conversion timed on the same file; 200 derived tiles compared; the same
     * profile relaunched to time the first tile after a reopen.
     */
    async P2B(ctx) {
        const { fixture } = ctx;
        const flat = fixture.flatMask;
        const python = cachedPythonPyramid(ctx, flat);
        const profile = mkdtempSync(path.join(tmpdir(), "plx-poc-p2b-"));
        const port = await freePort();
        const page = await openPage(ctx, { files: [flat], chrome: { profile }, staticOptions: { port } });
        const build = await page.page.evaluate((a) => window.POC.p2bBuild(a), { mask: path.basename(flat) });
        const peak = peakMemory(page.browser.userDataDir);
        const node = await ctx.startNode({ serve: [`segmentation:seg=${python.out}`], allowOrigins: [page.origin] });
        const identity = await page.page.evaluate((a) => window.POC.p2bIdentity(a), {
            mask: path.basename(flat), node: { endpoint: node.endpoint, token: node.token, segId: "seg" } });
        node.stop();
        const logs = [...page.browser.logs];
        await page.browser.close();
        page.stat.proc.kill();
        const again = await openPage(ctx, { files: [flat], chrome: { profile }, staticOptions: { port } });
        const reopen = await again.page.evaluate((a) => window.POC.p2bReopen(a), { mask: path.basename(flat) });
        logs.push(...again.browser.logs);
        await again.browser.close();
        try { rmSync(profile, { recursive: true, force: true }); } catch (e) { /* busy */ }
        return {
            browser: page.env, arm: "browser", state: "fresh profile; reopen = same profile relaunched",
            metrics: { build: { ...build, peakWorkingSetBytes: peak }, reopen, python },
            checks: { identity }, logs,
        };
    },

    /**
     * P5: an existing node from a static origin -- (a) the protocol end to end
     * against the same requests made outside the browser, (d) COOP/COEP,
     * (e) restart recovery, (f) the --allow-origin spellings, (g) connector
     * pairing (stub), (h) the node serving the app under an /rnode/ prefix.
     */
    async P5(ctx) {
        const { fixture } = ctx;
        const roles = fixture.tableRoles;
        const serve = [`image:img=${fixture.image}`, `table:cells=${fixture.table}`];
        const out = {};
        const checks = {};
        const logs = [];
        const ready = (page) => page.waitForFunction(() => window.POC && document.getElementById("status").textContent === "ready", null, { timeout: 60000 });
        const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");

        // -- (a) protocol, then (e) restart on the same page ------------------
        const port = await freePort();
        const stat = await ctx.startStatic({ port });
        const origin = `http://127.0.0.1:${port}`;
        const nodePort = await freePort();
        let node = await ctx.startNode({ serve, allowOrigins: [origin], port: nodePort });
        const token = node.token;
        const geometry = await (await fetch(`${node.endpoint}/node/v1/image/img/geometry?t=${token}`)).json();
        let seed = 17;
        const rand = () => ((seed = (seed * 1103515245 + 12345) >>> 0) / 4294967296);
        const tiles = Array.from({ length: 20 }, () => {
            const l = Math.floor(rand() * geometry.levels);
            const [h, w] = geometry.level_shapes[l];
            return { c: Math.floor(rand() * geometry.num_channels), l,
                     x: Math.floor(rand() * Math.ceil(w / 1024)), y: Math.floor(rand() * Math.ceil(h / 1024)) };
        });
        const browser = await ctx.launchChrome({ headed: ctx.args.headed });
        await browser.page.goto(`${origin}/poc/index.html`);
        await ready(browser.page);
        const env = await browser.page.evaluate(() => window.POC.env());
        const nodeArg = { endpoint: node.endpoint, token, imageId: "img", tableId: "cells" };
        out.a = await browser.page.evaluate((a) => window.POC.p5Protocol(a), { node: nodeArg, tiles, roles });
        // The same requests outside the browser: what the desktop primary
        // receives and forwards unchanged (data_model forwards node bytes as is).
        const ref = { hd: [], webp: [] };
        for (const t of tiles) {
            for (const hd of [true, false]) {
                const q = hd ? "&q=hd" : "";
                const r = await fetch(`${node.endpoint}/node/v1/image/img/tile/img_${t.c}/${t.l}/${t.x}_${t.y}.png?tw=1024&th=1024${q}`,
                                      { headers: { "X-Plexora-Node-Token": token } });
                ref[hd ? "hd" : "webp"].push(sha(Buffer.from(await r.arrayBuffer())));
            }
        }
        const idColumns = [roles.cell_id, roles.x, roles.y].join(",");
        const allCells = await fetch(`${node.endpoint}/node/v1/table/cells/all_cells?columns=${idColumns}&dtype=integer`,
                                     { headers: { "X-Plexora-Node-Token": token } });
        const describe = await fetch(`${node.endpoint}/node/v1/table/cells/describe`, { headers: { "X-Plexora-Node-Token": token } });
        checks.a = {
            hdTilesIdentical: out.a.tiles.hd.every((t, i) => t.sha256 === ref.hd[i]),
            webpTilesIdentical: out.a.tiles.webp.every((t, i) => t.sha256 === ref.webp[i]),
            batchMatchesSingles: out.a.batchMatchesSingles,
            allCellsIdentical: out.a.allCellsSha === sha(Buffer.from(await allCells.arrayBuffer())),
            describeIdentical: out.a.describeSha === sha(Buffer.from(await describe.arrayBuffer())),
            exposedHeaders: out.a.exposedHeaders,
        };

        // (e) restart: same port and token (a node relaunched by its job),
        // then a new token (a node relaunched by hand).
        const polling = browser.page.evaluate((a) => window.POC.p5Poll(a), { node: nodeArg, durationMs: 26000 });
        await sleep(2000);
        node.stop();
        await sleep(2000);
        node = await ctx.startNode({ serve, allowOrigins: [origin], port: nodePort, token });
        await sleep(4000);
        node.stop();
        await sleep(1000);
        node = await ctx.startNode({ serve, allowOrigins: [origin], port: nodePort });
        out.e = await polling;
        const states = out.e.timeline.map((s) => s.state);
        checks.e = {
            recoveredWithSameToken: states.join(",").startsWith("up,down,up"),
            newTokenRefused: states.includes("refused"),
        };
        logs.push(...browser.logs.slice(-10));
        await browser.close();
        stat.proc.kill();
        node.stop();

        // -- (f) --allow-origin spellings ---------------------------------------
        const spellings = [];
        for (const [allowed, host] of [["http://127.0.0.1:PORT", "127.0.0.1"], ["http://localhost:PORT", "localhost"],
                                       ["http://127.0.0.1:PORT", "localhost"], ["http://localhost:PORT", "127.0.0.1"],
                                       ["http://127.0.0.1:PORT/", "127.0.0.1"]]) {
            const p = await freePort();
            const st = await ctx.startStatic({ port: p });
            const allow = allowed.replace("PORT", String(p));
            const n = await ctx.startNode({ serve: [`image:img=${fixture.image}`], allowOrigins: [allow] });
            const b = await ctx.launchChrome({ headed: ctx.args.headed });
            await b.page.goto(`http://${host}:${p}/poc/index.html`);
            await ready(b.page);
            const r = await b.page.evaluate((a) => window.POC.p5Reach(a), { node: { endpoint: n.endpoint, token: n.token } });
            spellings.push({ allowOrigin: allowed, pageHost: host, reached: r.ok, error: r.error || null });
            await b.close();
            n.stop();
            st.proc.kill();
        }
        out.f = spellings;
        // The host must match exactly (127.0.0.1 and localhost are two origins);
        // a trailing slash on --allow-origin is tolerated.
        checks.f = { hostMustMatch: spellings.every((s) => s.reached === s.allowOrigin.startsWith(`http://${s.pageHost}:`)),
                     trailingSlashTolerated: spellings.some((s) => s.allowOrigin.endsWith("/") && s.reached) };

        // -- (d) COOP/COEP -------------------------------------------------------
        out.d = [];
        for (const mode of ["require-corp", "credentialless", null]) {
            const p = await freePort();
            const st = await ctx.startStatic({ port: p, coopCoep: mode });
            const o = `http://127.0.0.1:${p}`;
            const n = await ctx.startNode({ serve: [`image:img=${fixture.image}`], allowOrigins: [o] });
            const b = await ctx.launchChrome({ headed: ctx.args.headed });
            await b.page.goto(`${o}/poc/index.html`);
            await ready(b.page);
            const r = await b.page.evaluate((a) => window.POC.p5Isolation(a),
                                            { node: { endpoint: n.endpoint, token: n.token, imageId: "img" }, tile: tiles[0] });
            out.d.push({ coep: mode, ...r });
            await b.close();
            n.stop();
            st.proc.kill();
        }

        // -- (g) connector pairing (stub) -----------------------------------------
        {
            const p = await freePort();
            const st = await ctx.startStatic({ port: p });
            const o = `http://127.0.0.1:${p}`;
            const n = await ctx.startNode({ serve: [`image:img=${fixture.image}`], allowOrigins: [o] });
            const stub = spawn(PYTHON, ["-I", path.join(POC_ROOT, "connector_stub.py"), "--origin", o,
                                        "--node", n.endpoint, "--node-token", n.token], { stdio: ["ignore", "pipe", "inherit"] });
            const line = await new Promise((resolve) => stub.stdout.once("data", (d) => resolve(String(d).trim())));
            const announced = JSON.parse(line);
            const b = await ctx.launchChrome({ headed: ctx.args.headed });
            await b.page.goto(announced.pairing);
            await ready(b.page);
            out.g = await b.page.evaluate(() => window.POC.p5Pair());
            out.g.pairingUrlHadToken = announced.pairing.includes(announced.token);
            await b.close();
            stub.kill();
            n.stop();
            st.proc.kill();
            checks.g = { paired: out.g.connected === 200 && out.g.nodeHello?.api_version === 1,
                         refusedUnpaired: out.g.profilesWithoutToken === 403, fragmentCleared: out.g.hashAfter === "" };
        }

        // -- (h) the node serving the app under /rnode/<host>/<port>/ --------------
        {
            const p = await freePort();
            const st = await ctx.startStatic({ port: p, rnode: true });
            const n = await ctx.startNode({ serve: [`image:img=${fixture.image}`] });   // no --allow-origin at all
            const ranges = await cachedRanges(ctx, fixture, 2);
            const b = await ctx.launchChrome({ headed: ctx.args.headed });
            await b.page.goto(`http://127.0.0.1:${p}/rnode/127.0.0.1/${n.port}/app/poc/index.html`);
            await ready(b.page);
            out.h = await b.page.evaluate((a) => window.POC.p5Rnode(a), {
                token: n.token, channels: ranges.map((range, i) => ({ index: i, color: ["#0000ff", "#00ff00"][i], range })) });
            logs.push(...b.logs.slice(-10));
            await b.close();
            n.stop();
            st.proc.kill();
            checks.h = { sameOriginWithoutAllowOrigin: out.h.hello?.api_version === 1, viewerFull: out.h.viewerFull?.full === true,
                         drewPixels: out.h.nonBlack > 0, base: out.h.base };
        }
        return { browser: env, arm: "browser->node", state: "static origin; loopback node", metrics: out, checks, logs };
    },

    /**
     * P5 (b): Chrome's Local Network Access from a page that counts as PUBLIC
     * (--ip-address-space-overrides marks the static origin public, standing
     * in for plexoraapp.com) to a loopback node, over https (self-signed,
     * certificate errors ignored) and http, with the permission granted (CDP)
     * or left to the browser. No mkcert needed.
     */
    async P5LNA(ctx) {
        const { fixture } = ctx;
        const tls = selfSignedCert();
        const cases = [];
        const grantedNames = {};
        for (const scheme of ["https", "http"]) {
            for (const space of ["public", null]) {
                for (const grant of [false, true]) {
                    const port = await freePort();
                    const st = await ctx.startStatic({ port, tls: scheme === "https" ? tls : null });
                    const origin = `${scheme}://127.0.0.1:${port}`;
                    const n = await ctx.startNode({ serve: [`image:img=${fixture.image}`], allowOrigins: [origin] });
                    const extraArgs = ["--ignore-certificate-errors"];
                    if (space) extraArgs.push(`--ip-address-space-overrides=127.0.0.1:${port}=${space}`);
                    const b = await ctx.launchChrome({ headed: ctx.args.headed, extraArgs });
                    if (grant) {
                        const cdp = await b.context.newCDPSession(b.page);
                        // Chrome split the one permission in two (loopback vs local
                        // network); grant every spelling it may know and keep which took.
                        const granted = [];
                        for (const name of ["local-network-access", "loopback-network", "local-network"]) {
                            try {
                                await cdp.send("Browser.setPermission", { permission: { name }, setting: "granted", origin });
                                granted.push(name);
                            } catch (error) { /* this Chrome has no such permission */ }
                        }
                        grantedNames[`${scheme}-${space}`] = granted;
                    }
                    await b.page.goto(`${origin}/poc/index.html`);
                    await b.page.waitForFunction(() => window.POC && document.getElementById("status").textContent === "ready", null, { timeout: 60000 });
                    const stub = spawn(PYTHON, ["-I", path.join(POC_ROOT, "connector_stub.py"), "--origin", origin,
                                                "--node", n.endpoint, "--node-token", n.token], { stdio: ["ignore", "pipe", "inherit"] });
                    const announced = JSON.parse(await new Promise((resolve) => stub.stdout.once("data", (d) => resolve(String(d).trim()))));
                    const r = await b.page.evaluate((a) => window.POC.p5Lna(a), {
                        node: { endpoint: n.endpoint, token: n.token }, connector: `http://127.0.0.1:${announced.port}` });
                    cases.push({ scheme, space: space || "default (loopback)", grant, ...r,
                                 console: b.logs.filter((l) => /private|local network|cors|blocked/i.test(l)).slice(0, 6) });
                    stub.kill();
                    await b.close();
                    n.stop();
                    st.proc.kill();
                }
            }
        }
        return { browser: null, arm: "browser->node", state: "Chrome stable, headless, LNA by address-space override",
                 metrics: { cases, grantedNames }, checks: {}, logs: [] };
    },

    /** P1R: a bucket OME-TIFF opened and read by the browser node over HTTP ranges. */
    async P1R(ctx) {
        const url = (ctx.args.params && ctx.args.params.url)
            || "https://lin-2023-orion-crc.s3.amazonaws.com/data/CRC02/P37_S30_A24_C59kX_E15_20220106_014319_409148-zlib.ome.tiff";
        const page = await openPage(ctx, {});
        const out = await page.page.evaluate((a) => window.POC.p1Remote(a), { url, ...(ctx.args.params || {}) });
        const logs = [...page.browser.logs];
        await page.browser.close();
        const { env, ...metrics } = out;
        return { browser: env, arm: "browser", state: "cold: fresh profile, nothing cached", metrics, checks: {}, logs };
    },
};
