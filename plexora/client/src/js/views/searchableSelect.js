/**
 * @class SearchableSelect - lightweight typeahead combobox replacing a plain <select>.
 * Mounted into a container element; filters its option list live as the user types.
 *
 * Two shapes, because two different jobs:
 *
 *   trigger: "input"  (the default) -- the field IS the combobox. It shows the
 *       current value, and typing over it filters. Right where the control is
 *       the only thing on its line and the value is what you want to read.
 *
 *   trigger: "button" -- a button showing the current value, with the search
 *       field inside the dropdown it opens. Narrower, because it is sized to
 *       its label rather than to a text field, so it can share a line with
 *       other controls; and the search is where somebody looking for a search
 *       box would look, which a field that doubles as the value display is
 *       not -- there is nothing about it that says it can be typed into.
 *
 * Both filter the same list, report through the same onChange, and are reached
 * the same way from the keyboard.
 */
class SearchableSelect {
    constructor(mount, options = {}) {
        this.mount = mount;
        this.options = [...(options.options || [])];
        this.value = options.value || "";
        this.placeholder = options.placeholder || "Search…";
        //: What the search field inside a button-triggered menu says. The
        //: trigger's own label is the current value, so the two cannot share
        //: one string.
        this.searchPlaceholder = options.searchPlaceholder || this.placeholder;
        //: Shown on a button trigger that has no value yet.
        this.emptyLabel = options.emptyLabel || this.placeholder;
        this.describeOption = options.describeOption || (() => "");
        // Optional per-option status dot (e.g. "this marker already has a
        // gate set") -- separate from describeOption's always-visible text
        // hint, since this is a lightweight visual cue whose detail only
        // needs to show up on hover (title attribute), not inline text.
        this.getIndicator = options.getIndicator || null;
        // What an exhausted filter says. Defaulted to the marker wording this
        // widget was written for, so the channel rows and the gate picker read
        // as they always did.
        this.emptyText = options.emptyText || "No markers match";
        // For callers whose visible <label> cannot use `for` -- this mounts a
        // generated input, so there is no stable id to point one at.
        this.ariaLabel = options.ariaLabel || "";
        this.onChange = options.onChange || (() => {});
        this.trigger = options.trigger === "button" ? "button" : "input";
        this.filtered = [...this.options];
        this.activeIndex = -1;
        this.isOpen = false;
        this.render();
    }

    render() {
        this.mount.innerHTML = "";
        this.mount.classList.add("marker-combobox");

        this.menu = document.createElement("div");
        this.menu.className = "marker-combobox-menu";
        this.menu.hidden = true;
        // Appended to a positioning "portal", not this.mount: a disabled/dimmed
        // ancestor row has opacity < 1, which creates its own CSS stacking context and
        // would trap this menu's z-index behind the *next* row otherwise. The portal
        // decides that for itself -- see PopoverPortal for when <body> is the
        // wrong host and a menu parked there becomes invisible in fullscreen.
        PopoverPortal.attach(this.menu);

        if (this.trigger === "button") {
            this.renderButtonTrigger();
        } else {
            this.renderInputTrigger();
        }

        // `field` is whatever the typing goes into and `anchor` is what the
        // menu hangs off. They are the same element for an input trigger and
        // deliberately different for a button one, which is the whole of the
        // difference between the two shapes.
        this.field.addEventListener("input", () => this.filter(this.field.value));
        this.field.addEventListener("keydown", (event) => this.handleKeydown(event));
        this.field.addEventListener("blur", () => {
            window.setTimeout(() => {
                const active = document.activeElement;
                if (active === this.field || this.menu?.contains(active)
                    || active === this.anchor) {
                    return;
                }
                this.close();
                if (this.trigger === "input") this.field.value = this.value;
            }, 120);
        });
    }

    /** The classic shape: one field that is both the value and the search. */
    renderInputTrigger() {
        this.input = document.createElement("input");
        this.input.type = "text";
        this.input.className = "sidebar-select marker-combobox-input";
        this.input.autocomplete = "off";
        this.input.spellcheck = false;
        this.input.placeholder = this.placeholder;
        this.input.setAttribute("role", "combobox");
        this.input.setAttribute("aria-expanded", "false");
        this.input.setAttribute("aria-autocomplete", "list");
        if (this.ariaLabel) this.input.setAttribute("aria-label", this.ariaLabel);
        this.input.value = this.value;
        this.mount.appendChild(this.input);

        this.input.addEventListener("focus", () => this.open(true));
        this.input.addEventListener("click", () => this.open(true));

        this.anchor = this.input;
        this.field = this.input;
        this.menu.setAttribute("role", "listbox");
        this.list = this.menu;
    }

    /** The compact shape: a button, with the search inside the dropdown. */
    renderButtonTrigger() {
        this.button = document.createElement("button");
        this.button.type = "button";
        this.button.className = "sidebar-select marker-combobox-trigger";
        this.button.setAttribute("aria-haspopup", "listbox");
        this.button.setAttribute("aria-expanded", "false");
        if (this.ariaLabel) {
            this.button.setAttribute("aria-label", this.ariaLabel);
            this.button.title = this.ariaLabel;
        }

        this.buttonLabel = document.createElement("span");
        this.buttonLabel.className = "marker-combobox-trigger-label";
        const caret = document.createElement("span");
        caret.className = "fas fa-chevron-down marker-combobox-caret";
        caret.setAttribute("aria-hidden", "true");
        this.button.append(this.buttonLabel, caret);
        this.button.addEventListener("click", () => this.toggle());
        this.mount.appendChild(this.button);

        this.menu.classList.add("has-search");
        const searchRow = document.createElement("div");
        searchRow.className = "marker-combobox-searchbox";
        const icon = document.createElement("span");
        icon.className = "fas fa-magnifying-glass";
        icon.setAttribute("aria-hidden", "true");
        this.field = document.createElement("input");
        this.field.type = "text";
        this.field.className = "marker-combobox-search";
        this.field.autocomplete = "off";
        this.field.spellcheck = false;
        this.field.placeholder = this.searchPlaceholder;
        this.field.setAttribute("role", "combobox");
        this.field.setAttribute("aria-expanded", "true");
        this.field.setAttribute("aria-autocomplete", "list");
        this.field.setAttribute("aria-label", this.ariaLabel || this.searchPlaceholder);
        searchRow.append(icon, this.field);

        this.list = document.createElement("div");
        this.list.className = "marker-combobox-list";
        this.list.setAttribute("role", "listbox");
        this.menu.append(searchRow, this.list);

        this.anchor = this.button;
        this.paintTrigger();
    }

    /** The button's label: the current value, or what to pick if there is none. */
    paintTrigger() {
        if (!this.buttonLabel) return;
        this.buttonLabel.textContent = this.value || this.emptyLabel;
        this.buttonLabel.classList.toggle("is-placeholder", !this.value);
        // The value can be far wider than the button, which truncates it -- so
        // the full text is on the title, where the label alone would leave a
        // column name unreadable in a narrow sidebar.
        this.button.title = this.value
            ? (this.ariaLabel ? `${this.ariaLabel}: ${this.value}` : this.value)
            : (this.ariaLabel || this.emptyLabel);
    }

    setOptions(names) {
        this.options = [...names];
        if (this.isOpen) {
            this.filter(this.field.value);
        }
    }

    setValue(name) {
        this.value = name || "";
        if (this.trigger === "button") {
            this.paintTrigger();
        } else {
            this.field.value = this.value;
        }
    }

    filter(query) {
        const q = query.trim().toLowerCase();
        this.filtered = q ? this.options.filter((name) => name.toLowerCase().includes(q)) : [...this.options];
        this.activeIndex = this.filtered.length ? 0 : -1;
        this.renderMenu();
        this.open(false);
    }

    toggle() {
        if (this.isOpen) {
            this.close();
        } else {
            this.open(true);
        }
    }

    open(reset) {
        if (reset) {
            this.filtered = [...this.options];
            this.activeIndex = Math.max(this.options.indexOf(this.value), 0);
            this.renderMenu();
            if (this.trigger === "button") {
                // Opened empty rather than seeded with the current value: this
                // field is a search, not the value, and a query already in it
                // would show one match and look like the list.
                this.field.value = "";
                this.field.focus();
            } else {
                this.field.select();
            }
        }
        this.isOpen = true;
        this.positionMenu();
        this.menu.hidden = false;
        requestAnimationFrame(() => this.menu.classList.add("is-open"));
        this.anchor.setAttribute("aria-expanded", "true");
        this.bindDismissListeners();
    }

    positionMenu() {
        const rect = this.anchor.getBoundingClientRect();
        this.menu.style.left = `${rect.left}px`;
        this.menu.style.top = `${rect.bottom + 4}px`;
        // A button trigger is sized to its label, which can be narrower than
        // the option names it is choosing between -- so the menu takes the
        // wider of the two rather than inheriting a width nothing fits in.
        this.menu.style.width = this.trigger === "button"
            ? `${Math.max(rect.width, 240)}px`
            : `${rect.width}px`;
    }

    close() {
        this.isOpen = false;
        if (!this.menu) return;
        this.menu.classList.remove("is-open");
        this.anchor.setAttribute("aria-expanded", "false");
        window.setTimeout(() => {
            // The menu may have been destroyed inside this delay -- closing is
            // the first thing destroy() does, and the blur handler also lands
            // here on its own timer.
            if (!this.isOpen && this.menu) this.menu.hidden = true;
        }, 150);
        this.unbindDismissListeners();
    }

    bindDismissListeners() {
        if (this._dismissScroll) return;
        // Follows the trigger rather than shutting on any scroll at all.
        //
        // Closing was the cheap way to stop a body-portaled menu lingering at a
        // stale position, and the cure was worse: a wheel the option list could
        // not absorb -- at either end of it, or over the search box, which does
        // not scroll -- reached the panel behind, so the menu being read
        // vanished while the page moved under it. Repositioning keeps the
        // promise (the menu is never somewhere its trigger is not) without
        // taking the menu away from someone using it.
        //
        // Scrolling the menu's own list is not a page scroll and is ignored.
        this._dismissScroll = (event) => {
            if (event.target === this.menu || this.menu.contains(event.target)) return;
            this.positionMenu();
            // Scrolled out of view is the one case with nothing left to follow.
            const rect = this.anchor.getBoundingClientRect();
            const limit = window.innerHeight || document.documentElement?.clientHeight;
            if (limit && (rect.bottom < 0 || rect.top > limit)) this.close();
        };
        this._dismissResize = () => this.close();
        // A button trigger takes focus itself, so its menu cannot rely on the
        // field's blur alone -- clicking anywhere else on the page has to shut
        // it, the same way every other popover in the viewer behaves.
        this._dismissClick = (event) => {
            if (!this.isOpen) return;
            if (this.menu.contains(event.target) || this.mount.contains(event.target)) return;
            this.close();
        };
        window.addEventListener("scroll", this._dismissScroll, true);
        window.addEventListener("resize", this._dismissResize);
        document.addEventListener("click", this._dismissClick);
    }

    unbindDismissListeners() {
        if (!this._dismissScroll) return;
        window.removeEventListener("scroll", this._dismissScroll, true);
        window.removeEventListener("resize", this._dismissResize);
        document.removeEventListener("click", this._dismissClick);
        this._dismissScroll = null;
        this._dismissResize = null;
        this._dismissClick = null;
    }

    renderMenu() {
        this.list.innerHTML = "";
        if (!this.filtered.length) {
            const empty = document.createElement("div");
            empty.className = "marker-combobox-empty";
            empty.textContent = this.emptyText;
            this.list.appendChild(empty);
            return;
        }
        this.filtered.forEach((name, index) => {
            const option = document.createElement("div");
            option.className = "marker-combobox-option";
            option.setAttribute("role", "option");
            option.dataset.value = name;
            if (name === this.value) option.classList.add("is-selected");
            if (index === this.activeIndex) option.classList.add("is-active");

            if (this.getIndicator) {
                const indicatorTitle = this.getIndicator(name);
                if (indicatorTitle) {
                    const dot = document.createElement("span");
                    dot.className = "marker-combobox-option-indicator";
                    dot.title = indicatorTitle;
                    option.appendChild(dot);
                }
            }

            const label = document.createElement("span");
            label.className = "marker-combobox-option-label";
            label.textContent = name;
            option.appendChild(label);

            const hint = this.describeOption(name);
            if (hint) {
                const tag = document.createElement("span");
                tag.className = "marker-combobox-hint";
                tag.textContent = hint;
                option.appendChild(tag);
            }

            option.addEventListener("mousedown", (event) => {
                event.preventDefault();
                this.selectOption(name);
            });
            this.list.appendChild(option);
        });
    }

    selectOption(name) {
        this.setValue(name);
        this.close();
        // Focus goes back to what opened the menu, or a keyboard user is left
        // on an element that is no longer visible.
        if (this.trigger === "button") this.button.focus();
        this.onChange(name);
    }

    handleKeydown(event) {
        if (!this.isOpen && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
            event.preventDefault();
            this.open(true);
            return;
        }
        if (!this.isOpen) return;
        if (event.key === "ArrowDown") {
            event.preventDefault();
            this.activeIndex = Math.min(this.activeIndex + 1, this.filtered.length - 1);
            this.renderMenu();
            this.scrollActiveIntoView();
        } else if (event.key === "ArrowUp") {
            event.preventDefault();
            this.activeIndex = Math.max(this.activeIndex - 1, 0);
            this.renderMenu();
            this.scrollActiveIntoView();
        } else if (event.key === "Enter") {
            event.preventDefault();
            const active = this.filtered[this.activeIndex];
            if (active) this.selectOption(active);
        } else if (event.key === "Escape") {
            this.close();
            if (this.trigger === "button") {
                this.button.focus();
            } else {
                this.field.value = this.value;
                this.field.blur();
            }
        }
    }

    scrollActiveIntoView() {
        const active = this.menu.querySelector(".marker-combobox-option.is-active");
        if (active) active.scrollIntoView({ block: "nearest" });
    }

    /**
     * Take this combobox off the page for good.
     *
     * The menu is portaled out of the mount (see render), so removing the
     * element it was mounted into leaves the menu behind -- a panel that is torn
     * down and rebuilt otherwise strands one orphan per rebuild, a stale one can
     * still be opened by keyboard, and the portal would keep re-parenting the
     * orphan on every fullscreen toggle, putting it back on the page.
     */
    destroy() {
        this.close();
        this.unbindDismissListeners();
        PopoverPortal.detach(this.menu);
        this.menu = null;
        this.list = null;
    }
}
