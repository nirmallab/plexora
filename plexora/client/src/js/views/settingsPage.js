/**
 * settingsPage.js
 *
 * Drives settings.html: the left rail, and the Data section's change-directory
 * flow. Loaded only on that page.
 *
 * The flow is deliberately two-step -- Continue, then Save -- and not one. Every
 * question that decides whether this is safe is a question about the SERVER's
 * filesystem (does the directory exist, can it be written to, what is already
 * in it, would a move be a rename or an hour of copying), so the browser cannot
 * answer any of them and must not pretend to. `Continue` asks
 * /settings/data/check and renders the answer; `Save` is the first request that
 * writes anything.
 *
 * Nothing here decides policy. Which combinations are refused, and the ordering
 * that writes the setting only after a migration succeeds, live in
 * settings_routes.py and data_migration.py -- this renders their answers.
 */
(function () {
    "use strict";

    //: How often the migration progress is re-read. A migration moves whole
    //: projects, so the interval that matters is "fast enough that the last
    //: one does not look stalled", not one that tracks bytes.
    const POLL_MS = 700;

    function el(id) {
        return document.getElementById(id);
    }

    function show(node, visible) {
        if (node) node.hidden = !visible;
    }

    function text(node, value) {
        if (node) node.textContent = value == null ? "" : String(value);
    }

    function getJson(path) {
        return fetch(plexoraUrl(path)).then(readJson);
    }

    function postJson(path, body) {
        return fetch(plexoraUrl(path), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {}),
        }).then(readJson);
    }

    /**
     * The parsed body, with the HTTP status attached rather than thrown.
     *
     * Every refusal from this API carries an `error` string that is written to
     * be read by a person, and a rejected promise would throw that away in
     * favour of "400 Bad Request". The status is still available for the one
     * caller that needs it (202 means a migration started).
     */
    function readJson(response) {
        return response.json()
            .catch(() => ({ error: "The server sent a reply that could not be read." }))
            .then((body) => Object.assign({ status: response.status, ok: response.ok }, body));
    }

    // -- the rail --------------------------------------------------------

    function wireRail() {
        const rail = el("settings_rail");
        if (!rail) return;
        const tabs = Array.from(rail.querySelectorAll(".settings-tab"));
        const panels = Array.from(document.querySelectorAll(".settings-panel"));
        tabs.forEach((tab) => {
            tab.addEventListener("click", () => {
                const wanted = tab.dataset.section;
                tabs.forEach((other) => {
                    const active = other === tab;
                    other.classList.toggle("is-active", active);
                    other.setAttribute("aria-current", active ? "page" : "false");
                });
                panels.forEach((panel) => {
                    panel.classList.toggle("is-active", panel.dataset.section === wanted);
                });
            });
        });
    }

    // -- the data section ------------------------------------------------

    function DataSection() {
        this.checked = null;   // the last /check answer, or null before Continue
        this.polling = null;
    }

    DataSection.prototype.start = function () {
        this.wire();
        this.refresh();
        // A migration survives the page: it runs in a server thread, so a
        // reload during one has to find it rather than show an idle form.
        getJson("settings/data/migration").then((job) => {
            if (job && job.status === "running") this.watch();
        });
    };

    DataSection.prototype.wire = function () {
        const check = el("settings_check");
        const save = el("settings_save");
        const cancel = el("settings_cancel");
        const quit = el("settings_quit");
        const input = el("settings_new_path");
        const browse = el("settings_browse");

        if (check) check.addEventListener("click", () => this.check());
        if (save) save.addEventListener("click", () => this.save());
        if (cancel) cancel.addEventListener("click", () => this.reset());
        if (quit) {
            quit.addEventListener("click", () => {
                // Same endpoint the navbar's Quit uses. The response never
                // arrives -- the process exits mid-request -- so the catch is
                // the success path, not an error path.
                fetch(plexoraUrl("shutdown"), { method: "POST" }).catch(() => {});
            });
        }
        if (input) {
            input.addEventListener("keydown", (event) => {
                if (event.key === "Enter") {
                    event.preventDefault();
                    this.check();
                }
            });
            // Editing the path invalidates the review below it, which was
            // computed for the old text and would otherwise stay on screen
            // describing a directory that is no longer in the box.
            input.addEventListener("input", () => {
                if (this.checked) this.reset(true);
            });
        }
        if (browse && typeof attachBrowseButton === "function") {
            attachBrowseButton(browse, input, { mode: "directory" });
        }
    };

    DataSection.prototype.refresh = function () {
        return getJson("settings/data").then((state) => {
            if (state.error) return this.fail(state.error);
            this.render(state);
            return state;
        });
    };

    DataSection.prototype.render = function (state) {
        text(el("settings_current_path"), state.in_use);

        const count = state.entry_count;
        const items = count === 1 ? "1 item" : count + " items";
        text(el("settings_current_meta"),
             items + " · chosen by " + state.rule);

        const pending = Boolean(state.pending);
        show(el("settings_pending"), pending);
        if (pending) {
            text(el("settings_pending_body"),
                 "Saved: " + state.pending + ". Plexora is still serving from "
                 + state.in_use + " and will use the new directory the next time "
                 + "it starts.");
        }

        const locked = Boolean(state.env_override);
        show(el("settings_env_lock"), locked);
        if (locked) {
            text(el("settings_env_body"),
                 state.env_override + " is set for this server and overrides the "
                 + "stored setting. Unset it and restart Plexora to choose a "
                 + "directory here.");
        }
        // Hidden rather than disabled: a form that cannot do anything is not
        // worth the room, and the notice above it already says why.
        show(el("settings_data_form"), !locked);

        const shared = state.shared_roots || [];
        show(el("settings_shared_card"), shared.length > 0);
        text(el("settings_shared_list"), shared.join("\n"));
    };

    DataSection.prototype.check = function () {
        const input = el("settings_new_path");
        const wanted = input ? input.value.trim() : "";
        if (!wanted) {
            return this.fail("Enter a directory path, or use Browse to pick one.");
        }
        this.clearError();
        return postJson("settings/data/check", { path: wanted }).then((result) => {
            if (result.error) return this.fail(result.error);
            this.checked = result;
            this.renderReview(result);
        });
    };

    DataSection.prototype.renderReview = function (result) {
        text(el("settings_review_path"), result.path);

        const facts = [];
        if (result.is_current) {
            facts.push(["fa-circle-info", "This is already the current data directory."]);
        } else if (!result.exists) {
            facts.push(["fa-plus", "Does not exist yet — it will be created."]);
        } else if (result.entries_here === 0) {
            facts.push(["fa-check", "Exists and is empty."]);
        } else {
            facts.push(["fa-folder", "Exists."]);
        }
        facts.push(result.writable
            ? ["fa-check", "Plexora can write to it."]
            : ["fa-triangle-exclamation", "Plexora cannot write to it."]);
        facts.push(["fa-copy", result.entry_count === 0
            ? "There is nothing in the current directory to bring along."
            : (result.entry_count === 1 ? "1 item" : result.entry_count + " items")
              + " could be brought along."]);

        const list = el("settings_review_facts");
        if (list) {
            list.innerHTML = "";
            facts.forEach(([icon, line]) => {
                const item = document.createElement("li");
                const mark = document.createElement("span");
                mark.className = "fas " + icon;
                mark.setAttribute("aria-hidden", "true");
                item.appendChild(mark);
                item.appendChild(document.createTextNode(" " + line));
                list.appendChild(item);
            });
        }

        // Problems and collisions are reported apart from the facts because
        // they are the reason the two migrating options are unavailable, and a
        // greyed radio with no stated cause reads as a broken control.
        const blockers = [].concat(result.problems || [],
                                   (result.collisions || []).map(
                                       (name) => "Already in the new directory: " + name));
        const blockerBox = el("settings_review_blockers");
        show(blockerBox, blockers.length > 0);
        if (blockerBox) {
            blockerBox.innerHTML = "";
            if (blockers.length) {
                const heading = document.createElement("strong");
                heading.textContent = result.collisions && result.collisions.length
                    ? "Projects cannot be brought along:"
                    : "Projects cannot be brought along here:";
                blockerBox.appendChild(heading);
                const ul = document.createElement("ul");
                blockers.forEach((line) => {
                    const item = document.createElement("li");
                    item.textContent = line;
                    ul.appendChild(item);
                });
                blockerBox.appendChild(ul);
            }
        }

        text(el("settings_choice_none_note"), result.entry_count === 0
            ? "There is nothing in the current directory to move."
            : "Plexora will start empty in the new directory.");
        // The one number that changes how long this takes by orders of
        // magnitude, so it is said before the choice rather than after it.
        text(el("settings_choice_move_note"), result.same_filesystem
            ? "Same filesystem — this is a rename and will be immediate."
            : "A different filesystem — files are copied, then removed. "
              + "This can take a while.");

        const allow = Boolean(result.can_migrate);
        document.querySelectorAll('input[name="settings_migrate"]').forEach((radio) => {
            if (radio.value === "none") return;
            radio.disabled = !allow;
            if (!allow && radio.checked) {
                const none = document.querySelector('input[name="settings_migrate"][value="none"]');
                if (none) none.checked = true;
            }
        });

        const save = el("settings_save");
        if (save) save.disabled = Boolean(result.is_current);
        show(el("settings_review"), true);
    };

    DataSection.prototype.chosenMode = function () {
        const picked = document.querySelector('input[name="settings_migrate"]:checked');
        return picked ? picked.value : "none";
    };

    DataSection.prototype.save = function () {
        if (!this.checked) return Promise.resolve();
        const mode = this.chosenMode();
        const save = el("settings_save");
        if (save) save.disabled = true;
        this.clearError();

        return postJson("settings/data", { path: this.checked.path, migrate: mode })
            .then((result) => {
                if (result.error) {
                    if (save) save.disabled = false;
                    return this.fail(result.error);
                }
                this.reset();
                if (result.status === 202) {
                    this.watch();
                    return null;
                }
                return this.refresh();
            });
    };

    DataSection.prototype.watch = function () {
        show(el("settings_progress"), true);
        const tick = () => {
            getJson("settings/data/migration").then((job) => {
                this.renderProgress(job);
                if (job.status === "running") {
                    this.polling = window.setTimeout(tick, POLL_MS);
                    return;
                }
                this.polling = null;
                if (job.status === "error") {
                    show(el("settings_progress"), false);
                    this.fail(job.error || "The migration failed.");
                }
                // Refreshed either way: on success the setting has just been
                // written by the job's own last step, and the pending banner is
                // the only thing that says a restart is needed.
                this.refresh();
            });
        };
        tick();
    };

    DataSection.prototype.renderProgress = function (job) {
        const total = job.total || 0;
        const done = job.done || 0;
        const fill = el("settings_progress_fill");
        if (fill) {
            fill.style.width = total ? Math.round((done / total) * 100) + "%" : "0%";
        }
        if (job.status === "done") {
            text(el("settings_progress_title"), "Finished");
            text(el("settings_progress_detail"),
                 (job.migrated || []).length + " moved to " + (job.target || ""));
            return;
        }
        text(el("settings_progress_title"),
             job.mode === "copy" ? "Copying…" : "Moving…");
        text(el("settings_progress_detail"),
             total ? (done + " of " + total + (job.current ? " · " + job.current : ""))
                   : "Preparing…");
    };

    /** Put the form back to its resting state. `keepText` is for the case
     *  where the user is editing the path: clearing the box they are typing in
     *  would be the one thing they did not ask for. */
    DataSection.prototype.reset = function (keepText) {
        this.checked = null;
        show(el("settings_review"), false);
        const save = el("settings_save");
        if (save) save.disabled = false;
        if (!keepText) {
            const input = el("settings_new_path");
            if (input) input.value = "";
        }
    };

    DataSection.prototype.fail = function (message) {
        text(el("settings_error_body"), message);
        show(el("settings_error"), true);
        return null;
    };

    DataSection.prototype.clearError = function () {
        show(el("settings_error"), false);
    };

    // -- the data nodes section ------------------------------------------

    /**
     * The address book of machines this Plexora reads data from.
     *
     * Deliberately thin. Which resource a project reads from which node is a
     * question about that project and is asked on its own Edit page; this only
     * knows how to reach a node and whether it answers right now.
     *
     * Reachability is reported per node rather than as one status, because the
     * situation this whole feature exists for is precisely the one where some
     * of them are asleep: a laptop that closed its lid does not make the
     * cluster unreachable, and a list that said so would be wrong about the
     * only thing it is for.
     */
    function NodesSection() {}

    NodesSection.prototype.start = function () {
        const add = el("settings_node_add");
        if (add) add.addEventListener("click", () => this.add());
        // Enter in any field submits, because a four-field form where the
        // keyboard does nothing is a form people distrust.
        ["settings_node_name", "settings_node_endpoint",
         "settings_node_token", "settings_node_browser"].forEach((id) => {
            const input = el(id);
            if (input) {
                input.addEventListener("keydown", (event) => {
                    if (event.key === "Enter") this.add();
                });
            }
        });
        this.refresh();
    };

    NodesSection.prototype.refresh = function () {
        return getJson("/settings/nodes").then((body) => this.render(body.nodes || []));
    };

    NodesSection.prototype.render = function (nodes) {
        const list = el("settings_nodes_list");
        if (!list) return;
        list.textContent = "";
        if (!nodes.length) {
            const empty = document.createElement("div");
            empty.className = "settings-meta";
            empty.textContent = "No data nodes yet. Every project reads from this "
                + "machine.";
            list.appendChild(empty);
            return;
        }
        nodes.forEach((node) => list.appendChild(this.card(node)));
    };

    NodesSection.prototype.card = function (node) {
        const card = document.createElement("div");
        card.className = "settings-card settings-node-card";

        const head = document.createElement("div");
        head.className = "settings-node-head";
        const name = document.createElement("div");
        name.className = "settings-field-label";
        name.textContent = node.name;
        head.appendChild(name);

        const state = document.createElement("span");
        state.className = "settings-node-state "
            + (node.reachable ? "is-reachable" : "is-unreachable");
        state.textContent = node.reachable ? "Reachable" : "Not answering";
        head.appendChild(state);
        card.appendChild(head);

        const address = document.createElement("div");
        address.className = "settings-path";
        address.textContent = node.endpoint;
        card.appendChild(address);

        if (node.browser_endpoint) {
            const browser = document.createElement("div");
            browser.className = "settings-meta";
            browser.textContent = "Browser reaches it at " + node.browser_endpoint;
            card.appendChild(browser);
        }

        const detail = document.createElement("div");
        detail.className = "settings-meta";
        if (node.reachable) {
            // What it is serving, because "reachable" alone does not tell you
            // whether the resource your project points at is still there.
            detail.textContent = node.resources.length
                ? node.resources.map((r) => r.kind + " " + r.id).join(" · ")
                : "Serving nothing.";
        } else {
            detail.textContent = node.error || "No answer.";
        }
        card.appendChild(detail);

        // A node a saved connection set up gets a new port and a new token
        // every session, so its address is not a thing to repair by hand --
        // reconnecting is what fixes it, and saying so is the difference
        // between a stale entry and a confusing one.
        if (node.managed_by) {
            const managed = document.createElement("div");
            managed.className = "settings-meta";
            managed.textContent = "Set up automatically by the saved server "
                + "“" + String(node.managed_by).replace(/^connect:/, "") + "”. "
                + "Reconnect it under Remote servers rather than editing it here.";
            card.appendChild(managed);
        }

        const actions = document.createElement("div");
        actions.className = "settings-actions";
        const forget = document.createElement("button");
        forget.type = "button";
        forget.className = "btn btn-outline-light";
        forget.textContent = "Forget";
        forget.addEventListener("click", () => this.forget(node.name));
        actions.appendChild(forget);
        card.appendChild(actions);
        return card;
    };

    NodesSection.prototype.add = function () {
        show(el("settings_node_error"), false);
        const body = {
            name: (el("settings_node_name") || {}).value || "",
            endpoint: (el("settings_node_endpoint") || {}).value || "",
            token: (el("settings_node_token") || {}).value || "",
            browser_endpoint: (el("settings_node_browser") || {}).value || "",
        };
        const button = el("settings_node_add");
        if (button) button.disabled = true;
        return postJson("/settings/nodes", body)
            .then((answer) => {
                if (answer.error) {
                    text(el("settings_node_error_body"), answer.error);
                    show(el("settings_node_error"), true);
                    return;
                }
                ["settings_node_name", "settings_node_endpoint",
                 "settings_node_token", "settings_node_browser"].forEach((id) => {
                    const input = el(id);
                    if (input) input.value = "";
                });
                return this.refresh();
            })
            .finally(() => {
                if (button) button.disabled = false;
            });
    };

    NodesSection.prototype.forget = function (name) {
        return fetch(plexoraUrl("/settings/nodes/" + encodeURIComponent(name)),
                     { method: "DELETE" })
            .then(readJson)
            .then(() => this.refresh());
    };

    // -- remote servers --------------------------------------------------

    /**
     * Whether this connection has something to disconnect.
     *
     * Not the same as "still happening": a connected session is at rest and
     * polling it changes nothing, but its button still has to say Disconnect.
     * Which states are which is `PlexoraRemotes`'s to know -- this page used to
     * keep its own copy of the list and its own timer, and drifted.
     */
    function isLive(state) {
        return state === "connected" || window.PlexoraRemotes.isOpening(state);
    }

    //: The plain text boxes, by the key each one posts under. Everything with
    //: a shape of its own -- the two switches, the port list -- is handled
    //: beside this rather than in it.
    //:
    //: No `serve` / `local_serve` / `node_name`, and no `datasource` either.
    //: The first three named the files each end would offer, which had to be
    //: decided before Plexora started -- the exact thing the Local/Remote
    //: switch on every data field replaced. `datasource` tied a saved server
    //: to one project on it, which is a different object from a machine. A
    //: record written by `plexora connect --save` may still carry all four and
    //: is left alone: the route keeps every field the form does not send.
    const REMOTE_FIELDS = {
        name: "settings_remote_name",
        target: "settings_remote_target",
        data_dir: "settings_remote_data_dir",
        remote_command: "settings_remote_command",
        cores: "settings_remote_cores",
        memory: "settings_remote_memory",
        walltime: "settings_remote_walltime",
        srun: "settings_remote_srun",
    };

    //: The three resource boxes, which are filled in together the first time
    //: the scheduler switch goes on. Their values come from the template,
    //: which read them from recipes.defaults() -- this file writes no numbers.
    const JOB_FIELDS = ["settings_remote_cores", "settings_remote_memory",
                        "settings_remote_walltime", "settings_remote_srun"];

    /**
     * Saved servers, and the connections they are currently running.
     *
     * The whole point of the section is that a user should not have to hold an
     * ssh command in their head, so the card is written to answer the two
     * questions they actually have while it is slow -- what is it doing, and
     * is that normal -- with a sentence rather than a spinner. "Waiting for
     * the scheduler" is not a failure and reads as one when it is unlabelled.
     *
     * Progress is polled rather than pushed, because the thing being watched
     * is a subprocess on this machine rather than an event stream -- but the
     * polling is not done here. `services/remoteState.js` owns it for every
     * surface at once, and this section is one of its subscribers: an `active`
     * one, because a Settings page somebody is looking at is exactly the case
     * where a settled connection still has to be re-read when another tab acts
     * on it.
     */
    function RemotesSection() {
        this.watching = null;
        this.editing = null;
        //: One card per saved server, built once and repainted. Rebuilding
        //: them on every poll is what made the connection log unreadable: the
        //: pane was a NEW element once a second, so it started at the top once
        //: a second, which is exactly when there is something in it worth
        //: reading. It also blurred whichever button had the focus and ate a
        //: half-typed password, both of which needed hand-written workarounds
        //: that this removes rather than improves.
        this.cards = {};
        //: The names, in order, as last appended. Order is only re-applied
        //: when the SET changes: moving a node within the DOM re-attaches it,
        //: and a re-attached element loses its scroll position.
        this.order = "";
        //: Which logs are expanded, by name. Kept here rather than read off
        //: the <details>, because this is also what decides which connections
        //: are worth pulling the deep 200-line tail for.
        this.openLogs = {};
        //: The extra ports being forwarded, as a list rather than as lines in
        //: a textarea. The list is the state; the chips are a rendering of it.
        this.forwards = [];
    }

    /**
     * Fill the job boxes that are still empty from their `data-default`.
     *
     * Only the empty ones: turning the switch off and on again must not throw
     * away a walltime somebody typed. The values themselves come from the
     * template attribute rather than from here -- one source, in recipes.py.
     */
    function setValue(id, value) {
        const box = el(id);
        if (box) box.value = value == null ? "" : String(value);
    }

    function fillDefaults() {
        JOB_FIELDS.forEach((id) => {
            const box = el(id);
            if (!box || box.value.trim()) return;
            box.value = box.getAttribute("data-default") || "";
        });
    }

    RemotesSection.prototype.start = function () {
        const save = el("settings_remote_save");
        if (save) save.addEventListener("click", () => this.save());
        const reset = el("settings_remote_reset");
        if (reset) reset.addEventListener("click", () => this.clearForm());
        const preset = el("settings_remote_preset");
        if (preset) preset.addEventListener("click", () => this.addFromPreset());
        // Turning on "run through cluster scheduler" reveals the resource
        // boxes AND fills them in, once, and only where nobody has typed. A
        // default nobody can see is a default nobody can correct -- and an
        // empty Cores box does not mean "no cores", it means "whatever the
        // site does", which on most clusters is one core and a couple of
        // gigabytes: enough to start Plexora and not enough to open a
        // multiplexed pyramid in it. Setting a value programmatically fires no
        // change event, so `edit` restoring a saved profile's own numbers
        // cannot trip this.
        const useSrun = el("settings_remote_use_srun");
        if (useSrun) {
            useSrun.addEventListener("change", () => {
                this.revealJob(useSrun.checked);
                if (useSrun.checked) fillDefaults();
            });
        }

        const port = el("settings_remote_port");
        const addPort = el("settings_remote_port_add");
        if (addPort) addPort.addEventListener("click", () => this.addPort());
        // Enter is what a person types after a number in a box next to an Add
        // button. There is no <form> here to submit, so without this it does
        // nothing at all and the port is silently not added.
        if (port) port.addEventListener("keydown", (event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            this.addPort();
        });

        // The markup carries `hidden` on the job block; saying it here too
        // means the reveal has one owner rather than two that must agree.
        this.revealJob(Boolean(useSrun && useSrun.checked));
        this.forwards = [];
        this.renderForwards();
        this.watching = window.PlexoraRemotes.subscribe(
            (snapshot) => this.render(snapshot), {
                active: true,
                // A function, not a fixed spec: which log is open changes as
                // the user opens and closes them, and re-subscribing on every
                // toggle would tear down the subscription in order to preserve
                // the one thing being preserved.
                focus: () => Object.keys(this.openLogs)
                    .filter((name) => this.openLogs[name])
                    .map((name) => ({ name: name,
                                      kind: window.PlexoraRemotes.KIND_NODE })),
            });
    };

    RemotesSection.prototype.stop = function () {
        if (this.watching) this.watching();
        this.watching = null;
    };

    RemotesSection.prototype.refresh = function () {
        return window.PlexoraRemotes.refresh();
    };

    RemotesSection.prototype.render = function (snapshot) {
        const list = el("settings_remotes_list");
        if (!list) return;
        const entries = snapshot.entries || [];

        if (!entries.length) {
            this.cards = {};
            this.order = "";
            const empty = document.createElement("div");
            empty.className = "settings-meta";
            empty.textContent = "No servers saved yet. “Use preset” "
                + "fills most of this in for you.";
            list.replaceChildren(empty);
            return;
        }

        // The saved record as well as the merged entry: the entry says what the
        // connection is doing, and Edit needs the profile's own fields.
        const saved = {};
        (snapshot.remotes || []).forEach((remote) => {
            saved[remote.name] = remote;
        });
        entries.forEach((entry) => {
            if (!this.cards[entry.name]) {
                this.cards[entry.name] = this.buildCard(entry.name);
            }
            this.paintCard(this.cards[entry.name], entry,
                           saved[entry.name] || {});
        });
        Object.keys(this.cards).forEach((name) => {
            if (!entries.some((entry) => entry.name === name)) {
                delete this.cards[name];
                delete this.openLogs[name];
            }
        });

        // Only when the set changed. Re-appending a node that is already in
        // place still detaches and re-attaches it, and an element that has
        // been re-attached has forgotten where it was scrolled to.
        // JSON rather than a join: a saved name may contain a space, and
        // ["a b"] and ["a", "b"] joining to the same string would mean a
        // renamed pair of servers kept the old order.
        const order = JSON.stringify(entries.map((entry) => entry.name));
        if (order !== this.order) {
            this.order = order;
            list.replaceChildren(
                ...entries.map((entry) => this.cards[entry.name].root));
        }
    };

    /**
     * The parts of one server's card, built once.
     *
     * Everything that changes is a text or a class on one of these; nothing
     * here is created again while the card is on screen. That is what lets the
     * log keep its scroll position, the password box keep what is typed in it,
     * and a focused button stay focused through a connection that updates once
     * a second for a quarter of an hour.
     */
    RemotesSection.prototype.buildCard = function (name) {
        const root = document.createElement("div");
        root.className = "settings-card settings-remote-card";

        const head = document.createElement("div");
        head.className = "settings-node-head";
        const title = document.createElement("div");
        title.className = "settings-field-label";
        title.textContent = name;
        const chip = document.createElement("span");
        head.append(title, chip);

        const address = document.createElement("div");
        address.className = "settings-path";

        const phase = document.createElement("div");
        phase.className = "settings-meta";
        phase.hidden = true;

        const serving = document.createElement("div");
        serving.className = "settings-meta";
        serving.hidden = true;

        // Only ever shown for a connection running inside a scheduled job --
        // see paintCard. A login-node connection has no clock, and a row that
        // said "unlimited" would be inventing a fact about somebody's site.
        const clock = document.createElement("div");
        clock.className = "settings-meta settings-remote-clock";
        clock.hidden = true;

        const error = document.createElement("div");
        error.className = "settings-notice settings-notice-error";
        error.setAttribute("role", "alert");
        error.hidden = true;

        const promptSlot = document.createElement("div");

        const card = { root, chip, address, phase, serving, clock, error,
                       promptSlot, name: name, drawnPrompt: null };
        root.append(head, address, phase, serving, clock, error, promptSlot,
                    this.buildLog(card), this.buildActions(card));
        return card;
    };

    RemotesSection.prototype.paintCard = function (card, entry, remote) {
        card.remote = remote;
        // The NODE half. Connecting from this page opens a data node on that
        // machine and leaves Plexora here -- see connect() for why there is no
        // longer a second kind of connection to choose between.
        const half = entry.node;
        const live = Boolean(half.node) || isLive(half.state);
        const state = half.node ? "connected" : half.state;

        card.chip.className = "settings-node-state is-" + state;
        card.chip.textContent = window.PlexoraRemotes.label(state);

        // On the address line rather than as a badge of its own: these are
        // both "what pressing Connect will do to that machine before you see
        // anything", and the install one is the half that writes.
        card.address.textContent = entry.detail
            + (entry.queued ? "  ·  runs inside a job" : "")
            + (entry.install
                ? "  ·  installs Plexora"
                  + (entry.installEnv ? " in " + entry.installEnv : "")
                : "");

        card.phase.textContent = half.phase || "";
        card.phase.hidden = !half.phase;

        card.serving.textContent = half.node
            ? "Serving files to this Plexora as “" + half.node + "”."
            : "";
        card.serving.hidden = !half.node;

        // How long the job has left. This card is repainted on every poll and
        // the page polls once a second while it is open, so the number moves
        // without a timer of its own -- `remaining()` interpolates the rest.
        const left = window.PlexoraRemotes.remaining(entry);
        card.clock.hidden = left === null;
        card.clock.classList.toggle(
            "is-urgent",
            left !== null && left <= window.PlexoraRemotes.WARN_SECONDS);
        if (left !== null) {
            card.clock.textContent = left
                ? "Time remaining " + window.PlexoraRemotes.duration(left)
                  + " — this connection runs inside a job."
                : "This connection's job has run out of time. "
                  + "Connect again to start a new one.";
        }

        card.error.textContent = half.error || "";
        card.error.hidden = !half.error;

        this.paintPrompt(card, half.prompt);
        this.paintLog(card, entry);
        this.paintActions(card, live);
    };

    /**
     * Whatever SSH just asked, verbatim, with a box to answer it in.
     *
     * The prompt text is ssh's own and is not rewritten: it may be "Password:",
     * a Duo push, a passphrase for a specific key file, or the host-key
     * paragraph that wants "yes". Only the user can tell which, and a friendlier
     * label would be a guess about the one thing they have to read exactly.
     */
    /**
     * Whatever SSH just asked, verbatim, with a box to answer it in.
     *
     * The prompt text is ssh's own and is not rewritten: it may be "Password:",
     * a Duo push, a passphrase for a specific key file, or the host-key
     * paragraph that wants "yes". Only the user can tell which, and a friendlier
     * label would be a guess about the one thing they have to read exactly.
     *
     * Redrawn only when it is a DIFFERENT question. This runs every second and
     * the box is inside it: rebuilding it would take away whatever had been
     * typed, one character at a time.
     */
    RemotesSection.prototype.paintPrompt = function (card, prompt) {
        if (!prompt) {
            if (card.drawnPrompt !== null) card.promptSlot.replaceChildren();
            card.drawnPrompt = null;
            return;
        }
        if (card.drawnPrompt === prompt.id) return;
        card.drawnPrompt = prompt.id;

        const box = document.createElement("div");
        box.className = "settings-remote-prompt";

        const label = document.createElement("label");
        label.className = "settings-node-field";
        const text = document.createElement("span");
        text.textContent = prompt.text;
        label.appendChild(text);

        const input = document.createElement("input");
        // A host-key question is not a secret and hiding it would make it
        // unanswerable next to the fingerprint it is asking about; everything
        // else is. One shared predicate -- this page and the picker each had
        // their own, and disagreed about fingerprints.
        input.type = window.PlexoraRemotes.isSecret(prompt.text)
            ? "password" : "text";
        input.className = "form-control settings-remote-secret";
        input.autocomplete = "off";
        input.dataset.remote = card.name;
        label.appendChild(input);
        box.appendChild(label);

        const send = document.createElement("button");
        send.type = "button";
        send.className = "btn btn-primary";
        send.textContent = "Send";
        const submit = () => {
            const value = input.value;
            input.value = "";
            this.answer(card.name, prompt.id, value);
        };
        send.addEventListener("click", submit);
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter") submit();
        });
        box.appendChild(send);
        card.promptSlot.replaceChildren(box);
        // Deliberately NOT focused. The connection dialog does focus its box,
        // because it has just been opened for exactly this. Here the question
        // can arrive at a card behind somebody filling in the Add-a-server
        // form, and taking the cursor out of it mid-address would be the
        // dialog's helpfulness applied where it is an interruption.
    };

    /**
     * The connection log, as a terminal.
     *
     * Collapsed by default, because most of the time the card's own sentence
     * is the whole answer -- and opened when it is not, which is when ssh has
     * said something Plexora cannot summarise. Opening one is also what asks
     * for the deep tail: the list payload carries the last eight lines, and
     * two hundred is the number that includes the thing that went wrong.
     *
     * Everything about how it scrolls is services/logTerminal.js's, shared
     * with the connection modal, because the two of them disagreeing about
     * what a terminal does is how this went wrong the first time.
     */
    RemotesSection.prototype.buildLog = function (card) {
        const details = document.createElement("details");
        details.className = "settings-remote-log";
        const summary = document.createElement("summary");
        summary.textContent = "Connection log";
        const terminal = window.PlexoraLogTerminal.create({
            title: null,
            empty: "Nothing yet. Press Connect and this fills in.",
        });
        details.append(summary, terminal.element);
        details.addEventListener("toggle", () => {
            this.openLogs[card.name] = details.open;
            if (!details.open) return;
            // A pane that was hidden has no height to scroll, so the follow
            // has to happen once it is visible; and the deep tail is worth
            // asking for the moment somebody says they want to read it,
            // rather than at the next tick.
            terminal.follow();
            this.refresh();
        });
        card.log = details;
        card.terminal = terminal;
        return details;
    };

    RemotesSection.prototype.paintLog = function (card, entry) {
        const deep = window.PlexoraRemotes.focused(
            card.name, window.PlexoraRemotes.KIND_NODE) || {};
        const lines = (deep.log && deep.log.length) ? deep.log
            : (entry.node.log || []);
        card.log.hidden = !lines.length;
        card.terminal.paint(lines);
    };

    RemotesSection.prototype.buildActions = function (card) {
        const actions = document.createElement("div");
        actions.className = "settings-actions";

        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.addEventListener("click", () => {
            if (card.live) this.disconnect(card.name);
            else this.connect(card.name);
        });

        const edit = document.createElement("button");
        edit.type = "button";
        edit.className = "btn btn-outline-light";
        edit.textContent = "Edit";
        edit.addEventListener("click", () => this.edit(card.remote));

        const forget = document.createElement("button");
        forget.type = "button";
        forget.className = "btn btn-outline-light";
        forget.textContent = "Forget";
        forget.addEventListener("click", () => this.forget(card.name));

        actions.append(toggle, edit, forget);
        card.toggle = toggle;
        return actions;
    };

    RemotesSection.prototype.paintActions = function (card, live) {
        card.live = live;
        const wanted = live ? "Disconnect" : "Connect";
        if (card.toggle.textContent !== wanted) {
            card.toggle.textContent = wanted;
            card.toggle.className = live ? "btn btn-outline-light"
                                         : "btn btn-primary";
        }
    };

    /**
     * Connecting happens in the connection modal, here as everywhere else --
     * and it opens a DATA NODE, which is the only kind of connection Plexora
     * offers from inside itself.
     *
     * This button used to run Plexora on the far machine and tunnel the viewer
     * back, which made the Settings page a place where the host Plexora runs
     * on could be redefined from inside the running app. That is one concept
     * too many. The machine Plexora is running on is Local, always; everything
     * reached from it over SSH is Remote. If the base environment should BE the
     * cluster, Plexora is launched there -- `plexora connect`, a terminal, Open
     * OnDemand, JupyterHub -- and from that instance the cluster is Local.
     *
     * The saved profile is the same record either way, which is why the
     * advanced fields on the form below still exist: `plexora connect` reads
     * them from the command line.
     *
     * The card behind the dialog keeps its own prompt box on purpose: a
     * question can arrive at a card whose connection somebody opened from
     * another surface entirely, and a Settings page that showed "Needs your
     * password" with nowhere to type it would be a dead end. Two prompt
     * surfaces, one heuristic for whether the answer is a secret.
     */
    RemotesSection.prototype.connect = function (name) {
        return window.PlexoraConnectionModal.open({
            name: name,
            kind: window.PlexoraRemotes.KIND_NODE,
            intent: "Opens a data node on that machine. Plexora stays here; "
                    + "files over there become usable on every data field.",
        });
    };

    /**
     * Add a server by starting from a site somebody already works at.
     *
     * The same dialog every other surface opens, on its catalogue page. It
     * stays a dialog rather than a menu that fills this form in, because a
     * preset has things to say that this form has nowhere to put: the sites
     * whose values we have never verified carry a warning, and the ones that
     * know the host still cannot know the username -- which the dialog asks
     * for outright, and refuses to compose a target without.
     */
    RemotesSection.prototype.addFromPreset = function () {
        return window.PlexoraConnectionModal.open({
            kind: window.PlexoraRemotes.KIND_NODE,
            view: "recipes",
            intent: "Start from the machine you use. You can change any of it "
                    + "afterwards.",
        });
    };

    // -- the ports list -----------------------------------------------------

    RemotesSection.prototype.addPort = function () {
        const box = el("settings_remote_port");
        if (!box) return;
        const value = (box.value || "").trim();
        box.value = "";
        // Silently, not with an error: adding 8642 twice is somebody who
        // cannot see whether the first one landed, and the answer they want is
        // the list, not a complaint.
        if (!value || this.forwards.indexOf(value) >= 0) return;
        this.forwards.push(value);
        this.renderForwards();
        if (box.focus) box.focus();
    };

    RemotesSection.prototype.renderForwards = function () {
        const box = el("settings_remote_forwards");
        if (!box) return;
        const chips = this.forwards.map((port) => {
            const chip = document.createElement("span");
            chip.className = "remote-chip";
            const label = document.createElement("span");
            label.textContent = port;
            const drop = document.createElement("button");
            drop.type = "button";
            drop.className = "remote-chip-drop";
            drop.setAttribute("aria-label", "Remove port " + port);
            drop.innerHTML = '<span class="fas fa-xmark" aria-hidden="true"></span>';
            drop.addEventListener("click", () => {
                this.forwards = this.forwards.filter((other) => other !== port);
                this.renderForwards();
            });
            chip.appendChild(label);
            chip.appendChild(drop);
            return chip;
        });
        box.replaceChildren.apply(box, chips);
        show(box, chips.length > 0);
    };

    /** Show or hide everything that only means something inside a job. */
    RemotesSection.prototype.revealJob = function (on) {
        show(el("settings_remote_job"), !!on);
    };

    RemotesSection.prototype.disconnect = function (name) {
        return window.PlexoraRemotes
            .disconnect(name, window.PlexoraRemotes.KIND_NODE)
            .catch(() => {});
    };

    RemotesSection.prototype.answer = function (name, id, value) {
        return window.PlexoraRemotes
            .answer(name, window.PlexoraRemotes.KIND_NODE, id, value)
            .catch(() => {});
    };

    RemotesSection.prototype.forget = function (name) {
        return window.PlexoraRemotes.forget(name).catch(() => {});
    };

    RemotesSection.prototype.edit = function (remote) {
        if (!remote || !remote.name) return;
        Object.keys(REMOTE_FIELDS).forEach((key) => {
            setValue(REMOTE_FIELDS[key], remote[key]);
        });
        // The job line is stored as one string and edited as four boxes. The
        // server splits it -- recipes.split_srun -- so that the page which
        // SHOWS a walltime and the route which STORES one cannot disagree
        // about which flag carries it.
        const parts = remote.srun_parts || {};
        setValue("settings_remote_cores", parts.cores);
        setValue("settings_remote_memory", parts.memory);
        setValue("settings_remote_walltime", parts.walltime);
        setValue("settings_remote_srun", parts.extra);

        const useSrun = el("settings_remote_use_srun");
        // null means "no scheduler"; the empty string means "srun with your
        // site's defaults", which is a real and different choice -- and one
        // the boxes show correctly as empty, since nothing was asked for.
        const scheduler = remote.srun !== null && remote.srun !== undefined;
        if (useSrun) useSrun.checked = scheduler;
        this.revealJob(scheduler);
        const bind = el("settings_remote_bind_node");
        if (bind) bind.checked = !!remote.bind_node;
        const install = el("settings_remote_install");
        if (install) install.checked = !!remote.install;

        this.forwards = (remote.forwards || []).map(String);
        this.renderForwards();

        // Opened only when there is something in there to see. Opening an
        // empty disclosure on every Edit makes the form look like it has nine
        // questions when this server answered three of them.
        const advanced = el("settings_remote_advanced");
        // `install` counts here for a reason the others do not have: it is the
        // one setting in this form that makes connecting WRITE to the far
        // machine, so an Edit that hid it would be hiding the answer somebody
        // is most likely to have come to change.
        if (advanced) advanced.open = scheduler || this.forwards.length > 0
            || Boolean(remote.install)
            || Boolean(remote.remote_command
                       && remote.remote_command !== "plexora");
        text(el("settings_remote_form_title"), "Edit “" + remote.name + "”");
        show(el("settings_remote_reset"), true);
        const nameInput = el("settings_remote_name");
        if (nameInput) nameInput.focus();
    };

    RemotesSection.prototype.clearForm = function () {
        Object.keys(REMOTE_FIELDS).forEach((key) => {
            setValue(REMOTE_FIELDS[key], "");
        });
        setValue("settings_remote_port", "");
        ["settings_remote_use_srun", "settings_remote_bind_node",
         "settings_remote_install"].forEach((id) => {
            const box = el(id);
            if (box) box.checked = false;
        });
        this.revealJob(false);
        this.forwards = [];
        this.renderForwards();
        const advanced = el("settings_remote_advanced");
        if (advanced) advanced.open = false;
        text(el("settings_remote_form_title"), "Add a server");
        show(el("settings_remote_reset"), false);
        show(el("settings_remote_error"), false);
    };

    RemotesSection.prototype.save = function () {
        show(el("settings_remote_error"), false);
        const body = {};
        Object.keys(REMOTE_FIELDS).forEach((key) => {
            body[key] = (el(REMOTE_FIELDS[key]) || {}).value || "";
        });
        body.use_srun = !!(el("settings_remote_use_srun") || {}).checked;
        body.bind_node = !!(el("settings_remote_bind_node") || {}).checked;
        body.install = !!(el("settings_remote_install") || {}).checked;
        body.forwards = this.forwards.slice();

        const button = el("settings_remote_save");
        if (button) button.disabled = true;
        return window.PlexoraRemotes.save(body)
            .then(() => this.clearForm())
            .catch((error) => {
                text(el("settings_remote_error_body"), error.message);
                show(el("settings_remote_error"), true);
            })
            .finally(() => {
                if (button) button.disabled = false;
            });
    };

    PlexoraPage.register(() => {
        wireRail();
        if (!el("settings_panel_data")) return null;
        if (el("settings_panel_nodes")) new NodesSection().start();
        let remotes = null;
        if (el("settings_panel_remotes")) {
            remotes = new RemotesSection();
            remotes.start();
        }
        const section = new DataSection();
        section.start();
        // The migration poll is the one thing here that outlives the markup: it
        // reschedules itself, so leaving the Settings page mid-migration would
        // otherwise keep asking the server about a job on behalf of a panel that
        // is no longer on screen. The job itself is the server's and carries on;
        // reopening Settings picks it up again through watch().
        //
        // The remote-connection watch needs the same stop for the same reason:
        // the ssh processes belong to the server and keep running, so leaving
        // the page must drop the subscription, not the connection. Dropping it
        // is also what returns PlexoraRemotes to its resting state -- with no
        // active subscriber left, a settled connection is polled at nothing.
        return () => {
            window.clearTimeout(section.polling);
            if (remotes) remotes.stop();
        };
    });
}());
