/**
 * One display curve on the canvas and on the server (services/displayCurve.js).
 *
 * Pinned here:
 *
 *   **The same numbers as the server.** Every case of
 *   tests/golden/display_curve_vectors.json (written by
 *   plexora/server/utils/display_curve.py) is drawn at the same brightness,
 *   linear and asinh, inside and outside the window.
 *
 *   **The shader says the same thing.** frag.glsl's range_clamp carries the
 *   asinh branch on u_tile_cofactor, and the GL program sets that uniform;
 *   the 3-D view's `level` takes a cofactor per channel.
 *
 *   **A cofactor follows its units.** `normalize` divides a raw cofactor by a
 *   surface's scale, so a value placed proportionally draws the same.
 *
 * Run directly:  node tests/js/display_curve_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..", "..");
const require = createRequire(import.meta.url);
const curves = require(join(root, "plexora", "client", "src", "js", "services", "displayCurve.js"));

let passed = 0;
function check(name, condition, detail = "") {
    if (!condition) {
        console.log(`not ok - ${name} ${detail}`);
        process.exitCode = 1;
        return;
    }
    passed += 1;
    console.log(`ok - ${name}`);
}

const golden = JSON.parse(readFileSync(join(root, "tests", "golden", "display_curve_vectors.json"),
                                       "utf8"));
let worst = 0;
for (const item of golden.cases) {
    const curve = item.cofactor === null ? null : { kind: "asinh", cofactor: item.cofactor };
    const t = curves.level(item.x, item.window, curve);
    worst = Math.max(worst, Math.abs(t - item.t));
}
check(`every golden case matches the server (${golden.cases.length} cases)`, worst < 1e-9,
      `worst ${worst}`);

const window = [100, 4100];
check("linear when no curve", curves.level(2100, window, null) === 0.5);
check("a linear curve object is linear", curves.normalise({ kind: "linear" }) === null);
check("a zero cofactor is linear", curves.normalise({ kind: "asinh", cofactor: 0 }) === null);
const asinh = { kind: "asinh", cofactor: 50 };
check("asinh lifts a dim value above linear",
      curves.level(300, window, asinh) > curves.level(300, window, null));
check("asinh reaches white at the window's top", Math.abs(curves.level(4100, window, asinh) - 1) < 1e-12);
check("asinh is black at the window's bottom", curves.level(100, window, asinh) === 0);
let monotone = true;
let last = -1;
for (let x = 0; x <= 5000; x += 7) {
    const t = curves.level(x, window, asinh);
    if (t < last) monotone = false;
    last = t;
}
check("asinh is monotone", monotone);
let roundTrip = 0;
for (let t = 0; t <= 1; t += 0.05) {
    roundTrip = Math.max(roundTrip, Math.abs(curves.level(curves.inverse(t, window, asinh), window, asinh) - t));
}
check("inverse round-trips", roundTrip < 1e-9, `worst ${roundTrip}`);
const huge = { kind: "asinh", cofactor: 1e9 };
check("a huge cofactor is linear", Math.abs(curves.level(1100, window, huge) - 0.25) < 1e-6);

// Normalised units: a byte tile over [qmin, qmax], drawn in (raw - qmin) / span.
const qmin = 40;
const span = 8000;
const toUnit = (raw) => (raw - qmin) / span;
const c = curves.normalize(asinh, span);
let unitWorst = 0;
for (let raw = 0; raw <= 5000; raw += 37) {
    const direct = curves.level(raw, window, asinh);
    const viaUnits = curves.transfer(toUnit(raw), toUnit(window[0]), toUnit(window[1]), c);
    unitWorst = Math.max(unitWorst, Math.abs(direct - viaUnits));
}
check("a cofactor scaled into a surface's units draws the same", unitWorst < 1e-9,
      `worst ${unitWorst}`);
check("an unknown scale draws linear", curves.normalize(asinh, 0) === 0);
check("the Log toggle seeds span / 20", curves.seeded([0, 2000]).cofactor === 100);
check("same() compares curves", curves.same(null, { kind: "linear" })
      && curves.same(asinh, { kind: "asinh", cofactor: 50 }) && !curves.same(asinh, null));

const shader = readFileSync(join(root, "plexora", "client", "src", "shaders", "frag.glsl"), "utf8");
check("frag.glsl declares u_tile_cofactor", /uniform float u_tile_cofactor;/.test(shader));
check("range_clamp has the asinh branch",
      /asinh\(\(value - min_\) \/ u_tile_cofactor\) \/ top/.test(shader));
const gl = readFileSync(join(root, "plexora", "client", "src", "js", "views", "glInit.js"), "utf8");
check("the GL program sets u_tile_cofactor", /uniform1f\(this\.u_tile_cofactor/.test(gl));
const volume = readFileSync(join(root, "plexora", "client", "src", "js", "views", "volumeRenderer.js"),
                            "utf8");
check("the 3-D view's level takes a cofactor per channel",
      /float level\(float value, vec2 window, float c\)/.test(volume)
      && /uniform1fv\(uniform\("u_cofactor"\)/.test(volume));

console.log(`${passed} checks passed`);
