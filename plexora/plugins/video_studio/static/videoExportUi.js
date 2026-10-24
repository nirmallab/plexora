/**
 * videoExportUi.js - the Export dialog.
 *
 * A native <dialog> (focus trapped, Escape closes, the page behind inert):
 *
 *   Size        1080p, 1440p, 4K, or a custom width x height (even numbers)
 *   Frame rate  24, 30, 60 (GIF: 10, 15, 25 -- a GIF's frame delays are in
 *               hundredths of a second, which 30 fps cannot be)
 *   Format      every format, each with how it would be written ("in this
 *               browser", "on the server") and, when no route can write it,
 *               why not -- shown disabled, never hidden
 *   Quality     draft / good / high / max
 *   Transparent only where the format keeps alpha and the frame is 2-D
 *   Overlays    scale bar, plane/time label, dataset name, legend, axes,
 *               attribution, captions
 *
 * and an estimate: "{n} frames · {d} s at {fps} fps · about {t} to render ·
 * about {MB} MB". `open(options)` resolves the chosen settings and route, or
 * null when the dialog is dismissed.
 *
 * Classic script: `window.PlexoraVideoExportUi`.
 */
(function (global) {
    "use strict";

    const SIZES = [
        { key: "1080", label: "1080p", width: 1920, height: 1080 },
        { key: "1440", label: "1440p", width: 2560, height: 1440 },
        { key: "2160", label: "4K", width: 3840, height: 2160 },
        { key: "custom", label: "Custom" },
    ];
    const FPS = [24, 30, 60];
    const GIF_FPS = [10, 15, 25];
    const QUALITIES = [["draft", "Draft"], ["good", "Good"], ["high", "High"], ["max", "Max"]];
    const OVERLAYS = [["scalebar", "Scale bar"], ["label", "Plane and time"], ["dataset", "Dataset name"],
                      ["legend", "Channel legend"], ["axes", "Axes (3-D)"], ["attribution", "Attribution"],
                      ["captions", "Captions"], ["disclosure", "Transition tag"]];
    const ROUTE_WORDS = { browser: "in this browser", server: "on the server" };

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function segmented(name, options, value, onChange) {
        const group = el("div", "vs-segmented");
        group.setAttribute("role", "radiogroup");
        group.setAttribute("aria-label", name);
        const buttons = options.map(([v, label]) => {
            const b = el("button", "vs-seg", label);
            b.type = "button";
            b.setAttribute("role", "radio");
            b.dataset.value = String(v);
            b.addEventListener("click", () => {
                set(v);
                onChange(v);
            });
            group.appendChild(b);
            return b;
        });
        function set(v) {
            buttons.forEach((b) => b.setAttribute("aria-checked", String(b.dataset.value === String(v))));
        }
        set(value);
        return { node: group, set };
    }

    function duration(seconds) {
        if (seconds < 60) return `${Math.max(1, Math.round(seconds))} s`;
        const minutes = Math.round(seconds / 60);
        return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
    }

    function megabytes(mb) {
        return mb >= 1000 ? `${(mb / 1000).toFixed(1)} GB` : mb >= 10 ? `${Math.round(mb)} MB`
            : `${mb.toFixed(1)} MB`;
    }

    /**
     * `options`: `project`, `output` (the saved output settings), `report`
     * (server encoders), `prefer`, `volume` (any 3-D frame), `remote`.
     */
    function open({ project, output, report, prefer = "browser", volume = false, remote = false }) {
        return new Promise((resolve) => {
            const Encoders = global.PlexoraVideoEncoders;
            const settings = {
                width: output.width || 1920, height: output.height || 1080, fps: output.fps || 30,
                format: output.format || "mp4", quality: output.quality || "good",
                transparent: Boolean(output.transparent),
                overlays: { scalebar: true, label: true, disclosure: true, ...(output.overlays || {}) },
                attribution: output.attribution || "",
            };
            let rows = [];
            let route = null;

            const dialog = el("dialog", "vs-dialog");
            dialog.setAttribute("aria-labelledby", "vs-export-title");
            const form = el("form", "vs-dialog-body");
            form.method = "dialog";
            const title = el("h2", "vs-dialog-title", "Export video");
            title.id = "vs-export-title";
            const close = el("button", "vs-icon-button vs-dialog-close");
            close.type = "button";
            close.setAttribute("aria-label", "Close");
            close.innerHTML = '<span class="fas fa-xmark" aria-hidden="true"></span>';
            close.addEventListener("click", () => dialog.close("cancel"));
            const header = el("div", "vs-dialog-header");
            header.append(title, close);

            const section = (label) => {
                const wrap = el("div", "vs-dialog-row");
                wrap.appendChild(el("span", "vs-dialog-label", label));
                const body = el("div", "vs-dialog-control");
                wrap.appendChild(body);
                form.appendChild(wrap);
                return body;
            };

            // Size
            const sizeBody = section("Size");
            const custom = el("span", "vs-custom-size");
            const widthInput = el("input", "vs-input");
            const heightInput = el("input", "vs-input");
            for (const [input, key] of [[widthInput, "width"], [heightInput, "height"]]) {
                input.type = "number";
                input.min = "64";
                input.max = "7680";
                input.step = "2";
                input.value = String(settings[key]);
                input.setAttribute("aria-label", key === "width" ? "Width in pixels" : "Height in pixels");
                input.addEventListener("change", () => {
                    let v = Math.round(Number(input.value) || settings[key]);
                    v = Math.max(64, Math.min(7680, v - (v % 2)));
                    input.value = String(v);
                    settings[key] = v;
                    refresh();
                });
            }
            custom.append(widthInput, el("span", "vs-unit", "×"), heightInput, el("span", "vs-unit", "px"));
            const sizeKey = SIZES.find((s) => s.width === settings.width && s.height === settings.height)?.key
                || "custom";
            const size = segmented("Size", SIZES.map((s) => [s.key, s.label]), sizeKey, (key) => {
                const found = SIZES.find((s) => s.key === key);
                if (found && found.width) {
                    settings.width = found.width;
                    settings.height = found.height;
                    widthInput.value = String(found.width);
                    heightInput.value = String(found.height);
                }
                custom.hidden = key !== "custom";
                refresh();
            });
            custom.hidden = sizeKey !== "custom";
            sizeBody.append(size.node, custom);

            // Frame rate
            const fpsBody = section("Frame rate");
            let fpsControl = null;
            const drawFps = () => {
                const choices = settings.format === "gif" ? GIF_FPS : FPS;
                if (!choices.includes(settings.fps)) settings.fps = settings.format === "gif" ? 15 : 30;
                fpsControl = segmented("Frame rate", choices.map((f) => [f, `${f} fps`]), settings.fps, (f) => {
                    settings.fps = Number(f);
                    refresh();
                });
                fpsBody.replaceChildren(fpsControl.node);
            };

            // Format
            const formatBody = section("Format");
            const formatList = el("div", "vs-format-list");
            formatList.setAttribute("role", "radiogroup");
            formatList.setAttribute("aria-label", "Format");
            formatBody.appendChild(formatList);

            // Quality
            const qualityBody = section("Quality");
            const quality = segmented("Quality", QUALITIES, settings.quality, (q) => {
                settings.quality = q;
                refresh();
            });
            qualityBody.appendChild(quality.node);

            // Transparent
            const alphaBody = section("Background");
            const alpha = el("label", "vs-check");
            const alphaBox = el("input");
            alphaBox.type = "checkbox";
            alphaBox.checked = settings.transparent;
            alphaBox.addEventListener("change", () => {
                settings.transparent = alphaBox.checked;
                refresh();
            });
            const alphaText = el("span", null, "Transparent");
            alpha.append(alphaBox, alphaText);
            const alphaNote = el("span", "vs-note");
            alphaBody.append(alpha, alphaNote);

            // Overlays
            const overlayBody = section("Overlays");
            const overlayGrid = el("div", "vs-overlay-grid");
            for (const [key, label] of OVERLAYS) {
                const item = el("label", "vs-check");
                const box = el("input");
                box.type = "checkbox";
                box.checked = Boolean(settings.overlays[key]);
                box.addEventListener("change", () => {
                    settings.overlays[key] = box.checked;
                    attributionInput.hidden = !settings.overlays.attribution;
                });
                item.append(box, el("span", null, label));
                overlayGrid.appendChild(item);
            }
            const attributionInput = el("input", "vs-input vs-attribution");
            attributionInput.type = "text";
            attributionInput.maxLength = 200;
            attributionInput.placeholder = "Attribution text";
            attributionInput.value = settings.attribution;
            attributionInput.hidden = !settings.overlays.attribution;
            attributionInput.addEventListener("input", () => { settings.attribution = attributionInput.value; });
            overlayBody.append(overlayGrid, attributionInput);

            const estimateLine = el("p", "vs-estimate");
            estimateLine.setAttribute("aria-live", "polite");
            const warning = el("p", "vs-warning");
            warning.hidden = true;

            const actions = el("div", "vs-dialog-actions");
            const cancel = el("button", "vs-button", "Cancel");
            cancel.type = "button";
            cancel.addEventListener("click", () => dialog.close("cancel"));
            const go = el("button", "vs-button vs-primary", "Export");
            go.type = "submit";
            go.value = "export";
            actions.append(cancel, go);
            form.append(estimateLine, warning, actions);
            dialog.append(header, form);

            function drawFormats() {
                formatList.replaceChildren();
                const byFormat = new Map();
                for (const row of rows) {
                    if (!byFormat.has(row.format)) byFormat.set(row.format, []);
                    byFormat.get(row.format).push(row);
                }
                for (const [format, routes] of byFormat) {
                    const best = routes.find((r) => r.available) || routes[0];
                    const option = el("button", "vs-format");
                    option.type = "button";
                    option.setAttribute("role", "radio");
                    option.dataset.format = format;
                    option.disabled = !best.available;
                    option.setAttribute("aria-checked", String(settings.format === format && best.available));
                    option.appendChild(el("span", "vs-format-name", Encoders.LABELS[format] || best.label));
                    const how = best.route === "server" && best.writer !== "ffmpeg"
                        ? `${ROUTE_WORDS.server} · ${best.label}` : ROUTE_WORDS[best.route];
                    option.appendChild(el("span", "vs-format-how",
                                          best.available ? how : best.reason || "Not available here"));
                    option.addEventListener("click", () => {
                        settings.format = format;
                        drawFps();
                        refresh();
                    });
                    formatList.appendChild(option);
                }
            }

            async function refresh() {
                rows = await Encoders.available(settings, report, { prefer });
                const forFormat = rows.filter((r) => r.format === settings.format && r.available);
                route = forFormat[0] || null;
                if (!route) {
                    const fallback = Encoders.defaultRoute(rows);
                    if (fallback) {
                        settings.format = fallback.format;
                        route = fallback;
                        drawFps();
                    }
                }
                drawFormats();
                const canAlpha = Boolean(route?.alpha) && !volume;
                alphaBox.disabled = !canAlpha;
                if (!canAlpha) alphaBox.checked = false;
                settings.transparent = canAlpha && alphaBox.checked;
                alphaNote.textContent = canAlpha ? "" : volume ? "3-D frames are rendered on a ground colour."
                    : "This format has no transparency.";
                const est = global.PlexoraVideoExportLoop.estimate(project, settings);
                const seconds = (project.duration_s).toFixed(project.duration_s < 10 ? 1 : 0);
                estimateLine.textContent = `${est.frames} frames · ${seconds} s at ${settings.fps} fps · `
                    + `about ${duration(est.seconds)} to render · about ${megabytes(est.megabytes)}`;
                const warnings = [];
                if (volume && settings.width * settings.height > 2560 * 1440) {
                    warnings.push("4K 3-D frames are slow to render: each one waits for full detail.");
                }
                if (remote && route?.route === "server") {
                    warnings.push("Frames travel to the remote server as PNG; a local browser encoder is faster where it is available.");
                }
                if (settings.format === "gif" && (settings.width > 1280 || est.frames > 300)) {
                    warnings.push("GIFs this large or long are heavy; MP4 or WebM is usually better.");
                }
                if (route?.route === "browser" && est.megabytes > 1500) {
                    warnings.push("The browser holds the finished file in memory until it is saved.");
                }
                warning.textContent = warnings.join(" ");
                warning.hidden = !warnings.length;
                go.disabled = !route;
            }

            dialog.addEventListener("close", () => {
                const value = dialog.returnValue;
                dialog.remove();
                if (value === "export" && route) resolve({ settings: { ...settings }, route });
                else resolve(null);
            });
            dialog.addEventListener("keydown", (event) => event.stopPropagation());
            document.body.appendChild(dialog);
            drawFps();
            refresh().then(() => {
                dialog.showModal();
                go.focus();
            });
        });
    }

    global.PlexoraVideoExportUi = { open, SIZES };
})(typeof window !== "undefined" ? window : globalThis);
