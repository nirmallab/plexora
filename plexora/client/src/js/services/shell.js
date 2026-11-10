/**
 * The website's loader: what viewer.plexoraapp.com serves for every path.
 *
 * Plexora's pages are rendered by Plexora's own Python, which on the website
 * runs in a worker beside the page (services/pythonClient.js), and it takes
 * seconds to start. So the site runs in two layers, both this script:
 *
 * - **The host** -- the top-level page, loaded once. It sets the page up for
 *   the browser backend (the route table, the worker's client,
 *   services/backend.js, services/pickedFiles.js), registers the site's
 *   service worker (workers/backendRelay.js, which hands every request a page
 *   makes for a route to services/backend.js), starts Python behind a boot
 *   screen, mounts the files picked on earlier visits, and then shows the app
 *   in a frame filling the window.
 * - **The frame** -- every page of the app. Loading the address in the frame
 *   serves this loader again, which borrows the host's Python (and its
 *   picks) instead of starting its own, asks it for the page this address
 *   names -- the same HTML the desktop's server would have sent -- and writes
 *   it into the frame's document.
 *
 * A link, a form, `location.href = ...`, the app's own router: every way a
 * page moves to another is an ordinary navigation of the frame, with a fresh
 * document and nothing of the last page left running, and none of them starts
 * Python again. The host keeps the address bar and the tab's title in step
 * with the frame; the browser's Back and Forward move the frame, whose
 * entries are the tab's history.
 *
 * The host also holds the licence (services/licenseClient.js, plan D8): it
 * hands Python its files before the first page is asked for, answers the
 * licence routes for every page of the frame, and tells the frame's page when
 * the licence changed elsewhere (another tab, the background refresh).
 *
 * Reads `/client/shell.json` (`client/static_build.py`): `{version, py,
 * licence?, telemetry?}`, where `py` is the base of the Python bundle (the
 * assets bucket on the site), `licence` the platform's address when it is not
 * api.biocognia.com (a test build's), and `telemetry.endpoint` the usage-count
 * service the site's builds name (none: nothing is sent).
 *
 * **Under a path.** viewer.plexoraapp.com serves the site at its root. A data
 * node serving the app (`plexora node serve --app`) serves it at `/app/`,
 * and behind an Open OnDemand portal that is `/rnode/<host>/<port>/app/` on
 * the portal's address. The loader's `<base href>` names the site's root
 * either way (server/node/webapp.py writes it per address), and `BASE` is its
 * path: every file the site loads is under it, and every address of the app
 * is `BASE` + the route Python answers (`inApp`). Python renders its pages
 * with `base_url` = `BASE`, so their links stay under it too.
 */
(function (global) {
    "use strict";

    const doc = global.document;
    const host = global.parent !== global && global.parent.PlexoraShellHost
        ? global.parent : null;

    //: The site's base path: "" at a site's root, `/rnode/<host>/<port>/app`
    //: behind a portal (the loader's `<base href>`; see the header). Read
    //: before the frame's document is replaced by the app's page.
    const BASE = (() => {
        try {
            return new URL(doc.baseURI).pathname.replace(/\/+$/, "");
        } catch (error) {
            return "";
        }
    })();
    global.PLEXORA_SITE_BASE = BASE;
    //: A file of the site, by its path from the site's root.
    const at = (path) => BASE + path;
    //: The site's version, from this script's own address (the loader stamps
    //: its files: client/static_build.py `LOADER`). The site's files are kept
    //: by the browser and checked in the background (static_build.py `KEPT`),
    //: so every file this loader fetches carries it: a release is a new
    //: address for each, never last release's copy.
    const STAMP = (() => {
        try {
            return new URL(doc.currentScript.src).searchParams.get("v") || "";
        } catch (error) {
            return "";
        }
    })();
    const stamped = (src) => (STAMP && !src.includes("?") ? `${src}?v=${encodeURIComponent(STAMP)}` : src);
    //: The route an address of the app names, as Python answers it.
    const inApp = (where) => (where.pathname.slice(BASE.length) || "/") + where.search + where.hash;

    // -- The loader's screen ---------------------------------------------------
    //
    // One orb and one line under it (shell.css). The line says how far
    // Plexora has got, in four steps that always come in this order. A step
    // is said only once its part of the start has begun, and stays at least
    // HOLD_MS, so the line can trail the start but never run ahead of it:
    // "Almost ready" waits for Python to have started with the licence, the
    // projects and the picked files in (hostBoot `started`).
    //
    //   0  Opening Plexora           the loader's own words, from its first paint
    //   1  Preparing your workspace  Python's runtime up (pythonWorker.js mark
    //                                `core`): its packages, Plexora's files
    //   2  Loading the viewer        Plexora's files unpacked (mark `wheels`):
    //                                Plexora imported, the projects mounted
    //   3  Almost ready              Python started: the page asked for, written
    //
    // A page of the app asked for later is the frame's loader (frameBoot),
    // which shows nothing for its first NAV_GRACE_MS, and then starts where
    // the start is or -- Python up -- at "Loading the viewer" for a project's
    // viewer and "Almost ready" for any other page.
    //
    // The frame made while the host's loader is up is veiled (shell.css
    // `is-veiled`): the host's loader shows through it until its page is
    // written, and the page then fades in over it. When the frame's own
    // loader has to be seen first (a question, a last word), the frame is
    // unveiled over the host's, whose orb, glow and line it shows on the same
    // clock: one loader all the way.
    const STEPS = Object.freeze(["Opening Plexora", "Preparing your workspace",
                                 "Loading the viewer", "Almost ready"]);
    const VIEWER_STEP = 2;
    const ALMOST = STEPS.length - 1;
    //: Python's marks (workers/pythonWorker.js) and the step each begins.
    const MARK_STEPS = Object.freeze({ core: 1, wheels: 2 });
    //: How long a step stays once in, at least; how long the line takes to
    //: go out and to come in (consistent for every change).
    const HOLD_MS = 1400;
    const OUT_MS = 220;
    const IN_MS = 420;
    //: From the loader's first paint to its line being read (shell.css: the
    //: line comes in 160 ms after the orb, over 700 ms).
    const READABLE_MS = 620;
    //: A page's loader shows nothing this long (shell.css
    //: `html.plexora-boot-nav`).
    const NAV_GRACE_MS = 240;
    //: The frame's fade over the host's loader (shell.css `.plexora-app-frame`).
    const VEIL_MS = 320;
    //: The glow's breath (shell.css `plexora-boot-glow`).
    const GLOW_MS = 4800;
    //: The agent's orb (views/agentPanel.js), as the import dialog shows it
    //: while it reads (views/importSample.js `readingOrb`): the 64 preset at
    //: 80 px (shell.css --plexora-boot-orb), in a lighter silver than the
    //: agent panel's (agentPanel.css --plx-agent-ink), being the one thing on
    //: the screen (shell.css --plexora-boot-ink).
    const ORB = Object.freeze({ state: "searching", size: 64, display: 80, tint: "#dbe3ee", dark: true });
    const ORB_ENGINE = "/client/external/thinking-orbs-0.3.2/orbs.js";

    //: One clock for the host and its frame (ms): the orb, the glow and a
    //: step's hold carry on from one document to the other.
    const wall = () => {
        const clock = global.performance;
        return clock && clock.timeOrigin ? clock.timeOrigin + clock.now() : Date.now();
    };

    const reducedMotion = () => {
        try {
            return Boolean(global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches);
        } catch (error) {
            return false;
        }
    };

    //: `fn` once the loader's markup is parsed (this script runs in its head).
    function whenParsed(fn) {
        if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", fn, { once: true });
        else fn();
    }

    //: The loader's screen in this document: its line and its orb. `step`
    //: and `reached`: where it starts; `until`: when that step's hold ends;
    //: `epoch`: the clock's zero (the host's); `paused`: the orb mounted but
    //: not drawing (a veiled frame's).
    function bootScreen({ step = 0, reached = step, until = wall() + READABLE_MS + HOLD_MS,
                          epoch = wall(), paused = false } = {}) {
        let shown = step;
        let ahead = Math.max(step, reached);
        //: Words in place of the step's (a question, a last word), or null.
        let said = null;
        //: Bumped whenever the line is written outright: a change in flight
        //: then leaves it alone.
        let turn = 0;
        let moving = false;
        let asleep = false;
        let gone = false;
        let timer = 0;
        let orb = null;
        let orbPaused = paused;

        const box = () => doc.getElementById("plexora-boot");
        const line = () => doc.getElementById("plexora-boot-status");
        const write = (text) => {
            const node = line();
            if (node) node.textContent = text;
        };
        const still = () => {
            const node = line();
            if (node && typeof node.getAnimations === "function") node.getAnimations().forEach((one) => one.cancel());
        };
        const fadeIn = (node) => {
            if (node && typeof node.animate === "function" && !reducedMotion()) {
                node.animate([{ opacity: 0, transform: "translateY(4px)" }, { opacity: 1, transform: "none" }],
                             { duration: IN_MS, easing: "cubic-bezier(0.2, 0, 0, 1)" });
            }
        };

        function plan() {
            global.clearTimeout(timer);
            if (gone || asleep || said !== null || moving || shown >= ahead) return;
            const wait = until - wall();
            if (wait > 0) {
                timer = global.setTimeout(plan, wait);
                return;
            }
            advance();
        }

        //: The next step in: the line out (lifting away), its words changed,
        //: and back in (rising into place).
        async function advance() {
            const next = shown + 1;
            const mine = ++turn;
            const node = line();
            moving = true;
            if (node && typeof node.animate === "function" && !reducedMotion()) {
                const out = node.animate([{ opacity: 1, transform: "none" }, { opacity: 0, transform: "translateY(-4px)" }],
                                         { duration: OUT_MS, easing: "cubic-bezier(0.4, 0, 1, 1)", fill: "forwards" });
                await out.finished.catch(() => null);
                if (mine !== turn || gone) return;
                if (asleep) {
                    // Gone out of sight meanwhile: the step stays where it was.
                    out.cancel();
                    moving = false;
                    return;
                }
                node.textContent = STEPS[next];
                out.cancel();
                fadeIn(node);
            } else {
                write(STEPS[next]);
            }
            shown = next;
            moving = false;
            until = wall() + IN_MS + HOLD_MS;
            plan();
        }

        function setOrb(pausedNow) {
            orbPaused = pausedNow;
            if (!orb) return;
            if (pausedNow) orb.pause();
            else orb.resume();
        }

        return {
            //: The start has begun step `step`.
            reach(step) {
                if (step > ahead) ahead = Math.min(step, ALMOST);
                plan();
            },
            //: Words in place of the step's, at once: a question, a last word.
            say(text) {
                said = String(text);
                turn += 1;
                moving = false;
                global.clearTimeout(timer);
                still();
                write(said);
            },
            //: The step's words back after a question, and on from there.
            resume() {
                if (said === null || gone) return;
                said = null;
                turn += 1;
                still();
                write(STEPS[shown]);
                fadeIn(line());
                until = wall() + IN_MS + HOLD_MS;
                plan();
            },
            //: Where it is, for the frame's loader to take over from.
            snapshot: () => ({ shown, reached: ahead, until }),
            //: Take over from the host's screen, which was the one in sight.
            adopt(from) {
                if (!from || gone) return;
                shown = from.shown;
                ahead = Math.max(ahead, from.reached, shown);
                until = from.until;
                turn += 1;
                moving = false;
                still();
                if (said === null) write(STEPS[shown]);
                plan();
            },
            //: The orb mounted in the loader's canvas; the line and the glow
            //: set to where this screen is.
            attach() {
                if (gone) return;
                write(said === null ? STEPS[shown] : said);
                const frameBox = box();
                if (frameBox) frameBox.style.setProperty("--plexora-boot-glow-at", `${-Math.round((wall() - epoch) % GLOW_MS)}ms`);
                const canvas = doc.getElementById("plexora-boot-orb");
                if (!canvas || orb) return;
                const driver = global.PlexoraOrb;
                if (!driver || typeof driver.mount !== "function") {
                    canvas.setAttribute("data-orb", "static");
                    return;
                }
                try {
                    driver.configure({ load: () => import(at(ORB_ENGINE)) });
                    orb = driver.mount(canvas, { ...ORB, clock: () => wall() - epoch, paused: orbPaused || asleep });
                } catch (error) {
                    canvas.setAttribute("data-orb", "static");
                }
            },
            //: The orb drawing, or not (a veiled frame's; the host's under
            //: an unveiled frame).
            play: () => setOrb(false),
            pause: () => setOrb(true),
            //: A last word (`kind` the box's class): the line, and the orb
            //: standing still behind it.
            end(text, kind) {
                this.say(text);
                const frameBox = box();
                if (frameBox) {
                    frameBox.hidden = false;
                    frameBox.classList.add(kind);
                }
                setOrb(true);
            },
            //: Out of sight (the app's page over it), nothing drawn; and back.
            hide() {
                asleep = true;
                global.clearTimeout(timer);
                const frameBox = box();
                if (frameBox) frameBox.hidden = true;
                if (orb) orb.pause();
            },
            show() {
                asleep = false;
                const frameBox = box();
                if (frameBox) frameBox.hidden = false;
                setOrb(orbPaused);
                plan();
            },
            //: This document is about to be replaced by the app's page
            //: (writePage): no timer, animation or frame of the orb outlives it.
            dispose() {
                gone = true;
                global.clearTimeout(timer);
                still();
                if (orb) orb.destroy();
                orb = null;
            },
        };
    }

    //: The app's pages that are not a project's viewer: page_routes.py
    //: `image_viewer` answers every other single path segment.
    const NOT_VIEWER = new Set(["open_project", "edit_config", "settings", "figures", "captures",
                                "connect", "selfcheck", "google-auth.html", "index.html"]);
    function isViewer(route) {
        const parts = String(route).split(/[?#]/)[0].split("/").filter(Boolean);
        return parts.length === 1 && !NOT_VIEWER.has(parts[0]);
    }

    //: The host's loader, as its frame's loader sees it (frameBoot): the
    //: clock, the screen's place, the start's steps as they come, the veil.
    function shareLoader(screen, epoch) {
        const followers = new Set();
        let hiding = 0;
        const frameOf = () => doc.getElementById("plexora-app");
        const veiled = () => {
            const frame = frameOf();
            return Boolean(frame && frame.classList.contains("is-veiled"));
        };
        const later = (fn) => {
            global.clearTimeout(hiding);
            hiding = global.setTimeout(fn, VEIL_MS + 40);
        };
        const api = Object.freeze({
            epoch,
            snapshot: () => screen.snapshot(),
            //: `fn(step)` for every step the start begins; returns its undo.
            follow(fn) {
                followers.add(fn);
                return () => followers.delete(fn);
            },
            veiled,
            //: The frame's own loader has to be seen (a question, a last
            //: word): unveiled over this one, whose orb then stops.
            reveal() {
                if (!veiled()) return;
                frameOf().classList.remove("is-veiled");
                later(() => screen.pause());
            },
            //: The frame's page is written: faded in over this loader, which
            //: then goes.
            shown() {
                const frame = frameOf();
                if (frame) frame.classList.remove("is-veiled");
                later(() => screen.hide());
            },
        });
        return {
            api,
            reach(step) {
                screen.reach(step);
                for (const fn of [...followers]) {
                    try {
                        fn(step);
                    } catch (error) {
                        followers.delete(fn);
                    }
                }
            },
            //: The loader back over a frame that went (Python did not start).
            restore() {
                global.clearTimeout(hiding);
                screen.show();
                screen.play();
            },
        };
    }

    //: This document's screen: the host's; in the app's frame, one taking
    //: over from the host's (`continued`: the frame veiled over it) or a
    //: page's own (`nav`).
    const shared = host ? host.PlexoraShellLoader || null : null;
    const continued = Boolean(shared && shared.veiled());
    const epoch = shared ? shared.epoch : wall();
    //: The step a page asked for later starts at (see the header above).
    const settle = (hostStep, route) => (hostStep < ALMOST ? hostStep : isViewer(route) ? VIEWER_STEP : ALMOST);
    const screen = (() => {
        if (!host) return bootScreen({ epoch });
        doc.documentElement.classList.add(continued ? "plexora-boot-continued" : "plexora-boot-nav");
        const from = shared ? shared.snapshot() : { shown: ALMOST, reached: ALMOST, until: 0 };
        if (continued) return bootScreen({ step: from.shown, reached: from.reached, until: from.until, epoch, paused: true });
        return bootScreen({ step: settle(from.reached, inApp(global.location)),
                            until: wall() + NAV_GRACE_MS + READABLE_MS + HOLD_MS, epoch });
    })();
    whenParsed(() => screen.attach());
    //: The frame's subscription to the host's steps (frameBoot), undone with
    //: the screen.
    let unfollow = null;

    //: The loader's line replaced by the app's page (writePage), for good.
    function loaderGone() {
        if (unfollow) unfollow();
        unfollow = null;
        screen.dispose();
    }

    //: A veiled frame's loader brought into sight over the host's: the
    //: host's step and hold taken over, the orb drawing on the same clock.
    function unveil() {
        if (!shared || !shared.veiled()) return;
        screen.adopt(shared.snapshot());
        screen.play();
        shared.reveal();
    }

    const status = (text) => screen.say(text);
    const fail = (text) => {
        unveil();
        screen.end(text, "plexora-boot-failed");
    };

    //: A row of buttons under the line (a question's, a last word's).
    function actionRow() {
        const row = doc.createElement("div");
        row.className = "plexora-boot-actions";
        const below = doc.querySelector("#plexora-boot .plexora-boot-below") || doc.getElementById("plexora-boot");
        if (below) below.appendChild(row);
        return row;
    }

    //: A question in the loader, before the app starts: `text`, then a button
    //: per action; resolves with the chosen action's value. (A click here is
    //: what lets the page ask the browser for a permission.) The start's
    //: steps wait for it: the line goes back to them once it is answered.
    let asking = 0;
    function askInLoader(text, actions) {
        asking += 1;
        unveil();
        screen.say(text);
        const row = actionRow();
        return new Promise((resolve) => {
            for (const action of actions) {
                const button = doc.createElement("button");
                button.type = "button";
                button.textContent = action.label;
                if (action.primary) button.className = "is-primary";
                button.addEventListener("click", () => {
                    row.remove();
                    asking -= 1;
                    if (!asking) screen.resume();
                    resolve(action.value);
                });
                row.appendChild(button);
            }
        });
    }

    //: The app's page in the frame, once it can show a dialog and a notice:
    //: the frame loads the loader first and writes the app's page into it
    //: after. Null when it never does (30 s).
    async function whenApp(frame) {
        let app = null;
        for (let i = 0; i < 150 && !(app && app.PlexoraConfirm && app.PlexoraToast
                                       && app.PlexoraShellFramed); i += 1) {
            await new Promise((resolve) => setTimeout(resolve, 200));
            app = frame.contentWindow;
        }
        return app && app.PlexoraConfirm ? app : null;
    }

    function script(src) {
        return new Promise((resolve, reject) => {
            const node = doc.createElement("script");
            node.src = stamped(src);
            node.onload = resolve;
            node.onerror = () => reject(new Error(`could not load ${src}`));
            doc.head.appendChild(node);
        });
    }

    //: Files fetched together and run one after another, in the order given
    //: (a script inserted with `async` off keeps its place): the loader's
    //: services, which used to wait on each other's download one by one.
    function scripts(sources) {
        return Promise.all(sources.map((src) => new Promise((resolve, reject) => {
            const node = doc.createElement("script");
            node.src = stamped(src);
            node.async = false;
            node.onload = resolve;
            node.onerror = () => reject(new Error(`could not load ${src}`));
            doc.head.appendChild(node);
        })));
    }

    //: The pages this browser shows before Python has started: the last
    //: answer Python gave for each, kept in Cache Storage per site version,
    //: Python bundle and base path. Only the home page: what it does is open
    //: something, which waits for Python anyway, while a page that saves what
    //: it shows (Settings, a project) shown from an old copy could save old
    //: values back. A page shown from here is checked against Python's own
    //: answer once Python is up (frameBoot `early`).
    const PAGE_CACHE = "plexora-pages";
    const EARLY_ROUTES = new Set(["/"]);

    function pageStore(shell, py) {
        const caches = global.caches;
        const stamp = encodeURIComponent(`${shell.version || ""}|${py}`);
        const key = (route) => new URL(at(`/__plexora/page${route}?v=${stamp}`), global.location.origin).href;
        const open = () => (caches ? caches.open(PAGE_CACHE) : Promise.reject(new Error("no Cache Storage")));
        return Object.freeze({
            //: The HTML kept for `route`, or null.
            async get(route) {
                if (!EARLY_ROUTES.has(route)) return null;
                try {
                    const found = await (await open()).match(key(route));
                    return found ? await found.text() : null;
                } catch (error) {
                    return null;
                }
            },
            //: Python's answer for `route` kept, and every copy from another
            //: version dropped.
            async put(route, html) {
                if (!EARLY_ROUTES.has(route) || typeof html !== "string" || !html) return;
                try {
                    const cache = await open();
                    const mine = key(route);
                    for (const request of await cache.keys()) {
                        const url = new URL(request.url);
                        if (url.pathname === new URL(mine).pathname && request.url !== mine) await cache.delete(request);
                    }
                    await cache.put(mine, new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } }));
                } catch (error) { /* nothing kept: the next visit waits for Python, as before */ }
            },
        });
    }

    //: The home page as Python draws it on a first visit, rendered when the
    //: site was built (client/static_build.py `home_page`; shell.json `home`):
    //: what a browser that has kept nothing yet shows while Python starts.
    //: At the site's root only: a node serves the app under a path of its own.
    async function builtHome(shell, route) {
        if (!shell.home || BASE || route !== "/") return null;
        try {
            const response = await global.fetch(at(`/${shell.home}`), { cache: "no-cache" });
            return response.ok ? await response.text() : null;
        } catch (error) {
            return null;
        }
    }

    //: Two answers for a page the same but for what time has changed: the
    //: words of each `<time>` ("3 hours ago" a day later).
    const timeless = (html) => String(html).replace(/(<time\b[^>]*>)[^<]*(<\/time>)/g, "$1$2");

    function browserVariables() {
        global.flaskVariables = { backend: "browser", base_url: BASE };
        global.PLEXORA_BASE_URL = BASE;
    }

    const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

    //: The site's service worker registered, and this page controlled by it.
    //: False where the browser has none (a private window may not): the app's
    //: pages then answer their own requests (`relayed`, services/pageRelay.js).
    async function controlled() {
        if (!global.navigator.serviceWorker) return false;
        try {
            await global.navigator.serviceWorker.register(at("/client/src/js/workers/backendRelay.js"),
                                                          { scope: at("/") });
        } catch (error) {
            return false;
        }
        return claimed();
    }

    //: This page controlled by the site's worker. A navigation the browser did
    //: not hand to an active worker (a forced reload; Playwright's Firefox
    //: after the first) is claimed by asking it.
    async function claimed() {
        const workers = global.navigator.serviceWorker;
        if (!workers) return false;
        if (workers.controller) return true;
        const changed = new Promise((resolve) => workers.addEventListener("controllerchange", resolve, { once: true }));
        const registration = await Promise.race([workers.ready, wait(5000).then(() => null)]);
        if (!registration) return false;
        if (!workers.controller && registration.active) registration.active.postMessage("plexora-claim");
        return Promise.race([changed.then(() => true), wait(5000).then(() => Boolean(workers.controller))]);
    }

    //: Whether the site's worker sees this page's requests: it answers this one
    //: itself. Being its `controller` is not enough (Playwright's Firefox never
    //: hands it a frame's requests).
    async function relayed() {
        try {
            const response = await global.fetch(at("/__plexora/relayed"), { cache: "no-store" });
            return response.headers.get("X-Plexora-Relayed") === "1";
        } catch (error) {
            return false;
        }
    }

    //: The licence this browser holds, restored into Python's files; null
    //: when it cannot start (the site then runs as Free, as a desktop does
    //: with a damaged licence file).
    async function startLicence(shell, loading) {
        try {
            const module = await loading;
            return await module.startInPage({ config: shell.licence || {}, version: shell.version,
                                              python: global.PlexoraPython });
        } catch (error) {
            console.warn("[licence] not started:", error);
            return null;
        }
    }

    //: A change made elsewhere (another tab, the background refresh) shown in
    //: the frame's page: its licence hint, and Settings if it is open. The
    //: page that asked for a change draws the answer itself.
    function followLicence(frame) {
        const licence = global.PlexoraLicense;
        if (!licence) return;
        const key = (status) => JSON.stringify(status ? [status.state, status.entitlements] : null);
        let shown = key(licence.status());
        licence.on(({ kind, status, from }) => {
            if (kind !== "changed" || !status) return;
            const now = key(status);
            if (from === "local" || now === shown) {
                shown = now;
                return;
            }
            shown = now;
            const app = frame.contentWindow;
            try {
                app.PlexoraPro?.adopt?.(status);
                app.dispatchEvent(new app.CustomEvent("plexora:license-external", { detail: status }));
            } catch (error) { /* the frame is between pages */ }
        });
    }

    //: The address the frame shows, as the address bar should say it.
    const here = (where) => where.pathname + where.search + where.hash;

    //: A node's pairing link (`<site>/connect#v=1&e=…&t=…`, printed by
    //: `plexora node serve --pair`): its fragment, taken and cleared from the
    //: address bar and the history before anything else runs, so the token
    //: is never in either; the app opens at the site's start page. Null for
    //: any other address.
    function takePairingLink() {
        const where = global.location;
        if (where.pathname.replace(/\/+$/, "") !== at("/connect")) return null;
        const fragment = where.hash || "";
        global.history.replaceState(null, "", at("/"));
        return fragment;
    }

    //: Telemetry for the worker (plexora/browser/telemetry.py): the desktop's
    //: anonymous usage counts, when the build names the service
    //: (shell.json `telemetry`), with what a desktop's environment would say:
    //: Do Not Track (or Global Privacy Control) turns it off, as DO_NOT_TRACK
    //: does there, and the machine's coarse facts, which the worker cannot
    //: ask for. Null (off) for a build that names no service.
    function telemetryOptions(shell) {
        const endpoint = shell && shell.telemetry && shell.telemetry.endpoint;
        if (!endpoint) return null;
        const nav = global.navigator || {};
        const dnt = nav.doNotTrack === "1" || global.doNotTrack === "1" || nav.globalPrivacyControl === true;
        const platform = String((nav.userAgentData && nav.userAgentData.platform) || nav.platform || nav.userAgent || "");
        const os = /Win/i.test(platform) ? "windows" : /Mac/i.test(platform) ? "mac"
            : /Linux|X11|CrOS/i.test(platform) ? "linux" : "other";
        const context = { os };
        if (nav.hardwareConcurrency) context.cpus = nav.hardwareConcurrency;
        if (nav.deviceMemory) context.memory_gb = nav.deviceMemory;
        return { endpoint: String(endpoint), dnt, context };
    }

    //: A browser turned away (too old for the website's Python), counted
    //: once the message is up: anonymously, at most once a day, under
    //: telemetry's rules (services/refusalCount.js). Only a build that names
    //: the service, in a browser not asking for Do Not Track or Global
    //: Privacy Control, loads the script; nothing here can delay or break
    //: the message.
    function countRefusal(shell, old) {
        const options = telemetryOptions(shell);
        if (!options || options.dnt) return;
        script(at("/client/src/js/services/refusalCount.js"))
            .then(() => global.PlexoraRefusalCount.send({ options, version: shell.version, refused: old }))
            .catch(() => null);
    }

    //: The desktop app's download page (services/desktopHandoff.js's too).
    const DOWNLOAD = "https://github.com/nirmallab/plexora/releases/latest";
    const UPDATE_CHROME = "https://support.google.com/chrome/answer/95414";
    const UPDATE_EDGE = "https://support.microsoft.com/microsoft-edge/settings-update";

    //: The site's switches (plan Phase 5): `flags.json` on the assets bucket,
    //: cached five minutes (`tools/browser_build/publish.py flags`). `viewer`
    //: "desktop-only" pauses the website (the kill switch) and "preview" marks
    //: it; `min_site_version` stops a site older than it; `models.<name>.enabled`
    //: false switches a model off. Unreachable, slow or malformed: no flags --
    //: a missing file never locks anyone out.
    async function readFlags(shell) {
        if (!shell.flags) return {};
        try {
            const where = new URL(shell.flags, new URL(at("/"), global.location.origin)).href;
            const response = await Promise.race([global.fetch(where, { mode: "cors" }),
                                                 wait(3000).then(() => null)]);
            if (!response || !response.ok) return {};
            const flags = await response.json();
            return flags && typeof flags === "object" && !Array.isArray(flags) ? flags : {};
        } catch (error) {
            return {};
        }
    }

    //: Whether dotted version `version` is older than `minimum`, part by part
    //: (a part that is not a number counts as 0).
    function older(version, minimum) {
        const parts = (text) => String(text || "").split(/[.+-]/).map((part) => parseInt(part, 10) || 0);
        const [a, b] = [parts(version), parts(minimum)];
        for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
            if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) < (b[i] || 0);
        }
        return false;
    }

    //: The loader's last word when the site does not start: `text`, and its
    //: buttons on one row (`more`: [{label, act}] after the primary one).
    //: Python never loads, so the orb stands still.
    function stop(text, label, act, more = []) {
        screen.end(text, "plexora-boot-stopped");
        const row = actionRow();
        [{ label, act, primary: true }, ...more].forEach((action) => {
            const button = doc.createElement("button");
            button.type = "button";
            if (action.primary) button.className = "is-primary";
            button.textContent = action.label;
            button.addEventListener("click", action.act);
            row.appendChild(button);
        });
    }

    //: The site's description (`client/shell.json`) as the loader carries it
    //: (client/static_build.py writes it into index.html): no round trip
    //: before anything else can start. Null where it does not (a build from
    //: before), and the file is fetched.
    function described() {
        try {
            const block = doc.getElementById("plexora-shell");
            return block && block.textContent.trim() ? JSON.parse(block.textContent) : null;
        } catch (error) {
            return null;
        }
    }

    async function hostBoot() {
        global.PlexoraShellHost = true;
        // The loader as the app's frame sees it, before there is a frame.
        const loader = shareLoader(screen, epoch);
        global.PlexoraShellLoader = loader.api;
        const pairing = takePairingLink();
        browserVariables();
        // Everything the host runs, fetched at once and run in the order
        // given (`scripts`): the two files that can start Python, then the
        // services. And the site's worker registered meanwhile (the frame
        // waits for it in frameBoot's `claimed`). These used to wait for the
        // site's description, then for its switches, then for each other.
        const core = scripts([at("/client/src/js/services/backendRoutes.js"),
                              at("/client/src/js/services/pythonClient.js")]);
        const services = scripts([
            at("/client/src/js/services/backend.js"),
            at("/client/src/js/services/pickedFiles.js"),
            // The explorer's listings of the folders the user gives the site.
            at("/client/src/js/services/localFiles.js"),
            // Saving into a picked file Python wrote (Settings > Data, off by default).
            at("/client/src/js/services/sourceWrites.js"),
            at("/client/src/js/services/browserNode.js"),
            // Helper Pythons for the contrast fits, off the main Python's thread.
            at("/client/src/js/services/fitHelpers.js"),
            // Python's answers to a project's first screen, kept for a reload.
            at("/client/src/js/services/keptAnswers.js"),
            at("/client/src/js/services/browserSlides.js"),
            at("/client/src/js/services/browserTable.js"),
            at("/client/src/js/services/derivedIndex.js"),
            at("/client/src/js/services/browserPyramid.js"),
            at("/client/src/js/services/browserStore.js"),
            at("/client/src/js/services/desktopHandoff.js"),
            // A project's `.plexora` file written and read here, so a page
            // change in the app's frame does not stop it.
            at("/client/src/js/services/bundleWeb.js"),
            at("/client/src/js/services/handoffNotice.js"),
            at("/client/src/js/services/connectorClient.js"),
            // Connections to servers, held here so they outlive the app
            // frame's pages (docs/internal/WEB_REMOTE_RELAY.md).
            at("/client/src/js/services/relayRoom.js"),
            at("/client/src/js/services/webRemote.js"),
            // Google sign-in for Google Cloud (plan L9): the token is held
            // here, in the host, for the tab's life, and each page's frame
            // borrows it.
            at("/client/src/js/services/googleAuth.js"),
            // Magic select's model: onnxruntime-web from the Python bundle,
            // the model files from the assets' models folder.
            at("/client/src/js/services/onnxExecutor.js"),
        ]);
        core.catch(() => null);
        services.catch(() => null);
        const control = controlled();
        const shell = described() || await (await global.fetch(at("/client/shell.json"), { cache: "no-cache" })).json();
        // The site's switches, read while Python warms: what they stop is
        // stopped before Python starts and before the app's frame is made
        // (`halted` below), so a paused site still never runs its Python.
        const flagsRead = readFlags(shell);
        await core;
        // The bundle's address, absolute or from the site's root.
        const py = new URL(shell.py, new URL(at("/"), global.location.origin)).href;
        // A Chrome or Edge too old to run the website's Python (no JSPI)
        // crashes or hangs trying: asked to update, before anything starts.
        const old = global.PlexoraPython.unsupported();
        if (old) {
            stop(`Update ${old.browser} to use Plexora here (version ${old.version}; it needs 137 or later).`,
                 `How to update ${old.browser}`,
                 () => global.open(old.browser === "Edge" ? UPDATE_EDGE : UPDATE_CHROME, "_blank", "noopener"),
                 [{ label: "Get the desktop app", act: () => global.open(DOWNLOAD, "_blank", "noopener") }]);
            return countRefusal(shell, old);
        }
        // Python's downloads first -- the longest part of a visit by far --
        // and everything below while they run.
        global.PlexoraPython.on("mark", ({ mark }) => { if (MARK_STEPS[mark]) loader.reach(MARK_STEPS[mark]); });
        global.PlexoraPython.warm({ base: py, root: at("/"), tab: true });
        //: Resolves null to go on, or a function that stops the site.
        const halted = flagsRead.then((flags) => {
            global.PlexoraFlags = Object.freeze(flags);
            if (flags.viewer === "desktop-only") {
                return () => stop(flags.message || "The website is paused for now. The desktop app works as usual.",
                                  "Get the desktop app", () => global.open(DOWNLOAD, "_blank", "noopener"));
            }
            if (flags.min_site_version && older(shell.version, flags.min_site_version)) {
                return () => stop("This copy of Plexora is out of date.", "Reload", () => global.location.reload());
            }
            if (flags.viewer === "preview") {
                const mark = doc.createElement("div");
                mark.id = "plexora-preview-mark";
                mark.textContent = "Preview";
                doc.body.appendChild(mark);
            }
            return null;
        });
        // The page this address shows, as Python last drew it -- or, on a
        // first visit, as the site's build drew it -- looked up now.
        const pages = pageStore(shell, py);
        global.PlexoraPages = pages;
        const route = inApp(global.location);
        // A project's viewer, kept with what its first screen asks Python
        // (services/keptAnswers.js), once the services are in: shown while
        // Python starts when the page can draw its image without it.
        const kept = route === "/" ? pages.get(route).then((html) => html || builtHome(shell, route))
            : services.then(() => {
                global.PlexoraKept.configure({ stamp: `${shell.version || ""}|${py}` });
                return global.PlexoraKept.early(route);
            }).catch(() => null);
        // An ES module, fetched while Python starts.
        const licenceModule = import(stamped(at("/client/src/js/services/licenseClient.js")));
        licenceModule.catch(() => null);
        await services;
        global.PlexoraGoogleAuth.configure(shell.google || {});
        global.PlexoraWebRemote.configure({
            relay: shell.relay,
            bundle: `${at("/client/dist/ssh_bundle.js")}?v=${encodeURIComponent(shell.version || "")}`,
            // When the website's remote servers are behind a licence, the
            // relay asks for the certificate the page holds.
            licence: () => (global.PlexoraLicense && typeof global.PlexoraLicense.certificate === "function"
                ? global.PlexoraLicense.certificate() : null),
        });
        const models = new URL(shell.models || "/models/", new URL(at("/"), global.location.origin)).href;
        global.PlexoraOnnx.configure({ ort: shell.ort || `${py}ort/`, models });
        flagsRead.then((flags) => {
            if (((flags.models || {}).mobilesam || {}).enabled === false) {
                global.PlexoraOnnx.configure({ disabled: "Magic select is switched off on the website for now." });
            }
        });
        // Registered by now on any visit but a first (the worker was already
        // there); on a first, the store's preparation below waits for nothing.
        control.catch(() => null);
        // Where the projects are kept: a workspace the user granted, opened
        // again where the browser still grants it, or this browser's storage.
        // Nothing is asked here: the first save runs the onboarding
        // (views/webOnboarding.js), and a folder the browser no longer grants
        // is reconnected from a click there or on the home page.
        const store = await global.PlexoraStore.prepare().catch(() => null);
        // The copies the page made on earlier visits, found where they are
        // kept while Python starts (services/derivedIndex.js); mounted once it has.
        global.PlexoraDerived.prepare().catch(() => null);
        const halt = await halted;
        if (halt) return halt();
        // `?pydebug` on the address: the worker's test hooks (`run`, `sync`,
        // `trace`), for diagnosing the site in development.
        const debug = new URLSearchParams(global.location.search).has("pydebug");
        const booting = global.PlexoraPython.start({ base: py, root: at("/"), siteBase: BASE, tab: true,
                                                     debug, store, telemetry: telemetryOptions(shell) });
        //: Python started, with everything a page's requests may need: the
        //: licence in its files, the store's state, the files picked on
        //: earlier visits mounted. A page shown before then (frameBoot
        //: `early`) holds its requests until it resolves (services/backend.js).
        const started = (async () => {
            const info = await booting;
            if (info.error) throw new Error(info.error);
            // What Help › About says on the website (views/helpMenu.js).
            global.PlexoraSite = Object.freeze({ version: info.version || shell.version || "" });
            // The licence, in Python's files before the first page is rendered.
            global.PlexoraLicense = await startLicence(shell, licenceModule);
            global.PlexoraStore.booted(info.store);
            // The files picked on earlier visits, mounted again where the browser
            // still grants access; the rest wait for a click (PlexoraPicks.reconnect).
            await global.PlexoraPicks.restore().catch(() => null);
            // And the pyramidized copies saved in folders beside their originals.
            await global.PlexoraPyramid.restore().catch(() => null);
            // Everything a page needs is in: only the page is left to draw.
            loader.reach(ALMOST);
            return info;
        })();
        started.catch(() => null);
        global.PlexoraShellStarted = started;
        // Kept answers are given until Python is up, then each one given is
        // asked of Python again.
        global.PlexoraKept.configure({ stamp: `${shell.version || ""}|${py}` });
        global.PlexoraKept.starting(started);
        // The app's frame now when this page was kept, with the kept page
        // handed to it; otherwise once Python has started, as it always was.
        const early = await kept;
        if (early) {
            global.PlexoraShellEarly = typeof early === "string" ? { route, html: early }
                : { route, html: early.html, opens: early.opens || "", page: early.page || null };
        } else {
            await started;
        }
        const frame = doc.createElement("iframe");
        frame.id = "plexora-app";
        frame.title = "Plexora";
        // Veiled: this loader shows through it until its page is written
        // (writePage asks for it, PlexoraShellLoader.shown).
        frame.className = "plexora-app-frame is-veiled";
        frame.src = here(global.location);
        frame.addEventListener("load", () => {
            started.then(() => keepHome(frame, pages)).catch(() => null);
        });
        doc.body.appendChild(frame);
        // The key pressed with the host focused (before the frame has been
        // clicked into) reloads the frame too.
        reloadsFrame(global, () => doc.getElementById("plexora-app"));
        await started.catch((error) => {
            // Python did not start under a kept page: the page goes, and the
            // loader says why, as it would have without one.
            frame.remove();
            loader.restore();
            throw error;
        });
        followLicence(frame);
        // A node's pairing link: paired once the app can say how it went.
        if (pairing !== null) {
            whenApp(frame).then((app) => global.PlexoraConnector.pair(
                global.PlexoraConnector.parse(pairing) || { invalid: true }, app)).catch(() => null);
        }
    }

    //: The home page kept as Python would draw it now, a few seconds after the
    //: frame shows any other page: opening a project changes its Recent rows,
    //: and the next visit's home page should not be the one from before.
    let homeTimer = null;
    function keepHome(frame, pages) {
        let route = null;
        try {
            route = inApp(frame.contentWindow.location);
        } catch (error) {
            return;
        }
        if (EARLY_ROUTES.has(route)) return;
        global.clearTimeout(homeTimer);
        homeTimer = global.setTimeout(async () => {
            try {
                const response = await global.PlexoraPython.fetch("/", { headers: { Accept: "text/html" } });
                if (response.ok) await pages.put("/", await response.text());
            } catch (error) { /* kept as it was */ }
        }, 5000);
    }

    //: Keep the tab's address and title what the frame shows.
    function mirror() {
        const top = host;
        const sync = () => {
            try {
                if (here(top.location) !== here(global.location)) {
                    top.history.replaceState(null, "", here(global.location));
                }
                if (doc.title && top.document.title !== doc.title) top.document.title = doc.title;
            } catch (error) { /* the host went away */ }
        };
        for (const name of ["pushState", "replaceState"]) {
            const original = global.history[name].bind(global.history);
            global.history[name] = function (...args) {
                const result = original(...args);
                sync();
                return result;
            };
        }
        global.addEventListener("popstate", sync);
        global.addEventListener("hashchange", sync);
        global.addEventListener("load", sync);
        const title = doc.querySelector("title");
        if (title) new MutationObserver(sync).observe(title, { childList: true, characterData: true, subtree: true });
        sync();
    }

    //: The project a page Python rendered is for (its variables block's
    //: `datasource`), or "".
    function projectOf(html) {
        const found = /<script type="application\/json" id="plexora-variables">([\s\S]*?)<\/script>/.exec(html);
        if (!found) return "";
        try {
            return JSON.parse(found[1]).datasource || "";
        } catch (error) {
            return "";
        }
    }

    //: A project made in the desktop app names its files as that app knows
    //: them: asked once, in the loader, for the folder that holds them
    //: (PlexoraPicks.find), and what is found is recorded for Python. True
    //: when something was found (the page is then asked for again).
    async function findFiles(project) {
        const route = `/project/${encodeURIComponent(project)}/files_here`;
        let missing = [];
        try {
            missing = (await (await global.PlexoraPython.fetch(route)).json()).missing || [];
        } catch (error) {
            return false;
        }
        if (!missing.length) return false;
        // Only copies this browser made and keeps in a folder it may no
        // longer read without a click (Python reports them `waiting`): that
        // folder asked for again in one click, not the files looked for.
        if (missing.every((item) => item && item.waiting) && global.PlexoraDerived
            && typeof global.PlexoraDerived.reconnect === "function") {
            const open = await askInLoader(
                missing.length === 1 ? "This project's pyramid is in a folder the browser needs to open again."
                    : "This project's pyramids are in a folder the browser needs to open again.",
                [{ value: false, label: "Skip" }, { value: true, label: "Open folder", primary: true }]);
            if (!open) return false;
            try {
                const done = await global.PlexoraDerived.reconnect();
                return Boolean(done && done.mounted && done.mounted.length);
            } catch (error) {
                return false;
            } finally {
                screen.resume();
            }
        }
        if (!global.PlexoraPicks || !global.PlexoraPicks.find) return false;
        // A folder to look in where the browser has a folder picker; the file
        // itself where it has not (Firefox, Safari).
        const folders = typeof global.showDirectoryPicker === "function";
        const label = folders ? "Find them…" : missing.length === 1 ? "Locate file…" : "Locate files…";
        const go = await askInLoader(
            missing.length === 1 ? "This project's image is on this computer." : "This project's files are on this computer.",
            [{ value: false, label: "Skip" }, { value: true, label, primary: true }]);
        if (!go) return false;
        status("Looking for the files");
        try {
            const found = await global.PlexoraPicks.find(missing).catch(() => ({}));
            if (!Object.keys(found).length) return false;
            await global.PlexoraPython.fetch(route, { method: "POST", headers: { "Content-Type": "application/json" },
                                                      body: JSON.stringify({ found }) });
            return true;
        } finally {
            screen.resume();
        }
    }

    //: Takes this frame out of the host's layout for two of the host's frames,
    //: and resolves to the function that puts it back; frameBoot opens the
    //: app's document in between.
    //:
    //: Chrome (157) never gives pointer capture to a document opened while
    //: the pointer is over its frame -- over the last page of the app, or
    //: over this loader while it waited on Python, which is every page but a
    //: first one the mouse has not yet touched. `setPointerCapture` does
    //: nothing, and every pointer event arrives as if the pointer had just
    //: come in from outside the page: pointerover and pointerenter from the
    //: document down. OpenSeadragon takes the position from that pointerenter
    //: as the drag's last position, so every drag measured 0 px and the image
    //: zoomed but never panned; anything else that captures the pointer (a
    //: slider dragged past its end) lost it. The document stays like that
    //: when the pointer leaves and comes back. Opened while the frame has no
    //: layout box it is an ordinary document. Hiding it any other way
    //: (pointer-events, visibility, display for less than two frames) is not
    //: enough. Reproduced outside Plexora with a host page, an iframe and
    //: document.open alone (tests/test_shell_pointer_capture.py).
    //:
    //: The host's frames, not this one's: a frame without a box draws none.
    //: They can come late -- 0.46 and 0.71 s on the preview in headless
    //: Chrome while a project opened -- and a frame cut short by a timer is
    //: no use, so the timer is only for frames that never come.
    //:
    //: A tab in the background draws no frames at all, and it used to be
    //: opened as it was, unguarded. That is the import's case: a pyramid
    //: builds for minutes, the user looks at another window, the sample
    //: opens by itself, and the pointer the frame last saw is still over it --
    //: the viewer then zoomed but never panned until a reload. So the frame
    //: stays out of the layout until the tab is shown again, and then for two
    //: of the host's frames as above. A hidden tab shows nothing, so the wait
    //: costs nothing anyone sees. Self-contained (tests/browser/harness/
    //: pointer_capture/frame.html takes this function out of the file).
    function hiddenForOpen() {
        const frame = global.frameElement;
        if (!frame) return Promise.resolve(() => {});
        const before = frame.style.display;
        frame.style.display = "none";
        const shown = () => { frame.style.display = before; };
        const visible = () => host.document.visibilityState === "visible";
        const untilVisible = () => (visible() ? Promise.resolve() : new Promise((resolve) => {
            const seen = () => {
                if (!visible()) return;
                host.document.removeEventListener("visibilitychange", seen);
                resolve();
            };
            host.document.addEventListener("visibilitychange", seen);
        }));
        //: Resolves true after two of the host's frames, false if none came
        //: in 5 s.
        const twoFrames = () => new Promise((resolve) => {
            const timer = global.setTimeout(() => resolve(false), 5000);
            host.requestAnimationFrame(() => host.requestAnimationFrame(() => {
                global.clearTimeout(timer);
                resolve(true);
            }));
        });
        return (async () => {
            // Hidden again before the two frames came: wait for it again.
            for (;;) {
                await untilVisible();
                if (await twoFrames() || visible()) return shown;
            }
        })();
    }

    //: Whether the page just written gets pointer capture, checked on its
    //: first press of the main mouse button; `broken()` runs if it does not.
    //: The guard above covers every way in that is known; this is for one
    //: that is not, because the fault's only sign to a user is an image that
    //: zooms and never pans. The capture is taken on the root element and
    //: let go at once, before the press reaches the page: a target's own
    //: lostpointercapture would bubble to OpenSeadragon's tracker and end
    //: its drag. Self-contained, like hiddenForOpen.
    function watchCapture(doc, broken) {
        const check = (event) => {
            if (event.pointerType !== "mouse" || event.button !== 0) return;
            doc.removeEventListener("pointerdown", check, true);
            const root = doc.documentElement;
            let held = true;
            try {
                root.setPointerCapture(event.pointerId);
                held = root.hasPointerCapture(event.pointerId);
                if (held) root.releasePointerCapture(event.pointerId);
            } catch (error) {
                return;     // no pointer to capture: nothing learnt
            }
            if (!held) broken();
        };
        doc.addEventListener("pointerdown", check, true);
    }

    //: ⌘R / Ctrl+R / F5 reload the app's frame, not the site: the page is
    //: Python's answer again a moment later, with Python, the picked files
    //: and the project store as they are. A reload of the whole site starts
    //: Python over (5 s and more on a fast computer). With Shift -- a hard
    //: reload -- the browser's own reload still runs, the way out of a
    //: Python that has stopped answering; the browser's reload button cannot
    //: be caught at all. On `target` (a window or document), capturing, so
    //: no page handler sees the key first.
    function reloadKey(event) {
        if (event.shiftKey || event.altKey) return false;
        if (event.key === "F5") return !event.metaKey && !event.ctrlKey;
        return (event.metaKey || event.ctrlKey) && (event.key === "r" || event.key === "R");
    }

    function reloadsFrame(target, frameOf) {
        target.addEventListener("keydown", (event) => {
            if (!reloadKey(event)) return;
            const frame = frameOf();
            if (!frame || !frame.contentWindow) return;
            event.preventDefault();
            frame.contentWindow.location.reload();
        }, true);
    }

    //: The app's page `html` in place of this loader. `project`: the
    //: project it shows, or "".
    async function writePage(html, project, address) {
        const shown = await hiddenForOpen();
        loaderGone();
        doc.open();
        shown();
        // WebKit gives a document opened here the host's address (it takes
        // the entry document from the host's promise this continues): this
        // page's own address back, before the page's scripts read it.
        if (here(global.location) !== address) global.history.replaceState(null, "", address);
        doc.write(html);
        doc.close();
        doc.documentElement.classList.add("is-website");
        // On the document: document.open took every listener off the window.
        reloadsFrame(doc, () => global.frameElement);
        // Opened without pointer capture all the same: opened again, as a
        // reload would, but only the frame (Python and the host stay) and at
        // most once a minute, so an unknown cause cannot loop.
        watchCapture(doc, () => {
            const last = host.__plexoraCaptureReopened || 0;
            if (Date.now() - last < 60000) {
                console.warn("shell: this page has no pointer capture; not opened again so soon");
                return;
            }
            host.__plexoraCaptureReopened = Date.now();
            console.warn("shell: this page has no pointer capture (Chrome's document.open fault); opening it again");
            global.location.reload();
        });
        // More than a browser tab handles well: the desktop app offered.
        if (project && host.PlexoraEnvelope) host.PlexoraEnvelope.check(project, global).catch(() => null);
        // On the document, not the button: the app's router swaps pages in
        // place, so the project is the one shown when the item is clicked.
        doc.addEventListener("click", (event) => {
            const restart = event.target.closest && event.target.closest("#nav_restart_python");
            if (restart && host.PlexoraPython) {
                const confirm = global.PlexoraConfirm;
                const asked = confirm ? confirm.ask({
                    title: "Restart Plexora in this tab?",
                    body: "Anything still running here stops (an import, a mask being built). "
                          + "Your projects are kept.",
                    confirm: "Restart",
                }) : Promise.resolve(true);
                asked.then((go) => { if (go) host.PlexoraPython.restart("Help › Restart"); });
                return;
            }
            const item = event.target.closest && event.target.closest("#nav_open_desktop, #nav_open_bundle");
            if (!item || !host.PlexoraHandoff) return;
            if (item.id === "nav_open_bundle") {
                host.PlexoraHandoff.openFile(global);
                return;
            }
            const shown = global.flaskVariables && global.flaskVariables.datasource;
            if (shown) host.PlexoraHandoff.open(shown, global);
        });
        mirror();
        reveal();
    }

    //: The written page faded in over the host's loader, once it can draw:
    //: its first animation frame (none while its stylesheets still block
    //: it), or 400 ms in a tab in the background, which draws none.
    function reveal() {
        if (!shared) return;
        let done = false;
        const go = () => {
            if (done) return;
            done = true;
            shared.shown();
        };
        global.requestAnimationFrame(go);
        global.setTimeout(go, 400);
    }

    //: The written page parsed, its deferred scripts run.
    const parsed = () => (doc.readyState !== "loading" ? Promise.resolve()
        : new Promise((resolve) => doc.addEventListener("DOMContentLoaded", resolve, { once: true })));

    //: The page the host kept for this address (hostBoot `early`), taken once.
    function takeEarly(route) {
        const early = host.PlexoraShellEarly;
        host.PlexoraShellEarly = null;
        return early && early.route === route && typeof early.html === "string" ? early : null;
    }

    //: A kept page shown at once, while Python starts. Its requests wait for
    //: Python (services/backend.js `PlexoraShellGate`) and the status chip
    //: says "Starting" meanwhile. Then Python's own answer for the address:
    //: kept for the next visit; the same, or the same but for its times
    //: (written in); otherwise the page loads again from Python -- unless it
    //: has been used (pressed, typed in), when it stays as it is: a reload
    //: would throw away what was being done, a file being picked among it.
    //: `kept.opens`: the tool the address asked for (`?tool=`), on a kept
    //: plain viewer (`kept.page`, its address): opened once Python is up, and
    //: Python's plain page is what the kept one is checked against.
    async function earlyPage(route, html, address, early) {
        const kept = early || {};
        // A project's viewer, kept (services/keptAnswers.js `early`): its
        // image read by the browser node at once, as frameBoot does for a
        // page Python drew, while its first screen's requests are answered
        // from what Python said last time.
        const project = route === "/" ? "" : projectOf(html);
        await writePage(html, project, address);
        if (project && global.PlexoraNode) global.PlexoraNode.project(project);
        global.__plexoraEarly = { route, project };
        let used = false;
        const use = () => { used = true; };
        const USES = ["pointerdown", "keydown", "input", "change", "drop"];
        for (const type of USES) global.addEventListener(type, use, true);
        const task = parsed().then(() => (global.PlexoraStatus ? global.PlexoraStatus.begin("Starting") : null));
        let fresh = null;
        let checked = null;
        const asked = kept.opens && kept.page ? kept.page : route;
        try {
            await host.PlexoraShellStarted;
            const response = await global.PlexoraPython.fetch(asked, { headers: { Accept: "text/html" } });
            if (response.ok) fresh = await response.text();
            // Every kept answer the page was given, asked of Python again.
            if (project && host.PlexoraKept) checked = await host.PlexoraKept.checked(global).catch(() => null);
        } catch (error) {
            // Python did not start: the host takes the frame down and says so.
            return;
        } finally {
            const shown = await task;
            if (shown) shown.done();
            for (const type of USES) global.removeEventListener(type, use, true);
        }
        if (fresh === null) return;
        host.PlexoraPages.put(asked, fresh);
        const changed = Boolean(checked && checked.changed.length);
        global.__plexoraEarly.changed = checked ? checked.changed : null;
        global.__plexoraEarly.used = used;
        // The tool the address asked for, opened as from the Tools menu, on
        // a page that stands as it is.
        const opens = () => {
            if (!kept.opens || !global.PlexoraToolLoader || typeof global.PlexoraToolLoader.openTool !== "function") return;
            global.__plexoraEarly.opened = kept.opens;
            global.PlexoraToolLoader.openTool(kept.opens).catch(() => null);
        };
        // Left for another page meanwhile (a Recent row opened): `global` is
        // that page's window now, and nothing here is about it.
        if (global.document !== doc) return;
        if (fresh === html && !changed) {
            opens();
            return;
        }
        if (!changed && timeless(fresh) === timeless(html)) {
            const times = new DOMParser().parseFromString(fresh, "text/html").querySelectorAll("time");
            doc.querySelectorAll("time").forEach((node, i) => {
                if (times[i]) node.textContent = times[i].textContent;
            });
            opens();
            return;
        }
        if (!used) {
            global.location.replace(global.location.href);
            return;
        }
        // Used, and drawn from answers that are no longer so: its writes
        // were refused (services/backend.js), and a reload is offered.
        if (changed && global.PlexoraToast && typeof global.PlexoraToast.show === "function") {
            global.PlexoraToast.show({
                title: "This project changed",
                note: "It changed while Plexora was starting. Reload to see it as it is now.",
                timeout: 0,
                actions: [{ label: "Reload", primary: true,
                            onSelect: () => global.location.replace(global.location.href) }],
            });
        }
    }

    async function frameBoot() {
        const address = here(global.location);
        // The start's steps, as the host's loader takes them.
        if (shared) {
            const asked = inApp(global.location);
            unfollow = shared.follow((step) => screen.reach(settle(step, asked)));
        }
        browserVariables();
        // The host's Python and picks, not a second worker.
        global.PlexoraPython = host.PlexoraPython;
        global.PlexoraPicks = host.PlexoraPicks;
        global.PlexoraLocalFiles = host.PlexoraLocalFiles || null;
        global.PlexoraNode = host.PlexoraNode;
        global.PlexoraKept = host.PlexoraKept || null;
        // The host's licence, which it holds only once Python has started: read
        // through to it, since a kept page is up before then.
        Object.defineProperty(global, "PlexoraLicense", {
            configurable: true, enumerable: true, get: () => host.PlexoraLicense,
        });
        global.PlexoraGoogleAuth = host.PlexoraGoogleAuth;
        global.PlexoraShellFramed = true;
        // What the page's requests wait for (services/backend.js).
        global.PlexoraShellGate = host.PlexoraShellStarted || null;
        if (global.PlexoraNode) global.PlexoraNode.revalidate();
        await claimed();
        await scripts([at("/client/src/js/services/backendRoutes.js"), at("/client/src/js/services/backend.js")]);
        // Where the site's worker does not see this page's requests, the
        // page answers them itself.
        if (!await relayed()) {
            await script(at("/client/src/js/services/pageRelay.js"));
            global.PlexoraPageRelay.install(global);
            global.PlexoraShellRelay = "page";
        }
        const route = inApp(global.location);
        const early = takeEarly(route);
        if (early !== null) return earlyPage(route, early.html, address, early);
        // A page asked for while Python starts (one opened from a kept page)
        // is drawn once the licence and the picked files are in; this loader
        // follows the start's steps meanwhile (`unfollow` above).
        if (host.PlexoraShellStarted) await host.PlexoraShellStarted;
        const response = await global.PlexoraPython.fetch(route, { headers: { Accept: "text/html" } });
        let html = await response.text();
        // Only the page is left to draw.
        screen.reach(ALMOST);
        const project = projectOf(html);
        if (project && await findFiles(project)) {
            html = await (await global.PlexoraPython.fetch(route, { headers: { Accept: "text/html" } })).text();
        }
        // The next visit's first page, shown before Python is up.
        if (response.ok && !project && host.PlexoraPages) host.PlexoraPages.put(route, html);
        // A project's page: the browser node asks Python what it may serve
        // now, ahead of the page's own requests, so its first tile does not
        // wait behind them (or behind the work opening the project queues).
        if (project && global.PlexoraNode) global.PlexoraNode.project(project);
        await writePage(html, project, address);
    }

    (host ? frameBoot() : hostBoot())
        .catch((error) => fail(`Plexora could not start: ${error.message || error}`));
})(window);
