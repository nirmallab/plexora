/**
 * How many per-channel requests may be in flight at once.
 *
 * The SERVER decides this, not the browser: it is the only side that knows
 * whether it is a workstation or a 2-core SLURM allocation, and
 * navigator.hardwareConcurrency answers a different question entirely (the
 * viewer's machine, which does none of this work). base.html publishes it from
 * plexora._resources; the fallback covers a page served by an older build.
 *
 * Read per burst rather than cached, so a second instance on a page rendered by
 * a different server still sees the right number.
 */
function plexoraChannelConcurrency() {
    const advertised = Number(window.PLEXORA_SERVER_CONCURRENCY);
    return Number.isFinite(advertised) && advertised > 0 ? Math.floor(advertised) : 3;
}

/**
 * Promise.all with a ceiling on how many run at once.
 *
 * Replaces the bare `Promise.all(names.map(...))` applySavedChannels used to do.
 * That fanned out one request per saved channel simultaneously, and each of
 * those makes the server read an entire full-resolution channel plane through a
 * globally serialized reader -- so on a small allocation the burst occupied
 * every worker at once, leaving none to answer even the liveness probe, and the
 * reverse proxy in front of it returned 502. Total work is unchanged; only the
 * arrival rate is bounded.
 *
 * Workers pull from a shared cursor rather than taking a fixed slice each, so
 * one slow channel cannot leave the others idle behind it.
 */
async function plexoraMapWithLimit(items, limit, fn) {
    const list = Array.from(items || []);
    const ceiling = Math.max(1, Math.min(limit, list.length));
    let cursor = 0;
    const workers = [];
    for (let i = 0; i < ceiling; i++) {
        workers.push((async () => {
            while (cursor < list.length) {
                const index = cursor++;
                await fn(list[index], index);
            }
        })());
    }
    await Promise.all(workers);
}


/**
 * @class ViewerSidebar - unified controls for marker gating and image channels.
 *
 * ## Where it looks for its markup
 *
 * By default: the whole document, by id, exactly as it always did. The
 * `options` argument scopes it instead -- `{root, idPrefix, persist}` -- so a
 * SECOND instance can drive a second copy of the same markup elsewhere on the
 * page without the two finding each other's elements.
 *
 * That exists for Figure Builder's Quick Edit, which mounts the channel
 * controls beside a small preview of a figure panel. The alternative was a
 * second channel widget written from scratch, which is two implementations of
 * colour, contrast and channel ordering that agree until the day one of them
 * is fixed -- and a user who has to learn the second one.
 *
 * `persist: false` is the other half of it: a scoped instance must never write
 * the project's saved channel list, because the channels it is showing belong
 * to a figure panel rather than to the project. Note that `persistChannelList`
 * also reads main.js globals that only exist on the viewer page, so this is a
 * correctness guard and not merely a policy one.
 *
 * Every default is the previous behaviour, and the viewer passes no options at
 * all.
 */

//: How long an auto-levelled range takes to arrive on screen (easeSlotRange):
//: the channel fade's own length, so a channel switched on comes in and
//: develops its contrast in one movement.
const RANGE_EASE_MS = 240;
//: How long a gating calibration's window takes to arrive on a channel the
//: user has not set (syncGatingContrast): long enough to be seen happening.
const GATING_EASE_MS = 600;
//: How long the first open waits, at most, for the gating calibration it
//: asked the server for (startGatingContrast), and its first poll.
const GATING_POLL_LIMIT_MS = 60000;
const GATING_POLL_FIRST_MS = 1500;
//: Where a slot's window came from (`slot.rangeSource`, saved as a row's
//: `source`): the user's hand or an explicit Auto click, the viewer's own
//: auto-levelling, a gating calibration, a default the project was seeded
//: with -- or "legacy", a row saved before rows said, which counts as the
//: user's. Only auto, default and gating are ever replaced by a gating window.
const REPLACEABLE_SOURCES = new Set(["auto", "default", "gating"]);

class ViewerSidebar {
    constructor(config, columns, dataLayer, eventHandler, channelList, options) {
        this.config = config;
        this.columns = [...columns];
        this.dataLayer = dataLayer;
        this.eventHandler = eventHandler;
        this.channelList = channelList;
        const settings = options || {};
        //: Where this instance's markup lives. `document` for the viewer's own
        //: sidebar; an element for anything mounting a second copy.
        this.root = settings.root || document;
        //: Prepended to every id this instance looks up or generates, so two
        //: copies of the same markup can coexist.
        this.idPrefix = settings.idPrefix || "";
        //: Whether changes here are written to the project's channel list.
        this.persist = settings.persist !== false;
        //: Full channel name -> index in the image. main.js's `imageChannels`
        //: by default; injectable because that binding only exists on the
        //: viewer page, and a scoped instance runs where it does not.
        this.channelIndexMap = settings.channelIndex || null;
        //: HD pinned on or off for this instance, or undefined to keep asking
        //: the viewer. `isHdMode` reads the OSD viewer manager, which only
        //: exists on the viewer page -- so a scoped instance elsewhere is
        //: permanently in the coarse byte domain unless it says otherwise.
        //: Quick Edit pins it true: it fetches full-precision uint16 straight
        //: from the source and has no quantized WebP tiles to match, so the
        //: byte domain there is a 256-step slider over 16-bit data and nothing
        //: else. Undefined is the viewer's own behaviour, unchanged.
        this.hdModeOverride = settings.hdMode;
        this.databaseDescription = {};
        this.channelSlots = [];
        this.channelSlotSliders = new Map();
        // Remembers a manually-set intensity range per marker name (not per slot), so
        // switching a slot's marker away and back doesn't discard what the user tuned.
        this.markerRangeOverrides = new Map();
        //: The display curve last given per marker (null linear), so a slot
        //: switched away and back keeps it, as it keeps its range.
        this.markerCurves = new Map();
        //: How brightness follows intensity, for every channel at once:
        //: "linear", or "asinh" (the Image card's "Log brightness"). Saved
        //: with the channel list (each row's curve) and read back from it.
        this.curveMode = "linear";
        //: The project's gating calibration as the server offers it
        //: (/display_contrast): {state, rev, channels: {fullName: {window,
        //: curve, chosen_by}}}. Null until fetched; only the viewer's own
        //: sidebar fetches it.
        this.gatingContrast = null;
        this.colorPickers = new Map();
        this.markerSelects = new Map();
        this._saveChannelsTimer = null;
        this._restoring = false;
        // Add-on modules (e.g. gating) that extend the sidebar without core needing to know
        // their concrete type -- see registerModule() and pluginRegistry.js.
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
        // A pinned instance never listens: its domain cannot change, so
        // there is nothing for the event to tell it.
        //: Held so `destroy()` can take it off again. A layer's channel panel
        //: is unmounted whenever its card is removed, and an unbound listener
        //: on a dead instance goes on remapping slots whose DOM is gone.
        this._hdModeListener = null;
        if (this.hdModeOverride === undefined) {
            this._hdModeListener = (e) => this.onHdModeChanged(Boolean(e.detail?.enabled));
            window.addEventListener("plexora:hd-mode-changed", this._hdModeListener);
        }
        //: Which quantization window the reference tiles are in, for a volume:
        //: "volume" (every plane and most projections share one) or "std" (a
        //: standard-deviation projection, quantized against half the range --
        //: server/volume/windows.py). Only the viewer's own sidebar follows
        //: it; a scoped copy shows somebody else's channels.
        this.viewClass = "volume";
        this._viewClassListener = null;
        //: A z-stack's contrast is ONE level per channel, the channel's own
        //: (autoChannel), shared by every plane, projection and the 3-D view.
        //: Nothing re-levels when the view changes: levelling each view from
        //: its own statistics cost a request and a server-side overview per
        //: projection, held its tiles up to 1.5 s, and read the level again
        //: from disk for every cut -- to set a range the user then moved.
        this._volumeWindowListener = null;
        if (this.hdModeOverride === undefined && this.root === document && !this.idPrefix) {
            this._viewClassListener = (e) => this.onViewClassChanged(
                e.detail?.from || "volume", e.detail?.to || "volume");
            window.addEventListener("plexora:view-class-changed", this._viewClassListener);
            this._volumeWindowListener = (e) => this.onVolumeWindow(e.detail || {});
            window.addEventListener("plexora:volume-window", this._volumeWindowListener);
        }
    }

    /**
     * Unmount this instance: take back everything that outlives its markup.
     *
     * The viewer's own sidebar never calls this -- it lives as long as the
     * page. A scoped instance does not: a layer's channel panel goes away with
     * its card, and what it leaves behind is a `window` listener holding the
     * whole instance alive and three control classes with their own listeners
     * and popovers.
     *
     * Safe to call twice, because a card can be removed by the X and again by
     * the layer list noticing it is gone.
     */
    destroy() {
        if (this._hdModeListener) {
            window.removeEventListener("plexora:hd-mode-changed", this._hdModeListener);
            this._hdModeListener = null;
        }
        if (this._volumeWindowListener) {
            window.removeEventListener("plexora:volume-window", this._volumeWindowListener);
            this._volumeWindowListener = null;
        }
        if (this._viewClassListener) {
            window.removeEventListener("plexora:view-class-changed", this._viewClassListener);
            this._viewClassListener = null;
        }
        window.clearTimeout(this._saveChannelsTimer);
        this._saveChannelsTimer = null;
        this.channelSlotSliders.forEach((slider) => slider.destroy?.());
        this.channelSlotSliders.clear();
        this.colorPickers.forEach((picker) => picker.destroy?.());
        this.colorPickers.clear();
        this.markerSelects.forEach((select) => select.destroy?.());
        this.markerSelects.clear();
        this.sidebarModules = [];
    }

    /**
     * One of this instance's elements, by its unprefixed id.
     *
     * With the defaults this is exactly `document.getElementById(name)`. A
     * scoped instance looks inside its own root and for its own prefixed id,
     * so the two never find each other's controls -- and an element the scoped
     * copy does not mount comes back null, which every caller here already
     * handles because the viewer renders no channel section for an RGB image.
     */
    el(name) {
        const id = this.idPrefix + name;
        return this.root.getElementById
            ? this.root.getElementById(id)
            : this.root.querySelector(`[id="${id}"]`);
    }

    q(selector) {
        return this.root.querySelector(selector);
    }

    /** The id this instance gives one of its per-slot elements. */
    slotId(name, index) {
        return `${this.idPrefix}${name}_${index}`;
    }

    /** THIS instance's row for a slot, never another instance's. */
    slotRow(index) {
        return this.el(`channel_slot_${index}`);
    }

    isHdMode() {
        if (this.hdModeOverride !== undefined) return Boolean(this.hdModeOverride);
        return Boolean(window.__plexora?.seaDragonViewer?.viewerManagerVMain?.isHdMode?.());
    }

    /**
     * @function quantWindow
     * The server's quantization window {qmin, qmax} for a channel -- the only
     * thing needed to convert between the raw 16-bit units ranges are stored
     * in and the [0, 255] byte domain the slider and shader work in.
     *
     * Sourced from the channel's image stats (get_image_channel_stats), NOT
     * from the GMM packet. Both carry identical qmin/qmax -- they come from
     * the same server-side get_channel_quantization_window() -- but the GMM
     * packet only arrives after a ~1 s GaussianMixture fit, while stats takes
     * a few ms and is fetched on activation anyway. Reading it from the GMM
     * was why restoring saved channels ran a fit per channel purely to
     * convert a range they had already saved. Falls back to the GMM packet so
     * a caller that somehow has one but no stats still works.
     */
    quantWindow(name, viewClass = this.viewClass) {
        const fullName = this.dataLayer.getFullChannelName(name);
        const stats = this.databaseDescription[fullName];
        const base = (stats && stats.qmax !== undefined) ? stats : this.channelList.hasChannelGMM[name];
        // A standard-deviation projection's tiles are quantized against
        // (0, ceil(qmax / 2)) -- the exact bound on a std of values in
        // [0, qmax], so the server needs no scan and this needs no request.
        if (base && viewClass === "std") {
            return { ...base, qmin: 0, qmax: Math.max(1, Math.ceil(base.qmax / 2)) };
        }
        return base;
    }

    /**
     * @function rawToByteRange
     * Maps a [min, max] pair in raw 16-bit units into the [0, 255] byte
     * domain the server actually quantized against (packet.qmin/qmax --
     * see quantWindow), clamped to a valid byte range.
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
     * Falls back to slot.range unconverted if no quantization window is
     * cached yet (shouldn't happen for an active/auto-leveled slot).
     */
    toRawRangeForSlot(slot) {
        // Mid-ease (easeSlotRange) the range on screen is on its way: a save
        // landing now records where it is going, never a frame of the way.
        const range = slot.rangeEase && slot.rangeEaseTarget ? slot.rangeEaseTarget : slot.range;
        if (this.isHdMode()) return range;
        const packet = this.quantWindow(slot.name);
        if (!packet) return range;
        return this.byteToRawRange(range, packet);
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
            const packet = this.quantWindow(slot.name);
            if (!packet) return;
            slot.range = enabled
                ? this.byteToRawRange(slot.range, packet)
                : this.rawToByteRange(slot.range, packet);
            // A pending revert is a pair of numbers in the domain that was
            // active when Auto was pressed. Left alone it would restore a
            // 16-bit window into a 0..255 slider, or the reverse.
            if (slot.preAutoRange) {
                slot.preAutoRange.range = enabled
                    ? this.byteToRawRange(slot.preAutoRange.range, packet)
                    : this.rawToByteRange(slot.preAutoRange.range, packet);
            }
            this.setSlotRange(slot.index, slot.range, slot.userRangeChanged);
            // A new domain, not a new value: `setBounds` re-clamps and repaints
            // in place, where the old slider had to be thrown away and drawn
            // again from scratch.
            this.syncChannelSlider(slot);
        });
    }

    /**
     * @function onViewClassChanged
     * The tiles moved to another quantization window (a volume switched
     * between planes and a standard-deviation projection): remap every slot's
     * byte-domain range so each keeps the same RAW window, the same move
     * `onHdModeChanged` makes between bytes and raw units. In HD mode ranges
     * are raw already and nothing moves.
     */
    onViewClassChanged(from, to) {
        if (from === to) return;
        this.viewClass = to;
        if (this.isHdMode()) return;
        this.channelSlots.forEach((slot) => {
            if (!slot.name) return;
            const before = this.quantWindow(slot.name, from);
            const after = this.quantWindow(slot.name, to);
            if (!before || !after) return;
            const remap = (range) => this.rawToByteRange(this.byteToRawRange(range, before), after);
            slot.range = remap(slot.range);
            if (slot.preAutoRange) slot.preAutoRange.range = remap(slot.preAutoRange.range);
            this.setSlotRange(slot.index, slot.range, slot.userRangeChanged);
            this.syncChannelSlider(slot);
        });
    }

    async init(databaseDescription) {
        this.databaseDescription = databaseDescription;
        this.setupSidebarShell();
        this.sidebarModules.forEach((m) => m.setup && m.setup());
        this.bindActions();

        const maxLabel = this.el("max-channels");
        if (maxLabel) maxLabel.textContent = this.maxChannelSlots;

        const [savedChannels, ...moduleSaved] = await Promise.all([
            this.dataLayer.getSavedChannelList(),
            ...this.sidebarModules.map((m) => (m.fetchSaved ? m.fetchSaved() : Promise.resolve(null))),
        ]);

        // Channels this page view was OPENED with (plexora.view(channels=...)),
        // which outrank the project's saved list for this page and are never
        // written back to it -- see applyLaunchChannels.
        const launchChannels = this.launchChannels();

        // Channels the sample the user just walked away from had on. Ranked
        // BELOW a launch state (that is an explicit request from a notebook
        // cell, made about this page) and ABOVE this project's own saved list
        // (walking a dataset is a continuous act, and arriving to a different
        // set of channels than the one being compared is the whole complaint).
        // Empty unless a Prev/Next click put one there -- see
        // services/carryOver.js.
        const carriedChannels = this.carriedChannels(savedChannels);

        // Suppressed while restoring: applySavedChannels/a module's own apply-from-saved
        // reuse the same setters live edits use, which otherwise schedule an autosave on
        // every call - turning "load from DB" into "load from DB, then immediately write
        // back to DB".
        this._restoring = true;
        if (launchChannels.length) {
            await this.applyLaunchChannels(launchChannels);
        } else if (carriedChannels.length) {
            // The same path a launch state takes, and for the same reason: it
            // matches channels by NAME, honours a range when one is supplied
            // and auto-levels when one is not, and is never written back.
            await this.applyLaunchChannels(carriedChannels);
        } else if (savedChannels && savedChannels.length) {
            await this.applySavedChannels(savedChannels);
        } else {
            this.initChannelSlots();
            this.applyInitialChannels();
        }

        // Kept, so a restore that has to run AFTER these -- a carried marker
        // re-imposed on top of this sample's own saved gates -- has something
        // to wait on. `forEach` never awaited them, which is fine for the
        // sidebar itself and is not fine for anything that has to follow them.
        this._modulesApplied = Promise.allSettled(
            this.sidebarModules.map((m, i) => {
                try {
                    return m.applyOrDefault ? m.applyOrDefault(moduleSaved[i]) : null;
                } catch (error) {
                    console.error("viewerSidebar: applyOrDefault failed", error);
                    return null;
                }
            }));
        this._restoring = false;

        // Not persisted after a launch or a carried restore either. The default
        // branch above writes its guess so the project has a starting point; a
        // launch state is one cell's request, and a carried set is the sample
        // NEXT DOOR's arrangement -- saving either would make it this project's
        // channels for everybody afterwards, so walking a dataset once would
        // rewrite every sample in it.
        if (!launchChannels.length && !carriedChannels.length && !(savedChannels && savedChannels.length)) this.persistChannelList();
        this.sidebarModules.forEach((m, i) => m.persistIfNeeded && m.persistIfNeeded(Boolean(moduleSaved[i] && moduleSaved[i].length)));

        // The gating calibration, offered to every channel and applied to the
        // ones nobody set -- after the first picture is up, never before it.
        // Not for a launch or a carried set: those are somebody's request.
        if (!launchChannels.length && !carriedChannels.length) this.startGatingContrast();
    }

    /**
     * Registers a plugin's sidebar controller (see pluginRegistry.js). The controller
     * may implement setup(), fetchSaved(), applyOrDefault(savedRows), and
     * persistIfNeeded(hadSaved) -- all optional, called at the matching point in init()
     * above, alongside the core channel-slot restore flow.
     */
    registerModule(moduleController) {
        this.sidebarModules.push(moduleController);
    }

    /**
     * Registers a module discovered after init() already ran (see toolLoader.js /
     * main.js's activateAddonModule) -- e.g. a tool that was lazily loaded when the
     * user opened it mid-session, rather than known at page-boot time. Runs the same
     * setup()/fetchSaved()/applyOrDefault()/persistIfNeeded() lifecycle init() applies
     * to every module registered up front (lines above), just for this one module,
     * so a late module restores its saved state identically either way.
     */
    async registerModuleLate(moduleController) {
        this.sidebarModules.push(moduleController);
        moduleController.setup && moduleController.setup();
        const saved = moduleController.fetchSaved ? await moduleController.fetchSaved() : null;
        this._restoring = true;
        if (moduleController.applyOrDefault) await moduleController.applyOrDefault(saved);
        this._restoring = false;
        if (moduleController.persistIfNeeded) moduleController.persistIfNeeded(Boolean(saved && saved.length));
    }

    /**
     * Forget a module whose plugin has been removed (toolLoader's Remove
     * button, via main.js's deactivatePlugin).
     *
     * Registration is for the life of the page everywhere else, so this had no
     * counterpart until a tool could be taken away again. Without it a dead
     * controller stays in the list and goes on being handed setup(),
     * fetchSaved() and applyOrDefault() -- against a panel whose markup has been
     * removed from the page, so every element handle it takes is null.
     */
    unregisterModule(moduleController) {
        const index = this.sidebarModules.indexOf(moduleController);
        if (index < 0) return false;
        this.sidebarModules.splice(index, 1);
        return true;
    }

    isRestoring() {
        return this._restoring;
    }

    setupSidebarShell() {
        const collapseButton = this.el("sidebar_collapse_button");
        const expandButton = this.el("sidebar_expand_button");
        const shell = this.el("bodyDiv");
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

    /**
     * Fold the channels away, from code rather than from their chevron.
     *
     * The channels are the BASE IMAGE LAYER's card body now, not a section of
     * their own -- see views/layerManager.js -- so the chevron that folds them
     * is that card's and this has nothing to bind. What is left is the one
     * caller that is not the user: toolLoader's collapseForNewTool, making
     * room in the sidebar for a tool that has just opened.
     *
     * The card's own fold, not a second mechanism: same set, same class, so a
     * programmatic fold and a clicked one are indistinguishable afterwards and
     * the next click still toggles from wherever it was left.
     *
     * A no-op for an RGB image, which boots a viewer with no layer stack.
     */
    setChannelSectionCollapsed(collapsed) {
        window.PlexoraLayerManager?.setCollapsed?.(
            PlexoraLayerStack.REFERENCE_LAYER_ID, Boolean(collapsed));
    }

    bindActions() {
        const addButton = this.el("add_channel_button");
        // Guarded: a scoped instance may mount a subset of the markup, and the
        // viewer itself renders no channel section at all for an RGB image.
        if (addButton) addButton.addEventListener("click", () => this.addFirstAvailableChannel());
        // No resize listener. d3-simple-slider had to be handed a width in
        // pixels and so had to be rebuilt whenever the sidebar changed size;
        // a PlexoraSlider is a flex row that lays itself out.
    }

    initChannelSlots() {
        const slotList = this.el("channel_slot_list");
        if (!slotList) return;
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
                range: this.getImageRange(name),
                userColorChanged: false,
                userRangeChanged: false,
                autoLeveled: false,
                autoLeveling: false,
                //: The window Auto replaced, while its Revert icon is showing.
                preAutoRange: null,
            };
            slotList.appendChild(this.createChannelSlot(slot));
            return slot;
        });
        this.updateSelectedCount();
    }

    createChannelSlot(slot) {
        const row = document.createElement("div");
        row.classList.add("channel-slot");
        row.classList.toggle("is-hidden", !slot.visible);
        row.classList.toggle("is-disabled", !slot.enabled);
        row.setAttribute("data-slot", slot.index);
        // Prefixed id as well as the data attribute, because the data
        // attribute alone is not unique on a page with two instances: layer
        // cards mount their own channel list ABOVE the base image's card in
        // the same document, so a slot lookup written as a CSS selector from
        // the viewer's own sidebar (whose root IS the document) finds the
        // layer's row and rewrites it. Every per-slot lookup below goes
        // through `el()`, which is root- and prefix-aware.
        row.setAttribute("id", this.slotId("channel_slot", slot.index));
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

        // The three icons that end the line -- fold, Auto, remove -- as one
        // cluster, 2px apart, so the 7px the main controls are spaced by is
        // not paid twice more at the narrow end of a 300px sidebar.
        const actions = document.createElement("div");
        actions.classList.add("channel-slot-actions");
        top.appendChild(actions);

        const expandToggle = document.createElement("button");
        expandToggle.type = "button";
        expandToggle.classList.add("channel-slot-expand-toggle");
        expandToggle.classList.toggle("is-expanded", Boolean(slot.expanded));
        expandToggle.title = "Show threshold range";
        expandToggle.innerHTML = '<span class="fas fa-chevron-down"></span>';
        expandToggle.addEventListener("click", () => this.toggleSlotExpanded(slot.index));
        actions.appendChild(expandToggle);

        // Auto, on the line that is always showing rather than inside the
        // fold: levelling a channel should not need the slider opened first.
        // Still `.slider-auto-button` for its revert and busy states (and for
        // syncSlotAutoButton's lookup); `.channel-slot-auto` sizes it to the
        // chevron beside it.
        const auto = document.createElement("button");
        auto.type = "button";
        auto.classList.add("slider-auto-button", "channel-slot-auto");
        auto.addEventListener("click", () => this.onSlotAutoClick(slot.index));
        actions.appendChild(auto);
        this.syncSlotAutoButton(slot, auto);

        // The gating calibration's window for this channel, when there is
        // one: offered, never imposed on a window the user set. Hidden until
        // the project has one for this channel (syncSlotGatingButton).
        const gating = document.createElement("button");
        gating.type = "button";
        gating.classList.add("slider-auto-button", "channel-slot-auto", "channel-slot-gating");
        gating.hidden = true;
        gating.addEventListener("click", () => this.onSlotGatingClick(slot.index));
        actions.appendChild(gating);
        this.syncSlotGatingButton(slot, gating);

        const remove = document.createElement("button");
        remove.type = "button";
        remove.classList.add("slot-remove-button");
        remove.title = "Remove channel slot";
        remove.innerHTML = '<span class="fas fa-xmark"></span>';
        remove.addEventListener("click", () => this.removeChannelSlot(slot.index));
        actions.appendChild(remove);

        const detail = document.createElement("div");
        detail.classList.add("channel-slot-detail");
        detail.classList.toggle("is-expanded", Boolean(slot.expanded));

        // ONE LINE: `1 ---o=====o--- 255`. The window used to take two, a
        // header carrying the pair of number boxes above the track, because
        // two bordered boxes and their gaps are a third of a 300px sidebar and
        // the track needed the rest. Drawn as plain text until they are
        // clicked they cost three or five characters each -- see
        // sizeRangeFields -- which the line can spare, so the numbers went
        // back to the ends of the slider they belong to and the row they were
        // parked on is gone.
        const rangeRow = document.createElement("div");
        rangeRow.classList.add("slider-auto-row");

        const slider = document.createElement("div");
        slider.classList.add("sidebar-slider");
        slider.setAttribute("id", this.slotId("channel_slot_slider", slot.index));
        rangeRow.appendChild(slider);

        detail.appendChild(rangeRow);

        row.appendChild(detail);

        return row;
    }

    applyInitialChannels() {
        this.channelSlots.forEach((slot) => {
            if (slot.name && slot.enabled) {
                this.activateChannel(slot);
                // Every other slot-enable path (setSlotEnabled, setSlotMarker) auto-levels
                // right after activating -- this default/no-saved-channels path was missing
                // it, so the first channel on a brand-new datasource sat at the full [0, 255]
                // range until the user happened to touch it.
                this.autoLevelChannelIfNeeded(slot);
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
            const override = this.overrideInDomain(name);
            if (override) {
                slot.range = [...override];
                slot.userRangeChanged = true;
                slot.autoLeveled = true;
                slot.rangeSource = "user";
            } else {
                slot.range = this.getImageRange(name);
                slot.userRangeChanged = false;
                slot.autoLeveled = false;
                slot.rangeSource = null;
            }
            // The image's mode decides the curve; a marker's own cofactor
            // is remembered (autoChannel fits one for a levelled channel).
            slot.curve = this.curveMode === "asinh"
                ? (this.markerCurves?.get(name) || (override ? this.logCurveFor?.(slot) : null))
                : null;
            slot.preGatingRange = null;
            slot.autoLeveling = false;
            // A range belonging to the channel that just left the slot. There
            // is nothing here to restore it to any more.
            slot.preAutoRange = null;
            slot.expanded = true;
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
            this.requestGatingContrast?.([slot.name]);
        }
        this.syncSlotGatingButton?.(slot);
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
                this.requestGatingContrast?.([slot.name]);
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

    /** Where a channel sits in the image, by full name. */
    channelIndexOf(fullName) {
        if (this.channelIndexMap) return this.channelIndexMap[fullName];
        return imageChannels[fullName];
    }

    activateChannel(slot) {
        const fullName = this.dataLayer.getFullChannelName(slot.name);
        const channelIdx = this.channelIndexOf(fullName);
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
        // The slot's curve, which the canvas keeps per channel record: a
        // channel switched back on draws with the curve it was left with.
        if (ChannelList.events.CURVE_CHANGE) {
            this.eventHandler.trigger(ChannelList.events.CURVE_CHANGE, {
                name: slot.name,
                curve: slot.curve || null,
            });
        }

        // HD mode's slider domain is the real per-channel image_min/qmax
        // (default mode uses a fixed [0, 255] byte domain and never needs
        // this -- see getImageRange). getRawImageRange already falls back to
        // a generic bit range when stats haven't been fetched yet, so
        // activation itself isn't gated on this fetch -- just refresh the
        // slot's range once real stats land, unless the user has already
        // touched this slot's slider.
        if (this.isHdMode() && !slot.userRangeChanged) {
            this.channelList.ensureChannelStats(slot.name).then(() => {
                // Nor a contrast show's slot: its window is on screen already.
                if (slot.userRangeChanged || slot.rangeSource === "agent") return;
                slot.range = this.getRawImageRange(slot.name);
                this.channelList.sel[fullName] = slot.range;
                this.syncSlotDom(slot);
            });
        }
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

    /**
     * The contrast window, built once per slot and afterwards only told things.
     *
     * The d3-simple-slider this replaced had to be TORN DOWN AND REBUILT for
     * every change of domain, every resize and every expansion, because it was
     * handed a width in pixels and drew an SVG at that width; a `sliderDirty`
     * flag existed on every slot for no other reason. A PlexoraSlider lays
     * itself out, so a domain change is `setBounds` and a value change is a
     * silent `set`, and the thing the user is holding is never replaced under
     * their finger.
     *
     * LOGARITHMIC, because the interesting part of a 16-bit channel is the
     * bottom two percent of its range. The handle holds a position on a
     * 1000-step grid and the slider keeps the real value, so a window set to
     * 1234 by typing stays 1234 -- which the d3 version, reading its value
     * back off the scale, did not.
     */
    syncChannelSlider(slot) {
        if (!slot || !slot.name) return;
        let slider = this.channelSlotSliders.get(slot.index);
        // Built the first time a slot is opened, not before: most slots are
        // never expanded, and a control nobody has asked to see is DOM nobody
        // needs.
        if (!slider && !slot.expanded) return;
        const target = this.el(`channel_slot_slider_${slot.index}`);
        if (!target) return;
        const { min, max } = this.contrastBounds(slot.name);
        const held = [Math.max(slot.range[0], min), Math.max(slot.range[1], min)];
        if (slider) {
            slider.setBounds({ min, max });
            slider.set(held, { silent: true });
            this.sizeRangeFields(slider, max);
            return;
        }
        slider = new PlexoraSlider(target, {
            mode: "range", scale: "log", min, max, step: 1,
            low: held[0], high: held[1],
            // Whole numbers. A contrast window is a count of photons on an
            // integer grid (`step: 1` above), so `formatValue`'s two decimals
            // were two characters of noise -- and on a 16-bit channel they are
            // what pushed "65535.00" out of a sidebar-sized box. `integer`
            // and not only the format, so a typed 1234.6 is saved as the
            // 1235 the box then shows rather than as a number nobody sees.
            decimals: 0, integer: true,
            format: (value) => String(Math.round(value)),
            fieldIds: {
                low: this.slotId("channel_slot_min", slot.index),
                high: this.slotId("channel_slot_max", slot.index),
            },
            ariaLabels: ["Contrast window minimum", "Contrast window maximum"],
            // Inline, at the two ends of the track they describe: no
            // `fieldsSlot`, because there is no longer a second row to put
            // them on. See createChannelSlot.
            //
            // Per tick: a BRUSH_MOVE, which imageViewer coalesces into one
            // repaint per animation frame. On release: the 400ms save.
            onInput: (values) => this.setSlotRange(slot.index, values, true),
            onChange: () => this.scheduleSaveChannels(),
        });
        this.sizeRangeFields(slider, max);
        this.channelSlotSliders.set(slot.index, slider);
    }

    /**
     * How wide the two inline numbers are: exactly the digits the domain can
     * produce, in `ch` over a tabular-nums face, plus the padding that keeps
     * the hover and focus backing off the glyphs.
     *
     * Fixed rather than fitted to the text, and that is the point: a width
     * that tracked the content would resize the box, and therefore the track
     * between the two boxes, on the tick of a drag where 999 becomes 1000 --
     * the handle would slide out from under the pointer. A slot-wide constant
     * also lines every row's track up with every other row's. The byte domain
     * is three characters, a 16-bit HD domain five.
     */
    sizeRangeFields(slider, max) {
        const digits = Math.max(2, String(Math.round(max)).length);
        slider.el?.style?.setProperty("--plx-number-width", `calc(${digits}ch + 8px)`);
    }

    /**
     * A contrast window's slider domain for one channel. Floored at 1 and 2
     * because the slider is logarithmic and log(0) is not a position.
     *
     * Public, with `slotShowing` and `setSlotWindow` below, for a control
     * outside this sidebar that edits a channel's window -- the Thresholding
     * plugin's contrast slider over the viewer. It draws the same domain the
     * slot's own slider does, and asks here rather than copying the floor.
     */
    contrastBounds(name) {
        const range = this.getImageRange(name);
        return { min: Math.max(range[0], 1), max: Math.max(range[1], 2) };
    }

    /** The switched-on slot drawing `name`, or null. At most one:
     *  disableDuplicateChannels turns the others off. */
    slotShowing(name) {
        if (!name) return null;
        return this.channelSlots.find((slot) => slot.enabled && slot.name === name) || null;
    }

    /**
     * Set a slot's window from a control that is not the slot's own slider.
     *
     * The same path a drag of that slider takes -- `setSlotRange(..., true)`,
     * so the window is remembered for the marker and pinned against
     * auto-levelling exactly as a hand on the sidebar would -- and then the
     * one thing a drag there does not need: moving the sidebar's slider,
     * which did not move itself. One state, `slot.range`, and two controls
     * drawn from it. `commit` is the release: the 400ms save.
     */
    setSlotWindow(slotIndex, values, { commit = false } = {}) {
        const slot = this.channelSlots[slotIndex];
        if (!slot || !slot.name) return;
        this.setSlotRange(slotIndex, values, true);
        this.updateSlotReadout(slot);
        if (commit) this.scheduleSaveChannels();
    }

    toggleSlotExpanded(slotIndex) {
        const slot = this.channelSlots[slotIndex];
        if (!slot) return;
        slot.expanded = !slot.expanded;
        this.applySlotExpansion(slot);
    }

    applySlotExpansion(slot) {
        const row = this.slotRow(slot.index);
        if (!row) return;
        const detail = row.querySelector(".channel-slot-detail");
        const toggle = row.querySelector(".channel-slot-expand-toggle");
        if (detail) detail.classList.toggle("is-expanded", Boolean(slot.expanded));
        if (toggle) toggle.classList.toggle("is-expanded", Boolean(slot.expanded));
        if (slot.expanded) this.syncChannelSlider(slot);
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
            // An explicit window ends any ease on its way (easeSlotRange):
            // what was just set stands.
            slot.rangeEase = null;
            slot.rangeEaseTarget = null;
            slot.userRangeChanged = true;
            slot.rangeSource = "user";
            if (slot.name) {
                // Kept in raw 16-bit units, like the saved list: a range
                // remembered in HD mode and put back in default mode (or the
                // other way round) would otherwise be read in the wrong domain
                // -- a raw window read as bytes clamps to [255, 255], a
                // channel pushed off the right end of its slider.
                const raw = this.toRawRangeForSlot(slot);
                if (this.validRawRange(slot.name, raw)) {
                    this.markerRangeOverrides.set(slot.name, [Number(raw[0]), Number(raw[1])]);
                }
            }
        }
        this.channelList.image_channels[slot.name] = slot.range;
        // Not when the slider is the one that moved: it already shows what it
        // just emitted, and writing back into it per tick of a drag is a paint
        // and two field writes for nothing.
        if (!userChanged) this.updateSlotReadout(slot);
        if (slot.enabled && slot.name) {
            this.eventHandler.trigger(ChannelList.events.BRUSH_MOVE, {
                name: slot.name,
                dataRange: [...slot.range],
            });
        }
    }

    /**
     * The one action on the contrast line: Auto, and then the way back from it.
     *
     * Auto is destructive. It replaces whatever window is on screen, and on a
     * channel somebody has already tuned by eye that window is the only copy
     * of a number they cannot get back by pressing Auto again. So the exact
     * pair is taken down before the fit runs, and the button turns into the
     * way back to it.
     *
     * Revert is not a mode. It puts back a range and the two flags that say
     * where that range came from, and nothing else: not the colour, not the
     * marker, not whether the channel is on.
     *
     * The pending revert survives a drag, deliberately. Clearing it on the
     * first tick of one would swap the icon out from under the pointer, and
     * would mean that nudging the auto window by a handle's width silently
     * threw away the range the user was nudging it back towards.
     */
    async onSlotAutoClick(slotIndex) {
        const slot = this.channelSlots[slotIndex];
        if (!slot || !slot.name) return;
        if (slot.preAutoRange) {
            this.revertSlotRange(slotIndex);
            return;
        }
        // What the two numbers read right now, which is what Auto is about to
        // overwrite -- not `markerRangeOverrides`, which holds the last range
        // the user set by hand and may be several edits old, or absent.
        const before = {
            range: [...slot.range],
            userRangeChanged: slot.userRangeChanged,
            autoLeveled: slot.autoLeveled,
            rangeSource: slot.rangeSource || null,
            curve: slot.curve || null,
        };
        this.syncSlotAutoButton(slot, null, { busy: true });
        try {
            // Fitted in the image's curve mode (autoChannel): Linear or Log.
            await this.autoChannel(slotIndex, { force: true });
        } finally {
            // A fit that never landed -- no stats for the channel, or the
            // marker changed under it -- leaves the window where it was, and
            // a Revert icon offering to restore the range already on screen
            // would be a button that does nothing.
            // Where the window is going, not where it is: the fit eases in
            // (easeSlotRange) and has not taken its first frame yet.
            const now = slot.rangeEase && slot.rangeEaseTarget ? slot.rangeEaseTarget : slot.range;
            const moved = Number(now[0]) !== Number(before.range[0])
                || Number(now[1]) !== Number(before.range[1])
                || !(globalThis.PlexoraDisplayCurve?.same?.(slot.curve, before.curve) ?? true);
            if (moved) slot.preAutoRange = before;
            this.syncSlotAutoButton(slot);
        }
    }

    /**
     * Put back the window that was on screen when Auto was pressed.
     *
     * The two flags travel with the range because they are part of what
     * "before" was: a channel that had never been levelled must be free to
     * level itself again the next time it is switched on, exactly as it would
     * have been had Auto never been pressed.
     *
     * `markerRangeOverrides` is not touched. Auto never wrote to it -- both
     * of its passes go through `setSlotRange(..., false)` -- so there is
     * nothing of the user's in there for this to undo.
     */
    revertSlotRange(slotIndex) {
        const slot = this.channelSlots[slotIndex];
        const before = slot?.preAutoRange;
        if (!before) return;
        slot.preAutoRange = null;
        // `false`: this is putting a range back, not setting one. Passing
        // `true` would stamp the restored numbers over the user's remembered
        // override for this marker and pin the channel against auto-levelling
        // -- both of which are exactly the state this is meant to undo.
        this.setSlotRange(slotIndex, before.range, false);
        slot.userRangeChanged = before.userRangeChanged;
        slot.autoLeveled = before.autoLeveled;
        slot.rangeSource = before.rangeSource || null;
        const curves = globalThis.PlexoraDisplayCurve;
        if (curves && !curves.same(slot.curve, before.curve)) {
            this.setSlotCurve?.(slotIndex, before.curve, { save: false, source: null });
        }
        this.scheduleSaveChannels();
        this.syncSlotAutoButton(slot);
        this.syncSlotGatingButton?.(slot);
    }

    /**
     * The icon, its tooltip and whether it can be pressed.
     *
     * Guarded on the state it last drew, because `syncSlotDom` runs this on
     * every colour change, marker change and toggle of fifteen slots, and the
     * icon swap is an `innerHTML` write.
     */
    syncSlotAutoButton(slot, button, options = {}) {
        const node = button
            || this.slotRow(slot.index)?.querySelector(".slider-auto-button");
        if (!node) return;
        const state = options.busy ? "busy" : (slot.preAutoRange ? "revert" : "auto");
        if (node.dataset.state === state) return;
        node.dataset.state = state;
        node.classList.toggle("is-revert", state === "revert");
        node.classList.toggle("is-busy", state === "busy");
        // Not while the GMM fit is in flight: a second click would read the
        // still-unset `preAutoRange` and start a second fit.
        node.disabled = state === "busy";
        const label = state === "revert" ? "Restore previous range" : "Auto contrast";
        node.title = label;
        node.setAttribute("aria-label", label);
        node.innerHTML = state === "revert"
            ? '<span class="fas fa-rotate-left"></span>'
            : '<span class="fas fa-wand-magic-sparkles"></span>';
    }

    // -- display curve ----------------------------------------------------------

    /**
     * Set a slot's display curve (services/displayCurve.js): null linear, or
     * {kind: "asinh", cofactor} in RAW units. The canvas is told
     * (CURVE_CHANGE), the marker remembers it, and `source` (default "user")
     * says who set the slot's look; null leaves the slot's source alone.
     * A Log curve on 8-bit tiles shows steps at the dark end, so it asks for
     * HD (ensureHdForCurve).
     */
    setSlotCurve(slotIndex, curve, { save = true, source = "user" } = {}) {
        const slot = this.channelSlots[slotIndex];
        if (!slot) return;
        const next = PlexoraDisplayCurve.normalise(curve);
        const changed = !PlexoraDisplayCurve.same(slot.curve, next);
        slot.curve = next;
        if (slot.name) this.markerCurves.set(slot.name, next);
        if (source) slot.rangeSource = source;
        if (changed && slot.enabled && slot.name) {
            this.eventHandler.trigger(ChannelList.events.CURVE_CHANGE, {
                name: slot.name,
                curve: next,
            });
        }
        if (next) this.ensureHdForCurve();
        if (save) this.scheduleSaveChannels();
    }

    /**
     * The image-wide curve (the Image card's "Log brightness"): every channel
     * at once. A channel the viewer levelled itself, or a seeded default,
     * levels again in the new mode (autoChannel fits for it); one the gating
     * calibration set takes that calibration's window for the mode; a window
     * the user set keeps its numbers and takes the curve.
     */
    setCurveMode(mode, { save = true } = {}) {
        const next = mode === "asinh" ? "asinh" : "linear";
        if (next === this.curveMode) return;
        this.curveMode = next;
        if (next === "asinh") this.ensureHdForCurve();
        this.channelSlots.forEach((slot) => {
            if (slot && slot.name && slot.visible) this.applyCurveMode(slot);
        });
        if (save) this.scheduleSaveChannels();
        globalThis.dispatchEvent?.(new CustomEvent("plexora:curve-mode-changed", { detail: { mode: next } }));
    }

    /** One slot onto the image's curve mode (setCurveMode). */
    applyCurveMode(slot) {
        const source = slot.rangeSource || (slot.userRangeChanged ? "user" : "auto");
        if (source === "gating" && this.gatingEntry(slot)) {
            this.applyGatingContrast(slot.index, { save: false });
            return;
        }
        if (source === "auto" || source === "default") {
            if (slot.enabled) {
                this.autoChannel(slot.index, { force: true, source: "auto" });
            } else {
                // Levelled in the new mode when it is next switched on.
                slot.autoLeveled = false;
                slot.curve = null;
            }
            return;
        }
        this.setSlotCurve(slot.index, this.curveMode === "asinh" ? this.logCurveFor(slot) : null,
                          { save: false, source: null });
    }

    /**
     * A Log curve for a window that is not refitted (the user's): the
     * channel's own fit's cofactor when it has one (the mixture's background
     * spread, `log_window`), else the one it was last drawn with, else the
     * window's span / DEFAULT_ASINH_SPAN.
     */
    logCurveFor(slot) {
        const fitted = Number(this.channelList?.hasChannelGMM?.[slot.name]?.log_window?.cofactor);
        if (fitted > 0) return { kind: "asinh", cofactor: fitted };
        return this.markerCurves?.get(slot.name)
            || globalThis.PlexoraDisplayCurve?.seeded?.(this.toRawRangeForSlot(slot)) || null;
    }

    /** A Log curve wants 16-bit tiles; asks the viewer once (main.js). */
    ensureHdForCurve() {
        if (this.hdModeOverride !== undefined || this.root !== document || this.idPrefix) return;
        if (this.isHdMode()) return;
        window.dispatchEvent(new CustomEvent("plexora:hd-needed", { detail: { reason: "curve" } }));
    }

    // -- the gating calibration ------------------------------------------------

    /** The calibration's entry for a slot's channel, or null. */
    gatingEntry(slot) {
        if (!slot || !slot.name) return null;
        const channels = this.gatingContrast?.channels || {};
        const fullName = this.dataLayer.getFullChannelName(slot.name);
        return channels[fullName] || channels[slot.name] || null;
    }

    /** An entry's window and curve for the image's curve mode: the
     *  calibration's asinh, or its Linear window (`linear_window`). */
    gatingLook(entry) {
        if (!entry || !Array.isArray(entry.window)) return null;
        if (this.curveMode === "asinh") {
            // A marker the calibration drew straight (a straight line already
            // lifted its background) is bent gently: a cofactor the size of
            // its window, so Log means every channel.
            const span = Number(entry.window[1]) - Number(entry.window[0]);
            const curve = entry.curve && entry.curve.kind === "asinh" ? entry.curve
                : (span > 0 ? { kind: "asinh", cofactor: span } : null);
            return { window: entry.window, curve };
        }
        const linear = Array.isArray(entry.linear_window) ? entry.linear_window : entry.window;
        return { window: linear, curve: null };
    }

    /** Whether a slot draws an entry's window and curve already. */
    showsGating(slot, entry) {
        const look = this.gatingLook(entry);
        if (!look) return false;
        const raw = this.toRawRangeForSlot(slot);
        const close = (a, b) => Math.abs(Number(a) - Number(b))
            <= Math.max(1e-6, 1e-3 * Math.abs(Number(b)), this.isHdMode() ? 0.5 : this.byteStep(slot));
        return close(raw[0], look.window[0]) && close(raw[1], look.window[1])
            && PlexoraDisplayCurve.same(slot.curve, look.curve);
    }

    /** One byte of the slot's quantization window, in raw units. */
    byteStep(slot) {
        const packet = this.quantWindow(slot.name);
        return packet ? Math.max(1, Number(packet.qmax) - Number(packet.qmin)) / 255 : 1;
    }

    /**
     * Put the gating calibration's window and curve on a slot. `source`
     * "gating" unless told otherwise; eased over `ms`. Returns whether it
     * moved anything. The window is raw; converted into the slider's domain.
     */
    applyGatingContrast(slotIndex, { ms = GATING_EASE_MS, save = true, entry = null } = {}) {
        const slot = this.channelSlots[slotIndex];
        const found = entry || this.gatingEntry(slot);
        const look = this.gatingLook(found);
        if (!slot || !slot.name || !look) return false;
        let target = [Number(look.window[0]), Number(look.window[1])];
        if (!(target[1] > target[0])) return false;
        if (!this.isHdMode()) {
            const packet = this.quantWindow(slot.name);
            if (!packet) return false;
            target = this.rawToByteRangeFine(target, packet);
        }
        slot.rangeSource = "gating";
        slot.displayRev = this.gatingContrast?.rev || null;
        slot.userRangeChanged = false;
        slot.autoLeveled = true;
        this.setSlotCurve(slotIndex, look.curve, { save: false, source: "gating" });
        this.easeSlotRange(slotIndex, slot, target, { ms, force: true });
        this.syncSlotGatingButton(slot);
        if (save) this.scheduleSaveChannels();
        return true;
    }

    /** rawToByteRange without rounding to whole bytes: a calibrated window
     *  keeps its precision on the slider (the shader reads floats). */
    rawToByteRangeFine([rmin, rmax], packet) {
        const span = Math.max(packet.qmax - packet.qmin, 1);
        const toByte = (v) => Math.min(255, Math.max(0, ((v - packet.qmin) / span) * 255));
        return [toByte(rmin), toByte(rmax)];
    }

    /**
     * The gating button: shown when the calibration has an entry for the
     * channel. Pressed, it puts that window and curve on (remembering what
     * was there); pressed again, it puts back what was there.
     */
    onSlotGatingClick(slotIndex) {
        const slot = this.channelSlots[slotIndex];
        if (!slot || !slot.name) return;
        if (slot.preGatingRange) {
            const before = slot.preGatingRange;
            slot.preGatingRange = null;
            this.setSlotRange(slotIndex, before.range, false);
            slot.userRangeChanged = before.userRangeChanged;
            slot.autoLeveled = before.autoLeveled;
            slot.rangeSource = before.rangeSource || null;
            this.setSlotCurve(slotIndex, before.curve, { save: false, source: null });
            this.updateSlotReadout(slot);
            this.syncSlotGatingButton(slot);
            this.scheduleSaveChannels();
            return;
        }
        const before = {
            range: [...slot.range],
            userRangeChanged: slot.userRangeChanged,
            autoLeveled: slot.autoLeveled,
            rangeSource: slot.rangeSource || null,
            curve: slot.curve || null,
        };
        if (this.applyGatingContrast(slotIndex)) slot.preGatingRange = before;
        this.syncSlotGatingButton(slot);
    }

    syncSlotGatingButton(slot, button = null) {
        const node = button || this.slotRow(slot.index)?.querySelector(".channel-slot-gating");
        if (!node) return;
        const entry = this.gatingEntry(slot);
        const reverting = Boolean(slot.preGatingRange);
        node.hidden = !entry || (!reverting && this.showsGating(slot, entry));
        const state = reverting ? "revert" : "apply";
        if (node.dataset.state === state) return;
        node.dataset.state = state;
        node.classList.toggle("is-revert", reverting);
        const label = reverting ? "Restore the contrast from before"
            : "Use gating contrast (calibrated on this image's cells)";
        node.title = label;
        node.setAttribute("aria-label", label);
        node.innerHTML = reverting
            ? '<span class="fas fa-rotate-left"></span>'
            : '<span class="fas fa-microscope"></span>';
    }

    /**
     * Apply the calibration to every slot it may change: a channel whose
     * window was set by the viewer's own levelling, a seeded default or an
     * earlier calibration, and is not on it already. Never a window the user
     * set (by hand, an explicit Auto, or a row saved before rows said).
     * `only` limits it to those slot indices.
     */
    syncGatingContrast({ only = null, ms = GATING_EASE_MS } = {}) {
        if (!this.gatingContrast?.channels) return 0;
        let moved = 0;
        this.channelSlots.forEach((slot) => {
            if (!slot || !slot.name || !slot.enabled) return;
            if (only && !only.includes(slot.index)) return;
            this.syncSlotGatingButton(slot);
            const source = slot.rangeSource || (slot.userRangeChanged ? "user" : "auto");
            if (!REPLACEABLE_SOURCES.has(source)) return;
            const entry = this.gatingEntry(slot);
            if (!entry || this.showsGating(slot, entry)) return;
            // A fit still on its way would land over this: it is told not to
            // (autoChannel's stillCurrent reads the gating source).
            if (this.applyGatingContrast(slot.index, { ms, entry })) moved += 1;
        });
        return moved;
    }

    /** Whether this instance is the viewer's own (the only one that asks). */
    ownsProject() {
        return this.persist && this.root === document && !this.idPrefix
            && typeof plexoraUrl === "function";
    }

    /** GET /display_contrast: the calibration as the server holds it now. */
    async fetchGatingContrast() {
        if (!this.ownsProject()) return null;
        const source = window.flaskVariables?.datasource || (typeof datasource !== "undefined" ? datasource : null);
        if (!source) return null;
        try {
            const response = await fetch(plexoraUrl(
                `display_contrast?datasource=${encodeURIComponent(source)}`));
            if (!response.ok) return null;
            const body = await response.json();
            if (body && typeof body === "object") {
                this.gatingContrast = body;
                this.channelSlots.forEach((slot) => slot && this.syncSlotGatingButton(slot));
            }
            return body;
        } catch (error) {
            return null;
        }
    }

    /** POST /display_contrast/compute for `channels` (full names). */
    async computeGatingContrast(channels) {
        const source = window.flaskVariables?.datasource || (typeof datasource !== "undefined" ? datasource : null);
        if (!source || !this.ownsProject()) return null;
        try {
            const response = await fetch(plexoraUrl("display_contrast/compute"), {
                method: "POST",
                headers: { "Accept": "application/json", "Content-Type": "application/json" },
                body: JSON.stringify({ datasource: source, channels }),
            });
            return response.ok ? await response.json() : null;
        } catch (error) {
            return null;
        }
    }

    /**
     * The first open: fetch the calibration; when the server has none for the
     * channels on screen, ask for one (those channels only -- the rest are
     * asked for as they are switched on) and poll with backoff until it is
     * ready or GATING_POLL_LIMIT_MS passes; then apply it to the slots it may
     * change. Never blocks the picture: the hint and the fit show at once.
     */
    async startGatingContrast() {
        if (!this.ownsProject() || this._gatingStarted) return;
        this._gatingStarted = true;
        if (!this._gatingListener) {
            this._gatingListener = () => {
                this.fetchGatingContrast().then(() => this.syncGatingContrast());
            };
            window.addEventListener("plexora:display-contrast-changed", this._gatingListener);
        }
        await this.pollGatingContrast();
    }

    /** The channels on screen, by full name: what the calibration is asked for. */
    enabledChannelNames() {
        return this.channelSlots.filter((slot) => slot && slot.enabled && slot.name)
            .map((slot) => this.dataLayer.getFullChannelName(slot.name));
    }

    /**
     * Fetch the calibration, ask for the channels on screen it lacks, and
     * poll with backoff until they arrive (or GATING_POLL_LIMIT_MS). One poll
     * at a time: a channel switched on meanwhile is picked up by the poll
     * already running, which reads what is on screen on every pass and asks
     * for each channel once. A channel the record holds but does not offer
     * (the nuclear stain) is settled and never asked for again.
     */
    async pollGatingContrast() {
        if (this._gatingPolling) {
            this._gatingAgain = true;
            return;
        }
        this._gatingPolling = true;
        try {
            const asked = new Set();
            const missing = () => this.enabledChannelNames().filter((name) =>
                !(name in (this.gatingContrast?.channels || {}))
                && !(this.gatingContrast?.calibrated || []).includes(name));
            let body = await this.fetchGatingContrast();
            const deadline = Date.now() + GATING_POLL_LIMIT_MS;
            let wait = GATING_POLL_FIRST_MS;
            while (body && body.state !== "unavailable" && Date.now() < deadline) {
                this._gatingAgain = false;
                const fresh = missing().filter((name) => !asked.has(name));
                if (fresh.length) {
                    fresh.forEach((name) => asked.add(name));
                    const reply = await this.computeGatingContrast(fresh);
                    if (reply && reply.state === "unavailable") break;
                }
                this.syncGatingContrast();
                if (body.state !== "computing" && !fresh.length && !this._gatingAgain) break;
                await new Promise((resolve) => window.setTimeout(resolve, wait));
                body = await this.fetchGatingContrast();
                wait = Math.min(wait * 1.6, 8000);
            }
            this.syncGatingContrast();
        } finally {
            this._gatingPolling = false;
        }
    }

    /**
     * Clear every channel's rendering (the Image card's eraser): the channels
     * on screen stay on, each takes its slot's default colour, Log
     * brightness goes off, and its contrast is levelled automatically -- the
     * gating calibration's where the project has one -- with every remembered
     * hand-set window forgotten. Returns what it replaced, for
     * `restoreRendering` (the notice's Undo).
     */
    clearRendering() {
        const before = {
            slots: this.snapshotSlots(),
            mode: this.curveMode,
            overrides: [...this.markerRangeOverrides].map(([name, range]) => [name, [...range]]),
            curves: [...this.markerCurves],
        };
        this.markerRangeOverrides.clear();
        this.markerCurves.clear();
        this.curveMode = "linear";
        this.channelSlots.forEach((slot) => {
            if (!slot || !slot.name || !slot.visible) return;
            slot.preAutoRange = null;
            slot.preGatingRange = null;
            slot.userColorChanged = false;
            this.setSlotColor(slot.index, this.getDefaultColor(slot.index).hex, false);
            slot.userRangeChanged = false;
            slot.autoLeveled = false;
            slot.autoLeveling = false;
            slot.rangeSource = null;
            this.setSlotCurve(slot.index, null, { save: false, source: null });
            // Levelled like a channel just switched on; the calibration
            // takes over from the fit where there is one (autoChannel).
            if (slot.enabled) this.autoChannel(slot.index);
            this.syncSlotAutoButton(slot);
            this.syncSlotGatingButton(slot);
        });
        this.scheduleSaveChannels();
        return before;
    }

    /** Put back what `clearRendering` replaced, and save it. */
    async restoreRendering(before) {
        if (!before || !Array.isArray(before.slots)) return;
        this.curveMode = before.mode === "asinh" ? "asinh" : "linear";
        await this.applyLaunchChannels(before.slots.map((slot) => {
            const row = { name: slot.name, color: slot.colorHex, enabled: Boolean(slot.enabled) };
            if (Array.isArray(slot.range)) row.range = [slot.range[0], slot.range[1]];
            row.curve = slot.curve || null;
            if (slot.source) row.source = slot.source;
            return row;
        }), { silent: false });
        this.markerRangeOverrides = new Map(before.overrides || []);
        this.markerCurves = new Map(before.curves || []);
        this.scheduleSaveChannels();
    }

    /** A channel switched on: its calibration, when the project has none yet. */
    requestGatingContrast(names) {
        if (!this.ownsProject() || !this._gatingStarted) return;
        if (this.gatingContrast?.state === "unavailable") return;
        const full = (names || []).map((name) => this.dataLayer.getFullChannelName(name));
        if (this.gatingContrast && full.every((name) => name in (this.gatingContrast.channels || {})
                || (this.gatingContrast.calibrated || []).includes(name))) {
            this.syncGatingContrast();
            return;
        }
        this.pollGatingContrast();
    }

    /**
     * Ease one channel to a window and curve an agent has settled on, the
     * slider and the canvas moving together (agentBridge's animate_contrast).
     * Resolves when the ease is done. Not saved here: the lease decides.
     */
    async animateChannelTo(slotIndex, rawWindow, curve, { ms = 900, source = "gating" } = {}) {
        const slot = this.channelSlots[slotIndex];
        if (!slot || !slot.name) return false;
        let target = [Number(rawWindow[0]), Number(rawWindow[1])];
        if (!(target[1] > target[0])) return false;
        if (!this.isHdMode()) {
            await this.channelList.ensureChannelStats(slot.name).catch(() => {});
            const packet = this.quantWindow(slot.name);
            if (!packet) return false;
            target = this.rawToByteRangeFine(target, packet);
        }
        slot.expanded = true;
        this.applySlotExpansion(slot);
        slot.userRangeChanged = false;
        slot.autoLeveled = true;
        slot.rangeSource = source;
        this.setSlotCurve(slotIndex, curve && curve.kind === "asinh" ? curve : null,
                          { save: false, source });
        this.easeSlotRange(slotIndex, slot, target, { ms, force: true });
        await new Promise((resolve) => window.setTimeout(resolve, ms + 40));
        this.syncSlotGatingButton(slot);
        return true;
    }

    /**
     * The slot a contrast show works in (agentBridge's animate_contrast):
     * the second one, its marker REPLACED for each channel the show adjusts,
     * so the picture holds the first slot (the nuclear stain, as a rule) and
     * one marker at a time -- never a growing stack of channels. The other
     * slots are switched off for the show; the agent's lease puts them back.
     * A marker already in the first slot is shown where it is. Returns the
     * slot, or null for a channel this image does not have.
     */
    stageContrastSlot(name, { index = 1, color = null, curve = null } = {}) {
        if (!name || !(this.columns || []).includes(name)) return null;
        while (this.channelSlots.length <= index) {
            const used = this.channelSlots.filter((s) => s.name).map((s) => s.name);
            if (!this.createAdditionalSlot(used)) break;
        }
        const first = this.channelSlots[0];
        const slotIndex = first && first.enabled && first.name === name
            ? 0 : Math.min(index, this.channelSlots.length - 1);
        const slot = this.channelSlots[slotIndex];
        if (!slot) return null;
        this.suspendPersistence();
        try {
            this.channelSlots.forEach((other) => {
                if (other.index !== 0 && other.index !== slotIndex && other.enabled && other.name) {
                    this.setSlotEnabled(other.index, false);
                }
            });
            this.setSlotMarker(slotIndex, name,
                { keepColor: true, enable: true, reveal: true, force: true });
            // Up before anything yields: the auto-level setSlotMarker
            // scheduled would otherwise land on the show's window.
            slot.autoLeveled = true;
            slot.userRangeChanged = false;
            if (!slot.enabled) this.setSlotEnabled(slotIndex, true);
            if (color) this.setSlotColor(slotIndex, color, true);
            this.setSlotCurve(slotIndex, curve && curve.kind === "asinh" ? curve : null,
                              { save: false, source: null });
        } finally {
            this.resumePersistence();
        }
        return slot;
    }

    /**
     * A slot's row open with its slider showing, scrolled into the panel's
     * view, and marked as the one being adjusted (`on`), or unmarked.
     */
    revealContrastSlot(slot, on = true) {
        const row = slot && this.slotRow(slot.index);
        if (!row) return;
        row.classList.toggle("is-agent-adjusting", Boolean(on));
        if (!on) return;
        slot.expanded = true;
        this.applySlotExpansion(slot);
        try {
            row.scrollIntoView({ block: "nearest", behavior: "smooth" });
        } catch (error) {
            row.scrollIntoView?.(false);
        }
    }

    /**
     * A raw window in a slot's slider units (bytes off HD, raw in HD); null
     * without the channel's statistics.
     */
    async contrastDomain(slot, rawWindow) {
        const raw = [Number(rawWindow[0]), Number(rawWindow[1])];
        if (!raw.every(Number.isFinite)) return null;
        if (this.isHdMode()) return raw;
        await this.channelList.ensureChannelStats(slot.name).catch(() => {});
        const packet = this.quantWindow(slot.name);
        return packet ? this.rawToByteRangeFine(raw, packet) : null;
    }

    /**
     * Move a slot's window to `to` (slider units) the way a hand drags the
     * slider: through the slider's own logarithmic travel, eased in and out
     * (`ease: "out"` for a short correction), the thumb drawn held while it
     * moves (`handle`: "low", "high" or both when omitted). Every frame is the
     * write a drag makes, so the canvas follows. Resolves false when a hand
     * took the slider or the slot changed marker on the way.
     */
    dragSlotRange(slotIndex, to, { ms = 900, ease = "inOut", handle = null } = {}) {
        const slot = this.channelSlots[slotIndex];
        if (!slot || !slot.name || !Array.isArray(slot.range)) return Promise.resolve(false);
        const from = slot.range.map(Number);
        const target = [Number(to[0]), Number(to[1])];
        if (!target.every(Number.isFinite) || !(target[1] > target[0])) return Promise.resolve(false);
        const slider = this.channelSlotSliders.get(slot.index);
        const ends = handle ? [handle] : ["low", "high"];
        const held = (on) => ends.forEach((which) => slider?.showDrag?.(which, on));
        const marker = slot.name;
        const token = {};
        slot.rangeEase = token;
        slot.rangeEaseTarget = target;
        const write = (range) => {
            this.setSlotRange(slotIndex, range, false);
            this.syncChannelSlider(slot);
        };
        const smooth = globalThis.PlexoraChannelFade?.smooth?.() ?? true;
        if (!smooth || !(ms > 0) || typeof requestAnimationFrame !== "function") {
            slot.rangeEase = null;
            slot.rangeEaseTarget = null;
            write(target);
            return Promise.resolve(true);
        }
        // The slider's travel is logarithmic: moving through log(1 + v) gives
        // the thumb an even speed along the track, as a hand would.
        const lift = (v) => Math.log1p(Math.max(0, v));
        const drop = (v) => Math.expm1(v);
        const a = from.map(lift);
        const b = target.map(lift);
        const curve = ease === "out"
            ? (f) => 1 - (1 - f) ** 3
            : (f) => (f < 0.5 ? 4 * f * f * f : 1 - ((-2 * f + 2) ** 3) / 2);
        held(true);
        return new Promise((resolve) => {
            const started = performance.now();
            const done = (ok) => {
                held(false);
                if (slot.rangeEase === token) {
                    slot.rangeEase = null;
                    slot.rangeEaseTarget = null;
                }
                resolve(ok);
            };
            const step = () => {
                // A hand on the slider (setSlotRange(..., true) takes the
                // token) or another marker in the slot: the show stops here.
                if (slot.rangeEase !== token || slot.name !== marker) {
                    done(false);
                    return;
                }
                const f = Math.min(1, (performance.now() - started) / ms);
                const e = curve(f);
                write([drop(a[0] + (b[0] - a[0]) * e), drop(a[1] + (b[1] - a[1]) * e)]);
                if (f >= 1) {
                    write(target);
                    done(true);
                    return;
                }
                requestAnimationFrame(step);
            };
            requestAnimationFrame(step);
        });
    }

    autoLevelChannelIfNeeded(slot) {
        if (!slot || slot.autoLeveled || slot.autoLeveling || slot.userRangeChanged) {
            return;
        }
        window.setTimeout(() => this.autoChannel(slot.index), 0);
    }

    /**
     * @function applyAutoRange
     * Push an auto-derived [vmin, vmax] (raw 16-bit units) onto a slot,
     * converting into the slider's active domain. Shared by the provisional
     * and final passes of autoChannel so they cannot drift apart.
     */
    applyAutoRange(slotIndex, slot, rawRange, packet) {
        const target = this.isHdMode() ? rawRange : this.rawToByteRange(rawRange, packet);
        this.easeSlotRange(slotIndex, slot, target);
        // A channel put here by a launch state or by a walk to the next sample,
        // which auto-levelled because no range was carried for it. The save
        // below is deliberate for every other caller (see the comment under
        // it), and wrong for this one: `_restoring` is already false by the
        // time the GMM this ran on came back, so without this flag an
        // arrangement that is documented as never being written back WOULD be,
        // a second or two after the page settled, once per channel.
        if (slot.autoSilent) {
            slot.autoSilent = false;
            return;
        }
        // setSlotRange(..., false) deliberately doesn't autosave (auto-leveling isn't a
        // user edit) -- but persistChannelList's raw-unit conversion (toRawRangeForSlot)
        // needs the channel's quantization window, and the very first activation of a
        // channel autosaves (via setSlotEnabled/setSlotMarker's own scheduleSaveChannels
        // call, 400ms after activation) before the fetch that supplies it can finish.
        // Without this, that early save silently persists the still-default byte-domain
        // slot.range mislabeled as raw units, which then gets reinterpreted as raw on
        // every future restore and rescaled into a near-zero sliver of the real range.
        // Re-scheduling a save now that the window exists corrects it.
        this.scheduleSaveChannels();
    }

    /**
     * @function easeSlotRange
     * Bring a slot's range to `target` over RANGE_EASE_MS, eased out, rather
     * than in one step -- for the two ranges auto-levelling lands on its own
     * (applyAutoRange), never for the user's hand.
     *
     * Both arrive while the channel is already on screen: the provisional
     * hint within a request, the mixture fit a second or more later. Applied
     * outright, each is a jump in brightness of the whole channel, and the
     * second reads as the picture correcting itself. Eased, the channel
     * develops into its contrast. Each frame is the same range write a drag
     * of the slider makes (setSlotRange + its readout), so nothing downstream
     * can tell the difference; a newer target takes over from wherever the
     * last one had got to, and a hand on the slider ends it at once.
     *
     * At once instead: with motion off (services/viewerPrefs.js, or the
     * system's reduced-motion setting), for a channel not on screen, or when
     * there is no range to ease from.
     */
    easeSlotRange(slotIndex, slot, target, { ms = RANGE_EASE_MS, force = false } = {}) {
        const finish = () => {
            slot.rangeEase = null;
            slot.rangeEaseTarget = null;
            slot.range = target;
            this.setSlotRange(slotIndex, target, false);
            this.syncChannelSlider(slot);
        };
        const from = Array.isArray(slot.range) ? slot.range.map(Number) : null;
        const smooth = globalThis.PlexoraChannelFade?.smooth?.() ?? true;
        if (!smooth || !slot.enabled || !from || from.length !== 2 || !(ms > 0)
                || !from.every(Number.isFinite) || !target.every((v) => Number.isFinite(Number(v)))
                || (from[0] === Number(target[0]) && from[1] === Number(target[1]))
                || typeof requestAnimationFrame !== "function") {
            finish();
            return;
        }
        const token = {};
        slot.rangeEase = token;
        slot.rangeEaseTarget = target;
        const started = performance.now();
        const to = [Number(target[0]), Number(target[1])];
        const marker = slot.name;
        const hd = this.isHdMode();
        const step = () => {
            if (slot.rangeEase !== token) return;
            // The hand took the slider: what it set stands. And a range in
            // other units (the HD toggle remapped it) or of another marker is
            // no longer this ease's to move. A forced ease (a calibration put
            // on screen on purpose) is ended by a hand through setSlotRange,
            // which takes the token away.
            if ((!force && slot.userRangeChanged) || slot.name !== marker || this.isHdMode() !== hd) {
                slot.rangeEase = null;
                slot.rangeEaseTarget = null;
                return;
            }
            const f = Math.min(1, (performance.now() - started) / ms);
            if (f >= 1) {
                finish();
                return;
            }
            const e = 1 - (1 - f) ** 3;
            this.setSlotRange(slotIndex, [from[0] + (to[0] - from[0]) * e,
                                          from[1] + (to[1] - from[1]) * e], false);
            this.syncChannelSlider(slot);
            requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
    }

    /**
     * @function autoChannel
     * Auto-level a slot in two passes, because the good answer is slow.
     *
     * The authoritative window is get_channel_gmm's vmin/vmax -- a
     * GaussianMixture(3) fit costing 0.2-1.9 s per channel. A new slot starts
     * at [0, 255], the whole byte domain, which against a quantization ceiling
     * of the channel's full-plane max renders the tissue near-black. So until
     * the fit landed the channel was drawn but invisible: measured 6.6 s on a
     * freshly opened project before anything appeared on screen.
     *
     * Pass 1 applies vmin_hint/vmax_hint from get_image_channel_stats (a few
     * ms, percentiles of the same log-intensity distribution; worst case 15
     * byte-levels off the GMM across the reference slide's 19 channels, versus
     * ~234 for the [0, 255] default). Pass 2 replaces it with the real fit.
     *
     * Both passes re-check userRangeChanged, so a user who grabs the slider
     * mid-flight is never overridden by a late-arriving fit.
     *
     * On a z-stack this is the channel's contrast everywhere: every plane,
     * every projection and the 3-D view are drawn with it, and changing the
     * view never levels again (see the constructor). A projection or a coarse
     * 3-D level may want the slider moved; that is the user's, not a fetch's.
     */
    async autoChannel(slotIndex, options = {}) {
        const slot = this.channelSlots[slotIndex];
        if (!slot || !slot.name) return;
        if (!options.force && (slot.userRangeChanged || slot.autoLeveled || slot.autoLeveling)) {
            return;
        }
        const markerName = slot.name;
        // The newest levelling of this slot wins: an older one still waiting
        // on a fetch must not land after it.
        const turn = slot.levelTurn = (slot.levelTurn || 0) + 1;
        // An explicit Auto replaces a window set by hand: without this the
        // ease below stops at its first frame (a hand on the slider ends an
        // ease), and Auto on a hand-set channel did nothing. Revert puts the
        // flag back with the window (preAutoRange).
        if (options.force) slot.userRangeChanged = false;
        // A gating window already on the channel is better than either pass:
        // a late fit never replaces it (an explicit Auto click still does).
        // Nor over a contrast show's slot (`agent`: agentBridge's animate_contrast).
        const stillCurrent = () => slot.name === markerName && slot.levelTurn === turn &&
            (options.force || (!slot.userRangeChanged && slot.rangeSource !== "gating"
                               && slot.rangeSource !== "agent"));
        // An explicit Auto is the user's; a mode switch re-levels as the
        // viewer's own (setCurveMode passes `source: "auto"`).
        const source = options.source || (options.force ? "user" : "auto");
        // In Log the fit's own asinh window (`log_window`: black under the
        // mixture's background, its top dim, the signal's top bright); a
        // provisional hint takes a cofactor from its own span.
        const log = this.curveMode === "asinh";
        const curves = globalThis.PlexoraDisplayCurve;
        slot.autoLeveling = true;
        try {
            // Pass 1 -- provisional, so the channel is visible right away.
            await this.channelList.ensureChannelStats(markerName);
            if (!stillCurrent()) return;
            const stats = this.quantWindow(markerName);
            if (stats && stats.vmin_hint !== undefined) {
                slot.rangeSource = source;
                const hint = [stats.vmin_hint, stats.vmax_hint];
                this.setSlotCurve?.(slotIndex, log ? (curves?.seeded?.(hint) || null) : null,
                                    { save: false, source: null });
                this.applyAutoRange(slotIndex, slot, hint, stats);
                this.askForHdIfCoarse(stats);
            }

            // Pass 2 -- the real fit. Slow and produces no other visible sign
            // that anything is happening, so it reports to the status indicator.
            if (!(markerName in this.channelList.hasChannelGMM)) {
                const task = window.PlexoraStatus?.begin("Auto-contrast");
                try {
                    await this.channelList.getAndDrawChannelGMM(markerName);
                } finally {
                    task?.done();
                }
            }
            if (!stillCurrent()) return;
            const packet = this.channelList.hasChannelGMM[markerName];
            if (!packet) return;
            slot.rangeSource = source;
            const fitted = packet.log_window;
            const logWindow = log && fitted && Array.isArray(fitted.window) && fitted.cofactor > 0;
            const raw = logWindow ? fitted.window : [packet.vmin, packet.vmax];
            this.setSlotCurve?.(slotIndex, !log ? null
                : (logWindow ? { kind: "asinh", cofactor: Number(fitted.cofactor) }
                   : (curves?.seeded?.(raw) || null)), { save: false, source: null });
            this.applyAutoRange(slotIndex, slot, raw, this.quantWindow(markerName) || packet);
            slot.autoLeveled = true;
            // The gating window, when it arrived while the fit ran, takes
            // over from the fit on a channel nobody set.
            if (!options.force) this.syncGatingContrast?.({ only: [slot.index] });
        } finally {
            slot.autoLeveling = false;
        }
    }

    /**
     * A channel's window changed (services/volumeWindows.js -- the exact one
     * replacing the provisional one): the bytes the tiles are made of now map
     * to other raw values, so each slot showing that channel keeps the SAME
     * raw range, moved into the new bytes -- what a class change does
     * (onViewClassChanged). In HD the ranges are raw already.
     */
    onVolumeWindow({ name, window: next } = {}) {
        if (!name || !Array.isArray(next)) return;
        const packet = this.databaseDescription?.[name];
        if (!packet) return;
        const before = { qmin: Number(packet.qmin), qmax: Number(packet.qmax) };
        if (before.qmin === next[0] && before.qmax === next[1]) return;
        const after = { qmin: next[0], qmax: next[1] };
        packet.qmin = after.qmin;
        packet.qmax = after.qmax;
        if (this.isHdMode() || !Number.isFinite(before.qmax)) return;
        this.channelSlots.forEach((slot) => {
            if (!slot.name || this.dataLayer.getFullChannelName(slot.name) !== name) return;
            const remap = (range) => this.rawToByteRange(this.byteToRawRange(range, before), after);
            slot.range = remap(slot.range);
            if (slot.preAutoRange) slot.preAutoRange.range = remap(slot.preAutoRange.range);
            this.setSlotRange(slot.index, slot.range, slot.userRangeChanged);
            this.syncChannelSlider(slot);
        });
    }

    /**
     * A z-stack whose fast tiles cannot draw its own default contrast asks for HD.
     *
     * Fast tiles map [qmin, qmax] -- zero to the channel's brightest voxel on
     * any plane -- onto 256 byte levels. A light-sheet stack with its tissue
     * under 1000 and a few saturated voxels at 65535 has four of them across its
     * default range: a posterised picture no slider can repair. Under
     * MIN_DEFAULT_LEVELS the viewer turns HD on (16-bit tiles, windowed on the
     * GPU; main.js, once per page). Only the viewer's own sidebar asks, and
     * only for a stack: a flat image's tiles are what they always were.
     */
    askForHdIfCoarse(stats) {
        if (this.hdModeOverride !== undefined || this.root !== document || this.idPrefix) return;
        if (!window.__plexora?.viewState?.enabled?.() || this.isHdMode()) return;
        const span = Number(stats.qmax) - Number(stats.qmin);
        const range = Number(stats.vmax_hint) - Number(stats.vmin_hint);
        if (!(span > 0) || !(range > 0)) return;
        const levels = (255 * range) / span;
        if (levels >= ViewerSidebar.MIN_DEFAULT_LEVELS) return;
        window.dispatchEvent(new CustomEvent("plexora:hd-needed", { detail: { levels } }));
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
            range: this.getImageRange(name),
            userColorChanged: false,
            userRangeChanged: false,
            autoLeveled: false,
            autoLeveling: false,
            preAutoRange: null,
        };
        this.channelSlots.push(slot);
        this.el("channel_slot_list")?.appendChild(this.createChannelSlot(slot));
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
        slot.color = color.rgb;
        slot.colorHex = color.hex;
        slot.userColorChanged = false;
        slot.userRangeChanged = false;
        slot.autoLeveled = false;
        slot.autoLeveling = false;
        slot.preAutoRange = null;
        slot.preGatingRange = null;
        slot.curve = null;
        slot.rangeSource = null;
        this.channelSlotSliders.get(slotIndex)?.destroy();
        this.channelSlotSliders.delete(slotIndex);
        this.syncSlotDom(slot);
        this.applySlotExpansion(slot);
        this.updateSelectedCount();
        this.scheduleSaveChannels();
    }

    syncSlotDom(slot) {
        const row = this.slotRow(slot.index);
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
        this.syncSlotAutoButton(slot);
        this.updateSlotReadout(slot);
    }

    /**
     * Take on new names for the image's channels (main.js's adoptChannelNames).
     *
     * A slot holds its channel by NAME, `markerRangeOverrides` is keyed by one,
     * and every marker select is drawn from `this.columns`. Left behind, a slot
     * goes on naming a channel the server no longer has: it reads as an extra
     * marker that matches nothing, and the stats it asks for come back a 404.
     *
     * The slot itself is not rebuilt -- its colour, range and expansion belong
     * to the channel, which has not changed, only its name has.
     */
    renameChannels(renames) {
        const byShort = new Map(renames.map((r) => [r.fromShort, r.to]));
        const renamed = (name) => byShort.get(name) ?? name;
        this.columns = this.columns.map(renamed);
        // Rebuilt whole rather than moved key by key, so two channels that
        // swapped names cannot overwrite each other's remembered range.
        this.markerRangeOverrides = new Map(
            [...this.markerRangeOverrides].map(([name, range]) => [renamed(name), range]));
        this.channelSlots.forEach((slot) => { slot.name = renamed(slot.name); });
        this.markerSelects.forEach((select) => select.setOptions(this.columns));
        // Last: syncSlotDom writes each slot's (now renamed) marker back into
        // its select, so the option list has to be right before it runs.
        this.channelSlots.forEach((slot) => this.syncSlotDom(slot));
    }

    /**
     * Channels this page view was opened with, filtered to ones that exist.
     *
     * Set by the launching call (`plexora.view(..., channels=[...])`), carried
     * in the URL and put on the page as `flaskVariables.launch.channels` -- see
     * page_routes._parse_launch, which is what validates it.
     *
     * A name the image does not have is dropped with a line in the console
     * rather than silently: the two ways to get here are a typo and a stale
     * notebook cell, and both are worth saying out loud once. Absent for every
     * ordinary page load, where this returns [] and nothing below it runs.
     *
     * Never for a scoped instance: Figure Builder's Quick Edit is showing a
     * captured panel's channels, and the page's launch request is not about it.
     */
    launchChannels() {
        if (!this.persist) return [];
        const entries = window.flaskVariables?.launch?.channels;
        if (!Array.isArray(entries) || !entries.length) return [];
        const known = new Set(this.columns);
        const usable = entries.filter((entry) => entry && known.has(entry.name));
        if (usable.length !== entries.length) {
            const missing = entries.filter((entry) => !entry || !known.has(entry.name))
                .map((entry) => entry && entry.name);
            console.warn("Plexora: this image has no channel named", missing);
        }
        return usable;
    }

    /**
     * Throw the slot list away before a rebuild, turning OFF every channel
     * that is on screen now and will not be in the new list.
     *
     * Emptying `channelSlots` alone forgets the slots but not the picture:
     * their channels stay in `channelList.selections` and on the canvas, with
     * no row left in the sidebar to turn them off by (an agent's
     * `set_channels mode:"replace"` did exactly that). A name in `keep` is
     * left on -- the rebuild re-activates it, and channel_add returns early for
     * a channel already drawn, so it does not flash.
     */
    discardChannelSlots(keep = new Set()) {
        const slotList = this.el("channel_slot_list");
        if (slotList) slotList.innerHTML = "";
        (this.channelSlots || []).forEach((slot) => {
            if (slot && slot.enabled && slot.name && !keep.has(slot.name)) this.deactivateChannel(slot);
        });
        this.channelSlots = [];
        this.channelSlotSliders.forEach((slider) => slider.destroy());
        this.channelSlotSliders.clear();
        this.colorPickers.clear();
        this.markerSelects.clear();
    }

    /**
     * Open showing exactly these channels, in this order.
     *
     * The same shape as applySavedChannels -- rebuild the slot list, prefetch
     * every named channel's stats at the server's own concurrency, then fill
     * the slots -- with two differences that are the whole point of it:
     *
     * A colour or a range is optional per channel. Given one, it is honoured
     * the way a saved row's is (raw 16-bit units, converted into whichever
     * domain is showing). Given neither, the channel auto-levels exactly as it
     * would if the user had just picked it from the marker list, which is what
     * makes the short form -- `channels=["DAPI", "CD3"]` -- produce a picture
     * worth looking at rather than a flat one at [0, 255].
     *
     * Nothing here is written back to the project. init() skips its persist
     * call for this branch, and no setter below runs outside `_restoring`.
     * The one exception is a paste from the Image card's menu (layerManager.js),
     * which runs this outside `_restoring` with `silent: false`: there the
     * setters' own scheduleSaveChannels DOES persist, on purpose, because a
     * paste is an edit the user made to this project.
     *
     * An entry's `enabled: false` fills the slot with its marker, colour and
     * range and leaves it off; the default is on, which is what every launch
     * and carry-over row means.
     */
    async applyLaunchChannels(entries, { silent = true } = {}) {
        const slotList = this.el("channel_slot_list");
        if (!slotList) return;
        this.discardChannelSlots(new Set(entries
            .filter((entry) => entry && entry.enabled !== false).map((entry) => entry.name)));

        const wanted = entries.slice(0, this.maxChannelSlots);
        const count = Math.min(Math.max(wanted.length, this.initialChannelSlots), this.maxChannelSlots);
        // Slots past the requested channels still show a marker (off) rather
        // than sitting empty, matching what every other restore path leaves.
        const usedNames = new Set(wanted.map((entry) => entry.name));
        const fallbackNames = this.columns.filter((name) => !usedNames.has(name));
        let fallbackIdx = 0;
        for (let i = 0; i < count; i++) {
            const color = this.getDefaultColor(i);
            const name = i < wanted.length ? "" : (fallbackNames[fallbackIdx++] || "");
            const slot = {
                index: i,
                name,
                color: color.rgb,
                colorHex: color.hex,
                enabled: false,
                visible: true,
                expanded: false,
                range: this.getImageRange(name),
                userColorChanged: false,
                userRangeChanged: false,
                autoLeveled: false,
                autoLeveling: false,
                //: The window Auto replaced, while its Revert icon is showing.
                preAutoRange: null,
            };
            this.channelSlots.push(slot);
            slotList.appendChild(this.createChannelSlot(slot));
        }

        // Bounded by the server's advertised ceiling for the same reason
        // applySavedChannels is: each of these costs one full-resolution
        // channel read, and a launch naming six channels would otherwise put
        // six of them in flight at once on whatever allocation this is.
        const names = wanted.map((entry) => entry.name);
        const openTask = window.PlexoraStatus?.begin("Opening");
        try {
            await plexoraMapWithLimit(names, plexoraChannelConcurrency(), (name) =>
                this.channelList.ensureChannelStats(name).catch(() => {}));
        } finally {
            openTask?.done();
        }

        for (const [i, entry] of wanted.entries()) {
            const slot = this.channelSlots[i];
            if (!slot) continue;
            // Nothing this path auto-levels may be written back to the project
            // -- see applyAutoRange, where the flag is read and cleared. Set
            // before setSlotMarker, which is what schedules the auto-level.
            if (silent && !entry.range) slot.autoSilent = true;
            this.setSlotMarker(slot.index, entry.name, {
                keepColor: true, enable: entry.enabled !== false, reveal: true, force: true,
            });
            if (entry.color) this.setSlotColor(slot.index, entry.color, true);
            if (entry.curve !== undefined) {
                this.setSlotCurve?.(slot.index, entry.curve, { save: false, source: null });
            }
            if (entry.range) {
                // setSlotMarker has already scheduled an auto-level for this
                // slot on a setTimeout(0); these two flags are what it checks
                // before running, and they have to be set before this loop's
                // first await yields -- otherwise the auto-level lands after
                // the explicit range and overwrites it. Same reasoning, and the
                // same fix, as the saved-channel restore above.
                slot.userRangeChanged = true;
                slot.autoLeveled = true;
                let range = [entry.range[0], entry.range[1]];
                if (!this.isHdMode()) {
                    const packet = this.quantWindow(slot.name);
                    if (packet) range = this.rawToByteRange(range, packet);
                }
                this.setSlotRange(slot.index, range, true);
                // Who set it travels with it (a lease given back, a paste):
                // a gating window put back is still the gating calibration's.
                if (entry.source) slot.rangeSource = entry.source;
            }
            slot.expanded = false;
            this.applySlotExpansion(slot);
        }

        // Unawaited, after the channels are already on screen -- the GMM only
        // feeds the Auto button and the curve under each slider.
        const pendingGmm = names.filter((name) => !(name in this.channelList.hasChannelGMM));
        plexoraMapWithLimit(pendingGmm, plexoraChannelConcurrency(), (name) =>
            this.channelList.getAndDrawChannelGMM(name).catch(() => {}));

        this.updateSelectedCount();
    }

    /**
     * The channels carried from the sample the user just left, as launch rows.
     *
     * Two filters, and they are the whole of "component-wise and fault
     * tolerant" for channels: a name this image does not have is dropped (and
     * reported), and if that leaves nothing at all the caller falls through to
     * this project's own saved list -- which is what makes two samples with no
     * channels in common open as an ordinary fresh load rather than as an
     * empty panel.
     *
     * THE COLOUR TRAVELS AND THE WINDOW DOES NOT. A colour is a choice the user
     * made about a marker and would make again; a contrast window is a reading
     * off the previous image's pixels. So the range comes from THIS sample's
     * own saved row when it has one, and otherwise is left out, which makes
     * applyLaunchChannels auto-level the channel against its own data.
     *
     * @param savedRows this project's saved channel list, already fetched by
     *   init(). Its `start`/`end` are raw 16-bit units, which is the domain
     *   applyLaunchChannels expects.
     */
    carriedChannels(savedRows) {
        // A scoped instance is showing a figure panel's channels or one
        // registered layer's, not the project's. It has no business adopting a
        // whole-sample arrangement, and `persist` is already the flag that says
        // "this instance is the project's" -- the same guard launchChannels
        // makes, for the same reason.
        if (!this.persist) return [];
        const carried = window.PlexoraCarryOver?.current?.()?.components?.channels;
        const entries = carried && Array.isArray(carried.entries) ? carried.entries : [];
        if (!entries.length) return [];

        const known = new Set(this.columns || []);
        const usable = [];
        const dropped = [];
        entries.forEach((entry) => {
            if (!entry || !entry.name) return;
            if (!known.has(entry.name)) {
                dropped.push(entry.name);
                return;
            }
            const row = (savedRows || []).find(
                (saved) => saved && saved.channel === entry.name && saved.channel_active);
            const built = { name: entry.name };
            if (entry.color) built.color = entry.color;
            if (row) built.range = [row.start, row.end];
            usable.push(built);
        });

        if (dropped.length && window.PlexoraCarryOver) {
            window.PlexoraCarryOver.report("channels", [
                dropped.length === 1
                    ? `Channel ${dropped[0]} is not in this sample`
                    : `Channels not in this sample: ${dropped.join(", ")}`,
            ]);
        }
        if (usable.length && window.PlexoraCarryOver) window.PlexoraCarryOver.applied();
        return usable;
    }

    /**
     * The channel slots as they stand, for the Image card's "Copy rendering
     * settings" (layerManager.js, services/renderClipboard.js).
     *
     * `range` is RAW 16-bit units or null. Null when the slot is off, and when
     * no quantization window is cached for it: toRawRangeForSlot would then
     * hand back the byte pair unconverted, and labelling that raw would paste
     * a window three orders of magnitude off. A slot without one auto-levels
     * on the image it is pasted into instead.
     */
    snapshotSlots() {
        return this.channelSlots
            .filter((slot) => slot && slot.visible && slot.name)
            .map((slot) => {
                const convertible = slot.enabled
                    && (this.isHdMode() || Boolean(this.quantWindow(slot.name)));
                const range = convertible ? this.toRawRangeForSlot(slot) : null;
                return {
                    index: (this.columns || []).indexOf(slot.name),
                    name: slot.name,
                    colorHex: slot.colorHex,
                    enabled: Boolean(slot.enabled),
                    visible: true,
                    range: range ? [Number(range[0]), Number(range[1])] : null,
                    curve: slot.curve ? { ...slot.curve } : null,
                    source: slot.rangeSource || null,
                };
            });
    }

    /**
     * Resolves once every sidebar module has applied its own saved state.
     *
     * The seam a carried restore needs. `init()` fires `applyOrDefault` at
     * every module without awaiting any of them, which is right for the
     * sidebar -- none of them blocks the others -- and leaves nothing for a
     * caller that has to run strictly AFTER them. Re-imposing a carried marker
     * before this sample's own gates have loaded would read the previous
     * sample's numbers, which is the one thing this feature must never do.
     */
    whenModulesApplied() {
        return this._modulesApplied || Promise.resolve([]);
    }

    async applySavedChannels(rows) {
        const activeRows = rows.filter((row) => row && row.channel_active);
        // Log brightness is the image's: on when the rows were saved on it.
        this.curveMode = activeRows.some((row) => row.curve === "asinh") ? "asinh" : "linear";
        const slotList = this.el("channel_slot_list");
        if (!slotList) return;
        this.discardChannelSlots(new Set(activeRows.map((row) => row.channel)));

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
                range: this.getImageRange(name),
                userColorChanged: false,
                userRangeChanged: false,
                autoLeveled: false,
                autoLeveling: false,
                //: The window Auto replaced, while its Revert icon is showing.
                preAutoRange: null,
            };
            this.channelSlots.push(slot);
            slotList.appendChild(this.createChannelSlot(slot));
        }

        // Prefetch every restored channel's stats up front. The loop below needs
        // each channel's quantization window to convert its saved range out of raw
        // 16-bit units, and awaiting that per iteration made restore strictly
        // serial. It used to await get_channel_gmm here instead -- a ~1 s fit per
        // channel, purely to read the two qmin/qmax fields riding along on the GMM
        // packet -- which cost 5.8 s to restore 3 channels on a cold server. The
        // saved range already IS the auto-level result; no fit is needed to
        // redisplay it.
        //
        // Bounded rather than all-at-once: each of these costs the server one
        // full-resolution channel read, so a project saved with 6 active channels
        // used to put 6 of them in flight simultaneously and occupy every worker.
        // The ceiling comes from the server, so a big node still restores as wide
        // as it ever did.
        const restoredNames = activeRows.slice(0, count).map((row) => row.channel);
        const restoreTask = window.PlexoraStatus?.begin("Restoring");
        try {
            await plexoraMapWithLimit(restoredNames, plexoraChannelConcurrency(), (name) =>
                this.channelList.ensureChannelStats(name).catch(() => {}));
        } finally {
            restoreTask?.done();
        }

        for (const [i, row] of activeRows.slice(0, count).entries()) {
            const slot = this.channelSlots[i];
            if (!slot) continue;
            this.setSlotMarker(slot.index, row.channel, { keepColor: true, enable: true, force: true });
            // setSlotMarker's markerChanged branch always resets userRangeChanged/autoLeveled
            // to false and (since slot.enabled) synchronously schedules autoLevelChannelIfNeeded
            // -> autoChannel via setTimeout(0). We're restoring an explicit saved range below,
            // so shut that off now, before the loop's first await yields control -- setTimeout
            // always fires after this synchronous turn, so autoChannel will see these flags and
            // bail out instead of racing this loop's own GMM fetch/range restore (double-fetching
            // get_channel_gmm/get_image_channel_stats and sometimes clobbering the restored range
            // with an auto-leveled one once its own deferred fetch resolves).
            slot.userRangeChanged = true;
            slot.autoLeveled = true;
            this.setSlotColor(slot.index, this.rgbToHex(row.r, row.g, row.b), true);
            // row.start/row.end are always raw 16-bit units (see
            // persistChannelList/toRawRangeForSlot) -- convert to the
            // currently-active domain before assigning to slot.range.
            let range = [row.start, row.end];
            if (!this.validRawRange(slot.name, range)) {
                // A window saved collapsed or off the channel's scale (an
                // older build wrote byte windows as raw) would load the
                // channel invisible: auto-level it instead.
                slot.userRangeChanged = false;
                slot.autoLeveled = false;
                this.autoLevelChannelIfNeeded(slot);
                slot.expanded = false;
                this.applySlotExpansion(slot);
                continue;
            }
            if (!this.isHdMode()) {
                const packet = this.quantWindow(slot.name);
                if (packet) {
                    range = this.rawToByteRange(range, packet);
                }
            }
            this.setSlotRange(slot.index, range, true);
            // Who set the window, as saved; a row saved before rows said is
            // the user's ("legacy") and is never replaced by a calibration.
            slot.rangeSource = ViewerSidebar.ROW_SOURCES?.has(row.source) ? row.source : "legacy";
            slot.displayRev = typeof row.display_rev === "string" ? row.display_rev : null;
            if (row.curve === "asinh" && Number(row.cofactor) > 0) {
                this.setSlotCurve?.(slot.index, { kind: "asinh", cofactor: Number(row.cofactor) },
                                    { save: false, source: null });
            }
            slot.expanded = false;
            this.applySlotExpansion(slot);
        }

        // The GMM is only needed for the auto button's vmin/vmax and the
        // distribution curves drawn under each slider -- neither blocks display,
        // so fetch them after the channels are already on screen. Unawaited on
        // purpose; failures are the fetch's own problem, not the restore's.
        //
        // Bounded for the same reason as the prefetch above, and it matters more
        // here: a cold fit is 0.2-1.9 s of CPU each, this fires immediately after
        // the prefetch, and unawaited meant every one of them launched in the same
        // tick -- a second burst landing on a pool the first had not yet left.
        const pendingGmm = restoredNames.filter(
            (name) => !(name in this.channelList.hasChannelGMM));
        plexoraMapWithLimit(pendingGmm, plexoraChannelConcurrency(), (name) =>
            this.channelList.getAndDrawChannelGMM(name).catch(() => {}));

        this.updateSelectedCount();
    }

    rgbToHex(r, g, b) {
        const toHex = (value) => Math.max(0, Math.min(255, Math.round(value))).toString(16).padStart(2, "0");
        return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
    }

    persistChannelList() {
        // A scoped instance is showing a figure panel's channels, not the
        // project's, and writing them here would overwrite the project's saved
        // list with somebody else's. It also has none of the main.js globals
        // this method reads, so the guard is load-bearing rather than a policy.
        if (!this.persist) return Promise.resolve(null);
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
        // Raw 16-bit units for every one. `channelList.image_channels` is not
        // a source here: it holds each channel's range in whatever domain its
        // slot last showed (bytes in default mode), so writing it as raw put
        // byte windows like [255, 255] on disk.
        Object.values(imageChannelsIdx).forEach((name) => {
            const remembered = this.markerRangeOverrides.get(name);
            listChannels[name] = this.validRawRange(name, remembered)
                ? [...remembered] : this.getRawImageRange(name);
        });
        const activeChannels = {};
        const listColors = {};
        const listRanges = {};
        const listMeta = {};
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
            let rawRange = this.toRawRangeForSlot(slot);
            // A collapsed or off-scale window is never what anyone meant to
            // keep: the channel would load invisible. Its full range is saved.
            if (!this.validRawRange(slot.name, rawRange)) rawRange = this.getRawImageRange(slot.name);
            activeChannels[idx] = true;
            listColors[idx] = { color: { ...slot.color, opacity: 1 } };
            listRanges[idx] = [rawRange[0] / bitMax, rawRange[1] / bitMax];
            listChannels[slot.name] = rawRange;
            // Who set the window and how it is drawn (data_model._row_meta).
            // A legacy row stays the user's: saved again, it says so.
            const source = slot.rangeSource === "legacy" || !slot.rangeSource
                ? (slot.userRangeChanged ? "user" : "auto") : slot.rangeSource;
            const curve = globalThis.PlexoraDisplayCurve?.normalise?.(slot.curve) || null;
            listMeta[slot.name] = {
                source,
                curve: curve ? "asinh" : "linear",
                ...(curve ? { cofactor: curve.cofactor } : {}),
                ...(source === "gating" && slot.displayRev ? { display_rev: slot.displayRev } : {}),
            };
        });
        return this.dataLayer.saveChannelList(imageChannelsIdx, activeChannels, listColors, listRanges,
                                              listChannels, listMeta);
    }

    /**
     * Stop this project's channel list being written while somebody else is
     * driving the sidebar.
     *
     * `_restoring` already covers the sidebar's own startup restore. This is the
     * same guard for a caller OUTSIDE it: Figure Builder loads a captured
     * panel's channels into the live viewer through the ordinary setters, and
     * those setters schedule an autosave -- so without this, looking at a
     * figure panel would overwrite the project's own saved channels with the
     * figure's, permanently, with nothing on screen to say so.
     *
     * A counter rather than a flag: the restore and a plugin's session can
     * overlap, and the one that finishes first must not re-enable saving for
     * the one still running.
     */
    suspendPersistence() {
        this._persistenceSuspended = (this._persistenceSuspended || 0) + 1;
        window.clearTimeout(this._saveChannelsTimer);
    }

    resumePersistence() {
        this._persistenceSuspended = Math.max(0, (this._persistenceSuspended || 0) - 1);
    }

    scheduleSaveChannels() {
        // Every edit to the project's channels passes through here, saved or
        // not -- which makes it the one cheap place to tell an agent watching
        // this tab that what is on screen changed (services/agentBridge.js).
        window.PlexoraAgentBridge?.touch?.();
        if (this._restoring || this._persistenceSuspended) return;
        window.clearTimeout(this._saveChannelsTimer);
        this._saveChannelsTimer = window.setTimeout(() => {
            // Chained (not just debounced): a slow/out-of-order response from an earlier
            // save could otherwise land after a newer one and silently overwrite it.
            this._channelSaveChain = (this._channelSaveChain || Promise.resolve()).then(() => this.persistChannelList());
        }, 400);
    }

    updateSelectedCount() {
        const count = this.channelSlots.filter((slot) => slot.enabled && slot.name).length;
        const countElement = this.el("num-selected-channels");
        if (countElement) countElement.textContent = count;
        const addButton = this.el("add_channel_button");
        if (addButton) addButton.disabled = this.channelSlots.filter((slot) => slot.visible).length >= this.maxChannelSlots;
    }

    /** Put the slider back in step with the slot, silently: this is the app
     *  catching the control up, not the user moving it. */
    updateSlotReadout(slot) {
        this.channelSlotSliders.get(slot.index)?.set([...slot.range], { silent: true });
    }

    // Raw 16-bit bounds for a channel, regardless of current mode -- the
    // stable representation used for persistence and as the HD-mode slider
    // domain. getImageRange() below is the mode-aware wrapper UI code should
    // normally call instead.
    //
    // The ceiling is qmax, NOT the better-named image_max. image_max is
    // derived from `zarray`, the mean-pooled overview (a pyramid level plus a
    // further block_reduce, ~1000x area averaging), and exists as the
    // companion statistic to image_histogram, which is drawn in that same
    // pooled domain. Mean-pooling dilutes real single/few-pixel peaks, so
    // image_max lands far below the brightest pixel the HD tiles actually
    // contain -- get_channel_quantization_window's docstring records the same
    // trap costing it whole saturated channels. Using it here capped the HD
    // slider below its own data: a channel whose pooled max was 1313 could
    // not have its window moved above 1313, and every raw value above that
    // clamped to full brightness in frag.glsl's range_clamp.
    //
    // qmax is the full-resolution max, and it is also exactly what
    // byteToRawRange maps byte 255 onto -- so both modes' sliders now share a
    // top end, and toggling HD on a full-range channel can no longer strand
    // the upper handle outside the slider's own domain.
    /**
     * Whether `range` is a usable window in raw 16-bit units for `name`: two
     * finite numbers, low below high, and overlapping the channel's own
     * values (a low at or above the channel's maximum shows nothing).
     */
    validRawRange(name, range) {
        if (!Array.isArray(range) || range.length < 2) return false;
        const low = Number(range[0]);
        const high = Number(range[1]);
        if (!Number.isFinite(low) || !Number.isFinite(high) || !(high > low)) return false;
        const [floor, ceiling] = this.getRawImageRange(name);
        return low < ceiling && high > floor;
    }

    /** A remembered range (raw) in the domain the slider shows now, or null. */
    overrideInDomain(name) {
        const raw = this.markerRangeOverrides.get(name);
        if (!this.validRawRange(name, raw)) return null;
        if (this.isHdMode()) return [...raw];
        const packet = this.quantWindow(name);
        if (!packet) return null;
        const bytes = this.rawToByteRange(raw, packet);
        return bytes[1] > bytes[0] ? bytes : null;
    }

    /**
     * The per-channel memory an agent's inspection may change without
     * meaning to keep it (services/agentBridge.js's lease): the remembered
     * windows and the legacy range map. `restoreChannelMemory` puts a
     * snapshot back exactly.
     */
    snapshotChannelMemory() {
        return {
            overrides: [...this.markerRangeOverrides].map(([name, range]) => [name, [...range]]),
            imageChannels: Object.fromEntries(Object.entries(this.channelList.image_channels || {})
                .map(([name, range]) => [name, Array.isArray(range) ? [...range] : range])),
        };
    }

    restoreChannelMemory(snapshot) {
        if (!snapshot) return;
        this.markerRangeOverrides = new Map(snapshot.overrides || []);
        if (this.channelList.image_channels) {
            Object.keys(this.channelList.image_channels).forEach((name) => {
                if (!(name in (snapshot.imageChannels || {}))) delete this.channelList.image_channels[name];
            });
            Object.assign(this.channelList.image_channels, snapshot.imageChannels || {});
        }
    }

    getRawImageRange(name) {
        if (!name) return [0, 1];
        const fullName = this.dataLayer.getFullChannelName(name);
        const desc = this.databaseDescription[fullName] || {};
        const max = desc.qmax || desc.image_max || this.dataLayer.imageBitRange[1] || 65536;
        return [desc.image_min || this.dataLayer.imageBitRange[0] || 0, max];
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

//: Byte levels a stack's fast tiles must spend on its default contrast range
//: before the viewer stops asking for HD (askForHdIfCoarse).
ViewerSidebar.MIN_DEFAULT_LEVELS = 32;
//: A saved row's `source` values this build knows (data_model.CHANNEL_ROW_SOURCES).
ViewerSidebar.ROW_SOURCES = new Set(["user", "auto", "gating", "default"]);
