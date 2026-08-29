/**
 * dataLocation.js -- "which machine is this file on?", asked per field.
 *
 * Every path box in Plexora used to mean one machine, decided before Plexora
 * started: whichever one the server happened to be running on. That is the
 * wrong shape for how imaging data actually sits. The slide is on the cluster,
 * the .h5ad came back to the laptop months ago, and the mask is beside the
 * segmentation job that wrote it -- three machines, one form, and no reason
 * for any of it to have been declared in advance.
 *
 * So each field gets a **This computer / Remote** switch, and the question is
 * asked where it comes up: at the moment the data is added, per modality, not
 * at launch. Remote opens the place picker (placePicker.js), which lists the
 * saved SSH connections and opens one on the spot if it is not already up.
 *
 * **This computer means the machine the browser is on.** Which is reachable
 * two different ways, and the difference is not cosmetic:
 *
 * - Plexora is running here too (an ordinary desktop launch). Then "here" is
 *   the server's own filesystem, and a path is just a path -- exactly what the
 *   field always did.
 * - Plexora is running somewhere else. Then a path on this computer means
 *   nothing to it, and reading one needs a process on this machine: the data
 *   node `plexora connect` starts. Without that node, what is left is the one
 *   thing a browser can do unaided -- send the bytes of a quantification CSV.
 *
 * **Remote means some other machine**, and there are two of those too: the
 * server itself, when it is not this computer, and any saved connection, which
 * is reached through a data node opened on demand.
 *
 * What it produces is what every form already took: a path (a machine whose
 * filesystem the server can read directly, or an uploaded file, which is on
 * the server from that moment) or a `node://<node>/<resource>` locator.
 * Nothing downstream of the form learns a new shape -- `POST /import`, the
 * edit page and the requirements modal are unchanged.
 */
window.PlexoraDataLocation = (function () {
    const LOCAL = "local";
    const REMOTE = "remote";

    //: How often to ask whether a mask has finished converting. A whole-slide
    //: mask takes minutes, so this is a progress display rather than a wait --
    //: two seconds is often enough to feel live and rare enough to be free.
    const POLL_MS = 2000;

    //: What a browser can hand over directly, and the only thing it should.
    //: A quantification CSV is copied into the project directory on import
    //: anyway, so sending one costs a copy that was always going to happen --
    //: and the result outlives the session, which nothing reached through a
    //: tunnel does. An .h5ad or a .zarr store is read where it lies and is
    //: routinely tens of gigabytes.
    const UPLOAD_SUFFIXES = [".csv", ".tsv", ".txt"];

    //: What to say when the browser's machine is not attached to the server at
    //: all. The command is the whole fix, and it has to be run on the user's
    //: OWN computer -- which is precisely the thing a page served from a
    //: cluster cannot do for them, so it says it rather than offering a button
    //: that could not work.
    const DETACHED =
        "This computer is not attached to the server. Run "
        + "`plexora connect <you>@<server>` in a terminal here and its files "
        + "become available.";

    //: The machine the last field settled on, offered as the starting point
    //: for the next one. An import names an image, a mask and a table, and
    //: they are on one machine far more often than not -- asking three times
    //: is the same question three times, and the switch is right there for the
    //: case where the answer differs.
    let lastPlace = null;

    function clientNode() {
        return (window.flaskVariables && window.flaskVariables.client_node) || "";
    }

    function serverIsRemote() {
        // A registered client node is itself proof (see
        // page_routes.server_is_remote, which folds it in) -- read here too so
        // this stays right against an older page that predates the flag.
        return Boolean(clientNode())
            || Boolean(window.flaskVariables
                       && (window.flaskVariables.server_is_remote
                           || window.flaskVariables.notebook_mode));
    }

    /**
     * Whether to render the switch at all.
     *
     * Always, now. There is a second machine in every arrangement Plexora runs
     * in -- either the server is somewhere else, or a saved connection could
     * be -- and a field that hides the question answers it on the user's
     * behalf with the one answer that used to be wrong half the time.
     */
    function available() {
        return true;
    }

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

    /**
     * @function attach - put a location switch on one path field.
     *
     * @param input the text input the field already had. When the value has to
     *   become a locator its `name` moves to a hidden companion, so what the
     *   form POSTs is the locator while what the user reads is their own path
     *   -- nobody should have to look at `node://hpc/cells-7f3a91c2` to know
     *   they picked /scratch/study/cells.h5ad.
     * @param options `kind` (image | segmentation | table), `filter`/`mode`
     *   for the browse dialog, and `onChange` called whenever the value the
     *   form would submit changes.
     * @returns {{where, submitValue, blocking, release, element}} or null when
     *   the field is not shaped to render into.
     */
    function attach(input, options = {}) {
        if (!input) return null;
        // Every surface puts the input inside a row inside a field, and the
        // switch goes above the row. A caller that does not is one this cannot
        // render into -- better to leave their field exactly as it was than to
        // throw halfway through building it.
        const row = input.parentNode;
        const field = row && row.parentNode;
        if (!field) return null;

        const kind = options.kind || "table";
        //: Whether a browser upload is on the table for this field. Tables
        //: only -- see UPLOAD_SUFFIXES.
        const uploadable = kind === "table";
        const onChange = options.onChange || function () {};
        const formName = input.getAttribute("name") || "";

        // One letter each, and the reason is width. This control sits INSIDE
        // the field's row, immediately before the path box, on forms that
        // already have four of them -- and "This computer | Remote" spent more
        // of that row than the box it governs. What the letters mean is
        // carried by the aria-labels (which is what a screen reader reads
        // instead of them) and by the tooltip on the group; the place chip
        // beside it names the actual machine, which is the part that changes.
        const root = el("div", "data-location");
        const group = el("div", "data-location-toggle");
        group.setAttribute("role", "radiogroup");
        group.setAttribute("aria-label", "Where this file is");
        // The letters spelt out, in the shortest form that still expands them:
        // a mouse needs to be told what L and R stand for, and does not need a
        // sentence to be told it.
        group.setAttribute("data-tooltip", "Data Location — (L)ocal | (R)emote");
        const buttons = {};
        [[LOCAL, "L", "Local — this computer"],
         [REMOTE, "R", "Remote — another machine"]].forEach(
            ([value, label, described]) => {
                const button = el("button", "data-location-option", label);
                button.type = "button";
                button.dataset.where = value;
                button.setAttribute("role", "radio");
                button.setAttribute("aria-label", described);
                button.addEventListener("click", () => press(value));
                buttons[value] = button;
                group.appendChild(button);
            });
        root.appendChild(group);

        //: Which machine Remote currently means, and the way to change it
        //: without going back through the toggle. `force`: this chip is the
        //: only way to see the list when there is exactly one machine, which
        //: the toggle adopts silently -- without it that shortcut would be a
        //: one-way door.
        const placeChip = el("button", "data-location-place");
        placeChip.type = "button";
        placeChip.hidden = true;
        placeChip.addEventListener("click", () => choosePlace(true));
        root.appendChild(placeChip);

        // Sending the bytes -- the one thing a browser can do that naming a
        // path cannot. Offered alongside the path box when there is a node to
        // read files in place, and instead of it when there is not.
        let chooser = null;
        let uploadButton = null;
        if (uploadable) {
            chooser = el("input");
            chooser.type = "file";
            chooser.accept = UPLOAD_SUFFIXES.join(",");
            chooser.hidden = true;
            chooser.addEventListener("change", () => {
                const file = chooser.files && chooser.files[0];
                if (file) upload(file);
            });
            uploadButton = el("button", "browse-button", "Upload…");
            uploadButton.type = "button";
            uploadButton.addEventListener("click", () => chooser.click());
            // The button goes at the END of the row, beside Browse, because
            // that is what it is an alternative to. Only the invisible file
            // input lives here.
            root.appendChild(chooser);
        }

        // The switch goes IN the row, immediately before the box it governs --
        // "which machine?" and "which file?" are one question asked in two
        // halves, and a control floating above the row reads as a setting for
        // the whole form rather than for this one field. The status line goes
        // under the field, where a sentence has room to be a sentence.
        const status = el("div", "data-location-status");
        row.insertBefore(root, row.children[0]);
        if (uploadButton) row.appendChild(uploadButton);
        field.appendChild(status);

        //: Carries the locator under the field's own name while the visible
        //: input shows the user's path. Created only when the submitted value
        //: is not the typed one, so a plain-path field posts exactly the one
        //: value it always did.
        let hidden = null;
        //: A value the field was built with -- a stored answer on the edit
        //: page, which is either a server path or a `node://` address. Both
        //: are things the server takes exactly as they are, so the field opens
        //: in whichever mode submits the box unchanged. Re-interpreting a
        //: stored answer as a path on some other machine would break a project
        //: that was working, and it is not a guess worth making.
        const arriving = input.value.trim();
        const state = {
            // Otherwise This computer, because it is the machine somebody is
            // sitting at and knows the paths on. Switching away is an explicit
            // act, and it clears the box, which is honest: nothing here knows
            // what that file is called on another machine.
            where: arriving && serverIsRemote() ? REMOTE : LOCAL,
            place: arriving && serverIsRemote() ? serverPlace() : null,
            resourceId: null,
            locator: null,
            //: Which node is holding what this field shared. Remembered apart
            //: from the switch: by the time it is released the switch may
            //: point somewhere else, and the DELETE has to reach the machine
            //: that actually has the file.
            sharedOn: null,
            //: Where an uploaded file landed on the SERVER. From that moment
            //: it is an ordinary local file, named by a path like any other --
            //: nothing downstream ever learns a browser was involved.
            uploadedPath: null,
            phase: "empty",   // empty | sharing | ready | preparing | error
            message: "",
        };
        let pollTimer = null;
        let shareToken = 0;

        function serverPlace() {
            return { id: "server", kind: "server", label: "the server",
                     node: null };
        }

        /**
         * The node that has to be asked to read this field's file, or null
         * when the server can read it itself.
         *
         * The one derivation everything else here hangs off. A path is only
         * ever a plain path when the machine holding the file is the machine
         * running Plexora; every other combination needs a node in between,
         * and it is a different node depending on which side of the switch
         * asked for it.
         */
        function nodeName() {
            if (state.where === LOCAL) {
                return serverIsRemote() ? (clientNode() || null) : null;
            }
            return (state.place && state.place.node) || null;
        }

        /** Whether the typed path is what gets submitted, unchanged. */
        function plainPath() {
            if (state.where === LOCAL) return !serverIsRemote();
            return Boolean(state.place) && state.place.kind === "server";
        }

        function placeLabel() {
            if (state.where !== REMOTE) return "";
            if (!state.place) return "Choose…";
            return state.place.label;
        }

        function value() {
            if (plainPath()) return input.value.trim();
            return state.uploadedPath || state.locator || "";
        }

        /** What still stops this being submitted, or null. */
        function blocking() {
            if (plainPath()) return null;
            if (state.uploadedPath) return null;
            if (state.phase === "uploading") return "Still sending that file…";
            if (state.where === REMOTE && !state.place) {
                return "Choose the machine this file is on.";
            }
            if (!input.value.trim()) return null;   // an empty optional field
            if (!nodeName()) {
                // Nothing here can read a path on that machine, so a path in
                // the box is not an answer -- say which control is.
                if (state.where === REMOTE) {
                    return `Connect to ${placeLabel()} before naming a file `
                           + "on it.";
                }
                return uploadable
                    ? "Send the file with Upload… — this server cannot read "
                      + "paths on your computer."
                    : DETACHED;
            }
            if (state.phase === "sharing") {
                return "Still asking that machine for the file — try again in "
                       + "a moment.";
            }
            if (state.phase === "preparing") {
                return "That mask is still being prepared.";
            }
            if (state.phase === "error") return state.message;
            if (!state.locator) return state.message || "That file could not be shared.";
            return null;
        }

        function emit() {
            if (hidden) hidden.value = state.locator || "";
            onChange(value());
        }

        function paint() {
            Object.entries(buttons).forEach(([where, button]) => {
                const on = where === state.where;
                button.classList.toggle("is-active", on);
                button.setAttribute("aria-checked", on ? "true" : "false");
            });
            placeChip.hidden = state.where !== REMOTE;
            placeChip.textContent = placeLabel();
            placeChip.classList.toggle("is-unset", !state.place);

            const detached = !plainPath() && !nodeName();
            if (uploadButton) uploadButton.hidden = !(uploadable && detached
                                                      && state.where === LOCAL);
            // With nothing that can read a path on the chosen machine, the box
            // stops pretending to take one.
            input.disabled = detached;
            status.className = "data-location-status";
            if (detached && !state.uploadedPath && state.phase !== "uploading") {
                if (state.where === REMOTE) {
                    status.classList.add("is-error");
                    status.textContent = state.place
                        ? `Not connected to ${placeLabel()} yet.`
                        : "Choose the machine this file is on.";
                    return;
                }
                status.classList.add(uploadable ? "is-busy" : "is-error");
                status.textContent = uploadable
                    ? "Send a CSV with Upload… — or " + DETACHED
                    : DETACHED;
                return;
            }
            if (plainPath() || state.phase === "empty") {
                status.textContent = "";
                return;
            }
            if (state.phase === "uploading") {
                status.classList.add("is-busy");
                status.textContent = "Sending…";
                return;
            }
            if (state.uploadedPath) {
                status.classList.add("is-ready");
                status.textContent = "Sent from this computer";
                return;
            }
            const machine = state.where === LOCAL
                ? "this computer" : placeLabel();
            if (state.phase === "sharing") {
                status.classList.add("is-busy");
                status.textContent = "Sharing…";
            } else if (state.phase === "preparing") {
                status.classList.add("is-busy");
                // Named, because this is the one wait here that can be long and
                // a silent minute reads as a hang.
                status.textContent = `Preparing the mask on ${machine}…`;
            } else if (state.phase === "error") {
                status.classList.add("is-error");
                status.textContent = state.message;
            } else {
                status.classList.add("is-ready");
                status.textContent = `Shared from ${machine}`;
            }
        }

        /** Stop the node serving whatever this field shared. */
        function release() {
            clearTimeout(pollTimer);
            const id = state.resourceId;
            const node = state.sharedOn;
            state.resourceId = null;
            state.locator = null;
            state.sharedOn = null;
            if (!id || !node) return;
            // Fire and forget: the user has moved on, and a node that keeps
            // serving one extra file is a far smaller problem than a form that
            // waits on a DELETE before letting them type.
            fetch(plexoraUrl(`nodes/${encodeURIComponent(node)}/resources/${encodeURIComponent(id)}`),
                  { method: "DELETE" }).catch(() => {});
        }

        /**
         * The Remote half is a question rather than a setting: "somewhere
         * else" is not one place, so choosing it has to name which.
         *
         * Pressing it when a machine is already chosen does nothing -- the
         * chip beside it is how you change machines, and re-asking on every
         * click of an already-active segment would make the control feel like
         * it had forgotten.
         */
        function press(where) {
            if (where === REMOTE && !(state.where === REMOTE && state.place)) {
                return choosePlace();
            }
            if (where !== state.where) choose(where, null);
        }

        /**
         * Which machine "Remote" means, asked in whatever way suits how many
         * answers there are.
         *
         * Three situations, and a list of one is not a choice:
         *
         * - **None reachable.** There is nothing to pick from, so picking is
         *   the wrong question -- open the connection modal instead, which is
         *   where a machine is added and connected.
         * - **Exactly one.** Adopt it. Flipping the switch to Remote when
         *   there is one remote machine has already said everything the picker
         *   would ask. The place chip stays the way to change it, and it
         *   always opens the list (`force`) so this shortcut can be undone.
         * - **More than one.** The picker, as before.
         */
        async function choosePlace(force) {
            if (!window.PlexoraPlacePicker) return;
            if (!force) {
                const shortcut = await onlyPlace();
                if (shortcut) {
                    lastPlace = shortcut;
                    choose(REMOTE, shortcut);
                    return;
                }
                if (shortcut === null && window.PlexoraConnectionModal) {
                    const opened = await window.PlexoraConnectionModal.open({
                        kind: "node",
                        intent: "No other machine is connected yet. Open one "
                                + "and this field can name a file on it.",
                    });
                    if (opened && opened.connected) {
                        const place = { id: opened.name, kind: "remote",
                                        label: opened.label || opened.name,
                                        node: opened.node || null };
                        lastPlace = place;
                        choose(REMOTE, place);
                        return;
                    }
                    if (state.where === REMOTE && !state.place) {
                        choose(LOCAL, null);
                    }
                    return;
                }
            }
            const picked = await window.PlexoraPlacePicker.pick({
                current: (state.place && state.place.id) || "",
            });
            if (!picked) {
                // Cancelled with nothing chosen before: stay where we were,
                // rather than stranding the field on a Remote it cannot use.
                if (state.where === REMOTE && !state.place) choose(LOCAL, null);
                return;
            }
            lastPlace = picked;
            choose(REMOTE, picked);
        }

        /**
         * The one machine there is, `null` for none, `undefined` for several.
         *
         * "Reachable" means a file could be named on it right now: the server
         * itself when Plexora is running elsewhere, and any saved connection
         * with a data node already open. A saved-but-not-connected profile is
         * not one of these -- offering it here would adopt a machine the field
         * cannot read, which is the state the switch exists to avoid.
         */
        async function onlyPlace() {
            let list;
            try {
                list = await window.PlexoraPlacePicker.places();
            } catch (e) {
                return undefined;   // fall through to the picker, which says so
            }
            const reachable = list.filter(
                (place) => place.kind === "server" || Boolean(place.node));
            if (reachable.length > 1) return undefined;
            if (!reachable.length) return null;
            const only = reachable[0];
            return { id: only.id, kind: only.kind, label: only.label,
                     node: only.node || null };
        }

        function choose(where, place) {
            const wanted = (where === REMOTE
                ? (place || lastPlace || null) : null);
            if (where === state.where
                    && (wanted && wanted.id) === (state.place && state.place.id)) {
                return;
            }
            release();
            state.uploadedPath = null;
            state.where = where;
            state.place = wanted;
            state.phase = "empty";
            state.message = "";
            // The old value described another machine's filesystem, and a path
            // that means something over there means nothing here -- so it goes
            // rather than sitting in the box looking answered.
            input.value = "";
            input.classList.remove("is-valid", "is-invalid");
            if (input.setCustomValidity) input.setCustomValidity("");
            applyName();
            paint();
            emit();
            input.dispatchEvent(new Event("input", { bubbles: true }));
        }

        function applyName() {
            if (!formName) return;
            if (!plainPath()) {
                if (!hidden) {
                    hidden = el("input");
                    hidden.type = "hidden";
                    hidden.name = formName;
                    row.appendChild(hidden);
                }
                // Two inputs of one name would post both values, oldest first.
                input.removeAttribute("name");
            } else {
                if (hidden) {
                    hidden.remove();
                    hidden = null;
                }
                input.setAttribute("name", formName);
            }
        }

        async function share() {
            const node = nodeName();
            const path = input.value.trim();
            release();
            state.uploadedPath = null;
            if (!path || !node) {
                state.phase = "empty";
                state.message = "";
                paint();
                emit();
                return;
            }
            const mine = ++shareToken;
            state.phase = "sharing";
            state.message = "";
            paint();
            emit();

            let payload;
            try {
                payload = await ask(
                    plexoraUrl(`nodes/${encodeURIComponent(node)}/resources`), {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ kind, path }),
                    });
            } catch (error) {
                if (mine !== shareToken) return;
                state.phase = "error";
                state.message = error.message;
                input.classList.remove("is-valid");
                input.classList.add("is-invalid");
                paint();
                emit();
                return;
            }
            if (mine !== shareToken) return;

            const resource = payload.resource || {};
            state.resourceId = resource.id || null;
            state.locator = resource.locator || null;
            state.sharedOn = node;
            input.classList.remove("is-invalid");
            input.classList.add("is-valid");
            adopt(resource, mine, node);
        }

        function adopt(resource, mine, node) {
            if (resource.state === "preparing") {
                state.phase = "preparing";
                paint();
                emit();
                pollTimer = setTimeout(() => poll(mine, node), POLL_MS);
                return;
            }
            if (resource.state === "error") {
                state.phase = "error";
                state.message = resource.error || "That file could not be prepared.";
                input.classList.remove("is-valid");
                input.classList.add("is-invalid");
            } else {
                state.phase = "ready";
                state.message = "";
            }
            paint();
            emit();
        }

        /**
         * Send a CSV's bytes to the server, which stages it and answers with a
         * path. From there it is an ordinary local file: the import copies it
         * into the project directory, which is what it was going to do with a
         * CSV anyway -- so unlike everything else here, an uploaded table
         * outlives the session that carried it.
         */
        async function upload(file) {
            release();
            state.uploadedPath = null;
            const mine = ++shareToken;
            state.phase = "uploading";
            state.message = "";
            paint();
            emit();

            const body = new FormData();
            body.append("file", file);
            let payload;
            try {
                payload = await ask(plexoraUrl("upload_data_file"),
                                    { method: "POST", body });
            } catch (error) {
                if (mine !== shareToken) return;
                state.phase = "error";
                state.message = error.message;
                input.classList.remove("is-valid");
                input.classList.add("is-invalid");
                paint();
                emit();
                return;
            }
            if (mine !== shareToken) return;

            state.uploadedPath = payload.path || null;
            state.phase = state.uploadedPath ? "ready" : "error";
            // The box shows the name the user picked, as it does for a shared
            // file -- the staging path is the server's business.
            input.value = payload.name || file.name;
            input.classList.remove("is-invalid");
            input.classList.add("is-valid");
            paint();
            emit();
        }

        async function poll(mine, node) {
            if (mine !== shareToken || !state.resourceId) return;
            try {
                const payload = await ask(plexoraUrl(
                    `nodes/${encodeURIComponent(node)}/resources/`
                    + `${encodeURIComponent(state.resourceId)}/status`));
                if (mine !== shareToken) return;
                adopt(payload.resource || {}, mine, node);
            } catch (error) {
                if (mine !== shareToken) return;
                // A node that stopped answering mid-conversion is a real
                // failure and not a slow one -- say so rather than polling a
                // machine that has gone to sleep.
                state.phase = "error";
                state.message = error.message;
                paint();
                emit();
            }
        }

        // `change` rather than `input`: sharing opens a file on another
        // machine, which is not something to do per keystroke. Browse fills the
        // box and dispatches this itself.
        input.addEventListener("change", () => {
            if (!plainPath()) share();
        });

        applyName();
        paint();
        // Deliberately no `emit()` here. Nothing has changed yet -- the field
        // holds what it arrived with -- and calling a caller's handler from
        // inside `attach` means calling it before `attach` has returned the
        // handle that handler is written against. That is not a hazard worth
        // leaving lying around for the sake of an event that says nothing.
        if (hidden) hidden.value = state.locator || "";

        return {
            where: () => state.where,
            isLocal: () => state.where === LOCAL,
            /**
             * Whether the box holds a path THIS server can stat.
             *
             * The question callers actually have, and not the same as "is it
             * set to This computer": on an ordinary desktop launch those are
             * the same filesystem, and on a cluster neither side of the switch
             * necessarily is.
             */
            isPlainPath: plainPath,
            place: () => state.place,
            /**
             * Move the switch from code.
             *
             * One caller: the chips that fill a field with a node address a
             * node is ALREADY serving. That address is an answer about another
             * machine's world -- it is what the field posts as it stands -- so
             * the switch has to say so, or the box would hold a locator while
             * this computer tried to hand it to a node as a path.
             */
            setWhere: (where, place) => choose(where, place),
            /**
             * Put the field into whichever mode posts the box unchanged.
             *
             * For the chips that fill a field with an address a node is
             * ALREADY serving. That string is the finished answer -- it is
             * what the form submits as it stands -- so what it needs is a mode
             * that does not try to interpret it, and which mode that is
             * depends on where Plexora is running. Callers set the value
             * immediately afterwards, because switching modes clears the box.
             */
            setVerbatim: () => {
                if (serverIsRemote()) choose(REMOTE, serverPlace());
                else choose(LOCAL, null);
            },
            submitValue: value,
            blocking,
            release,
            /** The node to browse on, or null for this server's own files. */
            browseNode: nodeName,
            element: root,
            //: Separate, because the two no longer live together: the switch
            //: sits in the field's row and the status line under the field.
            statusElement: status,
        };
    }

    return { attach, available, clientNode, serverIsRemote };
})();
