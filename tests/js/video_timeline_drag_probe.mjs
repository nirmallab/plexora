/**
 * Video Studio's timeline: dragging, Alt-copying and clicking a keyframe
 * diamond, with pointer events delivered the way a browser delivers them.
 *
 *     node tests/js/video_timeline_drag_probe.mjs
 *
 * The small DOM here routes each pointer event as the Pointer Events spec
 * does: to the element holding pointer capture while that element is still
 * in the document, otherwise to the element under the pointer; then it
 * bubbles up through the ancestors to the document and the window. Capture is
 * released implicitly when its element leaves the document, and after a
 * pointerup.
 *
 * What it holds:
 *   - dragging a diamond 100 px to the right sends one update_keyframe that
 *     moves it 1 s later;
 *   - Alt-dragging it sends one add_keyframe at the new time, with the
 *     original's value and pace;
 *   - clicking a diamond without moving seeks the preview to its time.
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

// -- a small DOM with pointer routing -------------------------------------------------

const pointer = { capture: null };

class Element {
    constructor(tag) {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.parentElement = null;
        this.attributes = {};
        this.listeners = {};
        this.style = { setProperty(k, v) { this[k] = v; } };
        this._text = "";
        this.clientWidth = 1132;     // lanes 1000 px wide: 100 px per second for 10 s
        const self = this;
        this.dataset = new Proxy({}, {
            get: (_, k) => self.attributes[`data-${String(k)}`],
            set: (_, k, v) => { self.attributes[`data-${String(k)}`] = String(v); return true; },
        });
        this.classList = { contains: (c) => this.className.split(" ").includes(c) };
    }
    get className() { return this.attributes.class || ""; }
    set className(v) { this.attributes.class = v; }
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
    set textContent(v) { this._text = String(v); this.children = []; }
    setAttribute(k, v) { this.attributes[k] = String(v); }
    getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
    appendChild(c) { c.remove(); c.parentElement = this; this.children.push(c); return c; }
    append(...cs) { cs.forEach((c) => this.appendChild(c)); }
    replaceChildren(...cs) { this.children.forEach((c) => { c.parentElement = null; }); this.children = []; this.append(...cs); }
    remove() { if (!this.parentElement) return; const p = this.parentElement; p.children = p.children.filter((c) => c !== this); this.parentElement = null; }
    addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
    removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn); }
    get isConnected() { let at = this; while (at.parentElement) at = at.parentElement; return at === documentStub.body; }
    getBoundingClientRect() { return { left: 0, top: 0, width: 1000, height: 20 }; }
    setPointerCapture() {
        if (!this.isConnected) throw new Error("InvalidStateError: setPointerCapture on a detached element");
        pointer.capture = this;
    }
    releasePointerCapture() { if (pointer.capture === this) pointer.capture = null; }
    *walk() { yield this; for (const c of this.children) yield* c.walk(); }
    findAll(pred) { return [...this.walk()].filter(pred); }
}

const windowListeners = {};
const documentListeners = {};
const documentStub = {
    body: new Element("body"),
    createElement: (tag) => new Element(tag),
    addEventListener: (t, fn) => { (documentListeners[t] ||= []).push(fn); },
    removeEventListener: (t, fn) => { documentListeners[t] = (documentListeners[t] || []).filter((f) => f !== fn); },
};

const context = createContext({ console, Math, JSON, Promise, setTimeout, clearTimeout, Array, Object, Number,
                                String, Set, Map, Proxy, document: documentStub });
context.window = context;
context.globalThis = context;
context.addEventListener = (t, fn) => { (windowListeners[t] ||= []).push(fn); };
context.removeEventListener = (t, fn) => { windowListeners[t] = (windowListeners[t] || []).filter((f) => f !== fn); };
for (const file of ["videoSchema.js", "videoTimeline.js"]) {
    runInContext(readFileSync(join(STATIC, file), "utf8"), context, { filename: file });
}

/** The element under the pointer: a diamond of the track's lane within 6 px
 *  of clientX (the lane starts at x = 0), else the lane itself. */
function hitTest(root, trackId, clientX) {
    const lane = root.findAll((n) => n.getAttribute("data-track") === trackId)[0];
    const keys = lane.findAll((n) => n.classList.contains("vs-key"));
    const hit = keys.reverse().find((k) => Math.abs(parseFloat(k.style.left) - clientX) <= 6);
    return hit || lane;
}

function dispatchPointer(root, trackId, type, clientX, extra = {}) {
    if (pointer.capture && !pointer.capture.isConnected) pointer.capture = null;   // implicit release
    const target = pointer.capture || hitTest(root, trackId, clientX);
    let stopped = false;
    const event = { type, target, clientX, clientY: 5, pointerId: 1, button: 0, shiftKey: false, altKey: false,
                    ctrlKey: false, metaKey: false, preventDefault() {}, stopPropagation() { stopped = true; },
                    ...extra };
    for (let at = target; at && !stopped; at = at.parentElement) {
        event.currentTarget = at;
        for (const fn of [...(at.listeners[type] || [])]) fn(event);
    }
    for (const table of [documentListeners, windowListeners]) {
        if (stopped) break;
        for (const fn of [...(table[type] || [])]) fn(event);
    }
    if (type === "pointerup") pointer.capture = null;
    return target;
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

// -- a controller ------------------------------------------------------------------------

function makeTimeline() {
    const log = { edits: [], seeks: [], errors: [] };
    const wrapper = documentStub.body.appendChild(new Element("div"));
    const controller = {
        project: {
            duration_s: 10,
            tracks: [{ track_id: "tr1", kind: "camera2d", mode: "keyframes", target: null, label: null, muted: false,
                       keyframes: [{ kf_id: "k1", t: 1, value: { cx: 1, cy: 2, w: 3 }, ease: "cinematic" },
                                   { kf_id: "k2", t: 5, value: { cx: 4, cy: 5, w: 6 }, ease: "linear" }] }],
        },
        facts: { channels: [] },
        preview: { time: 0, pause() {}, seek(t) { log.seeks.push(t); controller.preview.time = t; } },
        edit: async (ops, label) => { log.edits.push({ ops, label }); },
        bar: { showError: (m) => log.errors.push(m), setHint() {} },
        env: { wrapper },
        ctx: { view: { reserve: () => () => {} } },
        history: { canUndo: false, canRedo: false },
    };
    const timeline = context.PlexoraVideoTimeline.create(controller);
    timeline.toggle(true);
    const root = wrapper.children.find((n) => n.classList.contains("vs-timeline"));
    const finish = () => { timeline.destroy(); wrapper.remove(); pointer.capture = null; };
    return { controller, timeline, root, log, finish };
}

async function gesture(root, steps, extra = {}) {
    // The diamond for k1 sits at t = 1 s, x = 100 px.
    dispatchPointer(root, "tr1", "pointerdown", 100, extra);
    for (const x of steps) dispatchPointer(root, "tr1", "pointermove", x, extra);
    dispatchPointer(root, "tr1", "pointerup", steps.length ? steps.at(-1) : 100, extra);
    await settle();
}

// -- drag ---------------------------------------------------------------------------------
{
    const { root, log, finish } = makeTimeline();
    check("the diamond for k1 sits at 100 px",
          root.findAll((n) => n.classList.contains("vs-key"))[0]?.style.left === "100px");
    await gesture(root, [120, 150, 180, 200]);
    const ops = log.edits.flatMap((e) => e.ops);
    check("dragging a diamond 100 px sends one update_keyframe", ops.length === 1 && ops[0].op === "update_keyframe"
          && ops[0].kf_id === "k1", { edits: log.edits, errors: log.errors });
    check("…that moves it to 2 s", ops.length === 1 && Math.abs((ops[0].changes?.t ?? NaN) - 2) < 1e-9, ops);
    finish();
}

// -- Alt-drag -----------------------------------------------------------------------------
{
    const { root, log, finish } = makeTimeline();
    await gesture(root, [130, 160, 200], { altKey: true });
    const ops = log.edits.flatMap((e) => e.ops);
    check("Alt-dragging a diamond sends one add_keyframe", ops.length === 1 && ops[0].op === "add_keyframe",
          { edits: log.edits, errors: log.errors });
    check("…at the new time with the original's value and pace", ops.length === 1
          && Math.abs((ops[0].keyframe?.t ?? NaN) - 2) < 1e-9 && ops[0].keyframe?.ease === "cinematic"
          && ops[0].keyframe?.value?.w === 3, ops);
    finish();
}

// -- click --------------------------------------------------------------------------------
{
    const { root, log, finish } = makeTimeline();
    await gesture(root, []);
    check("clicking a diamond seeks the preview to its time", log.seeks.length === 1 && log.seeks[0] === 1,
          { seeks: log.seeks });
    check("…and edits nothing", log.edits.length === 0, log.edits);
    finish();
}

process.stderr.write(JSON.stringify({ checked, failures }, null, 2));
process.stdout.write(failures.length ? `${failures.length} of ${checked} checks failed\n`
                                     : `all ${checked} checks passed\n`);
process.exit(failures.length ? 1 : 0);
