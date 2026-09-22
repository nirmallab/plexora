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

/** What `$.extend(true, ...)` does to a tile source: plain objects and arrays
 *  are cloned, everything else (functions above all) is carried across. */
function deepCopy(value) {
    if (Array.isArray(value)) return value.map(deepCopy);
    if (!value || typeof value !== "object") return value;
    if (Object.getPrototypeOf(value) !== Object.prototype) return value;
    return Object.fromEntries(
        Object.entries(value).map(([key, held]) => [key, deepCopy(held)]));
}

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

// The HD toggle announces itself on `window`, which node has none of. Just
// enough of one to let setHdMode run to the end -- the channel rebuild it
// performs on the way there is what is under test.
globalThis.window = globalThis;
globalThis.CustomEvent = class CustomEvent {
    constructor(type, options) { this.type = type; this.detail = options?.detail; }
};
globalThis.dispatchEvent = () => true;
globalThis.addEventListener = () => {};

// A layer's ground is a one-pixel PNG of its colour, drawn on a canvas (see
// `groundTileUrl`). Enough of one to let that run, so the check below can ask
// the real tile source for its real url rather than assert the shape of a
// function nobody called.
globalThis.document = {
    createElement: () => ({
        width: 0, height: 0,
        getContext: () => ({ fillStyle: "", fillRect() {} }),
        toDataURL: () => "data:image/png;base64,stub",
    }),
};

const { default: _unused, ...module } = await import(
    pathToFileURL(join(VIEWS, "viewerManager.js")).href);
const ViewerManager = module.default || module.ViewerManager;

// -- the smallest world that can be counted -------------------------------

function makeWorld() {
    const items = [];
    return {
        items,
        //: How many items have EVER landed, which is the only way to tell
        //: "never added" apart from "added and removed" after the fact -- and
        //: they cost entirely different amounts.
        everAdded: 0,
        //: THE FEWEST ITEMS THE WORLD EVER HELD since `mark()`. A rebuild that
        //: removes before it adds cannot be caught by looking at the world
        //: afterwards -- it ends up exactly where it started. What is wrong
        //: with it happens in between, and this is the only place a probe can
        //: see it: an empty world is a black frame on somebody's screen.
        fewest: 0,
        mark() { this.fewest = items.length; },
        getItemCount: () => items.length,
        getItemAt: (i) => items[i],
        getIndexOfItem: (item) => items.indexOf(item),
        setItemIndex: () => {},
        removeItem(item) {
            const at = items.indexOf(item);
            if (at >= 0) items.splice(at, 1);
            this.fewest = Math.min(this.fewest, items.length);
        },
    };
}

/**
 * A ViewerManager over a fake OSD.
 *
 * `settle` controls WHEN an add lands, which is what makes the race testable:
 * with `settle=false` the item is held back, so a hide can arrive first.
 *
 * `loading` controls whether an item that has landed can DRAW yet. With it on,
 * every item arrives not-fully-loaded and answers OpenSeadragon's
 * `getFullyLoaded` / `fully-loaded-change` pair, so a quality swap stays open
 * and the probe can look at the world while it is half done -- which is the
 * only moment the thing being tested is visible.
 */
function makeManager({ settle = true, composite = true, loading = false } = {}) {
    const world = makeWorld();
    const pending = [];
    //: Enough viewport for rememberView/restoreView to be observable: every
    //: add in ViewerManager restores a held view, and a layer channel can now
    //: be the first item to land in a world the HD toggle emptied.
    const viewport = {
        center: { x: 0.5, y: 0.5 },
        zoom: 1,
        getCenter: () => viewport.center,
        getZoom: () => viewport.zoom,
        zoomTo: (value) => { viewport.zoom = value; },
        panTo: (value) => { viewport.center = value; },
    };
    const viewer = {
        world,
        viewport,
        forceRedraw() {},
        addTiledImage(options) {
            const item = {
                // A DEEP COPY, because that is what OpenSeadragon does:
                // `$.TileSource` ends in `$.extend(true, this, options)`, so
                // every nested object handed to it -- a channel's colour and
                // window among them -- is cloned, and the caller's reference
                // to it is NOT what the drawer reads back. A fake that passed
                // the object straight through made "the tile source holds this
                // record by reference" look true here while it was false in
                // every browser, which is exactly the class of bug a probe over
                // a hand-rolled world exists to catch.
                source: deepCopy(options.tileSource),
                options,
                opacity: options.opacity,
                blend: options.compositeOperation,
                //: What lets an item at opacity 0 keep loading -- OSD's
                //: getDrawArea answers `false` without it. Tracked here
                //: because a handover has to turn it back OFF: the setting
                //: that makes a swap work would, left on, make a layer faded
                //: to zero with the slider keep fetching every tile in view.
                preload: Boolean(options.preload),
                setOpacity(value) { item.opacity = value; },
                setPreload(value) { item.preload = Boolean(value); },
            };
            if (loading) {
                // OpenSeadragon's own readiness pair, as much of it as a swap
                // reads: the flag, and the event that says it moved.
                const handlers = new Set();
                item.loaded = false;
                item.getFullyLoaded = () => item.loaded;
                item.addHandler = (name, fn) => {
                    if (name === "fully-loaded-change") handlers.add(fn);
                };
                item.removeHandler = (name, fn) => handlers.delete(fn);
                item.ready = () => {
                    item.loaded = true;
                    for (const fn of [...handlers]) fn({ fullyLoaded: true });
                };
            }
            // `setBlend` sets the composite operation in place where OSD offers
            // the setter and re-adds where it does not. `composite: false` is
            // the viewer that does not, so both halves are exercised.
            if (composite) item.setCompositeOperation = (op) => { item.blend = op; };
            const land = () => {
                world.items.push(item);
                world.everAdded += 1;
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
    return { manager, world, viewport,
             flush: () => pending.splice(0).forEach((f) => f()) };
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
    // Opacity and blend are the two controls that make "higher in the stack"
    // mean something for a raster: an H&E over fluorescence hides it until
    // either one is moved. Both have to be live edits -- a slider is DRAGGED,
    // and rebuilding the world item per tick would refetch the viewport.
    const { manager, world } = makeManager();
    const handle = manager.addTiledLayer({
        layerId: "he", src: "/x/", geometry: GEOMETRY,
        opacity: 0.4, compositeOperation: "source-over",
    });
    const item = world.getItemAt(0);

    check("a layer is added at the opacity it was given",
        item.opacity === 0.4,
        "a restored render.opacity must not flash at full strength first");
    check("and with the blend it was given",
        item.blend === "source-over",
        "`lighter` over a white-grounded slide washes the whole thing out");

    handle.setOpacity(0.25);
    check("opacity is a blend on the live item, not a re-add",
        world.getItemCount() === 1 && world.getItemAt(0) === item
        && item.opacity === 0.25,
        "a re-add per pointer move would refetch every tile in view");

    handle.setBlend("lighter");
    check("a blend change is set in place where OSD allows it",
        world.getItemCount() === 1 && world.getItemAt(0) === item
        && item.blend === "lighter",
        "a composite operation is a canvas flag, not a reason to refetch tiles");
}

{
    const { manager, world } = makeManager({ composite: false });
    const handle = manager.addTiledLayer({
        layerId: "he", src: "/x/", geometry: GEOMETRY, compositeOperation: "lighter",
    });
    const first = world.getItemAt(0);
    handle.setBlend("over");
    check("a blend change re-adds where the viewer has no setter for it",
        world.getItemCount() === 1 && world.getItemAt(0) !== first
        && world.getItemAt(0).options.compositeOperation === "over",
        "the fallback is what keeps this working on a viewer without one");
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
        world.items.filter((item) => item.source.layerId === "he").length === 2,
        "one channel is a pair of blits -- see addLayerChannelSet");

    // Idempotent: adopting a newly imported layer must not restack the rest.
    manager.syncLayerImages([
        { id: "he", kind: "image", width: 500, height: 500, maxLevel: 2,
          tileWidth: 256, tileHeight: 256, transform: null,
          channels: [{ name: "he_0", src: "/generated/layer/demo/he/he_0/" }] },
    ]);
    check("re-syncing leaves a layer already on screen alone",
        world.items.filter((item) => item.source.layerId === "he").length === 2);

    manager.syncLayerImages([]);
    check("a layer the config no longer lists is dropped",
        world.getItemCount() === 0);
}

{
    // What the server stored about how this layer is drawn, on screen. Without
    // this the panel's own controls work and a reload silently forgets them.
    const { manager, world } = makeManager();
    manager.syncLayerImages([
        { id: "he", kind: "image", width: 500, height: 500, maxLevel: 2,
          tileWidth: 256, tileHeight: 256, transform: null,
          render: { opacity: 0.3 },
          channels: [{ src: "/he/" }] },
    ]);
    check("a stored opacity reaches the world item",
        world.getItemAt(0).opacity === 0.3,
        "a layer restored faded must not flash at full strength first");
}

{
    const { manager, world } = makeManager();
    // Registered before the sync, the way imageViewer.syncLayers does it from
    // a spec that says `visible: false`.
    manager.layerStack.register("he", { kind: "image", visible: false });
    manager.syncLayerImages([
        { id: "he", kind: "image", width: 500, height: 500, maxLevel: 2,
          tileWidth: 256, tileHeight: 256, transform: null,
          channels: [{ src: "/he/" }] },
    ]);
    check("a layer restored with its eye off is dropped on arrival",
        world.getItemCount() === 0,
        "a layer whose eye is off must cost nothing at all");
    check("...and was never added in the first place",
        world.everAdded === 0,
        "adding it to drop it a moment later still starts a viewport of requests");
}

{
    // The GL texture cache is keyed on `getTileKey`, which interpolates
    // `srcIdx`. Every layer used to answer 0 -- the reference image's first
    // channel -- which was invisible while layer tiles were RGB and never
    // reached that cache, and is a wrong picture the moment one is drawn as a
    // channel plane.
    const { manager, world } = makeManager();
    manager.addTiledLayer({ layerId: "a", src: "/a/a_0/", geometry: GEOMETRY });
    manager.addTiledLayer({ layerId: "b", src: "/b/b_0/", geometry: GEOMETRY });
    const reference = { srcIdx: 0, tileFormat: 16, toTileLevels: world.getItemAt(0).source.toTileLevels,
                        extraZoomLevels: 0, maxLevel: 3, width: 1000, height: 1000,
                        _tileWidth: 256, _tileHeight: 256,
                        getTileKey: world.getItemAt(0).source.getTileKey,
                        toIdealTile: world.getItemAt(0).source.toIdealTile,
                        toRealTile: world.getItemAt(0).source.toRealTile };
    const keys = new Set([
        world.getItemAt(0).source.getTileKey(0, 0, 0),
        world.getItemAt(1).source.getTileKey(0, 0, 0),
        reference.getTileKey(0, 0, 0),
    ]);
    check("two layers and a reference channel key three distinct tiles",
        keys.size === 3,
        "a shared srcIdx makes glInit's texture cache serve one plane's pixels for another");
}

{
    // OSD fits the whole slide whenever an item lands in an emptied world, so
    // every add in this class has to put back what rememberView held. A layer
    // channel can now be that first item.
    const { manager, world, viewport } = makeManager();
    viewport.center = { x: 0.25, y: 0.75 };
    viewport.zoom = 8;
    manager.rememberView();
    viewport.center = { x: 0.5, y: 0.5 };
    viewport.zoom = 1;
    manager.addTiledLayer({ layerId: "he", src: "/x/", geometry: GEOMETRY });
    check("a layer landing in an emptied world puts the view back",
        viewport.zoom === 8 && viewport.center.x === 0.25,
        "without restoreView, flipping HD re-frames the whole slide");
}

{
    // THE FEATURE: a layer added with + Add Layer draws N channels, each its
    // own world item in the GL colorize pass, exactly as the reference image's
    // channels do -- so a colour or a contrast window is a repaint and not a
    // refetch of the viewport.
    const { manager, world } = makeManager();
    const spec = {
        id: "mx", kind: "image", width: 500, height: 500, maxLevel: 2,
        tileWidth: 256, tileHeight: 256, transform: null,
        render: { channels: [
            { index: 0, name: "mx_0", color: "#ff0000", range: [10, 900] },
            { index: 1, name: "mx_1", color: "#00ff00", range: [5, 400] },
        ] },
        channels: [{ name: "mx_0", src: "/mx/mx_0/" },
                   { name: "mx_1", src: "/mx/mx_1/" },
                   { name: "mx_2", src: "/mx/mx_2/" }],
    };
    manager.syncLayerImages([spec]);
    const set = manager.tiledLayers.get("mx");

    const paint = () => world.items.filter((item) => item.blend === "lighter");
    const cover = () => world.items.filter((item) => item.blend === "destination-out");

    check("a layer's saved channels are each a world item",
        paint().length === 2,
        "one item per channel, as the reference image's channel stack is");
    check("...and each is paired with a blit that clears the way for it",
        cover().length === 2
        && cover().map((item) => item.source.src).join()
            === paint().map((item) => item.source.src).join(),
        "without it the layer adds into the image below instead of covering it");
    check("drawn through the GL colorize pass, not coloured server-side",
        world.items.every((item) => item.source.tileFormat === 16
            && !item.source.srcQuery),
        "an uncoloured plane is what makes the colour free to change");
    check("each carrying its own channel record",
        world.items.every((item) => item.source.channel)
        && paint()[0].source.channel.color.r === 255
        && paint()[1].source.channel.color.g === 255,
        "the colorize pass reads this instead of the reference channel table");
    check("...the SAME record for both halves of a channel",
        cover()[0].source.channel === paint()[0].source.channel,
        "one colour change has to move both, and they share one tile");
    check("...and its own texture-cache identity",
        paint()[0].source.getTileKey(0, 0, 0)
            !== paint()[1].source.getTileKey(0, 0, 0),
        "a shared key would draw one channel's pixels for another");
    check("...which the pair deliberately shares",
        cover()[0].source.getTileKey(0, 0, 0) === paint()[0].source.getTileKey(0, 0, 0),
        "one fetch, one decode and one colorize for the two of them");
    check("at one placement for the whole layer",
        paint()[0].options.x === paint()[1].options.x
        && paint()[0].options.width === paint()[1].options.width,
        "channels of one layer are one registration");

    const before = [...world.items];
    set.setChannels([
        { name: "mx_0", color: { r: 1, g: 2, b: 3 }, range: [0.1, 0.5] },
        { name: "mx_1", color: { r: 0, g: 255, b: 0 }, range: [0, 1] },
    ]);
    check("a colour or window change adds and removes NOTHING",
        world.getItemCount() === 4 && world.items.every((item, i) => item === before[i]),
        "a slider emits per pointer move; a re-add per tick refetches the viewport");
    check("...it mutates the record the tile source already holds",
        before[0].source.channel.color.r === 1
        && before[0].source.channel.range[1] === 0.5,
        "OpenSeadragon DEEP-COPIES a tile source, so the item has to be handed "
        + "the live record back or a channel is frozen at whatever it was added with");

    // -- the ground the layer's channels are drawn on ---------------------
    //
    // Absent by default, and that IS the feature: a registered layer is
    // transparent where its channels have no signal, so the slide underneath
    // is read through it. A background makes it opaque within its own
    // footprint instead, which is the `ground` term in
    // `ground * PRODUCT(1 - v) + SUM(c * v)`.
    const groundItems = () => world.items.filter(
        (item) => String(item.source.srcIdx || "").endsWith(":ground"));

    check("a layer has no ground until it is given one",
        groundItems().length === 0,
        "transparent where there is no signal is the default, and the whole "
        + "reason a multiplex layer can be read against the slide below it");

    set.setGround("#ffffff");
    check("a background adds exactly one item, whatever the channel count",
        groundItems().length === 1,
        "the ground is the LAYER's, not the channels': it is one opaque fill "
        + "of the footprint, and the cover blits take it away between them");
    check("...drawn under every cover blit",
        groundItems()[0][PlexoraLayerStack.ITEM_Z]
            < cover()[0][PlexoraLayerStack.ITEM_Z],
        "a ground above the covers is a ground nothing can take away");
    check("...opaquely, so what is beneath the layer is hidden",
        groundItems()[0].blend === "source-over",
        "the point of a background is that it replaces what is under it");
    check("...from a tile source of its own",
        groundItems()[0].source.src === undefined
        && String(groundItems()[0].source.getTileUrl()).startsWith("data:image/png"),
        "two world items on one url share ONE OpenSeadragon cache record and "
        + "therefore one tile canvas -- which is what makes the pair cheap and "
        + "what would make a ground and a channel overwrite each other");

    set.setGround("#ffffff");
    check("setting the same background again changes nothing",
        groundItems().length === 1,
        "syncLayerImages seeds this on every re-sync");

    set.setOpacity(0.4);
    check("the layer's opacity reaches its ground",
        groundItems()[0].opacity === 0.4,
        "a half-faded layer with a solid ground would fade to its own "
        + "background rather than to the picture underneath");

    set.setVisible(false);
    check("an eye switched off takes the ground with it",
        groundItems().length === 0,
        "a hidden layer that left an opaque rectangle behind is an eye that "
        + "did not switch anything off");
    set.setVisible(true);
    check("...and switching it back on brings it back",
        groundItems().length === 1);

    set.setGround(null);
    check("and no background means no item again",
        groundItems().length === 0,
        "null is how the card says 'use the picture underneath'");

    set.setChannels([{ name: "mx_2", color: { r: 9, g: 9, b: 9 }, range: [0, 1] }]);
    check("a channel switched off takes both of its items with it",
        world.getItemCount() === 2
        && world.items.every((item) => item.source.src === "/mx/mx_2/"),
        "a hidden channel must cost nothing, as a hidden layer does");

    set.setVisible(false);
    check("the eye drops every channel item", world.getItemCount() === 0);
    set.setChannels([
        { name: "mx_0", color: { r: 1, g: 1, b: 1 }, range: [0, 1] },
        { name: "mx_2", color: { r: 9, g: 9, b: 9 }, range: [0, 1] },
    ]);
    check("a channel switched on inside a hidden layer is not added at all",
        world.getItemCount() === 0,
        "adding it to remove it on arrival still fetches a viewport of tiles");
    set.setVisible(true);
    check("...and the eye brings every one of them back",
        world.getItemCount() === 4);
    set.setChannels([{ name: "mx_2", color: { r: 9, g: 9, b: 9 }, range: [0, 1] }]);
    set.setOpacity(0.4);
    check("opacity reaches every channel item",
        world.items.length === 2 && world.items.every((item) => item.opacity === 0.4),
        "the cover blit has to fade with the colour or the layer fades to black");
}

{
    // THE ONE THAT SAYS A LAYER IS AN IMAGE THAT SITS OVER ANOTHER ONE.
    //
    // Its channels still add among themselves, the way the reference image's
    // do -- switch two on and both are on screen, and where they overlap the
    // colours mix. What the pairing adds is that the group of them takes the
    // picture underneath away in proportion to its own coverage, so a
    // fluorescence layer reads OVER an H&E instead of adding into it and
    // washing out. Neither `lighter` alone (invisible over a bright ground)
    // nor `source-over` per channel (one channel at a time, which is what the
    // retired Add/Over control did) says both of those at once.
    const { manager, world } = makeManager();
    manager.syncLayerImages([{
        id: "mx", kind: "image", width: 500, height: 500, maxLevel: 2,
        tileWidth: 256, tileHeight: 256, transform: null,
        channels: [{ name: "mx_0", src: "/mx/mx_0/" },
                   { name: "mx_1", src: "/mx/mx_1/" }],
        render: { channels: [{ index: 0, name: "mx_0", color: "#ff0000" },
                             { index: 1, name: "mx_1", color: "#00ff00" }] },
    }]);
    const set = manager.tiledLayers.get("mx");
    check("every channel of a layer still adds as a reference channel does",
        world.items.filter((item) => item.blend === "lighter").length === 2,
        "source-over between a layer's own channels shows one at a time");
    check("...and the layer as a whole clears the way for itself",
        world.items.filter((item) => item.blend === "destination-out").length === 2,
        "lighter alone means a layer over a white H&E is invisible");
    check("...nothing asks the shader for an intensity-driven alpha per item",
        world.items.every((item) => item.source.channel
            && !("alphaFromIntensity" in item.source.channel)),
        "coverage is a property of the tile, not of one of the two blits");
    check("...and there is no blend left to set on a channel set",
        set.setBlend === undefined,
        "a per-layer composite operation is what broke multichannel layers");
    check("every clearing blit is marked to sit below every colour blit",
        world.items.every((item) => item[PlexoraLayerStack.ITEM_Z]
            === (item.blend === "destination-out" ? 0 : 1)),
        "interleaved, a later channel dims an earlier channel's colour");
}

{
    // The reference image's kind used to decide this, and a brightfield one
    // flipped the layer to Over. It no longer does: a layer over an H&E is
    // still a multichannel image, and its channels still have to add up.
    const { manager, world } = makeManager();
    manager.imageViewer.config.image_kind = "brightfield";
    manager.syncLayerImages([{
        id: "mx", kind: "image", width: 500, height: 500, maxLevel: 2,
        tileWidth: 256, tileHeight: 256, transform: null,
        channels: [{ name: "mx_0", src: "/mx/mx_0/" }],
    }]);
    check("the reference image's kind does not change how a layer composites",
        world.items.filter((item) => item.blend === "lighter").length === 1
        && world.items.filter((item) => item.blend === "destination-out").length === 1,
        "where a layer sits is stack order and opacity, not a per-pixel op");
    check("...and layerStack no longer answers a question nobody asks",
        PlexoraLayerStack.defaultBlendFor === undefined,
        "two readers of one default is what kept the card and canvas in step");
}

{
    // An rgb layer keeps the one-item, server-coloured path: its bytes are
    // already the picture, so there is no channel to pick and no window.
    const { manager, world } = makeManager();
    manager.syncLayerImages([{
        id: "he", kind: "image", width: 500, height: 500, maxLevel: 2,
        tileWidth: 256, tileHeight: 256, transform: null,
        render: { rgb: true, color: "#ff8800" },
        channels: [{ name: "he_rgb", src: "/he/rgb/" }],
    }]);
    check("an rgb layer is still one server-coloured item, drawn over",
        world.getItemCount() === 1
        && world.getItemAt(0).source.tileFormat === 24
        && world.getItemAt(0).blend === "source-over"
        && world.getItemAt(0).source.srcQuery === "color=ff8800",
        "there is no channel to choose and no window to move");
}

{
    // The HD toggle changes every tile address without changing anything on
    // the handle, so the items have to be rebuilt for OSD to notice.
    const { manager, world } = makeManager();
    manager.syncLayerImages([{
        id: "mx", kind: "image", width: 500, height: 500, maxLevel: 2,
        tileWidth: 256, tileHeight: 256, transform: null,
        channels: [{ name: "mx_0", src: "/mx/mx_0/" },
                   { name: "mx_1", src: "/mx/mx_1/" }],
        render: { channels: [{ index: 0, name: "mx_0", color: "#ffffff" },
                             { index: 1, name: "mx_1", color: "#ffffff" }] },
    }]);
    const first = [...world.items];
    world.mark();
    await manager.setHdMode(true);
    check("the HD toggle refetches every layer channel",
        world.getItemCount() === 4
        && world.items.every((item) => !first.includes(item)),
        "invalidating in place leaves the old canvases on screen");
    check("...and the pair comes back the right way up",
        world.items.every((item) => item[PlexoraLayerStack.ITEM_Z]
            === (item.blend === "destination-out" ? 0 : 1)),
        "a re-add is exactly where insertion order stops being enough");
    check("...without the layer ever leaving the world on the way",
        world.fewest === 4,
        "remove-then-add is a hole the size of the layer, for as long as a "
        + "viewport of tiles takes to arrive");
    check("...and every replacement arrives invisible and preloading",
        world.items.every((item) => item.options.opacity === 0
                                 && item.options.preload === true),
        "an item added at full opacity beside the one it replaces is a "
        + "double-bright flash; one without preload never loads at all");
    check("...revealed only once it is ready, at the layer's own opacity",
        world.items.every((item) => item.opacity === 1 && item.preload === false),
        "a handover that forgets to fade the new item up hides the layer; "
        + "one that leaves preload on makes a faded-out layer keep fetching");
    check("...and the old quality is what was on screen until then",
        first.every((item) => item.source.hd === false)
        && world.items.every((item) => item.source.hd === true),
        "reading the live flag in getTileUrl would have the outgoing item "
        + "fetching the incoming quality for anything panned onto mid-swap");

    world.mark();
    await manager.setHdMode(false);
    check("and back again, still without a gap",
        world.getItemCount() === 4 && world.fewest === 4
        && world.items.every((item) => item.source.hd === false));
}

{
    // THE MIDDLE OF THE SWAP, which is the only moment any of this is about.
    // Everything above looks at the world once the dust has settled, where a
    // rebuild that blanks and a rebuild that does not end up identical. Here
    // the replacements land but cannot draw yet, and the question is what is
    // on screen while that is true.
    const { manager, world } = makeManager({ loading: true });
    manager.syncLayerImages([{
        id: "mx", kind: "image", width: 500, height: 500, maxLevel: 2,
        tileWidth: 256, tileHeight: 256, transform: null,
        channels: [{ name: "mx_0", src: "/mx/mx_0/" }],
        render: { channels: [{ index: 0, name: "mx_0", color: "#ffffff" }] },
    }]);
    const before = [...world.items];
    for (const item of before) item.ready();

    const swap = manager.setHdMode(true);
    await Promise.resolve();
    const fresh = world.items.filter((item) => !before.includes(item));
    check("mid-swap, both qualities are on the world at once",
        world.getItemCount() === 4 && fresh.length === 2,
        "there is no other way to change every tile address without a gap");
    check("...but only the old one is drawn",
        before.every((item) => world.items.includes(item) && item.opacity === 1)
        && fresh.every((item) => item.opacity === 0),
        "this is the whole of the fix: the picture on screen is the one that "
        + "can still be drawn");
    check("...and the new one is loading rather than waiting to be asked",
        fresh.every((item) => item.preload === true && item.source.hd === true));

    // Half the pair. Nothing may move until BOTH can draw: a cover blit
    // revealed without its paint blit takes the base away and puts nothing
    // back, which is this layer's shape as a dark hole.
    fresh[0].ready();
    await Promise.resolve();
    check("one half ready is not a handover",
        world.getItemCount() === 4 && fresh[1].opacity === 0
        && before.every((item) => world.items.includes(item)));

    fresh[1].ready();
    await swap;
    check("both halves ready is",
        world.getItemCount() === 2
        && world.items.every((item) => fresh.includes(item) && item.opacity === 1
                                    && item.preload === false),
        "and the old pair goes in the same frame the new one appears");
}

{
    // A layer switched OFF while its replacement is loading. The replacement
    // is invisible, so nothing on screen says it is there -- and it is
    // preloading, so it costs a viewport of tiles for as long as it stays.
    const { manager, world } = makeManager({ loading: true });
    const handle = manager.addTiledLayer({
        layerId: "he", src: "/generated/layer/demo/he/he_0/", geometry: GEOMETRY,
    });
    world.items[0].ready();
    handle.setStyle(undefined);
    await Promise.resolve();
    check("a refetch in flight is a second item on the world",
        world.getItemCount() === 2);

    handle.setVisible(false);
    check("...and the eye takes it with the one it was replacing",
        world.getItemCount() === 0,
        "an invisible item nobody can see is still fetching every tile in view");
}

console.log(failures.length ? `\n${failures.length} check(s) failed`
                            : "\nall checks passed");
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
