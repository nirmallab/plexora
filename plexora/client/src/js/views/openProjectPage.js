/**
 * openProjectPage.js
 *
 * Drives the Open Project page (open_project.html): fetches the project
 * list once, then does search/sort/grid-list rendering entirely client-side.
 * Loaded only on that page (not globally, unlike navbarControls.js).
 */
(function () {
    function onReady(fn) {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", fn);
        } else {
            fn();
        }
    }

    function escapeHtml(value) {
        return String(value).replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        }[c]));
    }

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
        return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    }

    onReady(() => {
        const resultsEl = document.getElementById("project-results");
        if (!resultsEl) return; // Not on the Open Project page.

        const searchInput = document.getElementById("project-search");
        const sortSelect = document.getElementById("project-sort");
        const gridButton = document.getElementById("project-view-grid");
        const listButton = document.getElementById("project-view-list");
        const countEl = document.getElementById("project-count");
        const emptyStateEl = document.getElementById("project-empty-state");
        const noResultsEl = document.getElementById("project-no-results");

        const state = {
            projects: [],
            query: "",
            sort: "lastOpenedAt",
            view: localStorage.getItem("plexora.openProjectView") === "list" ? "list" : "grid",
        };

        function dateCaption(project) {
            if (state.sort === "createdAt") {
                return project.createdAt ? "Created " + timeAgo(project.createdAt) : "";
            }
            return project.lastOpenedAt ? "Opened " + timeAgo(project.lastOpenedAt) : "Never opened";
        }

        function thumbMarkup(project) {
            const thumbUrl = plexoraUrl("project_thumbnail/" + encodeURIComponent(project.name));
            return `<span class="project-thumb">
                <img src="${thumbUrl}" alt="" loading="lazy" onerror="this.parentElement.classList.add('project-thumb-fallback');this.remove();">
                <span class="fas fa-image project-thumb-icon"></span>
            </span>`;
        }

        function actionsMarkup(project) {
            const editUrl = plexoraUrl("edit_config/" + encodeURIComponent(project.name));
            const name = escapeHtml(project.name);
            // Delete carries only the project name -- the request itself is
            // built and sent as a POST when the modal is confirmed. It used to
            // be a plain link to GET /delete/<name>, an irreversible rmtree any
            // crawler, prefetcher or stale bookmark could follow.
            return `<span class="project-actions">
                <a href="${editUrl}" class="project-action" title="Edit"><span class="fas fa-pencil"></span></a>
                <a href="#" class="project-action project-action-danger" title="Delete"
                   data-bs-toggle="modal" data-bs-target="#deleteProjectModal"
                   data-project-name="${name}"><span class="fas fa-trash"></span></a>
            </span>`;
        }

        function cardMarkup(project) {
            const href = plexoraUrl(encodeURIComponent(project.name));
            const name = escapeHtml(project.name);
            const caption = escapeHtml(dateCaption(project));
            return `<div class="project-card">
                <a class="project-card-link" href="${href}" title="${name}">
                    ${thumbMarkup(project)}
                    <span class="project-card-name">${name}</span>
                    <span class="project-card-date">${caption}</span>
                </a>
                ${actionsMarkup(project)}
            </div>`;
        }

        function rowMarkup(project) {
            const href = plexoraUrl(encodeURIComponent(project.name));
            const name = escapeHtml(project.name);
            const caption = escapeHtml(dateCaption(project));
            return `<div class="project-row">
                <a class="project-row-link" href="${href}" title="${name}">
                    ${thumbMarkup(project)}
                    <span class="project-row-name">${name}</span>
                    <span class="project-row-date">${caption}</span>
                </a>
                ${actionsMarkup(project)}
            </div>`;
        }

        function filteredSorted() {
            const query = state.query.trim().toLowerCase();
            let list = query
                ? state.projects.filter((p) => p.name.toLowerCase().includes(query))
                : state.projects.slice();

            if (state.sort === "name") {
                list.sort((a, b) => a.name.localeCompare(b.name));
            } else {
                list.sort((a, b) => {
                    const aTime = a[state.sort] ? new Date(a[state.sort]).getTime() : 0;
                    const bTime = b[state.sort] ? new Date(b[state.sort]).getTime() : 0;
                    return bTime - aTime;
                });
            }
            return list;
        }

        function render() {
            const total = state.projects.length;
            countEl.textContent = total === 1 ? "1 project" : total + " projects";

            if (total === 0) {
                resultsEl.innerHTML = "";
                emptyStateEl.hidden = false;
                noResultsEl.hidden = true;
                return;
            }
            emptyStateEl.hidden = true;

            const list = filteredSorted();
            resultsEl.className = "project-results " + (state.view === "grid" ? "project-grid" : "project-list");

            if (list.length === 0) {
                resultsEl.innerHTML = "";
                noResultsEl.hidden = false;
                return;
            }
            noResultsEl.hidden = true;
            resultsEl.innerHTML = list.map(state.view === "grid" ? cardMarkup : rowMarkup).join("");
        }

        function updateViewButtons() {
            gridButton.classList.toggle("active", state.view === "grid");
            listButton.classList.toggle("active", state.view === "list");
        }

        function setView(view) {
            state.view = view;
            localStorage.setItem("plexora.openProjectView", view);
            updateViewButtons();
            render();
        }

        searchInput.addEventListener("input", () => {
            state.query = searchInput.value;
            render();
        });
        sortSelect.addEventListener("change", () => {
            state.sort = sortSelect.value;
            render();
        });
        gridButton.addEventListener("click", () => setView("grid"));
        listButton.addEventListener("click", () => setView("list"));

        const deleteModalName = document.getElementById("deleteProjectModalName");
        const deleteModalConfirm = document.getElementById("deleteProjectModalConfirm");
        let pendingDelete = null;

        document.getElementById("deleteProjectModal")?.addEventListener("show.bs.modal", (event) => {
            pendingDelete = event.relatedTarget.dataset.projectName;
            deleteModalName.textContent = pendingDelete;
        });

        // The dialog closes on its own -- the button carries data-bs-dismiss, so
        // bootstrap's data API handles it and this never has to reach for a
        // global that the ES-module build does not define. Closing on the click
        // rather than on the response is also what the old code meant to do: it
        // hid in a `finally`, so the outcome never gated it either way. The
        // status chip is where success and failure are reported.
        deleteModalConfirm?.addEventListener("click", async () => {
            if (!pendingDelete) return;
            const name = pendingDelete;
            pendingDelete = null;
            const task = window.PlexoraStatus?.begin("Deleting");
            try {
                const response = await fetch(
                    plexoraUrl(`project/${encodeURIComponent(name)}/delete`),
                    { method: "POST" },
                );
                if (!response.ok) throw new Error("delete failed");
                task?.done();
                state.projects = state.projects.filter((p) => p.name !== name);
                render();
            } catch (e) {
                task?.fail("Delete failed");
            }
        });

        updateViewButtons();

        fetch(plexoraUrl("projects"))
            .then((response) => response.json())
            .then((data) => {
                state.projects = Array.isArray(data) ? data : [];
                render();
            })
            .catch(() => {
                state.projects = [];
                render();
            });
    });
})();
