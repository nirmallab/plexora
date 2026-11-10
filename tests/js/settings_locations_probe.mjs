/**
 * Settings > Data's locations: what each row offers, and what a press sends.
 *
 * The routes (server/models/library.py) decide what may change; the page
 * decides what to OFFER, and that is what is checked here, against the
 * shipped settingsPage.js:
 *
 *   1. **Every location is a row** -- label, path, kind, count, state -- and
 *      the default one is marked.
 *   2. **What the route would refuse is offered greyed, with the reason**,
 *      and pressing it says why and sends nothing.
 *   3. **Remove only for added workspaces, Tidy up only for the older
 *      folder**, and Tidy up asks first, naming what moves where.
 *   4. **A refusal shows as the route wrote it**, under the list or under
 *      the add field.
 *   5. **The website's rows come from the host page's store**: the default
 *      folder first, Reconnect where the browser has not allowed one, and
 *      every change followed by the worker mounting the folders again.
 *
 * Run directly:  node tests/js/settings_locations_probe.mjs
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
    const classes = new Set();
    const attributes = new Map();
    const element = {
        tagName: String(tag).toUpperCase(),
        children: [],
        hidden: false,
        disabled: false,
        title: "",
        value: "",
        type: "",
        dataset: {},
        style: {},
        get textContent() {
            return own + element.children.map((child) => child.textContent).join("");
        },
        set textContent(value) {
            own = value === null || value === undefined ? "" : String(value);
            element.children = [];
        },
        get className() { return Array.from(classes).join(" "); },
        set className(value) {
            classes.clear();
            String(value).split(/\s+/).filter(Boolean).forEach((name) => classes.add(name));
        },
        classList: {
            add: (...names) => names.forEach((name) => classes.add(name)),
            remove: (...names) => names.forEach((name) => classes.delete(name)),
            toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
            contains: (name) => classes.has(name),
        },
        setAttribute(name, value) { attributes.set(name, String(value)); },
        getAttribute(name) { return attributes.has(name) ? attributes.get(name) : null; },
        appendChild(child) { element.children.push(child); return child; },
        append(...nodes) { nodes.forEach((child) => element.children.push(child)); },
        replaceChildren(...nodes) { own = ""; element.children = nodes.slice(); },
        addEventListener(type, handler) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(handler);
        },
        removeEventListener() {},
        focus() {},
        querySelectorAll: () => [],
        async click() {
            for (const handler of listeners.get("click") || []) await handler({ type: "click" });
            if (typeof element.onclick === "function") await element.onclick({ type: "click" });
        },
        async key(key) {
            for (const handler of listeners.get("keydown") || []) {
                await handler({ key, preventDefault() {} });
            }
        },
    };
    return element;
}

function textNode(value) {
    return { textContent: String(value), children: [], classList: { contains: () => false } };
}

//: Every element under `root` (depth first), itself included.
function walk(root, out = []) {
    if (!root || !root.children) return out;
    out.push(root);
    root.children.forEach((child) => walk(child, out));
    return out;
}

const withClass = (root, name) => walk(root).filter((node) => node.classList && node.classList.contains(name));
const buttonsOf = (row) => walk(row).filter((node) => node.tagName === "BUTTON");
const button = (row, label) => buttonsOf(row).find((node) => node.textContent.trim() === label);
const rowFor = (list, label) => withClass(list, "plx-locations-row")
    .find((row) => withClass(row, "plx-locations-label")[0]?.textContent === label);

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
async function settled() {
    for (let i = 0; i < 6; i += 1) await settle();
}

// -- the page, loaded with a given set of ids ---------------------------------

//: What the templates render `hidden`, so a toggle starts where the page does.
const HIDDEN_AT_START = new Set(["settings_location_form", "settings_location_error",
                                 "settings_locations_message", "settings_locations_error",
                                 "settings_locations_env", "settings_web_place_message",
                                 "settings_web_place_error", "settings_web_add_folder",
                                 "settings_web_setup_again"]);

function load({ ids, respond, extra = {} }) {
    const byId = new Map();
    const elementFor = (id) => {
        if (!ids.includes(id)) return null;
        if (!byId.has(id)) {
            const made = makeElement(id.includes("button") || /_(add|open|browse|folder|again)$/.test(id) ? "button" : "div");
            made.hidden = HIDDEN_AT_START.has(id);
            byId.set(id, made);
        }
        return byId.get(id);
    };
    const sent = [];
    const context = {
        console, setTimeout, clearTimeout, Promise, JSON, Object, Array, Boolean,
        String, Number, Math, Date, Map, Set, Error, encodeURIComponent, URL,
        fetch: (url, init = {}) => {
            const method = (init.method || "GET").toUpperCase();
            const body = init.body ? JSON.parse(init.body) : null;
            sent.push({ method, path: String(url), body });
            return respond(method, String(url), body);
        },
        plexoraUrl: (path) => `/${String(path).replace(/^\/+/, "")}`,
        document: {
            createElement: makeElement,
            createTextNode: textNode,
            getElementById: elementFor,
            querySelector: () => null,
            querySelectorAll: () => [],
            addEventListener: () => {},
        },
        ...extra,
    };
    context.window = context;
    // The page's own window is its parent at the top; on the website the
    // host page is, and that is where PlexoraStore and PlexoraPython live.
    context.window.parent = context;
    context.window.addEventListener = () => {};
    context.window.removeEventListener = () => {};
    context.window.location = { hash: "", pathname: "/settings", search: "" };
    context.window.history = { replaceState() {}, state: null };
    context.PlexoraPage = { register: (fn) => { context.pageInit = fn; } };
    createContext(context);
    runInContext(readFileSync(SOURCE, "utf-8"), context);
    context.pageInit();
    return { el: elementFor, sent, context };
}

function reply(body, status = 200) {
    return Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
}

// -- checks -----------------------------------------------------------------

const failures = [];
let passed = 0;
function check(label, condition, detail = "") {
    if (condition) {
        passed += 1;
        console.log(`ok  ${label}`);
    } else {
        failures.push(label);
        console.log(`FAIL ${label}${detail ? `  (${detail})` : ""}`);
    }
}

// -- the desktop app ----------------------------------------------------------

const DESKTOP_IDS = ["settings_panel_data", "settings_locations", "settings_locations_saving",
                     "settings_locations_env", "settings_locations_env_body", "settings_locations_list",
                     "settings_locations_message", "settings_locations_message_body",
                     "settings_locations_error", "settings_locations_error_body",
                     "settings_location_open", "settings_location_form", "settings_location_path",
                     "settings_location_add", "settings_location_hint", "settings_location_error",
                     "settings_location_error_body"];

function location(fields) {
    return { available: true, writable: true, is_default: false, workspace: null, rule: "", ...fields };
}

function desktopState(overrides = {}) {
    const locations = overrides.locations || [
        location({ id: "data-1", kind: "data_root", label: "Data folder", path: "/home/u/plexora",
                   project_count: 127, is_default: true, rule: "platform default" }),
        location({ id: "lib-1", kind: "library", label: "Documents/Plexora",
                   path: "/home/u/Documents/Plexora/projects", workspace: "/home/u/Documents/Plexora",
                   project_count: 2 }),
        location({ id: "old-1", kind: "legacy", label: "Documents/Plexora/Plexora",
                   path: "/home/u/Documents/Plexora/Plexora/projects",
                   workspace: "/home/u/Documents/Plexora/Plexora", project_count: 1 }),
        location({ id: "ws-1", kind: "workspace", label: "Imaging", path: "/data/Imaging/projects",
                   workspace: "/data/Imaging", project_count: 4 }),
        location({ id: "ws-2", kind: "workspace", label: "Gone", path: "/mnt/usb/Gone/projects",
                   workspace: "/mnt/usb/Gone", available: false, writable: false, project_count: null }),
        location({ id: "sh-1", kind: "shared", label: "lab", path: "/srv/lab", project_count: 9 }),
    ];
    const chosen = locations.find((one) => one.is_default) || locations[0];
    return { default_save: { id: chosen.id, label: chosen.label, path: chosen.path },
             locations, website: false, env_override: "", ...overrides };
}

function desktop({ state = desktopState(), answers = {}, confirm = "tidy" } = {}) {
    const asked = [];
    const page = load({
        ids: DESKTOP_IDS,
        respond: (method, path, body) => {
            if (method === "GET" && path === "/settings/locations") return reply(state);
            const key = `${method} ${path}`;
            if (answers[key]) return reply(...answers[key](body));
            // The Data section's reads: never answered.
            return new Promise(() => {});
        },
        extra: {
            PlexoraConfirm: {
                choose: (options) => { asked.push(options); return Promise.resolve(confirm); },
            },
        },
    });
    return { ...page, asked, list: () => page.el("settings_locations_list") };
}

{
    const page = desktop();
    await settled();
    const list = page.list();
    const rows = withClass(list, "plx-locations-row");
    check("each location is a row", rows.length === 6, String(rows.length));
    const data = rowFor(list, "Data folder");
    check("a row carries its path, its kind and its project count",
        withClass(data, "plx-locations-path")[0]?.textContent === "/home/u/plexora"
        && withClass(data, "plx-locations-kind")[0]?.textContent === "App data"
        && data.textContent.includes("127 projects"));
    check("a workspace row shows the folder picked, not its projects folder",
        withClass(rowFor(list, "Imaging"), "plx-locations-path")[0]?.textContent === "/data/Imaging");
    check("the default location is marked, and only it",
        data.classList.contains("is-default") && withClass(list, "plx-locations-default").length === 1);
    check("the head names where new projects are saved",
        page.el("settings_locations_saving").textContent === "New projects are saved in Data folder.");
    check("a location not mounted says so, and is not counted",
        rowFor(list, "Gone").textContent.includes("Not mounted")
        && !rowFor(list, "Gone").textContent.includes("project"));

    check("the default row offers no Make default",
        !button(data, "Make default"));
    const greyed = (label) => button(rowFor(list, label), "Make default");
    check("Make default is greyed, with the reason, for the older nested folder",
        greyed("Documents/Plexora/Plexora").getAttribute("aria-disabled") === "true"
        && /Tidy it up first/.test(greyed("Documents/Plexora/Plexora").title));
    check("for a shared folder",
        /read-only/.test(greyed("lab").title) && greyed("lab").getAttribute("aria-disabled") === "true");
    check("and for a folder that is not there",
        /not there right now/.test(greyed("Gone").title));
    check("and is offered plainly for a writable folder of your own",
        greyed("Imaging").getAttribute("aria-disabled") === null);

    check("Remove is offered for added workspaces only",
        Boolean(button(rowFor(list, "Imaging"), "Remove")) && Boolean(button(rowFor(list, "Gone"), "Remove"))
        && !button(rowFor(list, "Documents/Plexora"), "Remove") && !button(data, "Remove")
        && !button(rowFor(list, "lab"), "Remove"));
    const tidy = button(rowFor(list, "Documents/Plexora/Plexora"), "Tidy up…");
    check("Tidy up is offered for the older nested folder only",
        Boolean(tidy) && withClass(list, "plx-locations-row")
            .filter((row) => button(row, "Tidy up…")).length === 1);
    check("and is greyed while Documents/Plexora holds projects",
        tidy.getAttribute("aria-disabled") === "true" && /already holds projects/.test(tidy.title));

    const before = page.sent.length;
    await greyed("lab").click();
    check("pressing a greyed offer says why, in the card, and sends nothing",
        page.sent.length === before
        && page.el("settings_locations_message").hidden === false
        && page.el("settings_locations_message_body").textContent === "A shared folder is read-only here.");
}

{
    const next = desktopState();
    next.locations = next.locations.map((one) => ({ ...one, is_default: one.id === "ws-1" }));
    next.default_save = { id: "ws-1", label: "Imaging", path: "/data/Imaging/projects" };
    const page = desktop({ answers: { "POST /settings/locations/default": () => [next] } });
    await settled();
    await button(rowFor(page.list(), "Imaging"), "Make default").click();
    await settled();
    const post = page.sent.find((one) => one.method === "POST");
    check("Make default posts that location's id",
        post && post.path === "/settings/locations/default" && post.body.id === "ws-1");
    check("and the answer is drawn: the new default marked, the head moved",
        rowFor(page.list(), "Imaging").classList.contains("is-default")
        && page.el("settings_locations_saving").textContent === "New projects are saved in Imaging.");
    check("and says so in a sentence",
        page.el("settings_locations_message_body").textContent === "New projects are saved in Imaging from now on.");
}

{
    const page = desktop({ answers: { "POST /settings/locations/default":
        () => [{ error: "Plexora cannot write to Imaging." }, 400] } });
    await settled();
    await button(rowFor(page.list(), "Imaging"), "Make default").click();
    await settled();
    check("a refusal is shown as the route wrote it, under the list",
        page.el("settings_locations_error").hidden === false
        && page.el("settings_locations_error_body").textContent === "Plexora cannot write to Imaging."
        && rowFor(page.list(), "Data folder").classList.contains("is-default"));
}

{
    const page = desktop({ answers: { "DELETE /settings/locations/ws-1": () => [desktopState(), 200] } });
    await settled();
    await button(rowFor(page.list(), "Imaging"), "Remove").click();
    await settled();
    const sent = page.sent.find((one) => one.method === "DELETE");
    check("Remove sends a DELETE for that location and says nothing in it changed",
        sent && sent.path === "/settings/locations/ws-1"
        && /Nothing in it was changed/.test(page.el("settings_locations_message_body").textContent));
}

{
    const state = desktopState();
    state.locations = state.locations.filter((one) => one.id !== "lib-1");
    const page = desktop({ state, confirm: null });
    await settled();
    const tidy = button(rowFor(page.list(), "Documents/Plexora/Plexora"), "Tidy up…");
    check("with nothing in Documents/Plexora yet, Tidy up is offered",
        tidy.getAttribute("aria-disabled") === null);
    await tidy.click();
    await settled();
    const question = page.asked[0] || {};
    const said = walk(question.content).map((one) => one.textContent).join(" ");
    check("Tidy up asks first, naming each folder that moves and where to",
        /Tidy up/.test(question.title || "")
        && said.includes("/home/u/Documents/Plexora/Plexora/projects")
        && said.includes("/home/u/Documents/Plexora/projects")
        && said.includes("/home/u/Documents/Plexora/pyramids"));
    check("with the dialog's X as its only way out",
        question.close === true && (question.choices || []).length === 1);
    check("and dismissed, it sends nothing",
        !page.sent.some((one) => one.method === "POST"));
}

{
    const state = desktopState();
    state.locations = state.locations.filter((one) => one.id !== "lib-1");
    const after = desktopState();
    const page = desktop({ state, answers: { "POST /settings/locations/tidy": () => [{ ...after,
        tidied: { moved: ["projects", "pyramids/abc", "pyramids/def"], left: [],
                  location: { label: "Documents/Plexora" } } }] } });
    await settled();
    await button(rowFor(page.list(), "Documents/Plexora/Plexora"), "Tidy up…").click();
    await settled();
    const post = page.sent.find((one) => one.method === "POST");
    check("confirmed, Tidy up posts the older folder's id",
        post && post.path === "/settings/locations/tidy" && post.body.id === "old-1");
    check("and says what moved where",
        page.el("settings_locations_message_body").textContent
            === "Moved the projects and 2 pyramids into Documents/Plexora.");
}

{
    const added = location({ id: "ws-9", kind: "workspace", label: "Research", path: "/r/Research/projects",
                             workspace: "/r/Research", project_count: 3 });
    const page = desktop({ answers: {
        "POST /settings/locations": (body) => (body.path === "/r/Research"
            ? [{ ...desktopState(), location: added }]
            : [{ error: "/r/Nothing is not a Plexora workspace: it has no workspace.json." }, 400]),
    } });
    await settled();
    await page.el("settings_location_open").click();
    check("Add an existing Plexora workspace opens the field",
        page.el("settings_location_form").hidden === false
        && page.el("settings_location_open").getAttribute("aria-expanded") === "true");
    page.el("settings_location_path").value = "/r/Nothing";
    await page.el("settings_location_add").click();
    await settled();
    check("a refused folder is said under the field, as the route wrote it",
        page.el("settings_location_error").hidden === false
        && /not a Plexora workspace/.test(page.el("settings_location_error_body").textContent)
        && page.el("settings_locations_error").hidden !== false);
    page.el("settings_location_path").value = "/r/Research";
    await page.el("settings_location_path").key("Enter");
    await settled();
    check("Enter adds the typed folder, and the answer says what was found",
        page.sent.filter((one) => one.method === "POST").length === 2
        && page.el("settings_location_form").hidden === true
        && page.el("settings_locations_message_body").textContent === "Added Research: 3 projects.");
}

{
    const page = desktop({ state: desktopState({ env_override: "PLEXORA_LIBRARY_PATH" }) });
    await settled();
    check("with PLEXORA_LIBRARY_PATH set the card says so",
        page.el("settings_locations_env").hidden === false
        && /PLEXORA_LIBRARY_PATH/.test(page.el("settings_locations_env_body").textContent));
    check("and nothing can be added, removed or tidied up",
        page.el("settings_location_open").disabled === true
        && button(rowFor(page.list(), "Imaging"), "Remove").getAttribute("aria-disabled") === "true"
        && button(rowFor(page.list(), "Documents/Plexora/Plexora"), "Tidy up…").getAttribute("aria-disabled") === "true");
    check("while where new projects go can still be chosen",
        button(rowFor(page.list(), "Imaging"), "Make default").getAttribute("aria-disabled") === null);
}

// -- the website ----------------------------------------------------------------

const WEB_IDS = ["settings_panel_data", "settings_web_storage", "settings_web_place",
                 "settings_web_place_saving", "settings_web_locations", "settings_web_place_message",
                 "settings_web_place_message_body", "settings_web_place_error",
                 "settings_web_place_error_body", "settings_web_add_folder", "settings_web_setup_again"];

function website({ place, kept, listed = null, overrides = {} }) {
    const calls = [];
    const record = { kept: kept.slice() };
    const store = {
        current: () => place,
        canWriteFolders: () => true,
        locations: async () => record.kept.slice(),
        addLocation: async (options) => { calls.push(["addLocation", options || null]); return null; },
        removeLocation: async (id) => {
            calls.push(["removeLocation", id]);
            record.kept = record.kept.filter((one) => one.id !== id);
            return record.kept;
        },
        setDefault: async (id) => { calls.push(["setDefault", id]); return {}; },
        adoptWorkspace: async (handle, options) => { calls.push(["adoptWorkspace", handle.name, options]); return {}; },
        reconnect: async () => { calls.push(["reconnect"]); return { ok: true, label: "Documents › Plexora" }; },
        switchTo: async (...args) => { calls.push(["switchTo", ...args]); return null; },
        ...overrides,
    };
    const page = load({
        ids: WEB_IDS,
        respond: (method, path) => (path === "/settings/locations" && listed
            ? reply({ locations: listed }) : reply({ error: "not here" }, 404)),
        extra: {
            PlexoraStore: store,
            PlexoraPython: { libraries: async () => { calls.push(["libraries"]); return []; } },
            PlexoraOnboarding: { open: async (options) => { calls.push(["onboarding", options]); return "ready"; } },
        },
    });
    return { ...page, calls, list: () => page.el("settings_web_locations") };
}

const handle = (name) => ({ name, requestPermission: async () => "granted" });

{
    const page = website({
        place: { kind: "folder", chosen: true, label: "Documents › Plexora", desktopFinds: true },
        kept: [
            { id: "home-1", label: "Documents › Plexora", handle: handle("Plexora"), isDefault: true, permission: "granted" },
            { id: "a1b2c3", label: "Imaging", handle: handle("Imaging"), isDefault: false, permission: "granted" },
            { id: "d4e5f6", label: "Shared drive", handle: handle("Shared drive"), isDefault: false, permission: "prompt" },
        ],
        listed: [{ id: "data_root-x", kind: "data_root", project_count: 5 },
                 { id: "a1b2c3", kind: "workspace", project_count: 2 }],
    });
    await settled();
    const list = page.list();
    const rows = withClass(list, "plx-locations-row");
    check("the website's default folder is the first row, marked default, with its count",
        rows.length === 3 && rows[0].classList.contains("is-default")
        && rows[0].textContent.includes("Documents › Plexora") && rows[0].textContent.includes("5 projects"));
    check("an added folder Python has mounted shows its count",
        rowFor(list, "Imaging").textContent.includes("Connected")
        && rowFor(list, "Imaging").textContent.includes("2 projects"));
    check("a folder the browser has not allowed this visit offers Reconnect",
        rowFor(list, "Shared drive").textContent.includes("Not connected")
        && Boolean(button(rowFor(list, "Shared drive"), "Reconnect"))
        && !button(rowFor(list, "Imaging"), "Reconnect"));
    check("the default folder offers neither Make default nor Remove",
        !button(rows[0], "Make default") && !button(rows[0], "Remove"));
    check("Add a folder… and Set up again are offered",
        page.el("settings_web_add_folder").hidden === false
        && page.el("settings_web_setup_again").hidden === false);

    await button(rowFor(list, "Imaging"), "Make default").click();
    await settled();
    check("Make default asks the store, with no reload, then remounts the folders",
        JSON.stringify(page.calls.slice(0, 2)) === JSON.stringify([["setDefault", "a1b2c3"], ["libraries"]]),
        JSON.stringify(page.calls));

    page.calls.length = 0;
    await button(rowFor(page.list(), "Imaging"), "Remove").click();
    await settled();
    check("Remove forgets the folder, then remounts, and says nothing in it changed",
        JSON.stringify(page.calls.slice(0, 2)) === JSON.stringify([["removeLocation", "a1b2c3"], ["libraries"]])
        && /Nothing in the folder was changed/.test(page.el("settings_web_place_message_body").textContent));

    page.calls.length = 0;
    await page.el("settings_web_setup_again").click();
    await settled();
    check("Set up again opens the onboarding",
        page.calls[0] && page.calls[0][0] === "onboarding" && page.calls[0][1].reason === "settings");

    page.calls.length = 0;
    await page.el("settings_web_add_folder").click();
    await settled();
    check("Add a folder… goes straight to the store's folder dialog",
        page.calls[0] && page.calls[0][0] === "addLocation" && page.calls[0][1] === null);
}

{
    const page = website({
        place: { kind: "browser", chosen: true, onboarded: true },
        kept: [{ id: "a1b2c3", label: "Imaging", handle: handle("Imaging"), isDefault: false, permission: "granted" }],
    });
    await settled();
    const rows = withClass(page.list(), "plx-locations-row");
    check("with this browser as the default, its row says so and offers a folder",
        rows[0].textContent.includes("This browser") && Boolean(button(rows[0], "Save on this computer…"))
        && page.el("settings_web_place_saving").textContent === "New work is saved in this browser.");
    await button(rowFor(page.list(), "Imaging"), "Make default").click();
    await settled();
    check("and Make default brings this browser's projects along into that folder",
        JSON.stringify(page.calls.slice(0, 3)) === JSON.stringify([
            ["adoptWorkspace", "Imaging", { init: true }], ["removeLocation", "a1b2c3"], ["libraries"]]),
        JSON.stringify(page.calls));
}

{
    const page = website({
        place: { kind: "browser", chosen: true, notOpened: "Documents › Plexora", needsPermission: true },
        kept: [{ id: "home-1", label: "Documents › Plexora", handle: handle("Plexora"), isDefault: true, permission: "prompt" }],
    });
    await settled();
    const first = withClass(page.list(), "plx-locations-row")[0];
    check("a default folder not opened this visit says so",
        first.classList.contains("is-default") && first.textContent.includes("Not connected")
        && /permission again/.test(first.textContent));
    await button(first, "Reconnect").click();
    await settled();
    check("and reconnects in place, with no reload",
        page.calls[0] && page.calls[0][0] === "reconnect"
        && /open again/.test(page.el("settings_web_place_message_body").textContent));
}

{
    const page = website({
        place: { kind: "folder", chosen: true, label: "Documents › Plexora" },
        kept: [{ id: "home-1", label: "Documents › Plexora", handle: handle("Plexora"), isDefault: true, permission: "granted" },
               { id: "a1b2c3", label: "Imaging", handle: handle("Imaging"), isDefault: false, permission: "granted" }],
        overrides: {
            setDefault: async () => {
                const error = new Error("The browser did not allow Plexora to change “Imaging”.");
                error.code = "denied";
                throw error;
            },
        },
    });
    await settled();
    await button(rowFor(page.list(), "Imaging"), "Make default").click();
    await settled();
    check("a store's refusal is shown in plain words",
        page.el("settings_web_place_error").hidden === false
        && page.el("settings_web_place_error_body").textContent
            === "The browser did not allow Plexora to change “Imaging”.");
}

console.log(`\n${failures.length ? `FAILURES: ${failures.join("; ")}` : `${passed} checks passed`}`);
process.exit(failures.length ? 1 : 0);
