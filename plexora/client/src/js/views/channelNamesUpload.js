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
 * cannot: the only way in is the path box, which names a file on the machine
 * running the server. On a cluster that is where the marker list actually is,
 * and the browser has no way to open it.
 *
 * ONE way in, not two. This used to offer a browser upload above the path box
 * as well. Locally the two do the same thing -- the Browse button opens a
 * native file dialog and writes the path it comes back with -- so the pair was
 * a choice between two spellings of the same act, presented before the user
 * has done anything. The route still accepts an uploaded file; nothing here
 * sends one.
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
     *  file that was edited since the preview was drawn. */
    function formData(extra) {
        const form = new FormData();
        form.append("datasource", session.datasource);
        form.append("path", session.path);
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
        // The only way in. The image and its marker list are on whatever
        // machine is running Plexora -- on a cluster the browser is on a
        // laptop that cannot see either. Same control as the home page's, for
        // the same reason.
        const path = el("div", "channel-names-path");
        const label = el("label", "channel-names-path-label",
            "Path to the file on the machine running Plexora");
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
        // Opens a native dialog on the SERVER, which is the machine the path
        // has to be valid on. Absent when browsePicker.js has not loaded, and
        // silent about it: the box beside it still works by hand, which is
        // what that helper's own fallback amounts to anyway.
        if (typeof attachBrowseButton === "function") {
            attachBrowseButton(browse, input, { mode: "file", filter: "channels" });
        }
        const load = button("btn btn-primary channel-names-load", "Load", () => {
            const value = input.value.trim();
            if (!value) return;
            session.path = value;
            session.description = null;
            submit();
        });
        load.disabled = true;
        row.append(input, browse, load);
        path.append(label, row, el("p", "field-hint",
            "On a cluster or a remote server the file is usually beside the image, "
            + "where the browser cannot reach it."));
        // The path the last attempt used, so a file that came back as the
        // wrong column or the wrong count is one edit away rather than one
        // re-typed cluster path away.
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
            description: null,
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
