/**
 * What a drop nobody claimed does (services/dropRouter.js), against a stubbed
 * page.
 *
 * What has to hold:
 *
 *   - **A drop that misses an open dialog is refused out loud**, and nothing
 *     is imported. Falling through to the Import dialog is how a markers.csv
 *     dropped beside "Add channel names" became a sample.
 *   - **On the viewer the server's plan is carried out**: an action marked
 *     `auto` runs, one marked `needs_confirm` runs only after a yes, one
 *     marked `blocked` only says why. Each through the route the plan names.
 *   - **Files nothing could read are named once**, and never stop the rest.
 *   - **Opening another sample carries the rest of the drop with it** -- once,
 *     to that sample only, and not when it is stale.
 *   - **A server with no plan** (an older one) gets the old behaviour.
 *
 * Run directly:  node tests/js/drop_router_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const ROUTER = readFileSync(join(REPO, "plexora/client/src/js/services/dropRouter.js"), "utf8");

let failures = 0;
function check(label, condition) {
    if (condition) {
        console.log(`ok - ${label}`);
    } else {
        failures += 1;
        console.log(`FAIL - ${label}`);
    }
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function makeElement(tag) {
    const classes = new Set();
    return {
        tagName: tag.toUpperCase(),
        children: [],
        value: "",
        textContent: "",
        className: "",
        classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            contains: (c) => classes.has(c),
        },
        append(...nodes) { this.children.push(...nodes); },
        appendChild(node) { this.children.push(node); return node; },
    };
}

/**
 * A page. `where` is "viewer" (a sample open and showing), "home", or
 * "modal" (a dialog open over the viewer). `plan` is what /drop/inspect
 * answers, or a number for an HTTP failure.
 */
function makeWorld({ where = "viewer", plan = null, confirm = [] } = {}) {
    const requests = [];
    const toasts = [];
    const went = [];
    const added = [];
    const opened = [];
    const dropped = [];
    const applied = [];
    const asked = [];
    const storage = new Map();
    const answers = confirm.slice();
    let replies = { "drop/inspect": plan };

    const container = makeElement("div");
    if (where === "home") container.classList.add("plexora-view-hidden");
    const dialog = { matches: (selector) => selector === ":modal" };
    const document = {
        body: { dataset: { plexoraDatasource: where === "home" ? "" : "tonsil" } },
        getElementById: (id) => (id === "container" ? container : null),
        querySelectorAll: (selector) => (selector === "dialog[open]" && where === "modal"
            ? [dialog] : []),
        querySelector: () => null,
        createElement: makeElement,
    };
    const window = {
        flaskVariables: { datasource: where === "home" ? "" : "tonsil" },
        PlexoraToast: { show: (options) => toasts.push(options) },
        PlexoraRouter: { go: (href) => went.push(href) },
        PlexoraConfirm: {
            choose: (options) => {
                asked.push(options);
                const answer = answers.shift();
                return Promise.resolve(typeof answer === "function" ? answer(options) : answer ?? null);
            },
        },
        PlexoraImportSample: {
            addToSample: (sample, paths, options) => {
                added.push({ sample, paths, answers: options.answers });
                return Promise.resolve({ ok: true, result: { reload: false } });
            },
            open: (options) => opened.push(options),
            dropPaths: (paths) => dropped.push(paths),
        },
        PlexoraChannelNames: {
            apply: (options) => { applied.push(options); return Promise.resolve(); },
        },
        sessionStorage: {
            getItem: (key) => (storage.has(key) ? storage.get(key) : null),
            setItem: (key, value) => storage.set(key, String(value)),
            removeItem: (key) => storage.delete(key),
        },
    };
    const fetch = (url, options) => {
        const path = url.replace(/^\//, "");
        const body = options && options.body ? JSON.parse(options.body) : null;
        requests.push({ path, body });
        const reply = replies[path];
        if (typeof reply === "number") {
            return Promise.resolve({ ok: false, status: reply, json: () => Promise.resolve({}) });
        }
        return Promise.resolve({
            ok: true, status: 200,
            json: () => Promise.resolve(reply === undefined ? {} : reply),
        });
    };
    const context = createContext({
        window, document, fetch, console, setTimeout, JSON, Date, Promise, Number,
        plexoraUrl: (path) => `/${path}`,
    });
    runInContext(ROUTER, context);
    return {
        router: window.PlexoraDropRouter, window, requests, toasts, went, added,
        opened, dropped, applied, asked, storage,
        reply(path, value) { replies[path] = value; },
    };
}

function plan(actions, extra = {}) {
    return {
        datasource: "tonsil", open: { name: "tonsil", title: "tonsil" },
        items: [], unrecognised: [], warnings: [], summary: { hint: "" },
        actions, ...extra,
    };
}

function action(kind, extra = {}) {
    return {
        id: "a0", kind, status: "auto", reason: null, message: `${kind} it`,
        picks: [0], paths: ["/d/x"], sample: "tonsil", route: null,
        choices: null, default: null, one_press: false,
        effects: { reload: false, navigate: false }, then: [], fit: {}, ...extra,
    };
}

const MASK = "/data/tonsil_cellmask.tif";
const layers = (sample, path, answers = {}) => ({
    method: "POST", path: "import/layers", json: { sample, paths: [path], answers },
});

// -- where a drop is ----------------------------------------------------------

{
    check("the viewer is the viewer", makeWorld({ where: "viewer" }).router.context() === "viewer");
    check("a hidden viewer is not", makeWorld({ where: "home" }).router.context() === "home");
    check("a modal dialog is", makeWorld({ where: "modal" }).router.context() === "modal");
}

// -- a dialog is open -----------------------------------------------------------

{
    const world = makeWorld({ where: "modal", plan: plan([action("attach_mask")]) });
    await world.router.unclaimed(["/data/markers.csv"]);
    check("a drop that misses an open dialog is refused with a notice",
          world.toasts.length === 1 && /dialog is open/.test(world.toasts[0].note));
    check("...and nothing is asked, imported or opened",
          world.requests.length === 0 && world.dropped.length === 0
          && world.opened.length === 0 && world.added.length === 0);
}

// -- the viewer -----------------------------------------------------------------

{
    const world = makeWorld({ plan: plan([action("attach_mask", {
        route: layers("tonsil", MASK), paths: [MASK], effects: { reload: true, navigate: false },
    })]) });
    await world.router.unclaimed([MASK]);
    const asked = world.requests[0];
    check("the server is asked about the files, the open sample and where",
          asked.path === "drop/inspect" && asked.body.datasource === "tonsil"
          && asked.body.target === "viewer" && asked.body.paths[0] === MASK
          && asked.body.forget === true);
    check("an auto action runs through the route it names, with no question",
          world.asked.length === 0 && world.added.length === 1
          && world.added[0].sample === "tonsil" && world.added[0].paths[0] === MASK);
}

{
    const choices = [
        { value: "replace", label: "Replace mask", kind: "attach_mask",
          route: layers("tonsil", MASK), tone: "danger", effects: { reload: true } },
        { value: "layer", label: "Add as an image layer", kind: "add_layer",
          route: layers("tonsil", MASK, { [`mask-or-image:${MASK}`]: "image" }), effects: {} },
    ];
    const ask = action("attach_mask", { status: "needs_confirm", reason: "mask_exists",
                                        paths: [MASK], choices, default: null });
    const no = makeWorld({ plan: plan([ask]), confirm: [null] });
    await no.router.unclaimed([MASK]);
    const shown = no.asked[0];
    check("an existing mask is asked about before anything happens",
          shown && shown.title === "This sample already has a mask");
    check("...with Replace red and not the focused answer",
          shown.choices.some((c) => c.label === "Replace mask" && c.kind === "danger" && !c.focus)
          && shown.choices[0].label === "Cancel" && shown.choices[0].focus);
    check("...and Cancel changes nothing", no.added.length === 0);

    const yes = makeWorld({ plan: plan([ask]), confirm: ["layer"] });
    await yes.router.unclaimed([MASK]);
    check("the answer chosen is the route taken",
          yes.added.length === 1
          && yes.added[0].answers[`mask-or-image:${MASK}`] === "image");
}

{
    const size = action("attach_mask", {
        status: "needs_confirm", reason: "size_mismatch", paths: [MASK], default: null,
        fit: { size: [64, 64], imageSize: [128, 128], compatible: false, nameMatch: "exact" },
        choices: [{ value: "attach", label: "Attach anyway", kind: "attach_mask",
                    route: layers("tonsil", MASK), effects: { reload: true } }],
    });
    const world = makeWorld({ plan: plan([size]), confirm: [null] });
    await world.router.unclaimed([MASK]);
    const shown = world.asked[0];
    check("a mask of the wrong size is shown with both sizes",
          shown.title === "Segmentation mask detected"
          && shown.body.some((line) => /64×64 px.*128×128 px/.test(line)));
    check("...and Attach anyway is not the primary answer",
          shown.choices.find((c) => c.label === "Attach anyway").kind !== "primary");
}

{
    const world = makeWorld({ plan: plan([
        action("apply_channel_names", { status: "blocked", reason: "needs_open_sample",
                                        message: "Open the sample first." }),
    ]) });
    await world.router.unclaimed(["/data/markers.csv"]);
    check("a blocked action only says why",
          world.toasts.length === 1 && world.toasts[0].note === "Open the sample first."
          && world.applied.length === 0);
}

{
    const world = makeWorld({ plan: plan([action("apply_channel_names", {
        paths: ["/data/markers.csv"],
        route: { method: "POST", path: "upload_channels",
                 form: { datasource: "tonsil", path: "/data/markers.csv",
                         column: "2", has_header: "true" } },
    })]) });
    await world.router.unclaimed(["/data/markers.csv"]);
    const call = world.applied[0];
    check("markers.csv on the viewer renames the channels with the column the server read",
          call && call.datasource === "tonsil" && call.path === "/data/markers.csv"
          && call.column === "2" && call.hasHeader === true);
    check("...and never opens the Import dialog",
          world.opened.length === 0 && world.dropped.length === 0);
}

{
    const world = makeWorld({ plan: plan([action("attach_table", {
        paths: ["/data/cells.csv"], route: layers("tonsil", "/data/cells.csv"),
    })], { unrecognised: [{ pick: 1, path: "/data/notes.xyz", reason: "unknown", code: "unsupported" }] }) });
    await world.router.unclaimed(["/data/cells.csv", "/data/notes.xyz"]);
    check("a file nothing could read is named once and does not stop the rest",
          world.added.length === 1
          && world.toasts.filter((t) => /notes\.xyz/.test(t.note || "")).length === 1
          && /notes\.xyz/.test(world.toasts.at(-1).note));
}

{
    const world = makeWorld({ plan: plan([action("upload_gates", { paths: ["/data/gates.csv"] })]) });
    const ran = [];
    world.router.registerExecutor("upload_gates", (act) => { ran.push(act.paths[0]); });
    await world.router.unclaimed(["/data/gates.csv"]);
    check("a plugin's kind runs the executor the plugin registered",
          ran.length === 1 && ran[0] === "/data/gates.csv");
}

{
    const world = makeWorld({ plan: plan([action("mystery")]) });
    await world.router.unclaimed(["/data/x"]);
    check("a kind nothing can run says so", /nothing here/.test(world.toasts[0]?.note || ""));
}

// -- elsewhere -------------------------------------------------------------------

{
    const world = makeWorld({ where: "home", plan: plan([action("import_new", {
        paths: ["/data/new.ome.tif"], one_press: true, sample: null,
        route: { method: "POST", path: "import/sample",
                 json: { paths: ["/data/new.ome.tif"], key: "stem:new" } },
        effects: { reload: false, navigate: true },
    })], { datasource: null, open: null }) });
    world.reply("import/sample", { name: "new", redirect: "/new" });
    await world.router.unclaimed(["/data/new.ome.tif"]);
    const sent = world.requests.find((r) => r.path === "import/sample");
    check("with nothing open the plan is asked without a sample",
          world.requests[0].body.datasource === null && world.requests[0].body.target === "home");
    check("a new image imports in one press and opens",
          sent && sent.body.key === "stem:new" && world.went[0] === "/new");
}

{
    const then = [action("attach_mask", { id: "t0", sample: "tonsil", paths: [MASK],
                                          route: layers("tonsil", MASK) })];
    const opener = action("open_project", {
        status: "needs_confirm", reason: "open_and_attach", sample: "tonsil",
        message: "Open tonsil and add 1 file?", paths: ["/data/tonsil.ome.tif"], then,
        effects: { reload: false, navigate: true },
    });
    const world = makeWorld({ where: "home", plan: plan([opener], { datasource: null, open: null }),
                              confirm: ["go"] });
    await world.router.unclaimed(["/data/tonsil.ome.tif", MASK]);
    const saved = JSON.parse(world.storage.get("plexora:drop-handoff") || "null");
    check("opening the sample an image belongs to asks first, then goes there",
          world.asked.length === 1 && world.went[0] === "/tonsil");
    check("...carrying the rest of the drop for that sample",
          saved && saved.datasource === "tonsil" && saved.then.length === 1);

    const there = makeWorld({ where: "viewer" });
    there.storage.set("plexora:drop-handoff", world.storage.get("plexora:drop-handoff"));
    const took = await there.router.takeHandoff("tonsil");
    check("the sample that opens attaches what was carried",
          took === true && there.added.length === 1 && there.added[0].paths[0] === MASK);
    check("...once", (await there.router.takeHandoff("tonsil")) === false);

    const other = makeWorld({ where: "viewer" });
    other.storage.set("plexora:drop-handoff", world.storage.get("plexora:drop-handoff"));
    check("another sample does not take it",
          (await other.router.takeHandoff("melanoma")) === false && other.added.length === 0);

    const stale = makeWorld({ where: "viewer" });
    stale.storage.set("plexora:drop-handoff",
                      JSON.stringify({ ...saved, at: Date.now() - 10 * 60 * 1000 }));
    check("a stale one is dropped",
          (await stale.router.takeHandoff("tonsil")) === false && stale.added.length === 0);
}

{
    const target = action("attach_mask", {
        status: "needs_confirm", reason: "choose_target", sample: "tonsil", default: "tonsil",
        paths: [MASK], route: layers("tonsil", MASK),
        choices: [
            { value: "tonsil", label: "tonsil", kind: "attach_mask", route: layers("tonsil", MASK),
              effects: { reload: true } },
            { value: "lung", label: "lung", kind: "attach_mask", route: layers("lung", MASK),
              effects: { reload: true } },
        ],
    });
    const world = makeWorld({ where: "home", plan: plan([target], { datasource: null, open: null }),
                              confirm: [(options) => {
                                  options.content.children[1].value = "lung";
                                  return "go";
                              }] });
    world.reply("import/layers", { reload: true });
    await world.router.unclaimed([MASK]);
    const sent = world.requests.find((r) => r.path === "import/layers");
    check("a mask with nothing open is attached to the sample chosen in the dialog, which opens",
          sent && sent.body.sample === "lung" && world.went[0] === "/lung");
}

{
    const world = makeWorld({ plan: 404 });
    await world.router.unclaimed(["/data/x.tif"]);
    check("a server with no plan gets the old answer: the Import dialog",
          world.dropped.length === 1 && world.dropped[0][0] === "/data/x.tif");
}

{
    let release = null;
    const world = makeWorld();
    world.router.registerExecutor("slow", () => new Promise((done) => { release = done; }));
    const first = world.router.runPlan(plan([action("slow")]), ["/a"]);
    await tick();
    const second = world.router.runPlan(plan([action("noop_already_open")]), ["/b"]);
    await tick();
    check("a drop while another is being added waits, and says so",
          world.toasts.some((t) => t.title === "Waiting")
          && !world.toasts.some((t) => t.title === "Already open"));
    release();
    await first;
    await second;
    check("...then runs", world.toasts.some((t) => t.title === "Already open"));
}

{
    const world = makeWorld();
    check("the home page hands over only what is more than a new sample",
          world.router.claims(plan([action("open_project")]))
          && !world.router.claims(plan([action("import_new")]))
          && !world.router.claims(null));
}

if (failures) {
    console.log(`${failures} failed`);
    process.exit(1);
}
