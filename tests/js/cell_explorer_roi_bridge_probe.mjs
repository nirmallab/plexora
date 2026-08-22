/**
 * Is the ROI composition summary actually right?
 *
 * The card says "this region is 42% macrophages". Nothing on screen can check
 * that. A wrong answer still draws a perfectly convincing card, and every way
 * of getting it wrong looks like biology rather than a bug:
 *
 *   - counting cells whose centre is outside the polygon inflates a region;
 *   - counting cells the column has no row for makes every percentage smaller
 *     than it should be, uniformly, so nothing looks out of place;
 *   - normalising the bars to the largest category instead of to 100% makes
 *     every region look the same shape as every other one;
 *   - folding the tail into `Other` with a remainder computed from the visible
 *     bars rather than the real one gives a number that is close enough to
 *     pass a glance and wrong;
 *   - summarising a continuous column at all is a statement nobody asked for.
 *
 * The bucket index is the other half: it exists so a hover costs the region's
 * area rather than the slide's cell count, and an index that quietly drops a
 * bucket produces an undercount that looks exactly like a sparse region.
 *
 * Run directly:  node tests/js/cell_explorer_roi_bridge_probe.mjs
 *   --source <path>   probe a different cellExplorerRoiBridge.js
 * Exit 0 = every check held. Exit 1 = at least one did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const CELL_EXPLORER = join(REPO, "plexora/plugins/cell_explorer/static");
const ROI = join(REPO, "plexora/plugins/roi/static");

const sourceArg = process.argv.indexOf("--source");
const SOURCE = sourceArg === -1
    ? join(CELL_EXPLORER, "cellExplorerRoiBridge.js")
    : process.argv[sourceArg + 1];

const context = {
    Math, Object, Array, Number, String, Boolean, JSON, Set, Map, Date, Infinity,
    console, Uint8Array, Uint32Array, Int32Array, Promise, Error,
    setTimeout: () => 1, clearTimeout: () => {},
    document: { createElement: () => null, body: { appendChild() {} } },
    window: { addEventListener() {}, removeEventListener() {}, innerWidth: 1440, innerHeight: 900 },
};
const ctx = createContext(context);
// RoiGeometry answers "is this point inside", and CellExplorerColors answers
// "what colour is this category" -- both are the real files, because a probe
// against stubs of them would only be checking itself.
// Both are top-level `const`/`class` bindings, which live in the context's
// lexical scope rather than on the context object -- so they are handed out
// explicitly to be reachable from here.
runInContext(`${readFileSync(join(ROI, "roiGeometry.js"), "utf8")}\n;globalThis.__Geometry = RoiGeometry;`, ctx);
runInContext(`${readFileSync(join(CELL_EXPLORER, "cellExplorerColors.js"), "utf8")}\n;globalThis.__Colors = CellExplorerColors;`, ctx);
runInContext(`${readFileSync(SOURCE, "utf8")}\n;globalThis.__Bridge = CellExplorerRoiBridge;`, ctx);

const Bridge = ctx.__Bridge;
const Geometry = ctx.__Geometry;
const Colors = ctx.__Colors;

const checks = [];
const failures = [];

function check(name, actual, expected) {
    const a = JSON.stringify(actual);
    const e = JSON.stringify(expected);
    checks.push(name);
    if (a !== e) failures.push({ check: name, expected: e, actual: a });
}

const SQUARE = (x, y, size) => ({
    type: "Polygon",
    coordinates: [[[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]],
});

/** A square with a square hole, which is where an even-odd fill rule earns its
 *  keep: a cell in the middle is NOT in the region. */
const DOUGHNUT = {
    type: "Polygon",
    coordinates: [
        [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]],
        [[40, 40], [60, 40], [60, 60], [40, 60], [40, 40]],
    ],
};

/** Cells on a 10px lattice over 0..990, which is 100x100 of them -- enough to
 *  span many buckets at the default span, so the index is genuinely exercised
 *  rather than answered out of one cell. */
function lattice() {
    const ids = [];
    const centers = [];
    let id = 1;
    for (let y = 0; y < 1000; y += 10) {
        for (let x = 0; x < 1000; x += 10) {
            ids.push(id);
            centers.push(x, y);
            id += 1;
        }
    }
    return { ids: new ctx.Uint32Array(ids), centers: new ctx.Uint32Array(centers) };
}

const CELLS = lattice();

/** Counted by brute force, so the index has something independent to be wrong
 *  against. This is the whole point of the check: the fast path and the slow
 *  path have to agree. */
function bruteForce(geometry) {
    let n = 0;
    for (let i = 0; i < CELLS.ids.length; i += 1) {
        if (Geometry.containsPoint(geometry, CELLS.centers[i * 2], CELLS.centers[i * 2 + 1])) {
            n += 1;
        }
    }
    return n;
}

// -- the bucket index ----------------------------------------------------

{
    for (const span of [64, 256, 512]) {
        const index = Bridge.buildIndex(CELLS.ids, CELLS.centers, span);
        const square = Bridge.membersIn(SQUARE(200, 200, 100), index);
        check(`the index finds every cell in a square (span ${span})`,
            square.length, bruteForce(SQUARE(200, 200, 100)));

        // A region straddling bucket boundaries at an offset that is not a
        // multiple of the span, which is where an off-by-one in the bucket
        // range drops a row or a column of cells.
        const offset = SQUARE(137, 291, 173);
        check(`...and one that straddles buckets (span ${span})`,
            Bridge.membersIn(offset, index).length, bruteForce(offset));
    }

    const index = Bridge.buildIndex(CELLS.ids, CELLS.centers, 512);
    check("a hole in the region is not part of it",
        Bridge.membersIn(DOUGHNUT, index).length, bruteForce(DOUGHNUT));
    check("...and the hole really did remove some",
        bruteForce(DOUGHNUT) < bruteForce(SQUARE(0, 0, 100)), true);

    // Off the edge of the slide entirely: an empty answer, not a crash and not
    // everything.
    check("a region over nothing holds nothing",
        Bridge.membersIn(SQUARE(5000, 5000, 100), index).length, 0);
}

// -- the tally -----------------------------------------------------------

{
    // Five cells, ids 1..5. Codes: 0,0,1,2 and then one id the column has no
    // row for, plus one code past the end of the dictionary.
    const cellIds = new ctx.Uint32Array([1, 2, 3, 4, 5, 6]);
    const table = new ctx.Int32Array([-1, 0, 0, 1, 2, -1, 9]);
    const tally = Bridge.tally([0, 1, 2, 3, 4, 5], cellIds, table, 3);

    check("cells are counted under their category", Array.from(tally.counts), [2, 1, 1]);
    check("a code past the dictionary is Unassigned", tally.unassigned, 1);
    check("a cell this column has no row for is not counted at all", tally.total, 5);

    // The distinction that matters: if the uncounted cell went into the total,
    // every percentage on the card would be quietly wrong.
    check("...so the parts add up to the total",
        tally.counts.reduce((a, b) => a + b, 0) + tally.unassigned, tally.total);

    const beyond = Bridge.tally([0], new ctx.Uint32Array([999]), table, 3);
    check("an id past the end of the lookup is skipped, not read", beyond.total, 0);
}

// -- ranking and Other ---------------------------------------------------

{
    const rows = (...counts) => counts.map((count, i) => ({ label: `cat${i}`, count }));

    const six = Bridge.rankCategories(rows(60, 50, 40, 30, 20, 10));
    check("six categories are all shown", six.rows.length, 6);
    check("...with no Other invented", six.rows.some((r) => r.other), false);
    check("...ranked most abundant first", six.rows.map((r) => r.count), [60, 50, 40, 30, 20, 10]);

    const seven = Bridge.rankCategories(rows(60, 50, 40, 30, 20, 10, 5));
    check("seven become five and an Other", seven.rows.length, 6);
    check("...with the tail folded in", seven.rows[5].other, true);
    check("...carrying the TRUE combined count", seven.rows[5].count, 15);
    check("...and saying how many it stands for", seven.folded, 2);

    const many = Bridge.rankCategories(rows(1, 2, 3, 4, 5, 6, 7, 8, 9, 10));
    check("a long tail is still five and an Other", many.rows.length, 6);
    check("...and nothing is lost in the fold",
        many.rows.reduce((sum, r) => sum + r.count, 0), 55);

    // Expanding asks for the whole list, which is the same function with a
    // different cut -- not a second ranking that could disagree with the first.
    const all = Bridge.rankCategories(rows(1, 2, 3, 4, 5, 6, 7, 8, 9, 10), 10);
    check("expanded, every category is a bar of its own", all.rows.length, 10);
    check("...with no Other", all.rows.some((r) => r.other), false);
}

// -- counts as they are printed ------------------------------------------

{
    check("a small count is exact", Bridge.formatCount(987), "987");
    check("a round thousand loses its decimal", Bridge.formatCount(1000), "1k");
    check("a thousand keeps one", Bridge.formatCount(1234), "1.2k");
    check("...at five figures too", Bridge.formatCount(14300), "14.3k");
    check("a million reads as one", Bridge.formatCount(2400000), "2.4m");
    check("nothing is nothing", Bridge.formatCount(0), "0");
}

// -- the whole summary, over a fake panel --------------------------------

/** Just enough CellExplorerState for the bridge to read. */
function makeState(kind, { hidden = [], colors = {} } = {}) {
    const categories = [
        { value: "Tumor" }, { value: "Macrophage" }, { value: "T cell" },
        { value: "B cell" }, { value: "Fibroblast" }, { value: "Endothelial" },
        { value: "Neutrophil" },
    ];
    // Every cell gets a code, cycling through the seven categories, so the
    // counts in a region are predictable from its cell count.
    const codes = [];
    for (let i = 0; i < CELLS.ids.length; i += 1) codes.push(i % categories.length);
    return {
        column: "phenotype",
        data: kind === "categorical"
            ? { kind: "categorical", ids: CELLS.ids, codes: new ctx.Uint32Array(codes), count: codes.length }
            : { kind: "continuous", ids: CELLS.ids, values: new ctx.Uint32Array(codes), count: codes.length },
        kindFor: () => kind,
        descriptor: () => ({ name: "phenotype", kind, categories }),
        categorical: () => ({ colors, hidden }),
        hiddenSet: () => new Set(hidden),
    };
}

function makeBridge(state) {
    const bridge = new Bridge({ viewer: null, config: { tileWidth: 512 } }, state);
    bridge._positions = Bridge.buildIndex(CELLS.ids, CELLS.centers, 512);
    return bridge;
}

{
    const state = makeState("categorical");
    const bridge = makeBridge(state);
    const detail = { id: "r-1", name: "Tumor core", geometry: SQUARE(200, 200, 100) };
    const summary = bridge.summarise(detail, bridge._positions);

    check("the summary names the region", summary.name, "Tumor core");
    check("...and the variable it is of", summary.column, "phenotype");
    check("...and counts every cell inside", summary.total, bruteForce(SQUARE(200, 200, 100)));
    check("...split across the categories present", summary.rows.length > 1, true);
    check("...adding up to the total",
        summary.rows.reduce((sum, r) => sum + r.count, 0), summary.total);
    check("...each carrying a colour", summary.rows.every((r) => /^#[0-9a-f]{6}$/i.test(r.color)), true);

    // The same region again: the answer must not depend on having been asked
    // before, which is the failure a membership cache introduces.
    const again = bridge.summarise(detail, bridge._positions);
    check("asking twice gives the same answer", again.total, summary.total);

    // Reshaped. The geometry object is replaced, which is the only signal the
    // cache has that the old answer is worthless.
    const reshaped = { id: "r-1", name: "Tumor core", geometry: SQUARE(200, 200, 50) };
    const smaller = bridge.summarise(reshaped, bridge._positions);
    check("a reshaped region is recounted, not remembered",
        smaller.total, bruteForce(SQUARE(200, 200, 50)));
    check("...and it really is a different number", smaller.total < summary.total, true);
}

// -- colours come from Cell Explorer, live -------------------------------

{
    const plain = makeBridge(makeState("categorical"));
    const detail = { id: "r-1", name: "R", geometry: SQUARE(0, 0, 100) };
    const before = plain.summarise(detail, plain._positions);
    check("a category with no override takes its palette colour",
        before.rows.find((r) => r.label === "Tumor").color,
        Colors.defaultCategoryColor(0));

    const overridden = makeBridge(makeState("categorical", { colors: { Tumor: "#ff0000" } }));
    const after = overridden.summarise(detail, overridden._positions);
    check("...and the user's own colour when they picked one",
        after.rows.find((r) => r.label === "Tumor").color, "#ff0000");
}

// -- hidden categories are left out of the summary ------------------------

{
    const shown = makeBridge(makeState("categorical"));
    const withHidden = makeBridge(makeState("categorical", { hidden: ["Tumor", "Macrophage"] }));
    const detail = { id: "r-1", name: "R", geometry: SQUARE(0, 0, 200) };

    const a = shown.summarise(detail, shown._positions);
    const b = withHidden.summarise(detail, withHidden._positions);

    // The legend's checkboxes are how somebody narrows the question they are
    // asking of the slide, so the card has to answer the narrowed one: with the
    // tumour hidden, what is on screen under the pointer is everything else.
    check("a hidden category loses its row",
        b.rows.some((r) => r.label === "Tumor" || r.label === "Macrophage"), false);
    check("...and every other row survives", b.rows.length, a.rows.length - 2);
    check("...and is said to have been dropped", b.hiddenCategories, 2);

    const dropped = a.rows
        .filter((r) => r.label === "Tumor" || r.label === "Macrophage")
        .reduce((sum, r) => sum + r.count, 0);
    check("...and its cells leave the total with it", b.total, a.total - dropped);
    // The whole point of taking them out of the total as well: the bars are
    // drawn as a share of it, so a total still counting hidden cells would make
    // every visible bar shorter than the share of the picture it stands for.
    check("...leaving the rows adding up to the total",
        b.rows.reduce((sum, r) => sum + r.count, 0), b.total);

    const everything = makeBridge(makeState("categorical", {
        hidden: ["Tumor", "Macrophage", "T cell", "B cell", "Fibroblast",
                 "Endothelial", "Neutrophil"],
    }));
    const empty = everything.summarise(detail, everything._positions);
    check("hiding everything leaves no rows", empty.rows.length, 0);
    check("...and nothing counted", empty.total, 0);
    // Distinguishable from a region that genuinely holds nothing, which is a
    // different fact about the slide and gets a different sentence on the card.
    check("...but says why it is empty", empty.hiddenCategories, 7);
}


{
    const bridge = makeBridge(makeState("continuous"));
    check("a continuous column has no composition to show", bridge.canSummarise(), false);
    check("...and a categorical one does", makeBridge(makeState("categorical")).canSummarise(), true);

    const loading = makeBridge(makeState("categorical"));
    loading.state.data = null;
    check("nor does a column still loading", loading.canSummarise(), false);

    const none = makeBridge(makeState("categorical"));
    none.state.column = null;
    check("nor no column at all", none.canSummarise(), false);
}

const report = {
    source: SOURCE.replace(`${REPO}/`, ""),
    checked: checks.length,
    failures,
};

console.error(JSON.stringify(report, null, 2));
process.exit(failures.length ? 1 : 0);
