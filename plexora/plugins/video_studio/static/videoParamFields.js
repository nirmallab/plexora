/**
 * videoParamFields.js - a preset's controls, drawn from the server's description.
 *
 * Each preset describes its parameters (server/presets.py `Field.describe`):
 * a kind, a label, options or bounds, a unit, a hint. This turns that list into
 * the Quick Capture strip's controls and reads them back, so a parameter added
 * on the server appears in the bar without a client change.
 *
 *   segmented   a row of 26 px buttons (role=radiogroup)
 *   number      a number input with its unit
 *   select      a <select>
 *   toggle      a switch (role=switch)
 *   view        "Set end view" / "Clear": captures the viewer's current view
 *   channels    a compact multi-select of the image's channels, in order
 *
 * `render(container, fields, values, {onChange, captureView, channels})`
 * replaces the container's content and returns `{values(), set(name, value)}`.
 *
 * Classic script: `window.PlexoraVideoParamFields`.
 */
(function (global) {
    "use strict";

    let uid = 0;

    function el(tag, className, attrs = {}) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        for (const [key, value] of Object.entries(attrs)) {
            if (value === undefined || value === null || value === false) continue;
            if (key === "text") node.textContent = value;
            else node.setAttribute(key, value === true ? "" : String(value));
        }
        return node;
    }

    function segmented(field, value, emit) {
        const group = el("div", "vs-segmented", { role: "radiogroup", "aria-label": field.label });
        const buttons = (field.options || []).map((option) => {
            const button = el("button", "vs-seg", {
                type: "button", role: "radio", text: option.label,
                "aria-checked": String(option.value === value), "data-value": JSON.stringify(option.value),
                title: field.hint || undefined,
            });
            button.tabIndex = option.value === value ? 0 : -1;
            button.addEventListener("click", () => {
                set(option.value);
                emit(option.value);
            });
            group.appendChild(button);
            return button;
        });
        group.addEventListener("keydown", (event) => {
            if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
            event.preventDefault();
            const index = buttons.findIndex((b) => b.getAttribute("aria-checked") === "true");
            const next = buttons[(index + (event.key === "ArrowRight" ? 1 : buttons.length - 1))
                % buttons.length];
            next.focus();
            next.click();
        });
        function set(next) {
            buttons.forEach((button) => {
                const on = button.dataset.value === JSON.stringify(next);
                button.setAttribute("aria-checked", String(on));
                button.tabIndex = on ? 0 : -1;
            });
        }
        return { node: group, set };
    }

    function number(field, value, emit) {
        const id = `vs-field-${uid += 1}`;
        const wrap = el("span", "vs-number");
        const input = el("input", "vs-input", {
            id, type: "number", inputmode: "decimal", step: field.step ?? "any",
            min: field.min, max: field.max, value, "aria-label": field.label,
            title: field.hint || undefined,
        });
        wrap.appendChild(input);
        if (field.unit) wrap.appendChild(el("span", "vs-unit", { text: field.unit, "aria-hidden": "true" }));
        let timer = null;
        const commit = () => {
            const parsed = Number(input.value);
            if (!Number.isFinite(parsed)) return;
            let next = parsed;
            if (field.min !== undefined) next = Math.max(field.min, next);
            if (field.max !== undefined && typeof field.max === "number") next = Math.min(field.max, next);
            if (next !== parsed) input.value = String(next);
            emit(next);
        };
        input.addEventListener("input", () => {
            clearTimeout(timer);
            timer = setTimeout(commit, 350);
        });
        input.addEventListener("change", () => {
            clearTimeout(timer);
            commit();
        });
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter") input.blur();
            event.stopPropagation();
        });
        return { node: wrap, set: (next) => { input.value = String(next); }, input };
    }

    function select(field, value, emit) {
        const node = el("select", "vs-select", { "aria-label": field.label });
        for (const option of field.options || []) {
            const opt = el("option", null, { value: JSON.stringify(option.value), text: option.label });
            if (option.value === value) opt.selected = true;
            node.appendChild(opt);
        }
        node.addEventListener("change", () => emit(JSON.parse(node.value)));
        return { node, set: (next) => { node.value = JSON.stringify(next); } };
    }

    function toggle(field, value, emit) {
        const button = el("button", "vs-switch", {
            type: "button", role: "switch", "aria-checked": String(Boolean(value)),
            "aria-label": field.label, title: field.hint || undefined,
        });
        button.appendChild(el("span", "vs-switch-knob", { "aria-hidden": "true" }));
        let on = Boolean(value);
        button.addEventListener("click", () => {
            on = !on;
            button.setAttribute("aria-checked", String(on));
            emit(on);
        });
        return { node: button, set: (next) => { on = Boolean(next); button.setAttribute("aria-checked", String(on)); } };
    }

    function viewField(field, value, emit, options) {
        const wrap = el("span", "vs-view-field");
        const setButton = el("button", "vs-chip", { type: "button", title: field.hint || undefined });
        const clear = el("button", "vs-chip vs-chip-quiet", { type: "button", text: "Clear",
                                                             "aria-label": `Clear ${field.label}` });
        let current = value || null;
        const paint = () => {
            setButton.textContent = current ? `${field.label} set` : `Set ${field.label.toLowerCase()}`;
            setButton.classList.toggle("is-set", Boolean(current));
            clear.hidden = !current;
        };
        setButton.addEventListener("click", () => {
            const view = options.captureView?.();
            if (!view) return;
            current = view;
            paint();
            emit(current);
        });
        clear.addEventListener("click", () => {
            current = null;
            paint();
            emit(null);
        });
        wrap.append(setButton, clear);
        paint();
        return { node: wrap, set: (next) => { current = next || null; paint(); } };
    }

    function channelsField(field, value, emit, options) {
        const channels = options.channels || [];
        const wrap = el("div", "vs-channel-pick", { role: "group", "aria-label": field.label });
        let chosen = Array.isArray(value) ? value.slice() : [];
        const buttons = channels.map((channel) => {
            const button = el("button", "vs-chan", { type: "button", "data-key": channel.key,
                                                     title: channel.name || channel.key });
            button.style.setProperty("--vs-chan", channel.color || "#94a3b8");
            button.appendChild(el("span", "vs-chan-dot", { "aria-hidden": "true" }));
            button.appendChild(el("span", "vs-chan-name", { text: channel.name || channel.key }));
            button.appendChild(el("span", "vs-chan-order", { "aria-hidden": "true" }));
            button.addEventListener("click", () => {
                chosen = chosen.includes(channel.key) ? chosen.filter((k) => k !== channel.key)
                    : chosen.concat(channel.key);
                paint();
                emit(chosen.slice());
            });
            wrap.appendChild(button);
            return button;
        });
        function paint() {
            buttons.forEach((button) => {
                const index = chosen.indexOf(button.dataset.key);
                button.setAttribute("aria-pressed", String(index >= 0));
                button.querySelector(".vs-chan-order").textContent = index >= 0 ? String(index + 1) : "";
            });
        }
        paint();
        return { node: wrap, set: (next) => { chosen = Array.isArray(next) ? next.slice() : []; paint(); } };
    }

    const KINDS = { segmented, number, select, toggle, view: viewField, channels: channelsField };

    function render(container, fields, values, options = {}) {
        container.replaceChildren();
        const current = { ...values };
        const controls = {};
        for (const field of fields) {
            const make = KINDS[field.kind];
            if (!make) continue;
            const row = el("div", `vs-field vs-field-${field.kind}`, { "data-field": field.name });
            const label = el("span", "vs-field-label", { text: field.label });
            const control = make(field, current[field.name] ?? field.default, (next) => {
                current[field.name] = next;
                options.onChange?.(field.name, next, { ...current });
            }, options);
            if (control.input) label.setAttribute("id", `${control.input.id}-label`);
            row.append(label, control.node);
            container.appendChild(row);
            controls[field.name] = control;
        }
        return {
            values: () => ({ ...current }),
            set(name, value) {
                current[name] = value;
                controls[name]?.set(value);
            },
        };
    }

    global.PlexoraVideoParamFields = { render };
})(typeof window !== "undefined" ? window : globalThis);
