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

// Datasource kinds whose gates can be written back to the source file, i.e.
// those backed by an AnnData group: an .h5ad, or the one table a SpatialData
// import selected out of its .zarr store. Must stay in step with the same
// check in the gating plugin's routes.py. CSV has nowhere to write to.
const SAVEABLE_SOURCE_TYPES = ["anndata", "spatialdata"];

// Z and X walk the marker list: side by side under the left hand while the
// right is on the slider. Bare letters nothing else binds -- ROI takes
// V/P/F/R, viewerControls T, the dataset nav B/N, and OpenSeadragon pans on
// W/A/S/D once the canvas has focus.
const MARKER_KEYS = { z: -1, x: 1 };

class GatingSidebarController {
    constructor(ctx) {
        this.ctx = ctx;
        this.sidebar = ctx.sidebar;
        this.gatingList = ctx.moduleInstance;
        this.dataLayer = ctx.dataLayer;
        this.api = new GatingApi(ctx);
        this.eventHandler = ctx.eventHandler;
        this.config = ctx.config;
        this.gateMarker = null;
        this.gateSlider = null;
        this.gateMarkerSelect = null;
        this.gateMarkerChangeTimer = null;
        this._saveGatingTimer = null;
        this._gatingSaveChain = null;
        this.gateDistributionScale = null;
        //: fullChannelName -> the gate that was on screen when Auto Threshold
        //: was pressed for it, or absent once it has been put back.
        //:
        //: Per MARKER and not per control, because the control is one slider
        //: shown for whichever marker is selected: keeping a single pending
        //: revert on `this` would offer, on marker B, to restore a range
        //: belonging to marker A. Never persisted -- it is an undo of the last
        //: press, not part of the gate.
        this.preAutoGates = new Map();
        //: True while the GMM fit for the current marker is in flight.
        this.autoBusy = false;
        //: `[{code, message}]` from core -- every way this project's table,
        //: mask and image fail to describe the same sample. Null until the
        //: one request lands; an empty array is the answer "they agree".
        this.consistency = null;
        //: Memo for `markersShareTheImageVocabulary()`.
        this.markersAreChannels = null;
        //: Whether Z/X are listening. On while the panel is shown, off once
        //: it is put away (onHide) or unloaded (the cleanup below).
        this._keysArmed = false;
        this._onKeyDown = (event) => this.onMarkerKey(event);
        this.ctx.onCleanup?.(() => this.disarmKeys());
    }

    // Called once from ViewerSidebar#init(), before the saved-state restore below.
    setup() {
        this.populateGateSelect();
        this.bindSaveToAnndata();

        // NO CLOSE OF ITS OWN. This panel used to open with a heading carrying
        // the tool's name and an X, directly under core's card header carrying
        // the same name and an X of its own -- and the two X's did different
        // things. Putting the tool away is the card's chevron, the Tools row
        // and the chord, all of which still land on hideToolPanel() and leave
        // this controller and its DOM alive, so reopening in the same session
        // is instant. The card's X is the one that unloads outright. See
        // views/toolLoader.js.

        // Auto Threshold, and afterwards the way back from it. A 20px muted
        // glyph at the end of the slider's own line rather than the full-width
        // button it was -- the same control the image channel's contrast window
        // ends with, down to the class (`.slider-auto-button` in viewer.css).
        const gateAuto = document.getElementById("gate_auto_button");
        gateAuto.addEventListener("click", () => this.onAutoClick());
        this.syncAutoButton();

        // One request, not awaited: the panel is usable while it is in flight
        // and the notes appear under the plot when it lands. Nothing below
        // this line depends on the answer.
        this.loadConsistency();

        // No resize listener. d3-simple-slider had to be handed a width in
        // pixels and rebuilt whenever the sidebar changed size; a
        // PlexoraSlider is a flex row that lays itself out.
    }

    // Called by toolLoader.js right after unhiding the panel (both on first lazy
    // load and on every reopen). The distribution plot measures its own width
    // via getBoundingClientRect(), which returns 0 while the panel is
    // display:none -- redraw it now that it is actually visible so it does not
    // render collapsed to zero width. The slider no longer measures anything.
    onShow() {
        this.drawGateDistribution();
        this.paintConsistency();
        this.armKeys();
    }

    // Called by toolLoader.js when the panel is put away, and when a routed
    // page (Settings, Figures) covers the viewer. The keys belong to the panel
    // on screen, not to one somewhere behind.
    onHide() {
        this.disarmKeys();
    }

    armKeys() {
        if (this._keysArmed) return;
        this._keysArmed = true;
        document.addEventListener("keydown", this._onKeyDown);
    }

    disarmKeys() {
        if (!this._keysArmed) return;
        this._keysArmed = false;
        document.removeEventListener("keydown", this._onKeyDown);
    }

    /** Whether a bare Z/X is meant for this panel. */
    acceptsKeys(event) {
        if (!this._keysArmed) return false;
        if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return false;
        // The marker search box is an INPUT: typing "x" into it searches.
        const typing = window.PlexoraShortcuts?.isTyping
            ? window.PlexoraShortcuts.isTyping()
            : (() => {
                const active = document.activeElement;
                const tag = active && active.tagName;
                return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT"
                    || Boolean(active && active.isContentEditable);
            })();
        if (typing) return false;
        // <dialog> traps focus but not keystrokes.
        if (window.PlexoraConfirm?.modalOpen?.()) return false;
        if (document.querySelector("dialog[open]")) return false;
        // The SELECTED tool's, the way ROI decides: open-but-unselected is not
        // enough, or both halves of a coexisting pair would answer.
        const loader = window.PlexoraToolLoader;
        if (loader && typeof loader.activeTool === "function" && loader.activeTool() !== "gating") {
            return false;
        }
        return true;
    }

    /**
     * Move the selected marker `delta` places along the dropdown's own list,
     * exactly as picking it there would. Clamps at both ends. True when it
     * moved.
     */
    stepMarker(delta) {
        const names = this.getGateMarkerNames();
        // -1 with nothing selected, so X picks the first marker.
        const at = names.indexOf(this.gateMarker);
        const next = names[at + delta];
        if (at + delta < 0 || next === undefined) return false;
        // A pick from the dropdown still waiting on its timer would land after
        // this and undo it.
        window.clearTimeout(this.gateMarkerChangeTimer);
        this.setGateMarker(next);
        return true;
    }

    onMarkerKey(event) {
        const raw = event.key || "";
        const delta = MARKER_KEYS[raw.length === 1 ? raw.toLowerCase() : ""];
        if (!delta) return;
        if (!this.acceptsKeys(event)) return;
        // Only when it did something: at the end of the list the key is free.
        if (this.stepMarker(delta)) event.preventDefault();
    }

    // ViewerSidebar#init() restore-flow hooks (see registerModule()'s doc comment).
    fetchSaved() {
        return this.api.getSavedGatingList();
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
        if (SAVEABLE_SOURCE_TYPES.includes(this.ctx.dataset?.table?.sourceKind)) {
            try {
                const gates = await this.api.getGatesFromAnndata();
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

    // -- walking to a sibling sample (services/carryOver.js) ---------------

    /**
     * The marker, and nothing else.
     *
     * WHICH marker somebody is gating is a question about the experiment --
     * "where is SOX10 in this cohort" -- and it is the same question on the
     * next sample. The THRESHOLD is not: 5.8 is a reading off this image's
     * intensity distribution, and carrying it to the next sample would be
     * asserting a measurement nobody made. So the number stays behind, and
     * applyCarryState below picks up whatever THIS sample has recorded for the
     * same marker, or its own full range if it has none.
     */
    captureCarryState() {
        return this.gateMarker ? { marker: this.gateMarker } : null;
    }

    /**
     * Select the same marker on this sample, at this sample's own numbers.
     *
     * Runs after applyOrDefault, so `gating_channels` already holds whatever
     * gates THIS project has saved. `force` because the guard at the top of
     * setGateMarker makes a same-name re-select a no-op, and the default
     * marker chosen a moment ago may well be the carried one -- in which case
     * nothing would be re-derived and the panel would keep the default's
     * slider. `syncSlot: false` because mirroring the marker into channel slot
     * 1 would overwrite a channel the carried arrangement just put there, and
     * would schedule a channel-list save on a sample the user has not edited;
     * applySavedGating passes the same pair for the same reason.
     */
    applyCarryState(state) {
        const marker = state && state.marker;
        if (!marker) return { skipped: [] };
        if (!this.getGateMarkerNames().includes(marker)) {
            return { skipped: [`Thresholding: ${marker} is not a marker in this sample`] };
        }
        this.setGateMarker(marker, { force: true, syncSlot: false, enableSlot: false });
        return { skipped: [] };
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
        const exitButton = document.getElementById("save_anndata_exit");
        const tableNameInput = document.getElementById("save_anndata_table_name");
        const status = document.getElementById("save_anndata_status");

        if (SAVEABLE_SOURCE_TYPES.includes(this.ctx.dataset?.table?.sourceKind)) {
            button.hidden = false;
        }

        // One way in, several ways out. The panel opens over the bottom of a
        // scrolling sidebar, so its dismiss control has to be visible from the
        // moment it opens rather than below the fold with the action row -- and
        // once a save has succeeded "Cancel" reads like it would undo it.
        const closePanel = () => {
            panel.hidden = true;
            cancelButton.textContent = "Cancel";
        };

        button.addEventListener("click", () => {
            status.textContent = "";
            status.className = "";
            cancelButton.textContent = "Cancel";
            panel.hidden = !panel.hidden;
        });

        cancelButton.addEventListener("click", closePanel);
        exitButton.addEventListener("click", closePanel);

        panel.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.stopPropagation();
                closePanel();
            }
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
                await this.api.saveGatingList(this.gatingList.gating_channels, this.gatingList.selections);
                const result = await this.saveWithImageId(tableNameInput.value.trim() || "gates");
                status.className = "success";
                status.textContent = `Saved column "${result.image_id}" (${result.n_active_gates} markers).`;
                // Nothing left to cancel -- the write already happened.
                cancelButton.textContent = "Close";
            } catch (error) {
                status.className = "error";
                status.textContent = error.message || "Failed to save gates to AnnData";
            } finally {
                confirmButton.disabled = false;
            }
        });
    }

    /**
     * Save, asking the host for an image-id column if the project has not
     * recorded one.
     *
     * This plugin declares `role:image_id` as an optional requirement, so core
     * owns collecting and storing it -- one column name, asked once, and every
     * other plugin then finds it already answered. The panel used to carry its
     * own free-text box defaulting to the literal "imageid", which asked the
     * user for something the host already had a place to keep.
     */
    async saveWithImageId(tableName) {
        try {
            return await this.api.saveGatesToAnndata(tableName);
        } catch (error) {
            if (!/image ID column/i.test(error.message || "")) throw error;
            const collected = await this.ctx.requirements.require(["role:image_id"]);
            if (!collected) throw error;
            return this.api.saveGatesToAnndata(tableName);
        }
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
        // frequently a different set of strings entirely. CSVGatingList holds
        // exactly that list (see its init, which takes the project's recorded
        // marker/metadata split off ctx.dataset), so reuse it here rather than
        // deriving a second answer that could disagree with the sliders.
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
        this.syncGateSlider();
        // The pending revert is the previous marker's, not this one's -- see
        // preAutoGates.
        this.syncAutoButton();
        this.drawGateDistribution();
        this.paintConsistency();
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
        this.eventHandler.trigger(CSVGatingList.events.SELECTION_CHANGED, this.gatingList.selections);
    }

    /**
     * The gate, built once and afterwards only told things.
     *
     * The d3 slider this replaced was torn down and rebuilt on every marker
     * change, every resize and every reopen of the panel, because it was handed
     * a width in pixels and drew an SVG at it. Changing markers is now a change
     * of domain -- `setBounds`, in place -- and the control the user is holding
     * is never replaced under their finger.
     *
     * The step is the grid `normalizeGateRange` rounds onto, so a handle cannot
     * land between two gates the rest of the plugin can express.
     */
    syncGateSlider() {
        if (!this.gateMarker) return;
        const target = document.getElementById("gate_slider");
        if (!target) return;
        const range = this.getGateRange(this.gateMarker);
        const values = this.gatingList.gating_channels[this.dataLayer.getFullChannelName(this.gateMarker)] || range;
        const step = Math.pow(10, -this.dataLayer.gateDecimals(range));
        if (this.gateSlider) {
            this.gateSlider.setBounds({ min: range[0], max: range[1], step });
            this.gateSlider.set([...values], { silent: true });
            this.sizeGateFields(range);
            return;
        }
        this.gateSlider = new PlexoraSlider(target, {
            mode: "range", min: range[0], max: range[1], step,
            low: values[0], high: values[1],
            // Read live rather than captured, because the precision belongs to
            // the MARKER and the marker changes under this control: a gate on
            // raw counts wants whole numbers where one on a log-transformed
            // copy of the same data wants two decimals. `setBounds` has no
            // `decimals`, and it does not need one -- supplying `format` makes
            // the slider and its two boxes ask this on every write.
            format: (value) => this.formatGate(value),
            fieldIds: { low: "gate_min_value", high: "gate_max_value" },
            ariaLabels: ["Gate lower threshold", "Gate upper threshold"],
            // Inline, at the two ends of the track they name. They used to take
            // a row of their own above it (`fieldsSlot`), which cost a line to
            // say what four characters say -- see gating/panel.html.
            //
            // `is-plain-numbers` is what stops them looking like boxes; it is
            // the image channel's contrast window's class, in main.css, and the
            // two controls are deliberately one thing to look at.
            className: "is-plain-numbers",
            // The viewer's own accent, as everything else on this panel is. It
            // was `--accent-gate`, an orange that predates gating becoming a
            // plugin and reads as a second chrome colour beside the cyan
            // handles two panels up -- DESIGN.md retires it by name.
            accent: "var(--accent-channel)",
            // Per tick: a brush move, which repaints the cells already on
            // screen. On release: the selection change, which is what the rest
            // of the app acts on, and the 400ms save.
            onInput: (value) => this.setGateRange(value, CSVGatingList.events.GATING_BRUSH_MOVE),
            onChange: (value) => this.setGateRange(value, CSVGatingList.events.SELECTION_CHANGED),
        });
        this.gateSlider.blurFieldsOnEnter();
        this.sizeGateFields(range);
    }

    /** This marker's precision: enough decimals for ~200 steps across its own
     *  observed range, which is the same grid `normalizeGateRange` rounds onto
     *  and the step the handles move by. */
    gateDecimals() {
        return this.dataLayer.gateDecimals(this.getGateRange(this.gateMarker));
    }

    formatGate(value) {
        return Number.parseFloat(value || 0).toFixed(this.gateDecimals());
    }

    /**
     * How wide the two inline numbers are: the characters this marker's domain
     * can actually produce, in `ch` over a tabular-nums face.
     *
     * Fixed for the marker rather than fitted to the text, for the reason
     * viewerSidebar's `sizeRangeFields` is: a width that tracked the content
     * would resize the box, and so the track between the boxes, on the tick of
     * a drag where 9.99 becomes 10.00 -- the handle would slide out from under
     * the pointer.
     */
    sizeGateFields(range) {
        const widest = Math.max(...range.map((end) => this.formatGate(end).length));
        this.gateSlider?.el?.style?.setProperty(
            "--plx-number-width", `calc(${Math.max(3, widest)}ch + 8px)`);
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
        if (eventName === CSVGatingList.events.SELECTION_CHANGED) {
            this.scheduleSaveGating();
        }
    }

    /**
     * The one action on the threshold line: Auto, and then the way back from it.
     *
     * Auto is destructive. It replaces whatever gate is on screen, and on a
     * marker somebody has already set by eye -- or copied off a paper, or
     * carried over from another sample -- that gate is the only copy of a
     * number they cannot get back by pressing Auto again. So the exact pair is
     * taken down before the fit runs, and the button turns into the way back
     * to it. Same bargain the channel contrast window strikes, and deliberately
     * the same glyphs (see viewerSidebar's onSlotAutoClick).
     *
     * Revert is not a mode: it puts back two numbers and nothing else.
     *
     * The pending revert survives a drag, and survives switching markers away
     * and back, because it is held per marker. Clearing it on the first tick of
     * a drag would swap the icon out from under the pointer and would mean that
     * nudging the auto gate by a handle's width silently threw away the gate
     * the user was nudging it back towards.
     */
    async onAutoClick() {
        if (!this.gateMarker || this.autoBusy) return;
        const fullName = this.dataLayer.getFullChannelName(this.gateMarker);
        if (this.preAutoGates.has(fullName)) {
            this.revertGate(fullName);
            return;
        }
        // What the two numbers read right now, which is what Auto is about to
        // overwrite.
        const before = [...(this.gatingList.gating_channels[fullName]
            || this.getGateRange(this.gateMarker))];
        this.autoBusy = true;
        this.syncAutoButton();
        try {
            await this.autoGate();
        } finally {
            this.autoBusy = false;
            // A fit that never landed -- no GMM for the marker, or the marker
            // changed under it -- leaves the gate where it was, and a Revert
            // icon offering to restore the range already on screen would be a
            // button that does nothing.
            const now = this.gatingList.gating_channels[fullName] || before;
            if (now[0] !== before[0] || now[1] !== before[1]) {
                this.preAutoGates.set(fullName, before);
            }
            this.syncAutoButton();
        }
    }

    /** Put back the gate that was on screen when Auto was pressed. */
    revertGate(fullName) {
        const before = this.preAutoGates.get(fullName);
        if (!before) return;
        this.preAutoGates.delete(fullName);
        this.setGateRange(before, CSVGatingList.events.SELECTION_CHANGED);
        this.updateGateReadout(before);
        this.syncAutoButton();
    }

    /**
     * The icon, its tooltip and whether it can be pressed.
     *
     * Guarded on the state it last drew, because this runs on every marker
     * change as well as on every press, and the icon swap is an `innerHTML`
     * write.
     */
    syncAutoButton() {
        const node = document.getElementById("gate_auto_button");
        if (!node) return;
        const fullName = this.gateMarker
            ? this.dataLayer.getFullChannelName(this.gateMarker) : null;
        const state = this.autoBusy ? "busy"
            : (fullName && this.preAutoGates.has(fullName)) ? "revert" : "auto";
        if (node.dataset.state === state) return;
        node.dataset.state = state;
        node.classList.toggle("is-revert", state === "revert");
        node.classList.toggle("is-busy", state === "busy");
        // Not while the fit is in flight: a second click would read the
        // still-unrecorded `preAutoGates` entry and start a second one.
        node.disabled = state === "busy";
        const label = state === "revert" ? "Restore previous threshold" : "Auto threshold";
        node.title = label;
        node.setAttribute("aria-label", label);
        node.innerHTML = state === "revert"
            ? '<span class="fas fa-rotate-left"></span>'
            : '<span class="fas fa-wand-magic-sparkles"></span>';
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
        this.setGateRange(values, CSVGatingList.events.SELECTION_CHANGED);
        this.syncGateSlider();
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

    /**
     * What is wrong with the frame this marker's numbers sit in, if anything.
     *
     * The three inputs -- the feature table, the segmentation mask and the
     * image -- come out of three steps of a pipeline, are named by hand, and
     * are attached one at a time. Nothing in any of the files says they belong
     * together, so a table paired with the wrong image, or a mask exported at
     * a different resolution than the image it was segmented from, produces a
     * panel that WORKS: the gate moves, cells light up, and the numbers on
     * screen belong to something else.
     *
     * Asked of core, once, because it is a question about the project and not
     * about gating -- every tool that draws per-cell results over the image
     * has it, and three tools answering it three ways is three answers to
     * disagree about. See models/consistency.py and dataLayer's
     * getConsistencyReport.
     */
    async loadConsistency() {
        this.consistency = await this.dataLayer.getConsistencyReport();
        this.paintConsistency();
    }

    /**
     * The marker-specific half, which core cannot answer: this marker is not
     * an image channel, so moving the gate will change which cells are drawn
     * and nothing at all about the picture underneath them.
     *
     * Only worth saying where the two vocabularies overlap AT ALL. A feature
     * table's columns and an image's channel names are frequently different
     * sets of strings by design -- a panel measured on one instrument and
     * imaged on another -- and on such a project this would fire on every
     * marker, which is not a warning but a description of the project.
     */
    markerFinding() {
        if (!this.gateMarker) return null;
        if (!this.markersShareTheImageVocabulary()) return null;
        if (this.ctx.dataset.image.has(this.dataLayer.getFullChannelName(this.gateMarker))) {
            return null;
        }
        return {
            code: "marker_has_no_channel",
            message: `No image channel is named ${this.gateMarker}, so moving `
                + "this threshold changes which cells are drawn but nothing "
                + "about the image under them.",
        };
    }

    /** Whether any gate-able marker is also a real image channel. Memoized:
     *  both lists are fixed for the life of the panel. */
    markersShareTheImageVocabulary() {
        if (this.markersAreChannels === null) {
            const names = this.getGateMarkerNames();
            if (!names.length) return false;
            this.markersAreChannels = names.some(
                (name) => this.ctx.dataset.image.has(this.dataLayer.getFullChannelName(name)));
        }
        return this.markersAreChannels;
    }

    /**
     * Draw the notes, or take the block away when there are none.
     *
     * Rebuilt rather than diffed: there are at most a handful of these, they
     * change only when the marker changes, and the alternative is a second
     * copy of the same list to keep in step with the first.
     */
    paintConsistency() {
        const target = document.getElementById("gate_consistency");
        if (!target) return;
        const findings = [...(this.consistency || [])];
        const marker = this.markerFinding();
        if (marker) findings.push(marker);
        target.innerHTML = "";
        target.hidden = !findings.length;
        for (const finding of findings) {
            const note = document.createElement("p");
            note.className = "gate-consistency-note";
            note.dataset.code = finding.code || "";
            const icon = document.createElement("span");
            icon.className = "fas fa-triangle-exclamation";
            // Decorative: the sentence beside it already says everything, and
            // a screen reader announcing "warning" before each of three notes
            // is three interruptions for no added fact.
            icon.setAttribute("aria-hidden", "true");
            note.append(icon, document.createTextNode(finding.message || ""));
            target.append(note);
        }
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
        return this.api.saveGatingList(this.gatingList.gating_channels, this.gatingList.selections);
    }

    scheduleSaveGating() {
        if (this.sidebar.isRestoring()) return;
        window.clearTimeout(this._saveGatingTimer);
        this._saveGatingTimer = window.setTimeout(() => {
            this._gatingSaveChain = (this._gatingSaveChain || Promise.resolve()).then(() => this.persistGatingList());
        }, 400);
    }

    /** Put the slider back in step with the gate, silently: this is the plugin
     *  catching the control up, not the user moving it. */
    updateGateReadout(values) {
        this.gateSlider?.set([...values], { silent: true });
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
        // The epsilon is not superstition. `0.29 * 100` is 28.999999999999996,
        // so flooring an on-grid value drops it a whole step -- which, now that
        // the slider's step IS this grid, would walk a gate downwards a little
        // every time it was touched.
        return [Math.floor(sorted[0] * factor + 1e-9) / factor,
                Math.ceil(sorted[1] * factor - 1e-9) / factor];
    }
}
