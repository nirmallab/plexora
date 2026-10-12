/**
 * videoOverlays.js - the scientific overlays, drawn at the video's resolution.
 *
 * The viewer's own furniture (legend, dataset name, scale bar) is sized for a
 * screen and hidden while a video renders; these are drawn instead, into each
 * frame, sized to the output (a 4K video gets 4K text, not a magnified 1080p
 * label). Which ones appear is the export's choice (`output.overlays`):
 *
 *   scalebar   a 1-2-5 length near 15 % of the width, from the frame's own
 *              zoom: exact in 2-D; in 3-D it is the length at the depth of
 *              the stack's centre (perspective makes nearer things larger),
 *              and the manifest says so
 *   label      the ACQUIRED plane and timepoint the frame shows ("z 12 / 190 ·
 *              22.0 µm", "t 3 / 10 · 6 s"), or the slab or 3-D range
 *   dataset    the image's name
 *   legend     the channels on screen, each in its colour
 *   axes       the stack's X, Y, Z as they point in a 3-D frame
 *   attribution free text, bottom left
 *   captions   the caption track's text, with a scrim
 *
 * While a display transition is under way (a channel fading, a window
 * moving) and `disclose` is on, a small "display transition" tag says so.
 *
 * Classic script: `window.PlexoraVideoOverlays`.
 */
(function (global) {
    "use strict";

    const FONT = '"Inter", "Helvetica Neue", Helvetica, Arial, sans-serif';
    const NICE = [1, 2, 5];

    /** The 1-2-5 length in µm nearest below `target`. */
    function niceLength(target) {
        if (!(target > 0)) return 0;
        const exp = Math.floor(Math.log10(target));
        let best = NICE[0] * 10 ** exp;
        for (const n of NICE) {
            const value = n * 10 ** exp;
            if (value <= target) best = value;
        }
        return best;
    }

    function formatMicrons(um) {
        if (um >= 1000) return `${+(um / 1000).toFixed(um >= 10000 ? 0 : 1)} mm`;
        if (um >= 1) return `${+um.toFixed(um >= 10 ? 0 : 1)} µm`;
        return `${+(um * 1000).toFixed(0)} nm`;
    }

    /** µm per output pixel for a frame, or null when the image has no scale. */
    function micronsPerPixel(state, facts, size) {
        const px = Number(facts.pixel_size);
        if (!(px > 0)) return null;
        if (state.view.mode === "volume" && state.camera3d) {
            const sx = Number(facts.spacing?.x?.value) || px;
            const sz = Number(facts.spacing?.z?.value) || sx;
            const [w, h] = state.box ? [state.box.width, state.box.height] : facts.image_size || [1, 1];
            const longest = Math.max(w * sx, h * sx, (facts.depth || 1) * sz);
            const fov = state.camera3d.fov || 0.7;
            const viewHeight = 2 * state.camera3d.dist * Math.tan(fov / 2);
            return (viewHeight * longest) / size.height;
        }
        if (!state.viewport) return null;
        return (px * state.viewport.w) / size.width;
    }

    function shadowText(ctx, text, x, y, { size, weight = 500, align = "left", color = "#fff",
                                          baseline = "alphabetic" } = {}) {
        ctx.save();
        ctx.font = `${weight} ${size}px ${FONT}`;
        ctx.textAlign = align;
        ctx.textBaseline = baseline;
        ctx.fillStyle = "rgba(0,0,0,0.65)";
        ctx.shadowColor = "rgba(0,0,0,0.8)";
        ctx.shadowBlur = Math.max(2, size * 0.25);
        ctx.fillText(text, x, y);
        ctx.shadowBlur = 0;
        ctx.fillStyle = color;
        ctx.fillText(text, x, y);
        ctx.restore();
    }

    function drawScalebar(ctx, size, state, facts, unit) {
        const umPerPx = micronsPerPixel(state, facts, size);
        if (!umPerPx) return null;
        const um = niceLength(size.width * 0.15 * umPerPx);
        const length = um / umPerPx;
        const margin = unit * 2;
        const x1 = size.width - margin;
        const x0 = x1 - length;
        const y = size.height - margin;
        ctx.save();
        ctx.fillStyle = "rgba(0,0,0,0.55)";
        ctx.fillRect(x0 - 1, y - unit * 0.35 - 1, length + 2, unit * 0.35 + 2);
        ctx.fillStyle = "#fff";
        ctx.fillRect(x0, y - unit * 0.35, length, unit * 0.35);
        ctx.restore();
        shadowText(ctx, formatMicrons(um), (x0 + x1) / 2, y - unit * 0.7,
                   { size: unit * 0.9, align: "center" });
        return { um, pixels: length, at_centre_depth: state.view.mode === "volume" };
    }

    // The viewer's own words for the plane and time (services/viewState.js
    // label()), so a frame of the video says what the viewer said: planes are
    // counted from 0, timepoints from 1, lengths to one decimal under 10.
    const OP_LABELS = { max: "MIP", mean: "Mean", min: "Min", std: "Std. dev.", med: "Median",
                        pct: "Percentile" };

    function length(value, unit) {
        return `${value.toFixed(value < 10 ? 1 : 0)} ${unit || "µm"}`;
    }

    function labelText(state, facts) {
        const view = state.view;
        const depth = facts.depth || 1;
        const frames = facts.frames || 1;
        const zStep = facts.spacing?.z && Number(facts.spacing.z.value) > 0 ? facts.spacing.z : null;
        const tStep = facts.spacing?.t && Number(facts.spacing.t.value) > 0 ? facts.spacing.t : null;
        let prefix = "";
        if (frames > 1) {
            const seconds = tStep ? view.t * Number(tStep.value) : null;
            prefix = `t ${view.t + 1}/${frames}`
                + (seconds !== null ? ` · ${seconds.toFixed(seconds < 10 ? 1 : 0)} ${tStep.unit || "s"}` : "")
                + " · ";
        }
        if (view.mode === "volume") {
            const composite = view.composite === "additive" ? "additive" : "MIP";
            const slab = view.slab || { z0: 0, z1: depth - 1 };
            const range = slab.z0 === 0 && slab.z1 === depth - 1 ? "all planes" : `z ${slab.z0}–${slab.z1}`;
            return `${prefix}Volume · ${composite} · ${range}`;
        }
        if (view.mode === "slab" && view.slab) {
            const { z0, z1, op } = view.slab;
            const planes = z1 - z0 + 1;
            const thick = zStep ? ` · ${length(planes * Number(zStep.value), zStep.unit)}` : "";
            return `${prefix}${OP_LABELS[op] || op} z ${z0}–${z1} · ${planes} plane${planes === 1 ? "" : "s"}${thick}`;
        }
        if (depth <= 1) return prefix.replace(/ · $/, "");
        const where = zStep ? ` · ${length(view.z * Number(zStep.value), zStep.unit)}` : "";
        return `${prefix}z ${view.z}/${depth}${where}`;
    }

    function drawLegend(ctx, size, state, unit) {
        const rows = (state.channels || []).filter((c) => c.visible && (c.presence ?? 1) > 0.02);
        if (!rows.length) return;
        const fontSize = unit * 0.85;
        const x = unit * 2;
        let y = unit * 2 + fontSize;
        for (const channel of rows) {
            ctx.save();
            ctx.globalAlpha = Math.max(0.15, Math.min(1, channel.presence ?? 1));
            ctx.fillStyle = channel.color || "#fff";
            ctx.fillRect(x, y - fontSize * 0.75, fontSize * 0.75, fontSize * 0.75);
            ctx.restore();
            ctx.save();
            ctx.globalAlpha = Math.max(0.3, Math.min(1, channel.presence ?? 1));
            shadowText(ctx, channel.name || channel.key, x + fontSize * 1.15, y,
                       { size: fontSize, color: channel.color || "#fff" });
            ctx.restore();
            y += fontSize * 1.45;
        }
    }

    function drawAxes(ctx, size, state, unit) {
        const q = state.camera3d?.q;
        const rotate = global.PlexoraVideoInterp?.rotate;
        if (!q || !rotate) return;
        const cx = unit * 3.4;
        const cy = size.height - unit * 3.4;
        const length = unit * 2.2;
        const axes = [["X", [1, 0, 0], "#f87171"], ["Y", [0, 1, 0], "#4ade80"], ["Z", [0, 0, 1], "#60a5fa"]]
            .map(([name, axis, color]) => ({ name, color, v: rotate(q, axis) }))
            .sort((a, b) => a.v[2] - b.v[2]);
        ctx.save();
        ctx.lineCap = "round";
        for (const { name, color, v } of axes) {
            const x = cx + v[0] * length;
            const y = cy - v[1] * length;
            ctx.strokeStyle = color;
            ctx.lineWidth = Math.max(1.5, unit * 0.16);
            ctx.globalAlpha = v[2] < -0.2 ? 0.55 : 1;
            ctx.beginPath();
            ctx.moveTo(cx, cy);
            ctx.lineTo(x, y);
            ctx.stroke();
            ctx.globalAlpha = 1;
            shadowText(ctx, name, x + v[0] * unit * 0.5, y - v[1] * unit * 0.5,
                       { size: unit * 0.8, weight: 700, align: "center", baseline: "middle", color });
        }
        ctx.restore();
    }

    function drawCaption(ctx, size, text, unit, opacity = 1) {
        if (!text) return;
        const fontSize = unit * 1.3;
        ctx.save();
        ctx.font = `600 ${fontSize}px ${FONT}`;
        const width = ctx.measureText(text).width;
        const padX = fontSize * 0.8;
        const padY = fontSize * 0.45;
        const x = size.width / 2;
        const y = size.height - unit * 5.2;
        ctx.globalAlpha = Math.max(0, Math.min(1, opacity));
        ctx.fillStyle = "rgba(10,14,20,0.62)";
        const rx = x - width / 2 - padX;
        const ry = y - fontSize - padY * 0.5;
        const rw = width + padX * 2;
        const rh = fontSize + padY * 1.6;
        const r = rh / 2;
        ctx.beginPath();
        ctx.moveTo(rx + r, ry);
        ctx.arcTo(rx + rw, ry, rx + rw, ry + rh, r);
        ctx.arcTo(rx + rw, ry + rh, rx, ry + rh, r);
        ctx.arcTo(rx, ry + rh, rx, ry, r);
        ctx.arcTo(rx, ry, rx + rw, ry, r);
        ctx.fill();
        ctx.fillStyle = "#f8fafc";
        ctx.textAlign = "center";
        ctx.fillText(text, x, y);
        ctx.restore();
    }

    function drawTitleCard(ctx, size, card, unit) {
        if (!card || !card.title) return;
        const alpha = Math.max(0, Math.min(1, card.opacity ?? 1));
        ctx.save();
        ctx.globalAlpha = alpha * 0.72;
        ctx.fillStyle = "#05070b";
        ctx.fillRect(0, 0, size.width, size.height);
        ctx.globalAlpha = alpha;
        ctx.restore();
        ctx.save();
        ctx.globalAlpha = alpha;
        ctx.textAlign = "center";
        ctx.fillStyle = "#f8fafc";
        ctx.font = `650 ${unit * 2.6}px ${FONT}`;
        ctx.fillText(card.title, size.width / 2, size.height / 2);
        if (card.subtitle) {
            ctx.fillStyle = "#94a3b8";
            ctx.font = `450 ${unit * 1.2}px ${FONT}`;
            ctx.fillText(card.subtitle, size.width / 2, size.height / 2 + unit * 2.2);
        }
        ctx.restore();
    }

    /** The spotlight (an emphasis track): everything outside `rect` dimmed by
     *  `strength`, or `rect` outlined. `rect` in the context's pixels. */
    function drawSpotlight(ctx, size, emphasis, rect) {
        if (!emphasis || !rect || emphasis.mode === "none") return;
        ctx.save();
        if (emphasis.mode === "outline") {
            ctx.strokeStyle = `rgba(125, 211, 252, ${Math.max(0.2, emphasis.strength)})`;
            ctx.lineWidth = Math.max(2, size.height / 360);
            ctx.strokeRect(rect.x, rect.y, rect.width, rect.height);
        } else {
            ctx.fillStyle = `rgba(0, 0, 0, ${Math.max(0, Math.min(0.95, emphasis.strength))})`;
            ctx.beginPath();
            ctx.rect(0, 0, size.width, size.height);
            ctx.rect(rect.x, rect.y, rect.width, rect.height);
            ctx.fill("evenodd");
        }
        ctx.restore();
    }

    /** Image rectangle `bbox` ([x, y, w, h], full-resolution pixels) in a 2-D
     *  frame's output pixels, or null (3-D, or a turned view). */
    function bboxInFrame(state, bbox, size) {
        const view = state.viewport;
        if (!bbox || !view || state.view.mode === "volume" || (view.degrees || 0) % 360) return null;
        const r = global.PlexoraVideoCamera2d.rect(view, size.width / size.height);
        const k = size.width / r.width;
        let x = (bbox[0] - r.x) * k;
        let y = (bbox[1] - r.y) * k;
        const w = bbox[2] * k;
        const h = bbox[3] * k;
        if (view.flip_h) x = size.width - x - w;
        if (view.flip_v) y = size.height - y - h;
        return { x, y, width: w, height: h };
    }

    /**
     * Over the live viewer while a preview plays: what the export will draw
     * that the viewer does not -- captions, a title card, a fade, a spotlight
     * -- inside `frame` (the output's rectangle on screen), and the area
     * outside the output's frame dimmed so the crop is visible.
     */
    function drawPreview(ctx, size, state, frame, { spotlight = null } = {}) {
        ctx.clearRect(0, 0, size.width, size.height);
        if (!frame) return;
        ctx.save();
        ctx.fillStyle = "rgba(5, 7, 11, 0.45)";
        ctx.beginPath();
        ctx.rect(0, 0, size.width, size.height);
        ctx.rect(frame.x, frame.y, frame.width, frame.height);
        ctx.fill("evenodd");
        ctx.strokeStyle = "rgba(255, 255, 255, 0.35)";
        ctx.setLineDash([6, 5]);
        ctx.lineWidth = 1;
        ctx.strokeRect(frame.x + 0.5, frame.y + 0.5, frame.width - 1, frame.height - 1);
        ctx.restore();
        ctx.save();
        ctx.beginPath();
        ctx.rect(frame.x, frame.y, frame.width, frame.height);
        ctx.clip();
        ctx.translate(frame.x, frame.y);
        const inner = { width: frame.width, height: frame.height };
        const unit = Math.max(8, Math.round(inner.height / 54));
        if (spotlight) drawSpotlight(ctx, inner, state.emphasis, spotlight);
        if (state.transition && state.transition.kind === "fade_black") {
            ctx.fillStyle = `rgba(0,0,0,${Math.max(0, Math.min(1, state.transition.alpha))})`;
            ctx.fillRect(0, 0, inner.width, inner.height);
        }
        if (state.hud?.caption) drawCaption(ctx, inner, state.hud.caption, unit, state.hud.opacity ?? 1);
        if (state.hud?.title) drawTitleCard(ctx, inner, { title: state.hud.title, subtitle: state.hud.subtitle,
                                                          opacity: state.hud.opacity }, unit);
        ctx.restore();
    }

    /**
     * Draw the chosen overlays onto `ctx` (an output-sized 2-D context).
     * Returns what was drawn, for the manifest: `{scalebar, label}`.
     */
    function draw(ctx, size, state, facts, overlays = {}, extra = {}) {
        const unit = Math.max(8, Math.round(size.height / 54));
        const drawn = {};
        if (state.emphasis?.bbox) {
            drawSpotlight(ctx, size, state.emphasis, bboxInFrame(state, state.emphasis.bbox, size));
        }
        if (overlays.legend) drawLegend(ctx, size, state, unit);
        if (overlays.axes && state.view.mode === "volume") drawAxes(ctx, size, state, unit);
        if (overlays.scalebar) drawn.scalebar = drawScalebar(ctx, size, state, facts, unit);
        const label = overlays.label ? labelText(state, facts) : "";
        if (label) {
            shadowText(ctx, label, size.width - unit * 2, unit * 2 + unit * 0.9,
                       { size: unit * 0.9, align: "right" });
            drawn.label = label;
        }
        if (overlays.dataset && extra.datasetName) {
            shadowText(ctx, extra.datasetName, size.width - unit * 2,
                       unit * 2 + unit * (label ? 2.3 : 0.9), { size: unit * 0.8, align: "right",
                                                               color: "#cbd5e1" });
        }
        if (overlays.attribution && extra.attribution) {
            shadowText(ctx, extra.attribution, unit * 2, size.height - unit * 2,
                       { size: unit * 0.75, color: "#cbd5e1" });
        }
        if (overlays.captions !== false && state.hud?.caption) {
            drawCaption(ctx, size, state.hud.caption, unit, state.hud.opacity ?? 1);
        }
        if (state.hud?.title) drawTitleCard(ctx, size, { title: state.hud.title,
                                                         subtitle: state.hud.subtitle,
                                                         opacity: state.hud.opacity }, unit);
        if (extra.disclose && state.display_transition?.length) {
            shadowText(ctx, "display transition", unit * 2, unit * 2 + unit * 0.75,
                       { size: unit * 0.7, color: "#fbbf24" });
        }
        return drawn;
    }

    global.PlexoraVideoOverlays = { draw, drawPreview, drawSpotlight, bboxInFrame, labelText, niceLength,
                                    formatMicrons, micronsPerPixel };
    if (typeof module !== "undefined" && module.exports) module.exports = global.PlexoraVideoOverlays;
})(typeof window !== "undefined" ? window : globalThis);
