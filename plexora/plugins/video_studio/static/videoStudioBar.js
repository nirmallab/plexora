/**
 * videoStudioBar.js - the Quick Capture bar over the image.
 *
 * Docked at the top centre of the canvas layout (services/canvasLayout.js:
 * `data-canvas-dock="top-center"`), so it never sits on the dataset name, the
 * legend, the depth strip or another plugin's dock. Two rows:
 *
 *   the pill    the preset menu, Start / Play / Repeat, the time, Export…,
 *               More (path editor, timeline, save, send frame to Figure
 *               Builder, back to my view), Hide settings, Close
 *   the strip   the preset's three to six controls, the scrub rail, a hint
 *
 * "Hide settings" folds the strip (remembered); a canvas too small for the
 * whole bar (`plexora:canvas-collapse`, or narrower than 360 px) folds it to a
 * compact pill with only Play, the time and Close.
 *
 * The bar draws and reports; videoStudioSidebarController.js decides.
 *
 * Classic script: `window.PlexoraVideoStudioBar`.
 */
(function (global) {
    "use strict";

    const STRIP_KEY = "plexora.video_studio.stripOpen";

    function el(tag, className, attrs = {}) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        for (const [key, value] of Object.entries(attrs)) {
            if (value === undefined || value === null || value === false) continue;
            if (key === "text") node.textContent = value;
            else if (key === "html") node.innerHTML = value;
            else node.setAttribute(key, value === true ? "" : String(value));
        }
        return node;
    }

    function icon(name) {
        return el("span", `fas fa-${name}`, { "aria-hidden": "true" });
    }

    /** Swap the icon at the front of `host` for `name`. Font Awesome replaces
     *  each icon span with an <svg> (whose class cannot be assigned), so the
     *  element is replaced rather than re-classed. */
    function setIcon(host, name) {
        const current = host.firstElementChild;
        const next = icon(name);
        if (current) current.replaceWith(next);
        else host.prepend(next);
        return next;
    }

    function iconButton(role, iconName, label, shortcut) {
        const button = el("button", "vs-icon-button", {
            type: "button", "data-role": role, "aria-label": label,
            title: shortcut ? `${label} (${shortcut})` : label,
        });
        button.appendChild(icon(iconName));
        return button;
    }

    function readStrip() {
        try {
            return global.localStorage?.getItem(STRIP_KEY) !== "0";
        } catch (error) {
            return true;
        }
    }

    function writeStrip(open) {
        try {
            global.localStorage?.setItem(STRIP_KEY, open ? "1" : "0");
        } catch (error) {
            // a private window keeps the strip as it is for this page only
        }
    }

    function formatTime(t) {
        const s = Math.max(0, t);
        const minutes = Math.floor(s / 60);
        return `${minutes}:${(s - minutes * 60).toFixed(1).padStart(4, "0")}`;
    }

    class VideoStudioBar {
        constructor(handlers = {}) {
            this.handlers = handlers;
            this.root = null;
            this.stripOpen = readStrip();
            this.compact = false;
            this.duration = 0;
            this.time = 0;
        }

        mount(wrapper) {
            if (this.root) return this.root;
            const root = el("div", "vs-dock", {
                "data-canvas-dock": "top-center", "data-canvas-order": "10",
                "data-viewer-furniture": true, "data-canvas-collapsible": true,
                role: "region", "aria-label": "Video Studio",
            });
            const pill = el("div", "vs-bar", { role: "toolbar", "aria-label": "Video Studio" });
            const orb = el("span", "vs-orb", { "aria-hidden": "true" });
            orb.appendChild(icon("video"));
            this.presetButton = el("button", "vs-preset", { type: "button", "aria-haspopup": "menu",
                                                            "data-role": "preset", title: "Choose a preset" });
            this.presetIcon = icon("rotate");
            this.presetLabel = el("span", "vs-preset-label", { text: "Video" });
            this.presetButton.append(this.presetIcon, this.presetLabel, icon("chevron-down"));
            this.startButton = iconButton("start", "backward-step", "Start", "Home");
            this.playButton = iconButton("play", "play", "Play", "Space");
            this.loopButton = iconButton("loop", "repeat", "Repeat playback", "L");
            this.loopButton.setAttribute("aria-pressed", "true");
            this.timeLabel = el("span", "vs-time", { "aria-live": "off", text: "0:00.0 / 0:00.0" });
            this.exportButton = el("button", "vs-button vs-primary vs-export", {
                type: "button", "data-role": "export", title: "Export (E)" });
            this.exportButton.append(icon("download"), el("span", null, { text: "Export…" }));
            this.moreButton = iconButton("more", "ellipsis", "More");
            this.moreButton.setAttribute("aria-haspopup", "menu");
            this.foldButton = iconButton("fold", "chevron-up", "Hide settings");
            this.closeButton = iconButton("close", "xmark", "Close Video Studio");
            pill.append(orb, this.presetButton, el("span", "vs-sep", { "aria-hidden": "true" }),
                        this.startButton, this.playButton, this.loopButton, this.timeLabel,
                        el("span", "vs-sep", { "aria-hidden": "true" }), this.exportButton,
                        this.moreButton, this.foldButton, this.closeButton);

            const strip = el("div", "vs-strip");
            this.fields = el("div", "vs-fields");
            this.scrub = el("div", "vs-scrub", { role: "slider", tabindex: "0", "aria-label": "Position",
                                                "aria-valuemin": "0", "aria-valuemax": "0",
                                                "aria-valuenow": "0", "aria-valuetext": "0.0 s" });
            this.scrubFill = el("div", "vs-scrub-fill");
            this.scrubKnob = el("div", "vs-scrub-knob");
            this.scrubTicks = el("div", "vs-scrub-ticks", { "aria-hidden": "true" });
            this.scrub.append(this.scrubTicks, this.scrubFill, this.scrubKnob);
            this.hint = el("p", "vs-hint");
            this.error = el("p", "vs-error", { role: "alert" });
            this.error.hidden = true;
            strip.append(this.fields, this.scrub, this.hint, this.error);
            this.strip = strip;
            root.append(pill, strip);
            this.root = root;
            this.bind();
            this.paintStrip();
            wrapper.appendChild(root);
            return root;
        }

        bind() {
            const h = this.handlers;
            this.presetButton.addEventListener("click", () => h.onPresetMenu?.(this.presetButton));
            this.startButton.addEventListener("click", () => h.onStart?.());
            this.playButton.addEventListener("click", () => h.onPlay?.());
            this.loopButton.addEventListener("click", () => {
                const on = this.loopButton.getAttribute("aria-pressed") !== "true";
                this.loopButton.setAttribute("aria-pressed", String(on));
                h.onLoop?.(on);
            });
            this.exportButton.addEventListener("click", () => h.onExport?.());
            this.moreButton.addEventListener("click", () => h.onMore?.(this.moreButton));
            this.foldButton.addEventListener("click", () => {
                this.stripOpen = !this.stripOpen;
                writeStrip(this.stripOpen);
                this.paintStrip();
            });
            this.closeButton.addEventListener("click", () => h.onClose?.());
            this.root.addEventListener("plexora:canvas-collapse", (event) => {
                this.compact = Boolean(event.detail?.collapsed);
                this.paintStrip();
            });
            // The scrub rail: pointer drag and keyboard (arrows, Home, End).
            const fromPointer = (event) => {
                const rect = this.scrub.getBoundingClientRect();
                const fraction = Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(1, rect.width)));
                h.onSeek?.(fraction * this.duration);
            };
            this.scrub.addEventListener("pointerdown", (event) => {
                this.scrub.setPointerCapture?.(event.pointerId);
                this.dragging = true;
                h.onScrubStart?.();
                fromPointer(event);
            });
            this.scrub.addEventListener("pointermove", (event) => {
                if (this.dragging) fromPointer(event);
            });
            const end = (event) => {
                if (!this.dragging) return;
                this.dragging = false;
                this.scrub.releasePointerCapture?.(event.pointerId);
            };
            this.scrub.addEventListener("pointerup", end);
            this.scrub.addEventListener("pointercancel", end);
            this.scrub.addEventListener("keydown", (event) => {
                const step = event.shiftKey ? 1 : 1 / 30;
                if (event.key === "ArrowRight") h.onSeek?.(this.time + step);
                else if (event.key === "ArrowLeft") h.onSeek?.(this.time - step);
                else if (event.key === "Home") h.onSeek?.(0);
                else if (event.key === "End") h.onSeek?.(this.duration);
                else return;
                event.preventDefault();
                event.stopPropagation();
            });
        }

        paintStrip() {
            if (!this.root) return;
            const show = this.stripOpen && !this.compact;
            this.strip.hidden = !show;
            this.root.classList.toggle("is-compact", this.compact);
            this.foldButton.setAttribute("aria-label", this.stripOpen ? "Hide settings" : "Show settings");
            this.foldButton.title = this.foldButton.getAttribute("aria-label");
            setIcon(this.foldButton, `chevron-${this.stripOpen ? "up" : "down"}`);
            this.foldButton.setAttribute("aria-expanded", String(this.stripOpen));
        }

        setPreset(preset) {
            setIcon(this.presetButton, preset?.icon || "video");
            this.presetLabel.textContent = preset?.label || "Video";
            this.hint.textContent = preset?.summary || "";
        }

        setPlaying(playing) {
            setIcon(this.playButton, playing ? "pause" : "play");
            const label = playing ? "Pause" : "Play";
            this.playButton.setAttribute("aria-label", label);
            this.playButton.title = `${label} (Space)`;
        }

        setLoop(on) {
            this.loopButton.setAttribute("aria-pressed", String(Boolean(on)));
        }

        setTime(t, duration = this.duration) {
            this.time = t;
            this.duration = duration;
            this.timeLabel.textContent = `${formatTime(t)} / ${formatTime(duration)}`;
            const fraction = duration > 0 ? Math.max(0, Math.min(1, t / duration)) : 0;
            this.scrubFill.style.transform = `scaleX(${fraction})`;
            this.scrubKnob.style.left = `${fraction * 100}%`;
            this.scrub.setAttribute("aria-valuemax", duration.toFixed(2));
            this.scrub.setAttribute("aria-valuenow", t.toFixed(2));
            this.scrub.setAttribute("aria-valuetext", `${t.toFixed(1)} of ${duration.toFixed(1)} seconds`);
        }

        /** Keyframe and segment marks on the rail: `[{t, kind}]`. */
        setMarks(marks) {
            this.scrubTicks.replaceChildren();
            if (!(this.duration > 0)) return;
            for (const mark of marks || []) {
                const tick = el("span", `vs-tick vs-tick-${mark.kind || "key"}`);
                tick.style.left = `${Math.max(0, Math.min(1, mark.t / this.duration)) * 100}%`;
                this.scrubTicks.appendChild(tick);
            }
        }

        setHint(text) {
            this.hint.textContent = text || "";
        }

        showError(text) {
            this.error.textContent = text || "";
            this.error.hidden = !text;
        }

        setBusy(busy) {
            this.root?.classList.toggle("is-busy", Boolean(busy));
            this.exportButton.disabled = Boolean(busy);
        }

        destroy() {
            this.root?.remove();
            this.root = null;
        }
    }

    global.PlexoraVideoStudioBar = VideoStudioBar;
    global.PlexoraVideoStudioBar.formatTime = formatTime;
})(typeof window !== "undefined" ? window : globalThis);
