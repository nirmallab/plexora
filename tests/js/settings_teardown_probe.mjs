/**
 * Leaving the Settings page stops what it was polling.
 *
 * The page is a routed fragment: leaving it runs the teardown its
 * PlexoraPage.register callback returns, and the page's timers would
 * otherwise go on asking the server on behalf of a panel nobody sees.
 *
 *   1. **A migration tick in flight** when the page goes must not schedule
 *      the next one when its answer lands (clearing the timer is not enough:
 *      there is no timer while the request is out).
 *   2. **The licence device-code poll** stops with the page.
 *
 * Run directly:  node tests/js/settings_teardown_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/views/settingsPage.js");

const failures = [];
function check(label, ok, detail = "") {
    console.log(`${ok ? "ok" : "FAIL"} - ${label}${ok || !detail ? "" : `  ${detail}`}`);
    if (!ok) failures.push(label);
}

function makeElement(id) {
    const listeners = new Map();
    return {
        id, hidden: false, disabled: false, value: "", textContent: "", href: "",
        checked: false, dataset: {}, style: {}, children: [],
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        setAttribute() {}, appendChild(c) { return c; }, querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(fn);
        },
        removeEventListener() {},
        click() { (listeners.get("click") || []).forEach((fn) => fn({ type: "click", preventDefault() {} })); },
    };
}

const PRESENT = new Set(["settings_panel_data", "settings_panel_license", "settings_license_connect",
    "settings_license_connect_cancel", "settings_license_code", "settings_license_verify",
    "settings_license_waiting", "settings_license_connect_box", "settings_progress"]);
const elements = new Map();
const elementFor = (id) => {
    if (!PRESENT.has(id)) return null;
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
};

// -- a clock this probe owns: every timer the page sets, by callback --------------
let nextId = 1;
const timers = new Map();
const fakeSetTimeout = (fn, ms) => { const id = nextId++; timers.set(id, { fn, ms }); return id; };
const fakeClearTimeout = (id) => { timers.delete(id); };

// -- the server ----------------------------------------------------------------------
const held = [];      // migration answers waiting to be released
let migrationReads = 0;
const polls = [];
function reply(body) {
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}
function respond(url, options) {
    const path = String(url);
    if (path.endsWith("settings/data/migration")) {
        migrationReads += 1;
        if (migrationReads === 1) return reply({ status: "running" });
        return new Promise((resolve) => held.push(() => resolve({
            ok: true, status: 200, json: () => Promise.resolve({ status: "running" }) })));
    }
    if (path.endsWith("settings/license/connect")) {
        return reply({ ok: true, code: "ABCD", verify_url: "https://example.org/v", interval: 5 });
    }
    if (path.endsWith("settings/license/connect/poll")) {
        polls.push(options);
        return reply({ ok: true, pending: true });
    }
    return new Promise(() => {});
}

const context = {
    console, Promise, JSON, Object, Array, Boolean, String, Number, Math, Date, Map, Set,
    encodeURIComponent, URL,
    setTimeout: fakeSetTimeout, clearTimeout: fakeClearTimeout,
    fetch: respond,
    open: () => null,
    plexoraUrl: (path) => `/${String(path).replace(/^\/+/, "")}`,
    location: { hash: "", href: "http://x/settings" },
    document: {
        createElement: makeElement,
        getElementById: elementFor,
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: () => {},
        removeEventListener: () => {},
        visibilityState: "visible",
    },
};
context.window = context;
context.addEventListener = () => {};
context.removeEventListener = () => {};
context.PlexoraPage = { register: (fn) => { context.pageInit = fn; } };
createContext(context);
runInContext(readFileSync(SOURCE, "utf8"), context, { filename: "settingsPage.js" });

const settle = async () => { for (let i = 0; i < 10; i += 1) await new Promise((r) => setImmediate(r)); };
const fire = (id) => { const t = timers.get(id); timers.delete(id); t.fn(); };
const timersOf = (ms) => [...timers.entries()].filter(([, t]) => t.ms === ms).map(([id]) => id);

const teardown = context.pageInit();
await settle();

// -- 1. the migration tick ------------------------------------------------------------
// start() found a migration running and watch() asked straight away: that
// tick's answer is held, so there is no timer for teardown to clear.
check("a running migration is being watched", held.length === 1, String(held.length));

// -- 2. the licence poll ----------------------------------------------------------------
elementFor("settings_license_connect").click();
await settle();
const pollTimers = timersOf(5000);
check("connecting schedules a device-code poll", pollTimers.length === 1, JSON.stringify([...timers.values()].map((t) => t.ms)));

teardown();

held.forEach((release) => release());
await settle();
const others = [...timers.keys()].filter((id) => !pollTimers.includes(id));
check("a migration tick that lands after the page left schedules no other",
    others.length === 0, JSON.stringify(others.map((id) => timers.get(id).ms)));
check("...and asks the server nothing more", migrationReads === 2, String(migrationReads));

check("the licence poll's timer is gone with the page",
    pollTimers.every((id) => !timers.has(id)));
// Even fired (a timer already running when it was cleared), it asks nothing.
const before = polls.length;
for (const id of pollTimers) {
    if (timers.has(id)) fire(id);
}
await settle();
check("...and it asks the server nothing", polls.length === before, String(polls.length - before));
check("nothing at all is left scheduled", timers.size === 0,
    JSON.stringify([...timers.values()].map((t) => t.ms)));

console.log(failures.length ? `\nFAILURES: ${failures.join(", ")}` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
