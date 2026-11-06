/**
 * P5T's browser arm: the product's tour on this page's viewer, every byte
 * straight from a remote data node -- the static-site layout -- timed from
 * navigation, as tools/bench_remote/tour.mjs times the product page that
 * reaches the same node through a Plexora on this machine (the desktop route).
 *
 * The page starts by itself when its address carries `#p5t=<json>`, so no
 * driver round trip sits inside the open: navigation -> geometry and one
 * window per channel from the node, in parallel (what a browser primary would
 * ask) -> the viewer -> every channel at its finest level. Then tour.mjs's
 * steps, in its order, with its moves and the product's springs, each timed by
 * tourpage.js's completion rule. `open` lists the channels the product opens
 * with and `add` the ones tour.mjs then switches on (its "channels" step), so
 * both arms do the same work in the same order.
 */
import { NodeClient } from "./nodeClient.js";
import { createViewer } from "./viewer.js";

const COLORS = ["#0000ff", "#00ff00", "#ff0000", "#ffff00", "#ff00ff", "#00ffff", "#ff8000"];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** The finest level OpenSeadragon draws for `item` at the current view (tourpage.js targetLevel). */
function targetLevel(item) {
    const source = item.source;
    const min = source.minLevel || 0;
    for (let level = source.maxLevel; level >= min; level -= 1) {
        const ratio = item.viewport.deltaPixelsFromPointsNoRotate(
            source.getPixelRatio(level), true).x * item._scaleSpring.current.value;
        if (ratio >= item.minPixelRatio) return level;
    }
    return min;
}

function finestOf(item) {
    let best = null;
    for (const info of item._lastDrawn || []) {
        const level = info.level ?? info.tile?.level;
        if (Number.isFinite(level)) best = best === null ? level : Math.max(best, level);
    }
    return best;
}

/**
 * tourpage.js's waitStep on this viewer: full once every item is fully loaded
 * and draws the finest level OSD wants, two redraws after the move, springs at
 * rest, held for two polls 5 ms apart; the time is when that run began.
 */
async function waitStep(viewer, t0, u0, ms) {
    const end = t0 + ms;
    let firstPixel = null;
    let since = null;
    let polls = 0;
    while (performance.now() < end) {
        if (firstPixel === null) {
            const t = viewer.__loads.find((x) => x >= t0);
            if (t !== undefined) firstPixel = t - t0;
        }
        const vp = viewer.viewport;
        const still = [vp.zoomSpring, vp.centerSpringX, vp.centerSpringY].every((s) => !s || s.isAtTargetValue());
        const world = viewer.world;
        let full = world.getItemCount() > 0;
        for (let i = 0; full && i < world.getItemCount(); i += 1) {
            const item = world.getItemAt(i);
            const finest = finestOf(item);
            full = item.getFullyLoaded() && finest !== null && finest >= targetLevel(item);
        }
        if (full && still && viewer.__updates >= u0 + 2) {
            since ??= performance.now();
            polls += 1;
            if (polls >= 2) return { firstPixel, full: since - t0 };
        } else {
            since = null;
            polls = 0;
        }
        await sleep(5);
    }
    return { firstPixel, full: null };
}

export async function p5Tour({ node, open, add = [], waypoints: W, size = [1060, 855], timeoutMs = 120000 }) {
    const client = new NodeClient(node.endpoint, node.token);
    const range = (s) => [s.vmin_hint / 65535, Math.max(s.vmin_hint + 1, s.vmax_hint) / 65535];
    const [g, ...stats] = await Promise.all([
        client.imageGeometry(node.imageId),
        ...open.map((c) => client.imageStats(node.imageId, c)),
    ]);
    const metadataMs = performance.now();
    const geometry = {
        width: g.width, height: g.height, numChannels: g.num_channels, names: g.channel_names,
        levels: g.level_shapes.map(([h, w]) => ({ width: w, height: h })), tileWidth: 1024, tileHeight: 1024,
    };
    const pane = document.createElement("div");
    pane.id = "p5tpane";
    pane.style.cssText = `position:fixed;left:0;top:0;width:${size[0]}px;height:${size[1]}px;background:#000;z-index:5`;
    document.body.appendChild(pane);
    const v = await createViewer({
        container: pane.id, geometry, feed: "node",
        channels: open.map((index, i) => ({ index, color: COLORS[i % COLORS.length], range: range(stats[i]) })),
        nodeTileUrl: (c, level, x, y) => client.tileUrl(node.imageId, c, level, x, y),
    });
    const viewerMs = performance.now();
    const viewer = v.viewer;
    // The product's main viewer keeps OpenSeadragon's springs (1.2 s, stiffness
    // 6.5); createViewer turns them off.
    for (const spring of [viewer.viewport.zoomSpring, viewer.viewport.centerSpringX, viewer.viewport.centerSpringY]) {
        spring.animationTime = 1.2;
        spring.springStiffness = 6.5;
    }
    viewer.__updates = 0;
    viewer.__loads = [];
    viewer.addHandler("update-viewport", () => { viewer.__updates += 1; });
    viewer.addHandler("tile-loaded", () => { viewer.__loads.push(performance.now()); });
    const steps = [{ step: "open", t0: 0, ...(await waitStep(viewer, 0, 0, Math.max(timeoutMs, 300000))),
                     metadataMs, viewerMs }];
    const item = () => viewer.world.getItemAt(0);
    const place = (ratio, at) => {
        viewer.viewport.zoomTo(item().imageToViewportZoom(ratio), null, true);
        viewer.viewport.panTo(item().imageToViewportCoordinates(at[0], at[1]), true);
        viewer.viewport.applyConstraints(true);
    };
    const step = async (name, move) => {
        const t0 = performance.now();
        const u0 = viewer.__updates;
        await move();
        steps.push({ step: name, t0, ...(await waitStep(viewer, t0, u0, timeoutMs)) });
    };
    if (add.length) {
        await step("channels", async () => {
            const more = await Promise.all(add.map((c) => client.imageStats(node.imageId, c)));
            await Promise.all(add.map((index, i) => v.addChannel({
                index, color: COLORS[(open.length + i) % COLORS.length], range: range(more[i]) })));
        });
    }
    await step("zoom_L2_a", () => place(0.25, W.a));
    await step("zoom_L0_a", () => place(1, W.a));
    await step("pan_L0_b", () => place(1, W.b));
    await step("home", () => viewer.viewport.goHome(true));
    await step("revisit_L0_a", () => place(1, W.a));
    await step("jump_L0_c", () => place(1, W.c));
    await step("anim_zoom_L2", () => {
        viewer.viewport.panTo(item().imageToViewportCoordinates(W.b[0], W.b[1]), true);
        viewer.viewport.zoomTo(item().imageToViewportZoom(0.25));
    });
    await step("anim_pan_L2", () => viewer.viewport.panTo(item().imageToViewportCoordinates(W.c[0], W.c[1])));
    const nav = performance.getEntriesByType("navigation")[0];
    const requests = client.log.map((e) => ({ path: e.path.replace(/\/[^/]+$/, "/…"), ms: Math.round(e.ms) }));
    return {
        steps, size, canvas: [viewer.drawer.canvas.width, viewer.drawer.canvas.height],
        pageLoadMs: nav ? nav.loadEventEnd : null, domContentLoadedMs: nav ? nav.domContentLoadedEventEnd : null,
        jsHeap: performance.memory?.usedJSHeapSize ?? null, metadataRequests: requests,
        tiles: v.tileLog.length,
    };
}

/** Run from `#p5t=<json>` at load; the driver polls window.__p5t. */
export function autorun() {
    const hash = location.hash;
    if (!hash.startsWith("#p5t=")) return false;
    const args = JSON.parse(decodeURIComponent(hash.slice(5)));
    window.__p5t = { done: false };
    p5Tour(args).then(
        (result) => { window.__p5t = { done: true, result }; },
        (error) => { window.__p5t = { done: true, error: `${error} | ${error?.stack || ""}` }; },
    );
    return true;
}

window.POC_P5T = { p5Tour, autorun };
autorun();
