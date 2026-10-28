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
 *   3. **The box says which machine it means.** It used to mean one machine,
 *      decided before Plexora started, and a marker list anywhere else had no
 *      way in -- so the row carries the Local/Remote switch every other path
 *      field carries, and what it settles is where the file is read: a path
 *      the server can open goes over as a path, and a file on a data node is
 *      fetched through the relay and posted as bytes.
 *   4. **The whole dialog takes a dropped file, and a drop loads it.** Its
 *      path row used to be the only drop target: a markers.csv let go on the
 *      title or the buttons was claimed by nothing and fell through to the
 *      Import dialog, which read it as a sample.
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
    //: What the node relay was asked for: the half of "which machine" that
    //: leaves this file.
    const reads = [];
    //: Every chip a stage mounted, newest last (the stub below).
    const chips = [];
    //: What whenConnected was handed, so a test says when the list arrives.
    let announce = null;
    let uploadReply = { success: true, names: ["DAPI", "CD3"] };
    let exists = true;
    let readFails = null;
    const applied = [];
    //: Every drop zone the dialog registered: {zone, spec}.
    const dropZones = [];

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

    // The world the dialog asks about which machines exist. `serverIsRemote`
    // is the fact the whole arrangement turns on -- is the machine running
    // Plexora a different machine from this one? -- and it is asked of
    // PlexoraDataLocation, which is where every data field asks it.
    const win = {
        PlexoraDataLocation: {
            serverIsRemote: () => Boolean(options.serverIsRemote),
            clientNode: () => options.clientNode || "",
            whenConnected: (apply) => { announce = apply; },
        },
        //: The machine chip, stubbed: its menu is machine_chip_probe.mjs's.
        //: What this file tests is what the DIALOG does with a pick, so the
        //: stub keeps what the dialog gave it and a test picks through that.
        PlexoraMachineChip: {
            create(chipOptions) {
                const chip = { element: makeElement("button"), options: chipOptions,
                               paint() {} };
                chip.element.className = "plx-machine-chip";
                chips.push(chip);
                return chip;
            },
            wrap(input, chip) {
                const box = makeElement("div");
                box.className = "plx-machine-field";
                const parent = input.parent;
                parent.children.splice(parent.children.indexOf(input), 1, box);
                box.parent = parent;
                input.parent = null;
                box.append(chip.element, input);
                return box;
            },
        },
        //: The node relay, which is the whole reason a file on a cluster can
        //: reach this dialog at all. Answers with a File, as the real one does.
        PlexoraFileLocation: {
            read: (node, path) => {
                reads.push({ node, path });
                return readFails
                    ? Promise.reject(new Error(readFails))
                    : Promise.resolve({ name: path.split("/").pop() });
            },
        },
    };
    if (options.desktop !== false) {
        win.PlexoraDesktop = {
            fileFromPath: (path) => Promise.resolve({ name: path.split("/").pop(), path }),
        };
    }
    if (options.connects !== undefined) {
        win.PlexoraConnectionModal = {
            open: () => Promise.resolve(options.connects),
        };
    }

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
        // browsePicker's registry, recorded: path_drop_probe.mjs holds what it
        // does with a zone; this file holds which zone the dialog registers.
        attachDropTarget: (zone, spec) => {
            const entry = { zone, spec, attached: true };
            dropZones.push(entry);
            return () => { entry.attached = false; };
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

    if (options.open !== false) {
        win.PlexoraChannelNames.open({
            datasource: "tonsil",
            onApplied: (names) => applied.push(names),
        });
    }

    const api = {
        win,
        body,
        dropZones,
        /** The zone the open dialog registered, still attached. */
        get dropZone() { return dropZones.filter((z) => z.attached).at(-1) || null; },
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
        get browseButton() {
            return api.fieldRow.find((node) => node.textContent === "Browse…");
        },
        /** Which machine Browse would open a dialog on, asked as it is asked. */
        get browseNode() { return api.browseCalls[0].node(); },
        /** The chip the stage on screen mounted. */
        get chip() { return chips.at(-1); },
        /** Where the chip says the box is: "local" or "remote". */
        get where() { return api.chip.options.current().where; },
        /** The machine the chip names, when it is not this computer. */
        get machine() {
            const place = api.chip.options.current().place;
            return place ? place.label : null;
        },
        /** The X in the dialog's corner. */
        get closeButton() { return api.dialog.first("channel-names-close"); },
        get hint() { return api.dialog.first("field-hint").textContent; },
        get locationStatus() {
            return api.dialog.first("data-location-status").textContent;
        },
        /** Pick on the chip, as its menu would, and let it settle. */
        async pickMachine(where, place = null) {
            api.chip.options.onPick(where, place);
            await tick();
            await tick();
        },
        /** The list of machines arriving, with this one connected. */
        async connected(place) {
            if (announce) announce(place);
            await tick();
        },
        get reads() { return reads; },
        /** Make the next read through the node fail, the way a dead one does. */
        readFails(message) { readFails = message; },
        /** The Upload… button, when this arrangement has built one. */
        get uploadButton() {
            return api.fieldRow.find(
                (node) => node.textContent === "Upload…") || null;
        },
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
    check("the first stage has no Cancel: the X in the corner is the way out",
        app.actionLabels.length === 0 && app.closeButton !== null
        && app.closeButton.getAttribute("aria-label") === "Close");
    app.closeButton.click();
    check("...and it closes the dialog", app.dialog === null);
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
    check("...and its Browse button opens a picker filtered for this",
        app.browseCalls.length === 1 && app.browseCalls[0].filter === "channels",
        JSON.stringify(app.browseCalls));
    check("...on the machine the switch is pointing at, asked at click time",
        typeof app.browseCalls[0].node === "function" && app.browseNode === null,
        "wired once, but the switch beside it can be flipped afterwards");
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
    check("...and no Close beside it: the X is the way out at every stage",
        !app.actionLabels.includes("Close") && app.closeButton !== null,
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
// Then the box means the SERVER's filesystem, and a marker list sitting on this
// laptop -- which is where it usually is, because it came from a collaborator
// by email -- can only get in as bytes.

{
    const app = boot();
    check("on a desktop launch there is one way in, not two",
        app.chooser === null,
        "Browse writes a path into the box: the same act, spelled twice");
    check("...and the chip opens on the side the box has always meant",
        app.where === "local",
        "here that side IS the machine running Plexora, so nothing changes");
}

{
    const app = boot({ serverIsRemote: true });
    check("with Plexora elsewhere, the box still opens meaning the server",
        app.where === "remote" && app.machine === "This Plexora server"
        && app.pathInput.disabled === false,
        "a switch appearing beside it must not change the ordinary route in");
    check("...with the hint saying which machine that is",
        /machine running Plexora/.test(app.hint), app.hint);
    check("...and the bytes are not offered as well, on the side that is a path",
        app.uploadButton !== null && app.uploadButton.hidden === true,
        "two ways to name one machine is the pair the chip exists to end");

    await app.pickMachine("local");
    check("this computer is one pick away, and says what it can and cannot do",
        app.where === "local" && app.pathInput.disabled === true
        && app.uploadButton.hidden === false
        && /cannot read paths on this computer/.test(app.hint),
        app.hint);
    check("...and Browse goes with the box, rather than opening the server's",
        app.browseButton.disabled === true,
        "a dialog on a machine nobody chose is worse than no dialog");

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
    await app.pickMachine("local");
    app.reply(description({ filename: "panel.csv" }));
    await app.pick({ name: "panel.csv" });
    app.reply({ success: true, names: ["DAPI", "CD3"] });
    app.action("Use this column").click();
    await tick();
    check("a file staged from this computer survives being asked about columns",
        app.lastUpload.options.body.get("file").name === "panel.csv",
        "otherwise the second request has nothing to read");
}

{
    // The one arrangement where "this computer" is nameable by path from a
    // Plexora running somewhere else: the node `plexora connect` started here.
    const app = boot({ serverIsRemote: true, clientNode: "laptop" });
    await app.pickMachine("local");
    check("a client node makes this computer's paths readable again",
        app.pathInput.disabled === false && app.browseButton.disabled === false
        && app.uploadButton.hidden === true,
        "sending the bytes is the fallback for having no node, not the point");
    check("...and Browse asks that node rather than the server",
        app.browseNode === "laptop", String(app.browseNode));
}

// -- a marker list on the cluster ---------------------------------------------
//
// The case that had no way in at all: the panel is beside the image on a
// machine reached through a data node, which is neither the browser's
// filesystem nor the server's.

const CLUSTER = [
    { id: "server", kind: "server", label: "This Plexora server" },
    { id: "HMS-O2", kind: "remote", label: "HMS-O2", node: "HMS-O2" },
];

{
    const app = boot();
    check("on a desktop launch the chip starts on this computer",
        app.where === "local");

    await app.pickMachine("remote", CLUSTER[1]);
    check("picking a machine on the chip puts the box on it",
        app.where === "remote" && app.machine === "HMS-O2");
    check("...and the box empties, because that path meant another machine",
        app.pathInput.value === "" && app.loadButton.disabled === true);
    check("...with Browse now asking the node",
        app.browseNode === "HMS-O2", String(app.browseNode));
    check("...and the hint naming the machine the box now means",
        /HMS-O2/.test(app.hint) && /reads the file from there/.test(app.hint),
        app.hint);

    await app.type("/scratch/panel.csv");
    check("a path on the node is not checked against the server's disk",
        app.requests.every((r) => !r.url.includes("check_file_existence"))
        && app.loadButton.disabled === false,
        "a stat relayed to a cluster per keystroke, for a box Load follows anyway");

    app.loadButton.click();
    await tick();
    await tick();
    check("loading it reads the bytes through the node",
        app.reads.length === 1 && app.reads[0].node === "HMS-O2"
        && app.reads[0].path === "/scratch/panel.csv",
        JSON.stringify(app.reads));
    const form = app.lastUpload.options.body;
    check("...and posts them as an upload, since the server has no path for it",
        form.get("file").name === "panel.csv" && form.has("path") === false,
        "a marker list is kilobytes; the same trick would be wrong for the image");
    check("...applying the names it came back with", app.applied.length === 1);
}

// -- a machine that is already connected ----------------------------------------
//
// Connecting a server is how somebody says where their data is. A dialog that
// still opens on this computer makes them say it again.

{
    const app = boot();
    await app.connected(CLUSTER[1]);
    check("a machine already connected is chosen when the dialog opens",
        app.where === "remote" && app.machine === "HMS-O2"
        && app.browseNode === "HMS-O2", String(app.machine));
}

{
    const app = boot();
    await app.type("/Users/me/markers.csv");
    await app.connected(CLUSTER[1]);
    check("...but not over a path typed before the list arrived",
        app.where === "local" && app.pathInput.value === "/Users/me/markers.csv");
}

{
    const app = boot();
    await app.pickMachine("local");
    await app.connected(CLUSTER[1]);
    check("...nor over a hand on Local",
        app.where === "local");
}

{
    const app = boot({ serverIsRemote: true });
    await app.connected(CLUSTER[1]);
    check("...and a Plexora running elsewhere keeps the server, where the image is",
        app.where === "remote" && app.machine === "This Plexora server");
}

{
    const app = boot();
    await app.pickMachine("remote", CLUSTER[1]);
    app.readFails("HMS-O2 stopped answering.");
    await app.type("/scratch/panel.csv");
    app.loadButton.click();
    await tick();
    await tick();
    check("a node that stops answering says so, and nothing is sent",
        app.error.hidden === false && /stopped answering/.test(app.error.textContent)
        && app.uploads.length === 0,
        app.error.textContent);
    check("...leaving the path in the box and Load armed to try again",
        app.pathInput.value === "/scratch/panel.csv"
        && app.loadButton.disabled === false,
        "a cluster that was busy for a second is not a file to re-choose");
}

{
    // The machine survives the stages, for the same reason the path does: being
    // sent back to the wrong machine is the modal losing the answer it asked
    // for.
    const app = boot();
    await app.pickMachine("remote", CLUSTER[1]);
    app.reply(description({ filename: "panel.csv" }));
    await app.type("/scratch/panel.csv");
    app.loadButton.click();
    await tick();
    await tick();
    app.action("Choose a different file").click();
    check("going back for another file lands on the machine the last one was on",
        app.where === "remote" && app.machine === "HMS-O2"
        && app.pathInput.value === "/scratch/panel.csv",
        "the alternative is answering 'which machine' once per attempt");
}

// -- a file dropped on the dialog ------------------------------------------------

{
    const app = boot();
    const zone = app.dropZone;
    check("the whole dialog is the drop target, not its path row",
        zone && zone.zone === app.dialog && zone.spec.filter === "channels"
        && app.browseCalls[0].drop === false);
    check("...and it takes one file, refusing several with a reason",
        zone.spec.count === "one" && /one file/i.test(zone.spec.several || ""));
    zone.spec.onDrop("/data/run/markers.csv");
    check("a drop is a Load at once: the file goes to the server as a path",
        app.uploads.length === 1
        && app.lastUpload.options.body.get("path") === "/data/run/markers.csv"
        && !app.lastUpload.options.body.has("file"));
    check("...and while it is read the dialog is not a drop target",
        zone.spec.enabled() === false);
    await tick();
    await tick();
    check("...and the names are applied", app.applied.length === 1);
    check("closing the dialog takes its zone away", app.dropZone === null);
}

{
    const app = boot();
    app.reply(description());
    app.dropZone.spec.onDrop("/data/panel.xlsx");
    await tick();
    await tick();
    const refusal = app.dropZone.spec.accepts("/data/other.csv", "other.csv");
    check("while a column is being chosen a drop is refused, with what to do instead",
        app.title === "Which column has the channel names?"
        && typeof refusal === "string" && /different file/.test(refusal));
}

{
    const app = boot();
    app.reply({ success: false, mismatch: true, marker_count: 3, channel_count: 2,
                filename: "markers.csv" });
    app.dropZone.spec.onDrop("/data/markers.csv");
    await tick();
    await tick();
    app.reply({ success: true, names: ["DAPI", "CD3"] });
    const accepted = app.dropZone.spec.accepts("/data/right.csv", "right.csv");
    app.dropZone.spec.onDrop("/data/right.csv");
    await tick();
    await tick();
    check("after a mismatch a second file can be dropped straight on",
        accepted === true && app.lastUpload.options.body.get("path") === "/data/right.csv"
        && app.applied.length === 1);
}

{
    const app = boot();
    await app.pickMachine("remote", CLUSTER[1]);
    app.dropZone.spec.onDrop("/Users/me/markers.csv");
    check("a file dropped from this computer moves the chip back to Local",
        app.where === "local" && app.uploads.length === 1
        && app.lastUpload.options.body.get("path") === "/Users/me/markers.csv"
        && app.reads.length === 0);
}

{
    const app = boot({ desktop: false });
    const file = { name: "markers.csv" };
    let stopped = false;
    app.dialog.fire("drop", {
        dataTransfer: { types: ["Files"], files: [file] },
        stopPropagation() { stopped = true; },
    });
    await tick();
    check("in a browser tab a file dropped on the dialog is sent as bytes",
        stopped && app.uploads.length === 1
        && app.lastUpload.options.body.get("file") === file);
}

{
    const app = boot({ open: false });
    const names = [];
    await app.win.PlexoraChannelNames.apply({
        datasource: "tonsil", path: "/data/markers.csv", column: 2, hasHeader: true,
        onApplied: (got) => names.push(got),
    });
    const body = app.lastUpload.options.body;
    check("apply() renames with no dialog when the server needs nothing more",
        names.length === 1 && app.dialog === null
        && body.get("path") === "/data/markers.csv" && body.get("column") === "2"
        && body.get("has_header") === "true");
}

{
    const app = boot({ open: false });
    app.reply(description());
    await app.win.PlexoraChannelNames.apply({ datasource: "tonsil", path: "/data/panel.xlsx" });
    check("...and opens the dialog only to ask which column",
        app.dialog && app.dialog.modalOpen && app.title === "Which column has the channel names?");
}

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
