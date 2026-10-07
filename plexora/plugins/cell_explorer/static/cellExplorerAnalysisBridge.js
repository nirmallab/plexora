/**
 * cellExplorerAnalysisBridge.js - "colour by this column", asked from outside.
 *
 * Three askers, one answer. An agent sends the viewer command `set_color_by`
 * (agentBridge.js offers it to whichever plugin claims it); the Analysis panel
 * an analysis application adds to Plexora dispatches `plexora:color-by-request`
 * when a result is shown; a launch link's context names `color_by`
 * (services/launchContext.js, through the same command). All three mean "show
 * the cells coloured by this column of the table, in this tab" -- which is
 * exactly what this panel's own variable picker does, so that is the path
 * taken: re-read the variable list, then `select(column, {persist: false})`.
 *
 * `persist: false` is the point. A colour-by asked for from outside is one
 * page view's arrangement; the project keeps whatever the user last chose by
 * hand, as `plexora.view(overlay=...)` already does.
 *
 * It also hears `core`/`dataset.changed` (server/models/dataset_events.py):
 * the table gained or rewrote a column underneath this page, so the variable
 * list is re-read, and the column on screen is re-selected when it is one of
 * the sections that changed -- a re-run phenotype recolours in place.
 *
 * A listener only. It owns no geometry and no store, and with Cell Explorer
 * closed it is not loaded at all -- the command handler opens the tool first.
 */
class CellExplorerAnalysisBridge {

    constructor(controller) {
        this.controller = controller;
        this._onCommand = (event) => this.onCommand(event);
        this._onRequest = (event) => this.onRequest(event);
        this._onChanged = (event) => this.onChanged(event);
    }

    attach() {
        if (typeof window === "undefined" || !window.addEventListener) return this;
        window.addEventListener("plexora:agent-command", this._onCommand);
        window.addEventListener("plexora:color-by-request", this._onRequest);
        window.addEventListener("plexora:agent-state-changed", this._onChanged);
        return this;
    }

    destroy() {
        if (typeof window === "undefined" || !window.removeEventListener) return;
        window.removeEventListener("plexora:agent-command", this._onCommand);
        window.removeEventListener("plexora:color-by-request", this._onRequest);
        window.removeEventListener("plexora:agent-state-changed", this._onChanged);
    }

    /** The column names this table offers, as the panel's picker lists them. */
    known() {
        return (this.controller.state.descriptors || []).map((entry) => entry.name);
    }

    /** Colour by `column`: `{column, selected}`, or a rejection that names
     *  the columns there are, so the asker can correct itself. */
    async colorBy(column) {
        const name = String(column || "").trim();
        if (!name) throw new Error("needs a `column`");
        await this.controller.fetchSaved();
        const columns = this.known();
        if (!columns.includes(name)) {
            const listed = columns.slice(0, 12).join(", ");
            throw new Error(`no column "${name}" in this table`
                + (listed ? ` (it has: ${listed}${columns.length > 12 ? ", ..." : ""})` : ""));
        }
        await this.controller.select(name, { persist: false });
        this.controller.render?.();
        return { column: name, selected: this.controller.state.column === name };
    }

    onCommand(event) {
        const detail = event && event.detail;
        if (!detail || detail.type !== "set_color_by" || detail.claimed) return;
        detail.claim(this.colorBy((detail.arguments || {}).column), "cell_explorer");
    }

    onRequest(event) {
        const column = event && event.detail && event.detail.column;
        this.colorBy(column).catch((error) => {
            console.error("Cell Explorer: could not colour by", column, error);
        });
    }

    async onChanged(event) {
        const detail = (event && event.detail) || {};
        if (detail.plugin !== "core" || detail.kind !== "dataset.changed") return;
        const sections = ((detail.payload || {}).sections || []).map(String);
        const current = this.controller.state.column;
        try {
            await this.controller.fetchSaved();
            if (current && sections.includes(`obs:${current}`) && this.known().includes(current)) {
                await this.controller.select(current, { persist: false });
            }
            this.controller.render?.();
        } catch (error) {
            console.error("Cell Explorer: could not re-read the table's columns", error);
        }
    }
}
