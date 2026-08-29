/**
 * placePicker.js -- "which machine?", asked once, in the middle of a form.
 *
 * The Remote half of a data field's Local/Remote switch. Local needs no
 * picking: there is exactly one machine the user is sitting at. Remote is a
 * question, and before this the only way to answer it was to have decided
 * before Plexora started -- `--serve kind:id=path` on a command line, or the
 * textareas that used to sit in Settings. Both ask somebody to name the file
 * they are about to go looking for.
 *
 * So the question is asked here instead, when it comes up. The list is the
 * saved SSH connections the user already has, plus -- when Plexora is itself
 * running somewhere else -- that machine. Choosing one that is not connected
 * opens the connection from inside this dialog, password prompt and all, and
 * hands back a data node the field can then browse. Nothing is reconfigured
 * and nothing restarts; the form is still open behind it.
 *
 * What comes back is `{id, kind, label, node}` or null. `node` is the name of
 * a registered data node, and it is the only part the field actually uses: it
 * is what /list_dir and /nodes/<name>/resources are addressed to. A place with
 * no node is the server's own filesystem, which needs no node to read.
 */
window.PlexoraPlacePicker = (function () {
    //: What is happening out there, and when to ask again, both belong to
    //: services/remoteState.js -- this dialog is one of four surfaces watching
    //: the same three processes, and it used to run its own timer against its
    //: own copy of the state list.
    const Remotes = () => window.PlexoraRemotes;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    /** Every machine a file could be on right now, freshly read. */
    async function places() {
        const snapshot = await Remotes().refresh();
        return snapshot.places || [];
    }

    /**
     * @function pick - open the list and resolve with a place, or null.
     * @param current - the id of the place already chosen, shown as selected.
     */
    function pick({ current = "" } = {}) {
        const dialog = el("dialog", "place-picker");
        dialog.innerHTML = `
            <div class="place-picker-body">
                <h2 class="place-picker-title">Where is this file?</h2>
                <p class="place-picker-intro">
                    Pick the machine the file is on. Saved connections are
                    opened when you choose one — nothing has to be set up in
                    advance.
                </p>
                <div class="place-picker-error" role="alert" hidden></div>
                <ul class="place-picker-list"></ul>
                <div class="place-picker-actions">
                    <button type="button" class="btn btn-secondary"
                            data-action="add">Add a new remote server…</button>
                    <button type="button" class="btn btn-secondary"
                            data-action="cancel">Cancel</button>
                </div>
            </div>
        `;
        const list = dialog.querySelector(".place-picker-list");
        const error = dialog.querySelector(".place-picker-error");
        document.body.appendChild(dialog);

        let answer = null;
        let unwatch = null;
        //: Which row is mid-connection, so a second click cannot start a
        //: second ssh to the same host while the first is still asking for a
        //: password.
        let opening = null;

        function finish(result) {
            answer = result;
            if (unwatch) unwatch();
            unwatch = null;
            dialog.close();
        }

        function fail(message) {
            error.textContent = message;
            error.hidden = false;
        }

        function draw(entries) {
            list.replaceChildren();
            if (!entries.length) {
                list.append(el("li", "place-picker-empty",
                               "No saved servers yet. Add one to read files "
                               + "from another machine."));
                return;
            }
            entries.forEach((place) => list.appendChild(row(place)));
        }

        function row(place) {
            const item = el("li", "place-picker-row");
            if (place.id === current) item.classList.add("is-current");
            const main = el("div", "place-picker-main");
            main.append(el("span", "place-picker-name", place.label));
            if (place.detail) {
                main.append(el("span", "place-picker-detail", place.detail));
            }
            item.append(main);

            const connected = place.kind === "server" || Boolean(place.node);
            const busy = Remotes().isOpening(place.state);
            const chip = el("span", "place-picker-state",
                            connected ? "Connected"
                                      : (busy ? (place.phase || "Connecting…")
                                              : "Not connected"));
            chip.classList.add(connected ? "is-ready"
                                         : (busy ? "is-busy" : "is-idle"));
            item.append(chip);

            if (place.error && !busy) {
                item.append(el("div", "place-picker-row-error", place.error));
            }

            // Two things worth knowing BEFORE pressing Connect, because both
            // cost something that is invisible from here.
            const notes = [];
            if (!connected && place.queued) {
                notes.push("This server runs Plexora inside a job, so "
                           + "connecting waits for the scheduler.");
            }
            if (place.viewer_state === "connected") {
                notes.push("You also have a viewer connection open to this "
                           + "server; this opens a second, separate one.");
            }
            if (notes.length) {
                item.append(el("div", "place-picker-note", notes.join(" ")));
            }

            const action = el("button", "btn btn-primary",
                              connected ? "Use this" : "Connect");
            action.type = "button";
            action.disabled = busy || (opening && opening !== place.id);
            action.addEventListener("click", () => {
                if (connected) {
                    finish({ id: place.id, kind: place.kind, label: place.label,
                             node: place.node || null });
                } else {
                    connect(place);
                }
            });
            item.append(action);
            return item;
        }

        /**
         * Hand the whole of connecting to the connection modal.
         *
         * This dialog asks one question -- which machine -- and used to answer
         * a second one badly: it grew a state chip, a password box and a
         * poller, none of which it could show as well as a surface built for
         * it, and all of which existed a second time in Settings. The modal
         * opens on top, so the list is still behind it: cancelling a
         * connection leaves the user where they were, choosing.
         */
        async function connect(place) {
            error.hidden = true;
            opening = place.id;
            const outcome = await window.PlexoraConnectionModal.open({
                name: place.id,
                kind: "node",
                intent: "Opening a data node on this machine, so a file on it "
                        + "can be named in the form behind.",
            });
            opening = null;
            if (outcome && outcome.connected) {
                finish({ id: outcome.name, kind: "remote",
                         label: outcome.label || place.label,
                         node: outcome.node || null });
                return;
            }
            // Not connected, and not a failure to report here -- the modal has
            // already said what happened, in more detail than a row can.
            draw((Remotes().snapshot().places) || []);
        }

        /**
         * Draw whatever the shared state now says.
         *
         * The dialog follows the LIST rather than its own action. A connection
         * can be in flight because another field started it, or because
         * Settings did, and a row drawn once and never refreshed sits frozen
         * on "Connecting…" with a disabled button for as long as the dialog is
         * open -- which is exactly what happens when a form has an image field
         * and a mask field and the user works down it.
         *
         * It does NOT finish on its own. Whether this dialog's errand
         * succeeded is the modal's answer, and a machine that came up because
         * somebody connected it in another tab is not an answer to the
         * question being asked here.
         */
        function update(snapshot) {
            if (snapshot.error) fail(snapshot.error);
            draw(snapshot.places || []);
        }

        dialog.querySelector('[data-action="cancel"]')
            .addEventListener("click", () => finish(null));
        dialog.querySelector('[data-action="add"]')
            .addEventListener("click", async () => {
                // Through the modal rather than to Settings: it is where a new
                // server is described now, and it connects the one it just
                // saved without a page load in between.
                const outcome = await window.PlexoraConnectionModal.open({
                    kind: "node",
                    intent: "Add the machine this file is on. Plexora will "
                            + "handle the SSH connection.",
                });
                if (outcome && outcome.connected) {
                    finish({ id: outcome.name, kind: "remote",
                             label: outcome.label || outcome.name,
                             node: outcome.node || null });
                }
            });

        const promise = new Promise((resolve) => {
            dialog.addEventListener("close", () => {
                if (unwatch) unwatch();
                unwatch = null;
                dialog.remove();
                resolve(answer);
            });
        });

        dialog.showModal();
        // `active`, because this dialog is open in front of somebody: a row
        // has to notice a connection another surface opened even while nothing
        // here is moving.
        unwatch = Remotes().subscribe(update, { active: true });
        return promise;
    }

    return { pick, places };
})();
