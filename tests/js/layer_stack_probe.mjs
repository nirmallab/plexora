/**
 * The layer stack: one ordered list of everything the viewer draws.
 *
 * Four rules are pinned here, and the first two are the ones that cost pixels if
 * they are wrong.
 *
 *   **The anchor.** CanvasOverlayHd calls onRedraw once per world item, each
 *   time with the context pre-transformed into THAT item's image space. An
 *   overlay that does not claim exactly one pass is drawn once per channel --
 *   seven times at seven channels. It has been invisible only because every item
 *   currently shares one transform. The guard has to be "which item is the
 *   reference image", and `index !== 0` is not that: item 0 is the RGB base for
 *   a brightfield project, whichever channel was added first for a fluorescence
 *   one, and whatever was last dragged after any setItemIndex.
 *
 *   **applyWorldOrder must agree with raiseLabelLayer for today's world.** It
 *   replaces it, and a project with one image and a mask has to come out
 *   stacked exactly as it was -- otherwise the mask is behind the tissue and
 *   nothing says so.
 *
 *   **Order is z-order, and a partial order never drops a layer.** Same rule the
 *   cell-layer registry has always followed, now stated once.
 *
 *   **The reserved ids match project.py's.** The client never reads project.py,
 *   so the two lists are kept in step by hand -- and by this.
 *
 * Run directly:  node tests/js/layer_stack_probe.mjs
 * Exit 0 = every rule holds. Exit 1 = not, with the reasons on stderr.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.join(here, "..", "..");
const SOURCE = path.join(REPO, "plexora/client/src/js/views/layerStack.js");

// The shipped file, run as the page runs it.
(0, eval)(readFileSync(SOURCE, "utf8"));
const {
    LayerStack, SubLayerStack, invertTransform,
    LAYER_KINDS, LAYER_KIND_SURFACE,
    REFERENCE_LAYER_ID, MASK_LAYER_ID, CENTROID_LAYER_ID,
} = globalThis.PlexoraLayerStack;

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

/** Enough of OSD's World to stack items in: an array with its four methods. */
function fakeWorld(items = []) {
    return {
        _items: items,
        getItemCount() { return this._items.length; },
        getItemAt(i) { return this._items[i]; },
        getIndexOfItem(item) { return this._items.indexOf(item); },
        setItemIndex(item, index) {
            const at = this._items.indexOf(item);
            if (at < 0) return;
            this._items.splice(at, 1);
            this._items.splice(index, 0, item);
        },
    };
}

const tiledImage = (tileFormat, layerId) => ({ source: { tileFormat, layerId } });

function stackWith(world) {
    return new LayerStack({ viewer: { world, forceRedraw() {} } });
}


// -- kinds and surfaces --------------------------------------------------

check("every kind names a surface",
    LAYER_KINDS.every((kind) => ["tiles", "overlay", "gl"].includes(LAYER_KIND_SURFACE[kind])),
    JSON.stringify(LAYER_KIND_SURFACE));

check("there are four kinds, not one per modality",
    LAYER_KINDS.join(",") === "image,labels,points,shapes",
    "a kind is a rendering strategy; a modality is not");

check("an unknown kind falls back to something with a renderer", (() => {
    const stack = new LayerStack();
    return stack.register("x", { kind: "hologram" }).kind === "image";
})());


// -- order is z-order ----------------------------------------------------

{
    const stack = new LayerStack();
    stack.register("a", { kind: "image" });
    stack.register("b", { kind: "image" });
    stack.register("c", { kind: "points" });

    check("a new layer goes on top",
        stack.order().join(",") === "a,b,c");

    check("drawList filters to one surface, in stack order",
        stack.drawList("tiles").map((l) => l.id).join(",") === "a,b"
        && stack.drawList("overlay").map((l) => l.id).join(",") === "c");

    stack.setVisible("a", false);
    check("a hidden layer is not in the draw list",
        stack.drawList("tiles").map((l) => l.id).join(",") === "b");
    stack.setVisible("a", true);

    stack.setOrder(["c", "a"]);
    check("an unmentioned layer keeps its place underneath rather than falling off",
        stack.order().join(",") === "b,c,a",
        "a partial order must never drop a layer off the stack");

    check("reordering to the same order reports no change",
        stack.setOrder(stack.order()) === false);

    check("an id that is not registered is ignored",
        stack.setOrder(["nope"]) === false && stack.order().length === 3);
}


// -- the anchor ----------------------------------------------------------

{
    // A fluorescence project: three channels, then the mask on top.
    const world = fakeWorld([
        tiledImage(16, REFERENCE_LAYER_ID),
        tiledImage(16, REFERENCE_LAYER_ID),
        tiledImage(16, REFERENCE_LAYER_ID),
        tiledImage(32, MASK_LAYER_ID),
    ]);
    check("the anchor is the reference image, not the mask",
        stackWith(world).anchorIndex() === 0);

    // The same world after the user dragged the mask to the bottom.
    const dragged = fakeWorld([
        tiledImage(32, MASK_LAYER_ID),
        tiledImage(16, REFERENCE_LAYER_ID),
    ]);
    check("the anchor follows the reference image, not index 0",
        stackWith(dragged).anchorIndex() === 1,
        "`index !== 0` would have picked the mask here");

    // A world whose items predate the tagging -- a channel added before the
    // layer list arrived.
    const untagged = fakeWorld([tiledImage(32, undefined), tiledImage(16, undefined)]);
    check("an untagged world still refuses to anchor on the label layer",
        stackWith(untagged).anchorIndex() === 1);

    check("an empty world has no anchor",
        stackWith(fakeWorld([])).anchorIndex() === -1,
        "-1 equals no index, so every overlay draws nothing");

    check("a world of nothing but a mask anchors on it rather than nowhere",
        stackWith(fakeWorld([tiledImage(32, MASK_LAYER_ID)])).anchorIndex() === 0);
}


// -- applyWorldOrder -----------------------------------------------------

/** What ViewerManager.raiseLabelLayer did, kept here as the thing to match. */
function raiseLabelLayer(world) {
    for (let i = 0; i < world.getItemCount(); i += 1) {
        const item = world.getItemAt(i);
        if (item?.source?.tileFormat === 32) {
            world.setItemIndex(item, world.getItemCount() - 1);
            return;
        }
    }
}

{
    const items = [
        tiledImage(16, REFERENCE_LAYER_ID),
        tiledImage(32, MASK_LAYER_ID),
        tiledImage(16, REFERENCE_LAYER_ID),
    ];
    const before = fakeWorld([...items]);
    raiseLabelLayer(before);

    const world = fakeWorld([...items]);
    const stack = stackWith(world);
    stack.register(REFERENCE_LAYER_ID, { kind: "image" }).items = [items[0], items[2]];
    stack.register(MASK_LAYER_ID, { kind: "labels" }).items = [items[1]];
    stack.applyWorldOrder();

    check("applyWorldOrder stacks a mask exactly where raiseLabelLayer did",
        world._items.map((i) => items.indexOf(i)).join(",")
        === before._items.map((i) => items.indexOf(i)).join(","),
        `${world._items.map((i) => items.indexOf(i))} vs ${before._items.map((i) => items.indexOf(i))}`);

    check("channels keep their relative order",
        world._items.indexOf(items[0]) < world._items.indexOf(items[2]));
}

{
    // The case raiseLabelLayer could not express: a second image above the mask.
    const base = tiledImage(16, REFERENCE_LAYER_ID);
    const mask = tiledImage(32, MASK_LAYER_ID);
    const he = tiledImage(24, "he");
    const world = fakeWorld([base, mask, he]);
    const stack = stackWith(world);
    stack.register(REFERENCE_LAYER_ID, { kind: "image" }).items = [base];
    stack.register("he", { kind: "image" }).items = [he];
    stack.register(MASK_LAYER_ID, { kind: "labels" }).items = [mask];
    stack.setOrder([REFERENCE_LAYER_ID, "he", MASK_LAYER_ID]);
    stack.applyWorldOrder();

    check("a second image layer can sit between the base and the mask",
        world._items.map((i) => [base, he, mask].indexOf(i)).join(",") === "0,1,2");
}

{
    // An item nobody claimed must not be shuffled to the bottom.
    const claimed = tiledImage(16, REFERENCE_LAYER_ID);
    const orphan = tiledImage(16, undefined);
    const world = fakeWorld([orphan, claimed]);
    const stack = stackWith(world);
    stack.register(REFERENCE_LAYER_ID, { kind: "image" }).items = [claimed];
    stack.applyWorldOrder();

    check("an unclaimed world item keeps its place",
        world._items.includes(orphan) && world._items.length === 2);
}


// -- transforms ----------------------------------------------------------

{
    const stack = new LayerStack();
    const layer = stack.register("x", { kind: "image" });

    check("no transform is null, not identity",
        layer.transform === null && stack.affineOf("x").join(",") === "1,0,0,1,0,0",
        "'never registered' and 'registered, and it lined up' are different claims");

    stack.setTransform("x", [2, 0, 0, 2, 10, 20]);
    check("a transform is stored verbatim, in canvas order",
        stack.get("x").transform.join(",") === "2,0,0,2,10,20");

    const inv = stack.get("x").inverse;
    const apply = ([a, b, c, d, e, f], x, y) => [a * x + c * y + e, b * x + d * y + f];
    const [fx, fy] = apply(stack.get("x").transform, 3, 5);
    const [bx, by] = apply(inv, fx, fy);
    check("the inverse round-trips a point",
        Math.abs(bx - 3) < 1e-9 && Math.abs(by - 5) < 1e-9,
        `(3,5) -> (${fx},${fy}) -> (${bx},${by})`);

    stack.setTransform("x", [0, 0, 0, 0, 0, 0]);
    check("a singular transform has no inverse rather than a wrong one",
        stack.get("x").inverse === null);

    stack.setTransform("x", [1, 2, 3]);
    check("a transform of the wrong length is refused, not padded",
        stack.get("x").transform === null);

    check("invertTransform reports a singular matrix as null",
        invertTransform([1, 1, 1, 1, 0, 0]) === null);
}


// -- describe ------------------------------------------------------------

{
    const item = tiledImage(16, REFERENCE_LAYER_ID);
    const world = fakeWorld([item]);
    const stack = stackWith(world);
    stack.register(REFERENCE_LAYER_ID, { kind: "image" }).items = [item];
    stack.register(CENTROID_LAYER_ID, { kind: "points", transform: [1, 0, 0, 1, 5, 5] });

    const described = stack.describe();
    check("describe says what is stacked, where, and on what",
        described.length === 2
        && described[0].id === REFERENCE_LAYER_ID
        && described[0].surface === "tiles"
        && described[0].worldIndex.join(",") === "0"
        && described[1].surface === "overlay"
        && described[1].transform.join(",") === "1,0,0,1,5,5",
        JSON.stringify(described));

    check("describe reports order, not just membership",
        described.map((d) => d.order).join(",") === "0,1");
}


// -- the sub-stack -------------------------------------------------------

{
    const sub = new SubLayerStack({ makeRecord: (name) => ({ name, hits: 0 }) });
    sub.register("a");
    sub.register("b");
    sub.get("a").hits = 7;

    check("re-registering keeps everything the record holds",
        sub.register("a").hits === 7,
        "switching a tool away and back has to be instant, not a reload");

    check("order is membership order, bottom first",
        sub.order().join(",") === "a,b");

    sub.setActive("b");
    check("unregistering the active sub-layer hands over to the topmost survivor",
        sub.unregister("b") === "a");

    check("unregistering something that is not there reports nothing removed",
        sub.unregister("zzz") === undefined);

    sub.unregister("a");
    check("unregistering the last one leaves nothing active",
        sub.active === null);
}


// -- the reserved ids ----------------------------------------------------

{
    const projectPy = readFileSync(path.join(REPO, "plexora/server/models/project.py"), "utf8");
    const stated = (name) => {
        const m = new RegExp(`^${name} = "([^"]+)"`, "m").exec(projectPy);
        return m && m[1];
    };
    check("the client's reserved ids are the ones project.py synthesizes",
        stated("REFERENCE_LAYER_ID") === REFERENCE_LAYER_ID
        && stated("MASK_LAYER_ID") === MASK_LAYER_ID
        && stated("CENTROID_LAYER_ID") === CENTROID_LAYER_ID,
        `${stated("REFERENCE_LAYER_ID")}/${stated("MASK_LAYER_ID")}/${stated("CENTROID_LAYER_ID")}`);

    const kinds = /^LAYER_KINDS = \(([^)]*)\)/m.exec(projectPy);
    const pyKinds = kinds ? kinds[1].match(/"([a-z]+)"/g).map((s) => s.slice(1, -1)) : [];
    check("the client's kinds are the ones project.py validates against",
        pyKinds.join(",") === LAYER_KINDS.join(","),
        `${pyKinds.join(",")} vs ${LAYER_KINDS.join(",")}`);
}


console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
