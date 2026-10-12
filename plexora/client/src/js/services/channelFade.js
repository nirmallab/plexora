/**
 * channelFade.js - a channel switched on comes in rather than lands.
 *
 * Every surface the image channel section drives adds a channel as
 * OpenSeadragon items: the plane (viewerManager.addChannelItems), a registered
 * layer's channels (viewerManager.addLayerChannelSet) and the XZ / YZ section
 * panes (sectionPanes.js). Added at full opacity, a channel appears tile by
 * tile as the bytes come in -- a burst. Here its items are added at nothing,
 * held there until every tile in view can draw (the same test a quality swap
 * waits on), and brought up together over FADE_MS, eased out. The 3-D view
 * does the same with a uniform (volumeRenderer.presence), over the same time.
 *
 * ALL OF A CHANNEL'S ITEMS AT ONCE. A plane or layer channel is a
 * `destination-out` cover and a `lighter` paint, and the composite at
 * presence p is `ground * (1 - p v) + p c v` only when both carry the same p:
 * the paint alone would brighten an H&E ground before the cover had taken it
 * away. So one fade owns every item of a channel (`group`).
 *
 * THE FADE OWNS THE ITEMS' OPACITY while it runs. What full presence means is
 * `target`, read every frame -- the base card's slider, a layer's slider --
 * and anything else that sets an item's opacity mid-fade
 * (applyReferenceState on a stack change, say) is put back in the same task
 * (`opacity-change`), so nothing can flash a channel to full and back.
 *
 * Held for READY_CEILING_MS at most: on a slow link the channel comes in with
 * what has arrived, and the rest follows tile by tile as it always did. With
 * motion off (services/viewerPrefs.js, or the system's reduced-motion
 * setting) a channel lands at once.
 *
 * Classic script, `window.PlexoraChannelFade`.
 */
(function (global) {
    "use strict";

    //: How long a channel takes to come in, once it can draw. The 3-D view's
    //: is the same (volumeRenderer.js FADE_IN_MS).
    const FADE_MS = 240;
    //: How long a channel is held back for its tiles before it comes in with
    //: what it has.
    const READY_CEILING_MS = 800;

    function now() {
        return global.performance?.now?.() ?? Date.now();
    }

    const frame = (run) => (typeof global.requestAnimationFrame === "function"
        ? global.requestAnimationFrame(run) : setTimeout(() => run(now()), 16));
    const cancelFrame = (id) => (typeof global.cancelAnimationFrame === "function"
        ? global.cancelAnimationFrame(id) : clearTimeout(id));

    /** Whether things may ease: the viewer's motion switch, or the system's
     *  reduced-motion setting where the page has no preferences. */
    function smooth() {
        const prefs = global.PlexoraViewerPrefs;
        if (typeof prefs?.smoothMotion === "function") return prefs.smoothMotion();
        try {
            return !global.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
        } catch (error) {
            return true;
        }
    }

    /** Eased out: quick to show, gentle to settle. */
    function ease(f) {
        const t = Math.min(1, Math.max(0, f));
        return 1 - (1 - t) ** 3;
    }

    /**
     * Call `done` once `item` can draw what the view needs: OpenSeadragon's
     * `fully-loaded`, which Plexora's decode has already answered by the time
     * it flips (viewerManager.whenItemCanDraw says why). An item that cannot
     * answer is ready at once. Returns a function that stops listening.
     */
    function whenCanDraw(item, done) {
        if (typeof item?.getFullyLoaded !== "function" || typeof item?.addHandler !== "function"
                || item.getFullyLoaded()) {
            done();
            return () => {};
        }
        const stop = () => item.removeHandler?.("fully-loaded-change", onChange);
        const onChange = () => {
            if (!item.getFullyLoaded()) return;
            stop();
            done();
        };
        item.addHandler("fully-loaded-change", onChange);
        return stop;
    }

    /** Whether `item` is still on its viewer's world (switched off mid-fade). */
    function onWorld(item) {
        const world = item?.viewer?.world;
        return !world || typeof world.getIndexOfItem !== "function" || world.getIndexOfItem(item) >= 0;
    }

    /**
     * One channel's fade, for `count` items that join as they land.
     *
     * Each item joins already on the world at opacity 0 (with `preload`, or
     * OpenSeadragon never fetches for it -- TiledImage.getDrawArea) and is
     * held there; once all `count` have answered (`join(null)` for one that
     * failed) and can draw, they come in together.
     *
     * @param target - what full presence is, read every frame.
     * @param onDone - `(items) => void`, once, when they are in full or gone.
     * @returns `{join(item | null), finish()}` -- finish ends it at once, in full.
     */
    function group(count, { target = () => 1, ready = whenCanDraw, onDone = null,
                            ms = FADE_MS, ceiling = READY_CEILING_MS } = {}) {
        const items = [];
        let answered = 0;
        //: joining -> waiting (for the tiles) -> fading -> done
        let state = "joining";
        let presence = 0;
        let start = null;
        let tick = 0;
        let timer = null;
        let applying = false;
        const stops = [];

        const level = () => Math.max(0, Math.min(1, Number(target()) || 0)) * presence;
        function apply() {
            const value = level();
            applying = true;
            try {
                for (const item of items) item.setOpacity?.(value);
            } finally {
                applying = false;
            }
        }
        // Anyone else setting the opacity mid-fade is answered in the same task.
        function guard() {
            if (!applying && state !== "done") apply();
        }
        function finish() {
            if (state === "done") return;
            state = "done";
            cancelFrame(tick);
            clearTimeout(timer);
            for (const stop of stops) stop?.();
            for (const item of items) item.removeHandler?.("opacity-change", guard);
            presence = 1;
            apply();
            onDone?.(items.slice());
        }
        function step() {
            tick = 0;
            if (state === "done") return;
            // Switched off (or replaced) while it came in: its items are gone.
            if (!items.every(onWorld)) {
                finish();
                return;
            }
            if (start !== null) presence = ease((now() - start) / ms);
            apply();
            if (presence >= 1) {
                finish();
                return;
            }
            tick = frame(step);
        }
        function begin() {
            if (state !== "waiting") return;
            state = "fading";
            start = now();
            clearTimeout(timer);
            for (const stop of stops) stop?.();
        }
        function joined() {
            if (!items.length) {
                state = "done";
                onDone?.([]);
                return;
            }
            if (!smooth()) {
                finish();
                return;
            }
            state = "waiting";
            let waiting = items.length;
            const one = () => {
                waiting -= 1;
                if (waiting <= 0) begin();
            };
            for (const item of items) stops.push(ready ? ready(item, one) : (one(), null));
            if (state === "waiting") timer = setTimeout(begin, ceiling);
            if (!tick) tick = frame(step);
        }
        return {
            join(item) {
                if (state === "done") {
                    // Late: after the others were already in full.
                    item?.setOpacity?.(Math.max(0, Math.min(1, Number(target()) || 0)));
                    return;
                }
                answered += 1;
                if (item) {
                    items.push(item);
                    item.addHandler?.("opacity-change", guard);
                    apply();
                }
                if (answered >= count && state === "joining") joined();
            },
            finish,
        };
    }

    /** Bring `items`, already on the world at opacity 0, in as one channel. */
    function fadeIn(items, options = {}) {
        const list = (items || []).filter(Boolean);
        const fade = group(list.length, options);
        if (!list.length) fade.join(null);
        for (const item of list) fade.join(item);
        return fade;
    }

    const ChannelFade = { group, fadeIn, smooth, ease, whenCanDraw, FADE_MS, READY_CEILING_MS };
    global.PlexoraChannelFade = ChannelFade;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = ChannelFade;
    }
})(typeof window !== "undefined" ? window : globalThis);
