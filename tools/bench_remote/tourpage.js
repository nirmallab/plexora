/*
 * tourpage.js - page-side helpers for the remote-access tour (window.__rb).
 *
 * Injected after ../bench3d/probes.js and pagekit.js. A step is "full" only
 * when every visible channel item is fully loaded AND draws the finest level
 * OpenSeadragon itself wants for the current view (the highest level whose
 * render pixel ratio reaches the viewer's minPixelRatio -- the test
 * TiledImage._updateLevelsForViewport applies) AND, in HD mode, is an HD
 * source. A coarser level stretched over the view, or the 8-bit preview
 * an HD swap starts from, is never "full".
 */
(function () {
    "use strict";
    if (window.__rb) return;
    const now = () => performance.now();
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const k = () => window.__bench.k;
    const RB = window.__rb = { loads: [], hooked: null, updates: 0 };

    function visibleChannels() {
        const v = k().viewer();
        if (!v) return [];
        return k().channelItems(v.world).filter((item) => item.getOpacity() > 0);
    }
    /** The finest level OpenSeadragon draws for `item` at the current view. */
    function targetLevel(item) {
        const source = item.source;
        const min = source.minLevel || 0;
        for (let level = source.maxLevel; level >= min; level -= 1) {
            const ratio = item.viewport.deltaPixelsFromPointsNoRotate(
                source.getPixelRatio(level), true).x * item._scaleSpring.current.value;
            if (ratio >= item.minPixelRatio) return level;
        }
        return min;
    }
    function finestOf(item) {
        let best = null;
        for (const info of item._lastDrawn || []) {
            const level = info.level ?? info.tile?.level;
            if (Number.isFinite(level)) best = best === null ? level : Math.max(best, level);
        }
        return best;
    }
    function hdWanted() {
        return Boolean(k().P().seaDragonViewer?.viewerManagerVMain?.isHdMode?.());
    }
    /** {full, items: [{target, finest, loaded, hd}]} for the view as it is now. */
    function state() {
        const list = visibleChannels();
        const hd = hdWanted();
        const items = list.map((item) => ({
            target: targetLevel(item), finest: finestOf(item), loaded: item.getFullyLoaded(),
            hd: Boolean(item.source?.hd),
        }));
        const full = items.length > 0 && items.every((it) =>
            it.loaded && it.finest !== null && it.finest >= it.target && (!hd || it.hd));
        return { full, hd, items };
    }
    /** Count tile loads of channel items (first pixel of a step). */
    function hook() {
        const v = k().viewer();
        if (!v || RB.hooked === v) return;
        RB.hooked = v;
        v.addHandler("tile-loaded", (event) => {
            if (event?.tiledImage?.source?.tileFormat === 16) RB.loads.push(now());
        });
        // Each redraw recomputes what every item draws and whether it is
        // fully loaded: a state read before the first one after a move is
        // the previous view's.
        v.addHandler("update-viewport", () => { RB.updates += 1; });
    }
    /** The viewport's springs at rest (an animated move has ended). */
    function still() {
        const vp = k().viewer()?.viewport;
        if (!vp) return false;
        return [vp.zoomSpring, vp.centerSpringX, vp.centerSpringY]
            .every((spring) => !spring || spring.isAtTargetValue());
    }
    /** The main surface shows the plane the view state asks for (pagekit). */
    function onPlane() {
        const spec = k().vs()?.spec?.();
        if (!spec) return true;
        return k().shows(window.__bench.sig?.main, spec);
    }
    /**
     * Wait for the step that began at `t0`: ms to the first tile loaded after
     * it, to meaningful (pagekit: every visible channel covers half the view at
     * one level), and to full (above) held for two polls. Null where a time
     * was not reached within `ms`.
     */
    async function waitStep(t0, ms = 120000, u0 = null) {
        hook();
        const end = t0 + ms;
        // Two redraws after the move, the first of which may have started
        // before it.
        const updatesFrom = u0 ?? RB.updates;
        let firstPixel = null;
        let meaningful = null;
        let since = null;
        let polls = 0;
        let last = null;
        while (now() < end) {
            hook();
            if (firstPixel === null) {
                const t = RB.loads.find((x) => x >= t0);
                if (t !== undefined) firstPixel = t - t0;
            }
            if (meaningful === null && k().booted() && k().meaningful()) meaningful = now() - t0;
            last = k().booted() ? state() : null;
            const fresh = RB.updates >= updatesFrom + 2 && still() && onPlane();
            if (last && last.full && fresh) {
                since ??= now();
                polls += 1;
                if (polls >= 2) {
                    return { firstPixel, meaningful, full: since - t0, state: last };
                }
            } else {
                since = null;
                polls = 0;
            }
            await sleep(5);
        }
        return { firstPixel, meaningful, full: null, state: last };
    }
    /** Put the view at `ratio` screen px per image px around image point `at`. */
    function place(ratio, at) {
        const v = k().viewer();
        const item = visibleChannels()[0] || v.world.getItemAt(0);
        v.viewport.zoomTo(item.imageToViewportZoom(ratio), null, true);
        v.viewport.panTo(item.imageToViewportCoordinates(at[0], at[1]), true);
        v.viewport.applyConstraints(true);
    }
    function imageSize() {
        const item = visibleChannels()[0] || k().viewer()?.world.getItemAt(0);
        const size = item?.source?.dimensions;
        return size ? [size.x, size.y] : null;
    }
    RB.state = state;
    RB.targetLevel = targetLevel;
    RB.waitStep = waitStep;
    RB.place = place;
    RB.imageSize = imageSize;
    RB.hook = hook;
})();
