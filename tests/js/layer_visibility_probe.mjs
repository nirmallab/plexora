/**
 * A hidden layer costs nothing.
 *
 * This is the whole performance argument for registered layers, and it is the
 * one that cannot be made by a comment. An OpenSeadragon TiledImage at opacity
 * 0 still requests every tile in view, decodes it and draws it -- so a scene
 * with six registered layers and one shown would pay for all six, which is the
 * difference between the layer model being usable and being a demo.
 *
 * So `setVisible(false)` REMOVES the world item and `setVisible(true)` adds it
 * back at the same placement. Three things have to hold and each has a silent
 * failure mode:
 *
 *   **Hiding removes.** An opacity toggle looks identical on screen and costs
 *   the full frame time.
 *
 *   **Showing restores the same placement.** A layer that came back at a
 *   different position would look like a registration bug, not a toggle bug.
 *
 *   **A hide that lands mid-load still hides.** `addTiledImage` is
 *   asynchronous; without a guard the item arrives after the removal and stays
 *   on screen forever -- visible as a layer that will not switch off.
 *
 * And one more, which is a boundary rather than a cost: a transform
 * OpenSeadragon cannot express (a shear) must draw NOTHING. A sheared layer
 * drawn without its shear looks entirely plausible and is wrong by a few
 * microns everywhere -- precisely the error a registration exists to remove.
 *
 * Run directly:  node tests/js/layer_visibility_probe.mjs
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

// -- the layer stack, as the classic script the page loads ----------------

const stackContext = createContext({ console, window: {}, globalThis: {} });
runInContext(readFileSync(join(VIEWS, "layerStack.js"), "utf8"), stackContext);
const PlexoraLayerStack = stackContext.window.PlexoraLayerStack
    || stackContext.PlexoraLayerStack;

// `viewerManager.js` reads PlexoraLayerStack off the global at call time, the
// way the browser has it -- one is a webpacked module and the other a classic
// script, and that seam is exactly what this loads both halves to test.
globalThis.PlexoraLayerStack = PlexoraLayerStack;

const { default: _unused, ...module } = await import(
    pathToFileURL(join(VIEWS, "viewerManager.js")).href);
const ViewerManager = module.default || module.ViewerManager;

// -- the smallest world that can be counted -------------------------------

function makeWorld() {
    const items = [];
    return {
        items,
        getItemCount: () => items.length,
        getItemAt: (i) => items[i],
        getIndexOfItem: (item) => items.indexOf(item),
        setItemIndex: () => {},
        removeItem: (item) => {
            const at = items.indexOf(item);
            if (at >= 0) items.splice(at, 1);
        },
    };
}

/**
 * A ViewerManager over a fake OSD.
 *
 * `settle` controls WHEN an add lands, which is what makes the race testable:
 * with `settle=false` the item is held back, so a hide can arrive first.
 */
function makeManager({ settle = true } = {}) {
    const world = makeWorld();
    const pending = [];
    const viewer = {
        world,
        addTiledImage(options) {
            const item = { source: options.tileSource, options };
            const land = () => {
                world.items.push(item);
                options.success?.({ item });
            };
            if (settle) land();
            else pending.push(() => { land(); });
        },
        raiseEvent() {},
    };
    const stack = new PlexoraLayerStack.LayerStack({ viewer });
    // `manager.layerStack` is a getter over `imageViewer.layerStack` -- the
    // stack belongs to the viewer and the manager only reaches through -- so
    // the fake viewer is where it goes.
    const manager = new ViewerManager(
        { viewer, config: { width: 1000, height: 1000, extraZoomLevels: 0,
                            maxLevel: 3, tileWidth: 256, tileHeight: 256 },
          layerStack: stack },
        { currentChannels: {} });
    manager.viewer = viewer;
    return { manager, world, flush: () => pending.splice(0).forEach((f) => f()) };
}

const GEOMETRY = {
    width: 500, height: 500, maxLevel: 2, tileWidth: 256, tileHeight: 256,
    transform: [2, 0, 0, 2, 100, 50],
};

// -- the checks ------------------------------------------------------------

{
    const { manager, world } = makeManager();
    const handle = manager.addTiledLayer({
        layerId: "he", src: "/generated/layer/demo/he/he_0/", geometry: GEOMETRY,
    });

    check("a registered layer lands as one world item",
        world.getItemCount() === 1);
    check("tagged with its layer id, so the stack can order it",
        world.getItemAt(0).source.layerId === "he",
        "applyWorldOrder and anchorIndex both read this tag");
    check("placed where its transform says",
        Math.abs(handle.placement.width - 500 * 2 / 1000) < 1e-9
        && Math.abs(handle.placement.x - 100 / 1000) < 1e-9,
        "viewport units: layerWidth * scale / referenceWidth");
    check("drawn with the LAYER's own tile grid, not the reference's",
        world.getItemAt(0).source.width === 500
        && world.getItemAt(0).source.maxLevel === 1,
        "borrowing the reference's would stretch a corner over the frame");
    check("and no extra zoom levels of its own",
        world.getItemAt(0).source.extraZoomLevels === 0,
        "extraZoomLevels is a fact about the reference image's magnification");

    const placement = handle.placement;
    handle.setVisible(false);
    check("hiding REMOVES the item rather than fading it",
        world.getItemCount() === 0,
        "an item at opacity 0 still fetches, decodes and draws every tile");

    handle.setVisible(true);
    check("showing adds it back", world.getItemCount() === 1);
    check("at the same placement",
        world.getItemAt(0).options.x === placement.x
        && world.getItemAt(0).options.width === placement.width,
        "a layer that moved when toggled reads as a registration bug");

    handle.remove();
    check("removing leaves an empty world", world.getItemCount() === 0);
}

{
    const { manager, world, flush } = makeManager({ settle: false });
    const handle = manager.addTiledLayer({
        layerId: "he", src: "/x/", geometry: GEOMETRY,
    });
    handle.setVisible(false);
    flush();
    check("a hide that lands mid-load still hides",
        world.getItemCount() === 0,
        "without the guard the item arrives after the removal and never leaves");
}

{
    const { manager, world } = makeManager();
    const sheared = manager.addTiledLayer({
        layerId: "skew", src: "/x/",
        geometry: { ...GEOMETRY, transform: [1, 0, 0.4, 1, 0, 0] },
    });
    check("a transform OpenSeadragon cannot express draws nothing",
        sheared === null && world.getItemCount() === 0,
        "a sheared layer drawn without its shear is wrong everywhere and looks right");
}

{
    const { manager, world } = makeManager();
    manager.syncLayerImages([
        { id: "__image__", kind: "image", channels: [{ src: "/ref/" }] },
        { id: "he", kind: "image", width: 500, height: 500, maxLevel: 2,
          tileWidth: 256, tileHeight: 256, transform: null,
          channels: [{ name: "he_0", src: "/generated/layer/demo/he/he_0/" }] },
        { id: "tx", kind: "points", channels: [{ src: "/points/" }] },
        { id: "later", kind: "image", status: "pending", width: 500, height: 500,
          channels: [{ src: "/later/" }] },
    ]);

    check("the reference layer is not drawn twice",
        world.items.every((item) => item.source.layerId !== "__image__"),
        "it is already in the world as the channel stack or the brightfield base");
    check("a points layer is not drawn by core",
        world.items.every((item) => item.source.layerId !== "tx"),
        "density is the transcripts plugin's picture, through ctx.layers.addTiled");
    check("a layer still being built is not drawn",
        world.items.every((item) => item.source.layerId !== "later"),
        "its tiles do not exist yet; adding it is a wall of 404s");
    check("and the one drawable layer is drawn",
        world.items.filter((item) => item.source.layerId === "he").length === 1);

    // Idempotent: adopting a newly imported layer must not restack the rest.
    manager.syncLayerImages([
        { id: "he", kind: "image", width: 500, height: 500, maxLevel: 2,
          tileWidth: 256, tileHeight: 256, transform: null,
          channels: [{ name: "he_0", src: "/generated/layer/demo/he/he_0/" }] },
    ]);
    check("re-syncing leaves a layer already on screen alone",
        world.items.filter((item) => item.source.layerId === "he").length === 1);

    manager.syncLayerImages([]);
    check("a layer the config no longer lists is dropped",
        world.getItemCount() === 0);
}

console.log(failures.length ? `\n${failures.length} check(s) failed`
                            : "\nall checks passed");
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
