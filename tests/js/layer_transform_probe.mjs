/**
 * The client's half of one affine, checked against the table the server's half
 * is checked against.
 *
 * Plexora now has two implementations of the same matrix: ngff_transform.py
 * decides where a layer is registered, layerStack.js decides where it is drawn.
 * Two implementations of one matrix drift, and the drift is invisible -- a layer
 * a few pixels off looks like a registration that was never very good. One
 * table, read by both sides, is what stops that: tests/golden/transform_cases.json
 * carries the transform, the points, where each point lands, and whether OSD can
 * draw it at all. tests/test_layer_transform.py runs the Python side over the
 * same file.
 *
 * Also pinned here, because neither side can check it alone:
 *
 *   - **The tolerance is one number.** A case sitting just inside it must be
 *     accepted by both, and one just outside refused by both -- otherwise a
 *     layer is registered by the server and refused by the viewer, and the only
 *     symptom is a layer that never appears.
 *   - **placementFor's viewport conversion.** OSD sizes a TiledImage in VIEWPORT
 *     units, where the reference image is exactly 1 wide. A layer at the wrong
 *     scale still looks like an image, so nothing downstream will notice.
 *
 * Run directly:  node tests/js/layer_transform_probe.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.join(here, "..", "..");

(0, eval)(readFileSync(path.join(REPO, "plexora/client/src/js/views/layerStack.js"), "utf8"));
const {
    decomposeTransform, unsupportedReason, placementFor, invertTransform,
    TRANSFORM_TOLERANCE,
} = globalThis.PlexoraLayerStack;

const TABLE = JSON.parse(
    readFileSync(path.join(REPO, "tests/golden/transform_cases.json"), "utf8"));

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

const apply = ([a, b, c, d, e, f], x, y) => [a * x + c * y + e, b * x + d * y + f];
const close = (u, v, tol = 1e-9) => Math.abs(u - v) <= tol;


// -- the shared table ----------------------------------------------------

check("the tolerance in the table is the one the client uses",
    close(TABLE.tolerance, TRANSFORM_TOLERANCE, 1e-12),
    `${TABLE.tolerance} vs ${TRANSFORM_TOLERANCE}`);

for (const testCase of TABLE.cases) {
    const t = testCase.transform;

    const mapped = testCase.points.map(([x, y]) => apply(t, x, y));
    check(`${testCase.name}: points land where the table says`,
        mapped.every((p, i) => close(p[0], testCase.mapped[i][0])
            && close(p[1], testCase.mapped[i][1])),
        JSON.stringify(mapped));

    check(`${testCase.name}: ${testCase.unsupported ? `refused as ${testCase.unsupported}` : "is drawable"}`,
        unsupportedReason(t) === testCase.unsupported,
        `got ${JSON.stringify(unsupportedReason(t))}`);

    // The inverse is what every hover uses to put a pointer back into a layer's
    // own space. A transform with no inverse must say so rather than return one.
    const inv = invertTransform(t);
    if (testCase.unsupported === "degenerate") {
        check(`${testCase.name}: has no inverse rather than a wrong one`, inv === null);
    } else {
        const round = testCase.points.map(([x, y]) => {
            const [fx, fy] = apply(t, x, y);
            return apply(inv, fx, fy);
        });
        check(`${testCase.name}: the inverse round-trips every point`,
            round.every((p, i) => close(p[0], testCase.points[i][0], 1e-9)
                && close(p[1], testCase.points[i][1], 1e-9)),
            JSON.stringify(round));
    }
}


// -- decomposition -------------------------------------------------------

{
    const parts = decomposeTransform([2, 0, 0, 2, 30, 40]);
    check("a scale-and-shift decomposes into exactly that",
        close(parts.scaleX, 2) && close(parts.scaleY, 2) && close(parts.rotation, 0)
        && close(parts.translateX, 30) && close(parts.translateY, 40)
        && parts.flipped === false,
        JSON.stringify(parts));
}

{
    const deg = 7;
    const r = deg * Math.PI / 180;
    const parts = decomposeTransform([Math.cos(r), Math.sin(r), -Math.sin(r), Math.cos(r), 0, 0]);
    check("a rotation decomposes to its own angle, not its sine",
        close(parts.rotation, deg, 1e-9) && close(parts.scaleX, 1) && close(parts.shear, 0),
        `${parts.rotation} degrees`);
}

{
    const parts = decomposeTransform([-1, 0, 0, 1, 0, 0]);
    check("a mirror is reported as flipped, not as a 180 degree turn",
        parts.flipped === true && close(Math.abs(parts.rotation), 0),
        JSON.stringify(parts));
}


// -- placementFor --------------------------------------------------------

{
    // A layer half the reference's pixel size, scaled 2x -- so it covers the
    // reference exactly, and its viewport width must be 1.
    const p = placementFor([2, 0, 0, 2, 0, 0], 1000, 2000);
    check("a layer scaled to cover the reference is one viewport unit wide",
        close(p.width, 1), JSON.stringify(p));
}

{
    const p = placementFor([1, 0, 0, 1, 500, 250], 2000, 2000);
    check("translation is expressed in viewport units, not pixels",
        close(p.x, 0.25) && close(p.y, 0.125),
        "OSD's viewport is normalised so the reference image is exactly 1 wide");
}

{
    const p = placementFor([1, 0, 0, 1, 0, 0], 4000, 2000);
    check("an unscaled layer twice the reference's width is two units wide",
        close(p.width, 2), JSON.stringify(p));
}

check("a transform OSD cannot draw gets no placement at all",
    placementFor([1, 0, 0.5, 1, 0, 0], 100, 100) === null,
    "a refusal the caller must show, rather than something almost right");

{
    const p = placementFor(null, 2000, 2000);
    check("no transform places the layer over the reference unchanged",
        close(p.x, 0) && close(p.y, 0) && close(p.width, 1) && close(p.degrees, 0),
        JSON.stringify(p));
}


// -- placementFor, turned and mirrored -------------------------------------
//
// OSD rotates a TiledImage about the centre of its UNROTATED bounds
// (`_getRotationPoint` = `getBoundsNoRotate().getCenter()`) and mirrors it
// horizontally in place inside those bounds (`getTileBounds` reflects the tile
// grid; the drawer reflects each tile), the flip applied in image space and the
// rotation after it. `osdPosition` is that model, with OSD's own Point.rotate
// formula, so the check is "every corner lands where the affine sends it".

function osdPosition(p, w, h, [px, py]) {
    const s = p.width / w;                       // viewport units per layer px
    const height = h * s;
    const X = p.x + (p.flipped ? (w - px) : px) * s;
    const Y = p.y + py * s;
    const cx = p.x + p.width / 2;
    const cy = p.y + height / 2;
    const r = p.degrees * Math.PI / 180;
    const cos = Math.cos(r);
    const sin = Math.sin(r);
    return [cos * (X - cx) - sin * (Y - cy) + cx,
        sin * (X - cx) + cos * (Y - cy) + cy];
}

function checkPlacement(name, t, w, h, refW) {
    const p = placementFor(t, w, refW, h);
    if (!p) {
        check(`${name}: is drawable`, false, "placementFor returned null");
        return;
    }
    const height = p.width * h / w;
    const [ex, ey] = apply(t, w / 2, h / 2);
    check(`${name}: the placement's centre is the affine image of the layer's centre`,
        close(p.x + p.width / 2, ex / refW, 1e-9) && close(p.y + height / 2, ey / refW, 1e-9),
        JSON.stringify(p));
    const corners = [[0, 0], [w, 0], [w, h], [0, h]];
    const worst = Math.max(...corners.map((corner) => {
        const got = osdPosition(p, w, h, corner);
        const [wx, wy] = apply(t, ...corner);
        return Math.max(Math.abs(got[0] - wx / refW), Math.abs(got[1] - wy / refW));
    }));
    check(`${name}: OSD's turn and mirror put every corner where the affine does`,
        worst <= 1e-6, `worst ${worst.toExponential(2)} of the reference width`);
}

{
    // 200 x 100, a quarter turn clockwise (y down), moved to (300, 50).
    checkPlacement("a 200x100 layer at 90 degrees", [0, 1, -1, 0, 300, 50], 200, 100, 1000);
    const p = placementFor([0, 1, -1, 0, 300, 50], 200, 1000, 100);
    check("...and its translation is NOT its top-left",
        !close(p.x, 0.3) && close(p.degrees, 90, 1e-9), JSON.stringify(p));
}

checkPlacement("a mirrored layer (a < 0, d > 0)", [-1, 0, 0, 1, 200, 0], 200, 100, 1000);
checkPlacement("a mirrored, scaled, non-square layer", [-2, 0, 0, 2, 900, 40], 300, 120, 1000);

{
    const r = 30 * Math.PI / 180;
    const s = 1.5;
    checkPlacement("a layer turned 30 degrees and scaled",
        [s * Math.cos(r), s * Math.sin(r), -s * Math.sin(r), s * Math.cos(r), 120, -35],
        400, 250, 2000);
    // Mirror first, then turn: L = s R(theta) diag(-1, 1).
    checkPlacement("a layer turned 30 degrees and mirrored",
        [-s * Math.cos(r), -s * Math.sin(r), -s * Math.sin(r), s * Math.cos(r), 800, 60],
        400, 250, 2000);
}

{
    // A Visium HD bin grid registered onto its reference: grid units -> reference
    // px, a mirror plus a small turn (det < 0). Once as registered, once
    // supersampled 8x with a..d divided by 8 -- the same picture either way.
    const visium = [-0.94458, -0.00214, -0.00214, 0.94458, 4669.36, 330.81];
    const refW = 5000;
    checkPlacement("the Visium HD affine, undivided", visium, 5524, 5524, refW);
    const ss = 8;
    const divided = [...visium.slice(0, 4).map((v) => v / ss), ...visium.slice(4)];
    checkPlacement("the Visium HD affine, supersampled 8x", divided, 5524 * ss, 5524 * ss, refW);
    const one = placementFor(visium, 5524, refW, 5524);
    const eight = placementFor(divided, 5524 * ss, refW, 5524 * ss);
    check("...and supersampling does not move it",
        close(one.x, eight.x, 1e-12) && close(one.y, eight.y, 1e-12)
        && close(one.width, eight.width, 1e-12) && one.flipped && eight.flipped,
        `${JSON.stringify(one)} vs ${JSON.stringify(eight)}`);
}

{
    // With no turn and no mirror the translation IS the top-left, and the
    // placement must be exactly what it was before the centre was used.
    const t = [1.5, 0, 0, 1.5, 123.4, -56.7];
    const p = placementFor(t, 777, 3333, 400);
    check("an unturned, unmirrored placement is the translation, to the bit",
        p.x === 123.4 / 3333 && p.y === -56.7 / 3333 && p.width === 777 * 1.5 / 3333,
        JSON.stringify(p));
    checkPlacement("an unturned, non-square layer", t, 777, 400, 3333);
}


console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
}
