// SCIMAP's requests, and the small DOM helpers every view file shares.
//
// One error shape for the whole page: a refusal from the server arrives as
// `ScimapApi.Refusal` carrying the server's code, its sentence and its detail,
// so a step can show "Over the Starter limit" with the numbers rather than a
// status line.
window.ScimapApi = (function () {
    "use strict";

    class Refusal extends Error {
        constructor(status, body) {
            super((body && (body.message || body.error)) || ("HTTP " + status));
            this.status = status;
            this.code = (body && body.error) || "error";
            this.detail = (body && body.detail) || {};
            this.body = body || {};
        }
    }

    let base = "";

    function setBase(url) {
        base = String(url || "").replace(/\/$/, "");
    }

    function url(path) {
        return base + path;
    }

    async function request(method, path, body, options) {
        const init = { method, headers: {} };
        if (body instanceof FormData) {
            init.body = body;
        } else if (body !== undefined) {
            init.headers["Content-Type"] = "application/json";
            init.body = JSON.stringify(body);
        }
        if (options && options.signal) init.signal = options.signal;
        const response = await fetch(url(path), init);
        let payload = null;
        try {
            payload = await response.json();
        } catch (err) {
            payload = null;
        }
        if (!response.ok || (payload && payload.success === false)) {
            throw new Refusal(response.status, payload);
        }
        return payload || {};
    }

    const nb = (id) => "/api/notebooks/" + encodeURIComponent(id);
    const cell = (id, cid) => nb(id) + "/cells/" + encodeURIComponent(cid);

    return {
        Refusal,
        setBase,
        url,
        // The answer is kept: `ScimapApi.needs` says which features wait for a
        // newer Scimap Pro, for views that have no status of their own.
        status: () => request("GET", "/api/status").then((a) => {
            if (a && a.status) window.ScimapApi.needs = a.status.needs || {};
            return a;
        }),
        tools: () => request("GET", "/api/tools"),
        places: () => request("GET", "/api/places"),
        warm: () => request("POST", "/api/warm", {}),
        notebooks: (kind, ref) => request("GET", "/api/notebooks" +
            (kind ? "?kind=" + encodeURIComponent(kind) + "&ref=" + encodeURIComponent(ref) : "")),
        create: (kind, ref, title) => request("POST", "/api/notebooks", { kind, ref, title }),
        view: (id) => request("GET", nb(id)),
        patch: (id, ops, revision) => request("PATCH", nb(id), { ops, revision }),
        remove: (id) => request("DELETE", nb(id)),
        duplicate: (id) => request("POST", nb(id) + "/duplicate", {}),
        plan: (id, folder) => request("GET", nb(id) + "/plan" + (folder ? "?folder=" + encodeURIComponent(folder) : "")),
        runAll: (id) => request("POST", nb(id) + "/run_all", {}),
        run: (id, cid, body) => request("POST", cell(id, cid) + "/run", body || {}),
        cancel: (id, cid) => request("POST", cell(id, cid) + "/cancel", {}),
        result: (id, cid, refresh) => request("GET", cell(id, cid) + "/result" + (refresh ? "?refresh=1" : "")),
        colors: (id, cid, column) => request("GET", cell(id, cid) + "/colors?column=" + encodeURIComponent(column)),
        figures: (id, cid) => request("GET", cell(id, cid) + "/figures"),
        figureUrl: (id, cid, name, fmt, view, opts) => url(cell(id, cid) + "/figures/" + encodeURIComponent(name) + "." + fmt +
            (view ? "?view=" + encodeURIComponent(view) + "&opts=" + encodeURIComponent(JSON.stringify(opts || {})) : "")),
        drawView: (id, cid, view, opts, compute) => request("POST", cell(id, cid) + "/draw", { view, opts: opts || {}, compute: Boolean(compute) }),
        lasso: (id, cid, polygon, color) => request("POST", cell(id, cid) + "/lasso", { polygon, color }),
        show: (id, cid, body) => request("POST", cell(id, cid) + "/view", body || {}),
        column: (id, name) => request("GET", nb(id) + "/column?name=" + encodeURIComponent(name)),
        estimate: (id) => request("GET", nb(id) + "/export/estimate"),
        exportUrl: (id, format, include) => url(nb(id) + "/export?format=" + encodeURIComponent(format) +
            "&include=" + encodeURIComponent(include || "workflow")),
        importFile: (file, kind, ref) => {
            const form = new FormData();
            form.append("file", file);
            if (kind) form.append("kind", kind);
            if (ref) form.append("ref", ref);
            return request("POST", "/api/import", form);
        },
        licence: () => request("GET", "/api/licence"),
        activate: (credential) => request("POST", "/api/licence/activate", { credential }),
        installLicence: (file) => {
            const form = new FormData();
            form.append("file", file);
            return request("POST", "/api/licence/install", form);
        },
        connect: () => request("POST", "/api/licence/connect", {}),
        connectPoll: (code) => request("POST", "/api/licence/connect/poll", { code }),
        history: (id, cid) => request("GET", cell(id, cid) + "/history"),
        compare: (id, cid, a, b) => request("GET", cell(id, cid) + "/compare?a=" + a + "&b=" + b),
        makeCurrent: (id, cid, version) => request("POST", cell(id, cid) + "/current", { version }),
        checkAdvanced: (id, cid, key, text, params) => request("POST", cell(id, cid) + "/advanced", { key, text, params }),
        functions: (tool) => request("GET", "/api/tools/" + encodeURIComponent(tool) + "/functions"),
        reading: (id, memory) => request("POST", nb(id) + "/reading", memory ? { memory } : {}),
        data: (id) => request("GET", nb(id) + "/data"),
        editData: (id, body) => request("POST", nb(id) + "/data", body),
        projectColumns: (id, name) => request("GET", nb(id) + "/data/projects/" + encodeURIComponent(name)),
        editProject: (id, name, edits) => request("POST", nb(id) + "/data/projects/" + encodeURIComponent(name), { edits }),
        samples: (id) => request("GET", nb(id) + "/samples"),
        saveSamples: (id, samples) => request("PUT", nb(id) + "/samples", { samples }),
        importSamples: (id, file) => {
            const form = new FormData();
            form.append("file", file);
            return request("POST", nb(id) + "/samples/import", form);
        },
        stamp: () => request("GET", "/api/stamp"),
        overlay: () => request("GET", "/api/overlay"),
        clearOverlay: () => request("DELETE", "/api/overlay"),
    };
})();

// h("div.sm-x", {attrs}, children...): a few lines of DOM building, no
// framework. Text children are text nodes, never HTML.
window.ScimapDom = (function () {
    "use strict";

    function h(tag, attrs, ...children) {
        const [name, ...classes] = String(tag).split(".");
        const node = document.createElement(name || "div");
        if (classes.length) node.className = classes.join(" ");
        if (attrs) {
            for (const [key, value] of Object.entries(attrs)) {
                if (value === null || value === undefined || value === false) continue;
                if (key === "class") node.className = (node.className ? node.className + " " : "") + value;
                else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
                else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
                else if (key === "text") node.textContent = value;
                else if (key === "html") node.innerHTML = value;
                else if (key in node && typeof value !== "string" && key !== "list") node[key] = value;
                else node.setAttribute(key, value === true ? "" : String(value));
            }
        }
        append(node, children);
        return node;
    }

    function append(node, children) {
        for (const child of children) {
            if (child === null || child === undefined || child === false) continue;
            if (Array.isArray(child)) append(node, child);
            else if (child instanceof Node) node.appendChild(child);
            else node.appendChild(document.createTextNode(String(child)));
        }
    }

    function icon(name, size) {
        return window.ScimapIcons.el(name, size);
    }

    function button(label, options) {
        const o = options || {};
        const cls = "sm-btn" + (o.kind ? " sm-btn-" + o.kind : "") + (o.class ? " " + o.class : "");
        return h("button." + cls.split(" ").join("."), {
            type: "button", onclick: o.onclick, disabled: o.disabled || null,
            title: o.title || null, "aria-label": o.aria || null,
        }, o.icon ? icon(o.icon) : null, label ? h("span", null, label) : null);
    }

    function iconButton(name, label, onclick, extra) {
        return h("button.sm-icon-btn" + (extra ? "." + extra : ""), {
            type: "button", "aria-label": label, title: label, onclick,
        }, icon(name));
    }

    const numberFormat = new Intl.NumberFormat("en-US");

    function n(value) {
        return value === null || value === undefined ? "—" : numberFormat.format(value);
    }

    function pct(value, digits) {
        if (value === null || value === undefined) return "—";
        const d = digits === undefined ? (value >= 10 ? 1 : 1) : digits;
        return value.toFixed(d) + "%";
    }

    function bytes(value) {
        if (!value && value !== 0) return "";
        const units = ["B", "KB", "MB", "GB", "TB"];
        let v = value;
        let i = 0;
        while (v >= 1024 && i < units.length - 1) {
            v /= 1024;
            i += 1;
        }
        return (v >= 100 || i === 0 ? Math.round(v) : v.toFixed(v >= 10 ? 0 : 1)) + " " + units[i];
    }

    function seconds(value) {
        if (value === null || value === undefined) return "";
        if (value < 1) return "under a second";
        if (value < 60) return value.toFixed(value < 10 ? 1 : 0) + " s";
        const m = Math.floor(value / 60);
        const s = Math.round(value % 60);
        return m + " min" + (s ? " " + s + " s" : "");
    }

    function clock(value) {
        const total = Math.max(0, Math.floor(value));
        const m = Math.floor(total / 60);
        const s = total % 60;
        return m + ":" + String(s).padStart(2, "0");
    }

    function tildePath(path, home) {
        if (!path) return "";
        if (home && path.indexOf(home) === 0) return "~" + path.slice(home.length);
        return path.replace(/^\/Users\/[^/]+/, "~").replace(/^\/home\/[^/]+/, "~");
    }

    // Change what a node holds and slide its height from the old size to the
    // new one: every fold and unfold on the page moves the same way. The
    // node's top never moves, so nothing jumps under the pointer.
    function reduced() {
        return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    }

    function slide(node, from, ms) {
        if (!node || !node.isConnected || reduced()) return;
        const to = node.getBoundingClientRect().height;
        if (from === null || from === undefined || Math.abs(to - from) < 2) return;
        const duration = ms || Math.min(260, 140 + Math.abs(to - from) / 12);
        node.style.height = from + "px";
        node.style.overflow = "hidden";
        void node.offsetHeight;
        node.style.transition = "height " + Math.round(duration) + "ms cubic-bezier(0.2, 0, 0, 1)";
        node.style.height = to + "px";
        let finished = false;
        const done = () => {
            if (finished) return;
            finished = true;
            node.style.height = "";
            node.style.overflow = "";
            node.style.transition = "";
        };
        node.addEventListener("transitionend", (e) => { if (e.target === node) done(); }, { once: true });
        setTimeout(done, duration + 80);
    }

    function morph(node, mutate, ms) {
        const from = node && node.isConnected ? node.getBoundingClientRect().height : null;
        mutate();
        slide(node, from, ms);
    }

    return { h, icon, button, iconButton, n, pct, bytes, seconds, clock, tildePath, morph, slide };
})();
