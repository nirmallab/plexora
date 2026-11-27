// One notebook step ("cell"), in every state the design draws.
//
// A step is rendered from its server view (state, run, chips, choices) plus a
// small local state the page keeps per step (is its form open, which image a
// heatmap shows, the lasso). The workspace re-renders a step only when its
// server view changes, so typing in a field never loses focus to a poll.
//
// Every step carries the TV button in its head: look at this step's result
// on the tissue. It is there in every state that has something to show, and
// says why when it has not.
window.ScimapCells = (function () {
    "use strict";
    const { h, icon, button, iconButton, n, pct, seconds, clock, tildePath } = window.ScimapDom;
    const P = () => window.ScimapPlots;

    // What a beginner does next, after each step (the "Suggested next" row).
    const NEXT = {
        prepare: ["quality", "cluster", "celltypes"],
        quality: ["cluster", "celltypes"],
        celltypes: ["composition", "distance", "neighborhoods"],
        cluster: ["markers", "umap", "composition"],
        markers: ["name_groups", "expression"],
        name_groups: ["composition", "distance", "neighborhoods"],
        umap: ["markers", "name_groups"],
        composition: ["compare", "distance"],
        expression: ["name_groups", "composition"],
        distance: ["interaction", "neighborhoods"],
        interaction: ["neighborhoods", "distance"],
        neighborhoods: ["name_groups", "compare"],
        compare: ["composition"],
    };

    const LABEL_TOOLS = new Set(["cluster", "name_groups", "neighborhoods", "celltypes"]);
    const NAMES_TOOL = "name_groups";

    // A label column wide enough for the longest name, within reason: an id
    // column of "0".."11" stays narrow, "CD4 T cells" is read whole.
    function labelWidth(labels, min, max) {
        const longest = Math.max(0, ...labels.map((l) => String(l).length));
        return Math.max(min || 40, Math.min(max || 140, Math.round(longest * 6.6 + 22)));
    }

    function num(i) {
        return String(i).padStart(2, "0");
    }

    function statePill(state, cell) {
        const map = {
            done: ["Completed", "done", true],
            running: ["Running", "running", true],
            queued: ["Queued", "queued", true],
            failed: ["Failed", "failed", true],
            outdated: ["Input changed", "outdated", true],
            cancelled: ["Cancelled", "cancelled", false],
            idle: ["Not run", "idle", false],
        };
        const [text, kind, dot] = map[state] || map.idle;
        let label = text;
        if (state === "idle" && cell && cell.draftText) label = cell.draftText;
        return h("span.sm-state.sm-state-" + kind + (dot ? "" : ".is-plain"), null,
            dot ? h("span.sm-dot") : null, label === "Draft" ? icon("file", "xs") : null, label);
    }

    // -- the head ------------------------------------------------------------

    function head(cell, ctx, opts) {
        const tool = ctx.tools[cell.tool];
        const run = shownRun(cell);
        const label = run && run.effective ? run.effective.label : null;
        const local = ctx.local(cell.id);
        const parts = [
            h("span.sm-cell-num", null, num(ctx.index(cell.id))),
            icon(tool.icon),
            h("h2.sm-cell-title", null, tool.name),
        ];
        if (opts.settings) {
            // Settings take the step: the head says so and offers the way back.
            parts.push(h("span.sm-cell-sub", null, cell.tool === "prepare" ? "Settings · the projects, their samples and the working file"
                : "Settings · changes make a new version when you run"));
            const cog = iconButton("cog", "Close settings", (e) => { e.stopPropagation(); openSettings(cell, ctx); });
            cog.classList.add("sm-cog", "is-on");
            cog.setAttribute("aria-pressed", "true");
            parts.push(h("span.sm-spacer"), cog);
            parts.push(button("Close settings", { icon: "x", class: "sm-btn-sm",
                onclick: () => { local.settings = false; ctx.rerender(cell.id, true); } }));
            return h("div.sm-cell-head", null, parts);
        }
        if (label && cell.tool !== "prepare" && cell.tool !== "composition" && cell.tool !== "expression") {
            parts.push(h("span.sm-code-chip", null, label));
        }
        if (cell.tool === "prepare" && ctx.work.exists) parts.push(h("span.sm-code-chip", null, ctx.work.file));
        // The version chip: this step's history.
        if (cell.versions && cell.tool !== "prepare") {
            const current = cell.version || cell.versions;
            parts.push(h("button.sm-version" + (local.history ? ".is-open" : ""), { type: "button",
                "aria-label": "History, version " + current + " of " + cell.versions, "aria-expanded": String(!!local.history),
                title: "History", onclick: (e) => { e.stopPropagation(); local.history = !local.history; if (local.history && cell.collapsed) ctx.op({ op: "set_collapsed", id: cell.id, collapsed: false }); ctx.rerender(cell.id, true); } },
            "v" + current, icon(local.history ? "chevD" : "history", "xs")));
        }
        // The settings in short; where the head has no room it gives way, and
        // the title still says it on hover.
        parts.push(h("span.sm-cell-sub", { "data-tooltip": opts.sub && opts.sub.length > 30 ? opts.sub : null }, opts.sub || ""));
        if (opts.sub) parts[2].setAttribute("data-tooltip", opts.sub);
        // The TV: view this step in the image viewer -- until the step has a
        // result, whose footer row offers View in tissue (one action, once).
        const viewable = canView(cell, ctx);
        const hasFoot = Boolean(run) && cell.tool !== "prepare" && ["done", "outdated"].includes(cell.state);
        const tv = hasFoot ? null : h("button.sm-icon-btn.sm-tv", {
            type: "button",
            "aria-label": "View in the image viewer",
            title: viewable.ok ? "View in the image viewer" : viewable.why,
            disabled: !viewable.ok || null,
            onclick: () => ctx.viewInViewer(cell.id, {}),
        }, icon("tv"));
        if (tv) parts.push(tv);
        if (cell.state !== "running" && cell.state !== "queued" && (cell.run || cell.tool === "prepare")) {
            const cog = iconButton("cog", "Settings", (e) => { e.stopPropagation(); openSettings(cell, ctx); });
            cog.classList.add("sm-cog");
            parts.push(cog);
        }
        let pill = statePill(cell.state, opts);
        if (run && ["done", "outdated"].includes(cell.state)) {
            // The state is also the way to when, where and with what it ran
            // (how long it took is there, not in the head).
            const shown = pill;
            const took = run.seconds !== undefined && run.seconds !== null ? " · took " + seconds(run.seconds) : "";
            pill = h("button.sm-state-btn", { type: "button", "aria-label": "Run details", "data-tooltip": "Run details" + took,
                onclick: (e) => { e.stopPropagation(); runDetails(e.currentTarget, run, ctx); } }, shown);
        }
        parts.push(pill);
        if (opts.progress) {
            // Annotation's own progress, apart from the step's state.
            const total = local.total || 0;
            const done = Object.values((cell.params || {}).names || {}).filter((v) => String(v).trim()).length;
            local.ring = window.ScimapUi.ring(done, total);
            local.ringText = h("span", null, done + " / " + (total || "–") + " labeled");
            parts.push(h("span.sm-head-rule"), h("span.sm-progress-pill", null, local.ring.node, local.ringText));
        }
        if (cell.state === "queued") {
            parts.push(button("Remove from queue", { onclick: () => ctx.cancel(cell.id) }));
        } else {
            parts.push(iconButton(cell.collapsed ? "chevR" : "chevD",
                cell.collapsed ? "Expand step" : "Collapse step",
                () => ctx.op({ op: "set_collapsed", id: cell.id, collapsed: !cell.collapsed })));
        }
        if (cell.state !== "queued") {
            const menu = iconButton("dots", "Step menu", (e) => stepMenu(e.currentTarget, cell, ctx));
            parts.push(menu);
        }
        return h("div.sm-cell-head", null, parts);
    }

    /** When, where and with what a run ran: behind the (i) in the head, not
     *  in every footer. */
    function runDetails(anchor, run, ctx) {
        const open = document.querySelector(".sm-run-pop");
        if (open) { open.remove(); if (open.dataset.for === run.run_id) return; }
        const sw = run.software || {};
        const when = run.finished_at || run.started_at;
        const rows = [
            ["Ran", (when ? new Date(when * (when < 1e12 ? 1000 : 1)).toLocaleString("en-US", { month: "short", day: "numeric",
                hour: "2-digit", minute: "2-digit" }) + " · " : "") + seconds(run.seconds)],
            ["Where", run.node ? "data node " + run.node : "this computer"],
            sw.scimappro ? ["Scimap Pro", sw.scimappro] : null,
            sw.plexora ? ["Plexora", sw.plexora] : null,
            sw.python ? ["Python", sw.python] : null,
            run.effective && run.effective.seed !== undefined && run.effective.seed !== null ? ["Seed", String(run.effective.seed)] : null,
            run.effective && run.effective.label ? ["Writes", run.effective.label] : null,
        ].filter(Boolean);
        const pop = h("div.sm-run-pop", { role: "dialog", "aria-label": "Run details" },
            rows.map(([k, v]) => h("div.sm-run-pop-row", null, h("span", null, k), h("span", null, v))));
        pop.dataset.for = run.run_id;
        const root = anchor.closest(".sm-workspace") || document.body;
        root.append(pop);
        const r = anchor.getBoundingClientRect();
        const box = root.getBoundingClientRect();
        pop.style.top = (r.bottom - box.top + root.scrollTop + 6) + "px";
        pop.style.left = Math.max(8, r.right - box.left - pop.offsetWidth) + "px";
        const away = (e) => {
            if (pop.contains(e.target) || anchor.contains(e.target)) return;
            pop.remove();
            document.removeEventListener("pointerdown", away, true);
        };
        setTimeout(() => document.addEventListener("pointerdown", away, true), 0);
    }

    function openSettings(cell, ctx) {
        const local = ctx.local(cell.id);
        // The cog toggles: a second press puts the step back.
        local.settings = !local.settings;
        local.history = false;
        if (local.settings && cell.collapsed) ctx.op({ op: "set_collapsed", id: cell.id, collapsed: false });
        ctx.rerender(cell.id, true);
    }

    // Prepare goes last (every other step reads its file); a running step is
    // stopped first.
    function canDelete(cell, ctx) {
        if (cell.state === "running" || cell.state === "queued") return false;
        return !(cell.tool === "prepare" && ctx.nb.cells.length > 1);
    }

    function canView(cell, ctx) {
        if (!ctx.work.exists) return { ok: false, why: "Prepare the data first" };
        if (cell.tool === "prepare") return { ok: true };
        if (!shownRun(cell)) return { ok: false, why: "Run this step first" };
        return { ok: true };
    }

    function shownRun(cell) {
        const run = cell.run || {};
        if (run.status === "done") return run;
        if (run.previous && run.previous.status === "done") return run.previous;
        return null;
    }

    function stepMenu(anchor, cell, ctx) {
        const items = [];
        const run = shownRun(cell);
        if (cell.state !== "running") {
            items.push({ label: run ? "Run again" : "Run", icon: "play", onSelect: () => ctx.run(cell.id) });
        }
        if (cell.state !== "running" && cell.state !== "queued") {
            items.push({ label: "Settings", icon: "cog", onSelect: () => openSettings(cell, ctx) });
        }
        if (cell.versions && cell.tool !== "prepare") {
            items.push({ label: "History", icon: "history", onSelect: () => { ctx.local(cell.id).history = true; ctx.rerender(cell.id, true); } });
        }
        items.push({ label: "View in the image viewer", icon: "tv", disabled: !canView(cell, ctx).ok,
                     onSelect: () => ctx.viewInViewer(cell.id, {}) });
        items.push({ sep: true });
        items.push({ label: "Move up", icon: "up", disabled: ctx.index(cell.id) <= 1,
                     onSelect: () => ctx.op({ op: "move_cell", id: cell.id, to: ctx.index(cell.id) - 2 }) });
        items.push({ label: "Move down", icon: "down", disabled: ctx.index(cell.id) >= ctx.nb.cells.length,
                     onSelect: () => ctx.op({ op: "move_cell", id: cell.id, to: ctx.index(cell.id) }) });
        items.push({ label: cell.collapsed ? "Expand" : "Collapse", icon: cell.collapsed ? "expand" : "collapse",
                     onSelect: () => ctx.op({ op: "set_collapsed", id: cell.id, collapsed: !cell.collapsed }) });
        items.push({ label: "Duplicate", icon: "copy",
                     onSelect: () => ctx.addTool(cell.tool, cell.params, cell.id) });
        items.push({ sep: true });
        items.push({ label: "Delete step", icon: "trash", danger: true, hint: "D D", disabled: !canDelete(cell, ctx),
                     onSelect: () => ctx.deleteStep(cell.id) });
        ctx.menu(anchor, items);
    }

    // -- fields ----------------------------------------------------------------

    // A field shown only for one choice of another ("Resolution" for Leiden)
    // reads that other field's value, or its default when it was never set.
    function visible(field, values, fields) {
        if (!field.show_if) return true;
        return Object.entries(field.show_if).every(([k, v]) => {
            const other = (fields || []).find((f) => f.name === k);
            return (values[k] ?? (other ? other.default : null)) === v;
        });
    }

    function fieldControl(field, cell, ctx, values, refresh) {
        const set = (value, immediate) => {
            values[field.name] = value;
            ctx.setParams(cell.id, { [field.name]: value }, immediate);
            if (field.kind === "segmented" || field.kind === "column") refresh();
        };
        const value = values[field.name] ?? field.default;
        switch (field.kind) {
        case "segmented":
            return h("div.sm-seg", { role: "radiogroup", "aria-label": field.label },
                field.choices.map(([v, text]) => h("button", {
                    type: "button", role: "radio", "aria-checked": String(value === v),
                    onclick: () => set(v, true),
                }, text)));
        case "slider": {
            // Core's one slider (views/slider.js): rail, fill, thumb and a
            // typeable number, drag ticks to onInput, release to onChange.
            if (typeof window.PlexoraSlider === "function") {
                const mount = h("div.sm-slider");
                new window.PlexoraSlider(mount, { min: field.min, max: field.max, step: field.step,
                    value: Number(value), label: field.label,
                    onInput: (v) => set(Number(v)), onChange: (v) => set(Number(v), true) });
                return h("div.sm-form-control", { style: { maxWidth: "440px" } }, mount);
            }
            const out = h("input.sm-input.is-number", { "aria-label": field.label, value: String(value),
                style: { width: "64px" }, inputmode: "decimal" });
            const range = h("input.sm-range", { type: "range", min: field.min, max: field.max, step: field.step,
                value: String(value), "aria-label": field.label });
            range.addEventListener("input", () => { out.value = range.value; set(Number(range.value)); });
            out.addEventListener("change", () => {
                const v = Math.min(field.max, Math.max(field.min, Number(out.value) || field.default));
                out.value = String(v);
                range.value = String(v);
                set(v, true);
            });
            return h("div.sm-form-control", { style: { maxWidth: "440px" } }, range, out);
        }
        case "number":
        case "seed": {
            const input = h("input.sm-input.is-number", { type: "number", "aria-label": field.label,
                value: String(value ?? ""), min: field.min ?? null, max: field.max ?? null, step: field.step ?? 1,
                style: { width: field.kind === "seed" ? "80px" : "96px" } });
            input.addEventListener("change", () => set(input.value === "" ? null : Number(input.value), true));
            return h("div.sm-form-control", null, input,
                field.unit ? h("span.sm-form-unit", null, unitText(field, ctx, Number(input.value))) : null);
        }
        case "label": {
            const input = h("input.sm-input.is-mono", { "aria-label": field.label, value: values.label || "",
                placeholder: ctx.defaultLabel(cell) || "", style: { width: "280px" }, spellcheck: "false" });
            input.addEventListener("change", () => set(input.value.trim() || null, true));
            return input;
        }
        case "column": {
            const choices = (cell.choices && cell.choices[field.name]) || [];
            if (!choices.length) return h("span.sm-form-help", null, "Nothing to choose yet");
            const fallback = (cell.choice_defaults && cell.choice_defaults[field.name]) || choices[choices.length - 1];
            const current = value && choices.includes(value) ? value : (field.optional ? "" : fallback);
            const select = h("select.sm-select.is-mono", { "aria-label": field.label, style: { minWidth: "220px" } },
                field.optional ? h("option", { value: "" }, "Automatic") : null,
                choices.map((c) => h("option", { value: c, selected: c === current || null }, c)));
            select.addEventListener("change", () => set(select.value || null, true));
            return select;
        }
        case "layer": {
            const layers = (ctx.work && ctx.work.layers) || [];
            const label = (l) => l === "raw" ? "raw (logged on the fly)" : l === "X" ? "X" + (ctx.work.x_logged ? " (logged)" : " (logged on the fly)") : l;
            const select = h("select.sm-select.is-mono", { "aria-label": field.label, style: { minWidth: "220px" } },
                h("option", { value: "" }, "Automatic"),
                layers.map((l) => h("option", { value: l, selected: l === value || null }, label(l))));
            select.addEventListener("change", () => set(select.value || null, true));
            return select;
        }
        case "markers":
            return markersControl(field, cell, ctx, values, set);
        case "toggle": {
            const sw = window.ScimapUi.toggle({ on: Boolean(value), label: field.label, onChange: (on) => set(on, true) });
            return sw.node;
        }
        case "text": {
            const input = h("input.sm-input", { type: "text", "aria-label": field.label, value: value || "",
                placeholder: field.placeholder || "", style: { width: "220px" }, spellcheck: "false" });
            input.addEventListener("change", () => set(input.value.trim() || null, true));
            return input;
        }
        default:
            return null;
        }
    }

    function unitText(field, ctx, value) {
        const sizes = Object.values((ctx.work && ctx.work.pixel_size) || {}).filter(Boolean);
        if (field.unit === "px" && sizes.length && sizes.every((s) => s === sizes[0]) && value) {
            return "px · " + (value * sizes[0]).toFixed(1) + " µm at " + sizes[0] + " µm/px";
        }
        return field.unit;
    }

    const NUCLEAR = /(dna|dapi|hoechst|nucle|^h3$|draq|sytox)/i;

    function markersControl(field, cell, ctx, values, set) {
        const all = (ctx.work && ctx.work.markers) || [];
        const chosen = values[field.name] && values[field.name].length ? values[field.name]
            : all.filter((m) => !NUCLEAR.test(m));
        const left = all.length - chosen.length;
        const summary = h("span", { style: { fontSize: "13px", color: "var(--sm-ink)" } }, chosen.length + " of " + all.length);
        const out = all.filter((m) => !chosen.includes(m));
        const note = h("span.sm-form-help", null,
            values[field.name] && values[field.name].length ? (out.length && out.length <= 3 ? out.join(", ") + " left out" : "chosen by hand")
                : (left ? (out.length <= 3 ? out.join(", ") + " left out" : "nuclear and background channels left out") : "every marker"));
        const choose = button("Choose", {
            onclick: (e) => ctx.chooseMarkers(e.currentTarget, all, chosen, (picked) => {
                set(picked.length === all.filter((m) => !NUCLEAR.test(m)).length &&
                    picked.every((m) => !NUCLEAR.test(m)) ? null : picked, true);
                ctx.rerender(cell.id);
            }),
        });
        return h("div.sm-form-control", null, summary, note, choose);
    }

    function form(cell, ctx, opts) {
        const tool = ctx.tools[cell.tool];
        const local = ctx.local(cell.id);
        const values = Object.assign({}, cell.params || {});
        const wrap = h("div");
        const refresh = () => {
            wrap.innerHTML = "";
            wrap.append(...build());
        };
        const build = () => {
            const common = tool.fields.filter((f) => !f.group && !["mapping", "groups", "rules"].includes(f.kind) && visible(f, values, tool.fields));
            const advanced = tool.fields.filter((f) => f.group === "advanced" && visible(f, values, tool.fields));
            const out = [];
            if (tool.fields.some((f) => f.kind === "rules")) out.push(rulesEditor(cell, ctx, values));
            if (common.length) {
                out.push(h("div.sm-form", null, common.map((f) => [
                    h("span.sm-form-label", null, f.label),
                    h("div.sm-form-control", null, fieldControl(f, cell, ctx, values, refresh),
                        f.help && f.kind !== "segmented" ? h("span.sm-form-help", null, f.help) : null),
                ])));
            }
            if (advanced.length) {
                // The form's own less-used fields sit with the rest: "Advanced"
                // is the free-form box below, for anything the form does not ask.
                out.push(h("div.sm-form", { style: { paddingTop: "0" } }, advanced.map((f) => [
                    h("span.sm-form-label", null, f.label),
                    h("div.sm-form-control", null, fieldControl(f, cell, ctx, values, refresh),
                        f.help && f.kind !== "segmented" ? h("span.sm-form-help", null, f.help) : null),
                ])));
            }
            return out;
        };
        refresh();
        const row = runRow(cell, ctx, Object.assign({ runLabel: cell.run ? "Run as v" + (cell.next_version || 2) : "Run" }, opts));
        const blocked = (text) => {
            const btn = row.querySelector(".sm-btn-primary");
            const hint = row.querySelector(".sm-hint");
            if (btn) btn.disabled = !!text;
            if (hint) {
                hint.textContent = text || (opts && opts.hint) || "Runs on this computer, one step at a time";
                hint.classList.toggle("is-bad", !!text);
            }
        };
        const adv = window.ScimapAdvanced ? window.ScimapAdvanced.box(cell, ctx, blocked) : null;
        if (window.ScimapAdvanced) setTimeout(() => blocked(window.ScimapAdvanced.blockedText(cell, ctx)), 0);
        return [wrap, adv, row];
    }

    function runRow(cell, ctx, opts) {
        const o = opts || {};
        return h("div.sm-run-row", null,
            button(o.runLabel || "Run", { kind: "primary", icon: "play", onclick: () => ctx.run(cell.id),
                disabled: o.runDisabled || null }),
            o.secondary || button("Reset to defaults", { kind: "quiet", onclick: () => ctx.op({ op: "reset_params", id: cell.id }) }),
            null,
            h("span.sm-spacer"),
            h("span.sm-hint", null, o.hint || "Runs on this computer, one step at a time"));
    }

    // -- annotate / merge -------------------------------------------------------

    // The strip over an annotate or merge step: which clusters it names, an
    // arrow, and the new column it writes (named here, or the default).
    function configStrip(cell, ctx, source, choices) {
        const U = window.ScimapUi;
        const field = U.field({ label: "Groups to name", onOpen: (anchor) => U.pickList(anchor, {
            title: "Groups to name", noun: "columns", items: choices, value: source,
            onChange: (v) => {
                ctx.setParams(cell.id, { source: v, names: {}, groups: [] }, true);
                ctx.rerender(cell.id);
            } }) });
        field.set(source || "Choose groups");
        field.node.classList.add("is-mono");
        const own = ((cell.run || {}).effective || {}).label;
        const fallback = own || (source ? source + "_named" : "");
        const input = h("input.sm-ui-text-input", { type: "text", value: cell.params.label || "", placeholder: fallback,
            spellcheck: "false", autocomplete: "off", "aria-label": "Output column" });
        const wrap = h("label.sm-ui-text.is-mono", { "data-tooltip": "The new column. Empty: " + fallback }, input, icon("pen", "xs"));
        const save = () => {
            const v = input.value.trim();
            const bad = v && (!/^[A-Za-z_][A-Za-z0-9_.-]*$/.test(v) || v === source);
            wrap.classList.toggle("is-bad", Boolean(bad));
            wrap.setAttribute("data-tooltip", bad ? (v === source ? "Must differ from the groups it names" :
                "Letters, digits, _ . - ; starting with a letter") : "The new column. Empty: " + fallback);
            if (!bad && v !== (cell.params.label || "")) ctx.setParams(cell.id, { label: v });
        };
        input.addEventListener("change", save);
        input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); input.blur(); } });
        return h("div.sm-strip", null,
            h("span.sm-strip-label", null, icon("layers", "sm"), h("span", null, "Groups")),
            h("div.sm-strip-field", null, field.node),
            h("span.sm-strip-arrow", { "aria-hidden": "true" }, icon("back", "sm")),
            h("span.sm-strip-label", null, icon("tag", "sm"), h("span", null, "Output column")),
            h("div.sm-strip-field", null, wrap));
    }

    function mappingBody(cell, ctx) {
        const local = ctx.local(cell.id);
        const choices = (cell.choices && cell.choices.source) || [];
        const fallback = (cell.choice_defaults && cell.choice_defaults.source) || choices[0];
        const source = (cell.params.source && choices.includes(cell.params.source)) ? cell.params.source : fallback;
        const box = h("div.sm-annot");
        box.append(configStrip(cell, ctx, source, choices));
        const table = h("div.sm-annot-body", null, h("p.sm-annot-wait", null, "Reading " + (source || "") + "…"));
        box.append(table);
        if (!source) {
            table.textContent = "";
            return [box];
        }
        ctx.column(source).then((summary) => {
            table.textContent = "";
            table.append(nameTable(cell, ctx, summary, local, source));
        }).catch((err) => { table.textContent = err.message; });
        return [box];
    }

    // Every label typed in this notebook's annotate steps: what a cell-type
    // field suggests, so one name is reused across clusters and steps.
    function knownLabels(ctx, names) {
        const seen = new Set();
        const add = (v) => { const t = String(v || "").trim(); if (t) seen.add(t); };
        Object.values(names).forEach(add);
        (ctx.nb.cells || []).forEach((c) => {
            if (c.tool === NAMES_TOOL) Object.values((c.params || {}).names || {}).forEach(add);
        });
        return Array.from(seen).sort((a, b) => a.localeCompare(b));
    }

    function nameTable(cell, ctx, summary, local, source) {
        const U = window.ScimapUi;
        const names = Object.assign({}, cell.params.names || {});
        const colorOf = ctx.colorOf(source);
        const groups = summary.groups || [];
        const limit = local.showAll ? groups.length : 12;
        const shown = groups.slice(0, limit);
        const producer = (ctx.nb.cells || []).find((c) => { const r = shownRun(c); return r && r.effective && r.effective.label === source; });
        const labeled = () => groups.filter((g) => String(names[g.id] || "").trim()).length;
        const paintProgress = () => {
            local.total = groups.length;
            if (local.ring) local.ring.set(labeled(), groups.length);
            if (local.ringText) local.ringText.textContent = labeled() + " / " + groups.length + " labeled";
            clear.disabled = !labeled() && !local.cleared;
        };
        const combos = [];
        const statusOf = (row, g) => {
            const on = Boolean(String(names[g.id] || "").trim());
            row.classList.toggle("is-labeled", on);
            const st = row.querySelector(".sm-annot-status");
            st.querySelector(":scope > span").textContent = on ? "Labeled" : "Unlabeled";
        };
        const rows = shown.map((g, i) => {
            const markers = (summary.top_markers || [])[groups.indexOf(g)] || [];
            const row = h("div.sm-annot-row", { role: "row" });
            const combo = U.combo({ value: names[g.id] || "", placeholder: "Enter a name", label: "Name for " + g.id,
                suggest: () => knownLabels(ctx, names),
                onInput: (v) => { names[g.id] = v; statusOf(row, g); paintProgress(); },
                onCommit: () => ctx.setParams(cell.id, { names: Object.assign({}, names) }),
                onNav: (d) => { const next = combos[i + d]; if (next) next.focus(); } });
            combos.push(combo);
            const eye = h("button.sm-annot-eye" + (local.preview === g.id ? ".is-on" : ""), { type: "button",
                "aria-pressed": String(local.preview === g.id), disabled: !producer || null,
                "data-tooltip": producer ? (local.preview === g.id ? "Hide the preview" : "Where " + g.id + " is in the tissue")
                    : "This column came with the data: no tissue map here",
                onclick: () => { local.preview = local.preview === g.id ? null : g.id; ctx.rerender(cell.id); } }, icon("eye", "sm"));
            row.append(
                h("span.sm-annot-id", { role: "cell" }, h("span.sm-annot-dot", { style: { background: colorOf(g.id, groups.indexOf(g)) } }), h("b", null, g.id)),
                h("span.sm-annot-n", { role: "cell" }, n(g.count)),
                h("span.sm-annot-markers", { role: "cell" }, markers.slice(0, 3).map((m) => h("span.sm-marker-chip", null, m)),
                    markers.length ? null : h("span.sm-muted", null, "—")),
                h("span.sm-annot-label", { role: "cell" }, combo.node),
                h("span.sm-annot-preview", { role: "cell" }, eye),
                h("span.sm-annot-status", { role: "cell" }, h("i", { "aria-hidden": "true" }, icon("check", "xs")), h("span")));
            statusOf(row, g);
            const out = [row];
            if (local.preview === g.id && producer) out.push(previewRow(cell, ctx, local, producer, g));
            return out;
        });
        const head = h("div.sm-annot-row.is-head", { role: "row" },
            h("span", { role: "columnheader" }, icon("cluster", "xs"), "Group"),
            h("span.is-num", { role: "columnheader" }, icon("table", "xs"), "Cells"),
            h("span.sm-annot-markers", { role: "columnheader" }, icon("bars", "xs"), "Top markers",
                h("span.sm-ui-info", { tabindex: "0", "data-tooltip": "The markers highest in each cluster against the others" }, icon("info", "xs"))),
            h("span", { role: "columnheader" }, icon("tag", "xs"), "Name"),
            h("span.sm-annot-preview", { role: "columnheader" }, icon("eye", "xs"), "Preview"),
            h("span.sm-annot-status", { role: "columnheader" }, icon("check", "xs"), "Status"));
        const save = button("Save names", { kind: "primary", icon: "play", onclick: () => ctx.run(cell.id) });
        const clear = button(local.cleared ? "Undo clear" : "Clear labels", { icon: "undo", onclick: () => {
            if (local.cleared) {
                const back = local.cleared;
                local.cleared = null;
                ctx.setParams(cell.id, { names: back }, true);
            } else {
                local.cleared = Object.assign({}, names);
                ctx.setParams(cell.id, { names: {} }, true);
                clearTimeout(local.clearTimer);
                local.clearTimer = setTimeout(() => { local.cleared = null; ctx.rerender(cell.id); }, 10000);
            }
            ctx.rerender(cell.id);
        } });
        clear.setAttribute("data-tooltip", local.cleared ? "Put the labels back" : "Empty every label (undo for 10 s)");
        const more = groups.length > limit ? h("button.sm-btn-link.sm-annot-more", { type: "button",
            onclick: () => { local.showAll = true; ctx.rerender(cell.id); } }, "Show all " + groups.length + " groups") : null;
        const out = h("div.sm-annot-table", { role: "table", "aria-label": "Group names" }, head, rows.flat(), more,
            h("div.sm-annot-foot", null, save, clear, h("span.sm-annot-rule"),
                h("span.sm-annot-note", null, icon("info", "xs"), "The same name on several groups joins them. Writes a new column; ",
                    h("span.sm-mono", null, source), " stays as it is.")));
        paintProgress();
        return out;
    }

    // Where one cluster is: the producing step's tissue map, the other
    // clusters in grey. Drawn once per image, restyled per cluster.
    function previewRow(cell, ctx, local, producer, g) {
        const host = h("div.sm-annot-preview-plot", null, h("p.sm-figure-wait", null, "Drawing the tissue…"));
        const images = ((ctx.work || {}).images || []).map(String);
        const image = local.previewImage && images.includes(local.previewImage) ? local.previewImage : images[0];
        const pick = images.length > 1 ? window.ScimapUi.segmented({ label: "Image", value: image,
            choices: images.slice(0, 4).map((v) => ({ value: v, label: v })),
            onChange: (v) => { local.previewImage = v; ctx.rerender(cell.id); } }) : null;
        const row = h("div.sm-annot-previewrow", null,
            h("div.sm-annot-preview-head", null, h("strong", null, "Cluster " + g.id), h("span.sm-muted", null, image ? "in " + image : ""),
                pick ? pick.node : null),
            host);
        ctx.drawView(producer.id, "tissue", image ? { image } : {}).then((entry) => {
            host.textContent = "";
            if (!entry || entry.error || !entry.figure) { host.append(h("p.sm-figure-error", null, (entry && entry.error) || "No tissue map")); return; }
            const fig = { ...entry, figure: { data: entry.figure.data, layout: { ...entry.figure.layout, showlegend: false, height: 300 } } };
            return window.ScimapFigures.mount(host, fig, "annot:" + producer.id + ":" + (image || "") + ":" + g.id).then((div) => {
                const traces = (div.data || []).map((t, i) => [i, String(t.name)]);
                const colors = traces.map(([i, name]) => (name === String(g.id) ? undefined : "#d3d9e2"));
                const idx = traces.filter((_, k) => colors[k]).map(([i]) => i);
                if (idx.length && window.Plotly) window.Plotly.restyle(div, { "marker.color": idx.map(() => "#d3d9e2"), "marker.opacity": idx.map(() => 0.55) }, idx);
            });
        }).catch((err) => { host.textContent = err.message; });
        return row;
    }

    // -- results ---------------------------------------------------------------

    function headline(cell, body, ctx) {
        const tool = cell.tool;
        const parts = [];
        if (body.headline) {
            parts.push(h("p", null, h("strong", null, body.headline.strong), body.headline.rest || ""));
        } else if (body.groups && LABEL_TOOLS.has(tool)) {
            const noun = tool === "neighborhoods" ? "neighborhoods" : tool === "cluster" ? "clusters" : tool === "celltypes" ? "cell types" : "groups";
            parts.push(h("p", null, h("strong", null, body.groups.length + " " + noun), " across " + n(body.n_cells) +
                " cells. The largest holds " + pct(body.largest_pct) + " of cells, the smallest " + pct(body.smallest_pct) + "."));
        } else if (tool === "composition") {
            parts.push(h("p", null, h("strong", null, body.categories.length + " groups"), " in " +
                body.images.length + (body.images.length === 1 ? " image" : " images") + ", " + n(body.n_cells) + " cells."));
        }
        // What a plot shows is its own title's to say, not a paragraph over
        // every view of the step.
        if (body.unit && body.unit.text) {
            const descriptive = body.unit.n === 1 ? " Descriptive for this sample; no comparison across samples is implied." : "";
            parts.push(h("p.sm-unit", null, body.unit.text + "." + descriptive));
        }
        return parts.length ? h("div.sm-headline", null, parts) : null;
    }

    // -- results: the views of the step's result -------------------------------

    // Every plot is one of Scimap Pro's, drawn from what the run stored; the
    // bar on each switches and narrows it without running the step again
    // (scimapViews.js). A composition's table and a UMAP's embedding are views
    // the page draws itself, so they come in as hooks.
    function resultBody(cell, ctx, body, figs, run) {
        const tool = cell.tool;
        if (tool === "umap") return umapResult(cell, ctx, body, figs, run);
        const hooks = {};
        if (tool === "composition") hooks.table = () => compositionTable(cell, ctx, body);
        else if (body.table) hooks.table = () => plainTable(cell, ctx, body.table);
        const out = [window.ScimapViews.grid(cell, ctx, run, figs, hooks)];
        if (body.mapping && body.groups) {
            const rows = Object.entries(body.mapping).map(([to, from]) => h("tr", null,
                h("td", null, to), h("td.is-muted", null, from.join(", "))));
            out.push(h("div.sm-stack", { style: { maxWidth: "520px" } }, h("span.sm-section-label", null, "Mapping"),
                h("table.sm-table", null, h("thead", null, h("tr", null, h("th", null, "Label"), h("th", null, "From"))), h("tbody", null, rows))));
        }
        setCsv(cell, ctx, body);
        return h("div.sm-result-body", null, out);
    }

    // Every cell of the embedding, drawn on the GPU, with a lasso that tests
    // every cell (scimapEmbed.js). The selection is the app's, keyed by
    // project and mask label, so the viewer shows the same cells. Its Annotate
    // panel is in the plot sidebar; the step's view switch sits in its bar.
    function umapResult(cell, ctx, body, figs, run) {
        const local = ctx.local(cell.id);
        local.csv = null;
        const color = (cell.params && cell.params.color) || ((run.effective || {}).display || {}).color || body.color || "";
        let embed = null;
        const hooks = {
            instance: () => embed,
            embedding: (host, extras) => {
                embed = window.ScimapEmbed.mount(host, cell.id + ":" + run.run_id, {
                    notebookId: ctx.nb.id, theme: "light", key: (run.effective || {}).label || "X_umap", color,
                    leading: extras.leading, trailing: extras.trailing,
                    onAnnotate: () => {
                        const panel = window.ScimapViews.firstPanel(cell.id);
                        ctx.inspect(cell.id, panel, "annotate");
                    },
                    onColor: (column) => ctx.setColor(cell.id, column),
                    onViewInTissue: () => {
                        if (window.PlexoraCellSelection) window.PlexoraCellSelection.setMode("only");
                        ctx.viewInViewer(cell.id, {});
                    },
                    onOpenInViewer: (handoff) => ctx.openEmbedding(cell.id, handoff),
                    onLabeled: () => ctx.refreshView(),
                    download: (anchor) => window.ScimapViews.download(anchor, window.ScimapViews.step(cell.id),
                        { id: window.ScimapViews.firstPanel(cell.id), view: "embedding", opts: {} }),
                });
            },
        };
        return h("div.sm-result-body", null, window.ScimapViews.grid(cell, ctx, run, figs, hooks));
    }

    // A step's numbers as a sortable table: `{columns: [{key, label, num}], rows}`.
    function plainTable(cell, ctx, table) {
        const local = ctx.local(cell.id);
        const sort = local.plainSort || null;
        const rows = table.rows.slice();
        if (sort) {
            rows.sort((a, b) => {
                const x = a[sort.i], y = b[sort.i];
                if (x === y) return 0;
                if (x === null || x === undefined) return 1;
                if (y === null || y === undefined) return -1;
                return (typeof x === "number" && typeof y === "number" ? x - y
                    : String(x).localeCompare(String(y), undefined, { numeric: true })) * sort.dir;
            });
        }
        const limit = local.plainAll ? rows.length : 200;
        const fmt = (v) => v === null || v === undefined ? "—" : typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 4 }) : String(v);
        const head = h("tr", null, table.columns.map((c, i) => h("th.is-sortable" + (c.num ? ".is-num" : ""), {
            "aria-sort": sort && sort.i === i ? (sort.dir > 0 ? "ascending" : "descending") : null,
            onclick: () => { local.plainSort = { i, dir: sort && sort.i === i ? -sort.dir : (c.num ? -1 : 1) }; ctx.rerender(cell.id); },
        }, c.label + (sort && sort.i === i ? (sort.dir > 0 ? " ↑" : " ↓") : ""))));
        const body = h("tbody", null, rows.slice(0, limit).map((r) => h("tr", null,
            r.map((v, i) => h("td" + (table.columns[i] && table.columns[i].num ? ".is-num" : ""), null, fmt(v))))));
        return h("div.sm-stack", null, h("div.sm-plain-scroll", null, h("table.sm-table", null, h("thead", null, head), body)),
            rows.length > limit ? h("button.sm-btn-link", { type: "button", onclick: () => { local.plainAll = true; ctx.rerender(cell.id); } },
                "Show all " + n(rows.length) + " rows") : null);
    }

    // Common cell types a panel can name from its markers, for a first set of
    // rules. A marker matches by name, ignoring case and punctuation.
    const STARTERS = [
        { name: "Immune cells", pos: [["CD45"]] },
        { name: "T cells", pos: [["CD3", "CD3E"]] },
        { name: "CD4 T cells", parent: "T cells", pos: [["CD4"]] },
        { name: "CD8 T cells", parent: "T cells", pos: [["CD8", "CD8A"]] },
        { name: "Regulatory T cells", parent: "CD4 T cells", pos: [["FOXP3"]] },
        { name: "B cells", pos: [["CD20", "MS4A1", "CD19"]] },
        { name: "Macrophages", pos: [["CD68", "CD163"]] },
        { name: "Epithelial cells", pos: [["PANCK", "ECAD", "KRT", "CK"]] },
        { name: "Endothelial cells", pos: [["CD31", "PECAM1"]] },
        { name: "Stromal cells", pos: [["SMA", "ASMA", "ACTA2"]] },
    ];

    function starterRules(markers, gated) {
        const key = (m) => String(m).toUpperCase().replace(/[^A-Z0-9]/g, "");
        const byKey = new Map(markers.map((m) => [key(m), m]));
        const usable = (m) => !gated || gated.has(m);
        const rules = [];
        STARTERS.forEach((st) => {
            const pos = st.pos.map((alts) => alts.map((a) => byKey.get(key(a))).find((m) => m && usable(m))).filter(Boolean);
            if (pos.length !== st.pos.length) return;
            const parent = st.parent && rules.some((r) => r.name === st.parent) ? st.parent : "all";
            if (st.parent && parent === "all") return;
            rules.push({ parent, name: st.name, pos, neg: [] });
        });
        // Immune is a parent only when something sits inside it; on its own it
        // would swallow every T and B cell.
        return rules.filter((r) => r.name !== "Immune cells");
    }

    // Cell types from gates: one row per cell type, read top to bottom.
    function rulesEditor(cell, ctx, values) {
        const U = window.ScimapUi;
        const markers = ((ctx.work && ctx.work.markers) || []).filter((m) => !NUCLEAR.test(m));
        const gated = new Set(cell.gated || []);
        const rules = (values.rules || []).map((r) => ({ parent: r.parent || "all", name: r.name || "",
            pos: (r.pos || []).slice(), neg: (r.neg || []).slice() }));
        const save = (soon) => { values.rules = rules; ctx.setParams(cell.id, { rules: rules.map((r) => ({ ...r })) }, soon); };
        const repaint = () => { save(true); ctx.rerender(cell.id, true); };
        const pickField = (rule, key, title) => {
            const f = U.field({ label: title, onOpen: (anchor) => U.pickList(anchor, {
                title, noun: "markers", multiple: true, allowEmpty: true, value: rule[key].slice(),
                items: markers.map((m) => ({ value: m, label: m + (gated.has(m) ? "" : " · no gate") })),
                onChange: (v) => { rule[key] = v.slice(); f.set(v.length ? v.join(", ") : "None", v.length > 1 ? v.length : null); save(true); } }) });
            f.set(rule[key].length ? rule[key].join(", ") : (key === "pos" ? "Choose markers" : "None"), rule[key].length > 1 ? rule[key].length : null);
            return f.node;
        };
        const rows = rules.map((rule, i) => {
            const parents = ["all"].concat(rules.slice(0, i).map((r) => r.name).filter(Boolean));
            const parentField = U.field({ label: "Inside", onOpen: (anchor) => U.pickList(anchor, {
                title: "Inside", noun: "cell types", value: rule.parent,
                items: parents.map((p) => ({ value: p, label: p === "all" ? "All cells" : p })),
                onChange: (v) => { rule.parent = v; repaint(); } }) });
            parentField.set(rule.parent === "all" ? "All cells" : rule.parent);
            const name = h("input.sm-ui-text-input", { type: "text", value: rule.name, placeholder: "Cell type",
                "aria-label": "Cell type name", spellcheck: "false", autocomplete: "off" });
            name.addEventListener("change", () => { rule.name = name.value.trim(); save(true); });
            const missing = rule.pos.concat(rule.neg).filter((m) => !gated.has(m));
            return h("div.sm-rule-row", { role: "row" },
                h("span.sm-rule-cell", { role: "cell" }, parentField.node),
                h("label.sm-ui-text.sm-rule-cell", { role: "cell" }, name),
                h("span.sm-rule-cell", { role: "cell" }, pickField(rule, "pos", "Positive for")),
                h("span.sm-rule-cell", { role: "cell" }, pickField(rule, "neg", "Negative for")),
                h("span.sm-rule-cell.sm-rule-tools", { role: "cell" },
                    missing.length ? h("span.sm-rule-warn", { "data-tooltip": "No gate yet for " + missing.join(", ") + ". Set it in Gating." }, icon("warning", "xs")) : null,
                    iconButton("x", "Remove this row", () => { rules.splice(i, 1); repaint(); })));
        });
        const head = h("div.sm-rule-row.is-head", { role: "row" },
            h("span", { role: "columnheader" }, "Inside"), h("span", { role: "columnheader" }, "Cell type"),
            h("span", { role: "columnheader" }, "Positive for"), h("span", { role: "columnheader" }, "Negative for"), h("span"));
        const add = button("Add a cell type", { icon: "plus", onclick: () => { rules.push({ parent: "all", name: "", pos: [], neg: [] }); repaint(); } });
        const starters = starterRules(markers, gated.size ? gated : null);
        const start = !rules.length && starters.length ? button("Start with common cell types", { kind: "quiet", icon: "tag",
            onclick: () => { rules.push(...starters); repaint(); } }) : null;
        if (start) start.setAttribute("data-tooltip", starters.map((r) => r.name).join(", "));
        return h("div.sm-rules", null,
            h("p.sm-rules-intro", null, "Each row names a cell type by the markers it is positive, or negative, for, using your gates. ",
                "Rows are read top to bottom; a row inside another refines it. A cell no row fits stays Unknown."),
            gated.size ? null : h("p.sm-rules-warn", null, icon("warning", "xs"), " No gates are set yet. Set them in Gating, then come back."),
            rules.length ? h("div.sm-rule-table", { role: "table", "aria-label": "Cell type rules" }, head, rows) : null,
            h("div.sm-rules-actions", null, add, start));
    }

    function compositionTable(cell, ctx, body) {
        const colorOf = ctx.colorOf(body.column);
        const local = ctx.local(cell.id);
        const sort = local.sort || { key: "count", dir: -1 };
        const items = body.categories.map((c, i) => ({ id: c, i, count: body.pooled_counts[i], pct: body.pooled_pcts[i] }));
        items.sort((a, b) => sort.key === "id" ? a.id.localeCompare(b.id, undefined, { numeric: true }) * sort.dir : (a[sort.key] - b[sort.key]) * sort.dir);
        const th = (label, k, isNum) => h("th.is-sortable" + (isNum ? ".is-num" : ""), {
            onclick: () => { local.sort = { key: k, dir: sort.key === k ? -sort.dir : (k === "id" ? 1 : -1) }; ctx.rerender(cell.id); },
            "aria-sort": sort.key === k ? (sort.dir > 0 ? "ascending" : "descending") : null,
        }, label + (sort.key === k ? (sort.dir > 0 ? " ↑" : " ↓") : ""));
        return h("div.sm-stack", { style: { maxWidth: "520px" } },
            h("div.sm-stack-head", null, h("span.sm-section-label", null, "All cells"),
                h("span.sm-section-note", null, body.images.length > 1 ? "pooled over images; cells as the unit, descriptive" : "")),
            h("table.sm-table", null,
                h("thead", null, h("tr", null, th("Group", "id"), th("Cells", "count", true), th("%", "pct", true))),
                h("tbody", null, items.map((it) => h("tr", null,
                    h("td", null, h("span.sm-swatch-id", { style: { fontSize: "12px" } },
                        h("span.sm-swatch", { style: { background: colorOf(it.id, it.i) } }), it.id)),
                    h("td.is-num", null, n(it.count)), h("td.is-num.is-muted", null, pct(it.pct)))))));
    }

    // The numbers behind a step, as CSV.
    function setCsv(cell, ctx, body) {
        const local = ctx.local(cell.id);
        local.csv = null;
        if (cell.tool === "composition" && body.images) {
            local.csv = () => P().csv("composition.csv", ["image"].concat(body.categories),
                body.images.map((img, i) => [img].concat(body.pcts[i])));
        } else if (cell.tool === "distance" && body.matrices) {
            local.csv = () => P().csv("spatial_distance.csv", ["image", "from", "to", "distance"],
                body.images.flatMap((img) => body.categories.flatMap((a, i) => body.categories.map((b, j) => [img, a, b, body.matrices[img][i][j]]))));
        } else if (cell.tool === "interaction" && body.matrices) {
            local.csv = () => P().csv("interaction.csv", ["image", "cell", "neighbor", "log_ratio", "p_adjusted"],
                body.images.flatMap((img) => body.categories.flatMap((a, i) => body.categories.map((b, j) =>
                    [img, a, b, body.matrices[img].score[i][j], body.matrices[img].p[i][j]]))));
        } else if (body.composition && body.groups) {
            local.csv = () => P().csv("neighborhoods.csv", ["neighborhood"].concat(body.composition.kinds),
                body.groups.map((g, i) => [g.id].concat(body.composition.fractions[i])));
        }
    }

    // The step's footer, one row in every step: View in tissue on the left,
    // the suggested next steps on the right.
    // On a narrow page the actions keep their icons and the suggestions fold
    // into "Next" (CSS container query on .sm-cell-foot).
    function resultFoot(cell, ctx, run) {
        const viewable = canView(cell, ctx);
        const act = (label, iconName, onclick, disabled, why) => {
            const b = button(label, { icon: iconName, onclick });
            b.classList.add("sm-foot-act");
            b.setAttribute("aria-label", label);
            b.title = disabled ? why : label;
            if (disabled) b.disabled = true;
            return b;
        };
        // Export, CSV and Figure Builder are the plot's, in the sidebar.
        const actions = [act("View in tissue", "image", () => ctx.viewInViewer(cell.id, {}), !viewable.ok, viewable.why)];
        return h("div.sm-cell-foot", null, h("div.sm-foot-actions", null, actions), nextPart(cell, ctx));
    }

    function nextIds(cell, ctx) {
        // Counts data has its own order (Normalize first); the server sends it.
        const own = (ctx.view.next || {})[cell.tool];
        return (own || NEXT[cell.tool] || []).filter((id) => {
            const a = ctx.view.availability[id];
            return a && a.enabled && !ctx.nb.cells.some((c) => c.tool === id && c.created_at > cell.created_at);
        });
    }

    function nextPart(cell, ctx) {
        const ids = nextIds(cell, ctx);
        if (!ids.length) return null;
        const fold = h("button.sm-btn.sm-btn-dashed.sm-next-fold", { type: "button", "aria-haspopup": "menu",
            onclick: (e) => ctx.menu(e.currentTarget, ids.map((id) => ({ label: ctx.tools[id].name, icon: ctx.tools[id].icon || "plus",
                onSelect: () => ctx.addTool(id, {}, cell.id) }))) }, "Next ▾");
        return h("div.sm-next", null, h("span.sm-next-label", null, "Suggested next"),
            ids.map((id) => h("button.sm-btn.sm-btn-dashed.sm-next-step", { type: "button", onclick: () => ctx.addTool(id, {}, cell.id) },
                "+ " + ctx.tools[id].name)), fold);
    }

    function nextRow(cell, ctx) {
        const part = nextPart(cell, ctx);
        return part ? h("div.sm-cell-foot", null, h("span.sm-spacer"), part) : null;
    }

    // -- states ----------------------------------------------------------------

    function progress(cell, ctx) {
        const job = cell.job || {};
        const started = job.started_at ? job.started_at * 1000 : Date.now();
        const elapsed = h("span", null, clock((Date.now() - started) / 1000) + " elapsed");
        ctx.ticker(cell.id, () => { elapsed.textContent = clock((Date.now() - started) / 1000) + " elapsed"; });
        return h("div.sm-progress", null,
            h("div.sm-progress-line", null, h("span", null, job.stage || "Starting"), elapsed),
            h("span.sm-bar"),
            h("div.sm-progress-actions", null,
                button("Cancel", { icon: "stop", onclick: () => ctx.cancel(cell.id) }),
                h("span", null, "Stops after the current stage. Earlier results stay as they are.")));
    }

    function failure(cell, ctx) {
        const err = (cell.run && cell.run.error) || {};
        const local = ctx.local(cell.id);
        if (err.code === "license_required" && err.detail && err.detail.reason === "limit_exceeded") {
            const cap = err.detail.max_cells || 100000;
            return h("div.sm-failure", null,
                h("p", null, h("strong", null, "Over the Starter limit. "), "This table has ",
                    h("strong", null, n(err.detail.n_cells) + " cells"), ". Starter analyzes up to " + n(cap) + "."),
                h("p", { style: { color: "var(--text-secondary)" } }, "Nothing was " + (cell.tool === "prepare" ? "converted" : "run") +
                    ". You can add a license, start a trial, or work on a random sample of " + n(cap) +
                    " cells for now. A sample is recorded with its seed, so the same cells come back in an export."),
                h("div.sm-failure-actions", null,
                    button("Add license", { kind: "primary", icon: "key", onclick: () => ctx.openLicence() }),
                    button("Start a 30-day trial", { icon: "external", onclick: () => ctx.startTrial() }),
                    cell.tool === "prepare" ? button("Sample " + n(cap) + " cells", { onclick: () => ctx.run(cell.id, { sample: cap }) })
                        : button("Prepare a sample instead", { onclick: () => ctx.focusPrepare() })));
        }
        const title = {
            license_required: "Scimap Pro's license does not allow this run.",
            precondition_missing: "Something this step reads is missing.",
            interrupted: "Plexora was closed while this step ran.",
            not_installed: "Scimap Pro is not installed here.",
        }[err.code] || "The step did not finish.";
        return h("div.sm-failure", null,
            h("p", null, h("strong", null, title + " "), err.message && err.message !== title ? err.message : ""),
            err.detail && err.detail.traceback ? h("button.sm-advanced", { type: "button", style: { margin: 0 },
                "aria-expanded": String(!!local.details), onclick: () => { local.details = !local.details; ctx.rerender(cell.id); } },
            icon("chevR"), "Details from Scimap Pro") : null,
            local.details && err.detail && err.detail.traceback ? h("pre.sm-details", null, err.detail.traceback) : null,
            h("div.sm-failure-actions", null,
                button("Retry", { kind: "primary", icon: "refresh", onclick: () => ctx.run(cell.id) }),
                button("Settings", { icon: "cog", onclick: () => openSettings(cell, ctx) }),
                h("span", null, shownRun(cell) ? "Nothing was written. The previous result of this step is still shown below."
                    : "Nothing was written.")));
    }

    function staleBanner(cell, ctx) {
        const text = h("span.sm-stale-text", { "data-tooltip": "This result was computed before; nothing is recomputed until you choose to." },
            cell.stale.reason || "An input changed.");
        return h("div.sm-stale", { role: "status" }, icon("warning"), text,
            button("Keep as is", { onclick: () => ctx.op({ op: "keep", id: cell.id }) }),
            button("Run again", { kind: "warn", icon: "refresh", onclick: () => ctx.run(cell.id) }));
    }

    function collapsedSub(cell, ctx) {
        if (cell.tool === "prepare") return window.ScimapPrepare.subtitle(ctx);
        if (cell.chips && cell.chips.length) return cell.chips.slice(0, 3).join(" · ");
        return ctx.tools[cell.tool].summary;
    }

    function queuedSub(cell, ctx) {
        const job = cell.job || {};
        if (!job.ahead) return "Starts in a moment";
        const running = ctx.view.running.find((j) => j.status === "running");
        if (running && running.cell_id) {
            return "Starts when step " + num(ctx.index(running.cell_id)) + " finishes";
        }
        return job.ahead === 1 ? "Waiting for one step" : "Waiting for " + job.ahead + " steps";
    }

    // -- the whole step -------------------------------------------------------

    /** A step, with its native selects swapped for the plugin's searchable
     *  menu (scimapPicker.js): the select stays as the value. */
    function render(...args) {
        const section = renderStep(...args);
        if (section && window.ScimapPicker) window.ScimapPicker.upgrade(section, { theme: "light" });
        return section;
    }

    function renderStep(cell, ctx) {
        const tool = ctx.tools[cell.tool];
        if (!tool) return h("section.sm-cell", null, h("div.sm-cell-head", null, "Unknown step"));
        const local = ctx.local(cell.id);
        const run = shownRun(cell);
        const busy = cell.state === "running" || cell.state === "queued";
        if (busy) local.settings = false;
        const settings = !!local.settings && !busy;
        const configure = !settings && (cell.state === "idle" || (cell.state === "cancelled" && !run));
        const classes = ["sm-cell"];
        if (configure || settings) classes.push("is-configure");
        if (settings) classes.push("is-settings");
        if (cell.state === "failed") classes.push("is-failed");
        if (cell.state === "outdated") classes.push("is-outdated");
        const open = settings || (!cell.collapsed && cell.state !== "queued");
        if (open) classes.push("is-open");
        const section = h("section." + classes.join("."), { "aria-label": "Step " + ctx.index(cell.id) + ", " + tool.name + (settings ? ", settings" : ""), "data-cell": cell.id });
        if (settings) {
            // Settings take the whole step until they close; the step's
            // results fold away and come back as they were.
            section.append(head(cell, ctx, { settings: true }));
            if (cell.tool === "prepare") section.append(window.ScimapPrepare.manager(cell, ctx));
            else if (cell.tool === NAMES_TOOL) section.append(...mappingBody(cell, ctx));
            else section.append(...form(cell, ctx, {}).filter(Boolean));
            return section;
        }
        let sub = "";
        const opts = {};
        if (cell.state === "queued") sub = queuedSub(cell, ctx);
        else if (configure && cell.tool === NAMES_TOOL) {
            // A draft keeps its progress when folded.
            opts.draftText = "Draft";
            opts.progress = true;
        } else if (cell.collapsed) sub = collapsedSub(cell, ctx);
        else if (cell.tool === "prepare" && run) sub = window.ScimapPrepare.subtitle(ctx);
        else if (cell.tool === "prepare" && configure) sub = "Choose the data, describe the samples, make the working AnnData";
        else if (run && cell.chips && cell.chips.length) {
            sub = cell.chips.join(" · ") + (cell.tool === "umap" && run.effective && run.effective.kwargs && run.effective.kwargs.useGraph ? " · reuses the neighbor graph" : "");
        }
        opts.sub = sub;
        const top = head(cell, ctx, opts);
        // The whole head folds and unfolds the step; its own controls keep theirs.
        if (cell.state !== "queued") {
            top.classList.add("is-toggle");
            top.addEventListener("click", (e) => {
                if (e.target.closest("button, a, input, select, textarea, label, [role=menu]")) return;
                if (window.getSelection && String(window.getSelection()).length) return;
                e.stopPropagation();
                ctx.op({ op: "set_collapsed", id: cell.id, collapsed: !cell.collapsed });
            });
        }
        section.append(top);
        if (cell.collapsed && cell.state !== "queued") {
            // A folded step opens from anywhere on it; its own buttons keep theirs.
            section.classList.add("is-folded");
            section.addEventListener("click", (e) => {
                if (e.target.closest("button, a, input, select, textarea, [role=menu]")) return;
                ctx.op({ op: "set_collapsed", id: cell.id, collapsed: false });
            });
        }
        if (!open) return section;
        if (local.history && cell.versions && cell.tool !== "prepare") section.append(...window.ScimapHistory.panel(cell, ctx));

        if (cell.state === "running") {
            section.append(progress(cell, ctx));
            return section;
        }
        if (cell.state === "failed") section.append(failure(cell, ctx));
        if (configure) {
            if (cell.tool === "prepare") section.append(window.ScimapPrepare.manager(cell, ctx));
            else if (cell.tool === NAMES_TOOL) section.append(...mappingBody(cell, ctx));
            else section.append(...form(cell, ctx, {}).filter(Boolean));
            return section;
        }
        if (!run) return section;
        if (cell.state === "outdated" && cell.stale) section.append(staleBanner(cell, ctx));
        if (cell.tool === "prepare") {
            section.append(window.ScimapPrepare.done(cell, ctx));
            const next = nextRow(cell, ctx);
            if (next) section.append(next);
            return section;
        }
        const slot = h("div.sm-result");
        // Held at the height it last had while the figures come, so a step
        // that re-renders (a collapse, a poll) does not shrink and regrow.
        if (local.height) slot.style.minHeight = local.height + "px";
        else slot.append(h("div.sm-headline", null, h("p.sm-unit", null, "Drawing the figures…")));
        section.append(slot);
        section.append(resultFoot(cell, ctx, run));
        Promise.all([ctx.result(cell.id, run.run_id),
            // The views on offer and the panels the step shows; a UMAP's
            // embedding is drawn by the page, so nothing is drawn for it here.
            ctx.figures(cell.id, run.run_id)]).then(([body, figs]) => {
            slot.innerHTML = "";
            const line = headline(cell, body, ctx);
            if (line) slot.append(line);
            const drawn = resultBody(cell, ctx, body, figs, run);
            if (drawn) slot.append(drawn);
            requestAnimationFrame(() => {
                if (!slot.isConnected) return;
                slot.style.minHeight = "";
                local.height = slot.offsetHeight;
            });
        }).catch((err) => {
            slot.innerHTML = "";
            slot.style.minHeight = "";
            slot.append(h("div.sm-failure", null, h("p", null, err.message),
                h("div.sm-failure-actions", null, button("Run again", { onclick: () => ctx.run(cell.id) }))));
        });
        return section;
    }

    return { render, shownRun, NEXT, statePill, canDelete, openSettings };
})();
