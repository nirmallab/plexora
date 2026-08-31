/**
 * channelNamesUpload.js -- naming the image's channels from a file the user has.
 *
 * A multiplexed image usually arrives with its channels called Channel_0 …
 * Channel_n, and the panel that says what they really are lives in a separate
 * CSV or spreadsheet. This is the way that list gets in. Nothing else in the
 * viewer can be trusted until it has: gating matches markers to channels BY
 * NAME, so an unnamed image is one where the marker names silently match
 * nothing.
 *
 * The flow is one dialog with three stages, because they are three answers to
 * the same question and moving between them must not lose the file:
 *
 *   source    which file, and where it is
 *   choose    which column in it (only when the file does not say by itself)
 *   mismatch  the count is wrong, so nothing was changed
 *
 * The server decides which stage comes next -- see POST /upload_channels in
 * server/routes/data_routes.py. This file never parses the file itself, and
 * cannot: what it sends is a path or the bytes, and the reading is the
 * server's.
 *
 * **Which machine, asked the way every other path field asks it.** This box
 * used to mean one machine and one only -- whichever Plexora happened to be
 * running on -- with Browse opening a dialog there, and an Upload button
 * appearing beside it when that was somewhere else. Two machines, and no way
 * to name a third: a marker list on the cluster a data node reaches had no way
 * in at all, which is the arrangement somebody most often has one in.
 *
 * So the row carries the same Local/Remote switch the import fields do (see
 * services/dataLocation.js, whose vocabulary this deliberately repeats), and
 * what it settles is where the file is READ:
 *
 *   a disk the server can open   the path goes over as a path, exactly as it
 *                                always did, and the bytes never come here
 *   a data node                  the bytes are read through the node relay
 *                                and posted as an upload
 *   this computer, unattached    the browser's own bytes, through Upload…
 *
 * Reading through the node is worth it here and would not be for an image: a
 * marker list is a few kilobytes, so the round trip through this tab costs
 * nothing anybody can measure. That relay -- POST /fetch_file, reached through
 * PlexoraFileLocation.read -- is what makes the third case possible; its
 * absence is what an older comment here called "left undone deliberately".
 *
 * A <dialog> opened with showModal(), like requirementsModal.js and unlike
 * segmentationWait.js. A modal dialog is promoted to the top layer, ABOVE the
 * fullscreen element and its opaque ::backdrop, so this one is visible over a
 * fullscreened viewer without going through PopoverPortal. An ordinary
 * positioned element on <body> would not be.
 */
window.PlexoraChannelNames = (function () {
    "use strict";

    const TITLE = "Channel names";
    const SUBTITLE = "One name per channel, in the order the image stacks them. "
        + "CSV, TSV, TXT, XLSX or XLSM.";

    //: The two sides of the switch, spelled as dataLocation.js spells them --
    //: they end up in the same aria-labels and the same CSS.
    const LOCAL = "local";
    const REMOTE = "remote";

    let dialog = null;
    //: The dialog's five fixed regions, held rather than looked up. A stage
    //: swaps what is inside them and never rebuilds them.
    let parts = null;
    //: The buttons the current stage put in the action row, so busy state can
    //: be applied to exactly them.
    let actionButtons = [];
    //: Whether a request is out. See submit().
    let busy = false;
    //: Everything that survives a stage change. `path` above all: the user
    //: typed that once, and being sent back to an empty box because they
    //: ticked a checkbox would be the modal losing their work.
    let session = null;

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

    /**
     * The machine Plexora is running on, as a place.
     *
     * Labelled the way the place picker labels it, so somebody who opens the
     * list from the chip does not find a second name for the machine the chip
     * was already showing.
     */
    function serverPlace() {
        return { id: "server", kind: "server", label: "This Plexora server",
                 node: null };
    }

    //: The data node `plexora connect` started on the browser's own machine,
    //: when there is one. It is what makes "this computer" nameable by path
    //: from a Plexora running elsewhere.
    function clientNodeName() {
        const location = window.PlexoraDataLocation;
        return (location && location.clientNode && location.clientNode()) || "";
    }

    /**
     * The node that has to be asked to read this file, "" when the server can
     * open it itself.
     *
     * The derivation everything else here hangs off, and the same one
     * dataLocation makes: a path is only ever a plain path when the machine
     * holding the file is the machine running Plexora, and every other
     * combination needs a node in between.
     */
    function nodeName() {
        if (session.where === LOCAL) {
            return serverIsElsewhere() ? clientNodeName() : "";
        }
        const place = session.place;
        if (!place || place.kind === "server") return "";
        // `registered_node` as well as `node`: a data node outlives the
        // Plexora that started it, so after a restart `node` is empty for a
        // machine that is up and answering.
        return place.node || place.registered_node || "";
    }

    /** Whether what is in the box is a path this server can open unaided. */
    function plainPath() {
        if (session.where === LOCAL) return !serverIsElsewhere();
        return Boolean(session.place) && session.place.kind === "server";
    }

    /**
     * Every machine a marker list could be read from right now, or null when
     * the list could not be got at all.
     *
     * Null rather than an empty array, and the difference decides what happens
     * next: "nothing is connected" is answered by opening a connection, and
     * "I could not ask" is answered by the picker, which says so properly.
     */
    async function reachablePlaces() {
        try {
            const list = await window.PlexoraPlacePicker.places();
            return list.filter((place) => place.kind === "server"
                                          || place.node
                                          || place.registered_node);
        } catch (e) {
            return null;
        }
    }

    /** A file on another machine, fetched as bytes this dialog can post. */
    function readOn(node, path) {
        const layer = window.PlexoraFileLocation;
        if (!layer || !layer.read) {
            return Promise.reject(new Error(
                "This page cannot read files on another machine."));
        }
        return layer.read(node, path);
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    //: Every number on the picker is a small one, and "1 names" beside "2
    //: channels" reads as a bug in the line that is meant to be settling the
    //: user's doubt about the file.
    function plural(count, word) {
        return `${count} ${word}${count === 1 ? "" : "s"}`;
    }

    function button(className, text, onClick) {
        const node = el("button", className, text);
        node.type = "button";
        node.addEventListener("click", onClick);
        return node;
    }

    // ---------------------------------------------------------------- shell

    function buildDialog() {
        const node = el("dialog", "channel-names-modal");
        const form = el("form", "channel-names-form");
        // method="dialog" so Enter inside a field closes rather than
        // submitting to the page's URL; every real action is a button below.
        form.method = "dialog";
        parts = {
            title: el("h2", "channel-names-title", TITLE),
            subtitle: el("p", "channel-names-subtitle", SUBTITLE),
            body: el("div", "channel-names-body"),
            error: el("div", "channel-names-error"),
            actions: el("div", "channel-names-actions"),
        };
        parts.error.setAttribute("role", "alert");
        parts.error.hidden = true;
        form.append(parts.title, parts.subtitle, parts.body, parts.error, parts.actions);
        node.appendChild(form);
        document.body.appendChild(node);
        return node;
    }

    /** Replace the stage: its heading, its body, and its buttons, together.
     *  One call rather than three, because a stage that changed its body and
     *  kept the previous stage's buttons is the failure mode worth designing
     *  out -- "Use this column" left over on the mismatch screen would apply
     *  the column that just failed. */
    function stage({ title, subtitle, body, actions }) {
        parts.title.textContent = title;
        parts.subtitle.textContent = subtitle;
        parts.body.replaceChildren(...body);
        parts.actions.replaceChildren(...actions);
        actionButtons = actions;
        clearError();
    }

    function showError(message) {
        // A successful rename closes the dialog from inside the same try, so
        // anything thrown after that has nowhere to be shown. Reported to the
        // console rather than swallowed silently.
        if (!parts) {
            console.error("channelNamesUpload:", message);
            return;
        }
        parts.error.textContent = message;
        parts.error.hidden = false;
    }

    function clearError() {
        parts.error.hidden = true;
        parts.error.textContent = "";
    }

    function setBusy(busy) {
        actionButtons.forEach((node) => { node.disabled = busy; });
    }

    function close() {
        if (!dialog) return;
        dialog.close();
        dialog.remove();
        dialog = null;
        parts = null;
        actionButtons = [];
        session = null;
        busy = false;
    }

    // ------------------------------------------------------------- the post

    /** The chosen file, restated. Every request carries it: the server holds
     *  nothing between calls, which is what keeps a re-read from picking up a
     *  file that was edited since the preview was drawn.
     *
     *  One of the two, never both -- `_channel_file_source` prefers the upload
     *  and would quietly ignore a path sent beside it, so sending both would
     *  make the wrong one look like it had been read. */
    function formData(extra) {
        const form = new FormData();
        form.append("datasource", session.datasource);
        if (session.file) form.append("file", session.file);
        else form.append("path", session.path);
        Object.keys(extra || {}).forEach((key) => form.append(key, extra[key]));
        return form;
    }

    async function submit(extra) {
        // The stages' own controls stay live while a request is out -- "Load"
        // sits in the body rather than the action row, and the file picker
        // fires on change with nothing to click twice. One flag is a surer
        // guard against a second upload than greying three scattered buttons,
        // and it survives a stage swapping the action row underneath it.
        if (busy) return;
        busy = true;
        setBusy(true);
        clearError();
        const task = window.PlexoraStatus?.begin?.("Channel names");
        try {
            const response = await fetch(plexoraUrl("upload_channels"), {
                method: "POST",
                body: formData(extra),
            });
            const result = await response.json();
            if (result.success) {
                task?.done?.();
                return applied(result.names || []);
            }
            // Neither of these is a failure -- they are the server asking for
            // one more thing, or reporting a file that does not fit. Both get
            // a stage; only a real error gets the red line.
            if (result.needs_column) {
                task?.done?.();
                return renderChoose(result);
            }
            if (result.mismatch) {
                task?.done?.();
                return renderMismatch(result);
            }
            throw new Error(result.error || "That file could not be read.");
        } catch (error) {
            task?.fail?.("Channel names");
            showError(error.message || "That file could not be read.");
        } finally {
            busy = false;
            setBusy(false);
        }
    }

    function applied(names) {
        const onApplied = session.onApplied;
        close();
        onApplied(names);
    }

    // ------------------------------------------------------- stage: source

    function renderSource() {
        const path = el("div", "channel-names-path");
        const label = el("label", "channel-names-path-label", "Path to the file");
        label.htmlFor = "channel_names_path";
        const row = el("div", "import-field-row");
        const input = el("input", "form-control");
        input.type = "text";
        input.id = "channel_names_path";
        input.autocomplete = "off";
        input.spellcheck = false;
        input.placeholder = "/path/to/markers.csv";
        const browse = el("button", "browse-button", "Browse…");
        browse.type = "button";
        // Which machine's dialog to open, asked at CLICK time rather than
        // wired once: the switch below can be flipped long after this button
        // was built, and then the same button means a different filesystem.
        // Absent when browsePicker.js has not loaded, and silent about it: the
        // box beside it still works by hand, which is what that helper's own
        // fallback amounts to anyway.
        if (typeof attachBrowseButton === "function") {
            attachBrowseButton(browse, input, {
                mode: "file",
                filter: "channels",
                node: () => nodeName() || null,
            });
        }
        const load = button("btn btn-primary channel-names-load", "Load",
                            () => loadChosen());
        load.disabled = true;

        // "Which machine is this file on?", in the switch every other path
        // field in Plexora asks it with. One letter a side, for the reason
        // dataLocation gives: this sits inside a row that already has a box
        // and three buttons in it, and the words spent more of that row than
        // the box they govern.
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
        //: One line under the row, for the waits and the failures. The error
        //: region above the actions is for what the SERVER said about the
        //: file; this is for what happened getting hold of it.
        const status = el("div", "data-location-status");
        const hint = el("p", "field-hint");

        // Sending the bytes -- the one thing a browser can do that naming a
        // path cannot. Built only where it can ever be the answer, so that a
        // desktop launch has exactly one way in: there, Browse opens a native
        // dialog on the machine that is also running the server, and a second
        // control beside it would be two spellings of one act.
        let chooser = null;
        let upload = null;
        if (serverIsElsewhere()) {
            chooser = el("input");
            chooser.type = "file";
            chooser.accept = ".csv,.tsv,.txt,.xlsx,.xlsm";
            chooser.hidden = true;
            // This row asks which machine in its own switch, two elements to
            // the left. Without the opt-out the shared layer
            // (services/fileLocation.js) asks again in a modal, so pressing
            // Upload would mean answering one question twice in two shapes.
            chooser.setAttribute("data-file-location", "local");
            chooser.addEventListener("change", () => {
                const chosen = chooser.files && chooser.files[0];
                if (!chosen) return;
                session.file = chosen;
                session.path = "";
                session.description = null;
                submit();
            });
            upload = button("browse-button", "Upload…", () => chooser.click());
        }

        row.append(location, input, browse);
        if (upload) row.append(upload, chooser);
        row.append(load);
        path.append(label, row, status, hint);

        /**
         * Remote is a question rather than a setting: "somewhere else" is not
         * one place, so choosing it has to name which. Pressing a side that is
         * already answered does nothing -- the chip is how the machine gets
         * changed, and re-asking on every click would make the control feel as
         * though it had forgotten.
         */
        function press(where) {
            if (where === REMOTE
                    && !(session.where === REMOTE && session.place)) {
                return choosePlace(false);
            }
            if (where !== session.where) choose(where, null);
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
                    return choose(REMOTE, reachable[0]);
                }
                if (reachable && !reachable.length) return connectSomewhere();
            }
            const picked = await window.PlexoraPlacePicker.pick({
                current: (session.place && session.place.id) || "",
            });
            if (!picked) {
                // Cancelled with nothing chosen before: back to the side that
                // still works, rather than stranded on a Remote that is not
                // any machine.
                if (session.where === REMOTE && !session.place) {
                    choose(LOCAL, null);
                }
                return;
            }
            choose(REMOTE, picked);
        }

        /** Nothing to pick from, so picking is the wrong question. */
        async function connectSomewhere() {
            if (!window.PlexoraConnectionModal) return;
            const opened = await window.PlexoraConnectionModal.open({
                kind: "node",
                intent: "No other machine is connected yet. Open one and this "
                        + "dialog can read a marker list from it.",
            });
            if (opened && opened.connected) {
                return choose(REMOTE, {
                    id: opened.name,
                    kind: "remote",
                    label: opened.label || opened.name,
                    node: opened.node || null,
                });
            }
            if (session.where === REMOTE && !session.place) choose(LOCAL, null);
        }

        function choose(where, place) {
            session.where = where;
            session.place = where === REMOTE ? (place || null) : null;
            // What was in the box described another machine's filesystem, and
            // a path that means something over there means nothing here -- so
            // it goes, rather than sitting in the box looking answered.
            session.path = "";
            session.file = null;
            session.description = null;
            input.value = "";
            input.classList.remove("is-invalid");
            load.disabled = true;
            if (parts) clearError();
            paint();
        }

        /** The machine currently chosen, as somebody would say it. */
        function machine() {
            if (session.where === LOCAL) return "this computer";
            return session.place ? session.place.label : "";
        }

        function hintText(detached) {
            if (session.where === REMOTE && !session.place) {
                return "Choose the machine this file is on.";
            }
            if (detached) {
                return "Plexora is running elsewhere and cannot read paths on "
                       + "this computer — send the file with Upload… instead.";
            }
            if (plainPath()) {
                return serverIsElsewhere()
                    ? "The box and Browse mean the machine running Plexora, "
                      + "where the file usually sits beside the image."
                    : "The box and Browse mean this computer, which is also "
                      + "the machine running Plexora.";
            }
            return `The box and Browse mean ${machine()}, and Plexora reads `
                   + "the file from there.";
        }

        function paint() {
            Object.keys(sides).forEach((where) => {
                const on = where === session.where;
                sides[where].classList.toggle("is-active", on);
                sides[where].setAttribute("aria-checked", on ? "true" : "false");
            });
            chip.hidden = session.where !== REMOTE;
            chip.textContent = session.place ? session.place.label : "Choose…";
            chip.classList.toggle("is-unset", !session.place);

            // With nothing that can read a path on the chosen machine, the box
            // stops pretending to take one -- and Browse with it, which would
            // otherwise open a dialog on a machine nobody chose.
            const detached = !plainPath() && !nodeName();
            input.disabled = detached;
            browse.disabled = detached;
            if (upload) upload.hidden = !(detached && session.where === LOCAL);
            hint.textContent = hintText(detached);
        }

        /**
         * Read the named file and hand it to the server.
         *
         * Two shapes, decided by which machine holds it. A path the server can
         * open goes over as a path, exactly as it always did, and a file on a
         * cluster never touches this browser. A file on a data node is fetched
         * through the relay and posted as bytes, because the server has no
         * path for it -- affordable only because a marker list is kilobytes,
         * which is why the same trick would be wrong for the image.
         */
        async function loadChosen() {
            const value = input.value.trim();
            if (!value || busy) return;
            session.description = null;
            session.path = value;
            if (plainPath()) {
                session.file = null;
                return submit();
            }
            const node = nodeName();
            if (!node) return;

            busy = true;
            setBusy(true);
            load.disabled = true;
            clearError();
            status.className = "data-location-status is-busy";
            status.textContent = `Reading from ${machine()}…`;
            const task = window.PlexoraStatus?.begin?.("Channel names");
            let file;
            try {
                file = await readOn(node, value);
            } catch (error) {
                task?.fail?.("Channel names");
                busy = false;
                setBusy(false);
                if (!session) return;
                status.className = "data-location-status";
                status.textContent = "";
                load.disabled = false;
                showError(error.message || "That file could not be read.");
                return;
            }
            busy = false;
            setBusy(false);
            task?.done?.();
            // Closed while the read was out. Nothing to hand the bytes to, and
            // `session` is gone -- so this stops rather than throwing into a
            // dialog that is no longer on screen.
            if (!session) return;
            status.className = "data-location-status";
            status.textContent = "";
            session.file = file;
            submit();
        }

        paint();
        // The path the last attempt used, so a file that came back as the
        // wrong column or the wrong count is one edit away rather than one
        // re-typed cluster path away. Armed without re-checking: the switch
        // has not moved since it was loaded, so the machine that could read it
        // then can read it now.
        if (session.path) {
            input.value = session.path;
            load.disabled = false;
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
            // for a box somebody is going to press Load on anyway.
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

        stage({
            title: TITLE,
            subtitle: SUBTITLE,
            body: [path],
            actions: [button("btn btn-secondary", "Cancel", close)],
        });
        input.focus();
    }

    // ------------------------------------------------------- stage: choose

    /** How many names the current answer would produce.
     *
     *  Computed here rather than asked for, so ticking the checkbox is
     *  instant and cannot be answered against a file the server re-read in
     *  the meantime. It mirrors channel_file.names() exactly: empty cells do
     *  not count, and the header row comes off the top only if it has
     *  anything in this column. */
    function nameCount(description, column, hasHeader) {
        const entry = description.columns[column];
        if (!entry) return 0;
        return entry.nonempty - (hasHeader && entry.header ? 1 : 0);
    }

    function columnLabel(description, column, hasHeader) {
        const entry = description.columns[column];
        const named = hasHeader && entry.header;
        return named ? entry.header : `Column ${column + 1}`;
    }

    function renderChoose(description) {
        session.description = description;
        // Only ever a starting point. The server guesses a header when
        // dropping the first row is what makes some column come out at the
        // channel count -- a reading, not a fact about the file.
        let hasHeader = Boolean(description.header_guess);
        let column = 0;

        const check = el("label", "channel-names-check");
        const box = el("input");
        box.type = "checkbox";
        box.checked = hasHeader;
        check.append(box, el("span", null, "File contains column headers"));

        const field = el("div", "channel-names-field");
        const selectLabel = el("label", null, "Channel name column");
        selectLabel.htmlFor = "channel_names_column";
        const select = el("select", "form-select");
        select.id = "channel_names_column";
        field.append(selectLabel, select);

        const preview = el("div", "channel-names-preview-wrap");
        const note = el("p", "channel-names-preview-note");
        const count = el("p", "channel-names-count");

        function paintSelect() {
            select.replaceChildren();
            description.columns.forEach((entry) => {
                const names = nameCount(description, entry.index, hasHeader);
                const option = new Option(
                    `${columnLabel(description, entry.index, hasHeader)} — ${plural(names, "name")}`,
                    String(entry.index));
                select.append(option);
            });
            select.value = String(column);
        }

        function paintPreview() {
            const table = el("table", "channel-names-preview");
            const shown = description.preview;
            const width = Math.max(0, ...shown.map((row) => row.length));

            const head = el("tr");
            for (let index = 0; index < width; index += 1) {
                const cell = el("th", null, columnLabel(description, index, hasHeader));
                cell.classList.toggle("is-selected", index === column);
                head.appendChild(cell);
            }
            const thead = el("thead");
            thead.appendChild(head);
            table.appendChild(thead);

            const body = el("tbody");
            // The header row is a label once the box is ticked, and it is
            // already drawn as one above -- leaving it in the body too would
            // show it twice and make the preview disagree with the count.
            shown.slice(hasHeader ? 1 : 0).forEach((row) => {
                const tr = el("tr");
                for (let index = 0; index < width; index += 1) {
                    const cell = el("td", null, row[index] || "");
                    cell.classList.toggle("is-selected", index === column);
                    tr.appendChild(cell);
                }
                body.appendChild(tr);
            });
            table.appendChild(body);
            preview.replaceChildren(table);

            const total = description.column_count;
            const shownColumns = Math.min(total, description.preview_columns);
            note.textContent = total > shownColumns
                ? `First ${shown.length} rows, first ${shownColumns} of ${total} columns — scroll sideways for more.`
                : `First ${shown.length} rows of ${description.row_count}.`;
            // The one case the preview cannot show what was picked. Said
            // plainly rather than left as a table with no highlight in it.
            if (column >= shownColumns) {
                note.textContent += ` ${columnLabel(description, column, hasHeader)} is past the preview.`;
            }
        }

        function paintCount() {
            const names = nameCount(description, column, hasHeader);
            const channels = description.channel_count;
            count.textContent = names === channels
                ? `${plural(names, "name")} — one for each of this image's ${plural(channels, "channel")}.`
                : `${plural(names, "name")}, but this image has ${plural(channels, "channel")}.`;
            count.classList.toggle("is-mismatch", names !== channels);
        }

        function repaint() {
            paintSelect();
            paintPreview();
            paintCount();
        }

        box.addEventListener("change", () => {
            hasHeader = box.checked;
            repaint();
        });
        select.addEventListener("change", () => {
            column = Number(select.value);
            paintPreview();
            paintCount();
        });

        repaint();

        const columnsNote = description.columns_truncated
            ? [el("p", "field-hint",
                `Only the first ${description.columns.length} columns are offered.`)]
            : [];

        stage({
            title: "Which column has the channel names?",
            subtitle: `${description.filename} has ${description.column_count} columns. `
                + `This image has ${description.channel_count} channels.`,
            body: [check, field, preview, note, ...columnsNote, count],
            actions: [
                button("btn btn-secondary", "Choose a different file", renderSource),
                button("btn btn-primary", "Use this column", () => submit({
                    column: String(column),
                    has_header: hasHeader ? "true" : "false",
                })),
            ],
        });
    }

    // ----------------------------------------------------- stage: mismatch

    /**
     * The count is wrong, so nothing happened.
     *
     * Stated as two numbers rather than as an error, because it is nearly
     * always the right kind of file for the wrong image -- a panel list from
     * the run next door. Partially applying it was never an option: an image
     * whose first thirty channels are named and whose last ten are still
     * Channel_30 looks named, and every wrong name in it would be believed by
     * gating.
     */
    function renderMismatch(result) {
        const summary = el("div", "channel-names-mismatch");
        const counts = el("div", "channel-names-mismatch-counts");
        // The number and its noun are two elements, not one sentence: the
        // number is set large and read first, and the noun under it is what
        // says which of the two it is.
        [
            [result.marker_count, "name", "in the file"],
            [result.channel_count, "channel", "in the image"],
        ].forEach(([value, noun, where]) => {
            const box = el("div", "channel-names-mismatch-count");
            box.append(
                el("span", "channel-names-mismatch-value", String(value)),
                el("span", "channel-names-mismatch-label",
                   `${noun}${value === 1 ? "" : "s"} ${where}`));
            counts.appendChild(box);
        });
        summary.appendChild(counts);
        summary.appendChild(el("p", "channel-names-note",
            "Nothing was changed. Upload a file with one name per channel, in the "
            + "order the image stacks them."));

        // Back to the picker only when there was one. Arriving here from a
        // single-column file means there was never a column to reconsider,
        // and the only useful move is a different file.
        const back = session.description
            ? [button("btn btn-secondary", "Back to columns",
                () => renderChoose(session.description))]
            : [];

        stage({
            title: "That file does not match this image",
            subtitle: result.filename
                ? `Read from ${result.filename}.`
                : "Read from the file you chose.",
            body: [summary],
            actions: [
                ...back,
                button("btn btn-secondary", "Choose a different file", renderSource),
                button("btn btn-primary", "Close", close),
            ],
        });
    }

    // ----------------------------------------------------------------- open

    /**
     * Ask for a channel-name file and apply it.
     *
     * @param options.datasource the project whose channels are being renamed
     * @param options.onApplied  called with the applied names, in imageData
     *                           order, once the server has accepted them; the
     *                           caller decides what to do about a page that is
     *                           now showing the old ones
     */
    function open(options) {
        close();
        session = {
            datasource: options.datasource,
            onApplied: options.onApplied || function () {},
            path: "",
            //: A file staged from this computer, when the server is on
            //: another one. Held across stages for the same reason `path` is:
            //: picking a column must not mean choosing the file again.
            file: null,
            description: null,
            //: Which machine the box means, and which one Remote is. Opened on
            //: whichever side submits the box the way this dialog always has
            //: -- the server's own disk -- so nothing about the ordinary route
            //: through here changes because a switch appeared beside it. Held
            //: on the session so that going back for a different file lands on
            //: the machine the last one came from.
            where: serverIsElsewhere() ? REMOTE : LOCAL,
            place: serverIsElsewhere() ? serverPlace() : null,
        };
        dialog = buildDialog();
        // Escape is a plain way out at every stage. Nothing here is saved
        // half-way -- the server applies a whole list or none of it -- so
        // there is no state to warn about losing.
        dialog.addEventListener("cancel", close);
        renderSource();
        dialog.showModal();
    }

    return { open };
})();
