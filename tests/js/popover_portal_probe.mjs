/**
 * Where the viewer's floating popups live, and why it has to depend on
 * fullscreen.
 *
 * The channel rows open two popups: SearchableSelect's marker menu and
 * ColorSwatchPicker's palette. Both are portaled out of their row, because a
 * dimmed row has opacity < 1 and would trap them in its own stacking context.
 * <body> was the portal, and that is exactly wrong under the Fullscreen API
 * whenever something smaller than the document goes fullscreen: the API paints
 * an opaque ::backdrop over everything that is not the fullscreen element or a
 * descendant of it. A menu on <body> is then a sibling of the fullscreen
 * element -- open, positioned, clickable in the abstract, and painted
 * underneath the backdrop where no z-index reaches. The symptom is "clicking a
 * channel does nothing in fullscreen".
 *
 * The viewer's own button fullscreens the document element now, so that the
 * navbar stays visible, and that case has the opposite requirement: <body> is
 * INSIDE the fullscreen element, so the popups must stay on it rather than be
 * hoisted onto <html>. Both are pinned below.
 *
 * So this probe runs the real popoverPortal.js, searchableSelect.js and
 * colorSwatchPicker.js against a DOM stand-in that tracks parentage, and asks
 * the only question that matters: is the popup inside the element that is
 * fullscreen? The three moments are construction (the sidebar built while
 * already fullscreen), the toggle (the ordinary case -- rows exist first, the
 * user presses the button afterwards), and teardown.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const VIEWS = join(REPO, "plexora/client/src/js/views");

// base.html's order. popoverPortal.js first: the other two reach for it while
// constructing, so a probe that loaded them the other way round would report a
// ReferenceError the browser would also hit.
const WIDGETS = ["popoverPortal.js", "searchableSelect.js", "colorSwatchPicker.js"];

/** A DOM node that remembers who its parent is -- the whole point here. */
function makeNode() {
    const node = {
        style: { setProperty() {} },
        dataset: {}, hidden: false, value: "", innerHTML: "",
        className: "", title: "", placeholder: "", type: "", textContent: "",
        parentNode: null,
        children: [],
        classes: new Set(),
        handlers: {},
        classList: {
            add(...names) { names.forEach((n) => node.classes.add(n)); },
            remove(...names) { names.forEach((n) => node.classes.delete(n)); },
            toggle(name, on) {
                const want = on === undefined ? !node.classes.has(name) : !!on;
                if (want) node.classes.add(name); else node.classes.delete(name);
            },
            contains: (name) => node.classes.has(name),
        },
        setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
        addEventListener(type, fn) { (node.handlers[type] ||= []).push(fn); },
        removeEventListener() {},
        focus() {}, select() {},
        // A real rect, so a reparented menu can be shown to still position off
        // the viewport rather than off its new parent.
        getBoundingClientRect: () => ({ left: 12, top: 30, bottom: 48, right: 92, width: 80, height: 18 }),
        querySelector: () => null,
        querySelectorAll: () => [],
        contains(other) {
            for (let n = other; n; n = n.parentNode) if (n === node) return true;
            return false;
        },
        append(...nodes) { nodes.forEach((n) => node.appendChild(n)); },
        appendChild(child) {
            if (child.parentNode) child.parentNode.removeChild(child);
            child.parentNode = node;
            node.children.push(child);
            return child;
        },
        removeChild(child) {
            const at = node.children.indexOf(child);
            if (at >= 0) node.children.splice(at, 1);
            child.parentNode = null;
            return child;
        },
        remove() { if (node.parentNode) node.parentNode.removeChild(node); },
    };
    return node;
}

const documentHandlers = {};
/** <html>: what the viewer's full-screen button fullscreens, so that the
 *  navbar -- a sibling of the app shell, not a child of it -- stays on
 *  screen. It CONTAINS <body>, which is the case section 7 pins. */
const documentElement = makeNode();
const body = documentElement.appendChild(makeNode());
/** #bodyDiv: the app shell. Still fullscreened in this probe's sections 2-6,
 *  because the portal's guarantee has to hold for anything that fullscreens a
 *  subtree, whoever does it. */
const shell = body.appendChild(makeNode());
/** A channel row inside the shell -- what the widgets are mounted into. */
const row = shell.appendChild(makeNode());

const ctx = createContext({
    console, Math, Object, Array, Number, String, Boolean, JSON, Set, Map,
    Error, TypeError, Promise, Date,
    setTimeout: () => 1, clearTimeout: () => {},
    requestAnimationFrame: () => 1,
    document: {
        // Left undefined until a test sets it, which is what a browser reports
        // when nothing is fullscreen.
        fullscreenElement: null,
        body,
        activeElement: null,
        createElement: () => makeNode(),
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener(type, fn) { (documentHandlers[type] ||= []).push(fn); },
        removeEventListener() {},
    },
    window: {
        innerHeight: 900,
        addEventListener() {}, removeEventListener() {},
        setTimeout: () => 1, clearTimeout: () => {},
    },
});

for (const name of WIDGETS) {
    runInContext(readFileSync(join(VIEWS, name), "utf8"), ctx, { filename: name });
}

/** Enter or leave fullscreen the way the browser does: set the property, then
 *  fire the event. Both halves matter -- code that only read the property would
 *  never learn about the change. */
function setFullscreen(element) {
    ctx.document.fullscreenElement = element;
    (documentHandlers.fullscreenchange || []).forEach((fn) => fn());
}

/** Would a viewer actually see this element? Only the fullscreen element and
 *  its descendants are painted above the backdrop. */
function isPainted(el) {
    const top = ctx.document.fullscreenElement;
    return !top || top === el || top.contains(el);
}

function newSelect() {
    return runInContext(
        `new SearchableSelect(__mount, { options: ["DAPI", "CD3", "CD8"], trigger: "button" })`,
        Object.assign(ctx, { __mount: row.appendChild(makeNode()) }));
}

function newPicker() {
    return runInContext(
        `new ColorSwatchPicker(__mount, { value: "#2388ff" })`,
        Object.assign(ctx, { __mount: row.appendChild(makeNode()) }));
}

// ---------------------------------------------------------------------------
// 1. Nothing fullscreen: <body> is still the portal, unchanged.
// ---------------------------------------------------------------------------
const select = newSelect();
const picker = newPicker();
assert.equal(select.menu.parentNode, body,
    "with nothing fullscreen the menu should still portal onto <body>");
assert.equal(picker.popover.parentNode, body,
    "with nothing fullscreen the palette should still portal onto <body>");
console.log("ok - outside fullscreen the popups portal onto <body> as before");

// ---------------------------------------------------------------------------
// 2. The reported bug: rows built first, fullscreen pressed afterwards. This is
//    the ordinary path -- the sidebar is long since rendered by then.
// ---------------------------------------------------------------------------
assert.ok(isPainted(select.menu), "sanity: painted outside fullscreen");
setFullscreen(shell);
assert.equal(select.menu.parentNode, shell,
    "entering fullscreen must move an existing menu into the fullscreen element");
assert.equal(picker.popover.parentNode, shell,
    "entering fullscreen must move an existing palette into the fullscreen element");
assert.ok(isPainted(select.menu),
    "the menu must be inside the fullscreen element, or it opens under the backdrop");
assert.ok(isPainted(picker.popover),
    "the palette must be inside the fullscreen element, or it opens under the backdrop");
console.log("ok - entering fullscreen moves already-built popups inside the fullscreen element");

// ---------------------------------------------------------------------------
// 3. A widget built while already fullscreen (a panel opened from the Tools
//    menu, a legend that rebuilds its rows) portals to the right place first
//    time, without waiting for another toggle.
// ---------------------------------------------------------------------------
const lateSelect = newSelect();
assert.equal(lateSelect.menu.parentNode, shell,
    "a menu built during fullscreen must portal into the fullscreen element");
console.log("ok - a popup built during fullscreen lands inside it immediately");

// ---------------------------------------------------------------------------
// 4. Reparenting must not disturb positioning: these popups are position:fixed
//    with inline viewport coordinates, so the new parent changes nothing.
// ---------------------------------------------------------------------------
select.open(true);
assert.equal(select.menu.style.left, "12px",
    "the menu still positions off the viewport rect, not off its new parent");
assert.equal(select.menu.style.top, "52px", "…and still hangs 4px below the trigger");
console.log("ok - a reparented menu still positions in viewport coordinates");

// ---------------------------------------------------------------------------
// 5. Leaving fullscreen hands everything back to <body>, or the popups would be
//    stranded inside a shell that no longer needs to hold them.
// ---------------------------------------------------------------------------
setFullscreen(null);
assert.equal(select.menu.parentNode, body,
    "leaving fullscreen must return the menu to <body>");
assert.equal(lateSelect.menu.parentNode, body,
    "leaving fullscreen must return a late-built menu to <body> too");
console.log("ok - leaving fullscreen returns the popups to <body>");

// ---------------------------------------------------------------------------
// 6. destroy() must leave the portal's registry, not just the DOM. A portal
//    that kept holding a destroyed element would appendChild it back on the
//    next toggle -- an orphan menu, reachable and stale, put on the page by the
//    very code meant to be cleaning up after it.
// ---------------------------------------------------------------------------
const doomed = newSelect();
const orphan = doomed.menu;
doomed.destroy();
assert.equal(orphan.parentNode, null, "destroy() takes the menu off the page");
setFullscreen(shell);
assert.equal(orphan.parentNode, null,
    "a destroyed menu must not be resurrected by a later fullscreen change");
const doomedPicker = newPicker();
const orphanPalette = doomedPicker.popover;
doomedPicker.destroy();
setFullscreen(null);
assert.equal(orphanPalette.parentNode, null,
    "a destroyed palette must not be resurrected by a later fullscreen change");
console.log("ok - a destroyed popup leaves the portal and is not re-attached");

// ---------------------------------------------------------------------------
// 7. The viewer's own full-screen button: <html> goes fullscreen so the navbar
//    survives, and <body> is then a descendant rather than a sibling. Nothing
//    is under the backdrop, so the popups must stay where the rest of the app
//    expects them instead of being hoisted onto <html>.
// ---------------------------------------------------------------------------
const wholePageSelect = newSelect();
setFullscreen(documentElement);
assert.equal(wholePageSelect.menu.parentNode, body,
    "with the whole document fullscreen the menu belongs on <body>, not <html>");
assert.equal(select.menu.parentNode, body,
    "an already-built menu is not hoisted out of <body> either");
assert.ok(isPainted(wholePageSelect.menu),
    "a menu on <body> is inside a fullscreened <html>, so it is painted");
const lateWholePage = newPicker();
assert.equal(lateWholePage.popover.parentNode, body,
    "a palette built while the document is fullscreen also lands on <body>");
setFullscreen(null);
console.log("ok - with the document element fullscreen the popups stay on <body>");
