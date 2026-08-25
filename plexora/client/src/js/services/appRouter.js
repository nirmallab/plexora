/**
 * appRouter.js -- internal navigation that does not throw the viewer away.
 *
 * Plexora is a server-rendered multi-page app: every destination is its own
 * Flask-rendered document. So walking from a slide to the Figures page and back
 * used to destroy the OpenSeadragon viewer, its WebGL2 context, every decoded
 * tile and every scrap of session state that was not written to the server --
 * the viewport above all, which nothing persisted at all -- and then rebuild it
 * from cold on return. This file makes that one class of navigation happen
 * inside the document that is already open.
 *
 * The rule it enforces is the one the architecture already implied:
 *
 *     The viewer is rebuilt when, and only when, the PROJECT changes.
 *
 * Everything else -- Open Project, Settings, the import wizard, the figure
 * library, a figure's canvas -- is fetched as a fragment (see _fragment.html)
 * and rendered into #plexora_page_host while the viewer is hidden but ALIVE,
 * one CSS property away from being back on screen with its viewport, zoom,
 * channels, layers, tool cards and selections exactly as they were left.
 *
 * Three deliberate limits, each buying a large amount of safety:
 *
 * 1. **Only a document that booted AS a viewer routes at all.** Landing on
 *    /open_project and clicking a project is a full navigation, exactly as
 *    before. There is no viewer to preserve yet, and booting one client-side
 *    would mean re-entering main.js -- which has document-scoped top-level
 *    bindings (`const eventHandler`, `const datasource`) and can only run once.
 * 2. **A link to a DIFFERENT project is a full navigation.** The server holds
 *    exactly one loaded datasource (data_model._loaded_source) and ImageViewer
 *    has no destroy path, so "swap the image under the live viewer" is not
 *    something this app can do without pretending. This is the "only when the
 *    project changes" half of the rule above, and it is the reason there is no
 *    teardown code in this file to get wrong.
 * 3. **The viewer is hidden with `visibility`, never `display`.** OSD's
 *    autoResize compares its container's clientWidth/clientHeight every frame;
 *    `display: none` reports 0x0, which resizes the viewport to nothing and
 *    takes the zoom state with it -- the precise state this file exists to
 *    protect. Keeping the box laid out (see .plexora-view-hidden in main.css)
 *    means OSD sees no change at all, and revealing it is a repaint of a canvas
 *    that never stopped being correct.
 *
 * Anything it cannot do, it declines to do: an unroutable link, a fragment that
 * will not fetch or will not parse, all fall through to `window.location`, which
 * lands the user exactly where they asked to go by the route that always worked.
 */
window.PlexoraRouter = (function () {
    "use strict";

    //: Mirrors page_routes.FRAGMENT_HEADER. A header rather than a query
    //: parameter so the URL a fragment is fetched from is the same URL that
    //: ends up in the address bar.
    const FRAGMENT_HEADER = "X-Plexora-Fragment";
    const HIDDEN_CLASS = "plexora-view-hidden";

    const body = document.body;
    const datasource = body ? body.dataset.plexoraDatasource || "" : "";
    const viewerHost = document.getElementById("container");
    const pageHost = document.getElementById("plexora_page_host");

    // Limit 1. Nothing here is worth preserving, so every link on this page
    // keeps its ordinary browser behaviour.
    //
    // A working `go()` is still returned rather than null, so a caller that
    // navigates from JavaScript writes one line -- `PlexoraRouter.go(url)` --
    // and never its own "route if you can, otherwise set location" fallback.
    // Two spellings of that fallback is how one of them ends up wrong.
    if (!datasource || !viewerHost || !pageHost) {
        return {
            go: (href) => { window.location.href = href; },
            canRoute: () => false,
            datasource: () => "",
        };
    }

    //: The viewer's own URL. Everything else is somewhere the user is visiting.
    const homePath = window.location.pathname;
    const homeTitle = document.title;

    //: Project names, for telling `/tonsil` (a viewer) from `/settings` (a page)
    //: without asking the server. Already on every page as flaskVariables.
    const projects = new Set(window.flaskVariables?.datasources || []);

    //: Stylesheets a fragment brought in, by resolved href. Disabled rather than
    //: removed when its page leaves: re-enabling cannot refetch, and leaving
    //: them live is not an option -- openProject.css and import.css both style
    //: bare class names that also appear in the viewer's sidebar.
    const sheets = new Map();

    //: Every <script src> this document has already run. A fragment that names
    //: one again must NOT re-execute it: these are classic scripts and several
    //: declare a top-level `class`, whose re-declaration is a SyntaxError rather
    //: than a harmless no-op. columnClassifier, coordinateField and
    //: segmentationProgress are all loaded by base.html and named again by the
    //: pages that use them, so this is the common case, not the corner.
    const executed = new Set();

    //: Resolved hrefs of the stylesheets the document loaded for itself. Never
    //: disabled by a page swap -- they are not this file's to switch off.
    const ownSheets = new Set();

    let navigating = false;

    //: The last destination asked for while another was still being fetched.
    //: One slot, not a queue: the intermediate pages of a burst of Back presses
    //: are places the user went THROUGH, and rendering each in turn would be
    //: slower and end up in the same place.
    let queued = null;

    document.querySelectorAll("script[src]")
        .forEach((node) => executed.add(assetKey(node.src)));
    document.querySelectorAll('link[rel="stylesheet"]')
        .forEach((node) => ownSheets.add(node.href));

    /** An asset's identity for "have I already run this": path, minus `?v=`. */
    function assetKey(src) {
        try {
            return new URL(src, document.baseURI).pathname;
        } catch (error) {
            return String(src);
        }
    }

    /**
     * The project a URL is the viewer for, or null.
     *
     * A viewer URL is one path segment that names a project -- the same shape
     * page_routes.image_viewer matches, and the reason that route 404s an
     * unknown name rather than rendering an empty viewer.
     */
    function projectOf(url) {
        const base = plexoraBaseUrl();
        let path = url.pathname;
        if (base && path.startsWith(base)) path = path.slice(base.length);
        const parts = path.split("/").filter(Boolean);
        if (parts.length !== 1) return null;
        let name;
        try {
            name = decodeURIComponent(parts[0]);
        } catch (error) {
            return null;   // a malformed escape is not a project name
        }
        return projects.has(name) ? name : null;
    }

    /** Whether this URL can be served without leaving the document. */
    function canRoute(url) {
        if (url.origin !== window.location.origin) return false;
        // Limit 2: a different image means a different loaded datasource on the
        // server and a viewer that would have to be torn down. Let the browser
        // do what it has always done.
        const project = projectOf(url);
        if (project && project !== datasource) return false;
        return true;
    }

    // -- showing the viewer again -------------------------------------------

    /**
     * Put the live viewer back on screen.
     *
     * Nothing is rebuilt and nothing is fetched. Everything below is either
     * clearing the page that was covering it or telling the parts of the app
     * that stood down while it was hidden that they can start again.
     */
    function showViewer(url) {
        PlexoraPage.unmount();
        pageHost.replaceChildren();
        pageHost.hidden = true;
        for (const link of sheets.values()) link.disabled = true;

        viewerHost.classList.remove(HIDDEN_CLASS);
        document.title = homeTitle;

        // Belt and braces: the container never changed size (limit 3), so the
        // canvas already holds the right pixels and this is a repaint at worst.
        window.__plexora?.seaDragonViewer?.viewer?.forceRedraw?.();

        // Announced rather than pushed to named listeners, the same way ROI
        // announces a hover: a plugin that suspended a poll or released the
        // canvas pointer handlers is the only thing that knows it did.
        window.dispatchEvent(new CustomEvent("plexora:viewer-shown"));

        // A link back into the viewer may name a tool -- the figure canvas's
        // back arrow does, so the capture dock is there on arrival. Opening one
        // that is already open would toggle it shut.
        const tool = url.searchParams.get("tool");
        const loader = window.PlexoraToolLoader;
        if (tool && loader && !loader.isToolOpen?.(tool)) {
            loader.toggleTool?.(tool);
        }
    }

    // -- showing a page over it ---------------------------------------------

    /**
     * Fetch a page's content, mount it, and hide the viewer behind it.
     *
     * Returns the URL the content actually came from, which is not always the
     * one asked for, or null when it turned out not to be routable after all
     * and a real navigation is already under way.
     */
    async function showPage(url) {
        const response = await fetch(url.href, {
            headers: { [FRAGMENT_HEADER]: "1" },
            credentials: "same-origin",
        });
        if (!response.ok) throw new Error(`fragment ${response.status}`);

        // A route that redirected -- /project/<name>/columns sends an already
        // configured project onwards -- served a page from somewhere other than
        // where we asked. The address bar has to say where the content actually
        // came from, and if that somewhere is a different project this is not a
        // routable navigation at all.
        if (response.redirected) {
            const landed = new URL(response.url);
            if (!canRoute(landed)) {
                window.location.href = landed.href;
                return null;
            }
            if (landed.pathname === homePath) {
                showViewer(landed);
                return landed;
            }
            url = landed;
        }

        const parsed = new DOMParser().parseFromString(await response.text(), "text/html");

        // Before anything is visible, so the page is never painted unstyled.
        await adoptStyles(parsed);

        PlexoraPage.unmount();
        window.dispatchEvent(new CustomEvent("plexora:viewer-hidden"));
        viewerHost.classList.add(HIDDEN_CLASS);

        // DOMParser hoists <title> into its head wherever the template put it --
        // the figure workspace declares one inside its content block.
        document.title = parsed.querySelector("title")?.textContent || homeTitle;

        pageHost.replaceChildren();
        for (const node of Array.from(parsed.body.childNodes)) {
            pageHost.appendChild(node);
        }

        // Pulled out AFTER the markup is in place, and in document order:
        // inserted as markup a <script> never runs, and several of these read
        // the elements beside them as their first act.
        const scripts = Array.from(pageHost.querySelectorAll("script"));
        scripts.forEach((node) => node.remove());

        pageHost.hidden = false;
        for (const node of scripts) await runScript(node);

        // Controllers whose file was already loaded get no script tag to run,
        // so this is the only thing that mounts them on a second visit.
        PlexoraPage.boot();
        window.PlexoraShortcuts?.scan(pageHost);
        window.scrollTo(0, 0);
        return url;
    }

    /**
     * Make the fragment's stylesheets the live set, and nothing else.
     *
     * Enabling and disabling rather than adding and removing, so the second
     * visit to a page costs no request and cannot flash unstyled.
     */
    function adoptStyles(parsed) {
        const wanted = new Set();
        const pending = [];

        parsed.querySelectorAll('link[rel="stylesheet"]').forEach((node) => {
            const href = new URL(node.getAttribute("href"), document.baseURI).href;
            wanted.add(href);
            // Loaded by the document itself -- viewer.css is on both the viewer
            // and the figure workspace. Already applying, and not ours to
            // disable when the page leaves.
            if (ownSheets.has(href)) return;

            const known = sheets.get(href);
            if (known) {
                known.disabled = false;
                return;
            }
            const link = document.createElement("link");
            link.rel = "stylesheet";
            link.href = href;
            sheets.set(href, link);
            pending.push(new Promise((resolve) => {
                link.onload = resolve;
                link.onerror = resolve;   // a missing stylesheet is not fatal
            }));
            document.head.appendChild(link);
        });

        for (const [href, link] of sheets) link.disabled = !wanted.has(href);
        return Promise.all(pending);
    }

    /**
     * Run one of a fragment's scripts, once.
     *
     * External scripts are awaited so the next one -- and the controllers that
     * follow -- see what this one defined. Inline scripts run in place, where
     * the elements they reach for already are.
     */
    function runScript(node) {
        const src = node.getAttribute("src");
        if (!src) {
            const inline = document.createElement("script");
            inline.textContent = node.textContent;
            pageHost.appendChild(inline);
            return Promise.resolve();
        }

        const key = assetKey(src);
        if (executed.has(key)) return Promise.resolve();
        executed.add(key);

        return new Promise((resolve) => {
            const script = document.createElement("script");
            for (const attr of node.attributes) {
                script.setAttribute(attr.name, attr.value);
            }
            // `defer` is meaningless for a script inserted after parsing, and
            // async:false is what keeps these in the order the page wrote them.
            script.removeAttribute("defer");
            script.async = false;
            script.onload = resolve;
            script.onerror = resolve;   // PlexoraPage.boot() still runs; the
            document.head.appendChild(script);   // page renders without it
        });
    }

    // -- navigation ----------------------------------------------------------

    /**
     * Go to `href` without leaving the document, or fall back to the browser.
     *
     * The address bar is updated only after the page is actually on screen: a
     * fetch that fails ends in a real navigation, and a URL that had already
     * been pushed would make that navigation the WRONG one.
     */
    async function go(href, options) {
        const url = new URL(href, window.location.href);
        if (!canRoute(url)) {
            window.location.href = url.href;
            return;
        }
        if (navigating) {
            // Held rather than dropped. For a popstate that distinction is the
            // whole thing: the browser has ALREADY moved the address bar, so
            // ignoring it leaves the URL describing a page that is not on
            // screen -- which is what holding Back down used to do.
            queued = { href: url.href, options };
            return;
        }
        navigating = true;
        const task = window.PlexoraStatus?.begin("Opening");
        try {
            let landed = url;
            if (url.pathname === homePath) showViewer(url);
            else landed = await showPage(url);
            // null means showPage handed off to a real navigation; the address
            // bar is about to be the browser's business, not ours.
            if (landed && (!options || options.push !== false)) {
                window.history.pushState({ plexora: true }, "",
                    landed.pathname + landed.search + landed.hash);
            }
        } catch (error) {
            console.error("Plexora: could not route, navigating instead", error);
            window.location.href = url.href;
        } finally {
            task?.done();
            navigating = false;
            const next = queued;
            queued = null;
            if (next) go(next.href, next.options);
        }
    }

    function onClick(event) {
        if (event.defaultPrevented || event.button !== 0) return;
        // A modified click is the user asking the BROWSER for something -- a new
        // tab, a download, a saved link -- and none of it is this file's to
        // reinterpret.
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;

        const anchor = event.target.closest?.("a[href]");
        if (!anchor) return;
        if (anchor.target && anchor.target !== "_self") return;
        if (anchor.hasAttribute("download")) return;
        // Tools-menu rows belong to toolLoader, which opens a tool in place and
        // never navigates at all.
        if (anchor.hasAttribute("data-tool")) return;

        const href = anchor.getAttribute("href");
        if (!href || href.startsWith("#")) return;

        let url;
        try {
            url = new URL(href, window.location.href);
        } catch (error) {
            return;
        }
        if (!canRoute(url)) return;

        event.preventDefault();
        go(url.href);
    }

    // Capture phase deliberately NOT used: a handler that has called
    // preventDefault (a confirm-before-leaving, a form the page owns) has
    // already decided, and this must see that decision rather than pre-empt it.
    document.addEventListener("click", onClick);

    // Back and forward through everything pushed above. `push: false` because
    // the entry being restored is already the current one.
    window.addEventListener("popstate", () => {
        go(window.location.href, { push: false });
    });

    return {
        /** Navigate without leaving the document, if this URL allows it. */
        go,
        /** Whether this URL would be routed rather than navigated to. */
        canRoute: (href) => {
            try {
                return canRoute(new URL(href, window.location.href));
            } catch (error) {
                return false;
            }
        },
        /** Which project this document's live viewer is showing. */
        datasource: () => datasource,
    };
})();
