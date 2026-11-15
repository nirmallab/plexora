/**
 * FigureCaptureBinGrid - the whole captures bin, browsable.
 *
 * The dock's strip shows the captures from the image currently on screen,
 * because that is the only place their outlines mean anything. This shows ALL
 * of them, across every image, which is the view that makes the bin a place
 * rather than a side effect: come back on Thursday, look at what you collected
 * on Tuesday, send four of them to a figure and throw the rest away.
 *
 * One component, three uses:
 *
 *   - a dialog on the figure canvas ("Captures" in the panel tray), where
 *     `onAdd` puts the selection into the figure that is already open;
 *   - the same dialog as BOOKMARKS ("Bookmarks" in the panel tray,
 *     `bookmarks: true`): only the views saved in the viewer (M), each with
 *     its own two actions -- Go to view (`onGo`) and Add to figure
 *     (`onAddOne`, straight onto the page) -- and nothing to tick or delete;
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
     * @param {object} options api, onAdd(ids), addLabel, emptyAction;
     *        bookmarks, onGo(capture), onAddOne(capture) for the bookmarks.
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
        //: The Bookmarks dialog: bookmarks only, each with its own actions.
        this.bookmarks = Boolean(options && options.bookmarks);
        this.onGo = (options && options.onGo) || null;
        this.onAddOne = (options && options.onAddOne) || null;
        //: Bookmarks added to the figure from this dialog, marked as such.
        this.added = new Set();
        //: Only the bookmarks (core's bookmarks tray): `?bookmarked=1`, or the chip.
        this.onlyBookmarked = this.bookmarks || Boolean(options && options.onlyBookmarked);
        //: Captures without the bookmarks, where the bookmarks have a place of
        //: their own beside it (the figure's Assets tab and its dialogs). No
        //: Bookmarked chip then: the same list in two places is one too many.
        this.excludeBookmarked = !this.bookmarks && Boolean(options && options.excludeBookmarked);
        if (this.excludeBookmarked) this.onlyBookmarked = false;
        //: false where the page has one search for this grid and more (the
        //: Assets tab), which sets it through `setQuery`.
        this.ownSearch = !(options && options.search === false);
        //: A heading over the grid, when it shares a column with other lists.
        this.heading = (options && options.heading) || "";
        //: Nothing said when there is nothing to list (the Assets tab, where
        //: the card under the grid already says how to get some).
        this.quietWhenEmpty = Boolean(options && options.quietWhenEmpty);
        //: What the search reads of a capture; the page can add to it (the
        //: Assets tab adds the project's and dataset's names).
        this.searchText = (options && options.searchText) || FigureSchema.captureSearchText;
        //: A filter on top of the search (the Assets tab's), or null.
        this.predicate = null;
        //: Told each time the list is (re)read, so options built from it
        //: (the Assets filter's) keep up.
        this.onLoaded = (options && options.onLoaded) || null;

        //: The Assets tab's own images: this figure's, of the tab's kind,
        //: shown first and as the same cards, tagged "Used" (or "On
        //: page", for a search's match on the page). Given already searched
        //: and filtered by the page (`setExtras`), with the total before that.
        //: `{id, name, meta, url, placed}`.
        this.extras = [];
        this.extrasTotal = 0;
        //: false: no bin behind the grid, only the extras (Uploads).
        this.collection = !(options && options.collection === false);
        //: What the count calls one item.
        this.noun = (options && options.noun) || (this.bookmarks ? "bookmark" : "capture");
        //: What happens to the figure's own images: put on the page, deleted
        //: from the figure, or (one on the page) selected there.
        this.onAddPanels = (options && options.onAddPanels) || null;
        this.onRemovePanels = (options && options.onRemovePanels) || null;
        this.onGoto = (options && options.onGoto) || null;
        //: Cards can be dragged onto a page (the Assets tab).
        this.draggable = Boolean(options && options.draggable);
        //: "Previously used": off, only what has not gone into any figure;
        //: on, only what has -- with this figure's own images. Keeps the
        //: list to what is still waiting to be used.
        //: false where everything listed is in the figure already (Uploads):
        //: no switch, all of it shown.
        this.usedToggle = !(options && options.usedToggle === false);
        this.showUsed = !this.usedToggle;
        //: Uploads: every one is the figure's own, so "used" means ON A PAGE.
        //: Off, the uploads off the page; "Previously used", those on one.
        this.byPlacement = Boolean(options && options.byPlacement);
        //: {off, on}: the uploads off and on the page, before any search.
        this.extrasSplit = { off: 0, on: 0 };
        //: Cards are ticked to act on several at once: always for captures,
        //: for bookmarks where asked (the Assets tab; the dialog keeps its
        //: two buttons per card).
        this.selectable = !this.bookmarks || Boolean(options && options.selectable);
        //: The last card clicked, where a Shift-click's range starts.
        this.anchor = null;
        //: A capture to bring into view once listed: `#cap_...`, where the
        //: tray's "Show in Figure Builder" points.
        this.locate = (options && options.locate) || "";
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
        let onlyBookmarked = false;
        let locate = "";
        try {
            onlyBookmarked = new URLSearchParams(window.location.search).get("bookmarked") === "1";
            locate = decodeURIComponent(String(window.location.hash || "").slice(1));
        } catch (error) {
            /* A page without a location: neither. */
        }
        const grid = new FigureCaptureBinGrid(root, {
            api: api,
            onlyBookmarked: onlyBookmarked,
            locate: locate,
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

    /** A Font Awesome name drawn as the design's line icon (FigureIcons),
     *  14px as in every text button, or the Font Awesome glyph where the
     *  icon set is not on the page. */
    static icon(name, size = 14) {
        return typeof FigureIcons !== "undefined" ? FigureIcons.fa(name, size)
            : `<span class="fas fa-${name}" aria-hidden="true"></span>`;
    }

    setup() {
        if (!this.host) return;
        if (this.bookmarks) {
            this.setupBookmarks();
            return;
        }
        this.host.innerHTML = `
            ${this.headingHtml()}
            <div class="fb-bin-bar">
                ${this.ownSearch ? `<div class="fb-bin-search">
                    ${FigureCaptureBinGrid.icon("magnifying-glass")}
                    <input type="search" data-role="search" autocomplete="off"
                           spellcheck="false" placeholder="Search by name, image or channel"
                           aria-label="Search captures by name, image or channel">
                </div>` : ""}
                ${this.excludeBookmarked ? "" : `<button class="fb-bin-chip" type="button"
                        data-role="bookmarked" aria-pressed="false"
                        title="Show only the views bookmarked in the viewer (M)">
                    ${FigureCaptureBinGrid.icon("bookmark")}
                    <span>Bookmarked</span>
                </button>`}
                <span class="fb-bin-count" data-role="count"></span>
                <button class="fb-bin-link" type="button" data-role="all" title="Select all">All</button>
                <button class="fb-bin-link" type="button" data-role="none">None</button>
                ${this.usedChipHtml()}
            </div>
            <div class="fb-bin-grid" data-role="grid"></div>
            <p class="fb-bin-state" data-role="state"></p>
            <div class="fb-bin-foot">
                <button class="fb-bin-delete" type="button" data-role="delete" disabled>
                    ${FigureCaptureBinGrid.icon("trash")}
                    <span data-role="deleteLabel">Delete</span>
                </button>
                <button class="fb-bin-add fb-button-primary" type="button"
                        data-role="add" disabled></button>
            </div>`;

        // Delegated: every card is rebuilt on every keystroke in the search
        // box, so handlers bound to the cards would be rebound continuously and
        // leak the ones that were replaced.
        this.bindHost();
        const search = this.el("search");
        search?.addEventListener("input", () => {
            this.query = search.value || "";
            this.render();
        });
        this.refresh();
    }

    /** The Bookmarks dialog's body: a search and the cards, no foot. */
    setupBookmarks() {
        this.host.innerHTML = `
            ${this.headingHtml()}
            <div class="fb-bin-bar">
                ${this.ownSearch ? `<div class="fb-bin-search">
                    ${FigureCaptureBinGrid.icon("magnifying-glass")}
                    <input type="search" data-role="search" autocomplete="off"
                           spellcheck="false" placeholder="Search by name, image or channel"
                           aria-label="Search bookmarks by name, image or channel">
                </div>` : ""}
                <span class="fb-bin-count" data-role="count"></span>
                ${this.selectable ? `
                    <button class="fb-bin-link" type="button" data-role="all" title="Select all">All</button>
                    <button class="fb-bin-link" type="button" data-role="none">None</button>
                    ${this.usedChipHtml()}` : ""}
            </div>
            <div class="fb-bin-grid fb-bookmark-grid" data-role="grid"></div>
            <p class="fb-bin-state" data-role="state"></p>
            ${this.selectable ? `<div class="fb-bin-foot">
                <button class="fb-bin-delete" type="button" data-role="delete" disabled>
                    ${FigureCaptureBinGrid.icon("trash")}
                    <span data-role="deleteLabel">Delete</span>
                </button>
                <button class="fb-bin-add fb-button-primary" type="button"
                        data-role="add" disabled></button>
            </div>` : ""}`;
        this.bindHost();
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

    /** "Previously used", beside Select all and None: a switch between what
     *  has not gone into a figure yet and what has. */
    usedChipHtml() {
        if (!this.usedToggle) return "";
        return `<button class="fb-bin-chip fb-bin-used${this.showUsed ? " is-active" : ""}" type="button"
                        data-role="used" aria-pressed="${this.showUsed}"
                        title="${this.byPlacement
                            ? "Show the uploads on a page, instead of those off the page"
                            : "Show the images already used in a figure, instead of those not used yet"}">
                    Previously used</button>`;
    }

    headingHtml() {
        return this.heading ? `<div class="fb-tray-section fb-bin-heading">
            <span>${FigureSchema.escapeHtml(this.heading)}</span></div>` : "";
    }

    /** The search, set from outside when the page has one search box for
     *  this grid and more (see `search: false`). */
    setQuery(query) {
        this.query = query || "";
        if (this.host && this.host.childElementCount) this.render();
    }

    /** A filter applied with the search: `fn(capture)` keeps it, or null. */
    setPredicate(fn) {
        this.predicate = fn || null;
        if (this.host && this.host.childElementCount) this.render();
    }

    /** What the list is drawn from: the bin, without the bookmarks where
     *  they have their own place. */
    listed() {
        const kind = this.bookmarks ? this.captures.filter((capture) => capture.bookmarked)
            : this.excludeBookmarked ? this.captures.filter((capture) => !capture.bookmarked)
                : this.captures;
        return kind.filter((capture) => FigureCaptureBinGrid.isUsed(capture) === this.showUsed);
    }

    /** Is there anything at all under this tab -- used or not, searched or
     *  not? (The Assets tab shrinks its "how to get some" card once there
     *  is.) */
    hasAny() {
        const kind = this.bookmarks ? this.captures.filter((capture) => capture.bookmarked)
            : this.excludeBookmarked ? this.captures.filter((capture) => !capture.bookmarked)
                : this.captures;
        return kind.length + this.extrasTotal > 0;
    }

    /** Has this capture gone into a figure? */
    static isUsed(capture) {
        return Array.isArray(capture && capture.used_in) && capture.used_in.length > 0;
    }

    /** The figure's own images, shown under "Previously used" only: being in
     *  the figure, they have been used. */
    shownExtras() {
        if (this.byPlacement) {
            return this.extras.filter((item) => Boolean(item.placed) === this.showUsed);
        }
        return this.showUsed ? this.extras : [];
    }

    shownTotal() {
        if (this.byPlacement) return this.showUsed ? this.extrasSplit.on : this.extrasSplit.off;
        return this.showUsed ? this.extrasTotal : 0;
    }

    /** The figure's own images for this grid (see `extras`). A ticked one
     *  that is no longer listed is let go. `total` is a count, or for
     *  Uploads `{off, on}`. */
    setExtras(items, total) {
        this.extras = items || [];
        if (total && typeof total === "object") {
            this.extrasSplit = { off: total.off || 0, on: total.on || 0 };
            total = this.extrasSplit.off + this.extrasSplit.on;
        }
        this.extrasTotal = Number.isFinite(total) ? total : this.extras.length;
        const live = new Set(this.extras.filter((item) => !item.placed).map((item) => item.id));
        for (const id of Array.from(this.checked)) {
            if (this.isPanelId(id) && !live.has(id)) this.checked.delete(id);
        }
        if (this.host && this.host.childElementCount) this.render();
    }

    /** Panel ids and capture ids never share a prefix. */
    isPanelId(id) {
        return String(id).startsWith("pnl_");
    }

    /** Clicks, double-clicks, keys and drags on the cards: delegated, because
     *  every card is rebuilt on every render. */
    bindHost() {
        this.host.addEventListener("click", (event) => this.clicked(event));
        this.host.addEventListener("pointerdown", (event) => this.startMarquee(event));
        this.host.addEventListener("dblclick", (event) => {
            const card = event.target.closest("[data-drag]");
            if (!card || event.target.closest("button")) return;
            const { panels, captures } = this.payloadFor(card);
            this.addToPage(panels, captures);
        });
        this.host.addEventListener("keydown", (event) => {
            if (event.key !== "Enter" && event.key !== " ") return;
            const card = event.target.closest('[data-role="card"]');
            if (!card || event.target !== card) return;
            event.preventDefault();
            card.click();
        });
        this.host.addEventListener("dragstart", (event) => {
            const card = event.target.closest("[data-drag]");
            if (!card || !event.dataTransfer) return;
            const { panels, captures } = this.payloadFor(card);
            if (panels.length) event.dataTransfer.setData("text/x-plexora-panel", JSON.stringify(panels));
            if (captures.length) {
                event.dataTransfer.setData("text/x-plexora-capture", JSON.stringify(captures));
            }
            event.dataTransfer.effectAllowed = "copyMove";
        });
    }

    /**
     * What a drag or double-click on `card` carries: the whole selection when
     * the card is part of it -- picking four and dragging one of them plainly
     * means all four -- or the card alone. Split into the figure's own images
     * (`panels`) and captures, which reach the page by different roads.
     */
    payloadFor(card) {
        const id = card.dataset.drag;
        const ids = this.checked.has(id) ? this.allChecked() : [id];
        return { panels: ids.filter((one) => this.isPanelId(one)),
                 captures: ids.filter((one) => !this.isPanelId(one)) };
    }

    /** Every ticked id, in the order the cards are listed. */
    allChecked() {
        return this.checkedPanels().concat(this.orderedChecked());
    }

    /** The ticked images of the figure, in the order listed. */
    checkedPanels() {
        return this.shownExtras().filter((item) => !item.placed && this.checked.has(item.id))
            .map((item) => item.id);
    }

    /** Put images on the page: the figure's own and captures alike. */
    async addToPage(panels, captures) {
        if (panels.length && this.onAddPanels) await this.onAddPanels(panels);
        if (captures.length && this.onAdd) await this.onAdd(captures);
        [...panels, ...captures].forEach((id) => this.checked.delete(id));
        this.render();
    }

    async refresh() {
        if (!this.collection) {
            this.loading = false;
            this.captures = [];
            this.render();
            this.onLoaded?.(this);
            return;
        }
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
            if (!this.isPanelId(id) && !live.has(id)) this.checked.delete(id);
        }
        this.render();
        this.onLoaded?.(this);
    }

    clicked(event) {
        const target = event.target.closest?.("[data-role]");
        const role = target?.dataset.role;
        if (role === "go" || role === "addOne") {
            const capture = this.captures.find((row) => row.capture_id === target.dataset.captureId);
            if (capture) this.act(role, capture, target);
            return;
        }
        if (role === "addPanel") {
            this.addToPage([target.dataset.panelId], []);
            return;
        }
        if (role === "goto") {
            this.onGoto?.(target.dataset.panelId);
            return;
        }
        if (role === "card") {
            const id = target.dataset.drag || target.dataset.captureId;
            if (!id) return;
            if (this.marqueeJustEnded) return;
            // Shift: everything from the last card clicked to this one.
            if (event.shiftKey && this.anchor) this.selectRange(this.anchor, id);
            else this.toggle(id);
            this.anchor = id;
        } else if (role === "used") {
            this.showUsed = !this.showUsed;
            this.checked.clear();
            this.render();
            this.onLoaded?.(this);
        } else if (role === "bookmarked") {
            this.onlyBookmarked = !this.onlyBookmarked;
            this.render();
        } else if (role === "all") {
            this.shownExtras().filter((item) => !item.placed).forEach((item) => this.checked.add(item.id));
            this.filtered().forEach((capture) => this.checked.add(capture.capture_id));
            this.render();
        } else if (role === "none") {
            this.checked.clear();
            this.render();
        } else if (role === "delete") {
            this.removeChecked();
        } else if (role === "add") {
            const panels = this.checkedPanels();
            const captures = this.orderedChecked();
            if (!this.onAddPanels && captures.length && this.onAdd) {
                this.onAdd(captures);
                return;
            }
            this.addToPage(panels, captures);
        }
    }

    /** A bookmark's own action: go to it in the viewer, or put it on the
     *  figure's page and say so on its card. */
    async act(role, capture, button) {
        if (role === "go") {
            this.onGo?.(capture);
            return;
        }
        if (!this.onAddOne) return;
        button.disabled = true;
        const ok = await this.onAddOne(capture);
        if (ok) this.added.add(capture.capture_id);
        this.render();
    }

    toggle(id) {
        if (this.checked.has(id)) this.checked.delete(id);
        else this.checked.add(id);
        this.render();
    }

    /** The selectable cards' ids, in the order they are drawn. */
    visibleIds() {
        return Array.from(this.host?.querySelectorAll('[data-role="card"][data-drag]') || [])
            .map((card) => card.dataset.drag);
    }

    /** Tick every card from `from` to `to`, both included, in drawn order. */
    selectRange(from, to) {
        const ids = this.visibleIds();
        let a = ids.indexOf(from);
        const b = ids.indexOf(to);
        if (b < 0) return;
        if (a < 0) a = b;
        const [low, high] = a < b ? [a, b] : [b, a];
        ids.slice(low, high + 1).forEach((id) => this.checked.add(id));
        this.render();
    }

    /**
     * Drag-to-select: press on the empty space around the cards and drag a
     * box; every card it touches is ticked. With Shift (or Cmd/Ctrl) the box
     * adds to what is ticked, without it replaces it. A press on a card is
     * that card's own (a click, or dragging it onto the page), so the box
     * starts only between them.
     */
    startMarquee(event) {
        if (event.button !== 0 || !this.host) return;
        if (event.target.closest(".fb-bin-card, button, input, a, label")) return;
        const grid = this.el("grid");
        if (!grid || !grid.querySelector('[data-role="card"]')) return;
        const additive = event.shiftKey || event.metaKey || event.ctrlKey;
        const base = additive ? new Set(this.checked) : new Set();
        const scroller = this.host.closest(".fb-assets-pane") || document.scrollingElement;
        const start = { x: event.clientX, y: event.clientY + (scroller?.scrollTop || 0) };
        const box = document.createElement("div");
        box.className = "fb-bin-marquee";
        document.body.appendChild(box);
        let moved = false;
        let last = event;

        const update = () => {
            const scroll = scroller?.scrollTop || 0;
            const nowY = last.clientY + scroll;
            const left = Math.min(start.x, last.clientX);
            const right = Math.max(start.x, last.clientX);
            const top = Math.min(start.y, nowY) - scroll;
            const bottom = Math.max(start.y, nowY) - scroll;
            Object.assign(box.style, { left: `${left}px`, top: `${top}px`,
                                       width: `${right - left}px`, height: `${bottom - top}px` });
            const hit = new Set(base);
            for (const card of grid.querySelectorAll('[data-role="card"][data-drag]')) {
                const r = card.getBoundingClientRect();
                const touches = r.right >= left && r.left <= right && r.bottom >= top && r.top <= bottom;
                if (touches) hit.add(card.dataset.drag);
                const on = hit.has(card.dataset.drag);
                card.classList.toggle("is-checked", on);
                card.setAttribute("aria-pressed", String(on));
            }
            this.marqueeHit = hit;
        };
        const move = (next) => {
            last = next;
            if (!moved && Math.hypot(next.clientX - event.clientX, next.clientY - event.clientY) < 4) return;
            moved = true;
            // Near the top or bottom of the list, it scrolls under the box.
            if (scroller && scroller !== document.scrollingElement) {
                const r = scroller.getBoundingClientRect();
                if (next.clientY < r.top + 24) scroller.scrollTop -= 12;
                else if (next.clientY > r.bottom - 24) scroller.scrollTop += 12;
            }
            update();
        };
        const end = () => {
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", end);
            window.removeEventListener("pointercancel", end);
            box.remove();
            if (!moved) return;
            this.checked = this.marqueeHit || base;
            // The click that ends a drag lands on whatever is under it; it is
            // not a click on that card.
            this.marqueeJustEnded = true;
            setTimeout(() => { this.marqueeJustEnded = false; }, 0);
            this.render();
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", end);
        window.addEventListener("pointercancel", end);
        event.preventDefault();
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
        // The figure's own images are deleted from the figure, by the page
        // that owns it (and asks in its own words); captures from the bin.
        const panels = this.checkedPanels();
        if (panels.length && this.onRemovePanels) {
            // Those deleted leave the extras, and with them the ticks.
            await this.onRemovePanels(panels);
        }
        const ids = this.orderedChecked();
        if (!ids.length) return;
        const marked = this.captures.filter((capture) => ids.includes(capture.capture_id)
            && capture.bookmarked).length;
        const ok = await FigureConfirm.ask({
            title: ids.length === 1 ? "Discard this capture?" : `Discard ${ids.length} captures?`,
            body: "The region and the rendering it recorded are removed from your "
                + "captures bin. Any panel already made from one is untouched."
                + (marked ? ` ${marked === 1 ? "One is a bookmark" : `${marked} are bookmarks`}, `
                    + "and goes from the viewer's bookmarks too." : ""),
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
        this.onLoaded?.(this);
    }

    /** The cards on screen: the list, narrowed by the chip and the search.
     *  The search reads every word against a capture's names, image,
     *  caption and channels (FigureSchema.captureSearchText). */
    filtered() {
        const listed = this.listed();
        let shown = this.onlyBookmarked
            ? listed.filter((capture) => capture.bookmarked) : listed;
        if (this.predicate) shown = shown.filter(this.predicate);
        if (!this.query.trim()) return shown;
        return shown.filter((capture) =>
            FigureSchema.matchesSearch(this.searchText(capture), this.query));
    }

    render() {
        if (!this.host) return;
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const list = this.filtered();

        // The figure's own images (off the page) count with the rest; a
        // search's matches on the page are shown but not counted.
        const own = this.byPlacement ? this.shownExtras().length
            : this.shownExtras().filter((item) => !item.placed).length;
        const count = this.el("count");
        if (count) {
            // "2 of 9 captures" while a search or filter narrows the list.
            const total = this.listed().length + this.shownTotal();
            const phrase = FigureSchema.countPhrase(total, this.noun);
            const shownCount = list.length + own;
            count.textContent = this.loading && !own ? ""
                : shownCount === total ? phrase : `${shownCount} of ${phrase}`;
        }

        const used = this.el("used");
        if (used) {
            used.setAttribute("aria-pressed", String(this.showUsed));
            used.classList.toggle("is-active", this.showUsed);
        }

        const chip = this.el("bookmarked");
        if (chip) {
            chip.setAttribute("aria-pressed", String(this.onlyBookmarked));
            chip.classList.toggle("is-active", this.onlyBookmarked);
        }

        const grid = this.el("grid");
        if (grid) {
            // The bookmark cards with their own two buttons are the dialog's;
            // where bookmarks are ticked (the Assets tab) they are the same
            // cards as captures.
            const own = this.bookmarks && !this.selectable;
            const markup = this.shownExtras().map((item) => (own
                ? this.extraBookmarkCard(item, escape) : this.extraCard(item, escape)))
                .concat(list.map((capture) => (own
                    ? this.bookmarkCard(capture, escape) : this.card(capture, escape))))
                .join("");
            // Kept pictures: the grid is redrawn on every change to the figure,
            // and a dozen previews re-fetched each time is a dozen requests.
            if (typeof FigureCanvas !== "undefined" && FigureCanvas.replaceKeepingImages) {
                FigureCanvas.replaceKeepingImages(grid, markup);
            } else {
                grid.innerHTML = markup;
            }
        }
        this.bringIntoView();

        const state = this.el("state");
        if (state) {
            const narrowed = Boolean(this.query.trim()) || Boolean(this.predicate);
            const anything = this.listed().length + this.shownTotal() > 0;
            const message = this.loading ? (this.quietWhenEmpty ? "" : "Loading…")
                : this.failed ? "Your captures could not be read."
                    : this.shownExtras().length || list.length ? ""
                    : this.quietWhenEmpty ? (anything && narrowed ? `No ${this.noun}s match.` : "")
                    : this.bookmarks && !this.listed().length
                        ? "No bookmarks yet. In the viewer, press M to save a view — "
                          + "it shows here, ready to go back to or to add to a figure."
                    : !this.listed().length
                        ? "Nothing captured yet. Open a project, press C, and drag a frame "
                          + "over anything worth keeping — it lands here."
                        : !list.length && this.onlyBookmarked && !this.query.trim()
                            ? "No bookmarks yet. In the viewer, press M to bookmark a view — "
                              + "it lands here, marked."
                            : !list.length ? (this.bookmarks ? "No bookmarks match." : "No captures match.")
                                : "";
            state.textContent = message;
            state.hidden = !message;
        }

        // Nothing to pick from, nothing to pick with: an empty list shows its
        // message alone, not a row of controls that can do nothing.
        const none = !this.loading && !this.listed().length && !this.shownTotal() && !this.shownExtras().length;
        for (const role of ["all", "none", "count"]) {
            const link = this.el(role);
            if (link) link.style.display = none ? "none" : "";
        }
        const foot = this.host.querySelector(".fb-bin-foot");
        if (foot) foot.style.display = none ? "none" : "";
        // Quiet and empty, the grid takes no room at all.
        this.host.style.display = this.quietWhenEmpty && none && !this.failed ? "none" : "";

        const ticked = this.orderedChecked().length + this.checkedPanels().length;
        const remove = this.el("delete");
        if (remove) remove.disabled = ticked === 0;
        const removeLabel = this.el("deleteLabel");
        if (removeLabel) removeLabel.textContent = ticked ? `Delete (${ticked})` : "Delete";
        const add = this.el("add");
        if (add) {
            add.disabled = ticked === 0 || !(this.onAdd || this.onAddPanels);
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
    /** The capture the page was opened at (`#cap_...`), scrolled to and
     *  outlined once it is listed. Once: a later render leaves the scroll be. */
    bringIntoView() {
        if (!this.locate || this.loading) return;
        const card = this.host?.querySelector(`[data-capture-id="${CSS.escape(this.locate)}"]`);
        if (!card) return;
        this.locate = "";
        card.classList.add("is-located");
        card.scrollIntoView?.({ block: "center" });
    }

    /** A bookmark: its picture, its name, its image and when, and its two
     *  actions. Not a toggle -- each action is its own button. */
    bookmarkCard(capture, escape) {
        const id = escape(capture.capture_id);
        const image = escape(capture.datasource || "Unknown image");
        const name = escape(capture.label || "Saved view");
        const when = FigureSchema.timeAgo(capture.created_at);
        const added = this.added.has(capture.capture_id);
        const thumbnail = capture.url
            ? `<img src="${escape(capture.url)}" alt="" loading="lazy" draggable="false">`
            : `<span class="fas fa-bookmark fb-bin-thumb-icon" aria-hidden="true"></span>`;
        // Tickable where the grid selects (the Assets tab): the card body
        // ticks it, its two buttons still act on it alone.
        const checked = this.selectable && this.checked.has(capture.capture_id);
        const pick = this.selectable
            ? `data-role="card" role="button" tabindex="0" aria-pressed="${checked}"` : "";
        return `<div class="fb-bin-card fb-bookmark-card${added ? " is-added" : ""}${checked ? " is-checked" : ""}"
                     data-capture-id="${id}" data-drag="${id}" ${pick}
                     ${this.draggable ? 'draggable="true"' : ""}>
            <span class="fb-bin-thumb">${thumbnail}</span>
            ${this.selectable ? `<span class="fb-bin-tick" aria-hidden="true">
                <span class="fas fa-check"></span></span>` : ""}
            <span class="fb-bin-name" title="${name}">${name}</span>
            <span class="fb-bin-meta">${escape([capture.datasource, when].filter(Boolean).join(" · "))}</span>
            <span class="fb-bookmark-actions">
                <button class="fb-bookmark-go" type="button" data-role="go" data-capture-id="${id}"
                        title="Open ${image} at this view">
                    <span class="fas fa-arrow-up-right-from-square" aria-hidden="true"></span>
                    Go to view
                </button>
                <button class="fb-bookmark-add fb-button-primary" type="button" data-role="addOne"
                        data-capture-id="${id}" title="Put this view on the figure's page">
                    <span class="fas fa-${added ? "check" : "plus"}" aria-hidden="true"></span>
                    ${added ? "Add again" : "Add to figure"}
                </button>
            </span>
        </div>`;
    }

    card(capture, escape) {
        const id = escape(capture.capture_id);
        const image = escape(capture.datasource || "Unknown image");
        // A bookmark (core's bookmarks tray): badged, its label first. Not
        // badged on the Bookmarks tab, where every card is one.
        const badge = capture.bookmarked && !this.bookmarks
            ? `<span class="fb-bin-badge" title="Bookmarked in the viewer">
                   <span class="fas fa-bookmark" aria-hidden="true"></span>
                   <span class="fb-visually-hidden">Bookmarked</span>
               </span>` : "";
        const label = capture.bookmarked && capture.label ? escape(capture.label) + " · " : "";
        const when = FigureSchema.timeAgo(capture.created_at);
        const checked = this.checked.has(capture.capture_id);
        const thumbnail = capture.url
            ? `<img src="${escape(capture.url)}" alt="" loading="lazy" draggable="false">`
            : `<span class="fas fa-image fb-bin-thumb-icon" aria-hidden="true"></span>`;
        // A div acting as the button, so it can also be dragged onto a page.
        return `<div class="fb-bin-card${checked ? " is-checked" : ""}${capture.bookmarked
                            ? " is-bookmarked" : ""}" role="button" tabindex="0"
                        data-role="card" data-capture-id="${id}" data-drag="${id}"
                        ${this.draggable ? 'draggable="true"' : ""}
                        aria-pressed="${checked}"
                        title="${image}${when ? " · " + escape(when) : ""}">
            <span class="fb-bin-thumb">${thumbnail}</span>
            ${badge}
            <span class="fb-bin-tick" aria-hidden="true">
                <span class="fas fa-check"></span>
            </span>
            <span class="fb-bin-name">${label}${image}</span>
            <span class="fb-bin-meta">${escape([capture.caption, when]
                .filter(Boolean).join(" · "))}</span>
        </div>`;
    }

    /** The tag on an image a search found on the page. (Used images have no
     *  tag: "Previously used" is where they are, apart.) */
    static tagHtml(placed) {
        return placed ? '<span class="fb-bin-tag is-placed">On page</span>' : "";
    }

    /**
     * One of this figure's own images, as the same card as a capture: ticked
     * the same way, dragged the same way. One a search found on the page is
     * tagged "On page", and a click selects it there.
     */
    extraCard(item, escape) {
        const id = escape(item.id);
        const thumbnail = item.url
            ? `<img src="${escape(item.url)}" alt="" loading="lazy" draggable="false">`
            : `<span class="fas fa-image fb-bin-thumb-icon" aria-hidden="true"></span>`;
        if (item.placed) {
            return `<div class="fb-bin-card is-placed" role="button" tabindex="0"
                         data-role="goto" data-panel-id="${id}"
                         title="${escape(item.name)} — on the page; click to select it there">
                <span class="fb-bin-thumb">${thumbnail}${FigureCaptureBinGrid.tagHtml(true)}</span>
                <span class="fb-bin-name">${escape(item.name)}</span>
                <span class="fb-bin-meta">${escape(item.meta || "")}</span>
            </div>`;
        }
        const checked = this.checked.has(item.id);
        return `<div class="fb-bin-card${checked ? " is-checked" : ""}" role="button" tabindex="0"
                     data-role="card" data-panel-id="${id}" data-drag="${id}"
                     ${this.draggable ? 'draggable="true"' : ""}
                     aria-pressed="${checked}" title="${escape(item.name)} — ${
                         this.byPlacement ? "off the page" : "used in this figure, off the page"}">
            <span class="fb-bin-thumb">${thumbnail}</span>
            <span class="fb-bin-tick" aria-hidden="true">
                <span class="fas fa-check"></span>
            </span>
            <span class="fb-bin-name">${escape(item.name)}</span>
            <span class="fb-bin-meta">${escape(item.meta || "")}</span>
        </div>`;
    }

    /** One of this figure's images among the bookmarks: the bookmark card's
     *  shape, tagged, with the one action it has -- onto the page. */
    extraBookmarkCard(item, escape) {
        const id = escape(item.id);
        const thumbnail = item.url
            ? `<img src="${escape(item.url)}" alt="" loading="lazy" draggable="false">`
            : `<span class="fas fa-bookmark fb-bin-thumb-icon" aria-hidden="true"></span>`;
        const action = item.placed
            ? `<button class="fb-bookmark-go" type="button" data-role="goto" data-panel-id="${id}"
                       title="Select it on the page">Show on page</button>`
            : `<button class="fb-bookmark-add fb-button-primary" type="button" data-role="addPanel"
                       data-panel-id="${id}" title="Put it back on the page">
                   <span class="fas fa-plus" aria-hidden="true"></span> Add to page</button>`;
        const checked = !item.placed && this.selectable && this.checked.has(item.id);
        const pick = !item.placed && this.selectable
            ? `data-role="card" role="button" tabindex="0" aria-pressed="${checked}"` : "";
        return `<div class="fb-bin-card fb-bookmark-card${checked ? " is-checked" : ""}" data-panel-id="${id}"
                     ${item.placed ? "" : `data-drag="${id}" ${pick} ${this.draggable ? 'draggable="true"' : ""}`}>
            <span class="fb-bin-thumb">${thumbnail}${FigureCaptureBinGrid.tagHtml(item.placed)}</span>
            ${pick ? `<span class="fb-bin-tick" aria-hidden="true"><span class="fas fa-check"></span></span>` : ""}
            <span class="fb-bin-name" title="${escape(item.name)}">${escape(item.name)}</span>
            <span class="fb-bin-meta">${escape(item.meta || "")}</span>
            <span class="fb-bookmark-actions">${action}</span>
        </div>`;
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
