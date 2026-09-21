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
 * A MODAL <dialog> is the same failure wearing different clothes, and it
 * shipped: `showModal()` promotes the dialog to the TOP LAYER, painted above
 * the whole ordinary document, so a menu on <body> opens underneath it however
 * high its z-index goes. That was "the gene dropdown in Create gene groups
 * opens behind the dialog". Section 8 pins it, including the part no event can
 * tell the portal about -- a dialog OPENING -- which is why a popup asks on its
 * way up (`PopoverPortal.reseat`) rather than waiting to be told.
 *
 * So this probe runs the real popoverPortal.js, searchableSelect.js and
 * colorSwatchPicker.js against a DOM stand-in that tracks parentage, and asks
 * the only question that matters: is the popup inside the element that is
 * painted on top? The moments are construction (the sidebar built while
 * already fullscreen), the toggle (the ordinary case -- rows exist first, the
 * user presses the button afterwards), a modal dialog opening and closing over
 * the top of all of it, and teardown.
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
        // Only a <dialog> ever answers this, and only about `:modal` -- which
        // is the one distinction the portal draws (see modalOnTop).
        matches: (selector) => selector === ":modal" && node.isModal === true,
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
/** Every <dialog> currently showing, oldest first -- the top layer's own
 *  order, which is what `modalOnTop` reads off the end of. */
const openDialogs = [];
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
        // The portal asks for exactly one selector. Anything else is a probe
        // that has drifted from the code it is standing in for, so it says so
        // rather than quietly returning nothing.
        querySelectorAll(selector) {
            assert.equal(selector, "dialog[open]",
                "the portal is expected to query only for open dialogs");
            return openDialogs.slice();
        },
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

// ---------------------------------------------------------------------------
// 8. A modal <dialog> over the top of everything. `showModal()` puts it in the
//    TOP LAYER, which is painted above the whole ordinary document -- so a menu
//    left on <body> is drawn underneath it and no z-index reaches. The dialog
//    has to host the popups for as long as it is up.
//
//    Nothing fires when a dialog OPENS, which is why the move happens on the
//    popup's way up rather than on an event. Closing does fire, and the portal
//    listens for it in the capture phase because `close` does not bubble.
// ---------------------------------------------------------------------------
/** What `<dialog>.showModal()` does, as far as anything here can observe it. */
function showModal(dialog) {
    dialog.isModal = true;
    openDialogs.push(dialog);
    body.appendChild(dialog);
}

/** …and `close()`, event included: without the event the portal never learns. */
function closeDialog(dialog) {
    dialog.isModal = false;
    openDialogs.splice(openDialogs.indexOf(dialog), 1);
    (documentHandlers.close || []).forEach((fn) => fn({ target: dialog }));
}

const dialog = makeNode();
const inDialog = newSelect();
assert.equal(inDialog.menu.parentNode, body,
    "sanity: built before the dialog, the menu starts on <body>");

showModal(dialog);
inDialog.open(true);
assert.equal(inDialog.menu.parentNode, dialog,
    "a menu opened while a modal dialog is up must be hosted BY the dialog, "
    + "or it is drawn under the top layer");
assert.equal(inDialog.menu.style.left, "12px",
    "…and still positions off the viewport rect, exactly as in section 4");

const bornInDialog = newPicker();
assert.equal(bornInDialog.popover.parentNode, dialog,
    "a palette CONSTRUCTED while the dialog is up lands inside it immediately");

closeDialog(dialog);
assert.equal(inDialog.menu.parentNode, body,
    "closing the dialog hands the menu back to <body>");
assert.equal(bornInDialog.popover.parentNode, body,
    "…and the palette too, or it is stranded inside an element nothing draws");
console.log("ok - a modal dialog hosts the popups while it is open and releases them on close");

// A modal dialog outranks fullscreen: both are in the top layer, and the
// dialog entered it last. A menu hoisted into a fullscreened subtree instead
// would be under the dialog it was opened from.
setFullscreen(shell);
const overFullscreen = makeNode();
showModal(overFullscreen);
const overSelect = newSelect();
assert.equal(overSelect.menu.parentNode, overFullscreen,
    "with both a fullscreen element and a modal dialog, the dialog wins");
closeDialog(overFullscreen);
assert.equal(overSelect.menu.parentNode, shell,
    "and closing it falls back to the fullscreen element, not to <body>");
setFullscreen(null);
console.log("ok - a modal dialog outranks a fullscreen element, and falls back to it on close");
