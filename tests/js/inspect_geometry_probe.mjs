/**
 * What part of a stack the 3-D view keeps (services/inspectGeometry.js).
 *
 * Every mode over every axis combination on an anisotropic stack: the box,
 * a cutaway along one axis only, the planes, the texture numbers relative to
 * the area the textures cover, the repairs `clean` makes, the area (cleaned,
 * framed, the cut kept inside it and moved with it), saved areas, and the
 * words -- microns only where the image states a spacing, never an assumed
 * one.
 *
 * Run directly:  node tests/js/inspect_geometry_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const JS = join(REPO, "plexora/client/src/js");

const failures = [];
let checks = 0;
function check(label, ok, why) {
    checks += 1;
    console.log(`${ok ? "ok" : "FAIL"} - ${label}`);
    if (!ok) failures.push(`${label}${why ? ` -- ${why}` : ""}`);
}
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const near = (a, b, eps = 1e-9) => Math.abs(a - b) <= eps;
const nearAll = (a, b, eps = 1e-9) => a.length === b.length && a.every((v, i) => near(v, b[i], eps));

const box = { console };
box.window = box;
box.globalThis = box;
runInContext(readFileSync(join(JS, "services/inspectGeometry.js"), "utf8"), createContext(box));
const G = box.PlexoraInspectGeometry;

// A 2000 x 1000 pixel stack of 40 planes; 0.5 µm pixels, 2 µm planes.
const info = {
    width: 2000, height: 1000, depth: 40,
    spacing: { x: { value: 0.5, unit: "µm", source: "metadata" },
               y: { value: 0.5, unit: "µm", source: "metadata" },
               z: { value: 2, unit: "µm", source: "manual" } },
};
const d = G.dims(info, 40);
const space = G.spacing(info);
check("dims read the stack", same(d, { width: 2000, height: 1000, depth: 40, area: null }), JSON.stringify(d));
check("a stated spacing is a length", space.x?.value === 0.5 && space.z?.value === 2);
const assumed = G.spacing({ spacing: { x: { value: 0.5, unit: "µm", source: "metadata" },
                                       z: { value: 1, unit: "µm", source: "assumed" } } });
check("an assumed z step is no length", assumed.z === null && assumed.x?.value === 0.5);
check("y is x unless stated", assumed.y?.value === 0.5);

// -- defaults and clean ------------------------------------------------------
const def = G.defaults(d);
check("the default is the whole stack, Cut the tool, on X", def.mode === "full" && def.tool === "cutaway"
      && same(def.axes, { x: true, y: false, z: false }), JSON.stringify(def));
check("each tool starts on its own axes: Cut on X, Crop on X, Y and Z",
      same(def.axesBy.cutaway, { x: true, y: false, z: false }) && same(def.axesBy.crop, { x: true, y: true, z: true }));
check("the default crop is everything", same(def.crop, { x: [0, 2000], y: [0, 1000], z: [0, 39] }));
check("no section or slab settings any more", !("slab" in def) && !("opacity" in def) && !("context" in def)
      && !("linkPanes" in def));

const messy = G.clean({ mode: "bogus", tool: "lasso", axes: { x: "yes" }, axesBy: { crop: { x: false, y: false, z: false } },
                        crop: { x: [900, 100], z: [50, -3.6] },
                        cutaway: { z: { at: 99, keep: "sideways" } }, face: "true" }, d);
check("an unknown mode is the whole stack, an unknown tool Cut", messy.mode === "full" && messy.tool === "cutaway");
check("axes are booleans, the tool's own when none", same(messy.axes, { x: true, y: false, z: false }),
      JSON.stringify(messy.axes));
check("a tool's axes with none on are its defaults", same(messy.axesBy.crop, { x: true, y: true, z: true }),
      JSON.stringify(messy.axesBy));
check("a range is ordered", same(messy.crop.x, [100, 900]), JSON.stringify(messy.crop.x));
check("z planes are whole and inside", same(messy.crop.z, [0, 39]), JSON.stringify(messy.crop.z));
check("a cut inside the stack", messy.cutaway.z.at === 39 && messy.cutaway.z.keep === "hi");
check("face only when true", messy.face === false);
const tight = G.clean({ crop: { x: [10, 10.2] } }, d);
check("a range keeps a pixel", tight.crop.x[1] - tight.crop.x[0] >= 1, JSON.stringify(tight.crop.x));
const legacy = G.clean({ mode: "section", axes: { z: true }, slab: { thickness: { z: 4 } }, opacity: 0.5,
                         context: 0.1, linkPanes: true }, d);
check("a cut saved as a section opens as the whole stack, its settings dropped", legacy.mode === "full"
      && !("slab" in legacy) && !("opacity" in legacy) && !("linkPanes" in legacy), JSON.stringify(legacy));
check("a tool on is the tool", G.clean({ mode: "crop", tool: "cutaway" }, d).tool === "crop");
check("off, the tool last chosen is kept", G.clean({ mode: "full", tool: "crop" }, d).tool === "crop");

const merged = G.merge(def, { crop: { x: [100, 200] }, cutaway: { y: { keep: "lo" } },
                              axesBy: { crop: { y: false } } });
check("merge keeps the other axes", same(merged.crop.y, [0, 1000]) && same(merged.crop.x, [100, 200]));
check("merge keeps a cutaway's position", merged.cutaway.y.keep === "lo" && merged.cutaway.y.at === 500);
check("merge keeps a tool's other axes", same(merged.axesBy.crop, { x: true, y: false, z: true })
      && same(merged.axesBy.cutaway, def.axesBy.cutaway), JSON.stringify(merged.axesBy));
check("merge leaves the base alone", same(def.crop.x, [0, 2000]) && def.axesBy.crop.y === true);

// -- resolve -----------------------------------------------------------------
const covered = [0, 0, 2000, 1000];
const full = G.resolve(def, d);
check("full keeps the whole box", same(full.box, { x: [0, 2000], y: [0, 1000], z: [0, 40] }) && !("hole" in full)
      && full.cut === G.CUT.none && full.axes.length === 0);

const crop = (axes, extra = {}) => G.resolve(G.clean({ mode: "crop", axes,
    crop: { x: [200, 1200], y: [100, 600], z: [4, 9] }, ...extra }, d), d);
const cx = crop({ x: true, y: false, z: false });
check("crop X narrows x only", same(cx.box.x, [200, 1200]) && same(cx.box.y, [0, 1000]) && same(cx.box.z, [0, 40]));
const cz = crop({ x: false, z: true });
check("crop Z keeps planes 4..9 inclusive", same(cz.box.z, [4, 10]));
const cxy = crop({ x: true, y: true, z: false });
check("crop X+Y keeps the intersection", same(cxy.box.x, [200, 1200]) && same(cxy.box.y, [100, 600])
      && same(cxy.box.z, [0, 40]));
const cxz = crop({ x: true, z: true });
check("crop X+Z", same(cxz.box.x, [200, 1200]) && same(cxz.box.z, [4, 10]) && same(cxz.box.y, [0, 1000]));
const cyz = crop({ x: false, y: true, z: true });
check("crop Y+Z", same(cyz.box.y, [100, 600]) && same(cyz.box.z, [4, 10]) && same(cyz.box.x, [0, 2000]));
const cxyz = crop({ x: true, y: true, z: true });
check("crop X+Y+Z keeps a box", same(cxyz.box, { x: [200, 1200], y: [100, 600], z: [4, 10] }));
const cface = crop({ x: true }, { face: true });
check("a crop's faces drawn solid when asked", cface.cut === G.CUT.faces && cface.face);

const cut = (axes, cutaway, extra = {}) => G.resolve(G.clean({ mode: "cutaway", axes, cutaway, ...extra }, d), d);
const kx = cut({ x: true, z: false }, { x: { at: 800, keep: "hi" } });
check("cutaway X keeps the high side", same(kx.box.x, [800, 2000]) && !("hole" in kx));
const kxl = cut({ x: true, z: false }, { x: { at: 800, keep: "lo" } });
check("cutaway X keeps the low side", same(kxl.box.x, [0, 800]));
const kz = cut({ x: false, z: true }, { z: { at: 10, keep: "lo" } });
check("cutaway Z keeps plane 10 and before", same(kz.box.z, [0, 11]));
// A cutaway is along ONE axis: one saved over two or three (before the
// corner was removed) opens on its first, x before y before z.
const kxy = cut({ x: true, y: true, z: false }, { x: { at: 800, keep: "hi" }, y: { at: 300, keep: "lo" } });
check("a cutaway over X and Y opens on X alone", same(kxy.axes, ["x"]) && same(kxy.box.x, [800, 2000])
      && same(kxy.box.y, [0, 1000]) && !("hole" in kxy), JSON.stringify(kxy));
const kyz = G.clean({ mode: "cutaway", axes: { x: false, y: true, z: true } }, d);
check("a cutaway's axes keep the first on", same(kyz.axes, { x: false, y: true, z: false }), JSON.stringify(kyz.axes));
check("a hidden cutaway's axes are one too",
      same(G.clean({ mode: "full", axesBy: { cutaway: { x: false, y: true, z: true } } }, d).axesBy.cutaway,
           { x: false, y: true, z: false }));
check("a crop keeps every axis it names",
      same(G.clean({ mode: "crop", axes: { x: true, y: true, z: true } }, d).axes, { x: true, y: true, z: true }));
const kraw = G.resolve({ mode: "cutaway", axes: { x: true, y: true, z: false },
                         cutaway: { x: { at: 800, keep: "hi" }, y: { at: 300, keep: "lo" } } }, d);
check("resolve cuts along the first axis even unclean", same(kraw.axes, ["x"]) && same(kraw.box.y, [0, 1000]));
const kface = cut({ x: true }, { x: { at: 800 } }, { face: true });
check("a cutaway's faces drawn solid when asked", kface.cut === G.CUT.faces);
check("the whole stack draws no faces, asked or not",
      G.resolve(G.clean({ mode: "full", face: true }, d), d).cut === G.CUT.none);

// -- texture space -----------------------------------------------------------
const tex = G.toTexture(cxz, covered, 40);
check("texture box", nearAll(tex.lo, [0.1, 0, 0.1]) && nearAll(tex.hi, [0.6, 1, 0.25]), JSON.stringify(tex));
check("the texture numbers carry nothing of sections, slabs or corners",
      same(Object.keys(tex).sort(), ["cut", "face", "hi", "lo"]), Object.keys(tex).join());
const area = [500, 200, 1500, 700];          // the 3-D view drew part of the plane
const texArea = G.toTexture(cxy, area, 40);
check("x and y relative to the area drawn", nearAll(texArea.lo, [0, 0, 0]) && nearAll(texArea.hi, [0.7, 0.8, 1]),
      JSON.stringify(texArea));
const texKept = G.toTexture(kxy, covered, 40);
check("a cutaway in texture space", nearAll(texKept.lo, [0.4, 0, 0]) && nearAll(texKept.hi, [1, 1, 1]));
check("the same cut is the same uniforms", G.sameTexture(G.toTexture(cxz, covered, 40), tex)
      && !G.sameTexture(tex, texKept));
check("kept planes of a crop", same(G.keptPlanes(cz, d), [4, 9]) && same(G.keptPlanes(full, d), [0, 39]));

// -- words -------------------------------------------------------------------
check("x in microns when stated", G.label("x", [200, 1200], space) === "100–600 µm", G.label("x", [200, 1200], space));
check("x in pixels when not", G.label("x", [200, 1200], null) === "200–1200 px");
check("z planes with their thickness", G.label("z", [4, 9], space) === "planes 4–9 · 12.0 µm", G.label("z", [4, 9], space));
check("z planes alone without a step", G.label("z", [4, 9], assumed) === "planes 4–9");
check("an assumed z step never becomes microns",
      !G.describe(G.clean({ mode: "cutaway", axes: { x: false, z: true } }, d), d, assumed).includes("µm"));
check("the whole stack is said as nothing", G.describe(def, d, space) === "");
const cropWords = G.describe(G.clean({ mode: "crop", axes: { x: true, z: true },
                                       crop: { x: [200, 1200], z: [4, 9] } }, d), d, space);
check("a crop in words", cropWords === "crop X 100–600 µm, Z planes 4–9 · 12.0 µm", cropWords);
const cutWords = G.describe(G.clean({ mode: "cutaway", axes: { x: true },
                                      cutaway: { x: { at: 800, keep: "lo" } } }, d), d, space);
check("a cut in words", cutWords === "cutaway X to 400 µm", cutWords);


// -- the area -----------------------------------------------------------------
check("an area is rounded out as the server rounds a box",
      same(G.cleanArea({ x: 10.4, y: 0, width: 16.2, height: 16 }, d), { x: 10, y: 0, width: 17, height: 16 }));
check("areaCovered is what X-Plexora-Box says",
      same(G.areaCovered({ x: 10.4, y: 0, width: 16.2, height: 16 }, d), [10, 0, 27, 16]));
check("an area is cut to the image", same(G.cleanArea({ x: 1900, y: -50, width: 300, height: 100 }, d),
                                          { x: 1900, y: 0, width: 100, height: 50 }));
check("no area: zero wide, outside, or not numbers",
      G.cleanArea({ x: 0, y: 0, width: 0, height: 10 }, d) === null
      && G.cleanArea({ x: 5000, y: 0, width: 10, height: 10 }, d) === null
      && G.cleanArea({ x: "a", y: 0, width: 10, height: 10 }, d) === null && G.cleanArea(null, d) === null);
check("a square at an edge keeps its size, moved inside",
      same(G.squareAt(1990, 5, 300, d), { x: 1700, y: 0, width: 300, height: 300 }));
check("a square larger than the image is the image", same(G.squareAt(10, 10, 5000, d),
                                                          { x: 0, y: 0, width: 2000, height: 1000 }));
check("inside never shrinks an area", same(G.inside({ x: -40, y: 900, width: 300, height: 300 }, d),
                                           { x: 0, y: 700, width: 300, height: 300 }));
const da = G.dims(info, 40, { x: 500, y: 200, width: 1000, height: 500 });
check("dims carry the area", same(da.area, { x: 500, y: 200, width: 1000, height: 500 }));
const defA = G.defaults(da);
check("the defaults are the area's", same(defA.crop.x, [500, 1500]) && same(defA.crop.y, [200, 700])
      && defA.cutaway.x.at === 1000 && defA.cutaway.y.at === 450, JSON.stringify(defA));
const inA = G.clean({ mode: "crop", axes: { x: true, y: true, z: false }, crop: { x: [0, 2000], y: [300, 400] },
                      cutaway: { x: { at: 10 } } }, da);
check("a crop is kept inside the area", same(inA.crop.x, [500, 1500]) && same(inA.crop.y, [300, 400]),
      JSON.stringify(inA.crop));
check("a cut is kept inside the area", inA.cutaway.x.at === 500);
check("the whole box is the area", same(G.resolve(G.clean({ mode: "full" }, da), da).box,
                                         { x: [500, 1500], y: [200, 700], z: [0, 40] }));
const moved = G.shift(inA, { dx: 100, dy: -50 });
check("shift moves the crop and the cut by the area's move",
      same(moved.crop.x, [600, 1600]) && same(moved.crop.y, [250, 350]) && moved.cutaway.x.at === 600
      && moved.cutaway.y.at === inA.cutaway.y.at - 50, JSON.stringify(moved));
check("no move, no shift", G.shift(inA, { dx: 0, dy: 0 }) === null);

console.log(`\n${checks - failures.length}/${checks} checks passed`);
if (failures.length) {
    console.log("FAILURES:\n  " + failures.join("\n  "));
    process.exit(1);
}
