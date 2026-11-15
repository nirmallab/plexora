/**
 * Figure Builder: Export renders the stored figure, so it waits for the saves
 * in flight and refuses while an edit is not stored.
 *
 *     node tests/js/figure_export_waits_for_saves_probe.mjs
 *
 * The server renders what IT holds. Export used to start the job at once: an
 * edit still on its way, or one whose save failed, was silently missing from
 * the file. Runs the real FigureDocumentState and FigureExportUi with the
 * server faked.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");

const context = createContext({ console: { ...console, error() {} }, Object, Array, JSON, Math,
                                Number, String, Promise, setTimeout, clearTimeout,
                                document: { getElementById: () => null } });
context.window = context;
for (const file of ["figureDocumentState.js", "figureExportUi.js"]) {
    runInContext(readFileSync(join(STATIC, file), "utf8"), context, { filename: file });
}
const State = runInContext("FigureDocumentState", context);
const ExportUi = runInContext("FigureExportUi", context);
const tick = () => new Promise((resolve) => setImmediate(resolve));

function harness(patch) {
    const log = [];
    const api = {
        async getFigure() { return { ok: true, status: 200, data: { document: { revision: 1, panels: {} } } }; },
        async patchFigure(id, revision, operations) {
            await tick(); await tick();
            const answer = await patch(revision);
            log.push(answer.ok ? "stored" : "refused");
            return answer;
        },
        async startExport() { log.push("export"); return { ok: true, data: { job_id: "j" } }; },
        async exportStatus() { return { ok: true, data: { status: "running" } }; },
    };
    const state = new State({ api, figureId: "f" });
    const ui = new ExportUi({ api, figureId: "f", state });
    ui.poll = () => {};
    return { state, ui, log };
}

const failures = [];

// 1. A save in flight: the export starts after it is stored.
{
    const { state, ui, log } = harness(async (revision) => ({ ok: true, status: 200, data: { revision: revision + 1 } }));
    await state.load();
    state.commit([{ op: "x" }], (draft) => { draft.title = "edited"; });
    await ui.start();
    if (log.join(",") !== "stored,export") failures.push(`in flight: order ${log.join(",")}, wanted stored,export`);
}

// 2. A save that failed: no export of a figure missing the edit.
{
    const { state, ui, log } = harness(async () => { throw new Error("offline"); });
    await state.load();
    await state.commit([{ op: "x" }], (draft) => { draft.title = "edited"; });
    await ui.start();
    if (log.includes("export")) failures.push("failed save: the export started without the edit");
}

if (failures.length) {
    console.log(failures.join("\n"));
    process.exit(1);
}
console.log("ok");
