/**
 * videoPathEditor.js - the camera path as positions the user chooses.
 *
 * Turn the path editor on (P, or More > Camera path editor) and the bar's
 * strip becomes a row of numbered camera positions. A position is whatever the
 * viewer shows when it is added: frame the view with the viewer's own controls
 * -- drag, wheel, the axis buttons, a turn -- and press "+ Add position" (K).
 * Selecting a position shows it; "Update" replaces it with the view now on
 * screen. Between positions the camera moves along a smooth path (squad for
 * orientation, Catmull-Rom for position, constant speed when "Even speed" is
 * on), and each segment has its own length and pace.
 *
 * Over the image, an SVG overlay (only while the editor is on) draws the
 * positions: in 3-D, where each position's camera is, numbered, seen from the
 * current view, with the stack's box; in 2-D, the rectangle each position
 * frames. Clicking a number selects that position. The overlay fades while
 * the user drags the image, so it never sits on what they are framing.
 *
 * Positions live in one keyframed camera track (`camera3d` in the 3-D view,
 * `camera2d` otherwise); a preset's procedural move is kept as the start of
 * the path when the first position is added.
 *
 * Classic script: `window.PlexoraVideoPathEditor`.
 */
(function (global) {
    "use strict";

    const SVG = "http://www.w3.org/2000/svg";
    const Interp = global.PlexoraVideoInterp;
    const SEGMENT_S = 2;
    const EASES = [["linear", "Even"], ["sine_in_out", "Gentle"], ["cubic_in_out", "Smooth"],
                   ["cinematic", "Cinematic"]];

    function el(tag, className, attrs = {}) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        for (const [k, v] of Object.entries(attrs)) {
            if (v === undefined || v === null || v === false) continue;
            if (k === "text") node.textContent = v;
            else node.setAttribute(k, v === true ? "" : String(v));
        }
        return node;
    }

    function svg(tag, attrs = {}) {
        const node = document.createElementNS(SVG, tag);
        for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
        return node;
    }

    function create(controller) {
        let active = false;
        let selected = 0;
        let strip = null;
        let overlay = null;
        let raf = 0;
        let offViewport = null;

        const env = controller.env;
        const volume = () => env.vs?.get?.().mode === "volume";
        const kind = () => (volume() ? "camera3d" : "camera2d");

        function track(project = controller.project) {
            return (project?.tracks || []).find((t) => t.kind === kind() && t.mode === "keyframes") || null;
        }

        function currentPose() {
            const scene = global.PlexoraVideoSceneState.capture(env);
            return kind() === "camera3d" ? scene.camera3d : scene.viewport;
        }

        async function addKey() {
            const pose = currentPose();
            if (!pose) return;
            controller.preview.pause();
            const existing = track();
            if (!existing) {
                const ops = [];
                // A procedural move (a turn) becomes muted, not lost: the path
                // takes over the camera, and unmuting brings the turn back.
                for (const t of controller.project.tracks) {
                    if (t.kind === kind() && !t.muted) {
                        ops.push({ op: "update_track", track_id: t.track_id, changes: { muted: true } });
                    }
                }
                ops.push({ op: "add_track", track: {
                    kind: kind(), mode: "keyframes", label: "Camera path",
                    interp: { orientation: "squad", path: "catmull", speed: "time" },
                    keyframes: [{ t: 0, value: pose, ease: "sine_in_out" }] } });
                await controller.edit(ops, "Add camera position");
                selected = 0;
            } else {
                const last = existing.keyframes[existing.keyframes.length - 1];
                const t = last ? last.t + SEGMENT_S : 0;
                const ops = [];
                if (t > controller.project.duration_s) {
                    ops.push({ op: "set_duration", duration_s: Math.round(t * 100) / 100 });
                }
                ops.push({ op: "add_keyframe", track_id: existing.track_id,
                           keyframe: { t, value: pose, ease: "sine_in_out" } });
                await controller.edit(ops, "Add camera position");
                selected = existing.keyframes.length;
            }
            render();
        }

        async function updateKey() {
            const existing = track();
            const kf = existing?.keyframes[selected];
            const pose = currentPose();
            if (!kf || !pose) return;
            await controller.edit([{ op: "update_keyframe", track_id: existing.track_id, kf_id: kf.kf_id,
                                     changes: { value: pose } }], "Update camera position");
            render();
        }

        async function removeKey() {
            const existing = track();
            const kf = existing?.keyframes[selected];
            if (!kf) return;
            await controller.edit([{ op: "remove_keyframe", track_id: existing.track_id, kf_id: kf.kf_id }],
                                  "Remove camera position");
            selected = Math.max(0, selected - 1);
            render();
        }

        async function reverse() {
            const existing = track();
            if (!existing || existing.keyframes.length < 2) return;
            const times = existing.keyframes.map((k) => k.t);
            const values = existing.keyframes.map((k) => k.value).reverse();
            const keyframes = existing.keyframes.map((k, i) => ({ ...k, t: times[i], value: values[i] }));
            await controller.edit([{ op: "update_track", track_id: existing.track_id,
                                     changes: { keyframes } }], "Reverse camera path");
            render();
        }

        async function duplicate() {
            const existing = track();
            const kf = existing?.keyframes[selected];
            if (!kf) return;
            const last = existing.keyframes[existing.keyframes.length - 1];
            const t = last.t + SEGMENT_S;
            const ops = [];
            if (t > controller.project.duration_s) ops.push({ op: "set_duration", duration_s: t });
            ops.push({ op: "add_keyframe", track_id: existing.track_id,
                       keyframe: { t, value: kf.value, ease: kf.ease } });
            await controller.edit(ops, "Duplicate camera position");
            selected = existing.keyframes.length;
            render();
        }

        async function setSegment(field, value) {
            const existing = track();
            const kf = existing?.keyframes[selected];
            if (!kf) return;
            if (field === "ease") {
                await controller.edit([{ op: "update_keyframe", track_id: existing.track_id,
                                         kf_id: kf.kf_id, changes: { ease: value } }], "Pace");
            } else if (field === "length") {
                const next = existing.keyframes[selected + 1];
                if (!next) return;
                const delta = Math.max(0.2, value) - (next.t - kf.t);
                const keyframes = existing.keyframes.map((k, i) => (i > selected ? { ...k, t: k.t + delta } : k));
                const end = keyframes[keyframes.length - 1].t;
                const ops = [];
                if (end > controller.project.duration_s) ops.push({ op: "set_duration", duration_s: end });
                ops.push({ op: "update_track", track_id: existing.track_id, changes: { keyframes } });
                if (end < controller.project.duration_s && delta < 0) {
                    ops.push({ op: "set_duration", duration_s: Math.max(end, 0.5) });
                }
                await controller.edit(ops, "Segment length");
            } else if (field === "speed") {
                await controller.edit([{ op: "update_track", track_id: existing.track_id, changes: {
                    interp: { ...existing.interp, speed: value ? "arc" : "time" } } }], "Even speed");
            }
            render();
        }

        async function focusMenu(anchor) {
            const rois = await global.PlexoraVideoTracking.listRois(env);
            const items = rois.length ? rois.map((target) => ({
                label: target.label, title: `Region ${target.id}`,
                onSelect: () => {
                    controller.preview.pause();
                    global.PlexoraVideoTracking.focus(env, controller.facts, target, controller.aspect());
                    controller.bar.setHint(`Framed “${target.label}”. + Add position to keep it.`);
                },
            })) : [{ label: "No regions on this image (draw one with the ROI tool)", disabled: true }];
            global.PlexoraMenu?.open(anchor, items, { align: "left" });
        }

        function select(index) {
            const existing = track();
            const kf = existing?.keyframes[index];
            if (!kf) return;
            selected = index;
            controller.preview.pause();
            controller.preview.seek(kf.t);
            render();
        }

        // -- the strip ---------------------------------------------------------------

        function render() {
            if (!active) return;
            const fields = controller.bar.fields;
            if (!strip) {
                strip = el("div", "vs-path-strip");
                strip.setAttribute("role", "group");
                strip.setAttribute("aria-label", "Camera positions");
            }
            fields.replaceChildren(strip);
            strip.replaceChildren();
            const existing = track();
            const list = el("div", "vs-viewpoints");
            (existing?.keyframes || []).forEach((kf, i) => {
                const button = el("button", "vs-viewpoint", { type: "button", "aria-pressed": String(i === selected),
                                                             title: `Position ${i + 1} at ${kf.t.toFixed(1)} s` });
                button.append(el("span", "vs-viewpoint-index", { text: String(i + 1) }),
                              el("span", null, { text: `${kf.t.toFixed(1)} s` }));
                button.addEventListener("click", () => select(i));
                list.appendChild(button);
            });
            const add = el("button", "vs-chip is-set", { type: "button", text: "+ Add position",
                                                        title: "Add the view on screen as the next position (K)" });
            add.addEventListener("click", addKey);
            const focus = el("button", "vs-chip", { type: "button", text: "Focus on…", "aria-haspopup": "menu",
                                                   title: "Frame a region, then add it as a position" });
            focus.addEventListener("click", () => focusMenu(focus));
            list.append(add, focus);
            strip.appendChild(list);

            const kf = existing?.keyframes[selected];
            if (kf) {
                const row = el("div", "vs-fields");
                const actions = el("div", "vs-field");
                actions.append(el("span", "vs-field-label", { text: `Position ${selected + 1}` }));
                const buttons = el("span", "vs-view-field");
                for (const [label, fn, title] of [["Update", updateKey, "Replace with the view on screen"],
                                                  ["Duplicate", duplicate, "Add a copy at the end"],
                                                  ["Remove", removeKey, "Remove this position"],
                                                  ["Reverse", reverse, "Run the path backwards"]]) {
                    const b = el("button", "vs-chip", { type: "button", text: label, title });
                    b.addEventListener("click", fn);
                    buttons.appendChild(b);
                }
                actions.appendChild(buttons);
                row.appendChild(actions);
                const next = existing.keyframes[selected + 1];
                if (next) {
                    const length = el("div", "vs-field");
                    length.append(el("span", "vs-field-label", { text: "To next" }));
                    const wrap = el("span", "vs-number");
                    const input = el("input", "vs-input", { type: "number", min: "0.2", step: "0.1",
                                                           value: (next.t - kf.t).toFixed(1),
                                                           "aria-label": "Seconds to the next position" });
                    input.addEventListener("change", () => setSegment("length", Number(input.value)));
                    input.addEventListener("keydown", (e) => e.stopPropagation());
                    wrap.append(input, el("span", "vs-unit", { text: "s" }));
                    length.appendChild(wrap);
                    row.appendChild(length);
                    const pace = el("div", "vs-field");
                    pace.append(el("span", "vs-field-label", { text: "Pace" }));
                    const group = el("div", "vs-segmented", { role: "radiogroup", "aria-label": "Pace" });
                    for (const [value, label] of EASES) {
                        const b = el("button", "vs-seg", { type: "button", role: "radio", text: label,
                                                          "aria-checked": String(kf.ease === value) });
                        b.addEventListener("click", () => setSegment("ease", value));
                        group.appendChild(b);
                    }
                    pace.appendChild(group);
                    row.appendChild(pace);
                }
                if (existing.keyframes.length > 2) {
                    const speed = el("div", "vs-field");
                    speed.append(el("span", "vs-field-label", { text: "Even speed" }));
                    const sw = el("button", "vs-switch", { type: "button", role: "switch",
                                                          "aria-checked": String(existing.interp?.speed === "arc"),
                                                          "aria-label": "Even speed along the path" });
                    sw.appendChild(el("span", "vs-switch-knob", { "aria-hidden": "true" }));
                    sw.addEventListener("click", () => setSegment("speed", existing.interp?.speed !== "arc"));
                    speed.appendChild(sw);
                    row.appendChild(speed);
                }
                strip.appendChild(row);
            }
            controller.bar.setHint(existing
                ? "Frame the view with the viewer's own controls, then add or update a position."
                : "Frame the first view, then + Add position. Each position you add becomes a stop on the path.");
            drawOverlay();
        }

        // -- the overlay ---------------------------------------------------------------

        function mountOverlay() {
            if (overlay) return;
            overlay = svg("svg", { class: "vs-path-overlay", "aria-hidden": "true" });
            env.wrapper?.appendChild(overlay);
            const fade = (on) => overlay?.classList.toggle("is-faded", on);
            overlay._down = () => fade(true);
            overlay._up = () => fade(false);
            env.wrapper?.addEventListener("pointerdown", overlay._down, true);
            global.addEventListener("pointerup", overlay._up, true);
            offViewport = env.ctx?.layers?.onViewportChange?.(() => drawOverlay()) || null;
            const loop = () => {
                if (!active) return;
                if (volume()) drawOverlay();
                raf = global.requestAnimationFrame(loop);
            };
            raf = global.requestAnimationFrame(loop);
        }

        function unmountOverlay() {
            if (raf) global.cancelAnimationFrame(raf);
            raf = 0;
            if (!overlay) return;
            env.wrapper?.removeEventListener("pointerdown", overlay._down, true);
            global.removeEventListener("pointerup", overlay._up, true);
            offViewport?.();
            offViewport = null;
            overlay.remove();
            overlay = null;
        }

        function drawOverlay() {
            if (!overlay || !active) return;
            const rect = env.wrapper.getBoundingClientRect();
            overlay.setAttribute("viewBox", `0 0 ${rect.width} ${rect.height}`);
            overlay.replaceChildren();
            const existing = track();
            if (!existing) return;
            if (kind() === "camera3d") draw3d(existing, rect);
            else draw2d(existing, rect);
        }

        function draw3d(existing, rect) {
            const renderer = env.renderer;
            if (!renderer) return;
            const Camera3d = global.PlexoraVideoCamera3d;
            const live = Camera3d.fromRenderer(renderer.camera);
            const aspect = rect.width / Math.max(1, rect.height);
            const toScreen = (p) => {
                const ndc = Camera3d.project(live, p, aspect);
                if (!(ndc[2] > -1 && ndc[2] < 1)) return null;
                return [(ndc[0] + 1) / 2 * rect.width, (1 - ndc[1]) / 2 * rect.height];
            };
            // A position whose camera is behind this view, or outside it, is
            // pinned to the edge in its direction, so every stop on the path
            // stays reachable from the overlay.
            const toEdge = (p) => {
                const r = Interp.rotate(live.q, p);
                const v = [r[0] - live.pan[0], r[1] - live.pan[1]];
                const n = Math.hypot(v[0], v[1]) || 1;
                const cx = rect.width / 2;
                const cy = rect.height / 2;
                const k = Math.min((cx - 18) / Math.max(1e-6, Math.abs(v[0] / n)),
                                   (cy - 18) / Math.max(1e-6, Math.abs(v[1] / n)));
                return { point: [cx + (v[0] / n) * k, cy - (v[1] / n) * k], edge: true };
            };
            const place = (p) => {
                const screen = toScreen(p);
                if (screen && screen[0] >= 12 && screen[0] <= rect.width - 12
                        && screen[1] >= 12 && screen[1] <= rect.height - 12) return { point: screen, edge: false };
                return toEdge(p);
            };
            const e = renderer.extent?.() || [1, 1, 1];
            const corners = [];
            for (const x of [-0.5, 0.5]) for (const y of [-0.5, 0.5]) for (const z of [-0.5, 0.5]) {
                corners.push([x * e[0], y * e[1], z * e[2]]);
            }
            const edges = [[0, 1], [0, 2], [0, 4], [1, 3], [1, 5], [2, 3], [2, 6], [3, 7], [4, 5], [4, 6], [5, 7], [6, 7]];
            for (const [a, b] of edges) {
                const pa = toScreen(corners[a]);
                const pb = toScreen(corners[b]);
                if (pa && pb) overlay.appendChild(svg("line", { class: "vs-path-box", x1: pa[0], y1: pa[1], x2: pb[0], y2: pb[1] }));
            }
            // The path the eye travels, sampled, and each position's eye.
            const frames = existing.keyframes.filter((k) => k.value && k.value.q);
            if (frames.length > 1) {
                const points = [];
                const first = frames[0].t;
                const last = frames[frames.length - 1].t;
                for (let i = 0; i <= 64; i += 1) {
                    const pose = Camera3d.pathAt(frames, first + (last - first) * i / 64, existing.interp || {});
                    const p = toScreen(Camera3d.eye(pose));
                    if (p) points.push(p);
                }
                if (points.length > 1) {
                    overlay.appendChild(svg("polyline", { class: "vs-path-curve",
                                                          points: points.map((p) => p.join(",")).join(" ") }));
                }
            }
            frames.forEach((kf, i) => {
                const at = place(Camera3d.eye(kf.value));
                handle(at.point, i, at.edge);
            });
        }

        function draw2d(existing, rect) {
            const iv = env.iv;
            const item = global.PlexoraViewerScene?.referenceItem?.(iv);
            if (!item?.imageToViewerElementCoordinates) return;
            const aspect = controller.aspect();
            const OSD = global.OpenSeadragon;
            existing.keyframes.forEach((kf, i) => {
                if (!kf.value || kf.value.w === undefined) return;
                const r = global.PlexoraVideoCamera2d.rect(kf.value, aspect);
                const a = item.imageToViewerElementCoordinates(new OSD.Point(r.x, r.y));
                const b = item.imageToViewerElementCoordinates(new OSD.Point(r.x + r.width, r.y + r.height));
                overlay.appendChild(svg("rect", { class: "vs-path-rect", x: Math.min(a.x, b.x), y: Math.min(a.y, b.y),
                                                  width: Math.abs(b.x - a.x), height: Math.abs(b.y - a.y) }));
                handle([Math.min(a.x, b.x) + 12, Math.min(a.y, b.y) + 12], i);
            });
        }

        function handle(point, index, edge = false) {
            if (!point) return;
            const g = svg("g", { class: `vs-path-handle${index === selected ? " is-selected" : ""}`
                                     + `${edge ? " is-edge" : ""}`,
                                 transform: `translate(${point[0]},${point[1]})`, role: "button",
                                 "aria-label": `Position ${index + 1}` });
            g.appendChild(svg("circle", { r: 9 }));
            const text = svg("text", { y: 3.5 });
            text.textContent = String(index + 1);
            g.appendChild(text);
            g.addEventListener("click", (event) => {
                event.stopPropagation();
                select(index);
            });
            overlay.appendChild(g);
        }

        function toggle(on = !active) {
            active = Boolean(on);
            if (active) {
                if (controller.timeline?.active) controller.timeline.toggle(false);
                mountOverlay();
                render();
            } else {
                unmountOverlay();
                controller.showPreset();
                controller.bar.setHint(controller.preset()?.summary || "");
            }
        }

        return {
            toggle, addKey, render,
            setProject() { if (active) render(); },
            get active() { return active; },
            destroy() { active = false; unmountOverlay(); },
        };
    }

    global.PlexoraVideoPathEditor = { create };
})(typeof window !== "undefined" ? window : globalThis);
