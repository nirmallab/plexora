/**
 * @class SearchableSelect - lightweight typeahead combobox replacing a plain <select>.
 * Mounted into a container element; filters its option list live as the user types.
 */
class SearchableSelect {
    constructor(mount, options = {}) {
        this.mount = mount;
        this.options = [...(options.options || [])];
        this.value = options.value || "";
        this.placeholder = options.placeholder || "Search…";
        this.describeOption = options.describeOption || (() => "");
        // Optional per-option status dot (e.g. "this marker already has a
        // gate set") -- separate from describeOption's always-visible text
        // hint, since this is a lightweight visual cue whose detail only
        // needs to show up on hover (title attribute), not inline text.
        this.getIndicator = options.getIndicator || null;
        this.onChange = options.onChange || (() => {});
        this.filtered = [...this.options];
        this.activeIndex = -1;
        this.isOpen = false;
        this.render();
    }

    render() {
        this.mount.innerHTML = "";
        this.mount.classList.add("marker-combobox");

        this.input = document.createElement("input");
        this.input.type = "text";
        this.input.className = "sidebar-select marker-combobox-input";
        this.input.autocomplete = "off";
        this.input.spellcheck = false;
        this.input.placeholder = this.placeholder;
        this.input.setAttribute("role", "combobox");
        this.input.setAttribute("aria-expanded", "false");
        this.input.setAttribute("aria-autocomplete", "list");
        this.input.value = this.value;
        this.mount.appendChild(this.input);

        this.menu = document.createElement("div");
        this.menu.className = "marker-combobox-menu";
        this.menu.setAttribute("role", "listbox");
        this.menu.hidden = true;
        // Appended to <body> (a positioning "portal"), not this.mount: a disabled/dimmed
        // ancestor row has opacity < 1, which creates its own CSS stacking context and
        // would trap this menu's z-index behind the *next* row otherwise.
        document.body.appendChild(this.menu);

        this.input.addEventListener("focus", () => this.open(true));
        this.input.addEventListener("click", () => this.open(true));
        this.input.addEventListener("input", () => this.filter(this.input.value));
        this.input.addEventListener("keydown", (event) => this.handleKeydown(event));
        this.input.addEventListener("blur", () => {
            window.setTimeout(() => {
                if (document.activeElement !== this.input) {
                    this.close();
                    this.input.value = this.value;
                }
            }, 120);
        });
    }

    setOptions(names) {
        this.options = [...names];
        if (this.isOpen) {
            this.filter(this.input.value);
        }
    }

    setValue(name) {
        this.value = name || "";
        this.input.value = this.value;
    }

    filter(query) {
        const q = query.trim().toLowerCase();
        this.filtered = q ? this.options.filter((name) => name.toLowerCase().includes(q)) : [...this.options];
        this.activeIndex = this.filtered.length ? 0 : -1;
        this.renderMenu();
        this.open(false);
    }

    open(reset) {
        if (reset) {
            this.filtered = [...this.options];
            this.activeIndex = Math.max(this.options.indexOf(this.value), 0);
            this.renderMenu();
            this.input.select();
        }
        this.isOpen = true;
        this.positionMenu();
        this.menu.hidden = false;
        requestAnimationFrame(() => this.menu.classList.add("is-open"));
        this.input.setAttribute("aria-expanded", "true");
        this.bindDismissListeners();
    }

    positionMenu() {
        const rect = this.input.getBoundingClientRect();
        this.menu.style.left = `${rect.left}px`;
        this.menu.style.top = `${rect.bottom + 4}px`;
        this.menu.style.width = `${rect.width}px`;
    }

    close() {
        this.isOpen = false;
        this.menu.classList.remove("is-open");
        this.input.setAttribute("aria-expanded", "false");
        window.setTimeout(() => {
            if (!this.isOpen) this.menu.hidden = true;
        }, 150);
        this.unbindDismissListeners();
    }

    bindDismissListeners() {
        if (this._dismissScroll) return;
        // Closes on scroll so a stale position (from the trigger scrolling underneath a
        // body-portaled menu) never lingers - but scrolling the menu's own option list
        // must not count as "dismiss", or the list becomes unscrollable.
        this._dismissScroll = (event) => {
            if (event.target === this.menu || this.menu.contains(event.target)) return;
            this.close();
        };
        this._dismissResize = () => this.close();
        window.addEventListener("scroll", this._dismissScroll, true);
        window.addEventListener("resize", this._dismissResize);
    }

    unbindDismissListeners() {
        if (!this._dismissScroll) return;
        window.removeEventListener("scroll", this._dismissScroll, true);
        window.removeEventListener("resize", this._dismissResize);
        this._dismissScroll = null;
        this._dismissResize = null;
    }

    renderMenu() {
        this.menu.innerHTML = "";
        if (!this.filtered.length) {
            const empty = document.createElement("div");
            empty.className = "marker-combobox-empty";
            empty.textContent = "No markers match";
            this.menu.appendChild(empty);
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
            this.menu.appendChild(option);
        });
    }

    selectOption(name) {
        this.value = name;
        this.input.value = name;
        this.close();
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
            this.input.value = this.value;
            this.input.blur();
        }
    }

    scrollActiveIntoView() {
        const active = this.menu.querySelector(".marker-combobox-option.is-active");
        if (active) active.scrollIntoView({ block: "nearest" });
    }
}
