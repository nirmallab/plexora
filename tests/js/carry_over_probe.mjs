/**
 * What survives a walk to the next sample, and what must not.
 *
 * The rules this file fences are the ones that are invisible in review and
 * expensive to get wrong, because each of them fails QUIETLY:
 *
 *   - A snapshot is for ONE sample and ONE arrival. Applied to the wrong
 *     sample it silently rearranges a viewer somebody opened deliberately;
 *     applied twice it comes back after a reload that should have been fresh.
 *   - A contrast window must never be captured. It is a reading off one
 *     image's pixels, and carrying it makes every sample in a cohort look
 *     like whichever one you started from -- which reads as the data being
 *     similar rather than as the viewer lying.
 *   - Nothing may THROW its way out of a capture or a report. This runs on the
 *     way out of a page, and an exception there would take the navigation with
 *     it.
 *
 * Run against the shipped file in a stand-in small enough to read: a fake
 * sessionStorage that can be made to fail the way a private window does, and a
 * fake `__plexora` whose panels can be made absent or broken one at a time.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/carryOver.js");

let checks = 0;
/**
 * The same value, rebuilt in THIS realm.
 *
 * Everything the module returns was constructed inside the vm context, so it
 * carries that realm's Object.prototype -- and a strict deep-equal compares
 * prototypes, so two identical-looking objects come out unequal. Round-tripping
 * through JSON compares what the values ARE, which is the question here.
 */
function plain(value) {
    return JSON.parse(JSON.stringify(value));
}

function check(label, fn) {
    fn();
    checks += 1;
    console.log("  ok  " + label);
}

/**
 * The module, loaded fresh against a stand-in page.
 *
 * Fresh per check because it holds module state -- the consumed snapshot, what
 * each component reported -- and that state is the thing under test.
 *
 * @param store false for a browser whose sessionStorage throws on access,
 *   which is what a private window with site data blocked does.
 */
function boot({ store = true, plexora = null, toolLoader = null,
                sections = null, toast = null, errorShowing = false,
                datasource = "sampleA", elements = {} } = {}) {
    const items = new Map();
    const sessionStorage = {
        getItem: (k) => (items.has(k) ? items.get(k) : null),
        setItem: (k, v) => items.set(k, String(v)),
        removeItem: (k) => items.delete(k),
    };
    const shown = [];
    const context = {
        // Muted, not passed through: three of the checks below drive the
        // error paths on purpose, and their console.error output is the
        // module working correctly rather than anything to read.
        console: { log: () => {}, error: () => {}, warn: () => {} },
        Date,
        JSON,
        Math,
        Number,
        Array,
        Object,
        Set,
        String,
        document: {
            getElementById: (id) => elements[id] || null,
        },
    };
    context.window = context;
    Object.defineProperty(context, "sessionStorage", {
        get() {
            if (!store) throw new Error("access denied");
            return sessionStorage;
        },
    });
    context.flaskVariables = { datasource };
    context.__plexora = plexora;
    context.PlexoraToolLoader = toolLoader;
    context.PlexoraLayerSections = sections;
    context.PlexoraViewerError = { isShowing: () => errorShowing };
    context.PlexoraToast = toast || {
        show: (options) => {
            shown.push(options);
            return { dismiss() {} };
        },
    };
    createContext(context);
    runInContext(readFileSync(SOURCE, "utf8"), context);
    return { api: context.PlexoraCarryOver, items, shown, context };
}

/** A viewer with two channels on, one of them recoloured. */
function viewerWithChannels() {
    return {
        viewerSidebar: {
            channelSlots: [
                { enabled: true, name: "DAPI", colorHex: "#2388ff", range: [12, 900] },
                { enabled: true, name: "CD3", colorHex: "#ff00ff", range: [40, 2000] },
                { enabled: false, name: "KRT5", colorHex: "#00ff00", range: [0, 255] },
                { enabled: true, name: "", colorHex: "#ffffff", range: [0, 255] },
            ],
        },
        viewerControls: { mode: "outlines" },
        layers: {
            layers: () => [
                { id: "__image__", kind: "image", visible: true, opacity: 1, spec: {} },
                { id: "tx-9", kind: "points", visible: false, opacity: 0.6,
                  spec: { modality: "transcripts" } },
                { id: "__mask__", kind: "labels", visible: true, opacity: 1, spec: {} },
            ],
        },
        plugins: new Map(),
    };
}

console.log("carry-over");

// -- what capture takes ------------------------------------------------------

check("only the channels that are on, and only their names and colours", () => {
    const { api } = boot({ plexora: viewerWithChannels() });
    const { channels } = api.capture();
    assert.deepEqual(plain(channels.entries), [
        { name: "DAPI", color: "#2388ff" },
        { name: "CD3", color: "#ff00ff" },
    ]);
});

check("a contrast window is never captured", () => {
    const { api } = boot({ plexora: viewerWithChannels() });
    const { channels } = api.capture();
    // The rule this whole feature turns on: an arrangement travels, a
    // measurement does not.
    channels.entries.forEach((entry) => {
        assert.deepEqual(plain(Object.keys(entry).sort()), ["color", "name"]);
    });
});

check("HD comes off the checkbox, not off a guess", () => {
    const on = boot({
        plexora: viewerWithChannels(),
        elements: { viewer_controls_hd: { checked: true } },
    });
    assert.equal(on.api.capture().channels.hd, true);
    const off = boot({ plexora: viewerWithChannels() });
    assert.equal(off.api.capture().channels.hd, false);
});

check("the mask and the centroids are left out of the layers", () => {
    const { api } = boot({ plexora: viewerWithChannels() });
    const ids = api.capture().layers.map((layer) => layer.id);
    assert.deepEqual(plain(ids), ["__image__", "tx-9"]);
});

check("a layer carries what it IS, so a sibling can be recognised", () => {
    const { api } = boot({ plexora: viewerWithChannels() });
    const points = api.capture().layers.find((layer) => layer.id === "tx-9");
    assert.equal(points.modality, "transcripts");
    assert.equal(points.visible, false);
    assert.equal(points.opacity, 0.6);
});

check("a panel that throws costs the other components nothing", () => {
    const plexora = viewerWithChannels();
    Object.defineProperty(plexora, "viewerControls", {
        get() { throw new Error("mid-rebuild"); },
    });
    const { api } = boot({ plexora });
    const captured = api.capture();
    assert.equal(captured.cells, undefined);
    assert.equal(captured.channels.entries.length, 2, "channels still captured");
});

check("a plugin that throws in its own hook costs the tool list nothing", () => {
    const plexora = viewerWithChannels();
    plexora.plugins.set("gating", {
        sidebarController: {
            captureCarryState() { throw new Error("no"); },
        },
    });
    plexora.plugins.set("roi", {
        sidebarController: { captureCarryState: () => ({ mode: "pen" }) },
    });
    const { api } = boot({
        plexora,
        toolLoader: {
            snapshot: () => ({
                active: "gating", pair: null,
                loaded: [{ name: "gating" }, { name: "roi" }],
            }),
        },
    });
    const tools = api.capture().tools;
    assert.equal(tools.loaded[0].state, undefined, "the thrower contributes nothing");
    assert.deepEqual(plain(tools.loaded[1].state), { mode: "pen" });
});

check("a layer section's state is captured even though no tool holds it", () => {
    // Transcripts is mounted for any sample that has the data and toolLoader
    // has never heard of it, so a capture that only walked the open tools
    // would silently drop the whole panel.
    const plexora = viewerWithChannels();
    plexora.plugins.set("transcripts", {
        sidebarController: { captureCarryState: () => ({ selected: ["ACTB"] }) },
    });
    const { api } = boot({
        plexora,
        sections: { names: () => ["transcripts"] },
    });
    assert.deepEqual(plain(api.capture().sections), { transcripts: { selected: ["ACTB"] } });
});

// -- the hand-off ------------------------------------------------------------

check("a snapshot is taken by the sample it was meant for", () => {
    const { api, items } = boot({ plexora: viewerWithChannels() });
    api.stash("sampleB");
    assert.ok(items.has(api.KEY));
    const taken = api.take("sampleB");
    assert.ok(taken);
    assert.equal(taken.from, "sampleA");
});

check("and refused by any other sample", () => {
    const { api } = boot({ plexora: viewerWithChannels() });
    api.stash("sampleB");
    // Following a bookmark, or a link elsewhere, must not rearrange the sample
    // it lands on.
    assert.equal(api.take("sampleZ"), null);
});

check("taking consumes it, so a reload is a fresh open", () => {
    const { api, items } = boot({ plexora: viewerWithChannels() });
    api.stash("sampleB");
    assert.ok(api.take("sampleB"));
    assert.equal(items.has(api.KEY), false, "the entry is gone from storage");
    assert.equal(api.take("sampleB"), null, "a second take answers nothing");
});

check("a snapshot older than the walk it belongs to is refused", () => {
    const { api, items } = boot({ plexora: viewerWithChannels() });
    api.stash("sampleB");
    const stale = JSON.parse(items.get(api.KEY));
    stale.at = Date.now() - (api.MAX_AGE_MS + 1000);
    items.set(api.KEY, JSON.stringify(stale));
    assert.equal(api.take("sampleB"), null);
});

check("a snapshot from a previous build is dropped rather than half-read", () => {
    const { api, items } = boot({ plexora: viewerWithChannels() });
    api.stash("sampleB");
    const old = JSON.parse(items.get(api.KEY));
    old.version = api.VERSION - 1;
    items.set(api.KEY, JSON.stringify(old));
    assert.equal(api.take("sampleB"), null);
});

check("unparseable storage is nothing to apply, not an exception", () => {
    const { api, items } = boot({ plexora: viewerWithChannels() });
    items.set(api.KEY, "{not json");
    assert.equal(api.take("sampleB"), null);
});

check("a browser that refuses storage still navigates", () => {
    // A private window with site data blocked throws on the accessor itself.
    // Carrying state is a convenience; being able to move is not.
    const { api } = boot({ store: false, plexora: viewerWithChannels() });
    assert.equal(api.stash("sampleB"), false);
    assert.equal(api.take("sampleB"), null);
});

// -- what the user is told ---------------------------------------------------

check("nothing is said when everything applied", () => {
    const { api, shown } = boot({ plexora: viewerWithChannels() });
    api.stash("sampleB");
    api.take("sampleB");
    api.applied();
    api.flush();
    assert.equal(shown.length, 0, "silence is the right answer here");
});

check("one notice, however many components report", () => {
    const { api, shown } = boot({ plexora: viewerWithChannels() });
    api.stash("sampleB");
    api.take("sampleB");
    api.applied();
    api.report("channels", ["Channel SOX10 is not in this sample"]);
    api.report("gating", ["Thresholding: SOX10 is not a marker in this sample"]);
    api.report("layers", ["Layer transcripts is not in this sample"]);
    api.flush();
    assert.equal(shown.length, 1, "three reports, one notice");
    assert.equal(shown[0].lines.length, 3);
    assert.match(shown[0].title, /sampleA/, "it names where the state came from");
});

check("a second flush says nothing", () => {
    const { api, shown } = boot({ plexora: viewerWithChannels() });
    api.stash("sampleB");
    api.take("sampleB");
    api.applied();
    api.report("channels", ["gone"]);
    api.flush();
    api.flush();
    assert.equal(shown.length, 1);
});

check("the same line reported twice is said once", () => {
    const { api, shown } = boot({ plexora: viewerWithChannels() });
    api.stash("sampleB");
    api.take("sampleB");
    api.applied();
    api.report("a", ["Channel SOX10 is not in this sample"]);
    api.report("b", ["Channel SOX10 is not in this sample"]);
    api.flush();
    assert.deepEqual(plain(shown[0].lines), ["Channel SOX10 is not in this sample"]);
});

check("a long list is summarised rather than shown whole", () => {
    const { api, shown } = boot({ plexora: viewerWithChannels() });
    api.stash("sampleB");
    api.take("sampleB");
    api.applied();
    api.report("channels", Array.from({ length: 11 }, (_, i) => "line " + i));
    api.flush();
    assert.equal(shown[0].lines.length, 7, "six lines plus the count of the rest");
    assert.match(shown[0].lines[6], /5 more/);
});

check("nothing in common reads as a fresh open, not as a failure", () => {
    const { api, shown } = boot({ plexora: viewerWithChannels() });
    api.stash("sampleB");
    api.take("sampleB");
    // Reported, but nothing applied at all.
    api.report("channels", ["Channels not in this sample: DAPI, CD3"]);
    api.flush();
    assert.equal(shown.length, 1);
    assert.match(shown[0].title, /fresh/i);
    assert.match(shown[0].note, /no channels, layers or tool inputs in common/);
});

check("the notice stays quiet while the canvas is explaining itself", () => {
    // A note about one channel, over a card saying the image would not open,
    // is the smaller problem shouting over the larger.
    const { api, shown } = boot({
        plexora: viewerWithChannels(), errorShowing: true,
    });
    api.stash("sampleB");
    api.take("sampleB");
    api.report("channels", ["gone"]);
    api.flush();
    assert.equal(shown.length, 0);
});

check("a page nobody walked to is never told anything", () => {
    const { api, shown } = boot({ plexora: viewerWithChannels() });
    api.report("channels", ["gone"]);
    api.flush();
    assert.equal(shown.length, 0, "no snapshot was taken, so there is no walk to explain");
});

console.log(`\n${checks} checks passed`);
