/**
 * The viewer's automatic contrast always lands (tests/test_contrast_landing.py).
 *
 * What tests/browser/contrast.mjs found on the website and the desktop alike:
 *
 * - A contrast fit that could not be had was KEPT -- the error body, or
 *   `undefined` -- in `channelList.hasChannelGMM`, so every later levelling of
 *   that channel found "a fit" and stopped: its window never landed for the
 *   rest of the session. And one failed request ended the levelling there.
 * - A viewer left before its fit came back had already saved its rows (a
 *   first open saves at once) with the full range or the stats' hint as
 *   source "auto", and every later open restored that as levelled.
 *
 * The methods are sliced out of the shipped files (channelList.js,
 * viewerSidebar.js, dataLayer.js) and run against stand-ins.
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createContext, runInContext } from "node:vm";
import path from "node:path";
import assert from "node:assert/strict";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(here, "..", "..");
const read = (rel) => readFile(path.join(repoRoot, rel), "utf8");
const [sidebarSource, listSource, layerSource] = await Promise.all([
    read("plexora/client/src/js/views/viewerSidebar.js"),
    read("plexora/client/src/js/views/channelList.js"),
    read("plexora/client/src/js/services/dataLayer.js"),
]);

/** One method's text, by brace matching that skips comments and strings. */
function extractMethod(text, name) {
    const signature = new RegExp(`\\n    (?:async )?${name}\\(`).exec(text);
    if (!signature) throw new Error(`method ${name} not found`);
    const start = signature.index + 1;
    let i = text.indexOf(") {", start) + 2;
    let depth = 0;
    let state = "code";
    for (; i < text.length; i++) {
        const c = text[i];
        const next = text[i + 1];
        if (state === "line") { if (c === "\n") state = "code"; continue; }
        if (state === "block") { if (c === "*" && next === "/") { state = "code"; i++; } continue; }
        if (c === "/" && next === "/") { state = "line"; i++; continue; }
        if (c === "/" && next === "*") { state = "block"; i++; continue; }
        if (c === '"' || c === "'" || c === "`") {
            const quote = c;
            for (i++; i < text.length; i++) {
                if (text[i] === "\\") { i++; continue; }
                if (text[i] === quote) break;
            }
            continue;
        }
        if (c === "{") depth++;
        else if (c === "}" && --depth === 0) return text.slice(start, i + 1);
    }
    throw new Error(`unterminated body for ${name}`);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const context = createContext({
    window: { setTimeout, PlexoraStatus: null },
    document: { getElementById: (id) => (id === "channel_list" ? { getBoundingClientRect: () => ({ width: 300 }) } : null) },
    globalThis: { PlexoraDisplayCurve: { same: (a, b) => JSON.stringify(a) === JSON.stringify(b) } },
    LEVEL_RETRY_MS: [5, 5, 5],
    console: { debug: () => {}, log: () => {} },
    Promise, Error, Number, Math, Map, Array, JSON, URLSearchParams,
});
const methods = (source, names) => runInContext(`({\n${names.map((n) => extractMethod(source, n)).join(",\n")}\n})`, context);
const SIDEBAR = methods(sidebarSource, ["autoChannel", "levelChannel", "askAgain", "showsWindow",
                                        "contrastSettled", "whenContrastSettled"]);
const LIST = methods(listSource, ["ensureChannelStats", "getAndDrawChannelGMM", "fetchChannelGMM"]);
const LAYER = methods(layerSource, ["getChannelGMM", "getImageChannelStats"]);

const STATS = { image_histogram: [], image_min: 0, image_max: 100, qmin: 0, qmax: 1000,
                vmin_hint: 100, vmax_hint: 900 };
const FIT = { vmin: 250, vmax: 700, qmin: 0, qmax: 1000, log_window: null,
              image_gmm_1: [], image_gmm_2: [], image_gmm_3: [] };

/** A channel list over a data layer whose answers come from `answers`. */
function channelList(answers) {
    const asked = { stats: 0, gmm: 0 };
    const list = Object.assign(Object.create(LIST), {
        databaseDescription: {}, hasChannelGMM: {}, image_channels: {}, channelIDs: {},
        addSlider: () => {},
        drawChannelGMM: () => { throw new Error("no slider for this channel here"); },
        dataLayer: {
            getFullChannelName: (name) => name,
            getImageChannelStats: async () => { asked.stats += 1; return answers.stats(); },
            getChannelGMM: async () => { asked.gmm += 1; await sleep(5); return answers.gmm(); },
        },
    });
    return { list, asked };
}

/** A sidebar of one HD slot over `list`. */
function sidebar(list, slot = {}) {
    const applied = [];
    const bar = Object.assign(Object.create(SIDEBAR), {
        curveMode: "linear",
        channelList: list,
        channelSlots: [{ index: 0, name: "DNA", enabled: true, range: [0, 65536], userRangeChanged: false,
                         autoLeveled: false, autoLeveling: false, rangeSource: null, ...slot }],
        isHdMode: () => true,
        byteStep: () => 1,
        quantWindow: (name) => list.databaseDescription[name] || null,
        toRawRangeForSlot: (s) => s.range,
        setSlotCurve: () => {},
        askForHdIfCoarse: () => {},
        syncGatingContrast: () => {},
        applyAutoRange: (index, s, raw) => { s.range = [...raw]; applied.push([...raw]); },
        _channelsOpened: Promise.resolve(),
    });
    return { bar, slot: bar.channelSlots[0], applied };
}

// -- 1. a fit asked for while a request fails lands once one succeeds --------
{
    let failures = 2;
    const { list, asked } = channelList({ stats: () => STATS, gmm: () => (failures-- > 0 ? null : FIT) });
    const { bar, slot, applied } = sidebar(list);
    await bar.autoChannel(0);
    assert.deepEqual(applied, [[100, 900], [250, 700]], JSON.stringify(applied));
    assert.equal(slot.autoLeveled, true);
    assert.equal(asked.gmm, 3);
    console.log("a fit whose request failed twice is asked again and lands");
}

// -- 2. a fit that cannot be had is not kept, and is asked for again ----------
{
    let answer = null;
    const { list, asked } = channelList({ stats: () => STATS, gmm: () => answer });
    const { bar, slot, applied } = sidebar(list);
    assert.equal(await bar.autoChannel(0), false);
    assert.ok(!("DNA" in list.hasChannelGMM), "a failed fit was kept");
    assert.equal(slot.autoLeveled, false);
    assert.equal(slot.autoLeveling, false);
    assert.deepEqual(applied, [[100, 900]]);
    answer = FIT;
    await bar.autoChannel(0);
    assert.deepEqual(applied, [[100, 900], [100, 900], [250, 700]]);
    assert.equal(slot.autoLeveled, true);
    assert.equal(asked.gmm, 5);
    console.log("a fit that could not be had is not kept: the channel's next levelling lands");
}

// -- 3. one request per channel; a fit with no slider to draw on is kept ------
{
    const { list, asked } = channelList({ stats: () => STATS, gmm: () => FIT });
    const [a, b] = await Promise.all([list.getAndDrawChannelGMM("DNA"), list.getAndDrawChannelGMM("DNA")]);
    assert.equal(asked.gmm, 1);
    assert.equal(a, b);
    assert.equal(list.hasChannelGMM.DNA, FIT);
    console.log("callers asking at once share one request, and an undrawable fit is still the fit");
}

// -- 4. statistics that cannot be had are not kept either ---------------------
{
    let answer = null;
    const { list } = channelList({ stats: () => answer, gmm: () => FIT });
    await assert.rejects(() => list.ensureChannelStats("DNA"));
    assert.deepEqual(list.databaseDescription, {});
    answer = STATS;
    await list.ensureChannelStats("DNA");
    assert.equal(list.databaseDescription.DNA.qmax, 1000);
    console.log("statistics that could not be had are asked for again");
}

// -- 5. a restored "auto" row is fitted again: moved only where it is not the fit
{
    const { list } = channelList({ stats: () => STATS, gmm: () => FIT });
    // Saved before its fit came back: the full range, as a first open saves it.
    const stale = sidebar(list, { range: [0, 65536], autoLeveled: true, rangeSource: "auto" });
    await stale.bar.autoChannel(0, { refit: true });
    assert.deepEqual(stale.applied, [[250, 700]], "the hint was put over a restored window");
    // Saved after: the fit itself. Nothing moves, nothing is saved.
    const { list: list2 } = channelList({ stats: () => STATS, gmm: () => FIT });
    const fitted = sidebar(list2, { range: [250, 700], autoLeveled: true, rangeSource: "auto" });
    await fitted.bar.autoChannel(0, { refit: true });
    assert.deepEqual(fitted.applied, []);
    assert.equal(fitted.slot.autoLeveled, true);
    // A window the user set meanwhile is theirs.
    const { list: list3 } = channelList({ stats: () => STATS, gmm: () => FIT });
    const touched = sidebar(list3, { range: [10, 20], autoLeveled: true, rangeSource: "user", userRangeChanged: true });
    await touched.bar.autoChannel(0, { refit: true });
    assert.deepEqual(touched.applied, []);
    console.log("a restored auto row takes its fit when it was saved before the fit, and is left when it is the fit");
}

// -- 6. the settled signal waits for the levelling ----------------------------
{
    const { list } = channelList({ stats: () => STATS, gmm: () => FIT });
    const { bar, slot } = sidebar(list);
    assert.equal(bar.contrastSettled(), false, "an unlevelled channel read as settled");
    const settled = bar.whenContrastSettled({ timeoutMs: 2000, quietMs: 20 });
    await bar.autoChannel(0);
    assert.equal(await settled, true);
    assert.equal(slot.autoLeveled, true);
    const { bar: idle } = sidebar(list, { autoLeveling: true });
    assert.equal(await idle.whenContrastSettled({ timeoutMs: 200, quietMs: 20 }), false);
    console.log("whenContrastSettled resolves once both passes have landed, and not before");
}

// -- 7. the data layer hands back no error body as a fit ---------------------
{
    const layer = Object.create(LAYER);
    const respond = (status, body) => async () => ({ ok: status < 400, status, json: async () => body });
    const run = async (fetchImpl, call) => {
        context.fetch = fetchImpl;
        context.plexoraUrl = (route) => `/${route}`;
        context.datasource = "x";
        return call();
    };
    assert.equal(await run(respond(500, { error: "boom" }), () => layer.getChannelGMM("DNA")), null);
    assert.equal(await run(respond(200, { success: false, error: "x" }), () => layer.getChannelGMM("DNA")), null);
    assert.equal(await run(async () => { throw new TypeError("offline"); }, () => layer.getChannelGMM("DNA")), null);
    assert.equal(await run(respond(503, { error: "not loaded" }), () => layer.getImageChannelStats("DNA")), null);
    assert.deepEqual(await run(respond(200, FIT), () => layer.getChannelGMM("DNA")), FIT);
    assert.deepEqual(await run(respond(200, STATS), () => layer.getImageChannelStats("DNA")), STATS);
    console.log("a failed or error answer is no fit and no statistics");
}
