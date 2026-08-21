/**
 * Does an ROI edit actually get saved, and what happens when it cannot be?
 *
 * RoiStore holds the working copy of a user's annotations and is the only thing
 * that will ever send them anywhere. Every way it can fail is silent by
 * construction: a queue quietly dropped, a stale write quietly accepted, a
 * retry that never fires. The panel goes on looking exactly right, because the
 * local copy -- the one being displayed -- is fine. The regions are simply not
 * anywhere else.
 *
 * That is the same shape of bug as the gating one that motivated
 * datalayer_globals_probe.mjs (a save that threw before it ever reached fetch,
 * behind a catch that logged), and it needs the same kind of check: not "did it
 * throw" but "what did it send, and what did it keep".
 *
 * The three answers that matter:
 *   accepted   -- the queue empties and the revision advances.
 *   conflict   -- the queue is KEPT and frozen. Replaying it over the other
 *                 session's work is the silent overwrite the revision exists
 *                 to prevent.
 *   unreachable-- the queue is KEPT and a retry is scheduled. Editing goes on.
 *
 * Run directly:  node tests/js/roi_state_probe.mjs
 *   --source <path>   probe a different roiState.js
 * Exit 0 = every check held. Exit 1 = at least one did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

const sourceArg = process.argv.indexOf("--source");
const SOURCE = sourceArg === -1
    ? join(REPO, "plexora/plugins/roi/static/roiState.js")
    : process.argv[sourceArg + 1];

const GEOMETRY = join(REPO, "plexora/plugins/roi/static/roiGeometry.js");

const TRIANGLE = { type: "Polygon", coordinates: [[[0, 0], [10, 0], [10, 10], [0, 0]]] };

/** Every request the store managed to send. */
const sent = [];

/** A scriptable stand-in for the server. */
class FakeApi {
    constructor() {
        this.revision = 0;
        this.mode = "ok";      // ok | conflict | offline | reject
    }

    async getState() {
        return {
            ok: true, status: 200, data: {
                success: true, schema_version: 1, revision: this.revision,
                image: "default", categories: [{
                    id: "uncategorized", label: "Uncategorized", color: "#8b93a6",
                    visible: true, locked: false, sort_order: 0,
                }],
                features: [], coordinate_space: { width: 100, height: 100 },
                image_size: [100, 100], stored_image_size: [100, 100],
                dimension_mismatch: false,
            },
        };
    }

    async postOperations(baseRevision, operations) {
        sent.push({ baseRevision, ops: operations.map((o) => o.op) });
        if (this.mode === "offline") throw new TypeError("Failed to fetch");
        if (this.mode === "conflict") {
            return { ok: false, status: 409, data: { success: false, error: "stale_revision", revision: 42 } };
        }
        if (this.mode === "reject") {
            return { ok: false, status: 400, data: { success: false, error: "unknown category 'c-9'" } };
        }
        this.revision = baseRevision + 1;
        return { ok: true, status: 200, data: { success: true, revision: this.revision } };
    }
}

const timers = [];
const context = {
    Math, Object, Array, Number, String, Boolean, JSON, Set, Map, Date, Promise,
    Error, TypeError, console, Uint8Array, Infinity,
    // Captured rather than run: the debounce is not what is under test here,
    // and a probe that waited on real timers would be slow and flaky.
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    Blob: class Blob { constructor(parts) { this.parts = parts; } },
    RoiApi: { saveBlob: () => sent.push({ kind: "download" }) },
    window: {
        crypto: { randomUUID: () => `id${timers.length}-${Math.random().toString(36).slice(2, 8)}` },
        PlexoraStatus: { begin: () => ({ done() {}, fail() {} }) },
    },
};
const ctx = createContext(context);
runInContext(readFileSync(GEOMETRY, "utf8"), ctx);
runInContext(readFileSync(SOURCE, "utf8") + "\n;globalThis.__Store = RoiStore;", ctx);

const checks = [];
const failures = [];

function check(name, actual, expected) {
    const a = JSON.stringify(actual);
    const e = JSON.stringify(expected);
    checks.push(name);
    if (a !== e) failures.push({ check: name, expected: e, actual: a });
}

function newStore(api) {
    return new ctx.__Store({ datasource: "probe" }, api);
}

function drawEntry(id) {
    return {
        label: "Draw ROI",
        redo: [{ op: "roi.create", image: "default", feature: { id, category_id: "uncategorized", geometry: TRIANGLE } }],
        undo: [{ op: "roi.delete", image: "default", id }],
    };
}

// -- accepted ------------------------------------------------------------

{
    sent.length = 0;
    const api = new FakeApi();
    const store = newStore(api);
    await store.load();

    store.commit(drawEntry("r-1"));
    check("an edit shows immediately, before any save", store.features.length, 1);
    check("...and is marked unsaved", store.status, "dirty");

    // Two more before the debounce would have elapsed.
    store.commit(drawEntry("r-2"));
    store.commit(drawEntry("r-3"));
    await store.flush();

    check("a burst of edits goes out as ONE request", sent.length, 1);
    check("...carrying all of them", sent[0].ops.length, 3);
    check("...against the revision the client last read", sent[0].baseRevision, 0);
    check("the queue empties once accepted", store.queue.length, 0);
    check("the revision advances", store.revision, 1);
    check("and the panel says so", store.status, "saved");
}

// -- undo ----------------------------------------------------------------

{
    sent.length = 0;
    const api = new FakeApi();
    const store = newStore(api);
    await store.load();

    store.commit(drawEntry("r-1"));
    await store.flush();
    store.undo();
    await store.flush();

    check("undo removes the shape locally", store.features.length, 0);
    // Undo is a new edit at a new revision, never a rewind: the conflict check
    // depends on the number only ever going forwards.
    check("undo sends a new operation", sent[1].ops, ["roi.delete"]);
    check("...at the revision the create produced", sent[1].baseRevision, 1);
    check("the revision went up, not back", store.revision, 2);

    store.redo();
    await store.flush();
    check("redo puts it back", store.features.length, 1);
    check("...and again moves forwards", store.revision, 3);
}

// -- conflict ------------------------------------------------------------

{
    sent.length = 0;
    const api = new FakeApi();
    api.mode = "conflict";
    const store = newStore(api);
    await store.load();

    store.commit(drawEntry("r-1"));
    await store.flush();

    check("a stale write is reported, not retried away", store.status, "conflict");
    // The whole point: replaying this queue would reinstate a stale world over
    // regions the other session just drew.
    check("the local work is KEPT", store.queue.length, 1);
    check("...and still on screen", store.features.length, 1);

    const before = sent.length;
    await store.flush();
    check("a conflicted store stops sending until the user chooses", sent.length, before);

    check("the user can export their version", store.editable, false);
}

// -- unreachable ---------------------------------------------------------

{
    sent.length = 0;
    timers.length = 0;
    const api = new FakeApi();
    api.mode = "offline";
    const store = newStore(api);
    await store.load();

    store.commit(drawEntry("r-1"));
    await store.flush();

    check("a save that never left keeps its operations", store.queue.length, 1);
    check("...and the shape stays on screen", store.features.length, 1);
    check("...and the panel does not claim it is saved", store.status, "dirty");
    check("a retry is scheduled", timers.some((t) => t.ms >= 2000), true);

    // Editing goes on while the server is away, and lands when it returns.
    store.commit(drawEntry("r-2"));
    check("more edits queue behind it", store.queue.length, 2);

    api.mode = "ok";
    await store.flush();
    check("everything lands once the server is back", store.queue.length, 0);
    check("...as one batch", sent[sent.length - 1].ops.length, 2);
    check("...and the panel catches up", store.status, "saved");
}

// -- refused -------------------------------------------------------------

{
    sent.length = 0;
    const api = new FakeApi();
    api.mode = "reject";
    const store = newStore(api);
    await store.load();

    store.commit(drawEntry("r-1"));
    await store.flush();

    // Retrying a rejected operation resends the same rejection forever, so the
    // store stops -- but never by throwing the geometry away.
    check("a rejected edit is reported", store.status, "failed");
    check("...with the server's reason", store.statusDetail, "unknown category 'c-9'");
    check("...and the geometry is still here", store.features.length, 1);
}

// -- export from local state ---------------------------------------------

{
    sent.length = 0;
    const api = new FakeApi();
    const store = newStore(api);
    await store.load();
    store.commit(drawEntry("r-1"));

    const document = store.toGeoJSON();
    check("the local export is a FeatureCollection", document.type, "FeatureCollection");
    check("...carrying the unsaved region", document.features.length, 1);
    check("...and says what its coordinates mean",
        document.plexora.coordinate_space, { width: 100, height: 100 });
}

const report = {
    source: SOURCE.replace(REPO + "/", ""),
    checked: checks.length,
    failures,
};

console.error(JSON.stringify(report, null, 2));
process.exit(failures.length ? 1 : 0);
