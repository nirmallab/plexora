/**
 * openProjectPage.js -- the project browser.
 *
 * It was a flat grid. That is the right shape for twelve projects and the
 * wrong one for a cohort of forty slides, where the only thing holding a trial
 * together was a naming convention in the project names.
 *
 * So: datasets are folders and this is a lightweight file browser. Folders
 * first, then everything not in one; click a folder to go in, drag cards onto
 * one to move them, drag them onto the breadcrumb to take them out again.
 * Deliberately NOT a management screen -- there is no dataset editor, no
 * properties panel, no second page. Everything a dataset can be asked is asked
 * from the card it is drawn as.
 *
 * Three properties the design turns on:
 *
 * - **A dataset owns nothing.** Deleting one releases its projects; taking a
 *   project out of one is not deleting the project. Every destructive-looking
 *   action here says which of the two it is, because that is the one thing a
 *   folder metaphor gets wrong by default.
 * - **Search flattens.** Inside a folder or not, typing searches every project
 *   there is and tags each result with where it lives. Somebody who is
 *   searching has stopped navigating, and a search that only looked in the
 *   current folder would silently hide the thing they were looking for.
 * - **A selection is what gets moved.** Dragging one card of five selected
 *   moves all five; dragging a card that is not in the selection moves that
 *   card alone and leaves the selection where it was. The same rule Figure
 *   Builder's workspace uses, because it is the rule every file browser uses.
 *
 * The project card's own markup is unchanged. Figure Builder's Figures and
 * Captures pages render from `.project-card` / `.project-thumb` /
 * `.project-actions` verbatim, so everything here ADDS classes and never
 * restructures.
 */
(function () {
    const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

    //: What a drag carries. A custom type rather than text/plain, so dragging a
    //: card over an unrelated drop target on the page does nothing at all --
    //: the same reason figureWorkspace.js has one.
    const DRAG_TYPE = "text/x-plexora-projects";

    function timeAgo(iso) {
        if (!iso) return "";
        const then = new Date(iso).getTime();
        if (Number.isNaN(then)) return "";
        const diffSec = Math.round((Date.now() - then) / 1000);
        if (diffSec < 60) return "just now";
        const diffMin = Math.round(diffSec / 60);
        if (diffMin < 60) return diffMin + (diffMin === 1 ? " minute ago" : " minutes ago");
        const diffHour = Math.round(diffMin / 60);
        if (diffHour < 24) return diffHour + (diffHour === 1 ? " hour ago" : " hours ago");
        const diffDay = Math.round(diffHour / 24);
        if (diffDay < 30) return diffDay + (diffDay === 1 ? " day ago" : " days ago");
        return new Date(iso).toLocaleDateString(undefined,
            { year: "numeric", month: "short", day: "numeric" });
    }

    function countPhrase(n, noun) {
        return `${n} ${noun}${n === 1 ? "" : "s"}`;
    }

    //: How each image kind is named on a badge. Anything unlisted falls back to
    //: "Image", which is true of every one of them and says nothing misleading.
    const IMAGE_KINDS = {
        ome_tiff: "OME-TIFF", tiff: "TIFF", ome_zarr: "OME-Zarr",
        brightfield: "H&E", rgb: "Image", dicom: "DICOM",
        // A sample registered against a blank reference frame. Named rather
        // than left to the "Image" fallback, because "Image" is the one thing
        // it is not -- what the card is telling the reader is that the picture
        // here is the layers.
        blank: "No image",
    };

    PlexoraPage.register(() => {
        const resultsEl = document.getElementById("project-results");
        if (!resultsEl) return; // Not on the Open Project page.

        const searchInput = document.getElementById("project-search");
        const sortSelect = document.getElementById("project-sort");
        const gridButton = document.getElementById("project-view-grid");
        const listButton = document.getElementById("project-view-list");
        const countEl = document.getElementById("project-count");
        const emptyStateEl = document.getElementById("project-empty-state");
        const noResultsEl = document.getElementById("project-no-results");
        const folderEmptyEl = document.getElementById("dataset-empty-state");
        const crumbsEl = document.getElementById("project-crumbs");
        const barEl = document.getElementById("project-selection-bar");
        const barCountEl = document.getElementById("project-selection-count");
        const createDatasetButton = document.getElementById("dataset-create");

        const state = {
            projects: [],
            datasets: [],
            //: The dataset being looked inside, or null for the top level.
            folder: null,
            query: "",
            sort: "lastOpenedAt",
            view: localStorage.getItem("plexora.openProjectView") === "list" ? "list" : "grid",
            //: Project names, as a Set: order is the render order, not the
            //: order they were ticked.
            selection: new Set(),
            //: Where a shift-click range starts from.
            anchor: null,
            //: The visible order, recomputed on every render. Shift-click
            //: extends over THIS rather than over the unfiltered list, because
            //: what a range means is "everything between these two on screen".
            order: [],
        };

        // ------------------------------------------------------------------
        // Where we are
        // ------------------------------------------------------------------

        /** The dataset record for `state.folder`, or null. */
        function currentFolder() {
            return state.datasets.find((d) => d.id === state.folder) || null;
        }

        /**
         * Enter or leave a folder, and record it in the URL.
         *
         * `replaceState` rather than `pushState`: the browser Back button on
         * this page means "go back to where I came from", and a user who
         * opened three folders looking for something should not have to press
         * it three times to leave. What the URL buys is that a reload, or a
         * link someone pastes to a colleague, lands in the same folder.
         */
        function setFolder(id) {
            state.folder = id || null;
            state.selection.clear();
            state.anchor = null;
            const url = new URL(window.location.href);
            if (state.folder) url.searchParams.set("dataset", state.folder);
            else url.searchParams.delete("dataset");
            window.history.replaceState(null, "", url);
            render();
        }

        // ------------------------------------------------------------------
        // What to show
        // ------------------------------------------------------------------

        function sorted(list) {
            const out = list.slice();
            if (state.sort === "name") {
                out.sort((a, b) => a.name.localeCompare(b.name));
            } else {
                out.sort((a, b) => {
                    const aTime = a[state.sort] ? new Date(a[state.sort]).getTime() : 0;
                    const bTime = b[state.sort] ? new Date(b[state.sort]).getTime() : 0;
                    return bTime - aTime;
                });
            }
            return out;
        }

        /**
         * The folders and projects this view shows, in render order.
         *
         * Three views out of one function, because they differ only in what is
         * in them: the top level (folders, then loose projects), inside a
         * folder (its members), and a search (every match, flattened, with no
         * folders in the project list -- matching folders lead instead).
         */
        function visible() {
            const query = state.query.trim().toLowerCase();
            if (query) {
                return {
                    // Folders only at the top level: a folder card inside
                    // another folder is a place the user cannot be, since
                    // datasets do not nest.
                    folders: state.folder ? [] : state.datasets.filter(
                        (d) => d.name.toLowerCase().includes(query)),
                    projects: sorted(state.projects.filter(
                        (p) => p.name.toLowerCase().includes(query))),
                    flattened: true,
                };
            }
            if (state.folder) {
                return {
                    folders: [],
                    projects: sorted(state.projects.filter(
                        (p) => p.dataset && p.dataset.id === state.folder)),
                    flattened: false,
                };
            }
            return {
                folders: state.datasets.slice().sort(
                    (a, b) => a.name.localeCompare(b.name)),
                projects: sorted(state.projects.filter((p) => !p.dataset)),
                flattened: false,
            };
        }

        // ------------------------------------------------------------------
        // Markup
        // ------------------------------------------------------------------

        function dateCaption(project) {
            if (state.sort === "createdAt") {
                return project.createdAt ? "Created " + timeAgo(project.createdAt) : "";
            }
            return project.lastOpenedAt
                ? "Opened " + timeAgo(project.lastOpenedAt) : "Never opened";
        }

        function thumbMarkup(project) {
            const thumbUrl = plexoraUrl("project_thumbnail/" + encodeURIComponent(project.name));
            return `<span class="project-thumb">
                <img src="${thumbUrl}" alt="" loading="lazy" onerror="this.parentElement.classList.add('project-thumb-fallback');this.remove();">
                <span class="fas fa-image project-thumb-icon"></span>
            </span>`;
        }

        function sharedMarkup(project) {
            // Only on shared projects, so an install with no shared roots --
            // which is every single-user one -- looks exactly as it did.
            if (!project.shared) return "";
            return `<span class="project-shared" title="On a shared data directory: open and explore it, but it cannot be edited or deleted here."><span class="fas fa-users"></span>Shared</span>`;
        }

        /**
         * What this project has, in three or four words.
         *
         * Only what somebody scanning a cohort needs to tell one card from
         * another: the image format, whether there is a mask, whether there is
         * data, and whether something about it is unfinished. Not a manifest --
         * the edit page is where a project is inspected.
         */
        function badgesMarkup(project) {
            const badges = [];
            const kind = IMAGE_KINDS[project.imageKind] || (project.imageKind ? "Image" : "");
            if (kind) badges.push(`<span class="project-badge">${escapeHtml(kind)}</span>`);
            if (project.segmentation === "present") {
                badges.push(`<span class="project-badge">Mask</span>`);
            } else if (project.segmentation === "pending") {
                // Dimmed rather than absent: the mask is real, it is simply
                // still converting, and a card that said nothing would read as
                // a project the user forgot to give one to.
                badges.push(`<span class="project-badge project-badge-dim"
                    title="The segmentation mask is still being prepared">Mask</span>`);
            }
            if (project.table === "present") {
                badges.push(`<span class="project-badge">Data</span>`);
            }
            // What ELSE is in this sample. One badge however many layers there
            // are, because a card with six badges is a card nobody reads --
            // the count is the useful part and the modalities are the title.
            const extra = project.layers?.count || 0;
            if (extra) {
                const what = (project.layers.modalities || [])
                    .filter((name) => name !== "mask" && name !== "centroids");
                badges.push(`<span class="project-badge" title="${
                    escapeHtml(what.join(", "))}">${
                    escapeHtml(countPhrase(extra, "layer"))}</span>`);
            }
            if (project.needsSetup) {
                // The one badge that is a call to action. A data file was named
                // and something about it is still undecided, so the project
                // opens as an image and there is nowhere else this would show.
                badges.push(`<span class="project-badge project-badge-warn"
                    title="A data file is registered but Plexora still needs to know which table or image to read. It will ask when a tool needs it."
                    ><span class="fas fa-triangle-exclamation"></span>Needs setup</span>`);
            }
            return badges.length
                ? `<span class="project-badges">${badges.join("")}</span>` : "";
        }

        /** Where a project lives, shown only in a flattened search result. */
        function tagMarkup(project, flattened) {
            if (!flattened || !project.dataset) return "";
            return `<span class="project-card-tag" data-goto-dataset="${escapeHtml(project.dataset.id)}"
                ><span class="fas fa-folder"></span>${escapeHtml(project.dataset.name)}</span>`;
        }

        function actionsMarkup(project) {
            const editUrl = plexoraUrl("edit_config/" + encodeURIComponent(project.name));
            const name = escapeHtml(project.name);
            // A shared project belongs to whoever provisioned the root it sits
            // on. The server refuses both of these with a 403 regardless; not
            // offering them is what stops the user finding that out only after
            // confirming a delete dialog.
            if (project.shared) return "";
            return `<span class="project-actions">
                <a href="${editUrl}" class="project-action" title="Edit"><span class="fas fa-pencil"></span></a>
                <a href="#" class="project-action project-action-danger" title="Delete"
                   data-delete-project="${name}"><span class="fas fa-trash"></span></a>
            </span>`;
        }

        /** The tick that selects a card without opening it. */
        function selectMarkup(project) {
            return `<span class="project-select" data-select-project="${escapeHtml(project.name)}"
                        role="checkbox" tabindex="0"
                        aria-checked="${state.selection.has(project.name) ? "true" : "false"}"
                        title="Select"><span class="fas fa-check"></span></span>`;
        }

        function projectMarkup(project, flattened, row) {
            const href = plexoraUrl(encodeURIComponent(project.name));
            const name = escapeHtml(project.name);
            const caption = escapeHtml(dateCaption(project));
            const kind = row ? "row" : "card";
            const selected = state.selection.has(project.name) ? " is-selected" : "";
            return `<div class="project-${kind}${selected}" draggable="true"
                         data-project-name="${name}">
                ${selectMarkup(project)}
                <a class="project-${kind}-link" href="${href}" title="${name}">
                    ${thumbMarkup(project)}
                    <span class="project-${kind}-name">${name}</span>
                    <span class="project-${kind}-date">${caption}${sharedMarkup(project)}</span>
                    ${badgesMarkup(project)}
                </a>
                ${tagMarkup(project, flattened)}
                ${actionsMarkup(project)}
            </div>`;
        }

        /**
         * The folder a dataset is drawn with: a back panel, two sheets of
         * paper and a front pocket.
         *
         * Not `fa-folder`. A bare folder outline says "container" and stops
         * there, and at the size a card gives it -- most of a 4:3 thumbnail --
         * a single flat glyph in accent amber was the loudest thing on a page
         * whose actual subject is the project thumbnails around it. The
         * paper is what says a dataset holds projects rather than files on
         * disk, and it earns the extra size by having something to show at it.
         *
         * Four flat tones from one muted slate, no stroke and no gradient: the
         * depth comes from the tones being ordered back-to-front, which
         * survives being 30px wide in a row as well as 64px in a card. Every
         * fill is opaque -- layering translucent shapes would let the sheets
         * ghost through the pocket that is meant to be in front of them.
         *
         * The two sheets are two tones rather than one. At a card's size a
         * pair of same-coloured rectangles overlapping by a third of their
         * width is one rectangle with a bite out of it, which is not what a
         * second sheet is for.
         */
        function folderGlyph() {
            return `<svg class="project-folder-glyph" viewBox="0 0 64 52"
                         aria-hidden="true" focusable="false">
                <path class="plx-folder-back"
                      d="M2 8a4 4 0 0 1 4-4h15a4 4 0 0 1 3.1 1.5l2.6 3.2a2 2 0 0 0 1.55.75H58a4 4 0 0 1 4 4v30a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4Z"/>
                <rect class="plx-folder-sheet-back" x="11" y="12" width="22" height="20" rx="1.5"
                      transform="rotate(-9 22 22)"/>
                <rect class="plx-folder-sheet" x="27" y="10" width="24" height="22" rx="1.5"
                      transform="rotate(6 39 21)"/>
                <rect class="plx-folder-face" x="3.2" y="26" width="57.6" height="21.4" rx="4"/>
            </svg>`;
        }

        /**
         * A dataset, drawn as a card in the same grid as the projects.
         *
         * `.project-card` as well as `.project-card-folder`, so it lands in the
         * grid the projects are in and inherits its size and spacing. The
         * folder is what differs, not the layout.
         */
        function folderMarkup(dataset, row) {
            const kind = row ? "row" : "card";
            const name = escapeHtml(dataset.name);
            return `<div class="project-${kind} project-${kind}-folder"
                         data-dataset-id="${escapeHtml(dataset.id)}"
                         data-drop-dataset="${escapeHtml(dataset.id)}">
                <a class="project-${kind}-link" href="?dataset=${encodeURIComponent(dataset.id)}"
                   data-open-dataset="${escapeHtml(dataset.id)}" title="${name}">
                    <span class="project-thumb project-thumb-folder">
                        ${folderGlyph()}
                    </span>
                    <span class="project-${kind}-name">${name}</span>
                    <span class="project-${kind}-date">${escapeHtml(
                        countPhrase(dataset.projectCount || 0, "sample"))}</span>
                </a>
                <span class="project-actions">
                    <a href="#" class="project-action" title="Rename"
                       data-rename-dataset="${escapeHtml(dataset.id)}"><span class="fas fa-pencil"></span></a>
                    <a href="#" class="project-action project-action-danger" title="Delete dataset"
                       data-delete-dataset="${escapeHtml(dataset.id)}"><span class="fas fa-trash"></span></a>
                </span>
            </div>`;
        }

        function crumbsMarkup() {
            const folder = currentFolder();
            const root = `<button type="button" class="open-project-crumb${folder ? "" : " is-current"}"
                    data-drop-root="1" data-open-dataset="">All projects</button>`;
            if (!folder) return root;
            return root
                + `<span class="open-project-crumb-sep fas fa-chevron-right" aria-hidden="true"></span>`
                + `<span class="open-project-crumb is-current">${escapeHtml(folder.name)}</span>`;
        }

        // ------------------------------------------------------------------
        // Rendering
        // ------------------------------------------------------------------

        function render() {
            const total = state.projects.length;
            const folder = currentFolder();
            countEl.textContent = folder
                ? `${escapeHtml(folder.name)} — ${countPhrase(folder.projectCount || 0, "sample")}`
                : countPhrase(total, "sample");
            crumbsEl.innerHTML = crumbsMarkup();

            const { folders, projects, flattened } = visible();
            state.order = projects.map((p) => p.name);
            // A selection cannot survive leaving the view it was made in: the
            // bar would offer to move projects that are no longer on screen.
            for (const name of Array.from(state.selection)) {
                if (!state.order.includes(name)) state.selection.delete(name);
            }

            const row = state.view === "list";
            resultsEl.className = "project-results "
                + (row ? "project-list" : "project-grid");
            resultsEl.innerHTML =
                folders.map((d) => folderMarkup(d, row)).join("")
                + projects.map((p) => projectMarkup(p, flattened, row)).join("");

            const nothingAtAll = total === 0 && !state.datasets.length;
            const nothingHere = !folders.length && !projects.length;
            emptyStateEl.hidden = !nothingAtAll;
            noResultsEl.hidden = !(nothingHere && !nothingAtAll && state.query.trim());
            // An emptied folder still has to be a drop target, which is the
            // whole reason this state has a panel of its own rather than
            // reusing "no projects match". The panel says "drag projects
            // here", so it has to carry the folder it stands for -- without
            // this it is a sentence that invites a gesture and then ignores
            // it, which is worse than not offering it.
            folderEmptyEl.hidden = !(nothingHere && !nothingAtAll
                                     && !state.query.trim() && folder);
            if (folder) folderEmptyEl.dataset.dropDataset = folder.id;
            else delete folderEmptyEl.dataset.dropDataset;

            paintSelection();
        }

        function paintSelection() {
            const n = state.selection.size;
            barEl.hidden = n === 0;
            if (!n) return;
            barCountEl.textContent = `${countPhrase(n, "sample")} selected`;
            const chosen = selectedProjects();
            // "Remove from dataset" only when every one of them is in one:
            // offered on a mixed selection it would look like it had failed on
            // half of them.
            barEl.querySelector('[data-action="unassign"]').hidden =
                !chosen.every((p) => p.dataset);
            // Delete is refused server-side for a shared project with a 403.
            // Disabling it here is what stops the user finding that out after
            // confirming.
            const anyShared = chosen.some((p) => p.shared);
            const remove = barEl.querySelector('[data-action="delete"]');
            remove.disabled = anyShared;
            remove.title = anyShared
                ? "Some of these are on a shared data directory and cannot be deleted here"
                : "";
        }

        function selectedProjects() {
            return state.projects.filter((p) => state.selection.has(p.name));
        }

        function updateViewButtons() {
            gridButton.classList.toggle("active", state.view === "grid");
            listButton.classList.toggle("active", state.view === "list");
        }

        // ------------------------------------------------------------------
        // Talking to the server
        // ------------------------------------------------------------------

        async function reload() {
            const [projects, datasets] = await Promise.all([
                fetch(plexoraUrl("projects")).then((r) => r.json()).catch(() => []),
                fetch(plexoraUrl("datasets")).then((r) => r.json()).catch(() => null),
            ]);
            state.projects = Array.isArray(projects) ? projects : [];
            state.datasets = (datasets && datasets.datasets) || [];
            // A folder that has been deleted in another tab is not somewhere
            // to stand; the crumb would name nothing and the view would be
            // permanently empty.
            if (state.folder && !currentFolder()) state.folder = null;
            render();
        }

        /** POST JSON, report through the status chip, reload on success. */
        async function post(url, body, label) {
            const task = window.PlexoraStatus?.begin(label);
            try {
                const response = await fetch(plexoraUrl(url), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body || {}),
                });
                const result = await response.json().catch(() => ({}));
                if (!response.ok || result.success === false) {
                    throw new Error(result.error || `${label} failed`);
                }
                task?.done();
                return result;
            } catch (e) {
                task?.fail(e.message);
                return null;
            }
        }

        /**
         * Move projects into a dataset, or out of every one.
         *
         * One request for the whole selection rather than one per project:
         * half a move that failed in the middle is a state nothing on this
         * page can describe, and the server does the whole thing under one
         * lock.
         */
        async function assign(names, datasetId) {
            if (!names.length) return;
            const result = await post("projects/assign",
                { projects: names, dataset: datasetId || null }, "Moving");
            if (!result) return;
            state.selection.clear();
            state.anchor = null;
            await reload();
        }

        // ------------------------------------------------------------------
        // Selection
        // ------------------------------------------------------------------

        function toggle(name) {
            if (state.selection.has(name)) state.selection.delete(name);
            else state.selection.add(name);
            state.anchor = name;
            render();
        }

        /** Everything between the anchor and `name` in the visible order. */
        function extendTo(name) {
            const from = state.order.indexOf(state.anchor);
            const to = state.order.indexOf(name);
            if (from < 0 || to < 0) return toggle(name);
            const [lo, hi] = from <= to ? [from, to] : [to, from];
            for (let i = lo; i <= hi; i += 1) state.selection.add(state.order[i]);
            render();
        }

        function clearSelection() {
            if (!state.selection.size) return;
            state.selection.clear();
            state.anchor = null;
            render();
        }

        // ------------------------------------------------------------------
        // Dataset actions
        // ------------------------------------------------------------------

        /** @param {?string} named a name the picker already collected, if the
         *  user came through its "New dataset…" box rather than the toolbar
         *  button -- which has nowhere to have asked yet. */
        async function createDataset(withProjects, named) {
            const name = named || await window.PlexoraConfirm.prompt({
                title: "New dataset",
                body: "A dataset groups projects that belong together — a cohort, a TMA series, one imaging run.",
                placeholder: "Melanoma Cohort",
                confirm: "Create",
            });
            if (!name) return null;
            const result = await post("datasets",
                { name, projects: withProjects || [] }, "Creating dataset");
            if (!result) return null;
            state.selection.clear();
            await reload();
            return result.dataset;
        }

        async function renameDataset(id) {
            const dataset = state.datasets.find((d) => d.id === id);
            if (!dataset) return;
            const name = await window.PlexoraConfirm.prompt({
                title: "Rename dataset", value: dataset.name, confirm: "Rename",
            });
            if (!name || name === dataset.name) return;
            if (await post(`datasets/${encodeURIComponent(id)}`, { name }, "Renaming")) {
                await reload();
            }
        }

        async function deleteDataset(id) {
            const dataset = state.datasets.find((d) => d.id === id);
            if (!dataset) return;
            const n = dataset.projectCount || 0;
            const ok = await window.PlexoraConfirm.ask({
                title: `Delete “${dataset.name}”?`,
                // Said explicitly, because a folder metaphor gets this exactly
                // wrong by default: deleting a folder normally deletes what is
                // in it, and this one does not.
                body: n
                    ? `The ${countPhrase(n, "sample")} in it stay where they are and go back to the top level. Nothing is deleted from disk.`
                    : "This dataset is empty.",
                confirm: "Delete dataset",
            });
            if (!ok) return;
            if (await post(`datasets/${encodeURIComponent(id)}/delete`, {}, "Deleting dataset")) {
                if (state.folder === id) setFolder(null);
                else await reload();
            }
        }

        async function moveSelection() {
            const chosen = selectedProjects();
            if (!chosen.length) return;
            const holders = new Set(chosen.map((p) => (p.dataset ? p.dataset.id : "")));
            const answer = await window.PlexoraDatasetPicker.choose({
                datasets: state.datasets,
                count: chosen.length,
                // The folder they are all already in is not a move. Only when
                // they share one -- a mixed selection has no such folder.
                exclude: holders.size === 1 ? Array.from(holders)[0] || undefined : undefined,
                allowRoot: chosen.some((p) => p.dataset),
                allowNew: true,
            });
            if (!answer) return;
            const names = chosen.map((p) => p.name);
            if (answer.kind === "new") return void await createDataset(names, answer.name);
            await assign(names, answer.kind === "dataset" ? answer.id : null);
        }

        async function deleteProjects(names) {
            const deletable = state.projects.filter(
                (p) => names.includes(p.name) && !p.shared);
            if (!deletable.length) return;
            const ok = await window.PlexoraConfirm.ask({
                title: deletable.length === 1
                    ? `Delete “${deletable[0].name}”?`
                    : `Delete ${countPhrase(deletable.length, "sample")}?`,
                body: "This removes the data from disk and cannot be undone.",
                confirm: "Delete",
            });
            if (!ok) return;
            const task = window.PlexoraStatus?.begin("Deleting");
            try {
                // Sequentially: each one is an rmtree, and the server writes
                // config.json for every delete. Firing them all at once buys
                // nothing and makes a partial failure harder to read.
                for (const project of deletable) {
                    const response = await fetch(
                        plexoraUrl(`project/${encodeURIComponent(project.name)}/delete`),
                        { method: "POST" });
                    if (!response.ok) throw new Error(`Could not delete ${project.name}`);
                }
                task?.done();
            } catch (e) {
                task?.fail(e.message);
            }
            state.selection.clear();
            await reload();
        }

        // ------------------------------------------------------------------
        // Events
        // ------------------------------------------------------------------

        resultsEl.addEventListener("click", (event) => {
            const target = event.target;

            const tick = target.closest?.("[data-select-project]");
            if (tick) {
                event.preventDefault();
                return toggle(tick.dataset.selectProject);
            }

            const removeProject = target.closest?.("[data-delete-project]");
            if (removeProject) {
                event.preventDefault();
                return void deleteProjects([removeProject.dataset.deleteProject]);
            }

            const rename = target.closest?.("[data-rename-dataset]");
            if (rename) {
                event.preventDefault();
                return void renameDataset(rename.dataset.renameDataset);
            }

            const removeDataset = target.closest?.("[data-delete-dataset]");
            if (removeDataset) {
                event.preventDefault();
                return void deleteDataset(removeDataset.dataset.deleteDataset);
            }

            const tag = target.closest?.("[data-goto-dataset]");
            if (tag) {
                event.preventDefault();
                searchInput.value = "";
                state.query = "";
                return setFolder(tag.dataset.gotoDataset);
            }

            const folderLink = target.closest?.("[data-open-dataset]");
            if (folderLink) {
                event.preventDefault();
                return setFolder(folderLink.dataset.openDataset);
            }

            // Ctrl/Cmd-click and shift-click select rather than open, which is
            // what they do in every file browser. Checked on the CARD rather
            // than on the link so the modifier works anywhere on it.
            const card = target.closest?.("[data-project-name]");
            if (!card) return;
            const name = card.dataset.projectName;
            if (event.metaKey || event.ctrlKey) {
                event.preventDefault();
                return toggle(name);
            }
            if (event.shiftKey) {
                event.preventDefault();
                return state.anchor ? extendTo(name) : toggle(name);
            }
            // A plain click on the link opens the project, as a full
            // navigation -- deliberately not intercepted (see appRouter.js).
            // A plain click anywhere ELSE on the card clears the selection,
            // which is how a file browser lets you put one down.
            if (!target.closest?.("a")) clearSelection();
        });

        // Space and Enter on a focused tick, so selecting is reachable without
        // a pointer. The tick is the only thing here that is not already a
        // link or a button.
        resultsEl.addEventListener("keydown", (event) => {
            if (event.key !== " " && event.key !== "Enter") return;
            const tick = event.target.closest?.("[data-select-project]");
            if (!tick) return;
            event.preventDefault();
            toggle(tick.dataset.selectProject);
        });

        // -- drag and drop --------------------------------------------------

        resultsEl.addEventListener("dragstart", (event) => {
            const card = event.target.closest?.("[data-project-name]");
            if (!card) return;
            const name = card.dataset.projectName;
            // Dragging a card that is in the selection moves the whole
            // selection; dragging one that is not moves that card alone and
            // leaves the selection alone. The rule every file browser uses.
            const names = state.selection.has(name)
                ? Array.from(state.selection) : [name];
            event.dataTransfer.setData(DRAG_TYPE, JSON.stringify(names));
            event.dataTransfer.effectAllowed = "move";
            card.classList.add("is-dragging");
        });

        resultsEl.addEventListener("dragend", (event) => {
            event.target.closest?.("[data-project-name]")?.classList
                .remove("is-dragging");
        });

        /** Whether this drag is one of ours, and where it would land. */
        function dropTarget(event) {
            if (!Array.from(event.dataTransfer.types).includes(DRAG_TYPE)) return null;
            return event.target.closest?.("[data-drop-dataset], [data-drop-root]") || null;
        }

        function onDragOver(event) {
            const target = dropTarget(event);
            if (!target) return;
            event.preventDefault();
            event.dataTransfer.dropEffect = "move";
            target.classList.add("is-dragover");
        }

        function onDragLeave(event) {
            dropTarget(event)?.classList.remove("is-dragover");
        }

        function onDrop(event) {
            const target = dropTarget(event);
            if (!target) return;
            event.preventDefault();
            target.classList.remove("is-dragover");
            let names;
            try {
                names = JSON.parse(event.dataTransfer.getData(DRAG_TYPE));
            } catch (e) {
                return;
            }
            if (!Array.isArray(names) || !names.length) return;
            // The root crumb is the "take these out" target, which is why
            // dropping there posts null rather than being a no-op.
            void assign(names, target.dataset.dropDataset || null);
        }

        [resultsEl, crumbsEl, folderEmptyEl].forEach((el) => {
            el.addEventListener("dragover", onDragOver);
            el.addEventListener("dragleave", onDragLeave);
            el.addEventListener("drop", onDrop);
        });

        // -- the empty-folder panel is a drop target too ---------------------

        folderEmptyEl.addEventListener("click", (event) => {
            const crumb = event.target.closest?.("[data-open-dataset]");
            if (crumb) setFolder(crumb.dataset.openDataset);
        });

        crumbsEl.addEventListener("click", (event) => {
            const crumb = event.target.closest?.("[data-open-dataset]");
            if (!crumb) return;
            event.preventDefault();
            setFolder(crumb.dataset.openDataset);
        });

        // -- the selection bar ----------------------------------------------

        barEl.addEventListener("click", (event) => {
            const button = event.target.closest?.("[data-action]");
            if (!button || button.disabled) return;
            const names = Array.from(state.selection);
            const action = button.dataset.action;
            if (action === "move") return void moveSelection();
            if (action === "unassign") return void assign(names, null);
            if (action === "delete") return void deleteProjects(names);
            if (action === "clear") return clearSelection();
        });

        // -- toolbar ---------------------------------------------------------

        searchInput.addEventListener("input", () => {
            state.query = searchInput.value;
            state.selection.clear();
            render();
        });
        sortSelect.addEventListener("change", () => {
            state.sort = sortSelect.value;
            render();
        });
        gridButton.addEventListener("click", () => {
            state.view = "grid";
            localStorage.setItem("plexora.openProjectView", "grid");
            updateViewButtons();
            render();
        });
        listButton.addEventListener("click", () => {
            state.view = "list";
            localStorage.setItem("plexora.openProjectView", "list");
            updateViewButtons();
            render();
        });
        createDatasetButton?.addEventListener("click", () => {
            void createDataset(Array.from(state.selection));
        });

        // Escape puts the selection down. On `document` because the focus may
        // be anywhere by then -- and skipped while a dialog is up, since a
        // <dialog> traps focus but not keydown, so Escape dismissing a
        // confirmation would clear the selection it was asking about.
        function onEscape(event) {
            if (event.key !== "Escape") return;
            if (window.PlexoraConfirm?.modalOpen()) return;
            clearSelection();
        }
        document.addEventListener("keydown", onEscape);

        // How something outside this page asks for a re-list. The import
        // dialog is the one caller: a sample imported while this page is open
        // should appear on it, and the dialog has no business knowing how this
        // page loads. Hung on the window rather than passed, because the two
        // are bound at different times -- this page is a routed fragment and
        // the dialog is loaded once in base.html.
        window.PlexoraOpenProject = {refresh: () => { void reload(); }};

        // Where we were, if this is a reload or a pasted link.
        state.folder = new URL(window.location.href).searchParams.get("dataset") || null;
        updateViewButtons();
        void reload();

        // The page is mounted as a fragment by the app shell, so it is torn
        // down and re-registered on every navigation. Everything else here is
        // bound to an element that goes with the fragment; this one is not.
        return () => document.removeEventListener("keydown", onEscape);
    });
})();
