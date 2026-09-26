/**
 * FigureSchema's orientation geometry -- what the preview compositor, Quick
 * Edit and the capture tool turn panels with -- run against a recording 2-D
 * context.
 *
 *     node tests/js/figure_orientation_probe.mjs '<cases json>'
 *
 * Reports, on stderr as JSON, `mapped`: for each case, where the vectors
 * (10,0), (0,10) and (3,-7) land through `orientContext` -- which the Python
 * side compares with `render.orient_offset`, so the browser and the exporter
 * are held to one answer -- and `failures` from its own checks:
 *
 *   - `imageDelta` undoes `orientContext` (a drag moves the field under it);
 *   - `orientedViewport` is the frame's turned extent, centred on the frame;
 *   - `aspectViewport` keeps a turned panel's centre, frame width and
 *     orientation, and takes the height from the aspect;
 *   - `frameSize` / `orientationOf` / `sameOrientation` read what they say.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/plugins/figure_builder/static/figureSchema.js");
// FigureSchema's orientation helpers delegate to core's PlexoraViewerScene,
// which base.html loads on every page before any plugin script.
const SCENE = join(REPO, "plexora/client/src/js/services/viewerScene.js");
const cases = JSON.parse(process.argv[2] || "[]");

const failures = [];
let checked = 0;
function check(name, ok, detail) {
    checked += 1;
    if (!ok) failures.push(detail === undefined ? { name } : { name, detail });
}
const near = (a, b, tolerance = 1e-9) => Math.abs(a - b) <= tolerance;

class RecordingContext {
    constructor() { this.m = [1, 0, 0, 1, 0, 0]; }
    _mul(a, b, c, d, e, f) {
        const [A, B, C, D, E, F] = this.m;
        this.m = [A * a + C * b, B * a + D * b, A * c + C * d, B * c + D * d,
                  A * e + C * f + E, B * e + D * f + F];
    }
    translate(x, y) { this._mul(1, 0, 0, 1, x, y); }
    scale(x, y) { this._mul(x, 0, 0, y, 0, 0); }
    rotate(r) { this._mul(Math.cos(r), Math.sin(r), -Math.sin(r), Math.cos(r), 0, 0); }
    apply(x, y) {
        const [a, b, c, d] = this.m;
        return [a * x + c * y, b * x + d * y];
    }
}

const context = createContext({ Math, Number, Object, Boolean, JSON, Date, console });
runInContext(readFileSync(SCENE, "utf8"), context, { filename: SCENE });
runInContext(`${readFileSync(SOURCE, "utf8")}\n;globalThis.__Schema = FigureSchema;`,
    context, { filename: SOURCE });
const S = context.__Schema;

const mapped = cases.map((orientation) => {
    const recorder = new RecordingContext();
    S.orientContext(recorder, orientation);
    return [[10, 0], [0, 10], [3, -7]].map(([x, y]) => recorder.apply(x, y));
});

for (const orientation of cases) {
    const recorder = new RecordingContext();
    S.orientContext(recorder, orientation);
    for (const [x, y] of [[10, 0], [0, 10], [3, -7]]) {
        const [sx, sy] = recorder.apply(x, y);
        const back = S.imageDelta(orientation, sx, sy);
        check(`imageDelta undoes orientContext ${JSON.stringify(orientation)}`,
            near(back.x, x, 1e-9) && near(back.y, y, 1e-9), { back, x, y });
    }

    const viewport = S.orientedViewport(500, 300, 160, 100, orientation);
    const center = S.frameCenter(viewport);
    check(`the box is centred on the frame ${orientation.degrees}`,
        near(center.x, 500) && near(center.y, 300), center);
    const r = orientation.degrees * Math.PI / 180;
    check(`the box is the frame's turned extent ${orientation.degrees}`,
        near(viewport.w, 160 * Math.abs(Math.cos(r)) + 100 * Math.abs(Math.sin(r)))
        && near(viewport.h, 160 * Math.abs(Math.sin(r)) + 100 * Math.abs(Math.cos(r))), viewport);
    const size = S.frameSize(viewport);
    check("frameSize is the frame", size.w === 160 && size.h === 100, size);

    const wide = S.aspectViewport(viewport, 2, { width: 10, height: 10 });
    const wideCenter = S.frameCenter(wide);
    check(`aspectViewport keeps a turned panel's centre and width ${orientation.degrees}`,
        near(wideCenter.x, 500) && near(wideCenter.y, 300)
        && near(S.frameSize(wide).w, 160) && near(S.frameSize(wide).h, 80), wide);
    check("aspectViewport keeps the orientation",
        S.sameOrientation(S.orientationOf(wide), orientation));
}

check("an upright viewport has no orientation",
    S.orientationOf({ x: 0, y: 0, w: 5, h: 5 }) === null
    && S.orientationOf({ x: 0, y: 0, w: 5, h: 5,
                         orientation: { degrees: 360, flip_h: false, flip_v: false } }) === null);
check("an upright frame is its box", S.frameSize({ x: 0, y: 0, w: 7, h: 9 }).w === 7);
check("upright matches upright", S.sameOrientation(null, null));
check("upright does not match a mirror", !S.sameOrientation(null, { degrees: 0, flip_h: true }));
check("359.9999999999 is upright", S.normalizeDegrees(-1e-13) === 0);
check("the view transform maps across",
    S.sameOrientation(S.fromViewTransform({ degrees: 90, flipH: true, flipV: false }),
                      { degrees: 90, flip_h: true, flip_v: false })
    && S.fromViewTransform({ degrees: 0, flipH: false, flipV: false }) === null);
check("an upright aspectViewport is unchanged",
    JSON.stringify(S.aspectViewport({ x: 10, y: 10, w: 100, h: 50 }, 2))
    === JSON.stringify({ x: 10, y: 10, w: 100, h: 50 }));

process.stderr.write(JSON.stringify({ checked, failures, mapped }));
process.exitCode = failures.length ? 1 : 0;
