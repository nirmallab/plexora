/**
 * Figure Builder: a REFUSED preview move is reported, not taken as success.
 *
 *     node tests/js/figure_adopt_refused_probe.mjs
 *
 * `FigureBuilderApi.adoptPreviews` answers `{ok: false}` on a refusal -- it
 * does not throw -- and `adoptInto` only caught throws. So a refused move
 * (bin unavailable, unknown figure) looked like success: the panels were in
 * the figure, the bin rows were still in the bin, and nothing said so -- the
 * same captures could be added a second time. It now reports how many
 * captures stayed in the bin (`binKept`), for the toast.
 *
 * Exit 0 = both cases hold.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");

const context = createContext({ console: { ...console, error() {} }, Object, Array, JSON, Math,
                                Number, String, Promise, Set, Map, Date,
                                crypto: { randomUUID: () => Math.random().toString(16).slice(2) } });
context.window = context;
for (const file of ["figureSchema.js", "figureCaptureBin.js"]) {
    runInContext(readFileSync(join(STATIC, file), "utf8"), context, { filename: file });
}
const Bin = runInContext("FigureCaptureBin", context);

function state() {
    return {
        figureId: "f1",
        document: { sources: {}, panels: {} },
        sourceForDatasource: () => ({ source_id: "s1", pixel_size: null }),
        async commit(operations, draft) { draft(this.document); return { revision: 2 }; },
    };
}
const entries = [{ capture_id: "c1", datasource: "ds", scene: { viewport: {} } },
                 { capture_id: "c2", datasource: "ds", scene: { viewport: {} } }];

const failures = [];
const refused = await Bin.adoptInto(state(), { adoptPreviews: async () => ({ ok: false, status: 503, data: {} }) },
                                    entries);
if (refused.panelIds.length !== 2) failures.push(`refused: ${refused.panelIds.length} panels, wanted 2`);
if (refused.binKept !== 2) failures.push(`refused: binKept ${refused.binKept}, wanted 2`);

const moved = await Bin.adoptInto(state(), { adoptPreviews: async () => ({ ok: true, status: 200, data: {} }) },
                                  entries);
if (moved.binKept) failures.push(`accepted: binKept ${moved.binKept}, wanted 0`);

if (failures.length) {
    console.log(failures.join("\n"));
    process.exit(1);
}
console.log("ok");
