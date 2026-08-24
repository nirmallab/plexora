/**
 * FigureLibrary - the Figures page.
 *
 * Fetches the list once and does search and rendering client-side, exactly as
 * core's Open Project page does, because it is the same interaction on the same
 * furniture. Self-boots when its root element is present and does nothing
 * anywhere else: this plugin's scripts also load on the viewer page and on a
 * figure's own page, and a controller that assumed its DOM would throw on both.
 *
 * A figure whose database could not be read is rendered as a card rather than
 * omitted. That is the whole reason the server reports `readable: false`
 * instead of dropping the row: the damaged figure is precisely the one the user
 * needs to see, and a list that silently skips it presents "damaged" as
 * "deleted".
 */
class FigureLibrary {

    constructor(options) {
        this.api = (options && options.api) || new FigureBuilderApi();
        this.figures = [];
        this.query = "";
        this.pendingDelete = null;

        this.resultsEl = document.getElementById("fb_library_results");
        this.countEl = document.getElementById("fb_library_count");
        this.emptyEl = document.getElementById("fb_library_empty");
        this.noResultsEl = document.getElementById("fb_library_no_results");
        this.searchEl = document.getElementById("fb_library_search");
    }

    static boot() {
        if (!document.getElementById("fb_library_results")) return null;
        const library = new FigureLibrary();
        library.setup();
        return library;
    }

    setup() {
        // Being on this page means the next figure opened was opened from
        // HERE, so the viewer's "you came from a slide" note is out of date and
        // the figure page's back arrow should point at this list again. Cleared
        // rather than left to be ignored: the note is keyed by figure, and
        // reopening the same figure from here would otherwise still find it.
        try {
            window.sessionStorage.removeItem("plexora:figure-builder-origin");
        } catch (error) {
            /* Private-browsing modes throw; the arrow just keeps its default. */
        }

        this.searchEl?.addEventListener("input", () => {
            this.query = this.searchEl.value;
            this.render();
        });

        document.getElementById("fb_library_create")
            ?.addEventListener("click", () => this.createFigure());
        document.getElementById("fb_library_create_empty")
            ?.addEventListener("click", () => this.createFigure());

        // Delegated, because every card is re-rendered on every keystroke in
        // the search box -- handlers bound to the cards themselves would be
        // rebound hundreds of times and leak the ones that were replaced.
        this.resultsEl.addEventListener("click", (event) => this.onCardClick(event));

        const modal = document.getElementById("fb_delete_modal");
        modal?.addEventListener("show.bs.modal", (event) => {
            this.pendingDelete = event.relatedTarget?.dataset.figureId || null;
            const nameEl = document.getElementById("fb_delete_modal_name");
            const figure = this.figures.find((f) => f.figure_id === this.pendingDelete);
            if (nameEl) nameEl.textContent = figure ? figure.title : "this figure";
        });
        document.getElementById("fb_delete_modal_confirm")
            ?.addEventListener("click", () => this.confirmDelete());

        this.refresh();
    }

    async refresh() {
        const result = await this.api.listFigures();
        this.figures = (result.ok && Array.isArray(result.data.figures)) ? result.data.figures : [];
        this.render();
    }

    async createFigure() {
        const task = window.PlexoraStatus?.begin("Creating figure");
        const result = await this.api.createFigure("");
        if (!result.ok) {
            task?.fail("Could not create a figure");
            return;
        }
        task?.done();
        window.location.href = this.api.figureHref(result.data.figure_id);
    }

    onCardClick(event) {
        const action = event.target.closest("[data-fb-action]");
        if (!action) return;
        const figureId = action.dataset.figureId;
        if (action.dataset.fbAction === "duplicate") {
            event.preventDefault();
            this.duplicate(figureId);
        } else if (action.dataset.fbAction === "rename") {
            event.preventDefault();
            this.rename(figureId);
        }
        // "delete" is left alone: the anchor carries bootstrap's data API
        // attributes and opening the dialog is the whole of its job here.
    }

    async duplicate(figureId) {
        const task = window.PlexoraStatus?.begin("Duplicating");
        const result = await this.api.duplicateFigure(figureId, "");
        if (!result.ok) {
            task?.fail("Could not duplicate");
            return;
        }
        task?.done();
        this.refresh();
    }

    async rename(figureId) {
        const figure = this.figures.find((f) => f.figure_id === figureId);
        const title = window.prompt("Rename figure", figure ? figure.title : "");
        if (title === null) return;
        const trimmed = title.trim();
        if (!trimmed) return;

        const task = window.PlexoraStatus?.begin("Renaming");
        // Renaming is an ordinary edit, so it goes through the same revision
        // check every other edit does -- a figure open in another tab must not
        // have its title changed out from under a save it is about to make.
        const current = await this.api.getFigure(figureId);
        if (!current.ok) {
            task?.fail("Could not rename");
            return;
        }
        const result = await this.api.patchFigure(figureId, current.data.document.revision,
            [{ op: "set_meta", changes: { title: trimmed } }]);
        if (!result.ok) {
            task?.fail(result.status === 409 ? "Changed in another session" : "Could not rename");
            return;
        }
        task?.done();
        this.refresh();
    }

    async confirmDelete() {
        const figureId = this.pendingDelete;
        this.pendingDelete = null;
        if (!figureId) return;
        const task = window.PlexoraStatus?.begin("Deleting");
        const result = await this.api.deleteFigure(figureId);
        if (!result.ok) {
            task?.fail("Delete failed");
            return;
        }
        task?.done();
        this.figures = this.figures.filter((f) => f.figure_id !== figureId);
        this.render();
    }

    filtered() {
        const query = this.query.trim().toLowerCase();
        if (!query) return this.figures.slice();
        return this.figures.filter((figure) =>
            figure.title.toLowerCase().includes(query)
            || (figure.sources || []).some((name) => String(name).toLowerCase().includes(query)));
    }

    render() {
        const total = this.figures.length;
        if (this.countEl) {
            this.countEl.textContent = FigureSchema.countPhrase(total, "figure");
        }
        if (total === 0) {
            this.resultsEl.innerHTML = "";
            if (this.emptyEl) this.emptyEl.hidden = false;
            if (this.noResultsEl) this.noResultsEl.hidden = true;
            return;
        }
        if (this.emptyEl) this.emptyEl.hidden = true;

        const list = this.filtered();
        if (!list.length) {
            this.resultsEl.innerHTML = "";
            if (this.noResultsEl) this.noResultsEl.hidden = false;
            return;
        }
        if (this.noResultsEl) this.noResultsEl.hidden = true;
        this.resultsEl.innerHTML = list.map((figure) => this.cardMarkup(figure)).join("");
    }

    cardMarkup(figure) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const title = escape(figure.title);
        if (!figure.readable) {
            return `<div class="project-card fb-card fb-card-damaged">
                <span class="project-thumb fb-thumb project-thumb-fallback">
                    <span class="fas fa-triangle-exclamation project-thumb-icon"></span>
                </span>
                <span class="project-card-name">${escape(figure.figure_id)}</span>
                <span class="project-card-date">This figure could not be read</span>
            </div>`;
        }

        const href = this.api.figureHref(figure.figure_id);
        const summary = [
            FigureSchema.countPhrase(figure.panel_count, "panel"),
            FigureSchema.countPhrase(figure.page_count, "page"),
        ].join(" · ");
        const when = FigureSchema.timeAgo(figure.updated_at);
        const sources = (figure.sources || []).slice(0, 3).map(escape).join(", ");

        return `<div class="project-card fb-card">
            <a class="project-card-link" href="${href}" title="${title}">
                <span class="project-thumb fb-thumb">
                    ${figure.has_thumbnail
                        ? `<img src="${this.api.thumbnailUrl(figure.figure_id, figure.revision)}" alt="" loading="lazy">`
                        : `<span class="fas fa-image project-thumb-icon"></span>`}
                </span>
                <span class="project-card-name">${title}</span>
                <span class="project-card-date">${escape(summary)}${when ? " · " + escape(when) : ""}</span>
                ${sources ? `<span class="fb-card-sources">${sources}</span>` : ""}
            </a>
            <span class="project-actions">
                <a href="#" class="project-action" title="Rename"
                   data-fb-action="rename" data-figure-id="${escape(figure.figure_id)}">
                    <span class="fas fa-pencil"></span></a>
                <a href="#" class="project-action" title="Duplicate"
                   data-fb-action="duplicate" data-figure-id="${escape(figure.figure_id)}">
                    <span class="fas fa-copy"></span></a>
                <a href="#" class="project-action project-action-danger" title="Delete"
                   data-fb-action="delete" data-bs-toggle="modal" data-bs-target="#fb_delete_modal"
                   data-figure-id="${escape(figure.figure_id)}">
                    <span class="fas fa-trash"></span></a>
            </span>
        </div>`;
    }
}

if (typeof document !== "undefined" && document.addEventListener) {
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => FigureLibrary.boot());
    } else {
        FigureLibrary.boot();
    }
}
