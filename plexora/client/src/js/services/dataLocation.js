/**
 * dataLocation.js -- "which machine is this file on?", asked per field.
 *
 * When Plexora runs on a cluster and the browser runs on a laptop, every path
 * box on every data form silently means "a path on the cluster". That is the
 * wrong default for half the files people actually have: the slide is on
 * scratch, but the .h5ad came back to the laptop months ago and the mask sits
 * beside the segmentation job that wrote it. Before this, saying so meant
 * copying files around, or knowing to type `node://…` by hand.
 *
 * So each field gets a Local / Remote switch. **Local means the user's own
 * computer** -- the machine the browser is on -- and Remote means whichever
 * machine is running Plexora. Local is the default, because it is the machine
 * the person is sitting at.
 *
 * The whole thing rests on one fact and it is worth stating plainly: **a
 * browser cannot serve a file by path.** Reading a file in place needs a
 * process on that machine, and that process is the data node `plexora connect`
 * starts on the laptop (see connect.py's `_start_local_node`). So Local means
 * two different things depending on whether that node is there:
 *
 * - **With it**, any file on the user's computer can be named and read where
 *   it lies -- the image included, which is the whole point.
 * - **Without it** -- a session started by hand over ssh, an Open OnDemand
 *   portal -- what is left is the one thing a browser can do unaided: send the
 *   bytes of a quantification CSV. For an image, a mask or an .h5ad it says
 *   what would make them possible instead of offering a control that could
 *   not work.
 *
 * A plain desktop launch has neither, and renders nothing at all: there is one
 * machine, and a switch between it and itself is noise.
 *
 * What it produces is what every form already took: a path (Remote, and an
 * uploaded file, which is on the server from that moment) or a
 * `node://<node>/<resource>` locator. Nothing downstream of the form learns a
 * new shape -- `POST /import`, the edit page and the requirements modal are
 * unchanged.
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

    function clientNode() {
        return (window.flaskVariables && window.flaskVariables.client_node) || "";
    }

    function notebookMode() {
        return Boolean(window.flaskVariables && window.flaskVariables.notebook_mode);
    }

    /**
     * Whether there is a machine to mean "Local" about.
     *
     * True in two situations, and they offer different things. With a data
     * node on the user's own computer, any file there can be read in place.
     * Without one -- a session started by hand over ssh, or an Open OnDemand
     * portal -- the server is still not the user's machine, so the question is
     * still real; what is left of the answer is a CSV upload, and for
     * everything else a sentence saying what would make it possible.
     */
    function available() {
        return Boolean(clientNode() || notebookMode());
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
     * @function attach - put a Local/Remote switch on one path field.
     *
     * @param input the text input the field already had. In Local mode its
     *   `name` moves to a hidden companion, so what the form POSTs is the
     *   locator while what the user reads is their own path -- nobody should
     *   have to look at `node://laptop/cells-7f3a91c2` to know they picked
     *   ~/study/cells.h5ad.
     * @param options `kind` (image | segmentation | table), `filter`/`mode`
     *   for the browse dialog, and `onChange` called whenever the value the
     *   form would submit changes.
     * @returns {{where, submitValue, blocking, release, element}} or null when
     *   there is no second machine to ask about -- in which case nothing is
     *   rendered and the field behaves exactly as it always did.
     */
    function attach(input, options = {}) {
        const node = clientNode();
        if (!input || !available()) return null;
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

        const root = el("div", "data-location");
        const group = el("div", "data-location-toggle");
        group.setAttribute("role", "radiogroup");
        group.setAttribute("aria-label", "Where this file is");
        const buttons = {};
        [[LOCAL, "This computer"], [REMOTE, "The server"]].forEach(([value, label]) => {
            const button = el("button", "data-location-option", label);
            button.type = "button";
            button.dataset.where = value;
            button.setAttribute("role", "radio");
            button.addEventListener("click", () => choose(value));
            buttons[value] = button;
            group.appendChild(button);
        });
        root.appendChild(group);

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
            root.append(uploadButton, chooser);
        }

        const status = el("span", "data-location-status");
        root.appendChild(status);
        field.insertBefore(root, row);

        //: Carries the locator under the field's own name while the visible
        //: input shows the user's path. Created only in Local mode, so a
        //: Remote field posts exactly the one value it always did.
        let hidden = null;
        const state = {
            // Local is the default, because the machine somebody is sitting at
            // is the one they know the paths on. A field that ARRIVES with a
            // value is the exception: that value is a stored answer -- a server
            // path or a node address, both of which the server takes as they
            // are -- and re-interpreting it as a path on the laptop would break
            // a project that was working. Switching to Local from there is an
            // explicit act, and it clears the box, which is honest: nothing
            // here knows what that file is called on the user's own machine.
            where: input.value.trim() ? REMOTE : LOCAL,
            resourceId: null,
            locator: null,
            //: Where an uploaded file landed on the SERVER. From that moment
            //: it is an ordinary local file, named by a path like any other --
            //: nothing downstream ever learns a browser was involved.
            uploadedPath: null,
            phase: "empty",   // empty | sharing | ready | preparing | error
            message: "",
        };
        let pollTimer = null;
        let shareToken = 0;

        function value() {
            if (state.where === REMOTE) return input.value.trim();
            return state.uploadedPath || state.locator || "";
        }

        /** What still stops this being submitted, or null. */
        function blocking() {
            if (state.where === REMOTE) return null;
            if (state.uploadedPath) return null;
            if (state.phase === "uploading") return "Still sending that file…";
            if (!input.value.trim()) return null;   // an empty optional field
            if (!node) {
                // Nothing here can read a path on the user's machine, so a
                // path in the box is not an answer -- say which control is.
                return uploadable
                    ? "Send the file with Upload… — this server cannot read "
                      + "paths on your computer."
                    : DETACHED;
            }
            if (state.phase === "sharing") {
                return "Still asking your computer for that file — try again in a moment.";
            }
            if (state.phase === "preparing") {
                return "That mask is still being prepared on your computer.";
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
            const local = state.where === LOCAL;
            if (uploadButton) uploadButton.hidden = !local;
            // With no node there is nothing on this end that can open a path
            // on the user's machine, so the box stops pretending to take one.
            input.disabled = local && !node;
            status.className = "data-location-status";
            if (local && !node && !state.uploadedPath
                    && state.phase !== "uploading") {
                status.classList.add(uploadable ? "is-busy" : "is-error");
                status.textContent = uploadable
                    ? "Send a CSV with Upload… — or " + DETACHED
                    : DETACHED;
                return;
            }
            if (state.where === REMOTE || state.phase === "empty") {
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
            if (state.phase === "sharing") {
                status.classList.add("is-busy");
                status.textContent = "Sharing…";
            } else if (state.phase === "preparing") {
                status.classList.add("is-busy");
                // Named, because this is the one wait here that can be long and
                // a silent minute reads as a hang.
                status.textContent = "Preparing the mask on your computer…";
            } else if (state.phase === "error") {
                status.classList.add("is-error");
                status.textContent = state.message;
            } else {
                status.classList.add("is-ready");
                status.textContent = "Shared from this computer";
            }
        }

        /** Stop the node serving whatever this field shared. */
        function release() {
            clearTimeout(pollTimer);
            const id = state.resourceId;
            state.resourceId = null;
            state.locator = null;
            if (!id) return;
            // Fire and forget: the user has moved on, and a node that keeps
            // serving one extra file is a far smaller problem than a form that
            // waits on a DELETE before letting them type.
            fetch(plexoraUrl(`nodes/${encodeURIComponent(node)}/resources/${encodeURIComponent(id)}`),
                  { method: "DELETE" }).catch(() => {});
        }

        function choose(where) {
            if (where === state.where) return;
            release();
            state.uploadedPath = null;
            state.where = where;
            state.phase = "empty";
            state.message = "";
            // The old value described the other machine's filesystem, and a
            // path that means something over there means nothing here -- so it
            // goes rather than sitting in the box looking answered.
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
            if (state.where === LOCAL) {
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
            const path = input.value.trim();
            release();
            state.uploadedPath = null;
            if (!path) {
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
            input.classList.remove("is-invalid");
            input.classList.add("is-valid");
            adopt(resource, mine);
        }

        function adopt(resource, mine) {
            if (resource.state === "preparing") {
                state.phase = "preparing";
                paint();
                emit();
                pollTimer = setTimeout(() => poll(mine), POLL_MS);
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

        async function poll(mine) {
            if (mine !== shareToken || !state.resourceId) return;
            try {
                const payload = await ask(plexoraUrl(
                    `nodes/${encodeURIComponent(node)}/resources/`
                    + `${encodeURIComponent(state.resourceId)}/status`));
                if (mine !== shareToken) return;
                adopt(payload.resource || {}, mine);
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

        // `change` rather than `input`: sharing opens a file on the other
        // machine, which is not something to do per keystroke. Browse fills the
        // box and dispatches this itself.
        input.addEventListener("change", () => {
            if (state.where === LOCAL) share();
        });

        applyName();
        paint();
        emit();

        return {
            where: () => state.where,
            isLocal: () => state.where === LOCAL,
            /**
             * Move the switch from code.
             *
             * One caller: the chips that fill a field with a node address a
             * node is ALREADY serving. That address is an answer about the
             * server's world -- it is what the field posts as it stands -- so
             * the switch has to say so, or the box would hold a locator while
             * Local mode tried to hand it to a node as a path.
             */
            setWhere: choose,
            submitValue: value,
            blocking,
            release,
            /** The node to open a browse dialog on, or null for this server. */
            browseNode: () => (state.where === LOCAL ? node : null),
            element: root,
        };
    }

    return { attach, available, clientNode };
})();
