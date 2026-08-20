/**
 * Runs main.js's refreshDataset() against a stand-in page.
 *
 * The requirements modal calls this after the user picks a different expression
 * matrix (or turns log1p on) so the open page stops drawing the matrix it has
 * stopped reading. What makes that subtle is object identity, not fetching:
 * ChannelList and ViewerSidebar each take a reference to the description object
 * at boot (`channelList.init(dd)` / `viewerSidebar.init(dd)`) and read their
 * ranges and histograms out of it for the rest of the session. A version of
 * this function that fetched perfectly good numbers and assigned them to
 * `__plexora.databaseDescription` left both of them on the old object -- which
 * is how the Thresholding panel ended up with a log-valued slider readout over
 * an X-valued histogram axis.
 *
 * The function is extracted from the real source rather than reimplemented --
 * a copy would happily pass while the shipped code was wrong.
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = await readFile(
    path.join(here, "..", "..", "plexora", "client", "src", "js", "main.js"),
    "utf8",
);

const OPENS = "__plexora.refreshDataset = async function refreshDataset() {";
const CLOSES = "\n    };";
const start = source.indexOf(OPENS);
if (start < 0) throw new Error("refreshDataset not found in main.js");
const end = source.indexOf(CLOSES, start);
if (end < 0) throw new Error("could not find the end of refreshDataset");
const body = source.slice(start, end + CLOSES.length);

// A column as it stands mid-session: the table statistics that arrived with the
// boot description, plus the image statistics ChannelList merged into the same
// entry when the user activated that channel (see ensureChannelStats).
function xColumn() {
    return {
        min: 0,
        max: 428.0,
        histogram: [{ x: 4.28, y: 0.9 }, { x: 423.72, y: 0.01 }],
        image_min: 0,
        image_max: 17500,
        image_histogram: [{ x: 175, y: 0.5 }],
        qmin: 0,
        qmax: 17500,
    };
}

function logColumn() {
    return {
        min: 0,
        max: 6.061,
        histogram: [{ x: 0.06, y: 3.2 }, { x: 6.0, y: 0.02 }],
    };
}

function build() {
    // One object, handed to everything at boot -- exactly as main.js does.
    const dd = { CD3: xColumn() };
    const config = { dataset: { source: "X" }, imageData: [{ name: "CD3" }] };

    const holders = {
        channelList: { databaseDescription: dd },
        viewerSidebar: { databaseDescription: dd },
    };
    const __plexora = { databaseDescription: dd, dataset: { describe: dd } };

    const factory = new Function(
        "__plexora", "plexoraUrl", "datasource", "config", "dataLayer",
        "dd", "imageChannels", "PlexoraDataset", "d3",
        `${body}\n        return __plexora.refreshDataset;`,
    );

    const refreshDataset = factory(
        __plexora,
        (route) => `/${route}`,
        "proj",
        config,
        { getDatabaseDescription: async () => ({ CD3: logColumn() }) },
        dd,
        new Map([["CD3", 0]]),
        { build: (cfg, channels, describe) => ({ describe }) },
        {
            json: async () => ({
                proj: {
                    dataset: { source: "layer", layer: "log1p" },
                    // A fresh /config carries the channel list too. Adopting it
                    // mid-session would shift every index the tile path and the
                    // channel sliders are keyed on, so it must be ignored.
                    imageData: [{ name: "CD3" }, { name: "Area" }],
                },
            }),
        },
    );

    return { refreshDataset, dd, config, holders, __plexora };
}

const { refreshDataset, dd, config, holders, __plexora } = build();
await refreshDataset();

// The actual regression: the panel is drawn from the sidebar's reference, so
// that reference -- not merely __plexora's -- has to report the new numbers.
const sidebarView = holders.viewerSidebar.databaseDescription.CD3;
assert.equal(sidebarView.max, 6.061, "the sidebar still reports the old matrix's range");
assert.equal(sidebarView.histogram[1].x, 6.0, "the sidebar still holds the old matrix's histogram");
assert.equal(
    holders.channelList.databaseDescription.CD3.max, 6.061,
    "the channel list still reports the old matrix's range",
);
console.log("the objects ChannelList and ViewerSidebar hold report the new matrix");

assert.equal(holders.viewerSidebar.databaseDescription, dd, "the sidebar's reference was swapped out");
assert.equal(__plexora.databaseDescription, dd, "__plexora was rebound to a different object");
console.log("every holder is still the one shared description object");

// Image statistics describe the image, which a change of feature matrix does
// not touch. They are fetched lazily per channel and live in these same
// entries, so replacing an entry wholesale would silently drop them.
assert.equal(sidebarView.image_max, 17500, "the channel's image range was dropped");
assert.deepEqual(sidebarView.image_histogram, [{ x: 175, y: 0.5 }], "the channel's image histogram was dropped");
assert.equal(sidebarView.qmax, 17500, "the channel's quantization window was dropped");
console.log("lazily fetched image statistics survive the refresh");

assert.deepEqual(config.dataset, { source: "layer", layer: "log1p" }, "the read spec was not updated");
assert.deepEqual(config.imageData, [{ name: "CD3" }], "the channel list was adopted mid-session");
console.log("the read spec is adopted and the channel list is left alone");
