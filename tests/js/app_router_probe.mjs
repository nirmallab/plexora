/**
 * What appRouter.js routes, what it refuses to route, and what survives.
 *
 * The whole value of this file is one sentence: the viewer is rebuilt when, and
 * only when, the project changes. That is a claim about a handful of decisions,
 * and every one of them is invisible in review and expensive to check by hand --
 * you would have to open a slide, walk somewhere, come back, and look at whether
 * the zoom survived.
 *
 * The five that matter, and what each of them costs when it is wrong:
 *
 *   - **Coming back to the viewer fetches NOTHING.** If it ever fetches, the
 *     viewer is being rebuilt and the entire feature is gone while still looking
 *     like it works.
 *   - **A link to a different project is NOT routed.** The server holds one
 *     loaded datasource and ImageViewer has no destroy path, so routing one of
 *     these would leave the old image under the new project's controls.
 *   - **A modified click is never routed.** Ctrl-click means "new tab", and a
 *     router that swallows it steals a browser affordance the user asked for.
 *   - **An already-loaded script is not re-executed.** These are classic
 *     scripts; several declare a top-level `class`, whose re-declaration is a
 *     SyntaxError that would take the page down rather than no-op.
 *   - **A fragment that will not load falls back to a real navigation.** The
 *     user asked to go somewhere; failing to route is not a reason to stay.
 *
 * Run against the shipped file, in a DOM stand-in built here. The stand-in is
 * deliberately small: what is under test is the router's decisions, not the
 * browser's HTML parser, so the fragments below are the simple shapes the real
 * templates produce rather than arbitrary markup.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/appRouter.js");
const ORIGIN = "http://localhost:8000";

// -- a DOM small enough to read and wide enough to route in -------------------

function makeElement(tag, id) {
    const classes = new Set();
    const attributes = new Map();
    const element = {
        tagName: String(tag || "div").toUpperCase(),
        id: id || "",
        dataset: {},
        hidden: false,
        disabled: false,
        textContent: "",
        children: [],
        parent: null,
        onload: null,
        onerror: null,
        classList: {
            add: (name) => classes.add(name),
            remove: (name) => classes.delete(name),
            contains: (name) => classes.has(name),
        },
        get attributes() {
            return Array.from(attributes, ([name, value]) => ({ name, value }));
        },
        getAttribute: (name) => (attributes.has(name) ? attributes.get(name) : null),
        setAttribute(name, value) {
            attributes.set(name, String(value));
            if (name === "id") element.id = String(value);
            if (name.startsWith("data-")) {
                const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
                element.dataset[key] = String(value);
            }
        },
        hasAttribute: (name) => attributes.has(name),
        removeAttribute: (name) => attributes.delete(name),
        appendChild(child) {
            child.parent = element;
            element.children.push(child);
            // A <script> or <link> the router inserts into the live document is
            // what "it loaded" means here, so resolve it the way the browser
            // would -- asynchronously, or the awaits in showPage never yield.
            if (child.onload) setTimeout(() => child.onload(), 0);
            return child;
        },
        replaceChildren() {
            element.children.forEach((child) => { child.parent = null; });
            element.children = [];
        },
        remove() {
            const siblings = element.parent?.children;
            if (siblings) siblings.splice(siblings.indexOf(element), 1);
            element.parent = null;
        },
        querySelectorAll(selector) {
            const wanted = selector.toUpperCase();
            const found = [];
            const walk = (node) => node.children.forEach((child) => {
                if (child.tagName === wanted) found.push(child);
                walk(child);
            });
            walk(element);
            return found;
        },
        closest(selector) {
            let node = element;
            while (node) {
                if (selector === "a[href]"
                    && node.tagName === "A" && node.hasAttribute("href")) return node;
                node = node.parent;
            }
            return null;
        },
    };
    // The router walks a parsed fragment by childNodes and the live document by
    // children; both are the same list here, since the parser below keeps no
    // text nodes at the top level.
    Object.defineProperty(element, "childNodes", { get: () => element.children });
    // `href` and `rel` are real properties on the elements the router creates,
    // and `href` is the RESOLVED form -- which is what makes ownSheets able to
    // recognise a stylesheet the document already has.
    Object.defineProperty(element, "href", {
        get: () => {
            const raw = attributes.get("href");
            return raw === undefined ? "" : new URL(raw, ORIGIN + "/").href;
        },
        set: (value) => attributes.set("href", String(value)),
    });
    Object.defineProperty(element, "rel", {
        get: () => attributes.get("rel") || "",
        set: (value) => attributes.set("rel", String(value)),
    });
    Object.defineProperty(element, "src", {
        get: () => {
            const raw = attributes.get("src");
            return raw === undefined ? "" : new URL(raw, ORIGIN + "/").href;
        },
        set: (value) => attributes.set("src", String(value)),
    });
    return element;
}

/**
 * Enough of an HTML parser for the shapes _fragment.html actually emits: a run
 * of <link> tags, an optional <title>, then markup with scripts in it.
 *
 * Not a general parser and not trying to be. A real one would be testing the
 * browser rather than the router.
 */
function parseFragment(html) {
    const body = makeElement("body");
    const head = makeElement("head");
    const TAG = /<(\/?)([a-zA-Z0-9]+)([^>]*?)(\/?)>|([^<]+)/g;
    const ATTR = /([a-zA-Z-]+)(?:="([^"]*)")?/g;
    const stack = [body];
    let match;
    while ((match = TAG.exec(html))) {
        const [, closing, rawName, rawAttrs, selfClosing, text] = match;
        if (text !== undefined) {
            const top = stack[stack.length - 1];
            if (top !== body) top.textContent += text;
            continue;
        }
        const name = rawName.toLowerCase();
        if (closing) {
            if (stack.length > 1) stack.pop();
            continue;
        }
        const element = makeElement(name);
        let attr;
        ATTR.lastIndex = 0;
        while ((attr = ATTR.exec(rawAttrs || ""))) {
            element.setAttribute(attr[1], attr[2] === undefined ? "" : attr[2]);
        }
        // A real parser hoists these into <head> wherever the template put
        // them, which the router relies on for both.
        if (name === "link" || name === "title") head.appendChild(element);
        else stack[stack.length - 1].appendChild(element);
        if (!selfClosing && name !== "link" && name !== "meta") {
            if (name !== "title") stack.push(element);
            else { stack.push(element); }
        }
    }
    const document = {
        body,
        head,
        querySelector: (selector) =>
            (selector === "title" ? head.children.find((c) => c.tagName === "TITLE") : null)
            || null,
        querySelectorAll: (selector) => {
            if (selector === 'link[rel="stylesheet"]') {
                return head.children.filter(
                    (c) => c.tagName === "LINK" && c.getAttribute("rel") === "stylesheet");
            }
            return body.querySelectorAll(selector);
        },
    };
    return document;
}

/**
 * A document with a live viewer in it, and a router loaded against it.
 *
 * @param datasource which project the viewer is showing. "" is the no-viewer
 *   case, where the router is expected to stand down entirely.
 * @param scripts what base.html already put in <head>. These go in BEFORE the
 *   router is loaded, because that is when it takes its census of what has
 *   already run -- appRouter.js is deferred, so in a browser every one of
 *   base.html's tags is in the document by then.
 */
function boot({ datasource = "demo", path = "/demo", fragments = {}, scripts = [] } = {}) {
    const body = makeElement("body");
    body.setAttribute("data-plexora-datasource", datasource);
    const head = makeElement("head");
    const viewerHost = makeElement("div", "container");
    const pageHost = makeElement("div", "plexora_page_host");
    pageHost.hidden = true;

    const byId = { container: viewerHost, plexora_page_host: pageHost };
    const log = { fetches: [], pushes: [], navigations: [], events: [], tools: [], booted: 0, unmounted: 0 };
    let clickHandler = null;
    let popstateHandler = null;

    const location = {
        origin: ORIGIN,
        get href() { return ORIGIN + this.pathname + this.search; },
        set href(value) { log.navigations.push(value); },
        pathname: path,
        search: "",
    };

    const document = {
        body,
        head,
        baseURI: ORIGIN + "/",
        title: "Viewer",
        getElementById: (id) => byId[id] || null,
        createElement: (tag) => makeElement(tag),
        querySelectorAll: (selector) => {
            if (selector === "script[src]") return head.querySelectorAll("script");
            if (selector === 'link[rel="stylesheet"]') {
                return head.querySelectorAll("link")
                    .filter((node) => node.getAttribute("rel") === "stylesheet");
            }
            return [];
        },
        addEventListener: (type, handler) => {
            if (type === "click") clickHandler = handler;
        },
    };

    const globals = {
        document,
        URL,
        setTimeout,
        console: { error() {}, warn() {}, log() {} },
        DOMParser: class { parseFromString(html) { return parseFragment(html); } },
        CustomEvent: class { constructor(type) { this.type = type; } },
        plexoraBaseUrl: () => "",
        PlexoraPage: {
            register() {},
            boot: () => { log.booted += 1; },
            unmount: () => { log.unmounted += 1; },
        },
        fetch: async (href) => {
            log.fetches.push(href);
            const key = new URL(href, ORIGIN).pathname;
            const answer = fragments[key];
            if (answer === undefined) return { ok: false, status: 404 };
            return {
                ok: true, status: 200, redirected: Boolean(answer.redirectedTo),
                url: answer.redirectedTo ? ORIGIN + answer.redirectedTo : href,
                text: async () => answer.html ?? answer,
            };
        },
    };
    globals.window = {
        location,
        flaskVariables: { datasources: ["demo", "tonsil"] },
        history: { pushState: (state, title, url) => log.pushes.push(url) },
        addEventListener: (type, handler) => {
            if (type === "popstate") popstateHandler = handler;
        },
        dispatchEvent: (event) => log.events.push(event.type),
        scrollTo() {},
        PlexoraStatus: { begin: () => ({ done() {}, fail() {} }) },
        PlexoraShortcuts: { scan() {} },
        PlexoraToolLoader: {
            isToolOpen: (name) => log.tools.includes(name),
            toggleTool: (name) => log.tools.push(name),
        },
        __plexora: { seaDragonViewer: { viewer: { forceRedraw() {} } } },
    };
    globals.globalThis = globals;

    for (const src of scripts) {
        const node = makeElement("script");
        node.setAttribute("src", src);
        head.appendChild(node);
    }

    const ctx = createContext(globals);
    runInContext(readFileSync(SOURCE, "utf8"), ctx, { filename: "appRouter.js" });

    /** Click an anchor, the way a user does. */
    const click = (attributes, modifiers = {}) => {
        const anchor = makeElement("a");
        for (const [name, value] of Object.entries(attributes)) anchor.setAttribute(name, value);
        anchor.target = attributes.target || "";
        let defaultPrevented = false;
        // No listener at all is the no-viewer case: the router stood down and
        // never registered one, which is exactly how it declines to intercept.
        if (!clickHandler) return { defaultPrevented: false, listening: false };
        clickHandler({
            defaultPrevented: false,
            button: 0,
            metaKey: false, ctrlKey: false, shiftKey: false, altKey: false,
            ...modifiers,
            target: anchor,
            preventDefault() { defaultPrevented = true; },
        });
        return { defaultPrevented };
    };

    return {
        router: globals.window.PlexoraRouter ?? ctx.PlexoraRouter,
        log, click, viewerHost, pageHost, document, head,
        popstate: () => popstateHandler(),
        hidden: () => viewerHost.classList.contains("plexora-view-hidden"),
    };
}

/** Let the router's awaits settle.
 *
 *  Several macrotask turns rather than one: showPage awaits the fetch, then the
 *  stylesheets, then each script in turn, and every one of those resolves from
 *  a setTimeout in the stand-in above. */
const settle = async () => {
    for (let turn = 0; turn < 20; turn += 1) {
        await new Promise((resolve) => setTimeout(resolve, 0));
    }
};

const said = [];
const check = (label, fn) => { fn(); said.push(label); console.log(label); };
const checkAsync = async (label, fn) => { await fn(); said.push(label); console.log(label); };

// -- the decisions ------------------------------------------------------------

await checkAsync("a page link routes and never touches the viewer", async () => {
    const app = boot({ fragments: { "/settings": "<div id=\"settings\"></div>" } });
    const { defaultPrevented } = app.click({ href: "/settings" });
    assert.equal(defaultPrevented, true, "the browser navigation was not suppressed");
    await settle();
    assert.deepEqual(app.log.fetches.map((href) => new URL(href).pathname), ["/settings"]);
    assert.equal(app.hidden(), true, "the viewer was not hidden");
    assert.equal(app.pageHost.hidden, false, "the page host stayed hidden");
    assert.equal(app.pageHost.children.length, 1, "the page was not mounted");
    assert.deepEqual(app.log.pushes, ["/settings"]);
});

await checkAsync("coming back to the viewer fetches nothing at all", async () => {
    const app = boot({ fragments: { "/settings": "<div id=\"settings\"></div>" } });
    app.click({ href: "/settings" });
    await settle();
    const after = app.log.fetches.length;

    app.click({ href: "/demo" });
    await settle();
    assert.equal(app.log.fetches.length, after,
        "returning to the viewer issued a request, so it is being rebuilt");
    assert.equal(app.hidden(), false, "the viewer was not shown again");
    assert.equal(app.pageHost.hidden, true, "the page host was left on screen");
    assert.equal(app.pageHost.children.length, 0, "the page was left mounted");
    assert.deepEqual(app.log.navigations, [], "it navigated instead of routing");
});

await checkAsync("the viewer announces going and coming back", async () => {
    // toolLoader.js turns these two into the onHide()/onShow() every plugin
    // already implements, which is how ROI's document-level keys and pen stand
    // down while a page is over the image. Dropping either one leaves a hidden
    // tool eating input meant for the page on top of it.
    const app = boot({ fragments: { "/settings": "<div></div>" } });
    app.click({ href: "/settings" });
    await settle();
    assert.deepEqual(app.log.events, ["plexora:viewer-hidden"]);

    app.click({ href: "/demo" });
    await settle();
    assert.deepEqual(app.log.events, ["plexora:viewer-hidden", "plexora:viewer-shown"]);
});

await checkAsync("a link to a different project is left to the browser", async () => {
    const app = boot();
    const { defaultPrevented } = app.click({ href: "/tonsil" });
    await settle();
    assert.equal(defaultPrevented, false,
        "a project change was routed; the viewer cannot be swapped in place");
    assert.deepEqual(app.log.fetches, []);
    assert.equal(app.hidden(), false);
});

await checkAsync("a modified click is the browser's, not the router's", async () => {
    for (const modifier of ["metaKey", "ctrlKey", "shiftKey", "altKey"]) {
        const app = boot({ fragments: { "/settings": "<div></div>" } });
        const { defaultPrevented } = app.click({ href: "/settings" }, { [modifier]: true });
        await settle();
        assert.equal(defaultPrevented, false, `${modifier}+click was swallowed`);
        assert.deepEqual(app.log.fetches, [], `${modifier}+click still fetched`);
    }
});

await checkAsync("a Tools row, a download and a new tab are all left alone", async () => {
    const app = boot({ fragments: { "/settings": "<div></div>" } });
    assert.equal(app.click({ href: "/demo/tools/roi", "data-tool": "roi" }).defaultPrevented,
        false, "a Tools-menu row was routed instead of reaching toolLoader");
    assert.equal(app.click({ href: "/settings", download: "" }).defaultPrevented,
        false, "a download link was routed");
    assert.equal(app.click({ href: "/settings", target: "_blank" }).defaultPrevented,
        false, "a new-tab link was routed");
    assert.equal(app.click({ href: "#section" }).defaultPrevented,
        false, "an in-page anchor was routed");
    await settle();
    assert.deepEqual(app.log.fetches, []);
});

await checkAsync("a script the document already ran is not run again", async () => {
    const app = boot({
        // Already in <head>, as base.html puts it -- with a DIFFERENT
        // cache-busting tag, because the fragment and the shell are stamped
        // independently and the router must match on the path, not the URL.
        scripts: ["/client/src/js/views/columnClassifier.js?v=1"],
        fragments: {
            "/open_project": '<div id="project-results"></div>'
                + '<script src="/client/src/js/views/columnClassifier.js?v=2"></script>'
                + '<script src="/client/src/js/views/openProjectPage.js?v=1"></script>',
        },
    });
    const before = app.head.querySelectorAll("script").length;
    app.click({ href: "/open_project" });
    await settle();
    const added = app.head.querySelectorAll("script")
        .slice(before)
        .map((node) => new URL(node.src).pathname);
    assert.deepEqual(added, ["/client/src/js/views/openProjectPage.js"],
        "the already-loaded script was re-executed, which is a SyntaxError for "
        + "any file declaring a top-level class");
});

await checkAsync("the page's controllers are mounted, and the last page's dropped", async () => {
    const app = boot({ fragments: { "/settings": "<div></div>" } });
    app.click({ href: "/settings" });
    await settle();
    assert.equal(app.log.booted, 1, "the arriving page was never mounted");
    assert.equal(app.log.unmounted, 1, "the leaving page was never torn down");

    app.click({ href: "/demo" });
    await settle();
    assert.equal(app.log.unmounted, 2,
        "the page was left mounted behind the viewer, with its timers running");
});

await checkAsync("a fragment that will not load becomes a real navigation", async () => {
    const app = boot();   // no fragments registered, so every fetch 404s
    app.click({ href: "/settings" });
    await settle();
    assert.deepEqual(app.log.navigations.map((href) => new URL(href).pathname), ["/settings"],
        "a failed route left the user where they were");
    assert.deepEqual(app.log.pushes, [], "a failed route still rewrote the address bar");
});

await checkAsync("a redirect to another project is handed to the browser", async () => {
    const app = boot({
        fragments: { "/project/tonsil/columns": { redirectedTo: "/tonsil", html: "<div></div>" } },
    });
    app.click({ href: "/project/tonsil/columns" });
    await settle();
    assert.deepEqual(app.log.navigations.map((href) => new URL(href).pathname), ["/tonsil"]);
    assert.deepEqual(app.log.pushes, [], "the address bar was pushed for a page never shown");
});

await checkAsync("a redirect elsewhere pushes where the content came from", async () => {
    const app = boot({
        fragments: { "/project/demo/columns": { redirectedTo: "/edit_config/demo", html: "<div></div>" } },
    });
    app.click({ href: "/project/demo/columns" });
    await settle();
    assert.deepEqual(app.log.pushes, ["/edit_config/demo"],
        "the address bar disagreed with what is on screen");
});

await checkAsync("returning with ?tool= opens it, and only if it is shut", async () => {
    const app = boot({ fragments: { "/settings": "<div></div>" } });
    app.click({ href: "/settings" });
    await settle();
    app.click({ href: "/demo?tool=figure_builder" });
    await settle();
    assert.deepEqual(app.log.tools, ["figure_builder"], "the named tool was not opened");

    app.click({ href: "/settings" });
    await settle();
    app.click({ href: "/demo?tool=figure_builder" });
    await settle();
    assert.deepEqual(app.log.tools, ["figure_builder"],
        "an already-open tool was toggled, which closes it");
});

await checkAsync("a page's stylesheet is disabled on the way out, not removed", async () => {
    const app = boot({
        fragments: {
            "/open_project": '<link rel="stylesheet" href="/client/src/css/openProject.css">'
                + '<div id="project-results"></div>',
        },
    });
    app.click({ href: "/open_project" });
    await settle();
    const sheets = app.head.querySelectorAll("link");
    assert.equal(sheets.length, 1, "the fragment's stylesheet was not adopted");
    assert.equal(sheets[0].disabled, false);

    app.click({ href: "/demo" });
    await settle();
    assert.equal(app.head.querySelectorAll("link").length, 1,
        "the stylesheet was removed, so going back would refetch and flash");
    assert.equal(sheets[0].disabled, true,
        "the page's CSS was left applying over the viewer");
});

await checkAsync("back and forward route without pushing a new entry", async () => {
    const app = boot({ fragments: { "/settings": "<div></div>" } });
    app.click({ href: "/settings" });
    await settle();
    const pushes = app.log.pushes.length;

    app.document.body.dataset.ignored = "";      // no-op, keeps the shape honest
    app.popstate();
    await settle();
    assert.equal(app.log.pushes.length, pushes,
        "popstate pushed an entry, which is how a Back button stops working");
});

await checkAsync("a burst of Back presses lands where the last one asked", async () => {
    // Found by holding Back in a real browser. Each popstate arrives while the
    // previous fetch is still in flight, and a router that DROPS those leaves
    // the address bar on the slide with the Settings page still on screen --
    // the browser has already moved the URL, so ignoring the event is not
    // "doing nothing", it is going out of sync.
    const app = boot({
        fragments: {
            "/settings": "<div></div>",
            "/open_project": '<div id="project-results"></div>',
        },
    });
    app.click({ href: "/settings" });
    app.click({ href: "/open_project" });   // while the first is still fetching
    app.click({ href: "/demo" });           // and back to the slide
    await settle();
    assert.equal(app.hidden(), false,
        "the last destination was dropped: the viewer is still covered");
    assert.equal(app.pageHost.children.length, 0, "a page was left on screen");
    assert.equal(app.log.pushes[app.log.pushes.length - 1], "/demo",
        "the address bar disagrees with what is on screen");
});

await checkAsync("a document with no viewer stands down but still navigates", async () => {
    const app = boot({ datasource: "", path: "/open_project" });
    assert.equal(app.click({ href: "/settings" }).listening, false,
        "a page with nothing to preserve is listening for clicks anyway");
    assert.equal(app.router.canRoute("/settings"), false);
    app.router.go("/settings");
    assert.deepEqual(app.log.navigations, ["/settings"],
        "PlexoraRouter.go must always go somewhere, viewer or no viewer");
});

console.log(`\n${said.length} checks passed`);
