/**
 * The ROI tools bar on the image: roiToolbar.js, driven the way the panel's
 * controller drives it, with the REAL RoiInteraction behind it.
 *
 * What is pinned is what fails quietly -- a bar that renders and is wrong:
 *
 *   - built only when first shown, over the image, gone again when hidden;
 *   - a tool button reporting its tool, the tool in hand lit, and the ones
 *     that cannot be picked disabled -- but never the one in hand, or the wand
 *     could not be clicked to fold its options away;
 *   - the wand opening magic select's options out of the bar, and a second
 *     click folding them back, to whatever was in hand before (nothing, on a
 *     fresh panel); folded away, they are inert, out of the tab order;
 *   - a mode on the bar becoming magic select's mode, and busy on the wand;
 *   - Through planes offered only on a z-stack, to an owner that can carry;
 *   - options opened before the bar is on screen (a panel shown again with
 *     magic select in hand) out from the first frame.
 *
 * A DOM no wider than what roiToolbar.js touches; layout is not measured here
 * (every rect is empty, so `anchor` stands down) -- that is the browser's.
 *
 * Run directly: `node tests/js/roi_toolbar_probe.mjs`
 * Prints `ok  <check>` per check held; exit 1 if any did not.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/roi/static");

class FakeElement {
    constructor(tag) {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.className = "";
        this.id = "";
        this.dataset = {};
        this.attributes = new Map();
        this.listeners = new Map();
        this.hidden = false;
        this.disabled = false;
        this.inert = false;
        this.title = "";
        this.type = "";
        this.style = { setProperty: (key, value) => { this.style[key] = value; } };
        const element = this;
        this.classList = {
            names: () => element.className.split(/\s+/).filter(Boolean),
            contains: (name) => element.classList.names().includes(name),
            add: (name) => {
                if (!element.classList.contains(name)) element.className += ` ${name}`;
            },
            remove: (name) => {
                element.className = element.classList.names().filter((n) => n !== name).join(" ");
            },
            toggle: (name, on = !element.classList.contains(name)) => {
                if (on) element.classList.add(name);
                else element.classList.remove(name);
                return on;
            },
        };
    }

    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }

    appendChild(child) {
        child.remove();
        child.parentNode = this;
        this.children.push(child);
        return child;
    }

    remove() {
        if (!this.parentNode) return;
        this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
        this.parentNode = null;
    }

    addEventListener(name, fn) {
        if (!this.listeners.has(name)) this.listeners.set(name, []);
        this.listeners.get(name).push(fn);
    }

    /** A click as the browser delivers it: the target first, then up. */
    click() {
        if (this.disabled) return;
        const event = { type: "click", target: this, stopPropagation: () => { event.stopped = true; } };
        for (let node = this; node && !event.stopped; node = node.parentNode) {
            for (const fn of node.listeners.get("click") || []) fn(event);
        }
    }

    getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; }

    find(test) {
        if (test(this)) return this;
        for (const child of this.children) {
            const hit = child.find(test);
            if (hit) return hit;
        }
        return null;
    }
}

const wrapper = new FakeElement("div");
wrapper.id = "openseadragon_wrapper";

const window = {
    addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
    PlexoraToolLoader: null,
};
const document = {
    activeElement: null,
    createElement: (tag) => new FakeElement(tag),
    getElementById: (id) => wrapper.find((node) => node.id === id),
    querySelector: () => null,
    addEventListener() {}, removeEventListener() {},
};
const ctx = createContext({
    Math, Object, Array, Number, String, Boolean, JSON, Set, Map, Date, Infinity,
    console, Promise, RegExp,
    setTimeout: () => 1, clearTimeout: () => {},
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {},
    document, window,
});
runInContext(readFileSync(join(STATIC, "roiGeometry.js"), "utf8"), ctx);
runInContext(readFileSync(join(STATIC, "roiTools.js"), "utf8")
             + "\n;globalThis.__Tools = RoiInteraction;", ctx, { filename: "roiTools.js" });
runInContext(readFileSync(join(STATIC, "roiToolbar.js"), "utf8")
             + "\n;globalThis.__Toolbar = RoiToolbar;", ctx, { filename: "roiToolbar.js" });

const failures = [];
function check(name, fn) {
    try {
        fn();
        console.log(`ok  ${name}`);
    } catch (error) {
        failures.push(name);
        console.log(`FAIL  ${name}\n      ${String(error && error.message || error).split("\n").join("\n      ")}`);
    }
}

/** The panel's wiring (RoiSidebarController's constructor), on a fresh tool. */
function panel() {
    const store = {
        editable: true, selected: null, selectionId: null,
        activeCategory: { id: "c" },
        changed() {}, feature: () => null,
    };
    const renderer = { draft: null, schedule() {}, invalidate() {}, setEnabled() {} };
    const tools = new ctx.__Tools({ viewer: { viewer: null }, config: { width: 100, height: 100 } },
                                  store, renderer);
    tools.armed = true;
    const picked = [];
    const toolbar = new ctx.__Toolbar({
        onTool: (tool) => {
            picked.push(tool);
            if (tool === "magic") tools.toggleMagic();
            else tools.setTool(tool);
        },
    });
    tools.magicBars = toolbar.magicBars();
    tools.onBusy = (busy) => toolbar.setBusy(busy);
    return { tools, toolbar, picked };
}

const button = (toolbar, tool) => toolbar.buttons[tool];
const pressed = (toolbar) => Object.keys(toolbar.buttons)
    .filter((tool) => button(toolbar, tool).getAttribute("aria-pressed") === "true");

check("the bar is built when first shown, over the image, with five tools and none lit", () => {
    const { toolbar } = panel();
    assert.equal(toolbar.root, null);
    toolbar.show();
    assert.equal(toolbar.root.parentNode, wrapper);
    assert.equal(toolbar.root.id, "roi_toolbar");
    assert.deepEqual(Object.keys(toolbar.buttons), ["select", "polygon", "freehand", "rectangle", "magic"]);
    assert.deepEqual(Object.values(toolbar.buttons).map((b) => b.id),
        ["roi_tool_select", "roi_tool_polygon", "roi_tool_freehand", "roi_tool_rectangle",
         "roi_tool_magic"]);
    assert.deepEqual(pressed(toolbar), []);
    toolbar.hide();
});

check("hidden, it leaves the image", () => {
    const { toolbar } = panel();
    toolbar.show();
    toolbar.hide();
    assert.equal(toolbar.root.parentNode, null);
    assert.equal(wrapper.children.length, 0);
});

check("a tool button picks its tool, and the bar lights it", () => {
    const { tools, toolbar, picked } = panel();
    toolbar.show();
    button(toolbar, "polygon").click();
    assert.deepEqual(picked, ["polygon"]);
    assert.equal(tools.tool, "polygon");
    toolbar.render(tools.tool);
    assert.deepEqual(pressed(toolbar), ["polygon"]);
    assert.equal(button(toolbar, "polygon").classList.contains("is-active"), true);
    toolbar.hide();
});

check("what cannot be picked is disabled, but never the tool in hand", () => {
    const { toolbar } = panel();
    toolbar.show();
    toolbar.render("magic", () => true);
    assert.equal(button(toolbar, "magic").disabled, false);
    for (const tool of ["select", "polygon", "freehand", "rectangle"]) {
        assert.equal(button(toolbar, tool).disabled, true, tool);
    }
    toolbar.render(null, (tool) => tool !== "select");
    assert.equal(button(toolbar, "select").disabled, false);
    assert.equal(button(toolbar, "magic").disabled, true);
    toolbar.hide();
});

check("the wand opens magic select's options out of the bar, in Box", () => {
    const { tools, toolbar } = panel();
    toolbar.show();
    assert.equal(toolbar.drawer.inert, true);
    button(toolbar, "magic").click();
    assert.equal(tools.tool, "magic");
    assert.equal(toolbar.root.classList.contains("is-magic"), true);
    assert.equal(toolbar.drawer.inert, false);
    assert.equal(toolbar.drawer.getAttribute("aria-hidden"), "false");
    assert.equal(button(toolbar, "magic").getAttribute("aria-expanded"), "true");
    assert.equal(toolbar.modeButtons.box.getAttribute("aria-checked"), "true");
    assert.equal(tools.magicBar.isShown(), true);
    toolbar.hide();
});

check("a mode on the bar is magic select's mode", () => {
    const { tools, toolbar } = panel();
    toolbar.show();
    button(toolbar, "magic").click();
    toolbar.modeButtons.add.click();
    assert.equal(tools.magicMode, "add");
    assert.equal(toolbar.modeButtons.add.getAttribute("aria-checked"), "true");
    assert.equal(toolbar.modeButtons.box.getAttribute("aria-checked"), "false");
    toolbar.hide();
});

check("the wand clicked again folds them away, back to no tool on a fresh panel", () => {
    const { tools, toolbar } = panel();
    toolbar.show();
    button(toolbar, "magic").click();
    button(toolbar, "magic").click();
    assert.equal(tools.tool, null);
    assert.equal(toolbar.root.classList.contains("is-magic"), false);
    assert.equal(toolbar.drawer.inert, true);
    assert.equal(toolbar.drawer.getAttribute("aria-hidden"), "true");
    assert.equal(button(toolbar, "magic").getAttribute("aria-expanded"), "false");
    toolbar.hide();
});

check("...and back to the tool that was in hand before it", () => {
    const { tools, toolbar } = panel();
    toolbar.show();
    button(toolbar, "rectangle").click();
    button(toolbar, "magic").click();
    button(toolbar, "magic").click();
    assert.equal(tools.tool, "rectangle");
    assert.equal(toolbar.open, false);
    toolbar.hide();
});

check("another tool picked while they are out folds them too", () => {
    const { tools, toolbar } = panel();
    toolbar.show();
    button(toolbar, "magic").click();
    button(toolbar, "freehand").click();
    assert.equal(tools.tool, "freehand");
    assert.equal(toolbar.open, false);
    toolbar.hide();
});

check("the wand breathes while an outline is on its way", () => {
    const { tools, toolbar } = panel();
    toolbar.show();
    tools.setMagicBusy(true);
    assert.equal(button(toolbar, "magic").getAttribute("aria-busy"), "true");
    tools.setMagicBusy(false);
    assert.equal(button(toolbar, "magic").getAttribute("aria-busy"), "false");
    toolbar.hide();
});

check("Through planes is offered on a z-stack, with Plexora 3D, and only there", () => {
    const { tools, toolbar } = panel();
    toolbar.show();
    window.PlexoraPro = { allows: () => true };
    window.__plexora = { viewState: { enabled: () => false, depth: 1 } };
    button(toolbar, "magic").click();
    assert.equal(toolbar.carry.hidden, true);
    button(toolbar, "magic").click();
    window.__plexora = { viewState: { enabled: () => true, depth: 12 } };
    button(toolbar, "magic").click();
    assert.equal(toolbar.carry.hidden, false);
    assert.equal(toolbar.carry.disabled, false);
    window.PlexoraPro = { allows: () => false };
    tools.setTool(null);
    button(toolbar, "magic").click();
    assert.equal(toolbar.carry.hidden, true);
    delete window.PlexoraPro;
    delete window.__plexora;
    toolbar.hide();
});

check("disarming the tools folds the options", () => {
    const { tools, toolbar } = panel();
    toolbar.show();
    button(toolbar, "magic").click();
    tools.hideMagicBar();
    assert.equal(toolbar.open, false);
    toolbar.hide();
});

check("options opened before the bar is on screen are out from its first frame", () => {
    const { tools, toolbar } = panel();
    tools.setTool("magic");
    assert.equal(toolbar.open, true);
    toolbar.show();
    assert.equal(toolbar.root.classList.contains("is-magic"), true);
    assert.equal(toolbar.drawer.inert, false);
    toolbar.hide();
});

if (failures.length) {
    console.log(`\n${failures.length} check(s) failed`);
    process.exit(1);
}
