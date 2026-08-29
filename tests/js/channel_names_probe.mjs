/**
 * Naming an image's channels from a file, as the viewer asks for it.
 *
 * Three things run through every check below, and each is something a user
 * cannot verify for themselves without losing work finding out they were
 * wrong:
 *
 *   1. **The file is named once.** Being sent back to an empty box because a
 *      checkbox was ticked, or because the count was wrong, is the modal
 *      throwing away the only thing it asked for. Every stage re-posts the
 *      same path.
 *   2. **A wrong file changes nothing.** A mismatch is a stage, not a partial
 *      rename -- an image whose first thirty channels are named and whose last
 *      ten are still Channel_30 looks named, and gating would believe it.
 *   3. **There is ONE way in**, and it is a path on the machine running the
 *      server. That machine is the only one that can see the file on a
 *      cluster, and locally the Browse button beside the box opens a native
 *      file dialog anyway -- so a browser upload above it was a choice between
 *      two spellings of the same act, offered before the user had done
 *      anything.
 *
 * The shipped file is run against a DOM stand-in. Nothing here parses a CSV:
 * that is the server's, deliberately (server/utils/channel_file.py), and the
 * fake `fetch` below is how this file states what the server said.
 *
 * Run directly:  node tests/js/channel_names_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/views/channelNamesUpload.js");

// -- a DOM small enough to read -----------------------------------------------

function makeElement(tag) {
    const classes = new Set();
    const attributes = new Map();
    const listeners = new Map();
    const element = {
        tagName: String(tag).toUpperCase(),
        id: "",
        type: "",
        title: "",
        htmlFor: "",
        accept: "",
        method: "",
        placeholder: "",
        autocomplete: "",
        spellcheck: true,
        hidden: false,
        disabled: false,
        checked: false,
        value: "",
        files: null,
        textContent: "",
        style: {},
        children: [],
        parent: null,
        //: Set by the dialog's own showModal()/close(), so a test can tell an
        //: element that exists from one the user can actually see.
        modalOpen: false,
        focused: false,
        get className() { return Array.from(classes).join(" "); },
        set className(value) {
            classes.clear();
            String(value).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
        },
        classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
            contains: (c) => classes.has(c),
        },
        setAttribute: (name, value) => attributes.set(name, String(value)),
        getAttribute: (name) => (attributes.has(name) ? attributes.get(name) : null),
        appendChild(child) {
            child.parent = element;
            element.children.push(child);
            return child;
        },
        append(...nodes) { nodes.forEach((node) => element.appendChild(node)); },
        replaceChildren(...nodes) {
            element.children.forEach((child) => { child.parent = null; });
            element.children = [];
            nodes.forEach((node) => element.appendChild(node));
        },
        remove() {
            const siblings = element.parent?.children;
            if (siblings) siblings.splice(siblings.indexOf(element), 1);
            element.parent = null;
        },
        focus() { element.focused = true; },
        showModal() { element.modalOpen = true; },
        close() { element.modalOpen = false; },
        addEventListener(name, fn) {
            if (!listeners.has(name)) listeners.set(name, []);
            listeners.get(name).push(fn);
        },
        /** Deliver an event the way the browser would, to this node only. */
        fire(name, event = {}) {
            (listeners.get(name) || []).forEach((fn) => fn({
                target: element, preventDefault() {}, ...event,
            }));
        },
        /** A real click, which is what every button here is driven by. */
        click() { element.fire("click"); },
        /** Every descendant carrying `className`, this node included. */
        find(className) {
            const found = [];
            const walk = (node) => {
                if (node.classList.contains(className)) found.push(node);
                node.children.forEach(walk);
            };
            walk(element);
            return found;
        },
        first(className) { return element.find(className)[0] || null; },
        /** Every descendant with this tag name. */
        tags(name) {
            const wanted = String(name).toUpperCase();
            const found = [];
            const walk = (node) => {
                if (node.tagName === wanted) found.push(node);
                node.children.forEach(walk);
            };
            walk(element);
            return found;
        },
    };
    return element;
}

/** All of a node's text, which is what a user reads off the card. */
function textOf(node) {
    let out = node.textContent || "";
    node.children.forEach((child) => { out += ` ${textOf(child)}`; });
    return out;
}

class FakeFormData {
    constructor() { this.entries = []; }
    append(name, value) { this.entries.push([name, value]); }
    get(name) {
        const found = this.entries.find(([key]) => key === name);
        return found ? found[1] : null;
    }
    has(name) { return this.entries.some(([key]) => key === name); }
}

/**
 * Load the module fresh. `reply` decides what the server says to the next
 * /upload_channels; everything else about the flow follows from that.
 */
function boot(options = {}) {
    const body = makeElement("body");
    const requests = [];
    const browseCalls = [];
    let uploadReply = { success: true, names: ["DAPI", "CD3"] };
    let exists = true;
    const applied = [];

    function respond(url) {
        if (url.includes("check_file_existence")) return exists;
        if (url.includes("upload_channels")) return uploadReply;
        return {};
    }

    const doc = {
        body,
        createElement: (tag) => makeElement(tag),
        getElementById: () => null,
        addEventListener() {},
        removeEventListener() {},
    };

    // The one fact that decides whether "Upload…" is worth offering: is the
    // machine running Plexora a different machine from this one? Asked of
    // PlexoraDataLocation, which is where every data field asks it.
    const win = {
        PlexoraDataLocation: {
            serverIsRemote: () => Boolean(options.serverIsRemote),
        },
    };

    const context = createContext({
        console, Object, Array, String, Boolean, Number, Math, JSON, Set, Map,
        Promise, Error, FormData: FakeFormData,
        window: win,
        document: doc,
        plexoraUrl: (path) => `/${path}`,
        // Real signature, recorded rather than run: what matters here is that
        // the server-side picker is asked for the channel-list filter, which
        // is the thing native_dialog.py has to know about.
        attachBrowseButton: (button, input, options) => {
            browseCalls.push(options);
        },
        Option: function Option(text, value) {
            const option = makeElement("option");
            option.textContent = text;
            option.value = value;
            return option;
        },
        fetch: (url, options) => {
            requests.push({ url, options });
            return Promise.resolve({
                ok: true,
                json: () => Promise.resolve(respond(url)),
            });
        },
    });
    runInContext(readFileSync(SOURCE, "utf8"), context, { filename: "channelNamesUpload.js" });

    win.PlexoraChannelNames.open({
        datasource: "tonsil",
        onApplied: (names) => applied.push(names),
    });

    const api = {
        applied,
        requests,
        browseCalls,
        /** What the server will say to the next upload. */
        reply(value) { uploadReply = value; },
        /** Whether the path box's live check finds anything. */
        pathExists(value) { exists = value; },
        get dialog() { return body.first("channel-names-modal"); },
        get title() { return api.dialog.first("channel-names-title").textContent; },
        get bodyText() { return textOf(api.dialog.first("channel-names-body")); },
        get error() { return api.dialog.first("channel-names-error"); },
        /** The buttons along the bottom, by their label. */
        action(label) {
            return api.dialog.first("channel-names-actions").children
                .find((node) => node.textContent === label) || null;
        },
        get actionLabels() {
            return api.dialog.first("channel-names-actions").children
                .map((node) => node.textContent);
        },
        get uploads() { return requests.filter((r) => r.url.includes("upload_channels")); },
        get lastUpload() { return api.uploads[api.uploads.length - 1]; },
        get pathInput() { return api.dialog.first("form-control"); },
        /** The controls beside the path box: Browse, maybe Upload, Load. */
        get fieldRow() { return api.dialog.first("import-field-row").children; },
        /** The hidden <input type="file">, when this arrangement offers one. */
        get chooser() {
            return api.dialog.first("import-field-row").children
                .find((node) => node.type === "file") || null;
        },
        /** Choose a file from this computer, the way the dialog sees it. */
        async pick(file) {
            api.chooser.files = [file];
            api.chooser.fire("change");
            await tick();
        },
        get loadButton() { return api.dialog.first("channel-names-load"); },
        /** Type a path, the way the box's live existence check sees it. */
        async type(path) {
            api.pathInput.value = path;
            api.pathInput.fire("input");
            await tick();
        },
        /** Type a path and load it -- the whole of the first stage. */
        async load(path) {
            await api.type(path);
            api.loadButton.click();
            await tick();
        },
    };
    return api;
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

/**
 * A three-column file the server could not read on its own.
 *
 * Column 0 is deliberately the WRONG one -- a notes column with a gap in it,
 * one name short of the image -- because column 0 is where the picker opens.
 * A fixture whose first column happens to be right would let a broken count
 * line pass unnoticed.
 */
function description(extra = {}) {
    return {
        success: false,
        needs_column: true,
        filename: "panel.xlsx",
        channel_count: 2,
        row_count: 3,
        column_count: 3,
        columns: [
            { index: 0, header: "note", nonempty: 2 },
            { index: 1, header: "marker", nonempty: 3 },
            { index: 2, header: "cycle", nonempty: 3 },
        ],
        columns_truncated: false,
        preview: [["note", "marker", "cycle"], ["nuclear", "DAPI", "1"], ["", "CD3", "2"]],
        preview_rows: 5,
        preview_columns: 5,
        header_guess: true,
        ...extra,
    };
}

// -- opening it ---------------------------------------------------------------

{
    const app = boot();
    check("open() puts a modal dialog up",
        Boolean(app.dialog) && app.dialog.modalOpen === true,
        "showModal, so it is in the top layer and clears a fullscreened viewer");
    check("...asking for a file first", app.title === "Channel names");
    check("...and saying what it will accept",
        /one name per channel/i.test(app.bodyText + textOf(app.dialog.first("channel-names-subtitle")))
        && /xlsx/i.test(textOf(app.dialog.first("channel-names-subtitle"))),
        "a spreadsheet is what a panel design is usually written in");
    check("nothing has been sent yet", app.requests.length === 0);
}

// -- one way in, not two ------------------------------------------------------

{
    const app = boot();
    check("the whole of the first stage is one path field and its buttons",
        Boolean(app.pathInput) && Boolean(app.loadButton)
        && app.dialog.tags("input").every((node) => node.type !== "file"),
        "a browser upload above this asked the user to choose between two "
        + "spellings of the same act before they had done anything");
    check("...so there is no second control offering to do it another way",
        app.dialog.first("channel-names-pick") === null
        && app.dialog.first("channel-names-or") === null);
    check("...and the box is where the caret already is",
        app.pathInput.focused === true,
        "nothing else on this stage takes typing");
}

// -- the ordinary way through -------------------------------------------------

{
    const app = boot();
    check("...and its Browse button opens the SERVER's picker, filtered for this",
        app.browseCalls.length === 1 && app.browseCalls[0].filter === "channels",
        JSON.stringify(app.browseCalls));
    check("Load stays off until there is a path", app.loadButton.disabled === true);

    await app.type("/cluster/panel.csv");
    check("a path that exists arms Load",
        app.loadButton.disabled === false,
        "checked as it is typed, not after the upload fails");

    app.loadButton.click();
    await tick();
    check("loading it uploads straight away",
        app.uploads.length === 1,
        "a one-column list is the common case and must cost no questions");
    const form = app.lastUpload.options.body;
    check("...naming the project it is for", form.get("datasource") === "tonsil");
    check("...sending the path rather than any bytes",
        form.get("path") === "/cluster/panel.csv" && form.has("file") === false,
        "the browser cannot read that file; the server can");
    check("...and asking for no column, so the server may work it out",
        form.has("column") === false,
        "one column and a matching row count is the file answering for itself");
    check("a file the server accepts closes the modal",
        app.dialog === null);
    check("...handing the caller the names it applied, in image order",
        app.applied.length === 1
        && JSON.stringify(app.applied[0]) === JSON.stringify(["DAPI", "CD3"]),
        "the page renames its channels from these rather than reloading");
}

{
    const app = boot();
    app.pathExists(false);
    await app.type("/cluster/typo.xlsx");
    check("a path that is not there leaves Load off and says so on the field",
        app.loadButton.disabled === true
        && app.pathInput.classList.contains("is-invalid"),
        "a typo in a long cluster path, reported while it is still being typed");
}

// -- the file that does not say which column ----------------------------------

{
    const app = boot();
    app.reply(description());
    await app.load("/cluster/panel.xlsx");

    check("a table of several columns asks which one holds the names",
        /which column/i.test(app.title), app.title);
    check("...saying how many columns it has and how many channels the image has",
        /3 columns/.test(textOf(app.dialog.first("channel-names-subtitle")))
        && /2 channels/.test(textOf(app.dialog.first("channel-names-subtitle"))),
        textOf(app.dialog.first("channel-names-subtitle")));
    check("...still holding the file, rather than sending the user back for it",
        app.uploads.length === 1);

    const select = app.dialog.tags("select")[0];
    check("every column is offered",
        select.children.length === 3, `${select.children.length} options`);
    check("...labelled by their headers, since the box is ticked",
        select.children[1].textContent.startsWith("marker"),
        select.children[1].textContent);
    check("...each with the number of names it would give",
        /2 names/.test(select.children[1].textContent),
        "the count is what the user is really choosing between");

    const table = app.dialog.tags("table")[0];
    check("a preview of the file is shown", Boolean(table));
    check("...with the header row drawn as headings, not as data",
        table.tags("th").map((cell) => cell.textContent).join(",") === "note,marker,cycle"
        && table.tags("tbody")[0].children.length === 2,
        "3 preview rows, one of which is the header");
    check("...and the chosen column marked in it",
        table.tags("th")[0].classList.contains("is-selected"));
}

{
    const app = boot();
    app.reply(description());
    await app.load("/cluster/panel.xlsx");

    const box = app.dialog.tags("input").find((node) => node.type === "checkbox");
    check("the header checkbox is there, and pre-ticked on the server's reading",
        Boolean(box) && box.checked === true,
        "dropping the first row is what makes a column come out at the channel count");
    check("...and it is what the user was told it is",
        /column headers/i.test(textOf(app.dialog.first("channel-names-check"))),
        textOf(app.dialog.first("channel-names-check")));

    box.checked = false;
    box.fire("change");
    const select = app.dialog.tags("select")[0];
    check("un-ticking it renames the columns by position",
        select.children[1].textContent.startsWith("Column 2"),
        select.children[1].textContent);
    check("...counts the first row as a name again",
        /3 names/.test(select.children[1].textContent),
        select.children[1].textContent);
    const table = app.dialog.tags("table")[0];
    check("...and puts that row back in the preview's body",
        table.tags("tbody")[0].children.length === 3);
}

{
    const app = boot();
    app.reply(description());
    await app.load("/cluster/panel.xlsx");

    const count = app.dialog.first("channel-names-count");
    check("the wrong column is called out before it is tried",
        /^1 name,/.test(count.textContent) && /2 channels/.test(count.textContent)
        && count.classList.contains("is-mismatch"),
        count.textContent);

    const select = app.dialog.tags("select")[0];
    select.value = "1";
    select.fire("change");
    check("...and the right one reads as right",
        /one for each/i.test(app.dialog.first("channel-names-count").textContent)
        && !app.dialog.first("channel-names-count").classList.contains("is-mismatch"),
        app.dialog.first("channel-names-count").textContent);

    app.reply({ success: true, names: ["DAPI", "CD3"] });
    app.action("Use this column").click();
    await tick();
    const form = app.lastUpload.options.body;
    check("choosing a column sends it, with the header answer",
        form.get("column") === "1" && form.get("has_header") === "true");
    check("...and the same file, not a second pick",
        form.get("path") === "/cluster/panel.xlsx",
        "the user named that file once");
    check("...and the rename goes through", app.applied.length === 1);
}

{
    const app = boot();
    app.reply(description({
        column_count: 12,
        preview: [["a", "b", "c", "d", "e"], ["1", "2", "3", "4", "5"]],
        columns: Array.from({ length: 12 }, (_, index) => (
            { index, header: `h${index}`, nonempty: 3 })),
    }));
    await app.load("/cluster/wide.csv");
    check("a wide file says the preview is only part of it",
        /5 of 12 columns/.test(app.dialog.first("channel-names-preview-note").textContent)
        && /scroll/i.test(app.dialog.first("channel-names-preview-note").textContent),
        app.dialog.first("channel-names-preview-note").textContent);

    const select = app.dialog.tags("select")[0];
    select.value = "9";
    select.fire("change");
    check("...and says when the chosen column is past it",
        /past the preview/i.test(app.dialog.first("channel-names-preview-note").textContent),
        "otherwise the highlight is simply missing and nothing explains it");
}

// -- the file that belongs to another image -----------------------------------

{
    const app = boot();
    app.reply({
        success: false, mismatch: true, filename: "panel.csv",
        marker_count: 44, channel_count: 40,
        error: "channel_names has 44 entries but 'tonsil' has 40 channels.",
    });
    await app.load("/cluster/panel.csv");

    check("a count that does not match is its own screen, not an error line",
        /does not match/i.test(app.title) && app.error.hidden === true,
        app.title);
    check("...stating both numbers",
        /44/.test(app.bodyText) && /40/.test(app.bodyText), app.bodyText);
    check("...and saying plainly that nothing happened",
        /nothing was changed/i.test(app.bodyText), app.bodyText);
    check("...having changed nothing", app.applied.length === 0);
    check("...offering another file rather than a way to apply it anyway",
        app.actionLabels.includes("Choose a different file")
        && !app.actionLabels.includes("Use this column"),
        app.actionLabels.join(" / "));
    check("...with no way back to a column picker there never was",
        !app.actionLabels.includes("Back to columns"),
        "a one-column file was never asked about, so there is nothing to reconsider");

    app.action("Choose a different file").click();
    check("...and that button really does go back to the start",
        app.title === "Channel names" && Boolean(app.pathInput));
    check("...with the path still in the box, ready to be edited",
        app.pathInput.value === "/cluster/panel.csv" && app.loadButton.disabled === false,
        "the fix is usually one directory along, not a cluster path retyped");
}

{
    const app = boot();
    app.reply(description());
    await app.load("/cluster/panel.xlsx");
    app.reply({
        success: false, mismatch: true, filename: "panel.xlsx",
        marker_count: 3, channel_count: 2, error: "…",
    });
    app.action("Use this column").click();
    await tick();
    check("a wrong column CAN be reconsidered",
        app.actionLabels.includes("Back to columns"),
        app.actionLabels.join(" / "));
    app.action("Back to columns").click();
    check("...landing back on the picker with the file still in hand",
        /which column/i.test(app.title) && Boolean(app.dialog.tags("table")[0]));
}

// -- a file that could not be read at all -------------------------------------

{
    const app = boot();
    app.reply({ success: false, error: "notes.pdf is not a .csv, .tsv, .txt, .xlsx or .xlsm file." });
    await app.load("/cluster/notes.pdf");
    check("a file the server cannot read says why, in its words",
        app.error.hidden === false && /notes\.pdf/.test(app.error.textContent),
        app.error.textContent);
    check("...on the stage the user is already on",
        app.title === "Channel names" && Boolean(app.pathInput),
        "there is nothing to move on to -- they need to pick something else");
    check("...and nothing was applied", app.applied.length === 0);
}

// -- the machine running Plexora is somewhere else ----------------------------
//
// Then Browse and the path box mean the SERVER's filesystem, and a marker list
// sitting on this laptop -- which is where it usually is, because it came from
// a collaborator by email -- has no way in at all.

{
    const app = boot();
    check("on a desktop launch there is one way in, not two",
        app.chooser === null,
        "Browse writes a path into the box: the same act, spelled twice");
}

{
    const app = boot({ serverIsRemote: true });
    check("with Plexora running elsewhere, the bytes can be sent instead",
        Boolean(app.chooser)
        && app.fieldRow.some((node) => node.textContent === "Upload\u2026"));
    check("...and the hint says which control means which machine",
        /machine running Plexora/.test(app.bodyText)
        && /from this computer/.test(app.bodyText),
        app.bodyText);

    await app.pick({ name: "markers.csv" });
    const sent = app.lastUpload.options.body;
    check("choosing a file sends it, without a path to name it by",
        sent.get("file").name === "markers.csv" && sent.has("path") === false,
        "the route prefers the upload and would ignore a path sent beside it");
    check("...and the names it came back with are applied",
        app.applied.length === 1);
}

{
    // A second stage must not lose the file, for the same reason it must not
    // lose a typed path: picking a column is not choosing the file again.
    const app = boot({ serverIsRemote: true });
    app.reply(description({ filename: "panel.csv" }));
    await app.pick({ name: "panel.csv" });
    app.reply({ success: true, names: ["DAPI", "CD3"] });
    app.action("Use this column").click();
    await tick();
    check("a file staged from this computer survives being asked about columns",
        app.lastUpload.options.body.get("file").name === "panel.csv",
        "otherwise the second request has nothing to read");
}

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
