/**
 * Browsing a machine that has no desktop, which is every cluster.
 *
 * The picker is the only way to find a file on a compute node -- the native
 * dialog needs a screen and there is not one -- so the things that break here
 * break silently and leave a user typing paths from memory again:
 *
 *   1. **Every path it navigates to came from the server.** Rows carry
 *      `entry.path`, breadcrumbs carry theirs, Up uses `payload.parent`. The
 *      version that joined names with "/" was correct until the node on the
 *      other end was a Windows box, and then Up from `C:\data` went to "".
 *   2. **A listing that fails changes nothing.** `state.here` is assigned in
 *      exactly one place, from a server answer. Being thrown back to your home
 *      directory for one mistyped folder is worse than the mistake.
 *   3. **Remembering places may not block browsing.** /picker_prefs is a
 *      convenience; a picker that will not open because a preferences file
 *      could not be read is a much worse failure than one with no Recent list.
 *   4. **Esc inside a text box does not close the modal.** The browser's own
 *      dialog-cancel fires on Escape, so clearing a filter took the whole
 *      picker with it unless the keydown is stopped.
 *   5. **What is chosen is what the caller asked for**: one path, or an array
 *      when `multiple` is set, in the order the rows are drawn.
 *   6. **Mode "any" takes either kind.** One button on every field opens this,
 *      without first asking whether the user has a file or a .zarr store --
 *      so a store row selects like a file, an ordinary folder still opens, and
 *      there is still a way into a store and a way to choose one under a name
 *      the rule does not recognise.
 *
 * Run directly:  node tests/js/path_picker_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/pathPicker.js");

// -- a DOM small enough to read ---------------------------------------------

function makeElement(tag) {
    const classes = new Set();
    const attributes = new Map();
    const listeners = new Map();
    const element = {
        tagName: String(tag).toUpperCase(),
        type: "",
        value: "",
        textContent: "",
        hidden: false,
        disabled: false,
        checked: false,
        open: false,
        children: [],
        parentNode: null,
        dataset: {},
        get className() { return Array.from(classes).join(" "); },
        set className(value) {
            classes.clear();
            String(value).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
        },
        classList: {
            add: (...names) => names.forEach((n) => classes.add(n)),
            remove: (...names) => names.forEach((n) => classes.delete(n)),
            toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
            contains: (name) => classes.has(name),
        },
        setAttribute(name, value) { attributes.set(name, String(value)); },
        getAttribute(name) {
            return attributes.has(name) ? attributes.get(name) : null;
        },
        removeAttribute(name) { attributes.delete(name); },
        appendChild(child) {
            child.parentNode = element;
            element.children.push(child);
            return child;
        },
        append(...nodes) { nodes.forEach((n) => element.appendChild(n)); },
        //: The picker redraws the listing, the crumb bar and the sidebar from
        //: each server answer rather than patching them, so this is the single
        //: most exercised DOM call in the file.
        replaceChildren(...nodes) {
            element.children.forEach((child) => { child.parentNode = null; });
            element.children = [];
            nodes.forEach((n) => element.appendChild(n));
        },
        remove() {
            const parent = element.parentNode;
            if (!parent) return;
            parent.children = parent.children.filter((c) => c !== element);
            element.parentNode = null;
        },
        addEventListener(type, handler) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(handler);
        },
        dispatchEvent(event) {
            (listeners.get(event.type) || []).forEach((h) => h(event));
            return true;
        },
        //: No layout here, and nothing that needs one.
        focus() { element.focused = true; },
        scrollIntoView() {},
        focused: false,
        //: The path box selects its contents when it opens, so that a pasted
        //: path replaces the one already there instead of landing inside it.
        select() { element.selected = true; },
        selected: false,
        //: <dialog>. `close` is what resolves pick(), Esc included -- which is
        //: why the shipped file hangs its whole exit path on that one event.
        showModal() { element.open = true; },
        close() {
            element.open = false;
            element.dispatchEvent({ type: "close" });
        },
    };
    return element;
}

const body = makeElement("body");
const documentStub = { createElement: makeElement, body };

// -- finding things without querySelector -----------------------------------

function walk(node, out) {
    out.push(node);
    (node.children || []).forEach((child) => walk(child, out));
    return out;
}

function allByClass(root, name) {
    return walk(root, []).filter((n) => n.classList.contains(name));
}

function byClass(root, name) {
    return allByClass(root, name)[0] || null;
}

function byText(root, text) {
    return walk(root, []).find((n) => n.textContent === text) || null;
}

function rowNames(dialog) {
    return allByClass(dialog, "path-picker-row")
        .map((row) => row.children[0].textContent);
}

function rowTypes(dialog) {
    return allByClass(dialog, "path-picker-row")
        .map((row) => row.children[1].textContent);
}

function press(target, key, extra = {}) {
    let defaultPrevented = false;
    let propagationStopped = false;
    const event = {
        type: "keydown",
        key,
        target,
        preventDefault() { defaultPrevented = true; },
        stopPropagation() { propagationStopped = true; },
        ...extra,
    };
    target.dispatchEvent(event);
    return { defaultPrevented, propagationStopped };
}

function clickRow(dialog, index, modifiers = {}) {
    const row = allByClass(dialog, "path-picker-row")[index];
    row.dispatchEvent({ type: "click", target: row, ...modifiers });
    return row;
}

// -- a server that answers whatever this test says --------------------------

const calls = [];
let listings = {};
let prefs = null;

function listedPath(options) {
    return JSON.parse(options.body).path;
}

function fetchStub(url, options = {}) {
    const method = options.method || "GET";
    calls.push({ url, method, body: options.body ? JSON.parse(options.body) : null });
    let answer;
    if (url.indexOf("picker_prefs") >= 0) {
        answer = prefs === null
            ? { status: 500, payload: { error: "no settings file" } }
            : { status: 200, payload: prefs };
    } else {
        const asked = listedPath(options);
        const found = listings[asked];
        answer = found
            ? { status: 200, payload: found }
            : { status: 400, payload: { error: `Not a folder: ${asked}` } };
    }
    return Promise.resolve({
        ok: answer.status >= 200 && answer.status < 300,
        status: answer.status,
        json: () => Promise.resolve(answer.payload),
    });
}

// -- one small filesystem, drawn the way the server draws it ----------------

function crumbsFor(path) {
    if (path.indexOf("\\") >= 0) {
        const parts = path.split("\\").filter(Boolean);
        const trail = [{ label: `${parts[0]}\\`, path: `${parts[0]}\\` }];
        parts.slice(1).forEach((part, index) => {
            trail.push({
                label: part,
                path: `${parts[0]}\\${parts.slice(1, index + 2).join("\\")}`,
            });
        });
        return trail;
    }
    const parts = path.split("/").filter(Boolean);
    const trail = [{ label: "/", path: "/" }];
    parts.forEach((part, index) => {
        trail.push({ label: part, path: `/${parts.slice(0, index + 1).join("/")}` });
    });
    return trail;
}

function folder(path, parent, entries, truncated = false) {
    return {
        path,
        parent,
        crumbs: crumbsFor(path),
        entries,
        truncated,
    };
}

function dir(path) {
    return { name: path.split(/[\\/]/).pop(), is_dir: true, size: null, path };
}

function file(path, size) {
    return { name: path.split(/[\\/]/).pop(), is_dir: false, size, path };
}

const HOME = "/home/aj";
const STUDY = "/home/aj/study";

function posixFilesystem() {
    return {
        // "" is what the server turns into the home directory.
        "": folder(HOME, "/home", [
            dir(`${HOME}/store.zarr`), dir(STUDY),
            file(`${HOME}/image.ome.tif`, 2048), file(`${HOME}/notes.txt`, 12),
        ]),
        [HOME]: folder(HOME, "/home", [
            dir(`${HOME}/store.zarr`), dir(STUDY),
            file(`${HOME}/image.ome.tif`, 2048), file(`${HOME}/notes.txt`, 12),
        ]),
        "/home": folder("/home", "/", [dir(HOME)]),
        "/": folder("/", null, [dir("/home")]),
        [STUDY]: folder(STUDY, HOME, [
            dir(`${STUDY}/runs`),
            file(`${STUDY}/cells.csv`, 812),
            file(`${STUDY}/cells2.csv`, 900),
            file(`${STUDY}/scan.svs`, 4096),
        ]),
        [`${STUDY}/runs`]: folder(`${STUDY}/runs`, STUDY, []),
        // A file's path opens the folder that holds it -- the server does that,
        // which is why the picker may hand a field's current value straight
        // over as `start`.
        [`${STUDY}/cells.csv`]: folder(STUDY, HOME, [
            dir(`${STUDY}/runs`), file(`${STUDY}/cells.csv`, 812),
        ]),
    };
}

// -- load the shipped file --------------------------------------------------

const context = {
    console,
    setTimeout,
    clearTimeout,
    fetch: fetchStub,
    plexoraUrl: (path) => `/${String(path).replace(/^\/+/, "")}`,
    document: documentStub,
    encodeURIComponent,
};
context.window = context;
createContext(context);
runInContext(readFileSync(SOURCE, "utf-8"), context);

const Picker = context.window.PlexoraPathPicker;

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

/** Open a picker and wait for its first listing to land. */
async function open(options = {}, {
    filesystem = posixFilesystem(),
    places = { last_dir: "", recent: [], pinned: [] },
} = {}) {
    listings = filesystem;
    prefs = places;
    calls.length = 0;
    const promise = Picker.pick(options);
    await settle();
    await settle();
    await settle();
    const dialog = body.children[body.children.length - 1];
    return { dialog, promise };
}

function listCalls() {
    return calls.filter((c) => c.url.indexOf("list_dir") >= 0);
}

function prefCalls(method) {
    return calls.filter((c) => c.url.indexOf("picker_prefs") >= 0
                          && c.method === method);
}

function crumbLabels(dialog) {
    return allByClass(dialog, "path-picker-crumb").map((c) => c.textContent);
}

function isOpen(dialog) {
    return body.children.indexOf(dialog) >= 0;
}

async function main() {
    // -- 1. the client does no path arithmetic ------------------------------

    {
        const { dialog } = await open();
        const before = listCalls().length;
        clickRow(dialog, 1);              // "study"
        await settle(); await settle();
        const asked = listCalls()[before].body.path;
        check("opening a folder asks for the path the SERVER gave that row",
              asked === STUDY);
        check("...and the listing that comes back is what is drawn",
              rowNames(dialog).join(",") === "runs,cells.csv,cells2.csv,scan.svs");
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    {
        // A Windows node, browsed from a Mac. Up here is the one thing the
        // client used to compute for itself, with a regex, and `C:\data`
        // reduced to "" -- an empty path, which lists the user's home.
        const windows = {
            "": folder("C:\\Users\\aj", "C:\\Users", []),
            "C:\\data": folder("C:\\data", "C:\\", [dir("C:\\data\\runs")]),
            "C:\\": folder("C:\\", null, [dir("C:\\data")]),
        };
        const { dialog } = await open({ start: "C:\\data" }, { filesystem: windows });
        check("a Windows node opens where it was asked to",
              crumbLabels(dialog).join("|") === "C:\\|data");
        const before = listCalls().length;
        byText(dialog, "\u2191").dispatchEvent({ type: "click" });
        await settle(); await settle();
        check("Up follows the server's own parent, separators and all",
              listCalls()[before].body.path === "C:\\");
        check("...and at the top of the tree Up is not offered",
              byText(dialog, "\u2191").disabled === true);
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    // -- 2. history, refresh, and staying put on failure --------------------

    {
        const { dialog } = await open();
        check("Back is dead until there is somewhere to go back to",
              byText(dialog, "\u2190").disabled === true);
        clickRow(dialog, 1);
        await settle(); await settle();
        check("...and live once a folder has been opened",
              byText(dialog, "\u2190").disabled === false);
        const before = listCalls().length;
        byText(dialog, "\u2190").dispatchEvent({ type: "click" });
        await settle(); await settle();
        check("Back returns to the previous directory",
              listCalls()[before].body.path === HOME
              && crumbLabels(dialog).join("|") === "/|home|aj");
        check("...and having gone back, Back is dead again",
              byText(dialog, "\u2190").disabled === true);

        const beforeRefresh = listCalls().length;
        byText(dialog, "\u21BB").dispatchEvent({ type: "click" });
        await settle(); await settle();
        check("Refresh re-reads where you are without adding a step back",
              listCalls()[beforeRefresh].body.path === HOME
              && byText(dialog, "\u2190").disabled === true);
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    {
        const { dialog } = await open();
        const edit = byClass(dialog, "path-picker-crumb-edit");
        byText(dialog, "\u270E").dispatchEvent({ type: "click" });
        edit.value = "/nowhere";
        press(edit, "Enter");
        await settle(); await settle();
        check("a folder that cannot be read leaves you where you were",
              crumbLabels(dialog).join("|") === "/|home|aj"
              && rowNames(dialog).length === 4);
        check("...and says why, in the picker rather than the console",
              byClass(dialog, "path-picker-error").hidden === false
              && byClass(dialog, "path-picker-error").textContent
                  .indexOf("/nowhere") >= 0);
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    // -- 3. the address bar --------------------------------------------------

    {
        const { dialog } = await open({ start: STUDY });
        check("the crumb trail is the server's, drawn as buttons",
              crumbLabels(dialog).join("|") === "/|home|aj|study");
        check("...with the folder you are in marked as such",
              allByClass(dialog, "path-picker-crumb")[3]
                  .getAttribute("aria-current") === "location");
        const before = listCalls().length;
        allByClass(dialog, "path-picker-crumb")[1]
            .dispatchEvent({ type: "click" });
        await settle(); await settle();
        check("clicking a crumb goes to the path that crumb carries",
              listCalls()[before].body.path === "/home");

        const edit = byClass(dialog, "path-picker-crumb-edit");
        const strip = byClass(dialog, "path-picker-address");
        check("the box is hidden while the crumbs are showing",
              edit.hidden === true);
        strip.dispatchEvent({ type: "click" });
        check("clicking the address bar turns the trail into a box, pre-filled",
              edit.hidden === false && edit.value === "/home");
        check("...with the path selected, so a pasted one replaces it",
              edit.selected === true);

        edit.selected = false;
        strip.dispatchEvent({ type: "click" });
        check("clicking inside the box being edited leaves the text alone",
              edit.selected === false && edit.hidden === false);

        const escape = press(edit, "Escape");
        check("Esc in the path box restores the crumbs",
              edit.hidden === true && isOpen(dialog));
        check("...and is stopped, or the browser closes the whole dialog",
              escape.defaultPrevented && escape.propagationStopped);

        const beforeCrumb = listCalls().length;
        allByClass(dialog, "path-picker-crumb")[0]
            .dispatchEvent({ type: "click" });
        await settle(); await settle();
        check("a crumb inside the bar navigates rather than opening the box",
              listCalls()[beforeCrumb].body.path === "/"
              && byClass(dialog, "path-picker-crumb-edit").hidden === true);

        byText(dialog, "\u270E").dispatchEvent({ type: "click" });
        edit.value = `  ${STUDY}  `;
        const beforeTyped = listCalls().length;
        press(edit, "Enter");
        await settle(); await settle();
        check("a typed path is trimmed and gone to -- the HPC gesture",
              listCalls()[beforeTyped].body.path === STUDY
              && crumbLabels(dialog).join("|") === "/|home|aj|study");
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    // -- 4. filtering this folder -------------------------------------------

    {
        const { dialog } = await open({ start: STUDY });
        const search = byClass(dialog, "path-picker-search");
        const count = byClass(dialog, "path-picker-count");
        check("with no filter typed there is no count to read", count.textContent === "");
        search.value = "cells";
        search.dispatchEvent({ type: "input" });
        check("typing narrows the listing in place, no request",
              rowNames(dialog).join(",") === "cells.csv,cells2.csv");
        check("...and says how much of the folder that is",
              count.textContent === "2 of 4 shown");

        const escape = press(search, "Escape");
        check("Esc empties the filter instead of closing the picker",
              search.value === "" && isOpen(dialog)
              && rowNames(dialog).length === 4);
        check("...and is stopped from reaching the dialog",
              escape.defaultPrevented && escape.propagationStopped);

        search.value = "cells";
        search.dispatchEvent({ type: "input" });
        clickRow(dialog, 0);   // cells.csv, first of the two shown
        await settle();
        check("a filtered row is the row it says it is",
              byText(dialog, "Choose").disabled === false);
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    {
        const { dialog } = await open();
        const search = byClass(dialog, "path-picker-search");
        search.value = "study";
        search.dispatchEvent({ type: "input" });
        clickRow(dialog, 0);
        await settle(); await settle();
        check("a filter belongs to the folder it was typed in",
              search.value === ""
              && rowNames(dialog).join(",") === "runs,cells.csv,cells2.csv,scan.svs");
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    {
        const truncated = posixFilesystem();
        truncated[STUDY] = folder(STUDY, HOME, truncated[STUDY].entries, true);
        const { dialog } = await open({ start: STUDY }, { filesystem: truncated });
        check("a cut-off folder says so, and says how to get past it",
              byClass(dialog, "path-picker-note").textContent
                  .indexOf("first 4 entries") >= 0);
        const search = byClass(dialog, "path-picker-search");
        search.value = "zzz";
        search.dispatchEvent({ type: "input" });
        check("...and a filter over a cut-off folder says WHICH entries it read",
              byClass(dialog, "path-picker-count").textContent
                  === "0 of the first 4 shown");
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    // -- 5. hidden files -----------------------------------------------------

    {
        const { dialog } = await open({ start: STUDY });
        const box = byClass(dialog, "path-picker-hidden").children[0];
        check("hidden files are off to begin with",
              listCalls()[0].body.show_hidden === false);
        const before = listCalls().length;
        box.checked = true;
        box.dispatchEvent({ type: "change" });
        await settle(); await settle();
        const asked = listCalls()[before];
        check("asking for them re-reads the same folder, hidden ones included",
              asked.body.show_hidden === true && asked.body.path === STUDY);
        check("...without adding a step to go back through",
              byText(dialog, "\u2190").disabled === true);
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    // -- 6. where it opens ---------------------------------------------------

    {
        const { dialog } = await open({ start: `${STUDY}/cells.csv` });
        check("a field's current value is handed over as-is; the server "
              + "turns a file into its folder",
              listCalls()[0].body.path === `${STUDY}/cells.csv`
              && crumbLabels(dialog).join("|") === "/|home|aj|study");
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    {
        const { dialog } = await open({ start: "node://laptop/cells-7f3a91c2" });
        check("a node locator is not a path, and is not opened as one",
              listCalls()[0].body.path === "");
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    {
        const { dialog } = await open({}, {
            places: { last_dir: STUDY, recent: [STUDY], pinned: [] },
        });
        check("with nothing to go on it opens where this machine was left",
              listCalls()[0].body.path === STUDY);
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    {
        const { dialog } = await open({ start: "/gone" });
        check("a remembered folder that has since been deleted falls back home",
              listCalls().map((c) => c.body.path).join("|") === "/gone|"
              && crumbLabels(dialog).join("|") === "/|home|aj");
        check("...and says that is what happened",
              byClass(dialog, "path-picker-note").textContent
                  .indexOf("/gone") >= 0);
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    {
        prefs = null;   // /picker_prefs answers 500
        listings = posixFilesystem();
        calls.length = 0;
        const promise = Picker.pick({});
        await settle(); await settle(); await settle();
        const dialog = body.children[body.children.length - 1];
        check("a preferences file that cannot be read costs the Recent list, "
              + "not the picker",
              rowNames(dialog).length === 4
              && allByClass(dialog, "path-picker-place").length === 1);
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await promise;
    }

    // -- 7. remembering places ----------------------------------------------

    {
        const { dialog, promise } = await open({ start: STUDY });
        clickRow(dialog, 1);   // cells.csv
        await settle();
        byText(dialog, "Choose").dispatchEvent({ type: "click" });
        const picked = await promise;
        await settle();
        check("choosing a file resolves with its server-given path",
              picked === `${STUDY}/cells.csv`);
        const written = prefCalls("POST");
        check("...and records the folder once, with both facts in one write",
              written.length === 1
              && written[0].body.last_dir === STUDY
              && written[0].body.add_recent === STUDY);
    }

    {
        // Cancelling is not an instruction to forget where you got to. Walking
        // six directories into /n/scratch and not finding the file is exactly
        // the case where being sent home next time costs the most.
        const { dialog, promise } = await open({ start: STUDY });
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        const picked = await promise;
        await settle();
        const written = prefCalls("POST");
        check("cancelling resolves with nothing, and still remembers the folder",
              picked === null
              && written.length === 1
              && written[0].body.last_dir === STUDY);
        check("...but nothing was taken from it, so Recent does not claim it was",
              written[0].body.add_recent === undefined);
    }

    {
        // Recent is a list of places that turned out to be worth something,
        // not a history of everywhere the picker was ever pointed.
        const { dialog, promise } = await open({}, {
            places: { last_dir: STUDY, recent: [STUDY], pinned: [] },
        });
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await promise;
        await settle();
        check("opening where it was left and closing again writes nothing",
              prefCalls("POST").length === 0);
    }

    {
        const { dialog } = await open({}, {
            places: { last_dir: "", recent: [`${STUDY}/runs`], pinned: [STUDY] },
        });
        const labels = allByClass(dialog, "path-picker-place")
            .map((p) => p.textContent);
        check("saved places are offered by name, home first",
              labels.join("|") === "Home|study|runs");
        const before = listCalls().length;
        allByClass(dialog, "path-picker-place")[1]
            .dispatchEvent({ type: "click" });
        await settle(); await settle();
        check("...and clicking one goes to the whole path behind it",
              listCalls()[before].body.path === STUDY);
        check("a folder that is already pinned says so",
              byClass(dialog, "path-picker-pin").getAttribute("aria-pressed")
                  === "true");

        prefs = { last_dir: "", recent: [`${STUDY}/runs`], pinned: [] };
        byClass(dialog, "path-picker-pin").dispatchEvent({ type: "click" });
        await settle(); await settle();
        check("un-pinning asks the server to forget it",
              prefCalls("POST").length === 1
              && prefCalls("POST")[0].body.unpin === STUDY);

        byText(dialog, "←").dispatchEvent({ type: "click" });
        await settle(); await settle();
        prefs = { last_dir: "", recent: [`${STUDY}/runs`], pinned: [HOME] };
        byClass(dialog, "path-picker-pin").dispatchEvent({ type: "click" });
        await settle(); await settle();
        const written = prefCalls("POST");
        check("pinning asks the server to keep it, per machine",
              written.length === 2 && written[1].body.pin === HOME
              && written[1].body.node === "");
        check("...and the sidebar redraws from what the server came back with",
              allByClass(dialog, "path-picker-place").map((p) => p.textContent)
                  .join("|") === "Home|aj|runs");
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    {
        const { dialog } = await open({ node: "hpc" });
        check("browsing a node lists THAT machine, and nothing else would do",
              listCalls()[0].body.node === "hpc"
              && calls[0].url.indexOf("node=hpc") >= 0);
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    // -- 8. keyboard, and what a listing is to a screen reader --------------

    {
        const { dialog, promise } = await open({ start: STUDY });
        const list = byClass(dialog, "path-picker-list");
        check("the listing is a listbox of options",
              list.getAttribute("role") === "listbox"
              && allByClass(dialog, "path-picker-row")
                  .every((r) => r.getAttribute("role") === "option"));

        press(dialog, "Enter", { target: byText(dialog, "Cancel") });
        check("Enter on a focused button belongs to the browser, which clicks "
              + "it -- answering it here too closed the picker from anywhere",
              isOpen(dialog));

        press(dialog, "ArrowDown", { target: list });
        press(dialog, "ArrowDown", { target: list });
        check("arrows walk the listing and say where they are",
              list.getAttribute("aria-activedescendant")
                  === allByClass(dialog, "path-picker-row")[1].getAttribute("id"));

        press(dialog, "Backspace", { target: list });
        await settle(); await settle();
        check("Backspace is Up",
              crumbLabels(dialog).join("|") === "/|home|aj");

        const search = byClass(dialog, "path-picker-search");
        search.value = "stu";
        const typed = press(dialog, "Backspace", { target: search });
        check("...but not while somebody is typing in a box",
              typed.defaultPrevented === false
              && crumbLabels(dialog).join("|") === "/|home|aj");

        press(dialog, "ArrowDown", { target: list });
        press(dialog, "ArrowDown", { target: list });
        press(dialog, "Enter", { target: list });
        await settle(); await settle();
        check("Enter on a folder opens it",
              crumbLabels(dialog).join("|") === "/|home|aj|study");

        press(dialog, "ArrowDown", { target: list });
        press(dialog, "ArrowDown", { target: list });
        press(dialog, "Enter", { target: list });
        const picked = await promise;
        check("Enter on a file chooses it and closes",
              picked === `${STUDY}/cells.csv`);
        await settle();
    }

    {
        const { dialog } = await open({ start: STUDY, filter: "h5ad" });
        const rows = allByClass(dialog, "path-picker-row");
        check("a file the field cannot take is shown, greyed",
              rows[1].classList.contains("is-muted") === true
              && rows[0].classList.contains("is-muted") === false);
        clickRow(dialog, 1);
        await settle();
        check("...and clicking it selects nothing",
              byText(dialog, "Choose").disabled === true);
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    // -- 9. what each row is -------------------------------------------------

    {
        const { dialog } = await open();
        check("the Type column names the thing, not the extension",
              rowTypes(dialog).join("|") === "Zarr store|Folder|OME-TIFF|Text");
        check("...and a size is only ever on a file",
              allByClass(dialog, "path-picker-row")
                  .map((r) => r.children[2].textContent).join("|")
                  === "||2.0 KB|12 B");
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    // -- 10. directory mode --------------------------------------------------

    {
        const { dialog, promise } = await open({ mode: "directory", start: STUDY });
        check("standing in a folder IS choosing it",
              byText(dialog, "Use this folder").disabled === false);
        byText(dialog, "Use this folder").dispatchEvent({ type: "click" });
        check("...which is how a .zarr store is picked", await promise === STUDY);
        await settle();
    }

    // -- 11. one button, either kind (mode "any") ---------------------------
    //
    // What every path field now asks for. The pair of buttons it replaces made
    // the user classify their own file -- File… or Store… -- before they were
    // allowed to point at it, and getting it wrong showed a dialog that would
    // not highlight the thing they came for. Here that distinction has to
    // disappear without the listing losing the ability to go INTO a store.

    {
        const { dialog, promise } = await open({ mode: "any" });
        const rows = allByClass(dialog, "path-picker-row");
        check("a store row is marked as one, and only it",
              rows[0].classList.contains("is-store") === true
              && rows[1].classList.contains("is-store") === false);

        const before = listCalls().length;
        clickRow(dialog, 0);              // store.zarr
        await settle();
        check("one click on a store selects it rather than opening it",
              listCalls().length === before
              && byText(dialog, "Choose").disabled === false);
        byText(dialog, "Choose").dispatchEvent({ type: "click" });
        check("...and Choose answers with the store itself",
              await promise === `${HOME}/store.zarr`);
        await settle();
    }

    {
        const { dialog, promise } = await open({ mode: "any" });
        const before = listCalls().length;
        clickRow(dialog, 1);              // study, an ordinary folder
        await settle(); await settle();
        check("an ordinary folder is still a place, and one click goes there",
              listCalls()[before].body.path === STUDY
              && crumbLabels(dialog).join("|") === "/|home|aj|study");
        clickRow(dialog, 1);              // cells.csv
        await settle();
        byText(dialog, "Choose").dispatchEvent({ type: "click" });
        check("...and a file in it is chosen exactly as in file mode",
              await promise === `${STUDY}/cells.csv`);
        await settle();
    }

    {
        const { dialog, promise } = await open({ mode: "any" });
        allByClass(dialog, "path-picker-row")[0]
            .dispatchEvent({ type: "dblclick" });
        check("double-clicking a store is 'this one', as it is on a file",
              await promise === `${HOME}/store.zarr`);
        await settle();
    }

    {
        const { dialog } = await open({ mode: "any" });
        const store = allByClass(dialog, "path-picker-row")[0];
        const enter = byClass(store, "path-picker-enter");
        const before = listCalls().length;
        enter.dispatchEvent({ type: "click", target: enter });
        await settle(); await settle();
        check("the › beside a store is the way into it, for the rare time "
              + "somebody wants what is inside",
              listCalls()[before].body.path === `${HOME}/store.zarr`);
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    {
        // The escape hatch for the one thing the naming rule gets wrong: a
        // store called `run7` draws as an ordinary folder and opens on a click,
        // and from inside it this is the answer.
        const { dialog, promise } = await open({ mode: "any", start: STUDY });
        check("'Use this folder' is live once a folder has been listed",
              byText(dialog, "Use this folder").disabled === false);
        byText(dialog, "Use this folder").dispatchEvent({ type: "click" });
        check("...and answers with the folder that is open, whatever it is called",
              await promise === STUDY);
        await settle();
    }

    {
        const { dialog } = await open({ mode: "file" });
        check("none of that exists in file mode: no store rows",
              allByClass(dialog, "path-picker-row")
                  .every((r) => r.classList.contains("is-store") === false)
              && allByClass(dialog, "path-picker-enter").length === 0);
        check("...and no folder button to press by accident",
              byText(dialog, "Use this folder") === null);
        const before = listCalls().length;
        clickRow(dialog, 0);              // store.zarr, a plain folder here
        await settle(); await settle();
        check("...so a .zarr opens, which is what file mode always did",
              listCalls()[before].body.path === `${HOME}/store.zarr`);
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    // -- 12. more than one file ---------------------------------------------

    {
        const { dialog, promise } = await open({ start: STUDY, multiple: true });
        check("a multi-select listing says so",
              byClass(dialog, "path-picker-list")
                  .getAttribute("aria-multiselectable") === "true");
        clickRow(dialog, 1);
        clickRow(dialog, 3, { metaKey: true });
        await settle();
        byText(dialog, "Choose").dispatchEvent({ type: "click" });
        check("cmd-click adds to the selection, and the answer is an array "
              + "in the order the rows are drawn",
              JSON.stringify(await promise)
                  === JSON.stringify([`${STUDY}/cells.csv`, `${STUDY}/scan.svs`]));
        await settle();
    }

    {
        const { dialog, promise } = await open({ start: STUDY, multiple: true });
        clickRow(dialog, 1);
        clickRow(dialog, 3, { shiftKey: true });
        await settle();
        byText(dialog, "Choose").dispatchEvent({ type: "click" });
        check("shift-click takes the run between them",
              JSON.stringify(await promise) === JSON.stringify([
                  `${STUDY}/cells.csv`, `${STUDY}/cells2.csv`, `${STUDY}/scan.svs`]));
        await settle();
    }

    {
        const { dialog, promise } = await open({ start: STUDY });
        clickRow(dialog, 1);
        clickRow(dialog, 3, { metaKey: true });
        await settle();
        byText(dialog, "Choose").dispatchEvent({ type: "click" });
        check("without `multiple` the answer is one path, as it always was",
              await promise === `${STUDY}/scan.svs`);
        await settle();
    }

    // -- 13. a node too old to send paths or crumbs -------------------------

    {
        const legacy = {
            "": { path: HOME, parent: "/home", truncated: false,
                  entries: [{ name: "study", is_dir: true, size: null }] },
            [`${HOME}/study`]: { path: STUDY, parent: HOME, truncated: false,
                                 entries: [] },
        };
        const { dialog } = await open({}, { filesystem: legacy });
        check("a node that sends no crumbs leaves the path box in place of them",
              byClass(dialog, "path-picker-crumb-edit").hidden === false
              && crumbLabels(dialog).length === 0);
        const before = listCalls().length;
        clickRow(dialog, 0);
        await settle(); await settle();
        check("...and a row with no path of its own is still openable",
              listCalls()[before].body.path === `${HOME}/study`);
        byText(dialog, "Cancel").dispatchEvent({ type: "click" });
        await settle();
    }

    // -- and nothing is left on the page ------------------------------------

    check("every picker that closed took its dialog with it",
          body.children.length === 0);

    console.log("");
    if (failures.length) {
        console.log(`${failures.length} check(s) failed`);
        process.exitCode = 1;
    } else {
        console.log("all checks passed");
    }
}

main();
