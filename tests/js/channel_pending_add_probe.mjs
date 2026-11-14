/**
 * A channel switched off before its pair has landed stays off.
 *
 * OpenSeadragon does not add an item when `addTiledImage` is called: it
 * builds the tile source in a `setTimeout`, waits for its `ready`, and adds
 * queued items in the order they were asked for (Viewer.addTiledImage,
 * instantiateTileSourceClass). So between `channel_add` and the pair's
 * `success` there is a window in which the world holds nothing of the
 * channel. Pinned here, with a world that lands its adds only when told to:
 *
 *   **Switched off in that window, it never shows.** `channel_remove` finds
 *   nothing to remove and deletes the slot; when the pair lands it must be
 *   dropped, not claimed -- with no slot, the colorize pass draws it white at
 *   the full range and nothing can take it down.
 *
 *   **The base eye closed in that window keeps it off too.** `hideReference`
 *   empties the world of reference items; a pair landing after it must not
 *   put a channel back under a closed eye.
 *
 * Run directly:  node tests/js/channel_pending_add_probe.mjs
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
globalThis.document = {
    getElementById: () => null,
    createElement: () => ({ width: 0, height: 0, getContext: () => ({ fillRect() {} }),
                            toDataURL: () => "data:image/png;base64,stub" }),
};

const module = await import(pathToFileURL(join(VIEWS, "viewerManager.js")).href);
const ViewerManager = module.default || module.ViewerManager;

/**
 * A world as OpenSeadragon 6 runs one: `addTiledImage` queues the add, and
 * nothing reaches the world (or `success`) until `land()` -- OSD's setTimeout
 * and tile source `ready` -- in the order the adds were asked for.
 */
function makeManager(channels) {
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
        addTiledImage(options) {
            queue.push(options);
        },
    };
    /** OSD's deferred work: every queued add lands, first asked first. */
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
    const imageData = channels.map((name) => ({ src: `/tiles/${name}/` }));
    const channelList = { currentChannels: {}, rangeConnector: {}, colorConnector: {} };
    channels.forEach((name, srcIdx) => {
        channelList.rangeConnector[srcIdx] = [0, 255];
        channelList.colorConnector[srcIdx] = { color: "#ff0000" };
    });
    const layerStack = new globalThis.PlexoraLayerStack.LayerStack({ viewer });
    const manager = new ViewerManager(
        { viewer, config: { width: 1000, height: 1000, extraZoomLevels: 0, maxLevel: 3,
                            tileWidth: 256, tileHeight: 256, imageData },
          layerStack,
          numericData: { bitRange: [0, 255] } },
        channelList);
    manager.viewer = viewer;
    const referenceItems = () =>
        layerStack.get(globalThis.PlexoraLayerStack.REFERENCE_LAYER_ID)?.items || [];
    return { manager, items, queue, land, imageData, channelList, referenceItems };
}

{
    // Switched on, then off before OpenSeadragon has added the pair.
    const { manager, items, queue, land, imageData, channelList, referenceItems } =
        makeManager(["dapi", "cd3"]);
    manager.channel_add(0);
    land();
    const dapi = items.slice();
    manager.channel_add(1);
    check("(an add is only queued: nothing of the channel is on the world yet)",
          queue.length === 2 && !items.some((item) => item.source.src === imageData[1].src));
    manager.channel_remove(1);
    check("(the slot is gone at once)", !(1 in channelList.currentChannels));
    land();
    const orphans = items.filter((item) => item.source.src === imageData[1].src);
    check("a channel switched off before its pair landed leaves nothing of it on the world",
          orphans.length === 0,
          `${orphans.length} item(s) of a channel with no slot are on the world; with no slot the `
          + "colorize pass draws them white at the full range and nothing removes them");
    check("and nothing of it in the reference layer",
          referenceItems().every((item) => item.source.src !== imageData[1].src),
          `reference layer holds ${referenceItems().length} items, `
          + `${referenceItems().filter((item) => item.source.src === imageData[1].src).length} of the removed channel`);
    check("the channel still on is untouched",
          dapi.length === 2 && dapi.every((item) => items.includes(item)));
}

{
    // Switched on, then the base layer's eye closed before the pair landed.
    const { manager, items, land, imageData, channelList } = makeManager(["dapi", "cd3"]);
    manager.channel_add(0);
    land();
    manager.channel_add(1);
    manager.hideReference();
    check("(closing the eye takes what is on the world off it)",
          items.length === 0 && manager.referenceHidden === true);
    land();
    const shown = items.filter((item) => item.source.layerId
                               === globalThis.PlexoraLayerStack.REFERENCE_LAYER_ID);
    check("a pair that lands after the base eye closed is not put on screen",
          shown.length === 0,
          `${shown.length} reference item(s) on the world under a closed eye `
          + `(${shown.filter((item) => item.source.src === imageData[1].src).length} of cd3)`);
    check("(the slot is kept, so opening the eye brings the channel back)",
          1 in channelList.currentChannels);
}

if (failures.length) {
    console.log(`\n${failures.length} check(s) FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log("\nall checks passed");
