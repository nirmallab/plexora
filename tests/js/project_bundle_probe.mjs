/**
 * The Export and Import dialogs' decisions (views/projectExport.js,
 * views/projectImport.js), without a browser.
 *
 * What has to hold:
 *
 *   - **Sizes read as Finder writes them**, powers of 1000.
 *   - **Images and ready-to-open files start OFF**; the project's own work,
 *     its edited tables, bookmarks and figures start on. A part with nothing
 *     in it is not offered; one whose files cannot go (on a data node,
 *     missing) is offered disabled, saying why.
 *   - **Ready-to-open files left out warn** when some are only on this
 *     computer, since nothing elsewhere rebuilds them by itself.
 *   - **A status offers only the choices that mean something**, and each
 *     starts at the server's default.
 *   - **An import goes only to the user's own folders**: there, writable,
 *     never a shared root or the old nested website folder, the default
 *     save location first.
 *   - **Every request goes through PlexoraBundleTransport**, whose server
 *     form asks the app for a save path before an export starts.
 *
 * Run directly:  node tests/js/project_bundle_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const EXPORT = readFileSync(join(REPO, "plexora/client/src/js/views/projectExport.js"), "utf8");
const IMPORT = readFileSync(join(REPO, "plexora/client/src/js/views/projectImport.js"), "utf8");

let failures = 0;
function check(label, condition, detail) {
    if (condition) {
        console.log(`ok - ${label}`);
    } else {
        failures += 1;
        console.log(`FAIL - ${label}${detail === undefined ? "" : ` (${JSON.stringify(detail)})`}`);
    }
}

function makeWorld({ website = false, desktop = null } = {}) {
    const classes = new Set(website ? ["is-website"] : []);
    const listeners = {};
    const requests = [];
    const executors = {};
    const document = {
        documentElement: { classList: { contains: (c) => classes.has(c) } },
        addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
        createElement: () => ({ style: {}, click() {}, remove() {}, addEventListener() {} }),
        body: { appendChild() {} },
    };
    const window = {
        document,
        PlexoraDesktop: desktop,
        PlexoraDropRouter: { registerExecutor: (kind, fn) => { executors[kind] = fn; } },
        setTimeout, clearTimeout,
    };
    window.window = window;
    const context = createContext({
        window, document, console, URLSearchParams, JSON, Promise, Error, Date, Number, String, Set,
        Math, Object, Array, Boolean, encodeURIComponent,
        plexoraUrl: (path) => `/${String(path).replace(/^\/+/, "")}`,
        fetch: (url, options = {}) => {
            requests.push({ url, method: options.method || "GET",
                            body: options.body ? JSON.parse(options.body) : null });
            return Promise.resolve({ ok: true, json: () => Promise.resolve({ job: "j1", status: "pending" }) });
        },
    });
    runInContext(EXPORT, context, { filename: "projectExport.js" });
    runInContext(IMPORT, context, { filename: "projectImport.js" });
    return { window, requests, executors, listeners,
             exportApi: window.PlexoraProjectExport, importApi: window.PlexoraProjectImport };
}

const world = makeWorld();
const ex = world.exportApi._internal;
const im = world.importApi._internal;

// -- sizes ---------------------------------------------------------------------

{
    const cases = [[0, "0 bytes"], [1, "1 byte"], [999, "999 bytes"], [1000, "1 KB"], [66314, "66 KB"],
                   [681044, "681 KB"], [2559386, "2.6 MB"], [460e6, "460 MB"], [66.7e9, "66.7 GB"],
                   [999.96e6, "1 GB"], [1.5e12, "1.5 TB"]];
    const wrong = cases.filter(([n, text]) => ex.formatBytes(n) !== text)
        .map(([n, text]) => `${n}: ${ex.formatBytes(n)} != ${text}`);
    check("sizes are written as Finder writes them", !wrong.length, wrong);
    check("a size nobody knows is left blank", ex.formatBytes(null) === "" && ex.formatBytes(-1) === "");
}

// -- export: what goes by default ---------------------------------------------------

const ESTIMATE = {
    projects: [{ id: "a", name: "NLU_01" }, { id: "b", name: "NLU_02" }],
    rows: {
        state: { size: 15e6, count: 4 }, tables: { size: 600e6, count: 2 },
        captures: { size: 2e6, count: 3 }, figures: { size: 0, count: 0 },
        images: { size: 66.7e9, count: 1, items: [
            { project: "NLU_01", role: "channelFile", kind: "file", where: "here", size: 66.7e9, name: "a.ome.tif", can_include: true },
            { project: "NLU_02", role: "channelFile", kind: "node", where: "node", node: "HMS-O2", size: null, name: "b.zarr", can_include: false },
        ] },
        derived: { size: 2.1e9, count: 1, items: [
            { project: "NLU_01", role: "dataset.src", kind: "file", where: "here", size: 2.1e9, name: "a.h5ad", can_include: true },
        ] },
    },
    include: { tables: true, captures: true, figures: true, images: false, derived: false },
    datasets: [], figures: [], left_out: [],
};

{
    const fresh = ex.defaultInclude({});
    check("tables, bookmarks and figures start on; images and ready-to-open files start off",
          fresh.tables && fresh.captures && fresh.figures && !fresh.images && !fresh.derived, fresh);
    const include = ex.defaultInclude(ESTIMATE);
    const rows = ex.rowsFor(ESTIMATE, include);
    const by = Object.fromEntries(rows.map((row) => [row.key, row]));
    check("the project's own work is always included, and cannot be unticked",
          by.state.on && by.state.locked && by.state.disabled);
    check("the rows on by default are the analysis, the tables and the bookmarks",
          rows.filter((row) => row.on).map((row) => row.key).join() === "state,tables,extras",
          rows.map((row) => [row.key, row.on]));
    check("bookmarks and figures are one choice over two parts",
          by.extras.size === 2e6 && by.extras.parts.join() === "captures,figures");
    check("images list every file, the ones that stay where they are included",
          by.images.items.length === 2 && !by.images.on && !by.images.disabled);
    const empty = ex.rowsFor({ ...ESTIMATE, rows: { ...ESTIMATE.rows, tables: { size: 0, count: 0 },
                                                    captures: { size: 0, count: 0 } } }, include);
    check("a part with nothing in it is not offered",
          !empty.some((row) => row.key === "tables" || row.key === "extras"));
    const onNode = ex.rowsFor({ ...ESTIMATE, rows: { ...ESTIMATE.rows, images: { size: 0, count: 0,
        items: [ESTIMATE.rows.images.items[1]] } } }, include).find((row) => row.key === "images");
    check("files that cannot go are offered disabled, saying where they stay",
          onNode.disabled && !onNode.on && /HMS-O2/.test(onNode.note), onNode);
    check("the total is an upper bound over what is ticked",
          ex.totalFor(ESTIMATE, include) === 15e6 + 600e6 + 2e6
          && ex.totalFor(ESTIMATE, { ...include, images: true }) === 15e6 + 600e6 + 2e6 + 66.7e9);
    check("ready-to-open files left out warn when only this computer has them",
          /only on this computer/.test(by.derived.hint) && /not rebuild/.test(by.derived.hint), by.derived.hint);
    check("and say nothing once they go, or when they are on a shared drive",
          ex.derivedNote(ESTIMATE, { ...include, derived: true }) === ""
          && ex.derivedNote({ rows: { derived: { items: [{ where: "shared" }] } } }, include) === "");
    check("the include rule is the parts that are on",
          ex.includeParam(include) === "tables,captures,figures" && ex.includeParam({}) === "none");
}

// -- export: smaller images ----------------------------------------------------------

const SMALL = {
    ...ESTIMATE,
    approximate: { available: true, share: 0.15 },
    rows: { ...ESTIMATE.rows, images: { ...ESTIMATE.rows.images, approximate_size: 10e9, items: [
        { ...ESTIMATE.rows.images.items[0], approximate: true, approximate_size: 10e9 },
        { ...ESTIMATE.rows.images.items[1], approximate: false, approximate_size: null },
    ] } },
};

{
    const include = ex.defaultInclude(SMALL);
    check("smaller images start off", include.approximate === false, include);
    const offered = ex.rowsFor(SMALL, { ...include, images: true }).find((row) => row.key === "images");
    check("smaller images are offered under images once they go, not before",
          !ex.rowsFor(SMALL, include).some((row) => row.smaller) && offered.smaller && offered.smaller.on === false,
          offered);
    const on = { ...include, images: true, approximate: true };
    const images = ex.rowsFor(SMALL, on).find((row) => row.key === "images");
    check("ticked, the images count as the server's estimate of them",
          images.smaller.on && images.size === 10e9 && ex.totalFor(SMALL, on) === 15e6 + 600e6 + 2e6 + 10e9
          && ex.fileSize(SMALL.rows.images.items[0], true) === "≈ 10 GB"
          && ex.fileSize(SMALL.rows.images.items[0], false) === "66.7 GB", images);
    check("a server that cannot make them offers nothing, and counts nothing smaller",
          ex.smallerRow(ESTIMATE, on) === null && ex.smallerRow({ ...SMALL, approximate: { available: false } }, on) === null
          && ex.totalFor(ESTIMATE, on) === 15e6 + 600e6 + 2e6 + 66.7e9);
    const site = makeWorld({ website: true }).exportApi._internal;
    check("and the website never offers them", site.smallerRow(SMALL, on) === null);
    check("the include rule names them when they are on",
          ex.includeParam(on) === "tables,captures,figures,images,approximate");
}

{
    check("one project is exported under its name",
          ex.titleFor({ names: ["lsp11385_2"], estimate: { projects: [{ name: "lsp11385_2" }] } }) === "Export lsp11385_2");
    check("several are counted", ex.titleFor({ names: ["a", "b", "c"], estimate: null }) === "Export 3 projects");
    check("a dataset is named", ex.titleFor({ dataset: "d1", datasetName: "NLU", estimate: ESTIMATE }) === "Export dataset NLU");
    check("the file is named for the project, made safe",
          ex.suggestedName({ names: ["a/b:c"], estimate: null }) === "a_b_c.plexora"
          && ex.suggestedName({ dataset: "d1", datasetName: "NLU", estimate: ESTIMATE }) === "NLU.plexora");
    check("a file kind is told from its role and name",
          ex.fileKind({ role: "segmentation", name: "m.tif" }).word === "Mask"
          && ex.fileKind({ role: "dataset.src", name: "c.csv" }).word === "Table"
          && ex.fileKind({ role: "channelFile", name: "a.ome.tif" }).word === "Image");
}

// -- import: choices and destinations ---------------------------------------------------

{
    const offered = (status) => im.choicesFor(status).map(([action]) => action).join();
    check("changed on both offers keep both or replace", offered("conflict") === "keep_both,replace");
    check("an update offers update, keep both or skip", offered("update") === "update,keep_both,skip");
    check("one already here offers skip or keep both", offered("already_here") === "skip,keep_both");
    check("a new project has nothing to decide", offered("new") === "" && offered("renamed") === "");
    const defaults = ["new", "renamed", "already_here", "update", "conflict"]
        .map((status) => im.defaultChoice({ status }));
    check("each status starts at the server's default",
          defaults.join() === "import,import,skip,update,keep_both", defaults);
    check("the server's own action is kept when it is one on offer",
          im.defaultChoice({ status: "conflict", action: "replace" }) === "replace"
          && im.defaultChoice({ status: "conflict", action: "update" }) === "keep_both");
    check("a renamed project says what it is imported as",
          im.badgeFor({ status: "renamed", target: "NLU_01_2" }).label === "Imported as NLU_01_2"
          && im.badgeFor({ status: "conflict" }).label === "Changed on both");
    check("its images are counted where they are",
          im.imagesLine({ found: 2, included: 1, missing: 3, node: 1, nodes: ["HMS-O2"] })
              === "2 found here · 1 included · 1 on HMS-O2 · 3 to find after import"
          && im.imagesLine({}) === "");
    check("a smaller copy is counted among the included, and says so",
          im.imagesLine({ included: 2, approximate: 1 }) === "2 included · 1 as a smaller copy"
          && im.imagesLine({ included: 3, approximate: 2 }) === "3 included · 2 as smaller copies");
    const projects = [{ id: "a", status: "new" }, { id: "b", status: "already_here" }, { id: "c", status: "conflict" }];
    check("Import counts what the choices bring in",
          im.importing(projects, {}).length === 2 && im.importLabel(projects, {}) === "Import 2 projects"
          && im.importLabel(projects, { c: "skip" }) === "Import");
}

{
    const answer = {
        default_save: { id: "lib" },
        locations: [
            { id: "root", kind: "data_root", label: "Data folder", available: true, writable: true },
            { id: "lib", kind: "library", label: "Plexora projects", available: true, writable: true },
            { id: "old", kind: "legacy", label: "Old website folder", available: true, writable: true },
            { id: "share", kind: "shared", label: "Lab share", available: true, writable: true },
            { id: "usb", kind: "workspace", label: "USB", available: false, writable: true },
            { id: "ro", kind: "workspace", label: "Read only", available: true, writable: false },
            { id: "ws", kind: "workspace", label: "Scratch", available: true, writable: true },
        ],
    };
    const ids = im.destinations(answer).map((place) => place.id);
    check("an import goes only to the user's own folders that are there and writable",
          ids.slice().sort().join() === "lib,root,ws", ids);
    check("the default save location comes first", ids[0] === "lib", ids);
    check("no locations is no choices", im.destinations({}).length === 0);
    const short = im.spaceLine({ needed: 68.2e9, free: 41.5e9, fits: false }, "Plexora projects");
    check("not enough space says so, with both numbers",
          short.short && /Not enough space in Plexora projects/.test(short.text)
          && /68\.2 GB/.test(short.text) && /41\.5 GB/.test(short.text), short);
    check("enough space says what it needs",
          im.spaceLine({ needed: 681e6, free: 412e9, fits: true }).text === "Needs 681 MB · 412 GB free");
    check("where a file came from is said in one line",
          /^Exported from Plexora 0\.0\.46 \(the desktop app\) on /.test(
              im.originLine({ created: "2026-11-08T15:20:00Z", app: { kind: "desktop", version: "0.0.46" } })));
    check("a finished project says how it got its name",
          im.outcomeFor({ action: "keep_both", name: "a", target: "a_2" }) === "Imported beside your a"
          && im.outcomeFor({ action: "import", name: "a", target: "a_2" }) === "Imported · a was taken"
          && im.outcomeFor({ action: "update", name: "a", target: "a" }) === "Updated"
          && im.outcomeFor({ action: "keep_both", name: "a", target: "a_2" }, "a") === "A copy of a");
}

// -- the transport ---------------------------------------------------------------------------

{
    const transport = world.window.PlexoraBundleTransport;
    check("the server transport is the default", transport && transport.kind === "server");
    check("the dialogs run on the desktop and its server",
          world.exportApi.available() && world.importApi.available());
    check("and not on the website, until it brings its own transport",
          !makeWorld({ website: true }).exportApi.available());
    check("a dropped .plexora opens the Import dialog (the drop router's executor)",
          typeof world.executors.import_bundle === "function");

    await transport.estimate({ ids: ["a", "b"], include: { tables: true, images: false } });
    check("the estimate names the projects and the parts",
          world.requests.at(-1).url === "/projects/export/estimate?ids=a%2Cb&include=tables",
          world.requests.at(-1));
    await transport.startExport({ ids: ["a"], include: { tables: true }, dest: "/x/a.plexora" });
    check("an export posts its destination",
          world.requests.at(-1).url === "/projects/export" && world.requests.at(-1).body.dest === "/x/a.plexora");
    await transport.preview({ path: "/x/a.plexora" }, "lib");
    check("a preview names the file and the place",
          world.requests.at(-1).url === "/projects/import/preview"
          && world.requests.at(-1).body.path === "/x/a.plexora" && world.requests.at(-1).body.location === "lib");
    await transport.startImport({ source: { upload: "abc123abc123" }, choices: { p: "replace" }, location: "lib" });
    check("an import of an uploaded file names the upload, not the bytes again",
          world.requests.at(-1).body.upload === "abc123abc123" && world.requests.at(-1).body.path === null
          && world.requests.at(-1).body.choices.p === "replace");
    await transport.cancel("export", "j1");
    check("a cancel is a POST, which the website's page can answer too",
          world.requests.at(-1).url === "/projects/export/j1/cancel" && world.requests.at(-1).method === "POST");
    check("a browser downloads the file when it is ready",
          JSON.stringify(await transport.chooseDestination({ name: "a.plexora" })) === JSON.stringify({ dest: null }));

    const asked = [];
    const app = makeWorld({ desktop: { pickSavePath: async (options) => { asked.push(options); return "/Volumes/lab/a.plexora"; },
                                       revealPath: async () => {} } });
    const chosen = await app.window.PlexoraBundleTransport.chooseDestination({ name: "a.plexora" });
    check("the desktop app asks where the file goes before the export starts",
          chosen.dest === "/Volumes/lab/a.plexora" && asked[0].name === "a.plexora");
    const cancelled = makeWorld({ desktop: { pickSavePath: async () => null } });
    check("and a cancelled Save dialog starts nothing",
          (await cancelled.window.PlexoraBundleTransport.chooseDestination({ name: "a.plexora" })) === null);
    check("the server transport picks its folder before findMissing; the website's picks it itself",
          !transport.picksFolderItself);
    check("Show in folder only where the app can reveal a file",
          app.window.PlexoraBundleTransport.canReveal() && !transport.canReveal());
}

if (failures) {
    console.log(`${failures} failed`);
    process.exit(1);
}
