/**
 * What a declared shortcut prints, and when it fires.
 *
 * keyboardShortcuts.js drives BOTH halves from one `data-shortcut` attribute:
 * the key printed into the menu row, and the binding that runs it. That is the
 * whole point of the design -- a hand-maintained accelerator table drifts, and
 * a menu that advertises ⌘E while the handler listens for ⌘R is worse than no
 * menu at all -- so the probe checks the two halves agree, against the real
 * shipped file rather than a description of it.
 *
 * The four things that are easy to get wrong and impossible to see in review:
 *
 *   - `mod` is Cmd on a Mac and Ctrl everywhere else, in BOTH the printed form
 *     and the match. A Mac reading "Ctrl+E" is as broken as a Mac where ⌘E does
 *     nothing.
 *   - Ctrl+E on a Mac must NOT fire the ⌘E binding. It is a text-editing
 *     binding there (move to end of line), and stealing it breaks every field.
 *   - A keystroke typed into an input is typing, not a command.
 *   - A disabled row still swallows the browser's default. Suppressing it only
 *     when the row happens to be enabled would make ⌘O open a file dialog on
 *     exactly the pages where the menu item is greyed out.
 *
 * Run twice over the same file, once per platform, because the platform is read
 * once at load time from `navigator` and cannot be changed afterwards.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/keyboardShortcuts.js");

/** A menu row: enough of an element for register(), paintKey() and click(). */
function makeRow(spec, { label = "Row", disabled = false } = {}) {
    const key = {
        className: "nav-item-key", textContent: "",
        matches: (sel) => sel === ".nav-item-key",
    };
    const row = {
        tagName: "A",
        clicks: 0,
        disabled,
        textContent: label,
        classList: {
            _set: new Set(disabled ? ["disabled"] : []),
            contains(name) { return this._set.has(name); },
        },
        attributes: { "data-shortcut": spec },
        getAttribute(name) { return this.attributes[name] ?? null; },
        setAttribute(name, value) { this.attributes[name] = value; },
        // The row has a key span already; no input, so isEnabled falls through
        // to the class/disabled checks.
        querySelector(sel) {
            if (sel === ".nav-item-key") return key;
            if (sel === ".nav-item-label") return { textContent: label };
            return null;
        },
        appendChild() {},
        click() { this.clicks += 1; },
    };
    row.key = key;
    return row;
}

/**
 * Load the real file with `navigator.platform` set, and return its export plus
 * the captured document-level keydown listener.
 */
function load(platform, rows) {
    let keydown = null;
    const listeners = {};
    const document = {
        readyState: "complete",
        activeElement: null,
        addEventListener(type, fn) {
            if (type === "keydown") keydown = fn;
            listeners[type] = fn;
        },
        contains: () => true,
        querySelectorAll(sel) {
            if (sel === "[data-shortcut]") return rows;
            return [];
        },
        createElement: () => ({ className: "", textContent: "" }),
    };
    const sandbox = {
        window: {},
        navigator: { platform, userAgentData: undefined },
        document,
        console: { warn() {}, error() {} },
    };
    sandbox.globalThis = sandbox;
    createContext(sandbox);
    runInContext(readFileSync(SOURCE, "utf8"), sandbox, { filename: SOURCE });
    return { api: sandbox.window.PlexoraShortcuts, keydown, document };
}

function press(keydown, { key, meta = false, ctrl = false, shift = false, alt = false }) {
    let prevented = false;
    keydown({
        key,
        metaKey: meta, ctrlKey: ctrl, shiftKey: shift, altKey: alt,
        preventDefault() { prevented = true; },
    });
    return prevented;
}

const say = (line) => console.log(line);

// ---------------------------------------------------------------- macOS ----
{
    const row = makeRow("mod+e");
    const { api, keydown, document } = load("MacIntel", [row]);

    assert.equal(api.isMac(), true);
    assert.equal(api.format("mod+e"), "⌘E");
    assert.equal(api.format("mod+shift+z"), "⇧⌘Z", "modifiers print in a fixed order");
    assert.equal(api.format("mod+,"), "⌘,");
    assert.equal(row.key.textContent, "⌘E",
        "the row prints exactly what the binding listens for");
    say("mac prints the glyph form and paints it into the row");

    assert.equal(press(keydown, { key: "e", meta: true }), true);
    assert.equal(row.clicks, 1, "cmd+E runs the row");
    say("mac fires on cmd and swallows the browser default");

    press(keydown, { key: "e", ctrl: true });
    assert.equal(row.clicks, 1, "ctrl+E on a Mac is a text binding, not this one");
    press(keydown, { key: "e", meta: true, ctrl: true });
    assert.equal(row.clicks, 1, "the wrong extra modifier disqualifies the stroke");
    press(keydown, { key: "e", meta: true, shift: true });
    assert.equal(row.clicks, 1, "cmd+shift+E is a different chord");
    say("mac leaves ctrl, cmd+ctrl and cmd+shift alone");

    document.activeElement = { tagName: "INPUT", isContentEditable: false };
    press(keydown, { key: "e", meta: true });
    assert.equal(row.clicks, 1, "a keystroke in a field is typing, not a command");
    document.activeElement = { tagName: "DIV", isContentEditable: true };
    press(keydown, { key: "e", meta: true });
    assert.equal(row.clicks, 1, "contenteditable counts as typing too");
    document.activeElement = null;
    say("mac ignores strokes aimed at a text field");
}

// -------------------------------------------------------------- Windows ----
{
    const row = makeRow("mod+e");
    const { api, keydown } = load("Win32", [row]);

    assert.equal(api.isMac(), false);
    assert.equal(api.format("mod+e"), "Ctrl+E");
    assert.equal(api.format("mod+shift+z"), "Ctrl+Shift+Z");
    assert.equal(row.key.textContent, "Ctrl+E");
    say("windows prints the word form and paints it into the row");

    assert.equal(press(keydown, { key: "E", ctrl: true }), true,
        "the key arrives uppercased when shift is not held on some layouts");
    assert.equal(row.clicks, 1);
    press(keydown, { key: "e", meta: true });
    assert.equal(row.clicks, 1, "the Windows key is not Ctrl");
    say("windows fires on ctrl and ignores meta");
}

// ------------------------------------------------------- disabled + clash ----
{
    const row = makeRow("mod+o", { label: "Open Project…", disabled: true });
    const { keydown } = load("Win32", [row]);

    assert.equal(press(keydown, { key: "o", ctrl: true }), true,
        "a disabled row still suppresses the browser's own Ctrl+O");
    assert.equal(row.clicks, 0, "...but does not run");
    say("a disabled row swallows the default without acting");
}

{
    const first = makeRow("mod+e", { label: "First" });
    const second = makeRow("mod+e", { label: "Second" });
    const { keydown } = load("Win32", [first, second]);

    press(keydown, { key: "e", ctrl: true });
    assert.equal(first.clicks, 1, "the first registration wins");
    assert.equal(second.clicks, 0);
    assert.equal(second.key.textContent, "",
        "the loser prints no key, rather than advertising one that does nothing");
    say("a clash resolves to the first row and the loser prints nothing");
}

// ---------------------------------------------------------- normalisation ----
{
    const { api } = load("Win32", []);
    assert.equal(api.normalize("MOD+E"), "mod+e");
    assert.equal(api.normalize("alt+shift+k"), "shift+alt+k",
        "same canonical order as the server's normalize_shortcut");
    assert.equal(api.normalize("e"), "", "a bare key is not a shortcut");
    assert.equal(api.normalize("mod+ctrl+e"), "", "ctrl is spelled mod");
    assert.equal(api.format("nonsense"), "");
    say("specs normalise the same way the server normalises them");
}

console.log("nav shortcuts probe passed");
