/**
 * Renaming an image's channels while the viewer is open.
 *
 * Applying a channel-name file used to reload the page. It does not any more
 * (main.js's adoptChannelNames), because a rename moves no index: the image is
 * the same file, imageData keeps its order, and so every slider, colour, tile
 * and cached fit still belongs to the channel it belonged to a moment ago.
 * Only the NAME changes -- and names are keys.
 *
 * That is the whole risk, and it is what these two methods carry. A container
 * left on the old key does not fail loudly: it shows the renamed channel
 * twice, once under its new name and once as a slot still naming a channel the
 * server no longer has. The stats request that second one makes is the 404
 * (previously a StopIteration) the user reported.
 *
 * Both methods are driven through Object.create(...prototype) rather than a
 * real constructor: what is under test is the re-keying, and building a whole
 * ChannelList needs d3, sliders and a sidebar's worth of markup that has
 * nothing to do with it.
 *
 * Run directly:  node tests/js/channel_rename_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const CLIENT = join(REPO, "plexora/client/src/js/views");

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// -- the sidebar's channel list ----------------------------------------------

/** One row of the channel list: the label the user reads, and the colour
 *  swatch whose datum the colour picker reads the channel's name back out of. */
function makeRow(name, channelID) {
    const label = { textContent: name };
    const row = {
        querySelector: (selector) => (selector === ".channel-name" ? label : null),
    };
    return {
        id: `color_${channelID}`,
        __data__: { color: "white", name },
        closest: (selector) => (selector === ".list-group-item" ? row : null),
        label,
    };
}

function loadChannelList() {
    const rows = new Map();
    const context = createContext({
        console, Object, Array, String, Boolean, Number, Math, JSON, Set, Map,
        window: { addEventListener() {} },
        document: { getElementById: (id) => rows.get(id) || null },
        d3: { select: (node) => ({ datum: () => node.__data__ }) },
    });
    // `class X {}` at the top of a script is a lexical binding, not a property
    // of the global object, so the context cannot be read for it directly.
    runInContext(`${readFileSync(join(CLIENT, "channelList.js"), "utf8")}
        ;globalThis.ChannelList = ChannelList;`, context, { filename: "channelList.js" });
    return { ChannelList: context.ChannelList, rows };
}

{
    const { ChannelList, rows } = loadChannelList();
    const list = Object.create(ChannelList.prototype);
    // Two channels, one of them switched on with a slider, a fit and a colour
    // already fetched -- i.e. the state a rename has to carry across.
    ["channel_0", "channel_1"].forEach((id, index) => {
        rows.set(`color_${id}`, makeRow(`Channel_${index}`, id));
    });
    Object.assign(list, {
        columns: ["Channel_0", "Channel_1"],
        channelIDs: { Channel_0: "channel_0", Channel_1: "channel_1" },
        image_channels: { Channel_0: [3, 900], Channel_1: [0, 65535] },
        hasChannelGMM: { Channel_0: { vmin: 12, vmax: 400 } },
        sel: { Channel_0: [3, 900] },
        sliders: new Map([["Channel_0", "slider-0"], ["Channel_1", "slider-1"]]),
        selections: ["Channel_0"],
        // Keyed by INDEX, not by name -- these must survive untouched.
        rangeConnector: { 0: [0.1, 0.9] },
        colorConnector: { 0: { color: "red" } },
    });

    list.renameChannels([
        { index: 0, fromShort: "Channel_0", fromFull: "Channel_0", to: "DAPI" },
        { index: 1, fromShort: "Channel_1", fromFull: "Channel_1", to: "CD3" },
    ]);

    check("the list's own order is renamed in place",
        same(list.columns, ["DAPI", "CD3"]), JSON.stringify(list.columns));
    check("...and so is the row the user had switched on",
        same(list.selections, ["DAPI"]), JSON.stringify(list.selections));
    check("the row's element id follows the name",
        list.channelIDs.DAPI === "channel_0" && list.channelIDs.Channel_0 === undefined,
        "a stale key here means the next click builds a second slider");
    check("...as does its slider",
        list.sliders.get("DAPI") === "slider-0" && !list.sliders.has("Channel_0"),
        "the slider itself is the same object: the pixels did not change");
    check("...its contrast range", same(list.image_channels.DAPI, [3, 900]));
    check("...its fitted auto-level", list.hasChannelGMM.DAPI.vmin === 12,
        "re-fitting costs ~1s per channel and would answer the same");
    check("...and what the viewer is told is selected",
        same(list.sel.DAPI, [3, 900]) && list.sel.Channel_0 === undefined);
    check("what is keyed by index is left alone",
        same(list.rangeConnector, { 0: [0.1, 0.9] })
        && list.colorConnector[0].color === "red",
        "a rename moves no index -- that is what makes this safe at all");

    check("the label in the row is rewritten",
        rows.get("color_channel_0").label.textContent === "DAPI"
        && rows.get("color_channel_1").label.textContent === "CD3",
        "the whole point: the channel the user is looking at is renamed too");
    check("...and so is the name the colour picker reports against",
        rows.get("color_channel_0").__data__.name === "DAPI",
        "a stale datum saves the new colour against a channel that is gone");
}

{
    const { ChannelList, rows } = loadChannelList();
    const list = Object.create(ChannelList.prototype);
    ["channel_0", "channel_1"].forEach((id, index) => {
        rows.set(`color_${id}`, makeRow(index === 0 ? "CD4" : "CD8", id));
    });
    Object.assign(list, {
        columns: ["CD4", "CD8"],
        channelIDs: { CD4: "channel_0", CD8: "channel_1" },
        image_channels: { CD4: [1, 1], CD8: [2, 2] },
        hasChannelGMM: {},
        sel: {},
        sliders: new Map(),
        selections: [],
        rangeConnector: {},
        colorConnector: {},
    });

    // The panel file had two markers the wrong way round. Moving key by key,
    // CD4 -> CD8 would overwrite CD8 before CD8 -> CD4 could read it.
    list.renameChannels([
        { index: 0, fromShort: "CD4", fromFull: "CD4", to: "CD8" },
        { index: 1, fromShort: "CD8", fromFull: "CD8", to: "CD4" },
    ]);
    check("two channels that swap names do not eat each other",
        list.channelIDs.CD8 === "channel_0" && list.channelIDs.CD4 === "channel_1"
        && same(list.image_channels.CD8, [1, 1]) && same(list.image_channels.CD4, [2, 2]),
        JSON.stringify(list.channelIDs));
}

// -- the viewer sidebar's channel slots ---------------------------------------

function loadViewerSidebar() {
    const context = createContext({
        console, Object, Array, String, Boolean, Number, Math, JSON, Set, Map,
        window: { addEventListener() {} },
        document: {},
    });
    runInContext(`${readFileSync(join(CLIENT, "viewerSidebar.js"), "utf8")}
        ;globalThis.ViewerSidebar = ViewerSidebar;`, context, { filename: "viewerSidebar.js" });
    return context.ViewerSidebar;
}

{
    const ViewerSidebar = loadViewerSidebar();
    const sidebar = Object.create(ViewerSidebar.prototype);
    const optionsSetTo = [];
    const synced = [];
    const select = (index) => ({
        index,
        setOptions(names) { optionsSetTo.push([...names]); },
    });
    Object.assign(sidebar, {
        columns: ["Channel_0", "Channel_1", "Channel_2"],
        channelSlots: [
            { index: 0, name: "Channel_0", colorHex: "#2388ff" },
            { index: 1, name: "Channel_2", colorHex: "#ff2d2d" },
            // An empty slot: nothing to rename, and it must not become
            // "undefined" on the way through.
            { index: 2, name: "", colorHex: "#2bd46f" },
        ],
        markerRangeOverrides: new Map([["Channel_2", [10, 200]]]),
        markerSelects: new Map([[0, select(0)], [1, select(1)], [2, select(2)]]),
        // Stubbed: what is under test here is the re-keying and the order it
        // happens in. syncSlotDom itself is the existing path every slot edit
        // already goes through, and it needs the sidebar's real markup.
        syncSlotDom(slot) { synced.push({ name: slot.name, optionLists: optionsSetTo.length }); },
    });

    sidebar.renameChannels([
        { index: 0, fromShort: "Channel_0", fromFull: "Channel_0", to: "DAPI" },
        { index: 1, fromShort: "Channel_1", fromFull: "Channel_1", to: "CD3" },
        { index: 2, fromShort: "Channel_2", fromFull: "Channel_2", to: "CD8" },
    ]);

    check("every marker select is offered the new names",
        optionsSetTo.length === 3
        && optionsSetTo.every((names) => same(names, ["DAPI", "CD3", "CD8"])),
        JSON.stringify(optionsSetTo[0]));
    check("the slot the user is looking at names the renamed channel",
        same(sidebar.channelSlots.map((s) => s.name), ["DAPI", "CD8", ""]),
        "left behind, it reads as an extra marker matching nothing");
    check("...and an empty slot stays empty",
        sidebar.channelSlots[2].name === "");
    check("a range the user tuned by hand follows its marker",
        same(sidebar.markerRangeOverrides.get("CD8"), [10, 200])
        && !sidebar.markerRangeOverrides.has("Channel_2"),
        "switching a slot away and back must not lose it");
    check("the option list is right before any slot is written back into it",
        synced.length === 3 && synced.every((s) => s.optionLists === 3),
        "syncSlotDom sets each select's value; a select whose options are "
        + "still the old names cannot show the new one");
}

{
    const ViewerSidebar = loadViewerSidebar();
    const sidebar = Object.create(ViewerSidebar.prototype);
    Object.assign(sidebar, {
        columns: ["CD4", "CD8"],
        channelSlots: [{ index: 0, name: "CD4" }, { index: 1, name: "CD8" }],
        markerRangeOverrides: new Map([["CD4", [1, 1]], ["CD8", [2, 2]]]),
        markerSelects: new Map(),
        syncSlotDom() {},
    });
    sidebar.renameChannels([
        { index: 0, fromShort: "CD4", fromFull: "CD4", to: "CD8" },
        { index: 1, fromShort: "CD8", fromFull: "CD8", to: "CD4" },
    ]);
    check("a swap here does not eat itself either",
        same(sidebar.markerRangeOverrides.get("CD8"), [1, 1])
        && same(sidebar.markerRangeOverrides.get("CD4"), [2, 2])
        && same(sidebar.channelSlots.map((s) => s.name), ["CD8", "CD4"]),
        JSON.stringify([...sidebar.markerRangeOverrides]));
}

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
