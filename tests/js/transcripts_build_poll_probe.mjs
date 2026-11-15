/**
 * The Transcripts panel's build poll must end when the server says `missing`.
 *
 * `/plugins/transcripts/status` answers `missing` when no job exists and no
 * store does either -- a build the server refused (no source on the layer).
 * The poll used to treat that as "keep waiting" and spin forever under
 * "Preparing transcripts", where the Visium panel gives up after three misses.
 *
 * And a manifest that could not be READ (null: a broken or briefly unreachable
 * server) is not a layer that needs building. It used to POST /build, which
 * re-reads and re-tiles the whole parquet; now it only polls, and the poll's
 * `ready` re-attaches.
 *
 * Run directly: node tests/js/transcripts_build_poll_probe.mjs
 * Exit 0 = the poll stops and the label says why.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const FILE = join(REPO, "plexora/plugins/transcripts/static/transcriptsSidebarController.js");

const label = { textContent: "Preparing transcripts…" };
let tick = null;
let cleared = 0;
const context = createContext({
    console, Object, Array, Number, String, Map, Set, Promise, JSON, Math,
    TranscriptLayer: class {
        constructor() { this.state = {}; this.manifest = null; }
        async attach() { return null; }
        destroy() {}
    },
    setInterval: (fn) => { tick = fn; return 7; },
    clearInterval: () => { cleared += 1; },
    document: { getElementById: (id) => (id === "transcripts_building_label" ? label : null) },
});
runInContext(readFileSync(FILE, "utf8"), context, { filename: FILE });
const Controller = runInContext("TranscriptsSidebarController", context);

const failures = [];
const controller = Object.create(Controller.prototype);
controller.poll = null;
controller.api = { status: async () => ({ status: "missing" }) };
controller.start = async () => failures.push("start() ran on a missing layer");
controller.watchBuild("transcripts");
for (let i = 0; i < 5 && controller.poll; i += 1) await tick();

if (controller.poll !== null) failures.push("the poll is still running after 5 `missing` answers");
if (!cleared) failures.push("clearInterval was never called");
if (/Preparing/.test(label.textContent)) failures.push(`label still reads "${label.textContent}"`);

// A null manifest: poll, do not build.
const second = Object.create(Controller.prototype);
let built = 0;
let watched = 0;
second.poll = null;
second.saved = null;
second.ctx = {};
second.api = { build: async () => { built += 1; } };
second.bindStaticControls = () => {};
second.firstTranscriptLayer = () => "transcripts";
second.show = () => {};
second.watchBuild = () => { watched += 1; };
await second.start();
if (built) failures.push("an unreadable manifest started a full rebuild");
if (!watched) failures.push("an unreadable manifest was not polled");

if (failures.length) {
    console.log(failures.join("\n"));
    process.exit(1);
}
console.log("ok");
