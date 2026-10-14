/**
 * maskSurfaces.js - a 3-D mask drawn in the 3-D view: Outlines and Filled as cell surfaces.
 *
 * The plane views draw a mask from its label tiles; the 3-D view has no label
 * path. There, the Cells control's Outlines and Filled draw every cell of a
 * 3-D mask (a label volume) as its own closed surface -- Surface Studio's
 * cell surfaces (`label_surfaces`, plexora/server/surface/nets.py), made for
 * the viewer and marked as the mask's own (`role: "mask"`). Surface Studio is
 * Paid (`volume:surfaces`): without it the control does not offer Outlines or
 * Filled in 3-D (views/viewerControls.js `volumeModes`), and this does nothing.
 *
 * The first time Outlines or Filled is chosen in 3-D the mask is converted,
 * in two builds: a PREVIEW (a coarse level, seconds) drawn as soon as it
 * lands, then the STANDARD build, which replaces it the frame its mesh is
 * uploaded -- the preview is hidden in the same handler, so there is never a
 * frame with neither. The wait is views/maskSurfaceWait.js: a modal for the
 * first build, "Continue in background" to the navbar chip. Later visits find
 * the surfaces made (`GET /volume/<ds>/surfaces`: role, quality, frame, and
 * whether the mask changed since -- a stale surface is built again).
 *
 * What is drawn follows the control: Outlines is the surface as glass (the
 * volume shows through, as the tissue does around 2-D outlines), Filled is
 * solid, None hides it. Colour follows the cells' colouring: the active
 * tool's colours by cell id when it has any, else a colour per cell.
 *
 * Nothing new on the server: it is the surface routes and the layer poll
 * (main.js `watchLayers`) every surface already uses. The preview has no card
 * in the Layers panel (views/layerManager.js `isCarded`): it is a step of the
 * conversion, which the wait reports, and the standard build is the layer.
 *
 * Classic script: `window.PlexoraMaskSurfaces`.
 */
window.PlexoraMaskSurfaces = (function () {
    "use strict";

    const ROLE = "mask";
    const ENTITLEMENT = "volume:surfaces";
    const LABELS = { preview: "Cells (preview)", standard: "Cells of the 3-D mask" };
    //: Display surfaces: 24 Taubin iterations take the voxel staircase out of
    //: a cell (95th-percentile normal error 14° against the true ellipsoid,
    //: 17° at 12) and leave its volume within a fraction of a percent.
    const SMOOTHING = "strong";
    //: How much of the wait's bar each build is: the preview is quick.
    const SHARE = { preview: [0, 30], standard: [30, 100] };

    //: The Cells control's mode, as it last announced it.
    let mode = "none";
    let started = false;
    //: frame -> {preview, standard}: the layer ids of this frame's builds, as
    //: the server named them (a stale one is never kept).
    const builds = new Map();
    //: The frame whose builds are being looked up or started, and its promise.
    const ensuring = new Map();
    //: frame -> ids of the mask's own surfaces no longer current (`lookUp`).
    const superseded = new Map();
    let wasVolume = false;
    let lastT = null;

    function layers() { return window.PlexoraSurfaceLayers || null; }
    function viewState() { return window.__plexora?.viewState || null; }
    function wait() { return window.PlexoraMaskSurfaceWait || null; }

    function frame() { return Number(viewState()?.get?.()?.t) || 0; }

    function inVolume() {
        const vs = viewState();
        return Boolean(vs?.enabled?.() && vs.isVolume());
    }

    /** Whether this page can draw the mask as surfaces at all: a 3-D mask,
     *  and Surface Studio in the licence. */
    function allowed() {
        return Boolean(viewState()?.labelsAre3d?.())
            && Boolean(window.PlexoraPaid?.allows?.(ENTITLEMENT));
    }

    function drawing(next = mode) { return next === "outlines" || next === "filled"; }

    function stackLayer(id) {
        return id ? (layers()?.surfaceLayers?.() || []).find((layer) => layer.id === id) || null
            : null;
    }

    function status(layer) { return layer ? (layer.spec?.status || "ready") : null; }

    /** Every mask surface on the page, whatever its frame. */
    function maskLayers() {
        return (layers()?.surfaceLayers?.() || []).filter((layer) => layer.spec?.surface?.role === ROLE);
    }

    /** This frame's builds as layers: `{preview, standard}` (null for none). */
    function current(t = frame()) {
        const ids = builds.get(t) || {};
        return { preview: stackLayer(ids.preview), standard: stackLayer(ids.standard) };
    }

    /** What is drawn now: the standard build once its mesh is in, the
     *  preview until then; null while neither is, or the control draws none. */
    function shownId(t = frame()) {
        if (!allowed() || !drawing()) return null;
        const { preview, standard } = current(t);
        const surfaces = layers()?.renderer;
        if (status(standard) === "ready" && surfaces?.has?.(standard.id)) return standard.id;
        if (status(preview) === "ready" && surfaces?.has?.(preview.id)) return preview.id;
        return null;
    }

    /** "none" | "building" | "preview" | "ready" | "failed", for this frame. */
    function state(t = frame()) {
        const { preview, standard } = current(t);
        if (status(standard) === "ready") return "ready";
        if (status(standard) === "failed" || (!standard && status(preview) === "failed")) return "failed";
        if (status(preview) === "ready") return "preview";
        if (preview || standard || ensuring.has(t)) return "building";
        return "none";
    }

    // -- the look ---------------------------------------------------------------

    /** The render bag for a mode: glass for Outlines, solid for Filled; the
     *  cells' colours when a tool gives them, a colour per cell when not. */
    function look(next) {
        const lut = layers()?.cellLut?.();
        return { style: next === "outlines" ? "glass" : "solid", colorBy: lut ? "cells" : "id" };
    }

    /**
     * Show the right surface the right way, and nothing else: of every mask
     * surface on the page, only this frame's best ready build while the
     * control draws the mask. A standard build whose mesh is still on its way
     * is made visible (that is what fetches it) with a drawn preview kept up
     * until it lands (`onLoaded`).
     */
    function apply() {
        const api = layers();
        if (!api) return;
        const on = allowed() && drawing();
        const t = frame();
        const { preview, standard } = current(t);
        const surfaces = api.renderer;
        const want = new Map();
        if (on) {
            if (status(standard) === "ready") {
                want.set(standard.id, true);
                // The preview stays up while the standard mesh is on its way --
                // if it is drawn already: fetching it as a stopgap would only
                // delay the mesh that replaces it.
                if (!surfaces?.has?.(standard.id) && preview && surfaces?.has?.(preview.id)) {
                    want.set(preview.id, true);
                }
            } else if (status(preview) === "ready") {
                want.set(preview.id, true);
            }
        }
        const bag = look(mode);
        const known = builds.has(t);
        for (const layer of maskLayers()) {
            // This frame's surfaces before the server has said which are
            // current: left as they were saved, rather than hidden and shown
            // again a moment later.
            if (on && !known && (Number(layer.spec?.surface?.t) || 0) === t) continue;
            const visible = Boolean(want.get(layer.id));
            const render = layer.spec?.render || {};
            const patch = {};
            if ((layer.visible !== false) !== visible) patch.visible = visible;
            if (visible) {
                if (render.style !== bag.style) patch.style = bag.style;
                if (render.colorBy !== bag.colorBy) patch.colorBy = bag.colorBy;
            }
            if (Object.keys(patch).length) api.style(layer.id, patch);
        }
    }

    /** The cells' colours changed (a tool coloured them, or another tool
     *  became the active one): redraw the shown surface's colour table. */
    function recolour() {
        if (!drawing()) return;
        const api = layers();
        const bag = look(mode);
        for (const layer of maskLayers()) {
            if (layer.visible === false) continue;
            if (layer.spec?.render?.colorBy !== bag.colorBy) api.style(layer.id, { colorBy: bag.colorBy });
            else api.renderer?.setStyle?.(layer.id, api.styleOf(layer));
        }
    }

    // -- the builds -------------------------------------------------------------

    function body(quality, t) {
        const bag = look(drawing() ? mode : "filled");
        return { type: "label_surfaces", role: ROLE, quality, t, smoothing: SMOOTHING,
                 label: LABELS[quality],
                 render: { ...bag, material: "soft", opacity: bag.style === "glass" ? 0.35 : 1 } };
    }

    /** Post one build. The server's answer, or null when the licence refused
     *  (quietly: nothing is offered without it, so this is a lapse mid-session). */
    async function post(quality, t, { replace = false } = {}) {
        const answer = await layers().create({ ...body(quality, t), ...(replace ? { replace: true } : {}) });
        if (!answer) return null;
        const ids = builds.get(t) || {};
        ids[quality] = answer.layer;
        builds.set(t, ids);
        // A surface that existed already is ready now and says so nowhere else.
        if (answer.status === "ready") onReady(answer.layer);
        return answer;
    }

    /** What the server has for frame `t`: the mask's current builds, by
     *  quality -- made by this recipe, from the mask as it is now. The rest of
     *  the mask's own surfaces of that frame (a mask changed since, an older
     *  recipe, a second copy) are `superseded`, to be retired. */
    async function lookUp(t) {
        const listing = await layers().list().catch(() => null);
        const found = {};
        const others = [];
        for (const entry of listing?.surfaces || []) {
            const spec = entry.spec || {};
            if ((entry.role || spec.role) !== ROLE || spec.type !== "label_surfaces") continue;
            if ((Number(spec.t) || 0) !== t) continue;
            if (entry.stale || !(spec.quality in LABELS) || spec.smoothing !== SMOOTHING) {
                others.push(entry.id);
                continue;
            }
            // A ready build outranks a failed one of the same quality.
            const held = found[spec.quality];
            if (!held || (held.status !== "ready" && entry.status === "ready")) {
                if (held) others.push(held.id);
                found[spec.quality] = entry;
            } else {
                others.push(entry.id);
            }
        }
        superseded.set(t, others);
        return found;
    }

    /**
     * Delete mask surfaces nothing will draw again: this frame's preview once
     * the standard build is on screen, and what `lookUp` found superseded.
     * They are Plexora's own files (surface/store.py `forget`), made for this
     * and for nothing else; a surface made in Surface Studio has no `role`
     * and is never touched.
     */
    async function retire(t) {
        const ids = builds.get(t) || {};
        const gone = [...(superseded.get(t) || [])];
        if (ids.preview && status(stackLayer(ids.standard)) === "ready") gone.push(ids.preview);
        if (!gone.length) return;
        superseded.delete(t);
        if (gone.includes(ids.preview)) ids.preview = null;
        const sample = encodeURIComponent(window.flaskVariables?.datasource || "");
        const url = (path) => (typeof window.plexoraUrl === "function" ? window.plexoraUrl(path) : `/${path}`);
        await Promise.all(gone.map((id) => fetch(url(`project/${sample}/layers/${encodeURIComponent(id)}`),
                                                 { method: "DELETE" }).catch(() => null)));
        for (const id of gone) window.__plexora?.layers?.unregister?.(id);
        await window.__plexora?.adoptLayers?.();
    }

    /**
     * Make sure frame `t`'s surfaces exist or are on their way. Once per frame
     * per page; the first build opens the wait's modal, a build already
     * running (a reload mid-build) only its chip.
     */
    function ensure(t = frame()) {
        if (!allowed() || !layers()) return Promise.resolve(null);
        if (builds.has(t)) return Promise.resolve(builds.get(t));
        if (ensuring.has(t)) return ensuring.get(t);
        const promise = (async () => {
            const found = await lookUp(t);
            const ids = { preview: found.preview?.id || null, standard: found.standard?.id || null };
            builds.set(t, ids);
            if (found.standard?.status === "ready") {               // drawn by `update`
                retire(t);
                return ids;
            }
            const running = [found.preview, found.standard].some((entry) => entry?.status === "pending");
            try {
                if (!found.preview || found.preview.status === "failed") {
                    if (!(await post("preview", t, { replace: Boolean(found.preview) }))) return ids;
                    wait()?.start({ modal: !running });
                } else {
                    wait()?.start({ modal: false });
                    if (found.preview.status === "ready" && (!found.standard || found.standard.status === "failed")) {
                        await post("standard", t, { replace: Boolean(found.standard) });
                    }
                }
            } catch (error) {
                wait()?.start({ modal: false });
                wait()?.failed(String(error?.message || error));
            }
            apply();
            return builds.get(t);
        })().finally(() => ensuring.delete(t));
        ensuring.set(t, promise);
        return promise;
    }

    /** The quality and frame a layer id is a build of, or null. */
    function buildOf(id) {
        for (const [t, ids] of builds) {
            for (const quality of Object.keys(LABELS)) if (ids[quality] === id) return { t, quality };
        }
        return null;
    }

    function onProgress(event) {
        const detail = event.detail || {};
        const of = buildOf(detail.layer);
        if (!of || of.t !== frame()) return;
        const [lo, hi] = SHARE[of.quality];
        const part = typeof detail.progress === "number" ? detail.progress : null;
        wait()?.progress({
            progress: part === null ? null : lo + (hi - lo) * part / 100,
            message: of.quality === "preview"
                ? `Quick preview: ${detail.message || "starting"}`
                : `Full quality: ${detail.message || "starting"}`,
            stageLabel: detail.stageLabel ? `${of.quality === "preview" ? "Preview" : "Cells"} · ${detail.stageLabel}` : "",
        });
    }

    function onReady(id) {
        const of = buildOf(id);
        if (!of) return;
        if (of.quality === "preview") {
            const ids = builds.get(of.t) || {};
            const standard = stackLayer(ids.standard);
            if (!ids.standard || status(standard) === "failed") {
                post("standard", of.t, { replace: Boolean(ids.standard) }).catch((error) => {
                    if (of.t === frame()) wait()?.failed(String(error?.message || error));
                });
            }
            if (of.t === frame()) {
                wait()?.progress({ progress: SHARE.standard[0],
                                   message: "The preview is drawn. Building the full-quality surfaces…",
                                   stageLabel: "Cells · full quality" });
            }
        } else if (of.t === frame()) {
            wait()?.ready();
        }
        apply();
        announce();
    }

    function onFailed(event) {
        const detail = event.detail || {};
        const of = buildOf(detail.layer);
        if (!of) return;
        if (of.t === frame()) {
            wait()?.start({ modal: false });
            wait()?.failed(detail.error || "");
        }
        apply();
        announce();
    }

    /** Build what failed again (the wait's Retry). */
    async function retry(t = frame()) {
        const ids = builds.get(t) || {};
        const preview = stackLayer(ids.preview);
        const standard = stackLayer(ids.standard);
        try {
            if (!preview || status(preview) === "failed") {
                if (await post("preview", t, { replace: Boolean(preview) })) wait()?.start();
            } else if (!standard || status(standard) === "failed") {
                if (await post("standard", t, { replace: Boolean(standard) })) wait()?.start();
            }
        } catch (error) {
            wait()?.start({ modal: false });
            wait()?.failed(String(error?.message || error));
        }
    }

    /** The standard build's mesh is in: the preview goes in the same frame,
     *  and then for good, with whatever it superseded. */
    function onLoaded(event) {
        const id = event.detail?.id;
        if (!id || !maskLayers().some((layer) => layer.id === id)) return;
        apply();
        const of = buildOf(id);
        if (of?.quality === "standard") retire(of.t);
    }

    function announce() {
        window.dispatchEvent(new CustomEvent("plexora:mask-surfaces-changed",
            { detail: { state: state(), layer: shownId(), mode } }));
    }

    /** Bring everything in line with the control, the view and the licence. */
    function update() {
        if (allowed() && drawing() && inVolume()) {
            ensure().then(() => {
                apply();
                announce();
            });
        }
        apply();
        announce();
    }

    function onView() {
        const volume = inVolume();
        const t = frame();
        // `view-requested` fires for every step of a scrub: only the edges
        // into 3-D, and a change of frame inside it, are news here.
        if (volume === wasVolume && (!volume || t === lastT)) return;
        wasVolume = volume;
        lastT = t;
        update();
    }

    /** What the agent bridge reports (`viewer_get_state`). */
    function report() {
        const { preview, standard } = current();
        return { allowed: allowed(), state: state(), shown: shownId(), mode,
                 preview: preview?.id || null, standard: standard?.id || null };
    }

    function init() {
        if (started) return;
        started = true;
        wasVolume = inVolume();
        lastT = frame();
        window.addEventListener("plexora:cell-mode-changed", (event) => {
            const next = event.detail?.mode;
            const was = mode;
            mode = next || "none";
            if (was === mode) {
                recolour();
                return;
            }
            // Choosing to see the cells in 3-D is also choosing to see
            // surfaces, if the view was set to the volume alone.
            if (drawing() && allowed() && inVolume() && layers()?.shown?.().surfaces === false) {
                layers().setShow({ surfaces: true });
            }
            update();
        });
        window.addEventListener("plexora:view-requested", onView);
        window.addEventListener("plexora:license-changed", update);
        // A data node's mask known to be 3-D only after load (main.js
        // `trackLabelPlanes`).
        window.addEventListener("plexora:labels-changed", update);
        window.addEventListener("plexora:layer-progress", onProgress);
        window.addEventListener("plexora:layer-ready", (event) => onReady(event.detail?.layer));
        window.addEventListener("plexora:layer-failed", onFailed);
        window.addEventListener("plexora:surface-loaded", onLoaded);
        window.addEventListener("plexora:cell-lut-changed", recolour);
        mode = window.__plexora?.viewerControls?.mode || "none";
        update();
    }

    return { init, ensure, apply, retry, state, shownId, current, report, update,
             ROLE, ENTITLEMENT, get mode() { return mode; } };
}());
