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
 * READY IS "FULL RESOLUTION", OR "THE WHOLE VIEW AT ANY RESOLUTION" A MOMENT
 * LATER (`whenCanDraw`'s `grace`). OpenSeadragon draws a coarser level under
 * every tile of the finer one it is still waiting for, and the tile scheduler
 * (tileFetch.js) asks for coarse levels first -- so a few small tiles cover
 * the view long before the full-resolution ones arrive. A channel whose full
 * resolution is that quick comes in sharp; one that is not (a cold plane of a
 * volume on a data node) comes in from its coarse level after GRACE_MS and
 * sharpens in place, instead of the screen holding still until it can be
 * shown at its best. `covers` is that test: OSD's own coverage needs a tile
 * DRAWN at full opacity, which an item held at nothing never is, so this
 * reads which tiles each level needs on screen (`coverage`, refreshed every
 * update) against which of them have loaded (`tilesMatrix`).
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
    //: How long full resolution gets before a channel that already covers the
    //: view at a coarser level comes in with that (see `whenCanDraw`): long
    //: enough that a warm plane is never shown soft for a frame, short enough
    //: that a cold one is on screen before the wait reads as a stall.
    const GRACE_MS = 140;
    //: How often an item's coverage is looked at while it is held back.
    const COVER_POLL_MS = 40;

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
     * Whether `item` can draw the whole view now, at SOME level: every tile
     * one level needs on screen (OSD's `coverage[level]`, written on each
     * update) has loaded (`tilesMatrix`). False for an item OSD has not
     * updated yet, or whose internals are not these.
     */
    function covers(item) {
        const coverage = item?.coverage;
        const matrix = item?.tilesMatrix;
        if (!coverage || !matrix) return false;
        for (const level of Object.keys(coverage)) {
            const columns = coverage[level];
            const tiles = matrix[level];
            if (!columns || !tiles) continue;
            let seen = 0;
            let whole = true;
            for (const x of Object.keys(columns)) {
                for (const y of Object.keys(columns[x] || {})) {
                    seen += 1;
                    const tile = tiles[x]?.[y];
                    // A tile past the image's edge (`exists` false) needs nothing.
                    if (!tile || (tile.exists !== false && !tile.loaded)) {
                        whole = false;
                        break;
                    }
                }
                if (!whole) break;
            }
            if (whole && seen) return true;
        }
        return false;
    }

    /**
     * Call `done` once `item` can draw what the view needs: OpenSeadragon's
     * `fully-loaded`, which Plexora's decode has already answered by the time
     * it flips (viewerManager.whenItemCanDraw says why) -- or, with `grace`,
     * once that long has passed and the item covers the view at a coarser
     * level (`covers`). An item that cannot answer is ready at once. Returns
     * a function that stops listening.
     */
    function whenCanDraw(item, done, { grace = 0 } = {}) {
        if (typeof item?.getFullyLoaded !== "function" || typeof item?.addHandler !== "function"
                || item.getFullyLoaded()) {
            done();
            return () => {};
        }
        const start = now();
        let over = false;
        let poll = null;
        const stop = () => {
            over = true;
            item.removeHandler?.("fully-loaded-change", onChange);
            if (poll !== null) clearTimeout(poll);
            poll = null;
        };
        const finish = () => {
            if (over) return;
            stop();
            done();
        };
        const onChange = () => {
            if (item.getFullyLoaded()) finish();
        };
        item.addHandler("fully-loaded-change", onChange);
        if (grace > 0) {
            const check = () => {
                poll = null;
                if (over) return;
                if (now() - start >= grace && covers(item)) {
                    finish();
                    return;
                }
                poll = setTimeout(check, COVER_POLL_MS);
            };
            poll = setTimeout(check, Math.min(grace, COVER_POLL_MS));
        }
        return stop;
    }

    /** `whenCanDraw` with the channel fade's grace: full resolution if it is
     *  quick, the view covered at any level if it is not. */
    function whenCanDrawSoon(item, done) {
        return whenCanDraw(item, done, { grace: GRACE_MS });
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
    function group(count, { target = () => 1, ready = whenCanDrawSoon, onDone = null,
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

    const ChannelFade = { group, fadeIn, smooth, ease, whenCanDraw, whenCanDrawSoon, covers,
                          FADE_MS, READY_CEILING_MS, GRACE_MS };
    global.PlexoraChannelFade = ChannelFade;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = ChannelFade;
    }
})(typeof window !== "undefined" ? window : globalThis);
