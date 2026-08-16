/**
 * @class GatingSidebarController - gating module's sidebar extension: gate-marker
 * selection, threshold slider/distribution plot, and "Save Gates to AnnData". Composed
 * against the core ViewerSidebar's public API rather than being part of it -- see
 * setGateMarker() below for the one confirmed cross-wire (mirroring the active gate
 * marker into channel slot 1 via sidebar.setSlotMarker()), which stays an explicit,
 * opportunistic call from the gating side into a stable core API, not the reverse.
 *
 * Registered with the core sidebar via ViewerSidebar#registerModule() -- see
 * pluginRegistry.js's `createSidebarController(ctx)` hook and its call site in main.js.
 */
class GatingSidebarController {
    constructor(ctx) {
        this.ctx = ctx;
        this.sidebar = ctx.sidebar;
        this.gatingList = ctx.moduleInstance;
        this.dataLayer = ctx.dataLayer;
        this.eventHandler = ctx.eventHandler;
        this.config = ctx.config;
        this.gateMarker = null;
        this.gateSlider = null;
        this.gateMarkerSelect = null;
        this.gateMarkerChangeTimer = null;
        this._saveGatingTimer = null;
        this._gatingSaveChain = null;
        this.gateDistributionScale = null;
    }

    // Called once from ViewerSidebar#init(), before the saved-state restore below.
    setup() {
        this.populateGateSelect();
        this.bindSaveToAnndata();

        // Thresholding is opened via the Tools menu (?tool=gating, see
        // tool_routes.py / toolLoader.js). Closing just hides the panel --
        // toolLoader.js owns the show/hide bookkeeping (and knows whether this
        // was a lazy client-side open or a direct/bookmarked server-rendered
        // one); this controller and its DOM stay alive so reopening later in
        // the same session is instant, no re-fetch/re-init.
        document.getElementById("gate_marker_close")?.addEventListener("click", () => {
            window.PlexoraToolLoader?.hideToolPanel("gating");
        });

        const gateAuto = document.getElementById("gate_auto_button");
        gateAuto.addEventListener("click", async () => {
            gateAuto.disabled = true;
            gateAuto.classList.add("auto-loading");
            try {
                await this.autoGate();
            } finally {
                gateAuto.disabled = false;
                gateAuto.classList.remove("auto-loading");
            }
        });

        // Registered through the plugin's cleanup list so deactivating the
        // plugin detaches it; this was previously a permanent window listener.
        const onResize = () => this.redrawGateSlider();
        window.addEventListener("resize", onResize);
        this.ctx?.onCleanup?.(() => window.removeEventListener("resize", onResize));
    }

    // Called by toolLoader.js right after unhiding the panel (both on first lazy
    // load and on every reopen). The slider/distribution plot measure their own
    // width via getBoundingClientRect() (redrawGateSlider/drawGateDistribution
    // below), which returns 0 while the panel is display:none -- redraw now that
    // it's actually visible so they don't render collapsed to zero width.
    onShow() {
        this.redrawGateSlider();
        this.drawGateDistribution();
    }

    // ViewerSidebar#init() restore-flow hooks (see registerModule()'s doc comment).
    fetchSaved() {
        return this.dataLayer.getSavedGatingList();
    }

    async applyOrDefault(savedGating) {
        if (savedGating && savedGating.length) {
            this.applySavedGating(savedGating);
            return;
        }
        // No gates saved in Plexora's own DB yet -- for an AnnData-backed
        // datasource, check whether adata.uns[table_name] already has gates
        // from outside Plexora (e.g. set on the source file before import)
        // before falling back to a blank default marker. Wrapped: unlike
        // the old fully-synchronous version, a rejected fetch here (network
        // hiccup, stale-cached dataLayer.js missing the method, unexpected
        // response shape) must still fall through to the default marker
        // below -- not leave the sidebar with nothing selected at all.
        if (this.config?.data_type === "anndata") {
            try {
                const gates = await this.dataLayer.getGatesFromAnndata();
                if (gates && Object.keys(gates).length) {
                    this.applyAnndataGates(gates);
                    return;
                }
            } catch (error) {
                console.error("Error loading gates from AnnData", error);
            }
        }
        this.setGateMarker(this.getGateMarkerNames()[1] || this.getGateMarkerNames()[0], { enableSlot: false });
    }

    // Seeds gating_channels with previously-saved AnnData gates (lower bound
    // only -- see anndata_gates.load_gates_from_anndata) so the marker
    // dropdown's gated indicator and slider reflect prior work done outside
    // Plexora as soon as the tool opens. Each channel's own current max
    // stands in for the missing upper bound. Marker selection still defers
    // to the normal default -- if that default happens to be a gated
    // channel its slider already reflects the loaded value; other gated
    // channels show via the dropdown's gated-indicator dot until picked.
    applyAnndataGates(gates) {
        for (const [channel, lowerBound] of Object.entries(gates)) {
            const range = this.getGateRange(channel);
            this.gatingList.gating_channels[channel] = [lowerBound, range[1]];
        }
        this.setGateMarker(this.getGateMarkerNames()[1] || this.getGateMarkerNames()[0], { enableSlot: false });
    }

    persistIfNeeded(hadSaved) {
        if (!hadSaved) this.persistGatingList();
    }

    // Wires the "Save Gates to AnnData" button/panel (adata.uns[table_name],
    // lower gate bound only, one column per image -- see anndata_gates.py).
    // Only meaningful for AnnData-backed datasources. The gates payload is
    // computed fresh from getCustomGatedChannels() at click time -- not from
    // the persisted GatingList row -- so every marker the user has actually
    // gated is included, not just whichever one is currently on screen.
    bindSaveToAnndata() {
        const button = document.getElementById("save_gates_anndata_button");
        const panel = document.getElementById("gating_save_anndata_panel");
        const confirmButton = document.getElementById("save_anndata_confirm");
        const cancelButton = document.getElementById("save_anndata_cancel");
        const tableNameInput = document.getElementById("save_anndata_table_name");
        const imageidColumnInput = document.getElementById("save_anndata_imageid_column");
        const status = document.getElementById("save_anndata_status");

        if (this.config?.data_type === "anndata") {
            button.hidden = false;
        }

        button.addEventListener("click", () => {
            status.textContent = "";
            status.className = "";
            panel.hidden = !panel.hidden;
        });

        cancelButton.addEventListener("click", () => {
            panel.hidden = true;
        });

        confirmButton.addEventListener("click", async () => {
            confirmButton.disabled = true;
            status.className = "";
            status.textContent = "Saving...";
            try {
                // The server derives gates from the persisted GatingList row
                // (the DB), not from anything sent here -- flush the current
                // in-memory state first so a gate set moments ago (still
                // inside the 400ms autosave debounce) isn't missed.
                await this.dataLayer.saveGatingList(this.gatingList.gating_channels, this.gatingList.selections);
                const result = await this.dataLayer.saveGatesToAnndata(
                    tableNameInput.value.trim() || "gates",
                    imageidColumnInput.value.trim() || "imageid"
                );
                status.className = "success";
                status.textContent = `Saved column "${result.image_id}" (${result.n_active_gates} markers).`;
            } catch (error) {
                status.className = "error";
                status.textContent = error.message || "Failed to save gates to AnnData";
            } finally {
                confirmButton.disabled = false;
            }
        });
    }

    populateGateSelect() {
        const names = this.getGateMarkerNames();
        const mount = document.getElementById("gate_marker_select");
        if (!this.gateMarkerSelect) {
            this.gateMarkerSelect = new SearchableSelect(mount, {
                options: names,
                value: this.gateMarker || "",
                placeholder: "Search markers…",
                getIndicator: (name) => this.describeGateIndicator(name),
                onChange: (name) => {
                    window.clearTimeout(this.gateMarkerChangeTimer);
                    this.gateMarkerChangeTimer = window.setTimeout(() => {
                        this.setGateMarker(name);
                    }, 0);
                },
            });
        } else {
            this.gateMarkerSelect.setOptions(names);
        }
    }

    getGateMarkerNames() {
        // sidebar.columns is the image channel list -- gate-able markers are
        // the feature table's own columns (e.g. adata.var_names), which are
        // frequently a different set of strings entirely. CSVGatingList
        // already computes exactly that list (see csvGatingList.js's init,
        // which filters get_datasource_description()'s output to columns
        // that actually have a 'histogram'), so reuse it here instead of
        // re-deriving a second, weaker version of the same filter.
        return [...this.gatingList.columns];
    }

    setGateMarker(name, options = {}) {
        if (!name) return;
        if (name === this.gateMarker && !options.force) return;
        const enableSlot = options.enableSlot !== false;
        this.gateMarker = name;
        if (this.gateMarkerSelect) {
            this.gateMarkerSelect.setValue(name);
        }
        this.ensureGateSelection(name);
        this.redrawGateSlider();
        this.drawGateDistribution();
        // Gating always works off the feature-table column (ensureGateSelection
        // above), independent of the image -- a gate marker is very often not
        // an image channel at all (adata.var_names vs. the image's channel
        // names are frequently different strings/lengths). Only mirror the
        // marker into a rendering slot when its name genuinely matches a real
        // image channel; otherwise leave the channel section untouched and let
        // the user find and enable the right channel themselves to verify the
        // gate visually. The correct check is against the image channel
        // vocabulary (imageChannels, keyed by full channel name -> tile
        // index), not the marker vocabulary.
        const hasMatchingImageChannel = this.ctx.dataset.image.has(this.dataLayer.getFullChannelName(name));
        if (options.syncSlot !== false && hasMatchingImageChannel) {
            this.sidebar.setSlotMarker(1, name, { keepColor: true, enable: enableSlot, reveal: enableSlot });
        }
        this.scheduleSaveGating();
    }

    ensureGateSelection(name) {
        const fullName = this.dataLayer.getFullChannelName(name);
        const range = this.gatingList.gating_channels[fullName] || this.getGateRange(name);
        this.gatingList.selections = {};
        this.gatingList.gating_channels[fullName] = range;
        this.gatingList.selections[fullName] = range;
        this.updateGateReadout(range);
        this.eventHandler.trigger(CSVGatingList.events.GATING_BRUSH_MOVE, this.gatingList.selections);
        this.eventHandler.trigger(CSVGatingList.events.GATING_BRUSH_END, this.gatingList.selections);
    }

    redrawGateSlider() {
        if (!this.gateMarker) return;
        const target = document.getElementById("gate_slider");
        target.innerHTML = "";
        const range = this.getGateRange(this.gateMarker);
        const values = this.gatingList.gating_channels[this.dataLayer.getFullChannelName(this.gateMarker)] || range;
        const width = Math.max(180, target.getBoundingClientRect().width - 16);
        const slider = d3.sliderBottom()
            .min(range[0])
            .max(range[1])
            .width(width)
            .ticks(0)
            .tickValues([])
            .default(values)
            .fill("#f36f45")
            .handle(d3.symbol().type(d3.symbolCircle).size(120))
            .on("onchange", (value) => this.setGateRange(value, CSVGatingList.events.GATING_BRUSH_MOVE))
            .on("end", (value) => this.setGateRange(value, CSVGatingList.events.GATING_BRUSH_END));

        this.gateSlider = slider;
        d3.select(target)
            .append("svg")
            .attr("width", width + 16)
            .attr("height", 44)
            .append("g")
            .attr("transform", "translate(8,18)")
            .call(slider);
        this.updateGateReadout(values);
    }

    setGateRange(values, eventName) {
        const fullName = this.dataLayer.getFullChannelName(this.gateMarker);
        const normalized = this.normalizeGateRange(values, this.getGateRange(this.gateMarker));
        this.gatingList.gating_channels[fullName] = normalized;
        this.gatingList.selections = {};
        this.gatingList.selections[fullName] = normalized;
        this.updateGateReadout(normalized);
        this.updateGateThresholdLines(normalized);
        this.eventHandler.trigger(eventName, this.gatingList.selections);
        if (eventName === CSVGatingList.events.GATING_BRUSH_END) {
            this.scheduleSaveGating();
        }
    }

    async autoGate() {
        if (!this.gateMarker) return;
        if (!(this.gateMarker in this.gatingList.hasGatingGMM)) {
            await this.gatingList.getGatingGMM(this.gateMarker);
        }
        const packet = this.gatingList.hasGatingGMM[this.gateMarker];
        if (!packet || packet.gate === undefined) return;
        const range = this.getGateRange(this.gateMarker);
        const factor = Math.pow(10, this.dataLayer.gateDecimals(range));
        const gate = Math.floor(parseFloat(packet.gate) * factor) / factor;
        const values = [gate, range[1]];
        if (this.gateSlider) {
            this.gateSlider.silentValue(values);
        }
        this.setGateRange(values, CSVGatingList.events.GATING_BRUSH_END);
        this.redrawGateSlider();
    }

    drawGateDistribution() {
        const target = document.getElementById("gate_distribution_plot");
        target.innerHTML = "";
        this.gateDistributionScale = null;
        if (!this.gateMarker) return;
        const fullName = this.dataLayer.getFullChannelName(this.gateMarker);
        const desc = this.sidebar.databaseDescription[fullName];
        const histogram = desc?.histogram || [];
        if (!histogram.length) return;

        const box = target.getBoundingClientRect();
        const width = Math.max(220, box.width || 280);
        const height = 120;
        const margin = { top: 12, right: 10, bottom: 24, left: 28 };
        const innerWidth = width - margin.left - margin.right;
        const innerHeight = height - margin.top - margin.bottom;
        const xDomain = d3.extent(histogram, (d) => d.x);
        const yMax = d3.max(histogram, (d) => d.y);
        const xScale = d3.scaleLinear().domain(xDomain).range([0, innerWidth]);
        const yScale = d3.scaleLinear().domain([0, yMax]).range([innerHeight, 0]);
        const line = d3.line()
            .x((d) => xScale(d.x))
            .y((d) => yScale(d.y))
            .curve(d3.curveMonotoneX);
        const values = this.gatingList.gating_channels[fullName] || this.getGateRange(this.gateMarker);

        const svg = d3.select(target)
            .append("svg")
            .attr("width", width)
            .attr("height", height);
        const g = svg.append("g").attr("transform", `translate(${margin.left},${margin.top})`);
        g.append("path")
            .datum(histogram)
            .attr("class", "sidebar-distribution-line")
            .attr("d", line);
        g.append("g")
            .attr("class", "gate-threshold-lines")
            .selectAll("line")
            .data(values)
            .enter()
            .append("line")
            .attr("class", "gate-threshold-line")
            .attr("x1", (value) => xScale(value))
            .attr("x2", (value) => xScale(value))
            .attr("y1", 0)
            .attr("y2", innerHeight);
        g.append("g")
            .attr("class", "distribution-axis")
            .attr("transform", `translate(0,${innerHeight})`)
            .call(d3.axisBottom(xScale).ticks(3).tickFormat(d3.format(".2f")));

        this.gateDistributionScale = xScale;
    }

    // Cheap per-tick update during a drag: reposition the existing threshold lines instead of
    // tearing down and rebuilding the whole histogram/axis (which was the source of drag lag).
    updateGateThresholdLines(values) {
        if (!this.gateDistributionScale) {
            this.drawGateDistribution();
            return;
        }
        const xScale = this.gateDistributionScale;
        d3.select("#gate_distribution_plot")
            .selectAll(".gate-threshold-line")
            .data(values)
            .attr("x1", (value) => xScale(value))
            .attr("x2", (value) => xScale(value));
    }

    applySavedGating(rows) {
        let activeRow = null;
        rows.forEach((row) => {
            if (!row || !row.channel || row.channel === "Lasso") return;
            this.gatingList.gating_channels[row.channel] = [row.gate_start, row.gate_end];
            if (row.gate_active) {
                activeRow = row;
            }
        });
        const marker = activeRow
            ? this.dataLayer.getShortChannelName(activeRow.channel)
            : (this.getGateMarkerNames()[1] || this.getGateMarkerNames()[0]);
        // syncSlot:false - applySavedChannels already placed every active channel (including
        // this one, if it was active) in its correct restored slot; letting setGateMarker's
        // normal slot-1 mirroring run here would clobber whatever channel actually belongs there.
        this.setGateMarker(marker, { force: true, syncSlot: false });
    }

    persistGatingList() {
        return this.dataLayer.saveGatingList(this.gatingList.gating_channels, this.gatingList.selections);
    }

    scheduleSaveGating() {
        if (this.sidebar.isRestoring()) return;
        window.clearTimeout(this._saveGatingTimer);
        this._saveGatingTimer = window.setTimeout(() => {
            this._gatingSaveChain = (this._gatingSaveChain || Promise.resolve()).then(() => this.persistGatingList());
        }, 400);
    }

    updateGateReadout(values) {
        document.getElementById("gate_min_value").textContent = this.sidebar.formatValue(values[0]);
        document.getElementById("gate_max_value").textContent = this.sidebar.formatValue(values[1]);
    }

    getGateRange(name) {
        const fullName = this.dataLayer.getFullChannelName(name);
        const desc = this.sidebar.databaseDescription[fullName] || {};
        return [desc.min || 0, desc.max || 1];
    }

    // A marker only counts as "gated" once its stored range differs from its
    // own full data range (getGateRange) -- ensureGateSelection() seeds every
    // marker the user merely browses to in the dropdown with that same full
    // range, so membership in gating_channels alone would over-count markers
    // nobody actually narrowed.
    hasCustomGate(name) {
        const fullName = this.dataLayer.getFullChannelName(name);
        const range = this.gatingList.gating_channels[fullName];
        if (!range) return false;
        const defaultRange = this.getGateRange(name);
        return range[0] !== defaultRange[0] || range[1] !== defaultRange[1];
    }

    // {fullChannelName: [low, high]} for every marker with a real, user-set
    // gate -- independent of gatingList.selections, which only ever holds
    // the single currently-displayed marker (ensureGateSelection() resets it
    // on every marker switch, by design, for the live single-marker slider/
    // segmentation-outline view). Used for exports (e.g. save-to-AnnData)
    // that need *all* gated markers, not just the one on screen right now.
    getCustomGatedChannels() {
        const result = {};
        for (const name of this.gatingList.columns) {
            if (!this.hasCustomGate(name)) continue;
            const fullName = this.dataLayer.getFullChannelName(name);
            result[fullName] = this.gatingList.gating_channels[fullName];
        }
        return result;
    }

    // Hover-tooltip text for the marker dropdown's gated-indicator dot; null
    // means "don't show a dot" (SearchableSelect skips rendering it).
    describeGateIndicator(name) {
        if (!this.hasCustomGate(name)) return null;
        const fullName = this.dataLayer.getFullChannelName(name);
        const range = this.gatingList.gating_channels[fullName];
        return `Gated ${this.sidebar.formatValue(range[0])}–${this.sidebar.formatValue(range[1])}`;
    }

    // Gate-specific rounding: precision is derived from the channel's own
    // observed range (dataLayer.gateDecimals) instead of the isTransformed
    // config flag, so it can't silently drift out of sync with the data's
    // actual scale. Floors the low handle / ceils the high handle (at that
    // precision) so rounding never excludes boundary cells.
    normalizeGateRange(values, range) {
        const sorted = [...values].map((value) => parseFloat(value)).sort((a, b) => a - b);
        const factor = Math.pow(10, this.dataLayer.gateDecimals(range));
        return [Math.floor(sorted[0] * factor) / factor, Math.ceil(sorted[1] * factor) / factor];
    }
}
