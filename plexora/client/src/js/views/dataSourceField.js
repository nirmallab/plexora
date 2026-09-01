/**
 * dataSourceField.js -- "which data file?", asked the same way everywhere.
 *
 * A path alone does not name a table. A .zarr store can hold several, and a
 * table can span several images, and neither is answerable from the file --
 * the server refuses to import until one is picked (see import_routes'
 * replace_project_data). The upload page has asked both questions since it was
 * written; the requirements modal and the edit page asked neither, so choosing
 * a multi-table store on either one posted a path the server could only
 * reject, with the message "choose which one to load" and nothing to choose
 * from.
 *
 * So the question lives here, once, and all three surfaces mount it: path
 * input, its picker, and the table/image selects that appear only when the
 * file itself forces the choice. What it produces is exactly the four keys
 * the server reads -- `data`, `table`, `subset_column`, `subset_value`.
 *
 * Two properties worth keeping:
 *
 * - **Nothing is inspected until the path changes.** The edit page mounts this
 *   with the project's stored file, already imported and already answered;
 *   re-opening it on load would re-read a multi-gigabyte store to ask a
 *   question that has an answer.
 * - **It reports what it is still waiting for.** `blocking()` is the caller's
 *   way to refuse to save rather than post something the server will reject --
 *   and to say why, in the same words the picker beside it is asking.
 */
window.PlexoraDataSourceField = (function () {
    const DATA_TYPE_LABELS = {
        csv: "CSV table",
        anndata: "AnnData",
        spatialdata: "SpatialData store",
    };

    const DEFAULT_HINT = "CSV, AnnData (.h5ad) or SpatialData (.zarr).";

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text) node.textContent = text;
        return node;
    }

    /**
     * @param container where to render
     * @param options   `value` the path already stored (never inspected),
     *   `id` for the input when a caller needs to address it, `hint` the
     *   resting note under the field, and `onChange` called with value()
     *   whenever any of the four keys changes.
     * @returns { value(), blocking(), element }
     */
    function mount(container, options = {}) {
        const initial = (options.value || "").trim();
        const onChange = options.onChange || function () {};

        const root = el("div", "data-source-field");
        const row = el("div", "import-field-row");
        const input = el("input", "form-control");
        input.type = "text";
        input.placeholder = options.placeholder || "/path/to/cells.csv";
        input.value = initial;
        if (options.id) input.id = options.id;
        row.appendChild(input);

        // Declared before it is built, because the browse buttons below close
        // over it and are wired first. `attach` no longer emits on mount --
        // that hazard is gone at the source -- but the ordering still stands.
        let location = null;

        // A .zarr store is a directory and a .csv/.h5ad is a file, and one
        // input takes both -- so one button takes both, in mode "any". There
        // used to be two, File… and Store…, which asked the user to classify
        // their own file before they were allowed to point at it.
        const browse = el("button", "browse-button", "Browse…");
        browse.type = "button";
        if (typeof attachBrowseButton === "function") {
            attachBrowseButton(browse, input, {
                mode: "any", filter: "data",
                // Asked at click time: the Local/Remote switch below can be
                // flipped long after this button was wired.
                node: () => (location ? location.browseNode() : null),
            });
        }
        row.appendChild(browse);
        root.appendChild(row);

        // "Which machine is this file on?" -- on every launch, because there is
        // always somewhere else it could be. Attached after `row` is in `root`,
        // because it mounts itself into the row and its status line into the
        // field around it.
        if (window.PlexoraDataLocation && window.PlexoraDataLocation.available()) {
            location = window.PlexoraDataLocation.attach(input, {
                kind: options.kind || "table",
                onChange: () => schedule(),
            });
        }

        const note = el("span", "field-hint", options.hint || DEFAULT_HINT);
        root.appendChild(note);

        const tableField = el("div", "data-source-extra");
        const tableSelect = el("select", "form-select");
        tableField.append(tableSelect,
            el("span", "field-hint",
               "This store holds several tables. Choose which one holds the cells."));
        tableField.hidden = true;
        root.appendChild(tableField);

        const subsetField = el("div", "data-source-extra");
        const subsetSelect = el("select", "form-select");
        const subsetHint = el("span", "field-hint");
        subsetField.append(subsetSelect, subsetHint);
        subsetField.hidden = true;
        root.appendChild(subsetField);

        container.appendChild(root);

        const state = {
            table: null,
            subsetColumn: null,
            subsetValue: null,
            blocking: null,
        };

        // Replies can arrive out of order when someone types quickly, and a
        // stale one would re-show a picker for a file no longer in the box.
        let token = 0;
        let timer = null;
        let pending = false;

        /** The path or node address this field would submit. */
        function submitted() {
            return location ? location.submitValue() : input.value.trim();
        }

        function value() {
            return {
                data: submitted(),
                table: state.table || undefined,
                subset_column: state.subsetColumn || undefined,
                subset_value: state.subsetValue || undefined,
            };
        }

        /** What still stops this being saved, or null. */
        function blocking() {
            // The file has to have reached the other machine before anything
            // can be read from it, so this comes first.
            const held = location && location.blocking();
            if (held) return held;
            if (pending) return "Still reading the data file — try again in a moment.";
            return state.blocking;
        }

        function emit() {
            onChange(value());
        }

        function reset() {
            state.table = null;
            state.subsetColumn = null;
            state.subsetValue = null;
            state.blocking = null;
            tableField.hidden = true;
            subsetField.hidden = true;
        }

        function schedule() {
            clearTimeout(timer);
            const path = submitted();
            // The stored file is already imported and already answered. Only a
            // change is a question, which is also what keeps a mount free.
            if (path === initial) {
                reset();
                input.classList.remove("is-valid", "is-invalid");
                note.textContent = options.hint || DEFAULT_HINT;
                emit();
                return;
            }
            reset();
            emit();
            if (!path) {
                input.classList.remove("is-valid", "is-invalid");
                note.textContent = options.hint || DEFAULT_HINT;
                return;
            }
            // Every inspection opens the file, and an .h5ad is not cheap to
            // open -- so wait for a pause in typing rather than per keystroke.
            timer = setTimeout(() => inspect(null), 250);
        }

        /**
         * @param table which table inside a .zarr store to look at, on the
         *   second pass. Everything below the table picker is a question about
         *   a table, so a multi-table store cannot answer any of it until one
         *   is named.
         */
        async function inspect(table) {
            const mine = ++token;
            const path = submitted();
            if (!path) return;
            pending = true;

            let payload;
            try {
                const response = await fetch(plexoraUrl("inspect_data"), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ path, table: table || null }),
                });
                payload = await response.json();
            } catch (e) {
                pending = false;
                return;  // transport failures are reported by PlexoraStatus
            }
            if (mine !== token) return;  // a newer path is in the box
            pending = false;

            if (!payload.ok) {
                input.classList.remove("is-valid");
                input.classList.add("is-invalid");
                note.textContent = payload.error || "Unreadable";
                state.blocking = payload.error || "That data file cannot be read.";
                emit();
                return;
            }

            input.classList.remove("is-invalid");
            input.classList.add("is-valid");
            note.textContent = DATA_TYPE_LABELS[payload.data_type] || payload.data_type;
            state.blocking = null;

            if (!table && (payload.tables || []).length > 1) {
                // Choosing re-runs this with the table, which is what puts the
                // image question -- a store with several tables has no answer
                // to it until one is named.
                showTables(payload.tables);
                emit();
                return;
            }
            state.table = table || payload.table || null;
            if ((payload.ambiguous || []).length) {
                showSubset(payload.ambiguous[0]);
            }
            emit();
        }

        function showTables(tables) {
            tableSelect.replaceChildren();
            tableSelect.append(new Option("Choose a table…", ""));
            tables.forEach((table) => {
                tableSelect.append(new Option(
                    `${table.name} — ${table.n_obs} cells × ${table.n_var} markers`,
                    table.name,
                ));
            });
            // Assigned rather than added: this runs again on every
            // re-inspection, and addEventListener would stack a handler each
            // time.
            tableSelect.onchange = () => {
                subsetField.hidden = true;
                state.subsetColumn = null;
                state.subsetValue = null;
                state.table = tableSelect.value || null;
                state.blocking = tableSelect.value
                    ? null : "Choose which table in the store holds the cells.";
                emit();
                if (tableSelect.value) inspect(tableSelect.value);
            };
            tableField.hidden = false;
            state.table = null;
            state.blocking = "Choose which table in the store holds the cells.";
        }

        function showSubset(ambiguous) {
            subsetHint.textContent =
                `This table covers several images (column "${ambiguous.column}"). `
                + "Choose the one this project's image shows.";
            subsetSelect.replaceChildren();
            subsetSelect.append(new Option("Choose an image…", ""));
            (ambiguous.values || []).forEach(
                (item) => subsetSelect.append(new Option(item, item)));
            subsetSelect.onchange = () => {
                state.subsetValue = subsetSelect.value || null;
                state.blocking = subsetSelect.value
                    ? null : "Choose which image in the table this project shows.";
                emit();
            };
            subsetField.hidden = false;
            state.subsetColumn = ambiguous.column;
            state.subsetValue = null;
            state.blocking = "Choose which image in the table this project shows.";
        }

        input.addEventListener("input", schedule);
        // browsePicker dispatches both; `input` alone would miss nothing, but
        // the upload page's fields listen on keyup and this stays in step.
        input.addEventListener("keyup", schedule);

        return { value, blocking, element: root, input, location };
    }

    return { mount };
})();
