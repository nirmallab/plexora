/**
 * The BASE image layer's eye and opacity.
 *
 * A registered layer is one world item and `layer_visibility_probe.mjs` covers
 * it. The base layer is not: it is N channel items that arrive and leave as the
 * user picks channels, and switching it off has to take all of them without
 * forgetting which ones were chosen. Four rules, each with a silent failure:
 *
 *   **Hiding removes the items.** Same argument as a registered layer, and
 *   worse here: the base image is the most expensive thing on screen, and an
 *   item at opacity 0 still requests, decodes and draws every tile in view.
 *
 *   **Hiding keeps the SLOTS.** `currentChannels` is which channels the user
 *   chose, not which are on the world. Clearing it would make the sidebar
 *   forget the whole composite because somebody blinked, and there would be
 *   nothing to rebuild from when the eye came back on.
 *
 *   **A channel chosen while the base is off draws nothing.** Otherwise one
 *   channel appears on an otherwise empty world and the hide looks broken.
 *
 *   **Opacity reaches every item, including ones added later.** The slider is
 *   the layer's, not the channel's; an item added after it was moved has to
 *   arrive already wearing it, because nothing revisits it afterwards.
 *
 * And one boundary: the CELL MASK is never touched by any of this. It is on the
 * same surface and in the same stack, but the Cells footer owns whether
 * boundaries are drawn -- one control, in one place.
 *
 * Run directly:  node tests/js/layer_state_probe.mjs
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

// -- the two halves of the seam, loaded the way the page loads them --------

const stackContext = createContext({ console, window: {}, globalThis: {} });
runInContext(readFileSync(join(VIEWS, "layerStack.js"), "utf8"), stackContext);
const PlexoraLayerStack = stackContext.window.PlexoraLayerStack
    || stackContext.PlexoraLayerStack;
globalThis.PlexoraLayerStack = PlexoraLayerStack;
//: `channel_add` falls back to d3's white for a channel the sidebar has not
//: coloured yet. Only its identity is read here.
globalThis.d3 = { color: (name) => ({ name }) };

const { default: _unused, ...module } = await import(
    pathToFileURL(join(VIEWS, "viewerManager.js")).href);
const ViewerManager = module.default || module.ViewerManager;

const { REFERENCE_LAYER_ID, MASK_LAYER_ID } = PlexoraLayerStack;
const RGB_TILE_FORMAT = 24;

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

function makeManager({ imageKind = "multiplex", channels = 3 } = {}) {
    const world = makeWorld();
    const viewer = {
        world,
        addTiledImage(options) {
            const item = {
                source: options.tileSource,
                options,
                opacity: options.opacity,
                setOpacity(value) { item.opacity = value; },
            };
            world.items.push(item);
            options.success?.({ item });
        },
        raiseEvent() {},
    };
    const imageData = [];
    for (let i = 0; i < channels; i += 1) imageData.push({ src: `/tiles/demo/c${i}/` });
    // Found by its tile key, exactly as load_brightfield_base finds it -- the
    // position of an entry in imageData depends on whether the project has a
    // segmentation, which is how that function once drew the mask as the slide.
    if (imageKind === "brightfield") imageData.push({ src: "/tiles/demo/rgb/" });

    const stack = new PlexoraLayerStack.LayerStack({ viewer });
    const channelList = { currentChannels: {}, rangeConnector: {}, colorConnector: {} };
    const manager = new ViewerManager(
        {
            viewer,
            layerStack: stack,
            numericData: { bitRange: [0, 65535] },
            config: {
                width: 1000, height: 1000, extraZoomLevels: 0, maxLevel: 3,
                tileWidth: 256, tileHeight: 256, imageData, image_kind: imageKind,
            },
        },
        channelList);
    manager.viewer = viewer;
    stack.register(REFERENCE_LAYER_ID, { kind: "image" });
    return { manager, stack, world, channelList };
}

// -- the eye ---------------------------------------------------------------

{
    const { manager, stack, world, channelList } = makeManager();
    manager.channel_add(0);
    manager.channel_add(1);
    check("the base layer is its channels on the world, two blits each",
        world.getItemCount() === 4,
        "a reference channel is a cover blit and a paint blit -- the image "
        + "composites as a group now, the same way a registered layer does, "
        + "which is what lets it be drawn over one (see channel_add)");
    // OFF THE OPTIONS, not off the tile source, and the distinction is the
    // whole check: OpenSeadragon reads `compositeOperation` from what is
    // handed to `addTiledImage`, and one written inside the tileSource is
    // carried along by the deep copy and never looked at. `channel_add` had
    // `"lighter"` in exactly that dead place for years, and the blend that
    // actually ran was the viewer-wide default -- which is also `lighter`, so
    // nothing ever showed it.
    const blitsOf = (operation) => world.items.filter(
        (item) => item.options.compositeOperation === operation);
    check("...as a cover blit and a paint blit each",
        blitsOf("destination-out").length === 2 && blitsOf("lighter").length === 2,
        "got " + world.items.map((i) => i.options.compositeOperation).join(","));
    check("...with every cover below every paint",
        Math.max(...blitsOf("destination-out")
            .map((i) => i[PlexoraLayerStack.ITEM_Z]))
        < Math.min(...blitsOf("lighter").map((i) => i[PlexoraLayerStack.ITEM_Z])),
        "interleave them and a later channel's cover dims an earlier "
        + "channel's colour instead of the ground's");

    stack.setVisible(REFERENCE_LAYER_ID, false);
    check("switching the base layer off removes every channel item",
        world.getItemCount() === 0,
        "an item at opacity 0 still fetches, decodes and draws every tile");
    check("and leaves the channel slots standing",
        Object.keys(channelList.currentChannels).join(",") === "0,1",
        "currentChannels is which channels the user chose, not which are drawn");

    manager.channel_add(2);
    check("a channel chosen while the base is off draws nothing yet",
        world.getItemCount() === 0,
        "one channel on an otherwise empty world makes the hide look broken");
    check("but takes its slot, so it arrives with the rest",
        Object.keys(channelList.currentChannels).join(",") === "0,1,2");

    stack.setVisible(REFERENCE_LAYER_ID, true);
    check("switching it back on rebuilds every slot's pair",
        world.getItemCount() === 6,
        "three slots, two blits each");
    check("each one tagged as the reference layer",
        world.items.every((item) => item.source.layerId === REFERENCE_LAYER_ID),
        "anchorIndex and applyWorldOrder both read this tag");
}

// -- the slider ------------------------------------------------------------

{
    const { manager, stack, world } = makeManager();
    manager.channel_add(0);
    manager.channel_add(1);

    stack.setOpacity(REFERENCE_LAYER_ID, 0.4);
    check("the base layer's opacity reaches every channel item",
        world.items.every((item) => item.opacity === 0.4),
        "one slider for the layer, not one per channel");

    manager.channel_add(2);
    check("and a channel added afterwards arrives already wearing it",
        world.items.every((item) => item.opacity === 0.4),
        "nothing revisits an item once it has landed");
}

// -- the boundary ----------------------------------------------------------

{
    const { manager, stack, world } = makeManager();
    manager.channel_add(0);
    // The mask, as load_label_image puts it there.
    const mask = { source: { layerId: MASK_LAYER_ID, tileFormat: 32 }, options: {} };
    world.items.push(mask);
    manager.claimWorldItem(MASK_LAYER_ID, mask);

    stack.setVisible(REFERENCE_LAYER_ID, false);
    check("switching the base layer off leaves the cell mask alone",
        world.items.includes(mask) && world.getItemCount() === 1,
        "the Cells footer owns whether boundaries are drawn -- one control, one place");
    check("and the mask is pinned, because it has no card to drag",
        stack.get(MASK_LAYER_ID).pinned === true);
}

// -- the other two kinds of base ------------------------------------------

{
    const { manager, stack, world } = makeManager({ imageKind: "brightfield", channels: 0 });
    manager.load_brightfield_base();
    check("a brightfield project's base layer is its one slide item",
        world.getItemCount() === 1
        && world.getItemAt(0).source.tileFormat === RGB_TILE_FORMAT);

    stack.setOpacity(REFERENCE_LAYER_ID, 0.6);
    check("the Adjustments slider fades the slide through the stack",
        world.getItemAt(0).opacity === 0.6,
        "so that slider and the base card are one control rather than two");

    stack.setVisible(REFERENCE_LAYER_ID, false);
    check("its eye takes the slide off the world too",
        world.getItemCount() === 0);

    stack.setVisible(REFERENCE_LAYER_ID, true);
    check("and puts it back, still faded",
        world.getItemCount() === 1 && world.getItemAt(0).opacity === 0.6);
}

{
    const { manager, stack, world } = makeManager({ imageKind: "blank", channels: 0 });
    manager.load_blank_base("/generated/blank/demo/");
    check("a blank project has its one transparent frame",
        world.getItemCount() === 1);

    stack.setVisible(REFERENCE_LAYER_ID, false);
    check("and its eye is inert, because there would be nothing left",
        world.getItemCount() === 1,
        "an empty world has no anchor, no viewportImageBounds and no home rectangle");
}

console.log(failures.length ? `\n${failures.length} check(s) failed`
                            : "\nall checks passed");
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
