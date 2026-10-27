/**
 * Settings > Usage data: a refused registration says so.
 *
 * An install the telemetry service will not register has no token, so
 * nothing it queues ever leaves the machine. It used to read exactly like a
 * new install -- "Nothing sent yet." -- so three installs could sit unseen for
 * a day. The section now carries the uploader's own sentence for it:
 *
 *   1. **A refusal is shown on load**, with the service's status and reason.
 *   2. **Send now says why nothing went**, not just a bare word.
 *   3. **A healthy install says when it last sent**, and nothing else.
 *   4. **No service configured means no complaint** about one.
 *
 * Run directly:  node tests/js/settings_telemetry_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/views/settingsPage.js");

// -- a DOM small enough to read ---------------------------------------------

function makeElement(tag) {
    let own = "";
    const listeners = new Map();
    const element = {
        tagName: String(tag).toUpperCase(),
        get textContent() { return own; },
        set textContent(value) {
            own = value === null || value === undefined ? "" : String(value);
        },
        hidden: false,
        disabled: false,
        children: [],
        dataset: {},
        style: {},
        className: "",
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        setAttribute() {},
        appendChild(child) {
            element.children.push(child);
            return child;
        },
        addEventListener(type, handler) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(handler);
        },
        removeEventListener() {},
        click() { (listeners.get("click") || []).forEach((h) => h({ type: "click" })); },
    };
    return element;
}

// -- the page's own elements, by id -----------------------------------------
//
// Only Usage data's ids resolve (and the data panel, which is what tells the
// page it is the Settings page at all). Every other section finds no markup
// and does not start.

const byId = new Map();

function elementFor(id) {
    if (id !== "settings_panel_data" && !String(id).startsWith("settings_telemetry")
            && id !== "settings_panel_telemetry") return null;
    if (!byId.has(id)) byId.set(id, makeElement(id === "settings_telemetry_send" ? "button" : "div"));
    return byId.get(id);
}

// -- the server --------------------------------------------------------------

const REFUSED = "The telemetry service refused to register this install "
    + "(HTTP 400: The client block does not match the schema.). "
    + "Updating Plexora, or redeploying the service, fixes this.";

function status(overrides = {}, uploader = {}) {
    return {
        mode: "anonymous", source: "default", ceiling: null, enabled: true,
        endpoint_configured: true, install_id: "0123456789abcdef0123456789abcdef",
        pending: { counter_rows: 4, records: 0, batches_waiting: 1 },
        uploader: { last_upload: null, last_outcome: "unregistered", registered: false,
                    register_status: 400, register_error: "The client block does not match the schema.",
                    problem: REFUSED, ...uploader },
        ...overrides,
    };
}

let onStatus = status();
let onSend = { outcome: "unregistered", status: status() };

function reply(body) {
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}

function respond(url) {
    const path = String(url);
    if (path.endsWith("telemetry/status")) return reply(onStatus);
    if (path.endsWith("telemetry/send")) return reply(onSend);
    // The Data section's reads. Never answered, so nothing it would draw
    // from them can throw from inside a promise nobody is holding.
    return new Promise(() => {});
}

// -- load the shipped file --------------------------------------------------

const context = {
    console, setTimeout, clearTimeout, Promise, JSON, Object, Array, Boolean,
    String, Number, Math, Date, encodeURIComponent,
    fetch: respond,
    plexoraUrl: (path) => `/${String(path).replace(/^\/+/, "")}`,
    document: {
        createElement: makeElement,
        getElementById: elementFor,
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: () => {},
    },
};
context.window = context;
context.PlexoraPage = { register: (fn) => { context.pageInit = fn; } };

createContext(context);
runInContext(readFileSync(SOURCE, "utf-8"), context);

// -- checks -----------------------------------------------------------------

const failures = [];

function check(label, condition) {
    if (condition) {
        console.log(`ok  ${label}`);
    } else {
        failures.push(label);
        console.log(`FAIL ${label}`);
    }
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const meta = () => elementFor("settings_telemetry_meta").textContent;
const send = () => elementFor("settings_telemetry_send");

async function main() {
    context.pageInit();
    await settle();

    // -- 1. on load ---------------------------------------------------------------
    check("a refused registration is shown under the install id",
          meta().includes("refused to register this install (HTTP 400: "
                          + "The client block does not match the schema.)"));

    // -- 2. Send now ------------------------------------------------------------------
    send().click();
    await settle();
    check("Send now says the install is not registered, and why",
          meta().includes("Not sent: this install is not registered")
          && meta().includes("refused to register"));
    check("...and gives the button back",
          send().disabled === false);

    // -- 3. healthy ---------------------------------------------------------------------
    const sentAt = Date.UTC(2026, 9, 27, 12, 0) / 1000;
    onSend = { outcome: "sent", status: status({}, {
        last_upload: sentAt, last_outcome: "sent", registered: true,
        register_status: null, register_error: null, problem: null }) };
    send().click();
    await settle();
    check("a healthy install says when it last sent, and nothing about a refusal",
          meta().startsWith("Last sent ") && meta().includes("Sent.")
          && !meta().includes("refused") && !meta().includes("not registered"));

    // -- 4. no service ---------------------------------------------------------------
    onSend = { outcome: "no_endpoint", status: status({ endpoint_configured: false }) };
    send().click();
    await settle();
    check("with no telemetry service configured, there is no refusal to report",
          meta().startsWith("No telemetry service is configured")
          && !meta().includes("refused") && send().hidden === true);

    console.log(failures.length
        ? `\n${failures.length} failed`
        : "\nall settings-telemetry checks passed");
    if (failures.length) process.exitCode = 1;
}

main();
