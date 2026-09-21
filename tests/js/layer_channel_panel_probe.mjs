/**
 * What a registered layer's channel panel stores, restores and draws.
 *
 * The panel itself is a second `ViewerSidebar` over a copy of the base card's
 * markup, and that class is already covered elsewhere -- what is new here is
 * the three TRANSLATIONS on either side of it, and each has a silent failure
 * mode that a screenshot would not catch:
 *
 *   **Saved rows -> slots.** `render.channels` holds raw 16-bit windows, the
 *   only domain that means the same thing across a reload and an HD toggle. A
 *   row read back in the wrong domain restores a window rescaled into a sliver
 *   of the real range -- the channel is still drawn, just wrong, which is the
 *   bug `toRawRangeForSlot` exists to prevent on the reference image.
 *
 *   **Slots -> `render.channels`.** Written WHOLE every time, because the
 *   PATCH merges `render` one key deep: a partial list IS the new list, so a
 *   filter that dropped a channel would delete it from the project.
 *
 *   **Slots -> what is drawn.** The shader reads one `u_tile_range` and does
 *   not know which kind of layer a tile came from, so the panel has to hand
 *   the layer set the same fractional units the reference image's
 *   `rangeConnector` holds.
 *
 * And one boundary: a layer added before `render.channels` existed named one
 * channel, one colour and one window applied server-side. It has to keep
 * opening the way it did.
 *
 * Only the pure halves are driven here. Mounting a real ViewerSidebar needs a
 * DOM this repo has no jsdom for -- `figure_quickedit_probe.mjs` stubs it for
 * the same reason -- and the scoping of that instance is
 * `tests/js/sidebar_scoping_probe.mjs`.
 *
 * Run directly:  node tests/js/layer_channel_panel_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const VIEWS = join(REPO, "plexora/client/src/js/views");

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

const ctx = createContext({
    console, Math, Number, Object, Array, Set, Map, JSON, String, Boolean,
    parseInt, parseFloat, Promise,
    // The module builds its markup at load time only inside mount(), so a
    // document that answers createElement is enough to load it.
    document: { createElement: () => ({ appendChild() {}, setAttribute() {} }) },
    window: {},
    globalThis: {},
});
runInContext(readFileSync(join(VIEWS, "layerChannelPanel.js"), "utf8"), ctx,
             { filename: "layerChannelPanel.js" });
const panel = ctx.window.PlexoraLayerChannels || ctx.globalThis.PlexoraLayerChannels;

const CHANNELS = [
    { name: "mx_0", fullname: "DAPI", src: "/generated/layer/s/mx/mx_0/" },
    { name: "mx_1", fullname: "CD3", src: "/generated/layer/s/mx/mx_1/" },
    { name: "mx_2", fullname: "CD8", src: "/generated/layer/s/mx/mx_2/" },
];

// -- the id prefix ---------------------------------------------------------

check("the id prefix is derived from the layer id",
    panel.prefixFor("mx") === "layer_mx_");
check("...and reduced to what an element id may hold",
    panel.prefixFor("HE scan (2).ome.tif") === "layer_HE_scan__2__ome_tif_",
    "a layer id comes from a filename and can hold anything one can");
check("...stably, so a rebuilt card finds the same markup",
    panel.prefixFor("mx") === panel.prefixFor("mx"));

// -- render.channels -> the saved rows the sidebar restores ---------------

{
    const spec = { render: { channels: [
        { index: 1, name: "mx_1", color: "#ff0000", range: [12, 900] },
        { index: 0, name: "mx_0", color: "#0f0", range: [3, 40] },
    ] }, channels: CHANNELS };
    const rows = panel.savedRowsFor(spec, CHANNELS);

    check("every saved channel comes back as an active row",
        rows.length === 2 && rows.every((row) => row.channel_active === true));
    check("in the order it was saved in",
        same(rows.map((r) => r.channel), ["mx_1", "mx_0"]),
        "slot order is the stacking order the user arranged");
    check("the window comes back in raw 16-bit units, untouched",
        rows[0].start === 12 && rows[0].end === 900,
        "the sidebar converts into whichever domain is showing; this must not");
    check("the colour comes back as the three bytes a row carries",
        rows[0].r === 255 && rows[0].g === 0 && rows[0].b === 0);
    check("a three-digit hex is expanded, not dropped",
        rows[1].r === 0 && rows[1].g === 255 && rows[1].b === 0);
}

{
    const rows = panel.savedRowsFor(
        { render: { channels: [{ index: 2, color: "#ffffff", range: [0, 5] }] } },
        CHANNELS);
    check("a saved row naming only an index still finds its channel",
        rows.length === 1 && rows[0].channel === "mx_2",
        "the name is carried for readability; the index is the identity");
}

{
    const rows = panel.savedRowsFor(
        { render: { channels: [{ index: 9, name: "gone" }] } }, CHANNELS);
    check("a saved row for a channel the layer no longer has is dropped",
        rows.length === 0,
        "a slot naming nothing asks the server for a channel that 404s");
}

// -- the layer saved before any of this existed ---------------------------

{
    const spec = { render: { channelIndex: 1, color: "#3366ff", range: [7, 700] } };
    const rows = panel.savedRowsFor(spec, CHANNELS);
    check("a layer saved with one channel and one colour opens as it did",
        rows.length === 1 && rows[0].channel === "mx_1"
        && rows[0].start === 7 && rows[0].end === 700
        && rows[0].b === 255,
        "this is what every registered layer in an existing project looks like");
}

check("a layer that was never styled restores nothing and auto-levels",
    panel.savedRowsFor({ render: {} }, CHANNELS).length === 0,
    "the sidebar's own default path is a better first picture than grey");

// -- the slots, on the way back out ---------------------------------------

function fakeSidebar(slots, { raw = true } = {}) {
    return {
        channelSlots: slots,
        // The real conversions are ViewerSidebar's and are tested there; what
        // matters here is WHICH of them each direction goes through.
        toRawRangeForSlot: (slot) => (raw ? slot.rawRange : slot.range),
        toImageConnectorRange: (values) => [values[0] / 255, values[1] / 255],
    };
}

{
    const slots = [
        { enabled: true, name: "mx_1", colorHex: "#ff0000",
          color: { r: 255, g: 0, b: 0 }, range: [10, 200], rawRange: [120.4, 4000.6] },
        { enabled: false, name: "mx_0", colorHex: "#00ff00",
          color: { r: 0, g: 255, b: 0 }, range: [0, 255], rawRange: [0, 65535] },
        { enabled: true, name: "", colorHex: "#ffffff",
          color: { r: 255, g: 255, b: 255 }, range: [0, 1], rawRange: [0, 1] },
    ];
    const sidebar = fakeSidebar(slots);
    const nameToIndex = { mx_0: 0, mx_1: 1, mx_2: 2 };
    const stored = panel.slotsToChannels(sidebar, nameToIndex);

    check("only an enabled, named slot is stored",
        stored.length === 1 && stored[0].name === "mx_1",
        "an empty slot is a row the user never filled in");
    check("stored with the index the server knows it by",
        stored[0].index === 1);
    check("the window is stored in raw units and rounded",
        same(stored[0].range, [120, 4001]),
        "a window that drifts by a fraction per save is a window that drifts");
    check("the colour is stored as the hex the picker gave",
        stored[0].color === "#ff0000");

    const drawn = panel.slotsToDrawn(sidebar);
    check("only an enabled, named slot is drawn",
        drawn.length === 1 && drawn[0].name === "mx_1");
    check("drawn in the fractional units the shader reads",
        same(drawn[0].range, [10 / 255, 200 / 255]),
        "the same units imageViewer.updateChannelRange puts on a reference channel");
    check("with the colour as the 0-255 triple toFloatColor takes",
        drawn[0].color.r === 255 && drawn[0].color.b === 0);
    check("and a copy of it, not the slot's own object",
        drawn[0].color !== slots[0].color,
        "the record outlives the slot and must not change under it");
}

{
    // The two directions must not be the same conversion. Stored raw and drawn
    // fractional is the whole reason both exist.
    const slot = { enabled: true, name: "mx_0", colorHex: "#ffffff",
                   color: { r: 255, g: 255, b: 255 },
                   range: [10, 200], rawRange: [500, 9000] };
    const sidebar = fakeSidebar([slot]);
    const stored = panel.slotsToChannels(sidebar, { mx_0: 0 })[0];
    const drawn = panel.slotsToDrawn(sidebar)[0];
    check("what is stored and what is drawn are different domains",
        stored.range[0] === 500 && Math.abs(drawn.range[0] - 10 / 255) < 1e-9,
        "storing the drawn range is what rescales a window into a sliver");
}

check("the ceiling is the sidebar's own", panel.MAX_SLOTS === 15,
    "every enabled slot is a world item fetching and compositing its own tiles");

console.log(failures.length ? `\n${failures.length} check(s) failed`
                            : "\nall checks passed");
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
