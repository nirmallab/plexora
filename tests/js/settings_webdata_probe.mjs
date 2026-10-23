/**
 * Settings > Web data: the store list shows a few, not all of them.
 *
 * A cache in use for a while holds dozens of stores, and every one of them
 * used to be a card -- a page of cards nobody scrolls. The list now draws the
 * three most recently opened (the server's order) and a button for more:
 *
 *   1. **Three, then a count and "Show N more"**, ten at a time.
 *   2. **What was opened stays open through a refresh.** The section polls
 *      every two seconds while a store fills; a poll that folded the list
 *      back to three would undo the press somebody just made.
 *   3. **"Show fewer" folds it back**, and a short list has no footer at all.
 *
 * Empty stores are left out by the server (remote_sources.usage), so they are
 * not this probe's business -- see tests/test_settings_webdata_page.py.
 *
 * Run directly:  node tests/js/settings_webdata_probe.mjs
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
    const classes = new Set();
    const listeners = new Map();
    const element = {
        tagName: String(tag).toUpperCase(),
        type: "",
        value: "",
        get textContent() {
            return own + element.children.map((child) => child.textContent).join("");
        },
        set textContent(value) {
            own = value === null || value === undefined ? "" : String(value);
            element.children = [];
        },
        hidden: false,
        disabled: false,
        children: [],
        dataset: {},
        style: {},
        get className() { return Array.from(classes).join(" "); },
        set className(value) {
            classes.clear();
            String(value).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
        },
        classList: {
            add: (...n) => n.forEach((x) => classes.add(x)),
            remove: (...n) => n.forEach((x) => classes.delete(x)),
            toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
            contains: (name) => classes.has(name),
        },
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

function walk(root, out = []) {
    (root.children || []).forEach((child) => {
        out.push(child);
        walk(child, out);
    });
    return out;
}

function buttonSaying(root, text) {
    return walk(root).find(
        (n) => n.tagName === "BUTTON" && n.textContent === text) || null;
}

// -- the page's own elements, by id -----------------------------------------
//
// Only Web data's ids resolve (and the data panel, which is what tells the
// page it is the Settings page at all). Every other section finds no markup
// and does not start.

const byId = new Map();

function elementFor(id) {
    if (id !== "settings_panel_data" && !String(id).startsWith("settings_webdata")
            && id !== "settings_panel_webdata") return null;
    if (!byId.has(id)) byId.set(id, makeElement("div"));
    return byId.get(id);
}

//: The rail's Web data tab: clicking it is the section's own "refresh now".
const tab = makeElement("button");

// -- the server --------------------------------------------------------------

let stores = [];

function store(i) {
    return { id: `s${i}`, url: `https://example.org/s${i}.zarr`, orphaned: false,
             pinned: false, last_opened_at: null, bytes: 1024 * (i + 1), entries: 1,
             projects: [], job: null };
}

function respond(url) {
    if (String(url).indexOf("settings/webdata") < 0) {
        // The Data section's reads. Never answered, so nothing it would draw
        // from them can throw from inside a promise nobody is holding.
        return new Promise(() => {});
    }
    return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({
            cache_dir: "/data/.remote_cache", env_override: false,
            budget_bytes: 20 * 1024 ** 3, used_bytes: 0, pinned_bytes: 0,
            support: {}, options: [], stores: stores.slice(),
        }),
    });
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
        querySelector: (selector) =>
            (selector === '.settings-tab[data-section="webdata"]' ? tab : null),
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

const list = () => elementFor("settings_webdata_stores");
const cards = () => list().children.filter(
    (n) => n.classList.contains("settings-webdata-store-card"));
const footer = () => list().children.find(
    (n) => n.classList.contains("settings-webdata-stores-more")) || null;

async function main() {
    stores = Array.from({ length: 14 }, (_, i) => store(i));
    context.pageInit();
    await settle();

    // -- 1. three, then more ---------------------------------------------------
    check("fourteen stores draw three cards",
          cards().length === 3);
    check("...the three the server put first",
          cards().map((c) => walk(c).find((n) => n.classList.contains("settings-path"))
                                   .textContent).join(",")
              === "https://example.org/s0.zarr,https://example.org/s1.zarr,"
                  + "https://example.org/s2.zarr");
    check("...with a count saying how many there are",
          Boolean(footer()) && footer().textContent.indexOf("3 of 14") === 0);
    check("...and a button for ten more",
          Boolean(buttonSaying(footer(), "Show 10 more")));
    check("...and nothing to fold while nothing is unfolded",
          buttonSaying(footer(), "Show fewer") === null);

    buttonSaying(footer(), "Show 10 more").click();
    check("pressing it draws ten more",
          cards().length === 13 && footer().textContent.indexOf("13 of 14") === 0);
    check("...offers exactly what is left",
          Boolean(buttonSaying(footer(), "Show 1 more")));
    check("...and offers to fold the list again",
          Boolean(buttonSaying(footer(), "Show fewer")));

    // -- 2. a refresh keeps it open ------------------------------------------------
    tab.click();
    await settle();
    check("a refresh redraws the list without folding it",
          cards().length === 13);

    buttonSaying(footer(), "Show 1 more").click();
    check("the last press draws the rest",
          cards().length === 14 && footer().textContent.indexOf("14 of 14") === 0);
    check("...and offers no more once there is none",
          !walk(footer()).some((n) => n.tagName === "BUTTON"
                                      && /^Show \d+ more$/.test(n.textContent)));

    // -- 3. folding, and a short list -----------------------------------------------
    buttonSaying(footer(), "Show fewer").click();
    check("Show fewer goes back to three",
          cards().length === 3 && Boolean(buttonSaying(footer(), "Show 10 more")));

    stores = Array.from({ length: 3 }, (_, i) => store(i));
    tab.click();
    await settle();
    check("three stores are three cards, with no footer under them",
          cards().length === 3 && footer() === null);

    stores = [];
    tab.click();
    await settle();
    check("no stores is the empty note",
          cards().length === 0 && footer() === null
          && list().textContent.indexOf("Nothing cached") === 0);

    console.log(failures.length
        ? `\n${failures.length} failed`
        : "\nall settings-webdata checks passed");
    if (failures.length) process.exitCode = 1;
}

main();
