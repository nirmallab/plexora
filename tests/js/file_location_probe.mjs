/**
 * Local/Remote for every file button, asked once and shared.
 *
 * Plugins each have their own Upload arrow and their own Download button, and
 * none of them knows the core's Local/Remote switch exists -- so on a session
 * whose data lives on a cluster, every one of those buttons quietly meant "the
 * laptop", the one machine the data is not on. This layer intercepts the click
 * instead of teaching each plugin the question.
 *
 * What it has to get right is almost entirely about restraint:
 *
 *   - **Nothing at all when there is nowhere else to go.** No dialog, no
 *     network, no interception: with one machine every button behaves exactly
 *     as it did, which is every single-server install.
 *   - **One re-click, not a loop.** Choosing "This computer" re-clicks the very
 *     element that was intercepted, and the second click must go past.
 *   - **The plugin's own handler, reached the ordinary way.** Files land on the
 *     input and `change` fires; nothing downstream learns a picker was involved.
 *   - **A node that outlived its session is still a machine.** `registered_node`
 *     as well as `node` -- filtering on `node` alone makes a reachable cluster
 *     invisible the morning after a restart.
 *   - **Cancelling changes nothing.** Escape at any stage leaves the input, the
 *     anchor and the plugin exactly as they were.
 *
 * Run directly:  node tests/js/file_location_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/fileLocation.js");

// -- a DOM with a document at the top of it ---------------------------------
//
// The one thing this fake has to do that the other probes' do not: bubble. The
// whole design rests on a single listener on `document` seeing a click that
// started on an input six levels down, so the dispatch here walks the parent
// chain and ends at the document, exactly as the real one does.

const REFLECTED = ["href", "download", "type", "accept"];

function matches(node, selector) {
    if (selector === 'input[type="file"]') {
        return node.tagName === "INPUT" && node.getAttribute("type") === "file";
    }
    if (selector === "a[download]") {
        return node.tagName === "A" && node.getAttribute("download") !== null;
    }
    if (selector === '[data-file-location="local"]') {
        return node.getAttribute("data-file-location") === "local";
    }
    if (selector.charAt(0) === ".") {
        return node.classList.contains(selector.slice(1));
    }
    throw new Error("probe does not know the selector " + selector);
}

function makeElement(tag) {
    const classes = new Set();
    const listeners = new Map();
    const attributes = new Map();
    const element = {
        tagName: String(tag).toUpperCase(),
        textContent: "",
        children: [],
        parentNode: null,
        open: false,
        style: {},
        value: "",
        files: null,
        multiple: false,
        get firstChild() { return element.children[0] || null; },
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
        remove() {
            const parent = element.parentNode;
            if (!parent) return;
            parent.children = parent.children.filter((c) => c !== element);
            element.parentNode = null;
        },
        closest(selector) {
            let here = element;
            while (here && here.tagName) {
                if (matches(here, selector)) return here;
                here = here.parentNode;
            }
            return null;
        },
        querySelector(selector) {
            return walk(element).find((n) => matches(n, selector)) || null;
        },
        addEventListener(type, handler) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(handler);
        },
        fire(event) {
            (listeners.get(event.type) || []).forEach((h) => h(event));
        },
        dispatchEvent(event) {
            if (!event.target) event.target = element;
            if (event.bubbles === false) {
                element.fire(event);
                return !event.defaultPrevented;
            }
            let here = element;
            while (here) {
                here.fire(event);
                here = here.parentNode;
            }
            documentStub.fire(event);
            return !event.defaultPrevented;
        },
        click() {
            const event = {
                type: "click", target: element, button: 0, bubbles: true,
                defaultPrevented: false,
                preventDefault() { event.defaultPrevented = true; },
            };
            element.dispatchEvent(event);
            // The default action, which is what the layer either allows or
            // pre-empts: a file input opens the OS dialog, an anchor saves.
            // Only those two -- a button's default action is nothing, and
            // recording it would count every row of the dialog as a download.
            if (!event.defaultPrevented
                    && (matches(element, 'input[type="file"]')
                        || matches(element, "a[download]"))) {
                opened.push(element);
            }
        },
        focus() { focused.push(element); },
        setSelectionRange() {},
        showModal() { element.open = true; },
        close() {
            element.open = false;
            element.fire({ type: "close" });
        },
    };
    REFLECTED.forEach((name) => {
        Object.defineProperty(element, name, {
            get() { return element.getAttribute(name); },
            set(value) { element.setAttribute(name, value); },
        });
    });
    return element;
}

function walk(root, out = []) {
    (root.children || []).forEach((child) => {
        out.push(child);
        walk(child, out);
    });
    return out;
}

function textOf(root) {
    return walk(root).map((n) => n.textContent).join(" ");
}

function rowSaying(root, text) {
    return walk(root).find(
        (n) => n.tagName === "BUTTON" && textOf(n).indexOf(text) >= 0) || null;
}

function buttonSaying(root, text) {
    return walk(root).find(
        (n) => n.tagName === "BUTTON" && n.textContent === text) || null;
}

const opened = [];
const focused = [];
const body = makeElement("body");

const documentListeners = new Map();
const documentStub = {
    createElement: makeElement,
    body,
    addEventListener(type, handler) {
        if (!documentListeners.has(type)) documentListeners.set(type, []);
        documentListeners.get(type).push(handler);
    },
    fire(event) {
        (documentListeners.get(event.type) || []).forEach((h) => h(event));
    },
};

// -- the network, recorded --------------------------------------------------

class BlobStub {
    constructor(parts = [], options = {}) {
        this.parts = parts;
        this.type = options.type || "";
    }
}

class FileStub extends BlobStub {
    constructor(parts, name, options = {}) {
        super(parts, options);
        this.name = name;
    }
}

class FormDataStub {
    constructor() { this.entries = []; }
    append(name, value, filename) {
        this.entries.push({ name, value, filename });
    }
    get(name) {
        const found = this.entries.find((e) => e.name === name);
        return found ? found.value : null;
    }
}

class EventStub {
    constructor(type, options = {}) {
        this.type = type;
        this.bubbles = Boolean(options.bubbles);
        this.defaultPrevented = false;
    }
    preventDefault() { this.defaultPrevented = true; }
}

const requests = [];
let replies = [];

function fetchStub(url, options = {}) {
    requests.push({ url, options });
    const reply = replies.shift() || { ok: true, body: {} };
    return Promise.resolve({
        ok: reply.ok !== false,
        status: reply.status || (reply.ok === false ? 409 : 200),
        headers: { get: (name) => (reply.headers || {})[name] || null },
        json: () => Promise.resolve(reply.body || {}),
        blob: () => Promise.resolve(new BlobStub([reply.bytes || "x"])),
    });
}

// -- the services this layer leans on ---------------------------------------

const EXTENSIONS = {
    image: [".tif", ".tiff", ".ome.tif", ".ome.tiff", ".svs", ".qptiff",
            ".png", ".jpg", ".jpeg"],
    csv: [".csv"],
    h5ad: [".h5ad"],
    data: [".csv", ".tsv", ".txt", ".h5ad"],
    channels: [".csv", ".tsv", ".txt", ".xlsx", ".xlsm"],
    any: null,
};

const pickerCalls = [];
let pickerAnswers = [];

const PathPickerStub = {
    accepts(name, filter) {
        const allowed = EXTENSIONS[filter];
        if (!allowed) return true;
        return allowed.some((suffix) => String(name).toLowerCase().endsWith(suffix));
    },
    pick(options) {
        pickerCalls.push(options);
        return Promise.resolve(pickerAnswers.shift() || null);
    },
};

const subscriptions = [];
let snapshot = { loaded: true, places: [] };

const RemotesStub = {
    snapshot: () => snapshot,
    refresh: () => Promise.resolve(snapshot),
    subscribe: (cb, options = {}) => {
        const record = { cb, active: Boolean(options.active), live: true };
        subscriptions.push(record);
        cb(snapshot);
        return () => { record.live = false; };
    },
};

function say(places) {
    snapshot = { loaded: true, places };
    subscriptions.filter((s) => s.live).forEach((s) => s.cb(snapshot));
}

const modalOpens = [];

// -- load the shipped file --------------------------------------------------

const context = {
    console,
    Promise,
    Math,
    String,
    Object,
    Array,
    JSON,
    Error,
    Boolean,
    Number,
    fetch: fetchStub,
    FormData: FormDataStub,
    Blob: BlobStub,
    File: FileStub,
    Event: EventStub,
    DataTransfer: class {
        constructor() {
            const files = [];
            this.files = files;
            this.items = { add: (file) => files.push(file) };
        }
    },
    URL: { createObjectURL: () => "blob:made-up", revokeObjectURL: () => {} },
    setTimeout: () => 0,
    document: documentStub,
    plexoraUrl: (path) => "/" + path,
};
context.window = context;
context.PlexoraRemotes = RemotesStub;
context.PlexoraPathPicker = PathPickerStub;
context.PlexoraConnectionModal = {
    open: (options) => {
        modalOpens.push(options);
        return Promise.resolve({ connected: false });
    },
};

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

const settle = () => new Promise((resolve) => {
    let left = 40;
    const step = () => (left-- > 0 ? Promise.resolve().then(step) : resolve());
    step();
});

function dialogNow() {
    return body.children.find((c) => c.tagName === "DIALOG" && c.open) || null;
}

function fileInput({ accept = "", multiple = false, optOut = false } = {}) {
    const input = makeElement("input");
    input.setAttribute("type", "file");
    if (accept) input.setAttribute("accept", accept);
    input.multiple = multiple;
    if (optOut) input.setAttribute("data-file-location", "local");
    body.appendChild(input);
    return input;
}

function downloadLink(href, name) {
    const anchor = makeElement("a");
    anchor.setAttribute("href", href);
    anchor.setAttribute("download", name);
    body.appendChild(anchor);
    return anchor;
}

const NODE_PLACE = { id: "o2", kind: "remote", label: "o2",
                     detail: "me@o2.hms.harvard.edu", node: "o2-data",
                     registered_node: null };
//: A node that outlived the Plexora that started it: the session is gone, the
//: tunnel is not, and only `registered_node` still names it.
const RESTARTED_PLACE = { id: "hpc", kind: "remote", label: "hpc",
                          detail: "me@hpc", node: null,
                          registered_node: "hpc-data" };

async function main() {
    // -- 1. one machine, no question ------------------------------------------
    say([]);
    await settle();
    check("it watches passively, so a settled connection costs no polling",
          subscriptions.length === 1 && subscriptions[0].active === false);
    check("with nowhere else to go it says there is nowhere else to go",
          context.PlexoraFileLocation.remoteAvailable() === false);

    const plain = fileInput();
    plain.click();
    await settle();
    check("...so an upload button opens the file dialog, untouched",
          opened.length === 1 && opened[0] === plain && dialogNow() === null);
    check("...and nothing was asked of the server",
          requests.length === 0);

    // -- 2. a machine appears --------------------------------------------------
    say([NODE_PLACE]);
    await settle();
    opened.length = 0;
    const upload = fileInput({ accept: ".csv" });
    upload.click();
    await settle();
    const asked = dialogNow();
    check("with a machine connected, the same button asks first",
          Boolean(asked) && opened.length === 0);
    check("...offering this computer and the machine by name",
          textOf(asked).indexOf("This computer") >= 0
          && textOf(asked).indexOf("o2") >= 0);

    // -- 3. this computer, once ------------------------------------------------
    rowSaying(asked, "This computer").click();
    await settle();
    check("choosing this computer opens the dialog it was going to open",
          opened.length === 1 && opened[0] === upload);
    check("...exactly once -- the re-click is not intercepted again",
          dialogNow() === null);

    // -- 4. the other machine --------------------------------------------------
    opened.length = 0;
    requests.length = 0;
    pickerCalls.length = 0;
    pickerAnswers = ["/n/scratch/aj/gates.csv"];
    replies = [{ ok: true, headers: { "X-Plexora-File-Name": "gates.csv" },
                 bytes: "a,b\n" }];
    upload.click();
    await settle();
    rowSaying(dialogNow(), "o2").click();
    await settle();
    check("choosing the machine browses ITS filesystem, not this one",
          pickerCalls.length === 1 && pickerCalls[0].node === "o2-data"
          && pickerCalls[0].mode === "file");
    check("...with the filter the field itself asked for",
          pickerCalls[0].filter === "csv");
    check("...and one file at a time, because the input takes one",
          pickerCalls[0].multiple === false);
    check("...the bytes coming back through this server",
          requests.length === 1 && requests[0].url === "/fetch_file"
          && JSON.parse(requests[0].options.body).path
             === "/n/scratch/aj/gates.csv");

    // -- 5. and the plugin's own handler runs, unchanged -----------------------
    check("the file lands on the input, named as the far side named it",
          upload.files && upload.files.length === 1
          && upload.files[0].name === "gates.csv");
    check("...and `change` fires, which is all the plugin was ever listening for",
          changes.length === 1 && changes[0].bubbles === true);

    // -- 5b. what the field asks for, translated once ------------------------
    //
    // Conservative on purpose: greying out a file the form would have taken is
    // a dead end with no way past it from inside the picker, so anything this
    // is unsure of is "any" and the form's own validation still has the last
    // word. `image/*` is the one shorthand worth translating -- a picker
    // filter exists for exactly it.
    pickerCalls.length = 0;
    pickerAnswers = [null, null];
    const images = fileInput({ accept: "image/*", multiple: true });
    images.click();
    await settle();
    rowSaying(dialogNow(), "o2").click();
    await settle();
    check("an image field browses with the image filter, several at a time",
          pickerCalls[0].filter === "image" && pickerCalls[0].multiple === true);

    const geo = fileInput({ accept: ".geojson,.json,application/geo+json" });
    geo.click();
    await settle();
    rowSaying(dialogNow(), "o2").click();
    await settle();
    check("...and a field asking for something no filter covers greys out nothing",
          pickerCalls[1].filter === "any");

    // -- 6. a node that outlived its session -----------------------------------
    say([RESTARTED_PLACE]);
    await settle();
    pickerCalls.length = 0;
    pickerAnswers = [null];
    const after = fileInput();
    after.click();
    await settle();
    rowSaying(dialogNow(), "hpc").click();
    await settle();
    check("a node that outlived the session that started it is still a machine",
          pickerCalls.length === 1 && pickerCalls[0].node === "hpc-data");

    // -- 7. cancelling changes nothing ----------------------------------------
    say([NODE_PLACE]);
    await settle();
    opened.length = 0;
    requests.length = 0;
    const untouched = fileInput();
    untouched.click();
    await settle();
    dialogNow().close();
    await settle();
    check("cancelling leaves the input alone and opens nothing",
          opened.length === 0 && requests.length === 0
          && untouched.files === null && dialogNow() === null);

    // -- 8. a field that has its own switch -----------------------------------
    opened.length = 0;
    const core = fileInput({ optOut: true });
    core.click();
    await settle();
    check("a field with its own Local/Remote switch is not asked twice",
          opened.length === 1 && dialogNow() === null);

    // -- 9. a modified click belongs to the browser ---------------------------
    opened.length = 0;
    const modified = fileInput();
    const event = { type: "click", target: modified, button: 0, bubbles: true,
                    metaKey: true, defaultPrevented: false,
                    preventDefault() { event.defaultPrevented = true; } };
    modified.dispatchEvent(event);
    check("a command-click is the user talking to their browser, not to us",
          event.defaultPrevented === false && dialogNow() === null);

    // -- 10. a download, sent to the machine ----------------------------------
    requests.length = 0;
    pickerCalls.length = 0;
    pickerAnswers = ["/n/scratch/aj/exports"];
    replies = [
        { ok: true, bytes: "report" },                       // the fetch of href
        { ok: false, status: 409, body: { exists: true,
            error: "There is already a file called report.pdf in there." } },
        { ok: true, body: { success: true, path: "/n/scratch/aj/exports/report.pdf" } },
    ];
    const link = downloadLink("/plugins/x/report.pdf", "report.pdf");
    link.click();
    await settle();
    rowSaying(dialogNow(), "o2").click();
    await settle();
    check("a download link asks where it should go",
          pickerCalls.length === 1 && pickerCalls[0].mode === "directory"
          && pickerCalls[0].node === "o2-data");
    const naming = dialogNow();
    check("...then what it should be called, prefilled from the link",
          Boolean(naming)
          && walk(naming).some((n) => n.value === "report.pdf"));
    buttonSaying(naming, "Save").click();
    await settle();
    const refused = dialogNow();
    check("a name already taken is a question, not a dead end",
          Boolean(refused) && Boolean(buttonSaying(refused, "Replace")));
    buttonSaying(refused, "Replace").click();
    await settle();
    const put = requests.filter((r) => r.url === "/put_file");
    check("...and Replace says so, rather than saving under another name",
          put.length === 2 && put[1].options.body.get("overwrite") === "1"
          && put[1].options.body.get("name") === "report.pdf"
          && put[1].options.body.get("node") === "o2-data");

    // -- 11. deliver(), for what a click cannot reach --------------------------
    say([]);
    await settle();
    requests.length = 0;
    opened.length = 0;
    const saved = await context.PlexoraFileLocation.deliver(
        new BlobStub(["cells"]), "gated.csv");
    await settle();
    check("with nowhere else to go, deliver saves here and asks nothing",
          saved === true && dialogNow() === null);
    check("...touching the network not at all, which is the emergency path",
          requests.length === 0);
    check("...through an anchor it does not then intercept itself",
          opened.length === 1 && opened[0].getAttribute("download") === "gated.csv");

    // -- 12. and the same call, with somewhere to send it ----------------------
    say([NODE_PLACE]);
    await settle();
    requests.length = 0;
    pickerCalls.length = 0;
    pickerAnswers = ["/n/scratch/aj"];
    replies = [{ ok: true, body: { success: true, path: "/n/scratch/aj/gated.csv" } }];
    const promised = context.PlexoraFileLocation.deliver(
        new BlobStub(["cells"]), "gated.csv");
    await settle();
    const where = dialogNow();
    check("with a machine connected, deliver asks the same question", Boolean(where));
    check("...remembering where the last one went",
          Boolean(where.querySelector(".is-last-used")));
    rowSaying(where, "o2").click();
    await settle();
    buttonSaying(dialogNow(), "Save").click();
    await settle();
    check("...and the blob goes to the machine, under the name it was given",
          (await promised) === true
          && requests.filter((r) => r.url === "/put_file").length === 1);

    console.log(failures.length
        ? `\n${failures.length} failed`
        : "\nall file-location checks passed");
    if (failures.length) process.exitCode = 1;
}

//: Every `change` the layer dispatched, which is the whole of its contract with
//: a plugin: the files are on the input and this event says so.
const changes = [];
documentStub.addEventListener("change", (event) => changes.push(event));

main();
