/**
 * The Visium HD bin layer's client arithmetic, run.
 *
 * Everything the browser decides about a bin layer is a query string and a
 * placement, and every way of getting either wrong draws a picture that looks
 * like data:
 *
 *   **The style.** No `color=` and core's `parse_style` returns None, which
 *   serves the tile as a grey channel plane. `colors=` in heatmap mode or
 *   a missing `comp=` in the composition draws the other picture, and a
 *   `comp=` whose indices do not follow `genes=` groups the wrong genes. `bin=` in microns
 *   rather than grid squares pools four times too coarse.
 *
 *   **The geometry.** The tile frame is grid x supersample, so the layer's
 *   GRID transform has its linear part divided by the supersample and its
 *   translation left alone. Divide the translation too and the slide lands in
 *   the corner; forget the divide and it is four times too big.
 *
 *   **The blend.** A bin tile is transparent off the tissue and composited
 *   `source-over`; `lighter` would wash an H&E to white.
 *
 *   **The picker.** Eighteen thousand genes: at most fifty rows, ever.
 *
 * Run directly:  node tests/js/visium_hd_layer_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/visium_hd/static");

const timers = [];
const ctx = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
    Date, Promise, Error, TypeError, Infinity, NaN, URLSearchParams,
    encodeURIComponent,
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout: () => {},
    setInterval: () => 1, clearInterval: () => {},
    fetch: async () => ({ ok: false }),
    document: { getElementById: () => null, querySelectorAll: () => [] },
    window: {
        addEventListener() {},
        setTimeout: (fn) => { timers.push(fn); return timers.length; },
        clearTimeout: () => {},
    },
});

// Core's ramps, which base.html loads before any plugin script and which
// `BinLayer.RAMPS` reads rather than keeping a copy of; and core's gene list,
// whose group bookkeeping BinLayer delegates to.
for (const name of ["views/slider.js", "views/gradientRange.js", "views/geneList.js"]) {
    runInContext(readFileSync(join(REPO, "plexora/client/src/js", name), "utf8"),
                 ctx, { filename: name });
}
// `window.X = ...` in those files lands on the stand-in, not the context.
ctx.PlexoraColorRamps = ctx.PlexoraColorRamps || ctx.window.PlexoraColorRamps;
for (const name of ["PlexoraGeneGroups", "PlexoraGeneTree", "PlexoraGeneVocabulary"]) {
    ctx[name] = ctx[name] || ctx.window[name];
}

for (const name of ["visiumHdApi.js", "binLayer.js", "visiumHdSidebarController.js"]) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}

const failures = [];
function check(what, condition, detail = "") {
    const ok = Boolean(condition);
    if (!ok) failures.push(`${what}${detail ? ` -- ${detail}` : ""}`);
    console.log(`${ok ? "PASS" : "FAIL"} ${what}${detail ? `  ${detail}` : ""}`);
}

const { BinLayer, PlexoraGeneVocabulary } = ctx;

// The real slide's registration: a half turn and a mirror, in GRID units.
const TRANSFORM = [-0.27, 0.0, 0.0, 0.27, 21000.5, -35.25];
const MANIFEST = {
    status: "ready", genes: ["EPCAM", "KRT8", "PTPRC", "ACTB"],
    gene_ids: ["E1", "E2", "E3", "E4"], gene_counts: [500, 900, 50, 5000],
    total_count: 6450, bin_count: 19_900_000, columns: 3350, rows: 3350,
    bin_um: 2, microns_per_pixel: 0.27, supersample: 4, tile_size: 512,
    level_count: 6, width: 13400, height: 13400, total_index: 4,
    revision: "abc123", version: "20260923_visium_hd",
};

function makeLayer(overrides = {}) {
    const added = [];
    const fakeCtx = {
        datasource: "sample A",
        url: (path) => `/base/${path}`,
        layers: {
            get: () => ({ visible: true, opacity: 1, transform: [...TRANSFORM] }),
            addTiled: (spec) => {
                const handle = { spec, styles: [], visible: [],
                                 setStyle(s) { this.styles.push(s); },
                                 setVisible(v) { this.visible.push(v); },
                                 setOpacity() {}, remove() {} };
                added.push(handle);
                return handle;
            },
            claim() {}, setOpacity() {}, setVisible() {},
        },
    };
    const layer = new BinLayer(fakeCtx, "bins", { manifest: async () => MANIFEST });
    layer.manifest = { ...MANIFEST, ...overrides };
    layer.indexGenes();
    layer.normalizeState();
    return { layer, added };
}


// -- the style: heatmap --------------------------------------------------

{
    const { layer } = makeLayer();
    check("the default is 8 µm squares, heatmap, log on",
        layer.state.binUm === 8 && layer.state.mode === "heatmap" && layer.state.log === true,
        JSON.stringify([layer.state.binUm, layer.state.mode, layer.state.log]));

    const q = new URLSearchParams(layer.tileStyle());
    check("color=ffffff is always sent (parse_style needs a colour)",
        q.get("color") === "ffffff", q.toString());
    check("with no genes the heatmap draws total",
        q.get("genes") === "total" && q.get("ramp") === "viridis" && !q.has("colors"),
        q.toString());
    check("bin is the pooling in GRID SQUARES: 8 µm / 2 µm = 4",
        q.get("bin") === "4", q.get("bin"));
    check("log=1 while the log scale is on", q.get("log") === "1");
    check("the default window is the whole automatic one",
        q.get("dlo") === "0.0000" && q.get("dhi") === "1.0000");
    check("v= carries the plugin version and the store revision",
        q.get("v") === "20260923_visium_hd-abc123", q.get("v"));
    check("color= comes first", layer.tileStyle().startsWith("color=ffffff&"));

    layer.addGene("EPCAM");
    layer.addGene("KRT8");
    layer.set({ ramp: "magma", log: false, dlo: 0.1, dhi: 0.75 });
    const heat = new URLSearchParams(layer.tileStyle());
    check("a heatmap aggregates the selected genes and reads them off one ramp",
        heat.get("genes") === "EPCAM,KRT8" && heat.get("ramp") === "magma"
        && !heat.has("colors"), heat.toString());
    check("several genes are combined by their MEAN unless asked otherwise",
        heat.get("agg") === "mean", heat.toString());
    check("the aggregation shows while it changes the picture", layer.aggregates());
    check("log is absent once off", !heat.has("log"));
    check("the window travels as fractions",
        heat.get("dlo") === "0.1000" && heat.get("dhi") === "0.7500");

    // Straight to a new style on the same item: no remove, no re-add, no
    // debounce -- the menu is one click, not a drag.
    const restyled = [];
    let removed = 0;
    layer._tiles = { setStyle: (style) => restyled.push(style),
                     setVisible() {}, remove: () => { removed += 1; } };
    layer._style = layer.tileStyle();
    check("an aggregation is taken", layer.setAggregation("max") === true);
    check("...and restyles the item at once, in place",
        restyled.length === 1 && new URLSearchParams(restyled[0]).get("agg") === "max"
        && removed === 0, `${restyled.length} restyles, ${removed} removals`);
    check("the same aggregation again does nothing", layer.setAggregation("max") === false);
    check("an unknown aggregation is refused", layer.setAggregation("median") === false
        && layer.state.agg === "max");
    for (const [how, expected] of [["mean", 6], ["sum", 12], ["max", 10], ["min", 2]]) {
        layer.state.agg = how;
        const got = layer.ceiling({ EPCAM: { window: 10 }, KRT8: { window: 2 } });
        check(`the bar's ceiling is the windows' ${how}`, got === expected, String(got));
    }
    layer._tiles = null;

    layer.state.agg = "sum";
    layer.setGeneHidden("KRT8", true);
    const single = new URLSearchParams(layer.tileStyle());
    check("one drawn gene names no aggregation (it is the same under all four)",
        !single.has("agg") && !layer.aggregates(), single.toString());
    layer.setGeneHidden("KRT8", false);
    layer.set({ mode: "composite" });
    check("a composite names no aggregation",
        !new URLSearchParams(layer.tileStyle()).has("agg") && !layer.aggregates());
}


// -- groups: core's bookkeeping, over bins -----------------------------------

{
    const { layer } = makeLayer();
    Object.assign(layer.state, {
        selected: ["EPCAM", "KRT8", "NOPE"],
        groups: [{ name: "Epithelium", genes: ["EPCAM", "NOPE"] }, { name: 42 }],
        collapsed: ["Epithelium", "Gone"],
        agg: undefined,
    });
    layer.normalizeState();
    check("a saved group keeps what this panel has and drops what it has not",
        layer.state.groups.length === 1
        && layer.state.groups[0].genes.join(",") === "EPCAM", JSON.stringify(layer.state.groups));
    check("a fold for a group that is gone is forgotten",
        layer.state.collapsed.join(",") === "Epithelium", layer.state.collapsed.join(","));
    check("the tree's top level is what is in no group", layer.ungrouped().join(",") === "KRT8");
    layer.removeGene("EPCAM");
    check("removing a gene takes it out of its group",
        layer.state.groups[0].genes.length === 0);
    layer.addGene("PTPRC");
    layer.assignToGroup("PTPRC", "Epithelium");
    layer.clearGenes();
    check("clearing the genes keeps the groups, emptied",
        layer.state.groups.length === 1 && layer.state.groups[0].genes.length === 0
        && layer.state.selected.length === 0);
    layer.addGene("EPCAM");
    layer.setColor("EPCAM", "#000000");
    layer.resetAppearance();
    check("reset puts the palette's colours back", layer.colorFor("EPCAM") === "#ff4d4d",
        layer.colorFor("EPCAM"));
    layer.setAllGenesHidden(true);
    check("the list's eye hides every gene", layer.allGenesHidden());
    layer.setAllGenesHidden(false);
    check("...and shows them again", layer.drawnGenes().join(",") === "EPCAM");
    check("old state with no aggregation gets the mean", layer.state.agg === "mean");
}


// -- the style: composition ----------------------------------------------

{
    const { layer } = makeLayer();
    layer.set({ mode: "composite" });
    const empty = new URLSearchParams(layer.tileStyle());
    check("a composition with nothing picked falls back to the total heatmap",
        empty.get("genes") === "total" && empty.has("ramp") && !empty.has("comp")
        && !empty.has("colors"), empty.toString());

    layer.addGene("EPCAM");
    layer.addGene("PTPRC");
    layer.setColor("PTPRC", "#00ff88");
    const q = new URLSearchParams(layer.tileStyle());
    check("ungrouped genes are one part each, in selection order",
        q.get("genes") === "EPCAM,PTPRC" && q.get("colors") === "ff4d4d,00ff88"
        && q.get("comp") === "0,1", q.toString());
    check("colours go without the #", !q.get("colors").includes("#"));
    const keys = [...q.keys()].join(",");
    check("a composition names only what its picture depends on",
        keys === "color,genes,colors,comp,bin,v", keys);
    check("...starting with color=ffffff", layer.tileStyle().startsWith("color=ffffff&"));
    check("...at the heatmap's bin size", q.get("bin") === "4");

    layer.setGeneHidden("EPCAM", true);
    const one = new URLSearchParams(layer.tileStyle());
    check("a hidden gene is left out of the url",
        one.get("genes") === "PTPRC" && one.get("colors") === "00ff88"
        && one.get("comp") === "0", one.toString());

    layer.setGeneHidden("PTPRC", true);
    check("every gene hidden draws nothing rather than the whole panel",
        layer.allGenesHidden() && !layer.shows());
    check("a gene the vocabulary lacks is refused", layer.addGene("NOPE") === false);
}

{
    // Groups first, in group order and members in the group's order; then
    // the ungrouped genes in selection order -- the order the tree paints.
    const { layer } = makeLayer();
    for (const gene of ["PTPRC", "EPCAM", "ACTB", "KRT8"]) layer.addGene(gene);
    check("a new group is combined by its mean", layer.createGroup("Epi")
        && layer.state.groups[0].agg === "mean", JSON.stringify(layer.state.groups));
    layer.assignToGroup("KRT8", "Epi");
    layer.assignToGroup("EPCAM", "Epi");
    layer.set({ mode: "composite" });
    let q = new URLSearchParams(layer.tileStyle());
    check("groups come first, then the ungrouped genes",
        q.get("genes") === "KRT8,EPCAM,PTPRC,ACTB" && q.get("comp") === "0|1:mean,2,3",
        q.toString());
    check("colours follow the genes into glyph order",
        q.get("colors").split(",")[0] === layer.colorFor("KRT8").slice(1), q.get("colors"));
    check("three parts is not too many", layer.componentCount() === 3
        && !layer.tooManyComponents());

    layer.createGroup("Imm");
    layer.assignToGroup("PTPRC", "Imm");
    q = new URLSearchParams(layer.tileStyle());
    check("a one-gene group is still a group with a rule",
        q.get("comp") === "0|1:mean,2:mean,3", q.get("comp"));

    layer.setGeneHidden("KRT8", true);
    q = new URLSearchParams(layer.tileStyle());
    check("hiding a member renumbers the indices",
        q.get("genes") === "EPCAM,PTPRC,ACTB" && q.get("comp") === "0:mean,1:mean,2",
        q.toString());
    layer.setGeneHidden("EPCAM", true);
    q = new URLSearchParams(layer.tileStyle());
    check("a group with every gene hidden is not a part",
        q.get("comp") === "0:mean,1" && layer.componentCount() === 2, q.get("comp"));
    layer.setGeneHidden("KRT8", false);
    layer.setGeneHidden("EPCAM", false);

    const restyled = [];
    let removed = 0;
    layer._tiles = { setStyle: (style) => restyled.push(style),
                     setVisible() {}, remove: () => { removed += 1; } };
    layer._style = layer.tileStyle();
    check("a group's rule is taken", layer.setGroupAggregation("Epi", "max") === true);
    check("...and restyles the item at once, in place",
        restyled.length === 1
        && new URLSearchParams(restyled[0]).get("comp") === "0|1:max,2:mean,3"
        && removed === 0, `${restyled.length} restyles, ${removed} removals`);
    check("the same rule again does nothing", layer.setGroupAggregation("Epi", "max") === false);
    check("an unknown rule is refused", layer.setGroupAggregation("Epi", "median") === false);
    check("an unknown group is refused", layer.setGroupAggregation("Nope", "min") === false);
    layer._tiles = null;

    layer.set({ mode: "heatmap" });
    q = new URLSearchParams(layer.tileStyle());
    check("the heatmap ignores the groups' rules",
        !q.has("comp") && q.get("agg") === "mean" && q.get("genes") === "PTPRC,EPCAM,ACTB,KRT8",
        q.toString());

    const carried = JSON.parse(JSON.stringify(layer.state));
    const { layer: other } = makeLayer();
    Object.assign(other.state, carried);
    other.normalizeState();
    check("a carried state keeps each group's rule",
        other.groupAggregation("Epi") === "max" && other.groupAggregation("Imm") === "mean");
}

{
    const { layer } = makeLayer();
    Object.assign(layer.state, {
        selected: ["EPCAM", "KRT8", "PTPRC"],
        groups: [{ name: "A", genes: ["EPCAM"], agg: "sum" },
                 { name: "B", genes: ["KRT8"], agg: "median" },
                 { name: "C", genes: ["PTPRC"] }],
    });
    layer.normalizeState();
    check("saved group rules are validated",
        layer.state.groups.map((group) => group.agg).join(",") === "sum,mean,mean",
        JSON.stringify(layer.state.groups));

    const { PlexoraGeneGroups } = ctx;
    const bare = { selected: ["X"], groups: [{ name: "g", genes: ["X"], agg: "max" }],
                   collapsed: [] };
    PlexoraGeneGroups.normalize(bare);
    check("core keeps a plugin's own group fields", bare.groups[0].agg === "max");
    PlexoraGeneGroups.create(bare, "h", { agg: "min" });
    check("core writes a plugin's fields on a new group",
        bare.groups[1].agg === "min" && bare.groups[1].genes.length === 0);
}

{
    const { layer } = makeLayer({
        genes: ["G1", "G2", "G3", "G4", "G5"], gene_ids: [1, 2, 3, 4, 5],
        gene_counts: [1, 1, 1, 1, 1], total_index: 5,
    });
    layer.indexGenes();
    for (const gene of ["G1", "G2", "G3", "G4", "G5"]) layer.addGene(gene);
    check("a heatmap of five is not warned about", !layer.tooManyComponents());
    layer.set({ mode: "composite" });
    check("five parts in a composition is too many", layer.tooManyComponents()
        && layer.componentCount() === 5);
    layer.createGroup("g");
    for (const gene of ["G1", "G2", "G3"]) layer.assignToGroup(gene, "g");
    check("grouped down to three it is not", !layer.tooManyComponents()
        && layer.componentCount() === 3);
}

// -- bin ladder --------------------------------------------------------------

{
    const { layer } = makeLayer();
    const ladder = layer.binLadder().map((rung) => `${rung.microns}:${rung.pooling}`);
    check("the ladder is 2 / 8 / 16 µm on a 2 µm grid",
        ladder.join(" ") === "2:1 8:4 16:8", ladder.join(" "));
    for (const [microns, pooling] of [[2, 1], [8, 4], [16, 8]]) {
        layer.set({ binUm: microns });
        check(`bin=${pooling} for ${microns} µm`,
            new URLSearchParams(layer.tileStyle()).get("bin") === String(pooling));
    }
    layer.set({ binUm: 11 });
    check("an off-ladder size snaps to the nearest rung", layer.state.binUm === 8,
        String(layer.state.binUm));

    const { layer: coarse } = makeLayer({ bin_um: 4 });
    const labels = coarse.binLadder().map((rung) => rung.microns).join(",");
    check("rung labels follow the manifest's grid, not a hard-coded 2",
        labels === "4,16,32", labels);
    coarse.state.binUm = 16;
    check("bin=binUm/bin_um on a 4 µm grid", coarse.pooling() === 4, String(coarse.pooling()));
}


// -- geometry and the addTiled spec ------------------------------------------

{
    const { layer, added } = makeLayer();
    const geometry = layer.geometry();
    check("width/height are the grid times the supersample",
        geometry.width === 3350 * 4 && geometry.height === 3350 * 4,
        `${geometry.width}x${geometry.height}`);
    check("tiles are the manifest's tile size",
        geometry.tileWidth === 512 && geometry.tileHeight === 512);
    check("maxLevel is the level COUNT (core's addTiledLayer subtracts one)",
        geometry.maxLevel === 6, String(geometry.maxLevel));

    const [a, b, c, d, e, f] = geometry.transform;
    check("the linear part is divided by the supersample",
        a === TRANSFORM[0] / 4 && b === 0 && c === 0 && d === TRANSFORM[3] / 4,
        JSON.stringify(geometry.transform));
    check("the translation is left in reference pixels",
        e === TRANSFORM[4] && f === TRANSFORM[5], `${e},${f}`);

    layer.show();
    check("show() adds exactly one tiled item", added.length === 1, String(added.length));
    const spec = added[0]?.spec || {};
    check("the item composites source-over, never lighter",
        spec.compositeOperation === "source-over", spec.compositeOperation);
    check("the tiles come from core's layer route, under bins/",
        spec.src === "/base/generated/layer/sample%20A/bins/bins/", spec.src);
    check("the item is added with the current style",
        spec.style === layer.tileStyle(), spec.style);
    check("the item is tagged with the layer id", spec.layerId === "bins");
    check("the item starts at the panel's opacity", spec.opacity === 0.8, String(spec.opacity));

    // A restyle is debounced and goes through setStyle, not a re-add.
    layer.set({ ramp: "cividis" });
    while (timers.length) timers.shift()();
    check("a restyle replaces the style in place",
        added.length === 1 && added[0].styles.length === 1
        && added[0].styles[0].includes("ramp=cividis"),
        JSON.stringify(added[0].styles));

    // A mode switch is a new style on the same item, never a re-add.
    layer.addGene("KRT8");
    layer.set({ mode: "composite" });
    while (timers.length) timers.shift()();
    const last = added[0].styles[added[0].styles.length - 1] || "";
    check("switching to the composition restyles in place",
        added.length === 1 && last.includes("comp="), last);
    layer.removeGene("KRT8");
    layer.set({ mode: "heatmap" });
    while (timers.length) timers.shift()();

    // Hiding every gene takes the item DOWN.
    layer.addGene("EPCAM");
    layer.setGeneHidden("EPCAM", true);
    while (timers.length) timers.shift()();
    check("every gene hidden hides the item",
        added[0].visible[added[0].visible.length - 1] === false,
        JSON.stringify(added[0].visible));
}


// -- the cursor --------------------------------------------------------------

{
    const { layer } = makeLayer();
    const column = 1200;
    const row = 310;
    // Forward through the layer transform to reference pixels, then back.
    const [a, b, c, d, e, f] = TRANSFORM;
    const x = a * (column + 0.5) + c * (row + 0.5) + e;
    const y = b * (column + 0.5) + d * (row + 0.5) + f;
    const square = layer.gridAt(x, y);
    check("a reference pixel inverts to its grid square",
        square && square.column === column && square.row === row, JSON.stringify(square));
    check("off the grid is null", layer.gridAt(e + 10, f - 10) === null);
}


// -- the picker ----------------------------------------------------------------

{
    const names = Array.from({ length: 18_085 }, (_, i) => `G${i}`);
    names[17] = "EPCAM";
    names[18] = "EPCAM-AS1";
    names[19] = "XEPCAMX";
    const counts = names.map((_, i) => (i * 7919) % 10_007);
    // Core's matcher now (geneList.js), the one the panel's search and the
    // group dialog's both use.
    const vocabulary = new PlexoraGeneVocabulary(names, counts);

    const t0 = Date.now();
    const all = vocabulary.match("");
    const some = vocabulary.match("g1");
    const elapsed = Date.now() - t0;
    check("an empty query lists at most fifty genes", all.length === 50, String(all.length));
    check("the empty list is the most abundant first",
        counts[names.indexOf(all[0])] >= counts[names.indexOf(all[49])]);
    check("a broad query is capped at fifty too", some.length === 50, String(some.length));
    check("matching 18k genes stays interactive", elapsed < 100, `${elapsed} ms`);

    const epcam = vocabulary.match("epcam");
    check("exact, then prefix, then substring",
        epcam.join(",") === "EPCAM,EPCAM-AS1,XEPCAMX", epcam.join(","));
}


if (failures.length) {
    console.log(`\n${failures.length} failure(s):`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log("\nall visium_hd layer checks passed");
