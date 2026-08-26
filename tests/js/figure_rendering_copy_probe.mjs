/**
 * Copying one panel's rendering onto others.
 *
 * A figure is routinely eight crops of one slide, and they have to agree about
 * what colour CD8 is and where its contrast sits. Every decision here is one
 * that ships green and is wrong in the figure:
 *
 *   * matching channels BY POSITION across two images puts a nuclear channel's
 *     window on whatever happened to be third in the other file. A key is a
 *     path inside one file, not a stain;
 *
 *   * a channel that has no counterpart in the target image has to be
 *     REPORTED. A panel that quietly lost a marker looks exactly like a panel
 *     that never had one, and the author finds out from the exported figure;
 *
 *   * applying to five panels in five commits is five presses of Ctrl+Z with
 *     the figure sitting half-applied in between;
 *
 *   * the compositing arithmetic has to be the exporter's. The windows were
 *     chosen by eye against it, so a preview that adds channels differently is
 *     a picture of a figure nobody is going to get.
 *
 * Run directly:
 *   node tests/js/figure_rendering_copy_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
const SCRIPTS = ["figureSchema.js", "figurePanelCompositor.js"];

const problems = [];

function check(what, got, want) {
    const a = JSON.stringify(got);
    const b = JSON.stringify(want);
    if (a !== b) problems.push({ what, got: a, want: b });
}

function canvasStub() {
    const canvas = {
        width: 1, height: 1,
        getContext: () => ({
            putImageData(pixels) { canvas.pixels = pixels; },
            drawImage() {}, fillRect() {}, fillStyle: "",
        }),
        toDataURL: () => "data:image/webp;base64,SHEET",
        toBlob: (resolve) => resolve({ type: "image/webp" }),
    };
    return canvas;
}

const ctx = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
    Date, Promise, Error, parseFloat, parseInt, isNaN, isFinite,
    Uint8ClampedArray, Uint16Array,
    document: { createElement: () => canvasStub(), getElementById: () => null },
    window: { addEventListener() {}, removeEventListener() {} },
});
ctx.globalThis = ctx;
ctx.ImageData = class {
    constructor(width, height) {
        this.width = width;
        this.height = height;
        this.data = new Uint8ClampedArray(width * height * 4);
    }
};
for (const name of SCRIPTS) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}

const run = (source) => runInContext(`(() => { ${source} })()`, ctx);

// -- which channel becomes which ---------------------------------------------

ctx.__copied = [
    { key: "ch_0", fullname_at_capture: "DNA",
      color: { r: 0, g: 0, b: 255 }, window: [100, 900], visible: true },
    { key: "ch_1", fullname_at_capture: "CD8",
      color: { r: 255, g: 0, b: 0 }, window: [50, 4000], visible: true },
];

const sameSource = run(`
    return FigureSchema.mapRenderingChannels(__copied, [
        { key: "ch_0", fullname_at_capture: "DNA" },
        { key: "ch_1", fullname_at_capture: "CD8" },
    ]);
`);
check("copying within one image is an identity",
    sameSource.channels.map((channel) => [channel.key, channel.window]),
    [["ch_0", [100, 900]], ["ch_1", [50, 4000]]]);
check("and skips nothing", sameSource.skipped, []);

// The other slide holds the same two stains in the other order, under
// different keys. Matching by position or by key alone puts the DNA window on
// CD8 -- a nuclear channel's contrast on a membrane marker, which looks like a
// blown-out panel rather than like a bug.
const crossSource = run(`
    return FigureSchema.mapRenderingChannels(__copied, [
        { key: "chan_A", fullname_at_capture: "CD8" },
        { key: "chan_B", fullname_at_capture: "DNA" },
    ]);
`);
check("across images the DISPLAY NAME decides",
    crossSource.channels.map((channel) => [channel.key, channel.fullname_at_capture]),
    [["chan_B", "DNA"], ["chan_A", "CD8"]]);
check("and each keeps the window it was copied with",
    crossSource.channels.map((channel) => channel.window), [[100, 900], [50, 4000]]);

// Sources captured before names were recorded carry keys only. The key is then
// the only thing there is to match on, and matching on it is better than
// refusing the whole copy.
const byKey = run(`
    return FigureSchema.mapRenderingChannels(__copied, [
        { key: "ch_1", fullname_at_capture: "" },
        { key: "ch_0", fullname_at_capture: "" },
    ]);
`);
check("with no names to match, the key is the fallback",
    byKey.channels.map((channel) => channel.key), ["ch_0", "ch_1"]);

const partial = run(`
    return FigureSchema.mapRenderingChannels(__copied, [
        { key: "ch_0", fullname_at_capture: "DNA" },
    ]);
`);
check("a channel the target image does not have is left out",
    partial.channels.map((channel) => channel.fullname_at_capture), ["DNA"]);
// Reported, not dropped: "this panel now shows one of your two channels" is
// something the user has to be told.
check("and named, so it can be said out loud", partial.skipped, ["CD8"]);

const ambiguous = run(`
    return FigureSchema.mapRenderingChannels(__copied, [
        { key: "first", fullname_at_capture: "DNA" },
        { key: "second", fullname_at_capture: "DNA" },
    ]).channels.map((channel) => channel.key);
`);
// Two channels sharing a display name make the name ambiguous. First wins, so
// the answer does not depend on the order the file happened to list them in.
check("a duplicated name resolves to the first of them", ambiguous, ["first"]);

// A copy that handed back the SAME objects would make one panel's colour a
// second panel's colour, so dragging a slider on either moved both.
const detached = run(`
    const mapped = FigureSchema.mapRenderingChannels(__copied, [
        { key: "ch_0", fullname_at_capture: "DNA" },
    ]);
    mapped.channels[0].color.r = 42;
    mapped.channels[0].window[0] = 7;
    return [__copied[0].color.r, __copied[0].window[0]];
`);
check("the mapped channels are copies, not the originals", detached, [0, 100]);

// -- the arithmetic ----------------------------------------------------------
//
// Hand-computed against server/render.render_panel: t = clip((v - lo) / span),
// then t * colour * CHANNEL_ALPHA, added and clipped at 255.

const alpha = runInContext("FigurePanelCompositor.CHANNEL_ALPHA", ctx);
const composited = run(`
    const canvas = FigurePanelCompositor.composite([
        { data: new Uint16Array([0, 500, 1000, 1000]), width: 2, height: 2,
          window: [0, 1000], color: { r: 0, g: 0, b: 255 } },
        { data: new Uint16Array([0, 0, 0, 1000]), width: 2, height: 2,
          window: [0, 1000], color: { r: 255, g: 0, b: 0 } },
    ]);
    return Array.from(canvas.pixels.data);
`);
// 255 * 0.9 = 229.5 at the top of a window, and 114.75 half way up; the canvas
// byte array rounds both. Written out rather than recomputed, so this disagrees
// with a changed alpha rather than following it.
check("channels are windowed, coloured and added", composited, [
    0, 0, 0, 255,        // both channels at zero
    0, 0, 115, 255,      // half way up the blue one
    0, 0, 230, 255,      // blue at the top of its window
    230, 0, 230, 255,    // both at the top: magenta, neither one clipping
]);
check("the alpha is the exporter's", alpha, 0.9);

// A plane of a different size is skipped rather than stretched: a stretched
// channel is a picture of the right tissue in the wrong place.
const mismatched = run(`
    const canvas = FigurePanelCompositor.composite([
        { data: new Uint16Array([1000]), width: 1, height: 1,
          window: [0, 1000], color: { r: 0, g: 0, b: 255 } },
        { data: new Uint16Array([1000, 1000, 1000, 1000]), width: 2, height: 2,
          window: [0, 1000], color: { r: 255, g: 0, b: 0 } },
    ]);
    return [canvas.width, canvas.height, Array.from(canvas.pixels.data).slice(0, 4)];
`);
check("a plane of the wrong size is left out, not stretched",
    mismatched, [1, 1, [0, 0, 230, 255]]);

check("nothing to composite is null, not a black rectangle",
    run("return FigurePanelCompositor.composite([]);"), null);

console.error(JSON.stringify({ problems }));
process.exitCode = problems.length ? 1 : 0;
