/**
 * cellExplorerFigureBridge.js - what Cell Explorer tells a figure about itself.
 *
 * Two DOM events and no import in either direction, exactly as
 * cellExplorerRoiBridge.js already does between this plugin and ROI: a build
 * with one and not the other has to work, and a listener nobody dispatches to
 * simply never fires.
 *
 *   plexora:figure-capture-state   "describe what you are drawing"
 *   plexora:figure-restore-state   "here is what you were drawing"
 *
 * ## What is restored, and what deliberately is not
 *
 * Which COLUMN is showing can be restored without persisting -- `select()`
 * takes `{persist: false}`, which exists precisely so a column can be shown
 * without becoming the project's remembered choice. That is the part that
 * matters: a panel captured on a phenotype map reopens on the phenotype map.
 *
 * The palette, the hidden rows and the manual range are PERSISTED preferences.
 * Applying them would be an edit to the project's own Cell Explorer settings,
 * made because the user looked at a figure panel -- which is the thing a figure
 * must never do. So they are reported rather than applied, and the figure says
 * which part the live viewer is not reproducing.
 *
 * The legend is computed at capture time and stored with the panel, so an
 * export on a machine where this plugin is not installed still carries the
 * right labels and colours. Recomputing it later from a palette that has since
 * changed would produce a legend that disagrees with the panel above it.
 */
(function () {
    const PLUGIN_NAME = "cell_explorer";

    function record() {
        return window.__plexora?.plugins?.get(PLUGIN_NAME) || null;
    }

    function controller() {
        return record()?.sidebarController || null;
    }

    /** The legend rows for whatever is showing: one per category, or a ramp. */
    function legendFor(state) {
        const column = state.column;
        if (!column) return [];
        const descriptor = state.descriptor(column);
        if (!descriptor) return [];

        if (state.kindFor(column) === "continuous") {
            const entry = state.continuous(column);
            const ramp = CellExplorerColors.ramp(entry.palette, entry.custom) || [];
            return [{
                kind: "continuous",
                label: column,
                // A handful of stops rather than all 256: this is drawn as a
                // gradient, and a document that carried a quarter of a kilobyte
                // of hex per continuous panel would be paying for precision
                // nobody can see.
                ramp: sampleRamp(ramp, 16),
                domain: state.domainFor(column),
            }];
        }

        const hidden = state.hiddenSet(column);
        const colors = state.categorical(column).colors || {};
        return state.allLabels(column)
            .filter((label) => !hidden.has(label))
            .map((label, index) => ({
                kind: "categorical",
                label: String(label),
                color: colors[label] || CellExplorerColors.defaultCategoryColor(index),
            }));
    }

    function sampleRamp(ramp, count) {
        if (!ramp.length) return [];
        const out = [];
        for (let i = 0; i < count; i += 1) {
            const stop = ramp[Math.round((i / (count - 1)) * (ramp.length - 1))];
            out.push(Array.isArray(stop) ? CellExplorerColors.toHex(stop) : String(stop));
        }
        return out;
    }

    window.addEventListener("plexora:figure-capture-state", (event) => {
        const cell = controller();
        const state = cell?.state;
        if (!state || !state.column) return;   // nothing showing: contribute nothing

        const column = state.column;
        const kind = state.kindFor(column);
        event.detail?.contribute?.(PLUGIN_NAME, {
            version: record()?.definition?.version || "",
            state: {
                column: column,
                kind: kind,
                display: { ...state.settings.display },
                // The per-column preferences, carried so a future build can
                // apply them somewhere they are not the project's own state --
                // a server-side export render, for one.
                categorical: kind === "categorical" ? { ...state.categorical(column) } : null,
                continuous: kind === "continuous" ? { ...state.continuous(column) } : null,
            },
            legend: legendFor(state),
        });
    });

    window.addEventListener("plexora:figure-restore-state", (event) => {
        const wanted = event.detail?.plugins?.[PLUGIN_NAME];
        const report = event.detail?.report;
        if (!wanted) return;

        const cell = controller();
        if (!cell) return;   // not open; Figure Builder records "skipped" for us

        const state = wanted.state || {};
        if (!state.column) {
            report?.(PLUGIN_NAME, "ok");
            return;
        }

        // Without persisting: showing a column for a figure panel is not the
        // same as choosing it for the project.
        const alreadyShowing = cell.state?.column === state.column;
        if (!alreadyShowing) cell.select(state.column, { persist: false });

        // The palette and the hidden rows are the project's own settings and
        // are left alone. "partial" is the honest answer whenever they differ.
        const current = cell.state?.categorical?.(state.column);
        const wantedHidden = (state.categorical?.hidden || []).join("|");
        const currentHidden = (current?.hidden || []).join("|");
        report?.(PLUGIN_NAME, wantedHidden === currentHidden ? "ok" : "partial");
    });
})();
