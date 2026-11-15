/**
 * Video Studio: pressing Play never swaps the user's own video for a fresh
 * preset build.
 *
 *     node tests/js/video_play_keeps_document_probe.mjs
 *
 * Loads the real videoStudioSidebarController.js in a vm context with the
 * bar, the preview, the scene capture and the server API faked. Play, while no
 * preview is showing, re-captures the viewer as the starting scene and, when
 * the viewer moved, rebuilds the video from its preset ("the starting scene
 * follows the viewer"). That is right for a preset build nobody has edited;
 * for a saved video just opened, or one edited in the camera path editor or
 * the timeline (where moving the viewer is how a camera position is set), it
 * silently replaced the document, and the next save overwrote the stored one.
 *
 * What it holds:
 *   - after opening a saved video, moving the viewer and pressing Play builds
 *     nothing, keeps the opened document and plays it;
 *   - after editing a preset build (path editor), moving the viewer and
 *     pressing Play builds nothing, keeps the edited document and plays it;
 *   - an unedited preset build still follows the viewer: moving it and
 *     pressing Play rebuilds from the new view and plays.
 *
 * Reports {checked, failures} as JSON on stderr.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/video_studio/static");

const failures = [];
let checked = 0;
function check(name, condition, detail) {
    checked += 1;
    if (!condition) failures.push(detail === undefined ? { name } : { name, detail });
}

// -- fakes -------------------------------------------------------------------------------

const noop = () => {};
/** Any method is a no-op; `fields`/`root` are plain objects. */
function lenient(extra = {}) {
    return new Proxy({ fields: {}, root: null, ...extra }, {
        get: (target, key) => (key in target ? target[key] : noop),
    });
}

function scene(x) {
    return { view: { mode: "2d", z: 0, t: 0 }, viewport: { x, y: 0.5, zoom: 2 },
             camera3d: null, channels: [] };
}

const world = { viewer: scene(0.1), calls: [] };

function presetDocument(base) {
    return { title: "Zoom", duration_s: 4, base, output: { width: 1920, height: 1080 },
             tracks: [{ track_id: "cam", kind: "camera2d", mode: "procedural", range: [0, 4] }],
             provenance: { preset: { name: "zoom", params: {} } }, notes: [] };
}

const savedDocument = {
    video_id: "v-saved", revision: 7, title: "My saved video", duration_s: 6, base: scene(0.9),
    output: { width: 1920, height: 1080 },
    tracks: [{ track_id: "cam", kind: "camera2d", mode: "keyframes",
               keyframes: [{ kf_id: "a", t: 0 }, { kf_id: "b", t: 3 }, { kf_id: "c", t: 6 }] }],
    provenance: { preset: { name: "zoom", params: {} } },
};

const api = {
    async state() { return { presets: [{ name: "zoom", available: true, fields: [] }], groups: [], videos: [] }; },
    async build({ scene: base }) {
        world.calls.push("build");
        return { document: presetDocument(base), warnings: [] };
    },
    async get(id) {
        world.calls.push("get");
        return { document: JSON.parse(JSON.stringify({ ...savedDocument, video_id: id })) };
    },
    async create({ document }) {
        world.calls.push("create");
        return { video_id: "v-new", document: { ...document, revision: 1 } };
    },
    async replace(id, revision) { world.calls.push("replace"); return { revision: revision + 1 }; },
    async ops(id, revision) {
        world.calls.push("ops");
        const edited = { ...presetDocument(scene(0.1)), title: "Edited", video_id: id, revision: revision + 1,
                         tracks: [{ track_id: "cam", kind: "camera2d", mode: "keyframes",
                                    keyframes: [{ kf_id: "a", t: 0 }, { kf_id: "n", t: 2 }] }] };
        return { document: edited, revision: revision + 1, inverse: [] };
    },
    async list() { return { videos: [] }; },
};

function makePreview() {
    return {
        playing: false, active: false, time: 0, plays: 0,
        play() { this.playing = true; this.active = true; this.plays += 1; },
        pause() { this.playing = false; },
        async end() { this.playing = false; this.active = false; },
        seek(t) { this.time = t; this.active = true; },
        invalidate: noop, rescale: noop,
    };
}

const context = {
    console,
    setTimeout, clearTimeout, Promise, AbortController,
    localStorage: { getItem: () => null, setItem: noop },
    PlexoraVideoProjectApi: api,
    PlexoraVideoStudioBar: function () { return lenient(); },
    PlexoraVideoPreview: { create: () => makePreview() },
    PlexoraVideoHistory: { create: () => lenient() },
    PlexoraVideoParamFields: { render: () => lenient() },
    PlexoraVideoSceneState: { capture: () => JSON.parse(JSON.stringify(world.viewer)) },
    PlexoraVideoAnimation: { evaluate: () => ({}) },
};
context.window = context;
createContext(context);
runInContext(readFileSync(join(STATIC, "videoStudioSidebarController.js"), "utf-8"), context,
             { filename: "videoStudioSidebarController.js" });
const Controller = context.PlexoraVideoStudioController;

async function settle() {
    for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve));
}

async function fresh() {
    world.viewer = scene(0.1);
    world.calls = [];
    const c = new Controller({ onCleanup: noop, datasource: "ds" });
    c.state = await api.state();
    c.presetName = "zoom";
    c.baseScene = context.PlexoraVideoSceneState.capture();
    await c.rebuild({ play: false });
    world.calls = [];
    return c;
}

// -- 1. a saved video, just opened -------------------------------------------------------

{
    const c = await fresh();
    await c.loadVideo("v-saved");
    world.calls = [];
    world.viewer = scene(0.3);             // the user looks somewhere else, then plays
    c.togglePlay();
    await settle();
    check("Play after opening a saved video builds nothing", !world.calls.includes("build"),
          { calls: world.calls });
    check("…keeps the opened document", c.project?.title === "My saved video"
          && c.project?.tracks?.[0]?.keyframes?.length === 3, { title: c.project?.title });
    check("…and plays it", c.preview.playing && c.preview.plays === 1);
}

// -- 2. a preset build edited in the path editor ----------------------------------------

{
    const c = await fresh();
    await c.edit([{ op: "add_keyframe" }], "Add camera position");
    world.calls = [];
    world.viewer = scene(0.6);             // moved to set the next camera position
    c.togglePlay();
    await settle();
    check("Play after a path edit and a viewer move builds nothing", !world.calls.includes("build"),
          { calls: world.calls });
    check("…keeps the edited document", c.project?.title === "Edited", { title: c.project?.title });
    check("…and plays it", c.preview.playing && c.preview.plays === 1);
}

// -- 3. an unedited preset build still follows the viewer --------------------------------

{
    const c = await fresh();
    world.viewer = scene(0.7);
    c.togglePlay();
    await settle();
    check("Play on an unedited preset build after a viewer move rebuilds",
          world.calls.filter((x) => x === "build").length === 1, { calls: world.calls });
    check("…from the new view", Math.abs((c.project?.base?.viewport?.x ?? 0) - 0.7) < 1e-9,
          { base: c.project?.base });
    check("…and plays it", c.preview.playing && c.preview.plays === 1);
}

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.exit(failures.length ? 1 : 0);
