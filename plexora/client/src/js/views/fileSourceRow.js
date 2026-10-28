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
 * applies from the machine chip at the front of its box (machineChip.js) plus
 * whether Plexora is running here -- the same derivation
 * services/dataLocation.js makes, in the same words.
 *
 * WHAT IT DOES NOT DO is read the file. It hands its caller either a path the
 * server can open or a `File` of bytes, and what that means is the caller's:
 * a marker list, a gene-group table and a transform matrix are three
 * different parses and none of them belongs in a row of controls. Nothing
 * here knows a CSV from a spreadsheet.
 *
 * Lifted out of views/channelNamesUpload.js, which is where this was first
 * written and which is still the fullest example of what it is for. The
 * classes are that dialog's, deliberately: `.import-field-row`, `.data-location-status`
 * and the rest are already styled and already the shape every path field in
 * Plexora has.
 *
 * Served as a classic script (see base.html).
 */
window.PlexoraFileSourceRow = (function () {
    "use strict";

    //: The two answers, spelled as dataLocation.js spells them.
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
     * Build the row.
     *
     * @param options.accept      - what the browser's file chooser offers
     * @param options.filter      - the Browse dialog's filter name
     * @param options.placeholder - example path for the empty box
     * @param options.label       - the field's own label, or "" for none
     * @param options.actionLabel - the button that means "use this one"
     * @param options.what        - what the file is ("the gene-group file"),
     *                              for the connection modal the chip opens
     * @param options.onChoose    - `({file, path}) => Promise`. Exactly one of
     *                              the two is set: `path` for a file the
     *                              server can open itself, `file` for bytes.
     *                              Rejecting shows the message on the row.
     * @returns `{element, reset, focus, busy}`
     */
    function create(options = {}) {
        //: `decided`: the machine has been set by hand or something typed, so
        //: a connected machine is no longer adopted for it.
        const state = { where: LOCAL, place: null, path: "", busy: false, decided: false };

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
        // wired once: the chip below can be changed long after this button
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
            // This row asks which machine in its own chip. Without the
            // opt-out the shared layer (services/fileLocation.js) asks again
            // in a modal, so pressing Upload would mean answering one
            // question twice in two shapes.
            chooser.setAttribute("data-file-location", "local");
            chooser.addEventListener("change", () => {
                const picked = chooser.files && chooser.files[0];
                if (picked) deliver({ file: picked, path: "" });
            });
            upload = button("browse-button", "Upload…", () => chooser.click());
        }

        row.append(input, browse);
        if (upload) row.append(upload, chooser);
        row.append(load);
        wrap.append(row, status, hint);

        // "Which machine is this file on?", asked as every other path field
        // in Plexora asks it: a chip at the front of the box, whose menu
        // lists the machines.
        const Chip = window.PlexoraMachineChip;
        const chip = Chip ? Chip.create({
            current: () => ({ where: state.where, place: state.place }),
            onPick: (where, place) => {
                state.decided = true;
                settle(where, place);
                if (!input.disabled) input.focus();
            },
            what: options.what,
        }) : null;
        if (chip) Chip.wrap(input, chip);

        // -- which machine -------------------------------------------------

        function nodeName() {
            if (state.where === LOCAL) {
                return serverIsElsewhere() ? clientNodeName() : "";
            }
            const place = state.place;
            if (!place || place.kind === "server") return "";
            // `registered_node` as well as `node`: a data node outlives the
            // Plexora that started it, so after a restart `node` is empty for
            // a machine that is up and answering. A files-only server has
            // neither, and is asked by its `sftp://` base, which
            // `/fetch_file` and the listing routes read over SFTP.
            return place.node || place.registered_node
                || (place.files && place.files.base) || "";
        }

        /** Whether what is in the box is a path this server can open unaided. */
        function plainPath() {
            if (state.where === LOCAL) return !serverIsElsewhere();
            return Boolean(state.place) && state.place.kind === "server";
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
            if (chip) chip.paint();

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
            if (value) state.decided = true;
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

        // Opens on a machine that is already connected, as every data field
        // does (dataLocation.js, `whenConnected`).
        window.PlexoraDataLocation?.whenConnected?.((place) => {
            if (state.decided || state.where !== LOCAL || input.value.trim()) return;
            settle(REMOTE, place);
        });

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
