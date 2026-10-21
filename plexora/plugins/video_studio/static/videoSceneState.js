/**
 * videoSceneState.js - the viewer as a SceneState, and a frame state applied to it.
 *
 * `capture(env)` reads what the viewer shows into the document's SceneState
 * (server/schema.py normalize_scene): the plane, timepoint, view mode and
 * slab; the 2-D view or the 3-D camera; every channel by its stable key
 * (the last segment of its tile URL, as Figure Builder keys them) with its
 * colour and RAW display window; HD mode; the cell layers and layers.
 *
 * `apply(env, state, options)` makes the viewer show a frame state from
 * videoAnimation.js. It persists NOTHING: the view goes through
 * `viewState.set(patch, {save: false})` (one navigation revision per frame,
 * and none at all when the frame changes nothing the revision keys on), the
 * view transform with `save: false`, the camera straight onto the renderer
 * (it is never saved), and channel edits happen while the coordinator holds
 * the sidebar's persistence suspended. Each setter is called only when its
 * value differs from what is on screen, so a frame that changes only the
 * camera touches nothing else.
 *
 * Channel PRESENCE (0..1) is applied as the channel's colour scaled toward
 * black: channels are composited additively in 2-D (and summed or maximised
 * in 3-D), so a channel at presence p contributes p of itself -- a fade that
 * changes no acquired value and no window. Colour itself is held between
 * keyframes (videoAnimation.js), never blended.
 *
 * `env` is the controller's handle on the page (videoStudioSidebarController.js
 * `environment()`): ctx, config, the image viewer, and getters for viewState,
 * the volume renderer, the sidebar, the section panes.
 *
 * Classic script: `window.PlexoraVideoSceneState`.
 */
(function (global) {
    "use strict";

    const Camera2d = () => global.PlexoraVideoCamera2d;

    // -- channels ---------------------------------------------------------------

    function slotKey(env, slot) {
        const sidebar = env.sidebar;
        const fullName = sidebar?.dataLayer?.getFullChannelName?.(slot.name) || slot.name;
        const index = sidebar?.channelIndexOf?.(fullName);
        const entry = Number.isInteger(index) ? env.config?.imageData?.[index] : null;
        const src = String(entry?.src || "").replace(/\/+$/, "");
        return { key: src.split("/").pop() || fullName, fullName, index };
    }

    /** Every named slot, with its key: `[{slot, i, key, fullName}]`. */
    function slots(env) {
        const list = env.sidebar?.channelSlots || [];
        const out = [];
        list.forEach((slot, i) => {
            if (!slot || !slot.name) return;
            out.push({ slot, i, ...slotKey(env, slot) });
        });
        return out;
    }

    function rawRange(env, slot) {
        try {
            const raw = env.sidebar.toRawRangeForSlot(slot);
            return Array.isArray(raw) ? [Number(raw[0]), Number(raw[1])] : null;
        } catch (error) {
            return null;
        }
    }

    /** A raw window in the slot's current domain (bytes, or raw in HD). */
    function domainRange(env, slot, raw) {
        const sidebar = env.sidebar;
        if (!raw) return null;
        if (sidebar.isHdMode?.()) return [raw[0], raw[1]];
        const packet = sidebar.quantWindow?.(slot.name);
        if (!packet) return null;
        return sidebar.rawToByteRange(raw, packet);
    }

    function hexToRgb(hex) {
        const value = /^#?([0-9a-f]{6})$/i.exec(String(hex || ""))?.[1] || "ffffff";
        return [0, 2, 4].map((i) => parseInt(value.slice(i, i + 2), 16));
    }

    function rgbToHex(rgb) {
        return `#${rgb.map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16)
            .padStart(2, "0")).join("")}`;
    }

    /** `hex` at presence `p`: scaled toward black (see the module docstring). */
    function presenceColor(hex, p) {
        const k = Math.max(0, Math.min(1, Number.isFinite(p) ? p : 1));
        return k >= 1 ? String(hex).toLowerCase() : rgbToHex(hexToRgb(hex).map((v) => v * k));
    }

    // -- capture ----------------------------------------------------------------

    function captureView(env) {
        const vs = env.vs;
        if (!vs) return { t: 0, z: 0, mode: "plane", composite: "mip", slab: null,
                          clip: { x: [0, 1], y: [0, 1], z: [0, 1] } };
        const state = vs.get();
        let mode = state.mode;
        if (mode === "plane" && state.layout === "sections") mode = "sections";
        return {
            t: state.t || 0,
            z: state.z || 0,
            mode,
            composite: state.volume?.composite || "mip",
            slab: state.slab ? { z0: state.slab.z0, z1: state.slab.z1, op: state.slab.op || "max" }
                : null,
            clip: { x: [0, 1], y: [0, 1], z: [0, 1] },
            // How the 3-D view is cut (core's services/inspectGeometry.js):
            // the video's cut, frame to frame. The clip track cuts on top of
            // it.
            inspect: state.inspect ? JSON.parse(JSON.stringify(state.inspect)) : null,
        };
    }

    function captureViewport(env) {
        const scene = global.PlexoraViewerScene;
        const iv = env.iv;
        if (!scene || !iv) return null;
        const v = scene.currentViewport(iv, env.config);
        const transform = iv.viewTransform?.get?.() || {};
        return { cx: v.x + v.w / 2, cy: v.y + v.h / 2, w: v.w,
                 degrees: transform.degrees || 0, flip_h: Boolean(transform.flipH),
                 flip_v: Boolean(transform.flipV) };
    }

    function captureCamera(env) {
        const renderer = env.renderer;
        if (!renderer?.camera) return null;
        const c = renderer.camera;
        return { q: Array.from(c.q), dist: c.dist, pan: [c.panX || 0, c.panY || 0] };
    }

    function capture(env) {
        const iv = env.iv;
        const vs = env.vs;
        const view = captureView(env);
        const channels = slots(env).map(({ slot, key, fullName }) => ({
            key, name: fullName, color: slot.colorHex || "#ffffff", window: rawRange(env, slot),
            visible: Boolean(slot.enabled), opacity: 1, presence: 1,
        }));
        let cellLayers = [];
        try {
            cellLayers = (iv?.cellLayers?.() || []).map((layer) => ({
                name: layer.name, mode: layer.mode, opacity: layer.opacity, visible: layer.visible }));
        } catch (error) {
            cellLayers = [];
        }
        let layers = [];
        try {
            layers = (env.ctx?.layers?.list?.() || []).map((layer) => ({
                id: layer.id, visible: layer.visible, opacity: layer.opacity }));
        } catch (error) {
            layers = [];
        }
        const renderer = env.renderer;
        return {
            dims: (vs?.depth || 1) > 1 ? "3d" : "2d",
            view,
            viewport: captureViewport(env),
            camera3d: captureCamera(env),
            // The area the 3-D view draws (the view state's `volume.area`;
            // null for the whole stack), under the scene's old name.
            box: vs?.get?.()?.volume?.area ? { ...vs.get().volume.area } : null,
            channels,
            hd_mode: Boolean(env.sidebar?.isHdMode?.()),
            cell_layers: cellLayers,
            layers,
            plugins: {},
        };
    }

    /** The facts a preset needs, as the live viewer has them. */
    function facts(env) {
        const vs = env.vs;
        const info = vs?.info || {};
        const iv = env.iv;
        const item = global.PlexoraViewerScene?.referenceItem?.(iv);
        const size = item?.source?.dimensions || item?.getContentSize?.();
        const width = Number(info.width || size?.x || 0);
        const height = Number(info.height || size?.y || 0);
        const channels = slots(env).map(({ key, fullName, slot }) => ({
            key, name: fullName, color: slot.colorHex }));
        const all = (env.config?.imageData || []).map((entry) => ({
            key: String(entry.src || "").replace(/\/+$/, "").split("/").pop(),
            name: entry.fullname || entry.name || "" })).filter((c) => c.key);
        const known = new Set(channels.map((c) => c.key));
        return {
            datasource: env.datasource,
            image_size: [width, height],
            depth: vs?.depth || 1,
            frames: vs?.frames || 1,
            spacing: info.spacing || {},
            pixel_size: pixelSize(env, info),
            channels: channels.concat(all.filter((c) => !known.has(c.key))),
        };
    }

    function pixelSize(env, info) {
        const x = info?.spacing?.x;
        if (x && Number(x.value) > 0 && /^(µm|um|micron)/i.test(String(x.unit || "µm"))) {
            return Number(x.value);
        }
        const meta = env.iv?.imgMetadata;
        const value = Number(meta?.physical_size_x);
        return value > 0 ? value : null;
    }

    // -- apply ------------------------------------------------------------------

    /**
     * The viewState patch for a frame's view (and, in 3-D, its area `box`),
     * or null when nothing differs. A new area goes with the frame's cut in
     * the same patch: the cut then says where it is, rather than being moved
     * with the area a second time (viewState.set).
     */
    function viewPatch(env, view, box = undefined) {
        const vs = env.vs;
        if (!vs) return null;
        const current = vs.get();
        const mode = view.mode === "sections" ? "plane" : view.mode;
        const layout = view.mode === "sections" ? "sections"
            : (mode === "volume" ? current.layout : "single");
        const top = Math.max(0, (vs.depth || 1) - 1);
        let slab = view.slab;
        if (!slab && mode === "volume") slab = { z0: 0, z1: top, op: current.slab?.op || "max" };
        const patch = {};
        if (current.t !== view.t) patch.t = view.t;
        if (current.z !== view.z) patch.z = view.z;
        if (current.mode !== mode) patch.mode = mode;
        if (current.layout !== layout) patch.layout = layout;
        if (slab && (current.slab?.z0 !== slab.z0 || current.slab?.z1 !== slab.z1
                     || (slab.op && current.slab?.op !== slab.op) || current.slab?.follow)) {
            patch.slab = { z0: slab.z0, z1: slab.z1, op: slab.op || current.slab?.op || "max",
                           follow: false, thickness: slab.z1 - slab.z0 + 1 };
        }
        if (view.composite && current.volume?.composite !== view.composite) {
            patch.volume = { composite: view.composite };
        }
        if (view.inspect && JSON.stringify(current.inspect || null) !== JSON.stringify(view.inspect)) {
            patch.inspect = view.inspect;
        }
        if (mode === "volume" && box !== undefined
                && JSON.stringify(current.volume?.area || null) !== JSON.stringify(box || null)) {
            patch.volume = { ...(patch.volume || {}), area: box ? { ...box } : null };
            if (view.inspect) patch.inspect = view.inspect;
        }
        return Object.keys(patch).length ? patch : null;
    }

    function applyView(env, view, box = undefined) {
        const patch = viewPatch(env, view, box);
        if (!patch) return false;
        env.vs.set(patch, { save: false });
        return true;
    }

    /** The 3-D camera onto the renderer. True when anything changed. (The
     *  area is the view state's, set with the view: applyView.) */
    function applyCamera(env, camera) {
        const renderer = env.renderer;
        if (!renderer || !camera) return false;
        const c = renderer.camera;
        const same = c.dist === camera.dist && c.panX === camera.pan[0] && c.panY === camera.pan[1]
            && c.q.every((v, i) => v === camera.q[i]);
        renderer.stopMotion?.();
        renderer.zoomTarget = null;
        if (same) return false;
        c.q = camera.q.slice();
        c.dist = camera.dist;
        c.panX = camera.pan[0];
        c.panY = camera.pan[1];
        renderer.invalidate?.();
        return true;
    }

    /** The 2-D view: the output-aspect rectangle of the frame's view, fitted.
     *  `aspect` is the output's width / height. */
    function applyViewport(env, viewport, aspect) {
        const iv = env.iv;
        const scene = global.PlexoraViewerScene;
        if (!iv || !scene || !viewport) return false;
        const transform = iv.viewTransform?.get?.() || {};
        if ((transform.degrees || 0) !== (viewport.degrees || 0)
            || Boolean(transform.flipH) !== Boolean(viewport.flip_h)
            || Boolean(transform.flipV) !== Boolean(viewport.flip_v)) {
            iv.viewTransform?.set?.({ degrees: viewport.degrees || 0, flipH: Boolean(viewport.flip_h),
                                      flipV: Boolean(viewport.flip_v) },
                                    { immediately: true, save: false });
        }
        const rect = Camera2d().rect(viewport, aspect);
        scene.fitRegion(iv, rect, { immediately: true, config: env.config });
        return true;
    }

    /** Channels by key onto the sidebar's slots. Returns the keys that have
     *  no slot (a channel the sidebar does not show), for the caller to warn. */
    function applyChannels(env, channels, { windows = true } = {}) {
        const sidebar = env.sidebar;
        if (!sidebar || !Array.isArray(channels)) return [];
        const byKey = new Map(slots(env).map((entry) => [entry.key, entry]));
        const missing = [];
        let changedWindow = false;
        let edits = 0;
        for (const channel of channels) {
            const entry = byKey.get(channel.key);
            if (!entry) {
                if (channel.visible && channel.presence > 0) missing.push(channel.key);
                continue;
            }
            const { slot, i } = entry;
            // At presence 0 a visible channel stays ENABLED, drawn black: its
            // tiles are loaded before it fades in, and enabling it mid-fade
            // would stall on a fetch (and auto-level it).
            const visible = Boolean(channel.visible);
            if (Boolean(slot.enabled) !== visible) {
                sidebar.setSlotEnabled(i, visible);
                edits += 1;
            }
            if (!visible) continue;
            const colour = presenceColor(channel.color || slot.colorHex, channel.presence);
            if (String(slot.colorHex || "").toLowerCase() !== colour) {
                sidebar.setSlotColor(i, colour, false);
                edits += 1;
            }
            if (windows && Array.isArray(channel.window)) {
                const raw = rawRange(env, slot);
                const differs = !raw || Math.abs(raw[0] - channel.window[0]) > 1e-6 * Math.max(1, Math.abs(channel.window[0]))
                    || Math.abs(raw[1] - channel.window[1]) > 1e-6 * Math.max(1, Math.abs(channel.window[1]));
                if (differs) {
                    const range = domainRange(env, slot, channel.window);
                    const now = slot.range || [];
                    if (range && (range[0] !== now[0] || range[1] !== now[1])) {
                        sidebar.setSlotWindow(i, range, { commit: false });
                        changedWindow = true;
                        edits += 1;
                    }
                }
            }
        }
        if (changedWindow) env.touchedWindows = true;
        env.channelEdits = (env.channelEdits || 0) + edits;
        return missing;
    }

    function applyLayers(env, state) {
        const iv = env.iv;
        for (const layer of state.cell_layers || []) {
            const current = (iv?.cellLayers?.() || []).find((l) => l.name === layer.name);
            if (!current) continue;
            if (layer.mode && current.mode !== layer.mode) iv.setCellLayerMode(layer.name, layer.mode);
            if (Number.isFinite(layer.opacity) && Math.abs(current.opacity - layer.opacity) > 1e-4) {
                iv.setLayerOpacity(layer.name, layer.opacity);
            }
            if (typeof layer.visible === "boolean" && current.visible !== layer.visible) {
                iv.setCellLayerVisible(layer.name, layer.visible);
            }
        }
        const layersApi = env.ctx?.layers;
        if (!layersApi) return;
        const records = new Map((layersApi.list?.() || []).map((l) => [l.id, l]));
        for (const layer of state.layers || []) {
            const current = records.get(layer.id);
            if (!current) continue;
            if (typeof layer.visible === "boolean" && current.visible !== layer.visible) {
                layersApi.setVisible(layer.id, layer.visible);
            }
            if (Number.isFinite(layer.opacity) && Math.abs(current.opacity - layer.opacity) > 1e-4) {
                layersApi.setOpacity(layer.id, layer.opacity);
            }
        }
    }

    /**
     * A frame state onto the viewer, in the order the viewer needs: the view
     * (which may build the 3-D renderer), then -- once the view has settled
     * when `settle` is true -- the camera or the 2-D view, the channels, the
     * layers. Returns `{missingChannels}`.
     */
    /** A frame's clip (x, y, z in [0, 1]) onto the 3-D renderer. In 3-D a
     *  frame's slab still narrows z, as it always has in a video: the viewer's
     *  own projection range no longer cuts its 3-D view (core's inspect does),
     *  so the slab is laid into the clip here. */
    function applyClip(env, clip, view = null) {
        const renderer = env.renderer;
        if (!renderer?.setClip) return false;
        let out = clip || null;
        const depth = Math.max(1, env.vs?.depth || 1);
        const slab = view?.mode === "volume" ? view.slab : null;
        if (slab && (slab.z0 > 0 || slab.z1 < depth - 1)) {
            out = JSON.parse(JSON.stringify(out || { x: [0, 1], y: [0, 1], z: [0, 1] }));
            const z = out.z || [0, 1];
            const lo = Math.max(z[0], slab.z0 / depth);
            out.z = [lo, Math.max(lo, Math.min(z[1], (slab.z1 + 1) / depth))];
        }
        return renderer.setClip(out);
    }

    async function apply(env, state, { aspect = 16 / 9, settle = true } = {}) {
        applyView(env, state.view, state.box);
        if (settle && env.vs) await env.vs.settled();
        const volume = state.view.mode === "volume";
        if (volume) {
            applyCamera(env, state.camera3d);
            applyClip(env, state.view.clip, state.view);
        } else if (state.viewport) applyViewport(env, state.viewport, aspect);
        const missingChannels = applyChannels(env, state.channels);
        applyLayers(env, state);
        return { missingChannels };
    }

    /** Synchronous variant for preview: nothing awaited. The camera is
     *  applied when the renderer already exists (it is built by the view's
     *  revision the first time volume mode is entered). */
    function applyNow(env, state, { aspect = 16 / 9 } = {}) {
        applyView(env, state.view, state.box);
        if (state.view.mode === "volume") {
            applyCamera(env, state.camera3d);
            applyClip(env, state.view.clip, state.view);
        } else if (state.viewport) applyViewport(env, state.viewport, aspect);
        applyChannels(env, state.channels);
        applyLayers(env, state);
    }

    /** A frame state as a Figure Builder scene snapshot (its schema has no
     *  plane, timepoint or camera; those ride in `plugins.video_studio`). */
    function toFigureScene(env, state, extra = {}) {
        const rect = state.viewport ? Camera2d().rect(state.viewport, extra.aspect || 16 / 9) : null;
        return {
            snapshot_version: 1,
            source_id: env.datasource,
            viewport: rect ? { x: rect.x, y: rect.y, w: rect.width, h: rect.height,
                               ...(state.viewport.degrees || state.viewport.flip_h || state.viewport.flip_v
                                   ? { orientation: { degrees: state.viewport.degrees || 0,
                                                      flip_h: state.viewport.flip_h,
                                                      flip_v: state.viewport.flip_v } } : {}) } : null,
            channels: (state.channels || []).filter((c) => c.visible && c.presence > 0).map((c) => {
                const [r, g, b] = hexToRgb(c.color);
                return { key: c.key, color: { r, g, b }, window: c.window, visible: true };
            }),
            core_overlays: { cell_layers: state.cell_layers || [], layers: state.layers || [],
                             hd_tiles: Boolean(state.hd_mode), scalebar_visible: true },
            plugins: { video_studio: { version: 1, state: {
                view: state.view, camera3d: state.camera3d, t_seconds: state.t,
                video_id: extra.video_id || null, revision: extra.revision ?? null } } },
            captured_at: new Date().toISOString(),
        };
    }

    /** A Figure Builder scene as a SceneState (z, t and camera from the
     *  plugin part when Video Studio wrote it, else defaulted -- and said so). */
    function fromFigureScene(scene, fallback = {}) {
        const part = scene?.plugins?.video_studio?.state || {};
        const defaulted = [];
        const v = scene?.viewport;
        const viewport = v ? { cx: v.x + v.w / 2, cy: v.y + v.h / 2, w: v.w,
                               degrees: v.orientation?.degrees || 0,
                               flip_h: Boolean(v.orientation?.flip_h),
                               flip_v: Boolean(v.orientation?.flip_v) } : fallback.viewport || null;
        if (!part.view) defaulted.push("plane and timepoint");
        if (!part.camera3d) defaulted.push("3-D camera");
        return {
            scene: {
                dims: fallback.dims || "2d",
                view: part.view || fallback.view,
                viewport,
                camera3d: part.camera3d || fallback.camera3d || null,
                channels: (scene?.channels || []).map((c) => ({
                    key: c.key, name: c.key, color: rgbToHex([c.color?.r || 0, c.color?.g || 0,
                                                              c.color?.b || 0]),
                    window: c.window, visible: c.visible !== false, opacity: 1, presence: 1 })),
                cell_layers: scene?.core_overlays?.cell_layers || [],
                layers: scene?.core_overlays?.layers || [],
                hd_mode: scene?.core_overlays?.hd_tiles ?? null,
                plugins: {},
            },
            defaulted,
        };
    }

    global.PlexoraVideoSceneState = {
        capture, facts, apply, applyNow, applyView, applyCamera, applyClip, applyViewport, applyChannels,
        applyLayers, viewPatch, slots, slotKey, rawRange, domainRange, presenceColor, toFigureScene,
        fromFigureScene, hexToRgb, rgbToHex,
    };
})(typeof window !== "undefined" ? window : globalThis);
