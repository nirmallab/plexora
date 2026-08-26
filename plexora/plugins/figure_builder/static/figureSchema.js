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
    newLabelId() { return this.newId("lbl"); },

    /**
     * Where a piece of furniture sits inside its panel.
     *
     * `server/schema.PANEL_ANCHORS`, in the same order, which is reading order
     * -- so the nine buttons in the sidebar can be emitted straight from this
     * list into a 3x3 grid.
     */
    PANEL_ANCHORS: ["top_left", "top_center", "top_right",
        "middle_left", "center", "middle_right",
        "bottom_left", "bottom_center", "bottom_right"],

    /** An anchor as {row, column}. "center" is the one without a seam. */
    anchorParts(anchor) {
        if (anchor === "center") return { row: "middle", column: "center" };
        const [row, column] = String(anchor || "").split("_");
        return {
            row: ["top", "middle", "bottom"].includes(row) ? row : "bottom",
            column: ["left", "center", "right"].includes(column) ? column : "right",
        };
    },

    /**
     * The top-left of a box of this size at one of the nine anchors, in mm.
     *
     * `compose.anchor_box`'s mirror, and pinned against it by
     * `test_the_canvas_and_the_exporter_anchor_furniture_alike`. The margin
     * applies only where the box is pushed against a side: a centred box is
     * centred on the panel itself, so a colour bar under one panel lines up
     * with the one under its neighbour even when the two are different widths.
     */
    anchorBox(place, anchor, wMm, hMm, marginMm) {
        const { row, column } = this.anchorParts(anchor);
        const x = column === "left" ? place.x_mm + marginMm
            : column === "right" ? place.x_mm + place.w_mm - marginMm - wMm
                : place.x_mm + (place.w_mm - wMm) / 2;
        const y = row === "top" ? place.y_mm + marginMm
            : row === "bottom" ? place.y_mm + place.h_mm - marginMm - hMm
                : place.y_mm + (place.h_mm - hMm) / 2;
        return { x: x, y: y };
    },

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
        return Object.values((document_ && document_.panels) || {})
            .filter((panel) => panel.placement && panel.placement.page_id === pageId)
            // Reading order, then z as the tiebreak. A user who lays out a 3x2
            // grid expects A B C / D E F, which is rows before columns; sorting
            // by z alone would number them by the order they happened to be
            // captured in.
            .sort((a, b) => (a.placement.y_mm - b.placement.y_mm)
                || (a.placement.x_mm - b.placement.x_mm)
                || (a.placement.z - b.placement.z));
    },

    /** Panels not on any page: captured, kept, not laid out.
     *
     *  Null-safe on the DOCUMENT, not merely on its panels. The workspace lays
     *  out its chrome before the figure has finished loading -- which is the
     *  state `tests/js/figure_builder_boot_probe.mjs` boots in on purpose --
     *  and a tray asking what is in it during that window is a fair question
     *  with the answer "nothing yet". */
    panelsInTray(document_) {
        return Object.values((document_ && document_.panels) || {})
            .filter((panel) => !panel.placement);
    },

    pageById(document_, pageId) {
        return ((document_ && document_.pages) || []).find((page) => page.page_id === pageId) || null;
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
     * A panel's furniture with nothing turned on.
     *
     * Every field the server would default, spelled out here as well, because
     * the browser draws from the DRAFT: a panel built client-side without a
     * `colorbar` key renders once against `undefined.visible` and takes the
     * canvas down before the round trip that would have filled it in. One
     * place to add a field rather than the four that build panels.
     *
     * Kept in step with `server/schema.normalize_scalebar` and
     * `normalize_colorbar` by `test_the_client_defaults_match_the_servers`.
     */
    defaultFurniture(overrides) {
        return {
            scalebar: {
                visible: false, target_um: null, unit: "auto",
                position: "bottom_right", color: "#ffffff",
                thickness_mm: 0.8, margin_mm: 1.2,
                label: true, label_size_pt: null,
            },
            colorbar: {
                visible: false, orientation: "horizontal",
                position: "bottom_left", thickness_mm: 1.6, gap_mm: 1.0,
                margin_mm: 1.2, ticks: 2, tick_color: "#ffffff",
                tick_width_pt: 0.5, tick_length_mm: 0.8, label_size_pt: null,
            },
            labels: [],
            legend: { channels: false },
            ...(overrides || {}),
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

    /**
     * The source region a panel of THIS shape should show.
     *
     * A panel is captured at one proportion and then dragged into another --
     * a square field cropped into a wide strip is the commonest thing anybody
     * does to a figure. Reopening it for editing has to frame the shape the
     * panel is NOW, not the shape it was captured at, or the edit would quietly
     * put back a proportion the user deliberately changed.
     *
     * Centre and WIDTH are preserved and the height follows the panel's aspect.
     * Width rather than height because the user framed the field by what is
     * across it: re-deriving the width would move the left and right edges they
     * chose, which is the part of the framing they can see.
     *
     * Used by both routes into editing -- Quick Edit's mini viewer and the main
     * viewer's outline -- so the two cannot disagree about what a panel is
     * looking at.
     *
     * Pure, and `image` is optional: with it the result is nudged back inside
     * the slide rather than hanging over the edge.
     */
    aspectViewport(viewport, aspect, image) {
        const width = viewport.w;
        const height = aspect > 0 ? width / aspect : viewport.h;
        let x = viewport.x;
        let y = viewport.y + viewport.h / 2 - height / 2;

        if (image && image.width > 0 && image.height > 0) {
            // Slid back inside where it fits, and left alone where it does not:
            // a region wider than the slide is a legitimate thing to have
            // captured at the edge of a small image, and shrinking it here
            // would change the field rather than move it.
            if (width <= image.width) x = Math.max(0, Math.min(x, image.width - width));
            if (height <= image.height) y = Math.max(0, Math.min(y, image.height - height));
        }
        return { x: x, y: y, w: width, h: height };
    },

    /**
     * One panel's channel settings, re-expressed for another panel's source.
     *
     * Copying rendering between two panels of the SAME image is an identity:
     * the keys are the same keys. Between two images it is a question about
     * names, because a key is a path inside one file -- "channel 3" of one
     * slide and of another are not the same stain, and applying a window by
     * position would put a nuclear channel's contrast on whatever happened to
     * be third in the other file.
     *
     * So the match is by DISPLAY NAME, with the key as a fallback for the
     * sources that carry no names. What cannot be matched is reported rather
     * than dropped silently: "this panel now shows two of your four channels"
     * is something the user has to be told, and a panel that quietly lost a
     * marker looks exactly like one that never had it.
     *
     * Pure. `sourceChannels` is the target source's `channels` list, which the
     * server normalises to `{key, fullname_at_capture}` (see schema.py).
     */
    mapRenderingChannels(channels, sourceChannels) {
        const byKey = new Map();
        const byName = new Map();
        for (const channel of sourceChannels || []) {
            if (!channel.key) continue;
            if (!byKey.has(channel.key)) byKey.set(channel.key, channel);
            const name = channel.fullname_at_capture || channel.key;
            // First wins: two channels sharing a display name make the name
            // ambiguous, and picking the later one would make the answer depend
            // on the order the file happened to list them in.
            if (!byName.has(name)) byName.set(name, channel);
        }
        const mapped = [];
        const skipped = [];
        for (const channel of channels || []) {
            const name = channel.fullname_at_capture || channel.key;
            const match = byName.get(name) || byKey.get(channel.key);
            if (!match) {
                skipped.push(name);
                continue;
            }
            mapped.push({
                key: match.key,
                fullname_at_capture: match.fullname_at_capture || match.key,
                color: { ...channel.color },
                // Raw units on both sides -- a window means the same thing in
                // any panel of any image, which is what makes this copyable.
                window: [channel.window[0], channel.window[1]],
                visible: channel.visible !== false,
            });
        }
        return { channels: mapped, skipped: skipped };
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

    /** What one of each scale-bar unit is worth in microns, and how it prints. */
    SCALEBAR_UNITS: {
        nm: { um: 0.001, text: "nm" },
        um: { um: 1, text: "µm" },
        mm: { um: 1000, text: "mm" },
    },

    /**
     * A length in microns, written the way the panel asks for.
     *
     * "auto" (and anything unrecognised) is the original behaviour and the
     * default. Naming a unit instead is what makes a row of panels comparable
     * at a glance: "500 µm" beside "1000 µm" rather than beside "1 mm", which
     * a reader has to convert before they can compare.
     *
     * `compose.format_microns`'s mirror.
     */
    formatMicrons(value, unit) {
        if (!(value > 0)) return "";
        const named = this.SCALEBAR_UNITS[unit];
        if (named) {
            const scaled = value / named.um;
            const text = scaled < 100
                ? String(Number(scaled.toFixed(2))) : String(Math.round(scaled));
            return text + " " + named.text;
        }
        if (value >= 1000) return (value / 1000).toFixed(value % 1000 ? 1 : 0) + " mm";
        if (value >= 10) return Math.round(value) + " µm";
        return value.toFixed(1) + " µm";
    },

    /**
     * A raw intensity, written for a colour bar's tick.
     *
     * Plain integers all the way up: these are 16-bit camera counts, and
     * "20000" is what a reader recognises where "2.0e+04" is a number nobody
     * would type into an acquisition setting. `compose.format_intensity`.
     */
    formatIntensity(value) {
        const magnitude = Math.abs(value);
        if (magnitude >= 10 || value === Math.round(value)) return String(Math.round(value));
        if (magnitude >= 0.01) return String(Number(value.toFixed(2)));
        return value.toExponential(1);
    },

    /**
     * Black to a channel's colour, as CSS stops.
     *
     * Multiplied by the renderer's own alpha (`FigurePanelCompositor`'s
     * CHANNEL_ALPHA, which is frag.glsl's) so that the bright end of the bar is
     * the brightest pixel the panel can actually contain, rather than a colour
     * the picture never shows. `compose._ramp`.
     */
    channelRamp(colour) {
        const alpha = 0.9;
        const at = (t) => `rgb(${Math.round(colour.r * t * alpha)},`
            + `${Math.round(colour.g * t * alpha)},${Math.round(colour.b * t * alpha)})`;
        return [at(0), at(1)];
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
