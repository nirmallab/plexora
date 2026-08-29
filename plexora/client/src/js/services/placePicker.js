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
    //: How often to ask a connection how it is getting on. An SSH login is a
    //: few seconds and a password prompt has a person on the other end of it,
    //: so this is a progress display rather than a race.
    const POLL_MS = 1500;

    //: States that mean "still on its way up", mirroring remote_sessions.
    //: OPENING_STATES. Anything not here is settled, one way or the other.
    const OPENING = ["connecting", "authenticating", "waiting_for_job",
                     "tunneling", "waiting_for_app"];

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    async function ask(url, options) {
        const response = await fetch(url, options);
        let payload = {};
        try {
            payload = await response.json();
        } catch (e) {
            payload = {};
        }
        if (!response.ok) {
            throw new Error(payload.error || "That machine could not be reached.");
        }
        return payload;
    }

    /** Every machine a file could be on right now. */
    async function places() {
        const payload = await ask(plexoraUrl("data_places"));
        return payload.places || [];
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
        let pollTimer = null;
        //: Which row is mid-connection, so a second click cannot start a
        //: second ssh to the same host while the first is still asking for a
        //: password.
        let opening = null;

        function finish(result) {
            answer = result;
            clearTimeout(pollTimer);
            dialog.close();
        }

        function fail(message) {
            error.textContent = message;
            error.hidden = false;
        }

        //: Which question is on screen right now. A poll must never eat a
        //: half-typed password: this list redraws every POLL_MS, and the input
        //: is inside the row being replaced. While the pending question is
        //: still the same question, the DOM is left exactly as it is.
        let drawnPrompt = null;

        function draw(entries) {
            const pending = entries.find((place) => place.prompt);
            if (pending && drawnPrompt === pending.prompt.id) return;
            drawnPrompt = pending ? pending.prompt.id : null;

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
            const busy = OPENING.includes(place.state);
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

            // The prompt is drawn inline rather than as a second dialog: it
            // belongs to this row, it arrives while the row is waiting, and a
            // password box that appears somewhere else reads as a phishing
            // attempt in the middle of somebody's import.
            if (place.prompt) item.append(promptRow(place));
            return item;
        }

        /**
         * Whether what ssh is asking for is a secret.
         *
         * Three kinds of question come through this one channel and only one
         * of them is confidential. A host-key confirmation -- "Are you sure
         * you want to continue connecting (yes/no/[fingerprint])?" -- is the
         * common one on a first connection, and masking it means somebody
         * types `yes` into a row of dots and cannot see what they typed, next
         * to a fingerprint they are being asked to check. The fingerprint is
         * public by construction; hiding the answer to it protects nothing.
         */
        function isSecret(text) {
            const lowered = String(text || "").toLowerCase();
            return !(/\(yes\/no/.test(lowered)
                     || lowered.includes("fingerprint")
                     || /\byes\b.*\bno\b/.test(lowered));
        }

        function promptRow(place) {
            const form = el("form", "place-picker-prompt");
            const secret = isSecret(place.prompt.text);
            // Verbatim, and wrapped rather than truncated: on a host-key
            // question the text IS the thing being checked, and it is several
            // lines of it.
            form.append(el("div", "place-picker-prompt-text", place.prompt.text));

            const field = el("input", "form-control");
            field.type = secret ? "password" : "text";
            field.autocomplete = "off";
            field.spellcheck = false;
            const controls = el("div", "place-picker-prompt-controls");
            controls.append(field);

            async function answer(value) {
                field.value = "";
                try {
                    await ask(plexoraUrl(
                        `settings/remotes/${encodeURIComponent(place.id)}`
                        + "/answer?kind=node"), {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ answer: value, id: place.prompt.id }),
                    });
                } catch (e) {
                    fail(e.message);
                }
            }

            if (secret) {
                controls.append(el("button", "btn btn-primary", "Send"));
            } else {
                // The two answers ssh actually accepts, as buttons -- while
                // leaving the box, because some versions want the fingerprint
                // typed back and anything else it asks still has to be
                // answerable.
                [["Yes", "yes"], ["No", "no"]].forEach(([label, value]) => {
                    const button = el("button", "btn btn-secondary", label);
                    button.type = "button";
                    button.addEventListener("click", () => answer(value));
                    controls.append(button);
                });
                controls.append(el("button", "btn btn-primary", "Send"));
            }
            form.append(controls);

            form.addEventListener("submit", (event) => {
                event.preventDefault();
                answer(field.value);
            });
            setTimeout(() => field.focus(), 0);
            return form;
        }

        async function connect(place) {
            error.hidden = true;
            opening = place.id;
            try {
                await ask(plexoraUrl(
                    `settings/remotes/${encodeURIComponent(place.id)}`
                    + "/connect?kind=node"), { method: "POST" });
            } catch (e) {
                // 409 among others: a connection for this profile is already
                // up or on its way. Not a failure to hide -- redraw, and the
                // row will show it as connected or connecting.
                opening = null;
                fail(e.message);
            }
            return tick();
        }

        /**
         * Read the list, draw it, and decide whether to look again.
         *
         * Polling follows the LIST rather than this dialog's own action. A
         * connection can be in flight because another field started it, or
         * because Settings did, and a row drawn once and never refreshed sits
         * frozen on "Connecting…" with a disabled button for as long as the
         * dialog is open -- which is exactly what happens when a form has an
         * image field and a mask field and the user works down it.
         */
        async function tick() {
            clearTimeout(pollTimer);
            let entries;
            try {
                entries = await places();
            } catch (e) {
                fail(e.message);
                return;
            }
            draw(entries);

            const watching = opening
                ? entries.find((place) => place.id === opening) : null;
            if (watching && !OPENING.includes(watching.state)
                    && !watching.prompt) {
                opening = null;
                if (watching.node) {
                    // Straight through rather than back to the list: the user
                    // pressed Connect in order to pick this machine, and making
                    // them press "Use this" afterwards is a step that exists
                    // only because the code has two states.
                    finish({ id: watching.id, kind: watching.kind,
                             label: watching.label, node: watching.node });
                    return;
                }
                if (watching.error) fail(watching.error);
            }

            if (entries.some((place) => OPENING.includes(place.state)
                                        || place.prompt)) {
                pollTimer = setTimeout(tick, POLL_MS);
            }
        }

        const refresh = tick;

        dialog.querySelector('[data-action="cancel"]')
            .addEventListener("click", () => finish(null));
        dialog.querySelector('[data-action="add"]')
            .addEventListener("click", () => {
                finish(null);
                window.location.href = plexoraUrl("settings#remotes");
            });

        const promise = new Promise((resolve) => {
            dialog.addEventListener("close", () => {
                clearTimeout(pollTimer);
                dialog.remove();
                resolve(answer);
            });
        });

        dialog.showModal();
        refresh();
        return promise;
    }

    return { pick, places };
})();
