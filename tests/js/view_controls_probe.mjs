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
                 color = false, layout = "single", inspect = null, show = null, area = null,
                 saved = null } = {}) {
    return {
        depth, frames, color, info: { width: 2000, height: 1000 },
        zStep: () => ({ value: 2, unit: "µm" }),
        get: () => ({ mode, z: 6, t: 0, layout, slab: { z0, z1, op, follow, q: 95 },
                      volume: { ...(show ? { show } : {}), ...(area ? { area } : {}),
                                ...(saved ? { saved } : {}) },
                      inspect: { mode: "full", tool: "cutaway", axes: { x: true, y: false, z: false },
                                 axesBy: { cutaway: { x: true, y: false, z: false }, crop: { x: true, y: true, z: true } },
                                 crop: { z: [0, depth - 1] }, cutaway: { z: { at: 20, keep: "hi" } },
                                 ...(inspect || {}) } }),
        set: () => { sets += 1; },
    };
}
const page = { volumeSupported: true, sectionsSupported: true, prefs: {}, cutSupported: true };
const ids = (vs, surface, extras = page) => R.resolve(R.context(vs, extras), { surface }).map((e) => e.id);
const strip = (vs, extras) => ids(vs, "strip", extras);
const popover = (vs, extras) => ids(vs, "popover", extras);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// -- the strip ------------------------------------------------------------------

check("one plane: modes, the Area tool, sections, play, the rail with its thumb and hover mark, cog",
      same(strip(stack()), ["mode.plane", "mode.slab", "mode.volume", "tool.area", "sections", "play", "rail",
                            "thumb", "hover", "cog"]), strip(stack()).join());
check("a projection adds its band",
      same(strip(stack({ mode: "slab" })), ["mode.plane", "mode.slab", "mode.volume", "tool.area", "sections",
                                            "play", "rail", "band", "thumb", "hover", "cog"]));
check("the Area tool: not in 3-D, not on a colour stack, not without a 3-D view",
      !strip(stack({ mode: "volume" })).includes("tool.area")
      && !strip(stack({ color: true })).includes("tool.area")
      && !strip(stack(), { ...page, volumeSupported: false }).includes("tool.area"));
const volume = strip(stack({ mode: "volume", follow: false }));
check("3-D: no sections, no rail -- the plane is frozen there; Cut and Crop instead",
      same(volume, ["mode.plane", "mode.slab", "mode.volume", "tool.cutaway", "tool.crop", "play", "cog"]),
      volume.join());
check("a colour stack is one plane at a time: no modes, no sections",
      same(strip(stack({ color: true })), ["play", "rail", "thumb", "hover", "cog"]));
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
check("the rail picks a plane over a plane and a projection; a band only over a projection",
      railIn(stack()).plane === true && railIn(stack()).band === false
      && railIn(stack({ mode: "slab" })).plane === true && railIn(stack({ mode: "slab" })).band === true);
check("a following projection's band is resized, not moved; a fixed one's is moved",
      railIn(stack({ mode: "slab", follow: true })).bandMove === false
      && railIn(stack({ mode: "slab", follow: false })).bandMove === true);

// -- the popover --------------------------------------------------------------------

check("one plane: the operation (to make a projection), the Area tool's size and Motion",
      same(popover(stack()), ["projection.op", "area.size", "view.motion"]), popover(stack()).join());
check("a projection: the operation, the planes, Follow; the Area's size; Motion",
      same(popover(stack({ mode: "slab" })), ["projection.op", "projection.range", "projection.follow",
                                              "area.size", "view.motion"]));
check("...and the percentile only when it is one",
      popover(stack({ mode: "slab", op: "pct" })).includes("projection.percentile")
      && !popover(stack({ mode: "slab", op: "mean" })).includes("projection.percentile"));
const vol = popover(stack({ mode: "volume" }));
check("3-D: Render, Motion, how Play turns (about, then speed) -- and no projection",
      same(vol, ["volume.composite", "view.motion", "rotate.axis", "rotate.speed"]), vol.join());
const surfacesIn = (extras) => popover(stack({ mode: "volume" }), { ...page, surfacesAvailable: true, ...extras })
    .filter((id) => id.startsWith("surfaces"));
const surfacesConfig = (extras) => R.configOf("surfaces", R.context(stack({ mode: "volume" }),
                                                                    { ...page, surfacesAvailable: true, ...extras }));
check("one Surfaces row: what is drawn once there is a surface, its light below it",
      surfacesIn({ surfaceCount: 2, surfaceStudio: true }).join() === "surfaces,surfaces.light"
      && surfacesConfig({ surfaceCount: 2, surfaceStudio: true }).show === true
      && surfacesConfig({ surfaceCount: 2, surfaceStudio: true }).studio === true);
check("...before the first, only making one: a mesh to import (Surface Studio where it is)",
      surfacesIn({ surfaceCount: 0 }).join() === "surfaces"
      && surfacesConfig({ surfaceCount: 0 }).show === false && surfacesConfig({ surfaceCount: 0 }).make === true
      && surfacesConfig({ surfaceCount: 0 }).studio === false);
check("without Plexora 3D nothing is made, but surfaces still show; with none, no row at all",
      surfacesIn({ surfaceCount: 1, surfaceStudio: true, surfaceMaking: false }).join() === "surfaces,surfaces.light"
      && surfacesConfig({ surfaceCount: 1, surfaceMaking: false }).make === false
      && surfacesIn({ surfaceCount: 0, surfaceMaking: false }).length === 0);
check("Motion wherever there is something to move through, and the prefs to keep it",
      popover(stack({ depth: 1, frames: 12 })).join() === "view.motion"
      && !popover(stack(), { ...page, prefs: null }).includes("view.motion"));
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

const whole = { mode: "volume", volume: { area: null }, inspect: { mode: "full" } };
check("into 3-D is the mode and the WHOLE stack: no area, the cut hidden (kept, for O), the projection its range",
      same(R.transition(R.context(stack({ mode: "slab", follow: true }), page), "volume"), whole)
      && same(R.transition(R.context(stack({ mode: "slab", follow: false }), page), "volume"), whole));
check("back to a plane is the mode, and the placed square goes",
      same(R.transition(R.context(stack({ mode: "volume" }), page), "plane"), { mode: "plane", volume: { area: null } }));

// -- the 3-D cut: the strip's tools and the panel (views/volumeDock.js) ----------------

const cutIn = (inspect, extra = {}) => stack({ mode: "volume", inspect, ...extra });
const dock = (vs, extras = page) => ids(vs, "dock", extras);
const STRIP3D = ["mode.plane", "mode.slab", "mode.volume", "tool.cutaway", "tool.crop", "play", "cog"];
const CUTS = [{}, { mode: "crop" }, { mode: "cutaway" }, { mode: "cutaway", axes: { x: false, z: true } }];
check("3-D, whatever the cut: Cut and Crop on the strip, no rail",
      CUTS.every((inspect) => same(strip(cutIn(inspect)), STRIP3D)),
      CUTS.map((inspect) => strip(cutIn(inspect)).join()).join(" | "));
const cuts = (ids) => ids.filter((id) => id === "tool.cutaway" || id === "tool.crop");
check("no Cut or Crop where the page cannot cut, nor outside 3-D",
      !cuts(strip(cutIn({}), { ...page, cutSupported: false })).length
      && !cuts(strip(stack())).length && !cuts(strip(stack({ mode: "slab" }))).length);
check("the popover's planes row is the planes the cut keeps",
      R.context(cutIn({ mode: "crop", axes: { x: false, z: true }, crop: { z: [4, 9] } }), page).keptZ0 === 4
      && R.context(cutIn({ mode: "crop", axes: { x: false, z: true }, crop: { z: [4, 9] } }), page).allPlanes === false
      && R.context(cutIn({}), page).allPlanes === true);
check("the context names the tool last chosen, kept while it is off",
      R.context(cutIn({ mode: "full", tool: "crop" }), page).cutTool === "crop"
      && R.context(cutIn({ mode: "cutaway" }), page).cutTool === "cutaway");
check("the popover no longer carries the cut (the strip's Cut and Crop do)",
      !popover(cutIn({ mode: "crop" })).some((id) => id.startsWith("volume.clip") || id.startsWith("cut.")));

const PANEL = ["cut.axes", "cut.face", "cut.handles", "cut.reset", "cut.history", "cut.interpolation",
               "cut.handleOpacity", "cut.toggle"];
check("the panel in 2-D: nothing", dock(stack()).length === 0 && dock(stack({ mode: "slab" })).length === 0);
check("the cut hidden: the same panel, for the tool last chosen (it stays out until its ×)",
      same(dock(cutIn({ mode: "full", tool: "crop" })), PANEL)
      && R.configOf("cut.axes", R.context(cutIn({ mode: "full", tool: "crop" }), page)).flip === false
      && R.configOf("cut.axes", R.context(cutIn({ mode: "full", tool: "cutaway" }), page)).flip === true,
      dock(cutIn({})).join());
check("a Cut: the axes, then faces, handles, reset and undo, then voxels and handles' strength",
      same(dock(cutIn({ mode: "cutaway" })), PANEL), dock(cutIn({ mode: "cutaway" })).join());
check("a Crop: exactly the same panel", same(dock(cutIn({ mode: "crop", axes: { x: true, y: true, z: true } })), PANEL),
      dock(cutIn({ mode: "crop" })).join());
check("only a Cut flips the side each axis keeps",
      R.configOf("cut.axes", R.context(cutIn({ mode: "cutaway" }), page)).flip === true
      && R.configOf("cut.axes", R.context(cutIn({ mode: "crop" }), page)).flip === false);
check("a Cut's axes are one at a time (a radio), a Crop's any",
      R.configOf("cut.axes", R.context(cutIn({ mode: "cutaway" }), page)).single === true
      && R.configOf("cut.axes", R.context(cutIn({ mode: "crop" }), page)).single === false);
const narrow = { mode: "crop", axes: { x: true, y: true, z: true }, crop: { x: [200, 600], y: [0, 1000], z: [0, 39] } };
check("a crop narrower than the image: Crop to area among the actions",
      same(dock(cutIn(narrow)), ["cut.axes", "cut.face", "cut.handles", "cut.reset", "cut.toArea", "cut.history",
                                 "cut.interpolation", "cut.handleOpacity", "cut.toggle"]), dock(cutIn(narrow)).join());
check("...not when it keeps the area, nor along Z only, nor in a Cut",
      !dock(cutIn(narrow, { area: { x: 200, y: 0, width: 400, height: 1000 } })).includes("cut.toArea")
      && !dock(cutIn({ ...narrow, crop: { x: [0, 2000], y: [0, 1000], z: [4, 9] } })).includes("cut.toArea")
      && !dock(cutIn({ ...narrow, mode: "cutaway" })).includes("cut.toArea"));
const surfacesOnly = { volume: false, surfaces: true };
check("surfaces only: nothing about voxels -- no faces, no Voxels",
      !dock(cutIn({ mode: "crop" }, { show: surfacesOnly })).some((id) => ["cut.face", "cut.interpolation"].includes(id))
      && dock(cutIn({ mode: "crop" }, { show: surfacesOnly })).includes("cut.axes"));
check("without the viewer prefs, no handle controls",
      !dock(cutIn({ mode: "cutaway" }), { ...page, prefs: null }).some((id) => id.startsWith("cut.handle")));
check("a page that cannot cut has no panel rows", dock(cutIn({ mode: "crop" }), { ...page, cutSupported: false }).length === 0);
check("no section, slab, mode or position rows are left",
      R.entries().every((entry) => !/^cut\.(mode|[xyz]|combine|thickness|opacity|context|link)$/.test(entry.id)
          && entry.id !== "volume.clip"));
check("no state flag for sections or slabs is left",
      !Object.keys(R.FLAGS).some((flag) => /^cut\.(section|slab|planes|combines|box)$/.test(flag))
      && same(R.CUTS, ["full", "cutaway", "crop"]));

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
