/**
 * surfaceStudio.js - Quick Surface: a surface in a few clicks, threshold moved live.
 *
 * A dock over the image (bottom left, services/canvasLayout.js), opened by
 * "+ Surface", the 3-D view's S key, a surface card's Edit, or the agent.
 *
 *   Source      a channel, or the 3-D mask (one surface per cell)
 *   Threshold   a slider over the channel's histogram (log counts) with the
 *               suggestions marked -- Otsu, triangle, the mixture split -- and
 *               a number box, all in the channel's RAW units: the display
 *               contrast never enters the surface's record
 *   Smoothing   none / light / medium / strong (Taubin, half a voxel at most)
 *   Min size    pieces smaller than this are dropped (µm³, or voxels³)
 *   Quality     preview / standard / full, with what that will build
 *
 * Moving the threshold asks the server for a preview (debounced, one request
 * in flight, a superseded one aborted): a coarse mesh drawn in the 3-D view
 * as a preview surface (never a layer), and the isovalue's contour, dashed,
 * on the plane on screen and in the section panes, so the boundary can be
 * judged against the voxels it is drawn from. "Create surface" builds the
 * real one, at the chosen quality, as a layer.
 *
 * Surface Studio is Paid (`surfaces`): the dock's first request is a Paid one,
 * and a refusal closes it and opens the Paid modal with the server's words.
 *
 * Classic script: `window.PlexoraSurfaceStudio`.
 */
window.PlexoraSurfaceStudio = (function () {
    "use strict";

    const DEBOUNCE_MS = 150;
    const MASK = "__mask__";
    const SMOOTHING = [
        { value: "none", label: "None" }, { value: "light", label: "Light" },
        { value: "medium", label: "Medium" }, { value: "strong", label: "Strong" },
    ];
    const QUALITY = [
        { value: "preview", label: "Preview", title: "Coarse and quick: a first look" },
        { value: "standard", label: "Standard", title: "Up to 3 M triangles: the usual choice" },
        { value: "full", label: "Full", title: "Full resolution, where it fits in the browser" },
    ];
    const MARKS = { otsu: "Otsu", triangle: "Triangle", gmm: "Mixture" };

    let root = null;
    let undock = null;
    let state = null;
    let controller = null;
    let previewTimer = 0;
    let estimateTimer = 0;
    let contourHandle = null;
    let contours = null;
    let sectionOff = null;

    function service() { return window.PlexoraSurfaceLayers || null; }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function fmt(value) {
        const n = Number(value);
        if (!Number.isFinite(n)) return "";
        const abs = Math.abs(n);
        return abs >= 100 ? String(Math.round(n)) : String(Number(n.toPrecision(4)));
    }

    /** About a thousandth of `span`, rounded to 1, 2 or 5 x 10^k: a drag
     *  lands on a number a person would write down, not on 1398.7. */
    function niceStep(span) {
        if (!(span > 0)) return 1;
        const raw = span / 1000;
        const power = 10 ** Math.floor(Math.log10(raw));
        const unit = raw / power;
        return (unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 5 ? 5 : 10) * power;
    }

    function count(n) {
        n = Number(n) || 0;
        return n >= 1e6 ? `${(n / 1e6).toFixed(1)} M` : (n >= 1e4 ? `${Math.round(n / 1e3)} k`
            : n.toLocaleString());
    }

    /** The image's channels by full name (the 3-D view's and the server's). */
    function channels() {
        const sidebar = window.__plexora?.viewerSidebar;
        const data = window.__plexora?.seaDragonViewer?.config?.imageData || [];
        const names = data.map((entry) => entry.fullname).filter((name) => name && name !== "Area");
        const on = (sidebar?.channelSlots || []).filter((slot) => slot.enabled && slot.name)
            .map((slot) => sidebar.dataLayer?.getFullChannelName?.(slot.name) || slot.name);
        // The channels on screen first: the one being looked at is the likely one.
        return [...new Set([...on.filter((name) => names.includes(name)), ...names])];
    }

    function viewState() { return window.__plexora?.viewState || null; }

    function maskIs3d() { return Boolean(viewState()?.labelsAre3d?.()); }

    function physical() {
        const spacing = viewState()?.info?.spacing || {};
        return ["x", "y", "z"].every((axis) => Number(spacing[axis]?.value) > 0)
            && ["metadata", "manual"].includes(spacing.z?.source);
    }

    function segmented(options, current, onPick, label) {
        const group = el("div", "plx-surf-seg");
        group.setAttribute("role", "radiogroup");
        group.setAttribute("aria-label", label);
        const buttons = options.map((option) => {
            const button = el("button", "plx-surf-seg-option", option.label);
            button.type = "button";
            button.dataset.value = option.value;
            button.setAttribute("role", "radio");
            if (option.title) button.title = option.title;
            button.addEventListener("click", () => {
                group.setValue(option.value);
                onPick(option.value);
            });
            group.appendChild(button);
            return button;
        });
        group.setValue = (value) => buttons.forEach((b) => b.setAttribute("aria-checked",
                                                                           String(b.dataset.value === value)));
        group.setValue(current);
        return group;
    }

    function field(label, control, unit) {
        const row = el("label", "plx-surf-studio-field");
        row.appendChild(el("span", "plx-surf-studio-label", label));
        row.appendChild(control);
        if (unit) row.appendChild(el("span", "plx-surf-studio-unit", unit));
        return row;
    }

    function numberBox(value, { min = 0, step = "any", placeholder = "" } = {}) {
        const input = el("input", "plx-number plx-surf-number");
        input.type = "number";
        input.min = String(min);
        input.step = String(step);
        if (placeholder) input.placeholder = placeholder;
        if (value !== null && value !== undefined) input.value = String(value);
        return input;
    }

    // -- the histogram ------------------------------------------------------------

    /** Where the histogram's value axis starts and ends inside the canvas: half
     *  a thumb in from each side, where the slider's thumb stops. */
    const HIST_PAD = (window.PlexoraSlider?.THUMB || 12) / 2;

    /** The values at the histogram's two edges: the threshold track's. */
    function histogramSpan(hist) {
        const slider = state?.ui?.isoSlider;
        const left = Number.isFinite(slider?.min) && slider.min <= hist.lo ? slider.min : hist.lo;
        return [left, hist.hi];
    }

    function drawHistogram() {
        const canvas = state?.ui?.histogram;
        const hist = state?.suggestion?.histogram;
        if (!canvas) return;
        const ratio = window.devicePixelRatio || 1;
        const width = canvas.clientWidth || 280;
        const height = canvas.clientHeight || 56;
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(height * ratio);
        const ctx = canvas.getContext("2d");
        if (!ctx) return;
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        ctx.clearRect(0, 0, width, height);
        if (!hist) return;
        const counts = hist.counts || [];
        const top = Math.max(1, ...counts.map((c) => Math.log1p(c)));
        // On the slider's scale (its `min` is the histogram's floor rounded
        // down onto its step), so a value's line sits over the thumb.
        const [left, right] = histogramSpan(hist);
        // Inset by half a thumb, as the track's thumb travel is.
        const pad = HIST_PAD;
        const x = (value) => pad + ((value - left) / Math.max(1e-9, right - left)) * (width - 2 * pad);
        const bar = (x(hist.hi) - x(hist.lo)) / Math.max(1, counts.length);
        const start = x(hist.lo);
        const at = state.iso;
        ctx.fillStyle = "rgba(142, 160, 184, 0.45)";
        counts.forEach((c, i) => {
            const h = (Math.log1p(c) / top) * (height - 10);
            ctx.fillRect(start + i * bar, height - h, Math.max(1, bar - 0.4), h);
        });
        // Above the threshold: what will be inside the surface.
        if (Number.isFinite(at)) {
            ctx.save();
            ctx.beginPath();
            ctx.rect(x(at), 0, width - x(at), height);
            ctx.clip();
            ctx.fillStyle = "rgba(56, 189, 248, 0.55)";
            counts.forEach((c, i) => {
                const h = (Math.log1p(c) / top) * (height - 10);
                ctx.fillRect(start + i * bar, height - h, Math.max(1, bar - 0.4), h);
            });
            ctx.restore();
        }
        ctx.font = "9px system-ui, sans-serif";
        ctx.textAlign = "center";
        for (const [name, value] of Object.entries(state.suggestion.thresholds || {})) {
            const px = x(value);
            if (!(px >= 0 && px <= width)) continue;
            ctx.strokeStyle = "rgba(248, 250, 252, 0.45)";
            ctx.setLineDash([2, 2]);
            ctx.beginPath();
            ctx.moveTo(px + 0.5, 9);
            ctx.lineTo(px + 0.5, height);
            ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillStyle = "rgba(216, 225, 237, 0.8)";
            ctx.fillText((MARKS[name] || name).slice(0, 1), px, 8);
        }
        if (Number.isFinite(at)) {
            ctx.strokeStyle = "#38bdf8";
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.moveTo(x(at), 0);
            ctx.lineTo(x(at), height);
            ctx.stroke();
        }
    }

    // -- the preview --------------------------------------------------------------

    function previewParams() {
        const vs = viewState()?.get?.() || {};
        const sections = window.__plexora?.sectionPanes;
        const params = {
            channel: state.channel, t: vs.t || 0, iso: state.iso,
            min_size: state.minSize || undefined,
            sigma_um: state.sigma || undefined, background_um: state.background || undefined,
            z: vs.z,
        };
        const frame = sections?.frame;
        if (frame && Number.isFinite(frame.cx)) {
            params.x = Math.round(frame.cx);
            params.y = Math.round(frame.cy);
        }
        return params;
    }

    function schedulePreview() {
        clearTimeout(previewTimer);
        if (state?.source === MASK || !Number.isFinite(state?.iso)) return;
        previewTimer = setTimeout(runPreview, DEBOUNCE_MS);
    }

    async function runPreview() {
        if (!state) return;
        controller?.abort();
        controller = typeof AbortController === "function" ? new AbortController() : null;
        const asked = state.iso;
        setBusy(true);
        try {
            const header = await service().preview(previewParams(), { signal: controller?.signal });
            if (!header) return close();         // the licence refused: close, quietly
            if (!state || state.iso !== asked) return;
            const pieces = header.report?.pieces_kept ?? (header.empty ? 0 : null);
            const triangles = header.triangles || 0;
            state.ui.status.textContent = header.empty
                ? `Nothing above ${fmt(asked)} here`
                : `Preview · level ${header.level} · ${count(triangles)} triangles`
                  + (pieces !== null ? ` · ${count(pieces)} piece${pieces === 1 ? "" : "s"}` : "");
            showContours(header.contours || null);
        } catch (error) {
            if (error?.name === "AbortError") return;
            state.ui.status.textContent = error.message || "The preview failed";
        } finally {
            if (state && state.iso === asked) setBusy(false);
        }
    }

    function setBusy(on) {
        if (state?.ui?.root) state.ui.root.setAttribute("aria-busy", on ? "true" : "false");
    }

    /** The isovalue's contour on the plane (and in the section panes). */
    function showContours(next) {
        contours = next;
        const viewer = window.__plexora?.seaDragonViewer;
        if (!contourHandle && viewer?.addOverlay) {
            contourHandle = viewer.addOverlay({
                id: "surface-studio:contours",
                kind: "shapes",
                draw: ({ context, px }) => {
                    const vs = viewState()?.get?.();
                    const xy = contours?.xy;
                    if (!xy || !context || (vs && vs.mode === "volume")) return;
                    if (vs && vs.mode === "plane" && Number(xy.z) !== Number(vs.z)) return;
                    context.strokeStyle = "#38bdf8";
                    context.lineWidth = 1.6 * px;
                    context.setLineDash([5 * px, 3 * px]);
                    for (const line of xy.lines || []) {
                        context.beginPath();
                        for (let i = 0; i < line.length; i += 2) {
                            if (i === 0) context.moveTo(line[i], line[i + 1]);
                            else context.lineTo(line[i], line[i + 1]);
                        }
                        context.stroke();
                    }
                    context.setLineDash([]);
                },
            });
        }
        contourHandle?.invalidate?.();
        const panes = window.__plexora?.sectionPanes;
        if (panes?.addOverlay && !sectionOff) {
            sectionOff = panes.addOverlay("surface-studio", ({ axis }) => {
                const part = contours?.[axis];
                return part ? [{ color: "#38bdf8", lines: part.lines || [] }] : [];
            });
        }
        panes?.paintOverlays?.();
    }

    function clearPreview() {
        clearTimeout(previewTimer);
        controller?.abort();
        controller = null;
        contours = null;
        contourHandle?.remove?.();
        contourHandle = null;
        sectionOff?.();
        sectionOff = null;
        service()?.clearPreview();
    }

    // -- the estimate ------------------------------------------------------------

    function body() {
        if (state.source === MASK) {
            const out = { type: "label_surfaces", smoothing: state.smoothing, quality: state.quality,
                          t: viewState()?.get?.()?.t || 0 };
            if (state.ids?.length) out.ids = state.ids;
            if (Number.isFinite(state.inset)) out.inset = state.inset;
            if (state.label) out.label = state.label;
            return out;
        }
        const out = {
            type: "isosurface", channel: state.channel, iso: state.iso,
            t: viewState()?.get?.()?.t || 0, smoothing: state.smoothing, quality: state.quality,
            min_size: state.minSize || 0,
        };
        if (state.sigma || state.background) {
            out.preprocess = { sigma_um: state.sigma || 0, background_um: state.background || 0 };
        }
        const region = regionNow();
        if (region) out.region = region;
        if (state.label) out.label = state.label;
        out.render = { color: state.colour };
        return out;
    }

    /** The region the dock's choice names, in level-0 pixels and planes. */
    function regionNow() {
        if (state.region === "screen") {
            const bounds = window.__plexora?.seaDragonViewer?.viewportImageBounds?.();
            if (bounds) {
                return { box: [Math.floor(bounds.minX), Math.floor(bounds.minY),
                               Math.ceil(bounds.maxX - bounds.minX), Math.ceil(bounds.maxY - bounds.minY)] };
            }
        }
        if (state.region === "slab") {
            const slab = viewState()?.get?.()?.slab;
            if (slab) return { z: [slab.z0, slab.z1] };
        }
        return null;
    }

    function scheduleEstimate() {
        clearTimeout(estimateTimer);
        estimateTimer = setTimeout(async () => {
            if (!state) return;
            const line = state.ui.estimate;
            try {
                const answer = await service().estimate(body());
                if (!answer || !state) return;
                if (answer.plan) {
                    line.textContent = `Level ${answer.plan.level} · ${count(answer.plan.voxels)} voxels read`;
                } else if (answer.objects !== undefined) {
                    line.textContent = `${count(answer.objects)} cells · about ${count(answer.triangles)} `
                        + `triangles · level ${answer.level}`;
                } else {
                    line.textContent = "";
                }
                line.classList.remove("is-bad");
                state.ui.create.disabled = false;
            } catch (error) {
                line.textContent = error.message;
                line.classList.add("is-bad");
                state.ui.create.disabled = true;
            }
        }, 250);
    }

    // -- building the dock ---------------------------------------------------------

    async function loadSuggestion() {
        if (state.source === MASK) return true;
        state.ui.status.textContent = "Reading the channel…";
        setBusy(true);
        try {
            const answer = await service().suggest({ channel: state.channel,
                                                     t: viewState()?.get?.()?.t || 0,
                                                     sigma_um: state.sigma || undefined,
                                                     background_um: state.background || undefined });
            if (!answer) return false;
            state.suggestion = answer;
            const hist = answer.histogram;
            if (hist) {
                const top = Number(answer.range?.[1]);
                const step = niceStep(hist.hi - hist.lo);
                // The grid is measured from `min`, so `min` sits on it too.
                state.ui.isoSlider.setBounds({
                    min: PlexoraSlider.tidy(Math.floor(hist.lo / step) * step),
                    max: hist.hi, step,
                    fieldMax: Number.isFinite(top) && top > hist.hi ? top : null,
                });
            }
            if (!Number.isFinite(state.iso)) state.iso = answer.default;
            paintMarks();
            setIso(state.iso, { preview: true });
            return true;
        } catch (error) {
            state.ui.status.textContent = error.message;
            return true;
        } finally {
            setBusy(false);
        }
    }

    function paintMarks() {
        const marks = state.ui.marks;
        marks.textContent = "";
        for (const [name, value] of Object.entries(state.suggestion?.thresholds || {})) {
            const chip = el("button", "plx-surf-chip", `${MARKS[name] || name} ${fmt(value)}`);
            chip.type = "button";
            chip.title = `Use the ${MARKS[name] || name} threshold`;
            chip.addEventListener("click", () => setIso(value, { preview: true }));
            marks.appendChild(chip);
        }
    }

    function setIso(value, { preview = false } = {}) {
        const n = Number(value);
        if (!Number.isFinite(n)) return;
        state.iso = n;
        // Silent: the slider is told, not asked; its own box keeps a typed
        // entry under way (numberField.set waits for the commit).
        state.ui.isoSlider.set(n, { silent: true });
        drawHistogram();
        if (preview) schedulePreview();
        scheduleEstimate();
    }

    function setSource(source) {
        state.source = source;
        const mask = source === MASK;
        state.channel = mask ? null : source;
        state.ui.threshold.hidden = mask;
        state.ui.minRow.hidden = mask;
        state.ui.maskOptions.hidden = !mask;
        state.ui.isoAdvanced.hidden = mask;
        if (mask) {
            clearPreview();
            state.smoothing = state.smoothing === "light" ? "medium" : state.smoothing;
            state.ui.smoothing.setValue(state.smoothing);
            state.ui.status.textContent = "One surface per cell of the 3-D mask, under its id.";
        } else {
            state.suggestion = null;
            state.iso = state.from?.channel === source ? Number(state.from.iso) : NaN;
            loadSuggestion();
        }
        scheduleEstimate();
    }

    function build() {
        const node = el("section", "plx-surf-studio");
        node.setAttribute("role", "dialog");
        node.setAttribute("aria-label", "Surface Studio");
        node.dataset.canvasCollapsible = "";

        const head = el("header", "plx-surf-studio-head");
        const mark = el("i", "fas fa-cubes plx-surf-studio-mark");
        mark.setAttribute("aria-hidden", "true");
        head.append(mark, el("h2", "plx-surf-studio-title", "Surface Studio"));
        const close_ = el("button", "plx-surf-icon-button");
        close_.type = "button";
        close_.title = "Close (S)";
        close_.setAttribute("aria-label", "Close Surface Studio");
        close_.innerHTML = '<i class="fas fa-xmark" aria-hidden="true"></i>';
        close_.addEventListener("click", close);
        head.appendChild(close_);
        node.appendChild(head);

        const ui = { root: node };
        // Source
        const source = el("select", "plx-surf-select plx-surf-studio-source");
        source.setAttribute("aria-label", "What to make a surface of");
        for (const name of channels()) {
            const option = el("option", null, name);
            option.value = name;
            source.appendChild(option);
        }
        if (maskIs3d()) {
            const option = el("option", null, "Cells of the 3-D mask");
            option.value = MASK;
            source.appendChild(option);
        }
        source.addEventListener("change", () => setSource(source.value));
        ui.source = source;
        node.appendChild(field("Source", source));

        // Threshold
        const threshold = el("div", "plx-surf-studio-threshold");
        const histogram = el("canvas", "plx-surf-histogram");
        histogram.setAttribute("aria-hidden", "true");
        const fromCanvas = (event) => {
            const hist = state?.suggestion?.histogram;
            if (!hist) return;
            const box = histogram.getBoundingClientRect();
            const f = Math.min(1, Math.max(0, (event.clientX - box.left - HIST_PAD)
                                              / Math.max(1, box.width - 2 * HIST_PAD)));
            const [left, right] = histogramSpan(hist);
            const slider = state.ui.isoSlider;
            // A point on the histogram is a point on the track: on its grid.
            setIso(PlexoraSlider.snap(left + f * (right - left), slider.min, slider.max, slider.step),
                   { preview: true });
        };
        histogram.addEventListener("pointerdown", (event) => {
            histogram.setPointerCapture?.(event.pointerId);
            fromCanvas(event);
            const move = (e) => fromCanvas(e);
            const up = () => {
                histogram.removeEventListener("pointermove", move);
                histogram.removeEventListener("pointerup", up);
            };
            histogram.addEventListener("pointermove", move);
            histogram.addEventListener("pointerup", up);
        });
        // The app's slider, with its typeable box: a threshold is often a
        // number read off a paper. Bounds arrive with the suggestion; the box
        // may go past the track's top (the 99.99th percentile) to the maximum.
        // Its box sits in the row above, so the track is as wide as the
        // histogram and a value's line is drawn right over the thumb.
        const boxSlot = el("span", "plx-surf-studio-iso");
        const isoSlider = new PlexoraSlider(null, {
            min: 0, max: 1, step: "any", value: 0,
            ariaLabel: "Threshold, raw intensity",
            className: "plx-surf-studio-slider",
            accent: "var(--accent-channel)",
            fieldsSlot: boxSlot,
            format: fmt,
            onInput: (value) => setIso(value, { preview: true }),
        });
        threshold.appendChild(field("Threshold", boxSlot, "raw"));
        threshold.appendChild(histogram);
        threshold.appendChild(isoSlider.el);
        const marks = el("div", "plx-surf-marks");
        threshold.appendChild(marks);
        node.appendChild(threshold);
        Object.assign(ui, { histogram, isoSlider, slider: isoSlider.nodes.inputs.high,
                            number: isoSlider.nodes.fields.high?.input || null,
                            marks, threshold });

        // Smoothing, min size, quality
        const smoothing = segmented(SMOOTHING, "light", (value) => {
            state.smoothing = value;
            scheduleEstimate();
        }, "Smoothing");
        ui.smoothing = smoothing;
        node.appendChild(field("Smoothing", smoothing));
        const minSize = numberBox(null, { min: 0, placeholder: "0" });
        minSize.addEventListener("change", () => {
            state.minSize = Math.max(0, Number(minSize.value) || 0);
            schedulePreview();
        });
        const minRow = field("Min size", minSize, physical() ? "µm³" : "voxels³");
        ui.minRow = minRow;
        node.appendChild(minRow);
        const quality = segmented(QUALITY, "standard", (value) => {
            state.quality = value;
            scheduleEstimate();
        }, "Quality");
        node.appendChild(field("Quality", quality));
        const estimate = el("p", "plx-surf-studio-estimate");
        ui.estimate = estimate;
        node.appendChild(estimate);

        // Cells of the mask
        const maskOptions = el("div", "plx-surf-studio-mask");
        const ids = el("input", "plx-surf-text");
        ids.type = "text";
        ids.placeholder = "All cells, or ids: 12, 40, 77";
        ids.setAttribute("aria-label", "Only these cell ids");
        ids.addEventListener("change", () => {
            const parsed = String(ids.value).split(/[\s,;]+/).map(Number)
                .filter((v) => Number.isInteger(v) && v > 0);
            state.ids = parsed.length ? parsed : null;
            scheduleEstimate();
        });
        maskOptions.appendChild(field("Cells", ids));
        maskOptions.hidden = true;
        ui.maskOptions = maskOptions;
        node.appendChild(maskOptions);

        // Advanced
        const advanced = el("details", "plx-surf-studio-advanced");
        advanced.appendChild(el("summary", null, "Advanced"));
        const isoAdvanced = el("div");
        const sigma = numberBox(null, { min: 0, placeholder: "0" });
        sigma.addEventListener("change", () => {
            state.sigma = Math.max(0, Number(sigma.value) || 0);
            state.suggestion = null;
            loadSuggestion();
        });
        isoAdvanced.appendChild(field("Blur σ", sigma, "µm"));
        const background = numberBox(null, { min: 0, placeholder: "off" });
        background.title = "Subtract the local background: the diameter of the objects to keep";
        background.addEventListener("change", () => {
            state.background = Math.max(0, Number(background.value) || 0);
            state.suggestion = null;
            loadSuggestion();
        });
        isoAdvanced.appendChild(field("Background", background, "µm object"));
        const region = el("select", "plx-surf-select");
        for (const [value, label] of [["whole", "Whole volume"], ["screen", "Area on screen"],
                                      ["slab", "Planes of the slab band"]]) {
            const option = el("option", null, label);
            option.value = value;
            region.appendChild(option);
        }
        region.addEventListener("change", () => {
            state.region = region.value;
            scheduleEstimate();
        });
        isoAdvanced.appendChild(field("Region", region));
        const colour = el("input", "plx-surf-colour");
        colour.type = "color";
        colour.value = "#d8b45a";
        colour.addEventListener("input", () => {
            state.colour = colour.value;
        });
        isoAdvanced.appendChild(field("Colour", colour));
        ui.isoAdvanced = isoAdvanced;
        advanced.appendChild(isoAdvanced);
        const label = el("input", "plx-surf-text");
        label.type = "text";
        label.placeholder = "Named after what it is";
        label.addEventListener("change", () => {
            state.label = String(label.value).trim() || null;
        });
        advanced.appendChild(field("Name", label));
        node.appendChild(advanced);

        // Status and create
        const foot = el("footer", "plx-surf-studio-foot");
        const status = el("p", "plx-surf-studio-status");
        status.setAttribute("aria-live", "polite");
        const create = el("button", "plx-surf-create", "Create surface");
        create.type = "button";
        create.addEventListener("click", submit);
        foot.append(status, create);
        node.appendChild(foot);
        Object.assign(ui, { status, create });
        return { node, ui };
    }

    async function submit() {
        if (!state) return;
        const request = body();
        state.ui.create.disabled = true;
        state.ui.status.textContent = "Starting the build…";
        try {
            const answer = await service().create(request);
            if (!answer) return close();
            window.PlexoraToast?.show?.({
                title: answer.created ? "Building the surface: its card shows the progress."
                    : "That surface exists already; it is in the Layers panel.",
                tone: "info",
            });
            close();
        } catch (error) {
            state.ui.status.textContent = error.message;
            state.ui.create.disabled = false;
        }
    }

    // -- open / close ------------------------------------------------------------------

    /**
     * Open the dock. `from` is a surface's request (Edit), `channel` a channel to
     * start on, `source: "mask"` the 3-D mask. Resolves false, and opens
     * nothing, without Plexora 3D or when the licence refused.
     */
    async function open({ from = null, channel = null, source = null, ids = null } = {}) {
        // Plexora 3D: without it Surface Studio does not open at all -- the
        // S key and every menu entry included -- and says nothing.
        if (!window.PlexoraPaid?.allows?.("volume:surfaces")) return false;
        if (!viewState()?.enabled?.()) {
            window.PlexoraToast?.show?.({ title: "Surfaces are made from a z-stack; this image has one plane.",
                                          tone: "warning" });
            return false;
        }
        if (root) close();
        const built = build();
        root = built.node;
        state = { ui: built.ui, from, iso: NaN, smoothing: from?.smoothing || "light",
                  quality: from?.quality || "standard", minSize: Number(from?.min_size) || 0,
                  sigma: Number(from?.preprocess?.sigma_um) || 0,
                  background: Number(from?.preprocess?.background_um) || 0,
                  region: "whole", colour: "#d8b45a", ids: from?.ids || ids || null,
                  inset: Number.isFinite(Number(from?.inset)) ? Number(from.inset) : NaN,
                  label: null, source: null, channel: null, suggestion: null };
        built.ui.smoothing.setValue(state.smoothing);
        undock = window.PlexoraCanvasLayout?.place?.(root, { corner: "bottom-left",
                                                             id: "surface-studio", order: 5 }) || null;
        if (!undock) document.getElementById("openseadragon_wrapper")?.appendChild(root);
        const start = from?.type === "label_surfaces" || source === "mask" ? MASK
            : (from?.channel || channel || built.ui.source.options[0]?.value);
        if (!start) {
            built.ui.status.textContent = "This image has no channel to make a surface of.";
            return true;
        }
        built.ui.source.value = start;
        if (Number.isFinite(Number(from?.iso))) state.iso = Number(from.iso);
        state.source = start;
        if (start === MASK) {
            setSource(MASK);
            // The licence, asked once: the estimate is Paid like the rest.
            const answer = await service().estimate(body()).catch(() => ({}));
            if (answer === null) {
                close();
                return false;
            }
            return true;
        }
        state.channel = start;
        built.ui.threshold.hidden = false;
        const ok = await loadSuggestion();
        if (!ok) {
            close();
            return false;
        }
        requestAnimationFrame(drawHistogram);
        return true;
    }

    function close() {
        clearPreview();
        clearTimeout(estimateTimer);
        if (typeof undock === "function") undock();
        root?.remove();
        root = null;
        undock = null;
        state = null;
    }

    function toggle(options) {
        if (root) close();
        else open(options);
    }

    function isOpen() { return Boolean(root); }

    function init() {
        window.addEventListener("plexora:surface-studio-toggle", () => toggle());
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && root && root.contains(document.activeElement)) close();
        });
    }

    return { init, open, close, toggle, isOpen,
             // For the probes.
             _state: () => state, _body: () => (state ? body() : null) };
}());
