/**
 * The plugin rendering API: one canvas, one pass per frame, N plugins on it.
 *
 * A plugin that wants to draw its own geometry used to build its own
 * CanvasOverlayHd -- which is what ROI did. Five rules replace that, and each
 * exists because getting it wrong is invisible until it is expensive.
 *
 *   **One pass, not one per channel.** CanvasOverlayHd calls onRedraw once per
 *   world item. An overlay that does not claim exactly one pass paints
 *   everything N times: invisible at full opacity, obvious the moment anything
 *   is translucent. Core owns that guard now -- see layer_stack_probe.mjs for
 *   why it is the stack's anchor and not `index !== 0`.
 *
 *   **save()/restore() around every draw.** A plugin that leaves a clip, a
 *   globalAlpha or a transform behind would otherwise corrupt every overlay
 *   after it, and the symptom would appear in somebody else's code.
 *
 *   **A throw does not stop the frame.** One broken plugin must not take the
 *   rest of the picture with it.
 *
 *   **Order follows the layer.** An overlay naming a layer moves when the user
 *   drags that layer's card. That is the whole reason the layer is the unit of
 *   ordering rather than the plugin.
 *
 *   **px, not zoom.** Line widths are in IMAGE pixels, so a 2px stroke is a 20px
 *   slab at 10x. Core passes `px = 1 / zoom` so the idiom is a multiply, and
 *   nobody has to rediscover the divide.
 *
 * Run directly:  node tests/js/overlay_api_probe.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.join(here, "..", "..");

// No requestAnimationFrame: the host then repaints synchronously, which is what
// makes invalidate() observable here at all.
(0, eval)(readFileSync(path.join(REPO, "plexora/client/src/js/views/layerStack.js"), "utf8"));
const { LayerStack, OverlayHost, REFERENCE_LAYER_ID, MASK_LAYER_ID } = globalThis.PlexoraLayerStack;

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

/** A 2D context that records what was done to it, and enforces save/restore
 *  pairing the way a real one does. */
function fakeContext() {
    const log = [];
    let depth = 0;
    return {
        log,
        get depth() { return depth; },
        save() { depth += 1; log.push("save"); },
        restore() { depth -= 1; log.push("restore"); },
        transform(...args) { log.push(`transform(${args.join(",")})`); },
        note(text) { log.push(text); },
    };
}

function hostWith(stack, repaints = []) {
    return new OverlayHost({ stack, repaint: () => repaints.push(1) });
}


// -- drawing order -------------------------------------------------------

{
    const stack = new LayerStack();
    stack.register(REFERENCE_LAYER_ID, { kind: "image" });
    stack.register(MASK_LAYER_ID, { kind: "labels" });
    stack.register("roi", { kind: "shapes" });
    const host = hostWith(stack);
    const context = fakeContext();

    host.add({ id: "b", layerId: "roi", draw: (o) => o.context.note("roi") });
    host.add({ id: "a", layerId: MASK_LAYER_ID, draw: (o) => o.context.note("mask") });

    host.drawAll({ context, zoom: 1 });
    check("overlays draw in their layers' stack order, not registration order",
        context.log.filter((e) => e === "roi" || e === "mask").join(",") === "mask,roi",
        context.log.join(" "));

    stack.setOrder(["roi", MASK_LAYER_ID, REFERENCE_LAYER_ID]);
    const second = fakeContext();
    host.drawAll({ context: second, zoom: 1 });
    check("restacking a layer restacks what is drawn on it",
        second.log.filter((e) => e === "roi" || e === "mask").join(",") === "roi,mask",
        "which is why the layer is the unit of ordering rather than the plugin");
}

{
    const host = hostWith(new LayerStack());
    const context = fakeContext();
    host.add({ id: "first", draw: (o) => o.context.note("1") });
    host.add({ id: "second", draw: (o) => o.context.note("2") });
    host.drawAll({ context, zoom: 1 });
    check("an overlay naming no layer keeps its registration order",
        context.log.filter((e) => e === "1" || e === "2").join(",") === "1,2");
}


// -- isolation -----------------------------------------------------------

{
    const host = hostWith(new LayerStack());
    const context = fakeContext();
    host.add({ id: "messy", draw: (o) => { o.context.save(); o.context.note("leak"); } });
    host.add({ id: "tidy", draw: (o) => o.context.note("tidy") });

    host.drawAll({ context, zoom: 1 });

    check("every draw is wrapped in save/restore",
        context.log.filter((e) => e === "save").length >= 2
        && context.log.filter((e) => e === "restore").length >= 2,
        context.log.join(" "));
    check("a plugin that leaks a save does not leak it into the next overlay",
        context.log.indexOf("tidy") > context.log.indexOf("leak"),
        "the restore is unconditional, whatever the draw did");
}

{
    const host = hostWith(new LayerStack());
    const context = fakeContext();
    const seen = [];
    host.add({ id: "broken", draw: () => { throw new Error("boom"); } });
    host.add({ id: "fine", draw: () => seen.push("fine") });

    const drawn = host.drawAll({ context, zoom: 1 });

    check("one plugin throwing does not stop the rest of the frame",
        seen.join(",") === "fine" && drawn === 1,
        "a broken plugin must not take the picture with it");
    check("and the context is left balanced",
        context.depth === 0, `depth ${context.depth}`);
}


// -- the transform -------------------------------------------------------

{
    const stack = new LayerStack();
    stack.register("he", { kind: "image", transform: [2, 0, 0, 2, 10, 20] });
    const host = hostWith(stack);
    const context = fakeContext();
    host.add({ id: "on-he", layerId: "he", draw: (o) => o.context.note("drew") });

    host.drawAll({ context, zoom: 1 });

    check("an overlay is drawn through its layer's transform",
        context.log.includes("transform(2,0,0,2,10,20)"),
        context.log.join(" "));
}

{
    const stack = new LayerStack();
    stack.register("plain", { kind: "shapes" });
    const host = hostWith(stack);
    const context = fakeContext();
    host.add({ id: "x", layerId: "plain", draw: (o) => o.context.note("drew") });
    host.drawAll({ context, zoom: 1 });
    check("a layer with no transform costs no transform call",
        !context.log.some((e) => e.startsWith("transform(")),
        "'never registered' draws identically to 'registered and aligned'");
}


// -- px -------------------------------------------------------------------

{
    const host = hostWith(new LayerStack());
    let seen = null;
    host.add({ id: "x", draw: (o) => { seen = o; } });

    host.drawAll({ context: fakeContext(), zoom: 4 });
    check("px is the inverse of the zoom", seen.px === 0.25, String(seen.px));

    host.drawAll({ context: fakeContext(), zoom: 0 });
    check("a nonsense zoom falls back to 1 rather than an infinite line width",
        seen.px === 1, String(seen.px));

    host.drawAll({ context: fakeContext(), zoom: 1e-12 });
    check("and an absurdly small one is capped",
        Number.isFinite(seen.px) && seen.px <= 1e6, String(seen.px));
}


// -- visibility ------------------------------------------------------------

{
    const stack = new LayerStack();
    stack.register("roi", { kind: "shapes" });
    const host = hostWith(stack);
    const context = fakeContext();
    const handle = host.add({ id: "x", layerId: "roi", draw: (o) => o.context.note("drew") });

    handle.setVisible(false);
    host.drawAll({ context, zoom: 1 });
    check("a hidden overlay is skipped entirely",
        !context.log.includes("drew"),
        "genuinely skipped, rather than a callback that returns early");

    handle.setVisible(true);
    stack.setVisible("roi", false);
    const second = fakeContext();
    host.drawAll({ context: second, zoom: 1 });
    check("hiding the LAYER hides what is drawn on it",
        !second.log.includes("drew"));

    stack.setVisible("roi", true);
    const third = fakeContext();
    host.drawAll({ context: third, zoom: 1 });
    check("and showing it brings it back", third.log.includes("drew"));
}

{
    const host = hostWith(new LayerStack());
    const context = fakeContext();
    host.add({ id: "orphan", layerId: "not-registered-yet", draw: (o) => o.context.note("drew") });
    host.drawAll({ context, zoom: 1 });
    check("an overlay whose layer does not exist yet still draws",
        context.log.includes("drew"),
        "a plugin may register its drawing before the layer list arrives");
}


// -- lifecycle -------------------------------------------------------------

{
    const repaints = [];
    const host = hostWith(new LayerStack(), repaints);
    const handle = host.add({ id: "x", draw: () => {} });

    check("registering asks for a repaint", repaints.length === 1);

    handle.invalidate();
    check("invalidate asks for another", repaints.length === 2);

    check("re-adding the same id replaces rather than duplicates",
        (() => {
            host.add({ id: "x", draw: () => {} });
            return host.ids().filter((id) => id === "x").length === 1;
        })());

    handle.remove();
    check("removing takes it out of the list", !host.has("x"));
    check("removing something twice is not an error", host.remove("x") === false);
}

{
    const host = hostWith(new LayerStack());
    check("an overlay with no draw function is refused",
        host.add({ id: "x" }) === null,
        "rather than registered as something that silently draws nothing");
    check("an overlay with no id is refused", host.add({ draw: () => {} }) === null);
}


// -- hit testing -----------------------------------------------------------

{
    const stack = new LayerStack();
    stack.register("a", { kind: "shapes" });
    stack.register("b", { kind: "shapes" });
    const host = hostWith(stack);
    host.add({ id: "under", layerId: "a", draw: () => {}, hitTest: () => "a" });
    host.add({ id: "over", layerId: "b", draw: () => {}, hitTest: () => "b" });

    check("the topmost overlay answers first",
        host.hitTest(1, 1)?.hit === "b",
        "'this one' means the thing the user can see");

    check("nothing under the point is null", (() => {
        const empty = hostWith(new LayerStack());
        empty.add({ id: "x", draw: () => {}, hitTest: () => null });
        return empty.hitTest(0, 0) === null;
    })());
}

{
    const stack = new LayerStack();
    stack.register("he", { kind: "image", transform: [2, 0, 0, 2, 10, 20] });
    const host = hostWith(stack);
    let got = null;
    host.add({
        id: "x", layerId: "he", draw: () => {},
        hitTest: (x, y) => { got = [x, y]; return true; },
    });

    host.hitTest(30, 40);

    check("a hit test point is put back into the layer's own space",
        got[0] === 10 && got[1] === 10,
        `(30,40) in the reference -> (${got}) in the layer -- through the inverse`);
}

{
    const host = hostWith(new LayerStack());
    host.add({ id: "broken", draw: () => {}, hitTest: () => { throw new Error("boom"); } });
    host.add({ id: "fine", draw: () => {}, hitTest: () => "ok" });
    check("a hit test that throws does not stop the ones under it",
        host.hitTest(0, 0)?.hit === "ok");
}


console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
