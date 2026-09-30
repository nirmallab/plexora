/**
 * QcRegistration - the Registration QC section of the QC panel.
 *
 * The check's state is the server's (`qc.registration_*`, kept beside the QC
 * store): on or off, the reference and comparison channels, the DNA rule,
 * the thresholds, the flicker switch, the colours.
 * This module only mirrors it onto the viewer, so an agent that calls
 * `set_registration_check` moves the open tab exactly as the panel does:
 *
 * - the reference goes in channel slot 1 and the comparison in slot 2, in
 *   the check's colours (reference red, every comparison green unless it has
 *   its own) -- unless the user has coloured that slot themselves, which
 *   stands; slots 3+ are never touched. What slots 1 and 2 held is put back
 *   when the check is turned off. Stepping the comparison replaces slot 2, so
 *   the previous comparison is switched off by the same move.
 * - THE COLOURS ARE ONE COLOUR. A swatch here writes the slot's colour and the
 *   check's; a colour picked in Image Channels on slot 1 or 2 is taken back
 *   into the check (`onChannelColor`), so the two panels never disagree.
 * - the pair line's < and > step the comparison (the server's `step`), and
 *   so do Z / X; the caption's flicker glyph pauses or resumes the flicker,
 *   and so does F. The keys only while QC is the selected tool, nothing is
 *   being typed and no dialog is open (gating's rule for its own Z / X).
 * - FLICKER marks the misaligned areas, never the channels: zebra stripes,
 *   like a camera's over-exposure warning, crawl over the pixels where the
 *   two stains disagree: inside the blocks displaced past the threshold,
 *   and anywhere the disagreement is DENSE -- a cell or two that moved,
 *   deformed or lifted between the cycles, too small to shift an 80 um
 *   block but plain on screen (the server marks those pixels; see
 *   `dense_mismatch`). Scattered disagreement in aligned tissue (a nucleus
 *   brighter in one cycle) never stripes. Both channels stay drawn in
 *   their colours throughout. A timer steps the stripes and repaints the
 *   overlay only (never a tile); it stops, the stripes held still, when the
 *   panel hides, the tab is hidden or the window loses focus. F (the
 *   server's `flicker`) takes the stripes off and puts them back.
 * - Play measures every other DNA channel against the reference, one after
 *   another, each channel's `% displaced` landing on its row as it comes.
 *
 * THE PICTURES of the disagreement, drawn through core's shared overlay:
 *
 * - the ZEBRA (the flicker above): its mask is the pixel-level disagreement
 *   from `/registration/disagreement`, asked for the view on screen at the
 *   pyramid level the viewer draws it at, so it is as sharp as the zoom; a
 *   whole-image copy stands in while a new view loads. The PNG's alpha is
 *   the disagreement and its blue channel is 255 on the dense pixels; each
 *   raster is split into those two masks once, on arrival. The stripes are
 *   drawn in screen pixels, so they are the same width at every zoom.
 * - the HEATMAP: the MISMATCH MAP, the stripes' own measure pooled -- per
 *   ~6.5 µm cell, the share of nuclear pixels whose two stains disagree,
 *   smoothed over ~8 µm (the server's `mismatch_map`, two byte planes in the
 *   compute answer). So it heats wherever disagreement crowds together, and
 *   the more of it nearby the hotter: a slid block and two cells that moved
 *   between cycles alike, which a block's shift never showed. Scaled by
 *   default from none to the image's own 99th percentile (never under 20%);
 *   its scale is core's colour bar (PlexoraGradientRange, the transcript
 *   density map's): both ends typeable or dragged, in %, a palette to
 *   choose, Auto to go back.
 * - the VECTOR FIELD: arrows in the direction of the local shift, one every
 *   ~40 screen pixels at any zoom (blocks pooled when zoomed out, the field
 *   interpolated when zoomed in). Their length is per image too: the image's
 *   95th-percentile shift (never under 0.5 px) draws a full-length arrow, so
 *   they are readable whether the image is off by 0.5 px or 20. Their colour
 *   is the heatmap's palette, on the shift's own scale.
 *
 * The heatmap and vector toggles and the palette are per-viewer conveniences
 * (localStorage, wrapped in try/catch); a window set on the bar holds for
 * the pair on screen.
 */
class QcRegistration {

    constructor(ctx, api, host) {
        this.ctx = ctx;
        this.api = api;
        this.host = host;
        this.state = null;          // public_state from the server
        this.last = null;           // the last compute answer (stats + overlay)
        this.scores = new Map();    // comparison name -> stats, from Play
        this.scoring = null;        // {done, total, name} while Play runs
        this.computing = false;
        this.error = null;
        this._computeTimer = null;
        this._computeSeq = 0;
        this._snapshot = null;      // slots 1 and 2 before the check took them
        this._applied = [null, null];       // the colours this module painted
        this._appliedNames = [null, null];  // and the channels it put there
        this._flickerTimer = null;
        this._phase = 0;            // the zebra's crawl, in ticks
        this._shown = true;
        this._keysArmed = false;
        this._alive = true;
        this._onKeyDown = (event) => this.onKeyDown(event);
        this._onVisibility = () => this.syncFlicker();
        this._colorTimer = null;
        this._rasterTimer = null;
        this._rasterSeq = 0;
        this.raster = null;         // {bitmap, box, key}: the view on screen
        this.baseRaster = null;     // the whole image, for while a view loads
        this._heat = null;          // {canvas, key}: the heatmap's small picture
        this._scale = null;         // fieldScale(): the image's own shift scale
        this._clip = null;          // {key, path}: the blocks the zebra may stripe
        this._zebra = null;         // the screen-sized canvas the stripes are cut in
        this.heatWindow = null;     // {key, low, high} 0..1 share, set on the bar; null = auto
        this._map = null;           // mismatchMap(): the decoded mismatch map
        this.ramp = null;           // PlexoraGradientRange, the heatmap's scale
        this._rampSpec = null;      // what it last drew, so a render mid-drag is a no-op
        this._lut = null;           // {palette, table}: 256 RGB stops
        this.pickers = new Map();   // swatch mount id -> ColorSwatchPicker
        this.overlay = null;
        this.views = this.loadViews();
    }

    //: Screen pixels between arrows of the vector field, at any zoom.
    static get ARROW_SPACING() { return 40; }
    //: The zebra: one step every ZEBRA_TICK_MS, ZEBRA_STEP screen pixels a
    //: step, a light and a dark band every ZEBRA_PERIOD screen pixels.
    static get ZEBRA_TICK_MS() { return 90; }
    static get ZEBRA_STEP() { return 1.5; }
    static get ZEBRA_PERIOD() { return 12; }
    //: Floors of the per-image scale, full-resolution pixels: under them a
    //: shift is measurement noise, and must not fill the ramp or an arrow.
    static get HEAT_FLOOR_PX() { return 0.75; }
    //: The mismatch share that fills the heatmap's ramp is never under this:
    //: below it a map with nothing wrong would stretch noise into colour.
    static get HEAT_FLOOR_SHARE() { return 0.2; }
    static get ARROW_FLOOR_PX() { return 0.5; }
    static get VIEWS_KEY() { return "plexora.qc.registration.views"; }

    el(id) {
        return document.getElementById(id);
    }

    setup() {
        this.el("qc_reg_run")?.addEventListener("click", () => this.run());
        // The header's eye is the check itself: off puts slots 1 and 2 back
        // and takes every picture away; on brings the same pair back.
        this.el("qc_reg_eye")?.addEventListener("click", () => this.toggle());
        this.el("qc_reg_view_heat")?.addEventListener("click", () => this.toggleView("heat"));
        this.el("qc_reg_view_vectors")?.addEventListener("click", () => this.toggleView("vectors"));
        this.el("qc_reg_view_flicker")?.addEventListener("click", () => this.toggleFlicker());
        this.el("qc_reg_edit")?.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openMenu(event.currentTarget);
        });
        this.el("qc_reg_ref_name")?.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openReferenceMenu(event.currentTarget);
        });
        this.el("qc_reg_cmp_name")?.addEventListener("click", (event) => {
            event.stopPropagation();
            this.openComparisonMenu(event.currentTarget);
        });
        this.el("qc_reg_prev")?.addEventListener("click", () => this.stepFromPanel("prev"));
        this.el("qc_reg_next")?.addEventListener("click", () => this.stepFromPanel("next"));
        document.addEventListener("visibilitychange", this._onVisibility);
        window.addEventListener("blur", this._onVisibility);
        window.addEventListener("focus", this._onVisibility);
        this.overlay = this.ctx.layers?.addOverlay?.({
            id: "registration",
            kind: "shapes",
            draw: (opts) => this.draw(opts),
        }) || null;
        this.ctx.layers?.onViewportChange?.(() => this.scheduleRaster());
        this.bindChannelColors();
        this.armKeys();
    }

    onShow() {
        this._shown = true;
        this.armKeys();
        this.syncFlicker();
        this.scheduleRaster();
    }

    onHide() {
        this._shown = false;
        this.disarmKeys();
        this.syncFlicker();
    }

    destroy() {
        this._alive = false;
        this.disarmKeys();
        this.stopFlicker();
        window.clearTimeout(this._computeTimer);
        window.clearTimeout(this._colorTimer);
        window.clearTimeout(this._rasterTimer);
        document.removeEventListener("visibilitychange", this._onVisibility);
        window.removeEventListener("blur", this._onVisibility);
        window.removeEventListener("focus", this._onVisibility);
        this.pickers.forEach((picker) => picker.destroy?.());
        this.pickers.clear();
        this.overlay?.remove?.();
        this.overlay = null;
    }

    // -- per-viewer view toggles ---------------------------------------------

    loadViews() {
        const views = { heat: false, vectors: false, palette: "magma" };
        try {
            const raw = JSON.parse(window.localStorage.getItem(QcRegistration.VIEWS_KEY) || "{}");
            for (const key of ["heat", "vectors"]) if (typeof raw[key] === "boolean") views[key] = raw[key];
            if (typeof raw.palette === "string") views.palette = raw.palette;
        } catch (error) {
            // Unreadable or blocked storage: both off.
        }
        return views;
    }

    saveViews() {
        try {
            window.localStorage.setItem(QcRegistration.VIEWS_KEY, JSON.stringify(this.views));
        } catch (error) {
            // A private window: the toggle still works for this page.
        }
    }

    toggleView(key) {
        this.views[key] = !this.views[key];
        this.saveViews();
        // A picture of nothing is no answer: turning a view on turns the check on.
        if (this.views[key] && !this.active) this.set({ active: true });
        this.render();
        this.overlay?.invalidate?.();
    }

    // -- the server's state ----------------------------------------------------

    get active() {
        return Boolean(this.state && this.state.active);
    }

    get ready() {
        return this.active && this.state.status === "ready";
    }

    /** A public_state from /state, a write's answer, or an agent's event. */
    adopt(state) {
        if (!state || state.error) {
            this.render();
            return;
        }
        const before = this.state;
        this.state = state;
        if (!before && state.active && state.flicker === false) this.set({ flicker: true });
        const pairChanged = !before || before.reference !== state.reference
            || before.comparison !== state.comparison
            || JSON.stringify(before.params) !== JSON.stringify(state.params);
        if (before && (before.reference !== state.reference
                       || JSON.stringify(before.params) !== JSON.stringify(state.params))) {
            // Every score was against the old reference (or threshold).
            this.scores.clear();
        }
        if (pairChanged && this.last && (this.last.reference !== state.reference
                                         || this.last.comparison !== state.comparison)) {
            this.last = null;
        }
        if (!this.last && state.last) this.last = state.last;
        if (this.last && this.last.stats && this.last.comparison) {
            this.scores.set(this.last.comparison, this.last.stats);
        }
        if (pairChanged) {
            this.raster = null;
            this.baseRaster = null;
        }
        this.applyViewer();
        this.render();
        if (this.ready && (pairChanged || !this.last || !this.last.overlay)) this.scheduleCompute();
        this.scheduleRaster();
        this.overlay?.invalidate?.();
    }

    /** `qc.registration*` from the agent bridge: another tab or an agent. */
    onEvent(kind, payload) {
        if (kind === "qc.registration" && payload && payload.event === "computed") {
            if (this.last && this.last.fingerprint === payload.fingerprint) return;
            this.api.registration(true).then((answer) => {
                if (!answer.ok) return;
                const data = answer.data;
                if (data.overlay && data.registration && data.registration.last) {
                    this.last = { ...data.registration.last, overlay: data.overlay };
                }
                this.adopt(data.registration);
            }).catch(() => {});
            return;
        }
        const after = payload && payload.after;
        if (after && typeof after === "object" && "active" in after) this.adopt(after);
    }

    async set(body) {
        const answer = await this.api.registrationSet(body).catch(() => null);
        if (!answer || !answer.ok) {
            this.host.message((answer && answer.data.error && answer.data.error.message)
                || "Registration QC could not be changed");
            return null;
        }
        this.adopt(answer.data.registration);
        return answer.data.registration;
    }

    toggle() {
        return this.set({ active: !this.active });
    }

    /** The user moved channel 1 or 2 themselves: that choice wins. */
    adoptSlots() {
        const slots = window.__plexora?.viewerSidebar?.channelSlots || [];
        const update = {};
        const [ref, cmp] = [slots[0], slots[1]];
        if (ref && ref.name && ref.enabled && ref.name !== this.state.reference
                && ref.name !== this._appliedNames[0]) update.reference = ref.name;
        if (cmp && cmp.name && cmp.enabled && cmp.name !== this.state.comparison
                && cmp.name !== this._appliedNames[1] && cmp.name !== (update.reference
                                                                         || this.state.reference)) {
            update.comparison = cmp.name;
        }
        return Object.keys(update).length ? update : null;
    }

    async step(direction) {
        if (!this.active) return;
        const moved = this.adoptSlots();
        if (moved) await this.set(moved);
        const answer = await this.api.registrationStep(direction).catch(() => null);
        if (!answer || !answer.ok) {
            this.host.message((answer && answer.data.error && answer.data.error.message)
                || "No other nuclear channel to compare");
            return;
        }
        this.adopt(answer.data.registration);
    }

    /** < and >: the keys' step, except that a click on a check that is off
     *  turns it on at the neighbour -- a picture of nothing is no answer, the
     *  view toggles' rule. The keys stay inert while it is off. */
    stepFromPanel(direction) {
        if (this.active) return this.step(direction);
        const names = this.state ? this.comparisons() : [];
        if (!names.length) return null;
        const at = names.indexOf(this.state.comparison);
        const next = at < 0 ? 0 : (at + (direction === "next" ? 1 : -1) + names.length) % names.length;
        return this.set({ comparison: names[next], active: true });
    }

    /** The flicker glyph: on while the check is on and flickering. Turning it
     *  on turns the check on, as every view does. */
    toggleFlicker() {
        const on = this.active && Boolean(this.state && this.state.flicker);
        return this.set(on ? { flicker: false } : { flicker: true, active: true });
    }

    press(key) {
        if (!this.active) return;
        if (key === "z") this.step("prev");
        else if (key === "x") this.step("next");
        else if (key === "f") this.set({ flicker: !this.state.flicker });
    }

    // -- computing -------------------------------------------------------------

    scheduleCompute() {
        window.clearTimeout(this._computeTimer);
        // Z / X held down steps quickly: measure the pair it stops on.
        this._computeTimer = window.setTimeout(() => this.compute(), 180);
    }

    async compute(force = false) {
        if (!this.ready) return;
        const seq = ++this._computeSeq;
        const pair = [this.state.reference, this.state.comparison];
        this.computing = true;
        this.render();
        const answer = await this.api.registrationCompute({ force, include_map: true })
            .catch(() => null);
        if (seq !== this._computeSeq) return;
        this.computing = false;
        if (!answer || !answer.ok) {
            this.error = (answer && answer.data.error && answer.data.error.message)
                || "The mismatch could not be measured";
            this.render();
            return;
        }
        this.error = null;
        if (answer.data.reference === pair[0] && answer.data.comparison === pair[1]) {
            this.last = answer.data;
            this.scores.set(pair[1], answer.data.stats);
            this._heat = null;
            this._scale = null;
            this._clip = null;
        }
        this.render();
        this.overlay?.invalidate?.();
    }

    /** The comparison channels, in channel order: every candidate but the
     *  reference, and the comparison even when the rule does not list it. */
    comparisons() {
        const s = this.state || {};
        const names = (s.candidates || []).filter((name) => name !== s.reference);
        if (s.comparison && !names.includes(s.comparison)) names.push(s.comparison);
        return names;
    }

    /** Play: turn the check on, then measure every comparison against the
     *  reference, one at a time, each score landing on its row. */
    async run() {
        if (this.scoring) return;
        if (!this.active) {
            const state = await this.set({ active: true });
            if (!state) return;
        }
        if (this.state.status !== "ready") {
            this.host.message(this.state.status === "needs_second_channel"
                ? "Only one DNA channel: pick a comparison in the settings"
                : "No DNA channel found: set a DNA rule in the settings");
            return;
        }
        const names = this.comparisons();
        this.scoring = { done: 0, total: names.length, name: names[0] };
        this.error = null;
        this.render();
        let failed = 0;
        for (const name of names) {
            if (!this._alive || !this.scoring) return;
            this.scoring.name = name;
            this.render();
            const current = name === this.state.comparison;
            const answer = await this.api.registrationCompute(current ? { include_map: true }
                : { comparison: name, include_overlay: false }).catch(() => null);
            if (answer && answer.ok) {
                this.scores.set(name, answer.data.stats);
                if (current && answer.data.comparison === this.state.comparison) {
                    this.last = answer.data;
                    this._heat = null;
                    this._scale = null;
                    this._clip = null;
                }
            } else {
                failed += 1;
            }
            this.scoring.done += 1;
            this.render();
        }
        this.scoring = null;
        if (failed) this.error = `${failed} channel${failed === 1 ? "" : "s"} could not be measured`;
        this.render();
        this.overlay?.invalidate?.();
    }

    // -- the viewer --------------------------------------------------------------

    static panel() {
        return window.__plexora?.viewerSidebar || null;
    }

    snapshot(panel) {
        return [0, 1].map((i) => {
            const slot = panel.channelSlots[i] || {};
            return { name: slot.name || null, enabled: Boolean(slot.enabled),
                     colorHex: slot.colorHex || null,
                     userColorChanged: Boolean(slot.userColorChanged) };
        });
    }

    /** The colour a role is drawn in: a comparison's own, else the role's. */
    wantedColor(role, name) {
        const s = this.state || {};
        const own = role === "comparison" && name && (s.channel_colors || {})[name];
        if (own) return { color: own, user_set: true };
        return (s.colors || {})[role] || {};
    }

    /** The colour a row shows: while the check holds the channel's slot,
     *  what that slot is actually drawn in (so this panel and Image Channels
     *  never disagree); otherwise the colour it will be given. */
    colorOf(name) {
        const s = this.state || {};
        const role = name === s.reference ? "reference" : "comparison";
        if (this.active && (role === "reference" || name === s.comparison)) {
            const slot = QcRegistration.panel()?.channelSlots?.[role === "reference" ? 0 : 1];
            if (slot && slot.name === name && /^#[0-9a-f]{6}$/i.test(slot.colorHex || "")) {
                return slot.colorHex;
            }
        }
        return this.wantedColor(role, name).color || "#2bd46f";
    }

    /** Slots 1 and 2 as the state says; put back what they held when off. */
    applyViewer() {
        const panel = QcRegistration.panel();
        if (!panel || !Array.isArray(panel.channelSlots)) return;
        if (!this.active) {
            this.stopFlicker();
            this.restore(panel);
            return;
        }
        if (!this.state.reference) {
            this.stopFlicker();
            return;
        }
        this.stopFlicker();
        if (!this._snapshot) this._snapshot = this.snapshot(panel);
        panel.suspendPersistence?.();
        // Our own painting raises Image Channels' colour events: not the user's.
        this._applying = true;
        try {
            [["reference", 0], ["comparison", 1]].forEach(([role, i]) => {
                const name = this.state[role];
                const slot = panel.channelSlots[i];
                if (!slot || !name) return;
                if (slot.name !== name || !slot.enabled || slot.visible === false) {
                    panel.setSlotMarker(i, name, { enable: true, keepColor: true });
                }
                this._appliedNames[i] = name;
                const wanted = this.wantedColor(role, name);
                // The user's own colour on this slot stands, unless they set
                // one for the check itself (here, or set_registration_check).
                const mine = !slot.userColorChanged || (slot.colorHex || "").toLowerCase()
                    === String(this._applied[i] || "").toLowerCase();
                if (wanted.color && (wanted.user_set || mine)
                        && (slot.colorHex || "").toLowerCase() !== wanted.color.toLowerCase()) {
                    panel.setSlotColor(i, wanted.color, false);
                    this._applied[i] = wanted.color;
                } else if (wanted.color && mine) {
                    this._applied[i] = wanted.color;
                }
            });
        } finally {
            this._applying = false;
            panel.resumePersistence?.();
        }
        this.syncFlicker();
    }

    restore(panel) {
        const snap = this._snapshot;
        if (!snap) return;
        this._snapshot = null;
        panel.suspendPersistence?.();
        try {
            snap.forEach((was, i) => {
                const slot = panel.channelSlots[i];
                if (!slot) return;
                if (!was.name) {
                    if (slot.name && slot.enabled) panel.setSlotEnabled(i, false);
                    return;
                }
                if (slot.name !== was.name || slot.enabled !== was.enabled) {
                    panel.setSlotMarker(i, was.name, { enable: was.enabled, keepColor: true });
                    if (!was.enabled && slot.enabled) panel.setSlotEnabled(i, false);
                }
                if (was.colorHex && slot.colorHex !== was.colorHex) {
                    panel.setSlotColor(i, was.colorHex, was.userColorChanged);
                }
                slot.userColorChanged = was.userColorChanged;
            });
        } finally {
            panel.resumePersistence?.();
        }
        this._applied = [null, null];
        this._appliedNames = [null, null];
    }

    // -- one colour, two panels ----------------------------------------------------

    /** A colour picked here: the check's, and the slot's at once. */
    pickColor(name, hex) {
        if (!this.state || !hex) return;
        const role = name === this.state.reference ? "reference" : "comparison";
        const panel = QcRegistration.panel();
        const index = role === "reference" ? 0 : 1;
        const slot = panel?.channelSlots?.[index];
        if (this.active && slot && slot.name === name) {
            this._applying = true;
            try {
                panel.setSlotColor(index, hex, false);
            } finally {
                this._applying = false;
            }
            this._applied[index] = hex;
        }
        window.clearTimeout(this._colorTimer);
        this._colorTimer = window.setTimeout(() => {
            this.set(role === "reference" ? { colors: { reference: hex } }
                : { channel_colors: { [name]: hex } });
        }, 250);
    }

    /** Image Channels' colour events: a colour picked there on slot 1 or 2,
     *  while the check holds them, is taken into the check. Painting done by
     *  this module arrives here too, and matches, so it changes nothing. */
    bindChannelColors() {
        const events = window.__plexora?.viewerSidebar?.eventHandler;
        const name = window.ChannelList?.events?.COLOR_TRANSFER_CHANGE || "COLOR_TRANSFER_CHANGE";
        if (!events || typeof events.bind !== "function") return;
        events.bind(name, (packet) => {
            if (this._alive) this.onChannelColor(packet);
        });
    }

    onChannelColor(packet) {
        if (!this.active || this._applying || !packet || !packet.name) return;
        // Whatever it was, the swatches show what the slots now are.
        this.render();
        const panel = QcRegistration.panel();
        const slots = panel?.channelSlots || [];
        const index = [0, 1].find((i) => slots[i] && slots[i].name === packet.name);
        // Only a colour the user chose there: a slot repainting itself on
        // (re)activation is not a choice.
        if (index === undefined || !slots[index].userColorChanged) return;
        const role = index === 0 ? "reference" : "comparison";
        if (this.state[role] !== packet.name) return;
        const hex = String(slots[index].colorHex || "").toLowerCase();
        if (!/^#[0-9a-f]{6}$/.test(hex)) return;
        if (hex === String(this.wantedColor(role, packet.name).color || "").toLowerCase()) return;
        this._applied[index] = hex;
        window.clearTimeout(this._colorTimer);
        this._colorTimer = window.setTimeout(() => {
            this.set(role === "reference" ? { colors: { reference: hex } }
                : { channel_colors: { [packet.name]: hex } });
        }, 300);
    }

    // -- flicker: zebra stripes crawling over the misaligned pixels ---------------

    /** Whether the stripes should be crawling now. */
    flickering() {
        return this.ready && Boolean(this.state.flicker) && this._shown
            && !document.hidden && (typeof document.hasFocus !== "function" || document.hasFocus());
    }

    syncFlicker() {
        if (this.flickering()) this.startFlicker();
        else this.stopFlicker();
        this.renderFlicker();
    }

    startFlicker() {
        if (this._flickerTimer) return;
        this._flickerTimer = window.setInterval(() => this.tick(), QcRegistration.ZEBRA_TICK_MS);
        this.scheduleRaster();
    }

    /** One step of the crawl: the overlay repaints, never a tile. */
    tick() {
        this._phase = (this._phase + 1) % 100000;
        if (this.raster || this.baseRaster) this.overlay?.invalidate?.();
    }

    stopFlicker() {
        if (!this._flickerTimer) return;
        window.clearInterval(this._flickerTimer);
        this._flickerTimer = null;
        this.overlay?.invalidate?.();
    }

    // -- keys ----------------------------------------------------------------------

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

    /** Whether a bare Z / X / F is meant for Registration QC. */
    acceptsKeys(event) {
        if (!this._keysArmed || !this.active) return false;
        if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return false;
        const typing = window.PlexoraShortcuts?.isTyping
            ? window.PlexoraShortcuts.isTyping()
            : (() => {
                const active = document.activeElement;
                const tag = active && active.tagName;
                return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT"
                    || Boolean(active && active.isContentEditable);
            })();
        if (typing) return false;
        if (window.PlexoraConfirm?.modalOpen?.()) return false;
        if (document.querySelector("dialog[open]")) return false;
        const loader = window.PlexoraToolLoader;
        if (loader && typeof loader.activeTool === "function" && loader.activeTool() !== "qc") {
            return false;
        }
        return true;
    }

    onKeyDown(event) {
        const key = String(event.key || "").toLowerCase();
        if (!["z", "x", "f"].includes(key) || event.repeat && key === "f") return;
        if (!this.acceptsKeys(event)) return;
        event.preventDefault();
        event.stopPropagation();
        this.press(key);
    }

    // -- the zebra's mask: rasters asked for the view on screen -----------------

    pairKey() {
        const s = this.state || {};
        return `${s.reference}|${s.comparison}`;
    }

    wantsRaster() {
        return this.ready && Boolean(this.state.flicker) && this._shown;
    }

    scheduleRaster() {
        if (!this.wantsRaster()) return;
        window.clearTimeout(this._rasterTimer);
        this._rasterTimer = window.setTimeout(() => this.loadRasters(), 220);
    }

    /** The viewer's canvas width in device pixels: the resolution worth asking for. */
    static screenPixels() {
        const node = document.getElementById("openseadragon");
        const width = (node && node.clientWidth) || 1200;
        return Math.round(width * Math.min(2, window.devicePixelRatio || 1));
    }

    async loadRasters() {
        if (!this.wantsRaster()) return;
        const key = this.pairKey();
        const image = this.ctx.dataset?.image || window.__plexora?.dataset?.image || {};
        const fullWidth = Number(image.width) || null;
        const fullHeight = Number(image.height) || null;
        if (!this.baseRaster || this.baseRaster.key !== key) {
            const whole = fullWidth && fullHeight ? [0, 0, fullWidth, fullHeight]
                : [0, 0, 1e9, 1e9];
            const base = await this.fetchRaster(whole, 1024, key);
            if (base) this.baseRaster = base;
        }
        const view = this.ctx.viewer?.viewportImageBounds?.(0);
        if (!view) {
            this.overlay?.invalidate?.();
            return;
        }
        const padX = (view.maxX - view.minX) * 0.15;
        const padY = (view.maxY - view.minY) * 0.15;
        const box = [view.minX - padX, view.minY - padY, view.maxX + padX, view.maxY + padY];
        const covers = (r) => r && r.key === key && r.box[0] <= Math.max(0, view.minX)
            && r.box[1] <= Math.max(0, view.minY) && r.box[2] >= view.maxX - 1
            && r.box[3] >= view.maxY - 1;
        const wanted = Math.min(2048, Math.round(QcRegistration.screenPixels() * 1.3));
        // The whole-image copy is already as sharp as this view needs.
        const baseScale = this.baseRaster ? this.baseRaster.width
            / Math.max(1, this.baseRaster.box[2] - this.baseRaster.box[0]) : 0;
        const viewScale = wanted / Math.max(1, box[2] - box[0]);
        if (covers(this.baseRaster) && baseScale >= viewScale * 0.9) {
            this.raster = null;
        } else if (!(covers(this.raster) && this.raster.scale >= viewScale * 0.9
                     && this.raster.scale <= viewScale * 2.5)) {
            const seq = ++this._rasterSeq;
            const raster = await this.fetchRaster(box, wanted, key);
            if (seq !== this._rasterSeq) return;
            if (raster) this.raster = raster;
        }
        this.overlay?.invalidate?.();
    }

    async fetchRaster(box, maxPx, key) {
        const params = new URLSearchParams({
            datasource: this.ctx.datasource,
            box: box.map((v) => Math.round(v)).join(","),
            max_px: String(Math.max(64, Math.round(maxPx))),
        });
        try {
            const response = await fetch(this.ctx.url("plugins/qc/registration/disagreement")
                + "?" + params);
            if (!response.ok || key !== this.pairKey()) return null;
            const covered = String(response.headers.get("X-QC-Box") || "").split(",").map(Number);
            if (covered.length !== 4 || covered.some((v) => !Number.isFinite(v))) return null;
            const blob = await response.blob();
            const bitmap = typeof createImageBitmap === "function"
                ? await createImageBitmap(blob) : await QcRegistration.imageOf(blob);
            return { bitmap, dense: QcRegistration.denseOf(bitmap), box: covered, key,
                     width: bitmap.width,
                     scale: bitmap.width / Math.max(1, covered[2] - covered[0]) };
        } catch (error) {
            return null;
        }
    }

    /** The raster's dense pixels (blue 255) as a mask of their own, or null
     *  when it has none -- or when the canvas cannot be read back. */
    static denseOf(bitmap) {
        try {
            const canvas = document.createElement("canvas");
            canvas.width = bitmap.width;
            canvas.height = bitmap.height;
            const c = canvas.getContext("2d");
            c.drawImage(bitmap, 0, 0);
            const pixels = c.getImageData(0, 0, canvas.width, canvas.height);
            const data = pixels.data;
            let any = false;
            for (let i = 0; i < data.length; i += 4) {
                if (data[i + 2] !== 255) data[i + 3] = 0;
                else if (data[i + 3]) any = true;
            }
            if (!any) return null;
            c.putImageData(pixels, 0, 0);
            return canvas;
        } catch (error) {
            return null;
        }
    }

    static imageOf(blob) {
        return new Promise((resolve, reject) => {
            const url = URL.createObjectURL(blob);
            const image = new Image();
            image.onload = () => { URL.revokeObjectURL(url); resolve(image); };
            image.onerror = (error) => { URL.revokeObjectURL(url); reject(error); };
            image.src = url;
        });
    }

    // -- the overlay ---------------------------------------------------------------

    draw(opts) {
        if (!this.active || !this.state || this.state.status !== "ready") return;
        const overlay = this.currentOverlay();
        if (!overlay) return;
        const context = opts.context;
        const zoom = opts.zoom || 1;
        if (this.views.heat) this.drawHeat(context, overlay);
        if (this.views.vectors) this.drawVectors(context, zoom, overlay);
        if (this.state.flicker) this.drawZebra(context, overlay);
    }

    /** The block field of the pair on screen, or null. */
    currentOverlay() {
        const last = this.last;
        if (!last || !last.overlay) return null;
        if (last.reference !== this.state.reference || last.comparison !== this.state.comparison) {
            return null;
        }
        return last.overlay;
    }

    /** This image's own scale of shift, full-resolution pixels, from its
     *  tissue blocks: `top` fills the heatmap's ramp (99th percentile),
     *  `unit` draws a full-length arrow (95th percentile). */
    fieldScale(overlay) {
        const key = `${this.last && this.last.fingerprint}|${this.pairKey()}|${overlay.grid.nx}`;
        if (this._scale && this._scale.key === key) return this._scale;
        const shifts = [];
        for (let i = 0; i < overlay.state.length; i++) {
            const state = overlay.state[i];
            if ((state === 1 || state === 2) && overlay.tissue[i] >= 0.2) {
                shifts.push(Math.hypot(overlay.dx_px[i], overlay.dy_px[i]));
            }
        }
        shifts.sort((a, b) => a - b);
        const at = (q) => (shifts.length
            ? shifts[Math.min(shifts.length - 1, Math.floor(q * shifts.length))] : 0);
        const stats = (this.last && this.last.stats) || {};
        const threshold = Number(stats.effective_threshold_px || stats.threshold_px) || null;
        const top = Math.max(at(0.99), QcRegistration.HEAT_FLOOR_PX);
        this._scale = { key, threshold, top,
                        extent: Math.max(top, shifts.length ? shifts[shifts.length - 1] : 0),
                        unit: Math.max(at(0.95), QcRegistration.ARROW_FLOOR_PX) };
        return this._scale;
    }

    /** The mismatch map of the pair on screen, decoded once: {grid, share,
     *  nucleus (Uint8Array, 0..255), minNucleus}, or null when the answer
     *  has none. */
    mismatchMap(overlay) {
        const packed = overlay && overlay.mismatch;
        if (!packed || !packed.share || !packed.nucleus) return null;
        const key = `${this.last && this.last.fingerprint}|${this.pairKey()}|${packed.grid.nx}`;
        if (this._map && this._map.key === key) return this._map;
        const bytes = (text) => {
            const raw = atob(text);
            const out = new Uint8Array(raw.length);
            for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
            return out;
        };
        this._map = { key, grid: packed.grid, share: bytes(packed.share),
                      nucleus: bytes(packed.nucleus),
                      minNucleus: Math.round(255 * (Number(packed.min_nucleus) || 0.05)) };
        return this._map;
    }

    /** The heatmap's own scale, a 0..1 share: `top` fills the ramp (the
     *  map's 99th percentile over tissue), `extent` its largest cell. */
    heatScale(overlay) {
        const map = this.mismatchMap(overlay);
        if (!map) return null;
        if (map.scale) return map.scale;
        const values = [];
        for (let i = 0; i < map.share.length; i++) {
            if (map.nucleus[i] >= map.minNucleus) values.push(map.share[i]);
        }
        values.sort((a, b) => a - b);
        const at = (q) => (values.length
            ? values[Math.min(values.length - 1, Math.floor(q * values.length))] / 255 : 0);
        const top = Math.max(at(0.99), QcRegistration.HEAT_FLOOR_SHARE);
        map.scale = { key: map.key, top,
                      extent: Math.max(top, values.length ? values[values.length - 1] / 255 : 0) };
        return map.scale;
    }

    /** The heatmap's window, a 0..1 share: the bar's, or auto. */
    heatRange(overlay) {
        const scale = this.heatScale(overlay) || { key: null, top: 1 };
        const own = this.heatWindow && this.heatWindow.key === scale.key ? this.heatWindow : null;
        return own ? { low: own.low, high: Math.max(own.high, own.low + 1e-3), auto: false }
            : { low: 0, high: scale.top, auto: true };
    }

    /** t in 0..1 -> [r, g, b] on the chosen palette (core's ramps). */
    color(t) {
        const palette = this.views.palette || "magma";
        if (typeof PlexoraColorRamps === "undefined") return QcRegistration.magma(t);
        if (!this._lut || this._lut.palette !== palette) {
            this._lut = { palette, table: PlexoraColorRamps.ramp(palette, null, 256) };
        }
        const i = Math.max(0, Math.min(255, Math.round(t * 255))) * 3;
        const table = this._lut.table;
        return [table[i], table[i + 1], table[i + 2]];
    }

    /** Magma, for a page without core's ramps: dark to pale yellow. */
    static get MAGMA() {
        return [[0, [40, 11, 84]], [0.25, [114, 31, 129]], [0.5, [183, 55, 121]],
                [0.75, [252, 137, 97]], [1, [252, 253, 191]]];
    }

    static magma(t) {
        const stops = QcRegistration.MAGMA;
        const u = Math.max(0, Math.min(1, t));
        for (let i = 1; i < stops.length; i++) {
            const [t0, c0] = stops[i - 1];
            const [t1, c1] = stops[i];
            if (u <= t1) {
                const f = (u - t0) / (t1 - t0);
                return c0.map((c, k) => Math.round(c + (c1[k] - c) * f));
            }
        }
        return stops[stops.length - 1][1];
    }

    heatCanvas(overlay) {
        const map = this.mismatchMap(overlay);
        if (!map) return null;
        const range = this.heatRange(overlay);
        const key = `${map.key}|${range.low}|${range.high}|${this.views.palette}`;
        if (this._heat && this._heat.key === key) return this._heat.canvas;
        const { nx, ny } = map.grid;
        const canvas = document.createElement("canvas");
        canvas.width = nx;
        canvas.height = ny;
        const context = canvas.getContext("2d");
        const picture = context.createImageData(nx, ny);
        const span = Math.max(1e-3, range.high - range.low);
        for (let i = 0; i < nx * ny; i++) {
            const nucleus = map.nucleus[i];
            if (nucleus < map.minNucleus) continue;
            const t = Math.max(0, Math.min(1, (map.share[i] / 255 - range.low) / span));
            const [r, g, b] = this.color(t);
            const o = i * 4;
            picture.data[o] = r;
            picture.data[o + 1] = g;
            picture.data[o + 2] = b;
            // All tissue is faintly tinted, so the map reads as a surface; the
            // more disagreement nearby the more opaque. Faded where the
            // nuclei thin out, so the tissue's edges do not end in a wall.
            picture.data[o + 3] = Math.round(255 * (0.12 + 0.68 * t)
                * Math.min(1, nucleus / 64));
        }
        context.putImageData(picture, 0, 0);
        this._heat = { canvas, key };
        return canvas;
    }

    drawHeat(context, overlay) {
        if (typeof document === "undefined" || !document.createElement) return;
        const canvas = this.heatCanvas(overlay);
        if (!canvas) return;
        const { x0, y0, step_px: step, nx, ny } = this.mismatchMap(overlay).grid;
        context.save();
        context.imageSmoothingEnabled = true;
        context.imageSmoothingQuality = "high";
        context.drawImage(canvas, x0, y0, nx * step, ny * step);
        context.restore();
    }

    /** The blocks displaced past the threshold, each grown by half a block:
     *  the only place the zebra may stripe. Null when none is. */
    issueClip(overlay) {
        const key = `${this.last && this.last.fingerprint}|${this.pairKey()}|${overlay.grid.nx}`;
        if (this._clip && this._clip.key === key) return this._clip.path;
        const { x0, y0, step_px: step, nx, ny } = overlay.grid;
        let path = null;
        if (typeof Path2D === "function") {
            for (let i = 0; i < nx * ny; i++) {
                if (overlay.state[i] !== 2) continue;
                path = path || new Path2D();
                const bx = i % nx;
                const by = Math.floor(i / nx);
                path.rect(x0 + (bx - 0.5) * step, y0 + (by - 0.5) * step, 2 * step, 2 * step);
            }
        }
        this._clip = { key, path };
        return path;
    }

    /** Zebra stripes over the disagreeing pixels of the misaligned blocks,
     *  and over the dense disagreement anywhere.
     *  The mask is drawn into a screen-sized canvas in image space, then the
     *  stripes are cut into it in screen space: crisp, one width at any zoom. */
    drawZebra(context, overlay) {
        const clip = this.issueClip(overlay);
        const key = this.pairKey();
        const rasters = (this.raster && this.raster.key === key ? [this.raster]
            : [this.baseRaster]).filter((r) => r && r.key === key);
        const dense = rasters.some((r) => r.dense);
        if (!(clip || dense) || !rasters.length || typeof context.getTransform !== "function") {
            return;
        }
        const width = context.canvas.width;
        const height = context.canvas.height;
        if (!this._zebra) this._zebra = document.createElement("canvas");
        const work = this._zebra;
        if (work.width !== width || work.height !== height) {
            work.width = width;
            work.height = height;
        }
        const w = work.getContext("2d");
        const transform = context.getTransform();
        w.setTransform(1, 0, 0, 1, 0, 0);
        w.globalCompositeOperation = "source-over";
        w.clearRect(0, 0, width, height);
        w.setTransform(transform);
        const paint = (pick) => {
            for (const raster of rasters) {
                const mask = pick(raster);
                if (!mask) continue;
                const [x0, y0, x1, y1] = raster.box;
                w.imageSmoothingEnabled = (x1 - x0) / raster.width * transform.a < 1.5;
                // Twice: alpha a becomes 1 - (1 - a)^2, so a pixel that only
                // just disagrees still stripes clearly.
                w.drawImage(mask, x0, y0, x1 - x0, y1 - y0);
                w.drawImage(mask, x0, y0, x1 - x0, y1 - y0);
            }
        };
        if (clip) {
            w.save();
            w.clip(clip);
            paint((raster) => raster.bitmap);
            w.restore();
        }
        if (dense) paint((raster) => raster.dense);
        // The stripes, kept only where the mask is: dark, then light bands.
        w.setTransform(1, 0, 0, 1, 0, 0);
        w.globalCompositeOperation = "source-in";
        w.fillStyle = "rgba(12, 12, 12, 0.8)";
        w.fillRect(0, 0, width, height);
        w.globalCompositeOperation = "source-atop";
        const dpr = Math.max(1, width / Math.max(1, context.canvas.clientWidth || width));
        const period = QcRegistration.ZEBRA_PERIOD * dpr;
        const shift = (this._phase * QcRegistration.ZEBRA_STEP * dpr) % period;
        w.fillStyle = "rgba(255, 255, 255, 0.95)";
        w.beginPath();
        // Bands of constant x + y, crawling along the diagonal.
        for (let d = shift - period - height; d < width + period; d += period) {
            w.moveTo(d, 0);
            w.lineTo(d + period / 2, 0);
            w.lineTo(d + period / 2 + height, height);
            w.lineTo(d + height, height);
            w.closePath();
        }
        w.fill();
        w.globalCompositeOperation = "source-over";
        context.save();
        context.setTransform(1, 0, 0, 1, 0, 0);
        context.drawImage(work, 0, 0);
        context.restore();
    }

    /** The field at a fractional block position (bilinear over evaluated
     *  blocks; null where none of the four was evaluated). */
    static sample(overlay, gx, gy) {
        const { nx, ny } = overlay.grid;
        const ix = Math.floor(gx);
        const iy = Math.floor(gy);
        const fx = gx - ix;
        const fy = gy - iy;
        let dx = 0;
        let dy = 0;
        let weight = 0;
        for (const [ox, oy, w] of [[0, 0, (1 - fx) * (1 - fy)], [1, 0, fx * (1 - fy)],
                                   [0, 1, (1 - fx) * fy], [1, 1, fx * fy]]) {
            const x = ix + ox;
            const y = iy + oy;
            if (x < 0 || y < 0 || x >= nx || y >= ny || w <= 0) continue;
            const i = y * nx + x;
            const state = overlay.state[i];
            if (state !== 1 && state !== 2) continue;
            dx += overlay.dx_px[i] * w;
            dy += overlay.dy_px[i] * w;
            weight += w;
        }
        return weight > 0.25 ? [dx / weight, dy / weight] : null;
    }

    /** Pooled over a k x k patch of blocks, weighted by confidence and tissue. */
    static pool(overlay, bx, by, k) {
        const { nx, ny } = overlay.grid;
        let dx = 0;
        let dy = 0;
        let weight = 0;
        for (let y = by; y < Math.min(ny, by + k); y++) {
            for (let x = bx; x < Math.min(nx, bx + k); x++) {
                const i = y * nx + x;
                const state = overlay.state[i];
                if (state !== 1 && state !== 2) continue;
                const w = Math.max(0.05, overlay.confidence[i]) * Math.max(0.05, overlay.tissue[i]);
                dx += overlay.dx_px[i] * w;
                dy += overlay.dy_px[i] * w;
                weight += w;
            }
        }
        return weight > 0 ? [dx / weight, dy / weight] : null;
    }

    drawVectors(context, zoom, overlay) {
        const { x0, y0, step_px: step, nx, ny } = overlay.grid;
        const blockOnScreen = step * zoom;
        const spacing = QcRegistration.ARROW_SPACING;
        // Zoomed out: pool k x k blocks per arrow. Zoomed in: `sub` arrows per
        // block side, interpolated -- finer detail as it gets room.
        const k = Math.max(1, Math.round(spacing / Math.max(1e-6, blockOnScreen)));
        const sub = k === 1 ? Math.max(1, Math.min(4, Math.floor(blockOnScreen / spacing))) : 1;
        const cell = (k * step) / sub;
        const view = this.ctx.viewer?.viewportImageBounds?.(16) || null;
        const scale = this.fieldScale(overlay);
        const arrows = [];
        if (sub === 1) {
            for (let by = 0; by < ny; by += k) {
                for (let bx = 0; bx < nx; bx += k) {
                    const cx = x0 + (bx + Math.min(k, nx - bx) / 2) * step;
                    const cy = y0 + (by + Math.min(k, ny - by) / 2) * step;
                    if (view && (cx < view.minX || cx > view.maxX
                                 || cy < view.minY || cy > view.maxY)) continue;
                    const d = QcRegistration.pool(overlay, bx, by, k);
                    if (!d) continue;
                    arrows.push([cx, cy, d[0], d[1]]);
                }
            }
        } else {
            const minX = view ? Math.max(x0, view.minX) : x0;
            const minY = view ? Math.max(y0, view.minY) : y0;
            const maxX = view ? Math.min(x0 + nx * step, view.maxX) : x0 + nx * step;
            const maxY = view ? Math.min(y0 + ny * step, view.maxY) : y0 + ny * step;
            const startX = x0 + (Math.floor((minX - x0) / cell) + 0.5) * cell;
            const startY = y0 + (Math.floor((minY - y0) / cell) + 0.5) * cell;
            for (let cy = startY; cy <= maxY; cy += cell) {
                for (let cx = startX; cx <= maxX; cx += cell) {
                    const d = QcRegistration.sample(overlay, (cx - x0) / step - 0.5,
                                                    (cy - y0) / step - 0.5);
                    if (!d) continue;
                    arrows.push([cx, cy, d[0], d[1]]);
                }
            }
        }
        if (!arrows.length) return;
        // Per image: the image's own 95th-percentile shift is a full-length
        // arrow (most of the gap to the next), the same on screen at any
        // zoom; longer ones stop a little past it, so an outlier cannot
        // cross its neighbours.
        const full = (0.8 * spacing) / zoom;
        const head = 6 / zoom;
        context.save();
        context.lineCap = "round";
        context.lineJoin = "round";
        for (const [cx, cy, dx, dy] of arrows) {
            const magnitude = Math.hypot(dx, dy);
            const relative = magnitude / scale.unit;
            const [r, g, b] = this.color(Math.max(0.45, Math.min(1,
                magnitude / Math.max(1e-6, scale.top))));
            context.globalAlpha = 0.6 + 0.4 * Math.min(1, relative);
            const length = Math.max(full * Math.min(1.3, relative), 3 / zoom);
            const angle = Math.atan2(dy, dx);
            const ux = Math.cos(angle);
            const uy = Math.sin(angle);
            const sx = cx - ux * length / 2;
            const sy = cy - uy * length / 2;
            const ex = cx + ux * length / 2;
            const ey = cy + uy * length / 2;
            context.beginPath();
            context.moveTo(sx, sy);
            context.lineTo(ex, ey);
            if (length > 1.6 * head) {
                context.moveTo(ex, ey);
                context.lineTo(ex - head * Math.cos(angle - 0.5), ey - head * Math.sin(angle - 0.5));
                context.moveTo(ex, ey);
                context.lineTo(ex - head * Math.cos(angle + 0.5), ey - head * Math.sin(angle + 0.5));
            }
            // A dark halo under a light arrow, tinted by the ramp: legible
            // over any stain colour, the heatmap included.
            context.strokeStyle = "rgba(0,0,0,0.8)";
            context.lineWidth = 4.2 / zoom;
            context.stroke();
            context.strokeStyle = `rgb(${Math.round((255 + r) / 2)},${Math.round((255 + g) / 2)},${Math.round((255 + b) / 2)})`;
            context.lineWidth = 2 / zoom;
            context.stroke();
        }
        context.restore();
    }

    // -- the section -----------------------------------------------------------------

    static esc(text) {
        return String(text == null ? "" : text).replace(/[&<>"]/g,
            (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    }

    renderFlicker() {
        const f = this.el("qc_reg_view_flicker");
        if (!f) return;
        const on = Boolean(this.active && this.state && this.state.flicker);
        f.setAttribute("aria-pressed", on ? "true" : "false");
        f.title = on ? "Flickering the misaligned areas (F stops)"
            : "Flicker the misaligned areas (F)";
    }

    static pct(stats) {
        if (!stats) return null;
        if (stats.highlighted_pct == null) return "–";
        const n = Number(stats.highlighted_pct);
        return n > 0 && n < 0.1 ? "<0.1%" : `${n.toFixed(1)}%`;
    }

    /** A colour picker mounted once on `mount`, kept in step after. */
    syncPicker(mount, color, title, onChange) {
        if (!mount || typeof ColorSwatchPicker === "undefined") return;
        const hex = String(color || "#9ca3af").toLowerCase();
        let picker = this.pickers.get(mount);
        if (!picker) {
            picker = new ColorSwatchPicker(mount, { value: hex, title, onChange });
            this.pickers.set(mount, picker);
            mount.classList.add("has-picker");
        } else if (String(picker.value || "").toLowerCase() !== hex) {
            picker.setValue(hex);
        }
    }

    render() {
        const tool = this.el("qc_tool_reg");
        if (!tool) return;
        const s = this.state;
        const active = this.active;
        const ready = this.ready;
        tool.dataset.state = this.scoring ? "running" : active ? "active" : "inactive";
        const run = this.el("qc_reg_run");
        if (run) {
            run.setAttribute("aria-disabled", this.scoring ? "true" : "false");
            run.title = this.scoring ? "Registration QC is running"
                : this.scores.size ? "Run Registration QC again: measure every DNA channel"
                    : "Run Registration QC: measure every DNA channel against the reference";
        }
        const views = [["qc_reg_view_heat", this.views.heat],
                       ["qc_reg_view_vectors", this.views.vectors]];
        for (const [id, on] of views) {
            this.el(id)?.setAttribute("aria-pressed", on && active ? "true" : "false");
        }
        const eye = this.el("qc_reg_eye");
        if (eye) {
            eye.setAttribute("aria-pressed", active ? "true" : "false");
            eye.disabled = !s;
            eye.title = active ? "Hide Registration QC: put channels 1 and 2 back"
                : "Show Registration QC";
        }
        this.renderProgress();
        this.renderReference();
        this.renderSummary();
        this.renderNote();
        this.renderRamp();
        this.renderFlicker();
    }

    /** px -> "1.2 µm" (or px when the image has no pixel size). */
    length(px) {
        const um = this.state && this.state.pixel_um;
        const value = um ? px * um : px;
        const text = value >= 10 ? value.toFixed(0) : value >= 1 ? value.toFixed(1) : value.toFixed(2);
        return `${text} ${um ? "µm" : "px"}`;
    }

    /** The heatmap's scale: core's colour bar, full width, while it is on.
     *  Values in µm (px without a pixel size); the window it sets is kept in
     *  full-resolution pixels, for the pair on screen. */
    renderRamp() {
        const mount = this.el("qc_reg_ramp");
        if (!mount) return;
        const overlay = this.active && this.ready ? this.currentOverlay() : null;
        const on = Boolean(overlay && this.views.heat && this.heatScale(overlay)
                           && typeof PlexoraGradientRange !== "undefined");
        mount.hidden = !on;
        if (!on) {
            this._rampSpec = null;
            return;
        }
        const scale = this.heatScale(overlay);
        const range = this.heatRange(overlay);
        const pct = 100;
        if (!this.ramp) {
            this.ramp = new PlexoraGradientRange(mount, {
                onRange: (low, high) => {
                    const key = this.heatScale(this.currentOverlay())?.key;
                    this.heatWindow = low === null || !key ? null
                        : { key, low: low / pct, high: high / pct };
                    this._rampSpec = null;
                    this.renderRamp();
                    this.overlay?.invalidate?.();
                },
                onPalette: (palette) => {
                    this.views.palette = palette;
                    this.saveViews();
                    this._rampSpec = null;
                    this.renderRamp();
                    this.overlay?.invalidate?.();
                },
            });
        }
        this.ramp.container = mount;
        if (this.ramp.typing) return;
        const extent = Math.min(1, Math.max(scale.extent, range.high)) * pct;
        const spec = {
            min: 0,
            max: Math.round(extent) || extent,
            low: Math.round(range.low * pct),
            high: Math.round(range.high * pct),
            palette: this.views.palette || "magma",
            palettes: Object.keys(PlexoraColorRamps.RAMPS),
            auto: range.auto,
            decimals: 0,
            format: (value) => `${Number(value).toFixed(0)}%`,
            caption: "% of nuclear pixels mismatched nearby",
        };
        const signature = JSON.stringify({ ...spec, format: null, open: this.ramp.paletteOpen });
        if (signature === this._rampSpec && mount.firstChild) return;
        this._rampSpec = signature;
        this.ramp.render(spec);
    }

    renderProgress() {
        const progress = this.el("qc_reg_progress");
        if (!progress) return;
        const scoring = this.scoring;
        progress.hidden = !scoring;
        if (!scoring) return;
        const fraction = scoring.total ? scoring.done / scoring.total : 0;
        const fill = this.el("qc_reg_fill");
        if (fill) fill.style.transform = `scaleX(${fraction.toFixed(3)})`;
        const phase = this.el("qc_reg_phase");
        if (phase) {
            phase.textContent = `${scoring.done} / ${scoring.total} · ${scoring.name || ""}`;
            phase.title = `Measuring ${scoring.name} against ${this.state.reference}`;
        }
    }

    renderReference() {
        const s = this.state || {};
        const name = this.el("qc_reg_ref_name");
        if (name) {
            name.textContent = s.reference || "No DNA channel";
            name.classList.toggle("is-missing", !s.reference);
            name.title = s.reference ? `Reference DNA channel: ${s.reference} (click to change)`
                : "No DAPI / DNA / Hoechst channel was found: click to pick one";
        }
        const mount = this.el("qc_reg_ref_color");
        if (mount) {
            mount.hidden = !s.reference;
            if (s.reference) {
                this.syncPicker(mount, this.colorOf(s.reference), "Reference colour",
                                (hex) => this.pickColor(this.state.reference, hex));
            }
        }
        this.renderComparison();
    }

    /** The pair line's right half: the comparison, its colour, and < >. */
    renderComparison() {
        const s = this.state || {};
        const names = this.state ? this.comparisons() : [];
        const cmp = s.comparison && s.comparison !== s.reference ? s.comparison : null;
        const name = this.el("qc_reg_cmp_name");
        if (name) {
            name.textContent = cmp || (names.length ? "Pick a channel" : "None");
            name.classList.toggle("is-missing", !cmp);
            name.title = cmp ? `Comparison DNA channel: ${cmp} (click to change)`
                : names.length ? "Pick the DNA channel to compare with the reference"
                    : "No other DNA channel: widen the DNA rule in the settings";
        }
        const mount = this.el("qc_reg_cmp_color");
        if (mount) {
            mount.hidden = !cmp;
            if (cmp) {
                this.syncPicker(mount, this.colorOf(cmp), `${cmp} colour`,
                                (hex) => this.pickColor(this.state.comparison, hex));
            }
        }
        // Nowhere to step to: no comparison at all, or the one there is.
        const stuck = !names.length || (names.length === 1 && names[0] === cmp);
        for (const id of ["qc_reg_prev", "qc_reg_next"]) {
            const button = this.el(id);
            if (button) button.disabled = stuck;
        }
    }

    static statsWords(stats, s) {
        if (stats.highlighted_pct == null) return "No block had enough tissue and signal to compare";
        const unit = stats.unit === "um" ? `${stats.threshold_um} µm` : `${stats.threshold_px} px`;
        const shift = stats.global_shift_px || {};
        return `${stats.highlighted_pct.toFixed(1)}% of the evaluated tissue is displaced by `
            + `${unit} or more (${stats.blocks.highlighted} of ${stats.blocks.evaluated} blocks; `
            + `${stats.pattern}). Global shift ${shift.magnitude} px against ${s.reference}.`;
    }

    renderSummary() {
        const node = this.el("qc_reg_summary");
        if (!node) return;
        const s = this.state;
        if (!s) {
            node.textContent = "";
            return;
        }
        const measured = [...this.scores.entries()].filter(([, v]) => v && v.highlighted_pct != null);
        const worst = measured.sort((a, b) => b[1].highlighted_pct - a[1].highlighted_pct)[0];
        const found = (s.candidates || []).length;
        node.classList.toggle("is-flagged", Boolean(worst && worst[1].highlighted_pct > 0));
        if (worst) {
            node.textContent = QcRegistration.pct(worst[1]);
            node.title = `Most displaced: ${worst[0]}, ${worst[1].highlighted_pct.toFixed(1)}% `
                + `of the evaluated tissue (${measured.length} channel${measured.length === 1
                    ? "" : "s"} measured)`;
        } else {
            node.textContent = found ? `${found} DNA` : "no DNA";
            node.title = found ? `DNA channels: ${(s.candidates || []).join(", ")}`
                : "No DAPI / DNA / Hoechst channel: set a DNA rule in the settings";
        }
    }

    renderNote() {
        const note = this.el("qc_reg_status");
        if (!note) return;
        const s = this.state;
        let text = "";
        let error = false;
        if (this.error && this.active) {
            text = this.error;
            error = true;
        } else if (s && s.status === "needs_second_channel") {
            text = "One DNA channel only: pick a comparison in the settings";
        } else if (s && s.status === "no_candidates") {
            text = "No DNA channel found: set a DNA rule in the settings";
        }
        note.hidden = !text;
        note.textContent = text;
        note.title = text;
        note.classList.toggle("is-error", error);
    }

    // -- menus ---------------------------------------------------------------------

    openReferenceMenu(anchor) {
        if (anchor.getAttribute("aria-expanded") === "true") {
            QcTree.closePopup();
            return;
        }
        const s = this.state || {};
        const candidates = s.candidates || [];
        const names = window.__plexora?.dataset?.image?.channelNames
            || this.ctx.dataset?.image?.channelNames || [];
        const others = names.filter((n) => !candidates.includes(n));
        const pick = (name) => ({ label: name, checked: name === s.reference,
                                  hint: `Measure every channel against ${name}`,
                                  onSelect: () => this.set({ reference: name }) });
        const items = [{ heading: "DNA channels" }, ...candidates.map(pick)];
        if (!candidates.length) items.push({ label: "None detected", disabled: true });
        if (others.length && others.length <= 60) {
            items.push({ heading: "Other channels" }, ...others.map(pick));
        }
        QcTree.menu(anchor, items, { heading: "Reference channel", className: "qc-picker",
                                     align: "left" });
    }

    openComparisonMenu(anchor) {
        if (anchor.getAttribute("aria-expanded") === "true") {
            QcTree.closePopup();
            return;
        }
        const s = this.state || {};
        const names = this.state ? this.comparisons() : [];
        // The list the rows under the pair used to be: every comparison, in
        // its colour, with its score once Run has measured it.
        const items = names.map((name) => {
            const stats = this.scores.get(name);
            const measuring = this.scoring && this.scoring.name === name;
            return {
                label: name, checked: this.active && name === s.comparison,
                color: this.colorOf(name),
                note: measuring ? "…" : QcRegistration.pct(stats) || "",
                noteTone: stats && stats.highlighted_pct > 0 ? "warn" : "",
                hint: stats ? QcRegistration.statsWords(stats, s)
                    : `Compare ${name} with ${s.reference || "the reference"}`,
                onSelect: () => this.set({ comparison: name, active: true }),
            };
        });
        if (!items.length) items.push({ label: "No other DNA channel", disabled: true });
        QcTree.menu(anchor, items, { heading: "Compare with", className: "qc-picker",
                                     align: "left" });
    }

    openMenu(anchor) {
        if (anchor.getAttribute("aria-expanded") === "true") {
            QcTree.closePopup();
            return;
        }
        const s = this.state || {};
        const row = document.createElement("label");
        row.className = "qc-picker-custom";
        const icon = document.createElement("span");
        icon.className = "fas fa-dna";
        icon.setAttribute("aria-hidden", "true");
        const input = document.createElement("input");
        input.type = "text";
        input.className = "qc-picker-input";
        input.placeholder = s.rule && s.rule.mode === "manual"
            ? (s.rule.pattern || (s.rule.channels || []).join(", ")) : "DNA rule: names or pattern";
        input.maxLength = 200;
        input.spellcheck = false;
        input.autocomplete = "off";
        input.setAttribute("aria-label", "DNA rule: channel names separated by commas, or a pattern; Enter to apply, empty for automatic");
        row.title = "Which channels are nuclear: names separated by commas, or a pattern. Empty = automatic (DAPI, DNA, Hoechst ...)";
        row.append(icon, input);
        input.addEventListener("keydown", (event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            const text = input.value.trim();
            QcTree.closePopup();
            if (!text) {
                this.set({ rule: { mode: "auto" } });
            } else if (text.includes(",")) {
                this.set({ rule: { mode: "manual",
                                   channels: text.split(",").map((t) => t.trim()).filter(Boolean) } });
            } else {
                this.set({ rule: { mode: "manual", pattern: text } });
            }
        });
        const params = s.params || {};
        const items = [];
        if (s.pixel_um) {
            items.push({ heading: "Displaced at" });
            for (const value of [1, 2, 3, 5]) {
                items.push({ label: `≥ ${value} µm`, checked: Number(params.threshold_um) === value,
                             hint: `Count tissue displaced by ${value} µm or more`,
                             onSelect: () => this.set({ params: { threshold_um: value } }) });
            }
        } else {
            items.push({ heading: "Displaced at (no pixel size)" });
            for (const value of [2, 4, 6, 10]) {
                items.push({ label: `≥ ${value} px`, checked: Number(params.threshold_px) === value,
                             hint: `Count tissue displaced by ${value} pixels or more`,
                             onSelect: () => this.set({ params: { threshold_px: value } }) });
            }
        }
        items.push({ label: this.active && s.flicker ? "Stop flickering mismatched areas (F)"
                         : "Flicker mismatched areas (F)",
                     className: "is-sectioned", disabled: !this.active,
                     onSelect: () => this.set({ flicker: !s.flicker }) });
        if (s.rule && s.rule.mode === "manual") {
            items.push({ label: "Automatic DNA rule", className: "is-sectioned",
                         hint: "Find DAPI / DNA / Hoechst channels by name again",
                         onSelect: () => this.set({ rule: { mode: "auto" } }) });
        }
        items.push({ label: "Measure again", className: s.rule && s.rule.mode === "manual" ? "" : "is-sectioned",
                     disabled: !this.ready, hint: "Recompute the mismatch for this pair",
                     onSelect: () => this.compute(true) });
        items.push({ label: "Reset colours", disabled: !this.state,
                     hint: "Reference red, every comparison green",
                     onSelect: () => {
                         this._applied = [null, null];
                         const panel = QcRegistration.panel();
                         (panel?.channelSlots || []).slice(0, 2).forEach((slot) => {
                             if (slot) slot.userColorChanged = false;
                         });
                         this.set({ reset_colors: true });
                     } });
        items.push({ label: "Turn off", disabled: !this.active,
                     hint: "Put channels 1 and 2 back as they were",
                     onSelect: () => this.set({ active: false }) });
        QcTree.menu(anchor, items, { heading: "Registration QC", before: row,
                                     className: "qc-picker" });
    }
}

window.QcRegistration = QcRegistration;
