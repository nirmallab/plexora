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
 * A FAILED TILE IS NOT COVERAGE until its retries are spent. OSD marks a tile
 * that failed `exists` false -- what it also calls a tile past the image's
 * edge -- and asks for it again up to `tileRetryMax` times; the tile scheduler
 * (tileFetch.js `fail`) tags which it is: "retrying" (and "aborted") is still
 * awaited, "final" is a hole the view counts as covered without, so a plane
 * with holes still hands over (and says so) rather than holding the old
 * picture forever -- unless it is ALL holes (below). `tileFetch.retryFailed`
 * asks for the holes again.
 *
 * NOR IS IT "FULLY LOADED". OpenSeadragon 6 stops counting a tile as loading
 * the moment it fails (`exists` false, between its retries), so an item whose
 * every tile failed says `getFullyLoaded()` with nothing drawn -- and a swap
 * that took the flag at its word handed over to a black plane.
 * `settledLoaded(item)` is the flag believed only while no tile is
 * "retrying"; `whenCanDraw` reads it on entry and on `fully-loaded-change`,
 * and polls it (COVER_POLL_MS) while OSD says loaded and a retry is out: a
 * retry that lands, or is spent, need not move OSD's flag again. A spent one
 * ("final") is a hole, and the item then counts as loaded with it.
 *
 * BUT NEVER ALL HOLES. An item with nothing to draw -- no tile the view needs
 * has loaded -- is never ready, whatever OSD or `covers` say: handing over to
 * it shows black where a picture was. `failedOut(item)` is the case that will
 * not end by itself: nothing loaded, nothing retrying, a tile failed for
 * good. `whenCanDraw` keeps waiting on it and says so once (`onFailed`), so a
 * swap can say "error" with the old picture still up; `tileFetch.retryFailed`
 * asks again, and the wait hands over once a tile lands. A partial failure
 * (something loaded, some holes) is ready, holes and all.
 *
 * `coveringLevel(item)` is the finest level that covers the view, or null.
 * `progress(item)` is `{level, needed, loaded, failed, covering}`: `level` the
 * level OSD shows the view at (the finest it visited on its last update),
 * `needed` the tiles of it on screen, `loaded` how many of those have loaded,
 * `failed` how many failed for good, `covering` = `coveringLevel(item)` (a
 * level or null: test it against null, 0 is the coarsest level). Zeros and
 * nulls for an item OSD has not updated. What a loading indicator counts.
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
    //: How often an item's coverage is looked at while it is held back:
    //: about a frame, the soonest OSD's draw can have counted a tile that
    //: landed. A drag's preview is ready the moment its plane covers the
    //: view; at 40 ms this poll alone was a fifth of each preview.
    const COVER_POLL_MS = 16;

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
     * Whether `tile` asks nothing more of its level: loaded; or missing for
     * good -- past the image's edge (`exists` false, untagged) or failed
     * with its retries spent (tagged "final" by tileFetch.js), a hole. A
     * tile that failed and is being asked for again ("retrying"), or whose
     * download was aborted, is still awaited.
     */
    function settled(tile) {
        if (!tile) return false;
        if (tile.loaded) return true;
        if (tile.exists !== false) return false;
        const failed = tile._plexoraFailed;
        return !failed || failed === "final";
    }

    /** A tile `progress` leaves out: past the image's edge, never asked for. */
    function pastEdge(tile) {
        return Boolean(tile) && !tile.loaded && tile.exists === false && !tile._plexoraFailed;
    }

    /** Whether every tile `level` of `item` needs on screen is settled. A
     *  level with no tiles at all proves nothing. */
    function levelCovers(item, level) {
        const columns = item.coverage[level];
        const tiles = item.tilesMatrix[level];
        if (!columns || !tiles) return false;
        let seen = 0;
        for (const x of Object.keys(columns)) {
            for (const y of Object.keys(columns[x] || {})) {
                seen += 1;
                if (!settled(tiles[x]?.[y])) return false;
            }
        }
        return seen > 0;
    }

    /**
     * Whether `item` can draw the whole view now, at SOME level: every tile
     * one level needs on screen (OSD's `coverage[level]`, written on each
     * update) has loaded (`tilesMatrix`), or is a hole for good (`settled`).
     * False for an item OSD has not updated yet, or whose internals are not
     * these.
     */
    function covers(item) {
        if (!item?.coverage || !item?.tilesMatrix) return false;
        for (const level of Object.keys(item.coverage)) {
            if (levelCovers(item, level)) return true;
        }
        return false;
    }

    /** The finest level at which `item` covers the view (`covers`), or null. */
    function coveringLevel(item) {
        if (!item?.coverage || !item?.tilesMatrix) return null;
        const levels = Object.keys(item.coverage).map(Number)
            .filter(Number.isFinite).sort((a, b) => b - a);
        for (const level of levels) {
            if (levelCovers(item, level)) return level;
        }
        return null;
    }

    /**
     * The level OSD shows `item`'s view at: the finest it visited on its last
     * update. OSD rebuilds `_tilesToDraw` from that level down on every
     * update, so its length says which; without it, the finest level in
     * `coverage` -- which also keeps the levels a zoom out has left behind.
     */
    function displayLevel(item) {
        const coverage = item.coverage;
        const drawn = item._tilesToDraw;
        if (Array.isArray(drawn) && drawn.length > 0 && coverage[drawn.length - 1]) {
            return drawn.length - 1;
        }
        let top = null;
        for (const key of Object.keys(coverage)) {
            const level = Number(key);
            if (Number.isFinite(level) && (top === null || level > top)) top = level;
        }
        return top;
    }

    /** How far `item` is with the view (see the header):
     *  `{level, needed, loaded, failed, covering}`. */
    function progress(item) {
        const out = { level: null, needed: 0, loaded: 0, failed: 0, covering: null };
        if (!item?.coverage || !item?.tilesMatrix) return out;
        const level = displayLevel(item);
        if (level === null) return out;
        out.level = level;
        const columns = item.coverage[level] || {};
        const tiles = item.tilesMatrix[level] || {};
        for (const x of Object.keys(columns)) {
            for (const y of Object.keys(columns[x] || {})) {
                const tile = tiles[x]?.[y];
                if (pastEdge(tile)) continue;
                out.needed += 1;
                if (tile?.loaded) out.loaded += 1;
                else if (tile?._plexoraFailed === "final") out.failed += 1;
            }
        }
        out.covering = coveringLevel(item);
        return out;
    }

    /** OSD's `getFullyLoaded()`, believed only while no tile of `item` is
     *  being asked for again after a failure (see the header). */
    function settledLoaded(item) {
        if (!item?.getFullyLoaded?.()) return false;
        for (const level of Object.values(item.tilesMatrix || {})) {
            for (const column of Object.values(level || {})) {
                for (const tile of Object.values(column || {})) {
                    if (tile?._plexoraFailed === "retrying" && !tile.loaded) return false;
                }
            }
        }
        return true;
    }

    /** `{needed, loaded, retrying, failed}` over every tile any level of
     *  `item` needs on screen (past the image's edge left out), or null for an
     *  item OSD has not updated. */
    function tally(item) {
        if (!item?.coverage || !item?.tilesMatrix) return null;
        const out = { needed: 0, loaded: 0, retrying: 0, failed: 0 };
        for (const level of Object.keys(item.coverage)) {
            const columns = item.coverage[level] || {};
            const tiles = item.tilesMatrix[level] || {};
            for (const x of Object.keys(columns)) {
                for (const y of Object.keys(columns[x] || {})) {
                    const tile = tiles[x]?.[y];
                    if (pastEdge(tile)) continue;
                    out.needed += 1;
                    if (tile?.loaded) out.loaded += 1;
                    else if (tile?._plexoraFailed === "retrying") out.retrying += 1;
                    else if (tile?._plexoraFailed === "final") out.failed += 1;
                }
            }
        }
        return out;
    }

    /** Whether `item` has nothing to draw: tiles the view needs, none loaded
     *  (see the header). False for an item OSD has not updated. */
    function blank(item) {
        const t = tally(item);
        return Boolean(t) && t.needed > 0 && t.loaded === 0;
    }

    /** Whether `item` is blank for good: nothing loaded, nothing retrying, a
     *  tile failed for good -- until `tileFetch.retryFailed` asks again. */
    function failedOut(item) {
        const t = tally(item);
        return Boolean(t) && t.loaded === 0 && t.retrying === 0 && t.failed > 0;
    }

    /**
     * Call `done` once `item` can draw what the view needs: OpenSeadragon's
     * `fully-loaded` (`settledLoaded`: not while a tile is retrying), which
     * Plexora's decode has already answered by the time it flips
     * (viewerManager.whenItemCanDraw says why) -- or, with `grace`,
     * once that long has passed and the item covers the view at a coarser
     * level (`covers`). `grace` null (the default): coverage never counts (an
     * HD swap waits for the new quality in full). 0: as soon as it covers --
     * looked at now, then every COVER_POLL_MS (a preview while the hand
     * moves). Never for an item with nothing to draw (`blank`): one whose
     * every tile failed for good (`failedOut`) waits for a retry, and
     * `onFailed(item)` hears it, once each time it gets there. An item that
     * cannot answer is ready at once. Returns a function that stops
     * listening.
     */
    function whenCanDraw(item, done, { grace = null, onFailed = null } = {}) {
        if (typeof item?.getFullyLoaded !== "function" || typeof item?.addHandler !== "function"
                || (settledLoaded(item) && !blank(item))) {
            done();
            return () => {};
        }
        // `null >= 0` is true in JS: null is said out loud.
        const graced = grace !== null && grace >= 0;
        if (graced && grace === 0 && covers(item) && !blank(item)) {
            done();
            return () => {};
        }
        const start = now();
        let over = false;
        let poll = null;
        //: `onFailed` was told, and the item has not left that state since.
        let toldFailed = false;
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
        const ready = () => !blank(item)
            && (settledLoaded(item) || (graced && now() - start >= grace && covers(item)));
        const failing = () => {
            const out = failedOut(item);
            if (out && !toldFailed) onFailed?.(item);
            toldFailed = out;
        };
        const check = () => {
            poll = null;
            if (over) return;
            if (ready()) {
                finish();
                return;
            }
            failing();
            arm();
        };
        // With a grace, always; without, only while OSD says loaded (a retry
        // out, or nothing to draw) -- otherwise `fully-loaded-change` will say.
        const arm = () => {
            if (!over && poll === null && (graced || item.getFullyLoaded())) {
                poll = setTimeout(check, COVER_POLL_MS);
            }
        };
        const onChange = () => {
            if (ready()) {
                finish();
                return;
            }
            failing();
            arm();
        };
        item.addHandler("fully-loaded-change", onChange);
        failing();
        // Grace 0 was looked at just now.
        if (graced && grace > 0) poll = setTimeout(check, Math.min(grace, COVER_POLL_MS));
        else arm();
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
                          coveringLevel, progress, settledLoaded, blank, failedOut, FADE_MS, READY_CEILING_MS, GRACE_MS,
                          COVER_POLL_MS };
    global.PlexoraChannelFade = ChannelFade;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = ChannelFade;
    }
})(typeof window !== "undefined" ? window : globalThis);
