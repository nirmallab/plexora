/**
 * pathPicker.js -- choosing a file on a machine that has no desktop.
 *
 * The native dialog (browsePicker.js) is the right control whenever there is a
 * screen for it to appear on. On a compute node there is not, and there never
 * will be: the server is running in a batch job on a machine with no display,
 * and `browse_for_path` there does not fail so much as wait for a person who
 * cannot be there. That used to leave one option -- know the path already and
 * type it -- which is a poor answer for somebody looking for a file they last
 * saw three months ago in a directory they named after a date.
 *
 * So this is the fallback: a listing of one directory at a time, from
 * /list_dir, drawn as a modal. It reads names, sizes and which entries are
 * folders. Never bytes, and never the whole filesystem -- one directory per
 * request, because /n/scratch is not a thing anybody wants to send over HTTP.
 *
 * Three rules the rest of this file follows:
 *
 *   1. **The client does no path arithmetic.** Every path it navigates to came
 *      from the server: `entry.path` for a row, `payload.crumbs[i].path` for a
 *      breadcrumb, `payload.parent` for Up. Joining names with "/" here worked
 *      until the node on the other end was a Windows box, and then it walked
 *      `C:\data` to `C:\data/runs` and to `` for its parent.
 *   2. **`state.here` is only ever assigned from a server answer.** Every
 *      gesture funnels into `show()`, and a listing that fails changes
 *      nothing: the user stays where they were, looking at the reason.
 *   3. **Nothing about remembering places may block browsing.** /picker_prefs
 *      failing means no Recent list, not a picker that will not open.
 *
 * Two things it deliberately is not. It is not a file manager -- there is no
 * rename, no delete, no upload, and the server route has no code for any of
 * them. And it is not a replacement for typing a path: the breadcrumb bar
 * turns into a text box on a click, because somebody who knows where their
 * file is should not have to click through six directories to say so.
 */
window.PlexoraPathPicker = (function () {
    //: What each filter accepts, so the listing greys out the rest rather than
    //: letting somebody pick a file the form is going to refuse. Kept in step
    //: with native_dialog.py's _TK_FILTERS -- the two describe the same
    //: choices, one for each kind of picker.
    const EXTENSIONS = {
        // ".zarr"/".ome.zarr" are here because the Image field takes an
        // OME-Zarr store, and this table is the client's one statement of what
        // each filter accepts. They change nothing about the listing itself: a
        // store is a directory, and `accepts` is only ever asked about files.
        // What makes one pickable is mode "any" -- see STORE_SUFFIXES.
        // ".dcm" is a file rather than a store: one instance of a DICOM slide
        // selects the whole slide, whose other instances the server gathers
        // from the metadata. The folder that holds them is choosable too, by
        // the same "Use this folder" button an OME-Zarr store uses.
        image: [".tif", ".tiff", ".ome.tif", ".ome.tiff", ".svs", ".ndpi",
                ".scn", ".bif", ".qptiff", ".dcm", ".png", ".jpg", ".jpeg",
                ".zarr", ".ome.zarr", ".mrxs"],
        csv: [".csv"],
        h5ad: [".h5ad"],
        data: [".csv", ".tsv", ".txt", ".h5ad"],
        channels: [".csv", ".tsv", ".txt", ".xlsx", ".xlsm"],
        any: null,
    };

    //: The folders that are a thing rather than a place. A .zarr store is a
    //: directory whose contents -- `.zgroup`, `0/`, `labels/` -- are chunks
    //: nobody wants to look at, so in mode "any" one click SELECTS it, the way
    //: a click selects a file, and the "›" beside it is there for the rare
    //: case of needing to go in.
    //:
    //: Only a naming convention, and knowingly so: the honest test is whether
    //: the directory holds a `.zgroup`, which costs a listing per row. The
    //: cost of being wrong is small in both directions -- a store not named
    //: `.zarr` is still choosable by the "Use this folder" button, and a plain
    //: folder that happens to end in `.zarr` selects instead of opening, which
    //: the "›" undoes.
    const STORE_SUFFIXES = [".zarr", ".ome.zarr"];

    function isStore(entry) {
        if (!entry || !entry.is_dir) return false;
        const lowered = String(entry.name || "").toLowerCase();
        return STORE_SUFFIXES.some((suffix) => lowered.endsWith(suffix));
    }

    //: What the Type column says. A fourth table of suffixes, and deliberately
    //: so: this one decides nothing -- not what is offered, not what is
    //: accepted, not what the server will read -- it only puts a word next to
    //: a filename so a directory of `s1.ome.tif` and `s1.csv` reads at a
    //: glance. Longest suffix first, so `.ome.tif` beats `.tif`.
    const TYPE_LABELS = [
        [".ome.tiff", "OME-TIFF"], [".ome.tif", "OME-TIFF"],
        [".qptiff", "QPTIFF"], [".tiff", "TIFF"], [".tif", "TIFF"],
        [".svs", "Aperio SVS"], [".ndpi", "Hamamatsu NDPI"], [".scn", "Leica SCN"],
        [".mrxs", "MIRAX slide"], [".bif", "Ventana BIF"],
        [".dcm", "DICOM"],
        [".png", "PNG"], [".jpeg", "JPEG"], [".jpg", "JPEG"],
        [".h5ad", "AnnData"], [".zarr", "Zarr store"],
        [".csv", "CSV"], [".tsv", "TSV"], [".txt", "Text"],
        [".xlsx", "Excel"], [".xlsm", "Excel"],
    ];

    //: Row ids have to be unique on the page for aria-activedescendant, and a
    //: picker can in principle be opened twice.
    let pickerCount = 0;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function button(className, text, title) {
        const node = el("button", className, text);
        node.setAttribute("type", "button");
        if (title) node.setAttribute("title", title);
        return node;
    }

    function accepts(name, filter) {
        const allowed = EXTENSIONS[filter];
        if (!allowed) return true;
        const lowered = name.toLowerCase();
        return allowed.some((suffix) => lowered.endsWith(suffix));
    }

    /** Bytes as something a person reads at a glance. */
    function readableSize(bytes) {
        if (bytes === null || bytes === undefined) return "";
        const units = ["B", "KB", "MB", "GB", "TB"];
        let value = bytes;
        let unit = 0;
        while (value >= 1024 && unit < units.length - 1) {
            value /= 1024;
            unit += 1;
        }
        return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`;
    }

    /** The word in the Type column. Display only -- see TYPE_LABELS. */
    function typeLabel(entry) {
        const lowered = String(entry.name || "").toLowerCase();
        const known = TYPE_LABELS.find(([suffix]) => lowered.endsWith(suffix));
        if (entry.is_dir) {
            // A .zarr store is a folder, and calling it one would hide the
            // single most important thing about it on the Data field.
            return known && known[1] === "Zarr store" ? known[1] : "Folder";
        }
        if (known) return known[1];
        const dot = lowered.lastIndexOf(".");
        return dot > 0 ? lowered.slice(dot + 1).toUpperCase() : "File";
    }

    /**
     * The last segment of a path, for a sidebar label. Display only: the
     * button navigates to the whole path, which came from the server -- this
     * splitter never produces something anything is asked to open.
     */
    function shortName(path) {
        const parts = String(path).split(/[\\/]/).filter(Boolean);
        return parts.length ? parts[parts.length - 1] : path;
    }

    /** The record /picker_prefs answers with, when there is nothing to say. */
    function noPlaces() {
        return { last_dir: "", recent: [], pinned: [] };
    }

    /**
     * @function pick - open the listing and resolve with a path, or null if
     *   the user closed it without choosing.
     * @param mode - "any", "file" or "directory". "any" is what the path
     *   fields ask for and takes either kind: files select as they always did,
     *   a folder that looks like a store (STORE_SUFFIXES) selects on a click
     *   rather than opening, and "Use this folder" answers with whichever
     *   folder is open -- so a store under any name is still reachable. In
     *   "directory" mode the chosen thing is always the folder currently open.
     * @param filter - one of EXTENSIONS' keys; greys out everything else.
     * @param start - where to open. A file's path opens the folder holding it
     *   (the server does that), so a field that already has a value can hand
     *   it straight over. Absent: wherever this machine was left last time,
     *   and failing that the user's home.
     * @param title - what the dialog says it is for.
     * @param node - whose filesystem to list. Absent means this server's; a
     *   node name means that machine's, relayed through /list_dir. That is the
     *   only way to browse a cluster, and the reason a path box set to Remote
     *   is not just a box to type into any more.
     * @param multiple - resolve with an array of paths instead of one. File
     *   mode only. No field wires this yet; the picker answers it so that the
     *   first one to need it does not have to reopen this file.
     * @returns Promise<string | string[] | null>
     */
    function pick({ mode = "file", filter = "any", start = "",
                    title = "Choose a file", node = "",
                    multiple = false } = {}) {
        const many = Boolean(multiple) && mode === "file";
        //: One button on the field opened this, and it did not ask which kind
        //: of thing the user has. Neither does this.
        const hybrid = mode === "any";
        const domId = `path-picker-${++pickerCount}`;

        const state = {
            here: "",
            parent: null,
            //: null means the server did not send any -- an older node. The
            //: crumb bar then stays a text box, which is what it was before
            //: breadcrumbs existed and is still perfectly usable.
            crumbs: null,
            entries: [],
            visible: [],
            rows: [],
            truncated: false,
            query: "",
            showHidden: false,
            selected: null,
            chosen: [],
            anchor: -1,
            cursor: -1,
            history: [],
            places: noPlaces(),
            //: Bumped on every navigation so a slow answer for a directory the
            //: user has already left cannot redraw the one they are in. NFS is
            //: slow enough for this to be an ordinary occurrence, not a race.
            epoch: 0,
        };

        // -- the dialog ------------------------------------------------------
        //
        // Built node by node rather than from an HTML string: the breadcrumb
        // bar and the sidebar are rendered over and over from server answers,
        // so this file needs element handles anyway -- and holding them
        // directly is what lets tests/js/path_picker_probe.mjs run the shipped
        // file against a DOM small enough to read.

        const dialog = el("dialog", "path-picker");
        const body = el("div", "path-picker-body");
        const heading = el("h2", "path-picker-title", title);

        const columns = el("div", "path-picker-columns");
        const places = el("aside", "path-picker-places");
        places.setAttribute("aria-label", "Places");
        const main = el("div", "path-picker-main");

        const bar = el("div", "path-picker-bar");
        const backButton = button("btn btn-secondary path-picker-step", "\u2190", "Back");
        const upButton = button("btn btn-secondary path-picker-step", "\u2191", "Up one folder");
        const refreshButton = button("btn btn-secondary path-picker-step", "\u21BB", "Reload this folder");
        const pinButton = button("btn btn-secondary path-picker-pin", "\u2606", "Pin this folder");
        const hiddenLabel = el("label", "path-picker-hidden");
        const hiddenBox = el("input");
        hiddenBox.type = "checkbox";
        hiddenLabel.append(hiddenBox);
        hiddenLabel.append(el("span", null, "Hidden files"));
        bar.append(backButton);
        bar.append(upButton);
        bar.append(refreshButton);
        bar.append(pinButton);
        bar.append(hiddenLabel);

        // The address bar: one wide strip that looks like the box it is about
        // to become, with the crumbs sitting inside it. Clicking anywhere in
        // it that is not a crumb turns it into that box. It began as a pencil
        // at the end of the trail, which was correct, discoverable by nobody,
        // and about four millimetres wide.
        const address = el("div", "path-picker-address");
        const crumbBar = el("nav", "path-picker-crumbs");
        crumbBar.setAttribute("aria-label", "Path");
        const crumbEdit = el("input", "form-control path-picker-crumb-edit");
        crumbEdit.type = "text";
        crumbEdit.setAttribute("spellcheck", "false");
        crumbEdit.setAttribute("aria-label", "Folder path");
        crumbEdit.setAttribute("placeholder", "/path/to/folder");
        crumbEdit.hidden = true;
        address.append(crumbBar);
        address.append(crumbEdit);

        const tools = el("div", "path-picker-tools");
        const search = el("input", "form-control path-picker-search");
        search.type = "search";
        search.setAttribute("aria-label", "Filter this folder");
        search.setAttribute("placeholder", "Filter by name");
        const count = el("span", "path-picker-count");
        tools.append(search);
        tools.append(count);

        const error = el("div", "path-picker-error");
        error.setAttribute("role", "alert");
        error.hidden = true;

        const head = el("div", "path-picker-head");
        head.append(el("span", "path-picker-name", "Name"));
        head.append(el("span", "path-picker-type", "Type"));
        head.append(el("span", "path-picker-size", "Size"));

        const list = el("ul", "path-picker-list");
        list.setAttribute("role", "listbox");
        list.setAttribute("tabindex", "0");
        list.setAttribute("aria-label", "Folder contents");
        if (many) list.setAttribute("aria-multiselectable", "true");

        const note = el("div", "path-picker-note");

        const actions = el("div", "path-picker-actions");
        const cancel = button("btn btn-secondary", "Cancel");
        // The way out of the one thing STORE_SUFFIXES can get wrong. A store
        // named `run7` rather than `run7.zarr` draws as an ordinary folder and
        // opens on a click -- and from inside it, this answers with it. Only
        // in "any": in file mode a folder is never the answer, and in
        // directory mode it is the ONLY answer and is what Choose already
        // says.
        const useFolder = hybrid
            ? button("btn btn-secondary", "Use this folder",
                     "Choose the folder that is open, whatever it is called")
            : null;
        const choose = button("btn btn-primary",
                              mode === "directory" ? "Use this folder" : "Choose");
        actions.append(cancel);
        if (useFolder) actions.append(useFolder);
        actions.append(choose);

        main.append(bar);
        main.append(address);
        main.append(tools);
        main.append(error);
        main.append(head);
        main.append(list);
        main.append(note);
        columns.append(places);
        columns.append(main);
        body.append(heading);
        body.append(columns);
        body.append(actions);
        dialog.append(body);
        document.body.appendChild(dialog);

        //: What `pick` will resolve with. Held here rather than passed to
        //: `resolve` at each call site because closing the dialog is the one
        //: exit every path shares -- Esc included, which the browser handles on
        //: its own and which would otherwise leave the caller waiting forever
        //: for an answer the user has already given.
        let answer = null;

        //: Set the moment the dialog closes, so the answer to a preferences
        //: write that lands afterwards is not painted into a detached sidebar.
        let closed = false;

        // -- what is currently picked ---------------------------------------

        /**
         * An entry's full path, as the server gave it. The join is a fallback
         * for a node too old to send one, and infers its separator from the
         * directory it is standing in rather than assuming this machine's.
         */
        function entryPath(entry) {
            if (entry && typeof entry.path === "string" && entry.path) {
                return entry.path;
            }
            const separator = state.here.indexOf("\\") >= 0 ? "\\" : "/";
            const base = state.here.endsWith(separator)
                ? state.here : state.here + separator;
            return base + entry.name;
        }

        function isChosen(entry) {
            if (!entry || mode === "directory") return false;
            const path = entryPath(entry);
            return many
                ? state.chosen.indexOf(path) >= 0
                : state.selected === path;
        }

        /** The chosen files in the order they are drawn in, not clicked in. */
        function orderedChosen() {
            const order = state.entries.map(entryPath);
            return state.chosen.slice()
                .sort((a, b) => order.indexOf(a) - order.indexOf(b));
        }

        function currentAnswer() {
            // Opening a folder IS choosing it in directory mode, which is how
            // a .zarr store is picked: there is nothing inside it to select.
            if (mode === "directory") return state.here || null;
            if (many) return state.chosen.length ? orderedChosen() : null;
            return state.selected || null;
        }

        function finish(result) {
            answer = result;
            // Where this leaves us is recorded by the `close` handler rather
            // than here, because Esc and a click on the backdrop close the
            // dialog without ever coming through this function.
            dialog.close();
        }

        // -- drawing ---------------------------------------------------------

        function setError(message) {
            error.textContent = message || "";
            error.hidden = !message;
        }

        function setBusy(busy) {
            list.classList.toggle("is-loading", busy);
            list.setAttribute("aria-busy", busy ? "true" : "false");
        }

        function paintSelection() {
            state.rows.forEach((row, index) => {
                const on = isChosen(state.visible[index]);
                row.classList.toggle("is-cursor", index === state.cursor);
                row.classList.toggle("is-selected", on && !many);
                row.classList.toggle("is-checked", on && many);
                row.setAttribute("aria-selected", on ? "true" : "false");
            });
            const cursorRow = state.rows[state.cursor];
            list.setAttribute("aria-activedescendant",
                              cursorRow ? cursorRow.getAttribute("id") : "");
            choose.disabled = currentAnswer() === null;
        }

        function renderCount() {
            if (!state.query) {
                count.textContent = "";
                return;
            }
            // "0 of 2000 shown" reads as "your file is not there" when what it
            // means is "your file is past the cut" -- so say which 2000.
            count.textContent = state.truncated
                ? `${state.visible.length} of the first ${state.entries.length} shown`
                : `${state.visible.length} of ${state.entries.length} shown`;
        }

        function visibleEntries() {
            const query = state.query.trim().toLowerCase();
            if (!query) return state.entries;
            return state.entries.filter(
                (entry) => String(entry.name).toLowerCase().indexOf(query) >= 0);
        }

        function renderList() {
            state.visible = visibleEntries();
            state.rows = [];
            list.replaceChildren();
            state.visible.forEach((entry, index) => {
                const usable = entry.is_dir || accepts(entry.name, filter);
                const row = el("li", "path-picker-row");
                row.setAttribute("role", "option");
                row.setAttribute("id", `${domId}-row-${index}`);
                if (entry.is_dir) row.classList.add("is-folder");
                // A file the field cannot take is shown rather than hidden:
                // "my file is not in this list" is much harder to act on than
                // "my file is greyed out", which says the format is the issue.
                if (!usable) row.classList.add("is-muted");
                row.append(el("span", "path-picker-name", entry.name));
                row.append(el("span", "path-picker-type", typeLabel(entry)));
                row.append(el("span", "path-picker-size",
                              entry.is_dir ? "" : readableSize(entry.size)));
                if (hybrid && isStore(entry)) {
                    // A store's row answers a click by selecting, so this is
                    // the only way into it -- for the once in a hundred times
                    // somebody wants the table inside rather than the store.
                    row.classList.add("is-store");
                    const enter = button("path-picker-enter", "›",
                                         `Open ${entry.name}`);
                    enter.setAttribute("aria-label", `Open ${entry.name}`);
                    enter.addEventListener("click", (event) => {
                        // Or the row beneath it takes the click as well and
                        // selects the store it has just navigated away from.
                        event?.stopPropagation?.();
                        openEntry(entry);
                    });
                    row.append(enter);
                }
                row.addEventListener("click", (event) => onRowClick(entry, index, event));
                row.addEventListener("dblclick", () => onRowActivate(entry, index));
                list.appendChild(row);
                state.rows.push(row);
            });
            if (state.cursor >= state.rows.length) state.cursor = -1;
            paintSelection();
            renderCount();
        }

        function renderCrumbs() {
            crumbBar.replaceChildren();
            if (!state.crumbs) return;
            state.crumbs.forEach((crumb, index) => {
                const step = button("path-picker-crumb", crumb.label);
                if (index === state.crumbs.length - 1) {
                    step.setAttribute("aria-current", "location");
                }
                step.addEventListener("click", (event) => {
                    // A crumb is somewhere to go. Without this the click also
                    // reaches the bar behind it, which would open the text box
                    // over the folder that was just asked for.
                    event?.stopPropagation?.();
                    show(crumb.path);
                });
                crumbBar.appendChild(step);
            });
            // The rest of the bar, as a real button rather than a bare click
            // handler on the strip: it is the whole empty half of the address
            // bar, it is reachable by Tab, and it says what it does.
            const edit = button("path-picker-crumb-open", "\u270E",
                                "Click to type or paste a path");
            edit.setAttribute("aria-label", "Type or paste a path");
            edit.addEventListener("click", (event) => {
                event?.stopPropagation?.();
                beginCrumbEdit();
            });
            crumbBar.appendChild(edit);
        }

        function renderPin() {
            const on = state.places.pinned.indexOf(state.here) >= 0;
            pinButton.textContent = on ? "\u2605" : "\u2606";
            pinButton.setAttribute("title", on ? "Unpin this folder" : "Pin this folder");
            pinButton.setAttribute("aria-pressed", on ? "true" : "false");
            pinButton.disabled = !state.here;
        }

        function placeButton(label, path, hint) {
            const entry = button("path-picker-place", label, hint || path);
            entry.addEventListener("click", () => show(path));
            return entry;
        }

        function renderPlaces() {
            places.replaceChildren();
            places.appendChild(placeButton("Home", "", "Your home folder"));
            [["Pinned", state.places.pinned], ["Recent", state.places.recent]]
                .forEach(([label, paths]) => {
                    if (!paths.length) return;
                    places.appendChild(el("div", "path-picker-places-head", label));
                    paths.forEach((path) => {
                        places.appendChild(placeButton(shortName(path), path));
                    });
                });
        }

        // -- navigation ------------------------------------------------------

        /**
         * Show one directory. The single way `state.here` ever changes, and
         * the single place a listing is fetched.
         *
         * `history` says what the back stack does with the answer: push it,
         * pop back to it, or leave the stack alone (a refresh, a hidden-files
         * toggle). `fallbackHome` is for the very first listing only -- a
         * remembered directory that has since been deleted must not open an
         * empty picker.
         */
        async function show(path, { history = "push", fallbackHome = false } = {}) {
            const ticket = ++state.epoch;
            setBusy(true);
            let payload = null;
            let failure = null;
            try {
                const response = await fetch(plexoraUrl("list_dir"), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        path: path || "",
                        node: node || "",
                        show_hidden: state.showHidden,
                    }),
                });
                payload = await response.json();
                if (!response.ok) throw new Error(payload && payload.error);
            } catch (e) {
                failure = e?.message || "That folder could not be read.";
            }
            if (ticket !== state.epoch) {
                // Somewhere else was asked for while this was in flight. That
                // request owns the view now, busy state included.
                return;
            }
            setBusy(false);

            if (failure) {
                if (fallbackHome) {
                    await show("", { history, fallbackHome: false });
                    if (state.here) {
                        note.textContent =
                            `Could not open ${path} — showing ${state.here} instead.`;
                    }
                    return;
                }
                // Whatever is on screen stays on screen. Being thrown back to
                // your home directory because you mistyped one folder is worse
                // than the mistake.
                setError(failure);
                return;
            }

            setError(null);
            state.here = payload.path;
            state.parent = payload.parent || null;
            state.crumbs = Array.isArray(payload.crumbs) ? payload.crumbs : null;
            state.entries = payload.entries || [];
            state.truncated = Boolean(payload.truncated);
            // A filter is about the folder you typed it in. Carrying "cells"
            // into the next directory shows an empty one and looks like it.
            state.query = "";
            search.value = "";
            state.cursor = -1;
            state.anchor = -1;
            state.selected = null;
            state.chosen = [];

            if (history === "push") {
                if (state.history[state.history.length - 1] !== state.here) {
                    state.history.push(state.here);
                }
            } else if (history === "pop") {
                state.history.pop();
                if (state.history.length) {
                    state.history[state.history.length - 1] = state.here;
                } else {
                    state.history.push(state.here);
                }
            }

            if (state.crumbs) endCrumbEdit();
            else beginCrumbEdit();
            crumbEdit.value = state.here;

            renderCrumbs();
            renderPin();
            renderList();
            backButton.disabled = state.history.length < 2;
            upButton.disabled = !state.parent;
            // Nothing to use until a directory has actually been listed. Set
            // here rather than in paintSelection because it is about where the
            // picker is standing, not about what is picked in it.
            if (useFolder) useFolder.disabled = !state.here;
            note.textContent = state.truncated
                ? `Showing the first ${state.entries.length} entries — click the `
                  + "path bar above to type one and go straight there."
                : "";
        }

        function goUp() {
            if (state.parent) show(state.parent);
        }

        function goBack() {
            if (state.history.length < 2) return;
            show(state.history[state.history.length - 2], { history: "pop" });
        }

        function openEntry(entry) {
            show(entryPath(entry));
        }

        // -- the crumb bar as a text box ------------------------------------

        function beginCrumbEdit() {
            crumbEdit.value = state.here;
            crumbEdit.hidden = false;
            crumbBar.hidden = true;
            if (crumbEdit.focus) crumbEdit.focus();
            // Selected, so the next thing typed or pasted replaces the path
            // rather than landing in the middle of it. Pasting is the gesture
            // this box exists for.
            if (crumbEdit.select) crumbEdit.select();
        }

        function endCrumbEdit() {
            crumbEdit.hidden = true;
            crumbBar.hidden = false;
        }

        // -- what a click does ----------------------------------------------

        function onRowClick(entry, index, event) {
            state.cursor = index;
            if (hybrid && isStore(entry)) {
                // The one folder a click picks rather than enters. What is
                // inside a .zarr is chunks, and the field asked for the store.
                state.selected = entryPath(entry);
                paintSelection();
                return;
            }
            if (entry.is_dir) {
                // Every other folder is a place, and one click goes there.
                openEntry(entry);
                return;
            }
            if (mode === "directory" || !accepts(entry.name, filter)) {
                paintSelection();
                return;
            }
            const path = entryPath(entry);
            if (many) {
                selectMany(path, index, event);
            } else {
                state.selected = path;
            }
            paintSelection();
        }

        function selectMany(path, index, event) {
            if (event && event.shiftKey && state.anchor >= 0) {
                const from = Math.min(state.anchor, index);
                const to = Math.max(state.anchor, index);
                const range = [];
                for (let i = from; i <= to; i += 1) {
                    const each = state.visible[i];
                    if (each && !each.is_dir && accepts(each.name, filter)) {
                        range.push(entryPath(each));
                    }
                }
                state.chosen = range;
                return;
            }
            state.anchor = index;
            if (event && (event.ctrlKey || event.metaKey)) {
                state.chosen = state.chosen.indexOf(path) >= 0
                    ? state.chosen.filter((each) => each !== path)
                    : [...state.chosen, path];
                return;
            }
            state.chosen = [path];
        }

        function onRowActivate(entry, index) {
            if (hybrid && isStore(entry)) {
                // Double-click is "this one" everywhere else in this list, and
                // a store is a thing to choose. The "›" is what opens it.
                state.selected = entryPath(entry);
                paintSelection();
                finish(currentAnswer());
                return;
            }
            if (entry.is_dir) {
                openEntry(entry);
                return;
            }
            if (mode === "directory" || !accepts(entry.name, filter)) return;
            const path = entryPath(entry);
            if (many) {
                selectMany(path, index, null);
            } else {
                state.selected = path;
            }
            paintSelection();
            finish(currentAnswer());
        }

        function moveCursor(step) {
            if (!state.rows.length) return;
            let next = state.cursor + step;
            if (next < 0) next = 0;
            if (next > state.rows.length - 1) next = state.rows.length - 1;
            state.cursor = next;
            paintSelection();
            const row = state.rows[next];
            if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
        }

        /**
         * Keys, read off `event.target` rather than `document.activeElement`:
         * the two agree in a browser, and only one of them is a thing this
         * file can be tested against.
         */
        function onKeydown(event) {
            const tag = String((event.target && event.target.tagName) || "").toUpperCase();
            if (tag === "INPUT" || tag === "TEXTAREA") return;
            if (event.key === "Enter" && tag === "BUTTON") {
                // The browser turns Enter on a focused button into a click.
                // Answering it here as well closed the picker on whatever was
                // selected the moment somebody pressed Enter on "Up".
                return;
            }
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                event.preventDefault();
                moveCursor(event.key === "ArrowDown" ? 1 : -1);
                return;
            }
            if (event.key === "Enter") {
                event.preventDefault();
                const entry = state.visible[state.cursor];
                if (entry) onRowActivate(entry, state.cursor);
                else if (!choose.disabled) finish(currentAnswer());
                return;
            }
            if (event.key === "Backspace") {
                event.preventDefault();
                goUp();
            }
        }

        // -- remembering where you were --------------------------------------

        async function loadPrefs() {
            try {
                const response = await fetch(plexoraUrl(
                    `picker_prefs?node=${encodeURIComponent(node || "")}`));
                const record = await response.json();
                if (response.ok && record && typeof record === "object") {
                    return {
                        last_dir: typeof record.last_dir === "string" ? record.last_dir : "",
                        recent: Array.isArray(record.recent) ? record.recent : [],
                        pinned: Array.isArray(record.pinned) ? record.pinned : [],
                    };
                }
            } catch (e) {
                // Deliberately silent, and deliberately not fatal: a picker
                // that will not open because a preferences file could not be
                // read is a much worse failure than one with no Recent list.
            }
            return noPlaces();
        }

        async function savePrefs(change) {
            try {
                const response = await fetch(plexoraUrl("picker_prefs"), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ node: node || "", ...change }),
                });
                const record = await response.json();
                if (closed) return;
                if (response.ok && record && Array.isArray(record.pinned)) {
                    state.places = {
                        last_dir: typeof record.last_dir === "string" ? record.last_dir : "",
                        recent: Array.isArray(record.recent) ? record.recent : [],
                        pinned: record.pinned,
                    };
                    renderPlaces();
                    renderPin();
                }
            } catch (e) {
                // As above. The pin star is already drawn; it will be right
                // again the next time this opens.
            }
        }

        /**
         * Where the picker was left, written once as it closes.
         *
         * On the way out rather than on a successful pick, because "the folder
         * I was in last time" is the thing worth reopening at and browsing to
         * it is what costs the effort -- somebody who walks six directories
         * into /n/scratch, does not find what they wanted and closes the picker
         * has to walk all six again next time. Cancelling is not an instruction
         * to forget.
         *
         * The Recent list still only records a folder a file was actually
         * taken from: it is a list of places that turned out to be worth
         * something, and every folder merely passed through on the way would
         * make it a history rather than a shortcut.
         *
         * Standing still writes nothing at all. Opening the picker at the
         * folder it already remembers and closing it again is not news, and
         * this is a read-modify-write of a file on disk.
         */
        function remember() {
            if (!state.here) return;
            const chose = answer !== null && answer !== undefined;
            if (!chose && state.here === state.places.last_dir) return;
            savePrefs(chose
                ? { last_dir: state.here, add_recent: state.here }
                : { last_dir: state.here });
        }

        function togglePin() {
            if (!state.here) return;
            const on = state.places.pinned.indexOf(state.here) >= 0;
            state.places.pinned = on
                ? state.places.pinned.filter((each) => each !== state.here)
                : [...state.places.pinned, state.here];
            renderPlaces();
            renderPin();
            savePrefs(on ? { unpin: state.here } : { pin: state.here });
        }

        // -- wiring ----------------------------------------------------------

        cancel.addEventListener("click", () => finish(null));
        choose.addEventListener("click", () => finish(currentAnswer()));
        if (useFolder) {
            useFolder.addEventListener("click", () => finish(state.here || null));
        }
        backButton.addEventListener("click", () => goBack());
        upButton.addEventListener("click", () => goUp());
        refreshButton.addEventListener("click", () => show(state.here, { history: "keep" }));
        pinButton.addEventListener("click", () => togglePin());
        hiddenBox.addEventListener("change", () => {
            state.showHidden = Boolean(hiddenBox.checked);
            show(state.here, { history: "keep" });
        });

        search.addEventListener("input", () => {
            state.query = search.value || "";
            renderList();
        });
        search.addEventListener("keydown", (event) => {
            if (event.key !== "Escape") return;
            if (!(search.value || "")) return;
            // Esc empties the box first. Without both of these the browser's
            // own dialog-cancel fires and the whole picker closes, which is a
            // startling amount to lose for clearing a filter.
            event.preventDefault();
            event.stopPropagation();
            search.value = "";
            state.query = "";
            renderList();
        });

        // Anywhere in the strip that is not a crumb. The guard is what lets
        // somebody click into the middle of the path they are already editing
        // without the whole thing being re-selected under them.
        address.addEventListener("click", () => {
            if (crumbEdit.hidden) beginCrumbEdit();
        });

        crumbEdit.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                const typed = (crumbEdit.value || "").trim();
                if (state.crumbs) endCrumbEdit();
                show(typed);
                return;
            }
            if (event.key === "Escape") {
                // Same guard as the search box, and the same reason.
                event.preventDefault();
                event.stopPropagation();
                crumbEdit.value = state.here;
                if (state.crumbs) endCrumbEdit();
            }
        });

        dialog.addEventListener("keydown", onKeydown);

        const promise = new Promise((resolve) => {
            dialog.addEventListener("close", () => {
                // Before `closed`, and before the dialog leaves the page: the
                // request goes out synchronously, and removing the element it
                // was started from does not cancel it.
                remember();
                closed = true;
                dialog.remove();
                resolve(answer);
            });
        });

        // Nothing is choosable until a directory has actually been listed --
        // in directory mode the answer IS the directory, and there is not one
        // yet.
        choose.disabled = true;
        backButton.disabled = true;
        upButton.disabled = true;
        if (useFolder) useFolder.disabled = true;
        renderPlaces();
        dialog.showModal();
        // showModal focuses the first focusable thing it finds, which is the
        // Back button. The list is what the arrow keys are for.
        if (list.focus) list.focus();

        (async function open() {
            state.places = await loadPrefs();
            renderPlaces();
            let opening = String(start || "").trim();
            // A field in verbatim mode holds `node://laptop/cells-7f3a91c2`,
            // which is an address rather than a path and would fail to list.
            if (opening.slice(0, 7) === "node://") opening = "";
            if (!opening) opening = state.places.last_dir || "";
            await show(opening, { fallbackHome: Boolean(opening) });
        })();

        return promise;
    }

    return { pick, accepts };
})();
