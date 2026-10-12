/**
 * videoRenderCoordinator.js - a render session: the viewer borrowed, and given back.
 *
 * `open(env, settings)` takes the viewer for an export:
 *
 *   1. the user's state is snapshotted (videoViewerState.js);
 *   2. the sidebar's persistence is suspended, motion is held off
 *      (viewerPrefs.hold: no plane fades, no eased zooms, no coasting 3-D),
 *      the viewer's own neighbour prefetch is paused;
 *   3. the stage is raised (videoStage.js: the viewer at output size) and the
 *      render cover goes over it;
 *   4. the page's data requests are counted for the readiness barrier.
 *
 * Then, per frame, `frame(state)` applies it, waits for the barrier (once
 * retrying with a forced redraw), and composes the output canvas.
 *
 * `close()` undoes all of it in reverse, whatever happened -- finished,
 * stopped, failed, or the page going away (`pagehide`). It is idempotent.
 *
 * Classic script: `window.PlexoraVideoRenderCoordinator`.
 */
(function (global) {
    "use strict";

    const Scene = () => global.PlexoraVideoSceneState;
    const Readiness = () => global.PlexoraVideoReadiness;

    async function open(env, settings) {
        const { width, height, transparent = false, overlays = {}, onCancel = null } = settings;
        const saved = global.PlexoraVideoViewerState.snapshot(env);
        // Named, so close() can undo them in the order the restore needs.
        const cleanup = {};
        const session = {
            env, saved, cleanup, width, height, aspect: width / height, retries: 0, unresolved: [],
            waits: [], closed: false, settings: { transparent, overlays },
        };
        try {
            env.sidebar?.suspendPersistence?.();
            cleanup.persistence = () => env.sidebar?.resumePersistence?.();
            cleanup.motion = global.PlexoraViewerPrefs?.hold?.() || null;
            const prefetch = env.planeMotion?.prefetch;
            if (prefetch?.pause) {
                prefetch.pause(true);
                cleanup.prefetch = () => prefetch.pause(false);
            }
            const network = Readiness().trackNetwork();
            env.network = network.tracker;
            cleanup.network = () => {
                network.stop();
                env.network = null;
            };
            session.stage = global.PlexoraVideoStage.enter(env, { width, height });
            cleanup.stage = () => session.stage.exit();
            await global.PlexoraVideoStage.settleLayout();
            global.PlexoraVideoStage.resize(env);
            session.cover = global.PlexoraVideoRenderCover.mount(env.wrapper, { onCancel });
            cleanup.cover = () => session.cover.remove();
            session.composer = global.PlexoraVideoComposite.create();
            session.prefetch = global.PlexoraVideoPrefetch.create(env);
            const onLeave = () => { close(session); };
            global.addEventListener("pagehide", onLeave);
            cleanup.pagehide = () => global.removeEventListener("pagehide", onLeave);
        } catch (error) {
            await close(session);
            throw error;
        }
        return session;
    }

    /**
     * Apply `state`, wait until the viewer shows it completely, compose the
     * output. Resolves `{canvas, drawn, waitedMs}`. Throws ReadinessTimeout
     * (after one retry) or AbortError.
     */
    async function frame(session, state, { signal = null, timeoutMs = 30000, extra = {} } = {}) {
        const env = session.env;
        const volume = state.view.mode === "volume";
        const result = await Scene().apply(env, state, { aspect: session.aspect, settle: true });
        if (volume) {
            // A renderer built by this frame's revision (the first 3-D frame)
            // is pinned to the output now. Detail is asked for once the
            // renderer has its level -- refine() does nothing before -- so the
            // first frame of a cold renderer is as detailed as every other.
            session.stage?.pinRenderer();
            const start = Date.now();
            while (!(env.renderer && ["ready", "error", "lost"].includes(env.renderer.status))) {
                if (signal?.aborted) throw new DOMException("stopped", "AbortError");
                if (Date.now() - start > timeoutMs) break;
                await new Promise((resolve) => setTimeout(resolve, 16));
            }
            const renderer = env.renderer;
            // A channel switched on by this frame has no texture yet: fetch it
            // now rather than wait for an event to.
            if (renderer?.channels && renderer.channels().some((c) => !renderer.textureOf(c))) {
                renderer.refresh?.();
            }
            renderer?.refine?.();
        }
        if (result.missingChannels?.length && !session.warnedMissing) {
            session.warnedMissing = true;
            session.missingChannels = result.missingChannels;
        }
        const options = { volume, signal, timeoutMs, frame: state,
                          onBlocked: (blocked) => {
                              if (blocked.some((b) => b.endsWith("-coarse"))) {
                                  session.prefetch.prepareSections();
                              }
                          } };
        let waited;
        try {
            waited = await Readiness().wait(env, options);
        } catch (error) {
            if (error?.name !== "ReadinessTimeout") throw error;
            // One forced retry: re-apply, rebuild the panes, redraw.
            session.retries += 1;
            env.panes?.rebuild?.();
            env.renderer?.refresh?.();
            env.renderer?.invalidate?.();
            env.renderer?.refine?.();
            env.iv?.viewer?.forceRedraw?.();
            await Scene().apply(env, state, { aspect: session.aspect, settle: true });
            waited = await Readiness().wait(env, options);
        }
        // Verify before capturing: something can change a channel after the
        // frame set it and before the barrier looked (the sidebar auto-levels
        // a slot asynchronously). Re-applying the frame's channels changes
        // nothing when all is as asked; if it changed anything, the frame waits
        // for the barrier again.
        for (let pass = 0; pass < 3; pass += 1) {
            const before = env.channelEdits || 0;
            Scene().applyChannels(env, state.channels);
            Scene().applyLayers(env, state);
            if ((env.channelEdits || 0) === before) break;
            session.reapplied = (session.reapplied || 0) + 1;
            waited = await Readiness().wait(env, options);
        }
        session.waits.push(waited.waitedMs);
        const { canvas, drawn } = session.composer.compose(env, state, {
            width: session.width, height: session.height, transparent: session.settings.transparent,
            overlays: session.settings.overlays, facts: env.facts, datasetName: extra.datasetName,
            attribution: extra.attribution, disclose: Boolean(extra.disclose),
        });
        return { canvas, drawn, waitedMs: waited.waitedMs };
    }

    async function close(session) {
        if (!session || session.closed) return;
        session.closed = true;
        const cleanup = session.cleanup || {};
        const run = (name) => {
            const fn = cleanup[name];
            cleanup[name] = null;
            if (!fn) return;
            try {
                fn();
            } catch (error) {
                console.warn(`Video Studio: could not undo ${name}`, error);
            }
        };
        // The cover and the stage first, so the user's view is restored at
        // the viewer's own size; motion and persistence last, so the restore
        // itself neither animates nor saves (nothing the user had changed).
        for (const name of ["pagehide", "cover", "stage", "network", "prefetch"]) run(name);
        // The viewer back at its own size before its view is restored: a view
        // fitted to the stage's size would come back zoomed wrong.
        await global.PlexoraVideoStage.settleLayout();
        global.PlexoraVideoStage.resize(session.env);
        await global.PlexoraVideoViewerState.restore(session.env, session.saved);
        run("motion");
        run("persistence");
    }

    global.PlexoraVideoRenderCoordinator = { open, frame, close };
})(typeof window !== "undefined" ? window : globalThis);
