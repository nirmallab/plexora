/**
 * cellExplorerState.js - what is being shown, and what the user has chosen.
 *
 * One object holds both halves so nothing else has to reconcile them: the
 * descriptors the server sent, and the display preferences that came out of the
 * plugin store. The panel reads from here, the LUT is built from here, and the
 * autosave writes from here.
 *
 * The `generation` counter is the guard against a stale response. It is bumped
 * whenever what is being shown changes -- a new column, an overridden kind, a
 * dataset refresh -- and every in-flight request is compared against it on
 * arrival. See cellExplorerApi.js for the other half.
 *
 * Preferences are stored per column, not globally. `cluster = "1"` and
 * `grade = "1"` are different things that happen to share a label, and a colour
 * keyed only by the label would force them to look the same.
 */
class CellExplorerState {

    constructor() {
        this.generation = 0;
        this.revision = 0;
        this.settings = CellExplorerState.defaults();
        this.descriptors = [];
        this.canDraw = { segmentation: false, segmentation_pending: false, centroids: false };
        //: Which column is showing, and the values behind it. `data` is null
        //: while a fetch is in flight, which is what the panel renders its
        //: loading state from.
        this.column = null;
        this.data = null;
        this.status = "idle";
        this.error = null;
        //: Set when a preference changes and cleared when it is written. The
        //: autosave reads it so a debounce that fires with nothing to say costs
        //: no request.
        this.dirty = false;
        //: True when the stored document is from a newer Plexora. The panel
        //: still works; it must not save over it.
        this.readOnly = false;
    }

    static defaults() {
        return {
            selected: null,
            display: { mode: null, opacity: 0.7 },
            overrides: {},
            categorical: {},
            continuous: {},
        };
    }

    /** Bump and return the generation a request should carry. */
    nextGeneration() {
        this.generation += 1;
        return this.generation;
    }

    isCurrent(generation) {
        return generation === this.generation;
    }

    // -- descriptors --------------------------------------------------------

    /**
     * One column's descriptor, by name.
     *
     * Indexed rather than scanned. The dropdown asks for a hint and a warning
     * per option, per keystroke of its filter, so a linear scan here is
     * quadratic in the width of the table -- which used to be a short list and
     * is now every metadata column there is. The index is rebuilt whenever the
     * array is replaced, which is the only way descriptors ever change.
     */
    descriptor(column) {
        if (this._index?.source !== this.descriptors) {
            this._index = {
                source: this.descriptors,
                byName: new Map(this.descriptors.map((entry) => [entry.name, entry])),
            };
        }
        return this._index.byName.get(column) || null;
    }

    get current() {
        return this.descriptor(this.column);
    }

    /**
     * How a column is being read: the user's override if they set one,
     * otherwise the server's inference.
     *
     * An override that names something the descriptor cannot supply is ignored
     * rather than honoured -- the payload for it was never computed, and a
     * saved preference outlives the data it was made against.
     */
    kindFor(column) {
        const descriptor = this.descriptor(column);
        if (!descriptor) return null;
        const override = this.settings.overrides[column];
        if (override && override !== descriptor.kind && descriptor.ambiguous) {
            return override;
        }
        return descriptor.kind;
    }

    /** The kind to ask the server for, or null to take its own answer. */
    requestedKind(column) {
        const descriptor = this.descriptor(column);
        const kind = this.kindFor(column);
        return descriptor && kind !== descriptor.kind ? kind : null;
    }

    setOverride(column, kind) {
        if (kind) {
            this.settings.overrides[column] = kind;
        } else {
            delete this.settings.overrides[column];
        }
        this.dirty = true;
    }

    // -- per-column preferences ---------------------------------------------

    /**
     * This column's category colours and hidden set, created on first use.
     *
     * Guarded against a null column so a stray call cannot file preferences
     * under the string "null" -- which would then be written to the store and
     * come back forever, keyed to a column that does not exist.
     */
    categorical(column) {
        if (!column) return { colors: {}, hidden: [] };
        let entry = this.settings.categorical[column];
        if (!entry) {
            entry = this.settings.categorical[column] = { colors: {}, hidden: [] };
        }
        entry.colors = entry.colors || {};
        entry.hidden = entry.hidden || [];
        return entry;
    }

    /** As `categorical`, and null-guarded for the same reason.
     *
     *  `hidden` is a boolean here where a categorical entry's is a list of
     *  labels. Not an inconsistency: a ramp has no rows to hide one of, so the
     *  column is the only unit there is. */
    continuous(column) {
        if (!column) {
            return { palette: "viridis", custom: { low: null, high: null },
                     range: { mode: "auto" }, hidden: false };
        }
        let entry = this.settings.continuous[column];
        if (!entry) {
            entry = this.settings.continuous[column] = {
                palette: "viridis", custom: { low: null, high: null },
                range: { mode: "auto" }, hidden: false,
            };
        }
        entry.custom = entry.custom || { low: null, high: null };
        entry.range = entry.range || { mode: "auto" };
        entry.hidden = Boolean(entry.hidden);
        return entry;
    }

    hiddenSet(column) {
        return new Set(this.categorical(column).hidden || []);
    }

    setHidden(column, label, hidden) {
        const entry = this.categorical(column);
        const current = new Set(entry.hidden);
        if (hidden) current.add(label); else current.delete(label);
        entry.hidden = Array.from(current).sort();
        this.dirty = true;
    }

    setAllHidden(column, hidden) {
        const entry = this.categorical(column);
        entry.hidden = hidden ? this.allLabels(column).sort() : [];
        this.dirty = true;
    }

    setColor(column, label, hex) {
        const entry = this.categorical(column);
        if (hex) entry.colors[label] = hex; else delete entry.colors[label];
        this.dirty = true;
    }

    setPalette(column, palette) {
        this.continuous(column).palette = palette;
        this.dirty = true;
    }

    setCustomColor(column, end, hex) {
        this.continuous(column).custom[end] = hex;
        this.dirty = true;
    }

    /** Draw this numeric column's overlay, or not. The counterpart of the
     *  legend's per-category eye, for a kind of column that has no rows. */
    setContinuousHidden(column, hidden) {
        this.continuous(column).hidden = Boolean(hidden);
        this.dirty = true;
    }

    setRange(column, low, high) {
        this.continuous(column).range = (low === null || high === null)
            ? { mode: "auto" }
            : { mode: "manual", min: low, max: high };
        this.dirty = true;
    }

    setOpacity(value) {
        this.settings.display.opacity = value;
        this.dirty = true;
    }

    setMode(mode) {
        this.settings.display.mode = mode;
        this.dirty = true;
    }

    /**
     * The low/high a continuous column is drawn between.
     *
     * The stored manual range when there is a usable one, otherwise the
     * server's robust percentiles -- p01/p99 rather than the literal extremes,
     * because one outlying cell otherwise compresses every other cell into a
     * few percent of the ramp.
     *
     * A stored manual range that no longer makes sense against this data falls
     * back to auto rather than refusing to draw. The source can change under a
     * saved preference.
     */
    domainFor(column) {
        const stats = this.descriptor(column)?.stats || {};
        const range = this.continuous(column).range;
        if (range.mode === "manual"
            && Number.isFinite(range.min) && Number.isFinite(range.max)
            && range.min < range.max) {
            return [range.min, range.max];
        }
        const low = Number.isFinite(stats.p01) ? stats.p01 : stats.min;
        const high = Number.isFinite(stats.p99) ? stats.p99 : stats.max;
        return [low, high];
    }

    isAutoRange(column) {
        return this.continuous(column).range.mode !== "manual";
    }

    /**
     * The lookup table for whatever is currently showing, or null.
     *
     * Everything the user can change without new data lands here: this is
     * rebuilt and handed to core, and nothing is refetched.
     */
    buildLUT() {
        const column = this.column;
        const data = this.data;
        if (!column || !data) return null;

        if (data.kind === "continuous") {
            const entry = this.continuous(column);
            return CellExplorerColors.buildLUT({
                kind: "continuous",
                ids: data.ids,
                values: data.values,
                domain: this.domainFor(column),
                palette: entry.palette,
                custom: entry.custom,
                blank: entry.hidden,
            });
        }

        const entry = this.categorical(column);
        return CellExplorerColors.buildLUT({
            kind: "categorical",
            ids: data.ids,
            codes: data.codes,
            categories: this.descriptor(column)?.categories || [],
            colors: entry.colors,
            hidden: this.hiddenSet(column),
        });
    }

    /** What gets written to the store. Only preferences -- never values. */
    toSettings() {
        return {
            selected: this.column,
            display: { ...this.settings.display },
            overrides: { ...this.settings.overrides },
            categorical: this.settings.categorical,
            continuous: this.settings.continuous,
        };
    }

    /**
     * Adopt what came back from the store.
     *
     * Unknown columns are kept rather than filtered against the current table:
     * a column that is temporarily absent comes back, and dropping the colours
     * somebody chose the one time it was missing throws away deliberate work.
     */
    adopt(stored) {
        const defaults = CellExplorerState.defaults();
        this.revision = Number.isInteger(stored?.revision) ? stored.revision : 0;
        this.readOnly = Boolean(stored?.unreadable);
        this.settings = {
            selected: stored?.selected ?? defaults.selected,
            display: { ...defaults.display, ...(stored?.display || {}) },
            overrides: { ...(stored?.overrides || {}) },
            categorical: { ...(stored?.categorical || {}) },
            continuous: { ...(stored?.continuous || {}) },
        };
        this.dirty = false;
    }

    /**
     * Whether every category is showing, none is, or it is a mix.
     *
     * The All/None control reads this: it reports what the legend is in as well
     * as offering to change it, and "mixed" -- neither lit -- is the ordinary
     * state once anybody has hidden a single row.
     */
    visibilityState(column) {
        const labels = this.allLabels(column);
        if (!labels.length) return "all";
        const hidden = this.hiddenSet(column);
        const count = labels.filter((label) => hidden.has(label)).length;
        if (count === 0) return "all";
        return count === labels.length ? "none" : "mixed";
    }

    /** Every row the legend can show, including Unassigned when there is one. */
    allLabels(column) {
        const descriptor = this.descriptor(column);
        if (!descriptor) return [];
        const labels = (descriptor.categories || []).map((entry) => entry.value);
        if (descriptor.n_missing) labels.push(CellExplorerColors.UNASSIGNED_LABEL);
        return labels;
    }

    /**
     * Which column to open on.
     *
     * In order: what was showing last time, if the table still has it; the
     * project's annotation column, which is what a cell-type column is for; the
     * first categorical that was not a guess; the first anything.
     *
     * A saved selection is honoured whatever it is -- including an
     * identifier-like column, which somebody picked on purpose the last time
     * they were here. The automatic steps skip those: opening the panel and
     * finding a million colours drawn from a barcode column is not a default
     * anybody would have asked for. If every column looks like an identifier,
     * nothing is chosen and the user picks.
     */
    chooseColumn(celltypeColumn) {
        const saved = this.settings.selected;
        if (saved && this.descriptors.some((entry) => entry.name === saved)) return saved;

        const usable = this.descriptors.filter((entry) => !entry.identifier_like);
        const celltype = usable.find(
            (entry) => entry.name === celltypeColumn && entry.kind === "categorical");
        if (celltype) return celltype.name;

        const confident = usable.find(
            (entry) => entry.kind === "categorical" && !entry.ambiguous);
        if (confident) return confident.name;

        return usable.length ? usable[0].name : null;
    }
}
