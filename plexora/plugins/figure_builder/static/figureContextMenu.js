/**
 * FigureContextMenu - the right-click, on the page and in the tray.
 *
 * The third level of the interface. Level 1 is the tool rail and the topbar
 * (always visible), level 2 is the floating bar (visible while something is
 * selected), and this is everything else: the actions that are worth having and
 * are not worth a permanent button, which is most of them.
 *
 * Having it is what lets the rest of the interface stay small. Bring to front,
 * Replace image, Copy, Delete from figure and the rest are all one click away
 * from the thing they act on, and none of them costs a pixel of chrome.
 *
 * ## Two deletes, and they are not the same delete
 *
 * "Remove from page" unplaces a panel: it goes back to the tray with its
 * captured scene intact. "Delete from figure" destroys it. The distinction is
 * load-bearing -- a captured scene can be the only record of a view somebody
 * spent an hour finding -- so the destructive one is further away, worded as
 * what it does, and asks first.
 */
class FigureContextMenu {

    constructor(options) {
        this.state = options.state;
        this.canvas = options.canvas;
        this.handlers = options.handlers || {};
        this.el = null;
        //: Where the menu was opened, in page millimetres. Paste puts things
        //: where the user right-clicked, which is the only place they could
        //: have meant.
        this.point = null;
    }

    setup(surfaceEl, trayEl) {
        this._onSurface = (event) => this.openForCanvas(event);
        surfaceEl?.addEventListener("contextmenu", this._onSurface);
        this._onTray = (event) => this.openForTray(event);
        trayEl?.addEventListener("contextmenu", this._onTray);

        this._onDismiss = (event) => {
            if (this.el && !this.el.contains(event.target)) this.close();
        };
        document.addEventListener("pointerdown", this._onDismiss, true);
        this._onScroll = () => this.close();
        window.addEventListener("scroll", this._onScroll, true);
        // Escape, which every other transient thing on this page answers to and
        // this one did not: the only way out of a right-click menu was to click
        // somewhere else, which on a canvas means clicking ON something --
        // changing the selection to dismiss a menu about the selection.
        this._onKey = (event) => {
            if (event.key !== "Escape" || !this.el) return;
            event.stopPropagation();
            this.close();
        };
        window.addEventListener("keydown", this._onKey, true);
    }

    destroy() {
        document.removeEventListener("pointerdown", this._onDismiss, true);
        window.removeEventListener("scroll", this._onScroll, true);
        window.removeEventListener("keydown", this._onKey, true);
        this.close();
    }

    // -- opening -----------------------------------------------------------

    openForCanvas(event) {
        event.preventDefault();
        this.point = this.canvas.surfacePoint(event);

        const panelEl = event.target.closest?.(".fb-panel");
        const annotationEl = event.target.closest?.(".fb-annotation");
        const id = panelEl ? panelEl.dataset.panelId
            : annotationEl ? annotationEl.dataset.annotationId : null;

        // Right-clicking something that is not in the selection selects it
        // first. A menu that acted on a selection somewhere else on the page is
        // the classic way to delete the wrong thing.
        if (id && !this.canvas.selection.has(id)) this.canvas.select([id], false);
        if (!id) this.canvas.select([], false);

        const ids = Array.from(this.canvas.selection);
        this.open(event.clientX, event.clientY,
                  ids.length ? this.selectionEntries(ids) : this.pageEntries());
    }

    openForTray(event) {
        const item = event.target.closest?.(".fb-tray-item");
        if (!item) return;
        event.preventDefault();
        this.point = null;
        this.handlers.onTrayContext?.(item.dataset.panelId);
        this.open(event.clientX, event.clientY,
                  this.trayEntries(this.handlers.traySelection?.() || [item.dataset.panelId]));
    }

    // -- what each menu holds ------------------------------------------------

    /**
     * The right-click rows, from the same registry the floating bar reads.
     *
     * It was a separate hand-built list, and the two had already drifted: this
     * one honestly disabled "Bring to front" when nothing could be reordered
     * while the bar offered it live on a text box, where it did nothing.
     */
    selectionEntries(ids) {
        const context = {
            ids: ids,
            sel: FigureSelection.describe(ids, this.state, this.canvas),
            canvas: this.canvas,
            state: this.state,
            handlers: this.handlers,
        };
        const entries = [];
        let group = null;
        // A separator at every change of GROUP, from the registry's own
        // clusters. It was one separator, at the single flip from type-specific
        // to generic, which left ten rows running together in the middle of the
        // menu -- the reordering four, the copy pair and the two deletes all in
        // one block.
        for (const action of FigureActions.forSurface("menu", context.sel, context)) {
            if (group !== null && action.group !== group) {
                entries.push({ separator: true });
            }
            group = action.group;
            entries.push({ act: action.id, label: action.label,
                           icon: action.icon, shortcut: action.shortcut,
                           disabled: !action.isEnabled, danger: action.danger });
        }
        return entries;
    }

    pageEntries() {
        return [
            { act: "paste", label: "Paste", disabled: !FigureClipboard.hasContent() },
            { separator: true },
            { act: "select_all", label: "Select everything on this page" },
            { separator: true },
            { act: "page_background", label: "Page background…" },
            { act: "page_duplicate", label: "Duplicate this page" },
        ];
    }

    trayEntries(ids) {
        return [
            { act: "tray_place", label: ids.length > 1 ? "Place all on this page" : "Place on this page" },
            { act: "tray_duplicate", label: "Duplicate" },
            { separator: true },
            { act: "tray_delete", label: "Delete from figure", danger: true },
        ];
    }

    // -- the menu itself -----------------------------------------------------

    open(clientX, clientY, entries) {
        this.close();
        this.el = document.createElement("div");
        this.el.className = "fb-menu";
        // The icon gutter is drawn only when SOME row in this menu has an icon.
        // A fixed gutter is what stops a half-iconed list having a ragged left
        // edge; in a list where no row has one at all -- the page menu, the tray
        // menu -- it is fourteen pixels of indent that says nothing.
        const gutter = entries.some((entry) => entry.icon);
        this.el.innerHTML = entries.map((entry) => entry.separator
            ? '<span class="fb-menu-separator"></span>'
            : `<button type="button" class="fb-menu-item${entry.danger ? " is-danger" : ""}"
                       data-act="${entry.act}" ${entry.disabled ? "disabled" : ""}>${gutter
                       ? `<span class="fb-menu-icon" aria-hidden="true">${entry.icon
                           ? `<span class="fas fa-${entry.icon}"></span>` : ""}</span>` : ""}
                   <span class="fb-menu-text">${FigureSchema.escapeHtml(entry.label)}</span>${
                   entry.shortcut
                       ? `<span class="fb-menu-key" aria-hidden="true">${
                           FigureSchema.escapeHtml(entry.shortcut)}</span>` : ""}</button>`
        ).join("");
        document.body.appendChild(this.el);

        // Flipped rather than clipped: a menu opened near the bottom right of
        // the window has to open up and to the left, or its last item -- which
        // is the destructive one -- is off screen.
        // offsetWidth/Height and not getBoundingClientRect: the card animates
        // in with a `scale(0.97)`, and a client rect is the TRANSFORMED box.
        // Measuring it mid-animation would under-report the size by a few
        // pixels and decide "does this fit below?" from a number that stops
        // being true 120ms later. The offset pair is the layout box, which the
        // animation never touches.
        const size = { width: this.el.offsetWidth, height: this.el.offsetHeight };
        const left = clientX + size.width > window.innerWidth - 8
            ? Math.max(8, clientX - size.width) : clientX;
        const top = clientY + size.height > window.innerHeight - 8
            ? Math.max(8, clientY - size.height) : clientY;
        this.el.style.left = Math.round(left) + "px";
        this.el.style.top = Math.round(top) + "px";

        this.el.addEventListener("click", (event) => {
            const item = event.target.closest("[data-act]");
            if (!item || item.disabled) return;
            const act = item.dataset.act;
            this.close();
            this.run(act);
        });
    }

    close() {
        this.el?.remove();
        this.el = null;
    }

    run(act) {
        const ids = Array.from(this.canvas.selection);
        const handlers = this.handlers;

        // A selection action runs from the registry. Only the entries that are
        // about the PAGE or the TRAY rather than about the selection are
        // dispatched here, because those have no selection to describe.
        const action = FigureActions.byId(act);
        if (action && action.run) {
            action.run({ ids: ids,
                         sel: FigureSelection.describe(ids, this.state, this.canvas),
                         canvas: this.canvas, state: this.state,
                         handlers: handlers });
            return;
        }
        ({
            paste: () => this.canvas.paste(this.point),
            select_all: () => this.canvas.selectAllOnPage(),
            page_background: () => handlers.onPageBackground?.(),
            page_duplicate: () => handlers.onDuplicatePage?.(),
            tray_place: () => handlers.onPlaceFromTray?.(),
            tray_duplicate: () => handlers.onDuplicateTray?.(),
            tray_delete: () => handlers.onDeleteTray?.(),
        }[act] || (() => {}))();
    }
}


/**
 * The clipboard, for this tab.
 *
 * A module singleton rather than the OS clipboard. What is copied here is a
 * captured SCENE -- a source id, a region in image pixels, a set of channels
 * and windows -- and none of that means anything in another application, or in
 * another figure that does not reference the same image. Writing it to the
 * system clipboard would offer a paste everywhere that can only fail almost
 * everywhere.
 *
 * Cross-tab paste is deliberately not built either: two tabs on one figure is
 * the case the whole revision system exists to keep honest, and a paste that
 * crossed between them would arrive against a revision it was not copied from.
 */
const FigureClipboard = {
    panels: [],
    annotations: [],

    hasContent() {
        return this.panels.length > 0 || this.annotations.length > 0;
    },

    put(panels, annotations) {
        this.panels = JSON.parse(JSON.stringify(panels || []));
        this.annotations = JSON.parse(JSON.stringify(annotations || []));
    },

    take() {
        return {
            panels: JSON.parse(JSON.stringify(this.panels)),
            annotations: JSON.parse(JSON.stringify(this.annotations)),
        };
    },
};
