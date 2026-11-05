/**
 * P2 / P2b: label masks in the browser node.
 *
 * P2: a prepared label pyramid read by geotiff.js, its uint32 ids handed to
 * the product's GPU cell layer (labelGpu.js) exactly as label tiles from a
 * node are, and a live gate drag timed per drawn update (use case 2's
 * measure).
 *
 * P2b: a flat mask made into a pyramid by workers/pyramidWorker.js in OPFS;
 * its tiles checked against the pyramid Python builds from the same file; and
 * the first tile after a reopen timed.
 */
import { NodeClient } from "./nodeClient.js";
import { TiffPool } from "./tiffPool.js";
import { createViewer, waitFull, canvasPixels } from "./viewer.js";
import { decodePng, firstDifference, sha256 } from "./png.js";

const now = () => performance.now();
const frame = () => new Promise((resolve) => requestAnimationFrame(resolve));
const workerUrl = (name) => new URL(`dist/${name}`, location.href).href;

function picked(name) {
    const file = [...document.getElementById("picker").files].find((f) => f.name === name);
    if (!file) throw new Error(`no picked file ${name}`);
    return file;
}

function summary(values) {
    const v = values.filter(Number.isFinite).slice().sort((a, b) => a - b);
    if (!v.length) return { n: 0 };
    const q = (p) => v[Math.min(v.length - 1, Math.floor(p * (v.length - 1) + 0.5))];
    return { n: v.length, min: v[0], p50: q(0.5), p90: q(0.9), p95: q(0.95), max: v[v.length - 1] };
}

function rng(seed) {
    let s = seed >>> 0;
    return () => {
        s = (s + 0x6d2b79f5) >>> 0;
        let t = Math.imul(s ^ (s >>> 15), 1 | s);
        t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
}

// ---------------------------------------------------------------------------
// The OPFS label store P2b writes

export async function openLabelStore(key, sourceFile) {
    const root = await navigator.storage.getDirectory();
    const dir = await (await root.getDirectoryHandle("labels")).getDirectoryHandle(key);
    const manifest = JSON.parse(await (await (await dir.getFileHandle("manifest.json")).getFile()).text());
    const files = {};
    let source = null;
    return {
        manifest,
        async tile(level, tx, ty) {
            const lv = manifest.levels[level];
            if (!lv) throw new Error(`no level ${level}`);
            if (lv.fromSource) {
                source ||= new TiffPool(workerUrl("tiffWorker.js"), 1);
                if (!source.opened) { await source.open({ file: sourceFile }); source.opened = true; }
                return source.tile(0, 0, tx, ty);
            }
            const across = Math.ceil(lv.width / manifest.tile);
            const entry = lv.tiles[ty * across + tx];
            if (!entry) return { width: 0, height: 0, array: new Uint8Array(0), format: "empty" };
            const [offset, length, w, h] = entry;
            files[level] ||= await (await dir.getFileHandle(lv.file)).getFile();
            let blob = files[level].slice(offset, offset + length);
            let bytes;
            if (manifest.compress === "deflate") {
                bytes = new Uint8Array(await new Response(blob.stream().pipeThrough(new DecompressionStream("deflate"))).arrayBuffer());
            } else {
                bytes = new Uint8Array(await blob.arrayBuffer());
            }
            return { width: w, height: h, array: bytes, format: "u32" };
        },
        close() { source?.terminate(); },
    };
}

/** P2b build: flat mask -> pyramid in OPFS, main thread watched for long tasks. */
export async function p2bBuild({ mask, key = "p2b", compress = true }) {
    const file = picked(mask);
    const root = await navigator.storage.getDirectory();
    try { await root.removeEntry("labels", { recursive: true }); } catch (e) { /* none */ }
    const longTasks = [];
    const observer = new PerformanceObserver((list) => { for (const e of list.getEntries()) longTasks.push(e.duration); });
    observer.observe({ type: "longtask" });
    // A frame counter: the page must keep drawing while the worker builds.
    let frames = 0, building = true;
    (async () => { while (building) { await frame(); frames += 1; } })();
    const t0 = now();
    const result = await new Promise((resolve, reject) => {
        const worker = new Worker(workerUrl("pyramidWorker.js"));
        worker.onmessage = (e) => {
            if (e.data.error) { worker.terminate(); reject(new Error(e.data.error)); }
            if (e.data.done) { worker.terminate(); resolve(e.data); }
        };
        worker.postMessage({ op: "build", file, key, compress });
    });
    const wall = now() - t0;
    building = false;
    observer.disconnect();
    const estimate = await navigator.storage.estimate();
    return {
        wallMs: wall, timings: result.timings, manifest: result.manifest,
        longTasks: { count: longTasks.length, over50: longTasks.filter((d) => d > 50).length, max: Math.max(0, ...longTasks) },
        framesDuring: frames, fps: frames / (wall / 1000),
        storage: { usage: estimate.usage, quota: estimate.quota },
        sourceBytes: file.size,
    };
}

/** P2b identity: OPFS tiles vs the node's tiles of the pyramid Python built. */
export async function p2bIdentity({ mask, key = "p2b", node, count = 200, seed = 5 }) {
    const store = await openLabelStore(key, picked(mask));
    const client = new NodeClient(node.endpoint, node.token);
    const rand = rng(seed);
    const levels = store.manifest.levels;
    let identical = 0;
    const mismatches = [];
    for (let i = 0; i < count; i += 1) {
        // Derived levels only (level 0 is the source file itself).
        const level = 1 + Math.floor(rand() * (levels.length - 1));
        const lv = levels[level];
        const tx = Math.floor(rand() * Math.ceil(lv.width / store.manifest.tile));
        const ty = Math.floor(rand() * Math.ceil(lv.height / store.manifest.tile));
        const mine = await store.tile(level, tx, ty);
        const response = await fetch(client.segTileUrl(node.segId, level, tx, ty), { credentials: "omit" });
        const theirs = await decodePng(new Uint8Array(await response.arrayBuffer()));
        const same = mine.width === theirs.width && mine.height === theirs.height
            && firstDifference(mine.array, theirs.data) === -1;
        if (same) identical += 1;
        else if (mismatches.length < 10) {
            mismatches.push({ level, tx, ty, mine: [mine.width, mine.height], node: [theirs.width, theirs.height],
                              firstDiff: firstDifference(mine.array, theirs.data) });
        }
    }
    store.close();
    return { count, identical, mismatches };
}

/** P2b reopen: a fresh page finds the store and draws its first tile. */
export async function p2bReopen({ mask, key = "p2b" }) {
    const t0 = now();
    const store = await openLabelStore(key, picked(mask));
    const tManifest = now();
    const coarse = store.manifest.levels.length - 1;
    const tile = await store.tile(coarse, 0, 0);
    const firstTileMs = now() - t0;
    const mid = Math.max(1, coarse - 2);
    const t1 = now();
    await store.tile(mid, 0, 0);
    const midTileMs = now() - t1;
    store.close();
    return { manifestMs: tManifest - t0, firstTileMs, firstTile: [tile.width, tile.height], midTileMs };
}

// ---------------------------------------------------------------------------
// P2: the GPU cell layer over label tiles from either feed, and a gate drag

export async function p2Viewer({ arm, image = null, mask, node, channels = [], roles, marker, ticks = 40,
                                 pane = "paneA", identityTiles = 100 }) {
    const idColumn = roles.cell_id;
    const client = new NodeClient(node.endpoint, node.token);
    const out = { arm };
    // The table half, before anything is timed: ids in table order and one marker.
    const t0 = now();
    const ids = await client.allCells(node.tableId, [idColumn], true);
    const columns = await client.columns(node.tableId, [marker]);
    const values = columns[marker];
    out.tableMs = now() - t0;
    out.cells = ids.length;

    // The mask's own geometry (read locally before anything is timed; the
    // L fixture has a mask and a table but no image here).
    const maskPool = new TiffPool(workerUrl("tiffWorker.js"), 2);
    const { geometry: g } = await maskPool.open({ file: picked(mask) });
    const geo = { width: g.width, height: g.height, tileWidth: 1024, tileHeight: 1024, levels: g.levels };
    let imagePool = null;
    if (arm === "browser" && image) {
        imagePool = new TiffPool(workerUrl("tiffWorker.js"), 4);
        await imagePool.open({ file: picked(image) });
    }
    const layer = { name: "cells", mode: "outline", opacity: 1, renderVersion: 0, gateMask: null };
    const viewer = await createViewer({
        container: pane, geometry: geo, channels, feed: arm,
        nodeTileUrl: (c, level, x, y) => client.tileUrl(node.imageId, c, level, x, y),
        browserTile: (c, level, x, y) => imagePool.tile(c, level, x, y),
        labels: {
            feed: arm, layer, segmentationMode: "filled",
            tileUrl: (level, x, y) => client.segTileUrl(node.segId, level, x, y),
            browserTile: (_c, level, x, y) => maskPool.tile(0, level, x, y),
        },
    });
    await viewer.labelGpu?.ready;
    out.labelGpu = { active: viewer.labelGpu?.active ?? false, reason: viewer.labelGpu?.reason ?? null };

    // Somewhere with cells: the centroid of the middle cell of the table.
    const xs = await client.allCells(node.tableId, [roles.x, roles.y], false);
    const mid = Math.floor(ids.length / 2);
    const cx = xs[mid * 2], cy = xs[mid * 2 + 1];
    const rect = { x: Math.max(0, cx - 1440), y: Math.max(0, cy - 1800), w: 2880, h: 3600 };
    const item = viewer.viewer.world.getItemAt(0);
    viewer.viewer.viewport.fitBounds(item.imageToViewportRectangle(rect.x, rect.y, rect.w, rect.h), true);
    const full = await waitFull(viewer.viewer, { timeoutMs: 60000 });
    out.viewFullMs = full.ms;

    // The gate drag: a threshold swept over the marker's range.
    const finite = Float32Array.from(values).filter(Number.isFinite).sort();
    const lo = finite[Math.floor(finite.length * 0.05)], hi = finite[Math.floor(finite.length * 0.995)];
    const evalMs = [], drawMs = [], counts = [];
    const label = viewer.items[viewer.items.length - 1];
    for (let i = 0; i < ticks; i += 1) {
        const threshold = lo + ((hi - lo) * (i % 20)) / 20;
        const s = now();
        const gate = PlexoraLabelGpu.evaluateGateMask(ids, { [marker]: values }, { [marker]: [threshold, Infinity] });
        const sEval = now();
        layer.gateMask = gate.mask;
        layer.renderVersion += 1;
        label._needsDraw = true;
        viewer.viewer.forceRedraw();
        await frame();
        await frame();
        drawMs.push(now() - s);
        evalMs.push(sEval - s);
        counts.push(gate.count);
    }
    out.drag = { ticks, evalMs: summary(evalMs), updateMs: summary(drawMs), positives: [Math.min(...counts), Math.max(...counts)],
                 labelStats: viewer.labelGpu?.stats ?? null };
    out.canvas = await sha256(canvasPixels(viewer.viewer));

    // Label tile bytes, this arm's feed vs the node's PNG (browser arm only).
    if (arm === "browser" && identityTiles) {
        const rand = rng(9);
        let identical = 0;
        const mism = [];
        for (let i = 0; i < identityTiles; i += 1) {
            const level = Math.floor(rand() * geo.levels.length);
            const lv = geo.levels[level];
            const tx = Math.floor(rand() * Math.ceil(lv.width / 1024));
            const ty = Math.floor(rand() * Math.ceil(lv.height / 1024));
            const mine = await maskPool.tile(0, level, tx, ty);
            const theirs = await decodePng(new Uint8Array(await (await fetch(client.segTileUrl(node.segId, level, tx, ty))).arrayBuffer()));
            const same = mine.width === theirs.width && mine.height === theirs.height && firstDifference(mine.array, theirs.data) === -1;
            if (same) identical += 1;
            else if (mism.length < 5) mism.push({ level, tx, ty, mine: [mine.width, mine.height], node: [theirs.width, theirs.height] });
        }
        out.labelIdentity = { count: identityTiles, identical, mismatches: mism };
    }
    out.jsHeap = performance.memory?.usedJSHeapSize ?? null;
    viewer.destroy();
    maskPool.terminate();
    imagePool?.terminate();
    return out;
}
