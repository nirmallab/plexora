/**
 * fileExplorer.js -- Plexora's file explorer: the one window every Browse
 * opens, on this machine or on a node, for a file, several files, a folder or
 * a dataset.
 *
 * It lists one directory at a time from /list_dir: names, sizes, dates and
 * which entries are folders or datasets. Never bytes, and never the whole
 * filesystem -- /n/scratch is not a thing anybody wants to send over HTTP.
 * Sizes and dates for a big folder arrive afterwards from /stat_entries, the
 * rows on screen first; a search below the open folder is /search_dir, which
 * stops on a budget rather than walking a cluster.
 *
 * Three rules the rest of this file follows, carried over from the picker it
 * replaces (tests/test_file_explorer.py pins them):
 *
 *   1. **The client does no path arithmetic.** Every path it navigates to came
 *      from the server: `entry.path` for a row, `crumbs[i].path` for a crumb,
 *      `parent` for Up, a place's `path` in the sidebar. Joining names with
 *      "/" worked until the node on the other end was a Windows box.
 *   2. **`state.here` is only ever assigned from a server answer**, in
 *      `show()`, and a listing that fails changes nothing: the user stays
 *      where they were, looking at the reason. History moves only on success.
 *   3. **Nothing about remembering places may block browsing.** /picker_prefs
 *      and /fs_places failing mean no Recent list and no volumes, not an
 *      explorer that will not open.
 *
 * It is not a file manager: there is no rename, no delete, no upload, and the
 * routes have no code for any of them.
 *
 * `window.PlexoraPathPicker` is this same object, so every caller written for
 * the old picker -- `pick({mode, filter, start, title, node, multiple})` and
 * `accepts(name, filter)` -- keeps working unchanged.
 */
window.PlexoraFileExplorer = (function () {
    "use strict";

    const T = () => window.PlexoraExplorerTypes;

    //: Names per /stat_entries request; the route refuses more.
    const META_CHUNK = 500;
    //: Typing pauses this long before a search below the folder is sent.
    const SEARCH_DELAY = 300;
    const SEARCH_LIMIT = 500;
    const SEARCH_BUDGET_MS = 3000;
    const SEARCH_DEPTH = 8;
    //: A listing faster than this never shows "Reading…".
    const LOADING_DELAY = 250;
    //: How long a greyed row's explanation stays in the footer.
    const EXPLAIN_MS = 4000;
    //: View changes (a sort, a column width) are batched into one write.
    const VIEW_SAVE_DELAY = 400;
    const TYPEAHEAD_MS = 700;

    const VIEW_DEFAULTS = {
        sort_by: "name",
        sort_dir: "asc",
        dirs_first: true,
        show_hidden: false,
        layout: "list",
        columns: { mtime: 150, kind: 120, size: 80 },
        sidebar: { collapsed: [] },
    };

    let explorerCount = 0;

    function noPrefs() {
        return { last_dir: "", recent: [], pinned: [] };
    }

    function strings(value) {
        return Array.isArray(value) ? value.filter((item) => typeof item === "string") : [];
    }

    function cleanView(raw) {
        const view = { ...VIEW_DEFAULTS, columns: { ...VIEW_DEFAULTS.columns },
                       sidebar: { collapsed: [] } };
        if (!raw || typeof raw !== "object") return view;
        if (["name", "mtime", "kind", "size"].indexOf(raw.sort_by) >= 0) view.sort_by = raw.sort_by;
        if (raw.sort_dir === "asc" || raw.sort_dir === "desc") view.sort_dir = raw.sort_dir;
        if (typeof raw.dirs_first === "boolean") view.dirs_first = raw.dirs_first;
        if (typeof raw.show_hidden === "boolean") view.show_hidden = raw.show_hidden;
        if (raw.layout === "list" || raw.layout === "grid") view.layout = raw.layout;
        if (raw.columns && typeof raw.columns === "object") {
            ["mtime", "kind", "size"].forEach((name) => {
                const width = Number(raw.columns[name]);
                if (Number.isFinite(width) && width >= 40 && width <= 2000) view.columns[name] = width;
            });
        }
        if (raw.sidebar && Array.isArray(raw.sidebar.collapsed)) {
            view.sidebar.collapsed = strings(raw.sidebar.collapsed);
        }
        return view;
    }

    function defaultTitle(want) {
        if (want.kind === "directory") return "Choose a folder";
        if (want.kind === "dataset") return "Choose a dataset";
        if (want.kind === "any") return "Choose a file or folder";
        return want.many ? "Choose files" : "Choose a file";
    }

    function abortable() {
        return typeof AbortController === "function" ? new AbortController() : null;
    }

    async function postJson(path, body, controller) {
        const response = await fetch(plexoraUrl(path), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
            ...(controller ? { signal: controller.signal } : {}),
        });
        let payload = null;
        try {
            payload = await response.json();
        } catch (error) {
            payload = null;
        }
        return { response, payload };
    }

    /**
     * @function pick - open the explorer and resolve with what was chosen:
     *   a path, an array of paths (`select: "many"` or legacy `multiple`), or
     *   null when it was closed without choosing.
     * @param accept - `{files, directories, datasets}`: `files` true or a list
     *   of suffixes, `datasets` true or a list of dataset types (zarr,
     *   spatialdata, xenium, visium, visium_hd, dicom). Absent: derived from
     *   the legacy `mode` ("file", "directory", "any") and `filter` (one of
     *   PlexoraExplorerTypes.EXTENSIONS' keys).
     * @param select - "one" or "many".
     * @param primary - the primary button's label; derived when absent
     *   ("Open", "Select", "Use Folder").
     * @param description - one line under the title saying what this is for.
     * @param start - where to open. A file's path opens the folder holding it
     *   (the server does that). Absent: wherever this machine was left last
     *   time, and failing that the user's home.
     * @param node - whose filesystem to list. Absent means this server's.
     * @param systemDialog - `{label, available() -> Promise<"hybrid"|"kinds"|
     *   "none">, open(kind) -> Promise<answer|null>}`, from browsePicker.js:
     *   the machine's own file dialog, offered in the overflow menu where one
     *   exists.
     * @param windowId - names the remembered window size and position.
     * @returns Promise<string | string[] | null>
     */
    function pick({ accept = null, select = null, primary = "", description = "",
                    windowId = "file-explorer", systemDialog = null,
                    mode = "file", filter = "any", multiple = false,
                    start = "", title = "", node = "" } = {}) {
        const types = T();
        const { el, button, glyph, setIcon, shortName, readableSize, fullDate, count, usable,
                storeOf } = types;
        const want = types.normalizeAccept({ accept, select, mode, filter, multiple });
        const domId = `plx-explorer-${++explorerCount}`;
        node = node || "";

        const state = {
            here: "",
            parent: null,
            crumbs: null,
            dataset: null,
            platform: "",
            partial: false,
            truncated: false,
            total: null,
            listed: false,
            back: [],
            forward: [],
            prefs: noPrefs(),
            view: cleanView(null),
            places: null,
            systemDialog: null,
            deep: false,
            //: Bumped on every navigation so a slow answer for a directory the
            //: user has already left cannot redraw the one they are in. NFS is
            //: slow enough for this to be an ordinary occurrence, not a race.
            epoch: 0,
            //: The ticket of the listing on screen. Chunks of sizes belong to
            //: it, and keep coming while a navigation that may yet fail is
            //: in flight.
            listedEpoch: 0,
            metaEpoch: 0,
            searchEpoch: 0,
            listAbort: null,
            metaAbort: null,
            searchAbort: null,
            searchTimer: null,
            loadingTimer: null,
            explainTimer: null,
            viewTimer: null,
            pendingView: null,
            typeahead: "",
            typeaheadTimer: null,
        };

        //: What `pick` resolves with. Held here rather than passed to
        //: `resolve` at each call site because closing the dialog is the one
        //: exit every path shares -- Esc included.
        let answer = null;
        //: Set the moment the window closes, so an answer that lands later is
        //: not painted into a detached window.
        let closed = false;

        // -- the window --------------------------------------------------------

        const win = window.PlexoraFloatingWindow.create({
            id: windowId,
            title: title || defaultTitle(want),
            subtitle: node ? `on ${node}` : "",
            className: "plx-explorer",
        });
        const dialog = win.dialog;
        dialog.setAttribute("data-explorer", domId);

        function entryPath(entry) {
            if (entry && typeof entry.path === "string" && entry.path) return entry.path;
            // The one tolerated join: a node too old to send `entry.path`. It
            // infers the separator from the directory it is standing in rather
            // than assuming this machine's.
            const separator = state.here.indexOf("\\") >= 0 ? "\\" : "/";
            const base = state.here.endsWith(separator) ? state.here : state.here + separator;
            return base + entry.name;
        }

        const crumbs = window.PlexoraExplorerCrumbs.create({
            onGo: (path) => show(path),
            onBack: () => goBack(),
            onForward: () => goForward(),
            onUp: () => goUp(),
            onRefresh: () => refresh(),
            onHome: () => show(""),
            onPin: () => togglePin(state.here),
            onContextMenu: (crumb, point) => crumbMenu(crumb, point),
        });

        const sidebar = window.PlexoraExplorerSidebar.create({
            onGo: (path) => show(path),
            onReorderPins: (order) => {
                state.prefs.pinned = order.slice();
                savePrefs({ pinned_order: order });
            },
            onContextMenu: (item, point) => placeMenu(item, point),
            onCollapse: (collapsed) => saveView({ sidebar: { collapsed } }),
        });

        const listing = window.PlexoraExplorerListing.create({
            domId,
            accept: want,
            pathOf: entryPath,
            onOpen: (entry) => show(entryPath(entry)),
            onActivate: () => {
                const chosen = currentAnswer();
                if (chosen !== null) finish(chosen);
            },
            onSelection: () => paintFooter(),
            onContextMenu: (entry, point) => rowMenu(entry, point),
            onSortChange: (sort) => saveView(sort),
            onColumnsChange: (columns) => saveView({ columns }),
            onExplain: (why) => explain(why),
        });

        // -- the toolbar's right end -------------------------------------------

        const searchBox = el("div", "plx-explorer-search");
        searchBox.append(glyph("fas fa-magnifying-glass", "plx-explorer-search-icon"));
        const searchInput = el("input", "plx-explorer-search-input");
        searchInput.type = "search";
        searchInput.setAttribute("aria-label", "Filter this folder");
        searchInput.setAttribute("placeholder", "Filter");
        searchInput.setAttribute("spellcheck", "false");
        const deepButton = button("plx-explorer-search-deep", "", "Search subfolders too (Enter)");
        deepButton.setAttribute("aria-pressed", "false");
        deepButton.append(glyph("fas fa-folder-tree"));
        deepButton.hidden = true;
        searchBox.append(searchInput, deepButton);

        function tool(action, icon, label) {
            const node = button("plx-explorer-tool", "", label);
            node.setAttribute("data-action", action);
            node.append(glyph(icon));
            return node;
        }

        const hiddenButton = tool("hidden", "fas fa-eye-slash", "Show hidden files (Ctrl+Shift+.)");
        hiddenButton.setAttribute("aria-pressed", "false");
        const layoutGroup = el("div", "plx-explorer-segment");
        layoutGroup.setAttribute("role", "group");
        layoutGroup.setAttribute("aria-label", "View as");
        const listButton = tool("layout-list", "fas fa-list", "List");
        const gridButton = tool("layout-grid", "fas fa-table-cells-large", "Icons");
        layoutGroup.append(listButton, gridButton);
        const sortButton = tool("sort", "fas fa-arrow-down-wide-short", "Sort");
        sortButton.setAttribute("aria-haspopup", "menu");
        const moreButton = tool("more", "fas fa-ellipsis-vertical", "More");
        moreButton.setAttribute("aria-haspopup", "menu");

        const actions = el("div", "plx-explorer-actions");
        actions.append(searchBox, hiddenButton, layoutGroup, sortButton, moreButton);

        const toolbar = el("div", "plx-explorer-toolbar");
        toolbar.append(crumbs.element, actions);

        // -- the body ----------------------------------------------------------

        if (description) {
            win.body.append(el("div", "plx-explorer-description", description));
        }
        win.body.append(toolbar, crumbs.messages);

        const banner = el("div", "plx-explorer-banner");
        banner.hidden = true;
        const searchBar = el("div", "plx-explorer-searchbar");
        searchBar.setAttribute("aria-live", "polite");
        searchBar.hidden = true;
        const content = el("div", "plx-explorer-content");
        content.append(banner, searchBar, listing.element);
        const main = el("div", "plx-explorer-main");
        main.append(sidebar.element, content);
        win.body.append(main);

        // -- the footer --------------------------------------------------------

        const summary = el("div", "plx-explorer-summary");
        summary.setAttribute("aria-live", "polite");
        const footActions = el("div", "plx-explorer-footer-actions");
        const cancel = button("plx-button plx-explorer-cancel", "Cancel");
        //: The way out of the one thing dataset marking can get wrong: a store
        //: under a name and layout nothing recognises draws as a folder and
        //: opens on a click -- and from inside it, this answers with it. Only
        //: where a field takes a file OR a folder.
        const useFolder = want.kind === "any"
            ? button("plx-button plx-explorer-use-folder", "Use this folder",
                     "Choose the folder that is open, whatever it is called")
            : null;
        const primaryButton = button("plx-button plx-button-primary plx-explorer-primary", "Open");
        footActions.append(cancel);
        if (useFolder) footActions.append(useFolder);
        footActions.append(primaryButton);
        win.footer.append(summary, footActions);

        // -- what is chosen ----------------------------------------------------

        function currentAnswer() {
            // Standing in a folder IS choosing it in directory mode.
            if (want.kind === "directory") return state.listed && state.here ? state.here : null;
            const chosen = listing.selection().map(entryPath);
            if (want.many) return chosen.length ? chosen : null;
            return chosen[0] || null;
        }

        function primaryLabel() {
            if (primary) return primary;
            if (want.kind === "directory") return "Use Folder";
            if (want.kind === "file") {
                const n = listing.selection().length;
                return want.many && n > 1 ? `Open ${n} files` : "Open";
            }
            return "Select";
        }

        function finish(result) {
            answer = result;
            // Where this leaves us is recorded by the `close` handler rather
            // than here: Esc closes the dialog without coming through here.
            win.close();
        }

        // -- the footer's sentence -----------------------------------------------

        function describeOne(entry) {
            const parts = [entry.name];
            const store = storeOf(entry);
            if (store) parts.push(store.label);
            else if (!entry.is_dir) {
                const size = readableSize(listing.sizeOf(entry));
                if (size) parts.push(size);
            }
            const when = fullDate(listing.mtimeOf(entry));
            if (when) parts.push(`Modified ${when}`);
            return parts.join(" · ");
        }

        function summaryText() {
            const selection = listing.selection();
            if (selection.length === 1) return describeOne(selection[0]);
            if (selection.length > 1) {
                let bytes = 0;
                let known = true;
                selection.forEach((entry) => {
                    const size = listing.sizeOf(entry);
                    if (entry.is_dir || size === null || size === undefined) known = false;
                    else bytes += Number(size);
                });
                return `${count(selection.length, "item", "items")} selected`
                    + (known ? ` · ${readableSize(bytes)}` : "");
            }
            if (!state.listed) return "";
            const total = listing.entriesCount();
            if (listing.isSearching()) return count(listing.rowsCount(), "match", "matches");
            if (listing.query()) {
                // "0 of 2000 shown" reads as "your file is not there" when what
                // it means is "your file is past the cut" -- so say which.
                return state.truncated
                    ? `${listing.rowsCount()} of the first ${total} shown`
                    : `${listing.rowsCount()} of ${total} shown`;
            }
            const items = count(total, "item", "items");
            if (state.truncated && state.total) return `${items} of ${Number(state.total).toLocaleString()}`;
            return items;
        }

        function paintFooter() {
            primaryButton.textContent = primaryLabel();
            primaryButton.disabled = currentAnswer() === null;
            if (useFolder) useFolder.disabled = !state.here || !state.listed;
            if (state.explainTimer) return;
            summary.textContent = summaryText();
        }

        /** A greyed row, clicked: say why, for a few seconds, where eyes go. */
        function explain(why) {
            if (!why) return;
            clearTimeout(state.explainTimer);
            summary.textContent = why;
            summary.classList.add("is-explaining");
            state.explainTimer = setTimeout(() => {
                state.explainTimer = null;
                summary.classList.remove("is-explaining");
                paintFooter();
            }, EXPLAIN_MS);
        }

        function flash(message) {
            explain(message);
            summary.classList.remove("is-explaining");
        }

        // -- drawing the rest --------------------------------------------------

        function paintPin() {
            crumbs.setPinned(state.prefs.pinned.indexOf(state.here) >= 0, Boolean(state.here));
        }

        function paintSidebarLists() {
            sidebar.setPinned(state.prefs.pinned);
            sidebar.setRecent(state.prefs.recent);
            sidebar.setCurrent(state.here);
        }

        function paintViewControls() {
            const view = state.view;
            hiddenButton.setAttribute("aria-pressed", view.show_hidden ? "true" : "false");
            hiddenButton.classList.toggle("is-on", view.show_hidden);
            const hiddenTitle = view.show_hidden ? "Hide hidden files (Ctrl+Shift+.)"
                : "Show hidden files (Ctrl+Shift+.)";
            hiddenButton.setAttribute("title", hiddenTitle);
            hiddenButton.setAttribute("aria-label", hiddenTitle);
            setIcon(hiddenButton.children[0], view.show_hidden ? "fas fa-eye" : "fas fa-eye-slash");
            listButton.setAttribute("aria-pressed", view.layout === "list" ? "true" : "false");
            gridButton.setAttribute("aria-pressed", view.layout === "grid" ? "true" : "false");
        }

        function paintBanner() {
            const dataset = state.dataset;
            banner.replaceChildren();
            if (!dataset || !dataset.type || listing.isSearching()) {
                banner.hidden = true;
                return;
            }
            const label = dataset.label || types.DATASET_LABELS[dataset.type] || "dataset";
            const verdict = usable({ name: shortName(state.here), is_dir: true, store: dataset }, want);
            if (verdict.role !== "dataset" && dataset.licensed !== false) {
                banner.hidden = true;
                return;
            }
            const article = /^[aeiou]/i.test(label) ? "an" : "a";
            banner.append(glyph("fas fa-circle-info", "plx-explorer-banner-icon"));
            if (dataset.licensed === false) {
                banner.append(el("span", "plx-explorer-banner-text",
                    `This folder is ${article} ${label}, which this licence does not import.`));
            } else {
                banner.append(el("span", "plx-explorer-banner-text",
                                 `This folder is ${article} ${label}.`));
                const use = button("plx-explorer-banner-use", "Use this folder");
                use.addEventListener("click", () => finish(state.here));
                banner.append(use);
            }
            banner.hidden = false;
        }

        function emptyText() {
            const query = searchInput.value.trim();
            if (listing.isSearching()) return `Nothing named “${query}” under ${shortName(state.here)}`;
            if (query) return `No matches for “${query}” in this folder`;
            return state.view.show_hidden ? "This folder is empty"
                : "This folder is empty (hidden files are not shown)";
        }

        function paintEmpty() {
            listing.setStatus({ empty: state.listed ? emptyText() : "" });
        }

        function setBusy(busy, path) {
            clearTimeout(state.loadingTimer);
            state.loadingTimer = null;
            listing.setStatus({ busy });
            if (!busy) return;
            state.loadingTimer = setTimeout(() => {
                state.loadingTimer = null;
                if (!listing.rowsCount()) listing.setStatus({ empty: `Reading ${path || "your home folder"}…` });
            }, LOADING_DELAY);
        }

        // -- navigation --------------------------------------------------------

        function abort(name) {
            const controller = state[name];
            state[name] = null;
            if (controller) {
                try { controller.abort(); } catch (error) { /* already done */ }
            }
        }

        /**
         * Show one directory. The single way `state.here` ever changes, and
         * the single place a listing is fetched.
         *
         * `history` says what Back and Forward do with the answer: "push" a
         * new step, "back"/"forward" move along them, "keep" (a refresh, the
         * hidden-files toggle) leaves them alone. They move only once the
         * listing has arrived, so a failure leaves them as they were.
         * `fallbackHome` is for the first listing only -- a remembered folder
         * that has since been deleted must not open an empty explorer.
         */
        async function show(path, { history = "push", fallbackHome = false } = {}) {
            const ticket = ++state.epoch;
            abort("listAbort");
            const controller = abortable();
            state.listAbort = controller;
            setBusy(true, path);
            let payload = null;
            let failure = null;
            try {
                const reply = await postJson("list_dir", {
                    path: path || "",
                    node,
                    show_hidden: state.view.show_hidden,
                }, controller);
                payload = reply.payload;
                if (!reply.response.ok || !payload) {
                    throw new Error((payload && payload.error) || "That folder could not be read.");
                }
            } catch (error) {
                failure = (error && error.name === "AbortError") ? null
                    : (error && error.message) || "That folder could not be read.";
                if (!failure) return;
            }
            if (ticket !== state.epoch || closed) {
                // Somewhere else was asked for while this was in flight. That
                // request owns the view now, busy state included.
                return;
            }
            state.listAbort = null;
            setBusy(false);

            if (failure) {
                if (fallbackHome) {
                    await show("", { history, fallbackHome: false });
                    if (state.here) {
                        crumbs.setNote(`Could not open ${path} — showing ${state.here} instead.`);
                    }
                    return;
                }
                // Whatever is on screen stays on screen. Being thrown back to
                // your home directory because you mistyped one folder is worse
                // than the mistake.
                crumbs.setError(history === "keep" && state.listed
                    ? `Couldn't refresh — showing the earlier listing. ${failure}` : failure);
                paintEmpty();
                return;
            }

            const previous = state.here;
            const wasEditing = crumbs.isEditing();
            abort("metaAbort");
            state.metaEpoch += 1;
            state.listedEpoch = ticket;
            crumbs.setError("");
            state.here = payload.path;
            state.parent = payload.parent || null;
            state.crumbs = Array.isArray(payload.crumbs) ? payload.crumbs : null;
            state.dataset = payload.dataset || null;
            state.platform = payload.platform || state.platform;
            state.partial = Boolean(payload.partial);
            state.truncated = Boolean(payload.truncated);
            state.total = Number.isFinite(payload.total) ? payload.total : null;
            state.listed = true;

            if (history === "push") {
                if (previous && previous !== state.here) {
                    state.back.push(previous);
                    state.forward = [];
                }
            } else if (history === "back") {
                state.back.pop();
                if (previous && previous !== state.here) state.forward.push(previous);
            } else if (history === "forward") {
                state.forward.pop();
                if (previous && previous !== state.here) state.back.push(previous);
            }

            // A filter is about the folder you typed it in. Carrying "cells"
            // into the next directory shows an empty one and looks like it.
            // A reload keeps it, but not a search below a folder now re-read.
            if (history !== "keep") searchInput.value = "";
            leaveSearch();
            listing.setListing(payload.entries || [], { partial: state.partial,
                                                        query: searchInput.value });
            crumbs.setLocation({ here: state.here, crumbs: state.crumbs });
            crumbs.setHistory({ back: state.back.length > 0, forward: state.forward.length > 0,
                                up: Boolean(state.parent) });
            crumbs.setNote(state.truncated
                ? `Showing the first ${listing.entriesCount().toLocaleString()}`
                  + (state.total ? ` of ${Number(state.total).toLocaleString()}` : "")
                  + " entries — type a path or search to go further."
                : "");
            sidebar.setCurrent(state.here);
            paintPin();
            paintBanner();
            paintEmpty();
            paintSearchControls();
            paintFooter();
            if (wasEditing && !crumbs.isEditing()) listing.focus();
            if (state.partial) fillMetadata(ticket);
        }

        function goUp() {
            if (state.parent) show(state.parent);
        }

        function goBack() {
            if (!state.back.length) return;
            show(state.back[state.back.length - 1], { history: "back" });
        }

        function goForward() {
            if (!state.forward.length) return;
            show(state.forward[state.forward.length - 1], { history: "forward" });
        }

        function refresh() {
            if (state.listed) show(state.here, { history: "keep" });
            else show("", { history: "keep" });
        }

        // -- sizes and dates, after the names ------------------------------------

        /**
         * A partial listing's missing sizes and dates, in chunks: the rows on
         * screen first, then top to bottom, one request at a time, abandoned
         * the moment the user goes somewhere else.
         */
        async function fillMetadata(ticket) {
            const epoch = ++state.metaEpoch;
            const pending = new Set(listing.entries()
                .filter((entry) => (entry.mtime === null || entry.mtime === undefined)
                    && entry.kind !== "broken")
                .map((entry) => entry.name));
            const here = state.here;
            while (pending.size) {
                const rows = listing.rows();
                const range = listing.visibleRange();
                const names = [];
                const take = (entry) => {
                    if (names.length < META_CHUNK && pending.has(entry.name)
                            && names.indexOf(entry.name) < 0) names.push(entry.name);
                };
                for (let i = range.first; i < range.last && names.length < META_CHUNK; i += 1) {
                    if (rows[i]) take(rows[i]);
                }
                for (let i = 0; i < rows.length && names.length < META_CHUNK; i += 1) take(rows[i]);
                if (!names.length) {
                    pending.forEach((name) => { if (names.length < META_CHUNK) names.push(name); });
                }
                const controller = abortable();
                state.metaAbort = controller;
                let reply;
                try {
                    reply = await postJson("stat_entries", { path: here, node, names }, controller);
                } catch (error) {
                    reply = null;
                }
                if (closed || epoch !== state.metaEpoch || ticket !== state.listedEpoch) return;
                state.metaAbort = null;
                if (!reply || !reply.response.ok || !reply.payload || reply.payload.unsupported) break;
                listing.mergeMetadata(reply.payload.entries || {});
                names.forEach((name) => pending.delete(name));
                paintFooter();
            }
            if (closed || epoch !== state.metaEpoch || ticket !== state.listedEpoch) return;
            listing.metadataComplete();
            paintFooter();
        }

        // -- filtering and searching ---------------------------------------------

        function paintSearchControls() {
            const query = searchInput.value.trim();
            deepButton.hidden = !query || !state.listed;
            deepButton.setAttribute("aria-pressed", state.deep ? "true" : "false");
            const deepTitle = state.deep
                ? "Search this folder only"
                : `Search all subfolders of ${shortName(state.here) || "this folder"} (Enter)`;
            deepButton.setAttribute("title", deepTitle);
            deepButton.setAttribute("aria-label", deepTitle);
            searchInput.setAttribute("placeholder", state.deep ? "Search subfolders" : "Filter");
        }

        function setSearchBar(text, { busy = false, action = null } = {}) {
            searchBar.replaceChildren();
            if (!text) {
                searchBar.hidden = true;
                return;
            }
            const icon = glyph(busy ? "fas fa-circle-notch" : "fas fa-folder-tree",
                               "plx-explorer-searchbar-icon");
            if (busy) icon.classList.add("is-spinning");
            searchBar.append(icon, el("span", "plx-explorer-searchbar-text", text));
            if (action) {
                const back = button("plx-explorer-searchbar-action", action.label);
                back.addEventListener("click", action.run);
                searchBar.append(back);
            }
            searchBar.hidden = false;
        }

        function leaveSearch() {
            clearTimeout(state.searchTimer);
            state.searchTimer = null;
            state.searchEpoch += 1;
            abort("searchAbort");
            const was = state.deep;
            state.deep = false;
            setSearchBar("");
            if (was && listing.isSearching()) {
                listing.setSearchResults(null);
                listing.setQuery(searchInput.value);
                paintBanner();
            }
            paintSearchControls();
        }

        function enterSearch() {
            if (!searchInput.value.trim() || !state.listed) return;
            state.deep = true;
            paintSearchControls();
            clearTimeout(state.searchTimer);
            runSearch();
        }

        function scheduleSearch() {
            clearTimeout(state.searchTimer);
            state.searchTimer = setTimeout(runSearch, SEARCH_DELAY);
        }

        async function runSearch() {
            state.searchTimer = null;
            const query = searchInput.value.trim();
            if (!query) {
                leaveSearch();
                return;
            }
            const ticket = ++state.searchEpoch;
            abort("searchAbort");
            const controller = abortable();
            state.searchAbort = controller;
            const where = shortName(state.here) || state.here;
            const backToFolder = { label: "Only this folder", run: () => leaveSearch() };
            setSearchBar(`Searching subfolders of ${where} for “${query}”…`, { busy: true, action: backToFolder });
            let reply;
            try {
                reply = await postJson("search_dir", {
                    path: state.here, node, query, show_hidden: state.view.show_hidden,
                    limit: SEARCH_LIMIT, budget_ms: SEARCH_BUDGET_MS, max_depth: SEARCH_DEPTH,
                }, controller);
            } catch (error) {
                if (error && error.name === "AbortError") return;
                reply = null;
            }
            if (closed || ticket !== state.searchEpoch || !state.deep) return;
            state.searchAbort = null;
            if (!reply || !reply.payload) {
                setSearchBar("The search could not be run.", { action: backToFolder });
                return;
            }
            if (reply.response.status === 429) {
                setSearchBar("Another search is still running on this machine — try again in a moment.",
                             { action: backToFolder });
                return;
            }
            if (!reply.response.ok) {
                setSearchBar(reply.payload.error || "The search could not be run.", { action: backToFolder });
                return;
            }
            if (reply.payload.unsupported) {
                state.deep = false;
                paintSearchControls();
                setSearchBar("Searching subfolders is not available on this machine — "
                             + "the filter still narrows this folder.");
                return;
            }
            const matches = Array.isArray(reply.payload.matches) ? reply.payload.matches : [];
            listing.setSearchResults(matches);
            paintBanner();
            const found = `${count(matches.length, "match", "matches")} for “${query}” under ${where}`;
            const stopped = {
                limit: ` — showing the first ${matches.length}`,
                budget: " — stopped after a few seconds; open a subfolder to search deeper",
                depth: ` — ${SEARCH_DEPTH} folders deep at most`,
            }[reply.payload.stopped] || "";
            setSearchBar(found + stopped, { action: backToFolder });
            paintEmpty();
            paintFooter();
        }

        searchInput.addEventListener("input", () => {
            if (state.deep) {
                if (!searchInput.value.trim()) leaveSearch();
                else scheduleSearch();
            } else {
                listing.setQuery(searchInput.value || "");
            }
            paintSearchControls();
            paintEmpty();
            paintFooter();
        });
        searchInput.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                if (event.stopPropagation) event.stopPropagation();
                enterSearch();
                return;
            }
            if (event.key !== "Escape") return;
            if (!(searchInput.value || "") && !state.deep) return;
            // Esc empties the box first. Without both of these the browser's
            // own dialog-cancel fires and the whole explorer closes, which is
            // a startling amount to lose for clearing a filter.
            event.preventDefault();
            event.stopPropagation();
            searchInput.value = "";
            leaveSearch();
            listing.setQuery("");
            paintSearchControls();
            paintEmpty();
            paintFooter();
        });
        deepButton.addEventListener("click", () => {
            if (state.deep) leaveSearch();
            else enterSearch();
        });

        // -- the view ----------------------------------------------------------

        /**
         * Change how things are shown, and remember it. Writes are batched:
         * dragging a column is many changes and should be one request.
         */
        function saveView(change) {
            state.view = cleanView({ ...state.view, ...change,
                                     columns: { ...state.view.columns, ...((change && change.columns) || {}) },
                                     sidebar: change && change.sidebar ? change.sidebar : state.view.sidebar });
            state.pendingView = { ...(state.pendingView || {}), ...change };
            clearTimeout(state.viewTimer);
            state.viewTimer = setTimeout(flushView, VIEW_SAVE_DELAY);
        }

        /** The view changes not yet written, taken off the queue. */
        function takeView() {
            clearTimeout(state.viewTimer);
            state.viewTimer = null;
            const view = state.pendingView;
            state.pendingView = null;
            return view;
        }

        function flushView() {
            const view = takeView();
            if (view && !closed) savePrefs({ view });
        }

        function applyView() {
            listing.setView({
                sort_by: state.view.sort_by,
                sort_dir: state.view.sort_dir,
                dirs_first: state.view.dirs_first,
                layout: state.view.layout,
                columns: state.view.columns,
            });
            sidebar.setCollapsed(state.view.sidebar.collapsed);
            paintViewControls();
        }

        function toggleHidden() {
            saveView({ show_hidden: !state.view.show_hidden });
            paintViewControls();
            show(state.here, { history: "keep" });
        }

        function setLayout(layout) {
            if (state.view.layout === layout) return;
            saveView({ layout });
            applyView();
        }

        function setSort(change) {
            saveView(change);
            applyView();
        }

        // -- remembering where you were --------------------------------------------

        async function loadPrefs() {
            try {
                const response = await fetch(plexoraUrl(
                    `picker_prefs?node=${encodeURIComponent(node)}`));
                const record = await response.json();
                if (response.ok && record && typeof record === "object") {
                    return {
                        prefs: {
                            last_dir: typeof record.last_dir === "string" ? record.last_dir : "",
                            recent: strings(record.recent),
                            pinned: strings(record.pinned),
                        },
                        view: record.view,
                    };
                }
            } catch (error) {
                // Deliberately silent, and deliberately not fatal: an explorer
                // that will not open because a preferences file could not be
                // read is a much worse failure than one with no Recent list.
            }
            return { prefs: noPrefs(), view: null };
        }

        async function loadPlaces() {
            try {
                const response = await fetch(plexoraUrl(`fs_places?node=${encodeURIComponent(node)}`));
                const record = await response.json();
                if (response.ok && record && typeof record === "object") return record;
            } catch (error) {
                // As above: no volumes is a smaller sidebar, not a failure.
            }
            return null;
        }

        function paintPlaces(record) {
            if (closed || !record) return;
            state.places = record;
            if (record.platform) state.platform = record.platform;
            const host = record.hostname || "";
            const desktop = Boolean(window.PlexoraDesktop) && !node;
            sidebar.setMachine({
                label: node ? (host && host !== node ? `${node} — ${host}` : node)
                    : (host ? `This computer — ${host}` : "This computer"),
                icon: node ? "fas fa-server" : desktop ? "fas fa-laptop" : "fas fa-computer",
                title: node ? `Browsing ${node}` : "Browsing the machine Plexora runs on",
            });
            win.setSubtitle(node ? `on ${node}${host && host !== node ? ` — ${host}` : ""}`
                : host ? `on ${host}` : "");
            sidebar.setPlaces({
                home: typeof record.home === "string" ? record.home : "",
                known: Array.isArray(record.known) ? record.known : [],
                volumes: Array.isArray(record.volumes) ? record.volumes : [],
                project: Array.isArray(record.project) ? record.project : [],
                unavailable: record.unavailable || (record.unsupported
                    ? "This machine does not list its drives." : ""),
            });
            paintSidebarLists();
        }

        async function savePrefs(change) {
            try {
                const response = await fetch(plexoraUrl("picker_prefs"), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ node, ...change }),
                });
                const record = await response.json();
                if (closed) return;
                if (response.ok && record && Array.isArray(record.pinned)) {
                    state.prefs = {
                        last_dir: typeof record.last_dir === "string" ? record.last_dir : "",
                        recent: strings(record.recent),
                        pinned: strings(record.pinned),
                    };
                    paintSidebarLists();
                    paintPin();
                }
            } catch (error) {
                // As above. The star is already drawn; it will be right again
                // the next time this opens.
            }
        }

        /**
         * Where the explorer was left, written once as it closes.
         *
         * On the way out rather than on a successful pick, because "the folder
         * I was in last time" is the thing worth reopening at, and browsing to
         * it is what costs the effort. Cancelling is not an instruction to
         * forget.
         *
         * The Recent list only records a folder something was actually taken
         * from: it is a list of places that turned out to be worth something,
         * not a history of everywhere the explorer was pointed.
         *
         * Standing still writes nothing at all -- this is a read-modify-write
         * of a file on disk -- unless a view change is still waiting to go.
         */
        function remember() {
            const view = takeView();
            const change = {};
            if (state.here) {
                const chose = answer !== null && answer !== undefined;
                if (chose) {
                    change.last_dir = state.here;
                    change.add_recent = state.here;
                } else if (state.here !== state.prefs.last_dir) {
                    change.last_dir = state.here;
                }
            }
            if (view) change.view = view;
            if (Object.keys(change).length) savePrefs(change);
        }

        function togglePin(path) {
            if (!path) return;
            const on = state.prefs.pinned.indexOf(path) >= 0;
            state.prefs.pinned = on
                ? state.prefs.pinned.filter((each) => each !== path)
                : [...state.prefs.pinned, path];
            paintSidebarLists();
            paintPin();
            savePrefs(on ? { unpin: path } : { pin: path });
        }

        // -- the machine's own dialog ------------------------------------------------

        function systemKind() {
            if (want.kind === "directory" || want.kind === "dataset") return "directory";
            if (want.kind === "file") return "file";
            return state.systemDialog === "hybrid" ? "any" : null;
        }

        async function openSystemDialog() {
            if (!systemDialog || !state.systemDialog) return;
            try {
                const result = await systemDialog.open(systemKind(), { multiple: want.many,
                                                                       anchor: moreButton });
                if (closed || result === null || result === undefined) return;
                const list = Array.isArray(result) ? result : [result];
                if (!list.length) return;
                finish(want.many ? list : list[0]);
            } catch (error) {
                if (!closed) crumbs.setError(error && error.message
                    ? error.message : "The system file dialog could not be opened.");
            }
        }

        // -- menus -----------------------------------------------------------------

        function menuAt(point) {
            return {
                getBoundingClientRect: () => ({ left: point.x, right: point.x, top: point.y,
                                               bottom: point.y, width: 0, height: 0 }),
                setAttribute() {},
            };
        }

        function openMenu(anchor, items, align = "left") {
            if (!window.PlexoraMenu || !items.length) return;
            window.PlexoraMenu.open(anchor, items, { align });
        }

        async function copyPath(path) {
            try {
                if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error("no clipboard");
                await navigator.clipboard.writeText(path);
                flash(`Copied ${path}`);
            } catch (error) {
                flash("Could not copy — click the path bar and copy it from there.");
            }
        }

        function pinItems(path) {
            if (!path) return [];
            const on = state.prefs.pinned.indexOf(path) >= 0;
            return [{ label: on ? "Unpin from Favorites" : "Pin to Favorites",
                      onSelect: () => togglePin(path) }];
        }

        function rowMenu(entry, point) {
            const anchor = menuAt(point);
            if (!entry) {
                openMenu(anchor, [
                    { label: "Reload", onSelect: () => refresh() },
                    ...pinItems(state.here),
                    { label: "Copy path of this folder", onSelect: () => copyPath(state.here),
                      disabled: !state.here },
                    { separator: true },
                    { label: "Show hidden files", checked: state.view.show_hidden, checkbox: true,
                      onSelect: () => toggleHidden() },
                ]);
                return;
            }
            const path = entryPath(entry);
            const verdict = usable(entry, want);
            const items = [];
            if (verdict.role === "folder") {
                items.push({ label: "Open", onSelect: () => show(path) });
                if (want.kind === "directory") {
                    items.push({ label: "Choose this folder", onSelect: () => finish(path) });
                }
            } else if (verdict.role === "dataset") {
                items.push({ label: "Select", onSelect: () => finish(want.many ? [path] : path) });
                items.push({ label: "Open folder", onSelect: () => show(path) });
            } else if (verdict.role === "file") {
                items.push({ label: primaryLabel().replace(/ \d+ files$/, ""),
                             onSelect: () => {
                                 const chosen = currentAnswer();
                                 finish(want.many ? (chosen && chosen.length ? chosen : [path]) : path);
                             } });
            } else if (verdict.why) {
                items.push({ label: verdict.why, disabled: true });
            }
            if (entry.is_dir && verdict.ok) items.push(...pinItems(path));
            if (items.length) items.push({ separator: true });
            items.push({ label: "Copy path", onSelect: () => copyPath(path) });
            openMenu(anchor, items);
        }

        function placeMenu(item, point) {
            const items = [{ label: "Open", onSelect: () => show(item.kind === "place" && !item.path ? "" : item.path) }];
            if (item.path) items.push(...pinItems(item.path));
            if (item.path) items.push({ label: "Copy path", onSelect: () => copyPath(item.path) });
            openMenu(menuAt(point), items);
        }

        function crumbMenu(crumb, point) {
            const anchor = menuAt(point);
            if (!crumb) {
                openMenu(anchor, [
                    { label: "Edit path", onSelect: () => crumbs.beginEdit() },
                    { label: "Copy path", onSelect: () => copyPath(state.here), disabled: !state.here },
                ]);
                return;
            }
            openMenu(anchor, [
                { label: "Open", onSelect: () => show(crumb.path) },
                ...pinItems(crumb.path),
                { label: "Copy path", onSelect: () => copyPath(crumb.path) },
            ]);
        }

        function sortMenu() {
            const view = state.view;
            const by = (name, label) => ({ label, checked: view.sort_by === name,
                                           onSelect: () => setSort({ sort_by: name }) });
            openMenu(sortButton, [
                by("name", "Name"),
                by("mtime", "Date Modified"),
                by("kind", "Type"),
                by("size", "Size"),
                { separator: true },
                { label: "Ascending", checked: view.sort_dir === "asc",
                  onSelect: () => setSort({ sort_dir: "asc" }) },
                { label: "Descending", checked: view.sort_dir === "desc",
                  onSelect: () => setSort({ sort_dir: "desc" }) },
                { separator: true },
                { label: "Folders first", checked: view.dirs_first, checkbox: true,
                  onSelect: () => setSort({ dirs_first: !view.dirs_first }) },
            ], "right");
        }

        function moreMenu() {
            const items = [];
            if (systemDialog && state.systemDialog) {
                items.push({ label: systemDialog.label || "Use system dialog…",
                             onSelect: () => openSystemDialog() });
                items.push({ separator: true });
            }
            items.push({ label: "Show hidden files", checked: state.view.show_hidden, checkbox: true,
                         onSelect: () => toggleHidden() });
            items.push({ label: "Copy path of this folder", onSelect: () => copyPath(state.here),
                         disabled: !state.here });
            items.push({ label: "Reload", onSelect: () => refresh() });
            openMenu(moreButton, items, "right");
        }

        // -- the keyboard ------------------------------------------------------

        function consume(event) {
            if (event.preventDefault) event.preventDefault();
            if (event.stopPropagation) event.stopPropagation();
        }

        function typeahead(key) {
            clearTimeout(state.typeaheadTimer);
            state.typeahead += key.toLowerCase();
            state.typeaheadTimer = setTimeout(() => { state.typeahead = ""; }, TYPEAHEAD_MS);
            const rows = listing.rows();
            const from = Math.max(0, listing.cursorIndex());
            const prefix = state.typeahead;
            for (let step = 0; step < rows.length; step += 1) {
                const index = (from + step + (prefix.length === 1 ? 1 : 0)) % rows.length;
                if (String(rows[index].name).toLowerCase().startsWith(prefix)) {
                    listing.moveCursorTo(index);
                    return;
                }
            }
        }

        /**
         * Keys, read off `event.target` rather than `document.activeElement`:
         * the two agree in a browser, and only one of them is a thing this
         * file can be tested against. Every key that reaches the window stops
         * here, so the page's own shortcuts never act behind a modal.
         */
        function onKeydown(event) {
            if (closed) return;
            const target = event.target || null;
            if (target && target.closest && target.closest(".plx-menu")) return;
            const tag = String((target && target.tagName) || "").toUpperCase();
            const typing = tag === "INPUT" || tag === "TEXTAREA";
            const mod = Boolean(event.metaKey || event.ctrlKey);
            const key = event.key;
            if (event.stopPropagation) event.stopPropagation();

            if (mod && !event.shiftKey && (key === "l" || key === "L")) {
                consume(event);
                if (win.isRolled()) win.unroll();
                crumbs.beginEdit();
                return;
            }
            if (mod && !event.shiftKey && (key === "f" || key === "F")) {
                consume(event);
                if (win.isRolled()) win.unroll();
                if (searchInput.focus) searchInput.focus();
                if (searchInput.select) searchInput.select();
                return;
            }
            if (mod && event.shiftKey && (key === "." || key === ">" || event.code === "Period")) {
                consume(event);
                toggleHidden();
                return;
            }
            if (win.isRolled()) return;
            if (typing) {
                if (target === searchInput && key === "ArrowDown") {
                    consume(event);
                    listing.focus();
                    listing.moveCursor(1);
                }
                return;
            }
            if (key === "Enter" && tag === "BUTTON") {
                // The browser turns Enter on a focused button into a click.
                // Answering it here as well closed the explorer on whatever was
                // selected the moment somebody pressed Enter on "Up".
                return;
            }
            if (event.altKey && key === "ArrowLeft") { consume(event); goBack(); return; }
            if (event.altKey && key === "ArrowRight") { consume(event); goForward(); return; }
            if (event.altKey && key === "ArrowUp") { consume(event); goUp(); return; }
            if (mod && key === "ArrowUp") { consume(event); goUp(); return; }
            if (key === "Backspace") {
                consume(event);
                goUp();
                return;
            }
            const inList = target === dialog || target === listing.list
                || (listing.element.contains && listing.element.contains(target));
            if (!inList) return;
            const grid = listing.isGrid();
            const step = grid ? listing.gridColumns() : 1;
            const extend = Boolean(event.shiftKey) && want.many;
            if (key === "ArrowDown" || key === "ArrowUp") {
                consume(event);
                listing.moveCursor(key === "ArrowDown" ? step : -step, { extend, keep: mod });
                return;
            }
            if (grid && (key === "ArrowRight" || key === "ArrowLeft")) {
                consume(event);
                listing.moveCursor(key === "ArrowRight" ? 1 : -1, { extend, keep: mod });
                return;
            }
            if (key === "Home" || key === "End") {
                consume(event);
                listing.moveCursorTo(key === "Home" ? 0 : listing.rowsCount() - 1, { extend });
                return;
            }
            if (key === "PageDown" || key === "PageUp") {
                consume(event);
                listing.moveCursor((key === "PageDown" ? 1 : -1) * listing.pageSize(), { extend });
                return;
            }
            if (key === " " || key === "Spacebar") {
                consume(event);
                listing.toggleCursor();
                return;
            }
            if (mod && (key === "a" || key === "A")) {
                consume(event);
                listing.selectAll();
                return;
            }
            if (key === "Enter") {
                consume(event);
                if (listing.activateCursor()) return;
                const chosen = currentAnswer();
                if (chosen !== null) finish(chosen);
                return;
            }
            if (!mod && !event.altKey && typeof key === "string" && key.length === 1 && key !== " ") {
                consume(event);
                typeahead(key);
            }
        }

        /**
         * Esc while a menu is open or a box is being edited: the menu's own
         * handler (PlexoraMenu) stops the key's propagation but not its
         * default, and the default of Esc in a modal is to close it. So the
         * default is prevented here, at the document, before either runs.
         */
        function onEscapeCapture(event) {
            if (event.key !== "Escape" || closed) return;
            const menuOpen = window.PlexoraMenu && window.PlexoraMenu.isOpen
                && window.PlexoraMenu.isOpen();
            if (menuOpen || win.isDragging()) {
                if (event.preventDefault) event.preventDefault();
            }
        }

        // -- wiring ------------------------------------------------------------

        cancel.addEventListener("click", () => finish(null));
        primaryButton.addEventListener("click", () => {
            const chosen = currentAnswer();
            if (chosen !== null) finish(chosen);
        });
        if (useFolder) useFolder.addEventListener("click", () => finish(state.here || null));
        hiddenButton.addEventListener("click", () => toggleHidden());
        listButton.addEventListener("click", () => setLayout("list"));
        gridButton.addEventListener("click", () => setLayout("grid"));
        sortButton.addEventListener("click", () => sortMenu());
        moreButton.addEventListener("click", () => moreMenu());
        dialog.addEventListener("keydown", onKeydown);
        dialog.addEventListener("cancel", (event) => {
            const menuOpen = window.PlexoraMenu && window.PlexoraMenu.isOpen
                && window.PlexoraMenu.isOpen();
            if (menuOpen && event.preventDefault) event.preventDefault();
        });
        win.onLayout(() => {
            listing.relayout();
            crumbs.relayout();
        });
        win.onRoll((rolled) => {
            if (!rolled) listing.focus();
        });
        const promise = new Promise((resolve) => {
            dialog.addEventListener("close", () => {
                // Before `closed`, and before the window leaves the page: the
                // write goes out synchronously, and removing the element it was
                // started from does not cancel it.
                remember();
                closed = true;
                document.removeEventListener("keydown", onEscapeCapture, true);
                abort("listAbort");
                abort("metaAbort");
                abort("searchAbort");
                [state.searchTimer, state.loadingTimer, state.explainTimer, state.viewTimer,
                 state.typeaheadTimer].forEach((timer) => clearTimeout(timer));
                if (window.PlexoraMenu && window.PlexoraMenu.isOpen && window.PlexoraMenu.isOpen()) {
                    window.PlexoraMenu.close();
                }
                win.dispose();
                // A menu adopted by this window goes back where it belongs.
                // Spelled bare: PopoverPortal is a top-level const, not a
                // property of `window`.
                if (typeof PopoverPortal !== "undefined") PopoverPortal.relocate();
                resolve(answer);
            });
        });

        // Nothing is choosable until a directory has actually been listed --
        // in directory mode the answer IS the directory, and there is not one
        // yet.
        primaryButton.disabled = true;
        if (useFolder) useFolder.disabled = true;
        crumbs.setHistory({});
        paintViewControls();
        paintSearchControls();
        paintSidebarLists();
        document.addEventListener("keydown", onEscapeCapture, true);
        win.open();
        // showModal focuses the first focusable thing it finds, which is the
        // Minimize button. The list is what the arrow keys are for.
        listing.focus();

        if (systemDialog && typeof systemDialog.available === "function") {
            Promise.resolve()
                .then(() => systemDialog.available())
                .then((capability) => {
                    if (closed) return;
                    state.systemDialog = capability === "hybrid" || capability === "kinds"
                        ? capability : null;
                })
                .catch(() => { state.systemDialog = null; });
        }

        (async function open() {
            const prefsLoaded = loadPrefs();
            // Not awaited before the first listing: reading a machine's mounts
            // (and asking each how full it is) must never hold up its folders.
            const placesLoaded = loadPlaces();
            const loaded = await prefsLoaded;
            if (closed) return;
            state.prefs = loaded.prefs;
            if (loaded.view) {
                state.view = cleanView(loaded.view);
                applyView();
            }
            paintSidebarLists();
            let opening = String(start || "").trim();
            // A field in verbatim mode holds `node://laptop/cells-7f3a91c2`,
            // which is an address rather than a path and would fail to list.
            if (opening.slice(0, 7) === "node://") opening = "";
            if (!opening) opening = state.prefs.last_dir || "";
            placesLoaded.then(paintPlaces);
            await show(opening, { fallbackHome: Boolean(opening) });
        })();

        return promise;
    }

    function accepts(name, filter) {
        return T().accepts(name, filter);
    }

    return { pick, accepts };
})();

//: Every caller written for the old in-app picker keeps working: same `pick`
//: options, same answers, same `accepts`.
window.PlexoraPathPicker = window.PlexoraFileExplorer;
