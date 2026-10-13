/**
 * Which depth control is offered in which mode (services/viewControls.js).
 *
 * The strip and its settings popover show what `resolve()` says and nothing
 * else, so the matrix pinned here IS the UI: every control against every
 * mode and every kind of image -- a plane, a projection that follows the
 * plane and one that does not, 3-D, a colour stack, a browser without WebGL2,
 * a stack too thin for sections, a single-plane time series. And the rules
 * that keep it honest: an unknown capability fails at registration, resolving
 * never touches the view state, a plugin's entry comes and goes with an
 * event.
 *
 * Run directly:  node tests/js/view_controls_probe.mjs
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

const events = [];
const box = {
    console,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent: (event) => { events.push(event); return true; },
    addEventListener: () => {},
    removeEventListener: () => {},
};
box.window = box;
box.globalThis = box;
runInContext(readFileSync(join(JS, "services/viewControls.js"), "utf8"), createContext(box));
const R = box.PlexoraViewControls;

let sets = 0;
function stack({ mode = "plane", follow = true, op = "max", z0 = 3, z1 = 9, depth = 40, frames = 1,
                 color = false, layout = "single", inspect = null, show = null } = {}) {
    return {
        depth, frames, color,
        zStep: () => ({ value: 2, unit: "µm" }),
        get: () => ({ mode, z: 6, t: 0, layout, slab: { z0, z1, op, follow, q: 95 },
                      volume: show ? { show } : {},
                      inspect: { mode: "full", axes: { x: false, y: false, z: true },
                                 crop: { z: [0, depth - 1] }, cutaway: { z: { at: 20, keep: "hi" } },
                                 slab: { thickness: { x: 8, y: 8, z: 4 }, composite: "max" }, ...(inspect || {}) } }),
        set: () => { sets += 1; },
    };
}
const page = { volumeSupported: true, sectionsSupported: true, prefs: {}, openCard: () => {}, cutSupported: true };
const ids = (vs, surface, extras = page) => R.resolve(R.context(vs, extras), { surface }).map((e) => e.id);
const strip = (vs, extras) => ids(vs, "strip", extras);
const popover = (vs, extras) => ids(vs, "popover", extras);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// -- the strip ------------------------------------------------------------------

check("one plane: modes, sections, play, the rail with its thumb and hover mark, readout, cog",
      same(strip(stack()), ["mode.plane", "mode.slab", "mode.volume", "sections", "play", "rail",
                            "thumb", "hover", "readout", "cog"]), strip(stack()).join());
check("a projection adds its band",
      same(strip(stack({ mode: "slab" })), ["mode.plane", "mode.slab", "mode.volume", "sections", "play",
                                            "rail", "band", "thumb", "hover", "readout", "cog"]));
const volume = strip(stack({ mode: "volume", follow: false }));
check("3-D: no sections, no plane thumb, no hover mark -- the plane is frozen there",
      same(volume, ["mode.plane", "mode.slab", "mode.volume", "play", "rail", "band", "readout", "cog"]),
      volume.join());
check("a colour stack is one plane at a time: no modes, no sections",
      same(strip(stack({ color: true })), ["play", "rail", "thumb", "hover", "readout", "cog"]));
check("without WebGL2 there is no 3-D button",
      !strip(stack(), { ...page, volumeSupported: false }).includes("mode.volume"));
check("without section panes there is no Sections button",
      !strip(stack(), { ...page, sectionsSupported: false }).includes("sections"));
check("a single-plane time series: play its frames, the cog, the frame steps; nothing about z",
      same(strip(stack({ depth: 1, frames: 12 })), ["play", "cog", "frame"]));
check("a time series of stacks has the frame steps beside everything else",
      strip(stack({ frames: 5 })).at(-1) === "frame");

// -- what each does -----------------------------------------------------------------

const config = (vs, id) => R.configOf(id, R.context(vs, page));
check("Play steps planes; slides a fixed projection's range; plays frames; turns the stack in 3-D",
      config(stack(), "play").kind === "planes"
      && config(stack({ mode: "slab", follow: true }), "play").kind === "planes"
      && config(stack({ mode: "slab", follow: false }), "play").kind === "band"
      && config(stack({ depth: 1, frames: 9 }), "play").kind === "frames"
      && config(stack({ mode: "volume" }), "play").kind === "rotate");
const railIn = (vs) => config(vs, "rail");
check("the rail picks a plane everywhere but 3-D, where only the clip range moves",
      railIn(stack()).plane === true && railIn(stack({ mode: "volume" })).plane === false
      && railIn(stack({ mode: "volume" })).bandMove === true && railIn(stack({ mode: "volume" })).band === true);
check("a following projection's band is resized, not moved; a fixed one's is moved",
      railIn(stack({ mode: "slab", follow: true })).bandMove === false
      && railIn(stack({ mode: "slab", follow: false })).bandMove === true);

// -- the popover --------------------------------------------------------------------

check("one plane: the operation (to make a projection) and the Depth card",
      same(popover(stack()), ["projection.op", "card"]), popover(stack()).join());
check("a projection: the operation, the planes, Follow",
      same(popover(stack({ mode: "slab" })), ["projection.op", "projection.range", "projection.follow", "card"]));
check("...and the percentile only when it is one",
      popover(stack({ mode: "slab", op: "pct" })).includes("projection.percentile")
      && !popover(stack({ mode: "slab", op: "mean" })).includes("projection.percentile"));
const vol = popover(stack({ mode: "volume" }));
check("3-D: composite, view, planes drawn, how Play turns -- and no projection",
      same(vol, ["volume.composite", "volume.view", "volume.clip", "rotate.speed", "rotate.axis",
                 "rotate.direction", "card"]), vol.join());
check("surfaces once there is one; a new one wherever Surface Studio is",
      popover(stack({ mode: "volume" }), { ...page, surfacesAvailable: true, surfaceCount: 2, surfaceStudio: true })
          .filter((id) => id.startsWith("surfaces.")).join() === "surfaces.show,surfaces.light,surfaces.new,surfaces.import"
      && popover(stack({ mode: "volume" }), { ...page, surfacesAvailable: true, surfaceCount: 0 })
          .filter((id) => id.startsWith("surfaces.")).join() === "surfaces.import");
check("without Plexora 3D there is no new surface or mesh import, but surfaces still show",
      popover(stack({ mode: "volume" }), { ...page, surfacesAvailable: true, surfaceCount: 1, surfaceStudio: true,
                                           surfaceMaking: false })
          .filter((id) => id.startsWith("surfaces.")).join() === "surfaces.show,surfaces.light");
check("the grid where the 3-D view has one, its options while it is on",
      popover(stack({ mode: "volume" }), { ...page, grid: true }).includes("grid.on")
      && !popover(stack({ mode: "volume" }), { ...page, grid: true }).includes("grid.options")
      && popover(stack({ mode: "volume" }), { ...page, grid: true, gridOn: true }).includes("grid.options")
      && !popover(stack(), { ...page, grid: true, gridOn: true }).some((id) => id.startsWith("grid.")));
check("no rotation settings without the prefs to keep them",
      !popover(stack({ mode: "volume" }), { ...page, prefs: null }).some((id) => id.startsWith("rotate.")));
check("a colour stack's popover offers no projection",
      !popover(stack({ color: true })).some((id) => id.startsWith("projection.")));

// -- switching modes ---------------------------------------------------------------

check("into 3-D is just the mode: the 3-D view keeps its own cut, the projection its range",
      same(R.transition(R.context(stack({ mode: "slab", follow: true }), page), "volume"), { mode: "volume" })
      && same(R.transition(R.context(stack({ mode: "slab", follow: false }), page), "volume"), { mode: "volume" }));
check("back to a plane is just the mode",
      same(R.transition(R.context(stack({ mode: "volume" }), page), "plane"), { mode: "plane" }));

// -- the 3-D cut: the rail and the dock (views/volumeDock.js) ------------------------

const cutIn = (inspect, extra = {}) => stack({ mode: "volume", inspect, ...extra });
const dock = (vs, extras = page) => ids(vs, "dock", extras);
const RAIL3D = ["mode.plane", "mode.slab", "mode.volume", "play", "rail"];
check("3-D, whole: the rail is the planes drawn, its band resized and moved -- a crop it becomes",
      same(strip(cutIn({})), [...RAIL3D, "band", "readout", "cog"])
      && same(railIn(cutIn({})), { plane: false, band: true, bandMove: true, cut: "full" }));
check("a Z section: the rail's thumb is its plane, picked and dragged; no band",
      same(strip(cutIn({ mode: "section" })), [...RAIL3D, "thumb", "hover", "readout", "cog"])
      && railIn(cutIn({ mode: "section" })).plane === true && railIn(cutIn({ mode: "section" })).band === false);
check("an X section only: nothing along Z on the rail",
      same(strip(cutIn({ mode: "section", axes: { x: true, z: false } })), [...RAIL3D, "readout", "cog"]));
check("a Z slab: its planes as a band and its plane as the thumb; the thumb moves it",
      same(strip(cutIn({ mode: "slab" })), [...RAIL3D, "band", "thumb", "hover", "readout", "cog"])
      && railIn(cutIn({ mode: "slab" })).plane === true && railIn(cutIn({ mode: "slab" })).band === false);
check("a Z crop's range is resized and moved; a Z cutaway's one cut moves",
      railIn(cutIn({ mode: "crop" })).bandMove === true
      && railIn(cutIn({ mode: "cutaway" })).band === true && railIn(cutIn({ mode: "cutaway" })).bandMove === false);
check("the popover's planes row is the planes the cut keeps",
      R.context(cutIn({ mode: "crop", crop: { z: [4, 9] } }), page).keptZ0 === 4
      && R.context(cutIn({ mode: "crop", crop: { z: [4, 9] } }), page).allPlanes === false
      && R.context(cutIn({}), page).allPlanes === true);

const TOOLS = ["cut.handles", "cut.reset", "cut.history", "cut.interpolation", "cut.handleOpacity"];
check("the dock in 2-D: nothing", dock(stack()).length === 0 && dock(stack({ mode: "slab" })).length === 0);
check("whole: the mode switch, undo, how voxels are read",
      same(dock(cutIn({})), ["cut.mode", "cut.history", "cut.interpolation"]), dock(cutIn({})).join());
check("a Z cutaway: the axes, its cut, the cut faces, the tools",
      same(dock(cutIn({ mode: "cutaway" })), ["cut.mode", "cut.axes", "cut.z", "cut.face", ...TOOLS]),
      dock(cutIn({ mode: "cutaway" })).join());
check("X+Y sections: two positions, their opacity, the surroundings, the panes",
      same(dock(cutIn({ mode: "section", axes: { x: true, y: true, z: false } })),
           ["cut.mode", "cut.axes", "cut.x", "cut.y", "cut.opacity", "cut.context", ...TOOLS, "cut.link"]),
      dock(cutIn({ mode: "section", axes: { x: true, y: true, z: false } })).join());
check("a Z slab: how it combines and how thick, the surroundings",
      same(dock(cutIn({ mode: "slab" })),
           ["cut.mode", "cut.axes", "cut.z", "cut.combine", "cut.thickness", "cut.context", ...TOOLS, "cut.link"]),
      dock(cutIn({ mode: "slab" })).join());
check("a single-plane slab has no thickness",
      !dock(cutIn({ mode: "slab", slab: { thickness: { z: 4 }, composite: "single" } })).includes("cut.thickness"));
check("an X+Z crop: two ranges, the cut faces",
      same(dock(cutIn({ mode: "crop", axes: { x: true, z: true } })),
           ["cut.mode", "cut.axes", "cut.x", "cut.z", "cut.face", ...TOOLS]),
      dock(cutIn({ mode: "crop", axes: { x: true, z: true } })).join());
check("X+Y shows exactly the X and Y rows, X+Z the X and Z, Y+Z the Y and Z",
      ["xy", "xz", "yz"].every((pair) => {
          const axes = { x: pair.includes("x"), y: pair.includes("y"), z: pair.includes("z") };
          return same(dock(cutIn({ mode: "crop", axes })).filter((id) => /^cut\.[xyz]$/.test(id)),
                      [...pair].map((a) => `cut.${a}`));
      }));
const surfacesOnly = { volume: false, surfaces: true };
check("surfaces only: nothing about voxels -- no combining, no surroundings, no faces",
      !dock(cutIn({ mode: "slab" }, { show: surfacesOnly })).some((id) => ["cut.combine", "cut.context",
                                                                          "cut.interpolation"].includes(id))
      && !dock(cutIn({ mode: "crop" }, { show: surfacesOnly })).includes("cut.face")
      && dock(cutIn({ mode: "slab" }, { show: surfacesOnly })).includes("cut.thickness"));
check("no section panes: no linking them",
      !dock(cutIn({ mode: "section" }), { ...page, sectionsSupported: false }).includes("cut.link"));
check("a page that cannot cut has no dock rows", dock(cutIn({ mode: "crop" }), { ...page, cutSupported: false }).length === 0);
check("each axis row says what it does in the mode",
      R.configOf("cut.x", R.context(cutIn({ mode: "crop", axes: { x: true } }), page)).kind === "range"
      && R.configOf("cut.x", R.context(cutIn({ mode: "cutaway", axes: { x: true } }), page)).kind === "cut"
      && R.configOf("cut.x", R.context(cutIn({ mode: "slab", axes: { x: true } }), page)).kind === "plane");
const modesShown = ["full", "cutaway", "section", "slab", "crop"].map((mode) => dock(cutIn({ mode })));
check("never an incompatible row: no slab rows outside Slab, no faces outside Crop and Cutaway",
      modesShown.every((rows, i) => (i === 3 || !rows.some((id) => id === "cut.combine" || id === "cut.thickness"))
          && ((i === 1 || i === 4) || !rows.includes("cut.face"))
          && (i === 2 || !rows.includes("cut.opacity"))));

// -- rules ------------------------------------------------------------------------------

let threw = null;
try {
    R.register({ id: "probe.typo", needs: ["webgl"] });
} catch (error) {
    threw = error.message;
}
check("an unknown capability fails at registration, not as a control that never shows",
      /no capability "webgl"/.test(threw || ""), threw);
threw = null;
try {
    R.register({ id: "probe.flag", when: ["volume"] });
} catch (error) {
    threw = error.message;
}
check("so does an unknown state flag", /no state flag/.test(threw || ""), threw);
threw = null;
try {
    R.register({ id: "mode.plane" });
} catch (error) {
    threw = error.message;
}
check("and a duplicate id", /registered already/.test(threw || ""), threw);

events.length = 0;
const off = R.register({ id: "gating.view", surfaces: ["gating"], modes: ["plane", "slab"], needs: ["depth"],
                         excludes: ["rotating"] });
check("a plugin's entry is announced", events.some((e) => e.type === "plexora:view-controls-changed"
                                                  && e.detail.id === "gating.view"));
check("and resolves like core's: shown in a plane, gone in 3-D",
      R.visible("gating.view", R.context(stack(), page))
      && !R.visible("gating.view", R.context(stack({ mode: "volume" }), page)));
check("...on its own surface, never in the strip",
      !strip(stack()).includes("gating.view") && ids(stack(), "gating").includes("gating.view"));
const dependent = R.register({ id: "probe.needsSections", surfaces: ["probe"], depends: ["sections"] });
check("`depends` follows the entry it names",
      R.visible("probe.needsSections", R.context(stack(), page))
      && !R.visible("probe.needsSections", R.context(stack({ mode: "volume" }), page)));
threw = null;
try {
    R.register({ id: "probe.chain", depends: ["gating.view"] });
} catch (error) {
    threw = error.message;
}
check("an entry may not lean on one that has rules of its own (two passes, never a fixed point)",
      /excludes or depends of its own/.test(threw || ""), threw);
dependent();
off();
check("unregistered, it is gone and that is announced too",
      !R.visible("gating.view", R.context(stack(), page))
      && events.some((e) => e.detail?.id === "gating.view" && e.detail.registered === false));

sets = 0;
for (const mode of ["plane", "slab", "volume"]) {
    for (const surface of ["strip", "popover", "dock"]) ids(stack({ mode }), surface);
}
check("resolving never changes the view state", sets === 0);
check("every core entry says what it is for",
      R.entries().filter((e) => !e.id.startsWith("probe.") && !e.id.startsWith("gating.")).every((e) => e.doc));

if (failures.length) {
    console.log(`\n${failures.length} of ${checks} checks FAILED:`);
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
}
console.log(`\nall ${checks} checks passed`);
