// On every viewer page: a notebook's embedding in a window over the tissue.
//
// Opened from Cell Explorer ("Embedding", which this script makes appear when
// a notebook holds an embedding of the project on screen) or from a UMAP step
// ("Open embedding in the viewer", which leaves a note in sessionStorage).
// The window is core's (views/canvasWindow.js): moved, sized, folded to a
// pill, closed; the embedding in it is the notebook's own view
// (scimapEmbed.js) in its glass skin.
//
// A lasso there is the app's selection (services/cellSelection.js); Cell
// Explorer draws it on the tissue through its colour table, as Highlight,
// Dim others or Only. A region the pointer rests on is outlined in gold in
// the embedding, and "Use as selection" makes it the selection.
//
// A selection spanning a dataset's images is counted per image in a pill at
// the top, with "Next image" walking to the next one that has selected cells.
// That walk is a full page load per image, so the window's state -- open,
// which notebook, embedding and colour -- is kept in the same note and the
// window opens again on arrival, where the user left it.
(function () {
    "use strict";
    if (typeof window === "undefined" || window.__scimapEmbedWindow) return;
    window.__scimapEmbedWindow = true;

    const NOTE = "plexora:scimap-embed";
    const WINDOW_ID = "scimap-embedding";
    let embed = null;
    let win = null;
    let pill = null;
    let unplacePill = null;
    let notebooks = null;

    function url(path) {
        return typeof window.plexoraUrl === "function" ? window.plexoraUrl(path) : path;
    }

    function project() {
        return (window.flaskVariables && window.flaskVariables.datasource) ||
            (document.body && document.body.dataset && document.body.dataset.plexoraDatasource) || "";
    }

    function note() {
        try {
            return JSON.parse(window.sessionStorage.getItem(NOTE) || "null") || {};
        } catch (err) {
            return {};
        }
    }

    function keep(patch) {
        try {
            window.sessionStorage.setItem(NOTE, JSON.stringify(Object.assign(note(), patch)));
        } catch (err) { /* this page only */ }
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    const fmt = (n) => Number(n || 0).toLocaleString("en-US");

    async function available() {
        if (notebooks) return notebooks;
        const here = project();
        if (!here) return [];
        try {
            const response = await fetch(url("/plugins/scimap/api/embedding?project=" + encodeURIComponent(here)));
            const body = await response.json();
            notebooks = (body && body.notebooks) || [];
        } catch (err) {
            notebooks = [];
        }
        return notebooks;
    }

    async function announce() {
        const list = await available();
        if (!list.length) return;
        document.documentElement.dataset.plexoraEmbedding = project();
        window.dispatchEvent(new CustomEvent("plexora:embedding-available", { detail: { project: project() } }));
    }

    function whenReady(timeout) {
        const deadline = Date.now() + (timeout || 30000);
        return new Promise((resolve) => {
            (function check() {
                if (window.__plexoraReady && window.PlexoraAgentBridge) return resolve(true);
                if (Date.now() > deadline) return resolve(false);
                setTimeout(check, 150);
            })();
        });
    }

    /** Colour the tissue the way the embedding is coloured: by the column
     *  itself when the table has it, else through the notebook's overlay. */
    async function colourTissue(notebookId, column, meta) {
        if (!column || !(await whenReady(15000))) return;
        try {
            await window.PlexoraAgentBridge.run("set_color_by", { column });
            return;
        } catch (err) { /* not a column of this table */ }
        if (!meta || meta.kind !== "category") {
            // A marker or a number the table does not carry: Cell Explorer is
            // still opened, so the selection shows.
            try { await window.PlexoraAgentBridge.run("set_color_by", { column: "scimap" }); } catch (e) { /* none */ }
            return;
        }
        try {
            await fetch(url("/plugins/scimap/api/notebooks/" + encodeURIComponent(notebookId) + "/overlay"), {
                method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ column }) });
            await window.PlexoraAgentBridge.run("set_color_by", { column: "scimap" });
        } catch (err) { /* the embedding still works */ }
    }

    // -- the pill at the top: the selection, image by image -------------------

    function paintPill(sel) {
        const here = project();
        if (!sel || !sel.count) {
            if (pill) pill.hidden = true;
            return;
        }
        if (!pill) {
            pill = el("div", "sm-embed-status");
            pill.setAttribute("role", "status");
            const layout = window.PlexoraCanvasLayout;
            unplacePill = layout && layout.place ? layout.place(pill, { corner: "top-center", order: 20, id: "scimap-embed-status" }) : null;
            if (!unplacePill) document.body.appendChild(pill);
        }
        pill.hidden = false;
        pill.textContent = "";
        const index = sel.projects.indexOf(here);
        const onThis = index >= 0 ? sel.perImage[index] : 0;
        pill.append(el("span", "sm-embed-status-dot"),
            el("strong", null, fmt(sel.count) + " selected"),
            el("span", null, " · " + fmt(onThis) + " on this image"));
        const others = sel.projects.map((p, i) => [p, sel.perImage[i]]).filter(([p, k]) => p !== here && k);
        const tail = [];
        if (others.length) {
            pill.append(el("span", "sm-embed-status-rest", " · " + others.slice(0, 3)
                .map(([p, k]) => fmt(k) + " on " + p).join(" · ") + (others.length > 3 ? " …" : "")));
            const order = sel.projects;
            const start = Math.max(0, index);
            let next = null;
            for (let step = 1; step <= order.length; step += 1) {
                const candidate = order[(start + step) % order.length];
                if (candidate !== here && sel.perImage[order.indexOf(candidate)]) { next = candidate; break; }
            }
            if (next) {
                const button = el("button", "sm-embed-status-next", "Next image →");
                button.type = "button";
                button.title = "Open " + next + ", where " + fmt(sel.perImage[order.indexOf(next)]) + " of them are";
                button.addEventListener("click", () => goTo(next));
                tail.push(button);
            }
        }
        if (!(win && !win.closed) && notebooks && notebooks.length) {
            const openIt = el("button", "sm-embed-status-next", "Embedding");
            openIt.type = "button";
            openIt.title = "Open the embedding in a window over the tissue";
            openIt.addEventListener("click", () => open({}));
            tail.push(openIt);
        }
        const clear = el("button", "sm-embed-status-clear", "×");
        clear.type = "button";
        clear.title = "Clear the selection and show every cell";
        clear.setAttribute("aria-label", "Clear the selection");
        clear.addEventListener("click", () => window.PlexoraCellSelection && window.PlexoraCellSelection.clear({ source: "viewer" }));
        tail.push(clear);
        pill.append(...tail);
    }

    /** The pill from core's selection alone, when no window is open: a
     *  selection made in the notebook ("View in tissue") must be visible and
     *  clearable here too. */
    function paintFromBus() {
        if (embed) return; // the window paints it, with its own counts
        const bus = window.PlexoraCellSelection;
        const sel = bus && bus.current();
        if (!sel || !(sel.scope || []).includes(project())) {
            paintPill(null);
            return;
        }
        const projects = sel.scope;
        const perImage = projects.map((p) => (sel.projects[p] ? sel.projects[p].length : 0));
        paintPill({ count: perImage.reduce((a, b) => a + b, 0), projects, perImage });
    }

    function goTo(name) {
        keep({ open: Boolean(win && !win.closed) });
        const href = url("/" + encodeURIComponent(name)) + "?tool=cell_explorer";
        window.location.href = href;
    }

    // -- the window -------------------------------------------------------------

    function style() {
        if (document.getElementById("sm_embed_window_style")) return;
        const link = document.createElement("link");
        link.id = "sm_embed_window_style";
        link.rel = "stylesheet";
        const script = Array.from(document.scripts).find((s) => /scimapEmbedWindow\.js/.test(s.src));
        const version = script ? (script.src.split("?")[1] || "") : "";
        link.href = url("/plugins/scimap/static/scimapEmbed.css") + (version ? "?" + version : "");
        document.head.appendChild(link);
    }

    async function open(options) {
        const here = project();
        if (!here || !window.PlexoraCanvasWindow || !window.ScimapEmbed) return;
        const saved = note();
        let notebookId = (options && options.notebook_id) || saved.notebook_id;
        if (!notebookId) {
            const list = await available();
            if (!list.length) return;
            notebookId = list[0].notebook_id;
        }
        style();
        if (embed && embed.o.notebookId !== notebookId) close(false);
        if (win && !win.closed) {
            win.collapse(false);
            return;
        }
        keep({ open: true, notebook_id: notebookId });
        let label = "Embedding";
        embed = window.ScimapEmbed.create({
            notebookId, theme: "glass", base: url("/plugins/scimap"),
            key: (options && options.key) || saved.key, color: (options && options.color) || saved.color,
            onKey: (key) => keep({ key }),
            onColor: (column, meta) => {
                keep({ color: column });
                colourTissue(notebookId, column, meta);
            },
            onSelection: (sel) => {
                paintPill(sel);
                if (win && win.collapsed) win.setCollapsedLabel();
            },
        });
        const title = el("div", "sm-embed-title");
        title.append(embed.keySelect, embed.colorSelect);
        win = window.PlexoraCanvasWindow.open({
            id: WINDOW_ID, label: "Embedding", title, body: embed.root,
            width: 520, height: 520, minWidth: 300, minHeight: 300, at: { x: 1, y: 0.5 },
            collapsedLabel: () => {
                const info = embed.state.info;
                const key = info && info.embeddings.find((e) => e.key === embed.state.key);
                label = key ? key.label : label;
                return label + " · " + (embed.selected ? fmt(embed.selected) + " selected" : "nothing selected");
            },
            onResize: () => embed && embed.resize(),
            onCollapse: () => embed && embed.resize(),
            onClose: () => close(true),
        });
        // Cell Explorer open, coloured as the embedding is, so the selection
        // shows on the tissue at once.
        const wait = setInterval(() => {
            if (!embed || embed.state.error) { clearInterval(wait); return; }
            if (embed.colorMeta !== undefined && !embed.state.loading) {
                clearInterval(wait);
                colourTissue(notebookId, embed.state.column, embed.colorMeta);
            }
        }, 200);
    }

    function close(byUser) {
        if (byUser) keep({ open: false });
        if (embed) embed.destroy();
        embed = null;
        if (win && !win.closed) win.close();
        win = null;
        paintFromBus();
    }

    window.addEventListener("plexora:cells-selected", () => paintFromBus());

    window.addEventListener("plexora:embedding-open", (e) => open({ notebook_id: null, project: e.detail && e.detail.project }));

    function arrive() {
        if (!project()) return;
        style();
        announce().then(() => paintFromBus());
        if (window.PlexoraCellSelection) window.PlexoraCellSelection.ready().then(() => paintFromBus());
        const saved = note();
        if (saved.open && saved.notebook_id) open(saved);
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", arrive);
    else arrive();
    // The viewer is kept alive (hidden) behind other pages; the window lives
    // in its canvas and goes and comes with it. A note left by a notebook
    // while the viewer was hidden opens it on return.
    window.addEventListener("plexora:viewer-shown", () => {
        const saved = note();
        if (saved.open && saved.notebook_id && !(win && !win.closed)) open(saved);
        else if (embed) embed.resize();
    });
})();
