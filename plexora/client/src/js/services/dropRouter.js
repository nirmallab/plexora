/**
 * dropRouter.js -- what files dropped on the window do, decided by where they
 * land and what is open.
 *
 * The desktop shell delivers a drop as PATHS (desktopBridge.js). A field, a
 * dialog or a panel that registered itself with browsePicker's
 * `attachDropTarget` claims a drop over it; this file is everything else:
 *
 *   - **The viewer is a target.** A mask, a cell table, a markers.csv or an
 *     image dropped on an open sample is asked about first -- POST
 *     /drop/inspect (server/models/drop_plan.py) answers what each file is
 *     relative to that sample and what to do with it -- and then done: on its
 *     own when the server is sure, after a compact confirmation when it is
 *     not, and never when it would replace something without asking.
 *   - **Anywhere else, the plan decides between "open" and "import"**: a
 *     project folder or an image Plexora already has opens that project; a
 *     new one goes through the one-press import or the Import dialog.
 *   - **A drop that misses an open dialog is refused**, with a notice saying
 *     so, rather than opening the Import dialog on top of it. That fallback
 *     is how a markers.csv dropped beside "Add channel names" used to be
 *     imported as a sample.
 *
 * Every action runs through a route the app already had (`/import/layers`,
 * `/import/sample`, `/desktop/open`, `/upload_channels`) and the waits that
 * already follow them, so a confirmed drop and one that ran by itself are the
 * same request. Plugins add their own kinds: a server-side sniffer
 * (`api.register_drop_sniffer`) and an executor here (`registerExecutor`).
 */
window.PlexoraDropRouter = (function () {
    "use strict";

    //: Actions carried across a navigation: "open this project, then attach
    //: these" (`plan.actions[].then`). sessionStorage, like carryOver.js.
    const HANDOFF_KEY = "plexora:drop-handoff";
    const HANDOFF_MS = 2 * 60 * 1000;

    //: kind -> async (action, {choice, plan, paths}) => void
    const executors = new Map();
    //: The drop being carried out, so a second waits for it rather than
    //: racing it -- two masks attached at once would each reload the page.
    let running = null;

    function url(path) {
        return typeof plexoraUrl === "function" ? plexoraUrl(path) : `/${path}`;
    }

    function datasource() {
        return window.flaskVariables?.datasource
            || document.body?.dataset?.plexoraDatasource || "";
    }

    function viewerShowing() {
        const host = document.getElementById("container");
        return Boolean(datasource()) && Boolean(host)
            && !host.classList.contains("plexora-view-hidden");
    }

    function openModal() {
        const open = Array.from(document.querySelectorAll("dialog[open]"));
        return open.some((dialog) => {
            try {
                return dialog.matches(":modal");
            } catch (error) {
                return true;
            }
        });
    }

    /** Where a drop is happening: "modal", "viewer", or "home" (anything
     *  with no sample open on screen). */
    function context() {
        if (openModal()) return "modal";
        if (viewerShowing()) return "viewer";
        return "home";
    }

    function toast(title, note, tone) {
        window.PlexoraToast?.show?.({title, note, tone});
    }

    function name(path) {
        return String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "";
    }

    // -- the plan ------------------------------------------------------------

    /**
     * What the server says these paths are, here. Null when it could not say
     * (an older server, a sample it does not know): the caller falls back to
     * what a drop did before there was a plan.
     */
    async function planFor(paths, {target, forget = false} = {}) {
        const where = target || context();
        if (where === "modal" || !paths || !paths.length) return null;
        try {
            const response = await fetch(url("drop/inspect"), {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    paths,
                    datasource: where === "viewer" ? datasource() : null,
                    target: where,
                    forget,
                }),
            });
            if (!response.ok) return null;
            return await response.json();
        } catch (error) {
            return null;
        }
    }

    /** The label beside the pointer, from the plan. */
    function hintFor(plan) {
        if (!plan || !plan.summary || !plan.summary.hint) return "";
        return {label: plan.summary.hint, tone: plan.summary.tone || "ok"};
    }

    // -- confirming ----------------------------------------------------------

    const TITLES = {
        size_mismatch: "Segmentation mask detected",
        name_mismatch: "Segmentation mask detected",
        size_unknown: "Segmentation mask detected",
        choose_target: "Segmentation mask detected",
        ambiguous_pixels: "Mask or image?",
        mask_exists: "This sample already has a mask",
        attached_elsewhere: "Already attached to another sample",
        table_exists: "This sample already has cell data",
        no_coordinates: "No cell coordinates found",
        names_present: "Replace the channel names?",
        related_to_open: "Add to this sample?",
        open_and_attach: "Open the sample these files belong to?",
        adds_layer: "Add as a layer?",
        bundle_to_open: "Add to this sample?",
        bundle_replaces: "Replace what this sample has?",
        overwrites_gates: "Load gates?",
    };

    //: Reasons whose next step IS a dialog that asks the question properly --
    //: so it is opened straight away rather than asked about first.
    const DIRECT = new Set(["count_mismatch", "subset_unknown", "bundle_questions"]);

    function sizeText(pair) {
        return pair ? `${Number(pair[0]).toLocaleString()}×${Number(pair[1]).toLocaleString()}` : "";
    }

    /** The lines under the title: what was found, and why it is being asked. */
    function bodyFor(action, plan) {
        const fit = action.fit || {};
        const open = plan.open || {};
        const lines = [action.message];
        if (["attach_mask"].includes(action.kind) || action.reason === "ambiguous_pixels") {
            if (fit.size && fit.imageSize) {
                lines.push(fit.compatible === false
                    ? `The mask is ${sizeText(fit.size)} px and the image is `
                      + `${sizeText(fit.imageSize)} px, so it would not line up with the cells.`
                    : `Mask ${sizeText(fit.size)} px · image ${sizeText(fit.imageSize)} px.`);
            } else if (fit.compatible === null && action.reason === "size_unknown") {
                lines.push("Its size could not be read, so it cannot be checked against the image.");
            }
            if (fit.nameMatch === "exact" || fit.nameMatch === "prefix") {
                lines.push(`Its name matches ${open.title || action.sample || "the image"}.`);
            } else if (action.reason === "name_mismatch") {
                lines.push(`Its name does not match ${open.title || "the image"}.`);
            }
            if (action.reason !== "ambiguous_pixels") {
                lines.push("Cell ids are checked against the cell data once it is attached.");
            }
        }
        if (action.reason === "names_present" && Array.isArray(fit.current)) {
            const show = (list) => list.slice(0, 6).join(", ")
                + (list.length > 6 ? `, … (${list.length})` : "");
            lines.push(`Now: ${show(fit.current)}`);
            lines.push(`From the file: ${show(fit.names || [])}`);
        }
        if (action.reason === "open_and_attach") {
            lines.push((action.then || []).map((next) => name(next.paths?.[0])).join(", "));
        }
        return lines.filter(Boolean);
    }

    /** A project picker for a mask with nothing open: the samples whose image
     *  is the mask's size, best name match first. */
    function targetSelect(action) {
        const wrap = document.createElement("label");
        wrap.className = "plx-drop-target-field";
        const text = document.createElement("span");
        text.textContent = "Attach to";
        const select = document.createElement("select");
        select.className = "form-select";
        (action.choices || []).forEach((choice) => {
            const option = document.createElement("option");
            option.value = choice.value;
            option.textContent = choice.label;
            select.appendChild(option);
        });
        select.value = action.default || "";
        wrap.append(text, select);
        return {wrap, select};
    }

    /**
     * Ask about one action. Resolves the choice taken -- a `choices[]` entry,
     * or `{value: "go"}` for an action with one way forward -- or null when
     * the user said no.
     */
    async function confirm(action, plan) {
        const ask = window.PlexoraConfirm;
        if (!ask) return null;
        const title = TITLES[action.reason] || "Add these files?";
        const body = bodyFor(action, plan);
        if (action.reason === "choose_target") {
            const picker = targetSelect(action);
            const value = await ask.choose({
                title, body, content: picker.wrap,
                choices: [
                    {value: null, label: "Cancel"},
                    {value: "go", label: "Attach", kind: "primary", focus: true},
                ],
            });
            if (value !== "go") return null;
            return (action.choices || []).find((c) => c.value === picker.select.value) || null;
        }
        if (Array.isArray(action.choices) && action.choices.length) {
            const choices = [{value: null, label: "Cancel",
                              focus: action.default === null || action.default === undefined}];
            action.choices.forEach((choice) => {
                choices.push({
                    value: choice.value,
                    label: choice.label,
                    // Destructive answers are red and never the default; the
                    // default is the one blue button. "Attach anyway" for a
                    // mask of the wrong size is neither.
                    kind: choice.tone === "danger" ? "danger"
                        : choice.value === action.default ? "primary" : undefined,
                    focus: choice.value === action.default && choice.tone !== "danger",
                });
            });
            const value = await ask.choose({
                title, body, choices, stack: action.choices.length > 1,
            });
            if (value === null || value === undefined) return null;
            return action.choices.find((choice) => choice.value === value) || null;
        }
        const verb = {
            attach_mask: "Attach", attach_table: action.reason === "table_exists" ? "Replace" : "Attach",
            apply_channel_names: "Replace names",
            add_layer: action.reason === "bundle_replaces" ? "Replace" : "Add",
            open_project: "Open", upload_gates: "Load gates",
        }[action.kind] || "Continue";
        const danger = ["table_exists", "names_present", "overwrites_gates",
                        "bundle_replaces"].includes(action.reason);
        const value = await ask.choose({
            title, body,
            choices: [
                {value: null, label: "Cancel", focus: danger},
                {value: "go", label: verb, kind: danger ? "danger" : "primary", focus: !danger},
            ],
        });
        return value === "go" ? {value: "go"} : null;
    }

    // -- doing ---------------------------------------------------------------

    async function postJson(path, body) {
        const response = await fetch(url(path), {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(body || {}),
        });
        let result = {};
        try {
            result = await response.json();
        } catch (error) {
            result = {error: `The server answered HTTP ${response.status}.`};
        }
        if (!response.ok || result.error) {
            throw new Error(result.error || `The server answered HTTP ${response.status}.`);
        }
        return result;
    }

    function go(href) {
        if (window.PlexoraRouter?.go) window.PlexoraRouter.go(href);
        else window.location.href = href;
    }

    function sampleUrl(sample) {
        return url(encodeURIComponent(sample));
    }

    /** Add files to a sample through `/import/layers`. On the sample that is
     *  open, the page takes them on (importSample.addToSample); on another,
     *  the files are attached and that sample opens. */
    async function addLayers(route, effects) {
        const {sample, paths, answers} = route.json;
        const status = window.PlexoraStatus?.begin?.(`Adding ${paths.map(name).join(", ")}`);
        if (sample === datasource() && viewerShowing() && window.PlexoraImportSample?.addToSample) {
            const done = await window.PlexoraImportSample.addToSample(sample, paths, {answers});
            if (done.ok) {
                status?.done?.();
                if (!done.result) toast("Already attached", paths.map(name).join(", "));
                else if (!done.result.reload) toast(`Added to ${sample}`, paths.map(name).join(", "));
                return;
            }
            status?.done?.();
            if (done.needs) {
                // Something only the dialog can settle: open it on the files,
                // scoped to this sample, with what is already known.
                window.PlexoraImportSample.open({sample, paths, answers});
                return;
            }
            throw new Error(done.error || "Could not add that.");
        }
        const result = await postJson("import/layers", {sample, paths, answers});
        status?.done?.();
        if (effects?.navigate || result.reload || sample !== datasource()) {
            go(sampleUrl(sample));
        }
    }

    /** Write the actions to run once `sample` has opened. */
    function handOff(sample, actions) {
        if (!actions || !actions.length) return;
        try {
            window.sessionStorage.setItem(HANDOFF_KEY, JSON.stringify({
                datasource: sample, then: actions, at: Date.now(),
            }));
        } catch (error) {
            // Private mode or storage off: the project still opens, and the
            // files can be dropped again on it.
        }
    }

    /**
     * Run the actions a drop handed over before it navigated here. Called by
     * main.js once the viewer is up. Once only, and only on the sample they
     * were for, and only fresh: a stale one is somebody else's drop.
     */
    async function takeHandoff(sample) {
        let entry = null;
        try {
            const raw = window.sessionStorage.getItem(HANDOFF_KEY);
            window.sessionStorage.removeItem(HANDOFF_KEY);
            entry = raw ? JSON.parse(raw) : null;
        } catch (error) {
            return false;
        }
        if (!entry || entry.datasource !== sample || !Array.isArray(entry.then)) return false;
        if (!(Date.now() - Number(entry.at || 0) < HANDOFF_MS)) return false;
        await runActions(entry.then, {open: {name: sample, title: sample}}, []);
        return true;
    }

    const CORE = {
        noop_already_open: async (action) => toast("Already open", action.message),
        noop_already_attached: async (action) => toast("Already attached", action.message),
        unsupported: async (action) => toast("Not dropped", action.message, "warning"),

        open_project: async (action, {choice}) => {
            const route = (choice && choice.route) || action.route;
            let target = (choice && choice.sample) || action.sample;
            let href = target ? sampleUrl(target) : "";
            if (route) {
                const result = await postJson(route.path, route.json);
                href = result.url || href;
                target = result.project || target;
            }
            handOff(target, action.then);
            if (href) go(href);
        },

        attach_mask: async (action, {choice}) => {
            const route = (choice && choice.route) || action.route;
            if (!route) return;
            await addLayers(route, (choice && choice.effects) || action.effects);
        },

        apply_channel_names: async (action) => {
            const names = window.PlexoraChannelNames;
            const form = action.route?.form || {};
            if (!names?.apply) return;
            await names.apply({
                datasource: form.datasource || datasource(),
                path: form.path || action.paths[0],
                column: form.column,
                hasHeader: form.has_header === "true",
                onApplied: async (applied) => {
                    toast("Channel names applied",
                          `${applied.length} channels named from ${name(form.path)}.`);
                    const adopted = await window.__plexora?.adoptChannelNames?.(applied);
                    if (!adopted) window.location.reload();
                },
            });
        },

        import_new: async (action, {choice, plan, paths}) => {
            const route = (choice && choice.route) || action.route;
            if (!route) return CORE.open_dialog(action, {plan, paths});
            try {
                const result = await postJson(route.path, route.json);
                go(result.redirect || sampleUrl(result.name));
            } catch (error) {
                // The dialog says what went wrong, on the files, with a way on.
                window.PlexoraImportSample?.open({paths: action.paths});
            }
        },

        open_dialog: async (action, {plan, paths}) => {
            const importer = window.PlexoraImportSample;
            if (!importer) return;
            if (action.sample) {
                importer.open({sample: action.sample, paths: action.paths});
                return;
            }
            // Handed what was already read when it was read from exactly these
            // files, so the dialog opens on it rather than reading again.
            const whole = plan && plan.proposal && paths && paths.length === action.paths.length
                && !(plan.unrecognised || []).some((entry) => entry.code !== "unsupported");
            importer.open(whole ? {paths: paths.slice(), proposal: plan.proposal}
                                : {paths: action.paths});
        },
    };
    CORE.attach_table = CORE.attach_mask;
    CORE.add_layer = CORE.attach_mask;

    /** Which executor runs this action, given the choice made: a choice may
     *  turn "attach the mask" into "open the other sample". */
    function executorFor(action, choice) {
        const kind = (choice && choice.kind) || action.kind;
        return executors.get(kind) || CORE[kind] || null;
    }

    async function runActions(actions, plan, paths) {
        for (const action of actions) {
            if (action.status === "blocked") {
                toast("Not dropped", action.message, "warning");
                continue;
            }
            let choice = null;
            if (action.status === "needs_confirm" && !DIRECT.has(action.reason)) {
                choice = await confirm(action, plan);
                if (!choice) continue;
            }
            if (action.reason === "subset_unknown" || action.reason === "bundle_questions") {
                await CORE.open_dialog(action, {plan, paths});
                continue;
            }
            const run = executorFor(action, choice);
            if (!run) {
                toast("Not dropped", `${action.message} (nothing here can do that)`, "warning");
                continue;
            }
            try {
                await run(action, {choice, plan, paths});
            } catch (error) {
                toast("Could not add that", error.message || String(error), "warning");
                // What follows may depend on it -- a mask after the table it
                // was meant to sit under -- so the rest are not attempted.
                return;
            }
            const effects = (choice && choice.effects) || action.effects || {};
            if (effects.navigate) return;
        }
    }

    /**
     * Carry out a plan: every action in order, each on its own or after a
     * confirmation, the files nothing could read named once at the end.
     */
    async function runPlan(plan, paths) {
        if (!plan) return legacy(paths);
        if (running) {
            toast("Waiting", "The previous drop is still being added.");
            try {
                await running;
            } catch (error) { /* reported by its own run */ }
        }
        const job = (async () => {
            const refused = plan.unrecognised || [];
            await runActions(plan.actions || [], plan, paths || []);
            if (refused.length) {
                toast(refused.length === 1 ? "One file was not added" : `${refused.length} files were not added`,
                      refused.map((entry) => `${name(entry.path)}: ${entry.reason}`).join("\n"),
                      "warning");
            }
        })();
        running = job;
        try {
            await job;
        } finally {
            if (running === job) running = null;
        }
    }

    /** What a drop did before there was a plan: the Import dialog. */
    function legacy(paths) {
        if (window.PlexoraImportSample?.dropPaths) window.PlexoraImportSample.dropPaths(paths);
    }

    /**
     * A drop no zone claimed (desktopBridge.js). A modal open means the drop
     * missed it, which is refused out loud; anywhere else, the plan.
     */
    async function unclaimed(paths) {
        if (!paths || !paths.length) return;
        const where = context();
        if (where === "modal") {
            toast("Not dropped",
                  "A dialog is open. Drop the files onto it, or close it first.",
                  "warning");
            return;
        }
        const plan = await planFor(paths, {target: where, forget: true});
        if (!plan) {
            legacy(paths);
            return;
        }
        await runPlan(plan, paths);
    }

    /** Whether a plan needs more than the home page's one-press open: a
     *  project to open, a mask with a sample to choose, a refusal. */
    function claims(plan) {
        return Boolean(plan) && (plan.actions || []).some((action) =>
            !["import_new", "open_dialog"].includes(action.kind));
    }

    function registerExecutor(kind, fn) {
        if (kind && typeof fn === "function") executors.set(kind, fn);
    }

    // -- the zones -----------------------------------------------------------

    if (typeof provideDropPlan === "function") {
        provideDropPlan((paths) => planFor(paths));
    }

    function mountViewerZone() {
        const shell = document.getElementById("bodyDiv");
        if (!shell || typeof attachDropTarget !== "function") return;
        attachDropTarget(shell, {
            filter: "any", mode: "any", count: "many",
            enabled: viewerShowing,
            hint: (paths, {plan}) => hintFor(plan) || "Add to this sample",
            onDrop: async (paths, {plan}) => {
                const known = plan || await planFor(paths, {target: "viewer", forget: true});
                await runPlan(known, paths);
            },
        });
    }

    function mountLibraryZone() {
        const page = document.querySelector(".open-project-page");
        if (!page || typeof attachDropTarget !== "function") return;
        attachDropTarget(page, {
            filter: "any", mode: "any", count: "many",
            hint: (paths, {plan}) => hintFor(plan) || "Open or import",
            onDrop: async (paths, {plan}) => {
                const known = plan || await planFor(paths, {target: "home", forget: true});
                await runPlan(known, paths);
            },
        });
    }

    if (typeof PlexoraPage !== "undefined") {
        PlexoraPage.register(() => {
            mountViewerZone();
            mountLibraryZone();
        });
    }

    return {context, planFor, unclaimed, runPlan, claims, takeHandoff, registerExecutor,
            _internal: {confirm, bodyFor, HANDOFF_KEY}};
})();
