/**
 * videoPreview.js - the video playing on the live viewer, as it will export.
 *
 * Preview uses the export's own evaluator and the same `apply`, so what plays
 * is what exports; it differs only in pace. It runs on the display's clock
 * (requestAnimationFrame) and never waits for the viewer to settle, so a
 * fast move may show coarse tiles that the export would wait for.
 *
 * A preview SESSION starts at the first play or scrub: the user's state is
 * snapshotted, the sidebar's persistence suspended and motion held off (a
 * z-sweep previews as whole steps, as it exports). `end()` gives the viewer
 * back exactly. While paused the viewer is live -- the user can move it to
 * choose an end view -- and the next play applies frames again.
 *
 * In 3-D the renderer draws its fast, lighter frames while playing and a full
 * frame when paused.
 *
 * Classic script: `window.PlexoraVideoPreview`.
 */
(function (global) {
    "use strict";

    function create(env, { getProject, getAspect, onTime, onPlayState, onSessionChange } = {}) {
        let session = null;
        let playing = false;
        let raf = 0;
        let anchorWall = 0;
        let anchorT = 0;
        let time = 0;
        let loop = true;
        let cache = {};
        let cacheFor = null;

        const now = () => (global.performance?.now?.() ?? Date.now());

        function begin() {
            if (session) return session;
            const saved = global.PlexoraVideoViewerState.snapshot(env);
            env.sidebar?.suspendPersistence?.();
            const release = global.PlexoraViewerPrefs?.hold?.() || null;
            session = { saved, release };
            onSessionChange?.(true);
            return session;
        }

        async function end() {
            pause();
            unmountOverlay();
            if (!session) return;
            const { saved, release } = session;
            session = null;
            await global.PlexoraVideoViewerState.restore(env, saved);
            release?.();
            env.sidebar?.resumePersistence?.();
            onSessionChange?.(false);
        }

        function stateAt(t) {
            const project = getProject?.();
            if (!project) return null;
            if (cacheFor !== project) {
                cache = {};
                cacheFor = project;
            }
            return global.PlexoraVideoAnimation.evaluate(project, t, { cache });
        }

        // -- the overlay over the live viewer ---------------------------------------

        let overlay = null;

        // While paused the viewer is live: once the user moves it (to choose an
        // end view, say) the frame guide no longer matches, so it is cleared
        // until the next frame is shown.
        const clearOnTouch = () => {
            if (playing || !overlay) return;
            overlay.getContext("2d").clearRect(0, 0, overlay.width, overlay.height);
        };

        function mountOverlay() {
            if (overlay || !env.wrapper) return;
            overlay = document.createElement("canvas");
            overlay.className = "vs-preview-overlay";
            overlay.setAttribute("aria-hidden", "true");
            overlay.setAttribute("data-viewer-furniture", "");
            env.wrapper.appendChild(overlay);
            env.wrapper.addEventListener("pointerdown", clearOnTouch, true);
            env.wrapper.addEventListener("wheel", clearOnTouch, { capture: true, passive: true });
        }

        function unmountOverlay() {
            if (!overlay) return;
            env.wrapper?.removeEventListener("pointerdown", clearOnTouch, true);
            env.wrapper?.removeEventListener("wheel", clearOnTouch, { capture: true });
            overlay.remove();
            overlay = null;
        }

        /** The output's rectangle on screen, in CSS pixels of the wrapper. */
        function frameOnScreen(state, aspect) {
            const box = env.wrapper.getBoundingClientRect();
            if (state.view.mode === "volume") {
                // The renderer's vertical field of view is fixed, so the
                // output shows the wrapper's height at the output's aspect.
                let height = box.height;
                let width = height * aspect;
                if (width > box.width) {
                    width = box.width;
                    height = width / aspect;
                }
                return { x: (box.width - width) / 2, y: (box.height - height) / 2, width, height };
            }
            const item = global.PlexoraViewerScene?.referenceItem?.(env.iv);
            if (!item?.imageToViewerElementCoordinates || !state.viewport || !global.OpenSeadragon) return null;
            const r = global.PlexoraVideoCamera2d.rect(state.viewport, aspect);
            const a = item.imageToViewerElementCoordinates(new global.OpenSeadragon.Point(r.x, r.y));
            const b = item.imageToViewerElementCoordinates(new global.OpenSeadragon.Point(r.x + r.width,
                                                                                       r.y + r.height));
            return { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), width: Math.abs(b.x - a.x),
                     height: Math.abs(b.y - a.y) };
        }

        function drawOverlay(state) {
            mountOverlay();
            if (!overlay) return;
            const box = env.wrapper.getBoundingClientRect();
            const dpr = global.devicePixelRatio || 1;
            const width = Math.round(box.width * dpr);
            const height = Math.round(box.height * dpr);
            if (overlay.width !== width || overlay.height !== height) {
                overlay.width = width;
                overlay.height = height;
            }
            const ctx = overlay.getContext("2d");
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            const aspect = getAspect?.() || 16 / 9;
            const frame = frameOnScreen(state, aspect);
            const bbox = state.emphasis?.bbox;
            const spotlight = bbox && frame
                ? global.PlexoraVideoOverlays.bboxInFrame(state, bbox, { width: frame.width, height: frame.height })
                : null;
            global.PlexoraVideoOverlays.drawPreview(ctx, { width: box.width, height: box.height }, state, frame,
                                                    { spotlight });
        }

        function show(t) {
            const state = stateAt(t);
            if (!state) return;
            try {
                global.PlexoraVideoSceneState.applyNow(env, state, { aspect: getAspect?.() || 16 / 9 });
                drawOverlay(state);
            } catch (error) {
                console.error("Video Studio: a preview frame could not be shown", error);
            }
        }

        function frame() {
            raf = 0;
            if (!playing) return;
            const project = getProject?.();
            if (!project) return;
            const duration = project.duration_s;
            let t = anchorT + (now() - anchorWall) / 1000;
            if (t >= duration) {
                if (loop) {
                    // Playback repeats; a seamless-loop video's frame at
                    // `duration` is its first, so wrapping shows no seam.
                    t %= duration;
                    anchorT = t;
                    anchorWall = now();
                } else {
                    t = duration;
                    time = t;
                    show(t);
                    onTime?.(t);
                    pause();
                    return;
                }
            }
            time = t;
            show(t);
            onTime?.(t);
            raf = global.requestAnimationFrame(frame);
        }

        function setInteracting(on) {
            const renderer = env.renderer;
            if (!renderer) return;
            renderer.interacting = on;
            if (!on) renderer.invalidate?.();
        }

        function play() {
            const project = getProject?.();
            if (!project || playing) return;
            begin();
            if (time >= project.duration_s - 1e-6) time = 0;
            anchorT = time;
            anchorWall = now();
            playing = true;
            setInteracting(true);
            onPlayState?.(true);
            raf = global.requestAnimationFrame(frame);
        }

        function pause() {
            if (raf) global.cancelAnimationFrame(raf);
            raf = 0;
            if (playing) {
                playing = false;
                setInteracting(false);
                onPlayState?.(false);
            }
        }

        function seek(t, { show: draw = true } = {}) {
            const project = getProject?.();
            if (!project) return;
            time = Math.max(0, Math.min(project.duration_s, t));
            anchorT = time;
            anchorWall = now();
            if (draw) {
                begin();
                show(time);
            }
            onTime?.(time);
        }

        /** Keep the same place when the project changes length: the same
         *  fraction of the way through. */
        function rescale(oldDuration, newDuration) {
            if (!(oldDuration > 0)) return;
            seek((time / oldDuration) * newDuration, { show: Boolean(session) });
        }

        return {
            play, pause, seek, end, begin, rescale, show,
            toggle() { if (playing) pause(); else play(); },
            step(dt) { pause(); seek(time + dt); },
            get playing() { return playing; },
            get time() { return time; },
            get active() { return Boolean(session); },
            get loop() { return loop; },
            set loop(value) { loop = Boolean(value); },
            invalidate() { cache = {}; cacheFor = null; },
        };
    }

    global.PlexoraVideoPreview = { create };
})(typeof window !== "undefined" ? window : globalThis);
