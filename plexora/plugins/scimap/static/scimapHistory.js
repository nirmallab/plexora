// A step's history: every run is a version. The version chip in the head
// opens it under the head (server/notebooks.py `history`).
//
// A row per version, children under the version they came from; the selected
// row's details (its exact call, what it read, where its result is kept) with
// Make current, Branch from here and Copy to a new step; two checked rows are
// compared side by side, with how closely their partitions agree.
window.ScimapHistory = (function () {
    "use strict";
    const { h, icon, button, n, tildePath } = window.ScimapDom;
    const API = window.ScimapApi;

    function hhmm(t) {
        if (!t) return "";
        const d = new Date(t * 1000);
        return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    }

    function when(t) {
        if (!t) return "";
        const d = new Date(t * 1000);
        return d.toLocaleDateString("en-US", { day: "numeric", month: "short" }) + ", " + hhmm(t);
    }

    function size(bytes) {
        if (!bytes && bytes !== 0) return "";
        if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + " KB";
        return (bytes / 1024 / 1024).toFixed(1) + " MB";
    }

    // Depth-first under each parent, oldest first; a version's depth is its
    // parent's plus one, held to two steps of indent.
    function tree(versions) {
        const by = new Map(versions.map((v) => [v.version, v]));
        const kids = new Map();
        const roots = [];
        versions.forEach((v) => {
            if (v.parent && by.has(v.parent) && v.parent !== v.version) {
                if (!kids.has(v.parent)) kids.set(v.parent, []);
                kids.get(v.parent).push(v);
            } else {
                roots.push(v);
            }
        });
        const out = [];
        const walk = (v, depth) => {
            out.push({ v, depth: Math.min(depth, 2) });
            (kids.get(v.version) || []).forEach((k) => walk(k, depth + 1));
        };
        roots.forEach((r) => walk(r, 0));
        return out;
    }

    function panel(cell, ctx) {
        const local = ctx.local(cell.id);
        const st = local.hist || (local.hist = { selected: null, compare: [] });
        const root = h("div.sm-history", null, h("span.sm-form-help", null, "Reading the history…"));
        const compareSlot = h("div");
        API.history(ctx.nb.id, cell.id).then((a) => {
            const data = a.history;
            const paint = () => {
                root.innerHTML = "";
                const versions = data.versions;
                if (st.selected === null || !versions.some((v) => v.version === st.selected)) st.selected = data.current || (versions.length ? versions[versions.length - 1].version : null);
                root.append(h("div.sm-history-head", null,
                    h("span.sm-eyebrow", null, "History"),
                    h("span.sm-adv-note", null, versions.length + (versions.length === 1 ? " version" : " versions") +
                        (data.current ? " · current: v" + data.current : "")),
                    h("span.sm-spacer"),
                    h("span.sm-adv-note", null, versions.length > 1 ? "Select two to compare" : "")));
                const list = h("div.sm-history-list", { role: "list" });
                tree(versions).forEach(({ v, depth }) => {
                    const checked = st.compare.includes(v.version);
                    const box = h("input.sm-check", { type: "checkbox", "aria-label": "Compare v" + v.version, checked: checked || null,
                        disabled: v.status !== "done" || null });
                    box.addEventListener("click", (e) => e.stopPropagation());
                    box.addEventListener("change", () => {
                        st.compare = st.compare.filter((x) => x !== v.version);
                        if (box.checked) st.compare.push(v.version);
                        if (st.compare.length > 2) st.compare = st.compare.slice(-2);
                        paint();
                        paintCompare();
                    });
                    const dotKind = v.current ? "is-current" : v.status === "failed" || v.status === "cancelled" ? "is-bad" : "";
                    const change = h("span.sm-history-change", null);
                    if (v.parent && v.change) {
                        change.append(h("span.sm-diff", null, v.change));
                        v.extra.filter((x) => v.change.indexOf(x) < 0).forEach((x) => change.append(" ", h("span.sm-diff.is-code", null, x)));
                    } else {
                        change.append(v.change || "");
                    }
                    const prev = versions.find((x) => x.version === v.version - 1);
                    if (v.parent && prev && v.parent !== prev.version) change.append(" ", h("span.sm-muted", null, "from v" + v.parent));
                    if (v.current) change.append(" ", h("span.sm-badge", null, "Current"));
                    const status = { done: "Done", failed: "Failed", cancelled: "Cancelled", running: "Running" }[v.status] || v.status;
                    const reused = v.how.indexOf("Reused") === 0;
                    const row = h("div.sm-history-row" + (v.version === st.selected ? ".is-selected" : ""),
                        { role: "listitem", "aria-current": String(!!v.current), tabindex: "0",
                          onclick: () => { st.selected = v.version; paint(); } },
                        box,
                        h("span.sm-history-v", { style: { paddingLeft: (depth * 14) + "px" } },
                            depth ? h("span.sm-history-branch") : null, h("span.sm-history-dot." + (dotKind || "is-plain")), "v" + v.version),
                        change,
                        h("span.sm-history-time", null, hhmm(v.started_at)),
                        h("span.sm-history-status.is-" + v.status, null, status),
                        h("span.sm-history-how" + (reused ? ".is-reused" : ""), { title: v.how }, reused ? icon("refresh", "xs") : null, v.how));
                    row.addEventListener("keydown", (e) => { if (e.key === "Enter") { st.selected = v.version; paint(); } });
                    list.append(row);
                });
                root.append(list);
                const sel = versions.find((v) => v.version === st.selected);
                if (sel) root.append(details(cell, ctx, sel, data));
            };
            const paintCompare = () => {
                compareSlot.innerHTML = "";
                if (st.compare.length !== 2) return;
                const [a, b] = st.compare.slice().sort((x, y) => x - y);
                compareSlot.append(h("div.sm-compare", null, h("span.sm-form-help", null, "Comparing v" + a + " and v" + b + "…")));
                API.compare(ctx.nb.id, cell.id, a, b).then((c) => {
                    compareSlot.innerHTML = "";
                    compareSlot.append(compare(cell, ctx, c.compare));
                }).catch((err) => { compareSlot.innerHTML = ""; compareSlot.append(h("div.sm-failure", null, h("p", null, err.message))); });
            };
            paint();
            paintCompare();
        }).catch((err) => {
            root.innerHTML = "";
            root.append(h("div.sm-failure", null, h("p", null, err.message)));
        });
        return [root, compareSlot];
    }

    function details(cell, ctx, v, data) {
        const tool = ctx.tools[cell.tool];
        const actions = [];
        if (!v.current) {
            actions.push(button("Make current", { kind: "primary", disabled: !v.restorable || null,
                title: v.restorable ? "Put this version's result back; nothing is recomputed"
                    : (v.status === "done" ? "Its result is not kept. Branch from it and run it." : "Only a finished version can be made current"),
                onclick: async () => {
                    try {
                        await ctx.makeCurrent(cell.id, v.version);
                        ctx.toast(tool.name + " v" + v.version + " is current.");
                    } catch (err) { ctx.toast(err.message); }
                } }));
        }
        actions.push(button("Branch from here", { onclick: async () => {
            await ctx.op({ op: "branch", id: cell.id, version: v.version });
            const local = ctx.local(cell.id);
            local.history = false;
            local.settings = true;
            local.adv = null;
            ctx.rerender(cell.id);
        } }));
        actions.push(button("Copy to a new step", { onclick: () => ctx.op({ op: "copy_version", id: cell.id, version: v.version }) }));
        const grid = h("div.sm-kv");
        const add = (k, val) => { if (val) grid.append(h("span.sm-kv-k", null, k), val); };
        if (v.call) {
            const call = h("span.sm-kv-code");
            let text = v.call;
            if (v.extra.length) {
                // The Advanced arguments, marked where they sit in the call.
                const marks = v.extra.map((x) => x.replace(" = ", "="));
                const parts = [];
                marks.forEach((m) => {
                    const at = text.indexOf(m);
                    if (at >= 0) { parts.push(text.slice(0, at), h("span.sm-diff.is-code", null, m)); text = text.slice(at + m.length); }
                });
                call.append(...parts, text);
            } else {
                call.append(text);
            }
            add("Call", call);
        }
        const inputs = v.inputs || {};
        const read = [];
        if (inputs.data) read.push("Prepare data v" + (inputs.data.version || 1) + " · " + (inputs.data.file || "the working file") +
            (inputs.data.at ? " as prepared at " + hhmm(inputs.data.at) : ""));
        Object.entries(inputs.columns || {}).forEach(([col, info]) => read.push(info.step + " v" + (info.version || "?") + " · " + col));
        add("Inputs", read.length ? h("span", null, read.join("; ")) : null);
        add("Result", v.result ? h("span", null, v.result) : null);
        if (v.error && v.status !== "done") add("Why", h("span", null, v.error.message || ""));
        if (v.current) add("Kept in", h("span", null, "the working file, as the current version"));
        else if (v.kept) add("Kept at", h("span.sm-kv-code", null, tildePath(v.kept) + (v.bytes ? " · " + size(v.bytes) : "")));
        else if (v.status === "done") add("Kept", h("span.sm-muted", null, "Settings only; its result was not kept"));
        return h("div.sm-history-detail", null,
            h("div.sm-history-detail-head", null,
                h("span.sm-history-detail-v", null, "v" + v.version),
                h("span.sm-adv-note", null, when(v.started_at) + " · ran on this computer" + (v.software ? " · Scimap Pro " + v.software : "")),
                h("span.sm-spacer"), ...actions),
            grid);
    }

    function compare(cell, ctx, c) {
        const head = (side) => "v" + side.version + (side.current ? " · current" : "");
        const table = h("div.sm-compare-table");
        table.append(h("div.sm-compare-row.is-head", null, h("span"), h("span", null, head(c.left)), h("span", null, head(c.right))));
        c.settings.forEach((r) => table.append(h("div.sm-compare-row", null, h("span.sm-muted" + (r.code ? ".sm-mono" : ""), null, r.label),
            h("span" + (r.differs ? ".is-diff" : "") + (r.code ? ".sm-mono" : ""), null, r.left),
            h("span" + (r.differs ? ".is-diff" : "") + (r.code ? ".sm-mono" : ""), null, r.right))));
        if (c.left.result || c.right.result) {
            table.append(h("div.sm-compare-row", null, h("span.sm-muted", null, "Result"),
                h("span", null, c.left.result || "—"), h("span", null, c.right.result || "—")));
        }
        if (c.agreement) {
            table.append(h("div.sm-compare-row.is-wide", null, h("span.sm-muted", null, "Agreement"),
                h("span", null, c.agreement.ari.toFixed(2) + " adjusted Rand index over all " + n(c.agreement.cells) + " cells ",
                    h("span.sm-muted", { style: { marginLeft: "4px" } }, "· 1 is the same partition"))));
        }
        const other = c.left.current ? c.right : c.left;
        const tool = ctx.tools[cell.tool];
        let note = null;
        if (!other.current && c.label) {
            note = h("div.sm-compare-note", null, icon("info", "sm"), h("span", null,
                "Making v" + other.version + " current puts its result back into ", h("code", null, c.label),
                " from its saved copy; nothing is recomputed." + (c.outdated.length
                    ? " " + list(c.outdated) + (c.outdated.length === 1 ? " reads" : " read") + " that column, so " +
                      (c.outdated.length === 1 ? "it shows" : "they show") + " Outdated until you rerun " +
                      (c.outdated.length === 1 ? "it. Its result is kept." : "them. Their results are kept.")
                    : " No other step reads it.")));
        }
        return h("div.sm-compare", null,
            h("div.sm-compare-head", null, h("span.sm-compare-title", null, "Compare v" + c.left.version + " and v" + c.right.version),
                h("span.sm-adv-note", null, "the two checked versions of " + tool.name)),
            table, note);
    }

    function list(items) {
        if (items.length <= 1) return items.join("");
        return items.slice(0, -1).join(", ") + " and " + items[items.length - 1];
    }

    return { panel };
})();
