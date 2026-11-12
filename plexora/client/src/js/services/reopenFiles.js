/**
 * PlexoraReopen -- a project's files, opened again on a later visit to the
 * website.
 *
 * A web page never learns where a file is. What it keeps (services/
 * pickedFiles.js) is the browser's handle to each file the user picked --
 * Chromium's File System Access, in IndexedDB on this site whatever the user
 * chose for the projects (the browser or a folder on the computer) -- and
 * otherwise what the file was: its name, size and dates, which the project
 * records too (server/models/sources.py). So when a project opens and Python
 * has no path for one of its files (/project/<name>/files_here), there are
 * three answers, tried in this order:
 *
 *   1. The browser grants the handle already ("Allow on every visit", an
 *      installed app, or another of its prompts answered on this visit --
 *      the home page's Reconnect): mounted with no question.
 *   2. It holds the handle but asks again on a new visit: "Open your files
 *      from last time", whose Allow access is the click the browser's prompt
 *      needs. That prompt lists every file and folder the site held, so one
 *      answer covers them all, and "Allow on every visit" makes every later
 *      visit step 1.
 *   3. No handle to reuse -- Firefox and Safari keep none, the file moved,
 *      access was not allowed, another browser chose it, the site's data was
 *      cleared, the desktop app made the project: the dialog says which,
 *      describes each file by what was recorded, and the user finds it: one
 *      file in the browser's file dialog, several in a folder (or all at
 *      once in the file dialog, without a folder picker). A file is taken
 *      when it is the same one -- its size, and its date or its bytes --
 *      whatever it is called now; a different one is shown beside the
 *      original and used only when the user says so (`anyway`: the project
 *      then uses it, server/utils/relink.py `record_found`).
 *
 *   run({doc, project, python, picks, derived, reveal, status})
 *       -> true when a file was opened or found (the page is asked for again)
 *
 * `doc` is where the dialog is drawn: the app frame's loader, before its
 * page is written (services/shell.js `findFiles`); its look is the loader's
 * stylesheet (css/shell.css `.plx-reopen`). `picks` is PlexoraPicks,
 * `derived` PlexoraDerived (pyramids kept in folders beside their
 * originals, which wait on the same kind of click), `reveal()` brings the
 * loader into sight before the dialog opens, `status(text)` is the loader's
 * line. Classic script, `window.PlexoraReopen`, loaded by the host page.
 */
(function (global) {
    "use strict";

    //: How long "Opening ..." stays once every file is in, before the page.
    const OPENED_MS = 650;

    //: Stroke icons (24-unit boxes): constant markup, never anything a user
    //: named. The loader has no icon font.
    const ICONS = {
        image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/>'
            + '<path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
        mask: '<path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/>',
        table: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18"/><path d="M3 15h18"/>'
            + '<path d="M10 4v16"/>',
        layer: '<path d="M12 3 2 8l10 5 10-5-10-5Z"/><path d="m2 16 10 5 10-5"/>',
        folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
        file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5"/>',
        pyramid: '<path d="M12 3 3 20h18L12 3Z"/><path d="M7.5 12h9"/>',
        key: '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.7 12.3 9.3-9.3"/><path d="m16 7 3 3"/>'
            + '<path d="m13.5 9.5 2 2"/>',
        find: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h4"/><path d="M14 3v5h5"/><path d="M19 8v3"/>'
            + '<circle cx="16.5" cy="16.5" r="3"/><path d="m21 21-2.4-2.4"/>',
        findFolder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v3"/><path d="M3 7v10a2 2 0 0 0 2 2h6"/>'
            + '<circle cx="17" cy="16" r="3"/><path d="m21.5 20.5-2.4-2.4"/>',
        "triangle-exclamation": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>'
            + '<path d="M12 9v4"/><path d="M12 17h.01"/>',
        check: '<path d="M20 6 9 17l-5-5"/>',
        close: '<path d="M18 6 6 18M6 6l12 12"/>',
        info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
        "magnifying-glass": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    };

    //: What a file is to its project (relink.py `_role`): [label, noun].
    const ROLES = {
        image: ["Image", "image"],
        mask: ["Mask", "mask"],
        table: ["Cell table", "cell table"],
        layer: ["Layer", "layer"],
        file: ["File", "file"],
    };

    //: A file's kind, by its name, for the one-file card.
    const KINDS = [
        [/\.ome\.tiff?$/i, "OME-TIFF image"], [/\.ome\.zarr$/i, "OME-Zarr store"], [/\.zarr$/i, "Zarr store"],
        [/\.qptiff$/i, "QPTIFF image"], [/\.tiff?$/i, "TIFF image"], [/\.svs$/i, "SVS slide"],
        [/\.ndpis$/i, "NDPIS set"], [/\.ndpi$/i, "NDPI slide"], [/\.mrxs$/i, "MIRAX slide"],
        [/\.vsi$/i, "VSI slide"], [/\.czi$/i, "CZI image"], [/\.nd2$/i, "ND2 image"],
        [/\.(lif|lof|xlef)$/i, "Leica image"], [/\.mcd$/i, "MCD file"], [/\.dcm$/i, "DICOM file"],
        [/\.csv$/i, "CSV table"], [/\.tsv$/i, "TSV table"], [/\.parquet$/i, "Parquet table"],
        [/\.h5ad$/i, "AnnData file"], [/\.(png|jpe?g)$/i, "Image"],
    ];

    // -- words -----------------------------------------------------------------------

    function bytes(count) {
        let size = Number(count);
        if (!Number.isFinite(size) || size < 0) return "";
        const units = ["bytes", "KB", "MB", "GB", "TB"];
        let unit = 0;
        while (size >= 1024 && unit < units.length - 1) {
            size /= 1024;
            unit += 1;
        }
        return `${unit === 0 || size >= 100 ? Math.round(size) : size.toFixed(size >= 10 ? 1 : 2)} ${units[unit]}`;
    }

    const counted = (count) => Number(count).toLocaleString();
    const plural = (n, one, many) => (n === 1 ? one : many);

    function day(ms) {
        if (ms == null || !Number.isFinite(Number(ms))) return "";
        return new Date(Number(ms)).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
    }

    function dayTime(ms) {
        if (ms == null || !Number.isFinite(Number(ms))) return "";
        const time = new Date(Number(ms)).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
        return `${day(ms)}, ${time}`;
    }

    function kindOf(item) {
        if (item.kind === "folder") return item.entries != null ? `Folder of ${counted(item.entries)} files` : "Folder";
        const found = KINDS.find(([pattern]) => pattern.test(item.name || ""));
        if (found) return found[1];
        const ext = /\.([a-z0-9]{1,8})$/i.exec(item.name || "");
        return ext ? `${ext[1].toUpperCase()} file` : "File";
    }

    const roleOf = (item) => (item.made ? ["Pyramid", "pyramid"] : ROLES[item.role] || ROLES.file);
    const iconOf = (item) => (item.made ? "pyramid" : item.kind === "folder" && item.role === "file" ? "folder"
        : ROLES[item.role] ? item.role : "file");
    //: One of the website's own paths, not where another app found the file.
    const pageFile = (path) => /^\/(picked|derived|data)\//.test(String(path || ""));
    const noun = (item) => (item.kind === "folder" ? "folder" : "file");

    //: One line under a file's name: what it is, its size, its date.
    function metaOf(item) {
        const parts = [roleOf(item)[0]];
        if (item.made && item.from_name) parts[0] = `Pyramid made from ${item.from_name}`;
        if (item.kind === "folder" && item.entries != null) parts.push(`${counted(item.entries)} files`);
        if (item.size != null) parts.push(bytes(item.size));
        if (item.mtime_ms != null) parts.push(`modified ${day(item.mtime_ms)}`);
        return parts.filter(Boolean).join(" · ");
    }

    //: Why a file must be found, for one file (`single`) and for several.
    const REASONS = {
        nohandle: {
            title: (item) => `Find ${item.name} again`,
            why: (item) => `This browser does not let a website keep access to a ${noun(item)} between visits, `
                + `so Plexora cannot open it by itself. It did keep a description of the ${noun(item)}, `
                + "so it can check that you choose the same one.",
            several: "This browser does not let a website keep access to files between visits, "
                + "so Plexora cannot open them by itself.",
        },
        moved: {
            warn: true,
            title: (item) => `${item.name} is not where it was`,
            why: (item) => `Your browser still allows Plexora to open it, but the ${noun(item)} has been moved, `
                + "renamed or deleted since you last opened it here. If you moved it, choose it in its new place.",
            several: "These files have been moved, renamed or deleted since you last opened them here.",
        },
        denied: {
            title: (item) => `Plexora needs this ${noun(item)} to open the project`,
            why: (item) => `Your browser has not allowed Plexora to open the ${noun(item)} on this visit. `
                + `You can choose the ${noun(item)} yourself instead.`,
            several: "Your browser has not allowed Plexora to open these files on this visit.",
        },
        none: {
            title: (item) => `Find ${item.name} again`,
            why: (item) => `This browser has no access to the ${noun(item)}: it was chosen in another browser, `
                + "or this browser’s data for the site was cleared. The project still describes it, "
                + "so Plexora can check that you choose the same one.",
            several: "This browser has no access to these files: they were chosen in another browser, "
                + "or this browser’s data for the site was cleared.",
        },
        desktop: {
            title: (item) => `Find ${item.name}`,
            why: (item) => `This project was made in the Plexora desktop app, which recorded where the `
                + `${noun(item)} was. A website cannot open a ${noun(item)} by its path, so choose it yourself. `
                + `Plexora checks that it is the same ${noun(item)}.`,
            several: "This project was made in the Plexora desktop app, which recorded where its files were. "
                + "A website cannot open a file by its path.",
        },
    };
    const reasonOf = (row) => REASONS[row.reason] || REASONS.none;

    function hintOf(row) {
        if (row.item.kind === "folder") {
            return "Plexora checks the number of files in it, their size and their dates, not its name.";
        }
        if (row.reason === "moved") return "A renamed file is fine: Plexora checks its size and modified date, not its name.";
        if (row.reason === "denied") return "Choosing the file here lets Plexora read it on this visit.";
        return "Search your computer for its name. It may have another name now: "
            + "Plexora checks its size and modified date, not its name.";
    }

    //: What a different file would put out of step, by what the file is.
    function consequence(item) {
        if (item.role === "table") return "The project’s results are tied to that table’s rows, so with this one they may not match.";
        if (["image", "mask", "layer"].includes(item.role) || item.made) {
            return "The project’s cells, gates and annotations were made on that one, so on this one they may not line up.";
        }
        return "What the project made from it may not match this one.";
    }

    // -- the rows ----------------------------------------------------------------------

    const isAskable = (row) => row.reason === "prompt" || row.reason === "derived";
    const pickId = (picks, item) => (picks.idOf ? picks.idOf(item.here || "") || picks.idOf(item.recorded || "") : "");

    //: Each missing file with why it is missing: "derived" (a pyramid whose
    //: folder waits on a click), a pick's state (pickedFiles.js `status`:
    //: "prompt", "denied", "moved", "nohandle"), "none" (one of the website's
    //: paths with no pick behind it here), "desktop" (a path another app
    //: recorded).
    async function classify(items, picks) {
        const rows = [];
        for (const item of items) {
            if (item.waiting) {
                rows.push({ item, reason: "derived", pick: null, found: false });
                continue;
            }
            const id = pickId(picks, item);
            let found = { state: "none", record: null };
            if (id) {
                try {
                    found = await picks.status(id);
                } catch (error) { /* no record to say otherwise */ }
            }
            let reason = found.state;
            if (reason === "none" || reason === "mounted") reason = pageFile(item.recorded) ? "none" : "desktop";
            rows.push({ item, reason, pick: found.record || null, found: false });
        }
        return rows;
    }

    // -- the dialog ----------------------------------------------------------------------

    function make(doc, tag, className = "", text = null) {
        const node = doc.createElement(tag);
        if (className) node.className = className;
        if (text !== null && text !== undefined) node.textContent = String(text);
        return node;
    }

    function glyph(doc, name, className = "plx-reopen-glyph") {
        const node = make(doc, "span", className);
        node.setAttribute("aria-hidden", "true");
        node.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" `
            + `stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ICONS.file}</svg>`;
        return node;
    }

    /**
     * One `<dialog>` in `doc`, drawn again for each view. `show(view)`
     * resolves with the key of the action pressed, "skip" for the X or
     * Escape; `showFor(ms, view)` shows a view without actions for a moment.
     */
    function dialog(doc, reveal) {
        let node = null;
        let settle = null;
        let revealed = false;

        function finish(value) {
            const done = settle;
            settle = null;
            if (done) done(value);
        }

        function ensure() {
            if (node) return node;
            if (!revealed) {
                revealed = true;
                try { reveal(); } catch (error) { /* the loader shows anyway */ }
            }
            node = make(doc, "dialog", "plx-reopen");
            node.setAttribute("aria-labelledby", "plx-reopen-title");
            node.setAttribute("aria-describedby", "plx-reopen-lead");
            // Escape is the X: the project opens without what is missing.
            node.addEventListener("cancel", (event) => {
                event.preventDefault();
                finish("skip");
            });
            node.addEventListener("close", () => finish("skip"));
            (doc.body || doc.documentElement).appendChild(node);
            if (typeof node.showModal === "function") node.showModal();
            else node.setAttribute("open", "");
            return node;
        }

        function button(action) {
            const kind = action.kind || "secondary";
            const one = make(doc, "button", `plx-reopen-button is-${kind}`);
            one.type = "button";
            one.dataset.action = action.key;
            if (action.icon) one.appendChild(glyph(doc, action.icon));
            one.appendChild(make(doc, "span", "", action.label));
            one.addEventListener("click", () => {
                if (!settle) return;
                for (const other of node.querySelectorAll("button")) other.disabled = true;
                if (action.busy) one.lastChild.textContent = action.busy;
                node.classList.add("is-busy");
                finish(action.key);
            });
            return one;
        }

        function fileRow(row, index, view) {
            const item = row.item;
            const li = make(doc, "li", "plx-reopen-file");
            li.style.setProperty("--plx-reopen-i", String(index));
            li.appendChild(glyph(doc, iconOf(item), "plx-reopen-file-icon"));
            const words = make(doc, "div", "plx-reopen-file-words");
            words.appendChild(make(doc, "div", "plx-reopen-file-name", item.made && item.from_name && view.pyramidNames
                ? `Pyramid of ${item.from_name}` : item.name));
            words.appendChild(make(doc, "div", "plx-reopen-file-meta", row.meta || metaOf(item)));
            if (view.paths && item.recorded && !pageFile(item.recorded)) {
                const at = make(doc, "div", "plx-reopen-file-path", `Was at ${item.recorded}`);
                at.title = item.recorded;
                words.appendChild(at);
            }
            li.appendChild(words);
            const state = make(doc, "div", "plx-reopen-file-state");
            if (row.found || view.opened) {
                const ok = make(doc, "span", "plx-reopen-state is-found");
                ok.appendChild(glyph(doc, "check"));
                ok.appendChild(make(doc, "span", "", view.opened ? "Opened" : "Found"));
                state.appendChild(ok);
            } else if (view.locate) {
                state.appendChild(make(doc, "span", view.searched ? "plx-reopen-state is-missing" : "plx-reopen-state",
                                       view.searched ? (view.folders ? "Not in that folder" : "Not among those files") : "Not found yet"));
                state.appendChild(button({ key: `locate:${index}`, label: "Locate…", kind: "small" }));
            }
            if (state.childNodes.length) li.appendChild(state);
            return li;
        }

        //: The one file's card: its name, what it is, and what was recorded.
        function fileCard(row, view) {
            const item = row.item;
            const card = make(doc, "div", `plx-reopen-card${row.found ? " is-found" : ""}`);
            const top = make(doc, "div", "plx-reopen-card-top");
            top.appendChild(glyph(doc, iconOf(item), "plx-reopen-file-icon is-large"));
            const words = make(doc, "div", "plx-reopen-file-words");
            words.appendChild(make(doc, "div", "plx-reopen-card-name", item.name));
            const [label] = roleOf(item);
            words.appendChild(make(doc, "div", "plx-reopen-file-meta", item.made && item.from_name
                ? `Pyramid Plexora made from ${item.from_name}` : `${label} in the project ${view.project}`));
            top.appendChild(words);
            card.appendChild(top);
            const facts = make(doc, "dl", "plx-reopen-facts");
            const fact = (term, value, extra = "", wide = false) => {
                if (!value) return;
                const cell = make(doc, "div", `plx-reopen-fact${wide ? " is-wide" : ""}`);
                cell.appendChild(make(doc, "dt", "", term));
                const dd = make(doc, "dd", "", value);
                if (extra) dd.appendChild(make(doc, "span", "plx-reopen-fact-extra", ` (${extra})`));
                cell.appendChild(dd);
                facts.appendChild(cell);
            };
            fact("Size", item.size != null ? bytes(item.size) : "", item.size >= 1024 ? `${counted(item.size)} bytes` : "");
            fact(item.kind === "folder" ? "Newest file" : "Last modified", dayTime(item.mtime_ms));
            fact("Type", kindOf(item));
            if (row.pick && row.pick.picked) fact("Chosen here", day(row.pick.picked));
            if (item.recorded && !pageFile(item.recorded)) fact("Was at", item.recorded, "", true);
            card.appendChild(facts);
            if (row.found) {
                const same = make(doc, "div", "plx-reopen-card-same");
                same.appendChild(glyph(doc, "check"));
                same.appendChild(make(doc, "span", "", item.kind === "folder"
                    ? "Same folder: its files, size and dates match." : "Same file: the size and the modified date match."));
                card.appendChild(same);
            }
            return card;
        }

        //: The original beside what the user chose: name, size, date.
        function compare(view) {
            const table = make(doc, "table", "plx-reopen-compare");
            const head = make(doc, "tr");
            head.appendChild(make(doc, "th", "", ""));
            head.appendChild(make(doc, "th", "", view.compare.left));
            head.appendChild(make(doc, "th", "", view.compare.right));
            head.firstChild.setAttribute("scope", "col");
            head.childNodes[1].setAttribute("scope", "col");
            head.childNodes[2].setAttribute("scope", "col");
            const thead = make(doc, "thead");
            thead.appendChild(head);
            table.appendChild(thead);
            const body = make(doc, "tbody");
            for (const line of view.compare.rows) {
                const tr = make(doc, "tr");
                const th = make(doc, "th", "", line.label);
                th.setAttribute("scope", "row");
                tr.appendChild(th);
                tr.appendChild(make(doc, "td", "", line.was));
                const now = make(doc, "td");
                const value = make(doc, "span", line.same ? "plx-reopen-same" : "plx-reopen-differs", line.now);
                if (line.same) value.appendChild(glyph(doc, "check"));
                now.appendChild(value);
                tr.appendChild(now);
                body.appendChild(tr);
            }
            table.appendChild(body);
            return table;
        }

        function draw(view) {
            const box = ensure();
            const card = make(doc, "div", "plx-reopen-body");
            card.dataset.view = view.name;
            const head = make(doc, "div", "plx-reopen-head");
            head.appendChild(glyph(doc, view.icon, `plx-reopen-icon${view.tone ? ` is-${view.tone}` : ""}`));
            const words = make(doc, "div", "plx-reopen-heading");
            const title = make(doc, "h2", "plx-reopen-title", view.title);
            title.id = "plx-reopen-title";
            words.appendChild(title);
            const lead = make(doc, "p", "plx-reopen-lead", view.lead);
            lead.id = "plx-reopen-lead";
            words.appendChild(lead);
            head.appendChild(words);
            if (!view.closed) {
                const close = make(doc, "button", "plx-reopen-close");
                close.type = "button";
                close.setAttribute("aria-label", "Close");
                close.appendChild(glyph(doc, "close"));
                close.addEventListener("click", () => finish("skip"));
                head.appendChild(close);
            }
            card.appendChild(head);
            if (view.card) card.appendChild(fileCard(view.card, view));
            if (view.rows) {
                const list = make(doc, "ul", "plx-reopen-files");
                view.rows.forEach((row, index) => list.appendChild(fileRow(row, index, view)));
                card.appendChild(list);
            }
            if (view.compare) card.appendChild(compare(view));
            if (view.tip) {
                const tip = make(doc, "div", "plx-reopen-tip");
                tip.appendChild(glyph(doc, "info"));
                const text = make(doc, "span");
                text.append(view.tip[0]);
                text.appendChild(make(doc, "strong", "", view.tip[1]));
                text.append(view.tip[2]);
                tip.appendChild(text);
                card.appendChild(tip);
            }
            if (view.hint) {
                const hint = make(doc, "div", "plx-reopen-hint");
                hint.appendChild(glyph(doc, "info"));
                hint.appendChild(make(doc, "span", "", view.hint));
                card.appendChild(hint);
            }
            if (view.error) {
                const error = make(doc, "div", "plx-reopen-error", view.error);
                error.setAttribute("role", "alert");
                card.appendChild(error);
            }
            if (view.progress) {
                const bar = make(doc, "div", "plx-reopen-progress");
                bar.appendChild(make(doc, "span"));
                card.appendChild(bar);
            }
            let primary = null;
            if (view.actions && view.actions.length) {
                const row = make(doc, "div", "plx-reopen-actions");
                const start = make(doc, "div", "plx-reopen-actions-start");
                const end = make(doc, "div", "plx-reopen-actions-end");
                for (const action of view.actions) {
                    const one = button(action);
                    (action.kind === "primary" ? end : start).appendChild(one);
                    if (action.kind === "primary") primary = one;
                }
                row.appendChild(start);
                row.appendChild(end);
                card.appendChild(row);
            }
            if (view.footnote) card.appendChild(make(doc, "p", "plx-reopen-foot", view.footnote));
            box.classList.remove("is-busy");
            box.replaceChildren(card);
            const first = primary || box.querySelector("button");
            if (first && typeof first.focus === "function") first.focus();
        }

        return {
            show(view) {
                finish("replaced");
                return new Promise((resolve) => {
                    settle = resolve;
                    draw(view);
                });
            },
            async showFor(ms, view) {
                finish("replaced");
                draw({ ...view, closed: true });
                await new Promise((resolve) => global.setTimeout(resolve, ms));
            },
            close() {
                finish("skip");
                if (!node) return;
                const gone = node;
                node = null;
                try {
                    if (gone.open && typeof gone.close === "function") gone.close();
                } catch (error) { /* already closed */ }
                gone.remove();
            },
        };
    }

    // -- the views -------------------------------------------------------------------------

    function allowView(rows, title) {
        const one = rows.length === 1;
        const derivedOnly = rows.every((row) => row.reason === "derived");
        return {
            name: "allow", icon: "key", title: "Open your files from last time", project: title,
            lead: one
                ? "You used this file here before. Your browser still knows it, but asks again before a website "
                  + "can open files on a new visit."
                : "You used these files here before. Your browser still knows them, but asks again before a "
                  + "website can open files on a new visit.",
            rows: rows.map((row) => (row.reason === "derived"
                ? { ...row, meta: `Pyramid Plexora made${row.item.from_name ? ` from ${row.item.from_name}` : ""}`
                    + `${row.item.place ? ` · in the folder ${row.item.place}` : ""}` }
                : { ...row, meta: metaOf(row.item) })),
            tip: ["Your browser asks next. Choose ", "Allow on every visit",
                  one ? " and it opens without asking next time." : " and these open without asking next time."],
            actions: [
                ...(derivedOnly ? [] : [{ key: "self", kind: "link",
                                          label: one ? "Choose the file myself instead" : "Choose the files myself instead" }]),
                { key: "allow", kind: "primary", icon: "folder", label: "Allow access", busy: "Waiting for your browser…" },
            ],
        };
    }

    function openedView(rows, title, how) {
        return {
            name: "opened", icon: "check", tone: "done", title: `Opening ${title}`,
            lead: how === "allowed" ? "Your browser allowed Plexora to open these files again."
                : rows.length === 1 ? "Found it." : "Found every file the project needs.",
            rows: rows.map((row) => ({ ...row, found: true })), opened: how === "allowed", progress: true,
        };
    }

    function singleView(row, ctx, { error = "" } = {}) {
        const item = row.item;
        const reason = reasonOf(row);
        const folder = item.kind === "folder";
        return {
            name: "locate", icon: folder ? "findFolder" : "find", tone: reason.warn ? "warn" : "",
            title: reason.title(item), lead: reason.why(item), card: row, project: ctx.title, error,
            hint: hintOf(row),
            actions: [
                { key: "skip", kind: "secondary", label: "Open without it" },
                { key: "locate:0", kind: "primary", icon: "magnifying-glass", label: folder ? "Locate folder…" : "Locate file…" },
            ],
            footnote: row.reason === "nohandle" ? "In Chrome or Edge, files you used here before reopen after one click." : "",
        };
    }

    function listView(rows, ctx, { searched = false, error = "" } = {}) {
        const open = rows.filter((row) => !row.found);
        const folders = ctx.folders;
        const reasons = new Set(open.map((row) => row.reason));
        const reason = reasons.has("desktop") ? REASONS.desktop
            : reasons.size === 1 ? reasonOf(open[0]) : REASONS.none;
        const action = folders
            ? "Choose the folder that holds them: Plexora looks through it and its subfolders for files with these sizes and dates."
            : "Choose them in the file dialog, several at once: Plexora matches each by its size and date.";
        const anyFound = rows.some((row) => row.found);
        return {
            name: "list", icon: "findFolder", tone: reasons.has("moved") && reasons.size === 1 ? "warn" : "",
            title: anyFound || searched
                ? `${open.length} ${plural(open.length, "file", "files")} of this project ${plural(open.length, "is", "are")} still missing`
                : `Find the ${open.length} files of this project`,
            lead: `${reason.several} ${action}`,
            rows, locate: true, searched, folders, paths: true, pyramidNames: false, error,
            actions: [
                { key: "skip", kind: "secondary", label: open.length === 1 ? "Open without it" : "Open without them" },
                { key: "search", kind: "primary", icon: folders ? "folder" : "magnifying-glass",
                  label: folders ? (searched ? "Search another folder…" : "Choose folder…")
                      : (searched ? "Choose more files…" : "Choose files…") },
            ],
            footnote: reasons.has("nohandle") ? "In Chrome or Edge, files you used here before reopen after one click." : "",
        };
    }

    function mismatchView(row, chosen, ctx, { fromList = false, error = "" } = {}) {
        const item = row.item;
        const folder = item.kind === "folder";
        const name = chosen.name || "";
        const sameName = name === item.name;
        const rows = [{ label: "Name", was: item.name, now: name || "—", same: sameName }];
        if (folder) {
            rows.push({ label: "Files", was: item.entries != null ? counted(item.entries) : "—",
                        now: chosen.entries != null ? counted(chosen.entries) : "—",
                        same: chosen.entries === item.entries });
        }
        rows.push({ label: "Size", was: item.size != null ? bytes(item.size) : "—",
                    now: chosen.size != null ? bytes(chosen.size) : "—", same: chosen.size === item.size });
        rows.push({ label: folder ? "Newest file" : "Modified", was: dayTime(item.mtime_ms) || "—",
                    now: dayTime(chosen.mtime_ms) || "—",
                    same: item.mtime_ms != null && chosen.mtime_ms != null && Math.abs(chosen.mtime_ms - item.mtime_ms) <= 1 });
        const what = folder ? "folder" : roleOf(item)[1];
        const sentence = sameName
            ? `It has the same name, but not the ${folder ? "files, size or dates" : "size or the modified date"} of the ${what} this project was made with.`
            : `It is not the ${what} this project was made with: ${folder ? "its files, size or dates differ" : "its size or modified date differ"}.`;
        return {
            name: "mismatch", icon: "triangle-exclamation", tone: "warn",
            title: folder ? "That is a different folder" : "That is a different file",
            lead: `${sentence} ${consequence(item)}`,
            compare: { left: `This project’s ${what}`, right: folder ? "The folder you chose" : "The file you chose", rows },
            hint: item.mtime_ms != null
                ? `${item.role === "image" ? "A newer export of the same slide is a different file to Plexora. " : ""}`
                  + `Look for the copy modified ${dayTime(item.mtime_ms)}.`
                : "",
            error,
            actions: [
                ...(fromList ? [{ key: "back", kind: "link", label: "Back to the list" }] : []),
                { key: "anyway", kind: "secondary", label: folder ? "Use this folder anyway" : "Use this file anyway",
                  busy: "Opening…" },
                { key: "another", kind: "primary", icon: "magnifying-glass", label: folder ? "Choose another folder…" : "Choose another file…" },
            ],
        };
    }

    function questionView(text) {
        return {
            name: "question", icon: "folder", title: "One more folder", lead: String(text),
            actions: [{ key: "yes", kind: "primary", icon: "folder", label: "Choose folder…" }],
        };
    }

    // -- the flow ----------------------------------------------------------------------------

    const message = (error) => String((error && error.message) || error || "Something went wrong.");

    async function run({ doc, project, python, picks, derived = null, reveal = () => {}, status = () => {} } = {}) {
        if (!doc || !project || !python || !picks) return false;
        const route = `/project/${encodeURIComponent(project)}/files_here`;
        const ask = async () => {
            const response = await python.fetch(route);
            if (!response.ok) throw new Error(`files_here answered ${response.status}`);
            return response.json();
        };
        let answer;
        try {
            answer = await ask();
        } catch (error) {
            return false;
        }
        let missing = Array.isArray(answer.missing) ? answer.missing : [];
        if (!missing.length) return false;
        const title = String(answer.title || project);
        let changed = false;

        // 1. What the browser opens without asking now.
        if (await quietly(missing, picks, derived)) {
            changed = true;
            try {
                missing = (await ask()).missing || [];
            } catch (error) {
                return true;
            }
            if (!missing.length) return true;
        }

        const ui = dialog(doc, reveal);
        const ctx = {
            title, picks, status, ui,
            folders: typeof global.showDirectoryPicker === "function",
            //: Where the files are now, told to Python: {recorded, missing, refused}.
            async record(found, anyway = []) {
                const response = await python.fetch(route, {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ found, anyway }),
                });
                if (!response.ok) throw new Error(`Plexora could not record the file (${response.status}).`);
                return response.json();
            },
            //: pickedFiles.js's one-button question (an MRXS's folder and the like).
            async question(text) {
                return (await ui.show(questionView(text))) === "yes";
            },
        };
        try {
            let rows = await classify(missing, picks);
            const askable = rows.filter(isAskable);
            // 2. The browser holds them, and asks again: one click.
            if (askable.length) {
                const action = await ui.show(allowView(askable, title));
                if (action === "skip") return changed;
                if (action === "allow") {
                    await allow(askable, picks, derived);
                    try {
                        missing = (await ask()).missing || [];
                    } catch (error) {
                        missing = askable.map((row) => row.item);
                    }
                    const still = new Set(missing.map((item) => item.recorded));
                    if (askable.some((row) => !still.has(row.item.recorded))) changed = true;
                    if (!missing.length) {
                        await ui.showFor(OPENED_MS, openedView(askable, title, "allowed"));
                        return true;
                    }
                    rows = await classify(missing, picks);
                }
                // Asked, or the user chose to find them: not allowed this visit.
                rows = rows.map((row) => (isAskable(row) ? { ...row, reason: "denied" } : row));
            }
            // 3. Found by the user.
            return (await locate(rows, ctx)) || changed;
        } finally {
            ui.close();
        }
    }

    //: The picks of `items` the browser grants already, and the pyramids'
    //: folders likewise, mounted without a question. -> how many opened.
    async function quietly(items, picks, derived) {
        let opened = 0;
        const ids = [...new Set(items.map((item) => pickId(picks, item)).filter(Boolean))];
        if (ids.length && picks.reconnect) {
            const done = await picks.reconnect({ ask: false, ids }).catch(() => null);
            opened += done && done.mounted ? done.mounted.length : 0;
        }
        if (derived && derived.reconnect && items.some((item) => item.waiting)) {
            const done = await derived.reconnect({ ask: false }).catch(() => null);
            opened += done && done.mounted ? done.mounted.length : 0;
        }
        return opened;
    }

    //: From Allow access's click: the browser asked for the picks (its prompt
    //: lists every handle the site held), then the pyramids' folders -- asked
    //: only if the picks were not refused, so a no is not asked twice.
    async function allow(rows, picks, derived) {
        const ids = rows.filter((row) => row.reason === "prompt" && row.pick).map((row) => row.pick.id);
        let refused = false;
        if (ids.length && picks.reconnect) {
            const done = await picks.reconnect({ ask: true, ids }).catch(() => null);
            refused = !(done && done.mounted && done.mounted.length);
        }
        if (derived && derived.reconnect && rows.some((row) => row.reason === "derived")) {
            await derived.reconnect({ ask: !refused }).catch(() => null);
        }
    }

    //: Python's `missing` after a record, as each row's `found`.
    function apply(rows, result) {
        const still = new Set(((result && result.missing) || []).map((item) => item.recorded));
        for (const row of rows) row.found = !still.has(row.item.recorded);
    }

    async function locate(rows, ctx) {
        const { ui } = ctx;
        let found = false;
        let searched = false;
        let error = "";
        for (;;) {
            if (rows.every((row) => row.found)) {
                await ui.showFor(OPENED_MS, openedView(rows, ctx.title, "found"));
                return true;
            }
            const single = rows.length === 1;
            const action = await ui.show(single ? singleView(rows[0], ctx, { error })
                : listView(rows, ctx, { searched, error }));
            error = "";
            if (action === "skip") return found;
            try {
                if (action === "search") {
                    const open = rows.filter((row) => !row.found);
                    const hits = await ctx.picks.find(open.map((row) => row.item));
                    if (hits === null) continue; // cancelled
                    searched = true;
                    if (Object.keys(hits).length) {
                        ctx.status("Looking for the files");
                        const result = await ctx.record(hits);
                        apply(rows, result);
                        if (result.recorded) found = true;
                    }
                    continue;
                }
                if (String(action).startsWith("locate:")) {
                    const row = rows[Number(String(action).split(":")[1])] || rows[0];
                    const done = await locateOne(row, rows, ctx, { fromList: !single });
                    if (done === "found") found = true;
                    if (done === "skip") return found;
                }
            } catch (failure) {
                error = message(failure);
            }
        }
    }

    //: One row found by the user: a file in the browser's file dialog (or a
    //: folder in its folder dialog), taken when it is the same one, else shown
    //: beside the original. -> "found", "back" or "skip".
    async function locateOne(row, rows, ctx, { fromList = false } = {}) {
        const item = row.item;
        if (item.kind === "folder") {
            const path = await ctx.picks.pick({ mode: "directory" });
            if (!path) return "back";
            const result = await ctx.record({ [item.recorded]: path });
            apply(rows, result);
            if (row.found) return "found";
            const facts = (result.refused || {})[item.recorded] || {};
            return differs(row, rows, ctx, { path, name: facts.name || path.split("/").filter(Boolean).pop(),
                                            size: facts.size, mtime_ms: facts.mtime_ms, entries: facts.entries },
                           { fromList });
        }
        const chosen = await ctx.picks.chooseFile(item);
        if (!chosen) return "back";
        return take(row, rows, ctx, chosen, { fromList });
    }

    //: A chosen file: kept and recorded when it is the same one; otherwise
    //: shown beside the original.
    async function take(row, rows, ctx, chosen, { fromList }) {
        const facts = { name: chosen.file.name, size: chosen.file.size, mtime_ms: chosen.file.lastModified, chosen };
        if (chosen.same) {
            const path = await ctx.picks.keep(chosen, { ask: ctx.question });
            if (!path) return "back";
            facts.path = path;
            const result = await ctx.record({ [row.item.recorded]: path });
            apply(rows, result);
            if (row.found) return "found";
        }
        return differs(row, rows, ctx, facts, { fromList });
    }

    //: The original beside what the user chose, until they choose another
    //: (taken if it is the one), use this one anyway, go back, or close.
    async function differs(row, rows, ctx, chosen, { fromList }) {
        const item = row.item;
        let current = chosen;
        let error = "";
        for (;;) {
            const action = await ctx.ui.show(mismatchView(row, current, ctx, { fromList, error }));
            error = "";
            if (action === "skip" || action === "back") return action;
            try {
                if (action === "another") {
                    if (item.kind === "folder") {
                        const path = await ctx.picks.pick({ mode: "directory" });
                        if (!path) continue;
                        const result = await ctx.record({ [item.recorded]: path });
                        apply(rows, result);
                        if (row.found) return "found";
                        const facts = (result.refused || {})[item.recorded] || {};
                        current = { path, name: facts.name || path.split("/").filter(Boolean).pop(),
                                    size: facts.size, mtime_ms: facts.mtime_ms, entries: facts.entries };
                        continue;
                    }
                    const next = await ctx.picks.chooseFile(item);
                    if (!next) continue;
                    if (next.same) return take(row, rows, ctx, next, { fromList });
                    current = { name: next.file.name, size: next.file.size, mtime_ms: next.file.lastModified, chosen: next };
                    continue;
                }
                if (action === "anyway") {
                    let path = current.path || null;
                    if (!path && current.chosen) path = await ctx.picks.keep(current.chosen, { ask: ctx.question });
                    if (!path) continue;
                    current.path = path;
                    const result = await ctx.record({ [item.recorded]: path }, [item.recorded]);
                    apply(rows, result);
                    if (row.found) return "found";
                    error = "Plexora could not use that file.";
                }
            } catch (failure) {
                error = message(failure);
            }
        }
    }

    global.PlexoraReopen = Object.freeze({
        run,
        //: For tests: why each missing file is missing.
        classify,
    });
})(window);
