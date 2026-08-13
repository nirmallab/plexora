/**
 * @class ViewerSidebar - unified controls for marker gating and image channels.
 */
class ViewerSidebar {
    constructor(config, columns, dataLayer, eventHandler, channelList) {
        this.config = config;
        this.columns = [...columns];
        this.dataLayer = dataLayer;
        this.eventHandler = eventHandler;
        this.channelList = channelList;
        this.databaseDescription = {};
        this.channelSlots = [];
        this.channelSlotSliders = new Map();
        // Remembers a manually-set intensity range per marker name (not per slot), so
        // switching a slot's marker away and back doesn't discard what the user tuned.
        this.markerRangeOverrides = new Map();
        this.colorPickers = new Map();
        this.markerSelects = new Map();
        this._saveChannelsTimer = null;
        this._restoring = false;
        // Add-on modules (e.g. gating) that extend the sidebar without core needing to know
        // their concrete type -- see registerModule() and appModules.js.
        this.sidebarModules = [];
        this.maxChannelSlots = 15;
        this.initialChannelSlots = 4;
        this.defaultColors = [
            { label: "Blue", hex: "#2388ff", rgb: { r: 35, g: 136, b: 255 } },
            { label: "Red", hex: "#ff2d2d", rgb: { r: 255, g: 45, b: 45 } },
            { label: "Green", hex: "#2bd46f", rgb: { r: 43, g: 212, b: 111 } },
            { label: "White", hex: "#ffffff", rgb: { r: 255, g: 255, b: 255 } },
        ];
        // Per-channel range sliders live in [0, 255] byte units by default
        // (matching the quantized WebP tile data 1:1) and switch to raw
        // 16-bit units when HD is on (see getImageRange/toImageConnectorRange/
        // autoChannel below, and frag.glsl's u8_r_range for why this matters:
        // without it the slider's domain doesn't match the encoded data's
        // domain, which is what caused visible banding).
        window.addEventListener("plexora:hd-mode-changed", (e) => this.onHdModeChanged(Boolean(e.detail?.enabled)));
    }

    isHdMode() {
        return Boolean(window.__plexora?.seaDragonViewer?.viewerManagerVMain?.isHdMode?.());
    }

    /**
     * @function rawToByteRange
     * Maps a [min, max] pair in raw 16-bit units into the [0, 255] byte
     * domain the server actually quantized against (packet.qmin/qmax --
     * see get_channel_gmm), clamped to a valid byte range.
     */
    rawToByteRange([rmin, rmax], packet) {
        const span = Math.max(packet.qmax - packet.qmin, 1);
        const toByte = (v) => Math.min(255, Math.max(0, Math.round(((v - packet.qmin) / span) * 255)));
        return [toByte(rmin), toByte(rmax)];
    }

    /**
     * @function byteToRawRange
     * Inverse of rawToByteRange: maps a [min, max] pair in [0, 255] byte
     * units back into raw 16-bit units.
     */
    byteToRawRange([bmin, bmax], packet) {
        const span = Math.max(packet.qmax - packet.qmin, 1);
        const toRaw = (v) => packet.qmin + (v / 255) * span;
        return [toRaw(bmin), toRaw(bmax)];
    }

    /**
     * @function toRawRangeForSlot
     * slot.range is byte-domain in default mode, raw in HD mode (see
     * onHdModeChanged) -- this always returns raw 16-bit units, for
     * persistence (persistChannelList) which must stay domain-independent
     * since it's read back across sessions/mode changes on restore.
     * Falls back to slot.range unconverted if no GMM packet is cached yet
     * (shouldn't happen for an active/auto-leveled slot).
     */
    toRawRangeForSlot(slot) {
        if (this.isHdMode()) return slot.range;
        const packet = this.channelList.hasChannelGMM[slot.name];
        if (!packet) return slot.range;
        return this.byteToRawRange(slot.range, packet);
    }

    /**
     * @function onHdModeChanged
     * Remaps every active channel slot's current range (whatever the user
     * has it set to, default or manually adjusted) into the newly-active
     * domain, so the visible contrast window doesn't silently jump/reset
     * on toggle, then updates the slider bounds and repaints.
     */
    onHdModeChanged(enabled) {
        this.channelSlots.forEach((slot) => {
            if (!slot.name) return;
            const packet = this.channelList.hasChannelGMM[slot.name];
            if (!packet) return;
            slot.range = enabled
                ? this.byteToRawRange(slot.range, packet)
                : this.rawToByteRange(slot.range, packet);
            this.setSlotRange(slot.index, slot.range, slot.userRangeChanged);
            if (slot.expanded) {
                this.redrawChannelSlider(slot);
            } else {
                slot.sliderDirty = true;
            }
        });
    }

    async init(databaseDescription) {
        this.databaseDescription = databaseDescription;
        this.setupSidebarShell();
        this.sidebarModules.forEach((m) => m.setup && m.setup());
        this.bindActions();

        const maxLabel = document.getElementById("max-channels");
        if (maxLabel) maxLabel.textContent = this.maxChannelSlots;

        const [savedChannels, ...moduleSaved] = await Promise.all([
            this.dataLayer.getSavedChannelList(),
            ...this.sidebarModules.map((m) => (m.fetchSaved ? m.fetchSaved() : Promise.resolve(null))),
        ]);

        // Suppressed while restoring: applySavedChannels/a module's own apply-from-saved
        // reuse the same setters live edits use, which otherwise schedule an autosave on
        // every call - turning "load from DB" into "load from DB, then immediately write
        // back to DB".
        this._restoring = true;
        if (savedChannels && savedChannels.length) {
            await this.applySavedChannels(savedChannels);
        } else {
            this.initChannelSlots();
            this.applyInitialChannels();
        }

        this.sidebarModules.forEach((m, i) => m.applyOrDefault && m.applyOrDefault(moduleSaved[i]));
        this._restoring = false;

        if (!(savedChannels && savedChannels.length)) this.persistChannelList();
        this.sidebarModules.forEach((m, i) => m.persistIfNeeded && m.persistIfNeeded(Boolean(moduleSaved[i] && moduleSaved[i].length)));
    }

    /**
     * Registers an add-on module's sidebar controller (see appModules.js). The controller
     * may implement setup(), fetchSaved(), applyOrDefault(savedRows), and
     * persistIfNeeded(hadSaved) -- all optional, called at the matching point in init()
     * above, alongside the core channel-slot restore flow.
     */
    registerModule(moduleController) {
        this.sidebarModules.push(moduleController);
    }

    isRestoring() {
        return this._restoring;
    }

    setupSidebarShell() {
        const collapseButton = document.getElementById("sidebar_collapse_button");
        const expandButton = document.getElementById("sidebar_expand_button");
        const shell = document.getElementById("bodyDiv");
        const toggleSidebar = () => {
            if (shell) {
                shell.classList.toggle("sidebar-collapsed");
            }
        };
        if (collapseButton) {
            collapseButton.addEventListener("click", toggleSidebar);
        }
        if (expandButton) {
            expandButton.addEventListener("click", toggleSidebar);
        }
    }

    bindActions() {
        const addButton = document.getElementById("add_channel_button");
        addButton.addEventListener("click", () => this.addFirstAvailableChannel());

        window.addEventListener("resize", () => {
            this.redrawChannelSliders();
        });
    }

    initChannelSlots() {
        const slotList = document.getElementById("channel_slot_list");
        slotList.innerHTML = "";
        this.channelSlots = [...Array(this.initialChannelSlots).keys()].map((slotIndex) => {
            const color = this.getDefaultColor(slotIndex);
            const name = this.columns[slotIndex] || "";
            const slot = {
                index: slotIndex,
                name,
                color: color.rgb,
                colorHex: color.hex,
                enabled: slotIndex === 0 && Boolean(name),
                visible: Boolean(name),
                expanded: false,
                sliderDirty: false,
                range: this.getImageRange(name),
                userColorChanged: false,
                userRangeChanged: false,
                autoLeveled: false,
                autoLeveling: false,
            };
            slotList.appendChild(this.createChannelSlot(slot));
            return slot;
        });
        this.updateSelectedCount();
        this.redrawChannelSliders();
    }

    createChannelSlot(slot) {
        const row = document.createElement("div");
        row.classList.add("channel-slot");
        row.classList.toggle("is-hidden", !slot.visible);
        row.classList.toggle("is-disabled", !slot.enabled);
        row.setAttribute("data-slot", slot.index);
        row.style.setProperty("--slot-color", slot.colorHex);

        const top = document.createElement("div");
        top.classList.add("channel-slot-top");
        row.appendChild(top);

        const toggle = document.createElement("input");
        toggle.type = "checkbox";
        toggle.classList.add("channel-toggle-switch");
        toggle.checked = slot.enabled;
        toggle.title = "Toggle channel";
        toggle.addEventListener("change", (event) => {
            this.setSlotEnabled(slot.index, event.target.checked);
        });
        top.appendChild(toggle);

        const colorMount = document.createElement("div");
        top.appendChild(colorMount);
        const colorPicker = new ColorSwatchPicker(colorMount, {
            value: slot.colorHex,
            onChange: (hex) => this.setSlotColor(slot.index, hex, true),
        });
        this.colorPickers.set(slot.index, colorPicker);

        const comboMount = document.createElement("div");
        top.appendChild(comboMount);
        const markerSelect = new SearchableSelect(comboMount, {
            options: this.columns,
            value: slot.name,
            placeholder: "Select marker…",
            describeOption: (name) => this.describeMarkerOption(name, slot.index),
            onChange: (name) => this.setSlotMarker(slot.index, name, { keepColor: true, enable: true }),
        });
        this.markerSelects.set(slot.index, markerSelect);

        const expandToggle = document.createElement("button");
        expandToggle.type = "button";
        expandToggle.classList.add("channel-slot-expand-toggle");
        expandToggle.classList.toggle("is-expanded", Boolean(slot.expanded));
        expandToggle.title = "Show threshold range";
        expandToggle.innerHTML = '<span class="fas fa-chevron-down"></span>';
        expandToggle.addEventListener("click", () => this.toggleSlotExpanded(slot.index));
        top.appendChild(expandToggle);

        const remove = document.createElement("button");
        remove.type = "button";
        remove.classList.add("slot-remove-button");
        remove.title = "Remove channel slot";
        remove.innerHTML = '<span class="fas fa-times"></span>';
        remove.addEventListener("click", () => this.removeChannelSlot(slot.index));
        top.appendChild(remove);

        const detail = document.createElement("div");
        detail.classList.add("channel-slot-detail");
        detail.classList.toggle("is-expanded", Boolean(slot.expanded));

        const detailHeader = document.createElement("div");
        detailHeader.classList.add("slot-detail-header");

        const values = document.createElement("div");
        values.classList.add("range-readout", "slot-range-readout");
        values.innerHTML = `<span id="channel_slot_min_${slot.index}">0.00</span><span id="channel_slot_max_${slot.index}">0.00</span>`;
        detailHeader.appendChild(values);

        const auto = document.createElement("button");
        auto.type = "button";
        auto.classList.add("slot-auto-button");
        auto.title = "Auto-set threshold range from data";
        auto.textContent = "Auto";
        auto.addEventListener("click", () => this.autoChannel(slot.index, { force: true }));
        detailHeader.appendChild(auto);

        detail.appendChild(detailHeader);

        const slider = document.createElement("div");
        slider.classList.add("sidebar-slider");
        slider.setAttribute("id", `channel_slot_slider_${slot.index}`);
        detail.appendChild(slider);

        row.appendChild(detail);

        return row;
    }

    applyInitialChannels() {
        this.channelSlots.forEach((slot) => {
            if (slot.name && slot.enabled) {
                this.activateChannel(slot);
            }
        });
        this.updateSelectedCount();
    }

    setSlotMarker(slotIndex, name, options = {}) {
        const slot = this.channelSlots[slotIndex];
        if (!slot || !name) return;
        const markerChanged = slot.name !== name;
        const enablesSlot = options.enable && !slot.enabled;
        const revealsSlot = options.reveal && !slot.visible;
        if (!markerChanged && !enablesSlot && !revealsSlot && !options.force) return;
        if (slot.name && slot.enabled && markerChanged) {
            this.deactivateChannel(slot);
        }
        this.disableDuplicateChannels(name, slotIndex);
        slot.name = name;
        if (markerChanged) {
            const override = this.markerRangeOverrides.get(name);
            if (override) {
                slot.range = [...override];
                slot.userRangeChanged = true;
                slot.autoLeveled = true;
            } else {
                slot.range = this.getImageRange(name);
                slot.userRangeChanged = false;
                slot.autoLeveled = false;
            }
            slot.autoLeveling = false;
            slot.expanded = true;
            slot.sliderDirty = true;
        }
        if (!options.keepColor) {
            this.setSlotColor(slotIndex, this.getDefaultColor(slotIndex).hex, false);
        }
        if (options.enable) {
            slot.enabled = true;
        }
        if (options.reveal || options.enable) {
            slot.visible = true;
        }
        this.syncSlotDom(slot);
        this.applySlotExpansion(slot);
        if (slot.enabled) {
            this.activateChannel(slot);
            this.autoLevelChannelIfNeeded(slot);
        }
        this.updateSelectedCount();
        this.scheduleSaveChannels();
    }

    setSlotEnabled(slotIndex, enabled) {
        const slot = this.channelSlots[slotIndex];
        if (!slot || !slot.name) return;
        const wasEnabled = slot.enabled;
        slot.enabled = enabled;
        slot.visible = true;
        if (enabled) {
            this.disableDuplicateChannels(slot.name, slotIndex);
            this.activateChannel(slot);
            if (!wasEnabled) {
                this.autoLevelChannelIfNeeded(slot);
            }
        } else {
            this.deactivateChannel(slot);
        }
        this.syncSlotDom(slot);
        this.updateSelectedCount();
        this.scheduleSaveChannels();
    }

    setSlotColor(slotIndex, hex, userColorChanged) {
        const slot = this.channelSlots[slotIndex];
        if (!slot) return;
        slot.colorHex = hex;
        slot.color = this.hexToRgb(hex);
        slot.userColorChanged = Boolean(userColorChanged || slot.userColorChanged);
        this.syncSlotDom(slot);
        if (slot.enabled && slot.name) {
            this.eventHandler.trigger(ChannelList.events.COLOR_TRANSFER_CHANGE, {
                name: slot.name,
                type: "white",
                color: d3.rgb(slot.color.r, slot.color.g, slot.color.b),
            });
        }
        this.scheduleSaveChannels();
    }

    activateChannel(slot) {
        const fullName = this.dataLayer.getFullChannelName(slot.name);
        const channelIdx = imageChannels[fullName];
        if (channelIdx === undefined) return;
        this.channelList.image_channels[slot.name] = slot.range;
        this.channelList.rangeConnector[channelIdx] = this.toImageConnectorRange(slot.range);
        this.channelList.colorConnector[channelIdx] = { color: slot.color };
        if (!this.channelList.selections.includes(slot.name)) {
            this.channelList.selections.push(slot.name);
        }
        this.channelList.sel[fullName] = slot.range;
        this.eventHandler.trigger(ChannelList.events.CHANNELS_CHANGE, {
            selections: this.channelList.selections,
            name: slot.name,
            status: true,
        });
        this.eventHandler.trigger(ChannelList.events.COLOR_TRANSFER_CHANGE, {
            name: slot.name,
            type: "white",
            color: d3.rgb(slot.color.r, slot.color.g, slot.color.b),
        });
        this.eventHandler.trigger(ChannelList.events.BRUSH_MOVE, {
            name: slot.name,
            dataRange: [...slot.range],
        });
    }

    deactivateChannel(slot) {
        const fullName = this.dataLayer.getFullChannelName(slot.name);
        this.channelList.selections = _.pull(this.channelList.selections, slot.name);
        delete this.channelList.sel[fullName];
        this.eventHandler.trigger(ChannelList.events.CHANNELS_CHANGE, {
            selections: this.channelList.selections,
            name: slot.name,
            status: false,
        });
    }

    disableDuplicateChannels(name, currentSlotIndex) {
        this.channelSlots.forEach((slot) => {
            if (slot.index !== currentSlotIndex && slot.enabled && slot.name === name) {
                slot.enabled = false;
                this.deactivateChannel(slot);
                this.syncSlotDom(slot);
            }
        });
    }

    redrawChannelSliders() {
        this.channelSlots.filter((slot) => slot.visible && slot.expanded).forEach((slot) => this.redrawChannelSlider(slot));
    }

    redrawChannelSlider(slot) {
        if (!slot || !slot.name) return;
        this.updateSlotReadout(slot);
        if (!slot.expanded) {
            slot.sliderDirty = true;
            return;
        }
        slot.sliderDirty = false;
        const target = document.getElementById(`channel_slot_slider_${slot.index}`);
        if (!target) return;
        target.innerHTML = "";
        const range = this.getImageRange(slot.name);
        const width = Math.max(180, target.getBoundingClientRect().width - 16);
        const slider = d3.sliderBottom(d3.scaleLog())
            .min(Math.max(range[0], 1))
            .max(Math.max(range[1], 2))
            .width(width)
            .ticks(0)
            .tickValues([])
            .default([Math.max(slot.range[0], 1), Math.max(slot.range[1], 2)])
            .fill("#38bdf8")
            .handle(d3.symbol().type(d3.symbolCircle).size(120))
            .on("onchange", (value) => this.setSlotRange(slot.index, value, true))
            .on("end", () => this.scheduleSaveChannels());

        this.channelSlotSliders.set(slot.index, slider);
        d3.select(target)
            .append("svg")
            .attr("width", width + 16)
            .attr("height", 44)
            .append("g")
            .attr("transform", "translate(8,18)")
            .call(slider);
    }

    toggleSlotExpanded(slotIndex) {
        const slot = this.channelSlots[slotIndex];
        if (!slot) return;
        slot.expanded = !slot.expanded;
        this.applySlotExpansion(slot);
    }

    applySlotExpansion(slot) {
        const row = document.querySelector(`.channel-slot[data-slot="${slot.index}"]`);
        if (!row) return;
        const detail = row.querySelector(".channel-slot-detail");
        const toggle = row.querySelector(".channel-slot-expand-toggle");
        if (detail) detail.classList.toggle("is-expanded", Boolean(slot.expanded));
        if (toggle) toggle.classList.toggle("is-expanded", Boolean(slot.expanded));
        if (slot.expanded && (slot.sliderDirty || !this.channelSlotSliders.has(slot.index))) {
            this.redrawChannelSlider(slot);
        }
    }

    describeMarkerOption(name, currentSlotIndex) {
        const activeElsewhere = this.channelSlots.some(
            (slot) => slot.index !== currentSlotIndex && slot.enabled && slot.name === name
        );
        return activeElsewhere ? "active elsewhere" : "";
    }

    setSlotRange(slotIndex, values, userChanged = false) {
        const slot = this.channelSlots[slotIndex];
        if (!slot) return;
        slot.range = this.normalizeRange(values, true);
        if (userChanged) {
            slot.userRangeChanged = true;
            if (slot.name) {
                this.markerRangeOverrides.set(slot.name, [...slot.range]);
            }
        }
        this.channelList.image_channels[slot.name] = slot.range;
        this.updateSlotReadout(slot);
        if (slot.enabled && slot.name) {
            this.eventHandler.trigger(ChannelList.events.BRUSH_MOVE, {
                name: slot.name,
                dataRange: [...slot.range],
            });
        }
    }

    autoLevelChannelIfNeeded(slot) {
        if (!slot || slot.autoLeveled || slot.autoLeveling || slot.userRangeChanged) {
            return;
        }
        window.setTimeout(() => this.autoChannel(slot.index), 0);
    }

    async autoChannel(slotIndex, options = {}) {
        const slot = this.channelSlots[slotIndex];
        if (!slot || !slot.name) return;
        if (!options.force && (slot.userRangeChanged || slot.autoLeveled || slot.autoLeveling)) {
            return;
        }
        const markerName = slot.name;
        slot.autoLeveling = true;
        if (!(slot.name in this.channelList.hasChannelGMM)) {
            await this.channelList.getAndDrawChannelGMM(slot.name);
        }
        slot.autoLeveling = false;
        if (slot.name !== markerName) {
            return;
        }
        const packet = this.channelList.hasChannelGMM[slot.name];
        if (!packet || (!options.force && slot.userRangeChanged)) return;
        slot.range = this.isHdMode() ? [packet.vmin, packet.vmax] : this.rawToByteRange([packet.vmin, packet.vmax], packet);
        slot.autoLeveled = true;
        const slider = this.channelSlotSliders.get(slotIndex);
        if (slider) {
            slider.silentValue(slot.range);
        }
        this.setSlotRange(slotIndex, slot.range, false);
        this.redrawChannelSlider(slot);
    }

    addFirstAvailableChannel() {
        const emptySlot = this.channelSlots.find((slot) => !slot.visible);
        const activeNames = this.channelSlots.filter((slot) => slot.enabled).map((slot) => slot.name);
        const usedNames = this.channelSlots.filter((slot) => slot.name).map((slot) => slot.name);
        const slot = emptySlot || this.createAdditionalSlot(usedNames);
        if (!slot) return;
        const next = slot.name && !activeNames.includes(slot.name)
            ? slot.name
            : this.columns.find((name) => !usedNames.includes(name)) || this.columns.find((name) => !activeNames.includes(name));
        if (next) {
            this.setSlotMarker(slot.index, next, { keepColor: true, enable: false, reveal: true });
        }
    }

    createAdditionalSlot(usedNames) {
        if (this.channelSlots.length >= this.maxChannelSlots) return null;
        const slotIndex = this.channelSlots.length;
        const color = this.getDefaultColor(slotIndex);
        const name = this.columns.find((column) => !usedNames.includes(column)) || "";
        const slot = {
            index: slotIndex,
            name,
            color: color.rgb,
            colorHex: color.hex,
            enabled: false,
            visible: true,
            expanded: false,
            sliderDirty: false,
            range: this.getImageRange(name),
            userColorChanged: false,
            userRangeChanged: false,
            autoLeveled: false,
            autoLeveling: false,
        };
        this.channelSlots.push(slot);
        document.getElementById("channel_slot_list").appendChild(this.createChannelSlot(slot));
        return slot;
    }

    removeChannelSlot(slotIndex) {
        const slot = this.channelSlots[slotIndex];
        if (!slot) return;
        if (slot.enabled && slot.name) {
            this.deactivateChannel(slot);
        }
        const color = this.getDefaultColor(slotIndex);
        slot.name = "";
        slot.range = this.getImageRange("");
        slot.enabled = false;
        slot.visible = false;
        slot.expanded = false;
        slot.sliderDirty = false;
        slot.color = color.rgb;
        slot.colorHex = color.hex;
        slot.userColorChanged = false;
        slot.userRangeChanged = false;
        slot.autoLeveled = false;
        slot.autoLeveling = false;
        this.channelSlotSliders.delete(slotIndex);
        this.syncSlotDom(slot);
        this.applySlotExpansion(slot);
        this.updateSelectedCount();
        this.scheduleSaveChannels();
    }

    syncSlotDom(slot) {
        const row = document.querySelector(`.channel-slot[data-slot="${slot.index}"]`);
        if (!row) return;
        row.classList.toggle("is-hidden", !slot.visible);
        row.classList.toggle("is-disabled", !slot.enabled);
        row.style.setProperty("--slot-color", slot.colorHex);
        const toggle = row.querySelector(".channel-toggle-switch");
        if (toggle) toggle.checked = slot.enabled;
        const colorPicker = this.colorPickers.get(slot.index);
        if (colorPicker) colorPicker.setValue(slot.colorHex);
        const markerSelect = this.markerSelects.get(slot.index);
        if (markerSelect) markerSelect.setValue(slot.name);
        this.updateSlotReadout(slot);
    }

    async applySavedChannels(rows) {
        const activeRows = rows.filter((row) => row && row.channel_active);
        const slotList = document.getElementById("channel_slot_list");
        slotList.innerHTML = "";
        this.channelSlots = [];
        this.channelSlotSliders.clear();
        this.colorPickers.clear();
        this.markerSelects.clear();

        const count = Math.min(Math.max(activeRows.length, this.initialChannelSlots), this.maxChannelSlots);
        // Slots beyond the active rows aren't assigned by a saved row at all, but they should
        // still show a marker (off) rather than sit empty, matching the pre-restore default look.
        const usedNames = new Set(activeRows.slice(0, count).map((row) => row.channel));
        const fallbackNames = this.columns.filter((name) => !usedNames.has(name));
        let fallbackIdx = 0;
        for (let i = 0; i < count; i++) {
            const color = this.getDefaultColor(i);
            const name = i < activeRows.length ? "" : (fallbackNames[fallbackIdx++] || "");
            const slot = {
                index: i,
                name,
                color: color.rgb,
                colorHex: color.hex,
                enabled: false,
                visible: true,
                expanded: false,
                sliderDirty: false,
                range: this.getImageRange(name),
                userColorChanged: false,
                userRangeChanged: false,
                autoLeveled: false,
                autoLeveling: false,
            };
            this.channelSlots.push(slot);
            slotList.appendChild(this.createChannelSlot(slot));
        }

        for (const [i, row] of activeRows.slice(0, count).entries()) {
            const slot = this.channelSlots[i];
            if (!slot) continue;
            this.setSlotMarker(slot.index, row.channel, { keepColor: true, enable: true, force: true });
            this.setSlotColor(slot.index, this.rgbToHex(row.r, row.g, row.b), true);
            // row.start/row.end are always raw 16-bit units (see
            // persistChannelList/toRawRangeForSlot) -- convert to the
            // currently-active domain before assigning to slot.range.
            let range = [row.start, row.end];
            if (!this.isHdMode()) {
                if (!(slot.name in this.channelList.hasChannelGMM)) {
                    await this.channelList.getAndDrawChannelGMM(slot.name);
                }
                const packet = this.channelList.hasChannelGMM[slot.name];
                if (packet) {
                    range = this.rawToByteRange(range, packet);
                }
            }
            this.setSlotRange(slot.index, range, true);
            slot.expanded = false;
            this.applySlotExpansion(slot);
        }
        this.updateSelectedCount();
    }

    rgbToHex(r, g, b) {
        const toHex = (value) => Math.max(0, Math.min(255, Math.round(value))).toString(16).padStart(2, "0");
        return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
    }

    persistChannelList() {
        const listChannels = {};
        // Must cover every entry map_channels (imageChannelsIdx, all real image
        // channels) can reference server-side -- NOT this.columns (gating markers
        // only). A structural/counterstain channel like DNA is commonly a real
        // image channel with no corresponding feature-table column (no histogram),
        // so it's excluded from this.columns but still present in imageChannelsIdx;
        // basing this loop on this.columns left it with no listChannels entry at
        // all, which the server then KeyErrors on since map_channels expects one
        // for every image channel. getImageRange(name) already resolves a DNA-like
        // channel's real image_min/image_max fine -- it just was never called for it.
        Object.values(imageChannelsIdx).forEach((name) => {
            listChannels[name] = this.channelList.image_channels[name] || this.getRawImageRange(name);
        });
        const activeChannels = {};
        const listColors = {};
        const listRanges = {};
        const bitMax = this.dataLayer.imageBitRange[1];
        this.channelSlots.forEach((slot) => {
            if (!slot.name || !slot.enabled) return;
            const fullName = this.dataLayer.getFullChannelName(slot.name);
            const idx = imageChannels[fullName];
            if (idx === undefined) return;
            // Persisted state must be domain-independent (always raw 16-bit
            // units) since it's read back across sessions and across mode
            // changes -- slot.range itself is byte-domain in default mode
            // (see toRawRangeForSlot).
            const rawRange = this.toRawRangeForSlot(slot);
            activeChannels[idx] = true;
            listColors[idx] = { color: { ...slot.color, opacity: 1 } };
            listRanges[idx] = [rawRange[0] / bitMax, rawRange[1] / bitMax];
            listChannels[slot.name] = rawRange;
        });
        return this.dataLayer.saveChannelList(imageChannelsIdx, activeChannels, listColors, listRanges, listChannels);
    }

    scheduleSaveChannels() {
        if (this._restoring) return;
        window.clearTimeout(this._saveChannelsTimer);
        this._saveChannelsTimer = window.setTimeout(() => {
            // Chained (not just debounced): a slow/out-of-order response from an earlier
            // save could otherwise land after a newer one and silently overwrite it.
            this._channelSaveChain = (this._channelSaveChain || Promise.resolve()).then(() => this.persistChannelList());
        }, 400);
    }

    updateSelectedCount() {
        const count = this.channelSlots.filter((slot) => slot.enabled && slot.name).length;
        const countElement = document.getElementById("num-selected-channels");
        if (countElement) countElement.textContent = count;
        const addButton = document.getElementById("add_channel_button");
        if (addButton) addButton.disabled = this.channelSlots.filter((slot) => slot.visible).length >= this.maxChannelSlots;
    }

    updateSlotReadout(slot) {
        const min = document.getElementById(`channel_slot_min_${slot.index}`);
        const max = document.getElementById(`channel_slot_max_${slot.index}`);
        if (min) min.textContent = this.formatValue(slot.range[0]);
        if (max) max.textContent = this.formatValue(slot.range[1]);
    }

    // Raw 16-bit bounds for a channel, regardless of current mode -- the
    // stable representation used for persistence and as the HD-mode slider
    // domain. getImageRange() below is the mode-aware wrapper UI code should
    // normally call instead.
    getRawImageRange(name) {
        if (!name) return [0, 1];
        const fullName = this.dataLayer.getFullChannelName(name);
        const desc = this.databaseDescription[fullName] || {};
        return [desc.image_min || this.dataLayer.imageBitRange[0] || 0, desc.image_max || this.dataLayer.imageBitRange[1] || 65536];
    }

    getImageRange(name) {
        if (!name) return [0, 1];
        if (!this.isHdMode()) {
            // Default mode: the slider works directly in the same [0, 255]
            // byte domain the server quantized into -- see rawToByteRange.
            return [0, 255];
        }
        return this.getRawImageRange(name);
    }

    toImageConnectorRange(values) {
        if (!this.isHdMode()) {
            return [values[0] / 255, values[1] / 255];
        }
        const defaultRange = this.dataLayer.imageBitRange;
        return [values[0] / defaultRange[1], values[1] / defaultRange[1]];
    }

    normalizeRange(values, keepFloat) {
        const sorted = [...values].map((value) => parseFloat(value)).sort((a, b) => a - b);
        if (keepFloat) {
            return sorted;
        }
        return [Math.floor(sorted[0]), Math.ceil(sorted[1])];
    }

    formatValue(value) {
        return Number.parseFloat(value || 0).toFixed(2);
    }

    hexToRgb(hex) {
        const cleaned = hex.replace("#", "");
        const value = parseInt(cleaned, 16);
        return {
            r: (value >> 16) & 255,
            g: (value >> 8) & 255,
            b: value & 255,
        };
    }

    getDefaultColor(slotIndex) {
        return this.defaultColors[slotIndex % this.defaultColors.length];
    }
}
