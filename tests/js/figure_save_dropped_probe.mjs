/**
 * A figure save that never reached the server is not forgotten by the next one.
 *
 * FigureDocumentState applies an edit to the tab's copy at once and sends the
 * same edit as a PATCH carrying only that commit's operations. When the request
 * itself fails -- the server restarting, the website's Python worker stalling,
 * a relay blip -- `fetch` rejects and the status becomes "failed" ("Your
 * changes are still here"). The tab keeps the edit.
 *
 * The next edit's PATCH carries only ITS operations, against the last
 * confirmed revision. When that one is stored, the status turns "Saved" -- yet
 * the server never received the first edit. It exists only in the tab: gone on
 * reload, missing from an export, and a later edit that depends on it is
 * refused.
 *
 * This runs the real FigureBuilderApi and FigureDocumentState against a stubbed
 * `fetch` that keeps a server copy of the figure and can drop one request. The
 * invariant checked: the status never says "saved" while the server's copy is
 * missing an edit the tab still shows.
 *
 * Run directly:
 *   node tests/js/figure_save_dropped_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/figure_builder/static");

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

/** A figure server as `fetch` sees it: one figure, PATCH applies add_panel and
 *  move_panels, refuses a move of a panel it does not hold, and answers 409 to
 *  a stale base revision. `dropNext` makes the next request fail the way a
 *  dead connection does. */
function makeServer() {
    const server = {
        revision: 7,
        panels: {},
        dropNext: 0,
        requests: 0,
    };
    server.fetch = async (url, init) => {
        server.requests += 1;
        if (server.dropNext > 0) {
            server.dropNext -= 1;
            throw new TypeError("Failed to fetch");
        }
        const reply = (status, data) => ({
            ok: status >= 200 && status < 300, status: status, json: async () => data,
        });
        const method = (init && init.method) || "GET";
        if (method !== "PATCH") return reply(405, { error: "unexpected " + method });
        const body = JSON.parse(init.body);
        if (body.base_revision !== server.revision) {
            return reply(409, { error: "conflict", revision: server.revision });
        }
        const next = JSON.parse(JSON.stringify(server.panels));
        for (const op of body.operations) {
            if (op.op === "add_panel") {
                next[op.panel.id] = { id: op.panel.id, x: op.panel.x || 0 };
            } else if (op.op === "move_panels") {
                for (const move of op.moves) {
                    if (!next[move.panel_id]) {
                        return reply(400, { error: `no panel ${move.panel_id}` });
                    }
                    next[move.panel_id].x = move.x;
                }
            } else {
                return reply(400, { error: "unknown operation " + op.op });
            }
        }
        server.panels = next;
        server.revision += 1;
        return reply(200, { revision: server.revision });
    };
    return server;
}

function makeState(server) {
    const ctx = createContext({
        console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
        Promise, Error, TypeError, encodeURIComponent,
        fetch: (url, init) => server.fetch(url, init),
        window: {},
    });
    for (const name of ["figureBuilderApi.js", "figureDocumentState.js"]) {
        runInContext(readFileSync(join(STATIC, name), "utf8"), ctx, { filename: name });
    }
    ctx.__server = server;
    runInContext(`
        globalThis.__state = new FigureDocumentState({
            figureId: "fig_probe",
            api: new FigureBuilderApi({ url: (path) => "/" + path }),
        });
        __state.document = { revision: 7, annotations: {}, pages: {}, panels: {},
                             sources: {}, settings: {} };
        __state._setStatus("saved");
    `, ctx);
    return ctx.__state;
}

function addPanel(state, id) {
    return state.commit([{ op: "add_panel", panel: { id: id, x: 0 } }], (draft) => {
        draft.panels[id] = { id: id, x: 0 };
    });
}

function movePanel(state, id, x) {
    return state.commit([{ op: "move_panels", moves: [{ panel_id: id, x: x }] }], (draft) => {
        if (draft.panels[id]) draft.panels[id].x = x;
    });
}

const ids = (panels) => Object.keys(panels).sort().join(",");

/** "saved" is only true when every panel the tab shows is on the server. */
function savedIsTrue(state, server) {
    if (state.status !== "saved") return true;
    return ids(state.document.panels) === ids(server.panels);
}

const describe = (state, server) =>
    `(status="${state.status}", tab panels [${ids(state.document.panels)}], `
    + `server panels [${ids(server.panels)}])`;

// -- control: nothing dropped ------------------------------------------------
{
    const server = makeServer();
    const state = makeState(server);
    await addPanel(state, "A");
    await addPanel(state, "B");
    check("two stored saves report Saved and the server holds both panels",
          state.status === "saved" && ids(server.panels) === "A,B",
          describe(state, server));
}

// -- a dropped save, then one that is stored ----------------------------------
{
    const server = makeServer();
    const state = makeState(server);
    server.dropNext = 1;
    const first = await addPanel(state, "A");
    const afterDrop = state.status;
    await addPanel(state, "B");
    check("a dropped save is reported as not stored",
          first === false && afterDrop === "failed",
          `(commit returned ${first}, status "${afterDrop}")`);
    check("after a dropped save and a stored one, the status does not say Saved "
          + "while the server is missing the dropped edit",
          savedIsTrue(state, server), describe(state, server));
}

// -- a dropped save, a dependent edit, then an unrelated stored one -----------
{
    const server = makeServer();
    const state = makeState(server);
    server.dropNext = 1;
    await addPanel(state, "A");
    await movePanel(state, "A", 10);
    await addPanel(state, "C");
    check("a dropped add, a move of that panel and a later stored edit do not end "
          + "in Saved while the server is missing the dropped panel",
          savedIsTrue(state, server), describe(state, server));
}

if (failures.length) {
    console.log(`${failures.length} check(s) failed`);
    process.exit(1);
}
console.log("all checks passed");
