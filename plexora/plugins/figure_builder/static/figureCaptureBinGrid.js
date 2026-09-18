/**
 * FigureCaptureBinGrid - the whole captures bin, browsable.
 *
 * The dock's strip shows the captures from the image currently on screen,
 * because that is the only place their outlines mean anything. This shows ALL
 * of them, across every image, which is the view that makes the bin a place
 * rather than a side effect: come back on Thursday, look at what you collected
 * on Tuesday, send four of them to a figure and throw the rest away.
 *
 * One component, two hosts:
 *
 *   - a dialog on the figure canvas ("From captures…" in the panel tray),
 *     where `onAdd` puts the selection into the figure that is already open;
 *   - the Captures tab on the Figures page, where `onAdd` asks which figure
 *     through FigureDestinationPicker and then navigates.
 *
 * Both are the same grid with the same selection and the same delete, because
 * they are the same question asked from two places -- and two implementations
 * of "which captures are ticked" is how one of them ends up deleting something
 * the other still shows.
 *
 * Everything is `fb-`-prefixed and token-driven, so the grid is dark on the
 * library page and light inside the workspace dialog with no second set of
 * rules. Self-boots on the library tab and does nothing anywhere else.
 */
class FigureCaptureBinGrid {

    /**
     * @param {Element} host where the grid is drawn.
     * @param {object} options api, onAdd(ids), addLabel, emptyAction.
     */
    constructor(host, options) {
        this.host = host;
        this.api = (options && options.api) || new FigureBuilderApi();
        this.bin = new FigureCaptureBin({ api: this.api });
        this.onAdd = (options && options.onAdd) || null;
        //: What the primary button says. The canvas dialog adds to the figure
        //: in front of the user; the library page has to ask which one first,
        //: and a button that says the same thing in both places would be
        //: promising the same immediacy for two different trips.
        this.addLabel = (options && options.addLabel) || "Add to figure";

        this.captures = [];
        this.checked = new Set();
        this.query = "";
        this.loading = true;
        this.failed = false;
    }

    /** The library tab, which is the one page this file owns. Returns null
     *  everywhere else, which is what makes loading it on all three pages
     *  safe. */
    static boot() {
        const root = document.getElementById("fb_captures_root");
        if (!root) return null;
        // Being on this page means the next figure opened was opened from HERE,
        // so the viewer's "you came from a slide" note is out of date and the
        // figure page's back arrow should point at a list again. The same clear
        // figureLibrary.js makes, and only in `boot` -- the dialog on the canvas
        // runs the rest of this class with a figure already open, and clearing
        // the note there would break the back arrow of the figure the user is
        // standing in.
        try {
            window.sessionStorage.removeItem("plexora:figure-builder-origin");
        } catch (error) {
            /* Private-browsing modes throw; the arrow keeps its default. */
        }
        const api = new FigureBuilderApi();
        const grid = new FigureCaptureBinGrid(root, {
            api: api,
            addLabel: "Add to figure…",
            // The library has no figure open, so the destination has to be
            // asked for -- the same picker the dock uses, and the same note
            // left for the figure page to act on when it opens.
            onAdd: async (ids) => {
                const listed = await api.listFigures();
                const figures = (listed.ok && listed.data.figures) || [];
                const answer = await FigureDestinationPicker.choose({
                    api: api, figures: figures.filter((figure) => figure.readable),
                    count: ids.length, preferred: FigureCaptureBinGrid.remembered(),
                });
                if (!answer) return;
                let figureId = answer.figureId;
                if (answer.kind === "new") {
                    const created = await api.createFigure("");
                    if (!created.ok) return;
                    figureId = created.data.figure_id;
                }
                FigureCaptureBin.leaveAdoptNote({ figure_id: figureId, capture_ids: ids });
                PlexoraRouter.go(api.figureHref(figureId));
            },
        });
        grid.setup();
        return grid;
    }

    /** Which figure this browser was last working on, for the picker's first
     *  card. Read off the sidebar controller's key rather than spelled again:
     *  two copies of a storage key is how a rename turns into captures landing
     *  in last week's figure. */
    static remembered() {
        try {
            return window.localStorage.getItem(
                FigureBuilderSidebarController.STORAGE_KEY) || null;
        } catch (error) {
            return null;
        }
    }

    setup() {
        if (!this.host) return;
        this.host.innerHTML = `
            <div class="fb-bin-bar">
                <div class="fb-bin-search">
                    <span class="fas fa-magnifying-glass" aria-hidden="true"></span>
                    <input type="search" data-role="search" autocomplete="off"
                           spellcheck="false" placeholder="Search by image"
                           aria-label="Search captures by image">
                </div>
                <span class="fb-bin-count" data-role="count"></span>
                <button class="fb-bin-link" type="button" data-role="all">Select all</button>
                <button class="fb-bin-link" type="button" data-role="none">None</button>
            </div>
            <div class="fb-bin-grid" data-role="grid"></div>
            <p class="fb-bin-state" data-role="state"></p>
            <div class="fb-bin-foot">
                <button class="fb-bin-delete" type="button" data-role="delete" disabled>
                    <span class="fas fa-trash" aria-hidden="true"></span>
                    <span data-role="deleteLabel">Delete</span>
                </button>
                <button class="fb-bin-add fb-button-primary" type="button"
                        data-role="add" disabled></button>
            </div>`;

        // Delegated: every card is rebuilt on every keystroke in the search
        // box, so handlers bound to the cards would be rebound continuously and
        // leak the ones that were replaced.
        this.host.addEventListener("click", (event) => this.clicked(event));
        const search = this.el("search");
        search?.addEventListener("input", () => {
            this.query = search.value || "";
            this.render();
        });
        this.refresh();
    }

    el(role) {
        return this.host?.querySelector(`[data-role="${role}"]`) || null;
    }

    async refresh() {
        this.loading = true;
        this.render();
        const entries = await this.bin.list();
        this.loading = false;
        this.failed = entries === null;
        this.captures = entries || [];
        // A capture that has been adopted or deleted elsewhere must not stay
        // ticked: the next Delete would be acting on something that is gone and
        // the count would be lying about how much.
        const live = new Set(this.captures.map((capture) => capture.capture_id));
        for (const id of Array.from(this.checked)) {
            if (!live.has(id)) this.checked.delete(id);
        }
        this.render();
    }

    clicked(event) {
        const target = event.target.closest?.("[data-role]");
        const role = target?.dataset.role;
        if (role === "card") {
            const id = event.target.closest("[data-capture-id]")?.dataset.captureId;
            if (id) this.toggle(id);
        } else if (role === "all") {
            this.filtered().forEach((capture) => this.checked.add(capture.capture_id));
            this.render();
        } else if (role === "none") {
            this.checked.clear();
            this.render();
        } else if (role === "delete") {
            this.removeChecked();
        } else if (role === "add") {
            const ids = this.orderedChecked();
            if (ids.length && this.onAdd) this.onAdd(ids);
        }
    }

    toggle(id) {
        if (this.checked.has(id)) this.checked.delete(id);
        else this.checked.add(id);
        this.render();
    }

    /** The ticked ids, oldest first -- the order they will become panels in.
     *  A Set remembers insertion order, which is the order they were clicked,
     *  and clicking three captures bottom-up should not reverse the figure. */
    orderedChecked() {
        return this.captures
            .filter((capture) => this.checked.has(capture.capture_id))
            .map((capture) => capture.capture_id)
            .reverse();
    }

    async removeChecked() {
        const ids = this.orderedChecked();
        if (!ids.length) return;
        const ok = await FigureConfirm.ask({
            title: ids.length === 1 ? "Discard this capture?" : `Discard ${ids.length} captures?`,
            body: "The region and the rendering it recorded are removed from your "
                + "captures bin. Any panel already made from one is untouched.",
            confirm: "Discard",
        });
        if (!ok) return;
        if (!await this.bin.remove(ids)) {
            this.failed = true;
            this.render();
            return;
        }
        ids.forEach((id) => this.checked.delete(id));
        this.captures = this.captures.filter((capture) => !ids.includes(capture.capture_id));
        this.render();
    }

    filtered() {
        const query = this.query.trim().toLowerCase();
        if (!query) return this.captures;
        return this.captures.filter((capture) =>
            String(capture.datasource || "").toLowerCase().includes(query)
            || String(capture.caption || "").toLowerCase().includes(query));
    }

    render() {
        if (!this.host) return;
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const list = this.filtered();

        const count = this.el("count");
        if (count) {
            count.textContent = this.loading ? ""
                : FigureSchema.countPhrase(this.captures.length, "capture");
        }

        const grid = this.el("grid");
        if (grid) grid.innerHTML = list.map((capture) => this.card(capture, escape)).join("");

        const state = this.el("state");
        if (state) {
            const message = this.loading ? "Loading…"
                : this.failed ? "Your captures could not be read."
                    : !this.captures.length
                        ? "Nothing captured yet. Open a project, press C, and drag a frame "
                          + "over anything worth keeping — it lands here."
                        : !list.length ? "No captures match." : "";
            state.textContent = message;
            state.hidden = !message;
        }

        const ticked = this.orderedChecked().length;
        const remove = this.el("delete");
        if (remove) remove.disabled = ticked === 0;
        const removeLabel = this.el("deleteLabel");
        if (removeLabel) removeLabel.textContent = ticked ? `Delete (${ticked})` : "Delete";
        const add = this.el("add");
        if (add) {
            add.disabled = ticked === 0 || !this.onAdd;
            add.textContent = ticked
                ? `${this.addLabel} (${ticked})` : this.addLabel;
        }
    }

    /**
     * One capture: the picture, which image it came from, and when.
     *
     * The whole card toggles, and the card IS the button -- a capture in the
     * bin has exactly one thing to decide about it, in or out, so a checkbox as
     * a separate target would be a second hit area for one intention. The tick
     * in the corner is feedback, not a control, which is why `aria-pressed` is
     * on the card rather than on a checkbox nobody can reach.
     */
    card(capture, escape) {
        const id = escape(capture.capture_id);
        const image = escape(capture.datasource || "Unknown image");
        const when = FigureSchema.timeAgo(capture.created_at);
        const checked = this.checked.has(capture.capture_id);
        const thumbnail = capture.url
            ? `<img src="${escape(capture.url)}" alt="" loading="lazy" draggable="false">`
            : `<span class="fas fa-image fb-bin-thumb-icon" aria-hidden="true"></span>`;
        return `<button class="fb-bin-card${checked ? " is-checked" : ""}" type="button"
                        data-role="card" data-capture-id="${id}"
                        aria-pressed="${checked}"
                        title="${image}${when ? " · " + escape(when) : ""}">
            <span class="fb-bin-thumb">${thumbnail}</span>
            <span class="fb-bin-tick" aria-hidden="true">
                <span class="fas fa-check"></span>
            </span>
            <span class="fb-bin-name">${image}</span>
            <span class="fb-bin-meta">${escape([capture.caption, when]
                .filter(Boolean).join(" · "))}</span>
        </button>`;
    }
}

// Through core's page registry rather than DOMContentLoaded, for the reason
// figureLibrary.js gives: the library is one of the pages appRouter.js can
// render without a document load, and that event fires once per document.
// boot() returns null when this is not the captures page. Guarded because this
// file is also parsed outside a browser by the probes.
if (typeof PlexoraPage !== "undefined") {
    PlexoraPage.register(() => FigureCaptureBinGrid.boot());
}
