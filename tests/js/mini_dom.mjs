/**
 * A DOM small enough to read, for the probes that draw a dialog in node
 * (tests/js/reopen_files_probe.mjs). The same elements as
 * web_onboarding_probe.mjs's own copy, with firstChild and lastChild.
 *
 * Selectors: `.a.b`, `tag`, `[attr]`, `[attr="v"]`, compounds, descendants.
 */

/** `.a.b`, `tag`, `[attr]`, `[attr="v"]`, compounds, and descendants (space). */
export function matchesSimple(node, simple) {
    if (!node || !node.tagName) return false;
    const parts = simple.match(/^[a-z0-9-]+|\.[\w-]+|\[[^\]]+\]/gi) || [];
    return parts.every((part) => {
        if (part.startsWith(".")) return node.classList.contains(part.slice(1));
        if (part.startsWith("[")) {
            const [, name, value] = part.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
            const have = node.getAttribute(name);
            return value === undefined ? have !== null : have === value;
        }
        return node.tagName.toLowerCase() === part.toLowerCase();
    });
}

export function matches(node, selector) {
    const chain = selector.trim().split(/\s+/);
    if (!matchesSimple(node, chain.at(-1))) return false;
    let at = node.parentNode;
    for (let i = chain.length - 2; i >= 0; i -= 1) {
        while (at && !matchesSimple(at, chain[i])) at = at.parentNode;
        if (!at) return false;
        at = at.parentNode;
    }
    return true;
}

export function makeDocument() {
    const doc = {};
    let focused = null;

    function text(value) {
        return { nodeType: 3, textContent: String(value), parentNode: null };
    }

    function element(tag) {
        const classes = new Set();
        const attributes = new Map();
        const listeners = new Map();
        const node = {
            nodeType: 1,
            tagName: String(tag).toUpperCase(),
            children: [],
            childNodes: [],
            parentNode: null,
            dataset: {},
            style: { setProperty(name, value) { this[name] = value; } },
            hidden: false,
            disabled: false,
            open: false,
            type: "",
            title: "",
            alt: "",
            src: "",
            href: "",
            target: "",
            rel: "",
            tabIndex: 0,
            offsetHeight: 0,
            markup: "",
            get id() { return attributes.get("id") || ""; },
            set id(value) { attributes.set("id", String(value)); },
            get className() { return [...classes].join(" "); },
            set className(value) {
                classes.clear();
                String(value).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c));
            },
            classList: {
                add: (...names) => names.forEach((n) => classes.add(n)),
                remove: (...names) => names.forEach((n) => classes.delete(n)),
                contains: (name) => classes.has(name),
                toggle: (name, on) => ((on === undefined ? !classes.has(name) : on) ? classes.add(name) : classes.delete(name)),
            },
            get textContent() { return node.childNodes.map((child) => child.textContent).join(""); },
            set textContent(value) {
                node.childNodes.forEach((child) => { child.parentNode = null; });
                node.childNodes = value === null || value === undefined || value === "" ? [] : [text(value)];
                node.children = [];
                node.childNodes.forEach((child) => { child.parentNode = node; });
            },
            set innerHTML(value) {
                node.markup = String(value);
                node.childNodes = [];
                node.children = [];
            },
            get innerHTML() { return node.markup; },
            get firstChild() { return node.childNodes[0] || null; },
            get lastChild() { return node.childNodes[node.childNodes.length - 1] || null; },
            setAttribute(name, value) {
                if (name === "class") node.className = value;
                else attributes.set(name, String(value));
            },
            getAttribute(name) {
                if (name === "class") return node.className;
                if (name.startsWith("data-")) {
                    const key = name.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase());
                    return key in node.dataset ? String(node.dataset[key]) : null;
                }
                return attributes.has(name) ? attributes.get(name) : null;
            },
            removeAttribute(name) { attributes.delete(name); },
            appendChild(child) {
                if (child.parentNode) child.parentNode.removeChild(child);
                child.parentNode = node;
                node.childNodes.push(child);
                if (child.nodeType === 1) node.children.push(child);
                return child;
            },
            append(...items) { items.forEach((item) => node.appendChild(typeof item === "string" ? text(item) : item)); },
            removeChild(child) {
                node.childNodes = node.childNodes.filter((c) => c !== child);
                node.children = node.children.filter((c) => c !== child);
                child.parentNode = null;
                return child;
            },
            replaceChildren(...items) {
                [...node.childNodes].forEach((child) => node.removeChild(child));
                items.forEach((item) => node.appendChild(item));
            },
            replaceWith(other) {
                const parent = node.parentNode;
                if (!parent) return;
                const at = parent.childNodes.indexOf(node);
                parent.removeChild(node);
                if (other.parentNode) other.parentNode.removeChild(other);
                other.parentNode = parent;
                parent.childNodes.splice(at, 0, other);
                parent.children = parent.childNodes.filter((c) => c.nodeType === 1);
            },
            remove() { if (node.parentNode) node.parentNode.removeChild(node); },
            contains(other) {
                for (let at = other; at; at = at.parentNode) if (at === node) return true;
                return false;
            },
            querySelectorAll(selector) {
                const out = [];
                const walk = (at) => at.children.forEach((child) => {
                    if (selector.split(",").some((one) => matches(child, one))) out.push(child);
                    walk(child);
                });
                walk(node);
                return out;
            },
            querySelector(selector) { return node.querySelectorAll(selector)[0] || null; },
            addEventListener(type, handler) {
                if (!listeners.has(type)) listeners.set(type, []);
                listeners.get(type).push(handler);
            },
            removeEventListener() {},
            dispatch(type, extra = {}) {
                const event = { type, target: node, defaultPrevented: false,
                                preventDefault() { this.defaultPrevented = true; }, ...extra };
                (listeners.get(type) || []).forEach((handler) => handler(event));
                return event;
            },
            click() { if (!node.disabled) node.dispatch("click"); },
            focus() { focused = node; },
            showModal() {
                node.open = true;
            },
            close() {
                if (!node.open) return;
                node.open = false;
                node.dispatch("close");
            },
        };
        return node;
    }

    doc.createElement = (tag) => element(tag);
    doc.createElementNS = (ns, tag) => element(tag);
    doc.createTextNode = (value) => text(value);
    doc.body = element("body");
    doc.getElementById = (id) => doc.body.querySelectorAll("*").find?.((n) => n.id === id)
        || doc.body.querySelectorAll(`[id="${id}"]`)[0] || null;
    Object.defineProperty(doc, "activeElement", { get: () => focused });
    return doc;
}
