/**
 * Settings > License > Connect this device, run in node against the shipped
 * settingsPage.js: the button asks for a code, shows it with the portal link,
 * polls until the person approves, and draws the licence that comes back; a
 * declined code says so; Cancel stops polling.
 *
 * Run directly:  node tests/js/license_connect_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (path) => readFileSync(join(REPO, path), "utf8");

async function check(label, fn) {
    await fn();
    console.log("  ok  " + label);
}

/** A DOM element just real enough for settingsPage.js. */
function element(id) {
    const listeners = {};
    return {
        id, hidden: false, disabled: false, value: "", checked: false, textContent: "", href: "",
        dataset: {}, style: {}, children: [], attributes: {},
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
        removeEventListener() {},
        async fire(type, event = {}) { for (const fn of listeners[type] || []) await fn(event); },
        setAttribute(name, value) { this.attributes[name] = String(value); },
        getAttribute(name) { return this.attributes[name] ?? null; },
        removeAttribute(name) { delete this.attributes[name]; },
        querySelector: () => null, querySelectorAll: () => [],
        replaceChildren(...nodes) { this.children = nodes; },
        append(...nodes) { this.children.push(...nodes); },
        appendChild(node) { this.children.push(node); return node; },
        closest: () => null, focus() {}, blur() {}, select() {},
    };
}

function sandbox(routes) {
    const elements = new Map();
    const posted = [];
    const opened = [];
    const timers = [];
    const byId = (id) => {
        if (!elements.has(id)) elements.set(id, element(id));
        return elements.get(id);
    };
    const ctx = {
        console, Promise, Object, Array, Map, Set, JSON, String, Number, Boolean, Date, Error, Math,
        RegExp, URL, URLSearchParams,
        setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
        clearTimeout: (n) => { if (timers[n - 1]) timers[n - 1].fn = null; },
        setInterval: () => 0, clearInterval() {},
        plexoraUrl: (path) => `/base/${path}`,
        fetch: async (url, init = {}) => {
            const path = String(url).replace(/^\/base\//, "");
            const body = init.body ? JSON.parse(init.body) : null;
            if (init.method === "POST") posted.push({ path, body });
            const answer = routes(path, body) || { status: 404, body: { error: "no route" } };
            return { status: answer.status || 200, ok: (answer.status || 200) < 400,
                     json: async () => answer.body };
        },
        document: {
            getElementById: (id) => (id === "settings_rail" ? null : byId(id)),
            querySelector: () => null,
            querySelectorAll: () => [],
            createElement: (tag) => element(tag),
            addEventListener() {}, removeEventListener() {},
            visibilityState: "visible", hidden: false,
        },
        location: { hash: "", href: "/base/settings", pathname: "/base/settings" },
        history: { replaceState() {} },
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        open: (url) => opened.push(url),
        addEventListener() {}, removeEventListener() {},
        PlexoraConfirm: { ask: async () => true, tell: async () => true, choose: async () => null },
        PlexoraPaid: { adopt() {}, forget() {} },
        PlexoraRemotes: { watch: () => ({ stop() {} }), subscribe: () => () => {} },
    };
    let registered = null;
    ctx.PlexoraPage = { register: (fn) => { registered = fn; } };
    ctx.window = ctx;
    createContext(ctx);
    runInContext(read("plexora/client/src/js/views/settingsPage.js"), ctx);
    // Only the licence panel is on this page: no other section starts.
    for (const id of ["settings_panel_nodes", "settings_panel_remotes", "settings_panel_webdata",
                      "settings_panel_updates", "settings_panel_telemetry"]) {
        elements.set(id, null);
    }
    const realGet = ctx.document.getElementById;
    ctx.document.getElementById = (id) => (elements.has(id) && elements.get(id) === null ? null : realGet(id));
    try {
        registered();
    } catch (error) {
        // The Data section may want more DOM than this sandbox draws; the
        // licence section has started by then (it starts first).
        if (!elements.has("settings_license_connect")) throw error;
    }
    const tick = async () => {
        const due = timers.filter((t) => t.fn);
        timers.length = 0;
        for (const t of due) await t.fn();
        await new Promise((resolve) => setImmediate(resolve));
    };
    return { el: byId, posted, opened, tick };
}

const FREE = { state: "free", plan: "free", paid: false, service_configured: true, unlocks: [] };
const PAID = { state: "paid_active", plan: "paid", paid: true, source: "cache", service_configured: true,
               environment: { type: "desktop", name: "Bench PC" }, validity: {},
               unlocks: [{ entitlement: "ai", label: "Plexora AI", granted: true }] };

function platform({ polls = 1, decline = false } = {}) {
    let pending = polls;
    return (path) => {
        if (path === "license/status") return { body: { success: true, license: FREE } };
        if (path === "settings/license/connect") {
            return { body: { success: true, code: "BIOC-7F3K-9Q2M", interval: 5, expires_in: 900,
                             verify_url: "https://account.biocognia.com/activate" } };
        }
        if (path === "settings/license/connect/poll") {
            if (decline) {
                return { status: 410, body: { success: false, error: "The activation was declined in the portal.",
                                              license: FREE } };
            }
            if (pending-- > 0) return { body: { success: true, pending: true } };
            return { body: { success: true, pending: false, license: PAID } };
        }
        return { body: {} };
    };
}

await check("Connect this device shows the code and the link, opens the portal, and polls", async () => {
    const { el, posted, opened, tick } = sandbox(platform({ polls: 1 }));
    el("settings_license_connect_name").value = "Bench PC";
    await el("settings_license_connect").fire("click");
    assert.equal(posted[0].path, "settings/license/connect");
    assert.equal(posted[0].body.name, "Bench PC");
    assert.equal(el("settings_license_code").textContent, "BIOC-7F3K-9Q2M");
    assert.equal(el("settings_license_verify").href, "https://account.biocognia.com/activate");
    assert.equal(el("settings_license_connect_box").hidden, false);
    assert.equal(el("settings_license_connect").hidden, true);
    assert.deepEqual(opened, ["https://account.biocognia.com/activate"]);
    await tick();                                         // pending
    assert.equal(posted.at(-1).path, "settings/license/connect/poll");
    assert.equal(posted.at(-1).body.code, "BIOC-7F3K-9Q2M");
    assert.equal(el("settings_license_plan").textContent, "Free");
    await tick();                                         // approved
    assert.equal(el("settings_license_plan").textContent, "Paid");
    assert.equal(el("settings_license_connect_box").hidden, true);
    assert.equal(el("settings_license_connect").hidden, false);
    assert.equal(el("settings_license_error").hidden, true);
});

await check("a declined code says so and stops polling", async () => {
    const { el, posted, tick } = sandbox(platform({ decline: true }));
    await el("settings_license_connect").fire("click");
    await tick();
    assert.match(el("settings_license_error").textContent, /declined/);
    assert.equal(el("settings_license_connect_box").hidden, true);
    const polls = posted.filter((p) => p.path.endsWith("/poll")).length;
    await tick();
    assert.equal(posted.filter((p) => p.path.endsWith("/poll")).length, polls);
});

await check("Cancel stops polling and puts the button back", async () => {
    const { el, posted, tick } = sandbox(platform({ polls: 99 }));
    await el("settings_license_connect").fire("click");
    await el("settings_license_connect_cancel").fire("click");
    await tick();
    assert.equal(posted.filter((p) => p.path.endsWith("/poll")).length, 0);
    assert.equal(el("settings_license_connect").hidden, false);
    assert.equal(el("settings_license_connect").disabled, false);
});
