/**
 * Video Studio: edits and first saves run one at a time, and closing the
 * tool stops a recording.
 *
 *     node tests/js/video_edit_serial_probe.mjs
 *
 * Loads the real videoStudioSidebarController.js with the bar, preview and
 * server faked (the server answers 409 to a stale revision, as the real one
 * does).
 *   - Two quick edits on a never-saved video used to both see "no video id"
 *     and POST /api/videos twice: two saved videos. And two quick edits on a
 *     saved one sent the same revision twice: the second was a 409 nobody saw.
 *   - An exploration recording left running when the tool closed kept its
 *     window listener and its 100 ms interval, and its samples grew forever.
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

const noop = () => {};
function lenient(extra = {}) {
    return new Proxy({ fields: {}, root: null, ...extra }, {
        get: (target, key) => (key in target ? target[key] : noop),
    });
}
const tick = () => new Promise((resolve) => setImmediate(resolve));

const server = { creates: 0, revision: 0, conflicts: 0, applied: 0 };
const api = {
    async state() { return { presets: [], groups: [], videos: [] }; },
    async create({ document }) {
        await tick(); await tick();
        server.creates += 1;
        server.revision = 1;
        return { video_id: `v${server.creates}`, document: { ...document, revision: 1 } };
    },
    async replace(id, revision) {
        await tick();
        if (revision !== server.revision) { server.conflicts += 1; throw Object.assign(new Error("conflict"), { status: 409 }); }
        server.revision += 1;
        return { revision: server.revision };
    },
    async ops(id, revision, operations) {
        await tick(); await tick();
        if (revision !== server.revision) { server.conflicts += 1; throw Object.assign(new Error("conflict"), { status: 409 }); }
        server.revision += 1;
        server.applied += operations.length;
        return { document: { title: "v", duration_s: 4, tracks: [], base: {}, revision: server.revision },
                 revision: server.revision, inverse: [] };
    },
    async list() { return { videos: [] }; },
};

const preview = { active: false, playing: false, time: 0, async end() {}, pause: noop,
                  invalidate: noop, rescale: noop, seek: noop };
const context = {
    console, setTimeout, clearTimeout, Promise, AbortController,
    localStorage: { getItem: () => null, setItem: noop },
    addEventListener: noop, removeEventListener: noop,
    PlexoraVideoProjectApi: api,
    PlexoraVideoStudioBar: function () { return lenient(); },
    PlexoraVideoPreview: { create: () => preview },
    PlexoraVideoHistory: { create: () => lenient() },
    PlexoraVideoParamFields: { render: () => lenient() },
    PlexoraVideoSceneState: { capture: () => ({ view: {}, viewport: null }) },
    PlexoraVideoAnimation: { evaluate: () => ({}) },
};
context.window = context;
createContext(context);
runInContext(readFileSync(join(STATIC, "videoStudioSidebarController.js"), "utf-8"), context,
             { filename: "videoStudioSidebarController.js" });
const Controller = context.PlexoraVideoStudioController;

// -- 1. two quick edits on a video never saved -----------------------------------------
{
    const c = new Controller({ onCleanup: noop, datasource: "ds" });
    c.state = await api.state();
    c.project = { title: "Zoom", duration_s: 4, tracks: [], base: {}, output: {} };
    const results = await Promise.allSettled([
        c.edit([{ op: "a" }], "A"), c.edit([{ op: "b" }], "B")]);
    check("two quick edits on an unsaved video create one saved video",
          server.creates === 1, { creates: server.creates });
    check("…and both edits land", server.applied === 2 && results.every((r) => r.status === "fulfilled"),
          { applied: server.applied, results: results.map((r) => r.status) });
    check("…with no 409", server.conflicts === 0, { conflicts: server.conflicts });
}

// -- 2. a recording running when the tool closes ---------------------------------------
{
    const c = new Controller({ onCleanup: noop, datasource: "ds" });
    let stopped = 0;
    c.recorder = { get active() { return stopped === 0; }, stop() { stopped += 1; return null; } };
    c.destroy();
    check("closing the tool stops an active recording", stopped === 1, { stopped });
}

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.exit(failures.length ? 1 : 0);
