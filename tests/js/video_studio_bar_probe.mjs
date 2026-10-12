/**
 * Video Studio's Quick Capture bar, its parameter controls and its keys, on a
 * small DOM stand-in.
 *
 *     node tests/js/video_studio_bar_probe.mjs
 *
 * What it holds:
 *   - the bar docks through the canvas layout (top-center, furniture,
 *     collapsible) and is a labelled toolbar whose buttons all have names;
 *   - "Hide settings" folds the strip (hidden, aria-expanded) and a canvas
 *     collapse folds the bar to its compact form;
 *   - the scrub rail is a slider whose value follows the time;
 *   - the parameter controls come from the server's description: a segmented
 *     control is a radiogroup that reports its choice, a number clamps to its
 *     bounds, a toggle is a switch, a view field captures and clears;
 *   - every class that sets `display` and can be hidden has a `[hidden]`
 *     partner rule (display beats the attribute otherwise);
 *   - keys: Space plays only while Video Studio is the active tool, never
 *     while typing or with a dialog open; Escape pauses first, then leaves
 *     the preview.
 *
 * Reports {checked, failures} as JSON on stderr.
 */
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STATIC = join(REPO, "plexora/plugins/video_studio/static");

const failures = [];
let checked = 0;
function check(name, condition, detail) {
    checked += 1;
    if (!condition) failures.push(detail === undefined ? { name } : { name, detail });
}

// -- a small DOM ----------------------------------------------------------------

class Element {
    constructor(tag) {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.parentElement = null;
        this.attributes = {};
        this.listeners = {};
        this.style = { setProperty(k, v) { this[k] = v; } };
        this._text = "";
        this.disabled = false;
        this.tabIndex = 0;
        const self = this;
        this.dataset = new Proxy({}, {
            get: (_, k) => self.attributes[`data-${String(k).replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`],
            set: (_, k, v) => { self.attributes[`data-${String(k)}`] = String(v); return true; },
        });
        this.classList = {
            add: (...c) => { const s = new Set(this.className.split(" ").filter(Boolean)); c.forEach((x) => s.add(x)); this.className = [...s].join(" "); },
            remove: (...c) => { this.className = this.className.split(" ").filter((x) => x && !c.includes(x)).join(" "); },
            toggle: (c, on) => { const has = this.classList.contains(c); const want = on === undefined ? !has : Boolean(on); if (want && !has) this.classList.add(c); if (!want && has) this.classList.remove(c); return want; },
            contains: (c) => this.className.split(" ").includes(c),
        };
    }
    get className() { return this.attributes.class || ""; }
    set className(v) { this.attributes.class = v; }
    get hidden() { return "hidden" in this.attributes; }
    set hidden(v) { if (v) this.attributes.hidden = ""; else delete this.attributes.hidden; }
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
    set textContent(v) { this._text = String(v); this.children = []; }
    set innerHTML(v) { this._text = ""; this.children = []; }
    get firstElementChild() { return this.children[0] || null; }
    get firstChild() { return this.children[0] || null; }
    setAttribute(k, v) { this.attributes[k] = String(v); }
    getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
    hasAttribute(k) { return k in this.attributes; }
    removeAttribute(k) { delete this.attributes[k]; }
    appendChild(c) { c.remove?.(); c.parentElement = this; this.children.push(c); return c; }
    append(...cs) { cs.forEach((c) => this.appendChild(c)); }
    prepend(c) { c.parentElement = this; this.children.unshift(c); }
    replaceChildren(...cs) { this.children.forEach((c) => { c.parentElement = null; }); this.children = []; this._text = ""; this.append(...cs); }
    remove() { if (!this.parentElement) return; const p = this.parentElement; p.children = p.children.filter((c) => c !== this); this.parentElement = null; }
    replaceWith(n) { const p = this.parentElement; if (!p) return; const i = p.children.indexOf(this); n.remove?.(); n.parentElement = p; p.children[i] = n; this.parentElement = null; }
    addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
    removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn); }
    dispatchEvent(e) { e.target ||= this; for (const fn of this.listeners[e.type] || []) fn(e); return true; }
    click() { this.dispatchEvent({ type: "click", preventDefault() {}, stopPropagation() {} }); }
    focus() { documentStub.activeElement = this; }
    getBoundingClientRect() { return { left: 0, top: 0, width: 200, height: 10 }; }
    setPointerCapture() {}
    releasePointerCapture() {}
    closest() { return null; }
    *walk() { yield this; for (const c of this.children) yield* c.walk(); }
    find(pred) { for (const n of this.walk()) if (pred(n)) return n; return null; }
    findAll(pred) { return [...this.walk()].filter(pred); }
    querySelector(sel) { return this.find((n) => matches(n, sel)); }
    querySelectorAll(sel) { return this.findAll((n) => matches(n, sel)); }
}

function matches(node, sel) {
    if (sel.startsWith(".")) return node.classList.contains(sel.slice(1));
    const attr = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(sel);
    if (attr) return attr[1] in node.attributes && (attr[2] === undefined || node.attributes[attr[1]] === attr[2]);
    return node.tagName === sel.toUpperCase();
}

const documentStub = {
    activeElement: null,
    body: new Element("body"),
    createElement: (tag) => new Element(tag),
    createElementNS: (_ns, tag) => new Element(tag),
    querySelector: () => null,
    querySelectorAll: () => [],
};

const context = createContext({ console, Math, JSON, Promise, setTimeout, clearTimeout, Array, Object, Number, String,
                                document: documentStub, localStorage: { getItem: () => null, setItem() {} } });
context.window = context;
context.globalThis = context;
for (const file of ["videoParamFields.js", "videoStudioBar.js"]) {
    runInContext(readFileSync(join(STATIC, file), "utf8"), context, { filename: file });
}

// -- the bar ---------------------------------------------------------------------
const events = [];
const bar = new context.PlexoraVideoStudioBar({
    onPlay: () => events.push("play"), onExport: () => events.push("export"), onClose: () => events.push("close"),
    onSeek: (t) => events.push(["seek", t]),
});
const wrapper = new Element("div");
const root = bar.mount(wrapper);
check("the bar docks top-center", root.getAttribute("data-canvas-dock") === "top-center");
check("…as viewer furniture", root.hasAttribute("data-viewer-furniture"));
check("…that may fold", root.hasAttribute("data-canvas-collapsible"));
const toolbar = root.find((n) => n.getAttribute("role") === "toolbar");
check("the pill is a labelled toolbar", toolbar && toolbar.getAttribute("aria-label") === "Video Studio");
const unnamed = root.findAll((n) => n.tagName === "BUTTON" && !n.getAttribute("aria-label") && !n.textContent.trim());
check("every button has a name", unnamed.length === 0, unnamed.map((n) => n.className));
bar.playButton.click();
bar.exportButton.click();
bar.closeButton.click();
check("the buttons report", JSON.stringify(events.slice(0, 3)) === JSON.stringify(["play", "export", "close"]), events);
bar.setPlaying(true);
check("playing swaps the icon and the name", bar.playButton.getAttribute("aria-label") === "Pause"
      && bar.playButton.firstElementChild.className.includes("fa-pause"));

bar.foldButton.click();
check("Hide settings folds the strip", bar.strip.hidden && bar.foldButton.getAttribute("aria-expanded") === "false");
bar.foldButton.click();
check("…and shows it again", !bar.strip.hidden);
root.dispatchEvent({ type: "plexora:canvas-collapse", detail: { collapsed: true } });
check("a canvas collapse makes the bar compact", root.classList.contains("is-compact") && bar.strip.hidden);
root.dispatchEvent({ type: "plexora:canvas-collapse", detail: { collapsed: false } });

bar.setTime(2.5, 10);
check("the rail is a slider at the time", bar.scrub.getAttribute("role") === "slider"
      && bar.scrub.getAttribute("aria-valuenow") === "2.50" && bar.scrub.getAttribute("aria-valuemax") === "10.00");
check("the time reads m:ss.s", bar.timeLabel.textContent === "0:02.5 / 0:10.0", bar.timeLabel.textContent);

// -- the parameter controls ----------------------------------------------------------
const changes = [];
const fields = [
    { name: "axis", label: "Axis", kind: "segmented", default: "y",
      options: [{ value: "x", label: "X" }, { value: "y", label: "Y" }] },
    { name: "duration_s", label: "Length", kind: "number", default: 8, min: 0.5, max: 60, unit: "s" },
    { name: "loop", label: "Loop", kind: "toggle", default: true },
    { name: "end_view", label: "End view", kind: "view", default: null },
];
const box = new Element("div");
const controls = context.PlexoraVideoParamFields.render(box, fields, {}, {
    onChange: (name, value) => changes.push([name, value]),
    captureView: () => ({ viewport: { cx: 1, cy: 2, w: 3 } }),
});
const group = box.find((n) => n.getAttribute("role") === "radiogroup");
check("a segmented control is a radiogroup", group && group.children.length === 2);
group.children[0].click();
check("…that reports its choice", JSON.stringify(changes.at(-1)) === JSON.stringify(["axis", "x"]), changes);
check("…and checks it", group.children[0].getAttribute("aria-checked") === "true"
      && group.children[1].getAttribute("aria-checked") === "false");
const input = box.find((n) => n.tagName === "INPUT");
input.value = "500";
input.dispatchEvent({ type: "change" });
check("a number clamps to its bounds", JSON.stringify(changes.at(-1)) === JSON.stringify(["duration_s", 60])
      && input.value === "60", changes.at(-1));
const sw = box.find((n) => n.getAttribute("role") === "switch");
sw.click();
check("a toggle is a switch", JSON.stringify(changes.at(-1)) === JSON.stringify(["loop", false])
      && sw.getAttribute("aria-checked") === "false");
const setView = box.find((n) => n.tagName === "BUTTON" && n.textContent.startsWith("Set"));
setView.click();
check("a view field captures the view", changes.at(-1)[0] === "end_view" && changes.at(-1)[1].viewport.w === 3);
check("…and offers to clear it", setView.textContent === "End view set");
check("controls.values() reflects the edits", controls.values().axis === "x" && controls.values().loop === false);

// -- [hidden] partners ------------------------------------------------------------------
const css = readFileSync(join(STATIC, "video_studio.css"), "utf8");
for (const scope of [".vs-dock [hidden]", ".vs-dialog [hidden]", ".vs-render-cover [hidden]", ".vs-timeline [hidden]"]) {
    check(`${scope} beats display`, css.includes(scope) && /\[hidden\][^{]*\{\s*display:\s*none\s*!important/.test(css));
}

// -- keys -------------------------------------------------------------------------------
runInContext(`
    window.Plexora = { registerPlugin() {} };
`, context);
runInContext(readFileSync(join(STATIC, "videoStudioSidebarController.js"), "utf8"), context,
             { filename: "videoStudioSidebarController.js" });
const handleKey = context.PlexoraVideoStudioController.prototype.handleKey;
const log = [];
const fake = {
    exporting: false,
    project: { output: { fps: 30 }, duration_s: 4 },
    preview: { playing: true, active: true, loop: true, pause: () => log.push("pause"), end: () => log.push("end"),
               seek: (t) => log.push(["seek", t]), step: (d) => log.push(["step", d]) },
    togglePlay: () => log.push("toggle"),
    bar: { setLoop() {} },
    exportNow: () => log.push("export"),
    pathEditor: null,
    timeline: null,
};
const key = (k, extra = {}) => ({ key: k, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false,
                                  preventDefault() {}, stopImmediatePropagation() {}, ...extra });
context.PlexoraToolLoader = { activeTool: () => "video_studio" };
context.PlexoraShortcuts = { isTyping: () => false };
handleKey.call(fake, key(" "));
check("Space plays while Video Studio is active", log.at(-1) === "toggle", log);
context.PlexoraToolLoader = { activeTool: () => "roi" };
log.length = 0;
handleKey.call(fake, key(" "));
check("…and not while another tool is", log.length === 0, log);
context.PlexoraToolLoader = { activeTool: () => "video_studio" };
context.PlexoraShortcuts = { isTyping: () => true };
handleKey.call(fake, key(" "));
check("…nor while typing", log.length === 0, log);
context.PlexoraShortcuts = { isTyping: () => false };
documentStub.querySelector = (sel) => (sel === "dialog[open]" ? {} : null);
handleKey.call(fake, key(" "));
check("…nor with a dialog open", log.length === 0, log);
documentStub.querySelector = () => null;
handleKey.call(fake, key("Escape"));
check("Escape pauses first", log.at(-1) === "pause", log);
fake.preview.playing = false;
handleKey.call(fake, key("Escape"));
check("…then leaves the preview", log.at(-1) === "end", log);
handleKey.call(fake, key("ArrowRight", { shiftKey: true }));
check("Shift-→ steps a second", JSON.stringify(log.at(-1)) === JSON.stringify(["step", 1]), log.at(-1));
handleKey.call(fake, key("ArrowLeft"));
check("← steps one frame back", Math.abs(log.at(-1)[1] + 1 / 30) < 1e-9, log.at(-1));
handleKey.call(fake, key("e"));
check("E exports", log.at(-1) === "export");
handleKey.call(fake, key("z", { metaKey: true }));
check("⌘Z is left alone outside the editors", log.at(-1) === "export", log.at(-1));

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.stdout.write(failures.length ? `${failures.length} of ${checked} checks failed\n`
                                     : `all ${checked} checks passed\n`);
process.exit(failures.length ? 1 : 0);
