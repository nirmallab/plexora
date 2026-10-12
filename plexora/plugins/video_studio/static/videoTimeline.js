/**
 * videoTimeline.js - every track of the video on one timeline.
 *
 * Hidden until asked for (T, or More > Timeline): a band along the bottom of
 * the canvas, reserved through the canvas layout (`ctx.view.reserve`) so the
 * image, the legend and the depth strip move out of its way rather than sit
 * under it. One row per track -- camera, planes, time, channels, captions --
 * with procedural moves drawn as a span and keyframes as diamonds.
 *
 *   drag a diamond        move it (snaps to 0.1 s; Shift for free)
 *   Alt-drag              a copy
 *   click / Shift-click   select / add to the selection
 *   Delete                remove the selection
 *   double-click          the keyframe's pace
 *   the ruler             scrub (the viewer follows)
 *   Capture viewer state  a keyframe of everything on screen at the playhead
 *   wheel over the lanes  zoom the time scale
 *
 * Planes, timepoints, slabs and the view mode are acquired data: their rows
 * show steps (amber), and a keyframe there can only hold a whole value -- the
 * server refuses anything else, and the timeline says so.
 *
 * Every edit is an operation batch through the controller (undoable).
 *
 * Classic script: `window.PlexoraVideoTimeline`.
 */
(function (global) {
    "use strict";

    const HEIGHT = 168;
    const SNAP = 0.1;
    const EASES = [["linear", "Even"], ["sine_in_out", "Gentle"], ["cubic_in_out", "Smooth"],
                   ["cinematic", "Cinematic"], ["step", "Hold"]];

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

    function icon(name) {
        return el("span", `fas fa-${name}`, { "aria-hidden": "true" });
    }

    function create(controller) {
        let active = false;
        let root = null;
        let release = null;
        let selection = new Set();
        let scale = null;                // px per second
        let lanesWidth = 0;
        let playhead = null;
        let undoButton = null;
        let redoButton = null;
        let snapOn = true;

        const Schema = global.PlexoraVideoSchema;

        function project() {
            return controller.project;
        }

        function x(t) {
            return t * scale;
        }

        function timeAt(px, free) {
            const t = Math.max(0, Math.min(project().duration_s, px / scale));
            return free || !snapOn ? t : Math.round(t / SNAP) * SNAP;
        }

        function trackLabel(track) {
            const kind = Schema.TRACK_KINDS[track.kind];
            const base = track.label || kind?.label || track.kind;
            if (track.kind === "channel") {
                const facts = controller.facts?.channels || [];
                return facts.find((c) => c.key === track.target)?.name || track.target;
            }
            return track.muted ? `${base} (muted)` : base;
        }

        function mount() {
            if (root) return;
            release = controller.ctx.view?.reserve?.("timeline", { edge: "bottom", size: HEIGHT, order: 5 }) || null;
            root = el("section", "vs-timeline", { "aria-label": "Video timeline", "data-viewer-furniture": true });
            root.style.setProperty("--plx-band-timeline-size", `${HEIGHT}px`);
            const head = el("div", "vs-timeline-head");
            head.appendChild(el("span", "vs-timeline-title", { text: "Timeline" }));
            const capture = el("button", "vs-chip", { type: "button", title: "A keyframe of everything on screen at the playhead (K)" });
            capture.append(icon("location-crosshairs"), el("span", null, { text: " Capture viewer state" }));
            capture.addEventListener("click", () => addKey());
            const snap = el("button", "vs-icon-button", { type: "button", "aria-pressed": "true",
                                                         title: "Snap to 0.1 s", "aria-label": "Snap to 0.1 s" });
            snap.appendChild(icon("magnet"));
            snap.addEventListener("click", () => {
                snapOn = !snapOn;
                snap.setAttribute("aria-pressed", String(snapOn));
            });
            undoButton = el("button", "vs-icon-button", { type: "button", "aria-label": "Undo", title: "Undo (⌘Z)" });
            undoButton.appendChild(icon("rotate-left"));
            undoButton.addEventListener("click", () => controller.undo());
            redoButton = el("button", "vs-icon-button", { type: "button", "aria-label": "Redo", title: "Redo (⇧⌘Z)" });
            redoButton.appendChild(icon("rotate-right"));
            redoButton.addEventListener("click", () => controller.redo());
            const close = el("button", "vs-icon-button", { type: "button", "aria-label": "Close timeline", title: "Close timeline (T)" });
            close.appendChild(icon("xmark"));
            close.addEventListener("click", () => toggle(false));
            head.append(capture, snap, el("span", "vs-timeline-spacer"), undoButton, redoButton, close);
            root.appendChild(head);
            const body = el("div", "vs-timeline-body");
            root.appendChild(body);
            root._body = body;
            body.addEventListener("wheel", (event) => {
                if (!event.ctrlKey && !event.metaKey && Math.abs(event.deltaY) < Math.abs(event.deltaX)) return;
                event.preventDefault();
                const factor = event.deltaY > 0 ? 0.85 : 1.18;
                scale = Math.max(8, Math.min(800, (scale || 40) * factor));
                render();
            }, { passive: false });
            controller.env.wrapper.appendChild(root);
            setHistory({ canUndo: controller.history.canUndo, canRedo: controller.history.canRedo });
        }

        function unmount() {
            root?.remove();
            root = null;
            release?.();
            release = null;
        }

        function render() {
            if (!root || !project()) return;
            const body = root._body;
            body.replaceChildren();
            const labelWidth = 120;
            lanesWidth = Math.max(200, root.clientWidth - labelWidth - 12);
            const fit = lanesWidth / Math.max(0.5, project().duration_s);
            if (!scale || scale < fit * 0.999) scale = fit;
            // Ruler.
            body.appendChild(el("div", "vs-track-label", { text: "" }));
            const ruler = el("div", "vs-ruler", { role: "slider", tabindex: "0", "aria-label": "Playhead",
                                                 "aria-valuemin": "0", "aria-valuemax": String(project().duration_s) });
            ruler.style.width = `${x(project().duration_s)}px`;
            const step = niceStep(project().duration_s, lanesWidth);
            for (let t = 0; t <= project().duration_s + 1e-9; t += step) {
                const tick = el("span", "vs-ruler-tick", { text: `${+t.toFixed(2)}s` });
                tick.style.left = `${x(t)}px`;
                ruler.appendChild(tick);
            }
            playhead = el("div", "vs-playhead");
            ruler.appendChild(playhead);
            const seek = (event) => {
                const r = ruler.getBoundingClientRect();
                controller.preview.pause();
                controller.preview.seek(timeAt(event.clientX - r.left, true));
            };
            ruler.addEventListener("pointerdown", (event) => {
                ruler.setPointerCapture?.(event.pointerId);
                seek(event);
                const move = (e) => seek(e);
                const up = () => {
                    ruler.removeEventListener("pointermove", move);
                    ruler.removeEventListener("pointerup", up);
                };
                ruler.addEventListener("pointermove", move);
                ruler.addEventListener("pointerup", up);
            });
            body.appendChild(ruler);
            for (const track of project().tracks) {
                const label = el("div", "vs-track-label", { title: `${track.kind}${track.target ? ` · ${track.target}` : ""}` });
                label.textContent = trackLabel(track);
                const lane = el("div", "vs-track-lane", { "data-track": track.track_id });
                lane.style.width = `${x(project().duration_s)}px`;
                const step = Schema.isStepKind(track.kind);
                if (track.mode === "procedural") {
                    const span = el("div", `vs-track-range${step ? " is-step" : ""}`,
                                    { title: track.procedural?.generator || "" });
                    span.style.left = `${x(track.range[0])}px`;
                    span.style.width = `${Math.max(2, x(track.range[1] - track.range[0]))}px`;
                    lane.appendChild(span);
                } else if (track.keyframes.length) {
                    const first = track.keyframes[0].t;
                    const last = track.keyframes[track.keyframes.length - 1].t;
                    const span = el("div", `vs-track-range${step ? " is-step" : ""}`);
                    span.style.left = `${x(first)}px`;
                    span.style.width = `${Math.max(2, x(last - first))}px`;
                    lane.appendChild(span);
                    track.keyframes.forEach((kf) => lane.appendChild(diamond(track, kf)));
                }
                body.append(label, lane);
            }
            setTime(controller.preview.time);
        }

        function niceStep(duration, width) {
            const target = duration / Math.max(1, width / 70);
            for (const s of [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300]) if (s >= target) return s;
            return 600;
        }

        function diamond(track, kf) {
            const key = `${track.track_id}/${kf.kf_id}`;
            const button = el("button", `vs-key${selection.has(key) ? " is-selected" : ""}`, {
                type: "button", "aria-label": `Keyframe at ${kf.t.toFixed(2)} s`, title: `${kf.t.toFixed(2)} s · ${kf.ease}` });
            button.style.left = `${x(kf.t)}px`;
            button.addEventListener("pointerdown", (event) => drag(event, track, kf, key, button));
            button.addEventListener("dblclick", (event) => {
                event.stopPropagation();
                easeMenu(button, track, kf);
            });
            return button;
        }

        function drag(event, track, kf, key, button) {
            event.preventDefault();
            event.stopPropagation();
            if (event.shiftKey) {
                if (selection.has(key)) selection.delete(key);
                else selection.add(key);
            } else if (!selection.has(key)) {
                selection = new Set([key]);
            }
            const lane = button.parentElement;
            const start = event.clientX;
            const startT = kf.t;
            const copy = event.altKey;
            let moved = false;
            button.setPointerCapture?.(event.pointerId);
            const move = (e) => {
                const dx = e.clientX - start;
                if (Math.abs(dx) > 2) moved = true;
                const t = timeAt(x(startT) + dx, e.shiftKey);
                button.style.left = `${x(t)}px`;
                button.dataset.t = String(t);
            };
            const up = async (e) => {
                button.removeEventListener("pointermove", move);
                button.removeEventListener("pointerup", up);
                void lane;
                if (!moved) {
                    controller.preview.pause();
                    controller.preview.seek(kf.t);
                    render();
                    return;
                }
                const t = Number(button.dataset.t);
                const op = copy
                    ? { op: "add_keyframe", track_id: track.track_id, keyframe: { t, value: kf.value, ease: kf.ease } }
                    : { op: "update_keyframe", track_id: track.track_id, kf_id: kf.kf_id, changes: { t } };
                try {
                    await controller.edit([op], copy ? "Copy keyframe" : "Move keyframe");
                } catch (error) {
                    controller.bar.showError(error.payload?.code === "integrity" ? error.message : `Could not move: ${error.message}`);
                    render();
                }
                void e;
            };
            button.addEventListener("pointermove", move);
            button.addEventListener("pointerup", up);
            render();
        }

        function easeMenu(anchor, track, kf) {
            if (Schema.isStepKind(track.kind)) {
                controller.bar.setHint("Planes, timepoints and slabs change in whole steps; they have no pace.");
                return;
            }
            global.PlexoraMenu?.open(anchor, EASES.map(([value, label]) => ({
                label, checked: kf.ease === value,
                onSelect: () => controller.edit([{ op: "update_keyframe", track_id: track.track_id,
                                                   kf_id: kf.kf_id, changes: { ease: value } }], "Pace"),
            })), { align: "left" });
        }

        async function deleteSelected() {
            if (!selection.size) return;
            const ops = [...selection].map((key) => {
                const [trackId, kfId] = key.split("/");
                return { op: "remove_keyframe", track_id: trackId, kf_id: kfId };
            });
            selection.clear();
            await controller.edit(ops, ops.length > 1 ? "Remove keyframes" : "Remove keyframe");
        }

        /** "Capture viewer state as keyframe": the camera (or 2-D view) and
         *  every channel as they are now, at the playhead. Acquired plane and
         *  time too, as steps. */
        async function addKey() {
            const scene = global.PlexoraVideoSceneState.capture(controller.env);
            const t = Math.round(controller.preview.time * 1000) / 1000;
            const p = project();
            const ops = [];
            const byKind = (kind, target = null) => p.tracks.find((tr) => tr.kind === kind && tr.mode === "keyframes"
                && (target === null || tr.target === target));
            const put = (kind, value, target = null, ease = "sine_in_out") => {
                const existing = byKind(kind, target);
                if (existing) {
                    const same = existing.keyframes.find((k) => Math.abs(k.t - t) < 1e-3);
                    if (same) ops.push({ op: "update_keyframe", track_id: existing.track_id, kf_id: same.kf_id, changes: { value } });
                    else ops.push({ op: "add_keyframe", track_id: existing.track_id, keyframe: { t, value, ease } });
                } else {
                    ops.push({ op: "add_track", track: { kind, target, mode: "keyframes",
                                                         keyframes: [{ t, value, ease }] } });
                }
            };
            if (scene.view.mode === "volume" && scene.camera3d) put("camera3d", scene.camera3d);
            else if (scene.viewport) put("camera2d", scene.viewport);
            if ((controller.facts?.depth || 1) > 1) put("acquired_z", scene.view.z, null, "step");
            if ((controller.facts?.frames || 1) > 1) put("acquired_t", scene.view.t, null, "step");
            for (const c of scene.channels) {
                put("channel", { visible: c.visible, presence: 1, window: c.window, color: c.color }, c.key, "linear");
            }
            await controller.edit(ops, "Capture viewer state");
        }

        function setTime(t) {
            if (playhead) playhead.style.left = `${x(t)}px`;
        }

        function setHistory(status) {
            if (undoButton) undoButton.disabled = !status.canUndo;
            if (redoButton) redoButton.disabled = !status.canRedo;
        }

        function toggle(on = !active) {
            active = Boolean(on);
            if (active) {
                mount();
                render();
                controller.timelineTick = (t) => setTime(t);
            } else {
                unmount();
                controller.timelineTick = null;
            }
        }

        return {
            toggle, addKey, deleteSelected, setHistory, setTime,
            setProject() { if (active) render(); },
            get active() { return active; },
            destroy() { active = false; unmount(); },
        };
    }

    global.PlexoraVideoTimeline = { create };
})(typeof window !== "undefined" ? window : globalThis);
