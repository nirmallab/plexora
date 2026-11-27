/**
 * cellExplorerSelection.js - the shared selection, drawn on the tissue.
 *
 * Cells picked anywhere -- a lasso on an embedding, a notebook step, a region
 * here -- live in core's selection (services/cellSelection.js), keyed by mask
 * label. This puts them on the image through the one channel Cell Explorer
 * already has: the colour table it hands core. Nothing is fetched and no tile
 * is re-rendered beyond what any colour change costs.
 *
 * Three ways to show it (`PlexoraCellSelection.mode()`):
 *   highlight  every cell in its colour, the selected ones white over them
 *   dim        the selected ones white, the rest darkened
 *   only       the selected ones in their colour, the rest not drawn
 *
 * Darkened rather than made translucent: centroids are drawn by colour alone
 * (ImageViewer.cellColorStyle reads alpha only as "drawn or not"), and a dim
 * that worked on outlines but not on dots would be a mode that half works.
 *
 * A listener on window; owns no DOM. Without core's selection it does nothing.
 */
class CellExplorerSelection {

    static WHITE = [255, 255, 255, 255];
    //: How much of its colour a dimmed cell keeps, and its alpha cap.
    static DIM = 0.28;
    static DIM_ALPHA = 150;

    constructor(controller) {
        this.controller = controller;
        this._onChange = () => this.controller.recolor();
    }

    attach() {
        if (typeof window === "undefined" || !window.addEventListener) return this;
        window.addEventListener("plexora:cells-selected", this._onChange);
        window.addEventListener("plexora:cells-selection-mode", this._onChange);
        // The stored selection lands after the first paint on a fresh page load.
        window.PlexoraCellSelection?.ready?.().then(() => this.controller.recolor());
        return this;
    }

    destroy() {
        if (typeof window === "undefined" || !window.removeEventListener) return;
        window.removeEventListener("plexora:cells-selected", this._onChange);
        window.removeEventListener("plexora:cells-selection-mode", this._onChange);
    }

    /** This project's selected ids, or null when no selection speaks for it. */
    ids() {
        const bus = window.PlexoraCellSelection;
        const project = this.controller.ctx?.datasource;
        return bus && project ? bus.get(project) : null;
    }

    /** `lut` (Cell Explorer's own table, or null) with the selection applied. */
    apply(lut) {
        const ids = this.ids();
        if (!ids) return lut;
        return CellExplorerSelection.compose(lut, ids, window.PlexoraCellSelection.mode());
    }

    /**
     * The pure part, testable without a page: a new table, never the input
     * mutated (Cell Explorer rebuilds its own and may hand the same one back).
     */
    static compose(lut, ids, mode) {
        const last = ids.length ? ids[ids.length - 1] : 0;
        const WHITE = CellExplorerSelection.WHITE;
        if (!lut) {
            // Nothing coloured by a column: the selection alone, in white.
            if (!ids.length) return mode === "only" ? { map: new Map() } : null;
            if (last > CellExplorerColors.DENSE_MAX_ID) {
                return { map: new Map(Array.from(ids, (id) => [id, WHITE])) };
            }
            const colors = new Uint8Array(4 * (last + 1));
            for (let i = 0; i < ids.length; i += 1) colors.set(WHITE, ids[i] * 4);
            return { colors, maxId: last };
        }
        if (lut.colors) {
            const maxId = Math.max(lut.maxId, mode === "only" ? 0 : last);
            const source = lut.colors;
            let colors;
            if (mode === "only") {
                colors = new Uint8Array(4 * (lut.maxId + 1));
                for (let i = 0; i < ids.length; i += 1) {
                    const o = ids[i] * 4;
                    if (ids[i] > lut.maxId) continue;
                    if (source[o + 3]) colors.set(source.subarray(o, o + 4), o);
                    else colors.set(WHITE, o);
                }
                return { colors, maxId: lut.maxId };
            }
            colors = new Uint8Array(4 * (maxId + 1));
            colors.set(source);
            if (mode === "dim") {
                const k = CellExplorerSelection.DIM;
                const cap = CellExplorerSelection.DIM_ALPHA;
                for (let o = 0; o < source.length; o += 4) {
                    if (!colors[o + 3]) continue;
                    colors[o] *= k;
                    colors[o + 1] *= k;
                    colors[o + 2] *= k;
                    if (colors[o + 3] > cap) colors[o + 3] = cap;
                }
            }
            for (let i = 0; i < ids.length; i += 1) colors.set(WHITE, ids[i] * 4);
            return { colors, maxId };
        }
        // The sparse form, for ids past DENSE_MAX_ID.
        const map = new Map();
        const chosen = new Set(ids);
        if (mode === "only") {
            for (const id of ids) map.set(id, lut.map.get(id) || WHITE);
            return { map };
        }
        const k = CellExplorerSelection.DIM;
        for (const [id, rgba] of lut.map) {
            if (chosen.has(id)) continue;
            map.set(id, mode === "dim"
                ? [rgba[0] * k, rgba[1] * k, rgba[2] * k, Math.min(rgba[3], CellExplorerSelection.DIM_ALPHA)]
                : rgba);
        }
        for (const id of ids) map.set(id, WHITE);
        return { map };
    }
}

if (typeof module !== "undefined" && module.exports) {
    module.exports = CellExplorerSelection;
}
