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

    /**
     * The rail, with the open section written into the URL as `#remotes`.
     *
     * Which section is open is worth keeping across a reload: half of this
     * page's work -- adding a remote, watching a migration, fixing a node --
     * ends in refreshing to see the result, and landing back on Data every
     * time meant re-finding the section before the answer could be read.
     *
     * The hash is where it goes rather than storage, because it is already the
     * browser's own answer to "where on this page was I": it survives a
     * reload, it makes /settings#nodes a link somebody can send, and the back
     * button keeps meaning "the page before this one" as long as the writes
     * are replaceState rather than pushState -- a tab is not a destination.
     */
    function wireRail() {
        const rail = el("settings_rail");
        if (!rail) return;
        const tabs = Array.from(rail.querySelectorAll(".settings-tab"));
        const panels = Array.from(document.querySelectorAll(".settings-panel"));

        function open(wanted) {
            // An unknown hash -- a renamed section, a typo, a link from an
            // older build -- opens the first tab rather than none of them.
            const tab = tabs.find((t) => t.dataset.section === wanted) || tabs[0];
            if (!tab) return;
            tabs.forEach((other) => {
                const active = other === tab;
                other.classList.toggle("is-active", active);
                other.setAttribute("aria-current", active ? "page" : "false");
            });
            panels.forEach((panel) => {
                panel.classList.toggle("is-active",
                    panel.dataset.section === tab.dataset.section);
            });
            return tab.dataset.section;
        }

        tabs.forEach((tab) => {
            tab.addEventListener("click", () => {
                const section = open(tab.dataset.section);
                if (!section) return;
                // replaceState rather than `location.hash = ...`: assigning to
                // the hash pushes a history entry, so clicking through three
                // tabs would take three presses of Back to leave the page.
                //
                // The existing state object is passed straight back through,
                // not dropped: appRouter.js marks its own entries with one, and
                // a tab click must not turn the page's entry into somebody
                // else's as far as the router's popstate handler is concerned.
                window.history.replaceState(window.history.state, "",
                    window.location.pathname + window.location.search + "#" + section);
            });
        });

        // A hash typed into the address bar, or arrived at by Back from
        // another page, moves the rail too -- neither reloads the document.
        const onHashChange = () => open(window.location.hash.replace(/^#/, ""));
        window.addEventListener("hashchange", onHashChange);

        open(window.location.hash.replace(/^#/, ""));

        // Window-level, so it outlives this markup and has to be handed back
        // for the router to drop -- see PlexoraPage.register below.
        return () => window.removeEventListener("hashchange", onHashChange);
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
    }

    RemotesSection.prototype.start = function () {
        this.drawCatalogue();
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
            empty.textContent = "No servers saved yet. Start from one of "
                + "the machines below.";
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

        // Which kind of machine, in two or three words. NOT the address:
        // see paintCard for what moved to the tooltip and why.
        const description = document.createElement("div");
        description.className = "settings-meta settings-remote-description";

        const phase = document.createElement("div");
        phase.className = "settings-meta";
        phase.hidden = true;

        // Only ever shown for a connection running inside a scheduled job --
        // see paintCard. A login-node connection has no clock, and a row that
        // said "unlimited" would be inventing a fact about somebody's site.
        // It is the last row this card has that is about the CONNECTION
        // rather than about the machine, and it is here because a job that is
        // about to end is a thing you have to be told without asking.
        const clock = document.createElement("div");
        clock.className = "settings-meta settings-remote-clock";
        clock.hidden = true;

        const error = document.createElement("div");
        error.className = "settings-notice settings-notice-error";
        error.setAttribute("role", "alert");
        error.hidden = true;

        // What a VM button just did, kept OUT of `error` on purpose: this card
        // is repainted on every poll, and a message written into the slot the
        // connection's own error lives in would be wiped a second later by a
        // repaint that had nothing to say.
        const notice = document.createElement("div");
        notice.className = "settings-notice";
        notice.setAttribute("role", "status");
        notice.hidden = true;

        // "You called this Windows; it answered as Linux." A notice rather
        // than an error, and separate from `error` for the same reason
        // `notice` is: the connection this is about SUCCEEDED, so writing it
        // into the slot a failure lives in would read as one.
        const osNote = document.createElement("div");
        osNote.className = "settings-notice";
        osNote.setAttribute("role", "status");
        osNote.hidden = true;

        const promptSlot = document.createElement("div");

        const card = { root, description, phase, clock,
                       osNote, error, notice, promptSlot, name: name,
                       drawnPrompt: null };
        root.append(this.buildHead(card), description, phase,
                    clock, osNote, error, notice, promptSlot,
                    this.buildLog(card), this.buildActions(card));
        return card;
    };

    /**
     * The name, the two things you can do TO this saved server, and a dot.
     *
     * Edit and Delete are icons rather than buttons because they are not what
     * this card is for: it is a status board and a Connect button, and two
     * more full-width buttons underneath the one that matters is how a card
     * stops having a primary action. They keep their words on `title` and
     * `aria-label`, which is where a tooltip and a screen reader both look --
     * an icon with neither is a control nobody can name.
     *
     * The dot carries no text of its own. It shares the `settings-node-state`
     * classes with every other machine on this page, so the colour has one
     * definition, and the word ("Connected", "Needs your password") is on its
     * label; the states that actually need reading -- a prompt, a failure --
     * put their own sentence on the card underneath.
     */
    RemotesSection.prototype.buildHead = function (card) {
        const head = document.createElement("div");
        head.className = "settings-remote-head";

        const title = document.createElement("div");
        title.className = "settings-field-label settings-remote-name";
        title.textContent = card.name;
        // The whole of it, for the name too long for a third of a column.
        title.title = card.name;

        const tools = document.createElement("div");
        tools.className = "settings-remote-tools";

        const chip = document.createElement("span");
        chip.className = "settings-node-state settings-remote-dot";
        chip.setAttribute("role", "img");

        tools.append(
            iconButton("fa-pencil", "Edit",
                       () => this.edit(card.remote)),
            // Marked destructive, and only on hover: the two icons sit eight
            // pixels apart and look alike until one of them is pointed at,
            // which is exactly the moment the difference matters.
            iconButton("fa-trash", "Delete",
                       () => this.forget(card.name, card), "is-danger"),
            chip);

        card.chip = chip;
        head.append(title, tools);
        return head;
    };

    /**
     * A control that is an icon and a tooltip and nothing else.
     *
     * `title` and `aria-label` both, deliberately: a tooltip is not an
     * accessible name and a name is not a tooltip, and this control has no
     * text for either to fall back on.
     */
    function iconButton(icon, label, onClick, tone) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "settings-icon-button" + (tone ? " " + tone : "");
        btn.title = label;
        btn.setAttribute("aria-label", label);
        const glyph = document.createElement("span");
        glyph.className = "fas " + icon;
        glyph.setAttribute("aria-hidden", "true");
        btn.append(glyph);
        btn.addEventListener("click", onClick);
        return btn;
    }

    RemotesSection.prototype.paintCard = function (card, entry, remote) {
        card.remote = remote;
        // The NODE half. Connecting from this page opens a data node on that
        // machine and leaves Plexora here -- see connect() for why there is no
        // longer a second kind of connection to choose between.
        const half = entry.node;
        const live = Boolean(half.node) || isLive(half.state);
        const state = half.node ? "connected" : half.state;

        const word = window.PlexoraRemotes.label(state);
        card.chip.className =
            "settings-node-state settings-remote-dot is-" + state;
        card.chip.title = word;
        card.chip.setAttribute("aria-label", word);

        card.gcloud = entry.gcloud || null;

        // Which kind of machine, in the recipe's own two or three words --
        // "Slurm compute cluster", "Google Cloud VM" -- and nothing else.
        //
        // What stood here, at various times, was the address, the machine
        // type, the bucket, the operating system, the environment pip writes
        // to, what becomes of the VM at the end and what the connection is
        // serving files as. Every one of those is configuration, and a card
        // is not where configuration is read: by the time a profile is saved,
        // the person looking at it chose all of that months ago and wants to
        // know which machine this is and whether it is up. They are asked for,
        // and shown again, on the form behind the pencil.
        //
        // The tooltip is the same sentence, not a longer one: the line is
        // clamped to two, so a description that does not fit is still
        // readable, and hovering a card is not a way to see round the back
        // of it.
        const says = entry.description || entry.detail || "";
        card.description.textContent = says;
        card.description.title = says;

        card.phase.textContent = half.phase || "";
        card.phase.hidden = !half.phase;

        // The machine's own answer, when it contradicts what this profile
        // says. Shown on a connection that is up, because that is when it can
        // be known -- and worth showing then rather than at the next failure,
        // which is where it would otherwise surface.
        const wrongOs = half.osMismatch;
        card.osNote.hidden = !wrongOs;
        if (wrongOs) {
            card.osNote.textContent =
                "This says " + (OS_WORDS[wrongOs.expected] || wrongOs.expected)
                + ", but the machine reports "
                + (OS_WORDS[wrongOs.found] || wrongOs.found)
                + ". The connection is fine; edit this server and set its "
                + "operating system so the next one is built for the right "
                + "shell.";
        }

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
                : "Job time has run out — connect again.";
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
        actions.className = "settings-actions settings-remote-actions";

        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.addEventListener("click", () => {
            if (card.live) this.disconnect(card.name);
            else this.connect(card.name);
        });

        // Only ever shown for a connection that RENTS its machine -- see
        // paintActions. Disconnect leaves that machine running on purpose,
        // because reconnecting to it is then instant; these are the two other
        // answers to "I am finished", and they cost different things.
        const startVm = document.createElement("button");
        startVm.type = "button";
        startVm.className = "btn btn-outline-light";
        startVm.textContent = "Start VM";
        startVm.hidden = true;
        startVm.addEventListener("click", () => this.startVm(card));

        const stopVm = document.createElement("button");
        stopVm.type = "button";
        stopVm.className = "btn btn-outline-light";
        stopVm.textContent = "Stop VM";
        stopVm.hidden = true;
        stopVm.addEventListener("click", () => this.stopVm(card));

        const deleteVm = document.createElement("button");
        deleteVm.type = "button";
        deleteVm.className = "btn btn-outline-light";
        deleteVm.textContent = "Delete VM…";
        deleteVm.hidden = true;
        deleteVm.addEventListener("click", () => this.deleteVm(card));

        // Connect, and then -- for the one kind of profile that rents its
        // machine -- the two buttons that end the renting. Nothing else: Edit
        // and Delete are icons up in the head, so a card at rest has exactly
        // one thing to press.
        actions.append(toggle, startVm, stopVm, deleteVm);
        card.toggle = toggle;
        card.startVm = startVm;
        card.stopVm = stopVm;
        card.deleteVm = deleteVm;
        return actions;
    };

    RemotesSection.prototype.paintActions = function (card, live) {
        const was = card.live;
        card.live = live;
        if (card.gcloud) {
            if (live) {
                // A connected session is proof the machine is up, and it is
                // proof that arrives without asking Google anything.
                card.vmState = "RUNNING";
            } else if (was) {
                // It just disconnected, which for a profile that stops its VM
                // means the machine is on its way down. Nothing else on this
                // page would ever notice that finishing.
                this.recheckVm(card);
            }
        }
        const wanted = live ? "Disconnect" : "Connect";
        if (card.toggle.textContent !== wanted) {
            card.toggle.textContent = wanted;
            card.toggle.className = live ? "btn btn-outline-light"
                                         : "btn btn-primary";
        }
        const cloud = Boolean(card.gcloud);
        // Which of Start and Stop to offer depends on what the machine is
        // actually doing, which is why the card asks. Before it knows -- and
        // if asking failed, which a project without Compute permission will do
        // -- it offers Stop and not Start: that is what the card did before it
        // could ask at all, and a button that cannot work is better than a
        // page that has silently lost one.
        const state = card.vmState || "";
        const stopped = ["TERMINATED", "STOPPED", "SUSPENDED"]
            .indexOf(state) >= 0;
        const gone = state === "missing";
        card.startVm.hidden = !cloud || !stopped;
        card.stopVm.hidden = !cloud || stopped || gone;
        // Delete is offered only for a machine Plexora made. The server
        // refuses the other case twice over -- on the saved record and on the
        // label written to the instance itself -- and a button that is only
        // ever going to be refused should not be on the page at all. Nor is
        // there anything to delete when there is no VM.
        card.deleteVm.hidden = !cloud
            || card.gcloud.vm_source === "existing"
            || gone;
        if (cloud) {
            // A live session moves `vmState` above without anybody asking
            // Google, so the words on the buttons are re-derived here rather
            // than only where the answer arrives.
            this.paintVmState(card);
            this.askVmState(card);
        }
    };

    //: How each operating system is spelled where a person reads it. The
    //: stored values are lower-case identifiers -- they travel to a command
    //: line, not to a label -- and "macOS" in particular is not something a
    //: capitalise-the-first-letter rule would ever produce.
    var OS_WORDS = {
        windows: "Windows", macos: "macOS", linux: "Linux",
    };

    //: How a Compute Engine status reads where a person reads it.
    //: "missing" is not a failure: a profile whose VM has been deleted is a
    //: perfectly good profile, and connecting it again simply makes another.
    var VM_STATE_WORDS = {
        RUNNING: "running", TERMINATED: "stopped", STOPPED: "stopped",
        SUSPENDED: "suspended", STAGING: "starting", PROVISIONING: "starting",
        STOPPING: "stopping", REPAIRING: "repairing",
        missing: "no VM yet",
    };

    /**
     * Ask what the VM is doing -- once, and never from the poll.
     *
     * The connection list is re-read every second while anything is
     * happening. A Compute Engine round trip inside that loop would be a
     * gcloud subprocess per cloud profile per second for as long as somebody
     * had this page open, which is not a status display; it is a bill. So the
     * answer is fetched once per card and then only when something this page
     * did could have changed it.
     */
    RemotesSection.prototype.askVmState = function (card, force) {
        if (card.vmAsking) return;
        if (card.vmAsked && !force) return;
        card.vmAsking = true;
        card.vmAsked = true;
        window.PlexoraRemotes.vmStatus(card.name)
            .then((payload) => {
                card.vmState = (payload && payload.status) || "";
                // The buttons are chosen from this, and the answer arrived
                // after the paint that asked for it.
                this.paintActions(card, card.live);
                this.paintVmState(card);
            })
            .catch(() => { card.vmState = ""; })
            .finally(() => { card.vmAsking = false; });
    };

    /**
     * Put the machine's state on the buttons that change it.
     *
     * It had a line of its own on the card -- "VM stopped", "VM no VM yet" --
     * which is a fact about somebody's Compute Engine project sitting on a
     * card that is meant to say which machine this is and whether it is up.
     * It is not lost: WHICH of Start and Stop is offered already says which
     * way the machine is, this names it in words for the tooltip and the
     * screen reader, and it is the same word from the same map as before.
     */
    RemotesSection.prototype.paintVmState = function (card) {
        const word = VM_STATE_WORDS[card.vmState] || "";
        const says = word ? "This machine is " + word + "." : "";
        [card.startVm, card.stopVm, card.deleteVm].forEach((btn) => {
            if (btn) btn.title = says;
        });
    };

    /**
     * Stop the rented machine. Its disk, and everything in the bucket, stay.
     *
     * No confirmation: stopping is reversible, costs nothing but the time to
     * start it again, and the environment the first connection built is still
     * on the disk when it comes back.
     */
    /**
     * Start the machine without connecting to it.
     *
     * Connecting already starts a stopped VM, so this is not the only way up.
     * It is the way up for somebody who wants it warm before they need it --
     * and it matters more than it would have yesterday, because stopping on
     * disconnect is the default now and stopped is where one of these
     * profiles rests.
     *
     * No confirmation: it costs a minute and starts the meter on a machine
     * the user has already agreed to rent.
     */
    RemotesSection.prototype.startVm = function (card) {
        return window.PlexoraRemotes.vmStart(card.name)
            .then((payload) => this.sayVm(card, payload))
            .catch((e) => this.sayVmError(card, e));
    };

    RemotesSection.prototype.stopVm = function (card) {
        return window.PlexoraRemotes.vmStop(card.name)
            .then((payload) => this.sayVm(card, payload))
            .catch((e) => this.sayVmError(card, e));
    };

    /**
     * Delete the rented machine -- and say, in the question, what survives.
     *
     * The one thing somebody needs to be certain of before pressing this is
     * that their data is not what is being deleted, so the confirmation says
     * it outright rather than asking "are you sure?". It is true by
     * construction: `plexora.gcloud` has no way to delete storage at all.
     */
    RemotesSection.prototype.deleteVm = function (card) {
        const cloud = card.gcloud || {};
        const asked = window.confirm(
            "Delete the VM “" + (cloud.vm_name || card.name) + "”?\n\n"
            + "Your bucket gs://" + (cloud.bucket || "") + " and everything in "
            + "it are untouched — Plexora never deletes storage.\n\n"
            + "This also ends the disk charge a stopped VM keeps costing.\n\n"
            + "This connection stays saved. Connecting again creates a new VM "
            + "against the same bucket.");
        if (!asked) return Promise.resolve();
        return window.PlexoraRemotes.vmDelete(card.name)
            .then((payload) => this.sayVm(card, payload))
            .catch((e) => this.sayVmError(card, e));
    };

    RemotesSection.prototype.sayVm = function (card, payload) {
        // In the card's own notice slot rather than an alert: it is beside the
        // button that was pressed, and it does not have to be dismissed before
        // the next thing can happen.
        card.notice.classList.remove("settings-notice-error");
        card.notice.textContent = (payload && payload.message) || "";
        card.notice.hidden = !card.notice.textContent;
        // The verb reports where it is going -- STAGING, STOPPING -- and the
        // card shows that immediately rather than the state from before the
        // button was pressed. Google is asked again shortly, because these
        // are the transitions that take a minute and nothing else on this
        // page will notice them ending.
        if (payload && payload.status) {
            card.vmState = payload.status;
            this.paintActions(card, card.live);
            this.paintVmState(card);
        }
        this.recheckVm(card);
        return this.refresh();
    };

    //: How long to wait before asking Google what came of a start or a stop.
    //: Long enough for the transition to have finished in the ordinary case,
    //: and it is only ever one extra call per press.
    var VM_RECHECK_MS = 45000;

    RemotesSection.prototype.recheckVm = function (card) {
        if (card.vmRecheck) window.clearTimeout(card.vmRecheck);
        card.vmRecheck = window.setTimeout(() => {
            card.vmRecheck = null;
            this.askVmState(card, true);
        }, VM_RECHECK_MS);
    };

    RemotesSection.prototype.sayVmError = function (card, error) {
        card.notice.classList.add("settings-notice-error");
        card.notice.textContent = error.message || "That did not work.";
        card.notice.hidden = false;
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
     * The presets, on the page, where a second form used to be.
     *
     * This section used to carry a hand-written "add a server" form beside a
     * button that opened the catalogue. The two asked the same questions --
     * name, launch command, install, cores, memory, walltime, job options --
     * with two sets of boxes, two sources for the same three defaults and one
     * test whose only job was keeping them in step. The catalogue asks them
     * with the site's answers already filled in, so the form was the half to
     * retire; the three things only it had (a data directory, extra ports, and
     * the bind-to-node switch) moved into the preset form's Advanced.
     *
     * The cards come from connectionModal.js rather than being drawn again
     * here: one grid, one card, one request for a catalogue that never
     * changes. Choosing one opens the dialog on that preset's form.
     */
    RemotesSection.prototype.drawCatalogue = function () {
        const slot = el("settings_remote_catalogue");
        if (!slot || !window.PlexoraConnectionModal.recipeGrid) return;
        window.PlexoraConnectionModal
            .recipeGrid((recipe) => this.openRecipe(recipe.id, null))
            .then((grid) => slot.replaceChildren(grid))
            .catch(() => {});
    };

    /**
     * One preset's form: empty for a new machine, filled in for a saved one.
     *
     * `remote` is the profile from the poll, whole -- the dialog reads the
     * address, the job line and the switches out of it. Which preset that is
     * was decided by the server (`recipes.for_remote`), including for every
     * profile written before a profile recorded which preset made it.
     */
    RemotesSection.prototype.openRecipe = function (id, remote) {
        return window.PlexoraConnectionModal.open({
            kind: window.PlexoraRemotes.KIND_NODE,
            view: "recipe",
            recipe: id,
            remote: remote,
            intent: "Start from the machine you use. You can change any of it "
                    + "afterwards.",
        });
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

    /**
     * Drop the saved connection. For a rented machine, say what that leaves.
     *
     * Forgetting a profile has never touched anything on the far side, and for
     * every other kind of connection that is obvious -- the machine was
     * somebody else's before Plexora heard of it. For a VM Plexora created it
     * is not obvious at all, and the mistake it invites is expensive in the
     * quiet direction: an instance still running, still billing, with nothing
     * left on this page that knows its name. So the question says so, and
     * points at the button that does end it.
     *
     * Every other kind asks too, which it did not use to. That was defensible
     * while this was a button in a row of buttons with the word "Forget" on
     * it; it is not defensible for a trash icon beside a pencil, where the
     * whole distance between "edit this" and "delete this" is about eight
     * pixels of mouse travel and no words at all.
     */
    RemotesSection.prototype.forget = function (name, card) {
        const cloud = card && card.gcloud;
        if (!cloud) {
            const asked = window.confirm(
                "Delete “" + name + "”?\n\nPlexora forgets how to reach this "
                + "machine. Nothing on the machine itself is touched, and you "
                + "can add it again at any time.");
            if (!asked) return Promise.resolve();
        }
        if (cloud) {
            const own = cloud.vm_source === "existing";
            const asked = window.confirm(
                "Forget “" + name + "”?\n\n"
                + (own
                    ? "Your VM “" + (cloud.vm_name || name) + "” is left "
                      + "exactly as it is — Plexora never stops or deletes a "
                      + "machine it did not create.\n\n"
                    : "The VM “" + (cloud.vm_name || name) + "” is left as it "
                      + "is, and a stopped one keeps billing for its disk — "
                      + "use “Delete VM…” first if you are finished with "
                      + "it.\n\n")
                + "Your bucket gs://" + (cloud.bucket || "") + " is untouched "
                + "either way.");
            if (!asked) return Promise.resolve();
        }
        return window.PlexoraRemotes.forget(name).catch(() => {});
    };

    RemotesSection.prototype.edit = function (remote) {
        if (!remote || !remote.name) return;
        // Straight back to the form this profile was described in. What used
        // to be here filled a second, hand-written form in from the same
        // fields -- see drawCatalogue for why that form is gone.
        this.openRecipe(remote.recipe || "ssh", remote);
    };

    PlexoraPage.register(() => {
        const unwireRail = wireRail();
        if (!el("settings_panel_data")) return unwireRail || null;
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
            if (unwireRail) unwireRail();
        };
    });
}());
