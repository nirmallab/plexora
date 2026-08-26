/**
 * Lines and arrows: the geometry the browser runs, and the tables both
 * languages read.
 *
 * A line is a start point, a signed offset and a handful of flat style keys.
 * The browser draws it as SVG, `compose` turns it into instructions and both
 * exporters walk those -- so the same arithmetic exists in JavaScript and in
 * Python, and a disagreement shows the user one arrow and prints another.
 * Nothing else in the suite would notice: the canvas renders from the browser's
 * answer and the PDF from Python's, and neither is ever compared to the other.
 *
 * So this probe owns ONE case table, emits its own answers beside the inputs,
 * and `test_figure_builder_lines.py` pushes the identical inputs through
 * `server/strokegeom.py` and `schema.normalize_annotation`. The table lives here
 * rather than being written out twice and drifting, which is the failure it
 * exists to catch.
 *
 * Each of the self-checks below would ship green and be wrong somewhere a user
 * only sees later:
 *
 *   * a head that does not land where the old `arrowHeadPoints` put it -- every
 *     arrow in every existing figure moves its barbs, and only on reload;
 *
 *   * "auto" head size that is not `max(3, 4w)` -- same thing, silently, for
 *     every arrow that predates the head-size control;
 *
 *   * a dash pattern that sums to zero or goes negative -- reportlab RAISES on
 *     one of those, and the exception surfaces as a failed export with no
 *     mention of which annotation caused it;
 *
 *   * a trim that exceeds the line's own length -- the shaft draws backwards,
 *     which looks like a bite out of the middle, while the user is shortening
 *     the line and watching it;
 *
 *   * a fade whose alpha ramp runs the wrong way -- invisible on screen, where
 *     SVG paints the gradient, and wrong in every export, where Python's plan is
 *     what gets drawn.
 *
 * Run directly:
 *   node tests/js/figure_stroke_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");
const SCRIPTS = ["figureStrokeGeometry.js"];

const problems = [];

function check(what, got, want) {
    const a = JSON.stringify(got);
    const b = JSON.stringify(want);
    if (a !== b) problems.push({ what, got: a, want: b });
}

function near(what, got, want, tolerance) {
    if (!(Math.abs(got - want) <= (tolerance === undefined ? 1e-9 : tolerance))) {
        problems.push({ what, got: String(got), want: String(want) });
    }
}

const ctx = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
    isFinite, RegExp, Date, Error, parseFloat, isNaN, Infinity, NaN,
});
for (const name of SCRIPTS) {
    runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
}
const S = runInContext("FigureStrokeGeometry", ctx);

// -- constants and vocabulary the Python side asserts equal to its own -------

const constants = {
    LINE_STYLES: S.LINE_STYLES,
    HEAD_STYLES: S.HEAD_STYLES,
    LINE_EDGES: S.LINE_EDGES,
    DASH_FACTORS: S.DASH_FACTORS,
    MIN_DASH_WIDTH_PT: S.MIN_DASH_WIDTH_PT,
    MAX_HEAD_SIZE_PT: S.MAX_HEAD_SIZE_PT,
    FADE_STEPS: S.FADE_STEPS,
    MAX_DASHES: S.MAX_DASHES,
    OPEN_HEAD_DEGREES: S.OPEN_HEAD_DEGREES,
    HEAD_HALF_WIDTH: S.HEAD_HALF_WIDTH,
    HEAD_TRIM: S.HEAD_TRIM,
    TAPER_THIN: S.TAPER_THIN,
};

// -- dash patterns ----------------------------------------------------------

const dashCases = [];
for (const style of ["solid", "dashed", "dotted", "", "wavy", null]) {
    for (const width of [0, 0.25, 0.75, 1, 2, 4.5]) {
        dashCases.push({
            name: `${JSON.stringify(style)} at ${width}pt`,
            style, width, output: S.dashPattern(style, width),
        });
    }
}

check("a solid line has no pattern", S.dashPattern("solid", 1), null);
check("nor does a name from a newer build", S.dashPattern("railroad", 1), null);
// reportlab refuses a pattern that sums to zero or carries a negative, and the
// exception it raises comes out of the middle of the PDF writer naming nothing.
for (const c of dashCases) {
    if (!c.output) continue;
    if (!(c.output[0] >= 0 && c.output[1] >= 0)) {
        problems.push({ what: `${c.name} has a negative entry`, got: JSON.stringify(c.output), want: ">= 0" });
    }
    if (!(c.output[0] + c.output[1] > 0)) {
        problems.push({ what: `${c.name} sums to nothing`, got: JSON.stringify(c.output), want: "> 0" });
    }
}
check("a hairline dash is floored at the minimum width, not at its own",
      S.dashPattern("dashed", 0.1), S.dashPattern("dashed", S.MIN_DASH_WIDTH_PT));
check("a dot is a zero-length dash", S.dashPattern("dotted", 1)[0], 0);

// -- head size --------------------------------------------------------------

const headSizeCases = [];
for (const asked of [0, -5, 1, 3, 6, 12, 72]) {
    for (const width of [0, 0.75, 2, 8]) {
        headSizeCases.push({
            name: `asked ${asked} at ${width}pt`,
            asked, width, output: S.headSize(asked, width),
        });
    }
}

// The rule `export._arrow_head` and `FigureCanvas.arrowHeadPx` both used before
// any of this existed. Every arrow drawn before the head-size control stores
// zero, so this branch is the one that keeps those documents looking right.
for (const width of [0, 0.75, 1, 2, 4, 10]) {
    near(`auto at ${width}pt is the legacy rule`, S.headSize(0, width),
         Math.max(3, width * 4));
}
near("a size the user typed is honoured exactly", S.headSize(6, 1), 6);
near("head size does not track stroke width", S.headSize(6, 4), 6);
near("except that a head cannot be thinner than its own pen", S.headSize(1, 8), 12);

// -- head geometry ----------------------------------------------------------

const headCases = [];
for (const style of S.HEAD_STYLES.concat(["chevron"])) {
    for (const [size, width] of [[3, 0.75], [12, 2], [0, 1]]) {
        headCases.push({
            name: `${style} at size ${size} width ${width}`,
            style, size, width, output: S.headGeometry(style, size, width),
        });
    }
}

check("no head draws nothing", S.headGeometry("none", 10, 1),
      { lines: [], polygon: null, trim: 0, extent: 0 });
check("and neither does a style from a newer build",
      S.headGeometry("chevron", 10, 1).extent, 0);
check("an open head is two stroked barbs and no fill",
      [S.headGeometry("open", 10, 1).lines.length, S.headGeometry("open", 10, 1).polygon],
      [2, null]);
check("a filled head is a filled triangle and no stroke",
      [S.headGeometry("filled", 10, 1).lines.length,
       S.headGeometry("filled", 10, 1).polygon.length], [0, 3]);
check("a diamond is four points", S.headGeometry("diamond", 10, 1).polygon.length, 4);
// Open styles have nothing to hide a round cap behind, so they must not eat any
// shaft; solid ones must, or a fat pen pokes out either side of the head.
check("open heads trim no shaft",
      [S.headGeometry("open", 10, 1).trim, S.headGeometry("bar", 10, 1).trim], [0, 0]);
near("a filled head trims most of its own length", S.headGeometry("filled", 10, 1).trim, 8.5);
near("a diamond trims nearly all of it", S.headGeometry("diamond", 10, 1).trim, 9);
// `extent` is what the canvas pads its element by, so a head that reaches
// further than it claims gets clipped by its own container.
for (const c of headCases) {
    const points = c.output.lines.reduce((all, line) => all.concat(line), [])
        .concat(c.output.polygon || []);
    for (const [x, y] of points) {
        if (Math.hypot(x, y) > c.output.extent + 1e-9) {
            problems.push({ what: `${c.name} reaches past its own extent`,
                            got: String(Math.hypot(x, y)), want: String(c.output.extent) });
        }
    }
}

// THE compatibility pin. `FigureCanvas.arrowHeadPoints` spread its two barbs
// 160 degrees from the FORWARD direction; this frame points back down the
// shaft, so the same barbs are 20 degrees off +x. If these two ever disagree,
// every arrow in every existing figure moves its head on reload.
function legacyBarbs(x1, y1, x2, y2, size) {
    const angle = Math.atan2(y2 - y1, x2 - x1);
    return [-1, 1].map((direction) => {
        const spread = angle + direction * (160 * Math.PI / 180);
        return [x2 + size * Math.cos(spread), y2 + size * Math.sin(spread)];
    });
}
const legacyCases = [];
for (const [x1, y1, x2, y2] of [[0, 0, 40, 0], [10, 10, 40, 70], [80, 20, 5, 5],
                                [0, 0, 0, -30]]) {
    const size = S.headSize(0, 2);
    const placed = S.placeHead([x2, y2], [x1, y1], S.headGeometry("open", size, 2));
    const legacy = legacyBarbs(x1, y1, x2, y2, size);
    const mine = placed.lines.map((line) => line[1]);
    near(`legacy barb A on (${x1},${y1})->(${x2},${y2}) x`, mine[0][0], legacy[0][0], 1e-9);
    near(`legacy barb A on (${x1},${y1})->(${x2},${y2}) y`, mine[0][1], legacy[0][1], 1e-9);
    near(`legacy barb B on (${x1},${y1})->(${x2},${y2}) x`, mine[1][0], legacy[1][0], 1e-9);
    near(`legacy barb B on (${x1},${y1})->(${x2},${y2}) y`, mine[1][1], legacy[1][1], 1e-9);
    legacyCases.push({ name: `(${x1},${y1})->(${x2},${y2})`, x1, y1, x2, y2, size,
                       legacy, placed });
}

// -- placement, trimming ----------------------------------------------------

const placeCases = [];
for (const [tip, other, style, size] of [
    [[100, 100], [0, 0], "open", 12],
    [[0, 0], [100, 100], "open", 12],
    [[50, 10], [50, 90], "filled", 8],
    [[10, 50], [90, 50], "bar", 6],
    [[30, 30], [70, 10], "diamond", 9],
    [[20, 20], [20, 20], "filled", 5],
]) {
    placeCases.push({
        name: `${style} at (${tip}) facing (${other})`,
        tip, other, style, size,
        output: S.placeHead(tip, other, S.headGeometry(style, size, 1)),
    });
}

const trimCases = [];
for (const [p1, p2, t1, t2] of [
    [[0, 0], [100, 0], 0, 0],
    [[0, 0], [100, 0], 10, 0],
    [[0, 0], [100, 0], 10, 25],
    [[0, 0], [30, 40], 5, 5],
    [[0, 0], [10, 0], 8, 8],
    [[0, 0], [10, 0], 30, 10],
    [[7, 7], [7, 7], 3, 3],
]) {
    trimCases.push({
        name: `(${p1})->(${p2}) trimmed ${t1}/${t2}`,
        p1, p2, trim1: t1, trim2: t2,
        output: S.trimmedShaft(p1, p2, t1, t2),
    });
}
// Two heads that want more room than there is must collapse the shaft, not
// invert it: an inverted shaft draws, and looks like a bite out of the middle.
for (const c of trimCases) {
    const [a, b] = c.output;
    const along = (b[0] - a[0]) * (c.p2[0] - c.p1[0]) + (b[1] - a[1]) * (c.p2[1] - c.p1[1]);
    if (along < -1e-9) {
        problems.push({ what: `${c.name} runs backwards`, got: String(along), want: ">= 0" });
    }
}

// -- tapers -----------------------------------------------------------------

const taperCases = [];
for (const edge of S.LINE_EDGES.concat(["taper_middle"])) {
    for (const [p1, p2, width, t1, t2] of [
        [[0, 0], [100, 0], 4, 0, 0],
        [[0, 0], [60, 80], 2, 0, 5],
        [[10, 10], [10, 10], 3, 0, 0],
    ]) {
        taperCases.push({
            name: `${edge} (${p1})->(${p2}) at ${width}`,
            edge, p1, p2, width, trim1: t1, trim2: t2,
            output: S.taperOutline(p1, p2, width, edge, t1, t2),
        });
    }
}
check("a standard edge is not a polygon at all", S.taperOutline([0, 0], [10, 0], 2, "standard", 0, 0), []);
check("nor is a fade", S.taperOutline([0, 0], [10, 0], 2, "fade_end", 0, 0), []);
check("a one-ended taper is a quadrilateral",
      S.taperOutline([0, 0], [10, 0], 2, "taper_end", 0, 0).length, 4);
check("a two-ended one has a waist and so needs six points",
      S.taperOutline([0, 0], [10, 0], 2, "taper_both", 0, 0).length, 6);
{
    const ribbon = S.taperOutline([0, 0], [100, 0], 4, "taper_end", 0, 0);
    near("the fat end is the full stroke width", ribbon[0][1] - ribbon[3][1], 4);
    // Never exactly zero: coincident polygon points render as a spike or as
    // nothing at all, depending on the rasteriser.
    near("the thin end is thin but not a point", ribbon[1][1] - ribbon[2][1], 4 * S.TAPER_THIN * 2);
    if (!(Math.abs(ribbon[1][1] - ribbon[2][1]) > 0)) {
        problems.push({ what: "the thin end collapsed to a point", got: "0", want: "> 0" });
    }
}

// -- fades ------------------------------------------------------------------

const fadeCases = [];
for (const edge of S.LINE_EDGES) {
    for (const t of [0, 0.1, 0.25, 0.5, 0.75, 1]) {
        fadeCases.push({ name: `${edge} at ${t}`, edge, t, output: S.fadeAlpha(t, edge) });
    }
}
near("a standard edge is fully opaque throughout", S.fadeAlpha(0.5, "standard"), 1);
near("fade_start starts invisible", S.fadeAlpha(0, "fade_start"), 0);
near("and ends solid", S.fadeAlpha(1, "fade_start"), 1);
near("fade_end runs the other way", S.fadeAlpha(1, "fade_end"), 0);
near("fade_both is solid only in the middle", S.fadeAlpha(0.5, "fade_both"), 1);
near("and gone at either end", S.fadeAlpha(0, "fade_both") + S.fadeAlpha(1, "fade_both"), 0);

// -- the render plan the two exporters walk ---------------------------------

const planCases = [];
for (const [p1, p2, style, width, fade] of [
    [[0, 0], [100, 0], "solid", 1, "standard"],
    [[0, 0], [100, 0], "solid", 1, "fade_end"],
    [[0, 0], [100, 0], "solid", 1, "fade_both"],
    [[0, 0], [100, 0], "dashed", 1, "standard"],
    [[0, 0], [100, 0], "dashed", 2, "fade_start"],
    [[0, 0], [100, 0], "dotted", 1, "standard"],
    [[0, 0], [60, 80], "dotted", 1.5, "fade_end"],
    [[10, 10], [10, 10], "dashed", 1, "standard"],
]) {
    const dash = S.dashPattern(style, width);
    planCases.push({
        name: `${style} ${fade} at ${width}pt (${p1})->(${p2})`,
        p1, p2, dash, fade,
        output: S.shaftRenderPlan(p1, p2, dash, fade, S.FADE_STEPS),
    });
}

check("a plain solid shaft is one piece",
      S.shaftRenderPlan([0, 0], [100, 0], null, "standard", S.FADE_STEPS).length, 1);
check("a faded solid shaft is cut into FADE_STEPS of them",
      S.shaftRenderPlan([0, 0], [100, 0], null, "fade_end", S.FADE_STEPS).length, S.FADE_STEPS);
check("a line of no length draws nothing at all",
      S.shaftRenderPlan([5, 5], [5, 5], null, "standard", S.FADE_STEPS), []);
{
    const dots = S.shaftRenderPlan([0, 0], [30, 0], S.dashPattern("dotted", 1), "standard", 24);
    check("every piece of a dotted line says it is a dot", dots.every((p) => p[3]), true);
    check("and has no length to draw along", dots.every((p) => p[0][0] === p[1][0]), true);
    const dashes = S.shaftRenderPlan([0, 0], [30, 0], S.dashPattern("dashed", 1), "standard", 24);
    check("a dashed line's pieces are not dots", dashes.some((p) => p[3]), false);
    // A dash landing exactly on the end would otherwise emit a zero-length
    // piece, which under a round cap is a blob out past the last gap.
    check("and none of them is empty", dashes.every((p) => p[1][0] > p[0][0]), true);
}
{
    const faded = S.shaftRenderPlan([0, 0], [100, 0], null, "fade_end", 8);
    near("a fading shaft starts near solid", faded[0][2], 1, 0.07);
    near("and ends near nothing", faded[faded.length - 1][2], 0, 0.07);
    const dashedFade = S.shaftRenderPlan(
        [0, 0], [100, 0], S.dashPattern("dashed", 1), "fade_start", 24);
    check("a dashed fade takes one alpha per dash, not per step",
          dashedFade.length,
          S.shaftRenderPlan([0, 0], [100, 0], S.dashPattern("dashed", 1),
                            "standard", 24).length);
    check("and the alphas climb along the line",
          dashedFade.every((piece, index) =>
              index === 0 || piece[2] >= dashedFade[index - 1][2] - 1e-9), true);
}
// Heads are NEVER drawn at a fade alpha -- a head at a faded end would vanish,
// which is not what "fade the line" means. Nothing in the plan touches them,
// and that is the point: the plan covers the shaft only.

console.error(JSON.stringify({
    problems, constants, dashCases, headSizeCases, headCases, legacyCases,
    placeCases, trimCases, taperCases, fadeCases, planCases,
}));
process.exitCode = problems.length ? 1 : 0;
