/**
 * Naming an image's channels from a file, as the viewer asks for it.
 *
 * Three things run through every check below, and each is something a user
 * cannot verify for themselves without losing work finding out they were
 * wrong:
 *
 *   1. **The file is picked once.** Being sent back to the file picker because
 *      a checkbox was ticked, or because the count was wrong, is the modal
 *      throwing away the only thing it asked for. Every stage re-posts the
 *      same source.
 *   2. **A wrong file changes nothing.** A mismatch is a stage, not a partial
 *      rename -- an image whose first thirty channels are named and whose last
 *      ten are still Channel_30 looks named, and gating would believe it.
 *   3. **There are two ways in.** The upload is the ordinary one; a path on
 *      the machine running the server is the only one an HPC user has, because
 *      their browser cannot see the filesystem the image is on.
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
function boot() {
    const body = makeElement("body");
    const requests = [];
    const browseCalls = [];
    let uploadReply = { success: true };
    let exists = true;
    const applied = [];

    function respond(url, options) {
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

    const win = {};

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
                json: () => Promise.resolve(respond(url, options)),
            });
        },
    });
    runInContext(readFileSync(SOURCE, "utf8"), context, { filename: "channelNamesUpload.js" });

    win.PlexoraChannelNames.open({
        datasource: "tonsil",
        onApplied: () => applied.push(true),
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
        /** Pretend the user picked `name` in the browser's file dialog. */
        pickFile(name) {
            const input = api.dialog.first("channel-names-file");
            input.files = [{ name }];
            input.fire("change");
        },
        get pathInput() { return api.dialog.first("form-control"); },
        get loadButton() { return api.dialog.first("channel-names-load"); },
        async type(path) {
            api.pathInput.value = path;
            api.pathInput.fire("input");
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

// -- the ordinary way in: a file from this computer ---------------------------

{
    const app = boot();
    app.pickFile("markers.csv");
    await tick();

    check("picking a file uploads it straight away",
        app.uploads.length === 1,
        "a one-column list is the common case and must cost no questions");
    const form = app.lastUpload.options.body;
    check("...naming the project it is for", form.get("datasource") === "tonsil");
    check("...carrying the file itself", form.get("file").name === "markers.csv");
    check("...and asking for no column, so the server may work it out",
        form.has("column") === false,
        "one column and a matching row count is the file answering for itself");
    check("a file the server accepts closes the modal",
        app.dialog === null);
    check("...and tells the caller, which is what reloads the page",
        app.applied.length === 1);
}

// -- the HPC way in: a path on the machine running the server ------------------

{
    const app = boot();
    check("the path box is offered without being asked for",
        Boolean(app.pathInput) && Boolean(app.loadButton),
        "on a cluster there is nothing local to upload -- this is the only way in");
    check("...and its Browse button opens the SERVER's picker, filtered for this",
        app.browseCalls.length === 1 && app.browseCalls[0].filter === "channels",
        JSON.stringify(app.browseCalls));
    check("Load stays off until there is a path", app.loadButton.disabled === true);

    await app.type("/cluster/panel.xlsx");
    check("a path that exists arms Load",
        app.loadButton.disabled === false,
        "checked as it is typed, not after the upload fails");

    app.loadButton.click();
    await tick();
    const form = app.lastUpload.options.body;
    check("loading it sends the path rather than any bytes",
        form.get("path") === "/cluster/panel.xlsx" && form.has("file") === false,
        "the browser cannot read that file; the server can");
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
    app.pickFile("panel.xlsx");
    await tick();

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
    app.pickFile("panel.xlsx");
    await tick();

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
    app.pickFile("panel.xlsx");
    await tick();

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

    app.reply({ success: true });
    app.action("Use this column").click();
    await tick();
    const form = app.lastUpload.options.body;
    check("choosing a column sends it, with the header answer",
        form.get("column") === "1" && form.get("has_header") === "true");
    check("...and the same file, not a second pick",
        form.get("file").name === "panel.xlsx",
        "the user chose that file once");
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
    app.pickFile("wide.csv");
    await tick();
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
    app.pickFile("panel.csv");
    await tick();

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
}

{
    const app = boot();
    app.reply(description());
    app.pickFile("panel.xlsx");
    await tick();
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
    app.pickFile("notes.pdf");
    await tick();
    check("a file the server cannot read says why, in its words",
        app.error.hidden === false && /notes\.pdf/.test(app.error.textContent),
        app.error.textContent);
    check("...on the stage the user is already on",
        app.title === "Channel names" && Boolean(app.pathInput),
        "there is nothing to move on to -- they need to pick something else");
    check("...and nothing was applied", app.applied.length === 0);
}

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
