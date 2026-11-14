/**
 * A capture's pixel size is in microns, whatever unit the file states it in.
 *
 * `/get_ome_metadata` passes the file's own unit through: an OME-Zarr whose
 * axes say "nanometer" serves `physical_size_x_unit: "nm"`, an OME-TIFF with
 * PhysicalSizeXUnit="mm" serves "mm". Core's viewer converts (imageViewer.js
 * getPixelsPerMeter). Every Figure Builder consumer -- FigureSchema.
 * physicalWidthUm, compose.scale_bar's `span_px * pixel_size["value"]`, the
 * canvas bar -- reads `source.pixel_size.value` as microns per pixel. So
 * FigureBuilderApi.readPixelSize has to hand back microns.
 *
 * Loads figureSchema.js and figureBuilderApi.js into a vm context with a
 * stubbed fetch, and reports what readPixelSize, describeCaptureSource and
 * physicalWidthUm produce for a few stated units. Report on stderr as JSON.
 *
 * Run directly:
 *   node tests/js/figure_pixel_size_unit_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");

let served = {};

const context = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
    Date, Promise, Error, TypeError, Infinity, URLSearchParams,
    window: {},
    fetch: async (url) => {
        const text = String(url);
        if (text.startsWith("get_ome_metadata")) {
            return { ok: true, status: 200, json: async () => served };
        }
        if (text.startsWith("plugins/figure_builder/api/sources/")) {
            return { ok: true, status: 200,
                     json: async () => ({ source: { datasource: "demo" } }),
                     text: async () => JSON.stringify({ source: { datasource: "demo" } }) };
        }
        return { ok: false, status: 404, json: async () => ({}), text: async () => "" };
    },
});
context.window = context;

// Core's viewerScene.js first, as base.html loads it: FigureSchema's viewport
// helpers delegate to it.
const SCRIPTS = [
    join(REPO, "plexora/client/src/js/services/viewerScene.js"),
    join(STATIC, "figureSchema.js"),
    join(STATIC, "figureBuilderApi.js"),
];
for (const path of SCRIPTS) {
    runInContext(readFileSync(path, "utf8"), context, { filename: path });
}
runInContext(`
    globalThis.__api = new FigureBuilderApi({ url: (path) => path });
    globalThis.__schema = FigureSchema;
`, context);

const api = context.__api;
const schema = context.__schema;

async function read(metadata) {
    served = metadata;
    return JSON.parse(JSON.stringify(await api.readPixelSize("demo")));
}

const report = { problems: [] };
try {
    report.nm = await read({ physical_size_x: 325, physical_size_x_unit: "nm" });
    report.mm = await read({ physical_size_x: 0.000325, physical_size_x_unit: "mm" });
    report.um = await read({ physical_size_x: 0.325, physical_size_x_unit: "µm" });
    report.unknown = await read({ physical_size_x: 0.325, physical_size_x_unit: "furlong" });

    // End to end: what a capture of a 1000-pixel-wide field of a nanometre
    // calibrated image says the field is, in microns.
    served = { physical_size_x: 325, physical_size_x_unit: "nm" };
    let source = null;
    try {
        source = await api.describeCaptureSource("demo");
    } catch (error) {
        report.problems.push("describeCaptureSource threw: " + error);
    }
    report.captured = source ? JSON.parse(JSON.stringify(source.pixel_size)) : null;
    report.widthUm = source
        ? schema.physicalWidthUm(source, { x: 0, y: 0, w: 1000, h: 800 })
        : null;
} catch (error) {
    report.problems.push(String(error && error.stack || error));
}

process.stderr.write(JSON.stringify(report));
process.exit(report.problems.length ? 1 : 0);
