/**
 * PlexoraStatus -- the application's single status indicator.
 *
 * Lives at the far right of the navbar on every page that extends base.html.
 * Three states, using the shared accent tokens from tokens.css:
 *
 *   green  "Live"          nothing outstanding, the view is current
 *   orange <1-2 word label> something is running that has no other visible sign
 *   red    <short reason>   the server is unreachable, or a request failed
 *
 * Why this exists as one system rather than per feature: the app used to
 * indicate work with `d3.select("body").style("cursor", "progress")` at two
 * call sites in main.js -- both of which set and cleared the cursor
 * *synchronously around an async call*, so they were on screen for microseconds
 * and told the user nothing -- plus an opacity fade on individual auto buttons.
 * Anything slow that wasn't one of those (auto-contrast, tiles streaming in
 * after a channel toggle) was completely silent, which is what made a slow
 * operation feel like a broken one.
 *
 * Usage:
 *   const task = PlexoraStatus.begin("Auto-contrast");
 *   ... ; task.done();                       // or task.fail("Fit failed")
 *   await PlexoraStatus.track("Saving", promise);
 *
 * Tasks are refcounted, so overlapping work from unrelated features composes
 * without either one clearing the other's indication. The label shown is the
 * most recently begun task.
 *
 * Loaded early and NOT deferred, because it wraps window.fetch and must be in
 * place before any other script issues a request.
 */
(function () {
    "use strict";

    const LIVE_LABEL = "Live";
    // Don't flash orange for work that finishes almost immediately -- a warm
    // channel toggle is ~120 ms and a blink there reads as a glitch.
    const SHOW_DELAY_MS = 150;
    // ...and once shown, hold it long enough to actually be read.
    const MIN_BUSY_MS = 400;
    // One dropped poll is a hiccup; two in a row is a dead server. At 5 s that
    // bounds the "server died while the page sat idle" detection at ~10 s --
    // measured 16 s at a 10 s interval, which is a long time to keep claiming
    // everything is fine. The probe does no work server-side, so the extra
    // traffic is 12 empty 204s a minute.
    const HEALTH_INTERVAL_MS = 5000;
    const HEALTH_FAILURES_BEFORE_ERROR = 2;

    const tasks = new Map();
    let nextId = 1;
    let errorLabel = null;
    let healthFailures = 0;
    let unloading = false;

    let rootEl = null;
    let labelEl = null;
    let rendered = null;
    let showTimer = null;
    let hideTimer = null;
    let busySince = 0;

    function desiredState() {
        if (errorLabel) return { state: "error", label: errorLabel };
        if (tasks.size) {
            let label = LIVE_LABEL;
            for (const value of tasks.values()) label = value;  // most recent wins
            return { state: "busy", label };
        }
        return { state: "live", label: LIVE_LABEL };
    }

    function paint(next) {
        rendered = next;
        if (!rootEl) return;
        rootEl.dataset.state = next.state;
        rootEl.setAttribute("title", next.state === "error"
            ? `Plexora: ${next.label}`
            : next.state === "busy" ? `Plexora is working: ${next.label}` : "Plexora is up to date");
        if (labelEl.textContent !== next.label) labelEl.textContent = next.label;
    }

    function sync() {
        const next = desiredState();

        if (next.state === "error") {
            if (showTimer) { clearTimeout(showTimer); showTimer = null; }
            if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
            paint(next);
            return;
        }

        if (next.state === "busy") {
            if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
            if (rendered && rendered.state === "busy") { paint(next); return; }
            if (showTimer) return;
            showTimer = setTimeout(() => {
                showTimer = null;
                const now = desiredState();
                if (now.state === "busy") { busySince = Date.now(); paint(now); }
            }, SHOW_DELAY_MS);
            return;
        }

        // Going back to live.
        if (showTimer) { clearTimeout(showTimer); showTimer = null; }
        if (rendered && rendered.state === "busy") {
            const held = Date.now() - busySince;
            if (held < MIN_BUSY_MS) {
                if (!hideTimer) {
                    hideTimer = setTimeout(() => { hideTimer = null; sync(); }, MIN_BUSY_MS - held);
                }
                return;
            }
        }
        paint(next);
    }

    function begin(label) {
        const id = nextId++;
        tasks.set(id, String(label || "Working"));
        sync();
        let settled = false;
        return {
            done() {
                if (settled) return;
                settled = true;
                tasks.delete(id);
                sync();
            },
            fail(reason) {
                if (settled) return;
                settled = true;
                tasks.delete(id);
                setError(reason || "Error");
            },
            relabel(next) {
                if (settled) return;
                tasks.set(id, String(next));
                sync();
            },
        };
    }

    function track(label, promise) {
        const task = begin(label);
        return Promise.resolve(promise).then(
            (value) => { task.done(); return value; },
            (err) => { task.done(); throw err; },
        );
    }

    function setError(label) {
        errorLabel = String(label || "Error");
        sync();
    }

    function clearError() {
        if (!errorLabel) return;
        errorLabel = null;
        sync();
    }

    // --- transport: every fetch in the app reports its own health -----------
    // Wrapping once here covers all ~40 call sites (26 in dataLayer.js alone,
    // each with its own try/catch that swallows the failure) without touching
    // any of them. Deliberately does NOT mark requests busy: most are fast, and
    // a generic label would be less useful than the specific ones features
    // supply via begin(). Errors are the part no feature reports today.
    const nativeFetch = window.fetch ? window.fetch.bind(window) : null;
    if (nativeFetch) {
        window.fetch = function plexoraFetch(...args) {
            return nativeFetch(...args).then((response) => {
                if (response.status >= 500) {
                    setError(`Server error ${response.status}`);
                } else {
                    healthFailures = 0;
                    clearError();
                }
                return response;
            }, (err) => {
                // A request cancelled by navigation isn't a server problem.
                if (!unloading && !(err && err.name === "AbortError")) {
                    setError("Disconnected");
                }
                throw err;
            });
        };
    }

    window.addEventListener("beforeunload", () => { unloading = true; });

    // --- transport: liveness while idle -------------------------------------
    // An idle page issues no requests, so without this a server that died five
    // minutes ago still shows green. Uses nativeFetch so a failed poll doesn't
    // recurse through the wrapper's own error handling.
    function pollHealth() {
        if (!nativeFetch || document.visibilityState !== "visible") return;
        nativeFetch(plexoraUrl("health"), { cache: "no-store" }).then((r) => {
            if (!r.ok) throw new Error(String(r.status));
            healthFailures = 0;
            clearError();
        }).catch(() => {
            healthFailures += 1;
            if (healthFailures >= HEALTH_FAILURES_BEFORE_ERROR) setError("Disconnected");
        });
    }

    // --- viewer: tiles still streaming in -----------------------------------
    // "A channel was switched on but hasn't finished loading."
    //
    // Tracked per TiledImage rather than through the Viewer's aggregate
    // fully-loaded-change: that aggregate only recomputes when some TiledImage
    // raises its own fully-loaded-change, and a freshly added image doesn't
    // raise anything on the way in (it starts un-loaded, which is not a change
    // for it). So the viewer's cached _fullyLoaded stays true through the whole
    // load and then matches again once the image finishes -- meaning it fires
    // no event at all for a newly added channel. Verified: zero events across a
    // channel toggle.
    //
    // Tiles go through OSD's own XHR rather than fetch, so tile-load-failed is
    // the only way this path can surface an error.
    function watchViewer(viewer) {
        if (!viewer || !viewer.addHandler || viewer.__plexoraStatusWatched) return;
        viewer.__plexoraStatusWatched = true;

        const pending = new Set();
        let loading = null;
        function refresh() {
            if (pending.size && !loading) {
                loading = begin("Loading");
            } else if (!pending.size && loading) {
                loading.done();
                loading = null;
            }
        }
        function trackItem(item) {
            if (!item || !item.addHandler || item.__plexoraStatusTracked) return;
            item.__plexoraStatusTracked = true;
            const update = () => {
                if (item.getFullyLoaded && item.getFullyLoaded()) pending.delete(item);
                else pending.add(item);
                refresh();
            };
            item.addHandler("fully-loaded-change", update);
            update();
        }
        function forget(item) {
            pending.delete(item);
            refresh();
        }

        if (viewer.world) {
            viewer.world.addHandler("add-item", (event) => trackItem(event.item));
            viewer.world.addHandler("remove-item", (event) => forget(event.item));
            for (let i = 0; i < viewer.world.getItemCount(); i++) {
                trackItem(viewer.world.getItemAt(i));
            }
        }
        viewer.addHandler("tile-load-failed", () => setError("Tile failed"));
        viewer.addHandler("close", () => { pending.clear(); refresh(); });
    }

    // --- mount ---------------------------------------------------------------
    function mount() {
        rootEl = document.getElementById("app_status");
        if (!rootEl) return;
        rootEl.classList.add("app-status");
        rootEl.innerHTML =
            '<span class="app-status-glyph" aria-hidden="true"></span>' +
            '<span class="app-status-label"></span>';
        labelEl = rootEl.querySelector(".app-status-label");
        rootEl.setAttribute("role", "status");
        rootEl.setAttribute("aria-live", "polite");
        rendered = null;
        paint(desiredState());
        window.setInterval(pollHealth, HEALTH_INTERVAL_MS);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", mount);
    } else {
        mount();
    }

    window.PlexoraStatus = { begin, track, setError, clearError, watchViewer };
})();
