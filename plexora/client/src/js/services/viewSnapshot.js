/**
 * viewSnapshot.js -- "what was on screen", captured and put back.
 *
 * A region somebody draws (by hand or with magic select) remembers the view it
 * was drawn under, so clicking it later shows it the way it was seen: where
 * the viewer was, how far in, HD mode, and every channel switched on with its
 * colour and contrast window. The QC panel stores it with each region; magic
 * select also uses it to decide whether a second click can reuse the first
 * click's work (same view, same channels -> same picture).
 *
 * Image pixels throughout (PlexoraViewerScene's convention), raw intensity
 * units for windows (what a saved channel list holds) -- and a channel whose
 * window cannot be converted to raw yet is recorded without one rather than
 * with a byte pair mislabelled as raw (agentBridge's describeChannels rule).
 *
 * Pure apart from `window.__plexora` and the HD checkbox, both read at call
 * time, so a probe can run it in a vm realm with stand-ins.
 */
(function (global) {
    "use strict";

    const VERSION = 1;
    const MAX_CHANNELS = 8;

    function core() {
        return global.__plexora || {};
    }

    function scene() {
        return global.PlexoraViewerScene || null;
    }

    function finite(value) {
        return Number.isFinite(Number(value));
    }

    function round(value, digits = 2) {
        const factor = 10 ** digits;
        return Math.round(Number(value) * factor) / factor;
    }

    function hdCheckbox() {
        return typeof document !== "undefined"
            ? document.getElementById("viewer_controls_hd") : null;
    }

    function hdMode(panel) {
        try {
            if (panel && typeof panel.isHdMode === "function") return Boolean(panel.isHdMode());
        } catch (error) {
            // fall through to the checkbox
        }
        const box = hdCheckbox();
        return box ? Boolean(box.checked) : false;
    }

    /** The visible channels with colour and raw window, at most eight. */
    function channels(panel) {
        const slots = (panel && panel.channelSlots) || [];
        return slots.filter((slot) => slot && slot.enabled && slot.visible !== false && slot.name)
            .slice(0, MAX_CHANNELS).map((slot) => {
                const entry = { name: slot.name, visible: true };
                if (/^#[0-9a-f]{6}$/i.test(slot.colorHex || "")) entry.color = slot.colorHex;
                let convertible = true;
                try {
                    if (typeof panel.quantWindow === "function" && !hdMode(panel)) {
                        convertible = Boolean(panel.quantWindow(slot.name));
                    }
                } catch (error) {
                    convertible = true;
                }
                let range = null;
                if (convertible) {
                    try {
                        range = typeof panel.toRawRangeForSlot === "function"
                            ? panel.toRawRangeForSlot(slot) : null;
                    } catch (error) {
                        range = null;
                    }
                }
                if (Array.isArray(range) && range.length === 2 && range.every(finite)
                        && Number(range[0]) < Number(range[1])) {
                    entry.range = [round(range[0], 3), round(range[1], 3)];
                }
                return entry;
            });
    }

    /**
     * The view now. `sample` is the image (datasource) it belongs to; the
     * caller knows it. Null when no viewer is open.
     */
    function capture(options = {}) {
        const plexora = core();
        const imageViewer = options.viewer || plexora.seaDragonViewer;
        const panel = options.panel || plexora.viewerSidebar;
        const viewerScene = scene();
        let viewport = null;
        let zoom = null;
        if (imageViewer && imageViewer.viewer && viewerScene) {
            try {
                const box = viewerScene.currentViewport(imageViewer);
                viewport = { x: round(box.x), y: round(box.y),
                             width: round(box.w), height: round(box.h) };
                if (box.orientation) viewport.orientation = box.orientation;
            } catch (error) {
                viewport = null;
            }
            try {
                const scale = viewerScene.scaleOf(imageViewer);
                zoom = scale == null ? null : round(scale, 5);
            } catch (error) {
                zoom = null;
            }
        }
        const sample = options.sample
            || (global.flaskVariables && global.flaskVariables.datasource) || null;
        return {
            version: VERSION,
            sample,
            viewport,
            zoom,
            hd_mode: hdMode(panel),
            channels: channels(panel),
            captured_at: new Date().toISOString(),
        };
    }

    /** The snapshot as QC's `views` object takes it (no version, w/h spelled out). */
    function forStorage(snapshot) {
        if (!snapshot) return null;
        const out = { channels: (snapshot.channels || []).slice(0, MAX_CHANNELS) };
        if (snapshot.sample) out.sample = snapshot.sample;
        if (snapshot.viewport) {
            const v = snapshot.viewport;
            out.viewport = { x: v.x, y: v.y, width: v.width ?? v.w, height: v.height ?? v.h };
        }
        if (finite(snapshot.zoom)) out.zoom = Number(snapshot.zoom);
        if (typeof snapshot.hd_mode === "boolean") out.hd_mode = snapshot.hd_mode;
        if (snapshot.captured_at) out.captured_at = snapshot.captured_at;
        return out;
    }

    function channelKey(list) {
        return (list || []).map((c) => [c.name, (c.color || "").toLowerCase(),
            Array.isArray(c.range) ? c.range.map((v) => round(v, 2)).join("~") : ""].join("|"))
            .join(";");
    }

    /**
     * Whether two snapshots show the same picture: same sample, same channels,
     * colours and windows, same HD mode, and a viewport that moved less than
     * `panTolerance` of its size and zoomed less than `zoomTolerance`.
     */
    function sameView(a, b, options = {}) {
        if (!a || !b) return false;
        const zoomTolerance = options.zoomTolerance ?? 0.10;
        const panTolerance = options.panTolerance ?? 0.25;
        if ((a.sample || null) !== (b.sample || null)) return false;
        if (Boolean(a.hd_mode) !== Boolean(b.hd_mode)) return false;
        if (channelKey(a.channels) !== channelKey(b.channels)) return false;
        const va = a.viewport;
        const vb = b.viewport;
        if (!va || !vb) return !va && !vb;
        const wa = va.width ?? va.w;
        const wb = vb.width ?? vb.w;
        const ha = va.height ?? va.h;
        if (!(wa > 0) || !(wb > 0)) return false;
        if (Math.abs(wb / wa - 1) > zoomTolerance) return false;
        const cxa = va.x + wa / 2;
        const cya = va.y + ha / 2;
        const cxb = vb.x + wb / 2;
        const cyb = vb.y + (vb.height ?? vb.h) / 2;
        return Math.abs(cxb - cxa) <= panTolerance * wa
            && Math.abs(cyb - cya) <= panTolerance * ha;
    }

    /** A string that is equal for two snapshots `sameView` would call equal
     *  at zero tolerance -- for keying caches. */
    function key(snapshot) {
        if (!snapshot) return "";
        const v = snapshot.viewport || {};
        return [snapshot.sample || "", snapshot.hd_mode ? 1 : 0, channelKey(snapshot.channels),
            round(v.x || 0, 0), round(v.y || 0, 0), round(v.width ?? v.w ?? 0, 0),
            round(v.height ?? v.h ?? 0, 0)].join("/");
    }

    /** Every point ({x, y} image pixels) inside the snapshot's viewport. */
    function contains(snapshot, points) {
        const v = snapshot && snapshot.viewport;
        if (!v) return false;
        const w = v.width ?? v.w;
        const h = v.height ?? v.h;
        return (points || []).every((p) => p && p.x >= v.x && p.x <= v.x + w
            && p.y >= v.y && p.y <= v.y + h);
    }

    /** Put the viewport back -- animated unless `immediately`. */
    function restoreViewport(snapshot, options = {}) {
        const viewerScene = scene();
        const imageViewer = options.viewer || core().seaDragonViewer;
        const v = snapshot && snapshot.viewport;
        if (!viewerScene || !imageViewer || !imageViewer.viewer || !v) return false;
        const viewport = { x: Number(v.x), y: Number(v.y), w: Number(v.width ?? v.w),
                           h: Number(v.height ?? v.h) };
        if (![viewport.x, viewport.y, viewport.w, viewport.h].every(Number.isFinite)
                || !(viewport.w > 0) || !(viewport.h > 0)) return false;
        if (v.orientation) viewport.orientation = v.orientation;
        return viewerScene.restoreViewport(imageViewer, undefined, viewport,
            { immediately: Boolean(options.immediately) });
    }

    /** Flip HD mode to the snapshot's, through the checkbox's own change event
     *  (viewerControls does the reload work). Only when it differs. */
    function restoreHdMode(snapshot) {
        if (!snapshot || typeof snapshot.hd_mode !== "boolean") return false;
        const box = hdCheckbox();
        if (!box || Boolean(box.checked) === snapshot.hd_mode) return false;
        box.checked = snapshot.hd_mode;
        box.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
    }

    const api = { VERSION, capture, forStorage, sameView, key, contains, restoreViewport,
                  restoreHdMode, channels };
    global.PlexoraViewSnapshot = api;
    if (typeof globalThis !== "undefined" && globalThis !== global) {
        globalThis.PlexoraViewSnapshot = api;
    }
})(typeof window !== "undefined" ? window : globalThis);
