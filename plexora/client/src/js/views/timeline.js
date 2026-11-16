/**
 * timeline.js - a time series' frames: a pill along the bottom of the image.
 *
 *     ⠿ [▶] ───────●═══──────────────  12 / 100  00:55  [10 fps]
 *
 * ONLY ON A TIME SERIES. An image with one frame never builds it and binds no
 * key. Frames exist only where the file says its T axis is time (a TIFF's
 * cycles are folded into channels, server/volume/sources/tiff.py), so every
 * series this shows is one.
 *
 * A PILL, NOT A BAND. It sits in the bottom-centre dock above the bookmark
 * tray (services/canvasLayout.js), so it takes no room from the image and the
 * image never shifts when it comes or goes; and it is MOVABLE -- pressed
 * anywhere but a button or the slider, it can be dragged anywhere on the
 * canvas and stays there for the tab (canvasLayout Movable, id "timeline");
 * its x puts it back.
 *
 * WHAT IT CHANGES IS `t` AND NOTHING ELSE. A frame is a view-state change
 * (services/viewState.js), drawn the way a plane step is: the new frame is
 * built off screen and swapped in when it covers the view, so the zoom, the
 * pan, the plane, the channels and their contrast stay as they are. On a
 * stack with depth, z (the strip on the right, views/depthStrip.js) and t
 * (this) move independently.
 *
 * PLAY SHOWS EVERY FRAME. It asks for the next frame only once the one it
 * asked for is on screen, and never sooner than the rate asked for allows
 * (`fps`, Settings: 2-30, kept with the viewer's preferences). When frames
 * arrive slower than that -- a cold remote series, the website's Python --
 * playback slows rather than skipping: the rate chip says what it is
 * achieving ("6 of 10 fps"), and a frame still on its way shows as a hollow
 * thumb. Meanwhile the frames ahead are prepared at the rate it plays at
 * (planeMotion.NeighbourPrefetch `lead`), and the lighter stretch of the
 * track after the thumb is the frames already loaded. At the last frame it
 * starts over, or stops with Loop off.
 *
 * Keys, never while typing: `.` / `,` one frame (as before), Shift+Space
 * plays and pauses -- not while the ROI tool, whose Space pans, is open.
 * Video Studio has its own timeline along the bottom; while it is up this
 * one steps aside (and stops), since its Space and T are its own.
 *
 * Classic script, `window.PlexoraTimeline`.
 */
(function (global) {
    "use strict";

    //: The rates offered (frames a second). Any 1-60 is kept (viewerPrefs).
    const RATES = [2, 5, 10, 20, 30];
    const DEFAULT_PLAYBACK = { fps: 10, loop: true };
    //: Achieved below this share of the rate asked for, for SLOW_AFTER_MS,
    //: and the chip says so.
    const SLOW_SHARE = 0.85;
    const SLOW_AFTER_MS = 800;
    //: A frame asked for and not on screen after this long reads as waiting.
    const WAIT_AFTER_MS = 250;
    //: How often the loaded stretch of the track is read again at rest.
    const AHEAD_EVERY_MS = 400;
    //: How far the loaded stretch is looked for past the thumb.
    const AHEAD_MAX = 12;
    //: Narrower than this (px), the pill drops the elapsed time and the
    //: chip keeps only its number.
    const NARROW_PX = 440;
    //: Video Studio's band along the bottom (videoTimeline.js reserve).
    const STUDIO_BAND = "timeline";
    const TIME_UNITS = {
        ns: 1e-9, nanosecond: 1e-9, nanoseconds: 1e-9,
        "µs": 1e-6, us: 1e-6, microsecond: 1e-6, microseconds: 1e-6,
        ms: 1e-3, millisecond: 1e-3, milliseconds: 1e-3,
        s: 1, sec: 1, second: 1, seconds: 1,
        min: 60, minute: 60, minutes: 60,
        h: 3600, hr: 3600, hour: 3600, hours: 3600,
        d: 86400, day: 86400, days: 86400,
    };

    const now = () => (global.performance?.now ? global.performance.now() : Date.now());
    const raf = (fn) => (typeof global.requestAnimationFrame === "function"
        ? global.requestAnimationFrame(fn) : setTimeout(fn, 16));
    const unraf = (id) => (typeof global.cancelAnimationFrame === "function"
        ? global.cancelAnimationFrame(id) : clearTimeout(id));
    const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

    function el(tag, className, attrs = {}) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        for (const [key, value] of Object.entries(attrs)) {
            if (value === null || value === undefined) continue;
            if (key === "text") node.textContent = value;
            else node.setAttribute(key, value);
        }
        return node;
    }

    function icon(name) {
        return el("span", `fas fa-${name}`, { "aria-hidden": "true" });
    }

    function isTyping() {
        if (global.PlexoraShortcuts?.isTyping) return global.PlexoraShortcuts.isTyping();
        const active = document.activeElement;
        if (!active) return false;
        const tag = active.tagName;
        return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || active.isContentEditable;
    }

    /**
     * Frame `t` (0-based) as time since the first, from the series' step
     * (`{value, unit}`), or "" when the file states none: "12.5 s", "4:05",
     * "1:02:30". A unit it does not know is written as given.
     */
    function elapsed(t, step) {
        const value = Number(step?.value);
        if (!(value > 0)) return "";
        const unit = String(step?.unit || "s").trim();
        const scale = TIME_UNITS[unit] ?? TIME_UNITS[unit.toLowerCase()];
        if (scale === undefined) {
            const amount = t * value;
            return `${amount.toFixed(amount < 10 && amount % 1 ? 1 : 0)} ${unit}`;
        }
        const seconds = t * value * scale;
        if (seconds < 60) {
            if (seconds > 0 && seconds < 1) return `${Math.round(seconds * 1000)} ms`;
            return `${seconds.toFixed(seconds < 10 && seconds % 1 ? 1 : 0)} s`;
        }
        const whole = Math.round(seconds);
        const h = Math.floor(whole / 3600);
        const m = Math.floor((whole % 3600) / 60);
        const s = whole % 60;
        const pad = (n) => String(n).padStart(2, "0");
        return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
    }

    class Timeline {
        constructor(viewState, { viewerManager = null, prefetch = null, prefs = null } = {}) {
            this.viewState = viewState;
            //: For the loaded stretch: its prefetched frames (`standby`).
            this.viewerManager = viewerManager;
            //: planeMotion's NeighbourPrefetch, told the rate while playing.
            this.prefetch = prefetch;
            this.prefs = prefs || global.PlexoraViewerPrefs || null;
            this.root = null;
            this.movable = null;
            this.playing = false;
            this.loop = 0;
            //: When the frame on screen was asked for, and the gap before it.
            this.steppedAt = 0;
            //: Frames a second actually shown while playing (a moving average).
            this.achieved = 0;
            this.slowSince = null;
            //: When the frame on screen now was first wanted but not shown.
            this.waitingSince = null;
            this.waitingFor = null;
            this.drag = null;
            this.hoverT = null;
            this.stepKey = null;
            this.hiddenByStudio = false;
            this.aheadTimer = 0;
            this._unsubscribe = null;
            this._onKey = (event) => this.onKey(event);
            this._onKeyUp = (event) => this.onKeyUp(event);
            this._onLayout = () => this.followStudio();
            this._onPrefs = () => this.paint();
            this._onChanged = () => this.paint();
        }

        get frames() {
            return this.viewState?.frames || 1;
        }

        /** `{fps, loop}`, from the viewer's preferences. */
        playback() {
            const held = this.prefs?.playback?.();
            return held && Number(held.fps) > 0 ? held : { ...DEFAULT_PLAYBACK };
        }

        mount() {
            const vs = this.viewState;
            if (this.root || !vs?.enabled?.() || this.frames < 2) return false;
            const layout = global.PlexoraCanvasLayout;
            if (!layout?.get?.()) return false;
            this.root = this.build();
            layout.place(this.root, { corner: "bottom-center", order: 30, id: "timeline" });
            this.movable = layout.movable(this.root, { id: "timeline",
                                                       back: "Move the timeline back" });
            // The x goes at the end of the pill, not inside the handle's text.
            if (this.movable?.reset) this.root.appendChild(this.movable.reset);
            this._unsubscribe = vs.subscribe(() => this.paint());
            global.addEventListener("plexora:view-changed", this._onChanged);
            global.addEventListener("plexora:viewer-prefs-changed", this._onPrefs);
            global.addEventListener("plexora:canvas-layout", this._onLayout);
            document.addEventListener("keydown", this._onKey);
            document.addEventListener("keyup", this._onKeyUp);
            if (typeof ResizeObserver === "function") {
                this.sizes = new ResizeObserver(() => this.fitWidth());
                this.sizes.observe(this.root);
            }
            this.followStudio();
            this.fitWidth();
            this.paint();
            this.watchAhead();
            return true;
        }

        destroy() {
            this.stop({ persist: false });
            this.endHover();
            this.closeRates();
            clearInterval(this.aheadTimer);
            this.sizes?.disconnect?.();
            this._unsubscribe?.();
            global.removeEventListener("plexora:view-changed", this._onChanged);
            global.removeEventListener("plexora:viewer-prefs-changed", this._onPrefs);
            global.removeEventListener("plexora:canvas-layout", this._onLayout);
            document.removeEventListener("keydown", this._onKey);
            document.removeEventListener("keyup", this._onKeyUp);
            this.movable?.dispose?.();
            this.root?.remove();
            this.root = null;
        }

        // -- building ------------------------------------------------------

        build() {
            const root = el("div", "viewer-timeline", {
                id: "viewer_timeline", role: "group", "aria-label": "Time series",
                "data-viewer-furniture": "",
            });
            const grip = el("span", "viewer-timeline-grip", { "aria-hidden": "true",
                                                              title: "Drag to move" });
            this.playButton = el("button", "viewer-timeline-play", {
                type: "button", "aria-pressed": "false",
            });
            this.playButton.addEventListener("click", () => this.toggle());

            const slider = el("div", "viewer-timeline-slider", {
                role: "slider", tabindex: "0", "aria-label": "Frame",
                "aria-valuemin": "1", "aria-valuemax": String(this.frames),
                "aria-orientation": "horizontal",
            });
            const track = el("div", "viewer-timeline-track");
            this.aheadBar = el("div", "viewer-timeline-ahead");
            this.fill = el("div", "viewer-timeline-fill");
            this.hoverMark = el("div", "viewer-timeline-hover", { "aria-hidden": "true" });
            this.hoverTip = el("span", "viewer-timeline-tip");
            this.hoverMark.appendChild(this.hoverTip);
            this.thumb = el("div", "viewer-timeline-thumb");
            track.append(this.aheadBar, this.fill, this.hoverMark, this.thumb);
            slider.appendChild(track);
            this.slider = slider;
            this.track = track;
            slider.addEventListener("pointerdown", (event) => this.startDrag(event));
            slider.addEventListener("pointermove", (event) => this.hover(event));
            slider.addEventListener("pointerleave", () => this.endHover());
            slider.addEventListener("keydown", (event) => this.sliderKey(event));

            const readout = el("div", "viewer-timeline-readout");
            this.frameText = el("span", "viewer-timeline-frame");
            this.timeText = el("span", "viewer-timeline-time");
            readout.append(this.frameText, this.timeText);

            const rates = el("div", "viewer-timeline-rates");
            this.rateButton = el("button", "viewer-timeline-rate", {
                type: "button", "aria-haspopup": "dialog", "aria-expanded": "false",
            });
            this.rateButton.addEventListener("click", (event) => {
                event.stopPropagation();
                this.toggleRates();
            });
            this.ratePanel = this.buildRates();
            rates.append(this.rateButton, this.ratePanel);
            root.append(grip, this.playButton, slider, readout, rates);
            return root;
        }

        buildRates() {
            const panel = el("div", "viewer-timeline-popover", {
                role: "dialog", "aria-label": "Playback",
            });
            panel.hidden = true;
            panel.appendChild(el("div", "viewer-timeline-popover-title", { text: "Frames per second" }));
            const grid = el("div", "viewer-timeline-rate-grid", { role: "group",
                                                                  "aria-label": "Frames per second" });
            this.rateChoices = RATES.map((fps) => {
                const button = el("button", "viewer-timeline-rate-choice", {
                    type: "button", "aria-pressed": "false", "data-fps": String(fps), text: String(fps),
                });
                button.addEventListener("click", () => {
                    this.setPlayback({ fps });
                    this.closeRates();
                });
                grid.appendChild(button);
                return button;
            });
            panel.appendChild(grid);
            this.loopSwitch = el("button", "viewer-timeline-loop", {
                type: "button", role: "switch", "aria-checked": "true",
            });
            this.loopSwitch.append(el("span", "viewer-timeline-loop-label", { text: "Loop" }),
                                   el("span", "viewer-timeline-switch", { "aria-hidden": "true" }));
            this.loopSwitch.addEventListener("click", () => {
                this.setPlayback({ loop: !this.playback().loop });
            });
            panel.appendChild(this.loopSwitch);
            panel.addEventListener("keydown", (event) => {
                if (event.key === "Escape") {
                    event.stopPropagation();
                    this.closeRates();
                    this.rateButton.focus?.();
                }
            });
            return panel;
        }

        setPlayback(patch) {
            if (this.prefs?.set) this.prefs.set({ playback: patch });
            else Object.assign(DEFAULT_PLAYBACK, patch);
            if (this.playing) this.prefetch?.lead?.({ rate: this.playback().fps,
                                                      loop: this.playback().loop });
            this.paint();
        }

        toggleRates() {
            if (this.ratePanel.hidden) this.openRates();
            else this.closeRates();
        }

        openRates() {
            this.ratePanel.hidden = false;
            this.rateButton.setAttribute("aria-expanded", "true");
            this.paint();
            this._outside = (event) => {
                if (!this.root?.querySelector(".viewer-timeline-rates")?.contains(event.target)) {
                    this.closeRates();
                }
            };
            document.addEventListener("pointerdown", this._outside, true);
        }

        closeRates() {
            if (!this.ratePanel || this.ratePanel.hidden) return;
            this.ratePanel.hidden = true;
            this.rateButton.setAttribute("aria-expanded", "false");
            document.removeEventListener("pointerdown", this._outside, true);
        }

        // -- painting --------------------------------------------------------

        fraction(t) {
            return clamp(t / Math.max(1, this.frames - 1), 0, 1);
        }

        frameLabel(t) {
            const time = elapsed(t, this.viewState.info?.spacing?.t);
            return { frame: `${t + 1} / ${this.frames}`, time };
        }

        paint() {
            if (!this.root) return;
            const state = this.viewState.get();
            const t = state.t || 0;
            const { frame, time } = this.frameLabel(t);
            if (this.drag === null) this.place(this.fraction(t));
            this.frameText.textContent = frame;
            this.timeText.textContent = time;
            this.timeText.hidden = !time;
            this.slider.setAttribute("aria-valuenow", String(t + 1));
            this.slider.setAttribute("aria-valuetext",
                                     `Frame ${t + 1} of ${this.frames}${time ? `, ${time}` : ""}`);
            const playback = this.playback();
            for (const button of this.rateChoices || []) {
                const on = Number(button.dataset.fps) === playback.fps;
                button.classList.toggle("is-active", on);
                button.setAttribute("aria-pressed", on ? "true" : "false");
            }
            this.loopSwitch?.setAttribute("aria-checked", playback.loop ? "true" : "false");
            this.paintPlay();
            this.paintRate();
            this.paintWaiting();
            this.paintAhead();
        }

        place(fraction) {
            const pct = `${fraction * 100}%`;
            this.thumb.style.left = pct;
            this.fill.style.width = pct;
            this.aheadBar.style.left = pct;
        }

        paintPlay() {
            const on = this.playing;
            const label = on ? "Pause (Shift+Space)" : "Play the frames (Shift+Space)";
            this.playButton.setAttribute("aria-pressed", on ? "true" : "false");
            this.playButton.setAttribute("aria-label", on ? "Pause" : "Play");
            this.playButton.title = label;
            const glyph = on ? "pause" : "play";
            if (this.playButton._glyph !== glyph) {
                this.playButton.replaceChildren(icon(glyph));
                this.playButton._glyph = glyph;
            }
            this.root.classList.toggle("is-playing", on);
        }

        /** The chip: the rate asked for, and while playing slower than that
         *  for a while, what it is achieving. */
        paintRate() {
            const fps = this.playback().fps;
            const slow = this.playing && this.slowSince !== null
                && now() - this.slowSince >= SLOW_AFTER_MS && this.achieved > 0;
            const shown = slow ? Math.max(1, Math.round(this.achieved)) : null;
            const narrow = this.root.classList.contains("is-narrow");
            const text = narrow ? (slow ? `${shown}/${fps}` : `${fps}`)
                : (slow ? `${shown} of ${fps} fps` : `${fps} fps`);
            if (this.rateButton.textContent !== text) this.rateButton.textContent = text;
            this.rateButton.classList.toggle("is-slow", slow);
            const words = slow
                ? `Playing ${shown} frames a second of the ${fps} asked for: the frames are arriving slower`
                : `Playback: ${fps} frames a second, loop ${this.playback().loop ? "on" : "off"}`;
            this.rateButton.title = words;
            this.rateButton.setAttribute("aria-label", words);
        }

        /** A hollow, breathing thumb while the frame asked for is on its way. */
        paintWaiting() {
            const vs = this.viewState;
            const wanted = vs.spec();
            const pending = wanted !== vs.appliedSpec();
            // Timed from when THIS frame was asked for: the next one asked for
            // the moment the last landed has not been waiting since then.
            if (!pending) {
                this.waitingSince = null;
                this.waitingFor = null;
            } else if (this.waitingFor !== wanted) {
                this.waitingFor = wanted;
                this.waitingSince = now();
            }
            const waiting = pending && now() - this.waitingSince >= WAIT_AFTER_MS;
            this.root.classList.toggle("is-waiting", waiting);
        }

        /** How many frames after this one are loaded and ready to show. */
        readyAhead() {
            const standby = this.viewerManager?.standby;
            if (typeof standby?.get !== "function" || !standby.size) return 0;
            const vs = this.viewState;
            const state = vs.get();
            let count = 0;
            // Drawn to the end of the track only: frames a loop wraps round
            // to are ready too, but a stretch cannot say so.
            for (let k = 1; k <= AHEAD_MAX && state.t + k < this.frames; k += 1) {
                const t = state.t + k;
                const entry = standby.get(vs.spec({ ...state, t }));
                if (!entry || entry.pending) break;
                const loaded = [...entry.pairs.values()].every((pair) => (pair || []).every((part) => {
                    const item = part?.item;
                    const settled = global.PlexoraChannelFade?.settledLoaded;
                    if (!item) return true;
                    return typeof settled === "function" ? settled(item) : item.getFullyLoaded?.() !== false;
                }));
                if (!loaded || !entry.pairs.size) break;
                count = k;
            }
            return count;
        }

        paintAhead() {
            const t = this.viewState.get().t || 0;
            const ahead = this.readyAhead();
            const end = this.fraction(t + ahead) - this.fraction(t);
            this.aheadBar.style.width = `${Math.max(0, end) * 100}%`;
        }

        /** The loaded stretch fills in as frames finish loading at rest. */
        watchAhead() {
            clearInterval(this.aheadTimer);
            this.aheadTimer = setInterval(() => {
                if (!this.root || this.root.hidden || this.playing) return;
                this.paintAhead();
                this.paintWaiting();
            }, AHEAD_EVERY_MS);
        }

        fitWidth() {
            const width = this.root?.getBoundingClientRect?.().width || 0;
            const narrow = width > 0 && width < NARROW_PX;
            if (narrow === this.root.classList.contains("is-narrow")) return;
            this.root.classList.toggle("is-narrow", narrow);
            this.paintRate();
        }

        // -- Video Studio ----------------------------------------------------

        /** Step aside while Video Studio's own timeline is along the bottom. */
        followStudio() {
            const bands = global.PlexoraCanvasLayout?.get?.()?.bands;
            const studio = typeof bands?.has === "function" && bands.has(STUDIO_BAND);
            if (studio === this.hiddenByStudio || !this.root) return;
            this.hiddenByStudio = studio;
            if (studio) {
                this.stop();
                this.closeRates();
                this.endHover();
            }
            this.root.hidden = studio;
        }

        // -- the slider --------------------------------------------------------

        tExact(clientX) {
            const box = this.track.getBoundingClientRect();
            const fraction = box.width > 0 ? (clientX - box.left) / box.width : 0;
            return clamp(fraction, 0, 1) * (this.frames - 1);
        }

        tAt(clientX) {
            return Math.round(this.tExact(clientX));
        }

        /** Go to frame `t`, as a hand does (the player stops). */
        go(t) {
            const vs = this.viewState;
            const next = clamp(Math.round(t), 0, this.frames - 1);
            if (next !== vs.get().t) vs.set({ t: next });
        }

        startDrag(event) {
            if (event.button !== 0) return;
            event.preventDefault();
            event.stopPropagation();
            this.slider.focus?.({ preventScroll: true });
            this.stop();
            this.endHover();
            this.drag = { pointer: event.pointerId };
            this.root.classList.add("is-scrubbing");
            this.place(this.fraction(this.tExact(event.clientX)));
            this.go(this.tAt(event.clientX));
            const move = (e) => {
                if (this.drag?.pointer !== undefined && e.pointerId !== undefined
                        && e.pointerId !== this.drag.pointer) return;
                this.place(this.fraction(this.tExact(e.clientX)));
                this.go(this.tAt(e.clientX));
            };
            const up = () => {
                this.drag = null;
                this.root?.classList.remove("is-scrubbing");
                global.removeEventListener("pointermove", move);
                global.removeEventListener("pointerup", up);
                global.removeEventListener("pointercancel", up);
                // The hand let go: what waited for it goes now, and the
                // thumb settles onto the frame on screen.
                this.viewState.endScrub?.();
                this.paint();
            };
            global.addEventListener("pointermove", move);
            global.addEventListener("pointerup", up);
            global.addEventListener("pointercancel", up);
        }

        hover(event) {
            if (this.drag || event.pointerType === "touch") {
                this.endHover();
                return;
            }
            const t = this.tAt(event.clientX);
            const { frame, time } = this.frameLabel(t);
            this.hoverMark.style.left = `${this.fraction(t) * 100}%`;
            this.hoverTip.textContent = time ? `${frame.split(" /")[0]} · ${time}` : frame.split(" /")[0];
            this.root.classList.add("is-hovering");
            if (t === this.hoverT) return;
            this.hoverT = t;
            global.dispatchEvent?.(new CustomEvent("plexora:frame-hover", { detail: { t } }));
        }

        endHover() {
            this.root?.classList.remove("is-hovering");
            if (this.hoverT === null) return;
            this.hoverT = null;
            global.dispatchEvent?.(new CustomEvent("plexora:frame-hover", { detail: { t: null } }));
        }

        sliderKey(event) {
            const steps = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1,
                            PageUp: 10, PageDown: -10 };
            const t = this.viewState.get().t;
            if (event.key in steps) {
                event.preventDefault();
                event.stopPropagation();
                this.stop();
                this.stepKey = event.code || event.key;
                this.go(t + steps[event.key]);
            } else if (event.key === "Home" || event.key === "End") {
                event.preventDefault();
                event.stopPropagation();
                this.stop();
                this.go(event.key === "Home" ? 0 : this.frames - 1);
            }
        }

        // -- Play --------------------------------------------------------------

        toggle() {
            if (this.playing) this.stop();
            else this.play();
        }

        play() {
            if (this.playing || this.frames < 2 || this.hiddenByStudio) return;
            const vs = this.viewState;
            const playback = this.playback();
            // From the last frame without Loop, Play starts at the first.
            if (!playback.loop && vs.get().t >= this.frames - 1) {
                vs.set({ t: 0 }, { save: false, steady: true });
            }
            this.playing = true;
            this.steppedAt = now();
            this.achieved = 0;
            this.slowSince = null;
            this.prefetch?.lead?.({ rate: playback.fps, loop: playback.loop });
            this.paint();
            this.loop = raf(() => this.tick());
        }

        /** Stop Play. `persist: false` when nobody chose the frame it stopped
         *  on (the page going away). */
        stop({ persist = true } = {}) {
            if (!this.playing) return;
            this.playing = false;
            unraf(this.loop);
            this.loop = 0;
            this.slowSince = null;
            this.prefetch?.lead?.(null);
            // The frame playback stopped on is the one to reopen on.
            if (persist) this.viewState.persist?.();
            if (this.root) this.paint();
        }

        /**
         * One look at the clock: the next frame is asked for once the frame
         * asked for is on screen AND the rate allows it -- never before, so
         * no frame is skipped and no request piles up behind a slow one.
         */
        tick() {
            if (!this.playing) return;
            const vs = this.viewState;
            const playback = this.playback();
            const interval = 1000 / playback.fps;
            const at = now();
            const landed = vs.spec() === vs.appliedSpec() && !vs.busy?.();
            if (landed && at - this.steppedAt >= interval) {
                const state = vs.get();
                let next = state.t + 1;
                if (next >= this.frames) {
                    if (!playback.loop) {
                        this.stop();
                        return;
                    }
                    next = 0;
                }
                const gap = at - this.steppedAt;
                const pace = 1000 / gap;
                this.achieved = this.achieved ? this.achieved * 0.7 + pace * 0.3 : pace;
                if (this.achieved < playback.fps * SLOW_SHARE) {
                    if (this.slowSince === null) this.slowSince = at;
                } else {
                    this.slowSince = null;
                }
                // On the beat when it is keeping up (a frame's lateness is not
                // carried into the next), from now when it is not.
                this.steppedAt = gap < interval * 2 ? this.steppedAt + interval : at;
                vs.set({ t: next }, { save: false, steady: true });
            }
            this.paintRate();
            this.paintWaiting();
            this.paintAhead();
            this.loop = raf(() => this.tick());
        }

        // -- keys --------------------------------------------------------------

        onKey(event) {
            if (event.defaultPrevented || isTyping() || document.querySelector("dialog[open]")) return;
            if (event.metaKey || event.ctrlKey || this.hiddenByStudio || !this.root) return;
            if ((event.code === "Period" || event.code === "Comma") && !event.altKey) {
                event.preventDefault();
                this.stop();
                this.stepKey = event.code;
                this.viewState.stepTime(event.code === "Period" ? 1 : -1);
            } else if (event.code === "Space" && event.shiftKey && !event.altKey && !event.repeat) {
                // ROI's Space is held to pan; Shift with it is still its own.
                if (global.PlexoraToolLoader?.activeTool?.() === "roi") return;
                event.preventDefault();
                this.toggle();
            }
        }

        /** A step key let go: the hand stopped (viewState.endScrub). */
        onKeyUp(event) {
            const code = event?.code || event?.key;
            if (!this.stepKey || code !== this.stepKey) return;
            this.stepKey = null;
            this.viewState.endScrub?.();
        }
    }

    /** Build the timeline for a time series; null for anything else. */
    function attach(viewState, options = {}) {
        if (!viewState?.enabled?.() || viewState.frames < 2) return null;
        const timeline = new Timeline(viewState, options);
        return timeline.mount() ? timeline : null;
    }

    global.PlexoraTimeline = { attach, Timeline, elapsed, RATES };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraTimeline;
    }
})(typeof window !== "undefined" ? window : globalThis);
