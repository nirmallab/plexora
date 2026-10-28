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
 * So each field asks it, where it comes up: at the moment the data is added,
 * per modality, not at launch. The question is the home page's chip at the
 * front of the path box (views/machineChip.js) -- "Local", or the server's
 * name -- whose menu lists the saved SSH connections and opens one on the spot
 * if it is not already up.
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
    //: A flat quantification table is copied into the project directory on
    //: import anyway, so sending one costs a copy that was always going to
    //: happen -- and the result outlives the session, which nothing reached
    //: through a tunnel does. An .h5ad or a .zarr store is read where it lies
    //: and is routinely tens of gigabytes.
    //:
    //: Must stay in step with `import_routes.UPLOAD_SUFFIXES`, which refuses
    //: anything else; this list is only what the option is offered for.
    const UPLOAD_SUFFIXES = [".csv", ".tsv", ".txt", ".parquet"];

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
    //: is the same question three times, and the chip is right there for the
    //: case where the answer differs.
    let lastPlace = null;

    //: Which side somebody last set a field to on this page, by hand. A user
    //: who chose This computer once has answered for the next field too, so
    //: a connected machine is not pressed on them again.
    let lastSide = null;

    //: The connected machine last chosen by hand, kept for the next page.
    const PLACE_KEY = "plexora.location.remote";

    //: Each attached field's handle, by its input. For code that is handed
    //: only the box -- browsePicker.js's drop target, which has to move a
    //: field set to Remote back to This computer before a file dropped from
    //: this computer's own Explorer or Finder can mean anything in it.
    const HANDLES = new WeakMap();

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
     * Whether to ask at all.
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

    /** Note a side chosen by hand, and the machine when it is a remote one. */
    function chosenByHand(where, place) {
        lastSide = where;
        if (where !== REMOTE || !place || !place.id || place.kind === "server") return;
        try {
            localStorage.setItem(PLACE_KEY, place.id);
        } catch (e) { /* storage refused: this page still remembers lastPlace */ }
    }

    /**
     * The connected machine a new, empty field should open on, out of the
     * live list -- or null. Connected means a file could be named on it now:
     * a data node that is up, or a files-only server's open SFTP connection.
     * The server itself is not one: when it is elsewhere, This computer is
     * still the machine somebody is sitting at.
     *
     * Several connected: the one the caller remembers (`prefer`), then the
     * last one chosen on this page, then on the last page, then the first.
     */
    function connectedPlace(places, prefer) {
        const live = (places || []).filter(
            (place) => place && place.kind !== "server"
                       && (Boolean(place.node) || Boolean(place.files)));
        if (!live.length) return null;
        let stored = null;
        try {
            stored = localStorage.getItem(PLACE_KEY);
        } catch (e) { stored = null; }
        const found = [prefer, lastPlace && lastPlace.id, stored]
            .filter(Boolean)
            .map((id) => live.find((place) => place.id === id))
            .find(Boolean) || live[0];
        return { id: found.id, kind: found.kind, label: found.label,
                 node: found.node || null, files: found.files || null };
    }

    /**
     * @function whenConnected - hand `apply` the connected machine a new,
     *   empty field should open on, once the list of machines is known.
     *
     * Called at most once, and never on a page that has chosen This computer
     * by hand or that has no live list. Never synchronously: `attach` must
     * return its handle before the field changes under its caller. Whether
     * the field is still untouched by then is the caller's to check.
     *
     * @param prefer - the id of a machine the caller remembers, tried first.
     */
    function whenConnected(apply, prefer) {
        const remotes = window.PlexoraRemotes;
        if (lastSide === LOCAL || !remotes || typeof remotes.subscribe !== "function") return;
        let done = false;
        let stop = null;
        const answer = (snapshot) => {
            if (done) return;
            done = true;
            if (stop) stop();
            if (lastSide === LOCAL) return;
            const place = connectedPlace(snapshot && snapshot.places, prefer);
            if (place) apply(place);
        };
        const now = typeof remotes.snapshot === "function" ? remotes.snapshot() : null;
        if (now && now.loaded) {
            Promise.resolve().then(() => answer(now));
            return;
        }
        stop = remotes.subscribe((snapshot) => Promise.resolve().then(() => answer(snapshot)));
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
     * @function attach - ask "which machine?" of one path field.
     *
     * @param input the text input the field already had. When the value has to
     *   become a locator its `name` moves to a hidden companion, so what the
     *   form POSTs is the locator while what the user reads is their own path
     *   -- nobody should have to look at `node://hpc/cells-7f3a91c2` to know
     *   they picked /scratch/study/cells.h5ad.
     * @param options `kind` (image | segmentation | table), `filter`/`mode`
     *   for the browse dialog, and `onChange` called whenever the value the
     *   form would submit changes. `statusMount` overrides where the status
     *   line lands. `bare` draws no chip at all, for a caller that draws its
     *   own and moves this one with `setWhere()`.
     * @returns {{where, submitValue, blocking, release, element}} or null when
     *   the field is not shaped to render into.
     */
    function attach(input, options = {}) {
        if (!input) return null;
        // Every surface puts the input inside a row inside a field, and the
        // status line goes under the row. A caller that does not is one this
        // cannot render into -- better to leave their field exactly as it was
        // than to throw halfway through building it.
        const row = input.parentNode;
        const field = row && row.parentNode;
        if (!field) return null;

        const kind = options.kind || "table";
        //: Whether a browser upload is on the table for this field. Tables
        //: only -- see UPLOAD_SUFFIXES.
        const uploadable = kind === "table";
        const onChange = options.onChange || function () {};
        const formName = input.getAttribute("name") || "";

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
            // This field has already asked which machine, in its own chip.
            // Without the opt-out the shared layer (services/fileLocation.js)
            // asks again in a modal, so pressing Upload would mean answering
            // the same question twice in two different shapes.
            chooser.setAttribute("data-file-location", "local");
            chooser.addEventListener("change", () => {
                const file = chooser.files && chooser.files[0];
                if (file) upload(file);
            });
            uploadButton = el("button", "browse-button", "Upload…");
            uploadButton.type = "button";
            uploadButton.addEventListener("click", () => chooser.click());
        }

        // The chip goes IN the path box, at its front, as on the home page --
        // "which machine?" and "which file?" are one question asked in two
        // halves, and a control floating above the row reads as a setting for
        // the whole form rather than for this one field. The status line goes
        // under the field, where a sentence has room to be a sentence.
        //
        // `bare` is the home page and the Import dialog, which draw that chip
        // themselves (they also govern controls outside the box) and drive
        // this state with setWhere(). The state stays HERE either way -- it is
        // the one thing that knows what to submit for a path on another
        // machine. The chip itself is built below, once that state exists.
        const status = el("div", "data-location-status");
        // The button goes at the END of the row, beside Browse, because that
        // is what it is an alternative to.
        if (uploadButton) row.append(uploadButton, chooser);
        (options.statusMount || field).appendChild(status);

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
            // sitting at and knows the paths on -- until the list of machines
            // says one is connected, which moves an untouched field there (see
            // `whenConnected`). Switching by hand clears the box, which is
            // honest: nothing here knows what that file is called elsewhere.
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
        //: Whether this field's machine has been settled -- by a stored value,
        //: by a hand on the chip, by a caller, or by something typed. Until
        //: it has, a connected machine may still be adopted (`whenConnected`).
        let decided = Boolean(arriving);

        const Chip = window.PlexoraMachineChip;
        const chip = !options.bare && Chip ? Chip.create({
            current: () => ({ where: state.where, place: state.place }),
            onPick: picked,
            what: { image: "the image", segmentation: "the mask" }[kind] || "the file",
        }) : null;
        if (chip) Chip.wrap(input, chip);

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

        /**
         * The files-only server this field is set to, as `{base, home}`, or
         * null. Such a place has no node and needs none: its files are read
         * by this Plexora at `sftp://` addresses, so a path on it is turned
         * into one here and submitted as it is -- nothing is shared first.
         */
        function filesPlace() {
            if (state.where !== REMOTE || !state.place) return null;
            return state.place.files || null;
        }

        /** The `sftp://` address the box names on a files-only server. */
        function filesAddress() {
            const files = filesPlace();
            const typed = input.value.trim();
            if (!files || !typed) return "";
            const locators = window.PlexoraLocators;
            if (locators.isRemoteLocator(typed)) return typed;
            return locators.sftpUrl(files.base, typed, files.home);
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
            if (filesPlace()) return filesAddress();
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
            if (filesPlace()) return null;
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
            if (hidden) hidden.value = filesPlace() ? filesAddress()
                                                    : (state.locator || "");
            onChange(value());
        }

        function paint() {
            if (chip) chip.paint();

            const detached = !plainPath() && !nodeName() && !filesPlace();
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
            if (filesPlace()) {
                // Nothing is shared or prepared: the address is the answer.
                status.textContent = input.value.trim()
                    ? `Read from ${placeLabel()} over SFTP` : "";
                if (input.value.trim()) status.classList.add("is-ready");
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
         * A machine picked on the chip: Local, or one that is up -- the chip
         * connects a saved one before it hands it over. Remembered for the
         * next field either way, because the next field on the page is
         * usually on the same machine.
         */
        function picked(where, place) {
            decided = true;
            if (where === REMOTE && place) lastPlace = place;
            chosenByHand(where, place);
            choose(where, place);
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
            if (filesPlace()) {
                // Nothing to share: the address is read from here.
                paint();
                emit();
                return;
            }
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

        const handle = {
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
            setWhere: (where, place) => {
                decided = true;
                // A remote machine is remembered for the next field. This
                // computer is not: the callers moving a field here are mostly
                // a file dropped from this computer, which says where that one
                // file is rather than where everything is.
                if (where === REMOTE && place) {
                    lastPlace = place;
                    chosenByHand(REMOTE, place);
                }
                choose(where, place);
            },
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
                decided = true;
                if (serverIsRemote()) choose(REMOTE, serverPlace());
                else choose(LOCAL, null);
            },
            submitValue: value,
            blocking,
            release,
            /**
             * The node to browse on, or null for this server's own files. A
             * files-only server is browsed by its `sftp://` base address,
             * which the listing routes answer over its SFTP connection.
             */
            browseNode: () => nodeName()
                || (filesPlace() ? filesPlace().base : null),
            //: The chip, or null when the caller draws its own (`bare`).
            element: chip ? chip.element : null,
            //: Separate, because the two do not live together: the chip sits
            //: in the path box and the status line under the field.
            statusElement: status,
            //: The hidden file chooser behind Upload…, when this field offers it.
            chooser,
        };
        HANDLES.set(input, handle);

        // An empty field opens on a machine that is already connected, not on
        // This computer: connecting one is how somebody says where their data
        // is. Anything typed first, or any hand on the chip, wins.
        input.addEventListener("input", () => {
            if (input.value.trim()) decided = true;
        });
        if (!decided && options.connectedDefault !== false) {
            const prefer = typeof options.preferPlace === "function"
                ? options.preferPlace() : options.preferPlace;
            whenConnected((place) => {
                if (decided || state.where !== LOCAL || input.value.trim()) return;
                lastPlace = lastPlace || place;
                choose(REMOTE, place);
            }, prefer);
        }
        return handle;
    }

    /** The handle `attach` returned for this input, or null. */
    function handleOf(input) {
        return (input && HANDLES.get(input)) || null;
    }

    return { attach, available, clientNode, serverIsRemote, handleOf, whenConnected };
})();
