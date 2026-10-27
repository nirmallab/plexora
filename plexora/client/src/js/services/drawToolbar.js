/**
 * drawToolbar.js - the drawing tools, on the image.
 *
 * A glass bar at the top centre of the image with the ways to draw a region:
 * a hand (the ROI panel's Select, QC's Pan), Polygon, Freehand, Rectangle and
 * magic select. The ROI panel shows it while its tools are armed; the QC
 * panel while a category picked from Add ROIs is in hand, with that category
 * before the tools (a click changes it) and × after them (done). Core, so the
 * two panels draw with one bar, and each owner makes its own.
 *
 * Magic select's own choices are not a second bar. Add, Remove, Box and
 * Scribble (and Through planes, on a z-stack, with Plexora 3D) open out of
 * this one, to the right of the wand, while magic select is the tool; a second
 * click on the wand folds them back in. The owner's magic select drives them
 * through `magicBars()`: show({owner, mode, onMode, onCarry}) returns a handle
 * {setMode, setBusy, setCanCarry, hide, isShown} for that owner alone.
 *
 * The name of the button under the pointer shows under the bar at once
 * (`named`): the browser's own tooltip came a second or two late.
 *
 * ANCHORED BY ITS CLOSED WIDTH. The bar is centred on everything but the
 * options, so opening them grows it to the right and the wand stays under the
 * pointer that clicked it: the click that folds them away lands where the
 * first one did. Only a canvas too narrow for the open bar moves it left, and
 * only as far as it must.
 *
 * Not docked through canvasLayout: a top-centre dock keeps what it holds
 * centred as it grows, which is what the anchor above exists to avoid. The
 * bar sits under whatever that dock holds (the Back pill) instead, by the
 * height the layout publishes, so the two never share pixels (viewer.css).
 *
 * Built on first use rather than in the constructor: the panels' controllers
 * are made for every project opened, whether the bar is ever shown or not.
 */
class PlexoraDrawBar {

    /**
     * @param {object} options
     * @param {string} options.id          the bar's element id; its buttons are `${buttonId}_${tool}`
     * @param {string} options.buttonId
     * @param {string} options.label       what the toolbar is, to a screen reader
     * @param {Array}  options.tools       [{id, icon, label}]; a tool `magic` opens the options
     * @param {Function} options.onTool    (toolId) => void
     * @param {Function} [options.onCategory]  (anchor) => void: a category before the tools
     * @param {Function} [options.onClose]     () => void: × after them
     */
    constructor({ id = "plx_draw_bar", buttonId = "plx_draw_tool", label = "Drawing tools",
                  tools = PlexoraDrawBar.TOOLS, onTool = null, onCategory = null,
                  onClose = null } = {}) {
        this.id = id;
        this.buttonId = buttonId;
        this.label = label;
        this.toolList = tools;
        this.onTool = onTool;
        this.onCategory = onCategory;
        this.onClose = onClose;
        this.root = null;
        this.buttons = {};       // tool id -> button
        this.modeButtons = {};   // magic select mode id -> button
        //: While magic select's options are out: who opened them, and what
        //: their clicks report to.
        this.magic = null;
        this.open = false;
        this.mode = "box";
        this.busy = false;
        this.categoryShown = { words: "", color: "" };
        this._sizes = null;
    }

    /** The shapes and magic select; each owner puts its own hand first. */
    static get TOOLS() {
        return [
            { id: "polygon", icon: "fas fa-draw-polygon", label: "Polygon" },
            { id: "freehand", icon: "fas fa-pen-nib", label: "Freehand" },
            { id: "rectangle", icon: "far fa-square", label: "Rectangle" },
            { id: "magic", icon: "fas fa-wand-magic-sparkles", label: "Magic select" },
        ];
    }

    /** Magic select's ways to point. */
    static get MODES() {
        return [
            { id: "add", icon: "fas fa-circle-plus", label: "Add" },
            { id: "remove", icon: "fas fa-circle-minus", label: "Remove" },
            { id: "box", icon: "fas fa-expand", label: "Box" },
            { id: "scribble", icon: "fas fa-paintbrush", label: "Scribble" },
        ];
    }

    //: Clearance from the canvas's edges, in px (canvasLayout's EDGE).
    static get EDGE() { return 12; }

    static host() {
        return document.getElementById("openseadragon_wrapper")
            || document.querySelector(".openseadragon-container")?.parentElement || null;
    }

    /** No `title`: the name shows under the bar instead (`named`). */
    static button(className, { icon, label }) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = className;
        button.setAttribute("aria-label", label);
        const glyph = document.createElement("span");
        glyph.className = icon;
        glyph.setAttribute("aria-hidden", "true");
        button.appendChild(glyph);
        return button;
    }

    build() {
        const root = document.createElement("div");
        root.className = "plx-draw-bar";
        root.id = this.id;
        root.setAttribute("role", "toolbar");
        root.setAttribute("aria-label", this.label);

        // What is being marked, before the tools: its colour and name.
        if (this.onCategory) {
            const category = document.createElement("button");
            category.type = "button";
            category.className = "plx-draw-bar-category";
            category.setAttribute("aria-haspopup", "true");
            category.setAttribute("aria-expanded", "false");
            const dot = document.createElement("span");
            dot.className = "plx-draw-bar-dot";
            dot.setAttribute("aria-hidden", "true");
            const words = document.createElement("span");
            words.className = "plx-draw-bar-words";
            category.append(dot, words);
            category.addEventListener("click", () => this.onCategory?.(category));
            this.named(category, "Change what you are marking");
            root.appendChild(category);
            this.category = category;
            this.categoryWords = words;
        }

        const tools = document.createElement("div");
        tools.className = "plx-draw-bar-group plx-draw-bar-tools";
        for (const tool of this.toolList) {
            const button = PlexoraDrawBar.button("plx-draw-bar-button", tool);
            button.id = `${this.buttonId}_${tool.id}`;
            button.dataset.tool = tool.id;
            button.setAttribute("aria-pressed", "false");
            button.addEventListener("click", () => this.onTool?.(tool.id));
            this.named(button, tool.label);
            tools.appendChild(button);
            this.buttons[tool.id] = button;
        }
        this.buttons.magic?.setAttribute("aria-controls", `${this.id}_magic`);
        root.appendChild(tools);

        // Magic select's options: a grid track that opens from nothing to
        // their width, so they slide out of the wand rather than appear.
        const drawer = document.createElement("div");
        drawer.className = "plx-draw-bar-drawer";
        drawer.id = `${this.id}_magic`;
        const inner = document.createElement("div");
        inner.className = "plx-draw-bar-drawer-inner";
        const rule = document.createElement("span");
        rule.className = "plx-draw-bar-rule";
        rule.setAttribute("aria-hidden", "true");
        inner.appendChild(rule);
        const modes = document.createElement("div");
        modes.className = "plx-draw-bar-group plx-draw-bar-modes";
        modes.setAttribute("role", "radiogroup");
        modes.setAttribute("aria-label", "How a click prompts magic select");
        for (const mode of PlexoraDrawBar.MODES) {
            const button = PlexoraDrawBar.button("plx-draw-bar-button plx-draw-bar-mode", mode);
            button.dataset.mode = mode.id;
            button.setAttribute("role", "radio");
            button.setAttribute("aria-checked", "false");
            button.addEventListener("click", () => this.magic?.onMode?.(mode.id));
            this.named(button, mode.label);
            modes.appendChild(button);
            this.modeButtons[mode.id] = button;
        }
        inner.appendChild(modes);
        // Through planes: an action, not a mode -- after the modes, dim until
        // there is an outline to carry.
        const carry = PlexoraDrawBar.button("plx-draw-bar-button plx-draw-bar-carry", {
            icon: "fas fa-layer-group", label: "Through planes",
        });
        carry.hidden = true;
        carry.disabled = true;
        carry.addEventListener("click", () => {
            if (!carry.disabled) this.magic?.onCarry?.();
        });
        this.named(carry, "Through planes");
        inner.appendChild(carry);
        drawer.appendChild(inner);
        root.appendChild(drawer);

        if (this.onClose) {
            const close = PlexoraDrawBar.button("plx-draw-bar-button plx-draw-bar-close", {
                icon: "fas fa-xmark", label: "Done",
            });
            close.addEventListener("click", () => this.onClose?.());
            this.named(close, "Done");
            root.appendChild(close);
        }

        // The name of the button under the pointer, centred under the bar.
        // Its button's aria-label already says it to a screen reader.
        const name = document.createElement("span");
        name.className = "plx-draw-bar-name";
        name.setAttribute("aria-hidden", "true");
        root.appendChild(name);
        this.name = name;

        // A press on the bar is never a press on the image under it.
        for (const event of ["pointerdown", "mousedown", "click", "dblclick", "wheel"]) {
            root.addEventListener(event, (e) => e.stopPropagation());
        }

        this.root = root;
        this.tools = tools;
        this.drawer = drawer;
        this.drawerInner = inner;
        this.carry = carry;
        this.applyMagic();
        this.setMode(this.mode);
        this.setBusy(this.busy);
        this.setCategory(this.categoryShown);
    }

    // -- on screen -------------------------------------------------------

    /** Over the image. */
    show() {
        const host = PlexoraDrawBar.host();
        if (!host) return;
        if (!this.root) this.build();
        if (this.root.parentNode !== host) host.appendChild(this.root);
        this.root.hidden = false;
        this.anchor();
        // The canvas narrows and widens under it (the window, the sidebar, a
        // section pane): the open bar must still fit.
        if (!this._sizes && typeof ResizeObserver === "function") {
            this._sizes = new ResizeObserver(() => this.anchor());
            this._sizes.observe(host);
        }
    }

    hide() {
        this._sizes?.disconnect();
        this._sizes = null;
        if (!this.root) return;
        this.showName(null);
        this.root.hidden = true;
        this.root.remove();
    }

    get shown() {
        return Boolean(this.root && !this.root.hidden && this.root.parentNode);
    }

    destroy() {
        this.hide();
        this.magic = null;
        this.root = null;
    }

    /**
     * Centre the bar on everything but magic select's options, and keep it
     * inside the canvas when they are out.
     *
     * `left` (viewer.css) is the middle of the canvas, clear of any band down
     * either side; this sets how far left of that the bar starts. Measured
     * rather than written into the stylesheet as a sum of button widths, so a
     * tool added later is centred too. The options' track is subtracted as it
     * stands, which is its whole width when closing and none when opening.
     */
    anchor() {
        const root = this.root;
        const host = root?.parentNode;
        if (!host || root.hidden || typeof getComputedStyle !== "function") return;
        const box = root.getBoundingClientRect();
        if (!box.width) return;
        const closed = box.width - this.drawer.getBoundingClientRect().width;
        let shift = -closed / 2;
        if (this.open) {
            const room = getComputedStyle(host);
            const insetLeft = parseFloat(room.getPropertyValue("--plx-inset-left")) || 0;
            const insetRight = parseFloat(room.getPropertyValue("--plx-inset-right")) || 0;
            const width = host.clientWidth;
            const middle = insetLeft + (width - insetLeft - insetRight) / 2;
            const full = closed + this.drawerInner.scrollWidth;
            const furthest = width - insetRight - PlexoraDrawBar.EDGE - middle - full;
            shift = Math.max(insetLeft + PlexoraDrawBar.EDGE - middle, Math.min(shift, furthest));
        }
        root.style.setProperty("--plx-draw-bar-shift", `${Math.round(shift)}px`);
    }

    // -- the name under the bar ------------------------------------------

    /** Show `label` under the bar the moment the pointer (or the keyboard's
     *  focus) is on `button`, and take it away when it leaves. */
    named(button, label) {
        const show = () => this.showName(label);
        const hide = () => this.showName(null);
        button.addEventListener("pointerenter", show);
        button.addEventListener("pointerleave", hide);
        button.addEventListener("focus", show);
        button.addEventListener("blur", hide);
    }

    /** Kept as the last name while it fades, so the text does not blank first. */
    showName(label) {
        if (!this.name) return;
        if (label) this.name.textContent = label;
        this.name.classList.toggle("is-shown", Boolean(label));
    }

    // -- state -----------------------------------------------------------

    /**
     * Which tool is in hand (lit), and which can be picked (`disabled(id)`).
     * The tool in hand is never disabled: a wand that cannot be clicked is a
     * wand whose options cannot be folded away.
     */
    render(tool, disabled = () => false) {
        if (!this.root) return;
        for (const [id, button] of Object.entries(this.buttons)) {
            const active = id === tool;
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-pressed", active ? "true" : "false");
            button.disabled = !active && Boolean(disabled(id));
        }
    }

    /** What is being marked: its name and colour, on the category button. */
    setCategory({ words = "", color = "" } = {}) {
        this.categoryShown = { words, color };
        if (!this.category) return;
        this.categoryWords.textContent = words;
        this.category.style.setProperty("--plx-draw-bar-color", color || "var(--text-muted)");
        this.category.setAttribute("aria-label", words ? `Marking ${words}: change it` : "Pick what to mark");
        this.anchor();
    }

    applyMagic() {
        if (!this.root) return;
        const open = this.open;
        this.root.classList.toggle("is-magic", open);
        this.buttons.magic?.setAttribute("aria-expanded", open ? "true" : "false");
        // Folded away, its buttons are out of the tab order and the tree.
        this.drawer.inert = !open;
        this.drawer.setAttribute("aria-hidden", open ? "false" : "true");
        this.anchor();
    }

    setOpen(open) {
        if (this.open === open && this.root) return;
        this.open = open;
        this.applyMagic();
    }

    setMode(mode) {
        this.mode = mode;
        if (!this.root) return;
        for (const [id, button] of Object.entries(this.modeButtons)) {
            button.setAttribute("aria-checked", id === mode ? "true" : "false");
        }
        this.root.dataset.mode = mode;
    }

    /** The wand breathes while an outline is on its way. */
    setBusy(busy) {
        this.busy = Boolean(busy);
        this.buttons.magic?.setAttribute("aria-busy", this.busy ? "true" : "false");
    }

    /** Through planes, for an owner that can carry, on a z-stack. */
    offerCarry(onCarry) {
        if (!this.carry) return;
        const stack = window.__plexora?.viewState;
        this.carry.hidden = !(onCarry && stack?.enabled?.() && stack.depth > 1);
        this.carry.disabled = true;
    }

    /** Magic select's options, for the owner's magic select to drive. */
    magicBars() {
        return {
            show: ({ owner, mode = "box", onMode = null, onCarry = null } = {}) => {
                if (!this.root) this.build();
                this.magic = { owner, onMode, onCarry };
                this.offerCarry(onCarry);
                this.setMode(mode);
                this.setOpen(true);
                const mine = () => Boolean(this.magic && this.magic.owner === owner);
                return {
                    setMode: (next) => { if (mine()) this.setMode(next); },
                    setBusy: (busy) => { if (mine()) this.setBusy(busy); },
                    setCanCarry: (can) => { if (mine() && this.carry) this.carry.disabled = !can; },
                    hide: () => {
                        if (!mine()) return;
                        this.magic = null;
                        this.setOpen(false);
                    },
                    isShown: () => mine() && this.open,
                };
            },
            hide: () => {
                this.magic = null;
                this.setOpen(false);
            },
        };
    }
}

window.PlexoraDrawBar = PlexoraDrawBar;
