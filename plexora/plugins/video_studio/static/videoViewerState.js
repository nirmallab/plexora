/**
 * videoViewerState.js - what the user had on screen, kept and given back.
 *
 * Preview and export both drive the live viewer, so before either touches it
 * the user's state is snapshotted, and afterwards -- on finishing, on Stop, on
 * an error, on the page going away -- it is put back exactly: the plane,
 * timepoint, mode, layout and slab; the 2-D view and its rotation; the 3-D
 * camera and area; every channel's marker, colour, window and on/off; the cell
 * layers and layers. Nothing is written to disk on the way in or out: the
 * view state with `save: false`, the channels inside the sidebar's suspended
 * persistence (held by the coordinator), the camera straight onto the
 * renderer.
 *
 * Channels go back through `applyLaunchChannels` with their RAW windows when a
 * video changed any window, because a window set through the slider marks the
 * slot user-adjusted; when only colours and on/off changed, the slots are put
 * back one setter at a time, which leaves an auto-levelled slot auto-levelled.
 *
 * Classic script: `window.PlexoraVideoViewerState`.
 */
(function (global) {
    "use strict";

    const Scene = () => global.PlexoraVideoSceneState;

    function snapshot(env) {
        const vs = env.vs;
        const renderer = env.renderer;
        const iv = env.iv;
        const sidebar = env.sidebar;
        const slots = (sidebar?.channelSlots || []).map((slot) => ({
            name: slot.name, colorHex: slot.colorHex, enabled: Boolean(slot.enabled),
            range: slot.range ? slot.range.slice() : null,
            raw: slot.name ? Scene().rawRange(env, slot) : null,
            userColorChanged: Boolean(slot.userColorChanged),
            userRangeChanged: Boolean(slot.userRangeChanged),
        }));
        let cellLayers = [];
        try {
            cellLayers = (iv?.cellLayers?.() || []).map((l) => ({
                name: l.name, mode: l.mode, opacity: l.opacity, visible: l.visible }));
        } catch (error) {
            cellLayers = [];
        }
        let layers = [];
        try {
            layers = (env.ctx?.layers?.list?.() || []).map((l) => ({
                id: l.id, visible: l.visible, opacity: l.opacity }));
        } catch (error) {
            layers = [];
        }
        return {
            view: vs ? vs.get() : null,
            viewport: global.PlexoraViewerScene?.currentViewport?.(iv, env.config) || null,
            transform: iv?.viewTransform?.get?.() || null,
            camera: renderer?.camera ? { q: Array.from(renderer.camera.q), dist: renderer.camera.dist,
                                         panX: renderer.camera.panX, panY: renderer.camera.panY } : null,
            clip: renderer ? (renderer.clipXY ? JSON.parse(JSON.stringify(renderer.clipXY)) : null) : undefined,
            box: renderer ? (renderer.box ? { ...renderer.box } : null) : undefined,
            lock: renderer?.lock ?? null,
            hd: Boolean(sidebar?.isHdMode?.()),
            slots,
            cellLayers,
            layers,
        };
    }

    async function restoreChannels(env, saved) {
        const sidebar = env.sidebar;
        if (!sidebar || !saved) return;
        const live = sidebar.channelSlots || [];
        const sameSlots = live.length === saved.slots.length
            && live.every((slot, i) => slot.name === saved.slots[i].name);
        if (!sameSlots || env.touchedWindows) {
            const rows = saved.slots.filter((s) => s.name).map((s) => ({
                name: s.name, color: s.colorHex, enabled: s.enabled,
                ...(s.userRangeChanged && s.raw ? { range: s.raw } : {}),
            }));
            if (typeof sidebar.applyLaunchChannels === "function") {
                await sidebar.applyLaunchChannels(rows, { silent: true });
            }
            // A window the user had set by hand goes back to exactly that, and
            // an auto-levelled one stays theirs to re-level -- as before.
            return;
        }
        live.forEach((slot, i) => {
            const was = saved.slots[i];
            if (!slot.name) return;
            if (Boolean(slot.enabled) !== was.enabled) sidebar.setSlotEnabled(i, was.enabled);
            if (String(slot.colorHex).toLowerCase() !== String(was.colorHex).toLowerCase()) {
                sidebar.setSlotColor(i, was.colorHex, was.userColorChanged);
            }
        });
    }

    /** Put everything back. Idempotent and never throws: it runs from
     *  finally blocks and from pagehide. */
    async function restore(env, saved) {
        if (!saved) return;
        const errors = [];
        const step = async (name, fn) => {
            try {
                await fn();
            } catch (error) {
                errors.push(`${name}: ${error?.message || error}`);
            }
        };
        await step("channels", () => restoreChannels(env, saved));
        await step("view", async () => {
            if (env.vs && saved.view) {
                const s = saved.view;
                env.vs.set({ t: s.t, z: s.z, mode: s.mode, layout: s.layout, slab: s.slab,
                             section: s.section, volume: s.volume }, { save: false });
                await env.vs.settled();
            }
        });
        await step("viewport", () => {
            const iv = env.iv;
            if (!iv || !saved.viewport) return;
            if (saved.transform) {
                iv.viewTransform?.set?.(saved.transform, { immediately: true, save: false });
            }
            const v = saved.viewport;
            global.PlexoraViewerScene?.fitRegion?.(iv, { x: v.x, y: v.y, width: v.w, height: v.h },
                                                    { immediately: true, config: env.config });
        });
        await step("camera", () => {
            const renderer = env.renderer;
            if (!renderer) return;
            if (!saved.camera) {
                // The renderer was built by the video: give it the opening view
                // a first visit to 3-D gets, not the video's last pose.
                const iso = global.PlexoraVolumeCamera?.VIEWS?.iso;
                if (iso) {
                    renderer.stopMotion?.();
                    renderer.zoomTarget = null;
                    Object.assign(renderer.camera, { q: Array.from(iso), dist: 2.2, panX: 0, panY: 0 });
                    renderer.setClip?.(null);
                    renderer.invalidate?.();
                }
                return;
            }
            renderer.stopMotion?.();
            renderer.zoomTarget = null;
            Object.assign(renderer.camera, { q: saved.camera.q.slice(), dist: saved.camera.dist,
                                             panX: saved.camera.panX, panY: saved.camera.panY });
            if (saved.box !== undefined
                    && JSON.stringify(renderer.box || null) !== JSON.stringify(saved.box || null)) {
                renderer.box = saved.box;
                renderer.refresh?.();
            }
            if (saved.clip !== undefined) renderer.setClip?.(saved.clip);
            renderer.invalidate?.();
        });
        await step("layers", () => {
            const iv = env.iv;
            for (const layer of saved.cellLayers) {
                const current = (iv?.cellLayers?.() || []).find((l) => l.name === layer.name);
                if (!current) continue;
                if (current.mode !== layer.mode) iv.setCellLayerMode(layer.name, layer.mode);
                if (current.opacity !== layer.opacity) iv.setLayerOpacity(layer.name, layer.opacity);
                if (current.visible !== layer.visible) iv.setCellLayerVisible(layer.name, layer.visible);
            }
            const api = env.ctx?.layers;
            const records = new Map((api?.list?.() || []).map((l) => [l.id, l]));
            for (const layer of saved.layers) {
                const current = records.get(layer.id);
                if (!current) continue;
                if (current.visible !== layer.visible) api.setVisible(layer.id, layer.visible);
                if (current.opacity !== layer.opacity) api.setOpacity(layer.id, layer.opacity);
            }
        });
        env.touchedWindows = false;
        if (errors.length) console.warn("Video Studio: the viewer was restored with problems", errors);
    }

    /** A comparable digest of the parts restore puts back (probes and the
     *  integration test compare before and after). */
    function digest(saved) {
        const round = (v) => (typeof v === "number" ? Math.round(v * 1e4) / 1e4 : v);
        const clean = (value) => JSON.parse(JSON.stringify(value, (k, v) => round(v)));
        // The camera matters only while the 3-D view is showing (a renderer
        // built later by a video is reset to its opening view on restore).
        const camera = saved.view?.mode === "volume" ? saved.camera : null;
        return clean({ view: saved.view && { t: saved.view.t, z: saved.view.z, mode: saved.view.mode,
                                            layout: saved.view.layout, slab: saved.view.slab,
                                            inspect: saved.view.inspect || null,
                                            section: saved.view.section || null },
                       viewport: saved.viewport, transform: saved.transform, camera,
                       slots: saved.slots.map((s) => ({ name: s.name, colorHex: s.colorHex,
                                                        enabled: s.enabled })),
                       cellLayers: saved.cellLayers, layers: saved.layers });
    }

    global.PlexoraVideoViewerState = { snapshot, restore, digest };
})(typeof window !== "undefined" ? window : globalThis);
