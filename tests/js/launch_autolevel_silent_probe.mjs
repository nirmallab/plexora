/**
 * A channel a launch state or a Prev/Next carry-over put on screen without a
 * range auto-levels -- and that levelling must never be written back to the
 * project's saved channel list.
 *
 * applyLaunchChannels marks such a slot `autoSilent` and init() drops
 * `_restoring` as soon as the restore returns. autoChannel then levels the
 * slot in TWO passes (the stats hint, then the mixture fit a second later),
 * and each pass lands through applyAutoRange, which schedules a save for
 * every slot that is not silent. The silence has to last for the whole
 * levelling, not just its first pass: otherwise the fit's landing writes the
 * carried arrangement as this sample's own.
 *
 * Driven through `Object.create(ViewerSidebar.prototype)` with the real
 * autoChannel / levelChannel / applyAutoRange / easeSlotRange /
 * scheduleSaveChannels, and stand-ins for the channel list's fetches and for
 * the save itself (persistChannelList, counted).
 *
 * Run directly:  node tests/js/launch_autolevel_silent_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SIDEBAR = "plexora/client/src/js/views/viewerSidebar.js";

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function makeSidebar() {
    const context = createContext({
        console: { ...console, debug() {} },
        Object, Array, String, Boolean, Number, Math, JSON, Set, Map, Promise,
        window: { addEventListener() {}, setTimeout, clearTimeout },
        document: { getElementById: () => null },
    });
    runInContext(`${readFileSync(join(REPO, SIDEBAR), "utf8")}\n;globalThis.__exported = ViewerSidebar;`,
        context, { filename: SIDEBAR });
    const sidebar = Object.create(context.__exported.prototype);
    const saves = [];
    const stats = { qmin: 0, qmax: 4000, vmin_hint: 120, vmax_hint: 2400 };
    Object.assign(sidebar, {
        persist: true,
        _restoring: false,
        _persistenceSuspended: 0,
        curveMode: "linear",
        channelSlots: [],
        channelList: {
            hasChannelGMM: {},
            ensureChannelStats: async () => stats,
            // The mixture fit comes back a while after the hint.
            async getAndDrawChannelGMM(name) {
                await sleep(30);
                this.hasChannelGMM[name] = { vmin: 150, vmax: 2600 };
            },
        },
        quantWindow: () => stats,
        isHdMode: () => true,
        setSlotRange(index, range) { this.channelSlots[index].range = range; },
        syncChannelSlider() {},
        setSlotCurve() {},
        askForHdIfCoarse() {},
        syncGatingContrast() {},
        persistChannelList() { saves.push(this.channelSlots.map((s) => s.name)); return Promise.resolve(null); },
    });
    return { sidebar, saves };
}

function slotFor(name, extra = {}) {
    return {
        index: 0, name, enabled: true, visible: true, range: [0, 65535],
        userRangeChanged: false, autoLeveled: false, autoLeveling: false, ...extra,
    };
}

// -- a carried / launched channel, levelled after init() let go of _restoring --

{
    const { sidebar, saves } = makeSidebar();
    const slot = slotFor("CD3", { autoSilent: true });
    sidebar.channelSlots.push(slot);
    // What init() does: the restore runs under _restoring, setSlotMarker
    // schedules the auto-level on a setTimeout(0), and _restoring is dropped
    // as soon as applyLaunchChannels returns -- before that timer fires.
    sidebar._restoring = true;
    sidebar.autoLevelChannelIfNeeded(slot);
    sidebar._restoring = false;
    await sleep(1000);  // both passes, then well past the 400 ms save debounce
    check("a carried channel is levelled by both passes (hint, then fit)",
        slot.autoLeveled === true && slot.range[0] === 150 && slot.range[1] === 2600,
        JSON.stringify(slot.range));
    check("a carried or launched channel's auto-level is never saved to the project",
        saves.length === 0,
        `persistChannelList called ${saves.length} time(s) with ${JSON.stringify(saves)}`);
}

// -- control: a channel the user picked IS saved once it is levelled ----------

{
    const { sidebar, saves } = makeSidebar();
    const slot = slotFor("CD8");
    sidebar.channelSlots.push(slot);
    sidebar.autoLevelChannelIfNeeded(slot);
    await sleep(1000);
    check("a channel the user picked is saved once its fit lands",
        saves.length >= 1, `persistChannelList called ${saves.length} time(s)`);
}

if (failures.length) {
    console.error(`\n${failures.length} check(s) failed`);
    process.exit(1);
}
console.log("all checks passed");
