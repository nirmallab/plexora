/**
 * PlexoraViewerLoader -- the spinner in the middle of the image viewer.
 *
 * The rule it enforces is one sentence: while the viewer is not showing an
 * image, something on screen has to say so. The loader used to be `display:
 * none` by default with exactly one caller -- ImageViewer.setLoading -- wired
 * to the centroid and segmentation fetches. Nothing showed it for the image
 * tiles themselves, which is the part the user actually waits for, so the
 * common case was a black rectangle with no explanation: boot a project, wait
 * several seconds, then have tiles appear. The markup is in index.html, so the
 * server-rendered page can show the spinner before a single script has run;
 * this file is what takes it down, and what puts it back up.
 *
 * Three inputs, combined rather than fighting each other:
 *
 *   holds    explicit "I am doing something" from feature code, ref-counted.
 *            `setLoading(true/false)` pairs in ImageViewer are these. A bare
 *            boolean made two overlapping pairs cancel each other, which is
 *            how the spinner used to vanish mid-load.
 *   painted  a tile has actually been drawn since the world was last empty.
 *            Not "loaded" and not "fully loaded": the question the user is
 *            asking is whether there is anything to look at.
 *   booted   main.js's own startup has settled. Before that, the viewer is
 *            empty because nothing has been asked for yet, which still looks
 *            exactly like a blank page and still deserves a spinner.
 *
 * Visible iff `holds > 0 || (!painted && (the world has a layer || !booted))`.
 * The world-is-empty-after-boot case is deliberately NOT loading: a project
 * with every channel switched off is showing exactly what was asked for.
 *
 * A classic script, like every other service here; no build step, no imports.
 */
window.PlexoraViewerLoader = (function () {
    "use strict";

    const HIDDEN_CLASS = "is-hidden";

    let holds = 0;
    let painted = false;
    let booted = false;
    let world = null;

    function isLoading() {
        if (holds > 0) return true;
        if (painted) return false;
        return (world ? world.getItemCount() > 0 : false) || !booted;
    }

    function render() {
        // Absent on every page that is not the viewer -- the quick-view
        // landing, Settings, a plugin page -- where this whole file is a no-op
        // rather than a reason to throw.
        const loader = document.getElementById("openseadragon_loader");
        if (!loader) return;
        if (isLoading()) loader.classList.remove(HIDDEN_CLASS);
        else loader.classList.add(HIDDEN_CLASS);
    }

    /**
     * Follow a viewer's world: what is in it, and whether any of it has drawn.
     *
     * `tile-drawn` rather than `fully-loaded-change` because the two answer
     * different questions. Fully-loaded means every tile at the current zoom
     * has arrived, which for a large image is many seconds after the first one
     * is on screen -- and the spinner has no business sitting over a picture
     * the user can already see. One tile drawn is the moment the viewer stops
     * being blank.
     */
    function watch(viewer) {
        if (!viewer || !viewer.addHandler || viewer.__plexoraLoaderWatched) return;
        viewer.__plexoraLoaderWatched = true;

        // One persistent handler that returns early once something has drawn,
        // rather than adding and removing one per load. OSD's addOnceHandler
        // wraps the function it is given, so the wrapper is what would have to
        // be removed, and a re-attach on every add-item would stack copies.
        // This runs per drawn tile for the life of the page; the early return
        // is the entire body in the steady state.
        const drawn = viewer.addHandler("tile-drawn", () => {
            if (painted) return;
            painted = true;
            render();
        });

        // A drawer that does not raise tile-drawn at all -- addHandler answers
        // false -- would otherwise spin forever. The RGB viewer takes this
        // path in a WebGL browser. Second best, but wrong only for as long as
        // the first tile takes: the layer being in the world is the signal.
        const tileDrawnHeard = drawn !== false;

        if (viewer.world) {
            world = viewer.world;
            const onAdd = () => {
                if (!tileDrawnHeard) painted = true;
                render();
            };
            world.addHandler("add-item", onAdd);
            world.addHandler("remove-item", () => {
                // Back to blank: a channel switched off, a resolution swap
                // tearing the layers down. The next image to arrive has to
                // earn `painted` again, or a rebuild would show no spinner.
                if (world.getItemCount() === 0) painted = false;
                render();
            });
        }

        // Tiles go through OSD's own XHR, so this is the only way a failed
        // load reaches this file. Treated as painted: the navbar chip and the
        // resource banner report the failure, and a spinner that never stops
        // on top of that message says the app is still trying when it is not.
        viewer.addHandler("tile-load-failed", () => {
            if (painted) return;
            painted = true;
            render();
        });

        render();
    }

    /**
     * Claim the spinner for a piece of work. Returns its release, which counts
     * once however many times it is called -- a release in a `finally` that
     * also ran on the success path would otherwise take the spinner down while
     * another feature was still waiting on it.
     */
    function hold() {
        holds += 1;
        render();
        let released = false;
        return function release() {
            if (released) return;
            released = true;
            holds -= 1;
            render();
        };
    }

    /**
     * main.js has finished booting. From here an empty world means "showing
     * nothing on purpose" rather than "has not started yet".
     *
     * Deferred by a turn on purpose. OSD builds a tile source inside a bare
     * setTimeout, so every addTiledImage issued during startup has a 0 ms timer
     * queued BEFORE this one; settling synchronously would see an empty world,
     * hide the spinner, and show it again a few milliseconds later. Same-delay
     * timers run in order, so this always lands after those add-item events.
     */
    function settle() {
        setTimeout(function () {
            booted = true;
            render();
        }, 0);
    }

    return { watch, hold, settle };
})();
