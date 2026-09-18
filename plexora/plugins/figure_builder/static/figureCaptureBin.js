/**
 * FigureCaptureBin - captures before they belong to anything.
 *
 * Capturing used to ask "which figure?" first: the dock carried a "Capture
 * into" row, and anything taken before an answer was given lived in page
 * memory, was marked as at risk in the strip, and was lost on the next
 * navigation -- which is why the dock had a beforeunload guard and a
 * "will be lost" confirmation on its own close button.
 *
 * Every capture now lands in a persistent bin on the server (server/captures.py)
 * and the figure question is asked once, later, on the way to the canvas. So
 * there is nothing left to lose by reloading, nothing to guard against, and no
 * decision demanded at the moment somebody is looking at tissue.
 *
 * ## Adoption happens at the canvas, not at the door
 *
 * The picker does not write panels. It writes a NOTE -- which figure, which
 * captures -- and navigates; the figure page reads the note on arrival and
 * does the work with the document already open in front of it. Three things
 * come out of that:
 *
 *   - no "save everything before the page changes, or block the navigation"
 *     dance, which is what the old path had to do because the strip was the
 *     only copy;
 *   - a failure is harmless and honest: the captures are still in the bin, and
 *     the canvas says so;
 *   - ONE code path serves the dock's "Figure Canvas", the tray's "From
 *     captures…" and the library's Captures tab, because all three end in
 *     `adoptInto` against an open document.
 *
 * The note is in sessionStorage and read once, so a reload of the canvas does
 * not adopt the same captures twice.
 */
class FigureCaptureBin {

    /** Where the picker leaves its answer for the figure page to pick up.
     *  Per-tab, like the pending-edit note next to it: two tabs can be on two
     *  figures with two different answers in flight. */
    static get ADOPT_KEY() { return "plexora:figure-builder-adopt"; }

    /**
     * The panel a capture becomes.
     *
     * Static and pure so the shape is checked without a browser -- one place
     * for the defaults means a field added to the format has one place to be
     * remembered in, whichever path built the panel. It lived on
     * FigureBuilderSidebarController until the bin existed; it is here now
     * because the viewer is no longer the only place a capture turns into a
     * panel.
     */
    static panelFor(capture, source) {
        return {
            panel_id: FigureSchema.newPanelId(),
            source_id: source.source_id,
            // The scene was taken before the source existed -- a capture in
            // the bin belongs to no figure, so nothing had assigned the image
            // an id inside one -- and this is where the two are joined.
            scene: { ...capture.scene, source_id: source.source_id },
            // Straight to the tray: composition is a different sitting from
            // exploration, and forcing a layout decision at the moment of
            // capture is what makes people stop capturing.
            placement: null,
            label: { text: "", auto: true, visible: true },
            // No calibration, no PHYSICAL scale bar -- a bar drawn from an
            // assumed pixel size looks exactly like one that is right. An
            // uncalibrated capture measures its bar in image pixels instead,
            // which is a true statement about the picture and is what the panel
            // switches back to microns the moment a real pixel size is typed.
            ...FigureSchema.defaultFurniture({
                scalebar: {
                    ...FigureSchema.defaultFurniture().scalebar,
                    visible: Boolean(source.pixel_size),
                    unit: source.pixel_size ? "um" : "px",
                },
            }),
            render_revision: 1,
        };
    }

    /**
     * Turn bin captures into panels of an open figure, in ONE commit.
     *
     * One commit because adding six captures is one thing the user did: six
     * would be six undo steps and six saves for a single decision. The sources
     * ride in the same batch for the same reason -- a figure that acquired a
     * source in one revision and the panels using it in the next is a figure
     * whose first revision is invalid on its own.
     *
     * The rasters move afterwards, server-side, and a failure there is a slow
     * panel rather than a lost one: the scene is the master and the preview is
     * a convenience, so a panel with no cached raster still re-renders and
     * still exports.
     *
     * @param {FigureDocumentState} state the OPEN document. Never a second
     *        state on the same figure -- see FigureWorkspace's class comment.
     * @param {FigureBuilderApi} api
     * @param {Array} entries bin rows, as `listCaptures` returns them.
     * @returns {Promise<{panelIds: string[], failed: number}>}
     */
    static async adoptInto(state, api, entries) {
        const out = { panelIds: [], failed: 0 };
        if (!state || !state.document || !Array.isArray(entries) || !entries.length) {
            out.failed = Array.isArray(entries) ? entries.length : 0;
            return out;
        }

        const operations = [];
        const added = {};
        const panels = [];
        const pairs = [];

        // Oldest first, so the tray reads in the order the captures were taken.
        // The bin hands them back newest-first because that is where the eye
        // goes in a strip; a figure is a record and records run forwards.
        for (const entry of entries.slice().reverse()) {
            const source = FigureCaptureBin._sourceFor(state, entry, added, operations);
            if (!source) {
                out.failed += 1;
                continue;
            }
            const panel = FigureCaptureBin.panelFor(entry, source);
            panels.push(panel);
            operations.push({ op: "add_panel", panel: panel });
            pairs.push({ capture_id: entry.capture_id, panel_id: panel.panel_id,
                         render_revision: 1 });
        }
        if (!panels.length) return out;

        const stored = await state.commit(operations, (draft) => {
            for (const source of Object.values(added)) draft.sources[source.source_id] = source;
            for (const panel of panels) draft.panels[panel.panel_id] = panel;
        });
        if (!stored) {
            out.failed = entries.length;
            return out;
        }

        out.panelIds = panels.map((panel) => panel.panel_id);
        // Never fatal: the panels are already in the figure, and the bin rows
        // are emptied by this call. A preview that did not move leaves a panel
        // that re-renders from its scene -- slower, never wrong.
        try {
            await api.adoptPreviews(state.figureId, pairs);
        } catch (error) {
            console.error("figure_builder: capture previews did not move", error);
        }
        return out;
    }

    /**
     * The figure source this capture's image should be a panel of.
     *
     * An image already in the figure is reused rather than registered twice: a
     * second source for one datasource is two provenance rows for one slide and
     * two places "this source has changed" has to be answered.
     *
     * A capture carries the description of its image -- dimensions, channel
     * keys, fingerprint, pixel size -- taken at the moment of capture, because
     * by the time it is adopted the datasource may not be loaded any more and
     * on the figure page there is no viewer at all. The stored id is a
     * placeholder (server/captures.py explains why) and is replaced here.
     */
    static _sourceFor(state, entry, added, operations) {
        const datasource = entry.datasource || (entry.source && entry.source.datasource) || "";
        if (datasource) {
            const existing = state.sourceForDatasource(datasource);
            if (existing) return existing;
            if (added[datasource]) return added[datasource];
        }
        const described = entry.source;
        if (!described || !described.kind) return null;
        const source = { ...described, source_id: FigureSchema.newSourceId() };
        operations.push({ op: "add_source", source: source });
        added[datasource || source.source_id] = source;
        return source;
    }

    // -- the note between the two pages ----------------------------------

    /** Say which captures should join which figure, for the page that is about
     *  to open it. */
    static leaveAdoptNote(note) {
        try {
            window.sessionStorage.setItem(FigureCaptureBin.ADOPT_KEY, JSON.stringify(note));
            return true;
        } catch (error) {
            // Private-browsing modes throw. The navigation is still worth
            // making: the captures are in the bin, and "From captures…" on the
            // canvas is the way to reach them without a note.
            return false;
        }
    }

    /** Read once and clear, so reloading the canvas cannot adopt the same
     *  captures a second time. */
    static takeAdoptNote() {
        try {
            const raw = window.sessionStorage.getItem(FigureCaptureBin.ADOPT_KEY);
            if (!raw) return null;
            window.sessionStorage.removeItem(FigureCaptureBin.ADOPT_KEY);
            const note = JSON.parse(raw);
            if (!note || !note.figure_id || !Array.isArray(note.capture_ids)) return null;
            return note;
        } catch (error) {
            return null;
        }
    }

    // -- the store, as the rest of the client sees it --------------------

    constructor(options) {
        this.api = (options && options.api) || new FigureBuilderApi();
    }

    /**
     * The bin, newest first, each entry carrying the URL of its thumbnail.
     *
     * A server route rather than an object URL, which is what makes the bin
     * survive a reload at all: the blob a capture was taken from is gone the
     * moment the page is.
     */
    async list(datasource) {
        const result = await this.api.listCaptures(datasource);
        if (!result.ok || !Array.isArray(result.data.captures)) return null;
        return result.data.captures.map((capture) => ({
            ...capture,
            url: capture.has_preview ? this.api.capturePreviewUrl(capture.capture_id) : null,
        }));
    }

    async add(entry) {
        const result = await this.api.addCapture(entry);
        return result.ok;
    }

    async putPreview(captureId, blob, size) {
        const result = await this.api.putCapturePreview(captureId, blob, size);
        return result.ok;
    }

    async remove(captureIds) {
        if (!captureIds || !captureIds.length) return true;
        const result = await this.api.removeCaptures(captureIds);
        return result.ok;
    }
}
