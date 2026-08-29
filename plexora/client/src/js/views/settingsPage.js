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

    const REMOTE_FIELDS = {
        name: "settings_remote_name",
        target: "settings_remote_target",
        datasource: "settings_remote_datasource",
        remote_command: "settings_remote_command",
        srun: "settings_remote_srun",
        data_dir: "settings_remote_data_dir",
        forwards: "settings_remote_forwards",
        // No `serve` / `local_serve` / `node_name`. They named the files each
        // end would offer, which had to be decided before Plexora started --
        // the exact thing the Local/Remote switch on every data field replaced.
        // A record written by `plexora connect --save` may still carry them and
        // is left alone: see the save path, which sends only what it edited.
    };

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
        // Which connection logs are expanded. Kept here rather than read off
        // the DOM because the DOM is what gets destroyed: a live connection
        // re-renders every card on every update, so a <details> that owned its
        // own open state would close again within the second, which is
        // exactly when the log is worth reading.
        this.openLogs = {};
    }

    RemotesSection.prototype.start = function () {
        const save = el("settings_remote_save");
        if (save) save.addEventListener("click", () => this.save());
        const reset = el("settings_remote_reset");
        if (reset) reset.addEventListener("click", () => this.clearForm());
        this.watching = window.PlexoraRemotes.subscribe(
            (snapshot) => this.render(snapshot.remotes), { active: true });
    };

    RemotesSection.prototype.stop = function () {
        if (this.watching) this.watching();
        this.watching = null;
    };

    RemotesSection.prototype.refresh = function () {
        return window.PlexoraRemotes.refresh();
    };

    RemotesSection.prototype.render = function (remotes) {
        const list = el("settings_remotes_list");
        if (!list) return;

        // A poll must never eat a half-typed password. The input is inside the
        // card being replaced, so its value and focus are carried across the
        // re-render by hand.
        const active = document.activeElement;
        const keep = active && active.classList
            && active.classList.contains("settings-remote-secret")
            ? { name: active.dataset.remote, value: active.value }
            : null;

        list.textContent = "";
        if (!remotes.length) {
            const empty = document.createElement("div");
            empty.className = "settings-meta";
            empty.textContent = "No servers saved yet. Add one below and "
                + "Plexora will handle the SSH connection for you.";
            list.appendChild(empty);
            return;
        }
        remotes.forEach((remote) => list.appendChild(this.card(remote)));

        if (keep) {
            const restored = list.querySelector(
                ".settings-remote-secret[data-remote=\"" + keep.name + "\"]");
            if (restored) {
                restored.value = keep.value;
                restored.focus();
            }
        }
    };

    RemotesSection.prototype.card = function (remote) {
        const card = document.createElement("div");
        card.className = "settings-card settings-remote-card";

        const head = document.createElement("div");
        head.className = "settings-node-head";
        const name = document.createElement("div");
        name.className = "settings-field-label";
        name.textContent = remote.name;
        head.appendChild(name);

        const state = document.createElement("span");
        state.className = "settings-node-state is-" + remote.state;
        state.textContent = window.PlexoraRemotes.label(remote.state);
        head.appendChild(state);
        card.appendChild(head);

        const address = document.createElement("div");
        address.className = "settings-path";
        address.textContent = remote.target
            + (remote.srun !== null && remote.srun !== undefined
                ? "  ·  runs inside a job" : "");
        card.appendChild(address);

        if (remote.phase) {
            const phase = document.createElement("div");
            phase.className = "settings-meta";
            phase.textContent = remote.phase;
            card.appendChild(phase);
        }
        if (remote.error) {
            const error = document.createElement("div");
            error.className = "settings-notice settings-notice-error";
            error.textContent = remote.error;
            card.appendChild(error);
        }
        (remote.data_nodes || []).forEach((node) => {
            const line = document.createElement("div");
            line.className = "settings-meta";
            line.textContent = "Data node “" + node.name + "” is serving to it.";
            card.appendChild(line);
        });
        // Amber, not red, and not folded into `error`: the viewer opened, and
        // what is missing is one layer of one project.
        (remote.node_errors || []).forEach((problem) => {
            const line = document.createElement("div");
            line.className = "settings-notice settings-notice-warn";
            line.textContent = "Connected, but a data node did not: " + problem;
            card.appendChild(line);
        });
        if (remote.prompt) card.appendChild(this.promptBox(remote));
        if (remote.log && remote.log.length) card.appendChild(this.logBox(remote));

        card.appendChild(this.actions(remote));
        return card;
    };

    /**
     * Whatever SSH just asked, verbatim, with a box to answer it in.
     *
     * The prompt text is ssh's own and is not rewritten: it may be "Password:",
     * a Duo push, a passphrase for a specific key file, or the host-key
     * paragraph that wants "yes". Only the user can tell which, and a friendlier
     * label would be a guess about the one thing they have to read exactly.
     */
    RemotesSection.prototype.promptBox = function (remote) {
        const box = document.createElement("div");
        box.className = "settings-remote-prompt";

        const label = document.createElement("label");
        label.className = "settings-node-field";
        const text = document.createElement("span");
        text.textContent = remote.prompt.text;
        label.appendChild(text);

        const input = document.createElement("input");
        // A host-key question is not a secret and hiding it would make it
        // unanswerable next to the fingerprint it is asking about; everything
        // else is. One shared predicate -- this page and the picker each had
        // their own, and disagreed about fingerprints.
        input.type = window.PlexoraRemotes.isSecret(remote.prompt.text)
            ? "password" : "text";
        input.className = "form-control settings-remote-secret";
        input.autocomplete = "off";
        input.dataset.remote = remote.name;
        label.appendChild(input);
        box.appendChild(label);

        const send = document.createElement("button");
        send.type = "button";
        send.className = "btn btn-primary";
        send.textContent = "Send";
        const submit = () => {
            const value = input.value;
            input.value = "";
            this.answer(remote.name, remote.prompt.id, value);
        };
        send.addEventListener("click", submit);
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter") submit();
        });
        box.appendChild(send);
        return box;
    };

    RemotesSection.prototype.logBox = function (remote) {
        const details = document.createElement("details");
        details.className = "settings-remote-log";
        details.open = !!this.openLogs[remote.name];
        details.addEventListener("toggle", () => {
            this.openLogs[remote.name] = details.open;
        });
        const summary = document.createElement("summary");
        summary.textContent = "Connection log";
        details.appendChild(summary);
        const pre = document.createElement("pre");
        pre.textContent = remote.log.join("\n");
        details.appendChild(pre);
        return details;
    };

    RemotesSection.prototype.actions = function (remote) {
        const actions = document.createElement("div");
        actions.className = "settings-actions";
        const busy = isLive(remote.state);

        if (remote.state === "connected" && remote.url) {
            // A link the user clicks, not an automatic window.open: this is
            // reached from a poll callback, and every browser blocks a popup
            // that did not come from a gesture. A new tab rather than an
            // iframe because the remote Plexora is a whole separate origin
            // with its own session.
            const open = document.createElement("a");
            open.className = "btn btn-primary";
            open.href = remote.url;
            open.target = "_blank";
            open.rel = "noopener";
            open.textContent = "Open remote Plexora";
            actions.appendChild(open);
        }

        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = busy ? "btn btn-outline-light" : "btn btn-primary";
        toggle.textContent = busy ? "Disconnect" : "Connect";
        toggle.addEventListener("click", () => {
            if (busy) this.disconnect(remote.name);
            else this.connect(remote.name);
        });
        actions.appendChild(toggle);

        const edit = document.createElement("button");
        edit.type = "button";
        edit.className = "btn btn-outline-light";
        edit.textContent = "Edit";
        edit.addEventListener("click", () => this.edit(remote));
        actions.appendChild(edit);

        const forget = document.createElement("button");
        forget.type = "button";
        forget.className = "btn btn-outline-light";
        forget.textContent = "Forget";
        forget.addEventListener("click", () => this.forget(remote.name));
        actions.appendChild(forget);
        return actions;
    };

    /**
     * Connecting happens in the connection modal, here as everywhere else.
     *
     * The card behind it keeps its own prompt box on purpose: a question can
     * arrive at a card whose connection somebody opened from another surface
     * entirely, and a Settings page that showed "Needs your password" with
     * nowhere to type it would be a dead end. Two prompt surfaces, one
     * heuristic for whether the answer is a secret -- see PlexoraRemotes.
     */
    RemotesSection.prototype.connect = function (name) {
        return window.PlexoraConnectionModal.open({
            name: name,
            kind: window.PlexoraRemotes.KIND_VIEWER,
            intent: "Runs Plexora on that machine and brings the viewer to "
                    + "this browser through the SSH tunnel.",
        });
    };

    RemotesSection.prototype.disconnect = function (name) {
        return window.PlexoraRemotes.disconnect(name).catch(() => {});
    };

    RemotesSection.prototype.answer = function (name, id, value) {
        return window.PlexoraRemotes
            .answer(name, window.PlexoraRemotes.KIND_VIEWER, id, value)
            .catch(() => {});
    };

    RemotesSection.prototype.forget = function (name) {
        return window.PlexoraRemotes.forget(name).catch(() => {});
    };

    RemotesSection.prototype.edit = function (remote) {
        Object.keys(REMOTE_FIELDS).forEach((key) => {
            const input = el(REMOTE_FIELDS[key]);
            if (!input) return;
            const value = remote[key];
            input.value = Array.isArray(value) ? value.join("\n")
                : (value == null ? "" : String(value));
        });
        const useSrun = el("settings_remote_use_srun");
        // null means "no scheduler"; the empty string means "srun with your
        // site's defaults", which is a real and different choice.
        if (useSrun) useSrun.checked = remote.srun !== null && remote.srun !== undefined;
        const bind = el("settings_remote_bind_node");
        if (bind) bind.checked = !!remote.bind_node;
        const advanced = el("settings_remote_advanced");
        if (advanced) advanced.open = true;
        text(el("settings_remote_form_title"), "Edit “" + remote.name + "”");
        show(el("settings_remote_reset"), true);
        const nameInput = el("settings_remote_name");
        if (nameInput) nameInput.focus();
    };

    RemotesSection.prototype.clearForm = function () {
        Object.keys(REMOTE_FIELDS).forEach((key) => {
            const input = el(REMOTE_FIELDS[key]);
            if (input) input.value = "";
        });
        ["settings_remote_use_srun", "settings_remote_bind_node"].forEach((id) => {
            const box = el(id);
            if (box) box.checked = false;
        });
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
