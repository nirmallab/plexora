/**
 * The website's first-use onboarding (views/webOnboarding.js), and the pick's
 * gate it hangs on (services/pickedFiles.js).
 *
 * The dialog runs here against a DOM small enough to read and a stand-in for
 * the host page's PlexoraStore that records what it was asked. What has to
 * hold:
 *
 *   - **Never on load, never twice.** `ensure` answers "ready" at once where
 *     a place is chosen and open, or where there is no host store at all;
 *     one dialog at a time.
 *   - **The original action resumes, and a cancel leaves nothing.** "Continue
 *     to my image" resolves "ready"; the X resolves "cancelled"; a pick whose
 *     gate says no mounts and records nothing.
 *   - **The folder dialog is the first thing a click awaits**, and so is the
 *     browser's request to keep its storage: both need the click.
 *   - Every path in the spec reaches its screen: returning (found, legacy,
 *     not recognised), new (folder, browser), no folder picker, cancelled,
 *     denied, not writable, no room, half made, and the lapsed folder's
 *     Reconnect.
 *   - Focus, labels, the progress dots, Back, and no confetti under reduced
 *     motion; names from the user's disk are text, never markup.
 *
 * Run directly:  node tests/js/web_onboarding_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = readFileSync(join(REPO, "plexora/client/src/js/views/webOnboarding.js"), "utf8");
const PICKS = readFileSync(join(REPO, "plexora/client/src/js/services/pickedFiles.js"), "utf8");

let checks = 0;
async function check(label, fn) {
    await fn();
    checks += 1;
    console.log("  ok  " + label);
}

// -- a DOM small enough to read -------------------------------------------------

/** `.a.b`, `tag`, `[attr]`, `[attr="v"]`, compounds, and descendants (space). */
function matchesSimple(node, simple) {
    if (!node || !node.tagName) return false;
    const parts = simple.match(/^[a-z0-9-]+|\.[\w-]+|\[[^\]]+\]/gi) || [];
    return parts.every((part) => {
        if (part.startsWith(".")) return node.classList.contains(part.slice(1));
        if (part.startsWith("[")) {
            const [, name, value] = part.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
            const have = node.getAttribute(name);
            return value === undefined ? have !== null : have === value;
        }
        return node.tagName.toLowerCase() === part.toLowerCase();
    });
}

function matches(node, selector) {
    const chain = selector.trim().split(/\s+/);
    if (!matchesSimple(node, chain.at(-1))) return false;
    let at = node.parentNode;
    for (let i = chain.length - 2; i >= 0; i -= 1) {
        while (at && !matchesSimple(at, chain[i])) at = at.parentNode;
        if (!at) return false;
        at = at.parentNode;
    }
    return true;
}

function makeDocument() {
    const doc = {};
    let focused = null;

    function text(value) {
        return { nodeType: 3, textContent: String(value), parentNode: null };
    }

    function element(tag) {
        const classes = new Set();
        const attributes = new Map();
        const listeners = new Map();
        const node = {
            nodeType: 1,
            tagName: String(tag).toUpperCase(),
            children: [],
            childNodes: [],
            parentNode: null,
            dataset: {},
            style: { setProperty(name, value) { this[name] = value; } },
            hidden: false,
            disabled: false,
            open: false,
            type: "",
            title: "",
            alt: "",
            src: "",
            href: "",
            target: "",
            rel: "",
            tabIndex: 0,
            offsetHeight: 0,
            markup: "",
            get id() { return attributes.get("id") || ""; },
            set id(value) { attributes.set("id", String(value)); },
            get className() { return [...classes].join(" "); },
            set className(value) {
                classes.clear();
                String(value).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
            },
            classList: {
                add: (...names) => names.forEach((n) => classes.add(n)),
                remove: (...names) => names.forEach((n) => classes.delete(n)),
                contains: (name) => classes.has(name),
                toggle: (name, on) => ((on === undefined ? !classes.has(name) : on) ? classes.add(name) : classes.delete(name)),
            },
            get textContent() { return node.childNodes.map((child) => child.textContent).join(""); },
            set textContent(value) {
                node.childNodes.forEach((child) => { child.parentNode = null; });
                node.childNodes = value === null || value === undefined || value === "" ? [] : [text(value)];
                node.children = [];
                node.childNodes.forEach((child) => { child.parentNode = node; });
            },
            set innerHTML(value) {
                node.markup = String(value);
                node.childNodes = [];
                node.children = [];
            },
            get innerHTML() { return node.markup; },
            setAttribute(name, value) {
                if (name === "class") node.className = value;
                else attributes.set(name, String(value));
            },
            getAttribute(name) {
                if (name === "class") return node.className;
                if (name.startsWith("data-")) {
                    const key = name.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase());
                    return key in node.dataset ? String(node.dataset[key]) : null;
                }
                return attributes.has(name) ? attributes.get(name) : null;
            },
            removeAttribute(name) { attributes.delete(name); },
            appendChild(child) {
                if (child.parentNode) child.parentNode.removeChild(child);
                child.parentNode = node;
                node.childNodes.push(child);
                if (child.nodeType === 1) node.children.push(child);
                return child;
            },
            append(...items) { items.forEach((item) => node.appendChild(typeof item === "string" ? text(item) : item)); },
            removeChild(child) {
                node.childNodes = node.childNodes.filter((c) => c !== child);
                node.children = node.children.filter((c) => c !== child);
                child.parentNode = null;
                return child;
            },
            replaceChildren(...items) {
                [...node.childNodes].forEach((child) => node.removeChild(child));
                items.forEach((item) => node.appendChild(item));
            },
            replaceWith(other) {
                const parent = node.parentNode;
                if (!parent) return;
                const at = parent.childNodes.indexOf(node);
                parent.removeChild(node);
                if (other.parentNode) other.parentNode.removeChild(other);
                other.parentNode = parent;
                parent.childNodes.splice(at, 0, other);
                parent.children = parent.childNodes.filter((c) => c.nodeType === 1);
            },
            remove() { if (node.parentNode) node.parentNode.removeChild(node); },
            contains(other) {
                for (let at = other; at; at = at.parentNode) if (at === node) return true;
                return false;
            },
            querySelectorAll(selector) {
                const out = [];
                const walk = (at) => at.children.forEach((child) => {
                    if (selector.split(",").some((one) => matches(child, one))) out.push(child);
                    walk(child);
                });
                walk(node);
                return out;
            },
            querySelector(selector) { return node.querySelectorAll(selector)[0] || null; },
            addEventListener(type, handler) {
                if (!listeners.has(type)) listeners.set(type, []);
                listeners.get(type).push(handler);
            },
            removeEventListener() {},
            dispatch(type, extra = {}) {
                const event = { type, target: node, defaultPrevented: false,
                                preventDefault() { this.defaultPrevented = true; }, ...extra };
                (listeners.get(type) || []).forEach((handler) => handler(event));
                return event;
            },
            click() { if (!node.disabled) node.dispatch("click"); },
            focus() { focused = node; },
            showModal() {
                node.open = true;
            },
            close() {
                if (!node.open) return;
                node.open = false;
                node.dispatch("close");
            },
        };
        return node;
    }

    doc.createElement = (tag) => element(tag);
    doc.createElementNS = (ns, tag) => element(tag);
    doc.createTextNode = (value) => text(value);
    doc.body = element("body");
    doc.getElementById = (id) => doc.body.querySelectorAll("*").find?.((n) => n.id === id)
        || doc.body.querySelectorAll(`[id="${id}"]`)[0] || null;
    Object.defineProperty(doc, "activeElement", { get: () => focused });
    return doc;
}

// -- the host page's store, recording what it is asked --------------------------

function makeStore(options = {}) {
    const asked = [];
    const state = {
        chosen: false, open: false, lapsed: false, missing: false, folders: true, declined: false,
        look: { kind: "workspace", name: "Plexora", label: "Plexora", projects: [{ name: "a", title: "Tonsil", id: null }],
                figures: 2, captures: true, nested: null, writable: true, workspaceId: null },
        pick: "folder", adoptError: null, adoptHang: false, desktopFinds: true, quota: false,
        reconnect: { ok: true, label: "Plexora" },
        ...options,
    };
    const store = {
        asked,
        state,
        inClick: false,
        needsOnboarding: () => !state.chosen,
        needsReconnect: () => state.lapsed && !state.declined,
        current: () => (state.lapsed ? { kind: "browser", chosen: true, notOpened: "Plexora", missing: state.missing }
                                     : { kind: state.chosenKind || "browser", chosen: state.chosen }),
        canWriteFolders: () => state.folders,
        pickFolder(opts) {
            asked.push(["pickFolder", opts, store.inClick]);
            if (!state.folders) return Promise.reject(Object.assign(new Error("no picker"), { code: "unsupported" }));
            if (state.pick === "cancel") return Promise.resolve(null);
            if (state.pick === "deny") return Promise.reject(Object.assign(new Error("no"), { code: "denied" }));
            return Promise.resolve({ name: state.look.name });
        },
        async inspectFolder(handle) {
            asked.push(["inspectFolder", handle.name]);
            return state.look;
        },
        async adoptWorkspace(handle, opts) {
            asked.push(["adoptWorkspace", handle.name, opts.init]);
            opts.onProgress("probe");
            opts.onProgress("folders");
            if (state.adoptError) throw Object.assign(new Error(state.adoptError.message || "no"), state.adoptError);
            opts.onProgress("merge");
            opts.onProgress("remember");
            if (state.adoptHang) {
                opts.onProgress("python");
                return new Promise(() => {});
            }
            opts.onProgress("open");
            opts.onProgress("done");
            state.chosen = true;
            return { kind: "folder", label: state.look.label, desktopFinds: state.desktopFinds, moved: [], look: state.look.kind };
        },
        async prepareBrowser() {
            asked.push(["prepareBrowser"]);
            return { probe: state.quota ? "quota" : "ok", estimate: null };
        },
        async useBrowser() {
            asked.push(["useBrowser"]);
            state.chosen = true;
            return { kind: "browser", chosen: true };
        },
        continueInBrowser() {
            asked.push(["continueInBrowser"]);
            state.declined = true;
        },
        reconnect() {
            asked.push(["reconnect", store.inClick]);
            return Promise.resolve(state.reconnect);
        },
        async switchTo(kind) { asked.push(["switchTo", kind]); },
    };
    return store;
}

function boot({ store = makeStore(), reducedMotion = true, noStore = false } = {}) {
    const document = makeDocument();
    const persisted = [];
    const context = {
        console, Promise, Object, Array, Map, Set, JSON, Error, String, Number, Boolean, Math,
        setTimeout, clearTimeout,
        document,
        matchMedia: () => ({ matches: reducedMotion }),
        navigator: { storage: {
            persist() { persisted.push(store.inClick); return Promise.resolve(true); },
            persisted: () => Promise.resolve(true),
        } },
        PlexoraToast: { shown: [], show(options) { this.shown.push(options); } },
    };
    context.window = context;
    if (!noStore) {
        context.PlexoraStore = store;
        context.PlexoraSite = { version: "0.0.45" };
    }
    createContext(context);
    runInContext(SOURCE, context);
    const dialog = () => document.body.querySelector("dialog.plx-onb");
    const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
    const settle = async () => { for (let i = 0; i < 12; i += 1) await tick(); };
    const press = async (selector) => {
        const node = dialog().querySelector(selector);
        assert.ok(node, `no ${selector} on ${dialog() && dialog().dataset.screen}`);
        store.inClick = true;
        node.click();
        store.inClick = false;
        await settle();
    };
    return { api: context.PlexoraOnboarding, context, document, dialog, store, settle, press, persisted };
}

const screenOf = (t) => t.dialog() && t.dialog().dataset.screen;
const titleOf = (t) => t.dialog().querySelector(".plx-onb-screen .plx-onb-title").textContent;

// -- the checks -------------------------------------------------------------------

await check("ensure is ready at once when a place is chosen and open, and draws nothing", async () => {
    const t = boot({ store: makeStore({ chosen: true }) });
    assert.equal(await t.api.ensure({ reason: "pick" }), "ready");
    assert.equal(t.dialog(), null);
});

await check("ensure is ready at once on a page with no host store", async () => {
    const t = boot({ noStore: true });
    assert.equal(await t.api.ensure({ reason: "pick" }), "ready");
    assert.equal(t.dialog(), null);
});

await check("the first use opens on the welcome screen: the name large, its version, and the two cards", async () => {
    const t = boot();
    t.api.ensure({ reason: "pick" });
    await t.settle();
    assert.equal(t.dialog().open, true);
    assert.equal(screenOf(t), "welcome");
    const hero = t.dialog().querySelector(".plx-onb-screen .plx-onb-brand-hero");
    assert.ok(hero, "the welcome screen leads with the name, not a tile");
    assert.equal(titleOf(t), "Welcome to Plexora");
    assert.ok(hero.querySelector("h2.plx-onb-name"));
    assert.equal(hero.querySelector(".plx-onb-name-version").textContent, "Version 0.0.45");
    // No Lite badge on this screen: the header's brand is hidden here, and the
    // hero has none.
    assert.equal(hero.querySelector(".plx-onb-edition"), null);
    const cards = t.dialog().querySelectorAll(".plx-onb-card");
    assert.deepEqual(cards.map((card) => card.querySelector(".plx-onb-card-title").textContent),
                     ["I've used Plexora before", "I'm new to Plexora"]);
});

await check("the welcome screen says what Plexora does, in four points, each with its own icon", async () => {
    const t = boot();
    t.api.ensure({});
    await t.settle();
    const features = t.dialog().querySelectorAll(".plx-onb-feature");
    assert.equal(features.length, 4);
    const titles = features.map((item) => item.querySelector(".plx-onb-feature-title").textContent);
    assert.deepEqual(titles, ["Your images are not uploaded", "Work where your data lives",
                              "Multiplexed and whole-slide", "Analysis in one place"]);
    const icons = features.map((item) => item.querySelector(".plx-onb-feature-icon").innerHTML);
    assert.equal(new Set(icons).size, 4, "each feature has its own icon");
    // The privacy line does not overclaim: the AI features send views.
    assert.match(features[0].textContent, /AI features, when used, send only the views they need/);
});

await check("the first screen says the setup is asked once, in this browser", async () => {
    const t = boot();
    t.api.ensure({});
    await t.settle();
    assert.equal(t.dialog().querySelector(".plx-onb-once-pill").textContent, "One-time setup");
    assert.match(t.dialog().querySelector(".plx-onb-start-note").textContent, /remembers your choice in this browser/);
    assert.equal(t.dialog().querySelector(".plx-onb-step-label").textContent, "Step 1 of 3");
});

await check("the header names Plexora and the site's version, with no Lite badge on any screen", async () => {
    const t = boot();
    t.api.ensure({});
    await t.settle();
    const head = t.dialog().querySelector(".plx-onb-head");
    assert.equal(head.querySelector(".plx-onb-wordmark").textContent, "Plexora");
    for (const screen of ["welcome", "home", "create"]) {
        if (screen === "home") await t.press('[data-choice="new"]');
        if (screen === "create") await t.press('[data-choice="folder"]');
        assert.equal(t.dialog().querySelector(".plx-onb-edition, .plx-edition-badge"), null, screen);
        assert.ok(!/\bLite\b/.test(t.dialog().querySelector(".plx-onb-head").textContent), screen);
    }
    assert.equal(head.querySelector(".plx-onb-version").textContent, "Version 0.0.45");
    assert.equal(head.querySelector(".plx-onb-version").hidden, false);
});

await check("the X closes it as a cancel, and the dialog is taken down", async () => {
    const t = boot();
    const answer = t.api.ensure({ reason: "pick" });
    await t.settle();
    t.dialog().querySelector(".plx-onb-close").click();
    assert.equal(await answer, "cancelled");
    assert.equal(t.dialog(), null);
});

await check("two calls while it is open share one dialog", async () => {
    const t = boot();
    const first = t.api.ensure({});
    const second = t.api.ensure({});
    await t.settle();
    assert.equal(first, second);
    assert.equal(t.document.body.querySelectorAll("dialog").length, 1);
    t.dialog().close();
    assert.equal(await first, "cancelled");
});

await check("new, folder: Create your Plexora folder, the folder made the workspace, and Continue to my image", async () => {
    const store = makeStore({ look: { kind: "empty", name: "Plexora", label: "Plexora", projects: [], figures: 0,
                                      captures: false, nested: null, writable: true } });
    const t = boot({ store });
    const answer = t.api.ensure({ reason: "pick" });
    await t.settle();
    await t.press('[data-choice="new"]');
    assert.equal(screenOf(t), "home");
    assert.equal(titleOf(t), "Give your work a home");
    assert.equal(t.dialog().querySelectorAll(".plx-onb-benefit").length, 5);
    // The reasons for a folder come first, as the argument for the choice;
    // then the two places, the folder recommended and the browser quieter.
    assert.equal(t.dialog().querySelector(".plx-onb-screen .plx-onb-lead").textContent,
                 "A folder on this computer lets you:");
    const body = t.dialog().querySelector(".plx-onb-screen .plx-onb-body");
    assert.deepEqual(body.children.map((child) => child.className), ["plx-onb-benefits", "plx-onb-cards is-options"]);
    assert.ok(t.dialog().querySelector('[data-choice="browser"]').classList.contains("is-quiet"));
    // Each rises in turn: the benefits, then the two places after them.
    const order = [...t.dialog().querySelectorAll(".plx-onb-benefit"), ...t.dialog().querySelectorAll(".plx-onb-card")]
        .map((node) => Number(node.style["--i"]));
    assert.deepEqual(order, [0, 1, 2, 3, 4, 5, 6]);
    assert.ok(t.dialog().querySelector('[data-choice="folder"] .plx-onb-chip').textContent === "Recommended");
    await t.press('[data-choice="folder"]');
    assert.equal(titleOf(t), "Create your Plexora folder");
    assert.equal(t.dialog().querySelectorAll(".plx-onb-stepline").length, 4);
    await t.press('[data-role="choose"]');
    const picked = store.asked.find((call) => call[0] === "pickFolder");
    assert.equal(picked[1].startIn, "documents");
    assert.deepEqual(store.asked.find((call) => call[0] === "adoptWorkspace"), ["adoptWorkspace", "Plexora", true]);
    assert.equal(titleOf(t), "Your workspace is ready");
    assert.equal(t.dialog().querySelector(".plx-onb-place-name").textContent, "Plexora");
    assert.equal(t.dialog().querySelector(".plx-onb-once").textContent, "You won't be asked again in this browser.");
    assert.ok(t.dialog().querySelector(".plx-onb-screen").classList.contains("is-finish"));
    assert.equal(t.dialog().querySelector(".plx-onb-step-label").textContent, "Done");
    const go = t.dialog().querySelector('[data-role="continue"]');
    assert.equal(go.textContent, "Continue to my image");
    go.click();
    assert.equal(await answer, "ready");
});

await check("the folder dialog is the first thing the click awaits", async () => {
    const t = boot();
    t.api.ensure({});
    await t.settle();
    await t.press('[data-choice="returning"]');
    await t.press('[data-role="locate"]');
    const picked = t.store.asked.find((call) => call[0] === "pickFolder");
    assert.equal(picked[2], true, "pickFolder ran inside the click");
});

await check("returning: a workspace found shows its projects, and Continue adopts it as it is", async () => {
    const t = boot();
    const answer = t.api.ensure({ reason: "remote" });
    await t.settle();
    await t.press('[data-choice="returning"]');
    assert.equal(titleOf(t), "Welcome back");
    await t.press('[data-role="locate"]');
    assert.equal(screenOf(t), "found");
    assert.equal(titleOf(t), "Found your workspace");
    const summary = t.dialog().querySelector(".plx-onb-summary");
    assert.match(summary.textContent, /1 project · 2 figures · bookmarks and captures/);
    assert.match(summary.textContent, /Tonsil/);
    await t.press('[data-role="continue"]');
    assert.deepEqual(t.store.asked.find((call) => call[0] === "adoptWorkspace"), ["adoptWorkspace", "Plexora", false]);
    assert.equal(t.dialog().querySelector('[data-role="continue"]').textContent, "Continue");
    t.dialog().querySelector('[data-role="continue"]').click();
    assert.equal(await answer, "ready");
});

await check("the library an old version made inside is found, and nothing is moved", async () => {
    const t = boot({ store: makeStore({ look: { kind: "legacy-nested", name: "Plexora", label: "Plexora › Plexora",
                                                projects: [], figures: 0, captures: false, nested: {}, writable: true } }) });
    t.api.ensure({});
    await t.settle();
    await t.press('[data-choice="returning"]');
    await t.press('[data-role="locate"]');
    assert.equal(screenOf(t), "found");
    assert.match(t.dialog().querySelector(".plx-onb-lead").textContent, /nothing is moved/);
    assert.match(t.dialog().querySelector(".plx-onb-summary-name").textContent, /Plexora › Plexora/);
});

await check("a folder that is not a workspace: choose another, set it up, or start in the browser", async () => {
    const t = boot({ store: makeStore({ look: { kind: "foreign", name: "Research", label: "Research", projects: [],
                                                figures: 0, captures: false, nested: null, writable: true } }) });
    t.api.ensure({});
    await t.settle();
    await t.press('[data-choice="returning"]');
    await t.press('[data-role="locate"]');
    assert.equal(screenOf(t), "unrecognised");
    assert.equal(titleOf(t), "“Research” is not a Plexora folder");
    const roles = t.dialog().querySelectorAll("button[data-role]").map((node) => node.dataset.role);
    assert.ok(roles.includes("another") && roles.includes("init") && roles.includes("browser"));
    await t.press('[data-role="init"]');
    assert.deepEqual(t.store.asked.find((call) => call[0] === "adoptWorkspace"), ["adoptWorkspace", "Research", true]);
});

await check("a cancelled folder dialog says so, and Choose folder opens it again", async () => {
    const t = boot({ store: makeStore({ pick: "cancel" }) });
    t.api.ensure({ reason: "pick" });
    await t.settle();
    await t.press('[data-choice="new"]');
    await t.press('[data-choice="folder"]');
    await t.press('[data-role="choose"]');
    assert.equal(titleOf(t), "No folder was chosen");
    assert.match(t.dialog().querySelector(".plx-onb-lead").textContent, /Your image is still waiting/);
    await t.press('[data-role="retry"]');
    assert.equal(t.store.asked.filter((call) => call[0] === "pickFolder").length, 2);
});

await check("a refused permission shows the denied screen with Try again", async () => {
    const t = boot({ store: makeStore({ pick: "deny" }) });
    t.api.ensure({});
    await t.settle();
    await t.press('[data-choice="returning"]');
    await t.press('[data-role="locate"]');
    assert.equal(screenOf(t), "denied");
    assert.equal(titleOf(t), "Plexora wasn't allowed to use that folder");
    assert.ok(t.dialog().querySelector('[data-role="retry"]'));
});

await check("no folder picker: the folder option is replaced by why, and Locate explains it", async () => {
    let t = boot({ store: makeStore({ folders: false }) });
    t.api.ensure({});
    await t.settle();
    await t.press('[data-choice="new"]');
    assert.equal(t.dialog().querySelector('[data-choice="folder"]'), null);
    assert.match(t.dialog().querySelector(".plx-onb-hint.is-warn").textContent, /Chrome/);
    assert.ok(t.dialog().querySelector('[data-choice="browser"]'));
    t = boot({ store: makeStore({ folders: false }) });
    t.api.ensure({});
    await t.settle();
    await t.press('[data-choice="returning"]');
    await t.press('[data-role="locate"]');
    assert.equal(screenOf(t), "unsupported");
    assert.equal(titleOf(t), "This browser can't save to a folder");
});

await check("browser storage asks the browser to keep it from the click, then says it is ready", async () => {
    const t = boot();
    const answer = t.api.ensure({ reason: "pick" });
    await t.settle();
    await t.press('[data-choice="new"]');
    await t.press('[data-choice="browser"]');
    assert.deepEqual(t.persisted, [true], "persist() ran inside the click");
    assert.ok(t.store.asked.some((call) => call[0] === "prepareBrowser"));
    assert.ok(t.store.asked.some((call) => call[0] === "useBrowser"));
    assert.equal(titleOf(t), "Browser storage is ready");
    assert.equal(t.dialog().querySelector(".plx-onb-place-name").textContent, "This browser");
    t.dialog().querySelector('[data-role="continue"]').click();
    assert.equal(await answer, "ready");
});

await check("no room in the browser shows the quota screen", async () => {
    const t = boot({ store: makeStore({ quota: true }) });
    t.api.ensure({});
    await t.settle();
    await t.press('[data-choice="new"]');
    await t.press('[data-choice="browser"]');
    assert.equal(screenOf(t), "quota");
    assert.equal(titleOf(t), "There is no room left to save");
    assert.ok(!t.store.asked.some((call) => call[0] === "useBrowser"));
});

await check("a folder that cannot be written, and one only partly set up, each say so; Try again runs it again", async () => {
    const empty = { kind: "empty", name: "Shared", label: "Shared", projects: [], figures: 0, captures: false,
                    nested: null, writable: true };
    let t = boot({ store: makeStore({ look: empty, adoptError: { code: "not-writable" } }) });
    t.api.ensure({});
    await t.settle();
    await t.press('[data-choice="new"]');
    await t.press('[data-choice="folder"]');
    await t.press('[data-role="choose"]');
    assert.equal(screenOf(t), "not-writable");
    assert.equal(titleOf(t), "Plexora can't save in “Shared”");
    t = boot({ store: makeStore({ look: empty, adoptError: { code: "partial", message: "disk said no" } }) });
    t.api.ensure({});
    await t.settle();
    await t.press('[data-choice="new"]');
    await t.press('[data-choice="folder"]');
    await t.press('[data-role="choose"]');
    assert.equal(screenOf(t), "partial");
    assert.equal(t.dialog().querySelector(".plx-onb-detail").textContent, "disk said no");
    t.store.state.adoptError = null;
    await t.press('[data-role="retry"]');
    assert.equal(t.store.asked.filter((call) => call[0] === "adoptWorkspace").length, 2);
    assert.equal(titleOf(t), "Your workspace is ready");
});

await check("a folder that already has Plexora data, or other files, is confirmed before it is used", async () => {
    let t = boot();
    t.api.ensure({});
    await t.settle();
    await t.press('[data-choice="new"]');
    await t.press('[data-choice="folder"]');
    await t.press('[data-role="choose"]');
    assert.equal(titleOf(t), "This folder already has Plexora data");
    await t.press('[data-role="use"]');
    assert.deepEqual(t.store.asked.find((call) => call[0] === "adoptWorkspace"), ["adoptWorkspace", "Plexora", false]);
    t = boot({ store: makeStore({ look: { kind: "foreign", name: "Research", label: "Research", projects: [],
                                          figures: 0, captures: false, nested: null, writable: true } }) });
    t.api.ensure({});
    await t.settle();
    await t.press('[data-choice="new"]');
    await t.press('[data-choice="folder"]');
    await t.press('[data-role="choose"]');
    assert.equal(titleOf(t), "“Research” already has other files");
});

await check("a workspace outside Documents › Plexora says the desktop app needs it added", async () => {
    const t = boot({ store: makeStore({ desktopFinds: false,
                                        look: { kind: "empty", name: "Imaging", label: "Imaging", projects: [], figures: 0,
                                                captures: false, nested: null, writable: true } }) });
    t.api.ensure({ reason: "pick" });
    await t.settle();
    await t.press('[data-choice="new"]');
    await t.press('[data-choice="folder"]');
    await t.press('[data-role="choose"]');
    assert.match(t.dialog().querySelector(".plx-onb-done").textContent, /Settings › Data › Locations/);
});

await check("a lapsed folder opens on Reconnect; Reconnect asks from the click and resolves ready", async () => {
    const t = boot({ store: makeStore({ chosen: true, lapsed: true }) });
    const answer = t.api.ensure({ reason: "pick" });
    await t.settle();
    assert.equal(screenOf(t), "reconnect");
    assert.equal(titleOf(t), "We couldn't reconnect to your Plexora folder");
    assert.equal(t.dialog().querySelector(".plx-onb-steps-at").hidden, true);
    // Not a setup: nothing here says one-time; it says when it is needed.
    assert.ok(!/one-time/i.test(t.dialog().querySelector(".plx-onb-screen").textContent));
    assert.match(t.dialog().querySelector(".plx-onb-screen").textContent, /only when the browser forgets its permission/);
    await t.press('[data-role="reconnect"]');
    assert.deepEqual(t.store.asked.find((call) => call[0] === "reconnect"), ["reconnect", true]);
    assert.equal(await answer, "ready");
});

await check("Continue with browser storage sets the folder aside for this visit and goes on", async () => {
    const t = boot({ store: makeStore({ chosen: true, lapsed: true }) });
    const answer = t.api.ensure({});
    await t.settle();
    await t.press('[data-role="browser"]');
    assert.equal(await answer, "ready");
    assert.ok(t.store.asked.some((call) => call[0] === "continueInBrowser"));
    assert.equal(await t.api.ensure({}), "ready");
});

await check("a refused reconnect shows the denied screen; a folder that is gone offers another", async () => {
    let t = boot({ store: makeStore({ chosen: true, lapsed: true, reconnect: { ok: false, reason: "denied" } }) });
    t.api.ensure({});
    await t.settle();
    await t.press('[data-role="reconnect"]');
    assert.equal(screenOf(t), "denied");
    t = boot({ store: makeStore({ chosen: true, lapsed: true, missing: true }) });
    t.api.ensure({});
    await t.settle();
    assert.equal(t.dialog().querySelector('[data-role="reconnect"]'), null);
    assert.ok(t.dialog().querySelector('[data-role="another"].plx-button-primary'));
});

await check("Back returns to the screen before, and the first screen has none", async () => {
    const t = boot();
    t.api.ensure({});
    await t.settle();
    assert.equal(t.dialog().querySelector('[data-role="back"]'), null);
    await t.press('[data-choice="new"]');
    await t.press('[data-choice="folder"]');
    assert.equal(screenOf(t), "create");
    await t.press('[data-role="back"]');
    assert.equal(screenOf(t), "home");
    await t.press('[data-role="back"]');
    assert.equal(screenOf(t), "welcome");
});

await check("while work is under way the X and Escape do nothing", async () => {
    const store = makeStore({ adoptHang: true, look: { kind: "empty", name: "Plexora", label: "Plexora", projects: [],
                                                       figures: 0, captures: false, nested: null, writable: true } });
    const t = boot({ store });
    t.api.ensure({});
    await t.settle();
    await t.press('[data-choice="new"]');
    await t.press('[data-choice="folder"]');
    await t.press('[data-role="choose"]');
    assert.equal(screenOf(t), "setup");
    assert.equal(t.dialog().querySelector(".plx-onb-close").disabled, true);
    assert.equal(t.dialog().dispatch("cancel").defaultPrevented, true);
    const tasks = t.dialog().querySelectorAll(".plx-onb-task");
    assert.deepEqual(tasks.map((task) => task.classList.contains("is-done")), [true, true, true, false]);
    assert.ok(tasks[3].classList.contains("is-running"));
    assert.match(tasks[3].textContent, /Waiting for Plexora to finish starting/);
});

await check("closed after the workspace is ready, the work still goes on", async () => {
    const t = boot({ store: makeStore({ look: { kind: "empty", name: "Plexora", label: "Plexora", projects: [],
                                                figures: 0, captures: false, nested: null, writable: true } }) });
    const answer = t.api.ensure({ reason: "pick" });
    await t.settle();
    await t.press('[data-choice="new"]');
    await t.press('[data-choice="folder"]');
    await t.press('[data-role="choose"]');
    t.dialog().querySelector(".plx-onb-close").click();
    assert.equal(await answer, "ready");
});

await check("the progress dots follow the steps, and all are done at the end", async () => {
    const t = boot({ store: makeStore({ look: { kind: "empty", name: "Plexora", label: "Plexora", projects: [],
                                                figures: 0, captures: false, nested: null, writable: true } }) });
    t.api.ensure({});
    await t.settle();
    const progress = () => t.dialog().querySelector(".plx-onb-progress");
    assert.equal(progress().getAttribute("aria-label"), "Step 1 of 3");
    await t.press('[data-choice="new"]');
    assert.equal(progress().getAttribute("aria-label"), "Step 2 of 3");
    await t.press('[data-choice="folder"]');
    assert.equal(progress().getAttribute("aria-label"), "Step 3 of 3");
    await t.press('[data-role="choose"]');
    assert.equal(progress().getAttribute("aria-label"), "All steps done");
    assert.equal(progress().querySelectorAll(".plx-onb-dot.is-done").length, 3);
});

await check("each screen labels the dialog and takes the focus to its title", async () => {
    const t = boot();
    t.api.ensure({});
    await t.settle();
    let heading = t.dialog().querySelector(".plx-onb-screen .plx-onb-title");
    assert.equal(t.dialog().getAttribute("aria-labelledby"), heading.id);
    assert.equal(heading.tabIndex, -1);
    assert.equal(t.document.activeElement, heading);
    await t.press('[data-choice="returning"]');
    heading = t.dialog().querySelector(".plx-onb-screen .plx-onb-title");
    assert.equal(t.dialog().getAttribute("aria-labelledby"), heading.id);
    assert.equal(t.document.activeElement, heading);
    assert.ok(t.dialog().getAttribute("aria-describedby"));
});

await check("a little confetti when ready, and none under reduced motion", async () => {
    const run = async (reducedMotion) => {
        const t = boot({ reducedMotion, store: makeStore({ look: { kind: "empty", name: "Plexora", label: "Plexora",
                                                                   projects: [], figures: 0, captures: false,
                                                                   nested: null, writable: true } }) });
        t.api.ensure({});
        await t.settle();
        await t.press('[data-choice="new"]');
        await t.press('[data-choice="folder"]');
        await t.press('[data-role="choose"]');
        return t.dialog().querySelectorAll(".plx-onb-confetti i").length;
    };
    assert.equal(await run(true), 0);
    assert.equal(await run(false), 14);
});

await check("motion: the head icon draws on, the step pill slides, an error shakes its tile", async () => {
    const t = boot({ store: makeStore({ pick: "cancel" }) });
    t.api.ensure({ reason: "pick" });
    await t.settle();
    const thumb = () => t.dialog().querySelector(".plx-onb-dot-thumb");
    assert.equal(thumb().style["--at"], "0");
    await t.press('[data-choice="new"]');
    assert.equal(thumb().style["--at"], "1");
    // Every stroke of the head icon has a length of one, so CSS draws it on;
    // the wash under it and its dots are left alone.
    const tile = t.dialog().querySelector(".plx-onb-screen .plx-onb-tile").innerHTML;
    assert.ok(/<path pathLength="1" d=/.test(tile), tile);
    assert.ok(!/class="d" pathLength|pathLength="1" class=/.test(tile));
    // The lists rise one after another.
    assert.deepEqual(t.dialog().querySelectorAll(".plx-onb-benefit").map((node) => node.style["--i"]),
                     ["0", "1", "2", "3", "4"]);
    await t.press('[data-choice="folder"]');
    assert.ok(!t.dialog().querySelector(".plx-onb-screen").classList.contains("is-error"));
    await t.press('[data-role="choose"]');
    assert.equal(screenOf(t), "cancelled");
    assert.ok(t.dialog().querySelector(".plx-onb-screen").classList.contains("is-error"));
});

await check("names from the user's disk are text, never markup", async () => {
    const hostile = "<img src=x onerror=alert(1)>";
    const t = boot({ store: makeStore({ look: { kind: "foreign", name: hostile, label: hostile, projects: [
        { name: hostile, title: hostile, id: null }], figures: 0, captures: false, nested: null, writable: true } }) });
    t.api.ensure({});
    await t.settle();
    await t.press('[data-choice="returning"]');
    await t.press('[data-role="locate"]');
    assert.ok(titleOf(t).includes(hostile));
    const markup = t.dialog().querySelectorAll("*").map((node) => node.innerHTML).join("");
    assert.ok(!markup.includes("onerror"), "a name reached innerHTML");
});

// -- the pick's gate (services/pickedFiles.js) -----------------------------------

function bootPicks({ gateAnswer, picker = "file" }) {
    const mounted = [];
    const remembered = [];
    const document = makeDocument();
    const context = {
        console, Promise, Object, Array, Map, Set, JSON, Error, String, Number, Boolean, Uint8Array, setTimeout,
        document,
        crypto: { getRandomValues: (bytes) => bytes.fill(7) },
        indexedDB: {
            open() {
                const request = {};
                setTimeout(() => {
                    request.result = {
                        transaction() {
                            const tx = { objectStore: () => ({ put: (record) => { remembered.push(record); return {}; },
                                                               getAll: () => ({ result: [] }) }) };
                            setTimeout(() => tx.oncomplete && tx.oncomplete(), 0);
                            return tx;
                        },
                        close() {},
                    };
                    request.onsuccess();
                }, 0);
                return request;
            },
        },
        PlexoraPython: {
            mount: async (dir, files) => { mounted.push(dir); return files.map((f) => `${dir}/${f.name}`); },
            mountTree: async (dir) => { mounted.push(dir); },
        },
    };
    const file = { name: "tissue.ome.tif", size: 3, lastModified: 1, async getFile() { return this; } };
    if (picker === "file") context.showOpenFilePicker = async () => [file];
    if (picker === "directory") {
        context.showDirectoryPicker = async () => ({
            name: "run", kind: "directory",
            async *entries() { yield ["a.tif", { kind: "file", getFile: async () => file }]; },
        });
    }
    context.window = context;
    createContext(context);
    runInContext(PICKS, context);
    const gated = [];
    const gate = async () => { gated.push(mounted.length); return gateAnswer; };
    return { picks: context.PlexoraPicks, mounted, remembered, gated, gate };
}

await check("a pick's gate runs before anything is mounted or recorded, and a no leaves nothing", async () => {
    for (const [mode, picker] of [["file", "file"], ["directory", "directory"]]) {
        let t = bootPicks({ gateAnswer: false, picker });
        assert.equal(await t.picks.pick({ mode, gate: t.gate }), null);
        assert.deepEqual(t.gated, [0]);
        assert.deepEqual(t.mounted, []);
        assert.deepEqual(t.remembered, []);
        t = bootPicks({ gateAnswer: true, picker });
        const path = await t.picks.pick({ mode, gate: t.gate });
        assert.match(String(path), /^\/picked\//);
        assert.deepEqual(t.gated, [0]);
        assert.equal(t.mounted.length, 1);
        assert.equal(t.remembered.length, 1);
    }
});

console.log(`${checks} checks passed`);
