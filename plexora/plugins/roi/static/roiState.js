/**
 * roiState.js - the annotations the user is editing, and getting them saved.
 *
 * Edits are applied HERE FIRST and sent afterwards. A vertex has to follow the
 * cursor at 60fps; it cannot wait for a round trip, and a design where it does
 * is one where the drawing feels broken whenever the disk is slow. So the
 * client holds the authoritative working copy, the server holds the durable
 * one, and a revision number keeps them honest about which is which.
 *
 * Three consequences that shape everything below:
 *
 * **Every edit is an operation, applied twice.** Once locally by `applyLocal`,
 * once on the server by the same-named handler in operations.py. The two have
 * to agree, which is why the local one is a small explicit interpreter rather
 * than ad-hoc mutation scattered through the UI.
 *
 * **The queue is never dropped.** A failed save keeps its operations and keeps
 * accepting more; the user goes on drawing while the server is unreachable and
 * everything lands when it comes back. Losing an annotation because a laptop
 * slept is not an acceptable outcome, and neither is blocking the pen on it.
 *
 * **A conflict is a question, not an error.** Two tabs on one project both hold
 * a full copy and both autosave. Whoever saves second is told, and gets to
 * choose between the other tab's version and their own -- rather than silently
 * reinstating a stale world over regions somebody just drew.
 */
class RoiStore {

    constructor(ctx, api) {
        this.ctx = ctx;
        this.api = api;

        this.schemaVersion = 1;
        this.revision = 0;
        this.image = "default";
        this.categories = [];
        this.features = [];
        this.coordinateSpace = null;
        this.imageSize = [null, null];
        this.dimensionMismatch = false;
        this.storedImageSize = [null, null];

        this.selectionId = null;
        // Null until the project has a category. There is no built-in one to
        // point at, so "nothing to draw into yet" is a state the tools handle
        // rather than a name invented here.
        this.activeCategoryId = null;

        //: 'saved' | 'saving' | 'dirty' | 'failed' | 'conflict' | 'blocked'
        this.status = "saved";
        this.statusDetail = "";

        this.queue = [];
        this.undoStack = [];
        this.redoStack = [];

        this._flushTimer = null;
        this._retryTimer = null;
        this._retryDelay = 0;
        this._flushing = false;
        this._listeners = new Set();
        this._suspended = false;
    }

    //: How long after the last committed edit the batch goes out. Long enough
    //: that dragging four vertices in a row is one request, short enough that
    //: "Saved" appears while the user is still looking at the shape.
    static get SAVE_DELAY() { return 400; }

    //: Backoff for a save that could not be delivered at all (server down, laptop
    //: asleep, wifi gone). Capped so a session left open overnight reconnects
    //: within half a minute of the server coming back.
    static get RETRY_MIN() { return 2000; }
    static get RETRY_MAX() { return 30000; }

    // -- listeners -------------------------------------------------------

    onChange(fn) {
        this._listeners.add(fn);
        return () => this._listeners.delete(fn);
    }

    changed() {
        for (const fn of this._listeners) {
            try {
                fn(this);
            } catch (error) {
                console.error("ROI: a change listener failed", error);
            }
        }
    }

    // -- loading ---------------------------------------------------------

    /**
     * Take the server's state as the starting point.
     *
     * `_suspended` is up throughout: seeding uses the same setters live editing
     * does, and without it "load from the server" would immediately schedule
     * "save back to the server" -- the restore-then-write-back loop the core
     * sidebar guards against with isRestoring().
     */
    async load() {
        const result = await this.api.getState();
        this._suspended = true;
        try {
            if (!result.ok || !result.data.success) {
                this.setStatus("failed", result.data.error || "could not load annotations");
                return false;
            }
            const data = result.data;
            this.schemaVersion = data.schema_version;
            this.revision = data.revision;
            this.image = data.image || "default";
            this.categories = data.categories || [];
            this.features = data.features || [];
            this.coordinateSpace = data.coordinate_space || null;
            this.imageSize = data.image_size || [null, null];
            this.storedImageSize = data.stored_image_size || [null, null];
            this.dimensionMismatch = Boolean(data.dimension_mismatch);
            this.queue = [];
            this.undoStack = [];
            this.redoStack = [];

            if (!this.categories.some((c) => c.id === this.activeCategoryId)) {
                this.activeCategoryId = (this.categories[0] || {}).id || null;
            }
            // Nothing is drawn and nothing may be edited while the stored
            // annotations belong to a different image than the one on screen.
            // They would render perfectly plausibly in the wrong places, which
            // is the failure mode worth refusing outright.
            this.setStatus(this.dimensionMismatch ? "blocked" : "saved", "");
            return true;
        } finally {
            this._suspended = false;
            this.changed();
        }
    }

    get editable() {
        return !this.dimensionMismatch && this.status !== "conflict";
    }

    // -- reading ---------------------------------------------------------

    category(id) {
        return this.categories.find((c) => c.id === id) || null;
    }

    feature(id) {
        return this.features.find((f) => f.id === id) || null;
    }

    get selected() {
        return this.selectionId ? this.feature(this.selectionId) : null;
    }

    get activeCategory() {
        return this.category(this.activeCategoryId) || this.categories[0] || null;
    }

    /** Categories in the order the panel lists them and the renderer draws them. */
    sortedCategories() {
        return [...this.categories].sort((a, b) => (a.sort_order - b.sort_order)
            || a.label.localeCompare(b.label));
    }

    countFor(categoryId) {
        return this.features.filter((f) => f.category_id === categoryId).length;
    }

    /** Whether this shape's geometry may be changed. A lock on the category
     *  applies to everything in it; a lock on either blocks moving, reshaping
     *  and deleting, but never renaming or reclassifying. */
    isLocked(feature) {
        if (!feature) return true;
        if (feature.locked) return true;
        const category = this.category(feature.category_id);
        return Boolean(category && category.locked);
    }

    isVisible(feature) {
        const category = this.category(feature.category_id);
        return !category || category.visible !== false;
    }

    /** What the renderer draws and what the pointer can hit: the same list, so
     *  a shape can never be invisible and still selectable. */
    visibleFeatures() {
        if (this.dimensionMismatch) return [];
        return this.features.filter((f) => this.isVisible(f));
    }

    // -- editing ---------------------------------------------------------

    select(id) {
        if (this.selectionId === id) return;
        this.selectionId = id;
        this.changed();
    }

    setActiveCategory(id) {
        if (!this.category(id) || this.activeCategoryId === id) return;
        this.activeCategoryId = id;
        this.changed();
    }

    /**
     * Apply an edit locally, record how to undo it, and queue it for the server.
     *
     * `entry` is {label, redo:[ops], undo:[ops]}. The redo ops are what gets
     * sent; the undo ops are what a Ctrl+Z sends INSTEAD -- undo is a new edit
     * at a new revision, never a rewind of the store. Rewinding would mean the
     * revision could go backwards, and the whole conflict check turns on it
     * only ever going forwards.
     */
    commit(entry) {
        if (!this.editable) return false;
        for (const op of entry.redo) this.applyLocal(op);
        this.undoStack.push(entry);
        this.redoStack.length = 0;
        this.enqueue(entry.redo);
        this.changed();
        return true;
    }

    undo() {
        const entry = this.undoStack.pop();
        if (!entry || !this.editable) return false;
        for (const op of entry.undo) this.applyLocal(op);
        this.redoStack.push(entry);
        this.enqueue(entry.undo);
        this.changed();
        return true;
    }

    redo() {
        const entry = this.redoStack.pop();
        if (!entry || !this.editable) return false;
        for (const op of entry.redo) this.applyLocal(op);
        this.undoStack.push(entry);
        this.enqueue(entry.redo);
        this.changed();
        return true;
    }

    /**
     * The client's half of operations.py.
     *
     * Kept deliberately small and literal so the two can be read side by side.
     * The server re-validates everything; this exists so the screen updates now
     * rather than in a round trip's time.
     */
    applyLocal(op) {
        const image = op.image || this.image;
        if (image !== this.image && op.op.startsWith("roi.")) return;

        switch (op.op) {
            case "category.create":
                this.categories.push({ ...op.category });
                break;
            case "category.update": {
                const category = this.category(op.id);
                if (category) Object.assign(category, op.changes);
                break;
            }
            case "category.delete": {
                if (op.orphans === "delete") {
                    this.features = this.features.filter((f) => f.category_id !== op.id);
                } else if (op.reassign_to) {
                    // No default target: the server refuses a reassign without
                    // one, so guessing here would only put the two copies out
                    // of step for as long as the round trip takes.
                    for (const feature of this.features) {
                        if (feature.category_id === op.id) feature.category_id = op.reassign_to;
                    }
                }
                this.categories = this.categories.filter((c) => c.id !== op.id);
                if (this.activeCategoryId === op.id) {
                    this.activeCategoryId = (this.categories[0] || {}).id || null;
                }
                break;
            }
            case "roi.create":
                this.features.push({ ...op.feature });
                break;
            case "roi.update_geometry": {
                const feature = this.feature(op.id);
                if (feature) {
                    feature.geometry = op.geometry;
                    if (op.flags) feature.flags = op.flags;
                }
                break;
            }
            case "roi.update_properties": {
                const feature = this.feature(op.id);
                if (feature) Object.assign(feature, op.changes);
                break;
            }
            case "roi.delete":
                this.features = this.features.filter((f) => f.id !== op.id);
                if (this.selectionId === op.id) this.selectionId = null;
                break;
            case "roi.bulk_delete": {
                const ids = new Set(op.ids || []);
                this.features = this.features.filter((f) => !ids.has(f.id));
                if (ids.has(this.selectionId)) this.selectionId = null;
                break;
            }
            case "roi.bulk_create":
                for (const category of op.categories || []) {
                    if (!this.category(category.id)) this.categories.push({ ...category });
                }
                for (const feature of op.features || []) {
                    if (!this.feature(feature.id)) this.features.push({ ...feature });
                }
                break;
            default:
                console.warn("ROI: unknown operation", op.op);
        }
    }

    // -- saving ----------------------------------------------------------

    enqueue(ops) {
        if (this._suspended) return;
        this.queue.push(...ops);
        this.setStatus("dirty", "");
        this.scheduleFlush();
    }

    scheduleFlush(delay = RoiStore.SAVE_DELAY) {
        if (this._flushTimer) clearTimeout(this._flushTimer);
        this._flushTimer = setTimeout(() => {
            this._flushTimer = null;
            this.flush();
        }, delay);
    }

    /**
     * Send everything queued as one request.
     *
     * One request rather than one per operation, and the whole queue rather
     * than a slice: the server applies a batch atomically, so a batch either
     * lands entirely or changes nothing, and there is never a half-applied edit
     * to reason about.
     */
    async flush() {
        if (this._flushing || !this.queue.length) return;
        if (this.status === "conflict" || this.dimensionMismatch) return;

        this._flushing = true;
        const batch = this.queue.slice();
        const baseRevision = this.revision;
        this.setStatus("saving", "");
        const task = window.PlexoraStatus?.begin("Saving ROIs");

        let result;
        try {
            result = await this.api.postOperations(baseRevision, batch);
        } catch (error) {
            // Never delivered: the operations are still ours and still valid.
            // Keep them, back off, and let the user go on drawing.
            task?.done();
            this._flushing = false;
            this.setStatus("dirty", "not saved yet -- retrying");
            this.scheduleRetry();
            return;
        }

        this._flushing = false;

        if (result.ok && result.data.success) {
            this.queue.splice(0, batch.length);
            this.revision = result.data.revision;
            this._retryDelay = 0;
            task?.done();
            this.setStatus(this.queue.length ? "dirty" : "saved", "");
            if (this.queue.length) this.scheduleFlush(0);
            return;
        }

        if (result.status === 409) {
            // Somebody else wrote first. Freeze the queue -- replaying it over
            // their work is exactly the silent overwrite this check exists to
            // stop -- and ask.
            task?.fail("ROI conflict");
            this.revision = result.data.revision ?? this.revision;
            this.setStatus("conflict", "");
            return;
        }

        if (result.status === 422) {
            task?.fail("ROI image changed");
            this.dimensionMismatch = true;
            this.storedImageSize = result.data.stored_image_size || this.storedImageSize;
            this.setStatus("blocked", result.data.error || "");
            return;
        }

        // A 4xx means the operations themselves are wrong, so retrying sends the
        // same rejection forever. Stop, keep the local geometry, and say so --
        // the export button is the way out and it works from local state.
        task?.fail("ROI save failed");
        this.setStatus("failed", result.data.error || "the server rejected this change");
    }

    scheduleRetry() {
        this._retryDelay = this._retryDelay
            ? Math.min(this._retryDelay * 2, RoiStore.RETRY_MAX)
            : RoiStore.RETRY_MIN;
        if (this._retryTimer) clearTimeout(this._retryTimer);
        this._retryTimer = setTimeout(() => {
            this._retryTimer = null;
            if (this._retryDelay >= RoiStore.RETRY_MAX) {
                this.setStatus("failed", "cannot reach the server -- still retrying");
            }
            this.flush();
        }, this._retryDelay);
    }

    setStatus(status, detail) {
        if (this.status === status && this.statusDetail === detail) return;
        this.status = status;
        this.statusDetail = detail || "";
        this.changed();
    }

    get hasUnsavedWork() {
        return this.queue.length > 0 || this.status === "conflict";
    }

    // -- conflict resolution ---------------------------------------------

    /** Take the other session's version. Local edits that never landed are
     *  discarded, which is why the panel offers the export beside it. */
    async reloadRemote() {
        this.queue = [];
        this._retryDelay = 0;
        this.status = "saved";
        this.selectionId = null;
        return this.load();
    }

    /** Get this session's work out to a file first, then take the remote one.
     *  Built from local state rather than the server's, because the server's is
     *  precisely the version that does not have these edits in it. */
    async keepMineAndExport() {
        this.exportLocal();
        return this.reloadRemote();
    }

    /**
     * The emergency export: the same document the server produces, built here.
     *
     * Works when the server does not: a failed save, a conflict, a changed
     * image. That is the whole point of it -- the moments a user most needs
     * their annotations out of the tab are the moments the normal path is the
     * thing that is broken.
     */
    exportLocal() {
        const document = this.toGeoJSON();
        const blob = new Blob([JSON.stringify(document, null, 2)],
            { type: "application/geo+json" });
        RoiApi.saveBlob(blob, `${this.ctx.datasource}_rois.geojson`);
    }

    toGeoJSON() {
        const categories = Object.fromEntries(this.categories.map((c) => [c.id, c]));
        return {
            type: "FeatureCollection",
            plexora: {
                schema_version: this.schemaVersion,
                datasource: this.ctx.datasource,
                exported_at: new Date().toISOString(),
                coordinate_space: this.coordinateSpace,
                categories: this.categories,
            },
            features: this.features.map((feature) => {
                const category = categories[feature.category_id] || {};
                return {
                    type: "Feature",
                    id: feature.id,
                    geometry: feature.geometry,
                    properties: {
                        name: feature.name || "",
                        category_id: feature.category_id,
                        category: category.label || "",
                        category_color: category.color || "",
                        locked: Boolean(feature.locked),
                        created_at: feature.created_at || null,
                        updated_at: feature.updated_at || null,
                        source_roi_id: feature.source_roi_id || null,
                    },
                };
            }),
        };
    }

    // -- teardown --------------------------------------------------------

    destroy() {
        if (this._flushTimer) clearTimeout(this._flushTimer);
        if (this._retryTimer) clearTimeout(this._retryTimer);
        this._flushTimer = this._retryTimer = null;
        this._listeners.clear();
    }

    /** A short, unique-enough id. Generated on the client so a shape exists the
     *  moment it is drawn instead of after a round trip -- the server rejects a
     *  duplicate rather than trusting it. */
    static newId(prefix) {
        const uuid = (window.crypto && window.crypto.randomUUID)
            ? window.crypto.randomUUID()
            : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
        return `${prefix}-${uuid}`;
    }
}
