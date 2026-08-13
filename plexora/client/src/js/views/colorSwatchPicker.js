/**
 * @class ColorSwatchPicker - one-click curated color palette popover, with a
 * native <input type="color"> escape hatch for exact/custom hex values.
 */
class ColorSwatchPicker {
    constructor(mount, options = {}) {
        this.mount = mount;
        this.value = options.value || "#2388ff";
        this.presets = options.presets || ColorSwatchPicker.DEFAULT_PRESETS;
        this.onChange = options.onChange || (() => {});
        this.isOpen = false;
        this.render();
        this.bindDismissHandlers();
    }

    render() {
        this.mount.innerHTML = "";
        this.mount.classList.add("color-swatch-mount");

        this.button = document.createElement("button");
        this.button.type = "button";
        this.button.className = "channel-color-swatch";
        this.button.title = "Channel color";
        this.button.style.setProperty("--swatch-color", this.value);
        this.button.setAttribute("aria-haspopup", "true");
        this.button.setAttribute("aria-expanded", "false");
        this.button.addEventListener("click", (event) => {
            event.stopPropagation();
            this.toggle();
        });
        this.mount.appendChild(this.button);

        this.popover = document.createElement("div");
        this.popover.className = "color-swatch-popover";
        this.popover.hidden = true;
        this.popover.addEventListener("click", (event) => event.stopPropagation());

        const grid = document.createElement("div");
        grid.className = "color-swatch-grid";
        this.presets.forEach((preset) => {
            const swatch = document.createElement("button");
            swatch.type = "button";
            swatch.className = "color-swatch-option";
            swatch.title = preset.label;
            swatch.style.setProperty("--swatch-color", preset.hex);
            swatch.classList.toggle("is-selected", preset.hex.toLowerCase() === this.value.toLowerCase());
            swatch.addEventListener("click", () => this.selectColor(preset.hex));
            grid.appendChild(swatch);
        });
        this.popover.appendChild(grid);

        const customRow = document.createElement("label");
        customRow.className = "color-swatch-custom-row";
        const customLabel = document.createElement("span");
        customLabel.textContent = "Custom";
        customRow.appendChild(customLabel);
        this.customInput = document.createElement("input");
        this.customInput.type = "color";
        this.customInput.value = this.value;
        this.customInput.addEventListener("input", (event) => this.selectColor(event.target.value, { keepOpen: true }));
        customRow.appendChild(this.customInput);
        this.popover.appendChild(customRow);

        // Appended to <body> (a positioning "portal"), not this.mount: a disabled/dimmed
        // ancestor row has opacity < 1, which creates its own CSS stacking context and
        // would trap this popover's z-index behind the *next* row otherwise.
        document.body.appendChild(this.popover);
    }

    toggle() {
        if (this.isOpen) {
            this.close();
        } else {
            this.open();
        }
    }

    open() {
        if (ColorSwatchPicker.activeInstance && ColorSwatchPicker.activeInstance !== this) {
            ColorSwatchPicker.activeInstance.close();
        }
        ColorSwatchPicker.activeInstance = this;
        this.isOpen = true;
        this.positionPopover();
        this.popover.hidden = false;
        requestAnimationFrame(() => this.popover.classList.add("is-open"));
        this.button.setAttribute("aria-expanded", "true");
        this.bindDismissListeners();
    }

    positionPopover() {
        const rect = this.button.getBoundingClientRect();
        this.popover.style.left = `${rect.left}px`;
        this.popover.style.top = `${rect.bottom + 6}px`;
    }

    close() {
        this.isOpen = false;
        this.popover.classList.remove("is-open");
        this.button.setAttribute("aria-expanded", "false");
        window.setTimeout(() => {
            if (!this.isOpen) this.popover.hidden = true;
        }, 150);
        if (ColorSwatchPicker.activeInstance === this) {
            ColorSwatchPicker.activeInstance = null;
        }
        this.unbindDismissListeners();
    }

    bindDismissListeners() {
        if (this._dismissScroll) return;
        this._dismissScroll = (event) => {
            if (event.target === this.popover || this.popover.contains(event.target)) return;
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

    selectColor(hex, opts = {}) {
        this.setValue(hex);
        this.onChange(hex);
        if (!opts.keepOpen) this.close();
    }

    setValue(hex) {
        this.value = hex;
        this.button.style.setProperty("--swatch-color", hex);
        this.customInput.value = hex;
        this.popover.querySelectorAll(".color-swatch-option").forEach((el) => {
            el.classList.toggle("is-selected", el.style.getPropertyValue("--swatch-color").toLowerCase() === hex.toLowerCase());
        });
    }

    bindDismissHandlers() {
        document.addEventListener("click", (event) => {
            if (this.isOpen && !this.mount.contains(event.target) && !this.popover.contains(event.target)) {
                this.close();
            }
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && this.isOpen) {
                this.close();
            }
        });
    }
}

ColorSwatchPicker.DEFAULT_PRESETS = [
    { label: "Blue", hex: "#2388ff" },
    { label: "Red", hex: "#ff2d2d" },
    { label: "Green", hex: "#2bd46f" },
    { label: "White", hex: "#ffffff" },
    { label: "Yellow", hex: "#ffd60a" },
    { label: "Magenta", hex: "#ec4899" },
    { label: "Cyan", hex: "#22e6e6" },
    { label: "Orange", hex: "#f97316" },
    { label: "Violet", hex: "#a78bfa" },
    { label: "Gray", hex: "#94a3b8" },
];

ColorSwatchPicker.activeInstance = null;
