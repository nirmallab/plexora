/**
 * pageBoot.js -- one place that knows how to mount a page's controller.
 *
 * Every page controller in this tree already had the same two lines at the
 * bottom: wait for DOMContentLoaded, then look for my root element and give up
 * quietly if it is not there. That shape is exactly right, and it is also
 * exactly what stops working once appRouter.js starts swapping pages in without
 * a document load -- DOMContentLoaded fires once per document, and the second
 * visit to a page never gets one.
 *
 * So a controller registers its mount function here instead:
 *
 *     PlexoraPage.register(function () {
 *         const root = document.getElementById("my-page");
 *         if (!root) return;            // not my page -- exactly as before
 *         ...
 *         return () => clearInterval(timer);   // optional teardown
 *     });
 *
 * and this file runs it on the initial load AND after every router swap. The
 * `if (!root) return` guard is what makes that safe: `boot()` runs every
 * registered controller and all but the relevant one no-op, so no controller
 * has to be told which page is showing.
 *
 * The returned cleanup is for state that outlives the markup -- a poll timer, a
 * window-level listener. A controller that only binds handlers to nodes inside
 * its own root needs none: those nodes are dropped with the page.
 *
 * Registration order is load order and nothing depends on it; two controllers
 * are never on screen at once.
 */
window.PlexoraPage = (function () {
    "use strict";

    //: { fn, generation, cleanup } per registered controller. `generation` is
    //: which mount it last ran for, which is what keeps a controller that
    //: registered DURING a swap (its script had not been loaded before) from
    //: also being run by the boot() call that follows.
    const controllers = [];

    //: Bumped by unmount(). Everything mounted against the old markup is
    //: therefore stale by definition, without having to track nodes.
    let generation = 0;

    function onReady(fn) {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", fn);
        } else {
            fn();
        }
    }

    function run(entry) {
        if (entry.generation === generation) return;
        entry.generation = generation;
        try {
            // A FUNCTION or nothing. Several of these controllers are one-liners
            // around a boot() that answers with the instance it built, and an
            // instance is truthy -- stored as a cleanup it would be called on
            // the way out and throw, which is a console full of noise from
            // controllers that were doing nothing wrong.
            const cleanup = entry.fn();
            entry.cleanup = typeof cleanup === "function" ? cleanup : null;
        } catch (error) {
            // One page's controller failing must not take the router -- or the
            // viewer sitting behind it -- down with it.
            console.error("Plexora: page controller failed to mount", error);
            entry.cleanup = null;
        }
    }

    /**
     * Declare a page controller. Mounts it now (or on DOMContentLoaded) and
     * again after every later router swap.
     */
    function register(fn) {
        if (typeof fn !== "function") return;
        const entry = { fn: fn, generation: -1, cleanup: null };
        controllers.push(entry);
        onReady(() => run(entry));
    }

    /** Mount every registered controller against whatever is on screen now. */
    function boot() {
        controllers.forEach(run);
    }

    /** Run the teardown of everything mounted against the outgoing markup. */
    function unmount() {
        for (const entry of controllers) {
            if (entry.generation !== generation) continue;
            try {
                entry.cleanup?.();
            } catch (error) {
                console.error("Plexora: page controller failed to unmount", error);
            }
            entry.cleanup = null;
        }
        generation += 1;
    }

    return { register, boot, unmount };
})();
