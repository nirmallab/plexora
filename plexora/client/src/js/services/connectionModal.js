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

    //: The things a connection does, in order, with what each is called while
    //: it is happening. Two of them are conditional, and each is drawn only
    //: for a profile that actually does it: `queued` for one whose Plexora
    //: runs inside a scheduler job, `install` for one with "install or update
    //: Plexora" switched on. Drawing either unconditionally would promise a
    //: wait that is never coming.
    //:
    //: Installing sits where it does because that is where it runs -- after
    //: the login and before anything is launched, so that what starts
    //: afterwards is the version it just installed.
    const STEPS = [
        { state: "connecting", label: "Reaching the machine" },
        { state: "authenticating", label: "Signing in" },
        { state: "installing", label: "Installing Plexora", install: true },
        { state: "waiting_for_job", label: "Waiting for the scheduler",
          queued: true },
        { state: "tunneling", label: "Opening the tunnel" },
        { state: "waiting_for_app", label: "Starting Plexora" },
    ];

    //: What the install step is called when the launch command names an
    //: environment. The name comes from the server -- one reading of one field
    //: (`connect.environment_label`), the same one `pip` will be run through
    //: -- so a step cannot promise an environment the install does not write
    //: to. Without a name it stays the plain label: "in undefined" would be
    //: worse than saying nothing, and a `module load` line genuinely has no
    //: name to give.
    function installLabel(environment) {
        return environment ? "Installing Plexora in " + environment
                           : "Installing Plexora";
    }

    //: What the last step is called for a data node, which does not start a
    //: viewer at all. The step list is otherwise identical because the login
    //: is identical -- same profile, same ssh, same password.
    const NODE_LAST_STEP = "Starting the data node";

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
    function stepStates(state, kind, queued, lastOpening, install) {
        const steps = STEPS.filter((step) => (!step.queued || queued)
                                          && (!step.install || Boolean(install)));
        const failed = state === "failed" || state === "exited";
        const at = steps.findIndex(
            (step) => step.state === (failed ? lastOpening : state));
        return steps.map((step, index) => {
            let label = step.label;
            if (step.state === "waiting_for_app" && kind === "node") {
                label = NODE_LAST_STEP;
            } else if (step.state === "installing") {
                // `install` doubles as the environment's name when there is
                // one: the caller has a profile, not two separate answers, and
                // a second parameter that could only ever be set alongside
                // this one is a pair that can be passed inconsistently.
                label = installLabel(typeof install === "string" ? install : "");
            }
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
     *   which; `kind` -- "node" for a data node (everything in the app) or
     *   "viewer" for a tunnelled Plexora (which nothing in the app opens: see
     *   the note in views/settingsPage.js); `intent` -- one sentence saying
     *   why, shown under the title; `view: "recipes"` -- open straight on the
     *   presets, for a caller whose button said "Add a server" rather than
     *   "Connect".
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
        //: The log pane. Built once, repainted, and never replaced -- it owns
        //: a scroll position that this closure does not, and whether it is
        //: following its own output. See services/logTerminal.js.
        let logView = null;
        //: The last step this dialog actually saw running, so that a failure
        //: can be drawn against it. See stepStates.
        let lastOpening = null;
        //: "auto" -- the body follows the state, which is what it does for all
        //: of connecting. Anything else is a form somebody is typing into, and
        //: the second-by-second update must leave it alone: without this, a
        //: poll would replace the half-filled Add-a-server boxes with the list
        //: of machines every second.
        let view = "auto";

        function close(answer) {
            result = Object.assign(result, answer || {});
            if (unwatch) unwatch();
            unwatch = null;
            dialog.close();
        }

        // -- the two views -----------------------------------------------------

        /** No profile chosen yet: which machine is this? */
        function drawChooser(snapshot) {
            drawnActions = null;
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

            // **The body is built once and repainted, never rebuilt.** This
            // runs every second, and two things in it hold state the DOM owns
            // rather than this closure: the password box, mid-typing, and the
            // log pane's scroll position. Replacing the pane every tick threw
            // somebody reading the log back to wherever a fresh element starts
            // -- which looked like it was working, because the position it
            // reset to was usually the one they had just scrolled to.
            if (!stepsEl || stepsEl.parentNode !== parts.body) {
                parts.body.replaceChildren();
                parts.body.append(stepList(), phaseLine(), promptSlot(),
                                  errorSlot(), logPane());
                drawnPrompt = null;
            }
            paintSteps(state, entry, phase, error);
            // The prompt is redrawn only when it is a DIFFERENT question --
            // and cleared when there is none, so an answered one does not sit
            // there looking as though it still wants something.
            if (!prompt) {
                if (drawnPrompt !== null) promptEl.replaceChildren();
                drawnPrompt = null;
            } else if (drawnPrompt !== prompt.id) {
                drawPrompt(prompt);
                drawnPrompt = prompt.id;
            }
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
            // The environment's name when the profile has one, `true` when it
            // installs without naming one, `false` when it does not install.
            // See stepStates: one value, because "does it install" and "into
            // what" are one fact about one profile.
            const installing = !entry || !entry.install ? false
                : (entry.installEnv || true);
            stepStates(state, kind, entry ? entry.queued : false, lastOpening,
                       installing)
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
         * Everything about how it behaves is in services/logTerminal.js,
         * because the Settings page shows the same log for the same connection
         * and the two of them disagreeing about what a terminal does is how
         * this went wrong the first time.
         */
        function logPane() {
            logView = window.PlexoraLogTerminal.create({
                title: "Connection log",
                empty: "Waiting for the first line…",
            });
            return logView.element;
        }

        function paintLog(lines) {
            if (logView) logView.paint(lines);
        }

        //: Which set of buttons is on screen. Compared before rebuilding,
        //: because this runs every second and replacing the row would take the
        //: focus off whichever button somebody had tabbed to -- once a second,
        //: for the whole of a queued job.
        let drawnActions = null;

        function paintActions(state, error) {
            const opening = Remotes().isOpening(state);
            const shape = (state === "failed" || state === "exited") ? "failed"
                : (opening ? "opening" : "idle");
            if (shape === drawnActions) {
                if (error) showError(error);
                return;
            }
            drawnActions = shape;
            parts.actions.replaceChildren();

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
            view = "auto";
            drawnPrompt = null;
            lastOpening = null;
            // A new attempt is new output, and whoever pressed Try again is
            // asking to watch it rather than to stay where they had scrolled.
            if (logView) logView.follow();
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

        // -- adding a server ---------------------------------------------------
        //
        // A preset, then two or three boxes. What is being asked for is a
        // property of somebody else's cluster -- partition, walltime, whether
        // ssh into a compute node works -- and those answers are the same for
        // everybody who works there, so they are answered in advance and only
        // what genuinely differs is asked. The server composes and saves, so
        // there is one implementation of what a preset means.

        //: The catalogue, fetched once per dialog that asks for it. Not shipped
        //: in the page: this file is loaded on every page including the viewer,
        //: and the list is used on one page in a hundred.
        let recipes = null;
        //: What the walltime, cores and memory boxes start out saying. Comes
        //: down with the catalogue rather than being written here, so that the
        //: numbers on screen are the same constants the server splices into
        //: the srun line. The literals below are a fallback for an old server,
        //: not a second opinion.
        let recipeDefaults = { walltime: "4:00:00", cores: "16",
                               memory: "128G" };

        async function addServer() {
            view = "recipes";
            drawnActions = null;
            parts.title.textContent = "Add a server";
            parts.subtitle.textContent = "Start from the machine you use. You "
                + "can change any of it afterwards.";
            parts.body.replaceChildren();
            parts.actions.replaceChildren(
                button("btn btn-secondary", "Back", () => {
                    view = "auto";
                    drawChooser(Remotes().snapshot());
                }),
                el("div", "connect-modal-spacer"),
                button("btn btn-outline-light", "Cancel", () => close(null)));

            if (!recipes) {
                parts.body.append(el("p", "connect-modal-empty", "Loading…"));
                try {
                    const answer = await fetch(plexoraUrl("settings/recipes"));
                    const payload = await answer.json();
                    recipes = payload.recipes || [];
                    if (payload.defaults) recipeDefaults = payload.defaults;
                } catch (e) {
                    recipes = [];
                }
            }
            parts.body.replaceChildren();
            const list = el("div", "connect-recipes");
            recipes.forEach((recipe) => list.append(recipeCard(recipe)));
            parts.body.append(list);
        }

        function recipeCard(recipe) {
            const card = button("connect-recipe", null,
                                () => recipeForm(recipe));
            const head = el("div", "connect-recipe-head");
            head.append(el("span", "connect-recipe-label", recipe.label));
            if (recipe.unverified) {
                // Said plainly, on the card, before it is chosen. Presenting a
                // guess with the same confidence as a verified fact is how
                // somebody spends an afternoon on a partition that never
                // existed. Only on a preset that names a real cluster: "any
                // Slurm cluster" asserts nothing to have got wrong, and a
                // badge there would devalue the ones that need it.
                head.append(el("span", "connect-recipe-badge", "untested"));
            }
            card.append(head);
            card.append(el("span", "connect-recipe-blurb", recipe.blurb));
            return card;
        }

        function recipeForm(recipe) {
            view = "form";
            drawnActions = null;
            parts.title.textContent = recipe.label;
            parts.subtitle.textContent = recipe.blurb;
            parts.body.replaceChildren();

            const form = el("div", "connect-form");
            const boxes = {};

            function field(key, label, placeholder, initial, hint) {
                const wrap = el("label", "connect-field");
                wrap.append(el("span", "connect-field-label", label));
                const input = el("input", "form-control");
                input.type = "text";
                input.autocomplete = "off";
                input.spellcheck = false;
                input.value = initial == null ? "" : String(initial);
                if (placeholder) input.placeholder = placeholder;
                wrap.append(input);
                if (hint) wrap.append(el("span", "connect-field-hint", hint));
                boxes[key] = input;
                return wrap;
            }

            /**
             * A switch, shaped so the form's own grid places it.
             *
             * `.connect-form` is `repeat(auto-fit, minmax(14rem, 1fr))`, so
             * this is a cell like any other: it sits beside the box above it
             * wherever the dialog is wide enough for two columns, and wraps
             * under it where it is not. That is the whole of "inline when
             * there is room" -- there is no row here to be given one.
             */
            function switchField(key, label, hint, initial) {
                const wrap = el("label", "connect-field connect-switch");
                const text = el("span", "connect-switch-text");
                text.append(el("span", "connect-field-label", label));
                if (hint) text.append(el("span", "connect-field-hint", hint));
                const input = el("input", "connect-switch-input");
                input.type = "checkbox";
                input.checked = Boolean(initial);
                const track = el("span", "connect-switch-track");
                track.setAttribute("aria-hidden", "true");
                track.append(el("span", "connect-switch-thumb"));
                wrap.append(text, input, track);
                // Read by the same loop that reads every text box, so the
                // answers object stays one shape. `checked`, not `value`: the
                // server reads this key as a boolean (recipes.compose keeps
                // the switches out of the trim-to-string pass for exactly
                // this reason).
                boxes[key] = { get value() { return input.checked; } };
                return wrap;
            }

            // The three job numbers arrive FILLED IN, not as grey placeholder
            // text over an empty box. A default nobody can see is a default
            // nobody can correct, and on a multiplexed image these three are
            // the difference between an import that finishes and one the
            // scheduler kills partway through: a 40-channel pyramid is tens of
            // gigabytes before anything is drawn.
            const fields = [
                ["name", "Name this connection",
                 "A short name you will recognise", recipe.id],
                ["user", "Your username on that machine", "", ""],
                ["host", "Address", "login.cluster.edu", ""],
                ["walltime", "How long to keep it (walltime)",
                 recipeDefaults.walltime, recipeDefaults.walltime],
                ["cores", "CPU cores", recipeDefaults.cores,
                 recipeDefaults.cores],
                ["memory", "Memory", recipeDefaults.memory,
                 recipeDefaults.memory],
            ];
            fields.forEach(([key, label, placeholder, initial]) => {
                if (key !== "name" && recipe.ask.indexOf(key) < 0) return;
                form.append(field(key, label, placeholder, initial));
            });
            parts.body.append(form);

            if (recipe.notes && recipe.notes.length) {
                const notes = el("ul", "connect-notes");
                recipe.notes.forEach(
                    (note) => notes.append(el("li", null, note)));
                parts.body.append(notes);
            }

            // The same escape hatch the Settings form has, in the same words
            // and shut by default. A preset is a starting point and never a
            // lock -- but until this was here, correcting one meant saving it,
            // leaving the dialog, finding the server on another page and
            // editing it there, which is not an escape hatch but a detour.
            //
            // The job-options box holds this site's options MINUS the three
            // above it (the server's Recipe.srun_extra), so the two can never
            // contradict each other: a walltime box reading 4:00:00 above a
            // line reading `-t 8:00:00` would be two answers to one question,
            // and only one of them would be the one that ran.
            const advanced = el("details", "connect-advanced");
            advanced.append(el("summary", "connect-advanced-summary",
                               "Advanced — job options, launch command"));
            const advancedForm = el("div", "connect-form");
            if (recipe.srun !== null && recipe.srun !== undefined) {
                advancedForm.append(field(
                    "srun", "Other job options", "-p interactive",
                    recipe.srun_extra,
                    "Passed to srun as written. The walltime, cores and "
                    + "memory above are added to this line."));
            }
            advancedForm.append(field(
                "remote_command", "Plexora command or environment",
                "plexora", recipe.remote_command,
                "Set this if a login over SSH cannot find plexora — by a wide "
                + "margin the commonest reason a connection fails. An "
                + "environment path is enough."));
            // Next to the field that names the environment, because that is
            // the environment it writes to. Off on arrival for every preset:
            // no starting point gets to decide that software should be
            // installed into somebody's account on a machine it has only read
            // the documentation for.
            advancedForm.append(switchField(
                "install", "Install or update Plexora",
                "Runs pip install --upgrade plexora in that environment "
                + "before launching, and shows it in the connection log.",
                false));
            advanced.append(advancedForm);
            parts.body.append(advanced);

            parts.body.append(errorSlot());

            parts.actions.replaceChildren(
                button("btn btn-secondary", "Back", addServer),
                el("div", "connect-modal-spacer"),
                button("btn btn-outline-light", "Cancel", () => close(null)),
                button("btn btn-primary", "Save and connect", async () => {
                    const answers = {};
                    Object.keys(boxes).forEach((key) => {
                        const value = boxes[key].value;
                        answers[key] = typeof value === "string"
                            ? value.trim() : value;
                    });
                    try {
                        const saved = await saveRecipe(recipe.id, answers);
                        name = saved.remote.name;
                        // Straight into connecting: somebody who has just
                        // described a machine in order to read a file on it
                        // has not asked to be returned to a list.
                        await Remotes().refresh();
                        begin();
                    } catch (e) {
                        showError(e.message);
                    }
                }));
            if (boxes.user && boxes.user.focus) {
                setTimeout(() => boxes.user.focus(), 0);
            }
        }

        async function saveRecipe(id, answers) {
            const response = await fetch(
                plexoraUrl("settings/recipes/" + encodeURIComponent(id)), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(answers),
                });
            let payload = {};
            try {
                payload = await response.json();
            } catch (e) {
                payload = {};
            }
            if (!response.ok) {
                throw new Error(payload.error || "That server could not be saved.");
            }
            return payload;
        }

        // -- wiring ------------------------------------------------------------

        function update(snapshot) {
            if (!snapshot.loaded || view !== "auto") return;
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
        if (options.view === "recipes") {
            // Still watching, because saving a preset connects it and the
            // steps have to be there when it does -- but the first thing on
            // screen is the catalogue, not a list of machines somebody has
            // just told us they do not have.
            watch();
            addServer();
        } else if (name) begin();
        else watch();
        return promise;
    }

    return { open, STEPS, stepStates };
})();
