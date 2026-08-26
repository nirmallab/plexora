/**
 * FigureViewOptions - rulers, grid, margins, snapping and the unit.
 *
 * Everything under the View menu, and nothing else. It is one file because it
 * is one idea: these are all statements about how the page is DRAWN for this
 * person on this machine, none of them touches the figure, and none of them
 * belongs in the document -- two people opening the same figure should not
 * inherit each other's rulers.
 *
 * So they live in localStorage, which also settles what happens on a new
 * machine: the defaults below, which are the ones that get out of the way.
 * Rulers and the grid are off, margins and smart guides are on, because the
 * first two are reference and the second two are what stops a figure being
 * subtly misaligned.
 *
 * ## Why the grid is a page child and the rulers are not
 *
 * The grid and the margins are drawn inside `.fb-page`, so they scale with it
 * and a 5 mm square stays 5 mm at every zoom. The rulers cannot be: they are
 * fixed to the viewport and have to redraw on scroll as well as on zoom, which
 * is why they are `<canvas>` -- an A4 page at 1 mm resolution is 210 ticks, and
 * 210 positioned elements rebuilt on every scroll event is a page that stutters.
 */
class FigureViewOptions {

    static get STORAGE_KEY() { return "plexora.figure_builder.view"; }

    /** Height of the horizontal ruler and width of the vertical one, in CSS px. */
    static get RULER_SIZE() { return 20; }

    static get DEFAULTS() {
        return {
            rulers: false,
            grid: false,
            snapGrid: false,
            smartGuides: true,
            margins: true,
            units: "mm",
            gridMm: 5,
        };
    }

    /** How many of the figure's millimetres one unit is, and how it is written. */
    static get UNITS() {
        return {
            mm: { per: 1, label: "mm", step: 10, minor: 5 },
            in: { per: 25.4, label: "in", step: 1, minor: 0.25 },
            pt: { per: 25.4 / 72, label: "pt", step: 72, minor: 18 },
        };
    }

    constructor(options) {
        this.workspace = options.workspace;
        this.canvas = options.canvas;
        this.prefs = { ...FigureViewOptions.DEFAULTS, ...this.read() };
    }

    read() {
        try {
            return JSON.parse(window.localStorage.getItem(FigureViewOptions.STORAGE_KEY)) || {};
        } catch (error) {
            // Private-browsing modes throw rather than returning null. Losing a
            // preference is a small inconvenience; throwing here would take the
            // whole workspace down with it.
            return {};
        }
    }

    write() {
        try {
            window.localStorage.setItem(
                FigureViewOptions.STORAGE_KEY, JSON.stringify(this.prefs));
        } catch (error) {
            /* see read() */
        }
    }

    setup() {
        this._onScroll = () => this.drawRulers();
        this.workspace.el("fb_canvas_scroll")
            ?.addEventListener("scroll", this._onScroll, { passive: true });
        this._onResize = () => this.apply();
        window.addEventListener("resize", this._onResize);
        this.apply();
    }

    destroy() {
        this.workspace.el("fb_canvas_scroll")
            ?.removeEventListener("scroll", this._onScroll);
        window.removeEventListener("resize", this._onResize);
    }

    // -- the menu ------------------------------------------------------------

    menuEntries() {
        const unit = FigureViewOptions.UNITS[this.prefs.units];
        return [
            { act: "rulers", label: "Rulers", checked: this.prefs.rulers },
            { act: "grid", label: "Grid", checked: this.prefs.grid },
            { act: "snapGrid", label: "Snap to grid", checked: this.prefs.snapGrid },
            { act: "smartGuides", label: "Smart guides", checked: this.prefs.smartGuides },
            { act: "margins", label: "Show margins", checked: this.prefs.margins },
            { separator: true },
            // A submenu, not a cycle. Cycling was defended here as saving a
            // click, and the note under `setUnit` -- written later, for the
            // Transform popover -- had already conceded the case against it: "a
            // field whose unit changes only by repeated clicking somewhere else
            // is a field you cannot set". Worse, it meant there were two
            // different ways of choosing the same setting, and the discoverable
            // one was the one that could not be aimed. Naming the three is one
            // extra click and no guessing.
            { act: "units", label: `Units: ${unit.label}` },
        ];
    }

    // "Page background…" is not in that list any more. It had three homes -- the
    // page menu, this menu, and the canvas right-click -- for one setting about
    // one page, and the page menu is where the rest of a page's settings are.
    // The right-click row stays, because it is a projection of the same call
    // rather than a fourth copy of the decision.

    pick(act) {
        if (act === "units") {
            this.workspace.openUnits();
            return;
        } else if (act in this.prefs) {
            this.prefs[act] = !this.prefs[act];
        } else {
            return;
        }
        this.write();
        this.apply();
    }

    /**
     * Set the unit outright.
     *
     * For a control that NAMES the three rather than cycling them -- the
     * Transform popover has room for a select, and a field whose unit changes
     * only by repeated clicking somewhere else is a field you cannot set.
     * Same preference, same storage: there is one answer to "what is this page
     * measured in", and the rulers and this popover both read it.
     */
    setUnit(name) {
        if (!(name in FigureViewOptions.UNITS) || name === this.prefs.units) return;
        this.prefs.units = name;
        this.write();
        this.apply();
    }

    // -- drawing ---------------------------------------------------------------

    /** Push the preferences at everything that renders from them. */
    apply() {
        const main = this.workspace.el("fb_main");
        main?.classList.toggle("has-rulers", this.prefs.rulers);

        this.canvas.snapping = {
            guides: this.prefs.smartGuides,
            grid: this.prefs.snapGrid,
            gridMm: this.prefs.gridMm,
        };

        this.drawGrid();
        this.drawMargins();
        this.drawRulers();
    }

    /**
     * The grid, as a repeating gradient rather than as lines.
     *
     * One element and one paint, whatever the page size. Drawing it as elements
     * would be four hundred divs on an A4 page at 5 mm, all of them rebuilt on
     * every zoom.
     */
    drawGrid() {
        const grid = this.workspace.el("fb_page_grid");
        if (!grid) return;
        grid.hidden = !this.prefs.grid;
        if (!this.prefs.grid) return;
        const step = this.canvas.toPx(this.prefs.gridMm);
        grid.style.backgroundSize = `${step}px ${step}px`;
    }

    drawMargins() {
        const element = this.workspace.el("fb_page_margins");
        const page = this.canvas.page;
        if (!element) return;
        element.hidden = !this.prefs.margins || !page;
        if (element.hidden) return;
        const margins = page.margins_mm;
        element.style.left = this.canvas.toPx(margins.left) + "px";
        element.style.top = this.canvas.toPx(margins.top) + "px";
        element.style.right = this.canvas.toPx(margins.right) + "px";
        element.style.bottom = this.canvas.toPx(margins.bottom) + "px";
    }

    /**
     * Both rulers, from where the page actually is on screen.
     *
     * Measured rather than computed from the scroll offset: the page is centred
     * with `margin: auto` when it is narrower than the viewport, so its left
     * edge is not a function of scrollLeft alone. Measuring is exact at every
     * zoom and costs one layout read.
     */
    drawRulers() {
        if (!this.prefs.rulers) return;
        const scroll = this.workspace.el("fb_canvas_scroll");
        const pageEl = this.workspace.el("fb_page");
        const page = this.canvas.page;
        if (!scroll || !pageEl || !page) return;

        const view = scroll.getBoundingClientRect();
        const paper = pageEl.getBoundingClientRect();
        this.paintRuler(this.workspace.el("fb_ruler_h"), "h",
                        paper.left - view.left, view.width, page.size_mm.w);
        this.paintRuler(this.workspace.el("fb_ruler_v"), "v",
                        paper.top - view.top, view.height, page.size_mm.h);
    }

    paintRuler(element, axis, originPx, lengthPx, spanMm) {
        if (!element) return;
        const dpr = window.devicePixelRatio || 1;
        const thickness = FigureViewOptions.RULER_SIZE;
        const width = axis === "h" ? lengthPx : thickness;
        const height = axis === "h" ? thickness : lengthPx;
        element.width = Math.max(1, Math.round(width * dpr));
        element.height = Math.max(1, Math.round(height * dpr));
        element.style.width = width + "px";
        element.style.height = height + "px";

        const context = element.getContext("2d");
        if (!context) return;
        context.setTransform(dpr, 0, 0, dpr, 0, 0);
        context.clearRect(0, 0, width, height);
        // Ink on a white strip. These were white when the workspace was dark;
        // a canvas draws with literal colours and knows nothing about the
        // stylesheet, so the theme change has to be made here by hand.
        context.fillStyle = "rgba(22, 32, 46, 0.62)";
        context.strokeStyle = "rgba(22, 32, 46, 0.38)";
        context.font = "9px system-ui, sans-serif";
        context.lineWidth = 1;

        const unit = FigureViewOptions.UNITS[this.prefs.units];
        const perUnitPx = this.canvas.toPx(unit.per);
        // Skip labels that would overlap: at 25% zoom every 10 mm is four
        // pixels apart, and a ruler of overlapping numbers is worse than none.
        const labelEvery = Math.max(unit.step, unit.step * Math.ceil(28 / (perUnitPx * unit.step)));
        const minorEvery = perUnitPx * unit.minor >= 4 ? unit.minor : labelEvery;

        const spanUnits = spanMm / unit.per;
        for (let value = 0; value <= spanUnits + 1e-6; value += minorEvery) {
            const at = originPx + value * perUnitPx;
            if (at < -20 || at > lengthPx + 20) continue;
            const major = Math.abs(value % labelEvery) < 1e-6;
            const tick = major ? thickness * 0.6 : thickness * 0.3;
            context.beginPath();
            if (axis === "h") {
                context.moveTo(Math.round(at) + 0.5, thickness - tick);
                context.lineTo(Math.round(at) + 0.5, thickness);
            } else {
                context.moveTo(thickness - tick, Math.round(at) + 0.5);
                context.lineTo(thickness, Math.round(at) + 0.5);
            }
            context.stroke();
            if (!major) continue;
            const text = String(Math.round(value * 100) / 100);
            if (axis === "h") {
                context.fillText(text, at + 2, 9);
            } else {
                context.save();
                context.translate(9, at - 2);
                context.rotate(-Math.PI / 2);
                context.fillText(text, 0, 0);
                context.restore();
            }
        }
    }
}
