/**
 * connectionModal.js -- one place where connecting to another machine happens.
 *
 * Four entry points lead here and they used to lead four different places: the
 * Settings page's Connect button, the machine picker a data field opens, the
 * globe in the navbar, and a field flipped to Remote with nothing saved yet.
 * Each had its own idea of what to show while an ssh was starting, and two of
 * them showed nothing at all beyond a state word.
 *
 * What this shows, in one dialog:
 *
 * **Steps, from the server's own states.** Nothing is invented here --
 * `connecting`, `authenticating`, `waiting_for_job`, `tunneling`,
 * `waiting_for_app` are the five things a connection actually does, and the
 * scheduler step is drawn only for a profile that runs inside a job. A
 * spinner would say the same thing about a two-second login and a
 * quarter-hour queue, and it was the queue that made the old flow feel broken
 * when it was merely slow.
 *
 * **The log, as a terminal.** Whatever ssh said, monospace, pinned to the
 * bottom -- and it stays on screen when the connection fails, because the
 * actionable line is almost always in it. Already redacted server-side; this
 * adds no second redactor and needs none.
 *
 * **The question, verbatim.** A password, a Duo push, or the host-key
 * paragraph that wants "yes". Only the user can tell which, and rewriting it
 * into something friendlier would paraphrase the one thing they must read
 * exactly. It is masked only when it is a secret -- `PlexoraRemotes.isSecret`
 * decides, once, for every surface.
 *
 * **Closing is not cancelling.** A queued job is a real fifteen minutes and
 * the ssh belongs to the server, not to this dialog. "Continue in background"
 * leaves it running and it goes on showing up in Settings and in the globe;
 * "Stop connecting" is the button that actually ends it, and says so.
 *
 * No secret is stored. What the user types goes straight to
 * `PlexoraRemotes.answer` and the box is cleared in the same breath.
 */
window.PlexoraConnectionModal = (function () {
    "use strict";

    const Remotes = () => window.PlexoraRemotes;

    //: The five things a connection does, in order, with what each is called
    //: while it is happening. `queued` marks the step that only exists for a
    //: profile whose Plexora runs inside a scheduler job -- drawing it for a
    //: plain ssh host would promise a wait that is never coming.
    const STEPS = [
        { state: "connecting", label: "Reaching the machine" },
        { state: "authenticating", label: "Signing in" },
        { state: "waiting_for_job", label: "Waiting for the scheduler",
          queued: true },
        { state: "tunneling", label: "Opening the tunnel" },
        { state: "waiting_for_app", label: "Starting Plexora" },
    ];

    //: What the last step is called for a data node, which does not start a
    //: viewer at all. The step list is otherwise identical because the login
    //: is identical -- same profile, same ssh, same password.
    const NODE_LAST_STEP = "Starting the data node";

    //: How close to the bottom still counts as "at the bottom", in pixels. A
    //: browser's fractional scroll heights mean an exact comparison is false
    //: on half the machines that run this.
    const PIN_SLACK = 6;

    let openDialog = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function button(className, text, onClick) {
        const node = el("button", className, text);
        node.type = "button";
        if (onClick) node.addEventListener("click", onClick);
        return node;
    }

    /**
     * Which step the connection is on, and what has already happened.
     *
     * Derived from the current state rather than accumulated, because the
     * state is all the server reports and a client-side history would disagree
     * with it the first time a retry reused this dialog. Anything before the
     * current state in the list is done by definition: a connection cannot be
     * tunnelling without having signed in.
     *
     * `lastOpening` is the one exception, and it is only ever used to place a
     * failure. "failed" is not a step -- it is what happened to whichever step
     * was running -- and marking the whole list pending would throw away the
     * most useful thing on screen, which is how far it got.
     */
    function stepStates(state, kind, queued, lastOpening) {
        const steps = STEPS.filter((step) => !step.queued || queued);
        const failed = state === "failed" || state === "exited";
        const at = steps.findIndex(
            (step) => step.state === (failed ? lastOpening : state));
        return steps.map((step, index) => {
            const label = (step.state === "waiting_for_app"
                           && kind === "node") ? NODE_LAST_STEP : step.label;
            let status = "pending";
            if (state === "connected") status = "done";
            else if (at < 0) status = "pending";
            else if (index < at) status = "done";
            else if (index === at) status = failed ? "failed" : "active";
            return { label: label, status: status };
        });
    }

    // -- the dialog ----------------------------------------------------------

    function build() {
        const dialog = el("dialog", "connect-modal");

        const head = el("div", "connect-modal-head");
        const heading = el("div", "connect-modal-heading");
        const title = el("h2", "connect-modal-title", "Connect to a server");
        const subtitle = el("p", "connect-modal-subtitle");
        heading.append(title, subtitle);
        head.append(heading);
        dialog.append(head);

        const body = el("div", "connect-modal-body");
        dialog.append(body);

        const actions = el("div", "connect-modal-actions");
        dialog.append(actions);

        document.body.appendChild(dialog);
        return { dialog, title, subtitle, body, actions };
    }

    /**
     * @function open - connect to a machine, showing the whole of it.
     *
     * @param options `name` -- the saved profile to connect, or null to ask
     *   which; `kind` -- "node" for a data node (what a data field and the
     *   globe want) or "viewer" for a tunnelled Plexora (what Settings means
     *   by Connect); `intent` -- one sentence saying why, shown under the
     *   title.
     * @returns Promise<{connected, name, node, kind}>. `connected` is false
     *   for every way out that did not end connected, including closing the
     *   dialog on a connection that is still opening -- which keeps running.
     */
    function open(options = {}) {
        if (openDialog) {
            openDialog.close();
            openDialog = null;
        }
        const kind = options.kind === "viewer" ? "viewer" : "node";
        const intent = options.intent || "";
        const parts = build();
        const dialog = parts.dialog;
        openDialog = dialog;

        //: Which profile this dialog is about, or null while it is still the
        //: question. Set by choosing a row, and by `open({name})`.
        let name = options.name || null;
        let unwatch = null;
        let result = { connected: false, name: null, node: null, kind: kind };
        //: The question drawn right now, so a redraw does not replace the box
        //: somebody is halfway through typing a password into.
        let drawnPrompt = null;
        //: Whether the terminal is following the output. Set false the moment
        //: the user scrolls up to read something, true again when they scroll
        //: back down -- a log that yanks itself to the bottom while being read
        //: is worse than one that does not follow at all.
        let pinned = true;
        let terminal = null;
        //: The last step this dialog actually saw running, so that a failure
        //: can be drawn against it. See stepStates.
        let lastOpening = null;

        function close(answer) {
            result = Object.assign(result, answer || {});
            if (unwatch) unwatch();
            unwatch = null;
            dialog.close();
        }

        // -- the two views -----------------------------------------------------

        /** No profile chosen yet: which machine is this? */
        function drawChooser(snapshot) {
            parts.title.textContent = "Connect to a server";
            parts.subtitle.textContent = intent
                || "Pick the machine to open a connection to. Nothing has to "
                   + "be set up in advance.";
            parts.body.replaceChildren();
            parts.actions.replaceChildren();

            const entries = snapshot.entries || [];
            if (!entries.length) {
                parts.body.append(el("p", "connect-modal-empty",
                                     "No servers saved yet. Add one and "
                                     + "Plexora will handle the SSH for you."));
            } else {
                const list = el("ul", "connect-modal-list");
                entries.forEach((entry) => list.append(chooserRow(entry)));
                parts.body.append(list);
            }
            if (snapshot.error) parts.body.append(errorLine(snapshot.error));

            parts.actions.append(
                button("btn btn-secondary", "Add a new server", addServer),
                el("div", "connect-modal-spacer"),
                button("btn btn-outline-light", "Cancel", () => close(null)));
        }

        function chooserRow(entry) {
            const item = el("li", "connect-modal-row");
            const main = el("div", "connect-modal-row-main");
            main.append(el("span", "connect-modal-row-name", entry.label));
            main.append(el("span", "connect-modal-row-detail", entry.detail));
            item.append(main);

            const half = Remotes().half(entry, kind);
            const ready = kind === "node" ? Boolean(half.node)
                                          : half.state === "connected";
            const chip = el("span", "connect-modal-chip",
                            ready ? "Connected" : Remotes().label(half.state));
            chip.classList.add(ready ? "is-ready"
                : (Remotes().isOpening(half.state) ? "is-busy" : "is-idle"));
            item.append(chip);

            if (entry.queued && !ready) {
                item.append(el("div", "connect-modal-row-note",
                               "Runs Plexora inside a job, so connecting waits "
                               + "for the scheduler."));
            }
            item.append(button("btn btn-primary", ready ? "Use this" : "Connect",
                               () => {
                                   name = entry.name;
                                   if (ready) return settle(entry);
                                   begin();
                               }));
            return item;
        }

        /** A profile chosen: what is it doing? */
        function drawProgress(snapshot) {
            const entry = (snapshot.entries || [])
                .find((item) => item.name === name);
            const half = entry ? Remotes().half(entry, kind) : null;
            const deep = Remotes().focused(name, kind) || {};
            const state = (half && half.state) || deep.state || "connecting";
            const prompt = (half && half.prompt) || deep.prompt || null;
            const phase = (half && half.phase) || deep.phase || "";
            const error = (half && half.error) || deep.error || null;
            const log = (deep.log && deep.log.length)
                ? deep.log : ((half && half.log) || []);

            if (state === "connected" && entry) return settle(entry);
            if (Remotes().isOpening(state)) lastOpening = state;

            parts.title.textContent = "Connecting to “" + name + "”";
            // The address, not the intent: by now the user has chosen, and
            // what is worth confirming is which machine they chose.
            parts.subtitle.textContent = entry ? entry.detail : "";

            // The prompt box is the one thing a redraw must not destroy while
            // it is being typed into. Everything else is rebuilt each update,
            // which is what keeps the steps honest against the server.
            if (prompt && drawnPrompt === prompt.id
                    && parts.body.children.length) {
                paintSteps(state, entry, phase, error);
                paintLog(log);
                paintActions(state, error);
                return;
            }
            drawnPrompt = prompt ? prompt.id : null;

            parts.body.replaceChildren();
            parts.body.append(stepList(), phaseLine(), promptSlot(),
                              errorSlot(), logPane());
            paintSteps(state, entry, phase, error);
            if (prompt) drawPrompt(prompt);
            paintLog(log);
            paintActions(state, error);
        }

        // -- the pieces of the progress view -----------------------------------

        let stepsEl = null;
        let phaseEl = null;
        let promptEl = null;
        let errorEl = null;

        function stepList() {
            stepsEl = el("ol", "connect-steps");
            return stepsEl;
        }

        function phaseLine() {
            phaseEl = el("p", "connect-phase");
            return phaseEl;
        }

        function promptSlot() {
            promptEl = el("div", "connect-prompt-slot");
            return promptEl;
        }

        function errorSlot() {
            errorEl = el("div", "connect-modal-error");
            errorEl.setAttribute("role", "alert");
            errorEl.hidden = true;
            return errorEl;
        }

        function paintSteps(state, entry, phase, error) {
            stepsEl.replaceChildren();
            stepStates(state, kind, entry ? entry.queued : false, lastOpening)
                .forEach((step) => {
                    const item = el("li", "connect-step is-" + step.status);
                    item.append(el("span", "connect-step-mark"));
                    item.append(el("span", "connect-step-label", step.label));
                    stepsEl.append(item);
                });
            // The server's own sentence, not a translation of it: it is where
            // "a first start can take a few minutes while it loads" comes
            // from, and that is the line that stops a slow node reading as a
            // hung one.
            phaseEl.textContent = phase;
            phaseEl.hidden = !phase;
            errorEl.textContent = error || "";
            errorEl.hidden = !error;
        }

        function drawPrompt(prompt) {
            promptEl.replaceChildren();
            const box = el("form", "connect-prompt");
            const secret = Remotes().isSecret(prompt.text);
            // Verbatim and wrapped rather than truncated: on a host-key
            // question this text IS the fingerprint being checked, and it
            // arrives as several lines.
            box.append(el("div", "connect-prompt-text", prompt.text));

            const controls = el("div", "connect-prompt-controls");
            const field = el("input", "form-control");
            field.type = secret ? "password" : "text";
            field.autocomplete = "off";
            field.spellcheck = false;
            field.setAttribute("aria-label", secret ? "Your answer, hidden"
                                                    : "Your answer");
            controls.append(field);

            const send = (value) => {
                field.value = "";
                Remotes().answer(name, kind, prompt.id, value)
                    .catch((e) => showError(e.message));
            };
            if (!secret) {
                // The two answers ssh actually accepts, as buttons -- while
                // leaving the box, because some versions want the fingerprint
                // typed back and anything else it asks still has to be
                // answerable.
                controls.append(
                    button("btn btn-secondary", "Yes", () => send("yes")),
                    button("btn btn-secondary", "No", () => send("no")));
            }
            controls.append(button("btn btn-primary", "Send",
                                   () => send(field.value)));
            box.append(controls);
            box.addEventListener("submit", (event) => {
                if (event.preventDefault) event.preventDefault();
                send(field.value);
            });
            promptEl.append(box);
            if (field.focus) setTimeout(() => field.focus(), 0);
        }

        /**
         * The log, as a terminal that follows its own output.
         *
         * Pinned to the bottom while the user is at the bottom, and left alone
         * the moment they scroll up -- a pane that yanks itself back down
         * every second is unreadable exactly when there is something in it
         * worth reading.
         */
        function logPane() {
            const wrap = el("div", "connect-log");
            const head = el("div", "connect-log-head", "Connection log");
            terminal = el("pre", "connect-log-body");
            terminal.tabIndex = 0;
            terminal.setAttribute("role", "log");
            terminal.setAttribute("aria-live", "polite");
            terminal.addEventListener("scroll", () => {
                pinned = terminal.scrollTop + terminal.clientHeight
                    >= terminal.scrollHeight - PIN_SLACK;
            });
            wrap.append(head, terminal);
            return wrap;
        }

        function paintLog(lines) {
            if (!terminal) return;
            const text = (lines || []).join("\n");
            if (terminal.textContent === text) return;
            terminal.textContent = text;
            if (pinned) terminal.scrollTop = terminal.scrollHeight;
        }

        function paintActions(state, error) {
            parts.actions.replaceChildren();
            const opening = Remotes().isOpening(state);

            if (state === "failed" || state === "exited") {
                parts.actions.append(
                    button("btn btn-secondary", "Edit connection", editServer),
                    el("div", "connect-modal-spacer"),
                    button("btn btn-outline-light", "Close", () => close(null)),
                    button("btn btn-primary", "Try again", begin));
                return;
            }
            if (opening) {
                parts.actions.append(
                    button("btn btn-secondary", "Stop connecting", stopIt),
                    el("div", "connect-modal-spacer"),
                    // Not "Cancel". The ssh belongs to the server and a queued
                    // job is a real fifteen minutes; closing this window ends
                    // the watching, not the connection, and the button has to
                    // say which of those it does.
                    button("btn btn-outline-light", "Continue in background",
                           () => close(null)));
                return;
            }
            parts.actions.append(
                el("div", "connect-modal-spacer"),
                button("btn btn-outline-light", "Cancel", () => close(null)),
                button("btn btn-primary", "Connect", begin));
            if (error) showError(error);
        }

        function errorLine(message) {
            const line = el("div", "connect-modal-error", message);
            line.setAttribute("role", "alert");
            return line;
        }

        function showError(message) {
            if (!errorEl) return;
            errorEl.textContent = message;
            errorEl.hidden = !message;
        }

        // -- actions -----------------------------------------------------------

        /**
         * Start this connection, or attach to the one already running.
         *
         * Pressing Connect on something that is already connecting is not an
         * error and must not look like one: the profile may have been opened
         * from a second data field, or from Settings in another tab, and what
         * the user wants is to watch it -- which is what this dialog is. The
         * server would refuse the second POST with a 409, and reporting that
         * as a failure is how the old flow made "somebody else already started
         * it" look like a broken connection.
         */
        async function begin() {
            if (!name) return;
            drawnPrompt = null;
            lastOpening = null;
            pinned = true;
            watch();
            const known = Remotes().snapshot().loaded
                ? Remotes().snapshot() : await Remotes().refresh();
            if (live(known)) return;
            try {
                await Remotes().connect(name, kind);
            } catch (e) {
                // A refusal is usually "already connecting", which is not a
                // failure -- re-read before believing it.
                if (!live(Remotes().snapshot())) showError(e.message);
            }
        }

        /** Whether this profile's half of the connection is up or on its way. */
        function live(snapshot) {
            const entry = (snapshot.entries || [])
                .find((item) => item.name === name);
            if (!entry) return false;
            const half = Remotes().half(entry, kind);
            const ready = kind === "node" ? Boolean(half.node)
                                          : half.state === "connected";
            if (ready) {
                settle(entry);
                return true;
            }
            return Remotes().isOpening(half.state);
        }

        function stopIt() {
            if (!name) return close(null);
            // Ends the ssh AND the watching. "Stop connecting" is the one
            // button here that touches the connection, so it also finishes the
            // errand -- offering "Try again" to somebody who just said stop is
            // asking them the question they have answered.
            Remotes().disconnect(name, kind).catch(() => {});
            close(null);
        }

        function settle(entry) {
            const half = Remotes().half(entry, kind);
            close({ connected: true, name: entry.name, kind: kind,
                    node: half.node || null, label: entry.label,
                    detail: entry.detail });
        }

        function editServer() {
            close(null);
            window.location.href = plexoraUrl("settings#remotes");
        }

        function addServer() {
            // Stage 4 puts the recipes here. Until then, the page that already
            // knows how to add a server.
            close(null);
            window.location.href = plexoraUrl("settings#remotes");
        }

        // -- wiring ------------------------------------------------------------

        function update(snapshot) {
            if (!snapshot.loaded) return;
            if (name) drawProgress(snapshot);
            else drawChooser(snapshot);
        }

        function watch() {
            if (unwatch) unwatch();
            // `active` and `focus`: this dialog is open in front of somebody
            // AND it is showing one connection's whole log, which the list
            // payload does not carry.
            unwatch = Remotes().subscribe(update, {
                active: true,
                focus: name ? { name: name, kind: kind } : null,
            });
        }

        const promise = new Promise((resolve) => {
            dialog.addEventListener("close", () => {
                if (unwatch) unwatch();
                unwatch = null;
                dialog.remove();
                if (openDialog === dialog) openDialog = null;
                resolve(result);
            });
        });

        // Escape is a plain way out and means "stop showing me this", never
        // "kill the connection" -- same reading as Continue in background.
        dialog.addEventListener("cancel", () => close(null));
        dialog.showModal();
        if (name) begin();
        else watch();
        return promise;
    }

    return { open, STEPS, stepStates };
})();
