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
 * **Saves are chained, never concurrent.** Each carries the revision it was
 * built against, so two in flight at once means the second is stale before it
 * is sent. Chaining also makes the outcome deterministic: without it, a slow
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
        this._undo.push(before);
        if (this._undo.length > FigureDocumentState.HISTORY_LIMIT) this._undo.shift();
        // A new edit is a new branch: whatever was undone is no longer
        // reachable, and offering a redo that would reinstate a state the user
        // has since edited past is offering an action nobody can predict.
        this._redo = [];
        this._setStatus("unsaved");
        this._emit("change", this.document);

        return this._enqueue(async () => {
            const baseRevision = before.revision;
            this._setStatus("saving");
            let result;
            try {
                result = await this.api.patchFigure(this.figureId, baseRevision, operations);
            } catch (error) {
                // Never discard local state after a failed save: the network is
                // the thing that failed, and the user's work is still here.
                this._setStatus("failed", "Could not reach the server. Your changes are still here.");
                return false;
            }
            return this._settle(result, before);
        });
    }

    /** Roll back one committed action. */
    undo() {
        if (!this._undo.length) return Promise.resolve(false);
        const target = this._undo.pop();
        this._redo.push(this._snapshot());
        return this._replaceWith(target);
    }

    redo() {
        if (!this._redo.length) return Promise.resolve(false);
        const target = this._redo.pop();
        this._undo.push(this._snapshot());
        return this._replaceWith(target);
    }

    _replaceWith(target) {
        const current = this.document;
        const draft = JSON.parse(JSON.stringify(target));
        // The revision is the CURRENT one, not the one the snapshot was taken
        // at: going back to an earlier state is a new edit, and rewinding the
        // number would make every other tab's next save look valid when it is
        // not.
        draft.revision = current.revision;
        this.document = draft;
        this._setStatus("unsaved");
        this._emit("change", this.document);

        return this._enqueue(async () => {
            this._setStatus("saving");
            let result;
            try {
                result = await this.api.replaceFigure(this.figureId, current.revision, draft);
            } catch (error) {
                this._setStatus("failed", "Could not reach the server. Your changes are still here.");
                return false;
            }
            return this._settle(result, current);
        });
    }

    _settle(result, before) {
        if (result.ok) {
            this.document.revision = result.data.revision;
            this._setStatus("saved");
            return true;
        }
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
        // server has never seen.
        this.document = before;
        this._undo.pop();
        this._setStatus("failed", result.data.error || "That change could not be saved.");
        this._emit("change", this.document);
        return false;
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
