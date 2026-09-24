/**
 * The small action menu (views/popoverMenu.js) and the plugin help content
 * (views/pluginHelp.js), both run for real against a DOM stand-in.
 *
 * The menu's decisions that fail silently: a click on the opening button that
 * immediately shuts the menu it opened, a disabled item that still acts, an
 * Escape that also deselects whatever the tool below had selected, a menu left
 * in the portal after it closed. The help's: a plugin string that reaches the
 * page as markup, and a chord printed the one way on every platform.
 *
 * Run directly: `node tests/js/popover_menu_probe.mjs`
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const MENU = join(REPO, "plexora/client/src/js/views/popoverMenu.js");
const HELP = join(REPO, "plexora/client/src/js/views/pluginHelp.js");

function node(tag) {
    const el = {
        tagName: tag.toUpperCase(),
        className: "",
        children: [],
        attributes: {},
        style: {},
        listeners: {},
        disabled: false,
        textContent: "",
        parent: null,
        offsetWidth: 180,
        offsetHeight: 120,
        focused: false,
        appendChild(child) { child.parent = this; this.children.push(child); return child; },
        setAttribute(name, value) { this.attributes[name] = String(value); },
        getAttribute(name) { return this.attributes[name]; },
        addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); },
        contains(other) {
            for (let at = other; at; at = at.parent) if (at === this) return true;
            return false;
        },
        focus() { this.focused = true; },
        querySelector(selector) {
            // Enough for ".plx-menu-item:not([disabled])".
            const wantEnabled = selector.includes(":not([disabled])");
            const cls = selector.replace(/:not\(\[disabled\]\)/, "").replace(/^\./, "");
            const walk = (n) => {
                for (const child of n.children) {
                    if (String(child.className).split(" ").includes(cls)
                        && (!wantEnabled || !child.disabled)) return child;
                    const hit = walk(child);
                    if (hit) return hit;
                }
                return null;
            };
            return walk(this);
        },
        click() {
            for (const fn of this.listeners.click || []) fn({ stopPropagation() {} });
        },
    };
    return el;
}

function bootMenu({ anchorBox = { top: 100, bottom: 120, left: 300, right: 330 },
                    viewport = { width: 1200, height: 800 } } = {}) {
    const docListeners = {};
    const winListeners = {};
    const timers = [];
    const attached = new Set();
    const context = {
        console, JSON, Math, String, Number, Boolean,
        innerWidth: viewport.width,
        innerHeight: viewport.height,
        setTimeout: (fn) => { timers.push(fn); return timers.length; },
        clearTimeout: () => {},
        addEventListener: (name, fn) => { (winListeners[name] ||= []).push(fn); },
        removeEventListener: (name, fn) => {
            winListeners[name] = (winListeners[name] || []).filter((f) => f !== fn);
        },
        document: {
            createElement: node,
            documentElement: node("html"),
            addEventListener: (name, fn) => { (docListeners[name] ||= []).push(fn); },
            removeEventListener: (name, fn) => {
                docListeners[name] = (docListeners[name] || []).filter((f) => f !== fn);
            },
        },
    };
    context.window = context;
    createContext(context);
    // PopoverPortal is a top-level const in the page, so it is bare here too.
    runInContext(`const PopoverPortal = {
        attach: (el) => __attached.add(el), detach: (el) => __attached.delete(el) };`
        + readFileSync(MENU, "utf8"), Object.assign(context, { __attached: attached }));
    const anchor = node("button");
    anchor.getBoundingClientRect = () => anchorBox;
    return {
        menu: context.PlexoraMenu, anchor, attached, docListeners, winListeners,
        tick: () => timers.splice(0).forEach((fn) => fn()),
        fire: (name, event) => (docListeners[name] || []).slice().forEach((fn) => fn(event)),
    };
}

const onlyMenu = (attached) => [...attached][0];
const items = (menuEl) => menuEl.children;

const passed = [];
function check(label, fn) {
    fn();
    passed.push(label);
    console.log(`PASS ${label}`);
}

// ---------------------------------------------------------------- menu

check("a menu of items and a separator, with their roles", () => {
    const t = bootMenu();
    t.menu.open(t.anchor, [
        { label: "Copy" }, { separator: true }, { label: "Paste", disabled: true },
    ]);
    const el = onlyMenu(t.attached);
    assert.equal(el.attributes.role, "menu");
    const [copy, line, paste] = items(el);
    assert.equal(copy.attributes.role, "menuitem");
    assert.equal(copy.textContent, "Copy");
    assert.equal(line.attributes.role, "separator");
    assert.equal(paste.disabled, true);
    assert.equal(copy.focused, true, "the first enabled item has focus");
});

check("it hangs under its button, right-aligned, and says it is open", () => {
    const t = bootMenu();
    t.menu.open(t.anchor, [{ label: "Copy" }]);
    const el = onlyMenu(t.attached);
    assert.equal(el.style.position, "fixed");
    assert.equal(el.style.top, "124px");
    assert.equal(el.style.left, `${330 - 180}px`);
    assert.equal(t.anchor.attributes["aria-expanded"], "true");
});

check("it stays on screen and flips above when there is no room below", () => {
    const t = bootMenu({ anchorBox: { top: 740, bottom: 760, left: 2, right: 20 } });
    t.menu.open(t.anchor, [{ label: "Copy" }]);
    const el = onlyMenu(t.attached);
    assert.equal(el.style.left, "8px", "clamped to the left edge");
    assert.equal(el.style.top, `${740 - 4 - 120}px`);
});

check("choosing an item closes the menu, then runs it", () => {
    const t = bootMenu();
    const order = [];
    t.menu.open(t.anchor, [{ label: "Copy", onSelect: () => order.push(t.menu.isOpen()) }]);
    items(onlyMenu(t.attached))[0].click();
    assert.deepEqual(order, [false]);
    assert.equal(t.attached.size, 0, "out of the portal");
    assert.equal(t.anchor.attributes["aria-expanded"], "false");
});

check("a disabled item does nothing", () => {
    const t = bootMenu();
    let ran = false;
    t.menu.open(t.anchor, [{ label: "Paste", disabled: true, onSelect: () => { ran = true; } }]);
    items(onlyMenu(t.attached))[0].click();
    assert.equal(ran, false);
    assert.equal(t.menu.isOpen(), true);
});

check("the opening click does not shut it; the next one does", () => {
    const t = bootMenu();
    t.menu.open(t.anchor, [{ label: "Copy" }]);
    t.fire("click", {});
    assert.equal(t.menu.isOpen(), true, "not listening yet");
    t.tick();
    t.fire("click", {});
    assert.equal(t.menu.isOpen(), false);
});

check("Escape shuts it and goes no further", () => {
    const t = bootMenu();
    t.menu.open(t.anchor, [{ label: "Copy" }]);
    let stopped = false;
    t.fire("keydown", { key: "Escape", stopPropagation: () => { stopped = true; } });
    assert.equal(t.menu.isOpen(), false);
    assert.equal(stopped, true);
    assert.equal((t.docListeners.keydown || []).length, 0, "listener removed");
});

check("one menu at a time", () => {
    const t = bootMenu();
    const other = node("button");
    other.getBoundingClientRect = () => ({ top: 200, bottom: 220, left: 300, right: 330 });
    t.menu.open(t.anchor, [{ label: "A" }]);
    t.menu.open(other, [{ label: "B" }]);
    assert.equal(t.attached.size, 1);
    assert.equal(items(onlyMenu(t.attached))[0].textContent, "B");
    assert.equal(t.anchor.attributes["aria-expanded"], "false", "the first button is told");
    assert.equal(other.attributes["aria-expanded"], "true");
});

// The Image card's `•••` stops its click from propagating (the header would
// fold otherwise), so the document listener never hears the second press:
// the menu itself has to recognise the button that opened it.
check("a second click on the open anchor closes it", () => {
    const t = bootMenu();
    t.menu.open(t.anchor, [{ label: "A" }]);
    t.tick();
    t.menu.open(t.anchor, [{ label: "A" }]);
    assert.equal(t.menu.isOpen(), false);
    assert.equal(t.attached.size, 0, "out of the portal");
    assert.equal(t.anchor.attributes["aria-expanded"], "false");
    t.menu.open(t.anchor, [{ label: "A" }]);
    assert.equal(t.menu.isOpen(), true, "and the third opens it again");
});

check("a row of icon actions carries its label as text and runs the one clicked", () => {
    const t = bootMenu();
    const ran = [];
    t.menu.open(t.anchor, [
        { label: "Channel <names>", actions: [
            { icon: "fas fa-copy", title: "Copy channel names",
              onSelect: () => ran.push(["copy", t.menu.isOpen()]) },
            { icon: "fas fa-paste", title: "Paste channel names",
              onSelect: () => ran.push(["paste", t.menu.isOpen()]) },
        ] },
    ]);
    const [row] = items(onlyMenu(t.attached));
    assert.equal(row.attributes.role, "group");
    assert.equal(row.attributes["aria-label"], "Channel <names>");
    const [label, actions] = row.children;
    assert.equal(label.textContent, "Channel <names>", "text, never markup");
    const [copy, paste] = actions.children;
    for (const [button, title] of [[copy, "Copy channel names"], [paste, "Paste channel names"]]) {
        assert.equal(button.attributes.role, "menuitem");
        assert.equal(button.title, title, "the sentence is the tooltip");
        assert.equal(button.attributes["aria-label"], title, "and the accessible name");
        assert.equal(button.textContent, "", "a glyph, with no word beside it");
    }
    assert.equal(copy.children[0].className, "fas fa-copy");
    assert.equal(copy.focused, true, "the first enabled action has focus");
    paste.click();
    assert.deepEqual(ran, [["paste", false]], "closed first, then run");
});

check("a disabled action does nothing", () => {
    const t = bootMenu();
    let ran = false;
    t.menu.open(t.anchor, [
        { label: "Rendering", actions: [
            { icon: "fas fa-paste", title: "Paste", disabled: true, onSelect: () => { ran = true; } },
        ] },
    ]);
    const paste = items(onlyMenu(t.attached))[0].children[1].children[0];
    assert.equal(paste.disabled, true);
    paste.click();
    assert.equal(ran, false);
    assert.equal(t.menu.isOpen(), true);
});

check("a label is text, never markup", () => {
    const t = bootMenu();
    t.menu.open(t.anchor, [{ label: "<b>x</b>" }]);
    const item = items(onlyMenu(t.attached))[0];
    assert.equal(item.textContent, "<b>x</b>");
    assert.equal(item.innerHTML, undefined);
});

// ---------------------------------------------------------------- help

function bootHelp({ mac = true } = {}) {
    let told = null;
    const context = { console, String, Array };
    context.window = context;
    context.document = { createElement: node };
    context.PlexoraShortcuts = {
        normalize: (spec) => (String(spec).includes("+") ? String(spec).toLowerCase() : ""),
        format: (spec) => (mac ? "⌘" : "Ctrl+") + String(spec).split("+").pop().toUpperCase(),
    };
    context.PlexoraConfirm = { tell: (options) => { told = options; return Promise.resolve(true); } };
    createContext(context);
    runInContext(readFileSync(HELP, "utf8"), context);
    return { help: context.PlexoraPluginHelp, told: () => told };
}

function rowsOf(content) {
    const table = content.children.find((c) => c.className === "plx-tool-help-shortcuts");
    return table.children[0].children.map((tr) => ({
        keys: tr.children[0].children.map((kbd) => kbd.textContent),
        label: tr.children[1].textContent,
    }));
}

check("help lists the plugin's keys and then core's open/close row", () => {
    const { help } = bootHelp();
    const content = help._content({
        help: { summary: "s", shortcuts: [{ keys: "z", label: "Previous" }, { keys: "X", label: "Next" }] },
        openKey: "⌘B", label: "Thresholding",
    });
    assert.deepEqual(rowsOf(content), [
        { keys: ["Z"], label: "Previous" },
        { keys: ["X"], label: "Next" },
        { keys: ["⌘B"], label: "Open or close Thresholding" },
    ]);
});

check("a chord is printed per platform, and several keys share a row", () => {
    const mac = bootHelp({ mac: true }).help._content({
        help: { summary: "s", shortcuts: [{ keys: "mod+z", label: "Undo" },
                                           { keys: ["←", "→"], label: "Fold" }] } });
    assert.deepEqual(rowsOf(mac)[0].keys, ["⌘Z"]);
    assert.deepEqual(rowsOf(mac)[1].keys, ["←", "→"]);
    const pc = bootHelp({ mac: false }).help._content({
        help: { summary: "s", shortcuts: [{ keys: "mod+z", label: "Undo" }] } });
    assert.deepEqual(rowsOf(pc)[0].keys, ["Ctrl+Z"]);
});

check("notes are text bullets, and a tool with nothing extra gets no block", () => {
    const { help } = bootHelp();
    const content = help._content({ help: { summary: "s", notes: ["<i>one</i>", " "] } });
    const list = content.children.find((c) => c.className === "plx-tool-help-notes");
    assert.equal(list.children.length, 1);
    assert.equal(list.children[0].textContent, "<i>one</i>");
    assert.equal(help._content({ help: { summary: "s" } }), null);
});

check("open hands the summary and the table to the confirm dialog", async () => {
    const t = bootHelp();
    t.help.open({ name: "gating", label: "Thresholding",
                  help: { summary: "Gate one marker." }, openKey: "⌘B" });
    assert.equal(t.told().title, "Thresholding");
    assert.equal(t.told().body, "Gate one marker.");
    assert.equal(t.told().confirm, "Close");
    assert.ok(t.told().content, "the open/close row alone still earns a table");
    const none = bootHelp();
    none.help.open({ name: "x", help: {} });
    assert.equal(none.told(), null, "no summary, no dialog");
});

console.log(`all checks passed (${passed.length})`);
