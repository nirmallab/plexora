/**
 * FigureCaptureDock - the whole of Figure Builder's in-viewer chrome.
 *
 * There is no sidebar panel any more. Figure Builder started with one because
 * every plugin has one, and it was the wrong home for all of it: capturing is
 * something you do WHILE looking at the image, and a panel three hundred pixels
 * to the left is a glance away from the thing being judged. Splitting the
 * controls between the two -- capture on the image, "which figure?" in the
 * sidebar -- was worse than either, because the two halves of one decision were
 * in two places. So everything is here: the mode toggle, the instructions, the
 * session's captures, which figure they go into, the way through to the canvas,
 * the borrowed-viewer session that reopening a panel starts, and the way out.
 *
 * Mounted as a sibling of #openseadragon inside #openseadragon_wrapper -- the
 * placement miniMap.js documents: OSD binds its mouse tracker to
 * #openseadragon, so clicks on this chrome never reach the viewer and nothing
 * here needs stopPropagation. Top-right because core's own furniture is
 * elsewhere: project label top-left, overview lens bottom-left, channel legend
 * bottom-right. The legend is the one that matters -- it is in the same column
 * as this, so the dock measures the room above it and caps itself there rather
 * than growing down through it as the strip fills up. See roomFor().
 *
 * Everything here is built in JavaScript rather than rendered into a panel slot
 * for one reason: core has no slot over the image, and adding one to
 * index.html for a single plugin would put a plugin-shaped hole in a template
 * every build renders. What a plugin creates it may also style, so the classes
 * are all `fb-` and tests/test_plugin_css_boundary.py stays satisfied.
 */
class FigureCaptureDock {

    /** The one-key shortcut. C for capture; nothing in core or in the other
     *  plugins binds a bare letter, which is checked in the probe rather than
     *  assumed. */
    static get SHORTCUT() { return "c"; }

    /** Whether the strip is open. A property of this browser and this person's
     *  habits, like the current figure -- not of the figure or the project. */
    static get STORAGE_KEY() { return "plexora.figure_builder.stripOpen"; }

    /** The dock's inset from the top of the viewer, matching `top` in the
     *  stylesheet; and the clearance it keeps from whatever is below it. */
    static get MARGIN() { return 12; }
    static get GAP() { return 10; }

    /**
     * The least the dock may be squeezed to, in pixels.
     *
     * On a short window under a tall channel legend there is not enough room
     * for both, and something has to give. It is this: about the orb, the
     * strip's header and a glimpse of the list. A dock squeezed below that is
     * one the user cannot capture with at all, which is worse than a dock that
     * overlaps a legend in the one case where nothing else fits.
     */
    static get MIN_HEIGHT() { return 168; }

    /** Core's channel legend: bottom-right of the same wrapper the dock hangs
     *  in, and the only thing the dock can collide with. Read for its geometry
     *  and never touched -- it is core's element, and this is a plugin. */
    static get OBSTACLE_ID() { return "viewer_channel_legend"; }

    /**
     * How tall the dock may be.
     *
     * The strip grows with every capture, and left to itself it grew straight
     * down into the channel legend -- which is `pointer-events: none`, so the
     * captures underneath it stayed clickable and the two just illegibly
     * overlapped. Rather than a fixed cap (wrong on every window that is not
     * the one it was chosen on), the dock takes the room that is actually free
     * above whatever occupies the corner below it.
     *
     * Pure so the arithmetic can be checked without a browser.
     *
     * @param {number} hostHeight the viewer wrapper's height.
     * @param {?number} obstacleTop the top of the thing below, in the same
     *        coordinates, or null when there is nothing there.
     */
    static roomFor(hostHeight, obstacleTop) {
        const margin = FigureCaptureDock.MARGIN;
        const floor = (obstacleTop === null || obstacleTop === undefined)
            ? hostHeight - margin
            : obstacleTop - FigureCaptureDock.GAP;
        return Math.max(FigureCaptureDock.MIN_HEIGHT, Math.round(floor - margin));
    }

    /**
     * Is this keystroke the capture shortcut?
     *
     * A bare letter, so the guards are the whole of it: no modifier chord (Cmd-C
     * is copy and always will be), and nothing while the user is typing --
     * otherwise naming a figure "cell cores" toggles capture mode four times.
     */
    static isShortcut(event) {
        if (!event || event.metaKey || event.ctrlKey || event.altKey) return false;
        if (typeof event.key !== "string") return false;
        if (event.key.toLowerCase() !== FigureCaptureDock.SHORTCUT) return false;
        return !FigureCaptureTool.isTyping();
    }

    /**
     * @param {object} handlers onToggleCapture, onSelectCapture(id),
     *        onRemoveCapture(id), onNewFigure, onChooseFigure, onOpenCanvas,
     *        onUpdatePanel, onCancelEdit, onClose.
     */
    constructor(handlers) {
        this.handlers = handlers || {};
        this.root = null;
        this.open = this.readOpen();
        this._onKeyDown = (event) => {
            if (!FigureCaptureDock.isShortcut(event)) return;
            event.preventDefault();
            this.handlers.onToggleCapture?.();
        };
        this._onResize = () => this.layout();
        //: Watches the legend, which changes height whenever a channel is
        //: turned on or off -- a plugin cannot hear core's channel events, and
        //: the box changing is the thing that actually matters here.
        this._watch = null;
    }

    // -- lifecycle -------------------------------------------------------

    mount() {
        const host = this.host();
        if (this.root || !host) return false;

        const root = document.createElement("div");
        root.className = "fb-dock";
        root.innerHTML = `
            <div class="fb-dock-head">
                <div class="fb-dock-caption" data-role="caption"></div>
                <button class="fb-orb" type="button" data-role="orb"
                        aria-pressed="false" title="Capture mode (C)">
                    <span class="fas fa-crop-simple" aria-hidden="true"></span>
                    <span class="fb-visually-hidden">Capture mode</span>
                </button>
                <button class="fb-dock-close" type="button" data-role="close"
                        title="Close Figure Builder">
                    <span class="fas fa-xmark" aria-hidden="true"></span>
                    <span class="fb-visually-hidden">Close Figure Builder</span>
                </button>
            </div>

            <p class="fb-dock-error" data-role="error" hidden></p>

            <section class="fb-strip" data-role="strip">
                <button class="fb-strip-toggle" type="button" data-role="collapse"
                        aria-expanded="true">
                    <span class="fas fa-chevron-down fb-strip-chevron" aria-hidden="true"></span>
                    <span class="fb-strip-title">Captures</span>
                    <span class="fb-strip-count" data-role="count">0</span>
                </button>
                <div class="fb-strip-body">
                    <div class="fb-strip-items" data-role="items"></div>
                    <p class="fb-strip-empty" data-role="empty">Nothing captured yet.</p>
                    <div class="fb-strip-foot">
                        <span class="fb-strip-label">Capture into</span>
                        <div class="fb-strip-figure-row">
                            <button class="fb-strip-figure" type="button" data-role="choose"
                                    title="Open a figure, or start one"></button>
                            <button class="fb-strip-add" type="button" data-role="new"
                                    title="New figure">
                                <span class="fas fa-plus" aria-hidden="true"></span>
                                <span class="fb-visually-hidden">New figure</span>
                            </button>
                        </div>
                        <span class="fb-strip-meta" data-role="meta"></span>
                        <button class="fb-strip-go" type="button" data-role="canvas"></button>
                    </div>
                </div>
            </section>

            <section class="fb-dock-edit" data-role="edit" hidden>
                <div class="fb-dock-edit-head">
                    <span class="fas fa-arrow-rotate-left" aria-hidden="true"></span>
                    <span>Editing <strong data-role="editLabel"></strong></span>
                </div>
                <p class="fb-dock-edit-body">The viewer is showing this panel's captured
                    view. Adjust the channels, contrast and overlays as usual, then update
                    the panel &mdash; or cancel to put the viewer back. Your project's own
                    saved channels are not touched either way.</p>
                <div data-role="editNotes"></div>
                <div class="fb-dock-edit-actions">
                    <button class="fb-strip-go" type="button" data-role="editUpdate">Update panel</button>
                    <button class="fb-strip-figure" type="button" data-role="editCancel">Cancel</button>
                </div>
            </section>`;

        // Delegated: the items are rebuilt on every capture, so handlers bound
        // to them would be rebound continuously and leak the ones replaced.
        root.addEventListener("click", (event) => this.clicked(event));
        host.appendChild(root);
        this.root = root;
        this.applyOpen();
        this.layout();

        // On the document, not on the dock: a shortcut that only worked while
        // the pointer was over a 150px strip would be a shortcut nobody found.
        document.addEventListener("keydown", this._onKeyDown);
        window.addEventListener("resize", this._onResize);
        this.watchObstacle();
        return true;
    }

    unmount() {
        document.removeEventListener("keydown", this._onKeyDown);
        window.removeEventListener("resize", this._onResize);
        this._watch?.disconnect();
        this._watch = null;
        this.root?.remove();
        this.root = null;
    }

    destroy() {
        this.unmount();
    }

    get mounted() {
        return Boolean(this.root);
    }

    // -- how much room there is ------------------------------------------

    /** The wrapper the dock and the legend both hang in. */
    host() {
        return document.getElementById("openseadragon_wrapper");
    }

    obstacle() {
        return document.getElementById(FigureCaptureDock.OBSTACLE_ID);
    }

    /**
     * Give the dock the height that is actually free above the legend.
     *
     * A max-height rather than a height: the dock is still only as tall as what
     * is in it, and this is the ceiling the strip's scroll starts at.
     */
    layout() {
        const host = this.host();
        if (!this.root || !host?.getBoundingClientRect) return;
        const bounds = host.getBoundingClientRect();

        const legend = this.obstacle();
        const box = legend?.getBoundingClientRect?.();
        // A legend with no height is one core has emptied or hidden, and a hidden
        // obstacle is not one: measuring it anyway would cap the dock against a
        // rectangle nobody can see.
        const top = box && box.height ? box.top - bounds.top : null;

        this.root.style.maxHeight = FigureCaptureDock.roomFor(bounds.height, top) + "px";
    }

    /**
     * Re-measure when the legend's box changes.
     *
     * Through a ResizeObserver rather than by listening to core: the legend is
     * redrawn on channel changes and on colour changes, and binding to those
     * events would be a plugin reaching into core's event vocabulary for a fact
     * it can observe directly. Guarded, because the probes' synthetic window
     * does not have one and neither does every browser this may meet.
     */
    watchObstacle() {
        const legend = this.obstacle();
        if (this._watch || !legend || typeof ResizeObserver !== "function") return;
        this._watch = new ResizeObserver(() => this.layout());
        this._watch.observe(legend);
    }

    el(role) {
        return this.root?.querySelector(`[data-role="${role}"]`) || null;
    }

    // -- events ----------------------------------------------------------

    clicked(event) {
        const target = event.target.closest("[data-role]");
        const role = target?.dataset.role;
        const id = () => event.target.closest("[data-capture-id]")?.dataset.captureId;
        if (role === "orb") {
            this.handlers.onToggleCapture?.();
        } else if (role === "close") {
            this.handlers.onClose?.();
        } else if (role === "collapse") {
            this.setOpen(!this.open);
        } else if (role === "remove") {
            // Nested inside the item, so `closest` finds this one first --
            // which is what stops discarding a capture from also selecting it
            // and flying the viewer to a region that is about to be gone.
            const captureId = id();
            if (captureId) this.handlers.onRemoveCapture?.(captureId);
        } else if (role === "select") {
            const captureId = id();
            if (captureId) this.handlers.onSelectCapture?.(captureId);
        } else if (role === "new") {
            this.handlers.onNewFigure?.();
        } else if (role === "choose") {
            this.handlers.onChooseFigure?.();
        } else if (role === "canvas") {
            this.handlers.onOpenCanvas?.();
        } else if (role === "editUpdate") {
            this.handlers.onUpdatePanel?.();
        } else if (role === "editCancel") {
            this.handlers.onCancelEdit?.();
        }
    }

    readOpen() {
        try {
            return window.localStorage.getItem(FigureCaptureDock.STORAGE_KEY) !== "0";
        } catch (error) {
            // Private-browsing modes throw rather than answering. Defaulting to
            // open is the recoverable direction: a strip nobody wanted is one
            // click away from gone, a strip nobody can find is not.
            return true;
        }
    }

    setOpen(open) {
        this.open = Boolean(open);
        try {
            window.localStorage.setItem(FigureCaptureDock.STORAGE_KEY, this.open ? "1" : "0");
        } catch (error) {
            /* see readOpen */
        }
        this.applyOpen();
    }

    applyOpen() {
        if (!this.root) return;
        this.root.classList.toggle("is-collapsed", !this.open);
        this.el("collapse")?.setAttribute("aria-expanded", String(this.open));
    }

    // -- rendering -------------------------------------------------------

    /**
     * @param {object} state armed, figureTitle (null when no figure
     *        is open), meta, error, editing ({label, notes} or null), selected
     *        and captures: [{id, url, caption, pending}].
     */
    render(state) {
        if (!this.root) return;
        const armed = Boolean(state.armed);
        const editing = state.editing || null;

        this.root.classList.toggle("is-armed", armed);
        // The capture half stands down while the viewer is showing a panel's
        // borrowed scene: a capture taken then would be a panel of somebody
        // else's view, and nothing on screen would say so.
        this.root.classList.toggle("is-editing", Boolean(editing));

        const orb = this.el("orb");
        if (orb) {
            orb.setAttribute("aria-pressed", String(armed));
            orb.setAttribute("title", armed ? "Leave capture mode (C)" : "Capture mode (C)");
        }

        // The instructions the sidebar used to carry, kept because they are the
        // only place the modifiers are written down -- but only while the mode
        // they describe is on, and never as a block that covers the image.
        const caption = this.el("caption");
        if (caption) {
            // Both letters are read off the classes that own them rather than
            // written out here, so a key that moves cannot leave the only
            // written record of it pointing at the old one.
            const shoot = FigureCaptureTool.SHOOT_KEY.toUpperCase();
            const mode = FigureCaptureDock.SHORTCUT.toUpperCase();
            caption.innerHTML = armed
                ? 'Drag the frame to move it, drag the image to redraw it. '
                  + '<kbd>Shift</kbd> square · <kbd>Space</kbd> pan · '
                  + `<kbd>${shoot}</kbd> capture · <kbd>Esc</kbd> exit`
                : `Capture <kbd>${mode}</kbd>`;
        }

        const error = this.el("error");
        if (error) {
            error.textContent = state.error || "";
            error.hidden = !state.error;
        }

        const strip = this.el("strip");
        if (strip) strip.hidden = Boolean(editing);
        const edit = this.el("edit");
        if (edit) edit.hidden = !editing;
        if (editing) this.renderEdit(editing);

        const captures = state.captures || [];
        const count = this.el("count");
        if (count) count.textContent = String(captures.length);
        const empty = this.el("empty");
        if (empty) empty.hidden = captures.length > 0;

        const items = this.el("items");
        if (items) {
            items.innerHTML = captures.map((capture, index) => {
                const escape = FigureSchema.escapeHtml;
                const classes = ["fb-strip-item"];
                if (capture.pending) classes.push("is-pending");
                if (capture.id === state.selected) classes.push("is-selected");
                const title = escape(capture.caption || "")
                    + (capture.pending ? " — not in a figure yet" : "");
                const image = capture.url
                    ? `<img src="${escape(capture.url)}" alt="" draggable="false">`
                    : '<span class="fb-strip-pending"></span>';
                // The whole item selects; the button inside it discards. The
                // number matches the label on the box out on the image.
                return `<figure class="${classes.join(" ")}" data-role="select"
                                data-capture-id="${escape(capture.id)}"
                                title="${title} — click to go back and aim the shutter at it again">
                    ${image}
                    <span class="fb-strip-index">${index + 1}</span>
                    <button class="fb-strip-remove" type="button" data-role="remove"
                            title="Discard this capture">
                        <span class="fas fa-xmark" aria-hidden="true"></span>
                        <span class="fb-visually-hidden">Discard</span>
                    </button>
                </figure>`;
            }).join("");
        }

        // Never a placeholder that reads like a name: "Untitled figure" is what
        // an unnamed figure is called, and showing it when there is no figure at
        // all would tell the user their captures are somewhere they are not.
        const choose = this.el("choose");
        if (choose) {
            choose.textContent = state.figureTitle || "Not chosen yet";
            choose.classList.toggle("is-empty", !state.figureTitle);
        }

        const meta = this.el("meta");
        if (meta) {
            meta.textContent = state.meta || "";
            meta.hidden = !state.meta;
        }

        // One label, because there is now one thing it does: the canvas is a
        // page of its own and this leaves the viewer for it. It used to toggle a
        // pane beside the image and say "Hide canvas" on the way back, which
        // made the same button mean two things depending on a state nothing
        // else on screen showed. The arrow says the page is going to change.
        const go = this.el("canvas");
        if (go) {
            go.innerHTML = 'Figure Canvas '
                + '<span class="fas fa-arrow-up-right-from-square" aria-hidden="true"></span>';
            go.title = "Open the Figure Canvas — leaves the viewer";
        }

        // The strip has just changed height, and the legend may have changed
        // too since the last look -- on a browser without a ResizeObserver this
        // is the only thing that would notice.
        this.watchObstacle();
        this.layout();
    }

    renderEdit(editing) {
        const label = this.el("editLabel");
        if (label) label.textContent = editing.label || "this panel";
        const notes = this.el("editNotes");
        if (notes) {
            notes.innerHTML = (editing.notes || [])
                .map((note) => `<p class="fb-dock-edit-note">${FigureSchema.escapeHtml(note)}</p>`)
                .join("");
        }
    }
}
