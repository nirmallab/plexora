/**
 * roiToolbar.js - the drawing tools, on the image.
 *
 * While the ROI panel is the one on screen, its tools float in a glass bar at
 * the top centre of the image: Select, Polygon, Freehand, Rectangle and magic
 * select. They used to be a row of 26px squares at the top of the panel --
 * beside the work rather than on it, and small enough to be read past. Over
 * the image they are where the drawing happens, at a size that is seen.
 *
 * Magic select's own choices are not a second bar. Add, Remove, Box and
 * Scribble (and Through planes, on a z-stack, with Plexora 3D) open out of
 * this one, to the right of the wand, while magic select is the tool; a second
 * click on the wand folds them back in (RoiInteraction.toggleMagic).
 * RoiInteraction drives them through `magicBars()`, which has the same face as
 * core's PlexoraMagicBar (services/magicToolbar.js) -- the bar QC still uses.
 *
 * ANCHORED BY ITS TOOLS. The bar is centred on the five tools, not on its
 * whole width, so opening magic select's options grows it to the right and
 * the wand stays under the pointer that clicked it: the click that folds them
 * away lands where the first one did. Only a canvas too narrow for the open
 * bar moves it left, and only as far as it must.
 *
 * Not docked through canvasLayout: a top-centre dock keeps what it holds
 * centred as it grows, which is what the anchor above exists to avoid. The
 * bar sits under whatever that dock holds (the Back pill) instead, by the
 * height the layout publishes, so the two never share pixels (roi.css).
 *
 * Built on first use rather than in the constructor: the panel's controller is
 * made for every project opened, ROI panel or not.
 */
class RoiToolbar {

    constructor({ onTool = null } = {}) {
        this.onTool = onTool;
        this.root = null;
        this.buttons = {};       // tool id -> button
        this.modeButtons = {};   // magic select mode id -> button
        //: While magic select's options are out: who opened them, and what
        //: their clicks report to (RoiInteraction.showMagicBar).
        this.magic = null;
        this.open = false;
        this.mode = "box";
        this.busy = false;
        this._sizes = null;
    }

    static get TOOLS() {
        return [
            { id: "select", icon: "fas fa-hand", label: "Select",
              title: "Select (V) -- click to select, drag to move, drag a handle to reshape" },
            { id: "polygon", icon: "fas fa-draw-polygon", label: "Polygon",
              title: "Polygon (P) -- click to place points; Enter or double-click finishes; "
                   + "Backspace removes the last point" },
            { id: "freehand", icon: "fas fa-pen-nib", label: "Freehand",
              title: "Freehand (F) -- press and drag" },
            { id: "rectangle", icon: "far fa-square", label: "Rectangle",
              title: "Rectangle (R) -- press and drag" },
            { id: "magic", icon: "fas fa-wand-magic-sparkles", label: "Magic select",
              title: "Magic select (E) -- click something to outline it; click inside a "
                   + "selected region to refine it. Click again to put it away." },
        ];
    }

    /** Magic select's ways to point, as core's bar has them. */
    static get MODES() {
        return [
            { id: "add", icon: "fas fa-circle-plus", label: "Add",
              title: "Add -- click to outline something, or to add to the outline" },
            { id: "remove", icon: "fas fa-circle-minus", label: "Remove",
              title: "Remove -- click an area to take it out of the outline (or Shift-click in Add)" },
            { id: "box", icon: "fas fa-expand", label: "Box",
              title: "Box -- drag a box round the object to outline it" },
            { id: "scribble", icon: "fas fa-paintbrush", label: "Scribble",
              title: "Scribble -- draw a line over the object, or a loop round it, to outline "
                   + "it (Shift: a line over what to leave out)" },
        ];
    }

    //: Clearance from the canvas's edges, in px (canvasLayout's EDGE).
    static get EDGE() { return 12; }

    static host() {
        return document.getElementById("openseadragon_wrapper")
            || document.querySelector(".openseadragon-container")?.parentElement || null;
    }

    static button(className, { icon, label, title }) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = className;
        button.title = title;
        button.setAttribute("aria-label", label);
        const glyph = document.createElement("span");
        glyph.className = icon;
        glyph.setAttribute("aria-hidden", "true");
        button.appendChild(glyph);
        return button;
    }

    build() {
        const root = document.createElement("div");
        root.className = "roi-bar";
        root.id = "roi_toolbar";
        root.setAttribute("role", "toolbar");
        root.setAttribute("aria-label", "ROI tools");

        const tools = document.createElement("div");
        tools.className = "roi-bar-group roi-bar-tools";
        for (const tool of RoiToolbar.TOOLS) {
            const button = RoiToolbar.button("roi-bar-button", tool);
            button.id = `roi_tool_${tool.id}`;
            button.dataset.tool = tool.id;
            button.setAttribute("aria-pressed", "false");
            button.addEventListener("click", () => this.onTool?.(tool.id));
            tools.appendChild(button);
            this.buttons[tool.id] = button;
        }
        this.buttons.magic.setAttribute("aria-controls", "roi_toolbar_magic");
        root.appendChild(tools);

        // Magic select's options: a grid track that opens from nothing to
        // their width, so they slide out of the wand rather than appear.
        const drawer = document.createElement("div");
        drawer.className = "roi-bar-drawer";
        drawer.id = "roi_toolbar_magic";
        const inner = document.createElement("div");
        inner.className = "roi-bar-drawer-inner";
        const rule = document.createElement("span");
        rule.className = "roi-bar-rule";
        rule.setAttribute("aria-hidden", "true");
        inner.appendChild(rule);
        const modes = document.createElement("div");
        modes.className = "roi-bar-group roi-bar-modes";
        modes.setAttribute("role", "radiogroup");
        modes.setAttribute("aria-label", "How a click prompts magic select");
        for (const mode of RoiToolbar.MODES) {
            const button = RoiToolbar.button("roi-bar-button roi-bar-mode", mode);
            button.dataset.mode = mode.id;
            button.setAttribute("role", "radio");
            button.setAttribute("aria-checked", "false");
            button.addEventListener("click", () => this.magic?.onMode?.(mode.id));
            modes.appendChild(button);
            this.modeButtons[mode.id] = button;
        }
        inner.appendChild(modes);
        // Through planes: an action, not a mode -- after the modes, dim until
        // there is an outline to carry.
        const carry = RoiToolbar.button("roi-bar-button roi-bar-carry", {
            icon: "fas fa-layer-group", label: "Through planes",
            title: "Through planes -- follow this outline up and down the stack",
        });
        carry.hidden = true;
        carry.disabled = true;
        carry.addEventListener("click", () => {
            if (!carry.disabled) this.magic?.onCarry?.();
        });
        inner.appendChild(carry);
        drawer.appendChild(inner);
        root.appendChild(drawer);

        // A press on the bar is never a press on the image under it.
        for (const name of ["pointerdown", "mousedown", "click", "dblclick", "wheel"]) {
            root.addEventListener(name, (event) => event.stopPropagation());
        }

        this.root = root;
        this.tools = tools;
        this.drawer = drawer;
        this.drawerInner = inner;
        this.carry = carry;
        this.applyMagic();
        this.setMode(this.mode);
        this.setBusy(this.busy);
    }

    // -- on screen -------------------------------------------------------

    /** Over the image, while the ROI panel's tools are armed. */
    show() {
        const host = RoiToolbar.host();
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
        this.root.hidden = true;
        this.root.remove();
    }

    destroy() {
        this.hide();
        this.magic = null;
        this.root = null;
    }

    /**
     * Centre the bar on its tools, and keep it inside the canvas when open.
     *
     * `left` (roi.css) is the middle of the canvas, clear of any band down
     * either side; this sets how far left of that the bar starts. Measured
     * rather than written into the stylesheet as a sum of button widths, so a
     * tool added later is centred too.
     */
    anchor() {
        const root = this.root;
        const host = root?.parentNode;
        if (!host || root.hidden || typeof getComputedStyle !== "function") return;
        const own = getComputedStyle(root);
        const box = root.getBoundingClientRect();
        const tools = this.tools.getBoundingClientRect();
        if (!box.width) return;
        const closed = tools.right - box.left
            + (parseFloat(own.paddingRight) || 0) + (parseFloat(own.borderRightWidth) || 0);
        let shift = -closed / 2;
        if (this.open) {
            const room = getComputedStyle(host);
            const insetLeft = parseFloat(room.getPropertyValue("--plx-inset-left")) || 0;
            const insetRight = parseFloat(room.getPropertyValue("--plx-inset-right")) || 0;
            const width = host.clientWidth;
            const middle = insetLeft + (width - insetLeft - insetRight) / 2;
            const full = closed + this.drawerInner.scrollWidth;
            const furthest = width - insetRight - RoiToolbar.EDGE - middle - full;
            shift = Math.max(insetLeft + RoiToolbar.EDGE - middle, Math.min(shift, furthest));
        }
        root.style.setProperty("--roi-bar-shift", `${Math.round(shift)}px`);
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

    applyMagic() {
        if (!this.root) return;
        const open = this.open;
        this.root.classList.toggle("is-magic", open);
        this.buttons.magic.setAttribute("aria-expanded", open ? "true" : "false");
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

    /**
     * Magic select's options, as RoiInteraction asks for them: the face of
     * core's PlexoraMagicBar, so the tool code is the same either way.
     * `onClose` has nothing to answer to -- the wand itself folds them.
     */
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
