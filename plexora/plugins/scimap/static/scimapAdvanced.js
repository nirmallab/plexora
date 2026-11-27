// The Advanced box in a step's Settings: any other parameter a Scimap Pro
// function takes, as `name = value` lines, one box per function the step calls
// (its analysis, and the pl.* functions that draw its figures).
//
// Checked by the server against the function's own signature as it is typed
// (server/advanced.py): a misspelt name is refused with the closest real one,
// a name the form above sets is refused with the field to change, and the
// step cannot run while a line is wrong. The text is saved with the step's
// settings, so the next run's version carries it.
window.ScimapAdvanced = (function () {
    "use strict";
    const { h, icon } = window.ScimapDom;
    const API = window.ScimapApi;
    const functionsCache = new Map();

    function functionsFor(tool) {
        if (!functionsCache.has(tool)) {
            functionsCache.set(tool, API.functions(tool).then((a) => a.functions).catch(() => []));
        }
        return functionsCache.get(tool);
    }

    function lines(text) {
        return String(text || "").split("\n").filter((l) => l.split("#")[0].trim()).length;
    }

    // state: ctx.local(cell.id).adv = {open, key, texts, checks}
    function stateOf(cell, ctx) {
        const local = ctx.local(cell.id);
        if (!local.adv) {
            const saved = (cell.params && cell.params.advanced) || {};
            local.adv = { open: Object.values(saved).some((t) => String(t || "").trim()), key: null,
                          texts: Object.assign({}, saved), checks: {} };
        }
        return local.adv;
    }

    function problems(cell, ctx) {
        const st = stateOf(cell, ctx);
        const out = [];
        Object.entries(st.checks).forEach(([key, check]) => {
            (check && check.errors || []).forEach((e) => out.push({ key, line: e.line }));
        });
        return out;
    }

    function blockedText(cell, ctx) {
        const list = problems(cell, ctx);
        if (!list.length) return "";
        const st = stateOf(cell, ctx);
        const keys = Array.from(new Set(list.map((p) => p.key)));
        const nums = list.map((p) => p.line);
        const where = keys.length > 1 ? " in " + keys.join(" and ") : (keys[0] !== st.key ? " in " + keys[0] : "");
        return "Fix line" + (nums.length > 1 ? "s " + nums.slice(0, -1).join(", ") + " and " + nums[nums.length - 1]
            : " " + nums[0]) + where + " to run";
    }

    function box(cell, ctx, onValidity) {
        const st = stateOf(cell, ctx);
        const root = h("div.sm-adv");
        const total = () => Object.values(st.texts).reduce((sum, t) => sum + lines(t), 0);
        const words = ["None", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"];
        let timer = null;
        let functions = [];

        const paint = () => {
            root.innerHTML = "";
            const count = total();
            const pill = h("button.sm-adv-pill" + (st.open ? ".is-open" : ""), { type: "button",
                "aria-expanded": String(st.open), onclick: () => { st.open = !st.open; window.ScimapDom.morph(root, paint); } },
            icon(st.open ? "chevD" : "chevR", "xs"), "Advanced");
            root.append(h("div.sm-adv-head", null, pill,
                h("span.sm-adv-note", null, "Any other parameter Scimap Pro takes, by name." +
                    (count ? " " + (words[count] || count) + " set." : ""))));
            if (!st.open) return;
            if (!functions.length) {
                root.append(h("span.sm-form-help", null, "Reading the function list…"));
                return;
            }
            if (!st.key || !functions.some((f) => f.key === st.key)) st.key = functions[0].key;
            const current = functions.find((f) => f.key === st.key);
            root.append(h("div.sm-adv-fns", null,
                h("span.sm-adv-label", null, "Function"),
                h("div.sm-adv-seg", { role: "radiogroup", "aria-label": "Function" },
                    functions.map((f) => {
                        const n = lines(st.texts[f.key]);
                        const bad = (st.checks[f.key] && st.checks[f.key].errors || []).length;
                        return h("button" + (bad ? ".is-bad" : ""), { type: "button", role: "radio",
                            "aria-checked": String(f.key === st.key), onclick: () => { st.key = f.key; paint(); } },
                        f.key, n ? h("span.sm-adv-count", null, " · " + n) : null);
                    })),
                h("span.sm-adv-note", null, current.role),
                h("span.sm-spacer"),
                h("a.sm-adv-docs", { href: current.docs, target: "_blank", rel: "noopener" },
                    current.key + " reference", icon("external", "xs"))));
            const text = st.texts[st.key] || "";
            const check = st.checks[st.key] || { errors: [], notes: [] };
            const badLines = new Set((check.errors || []).map((e) => e.line));
            const gutter = h("div.sm-adv-gutter", { "aria-hidden": "true" });
            const area = h("textarea.sm-adv-text", { spellcheck: "false", rows: "4",
                "aria-label": "Advanced parameters for " + st.key,
                placeholder: "name = value" });
            area.value = text;
            const paintGutter = () => {
                gutter.innerHTML = "";
                const count = Math.max(4, area.value.split("\n").length);
                for (let i = 1; i <= count; i += 1) {
                    gutter.append(h("span" + (badLines.has(i) ? ".is-bad" : ""), null, String(i)));
                }
                gutter.scrollTop = area.scrollTop;
            };
            area.addEventListener("scroll", () => { gutter.scrollTop = area.scrollTop; });
            area.addEventListener("input", () => {
                st.texts[st.key] = area.value;
                paintGutter();
                clearTimeout(timer);
                timer = setTimeout(() => validate(st.key, true), 350);
            });
            paintGutter();
            root.append(h("div.sm-adv-editor", null, gutter, area));
            const messages = h("div.sm-adv-messages");
            (check.errors || []).forEach((e) => {
                const text = h("span", null, e.message);
                if (e.suggestion) {
                    // "Did you mean leidenFlavor?" -- the name is a button that fixes the line.
                    const at = e.message.lastIndexOf(e.suggestion);
                    text.innerHTML = "";
                    text.append(e.message.slice(0, at),
                        h("button.sm-adv-fix", { type: "button", onclick: () => fix(e.line, e.name, e.suggestion) }, e.suggestion),
                        e.message.slice(at + e.suggestion.length));
                }
                messages.append(h("div.sm-adv-error", null, h("span.sm-adv-line", null, "Line " + e.line), text));
            });
            (check.notes || []).forEach((e) => {
                messages.append(h("div.sm-adv-notice", null, h("span.sm-adv-line", null, "Line " + e.line), h("span", null, e.message)));
            });
            messages.append(h("span.sm-adv-help", null, "One ", h("code", null, "name = value"), " per line, in Python: ",
                h("code", null, "30"), ", ", h("code", null, "\"text\""), ", ", h("code", null, "True"), ", ",
                h("code", null, "None"), ", ", h("code", null, "[\"CD3\", \"CD8\"]"),
                ". Checked against " + st.key + " as you type; values outside a parameter's range are reported by Scimap Pro when it runs. Saved with the version, and written into exported scripts."));
            root.append(messages);
        };

        const fix = (line, from, to) => {
            const rows = (st.texts[st.key] || "").split("\n");
            if (rows[line - 1] !== undefined) rows[line - 1] = rows[line - 1].replace(from, to);
            st.texts[st.key] = rows.join("\n");
            validate(st.key, true);
        };

        const validate = (key, save) => {
            const text = st.texts[key] || "";
            const done = (check) => {
                st.checks[key] = check;
                if (save) ctx.setParams(cell.id, { advanced: Object.assign({}, st.texts) });
                const focused = document.activeElement && document.activeElement.classList.contains("sm-adv-text");
                const caret = focused ? document.activeElement.selectionStart : null;
                paint();
                if (focused) {
                    const area = root.querySelector(".sm-adv-text");
                    if (area) { area.focus(); area.setSelectionRange(caret, caret); }
                }
                if (onValidity) onValidity(blockedText(cell, ctx));
            };
            if (!text.trim()) {
                done({ errors: [], notes: [] });
                return;
            }
            API.checkAdvanced(ctx.nb.id, cell.id, key, text, cell.params).then((a) => done(a.check))
                .catch((err) => done({ errors: [{ line: 1, message: err.message }], notes: [] }));
        };

        functionsFor(cell.tool).then((list) => {
            functions = list;
            paint();
            // Saved text is checked once on open, so a stale line is caught.
            Object.keys(st.texts).forEach((key) => { if (st.texts[key] && !st.checks[key]) validate(key, false); });
        });
        paint();
        return root;
    }

    return { box, blockedText, stateOf };
})();
