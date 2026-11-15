/**
 * FigureDocumentState - the open figure, and everything about saving it.
 *
 * One object owns the document, the revision, the save queue and the undo
 * stack, because all four are the same question asked from different angles and
 * splitting them is how a client ends up saving a revision it no longer holds.
 *
 * Three decisions worth stating:
 *
 * **The server is the only interpreter of operations.** This side edits its own
 * copy directly and sends the equivalent operation list; it does not reimplement
 * `operations.py`. Two implementations of "what does add_panel mean" is two
 * implementations that can disagree, and the disagreement would be invisible
 * until a figure was reopened. If the server rejects a batch, the local copy is
 * rolled back to the last confirmed one -- so the two can never drift, because
 * only agreement survives.
 *
 * **Saves are chained, never concurrent**, and each reads the revision at the
 * moment it is SENT rather than the moment the edit was made. Those two moments
 * are not the same one: a commit made while an earlier save is still in flight
 * would otherwise carry a revision the server has already moved past, and be
 * answered with a 409 that no other session had anything to do with. Reading it
 * late is safe precisely because the chain means nothing else can have written
 * in between. Chaining also makes the outcome deterministic: without it, a slow
 * earlier save can land after a newer one and quietly reinstate what the newer
 * one changed.
 *
 * **Undo is a snapshot, not an inverse.** A figure document is a few hundred
 * kilobytes at worst -- it holds references and geometry, never pixels -- so
 * keeping N of them costs nothing next to writing and maintaining an inverse
 * for every operation, and an inverse that is subtly wrong corrupts a figure
 * silently. Undo is sent as a whole-document replace, which is a new revision:
 * revisions only ever go forwards, and the conflict check depends on that.
 */
class FigureDocumentState {

    /** How many undo steps are kept. Past this, the oldest is dropped. */
    static get HISTORY_LIMIT() { return 50; }

    /** How long a pause still counts as the same sitting at one field. */
    static get MERGE_MS() { return 4000; }

    constructor(options) {
        this.api = options.api;
        this.figureId = options.figureId;

        this.document = null;
        this.sourceStatus = {};
        /** "loading" | "saved" | "saving" | "unsaved" | "failed" | "conflict" | "unreadable" */
        this.status = "loading";
        this.statusDetail = "";

        this._listeners = { change: [], status: [] };
        this._chain = Promise.resolve();
        /** Saves the server has not confirmed yet, oldest first. A save whose
         *  request never arrived stays here and goes out again, ahead of the
         *  next one -- that next one was built on it. */
        this._unsent = [];
        this._undo = [];
        this._redo = [];

        ["change", "status"].forEach((name) => {
            if (typeof options["on" + name.charAt(0).toUpperCase() + name.slice(1)] === "function") {
                this._listeners[name].push(options["on" + name.charAt(0).toUpperCase() + name.slice(1)]);
            }
        });
    }

    on(event, handler) {
        if (this._listeners[event]) this._listeners[event].push(handler);
        return this;
    }

    _emit(event, payload) {
        (this._listeners[event] || []).forEach((handler) => {
            try {
                handler(payload, this);
            } catch (error) {
                // One broken listener must not stop the others, and must not
                // abort a save that has already happened.
                console.error("figure_builder: listener failed", error);
            }
        });
    }

    _setStatus(status, detail) {
        this.status = status;
        this.statusDetail = detail || "";
        this._emit("status", { status: status, detail: this.statusDetail });
    }

    get revision() {
        return this.document ? this.document.revision : 0;
    }

    get canUndo() { return this._undo.length > 0; }
    get canRedo() { return this._redo.length > 0; }

    async load() {
        const result = await this.api.getFigure(this.figureId);
        if (result.status === 422) {
            this._setStatus("unreadable", result.data.detail || result.data.error || "");
            return false;
        }
        if (!result.ok) {
            this._setStatus("failed", result.status === 404
                ? "This figure no longer exists."
                : "Could not open this figure.");
            return false;
        }
        this.document = result.data.document;
        this.sourceStatus = result.data.source_status || {};
        this._unsent = [];
        this._undo = [];
        this._redo = [];
        this._setStatus("saved");
        this._emit("change", this.document);
        return true;
    }

    /**
     * Change the figure and save the change.
     *
     * `mutate(draft)` applies the edit to a copy of the document for immediate
     * display; `operations` is the same edit expressed for the server. They
     * describe one action, which is why they are passed together -- and one
     * call here is one undo step, whatever it contains, because that is what
     * makes a five-panel split undo as a split rather than as five deletions.
     *
     * Returns a promise for `true` if it was stored.
     */
    commit(operations, mutate) {
        if (!this.document) return Promise.resolve(false);

        const before = this._snapshot();
        const draft = this._snapshot();
        try {
            if (typeof mutate === "function") mutate(draft);
        } catch (error) {
            console.error("figure_builder: local edit failed", error);
            return Promise.resolve(false);
        }

        this.document = draft;
        // A field edited live (FigureLiveFields) commits as the value is
        // typed; every commit of one field in one sitting is ONE undo step,
        // back to what it was before the first.
        const key = this._mergeKey || null;
        const merged = key !== null && key === this._lastMergeKey
            && this._undo.length > 0 && this._undo[this._undo.length - 1] === this._lastMergeEntry
            && Date.now() - (this._lastMergeAt || 0) < FigureDocumentState.MERGE_MS;
        if (!merged) {
            this._undo.push(before);
            if (this._undo.length > FigureDocumentState.HISTORY_LIMIT) this._undo.shift();
            this._lastMergeEntry = before;
        }
        this._lastMergeKey = key;
        this._lastMergeAt = Date.now();
        // A new edit is a new branch: whatever was undone is no longer
        // reachable, and offering a redo that would reinstate a state the user
        // has since edited past is offering an action nobody can predict.
        this._redo = [];
        this._setStatus("unsaved");
        this._emit("change", this.document);

        const generation = this._generation || 0;
        const batch = { operations: operations, before: before, generation: generation };
        this._lastQueued = batch;
        return this._enqueue(() => this._save(batch));
    }

    /** Run `fn` with its commits merged into one undo step with the last
     *  ones made under the same `key` (see `commit`). */
    mergingAs(key, fn) {
        const previous = this._mergeKey;
        this._mergeKey = key;
        try {
            return fn();
        } finally {
            this._mergeKey = previous;
        }
    }

    /** Roll back one committed action. */
    undo() {
        this._lastMergeKey = null;
        if (!this._undo.length) return Promise.resolve(false);
        const target = this._undo.pop();
        const pushed = this._snapshot();
        this._redo.push(pushed);
        // Put back as it was if the server refuses the replace.
        return this._replaceWith(target, () => {
            FigureDocumentState._remove(this._redo, pushed);
            this._undo.push(target);
        });
    }

    redo() {
        this._lastMergeKey = null;
        if (!this._redo.length) return Promise.resolve(false);
        const target = this._redo.pop();
        const pushed = this._snapshot();
        this._undo.push(pushed);
        return this._replaceWith(target, () => {
            FigureDocumentState._remove(this._undo, pushed);
            this._redo.push(target);
        });
    }

    static _remove(list, entry) {
        const at = list.lastIndexOf(entry);
        if (at >= 0) list.splice(at, 1);
    }

    _replaceWith(target, revert) {
        const current = this.document;
        const draft = JSON.parse(JSON.stringify(target));
        // A panel's render revision never goes back, not even on an undo: the
        // preview stored for it is the latest render's, at the latest number,
        // and an edit that bumped the earlier number again would put a
        // different picture under a URL the browser keeps for good. The server
        // floors it the same way (repository._keep_render_revisions_forward).
        for (const [panelId, panel] of Object.entries(draft.panels || {})) {
            const now = current && current.panels && current.panels[panelId];
            if (now && (now.render_revision || 0) > (panel.render_revision || 0)) {
                panel.render_revision = now.render_revision;
            }
        }
        // The revision is the CURRENT one, not the one the snapshot was taken
        // at: going back to an earlier state is a new edit, and rewinding the
        // number would make every other tab's next save look valid when it is
        // not.
        draft.revision = current.revision;
        this.document = draft;
        this._setStatus("unsaved");
        this._emit("change", this.document);

        const generation = this._generation || 0;
        const batch = { document: draft, before: current, revert: revert, generation: generation };
        // Undo pressed ten times in a row queues ten whole-document saves.
        // A replace that has not started and is followed straight away by
        // another holds nothing the later one does not, so it is skipped --
        // never one with an edit queued after it, which was made on it.
        const previous = this._lastQueued;
        if (previous && previous.document && !previous.started) {
            previous.skip = true;
            // This one now stands for both: refused, it goes back to what
            // the server last had, and both history moves are put back.
            batch.before = previous.before;
            const later = revert;
            const earlier = previous.revert;
            batch.revert = () => { later?.(); earlier?.(); };
        }
        this._lastQueued = batch;
        return this._enqueue(() => this._save(batch));
    }

    /**
     * Send `batch`, after any earlier save whose request never arrived.
     *
     * A dropped request leaves its edit in the tab only. The next edit was made
     * on top of it, so sending that one alone and calling the result "saved"
     * would leave the server without the dropped edit while the tab says all is
     * stored. Each unsent batch goes out again in order, and "saved" is said
     * only once none is left.
     */
    async _save(batch) {
        batch.started = true;
        if (batch.skip) return true;
        // Made on top of an edit the server has since refused: `_settle` put
        // the tab back to the last confirmed state, which this edit is not
        // part of, so it is not sent (and its undo step goes with it).
        if ((batch.generation || 0) !== (this._generation || 0)) {
            FigureDocumentState._remove(this._undo, batch.before);
            return false;
        }
        // A whole-document replace already holds every earlier edit the tab
        // shows, so nothing before it needs sending -- once it is stored. Until
        // then it keeps them: if the server refuses it, the tab goes back to a
        // document that still shows them, and they must still go out.
        if (batch.document) {
            batch.supersedes = this._unsent;
            this._unsent = [];
        }
        this._unsent.push(batch);
        while (this._unsent.length) {
            const item = this._unsent[0];
            // Read HERE, not where the commit was made. The queue serialises
            // saves, so an earlier one may have moved the figure on between the
            // two moments -- and `before.revision` is then a revision the server
            // has already superseded, which it answers with a 409 that has
            // nothing to do with another session. Placing a text box and pressing
            // Escape was enough: the add and the remove are two commits in the
            // same breath, and the second carried the revision from before the
            // first. Two undos in a row broke the same way.
            //
            // Basing the operations on the latest CONFIRMED revision is exactly
            // what the chain exists to make safe: nothing else can have written
            // in between, because nothing else is allowed to be in flight.
            const baseRevision = this.revision;
            this._setStatus("saving");
            let result;
            try {
                if (item.document) {
                    item.document.revision = baseRevision;
                    result = await this.api.replaceFigure(this.figureId, baseRevision, item.document);
                } else {
                    result = await this.api.patchFigure(this.figureId, baseRevision, item.operations);
                }
            } catch (error) {
                // Never discard local state after a failed save: the network is
                // the thing that failed, and the user's work is still here. It
                // stays in `_unsent`, so the next save carries it.
                this._setStatus("failed", "Could not reach the server. Your changes are still here.");
                return false;
            }
            if (!result.ok) return this._settle(result, item);
            this._unsent.shift();
            this.document.revision = result.data.revision;
        }
        this._setStatus("saved");
        return true;
    }

    _settle(result, item) {
        if (result.status === 409) {
            // Both sides have work. Nothing is discarded here -- the banner
            // asks, and reloading is the user's decision.
            this._setStatus("conflict", "This figure was modified in another session.");
            return false;
        }
        if (result.status === 422) {
            this._setStatus("unreadable", result.data.detail || "");
            return false;
        }
        // The server refused the edit itself. The local copy is now describing
        // something that was never stored, so it goes back to what was
        // confirmed -- otherwise the next save would be built on a state the
        // server has never seen. Unsent edits made after it were built on it,
        // so they go too, with their undo steps; the ones before it are stored.
        const dropped = this._unsent.splice(0);
        dropped.slice(1).forEach((later) => {
            const at = this._undo.lastIndexOf(later.before);
            if (at >= 0) this._undo.splice(at, 1);
        });
        // A refused replace was to carry the unsent edits before it. The
        // document it goes back to shows them and the server never stored
        // them, so they are queued again for the next save.
        if (item.supersedes) this._unsent = item.supersedes;
        // The revision stays the confirmed one: unsent batches stored just now
        // moved it past the number this snapshot was taken at.
        const confirmed = this.revision;
        this.document = item.before;
        this.document.revision = confirmed;
        // The refused edit's own undo step, by identity: a commit made since
        // has pushed its own on top, and popping would take that one instead.
        FigureDocumentState._remove(this._undo, item.before);
        if (typeof item.revert === "function") item.revert();
        // Commits already queued behind this one were made on the state just
        // abandoned; `_save` drops them when their turn comes.
        this._generation = (this._generation || 0) + 1;
        this._setStatus("failed", result.data.error || "That change could not be saved.");
        this._emit("change", this.document);
        return false;
    }

    /**
     * Resolves once every save asked for so far has finished: `true` when the
     * server holds everything this tab shows. What Export waits on -- the
     * server renders what IT stores, so an edit still in flight or one whose
     * save failed would be missing from the file without a word.
     */
    whenSaved() {
        return this._chain.then(() => this.status === "saved", () => false);
    }

    _enqueue(task) {
        this._chain = this._chain.then(task, task);
        return this._chain;
    }

    _snapshot() {
        return JSON.parse(JSON.stringify(this.document));
    }

    // -- convenience readers, so callers do not reach into the shape ------

    get title() { return this.document ? this.document.title : ""; }

    get pages() { return this.document ? this.document.pages : []; }

    panel(panelId) {
        return (this.document && this.document.panels[panelId]) || null;
    }

    source(sourceId) {
        return (this.document && this.document.sources[sourceId]) || null;
    }

    /** The source registered for a datasource, or null if it is not one yet. */
    sourceForDatasource(datasource) {
        if (!this.document) return null;
        return Object.values(this.document.sources).find(
            (source) => source.kind === "plexora_project" && source.datasource === datasource) || null;
    }
}
