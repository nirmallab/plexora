/**
 * sectionCells.js - a 3-D mask's cells in the XZ / YZ section panes.
 *
 * The plane shows a 3-D mask's cells as they cross the plane on screen (its
 * label tiles are that plane's). A section pane is a plane too -- along a row
 * (XZ) or a column (YZ), through every z -- so it shows the cells as they cross
 * IT: each cell where its voxels lie on the pane's line, plane by plane, drawn
 * as its stepped outline (`/volume/<ds>/section_cells`, the same ids the plane
 * draws). Coloured cell for cell as the plane colours them (labelTile.js
 * `cellColour` over `ImageViewer.maskDrawList`): with a gate on, the cells it
 * passes; with none, every cell; with the outlines off, nothing.
 *
 * Read once the panes settle on a cut (`plexora:sections-moved`), at the label
 * level the pane's zoom can show, and kept per cut: panning back reads
 * nothing. Painted through the panes' overlay seam (SectionPanes.addOverlay),
 * repainted when a gate or the cell mode changes. Nothing for a 2-D mask.
 *
 * Classic script, exposed as `window.PlexoraSectionCells`.
 */
(function (global) {
    "use strict";

    const CACHE = 24;
    //: Pixels read past each end of the line on screen, so a small pan draws
    //: from what is held.
    const MARGIN = 0.5;

    function url(path) {
        return typeof global.plexoraUrl === "function" ? global.plexoraUrl(path) : `/${path}`;
    }

    class SectionCells {
        constructor(sections, { imageViewer, viewState, datasource, fetchImpl = null } = {}) {
            this.sections = sections;
            this.imageViewer = imageViewer;
            this.viewState = viewState;
            this.datasource = datasource;
            this._fetch = fetchImpl || ((...args) => global.fetch(...args));
            //: key -> {axis, at, t, a0, a1, level, z, label, first, last}
            this.cache = new Map();
            //: axis -> the last cut read: drawn until the next one lands.
            this.last = {};
            this.pending = new Map();
        }

        attach() {
            if (!this.sections?.addOverlay || !this.viewState?.labelsAre3d?.()) return this;
            this.off = this.sections.addOverlay("core:cells", (query) => this.shapes(query));
            const repaint = () => this.sections.paintOverlays?.();
            global.addEventListener?.("plexora:sections-moved", (event) => this.read(event.detail));
            global.addEventListener?.("plexora:cell-gate-changed", repaint);
            global.addEventListener?.("plexora:cell-mode-changed", repaint);
            global.addEventListener?.("plexora:view-changed", () => {
                // Another frame of a time series is other cells; another
                // plane is the same cut.
                const t = this.viewState.get?.().t || 0;
                if (t !== this.frame) {
                    this.frame = t;
                    this.read(null);
                }
            });
            this.frame = this.viewState.get?.().t || 0;
            return this;
        }

        /** The label level a pane at `scale` screen px per image px can show. */
        levelFor(scale) {
            if (!(scale > 0) || scale >= 1) return 0;
            return Math.max(0, Math.floor(Math.log2(1 / scale)));
        }

        /** Read both cuts where the panes are now. */
        read(detail) {
            const frame = this.sections?.frame;
            if (!frame || !this.sections.enabled) return;
            const t = this.viewState.get?.().t || 0;
            const level = this.levelFor(frame.scale);
            const x = Math.round(detail?.x ?? frame.cx);
            const y = Math.round(detail?.y ?? frame.cy);
            const spanX = (frame.x1 - frame.x0) * MARGIN;
            const spanY = (frame.y1 - frame.y0) * MARGIN;
            this.fetchCut({ axis: "xz", at: y, t, level,
                            a0: Math.floor(frame.x0 - spanX), a1: Math.ceil(frame.x1 + spanX) });
            this.fetchCut({ axis: "yz", at: x, t, level,
                            a0: Math.floor(frame.y0 - spanY), a1: Math.ceil(frame.y1 + spanY) });
        }

        /** A held cut covering `want`, or null. */
        held(want) {
            for (const cut of this.cache.values()) {
                if (cut.axis === want.axis && cut.at === want.at && cut.t === want.t
                        && cut.level <= Math.max(want.level, 0) && cut.a0 <= want.a0
                        && cut.a1 >= want.a1) return cut;
            }
            return null;
        }

        async fetchCut(want) {
            const hit = this.held({ ...want, a0: Math.max(0, want.a0), a1: want.a1 });
            if (hit) {
                this.last[want.axis] = hit;
                this.sections.paintOverlays?.();
                return hit;
            }
            const key = `${want.axis}|${want.at}|${want.t}|${want.level}|${want.a0}|${want.a1}`;
            if (this.pending.has(key)) return this.pending.get(key);
            const params = new URLSearchParams({
                axis: want.axis, at: String(want.at), t: String(want.t), level: String(want.level),
                a0: String(Math.max(0, want.a0)), a1: String(Math.max(0, want.a1)) });
            const address = url(`volume/${encodeURIComponent(this.datasource)}/section_cells`);
            const promise = this._fetch(`${address}?${params}`, { credentials: "same-origin" })
                .then(async (response) => {
                    if (!response?.ok) return null;
                    const header = JSON.parse(response.headers.get("X-Section-Cells") || "{}");
                    const buffer = await response.arrayBuffer();
                    const n = Number(header.n) || 0;
                    const cut = {
                        ...want, a0: Math.max(0, want.a0), level: Number(header.level) || 0,
                        z: new Uint16Array(buffer.slice(0, 2 * n)),
                        label: new Uint32Array(buffer.slice(2 * n, 6 * n)),
                        first: new Uint32Array(buffer.slice(6 * n, 10 * n)),
                        last: new Uint32Array(buffer.slice(10 * n, 14 * n)),
                    };
                    this.cache.set(key, cut);
                    while (this.cache.size > CACHE) this.cache.delete(this.cache.keys().next().value);
                    this.last[want.axis] = cut;
                    this.sections.paintOverlays?.();
                    return cut;
                })
                .catch(() => null)
                .finally(() => this.pending.delete(key));
            this.pending.set(key, promise);
            return promise;
        }

        /** Whether the plane draws a mask at all right now. */
        drawing() {
            const viewer = this.imageViewer;
            return Boolean(viewer && !viewer.noLabel && viewer.viewerManagerVMain?.sel_outlines);
        }

        /**
         * The panes' overlay provider: one stepped outline per cell cut, in
         * the colour the plane gives the cell (the topmost layer that draws
         * it wins, as on a label tile).
         */
        shapes(query) {
            if (!this.drawing()) return [];
            const cut = this.last[query.axis];
            if (!cut || cut.t !== (query.t || 0)) return [];
            const layers = this.imageViewer.maskDrawList?.() || [];
            const colour = global.PlexoraLabelTile?.cellColour;
            if (!layers.length || typeof colour !== "function") return [];
            const byCell = new Map();
            for (let i = 0; i < cut.label.length; i += 1) {
                const id = cut.label[i];
                let shape = byCell.get(id);
                if (shape === undefined) {
                    let rgba = null;
                    for (let k = layers.length - 1; k >= 0 && !rgba; k -= 1) rgba = colour(layers[k], id);
                    shape = rgba ? { color: `rgba(${rgba[0]}, ${rgba[1]}, ${rgba[2]}, ${(rgba[3] / 255).toFixed(3)})`,
                                     outline: true, id, spans: [] } : null;
                    byCell.set(id, shape);
                }
                if (shape) shape.spans.push({ z0: cut.z[i], z1: cut.z[i], a0: cut.first[i], a1: cut.last[i] });
            }
            return [...byCell.values()].filter(Boolean);
        }
    }

    function attach(sections, options) {
        if (!sections) return null;
        return new SectionCells(sections, options).attach();
    }

    global.PlexoraSectionCells = { attach, SectionCells };
    if (typeof module !== "undefined" && module.exports) module.exports = global.PlexoraSectionCells;
})(typeof window !== "undefined" ? window : globalThis);
