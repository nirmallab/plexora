/**
 * "Which file, and which machine is it on?" -- as one row, once.
 *
 * Plexora asks this question in several places and it is never as small as it
 * looks, because there are three different answers and they need three
 * different mechanics:
 *
 *   a disk the server can open   the path goes over as a path, and the bytes
 *                                never touch this browser
 *   a data node                  the bytes are read through the node relay
 *                                (POST /fetch_file) and posted as an upload
 *   this computer, unattached    the browser's own bytes, through Upload…
 *
 * On a cluster the third is often the only one that works and the first is
 * the only one that scales, so a field that offers one of them is a field
 * that half the users cannot use. This row offers all three and settles which
 * applies from the Local/Remote switch plus whether Plexora is running here
 * -- the same derivation services/dataLocation.js makes, in the same words.
 *
 * WHAT IT DOES NOT DO is read the file. It hands its caller either a path the
 * server can open or a `File` of bytes, and what that means is the caller's:
 * a marker list, a gene-group table and a transform matrix are three
 * different parses and none of them belongs in a row of controls. Nothing
 * here knows a CSV from a spreadsheet.
 *
 * Lifted out of views/channelNamesUpload.js, which is where this was first
 * written and which is still the fullest example of what it is for. The
 * classes are that dialog's, deliberately: `.data-location`, `.import-field-row`
 * and the rest are already styled and already the shape every path field in
 * Plexora has.
 *
 * Served as a classic script (see base.html).
 */
window.PlexoraFileSourceRow = (function () {
    "use strict";

    //: The two sides of the switch, spelled as dataLocation.js spells them --
    //: they end up in the same aria-labels and the same CSS.
    const LOCAL = "local";
    const REMOTE = "remote";

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function button(className, text, onClick) {
        const node = el("button", className, text);
        node.type = "button";
        node.addEventListener("click", onClick);
        return node;
    }

    /**
     * Whether the machine running Plexora is a different machine from this one.
     *
     * The only question that decides whether Upload is worth offering. Asked
     * of PlexoraDataLocation, which is where every other data field asks it,
     * rather than of flaskVariables directly -- a registered client node is
     * itself proof and that folding lives in one place.
     */
    function serverIsElsewhere() {
        const location = window.PlexoraDataLocation;
        return Boolean(location && location.serverIsRemote());
    }

    //: The data node `plexora connect` started on the browser's own machine,
    //: when there is one. It is what makes "this computer" nameable by path
    //: from a Plexora running elsewhere.
    function clientNodeName() {
        const location = window.PlexoraDataLocation;
        return (location && location.clientNode && location.clientNode()) || "";
    }

    /**
     * Every machine this file could be read from right now, or null when the
     * list could not be got at all.
     *
     * Null rather than an empty array, and the difference decides what
     * happens next: "nothing is connected" is answered by opening a
     * connection, and "I could not ask" is answered by the picker, which says
     * so properly.
     */
    async function reachablePlaces() {
        try {
            const list = await window.PlexoraPlacePicker.places();
            return list.filter((place) => place.kind === "server"
                                          || place.node
                                          || place.registered_node);
        } catch (error) {
            return null;
        }
    }

    /**
     * Build the row.
     *
     * @param options.accept      - what the browser's file chooser offers
     * @param options.filter      - the Browse dialog's filter name
     * @param options.placeholder - example path for the empty box
     * @param options.label       - the field's own label, or "" for none
     * @param options.actionLabel - the button that means "use this one"
     * @param options.intent      - why a connection would be opened, for the
     *                              connection modal when nothing is reachable
     * @param options.onChoose    - `({file, path}) => Promise`. Exactly one of
     *                              the two is set: `path` for a file the
     *                              server can open itself, `file` for bytes.
     *                              Rejecting shows the message on the row.
     * @returns `{element, reset, focus, busy}`
     */
    function create(options = {}) {
        const state = { where: LOCAL, place: null, path: "", busy: false };

        const wrap = el("div", "file-source");
        if (options.label) {
            const label = el("label", "file-source-label", options.label);
            if (options.id) label.htmlFor = options.id;
            wrap.appendChild(label);
        }

        const row = el("div", "import-field-row");
        const input = el("input", "form-control");
        input.type = "text";
        if (options.id) input.id = options.id;
        input.autocomplete = "off";
        input.spellcheck = false;
        input.placeholder = options.placeholder || "/path/to/file.csv";

        const browse = el("button", "browse-button", "Browse…");
        browse.type = "button";
        // Which machine's dialog to open, asked at CLICK time rather than
        // wired once: the switch below can be flipped long after this button
        // was built, and then the same button means a different filesystem.
        if (typeof attachBrowseButton === "function") {
            attachBrowseButton(browse, input, {
                mode: "file",
                filter: options.filter || "",
                node: () => nodeName() || null,
            });
        }

        const load = button("btn btn-primary file-source-load",
                            options.actionLabel || "Load", () => choose());
        load.disabled = true;

        // "Which machine is this file on?", in the switch every other path
        // field in Plexora asks it with. One letter a side, for the reason
        // dataLocation gives: this sits inside a row that already has a box
        // and three buttons in it.
        const location = el("div", "data-location");
        const group = el("div", "data-location-toggle");
        group.setAttribute("role", "radiogroup");
        group.setAttribute("aria-label", "Where this file is");
        group.setAttribute("data-tooltip", "Data Location — (L)ocal | (R)emote");
        const sides = {};
        [[LOCAL, "L", "Local — this computer"],
         [REMOTE, "R", "Remote — another machine"]].forEach(
            ([value, letter, described]) => {
                const side = button("data-location-option", letter,
                                    () => press(value));
                side.setAttribute("role", "radio");
                side.setAttribute("aria-label", described);
                sides[value] = side;
                group.appendChild(side);
            });
        //: Which machine Remote means, and the way to change it without going
        //: back through the toggle -- which matters because a list of one is
        //: adopted silently, and without this that shortcut is a one-way door.
        const chip = button("data-location-place", "", () => choosePlace(true));
        chip.hidden = true;
        location.append(group, chip);

        //: One line under the row, for the waits and the failures getting
        //: hold of the file. What the CALLER says about the file once it has
        //: it belongs wherever the caller puts its own errors.
        const status = el("div", "data-location-status");
        const hint = el("p", "field-hint");

        // Sending the bytes -- the one thing a browser can do that naming a
        // path cannot. Built only where it can ever be the answer, so a
        // desktop launch has exactly one way in.
        let chooser = null;
        let upload = null;
        if (serverIsElsewhere()) {
            chooser = el("input");
            chooser.type = "file";
            chooser.accept = options.accept || "";
            chooser.hidden = true;
            // This row asks which machine in its own switch, two elements to
            // the left. Without the opt-out the shared layer
            // (services/fileLocation.js) asks again in a modal, so pressing
            // Upload would mean answering one question twice in two shapes.
            chooser.setAttribute("data-file-location", "local");
            chooser.addEventListener("change", () => {
                const picked = chooser.files && chooser.files[0];
                if (picked) deliver({ file: picked, path: "" });
            });
            upload = button("browse-button", "Upload…", () => chooser.click());
        }

        row.append(location, input, browse);
        if (upload) row.append(upload, chooser);
        row.append(load);
        wrap.append(row, status, hint);

        // -- which machine -------------------------------------------------

        function nodeName() {
            if (state.where === LOCAL) {
                return serverIsElsewhere() ? clientNodeName() : "";
            }
            const place = state.place;
            if (!place || place.kind === "server") return "";
            // `registered_node` as well as `node`: a data node outlives the
            // Plexora that started it, so after a restart `node` is empty for
            // a machine that is up and answering.
            return place.node || place.registered_node || "";
        }

        /** Whether what is in the box is a path this server can open unaided. */
        function plainPath() {
            if (state.where === LOCAL) return !serverIsElsewhere();
            return Boolean(state.place) && state.place.kind === "server";
        }

        /**
         * Remote is a question rather than a setting: "somewhere else" is not
         * one place, so choosing it has to name which. Pressing a side that is
         * already answered does nothing -- the chip is how the machine gets
         * changed, and re-asking on every click would make the control feel as
         * though it had forgotten.
         */
        function press(where) {
            if (where === REMOTE && !(state.where === REMOTE && state.place)) {
                return choosePlace(false);
            }
            if (where !== state.where) settle(where, null);
            return undefined;
        }

        /**
         * Which machine Remote means, asked in whatever way suits how many
         * answers there are: none reachable is a connection to open rather
         * than a list to pick from, exactly one is not a choice, and more than
         * one is the picker.
         */
        async function choosePlace(force) {
            if (!window.PlexoraPlacePicker) return;
            if (!force) {
                const reachable = await reachablePlaces();
                if (reachable && reachable.length === 1) {
                    settle(REMOTE, reachable[0]);
                    return;
                }
                if (reachable && !reachable.length) {
                    await connectSomewhere();
                    return;
                }
            }
            const picked = await window.PlexoraPlacePicker.pick({
                current: (state.place && state.place.id) || "",
            });
            if (!picked) {
                // Cancelled with nothing chosen before: back to the side that
                // still works, rather than stranded on a Remote that is not
                // any machine.
                if (state.where === REMOTE && !state.place) settle(LOCAL, null);
                return;
            }
            settle(REMOTE, picked);
        }

        /** Nothing to pick from, so picking is the wrong question. */
        async function connectSomewhere() {
            if (!window.PlexoraConnectionModal) return;
            const opened = await window.PlexoraConnectionModal.open({
                kind: "node",
                intent: options.intent
                    || "No other machine is connected yet. Open one and this "
                       + "file can be read from it.",
            });
            if (opened && opened.connected) {
                settle(REMOTE, {
                    id: opened.name,
                    kind: "remote",
                    label: opened.label || opened.name,
                    node: opened.node || null,
                });
                return;
            }
            if (state.where === REMOTE && !state.place) settle(LOCAL, null);
        }

        function settle(where, place) {
            state.where = where;
            state.place = where === REMOTE ? (place || null) : null;
            // What was in the box described another machine's filesystem, and
            // a path that means something over there means nothing here -- so
            // it goes, rather than sitting in the box looking answered.
            state.path = "";
            input.value = "";
            input.classList.remove("is-invalid");
            load.disabled = true;
            say("");
            paint();
        }

        /** The machine currently chosen, as somebody would say it. */
        function machine() {
            if (state.where === LOCAL) return "this computer";
            return state.place ? state.place.label : "";
        }

        function hintText(detached) {
            if (state.where === REMOTE && !state.place) {
                return "Choose the machine this file is on.";
            }
            if (detached) {
                return "Plexora is running elsewhere and cannot read paths on "
                       + "this computer — send the file with Upload… instead.";
            }
            if (plainPath()) {
                return serverIsElsewhere()
                    ? "The box and Browse mean the machine running Plexora."
                    : "The box and Browse mean this computer, which is also "
                      + "the machine running Plexora.";
            }
            return `The box and Browse mean ${machine()}, and Plexora reads `
                   + "the file from there.";
        }

        function paint() {
            Object.keys(sides).forEach((where) => {
                const on = where === state.where;
                sides[where].classList.toggle("is-active", on);
                sides[where].setAttribute("aria-checked", on ? "true" : "false");
            });
            chip.hidden = state.where !== REMOTE;
            chip.textContent = state.place ? state.place.label : "Choose…";
            chip.classList.toggle("is-unset", !state.place);

            // With nothing that can read a path on the chosen machine, the box
            // stops pretending to take one -- and Browse with it, which would
            // otherwise open a dialog on a machine nobody chose.
            const detached = !plainPath() && !nodeName();
            input.disabled = detached;
            browse.disabled = detached;
            if (upload) upload.hidden = !(detached && state.where === LOCAL);
            hint.textContent = hintText(detached);
        }

        function say(text, kind = "") {
            status.className = kind ? `data-location-status ${kind}` : "data-location-status";
            status.textContent = text || "";
        }

        function busy(on) {
            state.busy = Boolean(on);
            load.disabled = state.busy || !input.value.trim();
            browse.disabled = state.busy;
            if (upload) upload.disabled = state.busy;
        }

        // -- handing the file over ------------------------------------------

        async function deliver(payload) {
            if (state.busy) return;
            busy(true);
            try {
                await options.onChoose?.(payload);
                say("");
            } catch (error) {
                say(error?.message || "That file could not be read.", "is-error");
            } finally {
                busy(false);
            }
        }

        /**
         * Read the named file and hand it over.
         *
         * Two shapes, decided by which machine holds it. A path the server can
         * open goes over as a path, and a file on a cluster never touches this
         * browser. A file on a data node is fetched through the relay and
         * handed over as bytes, because the server has no path for it.
         */
        async function choose() {
            const value = input.value.trim();
            if (!value || state.busy) return;
            state.path = value;
            if (plainPath()) {
                await deliver({ file: null, path: value });
                return;
            }
            const node = nodeName();
            if (!node) return;
            const reader = window.PlexoraFileLocation;
            if (!reader || !reader.read) {
                say("This page cannot read files on another machine.", "is-error");
                return;
            }
            busy(true);
            say(`Reading from ${machine()}…`, "is-busy");
            let file;
            try {
                file = await reader.read(node, value);
            } catch (error) {
                busy(false);
                say(error?.message || "That file could not be read.", "is-error");
                return;
            }
            busy(false);
            await deliver({ file, path: "" });
        }

        // Live existence check, exactly as the home page's path box does it --
        // a typo in a long cluster path is otherwise only reported after the
        // upload, by a message about a file the user is sure exists.
        let checkId = 0;
        input.addEventListener("input", async () => {
            const value = input.value.trim();
            input.classList.remove("is-invalid");
            load.disabled = true;
            if (!value) return;
            // Only this server's own filesystem can be checked from here. A
            // path on another machine is checked by the read that follows --
            // the alternative is a stat relayed to a cluster per keystroke,
            // for a box somebody is going to press the button on anyway.
            if (!plainPath()) {
                load.disabled = !nodeName();
                return;
            }
            const id = ++checkId;
            try {
                const response = await fetch(plexoraUrl("check_file_existence"), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ path: value }),
                });
                const exists = await response.json();
                if (id !== checkId) return;
                input.classList.toggle("is-invalid", !exists);
                load.disabled = !exists;
            } catch (error) {
                if (id === checkId) input.classList.add("is-invalid");
            }
        });
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && !load.disabled) {
                event.preventDefault();
                load.click();
            }
        });

        paint();

        return {
            element: wrap,
            focus: () => input.focus(),
            busy,
            say,
            reset: () => settle(state.where, state.place),
        };
    }

    return { create, LOCAL, REMOTE };
})();
