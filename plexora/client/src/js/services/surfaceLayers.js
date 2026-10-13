/**
 * surfaceLayers.js - surface layers on the page: loaded into the 3-D view, styled, made.
 *
 * A surface is a layer (`kind: "surface"`, plexora/server/surface/store.py):
 * it arrives with `/config` like any other, has a card in the Layers panel,
 * and its eye, opacity and look are its `render` bag, saved through the
 * ordinary layer PATCH. This is what connects those records to the 3-D view's
 * mesh renderer (views/surfaceRenderer.js):
 *
 *   - a ready, visible surface's mesh is fetched once (ETag-revalidated) and
 *     uploaded when the 3-D view exists -- not before: a user who never opens
 *     3-D never downloads a mesh;
 *   - the stack's eye and opacity, and the card's style, reach the renderer as
 *     uniforms (`setStyle`), never as a new upload;
 *   - `create()` posts a surface and takes the new layer on; Surface Studio is
 *     Plexora 3D (`volume:surfaces`), not offered without it, and a refusal
 *     that still arrives (a licence lapsed mid-session) is quiet: the caller
 *     closes, never a broken panel;
 *   - a click on a surface in 3-D picks its object (`plexora:surface-pick`
 *     from the renderer) and says so as `plexora:surface-selected`.
 *
 * Classic script: `window.PlexoraSurfaceLayers`.
 */
window.PlexoraSurfaceLayers = (function () {
    "use strict";

    const KIND = "surface";
    const PERSIST_DELAY = 400;

    let datasource = null;
    let stack = null;
    let unsubscribe = null;
    //: id -> {status, etag, loaded, loading, lod}
    const records = new Map();
    const pendingPersist = new Map();
    let selection = null;

    function url(path) {
        return typeof window.plexoraUrl === "function" ? window.plexoraUrl(path) : `/${path}`;
    }

    function ds() { return encodeURIComponent(datasource || window.flaskVariables?.datasource || ""); }

    function volumeView() { return window.__plexora?.volumeView || null; }

    function renderer() { return volumeView()?.surfaces || null; }

    function surfaceLayers() {
        return (stack?.layers?.() || []).filter((layer) => layer.kind === KIND);
    }

    /** Whether a Paid route refused (403 with a `license` block). Quiet: the
     *  caller closes what it opened, and nothing explains or upsells. */
    async function refused(response) {
        if (response.status !== 403) return false;
        const body = await response.clone().json().catch(() => ({}));
        if (body?.license) {
            console.warn(`surfaceLayers: ${body.license.label || "this"} is not included in this licence`);
            return true;
        }
        return false;
    }

    // -- style --------------------------------------------------------------

    /** The renderer's style for a stack layer: its render bag, eye and opacity. */
    function styleOf(layer) {
        const render = layer.spec?.render || {};
        return {
            color: render.color || "#d8b45a",
            opacity: Number.isFinite(Number(layer.opacity)) ? Number(layer.opacity)
                : (Number.isFinite(Number(render.opacity)) ? Number(render.opacity) : 1),
            style: render.style || "solid",
            material: render.material || "soft",
            visible: layer.visible !== false,
            colorBy: render.colorBy || "uniform",
            hidden: Array.isArray(render.hidden) ? render.hidden : null,
            isolate: Array.isArray(render.isolate) ? render.isolate : null,
            compareRaw: Boolean(render.compareRaw),
            lut: render.colorBy === "cells" ? cellLut() : (render.colorBy === "table" ? tableLut(layer.id) : null),
        };
    }

    /** The active cell layer's colours (Cell Explorer, gating), as RGBA by id. */
    function cellLut() {
        const viewer = window.__plexora?.seaDragonViewer;
        const record = viewer?.getCellLayer?.(viewer.cellLayerOwner);
        const lut = record?.lut;
        if (!lut) return null;
        if (lut.colors) return { colors: lut.colors, maxId: lut.maxId };
        if (lut.map instanceof Map) {
            let maxId = 0;
            for (const id of lut.map.keys()) maxId = Math.max(maxId, Number(id) || 0);
            const colors = new Uint8Array((maxId + 1) * 4);
            for (const [id, value] of lut.map) {
                const rgb = Array.isArray(value) ? value : hexBytes(value);
                colors.set([rgb[0], rgb[1], rgb[2], rgb[3] ?? 255], Number(id) * 4);
            }
            return { colors, maxId };
        }
        return null;
    }

    function hexBytes(hex) {
        const text = String(hex || "").replace(/^#/, "");
        const value = parseInt(text.length === 3 ? text.split("").map((c) => c + c).join("") : text, 16);
        return Number.isFinite(value) ? [(value >> 16) & 255, (value >> 8) & 255, value & 255, 255]
            : [200, 200, 200, 255];
    }

    //: id -> {colors, maxId}: a colour-by from the objects table (surfaceObjects.js).
    const tableLuts = new Map();
    function tableLut(id) { return tableLuts.get(id) || null; }

    function setTableLut(id, lut) {
        if (lut) tableLuts.set(id, lut);
        else tableLuts.delete(id);
        const layer = stack?.get(id);
        if (layer) renderer()?.setStyle(id, styleOf(layer));
    }

    /**
     * Change how a surface looks: the card's controls. The picture changes at
     * once (a uniform); the render bag is saved a moment later, coalesced.
     */
    function style(id, patch) {
        const layer = stack?.get(id);
        if (!layer) return;
        const spec = layer.spec || (layer.spec = {});
        // Glass is see-through by definition: from an opaque surface it
        // starts at 35 %, and back from glass it is opaque again -- one rule,
        // whichever control (card, agent, scene) asked.
        const was = Number(layer.opacity ?? spec.render?.opacity ?? 1);
        if (patch.style === "glass" && patch.opacity === undefined && was >= 0.995) patch = { ...patch, opacity: 0.35 };
        if (patch.style && patch.style !== "glass" && spec.render?.style === "glass"
                && patch.opacity === undefined && was < 0.5) patch = { ...patch, opacity: 1 };
        spec.render = { ...(spec.render || {}), ...patch };
        if (patch.opacity !== undefined) stack.setOpacity(id, patch.opacity);
        if (patch.visible !== undefined) stack.setVisible(id, patch.visible);
        renderer()?.setStyle(id, styleOf(layer));
        persist(id, patch);
    }

    function persist(id, patch) {
        if (!datasource) return;
        const queued = pendingPersist.get(id) || { render: {} };
        const { visible, ...render } = patch;
        if (visible !== undefined) queued.visible = visible;
        Object.assign(queued.render, render);
        clearTimeout(queued.timer);
        queued.timer = setTimeout(() => {
            pendingPersist.delete(id);
            const body = {};
            if (queued.visible !== undefined) body.visible = queued.visible;
            if (Object.keys(queued.render).length) body.render = queued.render;
            fetch(url(`project/${ds()}/layers/${encodeURIComponent(id)}`), {
                method: "PATCH", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            }).catch((error) => console.error("surfaceLayers: saving the surface failed", error));
        }, PERSIST_DELAY);
        pendingPersist.set(id, queued);
    }

    // -- meshes ---------------------------------------------------------------

    async function fetchMesh(id) {
        const record = records.get(id) || {};
        const response = await fetch(url(`volume/${ds()}/surfaces/${encodeURIComponent(id)}/mesh`));
        if (!response.ok) throw new Error(`surface mesh: HTTP ${response.status}`);
        record.etag = response.headers.get("ETag");
        records.set(id, record);
        return response.arrayBuffer();
    }

    /** Upload `id`'s mesh if it is ready, visible and the 3-D view exists. */
    async function ensure(layer) {
        const surfaces = renderer();
        const id = layer.id;
        const record = records.get(id) || {};
        records.set(id, record);
        const status = layer.spec?.status || "ready";
        record.status = status;
        if (!surfaces || status !== "ready") return;
        if (surfaces.has(id)) {
            surfaces.setStyle(id, styleOf(layer));
            return;
        }
        if (layer.visible === false || record.loading) return;
        record.loading = (async () => {
            try {
                const buffer = await fetchMesh(id);
                const live = stack?.get(id);
                if (!live || !renderer()) return;
                const summary = renderer().load(id, buffer, styleOf(live));
                record.loaded = summary;
                window.dispatchEvent(new CustomEvent("plexora:surface-loaded",
                    { detail: { id, ...summary } }));
            } catch (error) {
                console.error("surfaceLayers: the surface could not be loaded", error);
                record.error = String(error?.message || error);
            } finally {
                record.loading = null;
            }
        })();
        await record.loading;
    }

    /** Whether a surface's mesh is still being fetched (the 3-D view's
     *  `settled()`: a video frame waits for it). */
    function loading() {
        for (const record of records.values()) if (record.loading) return true;
        return false;
    }

    /** Bring the renderer in line with the stack: load, restyle, unload. */
    function sync() {
        if (typeof scheduleCuts === "function") scheduleCuts();
        const layers = surfaceLayers();
        const ids = new Set(layers.map((layer) => layer.id));
        for (const layer of layers) ensure(layer);
        const surfaces = renderer();
        if (surfaces) {
            for (const id of [...surfaces.meshes.keys()]) {
                if (id !== window.PlexoraSurfaceRenderer?.PREVIEW_ID && !ids.has(id)) surfaces.unload(id);
            }
        }
        for (const id of [...records.keys()]) if (!ids.has(id)) records.delete(id);
    }

    // -- making ---------------------------------------------------------------

    /**
     * Make a surface. `body` is the server's SurfaceSpec (+ `render`). Answers
     * the server's JSON, or null when the licence refused (the Paid modal is
     * then open) -- and throws with the server's sentence on a 400.
     */
    async function create(body) {
        const response = await fetch(url(`volume/${ds()}/surfaces`), {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (await refused(response)) return null;
        const answer = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(answer.error || `HTTP ${response.status}`);
            error.estimate = answer.estimate || null;
            throw error;
        }
        await window.__plexora?.adoptLayers?.();
        // Shown where it is drawn.
        const vs = window.__plexora?.viewState;
        if (vs?.enabled?.() && !vs.isVolume()) vs.set({ mode: "volume" });
        sync();
        return answer;
    }

    /** Build a failed surface again from the request it was made with. */
    async function retry(id) {
        const layer = stack?.get(id);
        const spec = layer?.spec?.surface;
        if (!spec) return null;
        return create({ ...spec, replace: true });
    }

    async function getJson(path, options = {}) {
        const response = await fetch(url(path), options);
        if (await refused(response)) return null;
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
            const error = new Error(body.error || `HTTP ${response.status}`);
            error.estimate = body.estimate || null;
            throw error;
        }
        return body;
    }

    function query(params) {
        const out = new URLSearchParams();
        for (const [key, value] of Object.entries(params || {})) {
            if (value === null || value === undefined || value === "") continue;
            out.set(key, Array.isArray(value) ? value.join(",") : String(value));
        }
        return out.toString();
    }

    /** Thresholds and the histogram for `channel` (the slider's start). */
    function suggest(params) {
        return getJson(`volume/${ds()}/surfaces/suggest?${query(params)}`);
    }

    /** What a request would build; null when refused by the licence. */
    function estimate(body) {
        return getJson(`volume/${ds()}/surfaces/estimate`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
    }

    /**
     * The slider's preview: the mesh into the 3-D view, and the parsed header
     * (level, counts, contours) back. `signal` aborts a superseded one.
     */
    async function preview(params, { signal } = {}) {
        const response = await fetch(url(`volume/${ds()}/surfaces/preview?${query(params)}`),
                                     { signal });
        if (await refused(response)) return null;
        if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            throw new Error(body.error || `HTTP ${response.status}`);
        }
        const buffer = await response.arrayBuffer();
        const parsed = window.PlexoraSurfaceRenderer.parse(buffer);
        renderer()?.setPreview(parsed, params.previewStyle || {});
        return parsed.header;
    }

    function clearPreview() {
        renderer()?.setPreview(null);
    }

    /** An edit as a new version of the mesh (`{op, ...}`; `undo` steps back),
     *  then the new mesh drawn. Null when the licence refused. */
    async function refine(id, body) {
        const answer = await getJson(`volume/${ds()}/surfaces/${encodeURIComponent(id)}/refine`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (!answer) return null;
        reload(id);
        return answer;
    }

    /** Fetch and upload `id`'s mesh again (a new version of it). */
    function reload(id) {
        renderer()?.unload(id, { keepStyle: true });
        const record = records.get(id);
        if (record) record.loaded = null;
        cuts.delete(id);
        sync();
    }

    /** A mesh file as a surface (units read from the file when it says). */
    async function importMesh(file, { units = null, label = null } = {}) {
        const form = new FormData();
        form.append("file", file, file.name);
        if (units) form.append("units", units);
        if (label) form.append("label", label);
        const response = await fetch(url(`volume/${ds()}/surfaces/import`), { method: "POST", body: form });
        if (await refused(response)) return null;
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
        await window.__plexora?.adoptLayers?.();
        sync();
        return body;
    }

    const LIGHTS = { left: { azimuth: -55, elevation: 35 }, above: { azimuth: 0, elevation: 70 },
                     right: { azimuth: 55, elevation: 35 }, front: { azimuth: 0, elevation: 5 } };
    let light = "left";

    /** Where the key light comes from: left, above, right or front. */
    function setLight(name) {
        if (!LIGHTS[name]) return;
        light = name;
        renderer()?.setLight(LIGHTS[name]);
    }

    function lightName() { return light; }

    function objects(id, ids = null) {
        const extra = ids ? `?ids=${ids.join(",")}` : "";
        return getJson(`volume/${ds()}/surfaces/${encodeURIComponent(id)}/objects${extra}`);
    }

    function manifest(id) {
        return getJson(`volume/${ds()}/surfaces/${encodeURIComponent(id)}/manifest`);
    }

    function list() {
        return getJson(`volume/${ds()}/surfaces`);
    }

    // -- what the 3-D view shows ------------------------------------------------

    /** Volume, surfaces, or both (`{volume, surfaces}`), saved with the view. */
    function setShow(show) {
        const vs = window.__plexora?.viewState;
        if (!vs?.enabled?.()) return;
        const volume = vs.get().volume || {};
        const next = { ...(volume.show || {}), ...show };
        vs.set({ volume: { ...volume, show: next } });
        // What is shown is not part of the view's revision (nothing reloads
        // for it), so the renderer is told directly as well.
        renderer()?.setShow(next);
    }

    function shown() {
        const show = window.__plexora?.viewState?.get?.()?.volume?.show || {};
        return { volume: show.volume !== false, surfaces: show.surfaces !== false };
    }

    // -- picking ----------------------------------------------------------------

    function select(next) {
        selection = next && next.surface ? next : null;
        renderer()?.setSelected(selection);
        window.dispatchEvent(new CustomEvent("plexora:surface-selected", { detail: selection }));
    }

    function selected() { return selection; }

    function onPick(event) {
        const detail = event.detail || {};
        const hit = detail.hit;
        if (!hit || !hit.object) {
            select(null);
            return;
        }
        select({ surface: hit.surface, object: hit.object, point: hit.point,
                 clientX: detail.clientX, clientY: detail.clientY });
    }

    /** Hide or isolate objects of a surface (saved in its render bag). */
    function hideObjects(id, objectIds, { add = true } = {}) {
        const layer = stack?.get(id);
        if (!layer) return;
        const current = new Set(layer.spec?.render?.hidden || []);
        for (const object of objectIds) {
            if (add) current.add(Number(object));
            else current.delete(Number(object));
        }
        style(id, { hidden: current.size ? [...current] : null });
    }

    function isolate(id, objectIds) {
        style(id, { isolate: objectIds && objectIds.length ? objectIds.map(Number) : null });
    }

    // -- the 2-D views: where each surface cuts the planes on screen ------------------

    //: id -> {key, cut: {xy, xz, yz}}
    const cuts = new Map();
    let cutTimer = 0;
    let cutOverlay = null;
    let paneOff = null;

    function planeQuery() {
        const vs = window.__plexora?.viewState;
        const state = vs?.get?.();
        if (!vs?.enabled?.() || !state) return null;
        const frame = window.__plexora?.sectionPanes?.frame;
        const out = { z: state.z };
        if (frame && Number.isFinite(frame.cx)) {
            out.x = Math.round(frame.cx);
            out.y = Math.round(frame.cy);
        }
        return out;
    }

    /** Fetch the cuts of every visible, ready surface through the planes on screen. */
    function scheduleCuts() {
        clearTimeout(cutTimer);
        cutTimer = setTimeout(refreshCuts, 120);
    }

    async function refreshCuts() {
        const where = planeQuery();
        const layers = surfaceLayers().filter((layer) => layer.visible !== false
            && (layer.spec?.status || "ready") === "ready");
        const keep = new Set(layers.map((layer) => layer.id));
        for (const id of [...cuts.keys()]) if (!keep.has(id)) cuts.delete(id);
        if (!where || !layers.length) {
            paintCuts();
            return;
        }
        const key = JSON.stringify(where);
        await Promise.all(layers.map(async (layer) => {
            if (cuts.get(layer.id)?.key === key) return;
            const record = records.get(layer.id) || {};
            try {
                const response = await fetch(url(`volume/${ds()}/surfaces/${encodeURIComponent(layer.id)}`
                                                  + `/sections?${query(where)}`));
                if (!response.ok) return;
                cuts.set(layer.id, { key, cut: await response.json(), etag: record.etag });
            } catch (error) {
                /* a cut that did not come is a line not drawn */
            }
        }));
        paintCuts();
    }

    function colourFor(layer, object) {
        const render = layer.spec?.render || {};
        if (render.colorBy === "id") {
            const rgb = window.PlexoraSurfaceRenderer?.idColour?.(Number(object)) || [1, 1, 1];
            return `rgb(${rgb.map((v) => Math.round(v * 255)).join(",")})`;
        }
        return render.color || "#d8b45a";
    }

    function hiddenSet(layer) {
        const render = layer.spec?.render || {};
        const hidden = new Set((render.hidden || []).map(Number));
        const only = render.isolate ? new Set(render.isolate.map(Number)) : null;
        return (object) => hidden.has(Number(object)) || (only && !only.has(Number(object)));
    }

    function paintCuts() {
        const viewer = window.__plexora?.seaDragonViewer;
        if (!cutOverlay && viewer?.addOverlay && cuts.size) {
            cutOverlay = viewer.addOverlay({
                id: "surfaces:cuts", kind: "shapes",
                draw: ({ context, px }) => {
                    const vs = window.__plexora?.viewState;
                    const state = vs?.get?.();
                    if (!context || !state || state.mode === "volume") return;
                    for (const [id, held] of cuts) {
                        const layer = stack?.get(id);
                        const xy = held.cut?.xy;
                        if (!layer || layer.visible === false || !xy || Number(xy.z) !== Number(state.z)) continue;
                        const skip = hiddenSet(layer);
                        context.globalAlpha = Math.max(0.35, Math.min(1, Number(layer.opacity ?? 1)));
                        for (const [object, flat] of Object.entries(xy.objects || {})) {
                            if (skip(object)) continue;
                            const picked = selection && selection.surface === id
                                && Number(selection.object) === Number(object);
                            context.strokeStyle = picked ? "#ffd75e" : colourFor(layer, object);
                            context.lineWidth = (picked ? 2.6 : 1.4) * px;
                            context.beginPath();
                            for (let i = 0; i + 3 < flat.length; i += 4) {
                                context.moveTo(flat[i], flat[i + 1]);
                                context.lineTo(flat[i + 2], flat[i + 3]);
                            }
                            context.stroke();
                        }
                    }
                    context.globalAlpha = 1;
                },
            });
        }
        cutOverlay?.invalidate?.();
        const panes = window.__plexora?.sectionPanes;
        if (panes?.addOverlay && !paneOff && cuts.size) {
            paneOff = panes.addOverlay("surfaces", ({ axis }) => {
                const shapes = [];
                for (const [id, held] of cuts) {
                    const layer = stack?.get(id);
                    const part = held.cut?.[axis];
                    if (!layer || layer.visible === false || !part) continue;
                    const skip = hiddenSet(layer);
                    for (const [object, flat] of Object.entries(part.objects || {})) {
                        if (skip(object)) continue;
                        const lines = [];
                        for (let i = 0; i + 3 < flat.length; i += 4) {
                            lines.push([flat[i], flat[i + 1], flat[i + 2], flat[i + 3]]);
                        }
                        const picked = selection && selection.surface === id
                            && Number(selection.object) === Number(object);
                        shapes.push({ color: picked ? "#ffd75e" : colourFor(layer, object), lines,
                                      dash: [], selected: picked });
                    }
                }
                return shapes;
            });
        }
        panes?.paintOverlays?.();
    }

    // -- a scene: what Video Studio animates and Figure Builder keeps -----------------

    /** Every surface's look, as a scene records it. */
    function captureState() {
        return surfaceLayers().map((layer) => {
            const render = layer.spec?.render || {};
            return { id: layer.id, visible: layer.visible !== false,
                     opacity: Number(layer.opacity ?? render.opacity ?? 1),
                     color: render.color || null, style: render.style || "solid",
                     material: render.material || "soft", colorBy: render.colorBy || "uniform",
                     hidden: render.hidden || null, isolate: render.isolate || null };
        });
    }

    /**
     * Put surfaces back as a scene recorded them. `save: false` (a video's
     * frame, a figure reopened) changes only the picture, never the saved
     * render bags.
     */
    function applyState(list, { save = false } = {}) {
        for (const entry of list || []) {
            const layer = stack?.get(entry?.id);
            if (!layer) continue;
            const patch = {};
            for (const key of ["color", "style", "material", "colorBy", "hidden", "isolate"]) {
                if (key in entry) patch[key] = entry[key];
            }
            if (Number.isFinite(Number(entry.opacity))) patch.opacity = Number(entry.opacity);
            if (typeof entry.visible === "boolean") patch.visible = entry.visible;
            if (save) {
                style(layer.id, patch);
                continue;
            }
            const spec = layer.spec || (layer.spec = {});
            const { visible, opacity, ...render } = patch;
            spec.render = { ...(spec.render || {}), ...render };
            if (opacity !== undefined) layer.opacity = opacity;
            if (visible !== undefined) layer.visible = visible;
            renderer()?.setStyle(layer.id, styleOf(layer));
            if (layer.visible) ensure(layer);
        }
    }

    /** The 3-D view as a figure panel records it (`scene.plugins.volume`). */
    function sceneState() {
        const vs = window.__plexora?.viewState;
        const volume = volumeView()?.renderer;
        const state = vs?.get?.() || {};
        return { mode: state.mode, composite: state.volume?.composite || "mip",
                 show: shown(), camera: volume?.cameraState?.() || null,
                 box: volume?.box || null, slab: state.slab || null, t: state.t || 0,
                 // [inspect] How the stack was cut, where its sections were,
                 // and the same in words for the panel's report.
                 inspect: state.inspect || null, z: state.z, section: state.section || null,
                 interpolation: state.volume?.interpolation || "linear",
                 cut: vs?.cutLabel?.(state) || "",
                 surfaces: captureState() };
    }

    function applyScene(scene) {
        const vs = window.__plexora?.viewState;
        if (!scene || !vs?.enabled?.()) return false;
        const volume = vs.get().volume || {};
        vs.set({ mode: "volume", t: scene.t || 0, slab: scene.slab || undefined,
                 // [inspect] The cut, its sections' places, how voxels were read.
                 ...(scene.inspect ? { inspect: scene.inspect } : {}),
                 ...(Number.isFinite(scene.z) ? { z: scene.z } : {}),
                 ...(scene.section ? { section: scene.section } : {}),
                 volume: { ...volume, composite: scene.composite || volume.composite,
                           show: scene.show || volume.show,
                           ...(scene.interpolation ? { interpolation: scene.interpolation } : {}),
                           ...(scene.camera ? { camera: scene.camera } : {}) } }, { save: false });
        const restore = () => {
            const renderer = volumeView()?.renderer;
            if (scene.camera) renderer?.restoreCamera?.(scene.camera);
            applyState(scene.surfaces || [], { save: false });
        };
        if (volumeView()?.renderer) restore();
        else window.addEventListener("plexora:volume-mounted", restore, { once: true });
        return true;
    }

    // -- life ---------------------------------------------------------------------

    function init({ datasource: name, layerStack } = {}) {
        datasource = name || window.flaskVariables?.datasource || null;
        stack = layerStack || window.__plexora?.seaDragonViewer?.layerStack || null;
        unsubscribe?.();
        unsubscribe = stack?.subscribe?.(() => sync()) || null;
        window.addEventListener("plexora:volume-mounted", () => {
            renderer()?.setLight(LIGHTS[light]);
            sync();
        });
        window.addEventListener("plexora:layer-ready", (event) => {
            const id = event.detail?.layer;
            if (id && stack?.get(id)?.kind === KIND) sync();
        });
        window.addEventListener("plexora:surface-pick", onPick);
        // The 2-D views follow the plane on screen and the section cuts.
        window.addEventListener("plexora:view-changed", scheduleCuts);
        window.addEventListener("plexora:sections-moved", scheduleCuts);
        window.addEventListener("plexora:surface-selected", () => paintCuts());
        // Figure Builder keeps a 3-D panel's camera, what it showed and its
        // surfaces (`scene.plugins.volume`), and gets them back on reopen.
        window.addEventListener("plexora:figure-capture-state", (event) => {
            if (!window.__plexora?.viewState?.isVolume?.()) return;
            event.detail?.contribute?.("volume", { version: 1, state: sceneState() });
        });
        window.addEventListener("plexora:figure-restore-state", (event) => {
            const part = event.detail?.plugins?.volume;
            if (!part?.state) return;
            event.detail?.report?.("volume", applyScene(part.state) ? "ok" : "partial");
        });
        sync();
        scheduleCuts();
    }

    return {
        init, sync, style, create, retry, suggest, estimate, preview, clearPreview, objects,
        manifest, list, setShow, shown, select, selected, hideObjects, isolate, setTableLut,
        styleOf, surfaceLayers, cellLut, captureState, applyState, sceneState, applyScene,
        refine, reload, importMesh, setLight, lightName, LIGHTS, loading,
        get renderer() { return renderer(); },
        KIND,
    };
}());
