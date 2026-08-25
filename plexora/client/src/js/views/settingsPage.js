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

    PlexoraPage.register(() => {
        wireRail();
        if (!el("settings_panel_data")) return null;
        const section = new DataSection();
        section.start();
        // The migration poll is the one thing here that outlives the markup: it
        // reschedules itself, so leaving the Settings page mid-migration would
        // otherwise keep asking the server about a job on behalf of a panel that
        // is no longer on screen. The job itself is the server's and carries on;
        // reopening Settings picks it up again through watch().
        return () => window.clearTimeout(section.polling);
    });
}());
