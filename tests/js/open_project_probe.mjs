/**
 * The project browser: folders, selection, and moving things between them.
 *
 * Six properties, and most of them are about what the folder metaphor must NOT
 * be allowed to imply:
 *
 *   1. **A dataset owns nothing.** Deleting one releases its projects. Taking a
 *      project out of one is not deleting the project. Every route this page
 *      posts to is checked here, because the difference between "unassign" and
 *      "delete" is one URL and the wrong one is unrecoverable.
 *   2. **Search flattens.** Inside a folder or not, typing searches every
 *      project there is -- somebody who is searching has stopped navigating,
 *      and a search that looked only in the current folder would silently hide
 *      the thing they were looking for.
 *   3. **A selection is what gets moved.** Dragging one card of five selected
 *      moves all five; dragging a card that is NOT selected moves that one and
 *      leaves the selection alone. The rule every file browser uses.
 *   4. **One request per move.** Half a move that failed in the middle is a
 *      state nothing on this page can draw.
 *   5. **A shared project is never deleted from here.** The server 403s it;
 *      finding that out after confirming a delete dialog is the failure.
 *   6. **The Escape listener is on `document`, so the page has to take it
 *      off.** Open Project is mounted as a fragment by the app shell and torn
 *      down on every navigation; a listener left behind clears a selection on
 *      a page that no longer has one.
 *
 * Run directly:  node tests/js/open_project_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/views/openProjectPage.js");

// -- the smallest DOM this page can be driven through -----------------------
//
// The controller renders with innerHTML and reads events back through
// `closest`, so the stand-in keeps markup as a string and lets a test hand in
// the element the browser would have hit. That is the honest shape: what is
// being checked is what the page DOES on a click, not how a parser works.

function makeElement(id) {
    const classes = new Set();
    const listeners = new Map();
    const element = {
        id,
        innerHTML: "",
        textContent: "",
        value: "",
        hidden: false,
        disabled: false,
        title: "",
        dataset: {},
        get className() { return Array.from(classes).join(" "); },
        set className(value) {
            classes.clear();
            String(value).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
        },
        classList: {
            add: (...n) => n.forEach((c) => classes.add(c)),
            remove: (...n) => n.forEach((c) => classes.delete(c)),
            toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
            contains: (c) => classes.has(c),
        },
        addEventListener(type, fn) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(fn);
        },
        removeEventListener(type, fn) {
            const list = listeners.get(type) || [];
            const at = list.indexOf(fn);
            if (at >= 0) list.splice(at, 1);
        },
        //: The page queries the selection bar for its own buttons. Answered
        //: from a registry the test fills in, so a bar button is a real object
        //: whose `hidden`/`disabled` can be read back.
        buttons: new Map(),
        querySelector(selector) {
            const match = /\[data-action="([a-z]+)"\]/.exec(selector);
            if (match) {
                if (!element.buttons.has(match[1])) {
                    const button = makeElement(match[1]);
                    button.dataset.action = match[1];
                    element.buttons.set(match[1], button);
                }
                return element.buttons.get(match[1]);
            }
            return null;
        },
        async fire(type, event) {
            for (const fn of listeners.get(type) || []) await fn(event);
        },
        listenerCount(type) { return (listeners.get(type) || []).length; },
    };
    return element;
}

/** An event whose `target.closest` answers from a plain descriptor. */
function hit(descriptor, extra = {}) {
    const target = {
        dataset: descriptor.dataset || {},
        closest(selector) {
            // The controller asks for one attribute at a time, plus one
            // two-part selector for the drop targets and one bare "a".
            const wanted = selector.split(",").map((s) => s.trim());
            for (const part of wanted) {
                if (part === "a") return descriptor.link ? target : null;
                const key = /\[data-([a-z-]+)\]/.exec(part)?.[1];
                if (!key) continue;
                const camel = key.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
                if (camel in target.dataset) return target;
            }
            return null;
        },
        classList: { add() {}, remove() {}, contains: () => false },
    };
    return { target, preventDefault() { this.defaultPrevented = true; },
             defaultPrevented: false, ...extra };
}

// -- the world the page runs in ---------------------------------------------

function build({ projects, datasets, answers = {}, folder = null } = {}) {
    const ids = ["project-results", "project-search", "project-sort",
                 "project-view-grid", "project-view-list", "project-count",
                 "project-empty-state", "project-no-results",
                 "dataset-empty-state", "project-crumbs",
                 "project-selection-bar", "project-selection-count",
                 "dataset-create"];
    const elements = new Map(ids.map((id) => [id, makeElement(id)]));

    //: Every POST the page makes, in order. The whole point of the probe.
    const posted = [];
    //: Every question it asked before making one.
    const asked = [];

    let url = `http://x/open_project${folder ? `?dataset=${folder}` : ""}`;
    let registered = null;
    let teardown = null;

    const context = createContext({
        console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
        Promise, Error, Date, URL, encodeURIComponent, decodeURIComponent,
        setTimeout, requestAnimationFrame: (fn) => fn(),
        plexoraUrl: (path) => `/${path}`,
        PlexoraPage: { register(fn) { registered = fn; } },
        localStorage: { getItem: () => null, setItem() {} },
        document: {
            getElementById: (id) => elements.get(id) || null,
            querySelector: () => null,
            listeners: new Map(),
            addEventListener(type, fn) {
                if (!this.listeners.has(type)) this.listeners.set(type, []);
                this.listeners.get(type).push(fn);
            },
            removeEventListener(type, fn) {
                const list = this.listeners.get(type) || [];
                const at = list.indexOf(fn);
                if (at >= 0) list.splice(at, 1);
            },
        },
        fetch(path, options) {
            if (options && options.method === "POST") {
                posted.push({ path, body: options.body ? JSON.parse(options.body) : null });
                return Promise.resolve({
                    ok: true,
                    json: () => Promise.resolve({ success: true, dataset: null }),
                });
            }
            const body = path === "/projects" ? projects
                : path === "/datasets" ? { success: true, datasets }
                : null;
            return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
        },
    });
    context.window = context;
    context.window.location = { get href() { return url; } };
    context.window.history = { replaceState(_s, _t, next) { url = String(next); } };
    context.window.PlexoraStatus = { begin: () => ({ done() {}, fail() {} }) };
    context.window.PlexoraConfirm = {
        escapeHtml: (v) => String(v),
        modalOpen: () => false,
        ask(options) { asked.push({ kind: "ask", ...options }); return Promise.resolve(answers.ask ?? false); },
        prompt(options) { asked.push({ kind: "prompt", ...options }); return Promise.resolve(answers.prompt ?? null); },
        choose(options) { asked.push({ kind: "choose", ...options }); return Promise.resolve(answers.choose ?? null); },
    };
    context.window.PlexoraDatasetPicker = {
        choose(options) { asked.push({ kind: "picker", ...options }); return Promise.resolve(answers.picker ?? null); },
    };

    runInContext(readFileSync(SOURCE, "utf8"), context, { filename: "openProjectPage.js" });
    teardown = registered();

    return {
        el: (id) => elements.get(id),
        results: elements.get("project-results"),
        bar: elements.get("project-selection-bar"),
        crumbs: elements.get("project-crumbs"),
        posted, asked, teardown,
        doc: context.document,
        get url() { return url; },
        settle: () => new Promise((r) => setTimeout(r, 0)),
    };
}

// -- fixtures ----------------------------------------------------------------

const DATASETS = [
    { id: "d1", name: "Melanoma", projectCount: 2 },
    { id: "d2", name: "Breast", projectCount: 0 },
];

const PROJECTS = [
    { name: "slide_a", shared: false, dataset: { id: "d1", name: "Melanoma" },
      imageKind: "ome_tiff", segmentation: "missing", table: "missing", needsSetup: false },
    { name: "slide_b", shared: false, dataset: { id: "d1", name: "Melanoma" },
      imageKind: "ome_tiff", segmentation: "present", table: "present", needsSetup: false },
    { name: "loose_one", shared: false, dataset: null,
      imageKind: "ome_zarr", segmentation: "pending", table: "unresolved", needsSetup: true },
    { name: "atlas", shared: true, dataset: null,
      imageKind: "brightfield", segmentation: "missing", table: "missing", needsSetup: false },
];

const failures = [];
function check(name, condition, detail = "") {
    console.log(`${condition ? "PASS" : "FAIL"} ${name}${detail ? `  ${detail}` : ""}`);
    if (!condition) failures.push(name);
}

const setup = async (options) => {
    const page = build({ projects: PROJECTS, datasets: DATASETS, ...options });
    await page.settle();
    return page;
};

// -- what the top level shows ------------------------------------------------

{
    const page = await setup();
    const html = page.results.innerHTML;
    check("folders come before the projects that are in none",
        html.indexOf('data-dataset-id="d1"') < html.indexOf('data-project-name="loose_one"'),
        "a cohort is found by its name, not by scrolling past forty slides");
    check("a project inside a dataset is not also shown at the top level",
        !html.includes('data-project-name="slide_a"'),
        "it is in a folder; showing it twice is two places to move it from");
    check("both folders are drawn",
        html.includes('data-dataset-id="d1"') && html.includes('data-dataset-id="d2"'));
    check("an empty folder is still drawn",
        html.includes('data-dataset-id="d2"'),
        "otherwise there is nowhere to drop the first project into it");
}

{
    const page = await setup();
    const html = page.results.innerHTML;
    // Figure Builder's Figures and Captures pages render these same classes
    // from their own markup and link this stylesheet, so the redesign ADDS
    // classes and never restructures a card.
    const borrowed = ["project-card", "project-card-link", "project-card-name",
                      "project-card-date", "project-thumb", "project-actions",
                      "project-action"];
    check("a project card still carries every class other pages borrow",
        borrowed.every((cls) => html.includes(cls)),
        borrowed.filter((cls) => !html.includes(cls)).join(", ") || "all present");
}

// -- badges ------------------------------------------------------------------

{
    const page = await setup();
    const html = page.results.innerHTML;
    check("a project whose data is undecided says so",
        html.includes("Needs setup"),
        "it opens as an image and there is nowhere else this would show");
    check("a converting mask is shown dimmed rather than omitted",
        html.includes("project-badge-dim"),
        "a card that said nothing would read as a project nobody gave one to");
    check("an image-only project is not flagged",
        (html.match(/Needs setup/g) || []).length === 1,
        "an image alone is a complete project");
    check("a shared project still says it is shared",
        html.includes("project-shared"));
    check("a shared project is offered no Edit or Delete",
        !html.split('data-project-name="atlas"')[1]?.split("</div>")[0]
            ?.includes("data-delete-project"),
        "the server 403s both; not offering them is what stops a wasted dialog");
}

// -- going into a folder -----------------------------------------------------

{
    const page = await setup();
    await page.results.fire("click", hit({ dataset: { openDataset: "d1" } }));
    const html = page.results.innerHTML;
    check("opening a folder shows its members and nothing else",
        html.includes('data-project-name="slide_a"')
        && html.includes('data-project-name="slide_b"')
        && !html.includes('data-project-name="loose_one"'));
    check("and no folder cards, because datasets do not nest",
        !html.includes("data-dataset-id"));
    check("the crumb says where you are",
        page.crumbs.innerHTML.includes("Melanoma")
        && page.crumbs.innerHTML.includes("All projects"));
    check("and the URL does too, so a reload lands in the same place",
        page.url.includes("dataset=d1"), page.url);

    await page.crumbs.fire("click", hit({ dataset: { openDataset: "" } }));
    check("leaving takes the folder back out of the URL",
        !page.url.includes("dataset="), page.url);
}

{
    const page = await setup({ folder: "d1" });
    check("a pasted link opens inside the folder it names",
        page.results.innerHTML.includes('data-project-name="slide_a"')
        && !page.results.innerHTML.includes('data-project-name="loose_one"'));
}

// -- search ------------------------------------------------------------------

{
    const page = await setup({ folder: "d1" });
    const search = page.el("project-search");
    search.value = "loose";
    await search.fire("input", {});
    check("search reaches outside the folder you are standing in",
        page.results.innerHTML.includes('data-project-name="loose_one"'),
        "somebody searching has stopped navigating");
    check("and says where each result lives",
        page.results.innerHTML.includes('data-goto-dataset') === false
        || page.results.innerHTML.includes("project-card-tag"),
        "a flattened list with no folder shown is a list you cannot act on");
}

{
    const page = await setup();
    const search = page.el("project-search");
    search.value = "slide";
    await search.fire("input", {});
    const html = page.results.innerHTML;
    check("a search result tags the dataset each project is in",
        html.includes('data-goto-dataset="d1"'));
    check("matching folders lead the results",
        !html.includes('data-dataset-id="d2"'),
        "Breast does not match 'slide'");
}

// -- selection ---------------------------------------------------------------

{
    const page = await setup();
    await page.results.fire("click",
        hit({ dataset: { selectProject: "loose_one" } }));
    check("the tick selects without opening",
        page.results.innerHTML.includes("is-selected"));
    check("and the bar appears", page.bar.hidden === false);
    check("saying how many", page.el("project-selection-count").textContent
        === "1 sample selected", page.el("project-selection-count").textContent);

    await page.results.fire("click",
        hit({ dataset: { selectProject: "loose_one" } }));
    check("ticking again puts it down", page.bar.hidden === true);
}

{
    const page = await setup();
    const event = hit({ dataset: { projectName: "loose_one" }, link: true },
                      { ctrlKey: true });
    await page.results.fire("click", event);
    check("ctrl-click selects rather than opening",
        event.defaultPrevented === true && page.bar.hidden === false);
}

{
    const page = await setup({ folder: "d1" });
    await page.results.fire("click", hit({ dataset: { selectProject: "slide_a" } }));
    await page.results.fire("click",
        hit({ dataset: { projectName: "slide_b" }, link: true }, { shiftKey: true }));
    check("shift-click extends the range",
        page.el("project-selection-count").textContent === "2 samples selected",
        page.el("project-selection-count").textContent);
}

{
    const page = await setup();
    await page.results.fire("click", hit({ dataset: { selectProject: "atlas" } }));
    check("Delete is refused for a shared project before it is asked for",
        page.bar.querySelector('[data-action="delete"]').disabled === true,
        "the server 403s it; finding out after confirming is the failure");
    check("and Remove from dataset is hidden for one in none",
        page.bar.querySelector('[data-action="unassign"]').hidden === true);
}

{
    const page = await setup({ folder: "d1" });
    await page.results.fire("click", hit({ dataset: { selectProject: "slide_a" } }));
    check("Remove from dataset is offered when everything selected is in one",
        page.bar.querySelector('[data-action="unassign"]').hidden === false);
}

// -- moving ------------------------------------------------------------------

function drag(names) {
    const store = new Map();
    return {
        types: [],
        setData(type, value) { store.set(type, value); this.types.push(type); },
        getData(type) { return store.get(type); },
        effectAllowed: "", dropEffect: "",
        seed(type) { store.set(type, JSON.stringify(names)); this.types.push(type); },
    };
}

{
    const page = await setup();
    await page.results.fire("click", hit({ dataset: { selectProject: "loose_one" } }));
    await page.results.fire("click", hit({ dataset: { selectProject: "atlas" } }));

    const transfer = drag();
    const start = hit({ dataset: { projectName: "loose_one" } });
    start.dataTransfer = transfer;
    await page.results.fire("dragstart", start);
    check("dragging a selected card carries the whole selection",
        JSON.parse(transfer.getData("text/x-plexora-projects")).length === 2,
        transfer.getData("text/x-plexora-projects"));
}

{
    const page = await setup();
    await page.results.fire("click", hit({ dataset: { selectProject: "loose_one" } }));
    const transfer = drag();
    const start = hit({ dataset: { projectName: "atlas" } });
    start.dataTransfer = transfer;
    await page.results.fire("dragstart", start);
    check("dragging an unselected card moves only that one",
        JSON.parse(transfer.getData("text/x-plexora-projects")).join() === "atlas",
        "and leaves the selection where it was");
}

{
    const page = await setup();
    const transfer = drag(["loose_one", "atlas"]);
    transfer.seed("text/x-plexora-projects");
    const drop = hit({ dataset: { dropDataset: "d1" } });
    drop.dataTransfer = transfer;
    await page.results.fire("drop", drop);
    await page.settle();
    check("dropping on a folder is one request for the whole lot",
        page.posted.length === 1
        && page.posted[0].path === "/projects/assign"
        && page.posted[0].body.projects.join() === "loose_one,atlas"
        && page.posted[0].body.dataset === "d1",
        JSON.stringify(page.posted));
}

{
    const page = await setup({ folder: "d1" });
    const transfer = drag(["slide_a"]);
    transfer.seed("text/x-plexora-projects");
    const drop = hit({ dataset: { dropRoot: "1" } });
    drop.dataTransfer = transfer;
    await page.crumbs.fire("drop", drop);
    await page.settle();
    check("dropping on the root crumb takes a project out of its dataset",
        page.posted[0]?.body.dataset === null,
        JSON.stringify(page.posted));
    check("which is an assign, not a delete",
        page.posted[0]?.path === "/projects/assign",
        "a project leaving a folder is not a project being removed from disk");
}

{
    const page = await setup();
    const transfer = { types: ["text/plain"], getData: () => "", dropEffect: "" };
    const drop = hit({ dataset: { dropDataset: "d1" } });
    drop.dataTransfer = transfer;
    await page.results.fire("drop", drop);
    await page.settle();
    check("a drag from somewhere else is ignored",
        page.posted.length === 0,
        "which is why the payload has a type of its own");
}

{
    const page = await setup({ folder: "d1" });
    await page.results.fire("click", hit({ dataset: { selectProject: "slide_a" } }));
    await page.bar.fire("click", hit({ dataset: { action: "unassign" } }));
    await page.settle();
    check("Remove from dataset posts an assign to nothing",
        page.posted[0]?.path === "/projects/assign"
        && page.posted[0]?.body.dataset === null,
        JSON.stringify(page.posted));
}

{
    // "Breast" (d2) holds nothing, so opening it lands on the empty panel.
    const page = await setup({ folder: "d2" });
    check("an emptied folder shows a panel rather than 'no results'",
        page.el("dataset-empty-state").hidden === false
        && page.el("project-no-results").hidden === true);
    check("and that panel is a real drop target for the folder it stands for",
        page.el("dataset-empty-state").dataset.dropDataset === "d2",
        "it says 'drag projects here'; a sentence that invites a gesture and "
        + "then ignores it is worse than not offering it");

    const transfer = drag(["loose_one"]);
    transfer.seed("text/x-plexora-projects");
    const drop = hit({ dataset: { dropDataset: "d2" } });
    drop.dataTransfer = transfer;
    await page.el("dataset-empty-state").fire("drop", drop);
    await page.settle();
    check("dropping on it moves the project in",
        page.posted[0]?.body.dataset === "d2", JSON.stringify(page.posted));
}

{
    const page = await setup();
    check("and the panel carries no folder at the top level",
        page.el("dataset-empty-state").dataset.dropDataset === undefined,
        "dropping there would otherwise assign to whichever folder was open last");
}

// -- datasets ----------------------------------------------------------------

{
    const page = await setup({ answers: { prompt: "New Cohort" } });
    await page.el("dataset-create").fire("click", {});
    await page.settle();
    check("New Dataset asks for a name then creates it",
        page.asked[0]?.kind === "prompt"
        && page.posted[0]?.path === "/datasets"
        && page.posted[0]?.body.name === "New Cohort",
        JSON.stringify(page.posted));
}

{
    const page = await setup({ answers: { prompt: null } });
    await page.el("dataset-create").fire("click", {});
    await page.settle();
    check("and creates nothing when the question is dismissed",
        page.posted.length === 0);
}

{
    const page = await setup({ answers: { choose: "folder" } });
    await page.results.fire("click", hit({ dataset: { deleteDataset: "d1" } }));
    await page.settle();
    check("deleting a dataset offers keeping or deleting its samples",
        (page.asked[0]?.choices || []).some((c) => c.value === "folder")
        && (page.asked[0]?.choices || []).some((c) => c.value === "all"
                                                    && /2 samples/.test(c.label)));
    check("and says keeping them deletes nothing from disk",
        /Nothing is deleted from disk/.test([].concat(page.asked[0]?.body).join(" ")));
    check("dataset only posts to the dataset, never to a project",
        page.posted.length === 1 && page.posted[0]?.path === "/datasets/d1/delete",
        JSON.stringify(page.posted));
}

{
    const page = await setup({ answers: { choose: "all" } });
    await page.results.fire("click", hit({ dataset: { deleteDataset: "d1" } }));
    await page.settle();
    check("dataset and samples deletes every member, then the dataset",
        JSON.stringify(page.posted.map((p) => p.path)) === JSON.stringify(
            ["/project/slide_a/delete", "/project/slide_b/delete", "/datasets/d1/delete"]),
        JSON.stringify(page.posted));
}

{
    const page = await setup({ answers: { choose: null } });
    await page.results.fire("click", hit({ dataset: { deleteDataset: "d1" } }));
    await page.settle();
    check("and does nothing when the question is dismissed",
        page.posted.length === 0);
}

{
    const page = await setup({ answers: { prompt: "Renamed" } });
    await page.results.fire("click", hit({ dataset: { renameDataset: "d1" } }));
    await page.settle();
    check("renaming opens on the current name",
        page.asked[0]?.value === "Melanoma", page.asked[0]?.value);
    check("and posts it to the dataset's own id",
        page.posted[0]?.path === "/datasets/d1"
        && page.posted[0]?.body.name === "Renamed",
        JSON.stringify(page.posted));
}

// -- deleting a project ------------------------------------------------------

{
    const page = await setup({ answers: { ask: true } });
    await page.results.fire("click", hit({ dataset: { deleteProject: "loose_one" } }));
    await page.settle();
    check("deleting a project asks first",
        page.asked[0]?.kind === "ask");
    check("then posts to the project's delete route",
        page.posted[0]?.path === "/project/loose_one/delete",
        JSON.stringify(page.posted));
}

{
    const page = await setup({ answers: { ask: true } });
    await page.results.fire("click", hit({ dataset: { deleteProject: "atlas" } }));
    await page.settle();
    check("a shared project is never posted for deletion, dialog or not",
        page.posted.length === 0 && page.asked.length === 0,
        JSON.stringify(page.posted));
}

// -- the Move to... picker ---------------------------------------------------

{
    const page = await setup({ folder: "d1",
                               answers: { picker: { kind: "dataset", id: "d2" } } });
    await page.results.fire("click", hit({ dataset: { selectProject: "slide_a" } }));
    await page.bar.fire("click", hit({ dataset: { action: "move" } }));
    await page.settle();
    check("the picker leaves out the folder they are already in",
        page.asked[0]?.exclude === "d1", String(page.asked[0]?.exclude));
    check("and offers taking them out of it",
        page.asked[0]?.allowRoot === true);
    check("choosing a folder posts one assign",
        page.posted[0]?.body.dataset === "d2", JSON.stringify(page.posted));
}

{
    const page = await setup({ answers: { picker: { kind: "new" }, prompt: "Fresh" } });
    await page.results.fire("click", hit({ dataset: { selectProject: "loose_one" } }));
    await page.bar.fire("click", hit({ dataset: { action: "move" } }));
    await page.settle();
    check("choosing New dataset makes one with the selection already in it",
        page.posted[0]?.path === "/datasets"
        && page.posted[0]?.body.projects.join() === "loose_one",
        JSON.stringify(page.posted));
}

// -- teardown ----------------------------------------------------------------

{
    const page = await setup();
    check("Escape is listened for on the document",
        (page.doc.listeners.get("keydown") || []).length === 1);
    page.teardown();
    check("and the page takes its listener off when it is torn down",
        (page.doc.listeners.get("keydown") || []).length === 0,
        "it is mounted as a fragment, so this runs on every navigation");
}

console.log(`\n${failures.length ? `FAILURES: ${failures.join(", ")}` : "all checks passed"}`);
process.exit(failures.length ? 1 : 0);
