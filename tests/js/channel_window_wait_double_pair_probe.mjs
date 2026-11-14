/**
 * A channel whose first add is still waiting for its volume window is drawn
 * by ONE cover/paint pair, whatever happens while it waits.
 *
 * On a volume in 8-bit tiles, the first add of a channel cannot name its
 * tiles until the channel's quantization window is known, so
 * `addChannelItems` returns `volumeWindows.ensure(...).then(...)` and nothing
 * of the channel is on the world meanwhile. A plane step (`setView`), the HD
 * toggle (`setHdMode`) or the base eye closed and reopened
 * (`hideReference` + `showReference`) in that wait finds no items for the
 * channel and so asks for an ORDINARY add of its own. Pinned here, with a
 * window answer released only when told to and a world that lands its adds
 * only when told to: once everything has answered, the channel is on the
 * world as exactly two items (one cover, one paint) -- not four, which draw
 * its colour twice (`lighter`) and take the ground away twice.
 *
 * Run directly:  node tests/js/channel_window_wait_double_pair_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const VIEWS = join(REPO, "plexora/client/src/js/views");

const failures = [];
function check(label, ok, why) {
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}

const stackContext = createContext({ console, window: {}, globalThis: {} });
runInContext(readFileSync(join(VIEWS, "layerStack.js"), "utf8"), stackContext);
globalThis.PlexoraLayerStack = stackContext.window.PlexoraLayerStack
    || stackContext.PlexoraLayerStack;
globalThis.window = globalThis;
globalThis.CustomEvent = class CustomEvent {
    constructor(type, options) { this.type = type; this.detail = options?.detail; }
};
globalThis.dispatchEvent = () => true;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.document = {
    getElementById: () => null,
    querySelector: () => null,
    createElement: () => ({ width: 0, height: 0, getContext: () => ({ fillRect() {} }),
                            toDataURL: () => "data:image/png;base64,stub" }),
};

const module = await import(pathToFileURL(join(VIEWS, "viewerManager.js")).href);
const ViewerManager = module.default || module.ViewerManager;
const { tileQuality } = module;

/** Let every resolved promise run its continuations. */
const settle = async () => {
    for (let i = 0; i < 20; i += 1) await Promise.resolve();
    await new Promise((resolve) => setImmediate(resolve));
    for (let i = 0; i < 20; i += 1) await Promise.resolve();
};

/**
 * A volume viewer: `addTiledImage` queues until `land()`, and the channel's
 * window (services/volumeWindows.js) is answered only on `answerWindow()`.
 */
function makeVolumeViewer() {
    const items = [];
    const queue = [];
    const world = {
        items,
        getItemCount: () => items.length,
        getItemAt: (i) => items[i],
        getIndexOfItem: (item) => items.indexOf(item),
        setItemIndex: () => {},
        addHandler() {},
        removeHandler() {},
        removeItem(item) {
            const at = items.indexOf(item);
            if (at >= 0) items.splice(at, 1);
        },
    };
    const viewer = {
        world,
        viewport: { getCenter: () => ({ x: 0.5, y: 0.5 }), getZoom: () => 1,
                    zoomTo() {}, panTo() {} },
        forceRedraw() {},
        raiseEvent() {},
        addHandler() {},
        removeHandler() {},
        addTiledImage(options) { queue.push(options); },
    };
    const land = () => {
        while (queue.length) {
            const options = queue.shift();
            const item = {
                source: { ...options.tileSource },
                opacity: options.opacity,
                blend: options.compositeOperation,
                setOpacity(value) { item.opacity = value; },
                setPreload() {},
                allowZombieCache() {},
                getFullyLoaded: () => true,
                addHandler() {},
                removeHandler() {},
            };
            items.push(item);
            options.success?.({ item });
        }
    };

    // The channel's window: unknown until answered, one shared request.
    const held = new Map();
    let release = null;
    const answer = new Promise((resolve) => { release = resolve; });
    const asked = [];
    globalThis.__plexora = {
        volumeWindows: {
            peek: (name) => held.get(name) || null,
            stamp: (name) => (held.has(name) ? "0,255" : ""),
            ensure(names) {
                asked.push(...names);
                return answer.then(() => undefined);
            },
        },
    };
    const answerWindow = () => {
        held.set("CD3", { qmin: 0, qmax: 255, exact: true });
        release();
    };

    const imageData = [{ src: "/tiles/cd3/", fullname: "CD3" }];
    const channelList = { currentChannels: {}, rangeConnector: { 0: [0, 255] },
                          colorConnector: { 0: { color: "#ff0000" } } };
    const layerStack = new globalThis.PlexoraLayerStack.LayerStack({ viewer });
    const manager = new ViewerManager(
        { viewer, config: { width: 1000, height: 1000, extraZoomLevels: 0, maxLevel: 3,
                            tileWidth: 256, tileHeight: 256, imageData },
          layerStack,
          numericData: { bitRange: [0, 255] } },
        channelList);
    manager.viewer = viewer;
    tileQuality.hd = false;
    manager.presetView("z0");
    const channelItems = () => items.filter((item) => item.source.src === imageData[0].src);
    const referenceItems = () =>
        (layerStack.get(globalThis.PlexoraLayerStack.REFERENCE_LAYER_ID)?.items || [])
            .filter((item) => item.source.src === imageData[0].src);
    return { manager, land, answerWindow, asked, channelItems, referenceItems };
}

const describe = (list) => list.map((item) => `${item.blend}@${item.source.view}`).join(", ");

{
    // Switched on for the first time, then a plane step before its window arrives.
    const v = makeVolumeViewer();
    v.manager.channel_add(0);
    check("(the first add waits for the channel's window: nothing is asked of the world yet)",
          v.asked.length === 1 && v.channelItems().length === 0);
    const swap = v.manager.setView("z5");
    v.answerWindow();
    await settle();
    v.land();
    await settle();
    v.land();
    await settle();
    check("a plane stepped while a channel's first add waits for its window leaves one pair of it",
          v.channelItems().length === 2,
          `${v.channelItems().length} items of the channel on the world (${describe(v.channelItems())}); `
          + "two pairs draw its colour twice and take the ground away twice");
    check("and one pair of it in the reference layer after the plane step",
          v.referenceItems().length === 2,
          `reference layer holds ${v.referenceItems().length} items of the channel`);
    void swap;
}

{
    // Switched on for the first time, then HD switched on before its window arrives.
    const v = makeVolumeViewer();
    v.manager.channel_add(0);
    v.manager.setHdMode(true);
    v.land();
    await settle();
    v.answerWindow();
    await settle();
    v.land();
    await settle();
    check("HD switched on while a channel's first add waits for its window leaves one pair of it",
          v.channelItems().length === 2,
          `${v.channelItems().length} items of the channel on the world (${describe(v.channelItems())})`);
    tileQuality.hd = false;
}

{
    // Switched on for the first time, then the base eye closed and reopened
    // before its window arrives.
    const v = makeVolumeViewer();
    v.manager.channel_add(0);
    v.manager.hideReference();
    v.manager.showReference();
    v.answerWindow();
    await settle();
    v.land();
    await settle();
    check("the base eye reopened while a channel's first add waits for its window leaves one pair of it",
          v.channelItems().length === 2,
          `${v.channelItems().length} items of the channel on the world (${describe(v.channelItems())})`);
}

if (failures.length) {
    console.log(`\n${failures.length} check(s) FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log("\nall checks passed");
