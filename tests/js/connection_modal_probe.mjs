/**
 * Connecting to another machine, as the one dialog that shows it.
 *
 * What is pinned here is the part that is not obvious from reading the file,
 * and that a user only discovers at the worst moment -- fifteen minutes into a
 * queue, or three seconds into a password prompt:
 *
 *   1. **The steps are the server's states.** Five things happen, they take
 *      wildly different amounts of time, and the scheduler step is drawn only
 *      for a profile that actually waits in a queue. Inventing a step, or
 *      showing one that will never happen, is how a slow connection reads as
 *      a broken one.
 *   2. **A failure is drawn where it happened**, and the log stays on screen,
 *      because the actionable line is almost always in it.
 *   3. **A redraw does not eat a half-typed password.** The dialog re-renders
 *      every second and the box is inside it.
 *   4. **Masking follows the question**, not the fact that it is a question:
 *      a host-key fingerprint typed into a row of dots is unanswerable.
 *   5. **Closing is not cancelling.** The ssh belongs to the server; only one
 *      button here ends it, and it says so.
 *   6. **Connecting something already connecting is not an error.** Another
 *      field, or another tab, may have started it -- and watching it is what
 *      this dialog is for.
 *   7. **The terminal follows the output until the user reads it.**
 *
 * Run directly:  node tests/js/connection_modal_probe.mjs
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/connectionModal.js");
// The real one, not a stub: the thing worth pinning here is that the dialog
// KEEPS the pane it was given, and a stub that handed back a fresh element
// would pass that check by accident.
const TERMINAL = join(REPO, "plexora/client/src/js/services/logTerminal.js");

// -- a DOM small enough to read ---------------------------------------------

function makeElement(tag) {
    const classes = new Set();
    const attributes = new Map();
    const listeners = new Map();
    const element = {
        tagName: String(tag).toUpperCase(),
        type: "",
        value: "",
        id: "",
        textContent: "",
        hidden: false,
        disabled: false,
        // The drawn dropdown writes its position here. Nothing is read back --
        // there is no layout in this DOM to read -- but the assignments have
        // to land somewhere, and `getBoundingClientRect` is deliberately
        // absent so that `place()` takes its early return.
        style: {},
        children: [],
        parentNode: null,
        dataset: {},
        tabIndex: 0,
        scrollTop: 0,
        scrollHeight: 500,
        clientHeight: 100,
        focused: false,
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
        replaceChildren(...nodes) {
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
        // A browser forwards a click on a `<label>` to the control it names,
        // and that forwarding is the whole way a switch on this form is
        // operated -- so a DOM that did not do it could not see a switch being
        // turned on, nor a field whose label was quietly re-activating
        // something inside it.
        click() {
            element.dispatchEvent({ type: "click" });
            if (element.tagName !== "LABEL") return;
            const control = walk(element).find(
                (n) => ["INPUT", "BUTTON", "SELECT", "TEXTAREA"]
                    .indexOf(n.tagName) >= 0);
            if (!control) return;
            if (control.disabled) return;
            if (control.type === "checkbox") control.checked = !control.checked;
            // A radio cannot be un-checked by clicking it, and its group is
            // put straight by the form's own paint rather than by this DOM --
            // which has no notion of a group at all.
            if (control.type === "radio") control.checked = true;
            control.dispatchEvent({ type: "click" });
            control.dispatchEvent({ type: "change" });
        },
        focus() { element.focused = true; },
        getBoundingClientRect() {
            return { left: 40, top: 300, right: 260, bottom: 332,
                     width: 220, height: 32 };
        },
        scrollIntoView() {},
        showModal() { element.open = true; },
        close() { element.open = false; element.dispatchEvent({ type: "close" }); },
        open: false,
    };
    return element;
}

/** Every element under `root`, depth first. */
function walk(root, out = []) {
    (root.children || []).forEach((child) => {
        out.push(child);
        walk(child, out);
    });
    return out;
}

function find(root, className) {
    return walk(root).filter((n) => n.classList.contains(className));
}

function one(root, className) {
    return find(root, className)[0] || null;
}

function buttonSaying(root, text) {
    return walk(root).find(
        (n) => n.tagName === "BUTTON" && n.textContent === text) || null;
}

/**
 * A drawn dropdown, driven the way somebody drives it.
 *
 * The control is not a `<select>` any more -- the browser's own menu cannot be
 * styled and this dialog is dark -- so there is no `.value =` to set from
 * outside. `open()` then `pick()` is the real path: press the trigger, read
 * the rows, click one. `control` is the same object the form holds, for the
 * checks that are about the form's logic rather than about the menu.
 */
function dropdown(root) {
    const box = root && root.classList.contains("connect-select")
        ? root
        : walk(root).find((n) => n.classList.contains("connect-select"));
    if (!box) return null;
    const kids = walk(box);
    const trigger = kids.find(
        (n) => n.classList.contains("connect-select-button"));
    const menu = kids.find((n) => n.classList.contains("connect-select-menu"));
    const rows = () => walk(menu).filter(
        (n) => n.classList.contains("connect-select-option"));
    return {
        root: box,
        control: box.plexoraSelect,
        trigger: trigger,
        menu: menu,
        open: () => trigger.click(),
        isOpen: () => menu.hidden === false,
        labels: () => rows().map((n) => n.textContent),
        shown: () => walk(trigger).find(
            (n) => n.classList.contains("connect-select-value")).textContent,
        pick: (label) => {
            if (menu.hidden) trigger.click();
            const row = rows().find((n) => n.textContent === label);
            if (!row) throw new Error("no option labelled " + label);
            row.click();
        },
        //: What the keyboard does, with the event shape the handler reads.
        press: (key) => trigger.dispatchEvent({
            type: "keydown", key: key,
            preventDefault: () => {}, stopPropagation: () => {},
        }),
    };
}

// -- a remote state the test drives -----------------------------------------

const OPENING = ["connecting", "authenticating", "installing",
                 "waiting_for_job", "tunneling", "waiting_for_app"];

const posted = [];
let snapshot = null;
let deep = {};
let connectRejects = null;
let subscriber = null;

function entryFor(name) {
    return (snapshot.entries || []).find((e) => e.name === name) || null;
}

const RemotesStub = {
    KIND_VIEWER: "viewer",
    KIND_NODE: "node",
    OPENING,
    isOpening: (state) => OPENING.indexOf(state) >= 0,
    isSecret: (text) => !/\(yes\/no|fingerprint/i.test(String(text || "")),
    label: (state) => ({ idle: "Not connected", connected: "Connected",
                         failed: "Failed" }[state] || state),
    half: (entry, kind) => (kind === "node" ? entry.node : entry.viewer),
    entry: entryFor,
    snapshot: () => snapshot,
    focused: (name, kind) => deep[`${kind}:${name}`] || null,
    refresh: () => Promise.resolve(snapshot),
    subscribe: (cb, options) => {
        subscriber = { cb, options };
        cb(snapshot);
        return () => { subscriber = null; };
    },
    connect: (name, kind) => {
        posted.push({ action: "connect", name, kind });
        if (connectRejects) {
            const message = connectRejects;
            connectRejects = null;
            return Promise.reject(new Error(message));
        }
        return Promise.resolve({});
    },
    disconnect: (name, kind) => {
        posted.push({ action: "disconnect", name, kind });
        return Promise.resolve({});
    },
    answer: (name, kind, id, value) => {
        posted.push({ action: "answer", name, kind, id, value });
        return Promise.resolve({});
    },
    vmStandard: (name) => {
        posted.push({ action: "vmStandard", name });
        return Promise.resolve({ ok: true, provisioning_model: "standard" });
    },
};

/** Push a new state at whoever is subscribed. */
function say(next) {
    snapshot = next;
    if (subscriber) subscriber.cb(snapshot);
}

function profile(name, opts = {}) {
    const viewer = Object.assign({ state: "idle", phase: "", error: null,
                                   recovery: "", prompt: null, url: null,
                                   log: [] },
                                 opts.viewer || {});
    const node = Object.assign({ state: "idle", phase: "", error: null,
                                 recovery: "", prompt: null, node: null },
                               opts.node || {});
    return {
        name, target: `me@${name}`, label: name, detail: `me@${name}`,
        queued: Boolean(opts.queued), install: Boolean(opts.install),
        installEnv: opts.installEnv || null, viewer, node,
        connected: viewer.state === "connected" || Boolean(node.node),
        opening: RemotesStub.isOpening(viewer.state)
                 || RemotesStub.isOpening(node.state),
        prompt: viewer.prompt || node.prompt || null,
    };
}

function world(entries) {
    return { loaded: true, error: null, remotes: [], places: [],
             entries: entries, clientNode: "", serverIsRemote: false,
             server: null, focus: {} };
}

// -- load the shipped file --------------------------------------------------

// -- the two routes the add-a-server flow talks to --------------------------

const fetched = [];
let recipeCatalogue = [
    { id: "hms-o2", label: "HMS O2", blurb: "Harvard's cluster.",
      ask: ["user", "walltime", "cores", "memory"],
      notes: ["Connect to the LOGIN node."],
      srun: "-p interactive -t 4:00:00 -c 16 --mem 128G",
      srun_extra: "-p interactive", remote_command: "plexora",
      site: true, tested: true, unverified: false },
    { id: "ssh", label: "A plain SSH server", blurb: "Any host.",
      ask: ["user", "host"], notes: [], srun: null, srun_extra: null,
      remote_command: "plexora", site: false, tested: false,
      unverified: false },
    { id: "aws", label: "An AWS EC2 instance", blurb: "An instance.",
      ask: ["user", "host"], notes: ["Untested by us."], srun: null,
      srun_extra: null, remote_command: "plexora", site: true,
      tested: false, unverified: true },
    // The one preset whose machine does not exist yet. Its questions are not
    // in the ask vocabulary at all -- they are which project, which bucket,
    // how big a VM -- so it carries a `flow` and its own catalogues instead.
    { id: "gcloud", label: "Google Cloud (Compute + Storage)",
      blurb: "Your images are in a bucket.", ask: [], notes: ["Untested by us."],
      srun: null, srun_extra: null, remote_command: "~/plexora-venv",
      target_template: "", site: true, tested: false, unverified: true,
      extra: {
          flow: "gcloud",
          // Three tiers, as the server sends them: something small enough to
          // test the connection on, the default sized for a pyramid, and the
          // largest. The shared-core wording is the server's too -- "2 vCPU"
          // on an e2-medium and on a dedicated-core type are not the same
          // offer, and nothing else in the name would say so.
          machine_types: [
              { name: "e2-medium", label: "e2-medium · 2 shared vCPU · 4 GB RAM",
                shared: true },
              { name: "e2-standard-8", label: "e2-standard-8 · 8 vCPU · 32 GB RAM" },
              { name: "e2-highmem-16", label: "e2-highmem-16 · 16 vCPU · 128 GB RAM" },
              { name: "n2-highmem-32", label: "n2-highmem-32 · 32 vCPU · 256 GB RAM" },
          ],
          default_machine_type: "e2-highmem-16",
          regions: [
              { name: "us-east1", label: "us-east1 · South Carolina" },
              { name: "europe-west4", label: "europe-west4 · Netherlands" },
              { name: "us-central1", label: "us-central1 · Iowa" },
          ],
          default_region: "us-east1",
          mount_path: "~/plexora-data",
          boot_disk_gb: 20,
          idle_shutdown_minutes: 30,
          provisioning_models: [
              { name: "spot", label: "Spot (preemptible)",
                hint: "Much cheaper — usually 60–91% off. Google can reclaim "
                      + "the machine at any time." },
              { name: "standard", label: "Standard",
                hint: "Full price, and nobody takes it away." },
          ],
          default_provisioning: "spot",
          exit_actions: [
              { name: "leave", label: "Leave VM running",
                hint: "Compute keeps billing the whole time." },
              { name: "stop", label: "Stop VM",
                hint: "Compute stops billing. The disk keeps its charge." },
              { name: "delete", label: "Delete VM",
                hint: "Nothing keeps billing. Your bucket is untouched." },
          ],
          default_exit: "stop",
          vm_sources: [
              { name: "plexora", label: "Create a new VM",
                hint: "Plexora asks Google for a machine." },
              { name: "existing", label: "Use an existing VM",
                hint: "A machine you already run." },
          ],
      } },
];

//: What the stubbed Google Cloud answers with. Rebound by the checks that
//: need a different answer -- a bucket that cannot be read, an account that
//: is not signed in yet.
let cloud = {
    status: { installed: true, account: "aj@example.com" },
    projects: [{ id: "lab-imaging", name: "Lab Imaging" },
               { id: "other-project", name: "Other" }],
    buckets: [{ name: "tonsil-images", location: "US-EAST1",
                region: "us-east1", exact: true },
              { name: "atlas-public", location: "EU",
                region: "europe-west1", exact: false }],
    bucket: { name: "tonsil-images", location: "US-EAST1",
              region: "us-east1", exact: true },
    bucketError: null,
    zones: ["us-east1-b", "us-east1-c"],
    // Deliberately NOT in the bucket's region: where somebody's own
    // machine lives has nothing to do with where their data is, and that
    // was the case the form used to make unsavable.
    instances: [{ name: "analysis-box", zone: "us-central1-a",
                  status: "TERMINATED", machine_type: "n2-highmem-32" }],
};
const recipeDefaults = { walltime: "4:00:00", cores: "16", memory: "128G",
                         srun: "-p interactive -t 4:00:00 -c 16 --mem 128G" };
let saveReply = null;

function fetchStub(url, options = {}) {
    fetched.push({ url, method: (options || {}).method || "GET",
                   body: (options || {}).body });
    const reply = (payload, ok = true) => Promise.resolve({
        ok, status: ok ? 200 : 404, json: () => Promise.resolve(payload),
    });
    if (url.indexOf("settings/gcloud/status") >= 0) return reply(cloud.status);
    if (url.indexOf("settings/gcloud/projects") >= 0) {
        return reply({ projects: cloud.projects });
    }
    if (url.indexOf("settings/gcloud/buckets") >= 0) {
        return reply({ buckets: cloud.buckets });
    }
    if (url.indexOf("settings/gcloud/bucket") >= 0) {
        return cloud.bucketError
            ? reply({ error: cloud.bucketError }, false)
            : reply({ bucket: cloud.bucket });
    }
    if (url.indexOf("settings/gcloud/instances") >= 0) {
        return reply({ instances: cloud.instances });
    }
    if (url.indexOf("settings/gcloud/zones") >= 0) {
        return reply({ zones: cloud.zones, pick: cloud.zones[0] });
    }
    if (url.indexOf("settings/gcloud/auth") >= 0) return reply({ started: true });
    if (url.indexOf("settings/recipes/") >= 0) {
        const answer = saveReply || { remote: { name: "o2" } };
        return Promise.resolve({
            ok: !answer.error, status: answer.error ? 400 : 200,
            json: () => Promise.resolve(answer),
        });
    }
    return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ recipes: recipeCatalogue,
                                      defaults: recipeDefaults }),
    });
}

const body = makeElement("body");
const context = {
    console,
    setTimeout,
    clearTimeout,
    Promise,
    JSON,
    encodeURIComponent,
    fetch: fetchStub,
    plexoraUrl: (path) => `/${String(path).replace(/^\/+/, "")}`,
    innerHeight: 800,
    innerWidth: 1200,
    document: { createElement: makeElement, body },
};
context.window = context;
context.PlexoraRemotes = RemotesStub;
createContext(context);
runInContext(readFileSync(TERMINAL, "utf-8"), context);
runInContext(readFileSync(SOURCE, "utf-8"), context);

const Modal = context.window.PlexoraConnectionModal;

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
    let left = 20;
    const step = () => (left-- > 0 ? Promise.resolve().then(step) : resolve());
    step();
});

function dialogNow() {
    return body.children[body.children.length - 1];
}

/** The log as it reads on screen: one element per line, since the pane marks
 *  the lines ssh relayed from the far machine differently from Plexora's own. */
function logText(term) {
    return (term.children || []).map(
        (line) => (line.children || []).length
            ? line.children.map((part) => part.textContent).join(" ")
            : line.textContent).join("\n");
}

function stepLabels(dialog) {
    return find(dialog, "connect-step").map((s) => ({
        label: one(s, "connect-step-label").textContent,
        status: ["done", "active", "failed", "pending"]
            .find((n) => s.classList.contains("is-" + n)),
    }));
}

async function main() {
    // -- 1. the steps come from the server's states ---------------------------
    snapshot = world([profile("hpc", { node: { state: "idle" } })]);
    posted.length = 0;
    let done = Modal.open({ name: "hpc", kind: "node" });
    await settle();
    check("opening for a profile connects it", posted.some(
        (p) => p.action === "connect" && p.name === "hpc" && p.kind === "node"));
    say(world([profile("hpc", { node: { state: "connecting" } })]));
    await settle();
    let dialog = dialogNow();
    check("a plain ssh host is not promised a scheduler wait",
          stepLabels(dialog).every((s) => s.label !== "Waiting for the scheduler"));
    check("the step being waited for is the one that is active",
          stepLabels(dialog)[0].status === "active");
    check("a data node's last step is not called starting Plexora",
          stepLabels(dialog).some((s) => s.label === "Starting the data node"));

    say(world([profile("hpc", { node: { state: "tunneling",
                                        phase: "Opening the tunnel…" } })]));
    await settle();
    dialog = dialogNow();
    const steps = stepLabels(dialog);
    check("...and everything before it is already done",
          steps[0].status === "done" && steps[1].status === "done");
    check("the server's own sentence is what is shown, not a translation",
          one(dialog, "connect-phase").textContent === "Opening the tunnel…");

    // -- 4/3. the question, and not eating the answer -------------------------
    say(world([profile("hpc", {
        node: { state: "authenticating",
                prompt: { id: "p1", text: "me@hpc's password:" } } })]));
    await settle();
    dialog = dialogNow();
    const secretBox = walk(one(dialog, "connect-prompt"))
        .find((n) => n.tagName === "INPUT");
    check("a password question is masked", secretBox.type === "password");
    check("...and the question is shown exactly as ssh asked it",
          one(dialog, "connect-prompt-text").textContent === "me@hpc's password:");

    secretBox.value = "half-typed";
    say(world([profile("hpc", {
        node: { state: "authenticating", phase: "still going",
                prompt: { id: "p1", text: "me@hpc's password:" } } })]));
    await settle();
    check("a redraw while the same question stands leaves the box alone",
          walk(one(dialogNow(), "connect-prompt"))
              .find((n) => n.tagName === "INPUT").value === "half-typed");

    posted.length = 0;
    buttonSaying(dialogNow(), "Send").click();
    await settle();
    check("sending hands the answer over and clears the box",
          posted.some((p) => p.action === "answer" && p.value === "half-typed")
          && walk(one(dialogNow(), "connect-prompt"))
              .find((n) => n.tagName === "INPUT").value === "");

    // -- 4. a host-key question is not a secret -------------------------------
    say(world([profile("hpc", {
        node: { state: "authenticating",
                prompt: { id: "p2",
                          text: "ED25519 key fingerprint is SHA256:abc.\n"
                                + "Are you sure you want to continue "
                                + "connecting (yes/no)?" } } })]));
    await settle();
    dialog = dialogNow();
    const openBox = walk(one(dialog, "connect-prompt"))
        .find((n) => n.tagName === "INPUT");
    check("a host-key question is answerable in the clear",
          openBox.type === "text");
    check("...with the two answers ssh accepts, as buttons",
          Boolean(buttonSaying(dialog, "Yes") && buttonSaying(dialog, "No")));
    posted.length = 0;
    buttonSaying(dialog, "Yes").click();
    await settle();
    check("...which send what ssh expects",
          posted.some((p) => p.action === "answer" && p.value === "yes"));

    // -- 7. the terminal follows until it is read -----------------------------
    deep = { "node:hpc": { state: "authenticating",
                           log: ["line one", "  [ssh] Permission denied"] } };
    say(world([profile("hpc", { node: { state: "authenticating" } })]));
    await settle();
    dialog = dialogNow();
    let term = one(dialog, "connect-log-body");
    check("the log is the whole tail the focused fetch brought back",
          logText(term) === "line one\nssh Permission denied");
    // What the far machine itself printed, told apart from Plexora narrating.
    // ssh merges the remote command's stdout and stderr into the one stream it
    // relays, and both arrive here labelled by the process that carried them.
    check("...with what the far machine said marked as its own",
          term.children[1].classList.contains("is-relayed")
          && one(term.children[1], "connect-log-from").textContent === "ssh"
          && one(term.children[1], "connect-log-said").textContent
             === "Permission denied");
    check("...pinned to the bottom", term.scrollTop === term.scrollHeight);

    // Halfway up, not the top: a rebuilt pane would come back at 0, which is
    // where a scrolled-to-the-top reader would have been anyway. This is the
    // check that can tell the two apart.
    const wasTerminal = term;
    term.scrollTop = 220;
    term.dispatchEvent({ type: "scroll" });
    deep = { "node:hpc": { state: "authenticating",
                           log: ["line one", "line two", "line three"] } };
    say(world([profile("hpc", { node: { state: "authenticating",
                                        phase: "still going" } })]));
    await settle();
    term = one(dialogNow(), "connect-log-body");
    check("the log pane survives a redraw rather than being replaced",
          term === wasTerminal);
    check("scrolling up to read stops it yanking itself back down",
          term.scrollTop === 220
          && logText(term) === "line one\nline two\nline three");

    // ...and returning to the bottom puts it back in step.
    term.scrollTop = term.scrollHeight;
    term.dispatchEvent({ type: "scroll" });
    deep = { "node:hpc": { state: "authenticating",
                           log: ["line one", "line two", "line three", "four"] } };
    say(world([profile("hpc", { node: { state: "authenticating" } })]));
    await settle();
    check("...and scrolling back to the bottom sets it following again",
          one(dialogNow(), "connect-log-body").scrollTop
          === one(dialogNow(), "connect-log-body").scrollHeight);

    // -- 5. closing is not cancelling -----------------------------------------
    dialog = dialogNow();
    posted.length = 0;
    check("while opening, the way out says it leaves the connection running",
          Boolean(buttonSaying(dialog, "Continue in background")));
    // The row is rebuilt only when the BUTTONS change. It used to be rebuilt
    // every tick, which took the focus off whichever one somebody had tabbed
    // to -- once a second, for the whole of a queued job.
    const wasStop = buttonSaying(dialog, "Stop connecting");
    say(world([profile("hpc", { node: { state: "authenticating",
                                        phase: "a moment later" } })]));
    await settle();
    check("the buttons are not rebuilt while they are still the same buttons",
          buttonSaying(dialogNow(), "Stop connecting") === wasStop);
    check("...and the button that ends it says that instead",
          Boolean(buttonSaying(dialog, "Stop connecting")));
    buttonSaying(dialog, "Continue in background").click();
    let outcome = await done;
    check("closing the window disconnects nothing",
          posted.length === 0 && outcome.connected === false);

    // -- 2. a failure is drawn where it happened ------------------------------
    deep = { "node:hpc": { state: "failed", log: ["Permission denied"] } };
    snapshot = world([profile("hpc", { node: { state: "connecting" } })]);
    done = Modal.open({ name: "hpc", kind: "node" });
    await settle();
    say(world([profile("hpc", { node: { state: "authenticating" } })]));
    await settle();
    say(world([profile("hpc", {
        node: { state: "failed",
                error: "That password was not accepted." } })]));
    await settle();
    dialog = dialogNow();
    const failed = stepLabels(dialog);
    check("the step that was running is the one marked failed",
          failed[1].status === "failed" && failed[0].status === "done");
    check("...and what went wrong is said in words",
          one(dialog, "connect-modal-error").textContent
          === "That password was not accepted.");
    check("...with the log still on screen, where the reason usually is",
          logText(one(dialog, "connect-log-body")) === "Permission denied");
    check("a failure offers the two things that can help",
          Boolean(buttonSaying(dialog, "Try again")
                  && buttonSaying(dialog, "Edit connection")));

    posted.length = 0;
    buttonSaying(dialog, "Try again").click();
    await settle();
    check("trying again starts a new connection rather than a new dialog",
          posted.some((p) => p.action === "connect")
          && body.children.filter((c) => c.classList
                                          .contains("connect-modal")).length === 1);

    // -- connected resolves with what the caller needs ------------------------
    say(world([profile("hpc", { node: { state: "connected",
                                        node: "hpc-data" } })]));
    outcome = await done;
    check("connecting resolves with the node a field has to address",
          outcome.connected === true && outcome.node === "hpc-data"
          && outcome.name === "hpc");

    // -- 6. joining a connection somebody else started ------------------------
    posted.length = 0;
    snapshot = world([profile("hpc", { node: { state: "waiting_for_job" } })]);
    done = Modal.open({ name: "hpc", kind: "node" });
    await settle();
    check("a connection already on its way is watched, not restarted",
          posted.every((p) => p.action !== "connect"));
    check("...and a queued profile is not what makes the scheduler step appear",
          stepLabels(dialogNow())
              .every((s) => s.label !== "Waiting for the scheduler"));
    buttonSaying(dialogNow(), "Continue in background").click();
    await done;

    snapshot = world([profile("hpc", { queued: true,
                                       node: { state: "waiting_for_job" } })]);
    done = Modal.open({ name: "hpc", kind: "node" });
    await settle();
    check("a profile that waits in a queue gets the step that says so",
          stepLabels(dialogNow())
              .some((s) => s.label === "Waiting for the scheduler"
                           && s.status === "active"));
    check("...and a profile that installs nothing is not promised that step "
          + "either",
          stepLabels(dialogNow())
              .every((s) => s.label.indexOf("Installing") !== 0));
    buttonSaying(dialogNow(), "Stop connecting").click();
    outcome = await done;
    check("stopping ends the connection and the errand together",
          posted.some((p) => p.action === "disconnect")
          && outcome.connected === false);

    // -- installing Plexora is a step, not something done quietly -------------
    //
    // It is minutes long, it writes to the far machine, and it is the step
    // most likely to be the one that failed. All three are reasons for it to
    // be on the list somebody is watching rather than beside it.
    snapshot = world([profile("hpc", { install: true, installEnv: "imaging",
                                       node: { state: "installing" } })]);
    done = Modal.open({ name: "hpc", kind: "node" });
    await settle();
    let installSteps = stepLabels(dialogNow());
    check("a profile that installs gets the step that says so",
          installSteps.some((s) => s.label === "Installing Plexora in imaging"
                                   && s.status === "active"));
    check("...before anything is launched, because that is where it runs",
          installSteps.findIndex((s) => s.label.indexOf("Installing") === 0)
          < installSteps.findIndex(
              (s) => s.label === "Starting the data node"));
    check("...and signing in is already behind it",
          installSteps.find((s) => s.label === "Signing in").status === "done");

    // The name comes from the server's one reading of the launch command, so
    // a profile whose command names no environment says only what it knows.
    say(world([profile("hpc", { install: true,
                                node: { state: "installing" } })]));
    await settle();
    check("...named plainly when the launch command names no environment",
          stepLabels(dialogNow())
              .some((s) => s.label === "Installing Plexora"));

    // A failed install has to be marked ON the install step: "failed" is not a
    // step, it is what happened to whichever one was running, and marking the
    // whole list pending would throw away the most useful thing on screen.
    say(world([profile("hpc", {
        install: true, installEnv: "imaging",
        node: { state: "failed",
                error: "Installing Plexora on me@hpc failed (pip exited 1)." },
    })]));
    await settle();
    installSteps = stepLabels(dialogNow());
    check("a failed install is marked against the install step",
          installSteps.find((s) => s.label === "Installing Plexora in imaging")
                      .status === "failed");
    check("...with the later steps left as never having run",
          installSteps.find((s) => s.label === "Starting the data node")
                      .status === "pending");
    check("...and pip's own account still on screen",
          one(dialogNow(), "connect-modal-error").textContent
              .indexOf("pip exited 1") >= 0);
    buttonSaying(dialogNow(), "Close").click();
    await done;

    // -- already connected is an answer, not a second connection --------------
    posted.length = 0;
    snapshot = world([profile("hpc", { node: { state: "connected",
                                               node: "hpc-data" } })]);
    outcome = await Modal.open({ name: "hpc", kind: "node" });
    check("a machine already connected resolves immediately",
          outcome.connected === true && outcome.node === "hpc-data"
          && posted.every((p) => p.action !== "connect"));

    // -- with no name, it asks which machine ----------------------------------
    snapshot = world([profile("hpc", { node: { state: "idle" } }),
                      profile("o2", { node: { state: "connected",
                                              node: "o2-data" } })]);
    done = Modal.open({ kind: "node" });
    await settle();
    dialog = dialogNow();
    check("with no machine named, the dialog asks which",
          find(dialog, "connect-modal-row").length === 2);
    check("...saying which of them is already up",
          one(find(dialog, "connect-modal-row")[1], "connect-modal-chip")
              .textContent === "Connected");
    buttonSaying(find(dialog, "connect-modal-row")[1], "Use this").click();
    outcome = await done;
    check("choosing a connected machine takes it as the answer",
          outcome.connected === true && outcome.name === "o2"
          && outcome.node === "o2-data");

    // -- adding a server ------------------------------------------------------
    snapshot = world([]);
    fetched.length = 0;
    posted.length = 0;
    done = Modal.open({ kind: "node" });
    await settle();
    check("with nothing saved at all, the dialog still offers a way forward",
          Boolean(buttonSaying(dialogNow(), "Add a new server")));
    buttonSaying(dialogNow(), "Add a new server").click();
    await settle();
    dialog = dialogNow();
    check("adding a server starts from the machine you use",
          find(dialog, "connect-recipe").length === 4);
    check("...fetched rather than shipped in every page",
          fetched.some((f) => f.url === "/settings/recipes"));
    const badges = find(dialog, "connect-recipe-badge");
    check("a preset we have not connected with says so before it is chosen",
          badges.length === 2);
    check("...and a generic shape carries no badge to devalue that one",
          find(dialog, "connect-recipe")[1].textContent.indexOf("untested") < 0);

    // A poll arriving mid-form must not replace what somebody is typing.
    find(dialog, "connect-recipe")[0].click();
    await settle();
    dialog = dialogNow();
    let fields = find(one(dialog, "connect-form"), "connect-field");
    check("a preset asks only for what genuinely differs",
          fields.length === 5);
    check("...and says what the site expects, in sentences",
          find(dialog, "connect-notes").length === 1);
    const boxes = fields.map((f) => walk(f).find((n) => n.tagName === "INPUT"));

    // The three job numbers arrive filled in, not as grey placeholder text
    // over an empty box: a default nobody can see is a default nobody can
    // correct, and these three decide whether an import finishes or the
    // scheduler kills it partway through.
    const labelled = {};
    fields.forEach((f) => {
        const kids = walk(f);
        const label = kids.find((n) => n.classList.contains("connect-field-label"));
        labelled[label.textContent] = kids.find((n) => n.tagName === "INPUT");
    });
    check("the walltime a job asks for is on screen, filled in",
          labelled["How long to keep it (walltime)"].value === "4:00:00");
    check("...and so is the number of cores",
          labelled["CPU cores"].value === "16");
    check("...and the memory, which is what a pyramid actually runs out of",
          labelled["Memory"].value === "128G");
    check("...taken from the server rather than written into the browser",
          fetched.some((f) => f.url === "/settings/recipes"));

    // The escape hatch: a preset is a starting point and never a lock, and
    // correcting one should not mean saving it, leaving the dialog and finding
    // the server on another page.
    const advanced = one(dialog, "connect-advanced");
    check("a preset can be corrected before it is saved, not only after",
          Boolean(advanced));
    check("...shut on arrival, so a cluster's flag syntax is not in the way",
          advanced.tagName === "DETAILS" && !advanced.open);
    const advancedFields = find(advanced, "connect-field");
    check("...offering the job line, the launch command and the install switch",
          advancedFields.length === 3);
    const advancedBoxes = advancedFields.map(
        (f) => walk(f).find((n) => n.tagName === "INPUT"));
    check("the job line holds what the boxes above it do not, so the two "
          + "cannot contradict each other",
          advancedBoxes[0].value === "-p interactive");
    check("...and the launch command starts from the preset's own",
          advancedBoxes[1].value === "plexora");

    // Beside the field that names the environment, because that is the
    // environment it writes to -- and as one of the form's own grid cells, so
    // it sits next to that field wherever there is room rather than taking a
    // row of its own.
    const installSwitch = advancedFields[2];
    check("...with the install switch beside the environment it would write to",
          installSwitch.classList.contains("connect-switch")
          && advancedBoxes[2].type === "checkbox");
    check("...off on arrival, because no preset gets to decide that software "
          + "should be installed into somebody's account",
          advancedBoxes[2].checked === false);

    boxes[1].value = "aj";
    say(world([]));
    await settle();
    check("a poll while the form is open leaves what was typed alone",
          walk(find(dialogNow(), "connect-field")[1])
              .find((n) => n.tagName === "INPUT").value === "aj");

    fetched.length = 0;
    posted.length = 0;
    snapshot = world([profile("o2", { node: { state: "idle" } })]);
    // Turned on before saving, so what the server receives is pinned rather
    // than only the default.
    walk(find(dialogNow(), "connect-switch")[0])
        .find((n) => n.tagName === "INPUT").checked = true;
    buttonSaying(dialogNow(), "Save and connect").click();
    await settle();
    const saved = fetched.find((f) => f.method === "POST");
    check("saving goes to the server, which composes what a preset means",
          Boolean(saved) && saved.url === "/settings/recipes/hms-o2"
          && JSON.parse(saved.body).user === "aj");
    const sent = JSON.parse(saved.body);
    check("...carrying the three job numbers that were on screen",
          sent.walltime === "4:00:00" && sent.cores === "16"
          && sent.memory === "128G");
    check("...and whatever Advanced was left holding",
          sent.srun === "-p interactive" && sent.remote_command === "plexora");
    check("...including the install switch, as a boolean rather than a string",
          sent.install === true);
    check("...and connecting follows without a second press",
          posted.some((p) => p.action === "connect" && p.name === "o2"));
    say(world([profile("o2", { node: { state: "connected",
                                       node: "o2-data" } })]));
    outcome = await done;
    check("...ending with the machine the field asked for",
          outcome.connected === true && outcome.node === "o2-data");

    // -- the Settings page's "Start from a preset" ----------------------------
    //
    // The presets used to be reachable only by flipping a data field to Remote
    // with nothing saved, which is the one place somebody adding a server for
    // the first time was NOT looking. The Settings page's own button opens
    // this dialog on the catalogue, skipping the list of machines they have
    // just told us they do not have.
    snapshot = world([]);
    done = Modal.open({ kind: "node", view: "recipes" });
    await settle();
    check("a caller can open straight on the presets",
          find(dialogNow(), "connect-recipe").length === 4
          && !buttonSaying(dialogNow(), "Add a new server"));
    check("...and can still get back to the machines already saved",
          Boolean(buttonSaying(dialogNow(), "Back")));
    buttonSaying(dialogNow(), "Cancel").click();
    await done;

    //: One field of the Google Cloud form, by the label above it: its wrapper,
    //: its dropdown, and the box that a field which also lets you type swaps
    //: in. Looked up by label because that is what somebody reading the form
    //: has to go on, and because the order of the fields is not the point of
    //: any check here.
    function controls(pane, label) {
        const wrap = find(pane, "connect-field").find(
            (f) => walk(f).some(
                (n) => n.classList.contains("connect-field-label")
                    && n.textContent === label));
        const kids = wrap ? walk(wrap) : [];
        return {
            wrap: wrap,
            drop: wrap ? dropdown(wrap) : null,
            input: kids.find((n) => n.tagName === "INPUT"),
        };
    }


    //: One radio group of the Google Cloud form, found by a row it contains,
    //: and driven the way somebody drives it: click the row you want.
    function choiceGroup(root, rowLabel) {
        const wrap = find(root, "connect-choice").find(
            (f) => walk(f).some(
                (n) => n.classList.contains("connect-choice-label")
                    && n.textContent === rowLabel));
        if (!wrap) return null;
        const rows = find(wrap, "connect-choice-row");
        const words = (row) => one(row, "connect-choice-label").textContent;
        return {
            wrap: wrap,
            labels: () => rows.map(words),
            pick: (text) => {
                const row = rows.find((r) => words(r) === text);
                if (!row) throw new Error("no choice labelled " + text);
                row.click();
            },
            chosen: () => {
                const row = rows.find((r) => r.classList.contains("is-chosen"));
                return row ? words(row) : "";
            },
            off: () => rows.filter((r) => r.classList.contains("is-off"))
                           .map(words),
            why: () => one(wrap, "connect-choice-why").textContent,
            note: () => one(wrap, "connect-choice-note").textContent,
        };
    }

    //: The four-page form, as the parts a person operates: which page is on
    //: screen, the strip that says where they are, and the footer.
    function wizard(dialog) {
        const pages = () => find(dialog, "connect-wizard-page");
        return {
            pages: pages,
            page: (index) => pages()[index],
            at: () => pages().findIndex((p) => p.hidden === false),
            steps: () => find(dialog, "connect-wizard-step"),
            next: () => buttonSaying(dialog, "Next"),
            back: () => buttonSaying(dialog, "Back"),
            blocked: () => one(dialog, "connect-wizard-blocked").textContent,
        };
    }

    // -- the preset whose machine does not exist yet --------------------------
    //
    // Four pages, and their order is the order the answers depend on each
    // other in: Identity, then Data, then Compute, then what happens to the
    // machine afterwards. You cannot list projects until you know who is
    // asking, you cannot list buckets until you know the project, and the
    // region is decided by where the data turned out to be. Asking for the
    // machine first -- which is how a cloud console usually asks -- would mean
    // choosing a region before knowing which one the data is in, and that is
    // the mistake that costs money in egress.
    snapshot = world([]);
    fetched.length = 0;
    posted.length = 0;
    done = Modal.open({ kind: "node", view: "recipes" });
    await settle();
    find(dialogNow(), "connect-recipe")[3].click();
    await settle();
    await settle();
    dialog = dialogNow();
    let step = wizard(dialog);

    check("the Google Cloud form asks four questions, one page at a time",
          step.pages().length === 4 && step.at() === 0
          && step.steps().map((s) => s.textContent).join(" → ")
             === "Google Cloud → Data → Compute → When Plexora exits");
    check("...and will not skip ahead to a page whose questions are unanswered",
          step.steps()[0].disabled === false
          && step.steps().slice(1).every((s) => s.disabled === true));

    const identity = one(dialog, "connect-gcloud-identity-text");
    check("the Google Cloud form says who is signed in before anything else",
          Boolean(identity)
          && identity.textContent.indexOf("aj@example.com") >= 0);
    const projectField = controls(step.page(0), "Google Cloud project");
    check("the projects come from the account rather than from a box to type in",
          Boolean(projectField.drop)
          && projectField.drop.control.options.map((o) => o.value).join(",")
             === "lab-imaging,other-project");

    // Not the browser's dropdown. A native <select> can be styled shut and not
    // open: the menu is drawn by the operating system, in the system's
    // colours, and on this dialog that is a white rectangle no stylesheet
    // reaches. So the menu is an element, and it is closed until it is asked
    // for.
    check("a dropdown is drawn rather than handed to the operating system",
          projectField.drop.trigger.tagName === "BUTTON"
          && projectField.drop.isOpen() === false);
    projectField.drop.open();
    check("...and opens on the control being pressed",
          projectField.drop.isOpen() === true
          && projectField.drop.labels().join(" | ")
             === "Lab Imaging (lab-imaging) | Other (other-project)");
    projectField.drop.press("Escape");
    check("...and closes on Escape without taking the dialog with it",
          projectField.drop.isOpen() === false && dialogNow() === dialog);
    // A <label> forwards a click to the first labelable thing inside it. The
    // trigger is a button and the menu lives in the same field, so a label
    // here would have every row click choose, close, and then re-open.
    check("...from a field that is not a label, so a row click cannot re-open it",
          projectField.wrap.tagName === "DIV"
          && projectField.drop.trigger.getAttribute("aria-labelledby")
             === walk(projectField.wrap).find(
                 (n) => n.classList.contains("connect-field-label")).id);

    check("a page with every answer in it can be left",
          step.next().disabled === false && step.blocked() === "");
    step.next().click();
    await settle();
    check("...which is the data, because the machine is chosen to suit it",
          step.at() === 1);

    // The account's own buckets, in a control that looks like it has something
    // in it. This was a `<datalist>` on a text box first, and a datalist draws
    // no affordance at all: the list was fetched, parsed and filled, and the
    // field still looked like an empty box you had to know a name to use.
    const bucketField = controls(step.page(1), "Cloud Storage bucket");
    check("the account's buckets are a dropdown, each saying where it lives",
          Boolean(bucketField.drop)
          && bucketField.input.hidden === true
          && bucketField.drop.labels().join(" | ")
             === "Choose a bucket… | tonsil-images — US-EAST1 "
                + "| atlas-public — EU | Another bucket — type its name…");

    // The gate. There is deliberately no "continue without a bucket": the
    // bucket IS the reason the VM is being asked for, and a connection without
    // one would start a machine, bill somebody for it, and open a viewer onto
    // an empty directory.
    check("nothing goes past the data page without a bucket",
          step.next().disabled === true);
    check("...and the reason is beside the button that will not go",
          step.blocked() === "Choose a bucket Plexora can read.");

    // Chosen the way somebody chooses it: open the menu, click the row.
    bucketField.drop.pick("tonsil-images — US-EAST1");
    await settle();
    check("...and once it checks out, it can",
          step.next().disabled === false && step.blocked() === "");
    check("...with the control showing what was chosen, not a raw name",
          bucketField.drop.shown() === "tonsil-images — US-EAST1"
          && bucketField.drop.isOpen() === false);
    check("where it is mounted on the VM is asked beside which bucket it is",
          controls(step.page(1), "Mount location inside the VM").input.value
          === "~/plexora-data");

    step.next().click();
    await settle();
    check("the machine is asked about third, once the data is known",
          step.at() === 2);
    const region = controls(step.page(2), "VM location");
    check("the region follows the data rather than being asked for first",
          region.drop.control.value === "us-east1");
    check("...saying where that came from, on the field itself",
          region.drop.control.plexoraHint.textContent
              .indexOf("detected from your bucket") >= 0);
    check("...and matching regions raise no warning",
          one(dialog, "connect-gcloud-warn").hidden === true);

    // Computing somewhere other than where the data is, said as what it costs
    // rather than as a rule -- and with the button that fixes it, because a
    // warning somebody has to act on elsewhere is one most people ignore.
    region.drop.control.value = "europe-west4";
    region.drop.control.onchange();
    await settle();
    const warn = one(dialog, "connect-gcloud-warn");
    check("computing away from the data warns, in what it costs",
          warn.hidden === false
          && walk(warn)[0].textContent.indexOf("charges for data leaving") >= 0);
    buttonSaying(warn, "Use bucket region").click();
    await settle();
    check("...and offers the one-press fix",
          region.drop.control.value === "us-east1"
          && one(dialog, "connect-gcloud-warn").hidden === true);

    // The size, in sizes. A machine type name encodes the answer and does not
    // say it, and the form is where somebody should find out that the choice
    // between two of them is a choice between 128 GB and 256 GB.
    const machine = controls(step.page(2), "Machine type");
    check("the machine type arrives at the size a pyramid actually needs",
          machine.drop.control.value === "e2-highmem-16"
          && machine.drop.shown() === "e2-highmem-16 · 16 vCPU · 128 GB RAM");
    machine.drop.open();
    const offered = machine.drop.labels();
    check("...with something small enough to try the connection on",
          offered.indexOf("e2-medium · 2 shared vCPU · 4 GB RAM") >= 0);
    check("...saying which of them are fractions of a core rather than cores",
          offered.filter((l) => l.indexOf("shared vCPU") >= 0).length === 1);
    check("...and a shortlist short enough to read, not a catalogue",
          offered.length <= 6);
    check("...with a way out of it entirely",
          offered[offered.length - 1] === "Custom — type a machine type…");
    machine.drop.pick("Custom — type a machine type…");
    check("...which is a box, because the type wanted is one nobody listed",
          machine.input.hidden === false);
    machine.input.value = "c3-highmem-22";
    machine.drop.pick("e2-highmem-16 · 16 vCPU · 128 GB RAM");
    check("...and choosing from the list again puts the box away",
          machine.input.hidden === true);

    // How the machine is bought, which is the difference between a session
    // that costs $2 and one that costs $20. Spot by default: the data is in
    // the bucket rather than on the machine, and Plexora asks for a preempted
    // VM to be stopped rather than deleted, so being reclaimed costs a
    // reconnect.
    const spot = choiceGroup(step.page(2), "Spot (preemptible)");
    check("a new VM is bought at the spot price unless somebody says otherwise",
          Boolean(spot) && spot.chosen() === "Spot (preemptible)");
    check("...with what that trade actually is, under the choice",
          spot.why().indexOf("reclaim") >= 0);
    spot.pick("Standard");
    check("...and the other side of it when the other side is chosen",
          spot.chosen() === "Standard"
          && spot.why().indexOf("nobody takes it away") >= 0);
    spot.pick("Spot (preemptible)");

    step.next().click();
    await settle();
    check("the last page is the one nobody would have scrolled to",
          step.at() === 3
          && one(dialog, "connect-modal-title").textContent
             === "What should happen when Plexora exits?");
    const ending = choiceGroup(step.page(3), "Stop VM");
    check("all three endings are on screen together, not hidden in a dropdown",
          ending.labels().join(" | ")
          === "Leave VM running | Stop VM | Delete VM");
    check("...with the one that stops the bill chosen by default",
          ending.chosen() === "Stop VM");
    check("...and what each costs said under the one selected",
          ending.why().indexOf("disk keeps its charge") >= 0);
    ending.pick("Delete VM");
    check("...one sentence at a time, following the choice",
          ending.chosen() === "Delete VM"
          && ending.why().indexOf("bucket is untouched") >= 0);
    // And nothing else. This page used to carry the recipe's seven notes as
    // well -- billing, IAP roles, Spot, what Delete does not touch -- none of
    // which is about the question being asked here, and all of which had
    // already been said on the page it applied to.
    check("...and the page is the question, not a page of notes about it",
          find(step.page(3), "connect-notes").length === 0
          && find(step.page(3), "connect-choice").length === 1);

    // Backwards, and nothing is lost: every control on all four pages is made
    // once and only ever hidden. A wizard that rebuilt the page it returns to
    // would have to remember the answers separately from the controls holding
    // them, and the two would disagree the first time a lookup came back late.
    step.back().click();
    await settle();
    check("going back does not lose what was already chosen",
          step.at() === 2
          && controls(step.page(2), "Machine type").drop.control.value
             === "e2-highmem-16"
          && controls(step.page(2), "VM location").drop.control.value
             === "us-east1");
    check("...and a page already answered can be jumped straight back to",
          step.steps()[1].disabled === false);
    step.steps()[1].click();
    await settle();
    check("...which is what the strip at the top is for",
          step.at() === 1
          && controls(step.page(1), "Cloud Storage bucket").drop.shown()
             === "tonsil-images — US-EAST1");
    step.steps()[3].click();
    await settle();

    saveReply = { remote: { name: "gcp" } };
    snapshot = world([profile("gcp", { node: { state: "idle" } })]);
    const create = buttonSaying(dialog, "Create & Connect");
    check("the button on the last page says what pressing it will do",
          Boolean(create) && create.disabled === false);
    create.click();
    await settle();
    const cloudSaved = fetched.find(
        (f) => f.method === "POST" && f.url.indexOf("/recipes/gcloud") >= 0);
    const cloudSent = JSON.parse(cloudSaved.body);
    check("saving sends what the form worked out, not only what was typed",
          cloudSent.project === "lab-imaging"
          && cloudSent.bucket === "tonsil-images"
          && cloudSent.region === "us-east1"
          && cloudSent.bucket_location === "US-EAST1"
          && cloudSent.account === "aj@example.com"
          && cloudSent.zone === "us-east1-b"
          && cloudSent.machine_type === "e2-highmem-16"
          && cloudSent.mount_path === "~/plexora-data");
    check("...including the two answers that decide what a session costs",
          cloudSent.provisioning_model === "spot"
          && cloudSent.on_exit === "delete");
    check("...and still has nowhere to put a credential",
          Object.keys(cloudSent).every(
              (key) => !/password|token|secret|credential/i.test(key)));
    say(world([profile("gcp", { node: { state: "connected",
                                        node: "gcp-data" } })]));
    await done;
    saveReply = null;

    // A bucket this account cannot read is said in Google's own words, and
    // leaves the page where it was -- because the fix is a permission on
    // somebody else's bucket rather than anything on this form.
    cloud.bucketError = "This account cannot read gs://someone-elses.";
    snapshot = world([]);
    done = Modal.open({ kind: "node", view: "recipes" });
    await settle();
    find(dialogNow(), "connect-recipe")[3].click();
    await settle();
    await settle();
    dialog = dialogNow();
    step = wizard(dialog);
    step.next().click();
    await settle();
    // Typed, not picked -- and that is the point of the escape hatch. Listing
    // buckets is its own permission, so the bucket somebody actually has
    // access to is exactly the one that can be missing from the list.
    const badField = controls(step.page(1), "Cloud Storage bucket");
    badField.drop.pick("Another bucket — type its name…");
    check("a bucket the list did not cover can still be named",
          badField.input.hidden === false);
    badField.input.value = "someone-elses";
    badField.input.onchange();
    await settle();
    check("a bucket that cannot be read says so in Google's own words",
          one(dialog, "connect-modal-error").textContent
              .indexOf("cannot read gs://someone-elses") >= 0);
    check("...and leaves the form on the page that can fix it",
          step.at() === 1 && step.next().disabled === true);
    cloud.bucketError = null;
    buttonSaying(dialog, "Cancel").click();
    await done;

    // Signed out is the ordinary first state of this form, not an error.
    cloud.status = { installed: true, account: null };
    snapshot = world([]);
    done = Modal.open({ kind: "node", view: "recipes" });
    await settle();
    find(dialogNow(), "connect-recipe")[3].click();
    await settle();
    await settle();
    dialog = dialogNow();
    step = wizard(dialog);
    check("signed out is answered with a button rather than a red box",
          Boolean(buttonSaying(dialog, "Sign in with Google"))
          && one(dialog, "connect-modal-error").hidden === true);
    check("...saying which program does the signing in, since it is not this one",
          one(dialog, "connect-gcloud-identity-text").textContent
              .indexOf("Google Cloud CLI installed on this computer") >= 0);
    check("...and nothing below it can be reached until it is answered",
          step.next().disabled === true
          && step.blocked() === "Sign in to Google to continue.");
    cloud.status = { installed: false, account: null };
    buttonSaying(dialog, "Cancel").click();
    await done;

    snapshot = world([]);
    done = Modal.open({ kind: "node", view: "recipes" });
    await settle();
    find(dialogNow(), "connect-recipe")[3].click();
    await settle();
    await settle();
    check("no gcloud on this machine says where to get it",
          one(dialogNow(), "connect-gcloud-identity-text").textContent
              .indexOf("cloud.google.com/cli") >= 0);
    cloud.status = { installed: true, account: "aj@example.com" };
    buttonSaying(dialogNow(), "Cancel").click();
    await done;

    // -- a failure whose fix is one press gets one ---------------------------
    //
    // A zone with no spare Spot capacity is a price problem rather than a
    // broken configuration: the zone, the machine and the bucket were all
    // right, and the same request at full price very likely succeeds this
    // minute. Without the button, acting on that sentence means leaving the
    // failure, opening Settings, finding the profile, reaching the third page
    // of the form and changing one radio.
    deep = {};
    snapshot = world([profile("gcp", { node: { state: "connecting" } })]);
    done = Modal.open({ name: "gcp", kind: "node" });
    await settle();
    say(world([profile("gcp", {
        node: { state: "failed",
                error: "us-east1-b has no spare e2-highmem-16 to give away "
                       + "as a Spot VM right now.",
                recovery: "standard" } })]));
    await settle();
    dialog = dialogNow();
    check("a spot refusal offers the fix as a button, not a sentence to act on",
          Boolean(buttonSaying(dialog, "Reconnect with Standard")));
    check("...beside the two ways out that suit every other failure",
          Boolean(buttonSaying(dialog, "Try again")
                  && buttonSaying(dialog, "Edit connection")));
    posted.length = 0;
    buttonSaying(dialog, "Reconnect with Standard").click();
    await settle();
    await settle();
    check("...which changes what the profile buys, and then retries it",
          posted.some((p) => p.action === "vmStandard" && p.name === "gcp")
          && posted.some((p) => p.action === "connect"));

    // The ordinary failure keeps the ordinary row. A button offering to change
    // somebody's configuration must not appear for a failure that nobody said
    // it would fix -- which is why the key comes from the server beside the
    // error rather than from reading the error here.
    say(world([profile("gcp", {
        node: { state: "failed", error: "Permission denied." } })]));
    await settle();
    check("a failure with no named fix offers no button to guess at one",
          buttonSaying(dialogNow(), "Reconnect with Standard") === null
          && Boolean(buttonSaying(dialogNow(), "Try again")));
    buttonSaying(dialogNow(), "Close").click();
    await done;

    // -- whose machine it is, and what that changes ---------------------------
    //
    // One choice decides four things: whether Plexora may create a VM, whether
    // it may change its network, whether the size/disk/timer questions mean
    // anything, and whether Delete is on the menu at the end. A machine
    // somebody already runs answers no to all of them.
    fetched.length = 0;
    snapshot = world([]);
    done = Modal.open({ kind: "node", view: "recipes" });
    await settle();
    find(dialogNow(), "connect-recipe")[3].click();
    await settle();
    await settle();
    dialog = dialogNow();
    step = wizard(dialog);

    // The bucket is the premise whoever owns the VM, so it is answered here
    // exactly as it would be before any of the questions below matter.
    step.next().click();
    await settle();
    controls(step.page(1), "Cloud Storage bucket").drop
        .pick("tonsil-images — US-EAST1");
    await settle();
    step.next().click();
    await settle();

    const compute = step.page(2);
    const advPane = find(compute, "connect-form").pop();
    const advBoxes = {};
    const advWraps = {};
    find(advPane, "connect-field").forEach((f) => {
        const kids = walk(f);
        const label = kids.find(
            (n) => n.classList.contains("connect-field-label"));
        advWraps[label.textContent] = f;
        advBoxes[label.textContent] = kids.find(
            (n) => n.tagName === "INPUT");
    });

    // Operated the way it is operated: the checkbox is 1px and invisible, and
    // the label is the control. Toggling one must leave the form standing --
    // and must leave no menu open behind it, because an open menu is a fixed,
    // opaque panel over the dialog and would read as the modal going blank.
    const gcloudInstall = find(advPane, "connect-switch").find(
        (f) => walk(f).some((n) => n.textContent === "Install Plexora"));
    const installBox = walk(gcloudInstall).find((n) => n.tagName === "INPUT");
    check("a VM Plexora rented keeps itself up to date by default",
          installBox.checked === true);
    gcloudInstall.click();
    check("...and turning it off leaves the form where it was",
          installBox.checked === false
          && step.at() === 2
          && find(dialog, "connect-select-menu")
                 .every((m) => m.hidden === true));
    gcloudInstall.click();

    // A way out, not a way in. Default on because a VM with no route to the
    // internet cannot install Cloud Storage FUSE or Plexora and so cannot
    // connect at all -- and the hint has to carry the other half, since
    // "public IP address" reads as an invitation to the internet and this is
    // the opposite of one.
    check("a rented VM can reach out to install what it needs",
          advBoxes["Give VM a public IP address"].checked === true);
    check("...and the box says the door is still shut",
          walk(advWraps["Give VM a public IP address"]).some(
              (n) => (n.textContent || "").includes("blocks inbound access")));
    check("...and switches itself off if it is left with nobody connected",
          advBoxes["Idle shutdown time (minutes)"].value === "30");
    // The disk is the one thing that goes on billing after the VM stops, so
    // the default is the small end of what fits rather than the roomy end.
    check("...and asks for the smallest disk the work actually needs",
          advBoxes["Boot disk (GB)"].value === "20");

    const vmField = controls(compute, "Existing VM");
    const source = choiceGroup(compute, "Create a new VM");
    check("the VM to use is the first question on the page, and a new one "
          + "is the default",
          source.chosen() === "Create a new VM" && vmField.wrap.hidden === true);

    source.pick("Use an existing VM");
    await settle();
    check("pointing at your own VM asks which one",
          vmField.wrap.hidden === false);
    check("...and stops asking what size to order, or what to bill for",
          advWraps["Boot disk (GB)"].hidden === true
          && advWraps["Idle shutdown time (minutes)"].hidden === true
          && controls(compute, "Machine type").wrap.hidden === true);
    check("...or offering to change somebody else's network",
          advWraps["Give VM a public IP address"].hidden === true);
    check("...or how to buy a machine that was bought long ago",
          choiceGroup(compute, "Spot (preemptible)").wrap.hidden === true);
    check("...or where to put one that is already somewhere",
          controls(compute, "VM location").wrap.hidden === true);
    // The zone stays, and it is the only control here that means something
    // different in each mode: a preference for a machine Plexora would place,
    // a fact for one that is already somewhere -- and the only way out for a
    // VM this account cannot list, whose zone cannot be looked up by name.
    check("...though the zone stays, because a VM Plexora cannot list has one",
          controls(compute, "Zone").wrap.hidden === false);
    check("...and refusing to go on until a machine is actually named",
          step.next().disabled === true
          && step.blocked() === "Choose the VM to connect to.");
    check("...while still offering the machines the project already has",
          fetched.some((f) => f.url.indexOf("settings/gcloud/instances") >= 0));
    // Everything choosing between two machines turns on: how big it is, where
    // it is -- which decides the region -- and whether Plexora will have to
    // start it first. Google's own TERMINATED is not a word for a list
    // somebody is reading.
    check("...as a dropdown of what the project has, not a name to remember",
          vmField.drop.root.hidden === false
          && vmField.drop.labels().join(" | ")
             === "Choose a VM… | analysis-box — n2-highmem-32 — us-central1-a "
                + "— Stopped | Another VM — type its name…");

    // The rest of this form reasons outwards from the data: the bucket picks
    // the region, the region picks the zone. A machine that already exists
    // inverts that -- it is somewhere, and that somewhere is a fact.
    vmField.drop.pick("analysis-box — n2-highmem-32 — us-central1-a — Stopped");
    check("...and going on once one is", step.next().disabled === false);
    check("choosing a VM takes the zone it is actually in",
          controls(compute, "Zone").drop.control.value === "us-central1-a");
    check("...and says where that is, since it is a fact and not a setting",
          one(compute, "connect-gcloud-where").hidden === false
          && one(compute, "connect-gcloud-where").textContent
                 .indexOf("us-central1-a") >= 0);
    const farWarn = one(dialog, "connect-gcloud-warn");
    // A name the list did not cover: the zone in the control belongs to the
    // BUCKET, and sending it would describe an instance in a zone it is not
    // in -- failing with "there is no VM called that" about a VM that exists.
    // Cleared, so the server looks the machine up by name instead.
    vmField.drop.pick("Another VM — type its name…");
    vmField.input.value = "some-other-box";
    vmField.input.onchange();
    check("a VM the list did not cover does not inherit the bucket's zone",
          controls(compute, "Zone").drop.control.value === "");
    vmField.drop.pick("analysis-box — n2-highmem-32 — us-central1-a — Stopped");

    check("...and being far from the data is still said, in what it costs",
          farWarn.hidden === false
          && walk(farWarn)[0].textContent.indexOf("us-east1") >= 0);
    check("...in the tense that is true of a machine already running",
          walk(farWarn)[0].textContent.indexOf("this VM runs in") >= 0);
    check("...and without offering to move a VM that cannot be moved",
          buttonSaying(farWarn, "Use bucket region") === null);

    step.next().click();
    await settle();
    const byoEnding = choiceGroup(step.page(3), "Stop VM");
    check("Plexora will not offer to delete a machine it did not create",
          byoEnding.off().join(",") === "Delete VM"
          && byoEnding.chosen() === "Stop VM");
    check("...and says why the row is greyed rather than leaving it a mystery",
          byoEnding.note().indexOf("did not create") >= 0);

    saveReply = { remote: { name: "byo" } };
    snapshot = world([profile("byo", { node: { state: "idle" } })]);
    const connectVm = buttonSaying(dialog, "Connect to VM");
    check("the button no longer offers to create anything",
          Boolean(connectVm)
          && buttonSaying(dialog, "Create & Connect") === null);
    connectVm.click();
    await settle();
    const byoSent = JSON.parse(fetched.filter(
        (f) => f.method === "POST"
        && f.url.indexOf("/recipes/gcloud") >= 0).pop().body);
    check("bringing your own VM sends which one, and still sends the bucket",
          byoSent.vm_source === "existing"
          && byoSent.vm_name === "analysis-box"
          && byoSent.bucket === "tonsil-images");
    check("...and never asks for it to be deleted",
          byoSent.on_exit !== "delete");
    say(world([profile("byo", { node: { state: "connected",
                                        node: "byo-data" } })]));
    await done;
    saveReply = null;

    // -- the steps a rented machine adds --------------------------------------
    check("a Google Cloud connection starts a VM before it signs in",
          Modal.stepStates("preparing_compute", "node", false, null, false,
                           true).map((s) => s.label)[0]
          === "Starting the Compute Engine VM");
    check("...and mounts the bucket after signing in, before anything is run",
          Modal.stepStates("mounting_data", "node", false, null, false, true)
              .map((s) => s.label).indexOf("Mounting your Cloud Storage bucket")
          === 3);
    check("...and neither step is drawn for a connection that does not do it",
          Modal.stepStates("connecting", "node", false, null, false, false)
              .some((s) => /Compute Engine|Cloud Storage/.test(s.label))
          === false);

    // -- a refusal that is really "already running" ---------------------------
    posted.length = 0;
    connectRejects = "A connection to “hpc” is already open.";
    snapshot = world([profile("hpc", { node: { state: "connecting" } })]);
    done = Modal.open({ name: "hpc", kind: "node" });
    await settle();
    check("a refusal from a connection that IS opening is not shown as failure",
          one(dialogNow(), "connect-modal-error").hidden === true);
    buttonSaying(dialogNow(), "Continue in background").click();
    await done;

    console.log(failures.length
        ? `\n${failures.length} failed`
        : "\nall connection-modal checks passed");
    if (failures.length) process.exitCode = 1;
}

main();
