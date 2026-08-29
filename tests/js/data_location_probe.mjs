/**
 * Which machine a data field's file is on, as the form actually posts it.
 *
 * Five properties, and each one is something that fails silently if it is
 * wrong -- the form still submits, the server still answers, and the project
 * that comes out points at the wrong machine or at nothing:
 *
 *   1. **The switch is drawn on every launch, and means different things.**
 *      On a desktop install "This computer" IS the server's filesystem, and
 *      the box has to post the path exactly as typed. Get that wrong and an
 *      ordinary local import starts asking a node that is not there.
 *   2. **The user reads their own path; the form posts a locator.** Nobody
 *      should have to look at `node://laptop/cells-7f3a91c2` to know they
 *      picked ~/study/cells.h5ad. So the visible box keeps the path and a
 *      hidden companion carries the address -- and the field's `name` moves
 *      with it, because two inputs sharing one name post both values.
 *   3. **Nothing is submittable until the other machine has the file.** A
 *      mask converting on a laptop is minutes of work, and a form that let
 *      that through would import a project whose mask cannot serve a tile.
 *   4. **A field that arrives with a value posts it unchanged.** That value is
 *      a stored answer -- a server path or a node address -- and reading it as
 *      a path on some other machine would break a project that was working.
 *   5. **Switching away releases the share.** Otherwise a node accumulates
 *      every path somebody browsed past on the way to the one they meant.
 *
 * Run directly:  node tests/js/data_location_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/dataLocation.js");

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
        children: [],
        parentNode: null,
        dataset: {},
        validationMessage: "",
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
        setAttribute(name, value) {
            attributes.set(name, String(value));
            if (name === "name") element.name = String(value);
        },
        getAttribute(name) {
            return attributes.has(name) ? attributes.get(name) : null;
        },
        removeAttribute(name) {
            attributes.delete(name);
            if (name === "name") element.name = undefined;
        },
        setCustomValidity(message) { element.validationMessage = message; },
        appendChild(child) {
            child.parentNode = element;
            element.children.push(child);
            return child;
        },
        append(...nodes) { nodes.forEach((n) => element.appendChild(n)); },
        insertBefore(child, before) {
            child.parentNode = element;
            const at = element.children.indexOf(before);
            element.children.splice(at < 0 ? element.children.length : at, 0, child);
            return child;
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
        //: What a test presses. Not part of the DOM -- the shipped file wires
        //: real click handlers, and this is how they get called.
        click() { element.dispatchEvent({ type: "click" }); },
        //: What a <input type="file"> hands over. Set by a test to say what
        //: the user picked; there is no dialog to open here.
        files: null,
        accept: "",
        disabled: false,
    };
    // `name` as a plain property so `getAttribute("name")` and `.name` agree,
    // which is the whole mechanism under test in property 1 above.
    Object.defineProperty(element, "name", {
        writable: true, configurable: true, value: undefined,
    });
    return element;
}

function fieldRow() {
    const field = makeElement("div");
    const row = makeElement("div");
    field.appendChild(row);
    const input = makeElement("input");
    input.setAttribute("name", "data_file");
    row.appendChild(input);
    return { field, row, input };
}

// -- a server that answers whatever this test says --------------------------

const calls = [];
let reply = null;

function fetchStub(url, options = {}) {
    calls.push({ url, method: options.method || "GET", body: options.body });
    const answer = typeof reply === "function" ? reply(url, options) : reply;
    const { status = 200, payload = {} } = answer || {};
    return Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        json: () => Promise.resolve(payload),
    });
}

// -- load the shipped file --------------------------------------------------

const context = {
    console,
    setTimeout,
    clearTimeout,
    fetch: fetchStub,
    plexoraUrl: (path) => `/${String(path).replace(/^\/+/, "")}`,
    document: { createElement: makeElement },
    encodeURIComponent,
    //: Enough of FormData for the upload: it only ever appends one file, and
    //: what matters to a test is that the file got there.
    FormData: class {
        constructor() { this.entries = []; }
        append(name, value) { this.entries.push([name, value]); }
    },
    //: The shipped file re-dispatches `input` after a switch, so whatever
    //: live-validation the surrounding form has runs on the cleared box.
    Event: class { constructor(type, options = {}) {
        this.type = type;
        this.bubbles = Boolean(options.bubbles);
    } },
};
context.window = context;
context.flaskVariables = { client_node: "laptop" };
//: What the Remote button opens. Stubbed rather than loaded, because what this
//: file is testing is what the FIELD does with an answer -- the dialog that
//: produces one is placePicker.js's business. A test sets `nextPlace` to the
//: machine the user is about to choose.
context.nextPlace = null;
context.PlexoraPlacePicker = {
    pick: () => Promise.resolve(context.nextPlace),
};
createContext(context);
runInContext(readFileSync(SOURCE, "utf-8"), context);

const DataLocation = context.window.PlexoraDataLocation;

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

function hiddenIn(row) {
    return row.children.find((c) => c.type === "hidden");
}

/** The control's own status line, which lives under the field, not in the row. */
function statusOf(location) {
    return location.statusElement;
}

function optionOf(location, where) {
    return location.element.children[0].children.find(
        (c) => c.dataset.where === where);
}

function chooserOf(location) {
    return location.element.children.find((c) => c.type === "file");
}

async function main() {
    // -- every field carries its own, and mounting one cannot cost another --
    context.flaskVariables = {};
    const mounted = [];
    ["image", "segmentation", "table"].forEach((kind) => {
        const each = fieldRow();
        const handle = DataLocation.attach(each.input, {
            kind,
            // A handler written the way a real caller writes one: it reaches
            // for the handle `attach` has not returned yet. Mounting must not
            // call it, or the first field's exception takes the rest with it
            // -- which is precisely what left the import form offering the
            // choice for the image alone.
            onChange: () => notYetAssigned.blocking(),
        });
        mounted.push({ each, handle });
    });
    check("every data field gets its own switch, independently",
          mounted.length === 3 && mounted.every((m) => m.handle !== null));
    check("...mounted in the row, right beside the path box it governs",
          mounted.every((m) => m.each.row.children[0] === m.handle.element
                          && m.each.row.children[1] === m.each.input));
    check("...and it reads Local | Remote",
          mounted[0].handle.element.children[0].children
              .map((b) => b.textContent).join("|") === "Local|Remote");

    // -- a desktop launch: one machine, and the switch still asks -----------
    const desktop = fieldRow();
    const desktopLocation = DataLocation.attach(desktop.input, { kind: "table" });
    check("the switch is offered even where Plexora runs on this machine",
          desktopLocation !== null && DataLocation.available() === true);
    check("...and This computer means the server's own filesystem",
          desktopLocation.isPlainPath() === true
          && desktopLocation.browseNode() === null);
    check("...so the box keeps its own name and posts the path as typed",
          desktop.input.getAttribute("name") === "data_file"
          && hiddenIn(desktop.row) === undefined);
    desktop.input.value = "/data/cells.csv";
    check("...and nothing is asked of any node", desktopLocation.blocking() === null
          && desktopLocation.submitValue() === "/data/cells.csv");

    // -- Remote is a question, and the answer is a machine ------------------
    calls.length = 0;
    context.nextPlace = { id: "hpc", kind: "remote", label: "hpc",
                          node: "hpc" };
    reply = { payload: { resource: { id: "slide-11", state: "ready",
                                     locator: "node://hpc/slide-11" } } };
    optionOf(desktopLocation, "remote").click();
    await settle();
    check("choosing Remote asks which machine, and takes the answer",
          desktopLocation.isLocal() === false
          && desktopLocation.browseNode() === "hpc");
    check("...and clears a path that described a different filesystem",
          desktop.input.value === "");
    desktop.input.value = "/scratch/slide.ome.tif";
    desktop.input.dispatchEvent({ type: "change" });
    await settle();
    check("...then shares through THAT machine's node",
          calls[0]?.url === "/nodes/hpc/resources"
          && desktopLocation.submitValue() === "node://hpc/slide-11");
    context.nextPlace = null;

    // -- with Plexora on the far side, This computer is the laptop node -----
    context.flaskVariables = { client_node: "laptop" };
    const empty = fieldRow();
    const location = DataLocation.attach(empty.input, { kind: "table" });
    check("an empty field defaults to the machine the user is sitting at",
          location.isLocal() === true);
    check("...so the form field's name moves to a hidden companion",
          empty.input.getAttribute("name") === null
          && hiddenIn(empty.row)?.name === "data_file");
    check("...and browse asks that machine's dialog, not the server's",
          location.browseNode() === "laptop");

    // -- a stored value is posted unchanged ---------------------------------
    const stored = fieldRow();
    stored.input.value = "/scratch/cells.h5ad";
    const kept = DataLocation.attach(stored.input, { kind: "table" });
    check("a field that arrives with a value is left describing the server",
          kept.isLocal() === false && kept.submitValue() === "/scratch/cells.h5ad");
    check("...with its own name still on it, so it posts what it always did",
          stored.input.getAttribute("name") === "data_file"
          && hiddenIn(stored.row) === undefined);
    check("...and browse goes to the server", kept.browseNode() === null);

    // -- sharing a local file ----------------------------------------------
    calls.length = 0;
    reply = { payload: { resource: { id: "cells-7f3a91c2", state: "ready",
                                     locator: "node://laptop/cells-7f3a91c2" } } };
    empty.input.value = "/Users/me/study/cells.h5ad";
    empty.input.dispatchEvent({ type: "change" });
    await settle();

    check("picking a local file asks the node to serve it",
          calls[0]?.method === "POST"
          && calls[0].url === "/nodes/laptop/resources"
          && JSON.parse(calls[0].body).path === "/Users/me/study/cells.h5ad"
          && JSON.parse(calls[0].body).kind === "table");
    check("the form posts the address...",
          hiddenIn(empty.row).value === "node://laptop/cells-7f3a91c2"
          && location.submitValue() === "node://laptop/cells-7f3a91c2");
    check("...while the box still shows the path the user picked",
          empty.input.value === "/Users/me/study/cells.h5ad");
    check("and nothing is blocking the form", location.blocking() === null);

    // -- a mask that has to be converted first ------------------------------
    const mask = fieldRow();
    const maskLocation = DataLocation.attach(mask.input, { kind: "segmentation" });
    calls.length = 0;
    reply = { payload: { resource: { id: "mask-ab", state: "preparing",
                                     locator: "node://laptop/mask-ab" } } };
    mask.input.value = "/Users/me/study/mask.tif";
    mask.input.dispatchEvent({ type: "change" });
    await settle();

    check("a mask still converting is not something to submit",
          /still being prepared/i.test(maskLocation.blocking() || ""));
    check("...and the field says so rather than sitting silent",
          /Preparing/i.test(statusOf(maskLocation).textContent));

    reply = { payload: { resource: { id: "mask-ab", state: "ready",
                                     locator: "node://laptop/mask-ab" } } };
    await new Promise((resolve) => setTimeout(resolve, 2100));
    check("once it lands the form is free again", maskLocation.blocking() === null);
    check("...having asked the node, not guessed",
          calls.some((c) => c.url.endsWith("/resources/mask-ab/status")));

    // -- the value arriving from Browse rather than from typing --------------
    //
    // attachBrowseButton assigns `input.value` and dispatches the events by
    // hand, because a programmatic assignment fires none of them. It used to
    // send `input` and `keyup` and not `change` -- and `change` is the one the
    // share waits for. Browsing to a file on a cluster therefore filled the
    // box and shared nothing, and the import answered "provide a valid path to
    // the image file" about a path that was plainly right there.
    const browsed = fieldRow();
    const browsedLocation = DataLocation.attach(browsed.input, { kind: "image" });
    calls.length = 0;
    reply = { payload: { resource: { id: "slide-9", state: "ready",
                                     locator: "node://laptop/slide-9" } } };
    browsed.input.value = "/Users/me/slide.ome.tif";
    ["input", "keyup", "change"].forEach(
        (type) => browsed.input.dispatchEvent({ type }));
    await settle();

    check("a path that arrived from Browse is shared like a typed one",
          calls[0]?.url === "/nodes/laptop/resources");
    check("...so the form has an address to post, not an empty box",
          browsedLocation.submitValue() === "node://laptop/slide-9"
          && hiddenIn(browsed.row).value === "node://laptop/slide-9");

    // -- a file that is not there -------------------------------------------
    const bad = fieldRow();
    const badLocation = DataLocation.attach(bad.input, { kind: "table" });
    reply = { status: 400, payload: { error: "there is nothing at /nope.csv" } };
    bad.input.value = "/nope.csv";
    bad.input.dispatchEvent({ type: "change" });
    await settle();
    check("a path that is not on that machine blocks the form, saying why",
          /nothing at \/nope\.csv/.test(badLocation.blocking() || ""));

    // -- switching away releases the share ----------------------------------
    calls.length = 0;
    context.nextPlace = { id: "server", kind: "server", label: "the server",
                          node: null };
    optionOf(location, "remote").click();
    await settle();
    check("switching to the server takes the share back",
          calls.some((c) => c.method === "DELETE"
                     && c.url === "/nodes/laptop/resources/cells-7f3a91c2"));
    check("...restores the field's own name, so it posts one value",
          empty.input.getAttribute("name") === "data_file"
          && hiddenIn(empty.row) === undefined);
    check("...and clears a path that described the other machine",
          empty.input.value === "" && location.submitValue() === "");

    // -- sending the bytes instead --------------------------------------------
    const csv = fieldRow();
    const csvLocation = DataLocation.attach(csv.input, { kind: "table" });
    calls.length = 0;
    reply = { payload: { ok: true, name: "cells.csv",
                         path: "/data/uploads/ab12/cells.csv" } };
    const chooser = chooserOf(csvLocation);
    chooser.files = [{ name: "cells.csv" }];
    chooser.dispatchEvent({ type: "change" });
    await settle();

    check("a CSV can be sent from the browser rather than named",
          calls[0]?.url === "/upload_data_file" && calls[0].method === "POST");
    check("...and what the form posts is a path on the server, not an address",
          csvLocation.submitValue() === "/data/uploads/ab12/cells.csv");
    check("...so the import copies it into the project as it would any CSV",
          csvLocation.blocking() === null);
    check("...while the box shows the name the user picked",
          csv.input.value === "cells.csv");

    // -- no node on this machine at all ---------------------------------------
    context.flaskVariables = { notebook_mode: true };
    check("a session with no node still asks the question",
          DataLocation.available() === true);

    const detachedTable = fieldRow();
    const detachedTableLocation = DataLocation.attach(detachedTable.input,
                                                      { kind: "table" });
    check("...because a CSV can still be sent",
          chooserOf(detachedTableLocation) !== undefined
          && /Upload/.test(statusOf(detachedTableLocation).textContent));
    check("...and the path box stops pretending to take a path",
          detachedTable.input.disabled === true);

    const detachedMask = fieldRow();
    const detachedMaskLocation = DataLocation.attach(detachedMask.input,
                                                     { kind: "segmentation" });
    detachedMask.input.value = "/Users/me/mask.tif";
    check("a mask cannot be sent, so it says what would make it possible",
          /plexora connect/.test(detachedMaskLocation.blocking() || ""));
    check("...and offers no upload it could not honour",
          chooserOf(detachedMaskLocation) === undefined);
    context.flaskVariables = { client_node: "laptop" };

    if (failures.length) {
        console.error(`\n${failures.length} failed`);
        process.exit(1);
    }
}

main().catch((error) => {
    console.error(error);
    process.exit(1);
});
