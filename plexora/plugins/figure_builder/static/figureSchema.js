/**
 * FigureSchema - the parts of the figure format the client has to agree with.
 *
 * Ids are generated HERE rather than by the server so a captured panel appears
 * the instant the pointer is released, not after a round trip. That is the same
 * bargain ROI makes, and it carries the same obligation: the server validates
 * every id it is sent (server/schema.py ID_PATTERN), because these end up in
 * DOM attributes and selectors.
 *
 * The two coordinate systems are kept apart on this side too. `mm` is page
 * geometry -- placements, annotations, page size. Full-resolution image pixels
 * are scene geometry, and nothing here converts between them: they only meet at
 * export, where mm x DPI decides how many source pixels to read. A helper that
 * turned one into the other would be a helper somebody eventually used to store
 * a panel's crop in screen units, which is exactly the mistake that makes a
 * figure un-re-renderable.
 */
const FigureSchema = {

    SCHEMA_VERSION: 1,
    SNAPSHOT_VERSION: 1,

    /** Millimetres per inch, and per CSS pixel at the nominal 96 dpi. */
    MM_PER_INCH: 25.4,

    PAGE_PRESETS: {
        a4: { w: 210.0, h: 297.0, label: "A4" },
        letter: { w: 215.9, h: 279.4, label: "US Letter" },
        square: { w: 200.0, h: 200.0, label: "Square" },
    },

    /**
     * A short random id with a readable prefix.
     *
     * crypto.randomUUID where it exists; the fallback matters because Plexora
     * is served over plain http on 127.0.0.1 in some deployments, and
     * `crypto.randomUUID` is a secure-context API. Without a fallback, capture
     * would simply stop working there with a TypeError nobody could interpret.
     */
    newId(prefix) {
        const random = (window.crypto && window.crypto.randomUUID)
            ? window.crypto.randomUUID().replace(/-/g, "")
            : (Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
        return prefix + "_" + random.slice(0, 12);
    },

    newPanelId() { return this.newId("pnl"); },
    newPageId() { return this.newId("pg"); },
    newSourceId() { return this.newId("src"); },
    newAnnotationId() { return this.newId("ann"); },
    newGroupId() { return this.newId("grp"); },

    /**
     * The panel label for a zero-based position: A..Z, then AA, AB, ...
     *
     * Base-26 with no zero digit, which is the spreadsheet-column sequence
     * rather than plain base conversion -- position 26 is "AA", not "BA".
     */
    labelFor(index, style) {
        if (style === "A1") return "A" + String(index + 1);
        let n = Math.max(0, Math.floor(index));
        let out = "";
        do {
            out = String.fromCharCode(65 + (n % 26)) + out;
            n = Math.floor(n / 26) - 1;
        } while (n >= 0);
        return style === "a" ? out.toLowerCase() : out;
    },

    /** Panels on one page, in the order their labels should run. */
    panelsOnPage(document_, pageId) {
        return Object.values(document_.panels || {})
            .filter((panel) => panel.placement && panel.placement.page_id === pageId)
            // Reading order, then z as the tiebreak. A user who lays out a 3x2
            // grid expects A B C / D E F, which is rows before columns; sorting
            // by z alone would number them by the order they happened to be
            // captured in.
            .sort((a, b) => (a.placement.y_mm - b.placement.y_mm)
                || (a.placement.x_mm - b.placement.x_mm)
                || (a.placement.z - b.placement.z));
    },

    /** Panels not on any page: captured, kept, not laid out. */
    panelsInTray(document_) {
        return Object.values(document_.panels || {}).filter((panel) => !panel.placement);
    },

    pageById(document_, pageId) {
        return (document_.pages || []).find((page) => page.page_id === pageId) || null;
    },

    /**
     * A scene snapshot with nothing captured yet.
     *
     * Exists so that a panel built by any path -- capture, split, an imported
     * asset -- has the same shape, and so a field added to the format has one
     * place to be defaulted rather than N call sites to be remembered in.
     */
    emptyScene(sourceId) {
        return {
            snapshot_version: this.SNAPSHOT_VERSION,
            source_id: sourceId || "",
            viewport: { x: 0, y: 0, w: 1, h: 1 },
            channels: [],
            core_overlays: { cell_layers: [], hd_tiles: false, scalebar_visible: false },
            plugins: {},
            captured_at: new Date().toISOString(),
        };
    },

    /**
     * How many microns across a captured region is, or null.
     *
     * Null propagates rather than defaulting: a scale bar drawn from an assumed
     * pixel size is wrong and looks exactly like one that is right, which is the
     * single worst failure mode a figure tool has.
     */
    physicalWidthUm(source, viewport) {
        const pixelSize = source && source.pixel_size;
        if (!pixelSize || !(pixelSize.value > 0) || !viewport) return null;
        return viewport.w * pixelSize.value;
    },

    /** A round number of microns that fits comfortably inside `spanUm`. */
    scaleBarLength(spanUm) {
        if (!(spanUm > 0)) return null;
        const target = spanUm / 4;
        const candidates = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500,
            1000, 2000, 5000, 10000];
        let best = candidates[0];
        for (const value of candidates) {
            if (value <= target) best = value;
        }
        return best;
    },

    formatMicrons(value) {
        if (!(value > 0)) return "";
        if (value >= 1000) return (value / 1000).toFixed(value % 1000 ? 1 : 0) + " mm";
        if (value >= 10) return Math.round(value) + " µm";
        return value.toFixed(1) + " µm";
    },

    /**
     * Text that is about to be put in the DOM as markup.
     *
     * Every user-supplied string on this side goes through here or through
     * textContent. The server strips control characters and caps length but
     * deliberately does not HTML-escape -- escaping at the store double-escapes
     * on every round trip -- so escaping is this side's job, at the point of
     * use.
     */
    escapeHtml(value) {
        return String(value === undefined || value === null ? "" : value)
            .replace(/[&<>"']/g, (c) => ({
                "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
            }[c]));
    },

    /** "3 minutes ago" for a timestamp, "" for anything unparseable. */
    timeAgo(iso) {
        if (!iso) return "";
        const then = new Date(iso).getTime();
        if (Number.isNaN(then)) return "";
        const seconds = Math.round((Date.now() - then) / 1000);
        if (seconds < 60) return "just now";
        const minutes = Math.round(seconds / 60);
        if (minutes < 60) return minutes + (minutes === 1 ? " minute ago" : " minutes ago");
        const hours = Math.round(minutes / 60);
        if (hours < 24) return hours + (hours === 1 ? " hour ago" : " hours ago");
        const days = Math.round(hours / 24);
        if (days < 30) return days + (days === 1 ? " day ago" : " days ago");
        return new Date(iso).toLocaleDateString(undefined,
            { year: "numeric", month: "short", day: "numeric" });
    },

    countPhrase(count, singular, plural) {
        return count + " " + (count === 1 ? singular : (plural || singular + "s"));
    },
};
