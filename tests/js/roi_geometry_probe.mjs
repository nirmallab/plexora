/**
 * The ROI plugin's geometry, checked against known answers.
 *
 * roiGeometry.js decides things that are invisible until they are wrong:
 * whether a click landed on a shape, how many of a freehand stroke's thousands
 * of points are kept, whether an outline crosses itself. None of it is
 * observable from the Python suite, which never executes client JS, and none of
 * it is observable from a screenshot either -- a shape simplified too hard
 * still looks like a shape.
 *
 * The one that matters most is point-in-polygon over a shape with a hole. Even-
 * odd ray casting gets holes right for free and the nonzero rule does not, so
 * the difference between the two implementations is a single word and shows up
 * only as "clicking the middle of a doughnut selects it, and it should not".
 *
 * Run directly:  node tests/js/roi_geometry_probe.mjs
 *   --source <path>   probe a different roiGeometry.js (used to prove the probe
 *                     can fail, by mutating a copy)
 * Exit 0 = every check held. Exit 1 = at least one did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

const sourceArg = process.argv.indexOf("--source");
const SOURCE = sourceArg === -1
    ? join(REPO, "plexora/plugins/roi/static/roiGeometry.js")
    : process.argv[sourceArg + 1];

const ctx = createContext({ Math, Object, Array, Number, Set, Map, Uint8Array, Infinity, console });
runInContext(readFileSync(SOURCE, "utf8") + "\n;globalThis.__geom = RoiGeometry;", ctx);
const G = ctx.__geom;

const checks = [];
const failures = [];

function check(name, actual, expected) {
    const a = JSON.stringify(actual);
    const e = JSON.stringify(expected);
    checks.push(name);
    if (a !== e) failures.push({ check: name, expected: e, actual: a });
}

// -- rings ---------------------------------------------------------------

check("closeRing repeats the first position",
    G.closeRing([[0, 0], [10, 0], [10, 10]]),
    [[0, 0], [10, 0], [10, 10], [0, 0]]);

check("closeRing leaves a closed ring alone",
    G.closeRing([[0, 0], [10, 0], [0, 0]]),
    [[0, 0], [10, 0], [0, 0]]);

check("openRing drops only the closing duplicate",
    G.openRing([[0, 0], [10, 0], [10, 10], [0, 0]]),
    [[0, 0], [10, 0], [10, 10]]);

check("dedupe removes consecutive repeats",
    G.dedupe([[0, 0], [0, 0], [1, 1], [1, 1], [1, 1], [2, 2]], 0),
    [[0, 0], [1, 1], [2, 2]]);

// A stationary pointer still fires move events; without a tolerance those
// become zero-length segments every later algorithm has to special-case.
check("dedupe collapses sub-tolerance jitter",
    G.dedupe([[0, 0], [0.2, 0.1], [5, 5]], 0.5).length, 2);

check("distinctCount ignores repeats",
    G.distinctCount([[1, 1], [2, 2], [1, 1]]), 2);

check("area of a 10x10 square", G.area([[0, 0], [10, 0], [10, 10], [0, 10]]), 100);

// -- hit testing ---------------------------------------------------------

const SQUARE = G.polygonFrom([[0, 0], [100, 0], [100, 100], [0, 100]]);
const DOUGHNUT = {
    type: "Polygon",
    coordinates: [
        [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]],
        [[40, 40], [60, 40], [60, 60], [40, 60], [40, 40]],
    ],
};
const TWO_PARTS = {
    type: "MultiPolygon",
    coordinates: [
        [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
        [[[50, 50], [60, 50], [60, 60], [50, 60], [50, 50]]],
    ],
};

check("a point inside a square is inside", G.containsPoint(SQUARE, 50, 50), true);
check("a point outside a square is outside", G.containsPoint(SQUARE, 150, 50), false);

// The check this probe exists for. Even-odd gets it right; nonzero does not.
check("the middle of a doughnut is NOT inside it", G.containsPoint(DOUGHNUT, 50, 50), false);
check("the ring of a doughnut IS inside it", G.containsPoint(DOUGHNUT, 20, 50), true);

check("either part of a multipolygon counts as inside",
    [G.containsPoint(TWO_PARTS, 5, 5), G.containsPoint(TWO_PARTS, 55, 55),
     G.containsPoint(TWO_PARTS, 30, 30)],
    [true, true, false]);

check("distanceToBoundary finds the nearest edge",
    G.distanceToBoundary(SQUARE, 50, 110), 10);

check("nearestVertex finds a corner within tolerance",
    G.nearestVertex(SQUARE, 98, 2, 5), { ring: 0, index: 1, distance: Math.hypot(2, 2) });

check("nearestVertex ignores one outside tolerance",
    G.nearestVertex(SQUARE, 50, 50, 5), null);

// -- simplification ------------------------------------------------------

// A "straight" freehand stroke: 200 samples wobbling by a third of a pixel.
const jittery = Array.from({ length: 200 }, (_, i) => [i, (i % 3) * 0.3]);
check("simplify collapses sub-tolerance wobble to its endpoints",
    G.simplify(jittery, 2).length, 2);

// ...and does not flatten anything a person would call a corner.
const corner = [[0, 0], [50, 0], [100, 0], [100, 50], [100, 100]];
check("simplify keeps a real corner", G.simplify(corner, 2), [[0, 0], [100, 0], [100, 100]]);

check("simplify with no tolerance changes nothing",
    G.simplify(corner, 0).length, corner.length);

// Recursion here would blow the stack, and losing a stroke to a RangeError at
// the moment of completion is the worst possible time for it.
const enormous = Array.from({ length: 200_000 }, (_, i) => [i, Math.sin(i / 500) * 40]);
let survivedLargeInput = true;
try {
    G.simplify(enormous, 1);
} catch (error) {
    survivedLargeInput = String(error);
}
check("simplify survives a 200,000-point trace", survivedLargeInput, true);

// -- validity ------------------------------------------------------------

check("a square does not cross itself",
    G.selfIntersects([[0, 0], [10, 0], [10, 10], [0, 10]]), false);

check("a bow-tie crosses itself",
    G.selfIntersects([[0, 0], [10, 10], [10, 0], [0, 10]]), true);

check("a concave but simple polygon does not cross itself",
    G.selfIntersects([[0, 0], [10, 0], [10, 10], [5, 4], [0, 10]]), false);

check("the crossing check declines rather than hangs on a huge ring",
    G.selfIntersects(Array.from({ length: G.MAX_INTERSECTION_CHECK + 10 },
        (_, i) => [i, i % 2])), false);

// -- editability and transforms ------------------------------------------

check("a plain polygon can have its vertices edited", G.isVertexEditable(SQUARE), true);
check("a shape with a hole cannot", G.isVertexEditable(DOUGHNUT), false);
check("a multi-part shape cannot", G.isVertexEditable(TWO_PARTS), false);

check("translate moves every ring, holes included",
    G.translate(DOUGHNUT, 5, 5).coordinates[1][0], [45, 45]);

check("bounds span every part of a multipolygon",
    G.bounds(TWO_PARTS), { minX: 0, minY: 0, maxX: 60, maxY: 60 });

check("clamp keeps a drawn point inside the image",
    [G.clamp([-5, 500], 100, 100), G.clamp([50, 50], 100, 100)],
    [[0, 100], [50, 50]]);

const report = {
    source: SOURCE.replace(REPO + "/", ""),
    checked: checks.length,
    failures,
};

console.error(JSON.stringify(report, null, 2));
process.exit(failures.length ? 1 : 0);
