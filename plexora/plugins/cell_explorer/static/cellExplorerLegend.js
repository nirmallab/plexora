/**
 * cellExplorerLegend.js - the category list.
 *
 * Every label here is data. It comes out of somebody's table, it may contain
 * `CD8+ T cell`, `Tumor/Stroma`, quotes, slashes or anything unicode offers, and
 * it reaches the DOM through `textContent` only -- never `innerHTML`, and never
 * as an id or a CSS selector. Rows are matched by index held on the element, so
 * a category called `#tumor` cannot become a selector that means something else.
 *
 * Two distinctions the panel depends on:
 *
 * **Search filters the legend, not the image.** Typing "T" shows the rows that
 * match and changes nothing about which cells are drawn. Conflating the two is
 * the obvious shortcut and it is wrong -- it makes the picture change while the
 * user is looking for something in a list.
 *
 * **The row is the button.** Clicking anywhere on it except the colour swatch
 * shows or hides that category. The eye at the end still reports the state and
 * is still what a screen reader and the keyboard reach, but aiming at a
 * small target at the far end of a row is not what this list is for.
 *
 * **Hiding is not the same as having no value.** Missing values get their own
 * row, last, in a neutral colour. They can be hidden like any other, but they
 * are never folded into a real category -- turning None into "0" invents a
 * finding.
 *
 * Colours are picked with core's ColorSwatchPicker, the same control the image
 * channels use: ten one-click swatches for the common case and a native colour
 * input behind "Custom" for an exact value. Picking a colour is the same
 * gesture everywhere in the viewer, and this panel does not get an opinion of
 * its own about how that gesture works.
 */
class CellExplorerLegend {

    /**
     * Rows built at a time. The list scrolls inside the panel past a dozen
     * (see .cex-legend), so how many exist and how many are built are separate
     * questions -- and they have to be. Every row carries a colour picker that
     * parks a popover on <body> and subscribes to document, and an
     * identifier-like column has a category per cell: built in one go, that is
     * tens of thousands of each, rebuilt on every keystroke of the filter. The
     * next lot is built as the list is scrolled toward its end.
     */
    static PAGE_ROWS = 200;

    /** How close to the end of what is built, in pixels, the next lot starts. */
    static PAGE_AHEAD_PX = 400;

    constructor(container, handlers) {
        this.container = container;
        this.handlers = handlers;
        this.filter = "";
        //: One per rendered row. Each holds a popover parked on <body> and a
        //: pair of document listeners, so they have to be handed back before
        //: the rows that own them are thrown away -- and this list re-renders
        //: on every keystroke of the filter.
        this.pickers = [];
        //: Every row the filter keeps, and how many of them are on the page.
        this.rows = [];
        this.built = 0;
        //: Every cell in the column, which a count's tooltip gives its share of.
        this.total = 0;
        //: Which list the last render drew (column and filter). A render of the
        //: SAME list is a row being acted on under the pointer -- hidden,
        //: recoloured -- and keeps its scroll and its built rows rather than
        //: jumping back to the top of the first page.
        this.listKey = null;
        container?.addEventListener("scroll", () => this.buildMoreIfNear());
    }

    setFilter(text) {
        this.filter = String(text || "").trim().toLowerCase();
    }

    /**
     * @param descriptor the column's server descriptor
     * @param entry      { colors, hidden } for this column
     */
    render(descriptor, entry) {
        const container = this.container;
        if (!container) return;
        const key = descriptor ? `${descriptor.name}\u0000${this.filter}` : null;
        const same = key !== null && key === this.listKey;
        const keepBuilt = same ? this.built : 0;
        const keepScroll = same ? container.scrollTop : 0;
        this.listKey = key;

        this.releasePickers();
        container.textContent = "";
        this.rows = [];
        this.built = 0;
        if (!descriptor) return;

        this.total = Number(descriptor.n) || 0;
        const hidden = new Set(entry.hidden || []);
        const rows = (descriptor.categories || []).map((category, index) => ({
            label: category.value,
            count: category.count,
            color: entry.colors?.[category.value]
                || CellExplorerColors.defaultCategoryColor(index),
            hidden: hidden.has(category.value),
        }));

        // Missing values, always last and always in their own row. A count of
        // zero means there are none, and the row is simply absent -- an empty
        // "Unassigned" row is a row that explains nothing.
        if (descriptor.n_missing > 0) {
            const label = CellExplorerColors.UNASSIGNED_LABEL;
            rows.push({
                label,
                count: descriptor.n_missing,
                color: entry.colors?.[label] || CellExplorerColors.UNASSIGNED,
                hidden: hidden.has(label),
                missing: true,
            });
        }

        const matching = this.filter
            ? rows.filter((row) => row.label.toLowerCase().includes(this.filter))
            : rows;

        if (!matching.length) {
            const empty = document.createElement("p");
            empty.className = "cex-empty";
            empty.textContent = rows.length
                ? `No categories match "${this.filter}".`
                : "This column has no values to show.";
            container.appendChild(empty);
            return;
        }

        this.rows = matching;
        this.buildRows(Math.max(keepBuilt, CellExplorerLegend.PAGE_ROWS));
        if (keepScroll) container.scrollTop = keepScroll;
    }

    /** Put rows on the page up to `end`, after the ones already there. */
    buildRows(end) {
        const last = Math.min(end, this.rows.length);
        for (let index = this.built; index < last; index += 1) {
            this.container.appendChild(this.buildRow(this.rows[index]));
        }
        this.built = last;
    }

    /** The scroll handler: the next lot, once the end of what is built is in
     *  sight. A Tab onto the last built row scrolls the list too, so the
     *  keyboard reaches the rest the same way the wheel does. */
    buildMoreIfNear() {
        if (this.built >= this.rows.length) return;
        const list = this.container;
        const left = list.scrollHeight - list.scrollTop - list.clientHeight;
        if (left > CellExplorerLegend.PAGE_AHEAD_PX) return;
        this.buildRows(this.built + CellExplorerLegend.PAGE_ROWS);
    }

    buildRow(row) {
        const element = document.createElement("div");
        element.className = row.hidden ? "cex-row cex-row-hidden" : "cex-row";
        // The whole row toggles the category, not just the eye. Hiding a
        // category is the thing people do most in this list and the eye is a
        // small target at the far end of it -- a row of them is a row of
        // small targets to aim at one after another. The eye stays as the
        // control that says what the state IS; this is where it is clicked.
        element.addEventListener("click", (event) => {
            // The swatch is the one part of the row that means something else.
            // Its own button stops the click already; this covers the mount
            // around it, so aiming just beside the swatch does not toggle.
            if (event.target.closest?.(".cex-swatch")) return;
            this.handlers.onVisibility?.(row.label, !row.hidden);
        });

        // Core's picker, so a cell category is coloured with the same gesture
        // as an image channel: quick swatches for the common case, a native
        // colour input behind "Custom" when an exact value is wanted.
        const swatch = document.createElement("div");
        swatch.className = "cex-swatch";
        this.pickers.push(new ColorSwatchPicker(swatch, {
            value: row.color,
            presets: CellExplorerColors.SWATCH_PRESETS,
            title: `Colour for ${row.label}`,
            onChange: (hex) => this.handlers.onColor?.(row.label, hex),
        }));

        const label = document.createElement("span");
        label.className = "cex-label";
        // textContent, and the full text as a tooltip: long annotations are
        // truncated by CSS rather than allowed to widen the sidebar.
        label.textContent = row.label;
        label.title = row.label;

        const count = document.createElement("span");
        count.className = "cex-count";
        count.textContent = CellExplorerLegend.formatCount(row.count);
        // The compact figure is for scanning down the column; the exact one,
        // and what share of the cells it is, are a hover away.
        count.title = CellExplorerLegend.describeCount(row.count, this.total);

        // A button, not a checkbox styled as an eye: hidden state is announced
        // by aria-pressed, so it is not conveyed by colour alone. It carries no
        // handler of its own -- activating it, by pointer or by keyboard,
        // dispatches a click that reaches the row, and two handlers for one
        // gesture would toggle twice.
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "cex-visibility";
        toggle.setAttribute("aria-pressed", row.hidden ? "false" : "true");
        toggle.title = row.hidden ? `Show ${row.label}` : `Hide ${row.label}`;
        toggle.setAttribute("aria-label", toggle.title);
        // The outlined eye: a column of solid ones down the right edge of the
        // list outweighed the names it belongs to.
        const icon = document.createElement("span");
        icon.className = row.hidden ? "far fa-eye-slash" : "far fa-eye";
        icon.setAttribute("aria-hidden", "true");
        toggle.appendChild(icon);

        element.append(swatch, label, count, toggle);
        return element;
    }

    /**
     * Hand back every picker this list built.
     *
     * Called before each render and once on teardown. Without it the popovers
     * pile up on <body> and their document listeners with them -- one set per
     * row per keystroke of the filter, which is a leak that grows with how much
     * the panel is used rather than with how much data it has.
     */
    releasePickers() {
        this.pickers.forEach((picker) => picker.destroy());
        this.pickers = [];
    }

    destroy() {
        this.releasePickers();
    }

    /**
     * Compact enough to sit in a narrow sidebar without wrapping: 13, 5.4K,
     * 190K, 201.7K, 1.2M. One decimal, dropped when it is a zero, so every
     * figure in the column is at most six characters wide.
     *
     * The thresholds sit just under each thousand rather than on it, so that
     * 999,960 reads "1M" instead of rounding up to "1000K".
     */
    static formatCount(count) {
        const value = Number(count) || 0;
        const compact = (scaled, unit) => `${Number(scaled.toFixed(1))}${unit}`;
        if (value < 1000) return String(value);
        if (value < 999_950) return compact(value / 1e3, "K");
        if (value < 999_950_000) return compact(value / 1e6, "M");
        return compact(value / 1e9, "B");
    }

    /** "5,412 cells (2.7%)": the exact figure behind a compact one. */
    static describeCount(count, total) {
        const value = Number(count) || 0;
        const cells = `${value.toLocaleString()} ${value === 1 ? "cell" : "cells"}`;
        if (!(total > 0)) return cells;
        const share = (100 * value) / total;
        const percent = share >= 10 ? share.toFixed(0)
            : share >= 0.1 ? share.toFixed(1)
                : share > 0 ? "<0.1" : "0";
        return `${cells} (${percent}%)`;
    }
}
