/**
 * PlexoraTileProgress -- the view sharpening, as a bar along the navbar's
 * bottom edge.
 *
 * A viewer draws what it has at once -- a coarse level first -- and the full
 * resolution of the view arrives tile by tile after it. From a disk that is a
 * blink; from the web it is seconds to minutes, and the navbar used to say so
 * with a word ("Loading") and a chip whose bar only swept back and forth:
 * neither said how far along the view was. This bar does: its width is the
 * share of the view's full-resolution tiles that have arrived, across every
 * channel drawn, and it fades out once they all have.
 *
 * MEASURED FROM THE VIEWER'S OWN PASS. Each frame OpenSeadragon walks every
 * image's levels from the finest it would draw at this zoom down to the
 * coarsest it needs for coverage (TiledImage `_updateLevelsForViewport`),
 * raising `update-level` for each level and `update-tile` for each tile of
 * the view at it. The first level of a pass is the one the view is sharp at;
 * its tiles, and which of them are `loaded`, are the measure. A pass is told
 * from the next by the time OSD stamps on all its levels (`currenttime`). An
 * image whose view is complete (`getFullyLoaded`) is not walked again, and
 * counts as complete.
 *
 * Monotone while the view stands still, so the bar only moves forward; when
 * the view moves (`viewport-change`) the share is taken as it is, since a pan
 * to an unloaded area really is less complete. Nothing shows for a load that
 * finishes inside SHOW_AFTER_MS -- a warm channel toggle should not flash.
 *
 *   PlexoraTileProgress.watch(viewer)   once per OSD viewer (appStatus.js
 *                                       `watchViewer` calls it)
 *   PlexoraTileProgress.state()         {active, shown, value} (tests)
 *
 * Mounts in #tile_progress (templates/base.html), inside the navbar.
 */
window.PlexoraTileProgress = (function () {
    "use strict";

    //: A load that finishes sooner draws no bar.
    const SHOW_AFTER_MS = 200;
    //: Full, this long, before it fades.
    const DONE_HOLD_MS = 220;
    //: The fade's length (the CSS transition on .nav-load-bar).
    const FADE_MS = 320;

    const viewers = new Set();
    //: TiledImage -> {time, level, total, loaded}: its latest pass.
    const passes = new WeakMap();
    let root = null;
    let fill = null;
    let scheduled = false;
    let moved = false;
    let session = null;
    let fadeTimer = null;

    function mount() {
        if (root) return root;
        root = document.getElementById("tile_progress");
        if (!root) return null;
        fill = root.querySelector(".nav-load-bar-fill");
        if (!fill) {
            fill = document.createElement("span");
            fill.className = "nav-load-bar-fill";
            root.appendChild(fill);
        }
        return root;
    }

    function items() {
        const out = [];
        for (const viewer of viewers) {
            const world = viewer.world;
            if (!world) continue;
            for (let i = 0; i < world.getItemCount(); i += 1) out.push(world.getItemAt(i));
        }
        return out;
    }

    //: {busy, value}: whether any image is still loading, and the share of
    //: the view's full-resolution tiles that have arrived.
    function measure() {
        let busy = false;
        let total = 0;
        let loaded = 0;
        for (const item of items()) {
            const complete = !item.getFullyLoaded || item.getFullyLoaded();
            if (!complete) busy = true;
            const pass = passes.get(item);
            if (!pass || !pass.total) continue;
            total += pass.total;
            loaded += complete ? pass.total : pass.loaded;
        }
        return { busy, value: total ? loaded / total : 0 };
    }

    function paint(value) {
        if (!mount()) return;
        fill.style.width = `${(Math.max(0, Math.min(1, value)) * 100).toFixed(2)}%`;
        root.setAttribute("aria-valuenow", String(Math.round(value * 100)));
    }

    function show() {
        if (!mount() || !session) return;
        window.clearTimeout(fadeTimer);
        root.classList.remove("is-done");
        root.hidden = false;
        session.shown = true;
        paint(session.value);
    }

    function finish() {
        const was = session;
        session = null;
        if (!was || !was.shown || !mount()) {
            if (was) window.clearTimeout(was.showTimer);
            return;
        }
        paint(1);
        fadeTimer = window.setTimeout(() => {
            root.classList.add("is-done");
            fadeTimer = window.setTimeout(() => {
                root.hidden = true;
                root.classList.remove("is-done");
                // Back to empty with no transition, ready for the next load.
                fill.style.transition = "none";
                paint(0);
                void fill.offsetWidth;
                fill.style.transition = "";
            }, FADE_MS);
        }, DONE_HOLD_MS);
    }

    function update() {
        scheduled = false;
        const { busy, value } = measure();
        if (!busy) {
            finish();
            return;
        }
        if (!session) {
            session = { value: 0, shown: false, showTimer: null };
            session.showTimer = window.setTimeout(() => {
                if (session && !session.shown) show();
            }, SHOW_AFTER_MS);
        }
        session.value = moved ? value : Math.max(session.value, value);
        moved = false;
        if (session.shown) paint(session.value);
    }

    //: Measured once a frame at most, after OSD's pass has finished.
    function schedule() {
        if (scheduled) return;
        scheduled = true;
        window.requestAnimationFrame(update);
    }

    function watch(viewer) {
        if (!viewer || !viewer.addHandler || viewer.__plexoraTileProgress) return;
        viewer.__plexoraTileProgress = true;
        viewers.add(viewer);
        viewer.addHandler("update-level", (event) => {
            const item = event.tiledImage;
            const pass = passes.get(item);
            // The first level of a pass is the finest one the view is drawn at.
            if (!pass || pass.time !== event.currenttime) {
                passes.set(item, { time: event.currenttime, level: event.level, total: 0, loaded: 0 });
            }
            schedule();
        });
        viewer.addHandler("update-tile", (event) => {
            const pass = passes.get(event.tiledImage);
            const tile = event.tile;
            if (!pass || !tile || tile.level !== pass.level || tile.exists === false) return;
            pass.total += 1;
            if (tile.loaded) pass.loaded += 1;
        });
        viewer.addHandler("viewport-change", () => { moved = true; });
        // A channel switched on or off changes what is loading with no pass
        // of the others; one that finishes raises its own change.
        const changed = () => schedule();
        if (viewer.world) {
            viewer.world.addHandler("add-item", (event) => {
                event.item?.addHandler?.("fully-loaded-change", changed);
                changed();
            });
            viewer.world.addHandler("remove-item", changed);
            for (let i = 0; i < viewer.world.getItemCount(); i += 1) {
                viewer.world.getItemAt(i).addHandler?.("fully-loaded-change", changed);
            }
        }
        // Closed, its world is empty; the same viewer opens the next image.
        viewer.addHandler("close", changed);
        changed();
    }

    function state() {
        return { active: Boolean(session), shown: Boolean(session && session.shown),
                 value: session ? session.value : null };
    }

    return { watch, state };
})();
