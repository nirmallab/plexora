/**
 * New Dataset: a dataset from a folder, in one dialog.
 *
 * Somebody points at the folder that holds a cohort -- `registration/ +
 * segmentation/ + quantification/`, or `Sample01/ ... Sample40/`, or a mix --
 * and Plexora reads it, matches every mask and table to its image, and shows
 * the result as a table to check: one row per sample, with the image, the
 * segmentation, the data and a status. The few files it could not place sit
 * under the table, each with its most likely sample suggested. One press
 * creates the dataset through the same service `plexora dataset create` uses.
 *
 *   pick       The dataset's name (taken from the folder until it is typed
 *              into) and the folder. A folder left blank makes an empty
 *              dataset, which is what this button used to do.
 *   scanning   The table fills in as the folder is read: names first, then
 *              each file's header. Cancel keeps what was found.
 *   plan       Check and correct. Expand a row to see every path and why it
 *              was matched; drag an unassigned file onto a row, or Assign to…
 *   building   The Status column becomes a progress rail. Closing the dialog
 *              does not stop it; reopening New Dataset shows it again.
 *   done       Open the dataset, or retry what failed.
 *
 * Nothing here decides what a file is or which sample it belongs to: the
 * server's plan (`/datasets/scan/<token>`) says, with its reasons, and this
 * draws it. A correction is posted whole and the plan comes back recomputed,
 * because moving one mask changes other rows -- the dialog never guesses the
 * outcome of an edit on the server's behalf.
 *
 * Split in two so the part that can be wrong quietly can be tested without a
 * browser: `createController` owns the requests, the polling and the order
 * replies are applied in (tests/js/dataset_builder_probe.mjs drives it); the
 * rest draws `controller.state`.
 *
 * Served as a classic script from base.html, after importSample.js.
 */
window.PlexoraDatasetBuilder = (function () {
    "use strict";

    //: How often a running scan is asked what it has found.
    const SCAN_POLL_MS = 700;
    //: How often a running build is asked. Registration steps take seconds.
    const BUILD_POLL_MS = 1500;
    //: Corrections made in quick succession (ticking three files and pressing
    //: Exclude, two drags) go to the server as one request.
    const PLAN_DEBOUNCE_MS = 150;
    //: A build left running behind a closed dialog is checked this often.
    const BACKGROUND_POLL_MS = 3000;

    const KIND_ICON = {
        image: "fa-layer-group",
        mask: "fa-shapes",
        table: "fa-table",
        channels: "fa-list-ul",
        other: "fa-file",
    };

    const KIND_WORD = {
        image: ["image", "images"],
        mask: ["mask", "masks"],
        table: ["data file", "data files"],
        channels: ["channel list", "channel lists"],
        other: ["other file", "other files"],
    };

    const SLOT_LABEL = {image: "Image", mask: "Segmentation", table: "Data", channels: "Channel names"};

    const STATUS = {
        matched: ["Matched", "is-matched"],
        review: ["Review", "is-review"],
        missing_mask: ["Missing mask", "is-missing"],
        missing_data: ["Missing data", "is-missing"],
        image_only: ["Image only", "is-plain"],
        existing: ["Already imported", "is-existing"],
        unavailable: ["File missing", "is-unavailable"],
        checking: ["Checking", "is-checking"],
    };

    const BUILD_STATUS = {
        pending: ["Waiting", "is-pending"],
        registering: ["Registering", "is-registering"],
        created: ["Created", "is-ready"],
        adopted: ["Added", "is-ready"],
        failed: ["Failed", "is-failed"],
        skipped: ["Not created", "is-pending"],
    };

    const ATTENTION = new Set(["review", "missing_mask", "missing_data", "unavailable"]);

    const FILTERS = [
        ["all", "All"],
        ["matched", "Matched"],
        ["attention", "Needs attention"],
        ["existing", "Already imported"],
    ];

    //: The machine the last pick was read from -- Import Sample's own key, so
    //: the two dialogs open on the same machine.
    const WHERE_KEY = "plexora.import.where";

    let dialog = null;
    let ui = null;
    let controller = null;
    //: `{token, name}` of a build still running after its dialog closed.
    let lastBuild = null;

    // -- small helpers ----------------------------------------------------------

    function esc(value) {
        return String(value ?? "").replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        }[c]));
    }

    function basename(path) {
        const parts = String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/);
        return parts[parts.length - 1] || "";
    }

    function dirname(rel) {
        const parts = String(rel || "").split("/");
        parts.pop();
        return parts.join("/");
    }

    /** A long path's last three folders, which is the part that says which. */
    function tail(path) {
        const parts = String(path || "").split(/[\\/]/).filter(Boolean);
        return parts.length > 3 ? `…/${parts.slice(-3).join("/")}` : String(path || "");
    }

    function plural(count, [one, many]) {
        return `${count} ${count === 1 ? one : many}`;
    }

    function samples(count) {
        return plural(count, ["sample", "samples"]);
    }

    function emptyOverrides() {
        return {assign: [], exclude: [], detach: [], new_project: [], rename: {}, learn: []};
    }

    /** A copy, with every list present whatever the server sent. */
    function cloneOverrides(overrides) {
        return {...emptyOverrides(), ...JSON.parse(JSON.stringify(overrides || {}))};
    }

    async function readJson(response) {
        try {
            return await response.json();
        } catch (error) {
            return {error: `${response.status} ${response.statusText || "No answer"}`};
        }
    }

    function defaultFetch(path, {method = "GET", body, signal} = {}) {
        const init = {method, signal};
        if (body !== undefined) {
            init.headers = {"Content-Type": "application/json"};
            init.body = JSON.stringify(body);
        }
        return fetch(plexoraUrl(path), init).then(async (response) => ({
            ok: response.ok, status: response.status, body: await readJson(response),
        }));
    }

    // -- the controller: requests, polling, and the order replies land in ------

    /**
     * The dialog's state machine, with no DOM.
     *
     * @param request - `(path, {method, body}) => Promise<{ok, status, body}>`.
     * @param timers - `{setTimeout, clearTimeout}`; the probe passes its own.
     * @param onChange - called after anything in `state` changed.
     */
    function createController({request = defaultFetch, timers = window, onChange = () => {}} = {}) {
        const state = {
            phase: "pick",
            token: null,
            //: The data node the folders are on; null here or on an `sftp://`
            //: server, whose addresses name it themselves.
            node: null,
            //: The last snapshot: progress, counts and the plan, as one object.
            scan: null,
            //: What the user has said, as the server will be told it.
            overrides: emptyOverrides(),
            //: The last overrides the server accepted, to fall back to.
            accepted: emptyOverrides(),
            //: Corrections sent and not yet answered.
            planning: false,
            planSeq: 0,
            build: null,
            error: null,
            closed: false,
        };
        let pollTimer = null;
        let planTimer = null;
        let polling = false;
        //: Which start is the current one; `stopStart` moves it on, so the
        //: reply to a start somebody stopped is recognised and let go.
        let startSeq = 0;

        const changed = () => { if (!state.closed) onChange(state); };

        function schedule(fn, ms) {
            timers.clearTimeout(pollTimer);
            pollTimer = timers.setTimeout(fn, ms);
        }

        function takeScan(body) {
            state.scan = body;
            if (body && body.overrides && !state.planning) {
                state.overrides = cloneOverrides(body.overrides);
                state.accepted = cloneOverrides(body.overrides);
            }
        }

        async function start(paths, node = null) {
            state.error = null;
            state.node = node || null;
            const ticket = ++startSeq;
            const body = node ? {paths, node} : {paths};
            const reply = await request("datasets/scan", {method: "POST", body}).catch(
                (error) => ({ok: false, body: {error: error.message}}));
            if (state.closed || ticket !== startSeq) {
                // Stopped, or closed, while the folder was being checked: the
                // scan that request started is nobody's, so it is stopped too.
                if (reply.ok && reply.body.token) {
                    request(`datasets/scan/${reply.body.token}/cancel`, {method: "POST"})
                        .catch(() => {});
                }
                return false;
            }
            if (!reply.ok) {
                state.error = reply.body.error || "The folder could not be read.";
                changed();
                return false;
            }
            state.token = reply.body.token;
            takeScan(reply.body);
            state.phase = reply.body.pending ? "scanning" : "plan";
            changed();
            if (reply.body.pending) poll();
            return true;
        }

        async function addFolder(path) {
            if (!state.token) return start([path], state.node);
            state.error = null;
            const body = {paths: [path], token: state.token};
            if (state.node) body.node = state.node;
            const reply = await request("datasets/scan", {method: "POST", body});
            if (state.closed) return false;
            if (!reply.ok) {
                state.error = reply.body.error || "That folder could not be added.";
                changed();
                return false;
            }
            takeScan(reply.body);
            state.phase = reply.body.pending ? "scanning" : "plan";
            changed();
            if (reply.body.pending) poll();
            return true;
        }

        function poll() {
            schedule(async () => {
                if (state.closed || polling) return;
                polling = true;
                const reply = await request(`datasets/scan/${state.token}`).catch(
                    () => ({ok: false, body: {}}));
                polling = false;
                if (state.closed) return;
                if (reply.ok) {
                    takeScan(reply.body);
                    if (!reply.body.pending) state.phase = "plan";
                    changed();
                    if (reply.body.pending) poll();
                } else if (reply.status === 404) {
                    state.error = reply.body.error || "The scan has expired.";
                    state.phase = "plan";
                    changed();
                } else {
                    poll();
                }
            }, SCAN_POLL_MS);
        }

        async function cancelScan() {
            if (!state.token) return;
            await request(`datasets/scan/${state.token}/cancel`, {method: "POST"});
            if (state.closed) return;
            const reply = await request(`datasets/scan/${state.token}`);
            if (state.closed) return;
            timers.clearTimeout(pollTimer);
            if (reply.ok) takeScan(reply.body);
            state.phase = "plan";
            changed();
        }

        /**
         * Change the overrides and send them. `edit(overrides)` mutates a copy;
         * nothing on screen moves until the server's plan comes back, because
         * one reassigned mask can change rows other than the one acted on.
         */
        function change(edit) {
            edit(state.overrides);
            state.planning = true;
            state.error = null;
            changed();
            timers.clearTimeout(planTimer);
            planTimer = timers.setTimeout(sendPlan, PLAN_DEBOUNCE_MS);
        }

        async function sendPlan() {
            const seq = ++state.planSeq;
            const sent = cloneOverrides(state.overrides);
            const reply = await request(`datasets/scan/${state.token}/plan`, {
                method: "POST", body: {overrides: sent}}).catch(
                (error) => ({ok: false, body: {error: error.message}}));
            // A later correction is already on its way: this answer is about
            // a plan nobody is looking at any more.
            if (state.closed || seq !== state.planSeq) return;
            state.planning = false;
            if (reply.ok) {
                state.accepted = sent;
                state.scan = reply.body;
            } else {
                state.overrides = cloneOverrides(state.accepted);
                state.error = reply.body.error || "That change could not be made.";
            }
            changed();
        }

        async function create({name, description = "", only = null, existOk = false}) {
            state.error = null;
            const body = {name, description, overrides: state.overrides, exist_ok: existOk};
            if (only) body.only = only;
            const reply = await request(`datasets/scan/${state.token}/create`, {
                method: "POST", body});
            if (state.closed) return reply;
            if (!reply.ok) {
                state.error = reply.body.error || "The dataset could not be created.";
                changed();
                return reply;
            }
            state.build = reply.body;
            state.phase = "building";
            changed();
            pollBuild();
            return reply;
        }

        function pollBuild() {
            schedule(async () => {
                if (state.closed) return;
                const reply = await request(`datasets/scan/${state.token}/create`).catch(
                    () => ({ok: false, body: {}}));
                if (state.closed) return;
                if (reply.ok) {
                    state.build = reply.body;
                    if (reply.body.status !== "pending") state.phase = "done";
                    changed();
                    if (reply.body.status === "pending") pollBuild();
                } else {
                    pollBuild();
                }
            }, BUILD_POLL_MS);
        }

        /** Pick up a build that was left running when its dialog closed. */
        async function resumeBuild(token) {
            state.token = token;
            const [scan, build] = await Promise.all([
                request(`datasets/scan/${token}`).catch(() => ({ok: false, body: {}})),
                request(`datasets/scan/${token}/create`).catch(() => ({ok: false, body: {}})),
            ]);
            if (state.closed) return;
            if (scan.ok) takeScan(scan.body);
            if (build.ok) state.build = build.body;
            state.phase = build.ok && build.body.status !== "pending" ? "done" : "building";
            changed();
            if (state.phase === "building") pollBuild();
        }

        function close() {
            const wasScanning = state.phase === "scanning";
            state.closed = true;
            timers.clearTimeout(pollTimer);
            timers.clearTimeout(planTimer);
            if (wasScanning && state.token) {
                request(`datasets/scan/${state.token}/cancel`, {method: "POST"}).catch(() => {});
            }
        }

        /** Let go of a start still waiting for its folder to be checked. */
        function stopStart() {
            startSeq += 1;
            state.error = null;
        }

        return {state, start, stopStart, addFolder, poll, cancelScan, change, sendPlan, create,
                resumeBuild, close};
    }

    // -- edits to the overrides ----------------------------------------------------

    function forgetPath(overrides, path) {
        overrides.assign = overrides.assign.filter((item) => item.path !== path);
        overrides.detach = overrides.detach.filter((item) => item !== path);
        overrides.exclude = overrides.exclude.filter((item) => item !== path);
    }

    const edits = {
        assign(overrides, paths, projects, slot) {
            paths.forEach((path) => {
                forgetPath(overrides, path);
                projects.forEach((project) => overrides.assign.push({path, project, slot}));
            });
        },
        exclude(overrides, paths) {
            paths.forEach((path) => {
                forgetPath(overrides, path);
                overrides.exclude.push(path);
            });
        },
        restore(overrides, path) {
            overrides.exclude = overrides.exclude.filter((item) => item !== path);
        },
        detach(overrides, path) {
            forgetPath(overrides, path);
            overrides.detach.push(path);
        },
        newProject(overrides, path) {
            forgetPath(overrides, path);
            overrides.new_project = overrides.new_project.filter((item) => item.image !== path);
            overrides.new_project.push({image: path});
        },
        rename(overrides, id, name) {
            if (name) overrides.rename[id] = name;
            else delete overrides.rename[id];
        },
        learn(overrides, path, project) {
            overrides.learn = overrides.learn.filter((item) => item.path !== path);
            overrides.learn.push({path, project});
        },
    };

    // -- what goes in the table ---------------------------------------------------

    function slotFor(kind) {
        return kind === "table" ? "table" : kind === "channels" ? "channels"
            : kind === "image" ? "image" : "mask";
    }

    function projectStatus(project, build) {
        if (build) {
            const found = (build.projects || []).find((p) => p.id === project.id);
            if (found) return {key: found.status, words: BUILD_STATUS[found.status] || [found.status, ""],
                               error: found.error};
        }
        return {key: project.status, words: STATUS[project.status] || [project.status, ""]};
    }

    function matchesFilter(project, filter) {
        const text = (filter.text || "").trim().toLowerCase();
        if (text) {
            const hay = [project.name, project.image?.rel, project.mask?.rel,
                         project.table?.rel].filter(Boolean).join(" ").toLowerCase();
            if (!hay.includes(text)) return false;
        }
        switch (filter.status) {
            case "matched": return project.status === "matched";
            case "attention": return ATTENTION.has(project.status);
            case "existing": return project.status === "existing";
            default: return true;
        }
    }

    /** File names that appear more than once in a column, shown with their folder. */
    function sharedNames(projects) {
        const seen = new Map();
        projects.forEach((project) => ["image", "mask", "table"].forEach((slot) => {
            const name = project[slot]?.name;
            if (name) seen.set(name, (seen.get(name) || 0) + 1);
        }));
        return new Set(Array.from(seen).filter(([, n]) => n > 1).map(([name]) => name));
    }

    function shortName(slot, shared) {
        if (!slot) return "";
        if (!slot.rel) return slot.label || "";
        if (!shared.has(slot.name)) return slot.name;
        const parts = slot.rel.split("/");
        return parts.slice(-2).join("/");
    }

    function cellHtml(project, slotName, shared) {
        const slot = project[slotName];
        const kind = slotName === "table" ? "table" : slotName;
        if (!slot) {
            return `<div class="plx-dsb-cell is-${kind} is-empty" data-slot="${slotName}"
                        role="cell"><span class="plx-dsb-none">—</span></div>`;
        }
        if (slot.bundled) {
            return `<div class="plx-dsb-cell is-${kind} is-bundled" data-slot="${slotName}"
                        role="cell" title="${esc(slot.reasons?.[0] || "")}">
                        <span class="plx-dsb-none">In the run</span></div>`;
        }
        const review = slot.confidence === "medium";
        const badges = [];
        if (slotName === "image" && project.kind === "bundle") {
            const layers = (project.image.layers || []).length;
            badges.push(`<span class="plx-dsb-badge" title="${esc(project.image.detail)}">${
                esc(project.image.detail ? project.image.detail.split(" · ")[0] : "Run")}${
                layers ? ` · ${layers}` : ""}</span>`);
        }
        if (slot.shared) {
            badges.push(`<span class="plx-dsb-badge" title="One file for several samples">shared</span>`);
        }
        const alternatives = (slot.alternatives || []).length;
        if (alternatives) {
            badges.push(`<span class="plx-dsb-badge" title="${esc(`${alternatives} other ${
                alternatives === 1 ? "file" : "files"} could be used instead`)}">+${alternatives}</span>`);
        }
        const name = slotName === "image" && project.kind === "bundle"
            ? (slot.label || slot.name) : shortName(slot, shared);
        return `<div class="plx-dsb-cell is-${kind}${review ? " is-review" : ""}${
                    slot.user ? " is-user" : ""}" data-slot="${slotName}" role="cell"
                    title="${esc(slot.path || "")}">
                    <span class="fas ${KIND_ICON[kind]} plx-dsb-kind" aria-hidden="true"></span>
                    <span class="plx-dsb-file">${esc(name)}</span>${
                    review ? `<span class="plx-dsb-q" title="Check this match">?</span>` : ""}${
                    badges.join("")}</div>`;
    }

    function confidenceHtml(slot) {
        if (!slot || slot.bundled) return "";
        if (slot.confidence === "user") return `<span class="plx-dsb-conf is-user">chosen by you</span>`;
        if (slot.confidence === "medium") {
            return `<span class="plx-dsb-conf is-medium">medium${
                slot.score != null ? ` · ${Number(slot.score).toFixed(2)}` : ""}</span>`;
        }
        if (slot.confidence === "high") return `<span class="plx-dsb-conf is-high">high</span>`;
        return "";
    }

    function detailLine(project, slotName, slot, phase) {
        const kind = slotName === "channels" ? "channels" : slotName;
        const editable = phase === "plan";
        const head = `<span class="plx-dsb-line-kind"><span class="fas ${KIND_ICON[kind]}"
            aria-hidden="true"></span>${esc(SLOT_LABEL[slotName])}</span>`;
        if (!slot) {
            const choose = editable && slotName !== "image"
                ? `<button class="plx-dsb-link" type="button" data-act="change"
                       data-slot="${slotName}">Choose…</button>` : "";
            return `<div class="plx-dsb-line is-empty">${head}
                <div class="plx-dsb-line-main"><span class="plx-dsb-none">${
                    slotName === "mask" ? "No segmentation mask" : slotName === "table"
                    ? "No cell data" : "None"}</span></div>
                <div class="plx-dsb-line-actions">${choose}</div></div>`;
        }
        if (slot.bundled) {
            return `<div class="plx-dsb-line">${head}<div class="plx-dsb-line-main">
                <span class="plx-dsb-none">${esc(slot.reasons?.[0] || "In the run")}</span>
                </div><div class="plx-dsb-line-actions"></div></div>`;
        }
        const reasons = (slot.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("");
        const subset = slot.subset
            ? `<p class="plx-dsb-subset">One file; this sample reads the rows where
                <code>${esc(slot.subset.column)} == ${esc(slot.subset.value)}</code>.</p>` : "";
        const names = slotName === "channels" && slot.names
            ? `<p class="plx-dsb-subset">${esc(slot.names.length)} names: ${
                esc(slot.names.slice(0, 8).join(", "))}${slot.names.length > 8 ? " …" : ""}</p>` : "";
        const alternatives = (slot.alternatives || []).map((alt) => `
            <div class="plx-dsb-alt">
                <code class="plx-dsb-path" title="${esc(alt.path)}">${esc(alt.rel || alt.path)}</code>
                ${alt.score != null ? `<span class="plx-dsb-alt-score">${Number(alt.score).toFixed(2)}</span>` : ""}
                ${editable ? `<button class="plx-dsb-link" type="button" data-act="use"
                    data-path="${esc(alt.path)}" data-slot="${slotName}">Use this</button>` : ""}
            </div>`).join("");
        const actions = !editable ? "" : slotName === "image"
            ? `<button class="plx-dsb-link" type="button" data-act="detach"
                   data-path="${esc(slot.path)}" title="Leave this image out of the plan">Remove</button>`
            : `${slot.confidence === "medium" ? `<button class="plx-dsb-link" type="button"
                   data-act="confirm" data-path="${esc(slot.path)}" data-slot="${slotName}">Confirm</button>` : ""}
               <button class="plx-dsb-link" type="button" data-act="change" data-slot="${slotName}">Change…</button>
               <button class="plx-dsb-link" type="button" data-act="detach"
                   data-path="${esc(slot.path)}" title="Move to Unassigned files">Remove</button>
               <button class="plx-dsb-link" type="button" data-act="exclude"
                   data-path="${esc(slot.path)}" title="Leave this file out entirely">Exclude</button>`;
        return `<div class="plx-dsb-line">${head}
            <div class="plx-dsb-line-main">
                <div class="plx-dsb-line-path"><code class="plx-dsb-path" title="${esc(slot.path)}">${
                    esc(slot.rel || slot.path)}</code>
                    ${confidenceHtml(slot)}</div>
                ${reasons ? `<ul class="plx-dsb-reasons">${reasons}</ul>` : ""}
                ${subset}${names}
                ${alternatives ? `<div class="plx-dsb-alts"><span class="plx-dsb-alts-head">Also possible</span>${alternatives}</div>` : ""}
            </div>
            <div class="plx-dsb-line-actions">${actions}</div></div>`;
    }

    function detailHtml(project, phase, status) {
        const slots = ["image", "mask", "table"];
        if (project.channels || project.kind !== "bundle") slots.push("channels");
        const lines = slots.map((slot) => {
            if (slot === "channels" && !project.channels) return "";
            return detailLine(project, slot, slot === "image" ? project.image : project[slot], phase);
        }).join("");
        const imageAlts = (project.image.alternatives || []).length
            ? `<div class="plx-dsb-alts"><span class="plx-dsb-alts-head">Other images with this name</span>${
                project.image.alternatives.map((alt) => `
                <div class="plx-dsb-alt"><code class="plx-dsb-path">${esc(alt.rel || alt.path)}</code>
                ${phase === "plan" ? `<button class="plx-dsb-link" type="button" data-act="use"
                    data-path="${esc(alt.path)}" data-slot="image">Use as the image</button>
                    <button class="plx-dsb-link" type="button" data-act="split"
                    data-path="${esc(alt.path)}">Make it a sample</button>` : ""}</div>`).join("")}</div>`
            : "";
        const warnings = [...(project.warnings || [])];
        if (status.error) warnings.unshift(status.error);
        const said = warnings.map((w) => `<li>${esc(w)}</li>`).join("");
        const identity = project.identity
            ? `<p class="plx-dsb-identity">Named from ${project.identity.source === "folder"
                ? "its folder" : project.identity.source === "bundle" ? "the run" : "the file name"}
                <code>${esc(project.identity.evidence)}</code>${
                project.existing ? ` · already imported as <strong>${esc(project.existing)}</strong>` : ""}</p>`
            : project.existing ? `<p class="plx-dsb-identity">Already imported as <strong>${
                esc(project.existing)}</strong></p>` : "";
        return `<div class="plx-dsb-detail" data-detail="${esc(project.id)}">
            ${identity}${lines}${imageAlts}
            ${said ? `<ul class="plx-dsb-warn-list">${said}</ul>` : ""}</div>`;
    }

    function rowHtml(project, view) {
        const status = projectStatus(project, view.build);
        const expanded = view.expanded.has(project.id);
        const warn = (project.warnings || []).length || status.error;
        const name = view.renaming === project.id
            ? `<input class="plx-dsb-rename-input" data-role="rename" value="${esc(project.name)}"
                   aria-label="Sample name" spellcheck="false" autocomplete="off">`
            : `<button class="plx-dsb-name-button" type="button" data-act="rename"
                   ${view.phase === "plan" ? "" : "disabled"}
                   title="${view.phase === "plan" ? "Rename" : esc(project.name)}">${esc(project.name)}</button>`;
        return `<div class="plx-dsb-row${expanded ? " is-expanded" : ""}${
                    view.busy.has(project.id) ? " is-busy" : ""}${
                    project.status === "existing" ? " is-existing" : ""}"
                    data-project="${esc(project.id)}" role="row">
                <div class="plx-dsb-cell" role="cell">
                    <button class="plx-dsb-expand" type="button" data-act="toggle"
                        aria-expanded="${expanded}" aria-label="${expanded ? "Hide" : "Show"} details for ${esc(project.name)}">
                        <span class="fas fa-chevron-right" aria-hidden="true"></span></button></div>
                <div class="plx-dsb-cell plx-dsb-name" role="cell">${name}${
                    warn ? `<span class="fas fa-triangle-exclamation plx-dsb-warn" title="${
                        esc((project.warnings || [])[0] || status.error || "")}" aria-hidden="true"></span>` : ""}</div>
                ${cellHtml(project, "image", view.shared)}
                ${cellHtml(project, "mask", view.shared)}
                ${cellHtml(project, "table", view.shared)}
                <div class="plx-dsb-cell" role="cell"><span class="plx-dsb-chip ${status.words[1]}"
                    title="${esc(status.error || "")}">${esc(status.words[0])}</span></div>
            </div>${expanded ? detailHtml(project, view.phase, status) : ""}`;
    }

    function tableHtml(plan, view) {
        const projects = (plan?.projects || []).filter((p) => matchesFilter(p, view.filter));
        view.shared = sharedNames(plan?.projects || []);
        const head = `<div class="plx-dsb-head" role="row">
            <span role="columnheader"></span><span role="columnheader">Sample</span>
            <span role="columnheader">Image</span><span role="columnheader">Segmentation</span>
            <span role="columnheader">Data</span><span role="columnheader">Status</span></div>`;
        if (!(plan?.projects || []).length) {
            const empty = view.phase === "scanning" ? "Looking for images…"
                : "No images were found in this folder.";
            return `${head}<p class="plx-dsb-empty">${esc(empty)}</p>`;
        }
        if (!projects.length) return `${head}<p class="plx-dsb-empty">No sample matches the filter.</p>`;
        return head + projects.map((project) => rowHtml(project, view)).join("");
    }

    function unassignedHtml(plan, view) {
        const items = plan?.unassigned || [];
        if (!items.length) return "";
        const kinds = {};
        items.forEach((item) => { kinds[item.kind] = (kinds[item.kind] || 0) + 1; });
        const sub = Object.entries(kinds).map(([kind, n]) => plural(n, KIND_WORD[kind] || KIND_WORD.other))
            .join(" · ");
        const editable = view.phase === "plan";
        const ticked = items.filter((item) => view.selected.has(item.path));
        const bulk = editable && ticked.length ? `<div class="plx-dsb-bulk">
            <span>${ticked.length} selected</span>
            <button class="plx-dsb-link" type="button" data-act="bulk-assign">Assign to…</button>
            <button class="plx-dsb-link" type="button" data-act="bulk-exclude">Exclude</button>
            <button class="plx-dsb-link" type="button" data-act="bulk-clear">Clear</button></div>` : "";
        const rows = items.map((item) => {
            const best = item.suggestions?.[0];
            const kind = item.kind;
            const folder = dirname(item.rel);
            const actions = !editable ? "" : kind === "image"
                ? `<button class="plx-import-role is-switch plx-dsb-assign-button" type="button"
                       data-act="new-project">New sample</button>`
                : kind === "other" ? ""
                : `${best ? `<button class="plx-dsb-suggest" type="button" data-act="accept"
                        data-project="${esc(best.project)}" title="${esc((best.reasons || []).join(" · "))}">
                        <span class="fas fa-check" aria-hidden="true"></span>Likely ${esc(best.name)}</button>` : ""}
                   <button class="plx-import-role is-switch plx-dsb-assign-button" type="button"
                       data-act="assign">Assign to…</button>`;
            return `<div class="plx-dsb-urow${view.selected.has(item.path) ? " is-selected" : ""}"
                        data-file="${esc(item.path)}" data-kind="${esc(kind)}"
                        ${editable && kind !== "other" ? 'draggable="true"' : ""}>
                    ${editable ? `<input type="checkbox" class="plx-dsb-tick" data-act="tick"
                        aria-label="Select ${esc(item.name)}" ${view.selected.has(item.path) ? "checked" : ""}>` : "<span></span>"}
                    <span class="fas ${KIND_ICON[kind] || KIND_ICON.other} plx-dsb-kind" aria-hidden="true"
                        title="${esc((KIND_WORD[kind] || KIND_WORD.other)[0])}"></span>
                    <div class="plx-dsb-uname" title="${esc(item.path)}">
                        <span class="plx-dsb-file">${esc(item.name)}</span>
                        ${folder ? `<span class="plx-dsb-dir">${esc(folder)}/</span>` : ""}
                        <span class="plx-dsb-why">${esc((item.reasons || []).join("; "))}</span>
                    </div>
                    <div class="plx-dsb-uactions">${actions}</div>
                    ${editable ? `<button class="plx-picker-close plx-dsb-more" type="button" data-act="more"
                        aria-label="More for ${esc(item.name)}" title="More">
                        <span class="fas fa-ellipsis" aria-hidden="true"></span></button>` : "<span></span>"}
                </div>`;
        }).join("");
        return `<div class="plx-dsb-section-head">
                <h3>Unassigned files <span class="plx-dsb-count">${items.length}</span></h3>
                <span class="plx-dsb-sub">${esc(sub)}</span>${bulk}</div>
            ${editable ? `<p class="plx-dsb-hint">Drag a file onto a sample, or choose where it goes.</p>` : ""}
            <div class="plx-dsb-ulist">${rows}</div>`;
    }

    function excludedHtml(plan, view) {
        const items = plan?.excluded || [];
        if (!items.length) return "";
        const files = items.filter((i) => !i.folder).length;
        const folders = items.length - files;
        const summary = [files ? plural(files, ["file", "files"]) : "",
                         folders ? plural(folders, ["folder", "folders"]) : ""].filter(Boolean).join(", ");
        const rows = items.map((item) => `<li><code class="plx-dsb-path">${esc(item.path)}</code>${
            item.folder ? ` <span class="plx-dsb-sub">${plural(item.count, ["file", "files"])}</span>` : ""}
            ${view.phase === "plan" ? `<button class="plx-dsb-link" type="button" data-act="restore"
                data-path="${esc(item.path)}">Restore</button>` : ""}</li>`).join("");
        return `<details class="plx-dsb-excluded" ${view.openExcluded ? "open" : ""}>
            <summary>Excluded · ${esc(summary)}</summary><ul>${rows}</ul></details>`;
    }

    function countsLine(scan, build) {
        const counts = scan?.counts || {};
        if (build && build.projects?.length) {
            const done = build.projects.filter((p) => ["created", "adopted"].includes(p.status)).length;
            const failed = build.projects.filter((p) => p.status === "failed").length;
            return [`${done} of ${samples(build.projects.length)} registered`,
                    failed ? `${failed} failed` : ""].filter(Boolean).join(" · ");
        }
        const parts = [samples(counts.projects || 0)];
        if (counts.with_mask) parts.push(`${counts.with_mask} with a mask`);
        if (counts.with_table) parts.push(`${counts.with_table} with data`);
        if (counts.attention) parts.push(`<strong class="is-attention">${counts.attention} to check</strong>`);
        if (counts.existing) parts.push(`${counts.existing} already imported`);
        if (counts.unassigned) parts.push(`${plural(counts.unassigned, ["file", "files"])} unassigned`);
        return parts.join(" · ");
    }

    // -- the dialog ---------------------------------------------------------------

    function part(role) {
        return dialog ? dialog.querySelector(`[data-role="${role}"]`) : null;
    }

    function portal(verb, node) {
        if (typeof PopoverPortal !== "undefined") {
            PopoverPortal[verb](node);
            return;
        }
        if (verb === "attach") document.body.appendChild(node);
        else node.remove();
    }

    function build() {
        const node = document.createElement("dialog");
        node.className = "plx-dialog plx-import plx-dsb";
        node.innerHTML = `
            <div class="plx-import-head">
                <div class="plx-import-head-text">
                    <h2 class="plx-dialog-title" data-role="title">New dataset</h2>
                    <p class="plx-import-subtitle" data-role="subtitle"></p>
                </div>
                <div class="plx-import-head-actions">
                    <button class="plx-picker-close" type="button" data-role="close"
                            title="Close" aria-label="Close">
                        <span class="fas fa-xmark" aria-hidden="true"></span>
                    </button>
                </div>
            </div>
            <div class="plx-import-body plx-dsb-body" data-role="body"></div>
            <p class="plx-import-status" data-role="status" role="status" aria-live="polite"></p>
            <div class="plx-import-foot" data-role="foot"></div>`;
        node.querySelector('[data-role="close"]').addEventListener("click", close);
        node.addEventListener("cancel", (event) => {
            if (ui?.picker) {
                event.preventDefault();
                closePicker();
                return;
            }
            close();
        });
        node.addEventListener("click", (event) => {
            if (ui?.picker && !ui.picker.contains(event.target)
                && !(ui.pickerAnchor && ui.pickerAnchor.contains(event.target))) {
                closePicker();
            }
        });
        portal("attach", node);
        return node;
    }

    function setStatus(message, isError, html) {
        const status = part("status");
        if (!status) return;
        if (html) status.innerHTML = html;
        else status.textContent = message || "";
        status.classList.toggle("is-error", Boolean(message || html) && Boolean(isError));
    }

    /**
     * Open the dialog.
     *
     * @param root - a folder to start scanning at once (a dropped folder).
     * @param name - the dataset's name, when the caller already has one.
     * @param onClose - called when the dialog goes away.
     * @param onCreated - `(dataset)` once a dataset exists; the Samples page
     *   lands in it. Without one, Open dataset goes there through the router.
     */
    function open(options) {
        options = options || {};
        if (dialog) close();
        ui = {
            name: options.name || "",
            nameTouched: Boolean(options.name),
            root: "",
            expanded: new Set(),
            selected: new Set(),
            busy: new Set(),
            filter: {text: "", status: "all"},
            renaming: null,
            picker: null,
            pickerAnchor: null,
            callout: null,
            openExcluded: false,
            lastSelected: null,
            dragging: null,
            signature: null,
            onClose: options.onClose || null,
            onCreated: options.onCreated || null,
            detachDrop: null,
            learnedSeen: 0,
            //: Which machine the folder is on: null for this computer, else a
            //: place from `PlexoraRemotes` -- `{id, label, node, files}`.
            where: whereFor(options),
        };
        controller = createController({onChange});
        dialog = build();
        if (typeof attachDropTarget === "function") {
            ui.detachDrop = attachDropTarget(dialog, {
                mode: "directory", count: "one", filter: "any",
                onDrop: (path) => {
                    // A dropped folder is on this computer, whatever the chip says.
                    if (controller.state.phase === "pick") {
                        if (ui.starting) return;
                        chooseWhere(null);
                        startScan(path);
                    } else if (controller.state.phase === "plan" && !ui.where) {
                        addFolder(path);
                    } else if (controller.state.phase === "plan") {
                        setStatus(`That folder is on this computer; this dataset is read `
                                  + `from ${ui.where.label}.`, true);
                    }
                },
            });
        }
        dialog.showModal();
        if (!options.root && lastBuild) {
            ui.name = lastBuild.name;
            void controller.resumeBuild(lastBuild.token);
            return dialog;
        }
        renderPick();
        if (options.root) startScan(options.root);
        else if (!ui.where) restoreWhere();
        return dialog;
    }

    function close() {
        if (!dialog) return;
        const after = ui?.onClose;
        ui?.unwatchPlaces?.();
        const state = controller?.state;
        if (state && state.phase === "building" && state.token) {
            lastBuild = {token: state.token, name: ui.name};
            watchInBackground(state.token, ui.name);
        } else if (state && state.phase === "done") {
            lastBuild = null;
        }
        controller?.close();
        closePicker();
        ui?.detachDrop?.();
        try {
            dialog.close();
        } catch (error) { /* already closed */ }
        portal("detach", dialog);
        dialog = null;
        controller = null;
        ui = null;
        if (after) after();
    }

    // -- pick -------------------------------------------------------------------------

    function renderPick() {
        dialog.dataset.phase = "pick";
        part("subtitle").textContent = "Choose the folder that holds the samples. "
            + "Plexora finds the images in it and matches each one's mask and data.";
        part("body").innerHTML = `
            <div class="plx-dsb-pick">
                <label class="plx-dsb-label" for="plx-dsb-name">Dataset name</label>
                <input id="plx-dsb-name" class="plx-prompt-input plx-dsb-name-input" data-role="name"
                       placeholder="Taken from the folder name" autocomplete="off" spellcheck="false">
                <label class="plx-dsb-label" for="plx-dsb-path">Folder</label>
                <div class="plx-import-pick" data-role="pick" data-face="browse">
                    <div class="plx-import-pick-row">
                        <div class="plx-import-field">${machineHtml()}
                            <input id="plx-dsb-path" class="plx-import-input" data-role="path" type="text"
                                   placeholder="Paste a folder path, or browse" spellcheck="false"
                                   autocomplete="off">
                        </div>
                        <button class="plx-import-browse" type="button" data-role="browse">
                            <span class="plx-import-browse-face is-browse">
                                <span class="fas fa-folder-open" aria-hidden="true"></span><span>Browse</span>
                            </span>
                            <span class="plx-import-browse-face is-add">
                                <span>Scan</span><span class="plx-import-browse-arrow" aria-hidden="true"><span class="fas fa-arrow-right"></span></span>
                            </span>
                        </button>
                    </div>
                </div>
                <div class="plx-dsb-progress plx-dsb-starting" data-role="starting" hidden>
                    <div class="plx-import-step-bar is-indeterminate"><span class="plx-import-step-fill"></span></div>
                    <span class="plx-dsb-progress-text" data-role="starting-text"></span>
                    <button class="plx-dsb-link" type="button" data-act="stop-start">Stop</button>
                </div>
                <p class="plx-dsb-hint" data-role="hint">Every image in the folder becomes a sample; masks and
                    tables beside it are matched by name and folder. Files stay where they are.</p>
            </div>`;
        const name = part("name");
        const path = part("path");
        const pick = part("pick");
        name.value = ui.name;
        name.addEventListener("input", () => {
            ui.name = name.value;
            ui.nameTouched = Boolean(name.value.trim());
            paintFoot();
        });
        name.addEventListener("keydown", (event) => {
            if (event.key === "Enter") { event.preventDefault(); primary(); }
        });
        path.addEventListener("input", () => {
            pick.dataset.face = path.value.trim() ? "add" : "browse";
            if (!ui.nameTouched) {
                ui.name = basename(path.value.trim());
                name.value = ui.name;
            }
            paintFoot();
        });
        path.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && path.value.trim()) {
                event.preventDefault();
                startScan(path.value.trim());
            }
        });
        part("browse").addEventListener("click", () => {
            if (ui.starting) return;
            if (path.value.trim()) startScan(path.value.trim());
            else browse((picked) => startScan(picked), "Choose the folder that holds the samples");
        });
        part("machine")?.addEventListener("click", (event) => openMachineMenu(event.currentTarget));
        part("starting").querySelector('[data-act="stop-start"]').addEventListener("click", stopStart);
        paintMachine();
        paintStarting();
        setTimeout(() => (ui?.name ? path : name)?.focus(), 0);
    }

    function browse(onPicked, title) {
        if (typeof browseForPath !== "function") {
            setStatus("The file browser is not available here; paste the folder's path.", true);
            return;
        }
        browseForPath({
            mode: "directory", filter: "any", start: ui?.root || "",
            // On a server, its own filesystem: a data node by name, a
            // files-only server by its `sftp://` base, whose listing hands
            // back addresses the scan takes as they are.
            node: browseNode(),
            anchorEl: part("browse") || null, title, primary: "Choose folder",
            onPicked: (picked) => onPicked(Array.isArray(picked) ? picked[0] : picked),
            onUnavailable: () => setStatus("The file browser could not be opened; "
                                           + "paste the folder's path instead.", true),
        });
    }

    /**
     * Scan a folder. Until the server has checked it -- on a server, a round
     * trip or two over a slow link -- the box shows the folder, the box,
     * Browse and the machine are locked so it cannot be picked twice, and a
     * bar runs with Stop beside it. The scan's own progress takes over once
     * it has started.
     */
    async function startScan(path) {
        if (!ui || !path || ui.starting) return;
        const address = addressOf(path);
        ui.root = address;
        if (!ui.nameTouched || !ui.name.trim()) {
            ui.name = basename(address);
            const name = part("name");
            if (name) name.value = ui.name;
        }
        const box = part("path");
        if (box) {
            box.value = shownPath(address);
            part("pick")?.setAttribute("data-face", "add");
        }
        ui.starting = true;
        setStatus("");
        paintStarting();
        const ok = await controller.start([address], ui.where?.node || null);
        if (!ui || !ui.starting) return;      // closed, or stopped
        ui.starting = false;
        if (!ok) {
            paintStarting();
            setStatus(controller.state.error, true);
            return;
        }
        rememberWhere();
    }

    function stopStart() {
        if (!ui?.starting) return;
        ui.starting = false;
        controller.stopStart();
        paintStarting();
        setStatus("Stopped. Choose the folder again, or another one.");
        part("path")?.focus();
    }

    /** The pick step while a folder is being checked: locked, with a bar. */
    function paintStarting() {
        if (!ui || controller?.state.phase !== "pick") return;
        const busy = Boolean(ui.starting);
        for (const role of ["path", "browse", "machine"]) {
            const control = part(role);
            if (control) control.disabled = busy;
        }
        part("pick")?.classList.toggle("is-busy", busy);
        const row = part("starting");
        if (row) {
            row.hidden = !busy;
            part("starting-text").textContent = ui.where
                ? `Reading the folder on ${ui.where.label}…` : "Reading the folder…";
        }
        const hint = part("hint");
        if (hint) hint.hidden = busy;
        // The end of a long path is the part anybody recognises. A task later:
        // disabling the box puts it back to its start.
        const box = part("path");
        if (box) setTimeout(() => { box.scrollLeft = box.scrollWidth; }, 0);
        paintFoot();
    }

    /** A path as the box shows it: on a files-only server, without its address. */
    function shownPath(address) {
        const base = ui?.where?.files?.base;
        if (base && address.startsWith(base)) {
            try {
                return decodeURIComponent(address.slice(base.length)) || "/";
            } catch (error) {
                return address.slice(base.length);
            }
        }
        return address;
    }

    async function addFolder(path) {
        if (!ui || !path || ui.adding) return;
        const address = addressOf(path);
        ui.adding = true;
        paintFoot();
        setStatus(`Adding ${basename(address)}…`);
        const ok = await controller.addFolder(address);
        if (!ui) return;
        ui.adding = false;
        paintFoot();
        if (ok) setStatus("");
        else setStatus(controller.state.error, true);
    }

    // -- which machine ----------------------------------------------------------------

    const Remotes = () => window.PlexoraRemotes;

    /** The machine `open()` was told about: a node, or an `sftp://` root. */
    function whereFor(options) {
        if (options.node) {
            return {id: options.node, label: options.node, node: options.node, files: null};
        }
        const root = String(options.root || "");
        const match = /^(sftp:\/\/[^/]+)/i.exec(root);
        if (!match) return null;
        const known = (Remotes()?.snapshot()?.places || []).find(
            (place) => place.files && place.files.base === match[1]);
        if (known) return placeOf(known);
        const host = match[1].replace(/^sftp:\/\/(?:[^@/]*@)?/i, "");
        return {id: match[1], label: host, node: null, files: {base: match[1], home: ""}};
    }

    function placeOf(place) {
        return {id: place.id, label: place.label || place.id, node: place.node || null,
                files: place.files || null};
    }

    /** What the scan is sent for a path typed or picked on the chosen machine. */
    function addressOf(path) {
        const text = String(path || "").trim();
        const files = ui?.where?.files;
        if (!files || /^[a-z][a-z0-9+.-]*:\/\//i.test(text)) return text;
        return window.PlexoraLocators
            ? window.PlexoraLocators.sftpUrl(files.base, text, files.home)
            : text;
    }

    function browseNode() {
        return ui?.where ? (ui.where.node || ui.where.files?.base || null) : null;
    }

    function machineHtml() {
        if (!Remotes()) return "";
        // Every icon is in the markup from the start; which one shows is CSS
        // over `.is-remote` (Font Awesome swaps each span for an svg once).
        return `<button class="plx-import-machine" type="button" data-role="machine"
                    aria-haspopup="menu" aria-expanded="false">
                <span class="plx-import-machine-local" aria-hidden="true"><span class="fas fa-laptop"></span></span>
                <span class="plx-import-machine-dot" aria-hidden="true"></span>
                <span class="plx-import-machine-label">Local</span>
                <span class="plx-import-machine-chevron" aria-hidden="true"><span class="fas fa-chevron-down"></span></span>
            </button>`;
    }

    function paintMachine() {
        const button = part("machine");
        if (!button) return;
        const name = ui.where ? ui.where.label : "Local";
        button.classList.toggle("is-remote", Boolean(ui.where));
        button.querySelector(".plx-import-machine-label").textContent = name;
        button.setAttribute("aria-label", `The folder is on ${ui.where ? name : "this computer"}. `
                                          + "Change machine");
        button.title = ui.where ? `Folders on ${name}` : "Folders on this computer";
        const path = part("path");
        if (path) {
            path.placeholder = ui.where?.files
                ? `Paste a folder path on ${name}, or browse`
                : "Paste a folder path, or browse";
        }
    }

    /**
     * Which machine, as the home page and Import Sample ask it: Local, then
     * every saved server with its state, then Add and Manage. Drawn in the
     * dialog (the body scrolls and would clip it) as the assign picker is, so
     * the same outside click and Escape close it.
     */
    function openMachineMenu(anchor) {
        if (!Remotes()) return;
        if (ui.picker && ui.pickerAnchor === anchor) {
            closePicker();
            return;
        }
        closePicker();
        const menu = document.createElement("div");
        menu.className = "plx-import-menu plx-dsb-machine-menu";
        menu.setAttribute("role", "menu");
        menu.setAttribute("aria-label", "Where the folder is");
        dialog.appendChild(menu);
        ui.picker = menu;
        ui.pickerAnchor = anchor;
        anchor.setAttribute("aria-expanded", "true");
        let key = "";
        const draw = (places) => {
            if (ui?.picker !== menu) return;
            const current = ui.where ? ui.where.id : null;
            const next = JSON.stringify([current, places.map((place) =>
                [place.id, place.node, Boolean(place.files), place.state, place.phase])]);
            // A poll that changes nothing must not rebuild the list under the focus.
            if (next === key) return;
            key = next;
            menu.replaceChildren(...machineRows(places, current));
        };
        draw(Remotes().snapshot()?.places || []);
        // While it is open, a server that comes up shows here.
        const unwatch = Remotes().subscribe((snapshot) => draw(snapshot.places || []),
                                            {active: true});
        const remove = menu.remove.bind(menu);
        menu.remove = () => {
            unwatch?.();
            anchor.setAttribute("aria-expanded", "false");
            remove();
        };
        menu.addEventListener("keydown", (event) => {
            const items = Array.from(menu.querySelectorAll(".plx-import-menu-item:not(:disabled)"));
            const at = items.indexOf(document.activeElement);
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                const step = event.key === "ArrowDown" ? 1 : -1;
                items[(at + step + items.length) % items.length]?.focus();
            } else if (event.key === "Tab") {
                closePicker();
            }
        });
        const box = anchor.getBoundingClientRect();
        menu.style.left = `${Math.round(box.left - 2)}px`;
        menu.style.top = `${Math.round(box.bottom + 10)}px`;
        menu.querySelector(".plx-import-menu-item")?.focus();
    }

    function machineRows(places, current) {
        const node = (tag, className, text) => {
            const made = document.createElement(tag);
            if (className) made.className = className;
            if (text != null) made.textContent = text;
            return made;
        };
        const item = ({icon, dot, name, sub, checked, onPick, href, disabled, add}) => {
            const row = node(href ? "a" : "button", "plx-import-menu-item" + (add ? " is-add" : ""));
            row.setAttribute("role", checked === undefined ? "menuitem" : "menuitemradio");
            if (checked !== undefined) row.setAttribute("aria-checked", checked ? "true" : "false");
            if (href) {
                row.href = href;
            } else {
                row.type = "button";
                row.disabled = Boolean(disabled);
                row.addEventListener("click", onPick);
            }
            const lead = node("span", dot || "plx-import-menu-icon");
            lead.setAttribute("aria-hidden", "true");
            if (!dot) lead.appendChild(node("span", `fas fa-${icon}`));
            row.append(lead, node("span", "plx-import-menu-name", name));
            if (sub) row.append(node("span", "plx-import-menu-sub", sub));
            if (checked) {
                const tick = node("span", "plx-import-menu-check");
                tick.setAttribute("aria-hidden", "true");
                tick.appendChild(node("span", "fas fa-check"));
                row.append(tick);
            }
            return row;
        };
        const sep = () => {
            const line = node("div", "plx-import-menu-sep");
            line.setAttribute("role", "separator");
            return line;
        };
        const rows = [item({icon: "laptop", name: "Local", sub: "This computer", checked: !current,
                            onPick: () => { closePicker(); chooseWhere(null); }}),
                      sep(), node("div", "plx-import-menu-label", "Remote")];
        if (!places.length) rows.push(node("div", "plx-import-menu-empty", "No servers yet."));
        places.forEach((place) => {
            const up = place.kind === "server" || Boolean(place.node) || Boolean(place.files);
            const waiting = !up && Remotes().isOpening?.(place.state);
            const row = item({
                dot: "plx-import-menu-dot" + (up ? "" : (waiting ? " is-waiting" : " is-off")),
                name: place.label,
                sub: up ? "Connected" : (waiting ? (place.phase || "Connecting…") : "Not connected"),
                checked: place.id === current,
                disabled: waiting,
                onPick: () => {
                    closePicker();
                    if (up) chooseWhere(placeOf(place));
                    else connectWhere(place);
                },
            });
            row.dataset.place = place.id;
            rows.push(row);
        });
        rows.push(sep(), item({icon: "plus", name: "Add a server…", add: true,
                               onPick: () => { closePicker(); connectWhere(null); }}));
        rows.push(item({icon: "gear", name: "Manage servers", sub: "Settings",
                        href: (typeof plexoraUrl === "function" ? plexoraUrl("settings")
                               : "/settings") + "#remotes"}));
        return rows;
    }

    function chooseWhere(where) {
        if (!ui) return;
        if ((ui.where?.id || null) !== (where?.id || null)) {
            // A path means nothing on another machine: the box starts empty.
            const path = part("path");
            if (path) path.value = "";
            part("pick")?.setAttribute("data-face", "browse");
        }
        ui.where = where;
        paintMachine();
        part("path")?.focus();
    }

    /** Connect a saved server that is down, or add one, and use it once it is up. */
    async function connectWhere(place) {
        if (!window.PlexoraConnectionModal) return;
        const outcome = await window.PlexoraConnectionModal.open(place
            ? {name: place.id, kind: "node",
               intent: place.files_only
                   ? "Opening an SFTP connection to this machine, so the folders on "
                     + "it can be read. Nothing is started there."
                   : "Opening a data node on this machine, so the folders on it "
                     + "can be read."}
            : {kind: "node",
               intent: "Add the machine the folder is on. Plexora will handle the "
                       + "SSH connection."});
        if (!ui || !outcome || !outcome.connected) return;
        chooseWhere({id: outcome.name, label: outcome.label || place?.label || outcome.name,
                     node: outcome.node || null, files: outcome.files || null});
    }

    function rememberWhere() {
        try {
            if (ui?.where) {
                localStorage.setItem(WHERE_KEY, JSON.stringify({
                    id: ui.where.id, kind: "remote", label: ui.where.label,
                    node: ui.where.node || null}));
            } else {
                localStorage.removeItem(WHERE_KEY);
            }
        } catch (error) { /* storage refused: nothing to remember it in */ }
    }

    /** Open on the machine the last pick was read from, once it is known to be up. */
    function restoreWhere() {
        let wanted = null;
        try {
            wanted = JSON.parse(localStorage.getItem(WHERE_KEY) || "null");
        } catch (error) { wanted = null; }
        if (!wanted || !wanted.id || !Remotes()) return;
        const owner = ui;
        const apply = (snapshot) => {
            if (ui !== owner || ui.where || part("path")?.value.trim()) return;
            const place = (snapshot.places || []).find((entry) => entry.id === wanted.id);
            if (!place || !(place.node || place.files)) return;
            ui.where = placeOf(place);
            paintMachine();
        };
        const now = Remotes().snapshot();
        if (now && now.loaded) {
            apply(now);
            return;
        }
        let done = false;
        let stop = null;
        stop = Remotes().subscribe((snapshot) => {
            if (done) return;
            done = true;
            apply(snapshot);
            stop?.();
        });
        if (done) stop?.();
        else ui.unwatchPlaces = () => { done = true; stop?.(); };
    }

    async function createEmpty() {
        const name = ui.name.trim();
        if (!name) return;
        setStatus("Creating the dataset…");
        const reply = await defaultFetch("datasets", {method: "POST", body: {name, projects: []}});
        if (!ui) return;
        if (!reply.ok || reply.body.success === false) {
            setStatus(reply.body.error || "The dataset could not be created.", true);
            return;
        }
        const created = reply.body.dataset;
        const onCreated = ui.onCreated;
        close();
        window.PlexoraOpenProject?.refresh?.();
        if (onCreated && created) onCreated(created);
    }

    // -- scanning, plan, building -----------------------------------------------------

    function renderPlanShell() {
        part("body").innerHTML = `
            <div class="plx-dsb-top">
                <label class="plx-dsb-namefield">
                    <span class="plx-dsb-label">Dataset name</span>
                    <input class="plx-prompt-input plx-dsb-name-input" data-role="name"
                           autocomplete="off" spellcheck="false" placeholder="Name the dataset">
                </label>
                <div class="plx-dsb-roots" data-role="roots"></div>
            </div>
            <div class="plx-dsb-progress" data-role="progress" hidden>
                <div class="plx-import-step-bar"><span class="plx-import-step-fill" data-role="fill"></span></div>
                <span class="plx-dsb-progress-text" data-role="progress-text"></span>
                <button class="plx-dsb-link" type="button" data-act="cancel-scan">Cancel scan</button>
            </div>
            <p class="plx-dsb-counts" data-role="counts"></p>
            <div class="plx-dsb-warnings" data-role="warnings"></div>
            <div class="plx-dsb-callout" data-role="callout" hidden></div>
            <div class="plx-dsb-tools">
                <span class="plx-dsb-filter-wrap">
                    <span class="fas fa-magnifying-glass" aria-hidden="true"></span>
                    <input class="plx-dsb-filter" data-role="filter" type="search"
                           placeholder="Filter by name or path" aria-label="Filter samples">
                </span>
                <div class="plx-dsb-chips" data-role="chips" role="group" aria-label="Show"></div>
                <button class="plx-dsb-link plx-dsb-confirm-all" type="button" data-act="confirm-all"
                        data-role="confirm-all" hidden></button>
            </div>
            <div class="plx-dsb-table" data-role="table" role="table" aria-label="Samples"></div>
            <p class="plx-dsb-shown" data-role="shown"></p>
            <section class="plx-dsb-unassigned" data-role="unassigned"></section>
            <div data-role="excluded"></div>`;
        const name = part("name");
        name.value = ui.name;
        name.addEventListener("input", () => {
            ui.name = name.value;
            ui.nameTouched = true;
            paintFoot();
        });
        name.addEventListener("keydown", (event) => {
            if (event.key === "Enter") { event.preventDefault(); primary(); }
        });
        const filter = part("filter");
        filter.addEventListener("input", () => {
            ui.filter.text = filter.value;
            paintTable(true);
        });
        wireBody(part("body"));
    }

    function paint(state) {
        if (!dialog || !ui) return;
        if (state.phase === "pick") return paintFoot();
        if (!part("table")) renderPlanShell();
        dialog.dataset.phase = state.phase;
        const scan = state.scan || {};
        const subtitle = part("subtitle");
        if (state.phase === "scanning") {
            subtitle.textContent = "Reading the folder. The table fills in as files are found.";
        } else if (state.phase === "plan") {
            subtitle.textContent = scan.status === "cancelled"
                ? "Stopped before the end: this is what was found so far."
                : (scan.counts?.attention || scan.counts?.unassigned)
                    ? "Check the rows marked for review and place the unassigned files, then create."
                    : scan.counts?.projects && scan.counts.image_only === scan.counts.projects
                    ? "Each image becomes a sample. No masks or tables were found beside them."
                    : "Everything was matched. Check the table, then create.";
        } else if (state.phase === "building") {
            subtitle.textContent = "Creating the dataset. You can close this; it keeps going.";
        } else if (state.phase === "done") {
            const failed = state.build?.counts?.failed || 0;
            subtitle.textContent = failed
                ? `Created, except ${samples(failed)} that could not be registered.`
                : "The dataset is ready.";
        }
        paintRoots(scan);
        paintProgress(state);
        part("counts").innerHTML = countsLine(scan, state.phase === "building" || state.phase === "done"
            ? state.build : null);
        paintWarnings(scan);
        paintCallout(scan);
        paintChips(scan);
        paintConfirmAll(scan);
        paintTable(false);
        paintFoot();
        if (state.error) setStatus(state.error, true);
        else if (state.planning) setStatus("Updating…");
        else if (!part("status").querySelector("button")) setStatus("");
        noticeLearned(scan);
    }

    function paintRoots(scan) {
        const roots = scan.roots || (ui.root ? [ui.root] : []);
        const where = ui.where ? ` on ${ui.where.label}` : "";
        const icon = ui.where ? "fa-server" : "fa-folder";
        part("roots").innerHTML = roots.map((root) => `<span class="plx-dsb-root" title="${esc(root + where)}">
            <span class="fas ${icon}" aria-hidden="true"></span><code>${esc(tail(root))}</code></span>`).join("");
    }

    function paintProgress(state) {
        const box = part("progress");
        const scanning = state.phase === "scanning";
        const building = state.phase === "building";
        box.hidden = !(scanning || building);
        if (box.hidden) return;
        const progress = scanning ? (state.scan?.progress || {}) : {
            percent: state.build?.progress || 0, stage_label: state.build?.stage_label};
        part("fill").style.width = `${Math.max(3, Math.min(100, progress.percent || 0))}%`;
        part("progress-text").textContent = [progress.stage_label, scanning ? progress.message : ""]
            .filter(Boolean).join(" · ");
        box.querySelector('[data-act="cancel-scan"]').hidden = !scanning;
    }

    function paintWarnings(scan) {
        const warnings = scan.warnings || [];
        const box = part("warnings");
        if (!warnings.length) { box.innerHTML = ""; return; }
        const shown = ui.allWarnings ? warnings : warnings.slice(0, 2);
        box.innerHTML = shown.map((w) => `<div class="plx-dsb-warning">
            <span class="fas fa-triangle-exclamation" aria-hidden="true"></span><span>${esc(w)}</span></div>`).join("")
            + (warnings.length > 2 ? `<button class="plx-dsb-link" type="button" data-act="all-warnings">${
                ui.allWarnings ? "Fewer" : `${warnings.length - 2} more`}</button>` : "");
    }

    function paintChips(scan) {
        const counts = scan.counts || {};
        const n = {
            all: counts.projects || 0,
            matched: counts.matched || 0,
            attention: (counts.attention || 0) + (counts.unavailable || 0),
            existing: counts.existing || 0,
        };
        if (ui.filter.status !== "all" && !n[ui.filter.status]) ui.filter.status = "all";
        part("chips").innerHTML = FILTERS.filter(([key]) => key === "all" || n[key])
            .map(([key, label]) => `<button type="button" class="plx-import-choice plx-dsb-chipfilter${
                ui.filter.status === key ? " is-chosen" : ""}" data-act="filter" data-filter="${key}"
                aria-pressed="${ui.filter.status === key}">${esc(label)} <span>${n[key]}</span></button>`).join("");
    }

    /** Every filled-in match the plan is not sure of: `[{path, project, slot}]`. */
    function reviewable(scan) {
        const found = [];
        (scan?.projects || []).forEach((project) => ["mask", "table"].forEach((slot) => {
            const held = project[slot];
            if (held && held.path && held.confidence === "medium") {
                found.push({path: held.path, project: project.id, slot});
            }
        }));
        return found;
    }

    function paintConfirmAll(scan) {
        const button = part("confirm-all");
        if (!button) return;
        const found = controller.state.phase === "plan" ? reviewable(scan) : [];
        button.hidden = !found.length;
        button.textContent = found.length === 1 ? "Confirm the suggested match"
            : `Confirm ${found.length} suggested matches`;
    }

    function viewModel() {
        const state = controller.state;
        return {
            phase: state.phase,
            build: state.phase === "building" || state.phase === "done" ? state.build : null,
            expanded: ui.expanded,
            selected: ui.selected,
            busy: ui.busy,
            filter: ui.filter,
            renaming: ui.renaming,
            openExcluded: ui.openExcluded,
            shared: new Set(),
        };
    }

    /**
     * The table and the lists under it. Rebuilt only when what they show has
     * changed, so a poll that brought nothing new leaves the scroll position,
     * the hover and an open rename alone.
     */
    function paintTable(force) {
        const state = controller.state;
        const plan = state.scan;
        const view = viewModel();
        const signature = JSON.stringify([
            plan?.projects, plan?.unassigned, plan?.excluded, view.phase, view.build?.projects,
            Array.from(ui.expanded), Array.from(ui.selected), Array.from(ui.busy),
            ui.filter, ui.renaming, state.planning]);
        if (!force && signature === ui.signature) return;
        ui.signature = signature;
        part("table").innerHTML = tableHtml(plan, view);
        part("table").classList.toggle("is-planning", Boolean(state.planning));
        const total = (plan?.projects || []).length;
        const shown = (plan?.projects || []).filter((p) => matchesFilter(p, ui.filter)).length;
        part("shown").textContent = shown !== total ? `Showing ${shown} of ${total}` : "";
        part("unassigned").innerHTML = unassignedHtml(plan, view);
        part("excluded").innerHTML = excludedHtml(plan, view);
        if (ui.renaming) {
            const input = part("rename");
            if (input) { input.focus(); input.select(); }
        }
    }

    function creatable(plan) {
        return (plan?.projects || []).filter((p) => p.status !== "unavailable");
    }

    function paintFoot() {
        const foot = part("foot");
        if (!foot || !controller) return;
        const state = controller.state;
        const plan = state.scan;
        let left = "";
        let right = "";
        if (state.phase === "pick" && ui.starting) {
            right = `<button class="plx-button" type="button" data-act="close">Cancel</button>
                <button class="plx-button plx-button-primary" type="button" disabled>Reading…</button>`;
        } else if (state.phase === "pick") {
            const name = ui.name.trim();
            const path = part("path")?.value.trim();
            const label = path ? "Scan folder" : name ? "Create empty dataset" : "Create";
            const reason = !name && !path ? "Name the dataset or choose a folder" : "";
            right = `<span class="plx-import-blocked">${esc(reason)}</span>
                <button class="plx-button" type="button" data-act="close">Cancel</button>
                <button class="plx-button plx-button-primary" type="button" data-act="primary"
                    ${!name && !path ? "disabled" : ""}>${esc(label)}</button>`;
        } else if (state.phase === "scanning") {
            right = `<button class="plx-button" type="button" data-act="close">Close</button>
                <button class="plx-button plx-button-primary" type="button" disabled>Scanning…</button>`;
        } else if (state.phase === "plan") {
            const projects = creatable(plan);
            const unavailable = (plan?.counts?.unavailable || 0);
            const existing = projects.filter((p) => p.existing).length;
            const fresh = projects.length - existing;
            const label = !projects.length ? "Nothing to create"
                : unavailable ? `Leave out ${unavailable} and create`
                : fresh ? `Create ${samples(projects.length)}` : `Add ${samples(projects.length)}`;
            const reason = !ui.name.trim() ? "Name the dataset"
                : state.planning ? "Updating…" : "";
            left = `<button class="plx-button" type="button" data-act="add-folder"
                    ${ui.adding ? "disabled" : ""}>
                    <span class="fas fa-folder-plus" aria-hidden="true"></span> Add folder…</button>
                <button class="plx-button" type="button" data-act="save-spec"
                    title="Save the plan as a file that plexora dataset create --from reads">
                    <span class="fas fa-file-export" aria-hidden="true"></span> Save spec…</button>`;
            right = `<span class="plx-import-blocked">${esc(reason)}</span>
                <button class="plx-button" type="button" data-act="close">Close</button>
                <button class="plx-button plx-button-primary" type="button" data-act="primary"
                    ${!projects.length || !ui.name.trim() || state.planning ? "disabled" : ""}>${esc(label)}</button>`;
        } else if (state.phase === "building") {
            right = `<span class="plx-import-blocked">Keeps going if you close this</span>
                <button class="plx-button" type="button" data-act="close">Close</button>
                <button class="plx-button plx-button-primary" type="button" disabled>Creating…</button>`;
        } else if (state.phase === "done") {
            const failed = (state.build?.projects || []).filter((p) => p.status === "failed");
            left = failed.length ? `<button class="plx-button" type="button" data-act="retry">
                Retry ${failed.length} failed</button>` : "";
            right = `<button class="plx-button" type="button" data-act="close">Close</button>
                <button class="plx-button plx-button-primary" type="button" data-act="open-dataset"
                    ${state.build?.dataset ? "" : "disabled"}>Open dataset</button>`;
        }
        foot.innerHTML = `<div class="plx-dsb-foot-left">${left}</div>${right}`;
        foot.onclick = (event) => {
            const act = event.target.closest("[data-act]")?.dataset.act;
            if (!act) return;
            if (act === "close") close();
            else if (act === "primary") primary();
            else if (act === "add-folder") browse((picked) => addFolder(picked), "Add a folder to this dataset");
            else if (act === "save-spec") saveSpec();
            else if (act === "retry") retryFailed();
            else if (act === "open-dataset") openDataset();
        };
    }

    async function primary() {
        const state = controller?.state;
        if (!state) return;
        if (state.phase === "pick") {
            if (ui.starting) return;
            const path = part("path")?.value.trim();
            if (path) return startScan(path);
            if (ui.name.trim()) return createEmpty();
            return;
        }
        if (state.phase !== "plan" || state.planning) return;
        const name = ui.name.trim();
        if (!name) {
            part("name")?.focus();
            return;
        }
        await go({name});
    }

    async function go({name, only = null, existOk = false}) {
        setStatus("Starting…");
        const reply = await controller.create({name, only, existOk});
        if (!ui) return;
        if (reply.ok) {
            ui.busy.clear();
            setStatus("");
            return;
        }
        if (reply.status === 409 && reply.body.taken) {
            setStatus(null, true, `${esc(reply.body.error)} <button class="plx-dsb-link" type="button"
                data-act="add-to-existing">Add these samples to it</button>`);
            part("status").querySelector("button").addEventListener("click", () => {
                go({name, only, existOk: true});
            });
            return;
        }
        setStatus(reply.body.error || "The dataset could not be created.", true);
    }

    function retryFailed() {
        const build = controller.state.build;
        const failed = (build?.projects || []).filter((p) => p.status === "failed").map((p) => p.id);
        if (!failed.length) return;
        go({name: build.name || ui.name.trim(), only: failed, existOk: true});
    }

    function openDataset() {
        const dataset = controller.state.build?.dataset;
        if (!dataset) return;
        const onCreated = ui.onCreated;
        lastBuild = null;
        close();
        if (onCreated) onCreated(dataset);
        else if (window.PlexoraOpenProject?.open) window.PlexoraOpenProject.open(dataset.id);
        else {
            const href = `/open_project?dataset=${encodeURIComponent(dataset.id)}`;
            if (window.PlexoraRouter) window.PlexoraRouter.go(href);
            else window.location.href = plexoraUrl(href);
        }
    }

    async function saveSpec() {
        const state = controller.state;
        const name = ui.name.trim() || "Dataset";
        let response;
        try {
            response = await fetch(plexoraUrl(`datasets/scan/${state.token}/spec?name=${
                encodeURIComponent(name)}`));
        } catch (error) {
            setStatus("The spec could not be saved.", true);
            return;
        }
        if (!response.ok) {
            setStatus((await readJson(response)).error || "The spec could not be saved.", true);
            return;
        }
        const blob = new Blob([await response.text()], {type: "application/json"});
        const filename = `${name.replace(/[^\w.-]+/g, "_")}.dataset.json`;
        if (window.PlexoraDesktop?.saveBlob) {
            window.PlexoraDesktop.saveBlob(blob, filename).catch(() => {
                setStatus("The spec could not be saved.", true);
            });
            return;
        }
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(link.href), 1000);
        setStatus(`Saved ${filename}. Create from it with plexora dataset create "${name}" --from ${filename}`);
    }

    // -- acting on rows ----------------------------------------------------------------

    function projectById(id) {
        return (controller.state.scan?.projects || []).find((p) => p.id === id) || null;
    }

    /** A file filled into some sample's slot, shaped like an unassigned one. */
    function filledItem(path) {
        for (const project of controller.state.scan?.projects || []) {
            for (const slot of ["mask", "table"]) {
                const held = project[slot];
                if (held && held.path === path) return {...held, kind: slot};
            }
        }
        return null;
    }

    function unassignedItem(path) {
        return (controller.state.scan?.unassigned || []).find((u) => u.path === path) || null;
    }

    function edit(fn, touched) {
        (touched || []).forEach((id) => ui.busy.add(id));
        controller.change(fn);
    }

    function assign(paths, projects, slot) {
        if (!paths.length || !projects.length) return;
        edit((o) => edits.assign(o, paths, projects, slot), projects);
        paths.forEach((path) => ui.selected.delete(path));
        if (paths.length === 1 && projects.length === 1 && slot !== "image") {
            offerRule(paths[0], projects[0]);
        }
    }

    /**
     * After one file is placed by hand: if its name spells the sample's and
     * other unplaced files are named the same way, offer to place them too.
     * The server works out and applies the rule; this only asks.
     */
    function offerRule(path, projectId) {
        const project = projectById(projectId);
        const item = unassignedItem(path) || filledItem(path);
        if (!project || !item) return;
        const name = project.name;
        const index = item.rel.toLowerCase().indexOf(name.toLowerCase());
        if (index < 0 || name.length < 2) return;
        const pattern = item.rel.slice(0, index) + "{id}" + item.rel.slice(index + name.length);
        const [before, after] = pattern.split("{id}");
        const open = [...(controller.state.scan?.unassigned || []),
                      ...reviewable(controller.state.scan).map((m) => filledItem(m.path))];
        const others = open.filter((u) => u && u.path !== path
            && u.rel.toLowerCase().startsWith(before.toLowerCase())
            && u.rel.toLowerCase().endsWith(after.toLowerCase())
            && u.rel.length > before.length + after.length);
        if (!others.length) return;
        ui.callout = {path, project: projectId, pattern, count: others.length,
                      kind: item.kind};
        paintCallout(controller.state.scan || {});
    }

    function paintCallout() {
        const box = part("callout");
        if (!box) return;
        const offer = ui.callout;
        box.hidden = !offer || controller.state.phase !== "plan";
        if (box.hidden) { box.innerHTML = ""; return; }
        const word = offer.kind === "table" ? "data" : "masks";
        box.innerHTML = `<span class="fas fa-wand-magic-sparkles" aria-hidden="true"></span>
            <span>Match <code>${esc(offer.pattern)}</code> as ${word} for every sample?
            ${esc(plural(offer.count, ["other file", "other files"]))} follow this pattern.</span>
            <button class="plx-button plx-button-primary" type="button" data-act="learn">Apply</button>
            <button class="plx-dsb-link" type="button" data-act="no-learn">Not now</button>`;
    }

    function noticeLearned(scan) {
        const learned = scan.conventions?.learned || [];
        if (learned.length <= ui.learnedSeen) return;
        const latest = learned[learned.length - 1];
        ui.learnedSeen = learned.length;
        window.PlexoraToast?.show({
            title: `Applied to ${plural(latest.applied_to, ["file", "files"])}`,
            note: `Files named like ${latest.pattern} are matched the same way.`,
            timeout: 8000,
        });
    }

    function wireBody(body) {
        body.addEventListener("click", (event) => {
            const target = event.target.closest("[data-act]");
            if (!target || !body.contains(target)) return;
            const act = target.dataset.act;
            const row = target.closest("[data-project]");
            const detail = target.closest("[data-detail]");
            const projectId = row?.dataset.project || detail?.dataset.detail || null;
            const urow = target.closest("[data-file]");
            const path = target.dataset.path || urow?.dataset.file || null;
            switch (act) {
                case "toggle":
                    if (ui.expanded.has(projectId)) ui.expanded.delete(projectId);
                    else ui.expanded.add(projectId);
                    paintTable(true);
                    break;
                case "rename":
                    if (controller.state.phase !== "plan") break;
                    ui.renaming = projectId;
                    paintTable(true);
                    break;
                case "filter":
                    ui.filter.status = target.dataset.filter;
                    paintChips(controller.state.scan || {});
                    paintTable(true);
                    break;
                case "cancel-scan":
                    controller.cancelScan();
                    break;
                case "all-warnings":
                    ui.allWarnings = !ui.allWarnings;
                    paintWarnings(controller.state.scan || {});
                    break;
                case "change":
                    openPicker(target, {mode: "file", projectId, slot: target.dataset.slot});
                    break;
                case "use":
                    assign([path], [projectId], target.dataset.slot);
                    break;
                case "confirm":
                    assign([path], [projectId], target.dataset.slot);
                    break;
                case "confirm-all": {
                    const found = reviewable(controller.state.scan);
                    edit((o) => found.forEach((m) => edits.assign(o, [m.path], [m.project], m.slot)),
                         found.map((m) => m.project));
                    break;
                }
                case "split":
                    edit((o) => edits.newProject(o, path), [projectId]);
                    break;
                case "detach":
                    edit((o) => edits.detach(o, path), [projectId]);
                    break;
                case "exclude":
                    edit((o) => edits.exclude(o, [path]), projectId ? [projectId] : []);
                    ui.selected.delete(path);
                    break;
                case "restore":
                    ui.openExcluded = true;
                    edit((o) => edits.restore(o, path));
                    break;
                case "accept": {
                    const item = unassignedItem(path);
                    assign([path], [target.dataset.project], slotFor(item?.kind));
                    break;
                }
                case "assign":
                    openPicker(target, {mode: "project", paths: [path],
                                        kind: unassignedItem(path)?.kind});
                    break;
                case "new-project":
                    edit((o) => edits.newProject(o, path));
                    break;
                case "more":
                    openMore(target, unassignedItem(path));
                    break;
                case "tick":
                    break;
                case "bulk-assign": {
                    const paths = Array.from(ui.selected);
                    const kinds = new Set(paths.map((p) => unassignedItem(p)?.kind));
                    openPicker(target, {mode: "project", paths,
                                        kind: kinds.size === 1 ? Array.from(kinds)[0] : "mask"});
                    break;
                }
                case "bulk-exclude":
                    edit((o) => edits.exclude(o, Array.from(ui.selected)));
                    ui.selected.clear();
                    break;
                case "bulk-clear":
                    ui.selected.clear();
                    paintTable(true);
                    break;
                case "learn": {
                    const offer = ui.callout;
                    ui.callout = null;
                    if (offer) edit((o) => edits.learn(o, offer.path, offer.project));
                    paintCallout();
                    break;
                }
                case "no-learn":
                    ui.callout = null;
                    paintCallout();
                    break;
                default:
                    break;
            }
        });
        // Ticks: a change event, so shift-click can select a run of rows.
        body.addEventListener("click", (event) => {
            const tick = event.target.closest('[data-act="tick"]');
            if (!tick) return;
            const path = tick.closest("[data-file]").dataset.file;
            const items = (controller.state.scan?.unassigned || []).map((u) => u.path);
            if (event.shiftKey && ui.lastSelected && items.includes(ui.lastSelected)) {
                const [a, b] = [items.indexOf(ui.lastSelected), items.indexOf(path)].sort((x, y) => x - y);
                items.slice(a, b + 1).forEach((p) => ui.selected.add(p));
            } else if (tick.checked) ui.selected.add(path);
            else ui.selected.delete(path);
            ui.lastSelected = path;
            paintTable(true);
        });
        body.addEventListener("keydown", (event) => {
            const input = event.target.closest?.('[data-role="rename"]');
            if (!input) return;
            if (event.key === "Enter") {
                event.preventDefault();
                commitRename(input.value);
            } else if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                ui.renaming = null;
                paintTable(true);
            }
        });
        body.addEventListener("focusout", (event) => {
            const input = event.target.closest?.('[data-role="rename"]');
            if (input && ui.renaming) commitRename(input.value);
        });
        body.addEventListener("toggle", (event) => {
            if (event.target.matches?.(".plx-dsb-excluded")) ui.openExcluded = event.target.open;
        }, true);
        wireDrag(body);
    }

    function commitRename(value) {
        const id = ui.renaming;
        ui.renaming = null;
        const project = projectById(id);
        const name = String(value || "").trim();
        if (!project || !name || name === project.name) {
            paintTable(true);
            return;
        }
        edit((o) => edits.rename(o, id, name), [id]);
    }

    // -- dragging an unassigned file onto a sample -----------------------------------

    const DRAG_TYPE = "text/x-plexora-dsb-files";

    function dragged(urow) {
        const path = urow.dataset.file;
        const paths = ui.selected.has(path) ? Array.from(ui.selected) : [path];
        return {paths, kind: urow.dataset.kind};
    }

    function dropOn(projectId, payload) {
        if (!payload || !projectId) return;
        const slot = slotFor(payload.kind);
        if (slot === "image") return;
        assign(payload.paths, [projectId], slot);
    }

    function wireDrag(body) {
        body.addEventListener("dragstart", (event) => {
            const urow = event.target.closest?.(".plx-dsb-urow[data-file]");
            if (!urow) return;
            ui.dragging = dragged(urow);
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData(DRAG_TYPE, JSON.stringify(ui.dragging));
            event.dataTransfer.setData("text/plain", ui.dragging.paths.join("\n"));
        });
        body.addEventListener("dragend", () => {
            ui.dragging = null;
            body.querySelectorAll(".is-drop-over").forEach((el) => el.classList.remove("is-drop-over"));
        });
        body.addEventListener("dragover", (event) => {
            const row = event.target.closest?.(".plx-dsb-row[data-project]");
            if (!row || !ui.dragging) return;
            event.preventDefault();
            event.dataTransfer.dropEffect = "move";
            body.querySelectorAll(".is-drop-over").forEach((el) => {
                if (el !== row) el.classList.remove("is-drop-over");
            });
            row.classList.add("is-drop-over");
            row.dataset.dropSlot = slotFor(ui.dragging.kind);
        });
        body.addEventListener("dragleave", (event) => {
            const row = event.target.closest?.(".plx-dsb-row[data-project]");
            if (row && !row.contains(event.relatedTarget)) row.classList.remove("is-drop-over");
        });
        body.addEventListener("drop", (event) => {
            const row = event.target.closest?.(".plx-dsb-row[data-project]");
            if (!row) return;
            event.preventDefault();
            row.classList.remove("is-drop-over");
            let payload = ui.dragging;
            try {
                payload = JSON.parse(event.dataTransfer.getData(DRAG_TYPE)) || payload;
            } catch (error) { /* the drag that started here is in ui.dragging */ }
            ui.dragging = null;
            dropOn(row.dataset.project, payload);
        });
        // The desktop app's window, where HTML5 drags inside the page do not
        // land (services/pointerDrag.js says why): the same drop, by pointer.
        if (window.PlexoraDesktop && window.PlexoraPointerDrag) {
            window.PlexoraPointerDrag.attach(body, {
                source: ".plx-dsb-urow[draggable]",
                payload: (urow) => dragged(urow),
                label: (data) => data.paths.length === 1 ? basename(data.paths[0])
                    : plural(data.paths.length, ["file", "files"]),
                targetAt: (under) => under.closest?.(".plx-dsb-row[data-project]") || null,
                onOver: (target, over) => target.classList.toggle("is-drop-over", over),
                onDrop: (target, data) => dropOn(target.dataset.project, data),
            });
        }
    }

    // -- the assign picker ----------------------------------------------------------------

    function closePicker() {
        if (!ui?.picker) return;
        ui.picker.remove();
        ui.picker = null;
        ui.pickerAnchor?.focus?.();
        ui.pickerAnchor = null;
    }

    function place(menu, anchor) {
        const box = anchor.getBoundingClientRect();
        menu.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - 356))}px`;
        menu.style.top = `${box.bottom + 4}px`;
        const spread = menu.getBoundingClientRect();
        if (spread.bottom > window.innerHeight - 8 && box.top > spread.height + 8) {
            menu.style.top = `${box.top - spread.height - 4}px`;
        }
    }

    /**
     * A list to choose from, under the button that asked.
     *
     * `mode: "project"` -- which sample do these files go to (with "Several"
     * for a table read by more than one). `mode: "file"` -- which unassigned
     * file fills this sample's slot.
     */
    function openPicker(anchor, options) {
        closePicker();
        const plan = controller.state.scan || {};
        const menu = document.createElement("div");
        menu.className = "plx-import-menu plx-dsb-assign";
        menu.setAttribute("role", "dialog");
        const several = options.mode === "project" && options.kind === "table";
        const slot = options.mode === "file" ? options.slot : slotFor(options.kind);
        menu.innerHTML = `
            <div class="plx-import-menu-label">${options.mode === "file"
                ? `Choose ${slot === "mask" ? "a segmentation mask" : slot === "table" ? "a data file"
                    : "a channel list"}`
                : options.paths.length > 1 ? `Assign ${plural(options.paths.length, ["file", "files"])} to`
                : "Assign to"}</div>
            <input class="plx-prompt-input plx-dsb-assign-search" type="search"
                   placeholder="${options.mode === "file" ? "Find a file" : "Find a sample"}"
                   aria-label="Search" autocomplete="off" spellcheck="false">
            ${several ? `<label class="plx-dsb-several"><input type="checkbox" data-role="several">
                Several samples read this file</label>` : ""}
            <div class="plx-dsb-assign-list" data-role="list"></div>
            <div class="plx-dsb-assign-foot" data-role="multi" hidden>
                <button class="plx-button plx-button-primary" type="button" data-role="multi-go" disabled>Assign</button>
            </div>`;
        dialog.appendChild(menu);
        ui.picker = menu;
        ui.pickerAnchor = anchor;
        const search = menu.querySelector(".plx-dsb-assign-search");
        const list = menu.querySelector('[data-role="list"]');
        const multi = {on: false, chosen: new Set()};

        const choices = () => {
            const text = search.value.trim().toLowerCase();
            if (options.mode === "file") {
                const kind = slot;
                return (plan.unassigned || []).filter((u) => u.kind === kind
                        && (!text || u.rel.toLowerCase().includes(text)))
                    .map((u) => ({id: u.path, name: u.name, sub: dirname(u.rel)}));
            }
            return (plan.projects || []).filter((p) => !text || p.name.toLowerCase().includes(text))
                .map((p) => {
                    const holding = p[slot === "channels" ? "channels" : slot];
                    return {id: p.id, name: p.name,
                            sub: holding ? (holding.name || holding.label) : `no ${
                                slot === "table" ? "data" : slot === "channels" ? "channel names" : "mask"}`};
                });
        };

        const draw = () => {
            const found = choices();
            list.innerHTML = found.length ? found.map((c, i) => `
                <button class="plx-import-menu-item${i === 0 ? " is-first" : ""}" type="button"
                        data-choice="${esc(c.id)}" role="${multi.on ? "menuitemcheckbox" : "menuitem"}"
                        aria-checked="${multi.chosen.has(c.id)}">
                    ${multi.on ? `<span class="plx-import-menu-check"><span class="fas ${
                        multi.chosen.has(c.id) ? "fa-square-check" : "fa-square"}" aria-hidden="true"></span></span>` : ""}
                    <span class="plx-import-menu-name">${esc(c.name)}</span>
                    <span class="plx-import-menu-sub">${esc(c.sub)}</span>
                </button>`).join("")
                : `<p class="plx-import-menu-empty">${options.mode === "file"
                    ? "No unassigned file of this kind." : "No sample matches."}</p>`;
            const go = menu.querySelector('[data-role="multi-go"]');
            menu.querySelector('[data-role="multi"]').hidden = !multi.on;
            go.disabled = !multi.chosen.size;
            go.textContent = `Assign to ${samples(multi.chosen.size)}`;
        };

        const pickOne = (id) => {
            if (multi.on) {
                if (multi.chosen.has(id)) multi.chosen.delete(id);
                else multi.chosen.add(id);
                draw();
                return;
            }
            closePicker();
            if (options.mode === "file") assign([id], [options.projectId], slot);
            else assign(options.paths, [id], slot);
        };

        list.addEventListener("click", (event) => {
            const choice = event.target.closest("[data-choice]");
            if (choice) pickOne(choice.dataset.choice);
        });
        search.addEventListener("input", draw);
        search.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                const first = list.querySelector("[data-choice]");
                if (first) pickOne(first.dataset.choice);
            } else if (event.key === "ArrowDown") {
                event.preventDefault();
                list.querySelector("[data-choice]")?.focus();
            }
        });
        menu.querySelector('[data-role="several"]')?.addEventListener("change", (event) => {
            multi.on = event.target.checked;
            multi.chosen.clear();
            draw();
        });
        menu.querySelector('[data-role="multi-go"]').addEventListener("click", () => {
            const chosen = Array.from(multi.chosen);
            closePicker();
            assign(options.paths, chosen, slot);
        });
        draw();
        place(menu, anchor);
        search.focus();
    }

    function openMore(anchor, item) {
        if (!item || !window.PlexoraMenu) return;
        const folder = dirname(item.rel);
        const root = (controller.state.scan?.roots || []).find((r) => item.path.startsWith(r)) || "";
        const items = [];
        if (item.kind === "image" || item.kind === "mask") {
            items.push({label: "New sample from this image",
                        onSelect: () => edit((o) => edits.newProject(o, item.path))});
        }
        if (item.kind === "table") {
            items.push({label: "Assign to several samples…",
                        onSelect: () => openPicker(anchor, {mode: "project", paths: [item.path],
                                                            kind: "table"})});
        }
        if (items.length) items.push({separator: true});
        items.push({label: "Exclude", onSelect: () => edit((o) => edits.exclude(o, [item.path]))});
        if (folder && root) {
            const full = `${root.replace(/[\\/]+$/, "")}/${folder}`;
            items.push({label: `Exclude the folder ${folder}/`,
                        onSelect: () => edit((o) => edits.exclude(o, [full]))});
        }
        window.PlexoraMenu.open(anchor, items);
    }

    // -- a build left running ----------------------------------------------------------------

    function watchInBackground(token, name) {
        const tick = async () => {
            if (dialog && controller?.state.token === token) return;
            const reply = await defaultFetch(`datasets/scan/${token}/create`).catch(() => null);
            if (!reply || !reply.ok) return;
            if (reply.body.status === "pending") {
                setTimeout(tick, BACKGROUND_POLL_MS);
                return;
            }
            if (lastBuild && lastBuild.token === token) lastBuild = null;
            announce(reply.body, name);
        };
        setTimeout(tick, BACKGROUND_POLL_MS);
    }

    function announce(build, name) {
        const counts = build.counts || {};
        const made = (counts.created || 0) + (counts.adopted || 0);
        const note = [samples(made), counts.failed ? `${counts.failed} failed` : ""]
            .filter(Boolean).join(" · ");
        window.PlexoraOpenProject?.refresh?.();
        window.PlexoraToast?.show({
            title: `${build.name || name} created`, note,
            tone: counts.failed ? "warning" : undefined,
            actions: build.dataset && window.PlexoraOpenProject?.open ? [{
                label: "Open", primary: true,
                onSelect: () => window.PlexoraOpenProject.open(build.dataset.id)}] : undefined,
        });
        window.PlexoraDesktop?.notifyIfAway?.({title: `${build.name || name} created`, body: note});
    }

    //: Builds already announced, so a repaint after "done" does not toast twice.
    const announced = new Set();

    function onChange(state) {
        // The rows an edit touched are dimmed until its plan comes back.
        if (!state.planning) ui?.busy.clear();
        paint(state);
        if (state.phase !== "done" || !state.token || announced.has(state.token)) return;
        announced.add(state.token);
        lastBuild = null;
        announce(state.build || {}, ui?.name || "");
    }

    return {
        open,
        close,
        isOpen: () => Boolean(dialog),
        createController,
        edits,
        html: {table: tableHtml, unassigned: unassignedHtml, counts: countsLine},
    };
})();
