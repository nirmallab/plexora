/**
 * The bottom-right notice: when it goes, and when it stays.
 *
 * Two of these are the reason the file exists rather than being a div and a
 * setTimeout:
 *
 *   - HOVER STOPS THE CLOCK. A notice that disappears while it is being read
 *     is worse than no notice, and its list can be six channel names somebody
 *     is checking against what they expected.
 *   - ONE AT A TIME. Several notices about one action is the failure this is
 *     meant to prevent, so a second show() replaces rather than stacks.
 */

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SOURCE = join(REPO, "plexora/client/src/js/services/toast.js");

let checks = 0;
function check(label, fn) {
    fn();
    checks += 1;
    console.log("  ok  " + label);
}

function element(tag = "div") {
    const node = {
        tagName: tag.toUpperCase(),
        className: "",
        id: "",
        type: "",
        title: "",
        textContent: "",
        children: [],
        attributes: {},
        listeners: {},
        classList: {
            _set: new Set(),
            add(name) { this._set.add(name); },
            contains(name) { return this._set.has(name); },
        },
        appendChild(child) {
            this.children.push(child);
            child.parentNode = this;
            return child;
        },
        removeChild(child) {
            this.children = this.children.filter((c) => c !== child);
            child.parentNode = null;
        },
        setAttribute(name, value) { this.attributes[name] = value; },
        addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); },
        fire(name) { (this.listeners[name] || []).forEach((fn) => fn()); },
    };
    node.parentNode = null;
    return node;
}

/** A page with a controllable clock, so a 20 s timeout is testable. */
function boot() {
    const body = element("body");
    const timers = new Map();
    let nextId = 1;
    let now = 0;
    const byId = {};

    const context = {
        console: { log() {}, error() {}, warn() {} },
        Number, Array, Object, String, Boolean, Set,
        document: {
            body,
            getElementById: (id) => byId[id] || null,
            createElement: (tag) => {
                const node = element(tag);
                const original = node.appendChild;
                node.appendChild = function (child) {
                    const out = original.call(this, child);
                    if (child.id) byId[child.id] = child;
                    return out;
                };
                return node;
            },
        },
    };
    context.window = context;
    context.setTimeout = (fn, ms) => {
        const id = nextId++;
        timers.set(id, { fn, at: now + (ms || 0) });
        return id;
    };
    context.clearTimeout = (id) => timers.delete(id);
    // Register the host the same way the module's own appendChild would.
    const bodyAppend = body.appendChild.bind(body);
    body.appendChild = (child) => {
        if (child.id) byId[child.id] = child;
        return bodyAppend(child);
    };

    createContext(context);
    runInContext(readFileSync(SOURCE, "utf8"), context);

    return {
        api: context.PlexoraToast,
        body,
        /** Move the clock and run whatever is due. */
        tick(ms) {
            now += ms;
            [...timers.entries()]
                .filter(([, t]) => t.at <= now)
                .forEach(([id, t]) => { timers.delete(id); t.fn(); });
        },
        host: () => byId.plexora_toast_host || null,
        toasts: () => (byId.plexora_toast_host?.children || [])
            .filter((n) => n.className === "plx-toast"),
    };
}

function textOf(node) {
    const own = node.textContent || "";
    return own + (node.children || []).map(textOf).join(" ");
}

console.log("toast");

check("a notice renders its title", () => {
    const t = boot();
    t.api.show({ title: "Carried over from sampleA" });
    assert.equal(t.toasts().length, 1);
    assert.match(textOf(t.toasts()[0]), /Carried over from sampleA/);
});

check("with no title there is nothing to say and nothing is drawn", () => {
    const t = boot();
    assert.equal(t.api.show({ lines: ["a", "b"] }), null);
    assert.equal(t.host(), null, "not even a host element");
});

check("the note and the list are drawn when given", () => {
    const t = boot();
    t.api.show({
        title: "Carried over from sampleA",
        note: "Not available on this sample:",
        lines: ["Channel SOX10 is not in this sample", "Layer transcripts"],
    });
    const text = textOf(t.toasts()[0]);
    assert.match(text, /Not available on this sample:/);
    assert.match(text, /SOX10/);
    assert.match(text, /transcripts/);
});

check("the host is a polite live region", () => {
    const t = boot();
    t.api.show({ title: "x" });
    assert.equal(t.host().attributes.role, "status");
    assert.equal(t.host().attributes["aria-live"], "polite");
});

check("it goes by itself after twenty seconds", () => {
    const t = boot();
    t.api.show({ title: "x" });
    t.tick(t.api.DEFAULT_TIMEOUT_MS - 1);
    assert.equal(t.toasts().length, 1, "still up a moment before");
    t.tick(2);
    t.tick(300);  // the fade, then the removal
    assert.equal(t.toasts().length, 0);
});

check("hovering stops the clock", () => {
    const t = boot();
    t.api.show({ title: "x" });
    const node = t.toasts()[0];
    node.fire("mouseenter");
    t.tick(t.api.DEFAULT_TIMEOUT_MS * 3);
    t.tick(300);
    assert.equal(t.toasts().length, 1, "a notice being read does not vanish");
});

check("and leaving starts it again, in full", () => {
    const t = boot();
    t.api.show({ title: "x" });
    const node = t.toasts()[0];
    node.fire("mouseenter");
    t.tick(t.api.DEFAULT_TIMEOUT_MS * 2);
    node.fire("mouseleave");
    t.tick(t.api.DEFAULT_TIMEOUT_MS - 1);
    assert.equal(t.toasts().length, 1, "the whole timeout, not the remainder");
    t.tick(2);
    t.tick(300);
    assert.equal(t.toasts().length, 0);
});

check("keyboard focus holds it too", () => {
    // A notice whose dismiss button has focus is one somebody is about to press.
    const t = boot();
    t.api.show({ title: "x" });
    t.toasts()[0].fire("focusin");
    t.tick(t.api.DEFAULT_TIMEOUT_MS * 2);
    t.tick(300);
    assert.equal(t.toasts().length, 1);
});

check("the dismiss button takes it away", () => {
    const t = boot();
    t.api.show({ title: "x" });
    const close = t.toasts()[0].children[0].children[1];
    assert.equal(close.className, "plx-toast-dismiss");
    close.fire("click");
    t.tick(300);
    assert.equal(t.toasts().length, 0);
});

check("the caller can take back one it raised", () => {
    const t = boot();
    const handle = t.api.show({ title: "x" });
    handle.dismiss();
    t.tick(300);
    assert.equal(t.toasts().length, 0);
});

check("a second notice replaces the first rather than stacking", () => {
    const t = boot();
    t.api.show({ title: "first" });
    t.api.show({ title: "second" });
    t.tick(300);
    const live = t.toasts();
    assert.equal(live.length, 1);
    assert.match(textOf(live[0]), /second/);
});

check("dismissing twice is not an error and removes nothing twice", () => {
    const t = boot();
    const handle = t.api.show({ title: "x" });
    handle.dismiss();
    handle.dismiss();
    t.tick(300);
    assert.equal(t.toasts().length, 0);
});

check("a timeout of 0 means it stays until dismissed", () => {
    const t = boot();
    t.api.show({ title: "x", timeout: 0 });
    t.tick(t.api.DEFAULT_TIMEOUT_MS * 10);
    t.tick(300);
    assert.equal(t.toasts().length, 1);
});

// -- actions, and knowing why it went ---------------------------------------
//
// A remote machine that stopped answering is still a thing that happened --
// the page goes on working around it -- but it has a fix, and a Reconnect
// button in the notice is shorter than directions to Settings.

const actionRow = (toast) => toast.children.find((n) => n.className === "plx-toast-actions");
const everything = (t) => (t.host()?.children || []);

check("an action runs, then dismisses the notice", () => {
    const t = boot();
    const order = [];
    const handle = t.api.show({
        title: "Remote server disconnected", timeout: 0,
        actions: [{ label: "Reconnect", primary: true,
                    onSelect: () => order.push(handle.isLive()) }],
    });
    const [button] = actionRow(everything(t)[0]).children;
    assert.equal(button.textContent, "Reconnect");
    assert.equal(button.className, "plx-toast-action is-primary");
    button.fire("click");
    assert.deepEqual(order, [true], "it ran while the notice was still up");
    assert.equal(handle.isLive(), false);
    t.tick(300);
    assert.equal(everything(t).length, 0);
});

check("an action that answers false leaves it up", () => {
    const t = boot();
    const handle = t.api.show({
        title: "x", timeout: 0, actions: [{ label: "Busy", onSelect: () => false }],
    });
    actionRow(everything(t)[0]).children[0].fire("click");
    assert.equal(handle.isLive(), true);
});

check("onDismiss hears why it went, once", () => {
    const why = [];
    const t = boot();
    const first = t.api.show({ title: "a", timeout: 0, onDismiss: (w) => why.push(w) });
    first.node.children[0].children[1].fire("click");
    first.dismiss();
    t.api.show({ title: "b", timeout: 0, onDismiss: (w) => why.push(w) });
    t.api.show({ title: "c", onDismiss: (w) => why.push(w) });
    t.tick(t.api.DEFAULT_TIMEOUT_MS + 1);
    const withAction = t.api.show({ title: "d", timeout: 0, onDismiss: (w) => why.push(w),
                                    actions: [{ label: "Go", onSelect: () => {} }] });
    actionRow(withAction.node).children[0].fire("click");
    assert.deepEqual(why, ["user", "replaced", "timeout", "action"]);
});

check("a warning tone is marked, and nothing else changes", () => {
    const t = boot();
    const warning = t.api.show({ title: "x", tone: "warning" });
    assert.equal(warning.node.className, "plx-toast is-warning");
    const plain = t.api.show({ title: "y" });
    assert.equal(plain.node.className, "plx-toast");
});

console.log(`\n${checks} checks passed`);
