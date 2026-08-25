/**
 * cellExplorerRoiBridge.js - what is inside that region?
 *
 * An ROI on its own is a shape. The question anyone drawing one is actually
 * asking is what the cells inside it are, and until now answering it meant
 * exporting the region and counting somewhere else. This joins the two plugins
 * that already hold both halves of the answer.
 *
 * ## The split, which this file does not cross
 *
 *   ROI            owns geometry. It says which region the pointer is over and
 *                  where that region is on the screen, through the
 *                  `plexora:roi-hover` / `plexora:roi-unhover` events, and it
 *                  knows nothing about metadata.
 *   Cell Explorer  owns the variable, its categories and their colours. It has
 *                  no idea what an ROI is.
 *   this file      is the only thing that knows both, and it is a LISTENER --
 *                  it never reaches into RoiStore, never edits a shape, and
 *                  never asks the server for anything of its own.
 *
 * Written this way so the ROI plugin stays usable without Cell Explorer
 * installed and vice versa: with no listener the events fall on the floor, and
 * with no ROI plugin loaded nothing ever fires.
 *
 * ## Why hovering is cheap
 *
 * Every cell's centre is fetched ONCE per session, from core's own
 * `NumericData.loadCells()` -- the same request the viewer already makes -- and
 * indexed into a coarse grid. A hover then tests only the cells in the buckets
 * the region's bounding box touches, so the cost follows the size of the region
 * rather than the size of the slide.
 *
 * Two caches, deliberately separate, because they go stale for different
 * reasons: WHICH cells are in a region depends only on geometry, and WHAT they
 * are depends only on the column. Switching cell_type -> phenotype therefore
 * re-tallies against cells already known to be inside, without recomputing
 * membership and without the region being touched at all.
 */
class CellExplorerRoiBridge {

    //: Bars before the tail is folded into `Other`. Nothing is folded away to
    //: reveal a single row -- five plus an `Other` standing for one category
    //: would be a worse card than six real ones, so the fold needs two.
    static TOP_CATEGORIES = 5;

    //: How long the card survives the pointer leaving the region, so it can be
    //: walked into and its bars inspected.
    static GRACE_MS = 180;

    //: Distance from the region's bounding box, and from the edge of the image.
    static ANCHOR_GAP = 14;
    static EDGE_PAD = 8;

    //: Past this share of the track there is no room outside the bar for its
    //: own number, so the number moves inside it.
    static COUNT_INSIDE_PERCENT = 78;

    //: Regions whose membership is worth keeping. A handful, because the point
    //: is re-hovering the same few shapes, not holding a slide's worth.
    static MEMBER_CACHE = 16;

    //: `Other` is not a category and must not look like one, so it takes a
    //: neutral of its own rather than the next palette colour.
    static OTHER_COLOR = "#79839a";

    constructor(ctx, state) {
        this.ctx = ctx;
        this.state = state;
        this.viewer = ctx.viewer?.viewer || null;

        //: The `plexora:roi-hover` detail the card is currently describing.
        this.hover = null;
        this.card = null;
        this.expanded = false;
        this._hideTimer = null;
        //: Bumped on every hover and on close, so a summary whose position
        //: lookup was still in flight cannot paint over a later one.
        this._token = 0;

        this._positions = null;
        this._positionsPromise = null;
        //: roiId -> {geometry, indices}, insertion-ordered as an LRU.
        this._members = new Map();
        //: {source, table} -- cell id -> code for one `state.data` object.
        this._lookup = null;
        //: Columns already reported for a poor id overlap; see warnOnOverlap.
        this._warned = new Set();

        this._onHover = (event) => this.hoverEntered(event.detail);
        this._onUnhover = (event) => this.hoverLeft(event.detail);

        window.addEventListener("plexora:roi-hover", this._onHover);
        window.addEventListener("plexora:roi-unhover", this._onUnhover);
        // Nothing is listened for on the viewer here. An anchor taken in client
        // pixels does go stale the moment the picture moves, but ROI owns where
        // its shapes are and re-announces the hover with a fresh anchor while
        // one is standing -- so the card follows the region through a pan
        // instead of being closed by it. Closing was worse than it sounds: with
        // the pointer sitting still afterwards, no further enter was ever
        // dispatched, and the card could not be brought back without leaving the
        // region and coming back to it.
    }

    destroy() {
        window.removeEventListener("plexora:roi-hover", this._onHover);
        window.removeEventListener("plexora:roi-unhover", this._onUnhover);
        this.close();
        // Portaled, so it outlives this panel's markup and has to be handed back
        // explicitly -- the same reason the legend and the variable picker are
        // torn down by name in the controller's destroy(). Given back to the
        // portal rather than just removed: a portal still holding it would
        // re-attach the orphan on the next fullscreen toggle.
        PopoverPortal.detach(this.card);
        this.card = null;
        this._members.clear();
        this._lookup = null;
        this._positions = null;
        this._positionsPromise = null;
    }

    // -- hover ---------------------------------------------------------------

    /**
     * The pointer has entered a region -- or the picture has moved under one it
     * was already in.
     *
     * Deliberately not `async`. The overwhelmingly common case is a second and
     * later hover, where every input is already in memory, and awaiting an
     * already-resolved promise there would still put the card a frame behind the
     * pointer. Only the first hover of a session has anything to wait for, and
     * it says so on the card rather than appearing to have ignored the pointer.
     */
    hoverEntered(detail) {
        if (!detail || !detail.id) return;
        this.cancelHide();

        // Panning or zooming with the pointer at rest inside a region: ROI
        // re-announces the same shape with a fresh anchor, and all that has to
        // happen is the card following it. Re-summarising would rebuild an
        // identical card once per frame for the length of the gesture.
        if (this.isSameHover(detail) && this.card && !this.card.hidden) {
            this.hover = detail;
            this.place(detail.anchorRect, detail.viewportRect);
            return;
        }

        this.hover = detail;
        this.expanded = false;
        const token = ++this._token;
        if (!this.canSummarise()) {
            this.close();
            return;
        }

        if (this._positions) {
            this.paint(detail, this._positions);
            return;
        }

        // First hover of the session, with every cell's position still on its
        // way. The card opens now, saying what it is doing: a hover that appears
        // to do nothing for a second reads as a hover that was missed, and the
        // pointer moves on before the answer arrives.
        this.renderPending(detail);
        this.place(detail.anchorRect, detail.viewportRect);
        this.positions().then((positions) => {
            // Everything can have changed across that wait: the pointer may have
            // left, moved to another region, or the user may have switched to a
            // continuous column.
            if (token !== this._token || this.hover !== detail || !positions) return;
            if (!this.canSummarise()) {
                this.close();
                return;
            }
            this.paint(detail, positions);
        });
    }

    /** The same shape, unchanged, as the card is already describing. Name as
     *  well as geometry, so a rename still redraws the header. */
    isSameHover(detail) {
        const current = this.hover;
        return Boolean(current && current.id === detail.id
            && current.geometry === detail.geometry
            && current.name === detail.name);
    }

    paint(detail, positions) {
        const summary = this.summarise(detail, positions);
        if (!summary) {
            this.close();
            return;
        }
        this.render(summary);
        this.place(detail.anchorRect, detail.viewportRect);
    }

    /**
     * Fetch the positions before anything is hovering.
     *
     * Called when the user asks for the ROI tool, which is the earliest honest
     * signal that regions are about to be drawn. On a slide with a million cells
     * the request behind this takes long enough to be the difference between a
     * card that appears with the pointer and one that appears after it.
     */
    warm() {
        this.positions();
    }

    hoverLeft(detail) {
        if (!this.hover) return;
        // Ignored when it names a region the card is not describing -- ROI
        // reports the leave before the enter when the pointer crosses straight
        // from one shape into another.
        if (detail?.id && detail.id !== this.hover.id) return;
        this.cancelHide();
        // Not closed outright: the pointer has to be able to travel off the
        // region and onto the card to read a bar. Entering the card cancels
        // this; see ensureCard.
        this._hideTimer = setTimeout(() => this.close(), CellExplorerRoiBridge.GRACE_MS);
    }

    cancelHide() {
        if (this._hideTimer) clearTimeout(this._hideTimer);
        this._hideTimer = null;
    }

    close() {
        this.cancelHide();
        this._token += 1;
        this.hover = null;
        this.expanded = false;
        if (this.card) this.card.hidden = true;
        this.hideTooltip();
    }

    /**
     * The active variable changed under an open card.
     *
     * Only the tally is stale. Membership is a fact about geometry and the
     * geometry has not moved, so this recomputes in place -- which is what lets
     * switching cell_type -> phenotype change the answer without the region
     * being redrawn, reloaded or recreated.
     */
    refreshOpenCard() {
        if (!this.hover || !this.card || this.card.hidden) return;
        if (!this.canSummarise()) {
            this.close();
            return;
        }
        if (!this._positions) return;
        const summary = this.summarise(this.hover, this._positions);
        if (!summary) return;
        this.render(summary);
        // Re-clamped rather than re-anchored: hiding categories takes bars off
        // the card, and one that had been pushed up to fit can sit where it
        // belongs again.
        this.place(this.hover.anchorRect, this.hover.viewportRect);
    }

    /**
     * Categorical only.
     *
     * A continuous variable has no categories to be a composition OF, and a
     * mean or a histogram is a different question nobody asked by hovering.
     * The region stays drawn, hoverable and editable; only the card is
     * withheld, until a categorical column is chosen.
     */
    canSummarise() {
        const column = this.state.column;
        if (!column || this.state.kindFor(column) !== "categorical") return false;
        const data = this.state.data;
        return Boolean(data && data.kind === "categorical" && data.ids && data.ids.length);
    }

    // -- cell positions ------------------------------------------------------

    positions() {
        if (this._positions) return Promise.resolve(this._positions);
        if (!this._positionsPromise) {
            this._positionsPromise = this.loadPositions()
                .then((index) => {
                    this._positions = index;
                    return index;
                })
                .catch((error) => {
                    console.error("Cell Explorer: could not load cell positions "
                        + "for region summaries", error);
                    // Cleared, so a transient failure does not disable the
                    // feature for the rest of the session.
                    this._positionsPromise = null;
                    return null;
                });
        }
        return this._positionsPromise;
    }

    /**
     * Every cell's centre, once per session.
     *
     * Through core's own NumericData, which already memoises this request and
     * already answers it whether the project draws tiled labels or legacy
     * centroids -- the alternative was a second endpoint returning the same
     * three columns to the same browser.
     *
     * These coordinates are RAW full-resolution image pixels, which is exactly
     * the space ROI geometry is stored in, so they are used unscaled. The
     * `* 2 ** extraZoomLevels` in core's legacy centroid cache belongs to the
     * OSD-inflated overlay that cache feeds, and applying it here would put
     * every cell in the wrong place.
     */
    async loadPositions() {
        const source = this.ctx.viewer?.numericData;
        if (!source?.loadCells) return null;
        const { ids, centers } = await source.loadCells();
        if (!ids?.length || !centers) return null;
        return CellExplorerRoiBridge.buildIndex(ids, centers, this.bucketSpan());
    }

    bucketSpan() {
        const width = Number(this.ctx.config?.tileWidth);
        return Number.isFinite(width) && width > 0 ? width : 512;
    }

    /**
     * A coarse grid over the cells, so a hover costs the region's area rather
     * than the slide's cell count.
     *
     * Without it every hover is a point-in-polygon test per cell -- a million
     * of them to describe a region holding four hundred, on every shape the
     * pointer crosses. Buckets are keyed by one number rather than an "x|y"
     * string because building a million strings is the expensive half of
     * indexing a million cells.
     */
    static buildIndex(ids, centers, span) {
        let maxX = 0;
        let maxY = 0;
        for (let i = 0, p = 0; i < ids.length; i += 1, p += 2) {
            if (centers[p] > maxX) maxX = centers[p];
            if (centers[p + 1] > maxY) maxY = centers[p + 1];
        }
        const columns = Math.floor(maxX / span) + 1;
        const buckets = new Map();
        for (let i = 0, p = 0; i < ids.length; i += 1, p += 2) {
            const key = Math.floor(centers[p + 1] / span) * columns
                + Math.floor(centers[p] / span);
            const bucket = buckets.get(key);
            if (bucket) bucket.push(i);
            else buckets.set(key, [i]);
        }
        return { ids, centers, span, columns, buckets };
    }

    // -- membership ----------------------------------------------------------

    /** Indices into the position arrays for the cells inside this shape. */
    static membersIn(geometry, index) {
        if (typeof RoiGeometry === "undefined" || !geometry || !index) return null;
        const box = RoiGeometry.bounds(geometry);
        if (!box) return [];
        const { span, columns, buckets, centers } = index;
        const firstColumn = Math.max(0, Math.floor(box.minX / span));
        const lastColumn = Math.max(0, Math.floor(box.maxX / span));
        const firstRow = Math.max(0, Math.floor(box.minY / span));
        const lastRow = Math.max(0, Math.floor(box.maxY / span));

        const found = [];
        for (let row = firstRow; row <= lastRow; row += 1) {
            for (let column = firstColumn; column <= lastColumn; column += 1) {
                const bucket = buckets.get(row * columns + column);
                if (!bucket) continue;
                for (let b = 0; b < bucket.length; b += 1) {
                    const i = bucket[b];
                    const x = centers[i * 2];
                    const y = centers[i * 2 + 1];
                    // The bounding box first: four comparisons reject most of
                    // what a fringe bucket holds, and a ray cast over every
                    // ring is far more than four comparisons.
                    if (x < box.minX || x > box.maxX || y < box.minY || y > box.maxY) continue;
                    if (RoiGeometry.containsPoint(geometry, x, y)) found.push(i);
                }
            }
        }
        return found;
    }

    members(detail, index) {
        const cached = this._members.get(detail.id);
        // Geometry objects are REPLACED on every edit and never mutated, so
        // object identity alone says whether this is still the same shape --
        // the same trick RoiRenderer.pathFor uses to cache its Path2D objects,
        // and the reason a reshaped region recomputes for free.
        if (cached && cached.geometry === detail.geometry) return cached.indices;

        const indices = CellExplorerRoiBridge.membersIn(detail.geometry, index);
        if (!indices) return null;
        this._members.delete(detail.id);
        this._members.set(detail.id, { geometry: detail.geometry, indices });
        while (this._members.size > CellExplorerRoiBridge.MEMBER_CACHE) {
            this._members.delete(this._members.keys().next().value);
        }
        return indices;
    }

    // -- composition ---------------------------------------------------------

    /**
     * cell id -> category code for the column on screen, as a dense array.
     *
     * Dense rather than a Map because ids run from one to the cell count: four
     * bytes each beats a hash entry each by an order of magnitude at a million
     * cells, which is the same reasoning behind CellExplorerColors' own lookup
     * table. -1 marks an id this column has no row for, which is NOT the same
     * thing as Unassigned -- see tally().
     */
    codeById() {
        const data = this.state.data;
        if (!data || data.kind !== "categorical") return null;
        // Keyed on the payload object, which select() replaces wholesale for
        // every column, so there is nothing to invalidate by hand.
        if (this._lookup?.source === data) return this._lookup;
        const { ids, codes } = data;
        let maxId = 0;
        for (let i = 0; i < ids.length; i += 1) {
            if (ids[i] > maxId) maxId = ids[i];
        }
        const table = new Int32Array(maxId + 1).fill(-1);
        for (let i = 0; i < ids.length; i += 1) table[ids[i]] = codes[i];
        this._lookup = { source: data, table };
        return this._lookup;
    }

    /**
     * Count members by category code.
     *
     * Two kinds of absence, kept apart. An id this column has no row for is not
     * counted at all -- it is not part of this variable's population, and
     * putting it in the total would make every percentage on the card wrong. A
     * code past the end of the dictionary IS a value, the missing one, and is
     * counted as Unassigned. The same distinction CellExplorerColors draws when
     * it decides which cells get the unassigned swatch.
     */
    static tally(indices, cellIds, table, categoryCount) {
        const counts = new Uint32Array(categoryCount);
        let unassigned = 0;
        let total = 0;
        for (let n = 0; n < indices.length; n += 1) {
            const id = cellIds[indices[n]];
            const code = id < table.length ? table[id] : -1;
            if (code < 0) continue;
            total += 1;
            if (code < categoryCount) counts[code] += 1;
            else unassigned += 1;
        }
        return { counts, unassigned, total };
    }

    summarise(detail, index) {
        const lookup = this.codeById();
        const indices = this.members(detail, index);
        if (!lookup || !indices) return null;

        const column = this.state.column;
        const categories = this.state.descriptor(column)?.categories || [];
        const tally = CellExplorerRoiBridge.tally(
            indices, index.ids, lookup.table, categories.length);
        // The id-overlap check is asked of the whole population, before the
        // legend takes anything out of it: hiding four categories is not
        // evidence that the two id spaces disagree.
        this.warnOnOverlap(column, indices.length, tally.total);

        const shown = this.rows(column, categories, tally);
        // Summed from what is on the card rather than taken from the tally, so
        // the header, the bars and their percentages all count one population.
        // A total that included hidden categories would leave every bar shorter
        // than the share of the picture it stands for.
        let total = 0;
        for (const row of shown.rows) total += row.count;

        return {
            name: detail.name || "Region",
            column,
            total,
            hiddenCategories: shown.hidden,
            rows: shown.rows,
        };
    }

    /**
     * One row per VISIBLE category present in the region, carrying the exact
     * colour Cell Explorer is drawing that category with right now.
     *
     * Colours are resolved fresh on every render rather than cached, in the same
     * order the lookup table resolves them -- the user's override, else the
     * palette colour for the category's position -- so recolouring a category
     * from the legend shows up on the next render with nothing to invalidate.
     *
     * Hidden categories are left out entirely, counts and all. The legend's
     * checkboxes are how somebody narrows the question they are asking of the
     * slide: with the stroma hidden, the picture under the pointer is the
     * immune cells, and a card that went on reporting the stroma would be
     * describing something other than what is on screen. The count of what was
     * dropped goes back with the rows so the card can say so rather than
     * quietly shrink.
     *
     * @returns {{rows: Array, hidden: number}}
     */
    rows(column, categories, tally) {
        const overrides = this.state.categorical(column).colors || {};
        const hiddenLabels = this.state.hiddenSet(column);
        const rows = [];
        let hidden = 0;
        for (let i = 0; i < categories.length; i += 1) {
            const count = tally.counts[i];
            if (!count) continue;
            const label = categories[i].value;
            if (hiddenLabels.has(label)) {
                hidden += 1;
                continue;
            }
            rows.push({
                label,
                count,
                color: overrides[label] || CellExplorerColors.defaultCategoryColor(i),
            });
        }
        if (tally.unassigned) {
            const label = CellExplorerColors.UNASSIGNED_LABEL;
            // Unassigned is a row of the legend like any other and can be
            // hidden like any other -- see state.allLabels.
            if (hiddenLabels.has(label)) {
                hidden += 1;
            } else {
                rows.push({
                    label,
                    count: tally.unassigned,
                    color: overrides[label] || CellExplorerColors.UNASSIGNED,
                });
            }
        }
        return { rows, hidden };
    }

    /**
     * Rank by abundance and fold the tail into one honest `Other`.
     *
     * Only categories actually present are ranked: a variable with forty labels
     * of which six occur in this region should show six bars, not thirty-four
     * empty ones. `Other` therefore appears only when the region really does
     * hold more kinds of cell than the card is showing, and it carries the true
     * combined count rather than a remainder inferred from the visible bars.
     */
    static rankCategories(rows, topN = CellExplorerRoiBridge.TOP_CATEGORIES) {
        const ranked = [...rows].sort(
            (a, b) => b.count - a.count || String(a.label).localeCompare(String(b.label)));
        if (ranked.length <= topN + 1) return { rows: ranked, folded: 0 };

        const shown = ranked.slice(0, topN);
        const rest = ranked.slice(topN);
        let count = 0;
        for (const row of rest) count += row.count;
        shown.push({
            label: "Other",
            count,
            color: CellExplorerRoiBridge.OTHER_COLOR,
            other: true,
        });
        return { rows: shown, folded: rest.length };
    }

    /** 987, 1.2k, 14.3k -- a count read at a glance beside a bar, not an exact
     *  figure. The exact one is in the bar's own tooltip. */
    static formatCount(value) {
        const n = Number(value) || 0;
        if (n < 1000) return String(n);
        if (n < 1e6) return `${CellExplorerRoiBridge.trimZero(n / 1000)}k`;
        return `${CellExplorerRoiBridge.trimZero(n / 1e6)}m`;
    }

    static trimZero(value) {
        const text = value.toFixed(1);
        return text.endsWith(".0") ? text.slice(0, -2) : text;
    }

    /** The two id spaces -- core's /get_all_cells and this column's payload --
     *  come from one table and should agree. Said once per column when they do
     *  not, because counting a fraction of the cells looks exactly like a
     *  region that happens to be sparse. */
    warnOnOverlap(column, candidates, matched) {
        if (!candidates || this._warned.has(column)) return;
        if (matched >= candidates * 0.5) return;
        this._warned.add(column);
        console.warn(`Cell Explorer: only ${matched} of ${candidates} cells inside `
            + `this region have a value for "${column}". The cell ids from `
            + "/get_all_cells and from the column payload may not agree.");
    }

    // -- the card ------------------------------------------------------------

    ensureCard() {
        if (this.card) return this.card;
        const card = document.createElement("div");
        card.className = "cex-roi-card";
        card.hidden = true;

        // Entering the card is the other half of the grace period: the pointer
        // has left the region by definition, and closing on that would make the
        // bars impossible to reach.
        card.addEventListener("mouseenter", () => this.cancelHide());
        card.addEventListener("mouseleave", () => this.close());

        const head = document.createElement("div");
        head.className = "cex-roi-card-head";
        const name = document.createElement("span");
        name.className = "cex-roi-card-name";
        const total = document.createElement("span");
        total.className = "cex-roi-card-total";
        head.append(name, total);

        const variable = document.createElement("div");
        variable.className = "cex-roi-card-variable";

        const bars = document.createElement("div");
        bars.className = "cex-roi-card-bars";

        const more = document.createElement("button");
        more.type = "button";
        more.className = "cex-roi-card-more";
        more.hidden = true;
        more.addEventListener("click", () => {
            this.expanded = !this.expanded;
            this.refreshOpenCard();
            // Re-clamped, not re-anchored: the card grew, and the only thing
            // that may have to change is how far down the screen it can start.
            if (this.hover) this.place(this.hover.anchorRect, this.hover.viewportRect);
        });

        const tip = document.createElement("div");
        tip.className = "cex-roi-card-tip";
        tip.hidden = true;
        // Portaled rather than mounted in the viewer's subtree: the anchor
        // arrives in client pixels, and nothing an ancestor does with overflow
        // can then clip a card that sits near the edge of the image. Handed to
        // core's PopoverPortal rather than straight to <body>, because the
        // full-screen button fullscreens #bodyDiv and a body-level card would be
        // painted under the fullscreen backdrop -- see popoverPortal.js.
        PopoverPortal.attach(card);
        this.card = card;
        return card;
    }

    render(summary) {
        const card = this.ensureCard();
        const ranked = this.expanded
            ? CellExplorerRoiBridge.rankCategories(summary.rows, summary.rows.length)
            : CellExplorerRoiBridge.rankCategories(summary.rows);

        card.classList.remove("is-pending");
        card.querySelector(".cex-roi-card-name").textContent = summary.name;
        card.querySelector(".cex-roi-card-total").textContent =
            `${CellExplorerRoiBridge.formatCount(summary.total)} cells`;
        // Said out loud whenever the legend is filtering, because the counts on
        // this card are of the shown categories only. Without it the card is
        // indistinguishable from one describing a region that genuinely holds
        // nothing else, which is a different fact about the slide.
        card.querySelector(".cex-roi-card-variable").textContent = summary.hiddenCategories
            ? `${summary.column || ""} · ${summary.hiddenCategories} hidden`
            : (summary.column || "");

        const bars = card.querySelector(".cex-roi-card-bars");
        bars.textContent = "";
        if (ranked.rows.length) {
            for (const row of ranked.rows) bars.appendChild(this.buildBar(row, summary.total));
        } else {
            const empty = document.createElement("p");
            empty.className = "cex-roi-card-empty";
            empty.textContent = summary.hiddenCategories
                ? "Every category in this region is hidden."
                : "No cells in this region.";
            bars.appendChild(empty);
        }

        const more = card.querySelector(".cex-roi-card-more");
        more.hidden = !(this.expanded || ranked.folded > 0);
        more.textContent = this.expanded ? "Collapse" : "Show all ›";
        card.classList.toggle("is-expanded", this.expanded);
        this.hideTooltip();
        card.hidden = false;
    }

    /**
     * The card before there is anything to put in it.
     *
     * Only ever seen once per session, while every cell's position is being
     * fetched. It exists because the alternative is silence: the pointer enters
     * a region, nothing appears, and the natural reading is that the hover did
     * not register -- so the user moves on, and the card they were waiting for
     * opens over a region they have already left.
     */
    renderPending(detail) {
        const card = this.ensureCard();
        card.classList.add("is-pending");
        card.classList.remove("is-expanded");
        card.querySelector(".cex-roi-card-name").textContent = detail.name || "Region";
        card.querySelector(".cex-roi-card-total").textContent = "";
        card.querySelector(".cex-roi-card-variable").textContent = this.state.column || "";

        const bars = card.querySelector(".cex-roi-card-bars");
        bars.textContent = "";
        const waiting = document.createElement("p");
        waiting.className = "cex-roi-card-empty";
        waiting.textContent = "Counting cells…";
        bars.appendChild(waiting);

        card.querySelector(".cex-roi-card-more").hidden = true;
        this.hideTooltip();
        card.hidden = false;
    }

    buildBar(row, total) {
        const percent = total ? (row.count / total) * 100 : 0;
        const bar = document.createElement("div");
        bar.className = "cex-roi-bar";
        if (row.other) bar.classList.add("is-other");

        const label = document.createElement("span");
        label.className = "cex-roi-bar-label";
        label.textContent = row.label;
        bar.appendChild(label);

        const track = document.createElement("div");
        track.className = "cex-roi-bar-track";

        const fill = document.createElement("div");
        fill.className = "cex-roi-bar-fill";
        // Against a fixed 0-100% track, never normalised to the largest
        // category: a fortieth of the cells has to LOOK like a fortieth, or two
        // regions cannot be compared by eye -- which is what the card is for.
        fill.style.width = `${percent.toFixed(2)}%`;
        fill.style.background = row.color;
        track.appendChild(fill);

        const count = document.createElement("span");
        count.className = "cex-roi-bar-count";
        count.textContent = CellExplorerRoiBridge.formatCount(row.count);
        // Hugging the end of its own bar rather than lining up in a column down
        // the right-hand side: the number belongs to that bar, and a numeric
        // column pulls the eye away from the shape the ranking exists to show.
        // Past the point where it would run off the end it moves inside.
        if (percent >= CellExplorerRoiBridge.COUNT_INSIDE_PERCENT) {
            count.classList.add("is-inside");
            count.style.right = `calc(${(100 - percent).toFixed(2)}% + 6px)`;
        } else {
            count.style.left = `calc(${percent.toFixed(2)}% + 6px)`;
        }
        track.appendChild(count);
        bar.appendChild(track);

        // The exact figures are one hover away rather than printed on every
        // row: six permanent percentages is a table, and the bars stop being
        // readable as a shape.
        const detail = `${row.label} / ${row.count.toLocaleString()} cells · `
            + `${percent.toFixed(1)}%`;
        bar.addEventListener("mouseenter", () => this.showTooltip(bar, detail));
        bar.addEventListener("mouseleave", () => this.hideTooltip());
        return bar;
    }

    showTooltip(bar, text) {
        const tip = this.card?.querySelector(".cex-roi-card-tip");
        if (!tip) return;
        tip.textContent = text;
        tip.hidden = false;
        // Positioned against the card, which is its offset parent, so it
        // travels with the card and needs no client-space arithmetic.
        const above = bar.offsetTop - tip.offsetHeight - 4;
        tip.style.top = `${above >= 0 ? above : bar.offsetTop + bar.offsetHeight + 4}px`;
    }

    hideTooltip() {
        const tip = this.card?.querySelector(".cex-roi-card-tip");
        if (tip) tip.hidden = true;
    }

    /**
     * Put the card beside the region, once.
     *
     * Computed when the region is entered and left alone afterwards. A card
     * that followed the pointer would be unreadable inside a large region, and
     * would slide away from the bar somebody is reaching for. It flips to the
     * other side, or slides up, only as far as it must to stay inside the
     * image -- the bounds come from the ROI plugin along with the anchor, so
     * "inside" means inside the picture rather than inside the window.
     */
    place(anchorRect, viewportRect) {
        const card = this.card;
        if (!card || !anchorRect) return;
        const pad = CellExplorerRoiBridge.EDGE_PAD;
        const gap = CellExplorerRoiBridge.ANCHOR_GAP;
        const bounds = viewportRect || {
            left: 0,
            top: 0,
            right: window.innerWidth,
            bottom: window.innerHeight,
        };
        const box = card.getBoundingClientRect();

        let left = anchorRect.right + gap;
        if (left + box.width > bounds.right - pad) left = anchorRect.left - gap - box.width;
        if (left < bounds.left + pad) {
            // Neither side fits, which is a region wider than the space around
            // it. Sit over it instead, still inside the picture.
            left = Math.min(
                Math.max(anchorRect.left + gap, bounds.left + pad),
                Math.max(bounds.right - box.width - pad, bounds.left + pad));
        }

        let top = anchorRect.top;
        if (top + box.height > bounds.bottom - pad) top = bounds.bottom - pad - box.height;
        if (top < bounds.top + pad) top = bounds.top + pad;

        card.style.left = `${Math.round(left)}px`;
        card.style.top = `${Math.round(top)}px`;
    }
}
