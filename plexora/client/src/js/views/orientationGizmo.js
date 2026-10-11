/**
 * orientationGizmo.js - which way the stack is facing, in a corner of the 3-D view.
 *
 * Three short axes -- x red, y green, z blue, the convention every 3-D tool
 * shares -- drawn from the stack's own orientation every frame the view moves,
 * so it always says where the image's rows, columns and planes point. Nearly
 * invisible at rest (a third opaque, no background, no rings); solid under the
 * pointer, while the view moves, or while an axis is locked.
 *
 * A labelled end is a button: it turns the view to look along that axis at
 * the plane across it, the way the section panes show that plane (X: the YZ
 * plane, Y: XZ, Z: XY); pressed again, or the small dot at the other end,
 * it looks from the far side. A locked axis (X / Y / Z keys,
 * views/volumeRenderer.js) wears a ring, the other two fade, and a short
 * line under the gizmo says how to let go.
 *
 * Drawn in SVG: a few elements rewritten per frame, nothing on the GPU, and
 * crisp at any zoom of the page.
 */
(function (global) {
    "use strict";

    const NS = "http://www.w3.org/2000/svg";
    const SIZE = 64;
    const REACH = 21;                      // px from the centre to an axis end
    const COLOURS = { x: "#e5484d", y: "#46a758", z: "#4c7cf0" };
    const VIEWS = { x: "the YZ plane", y: "the XZ plane", z: "the XY plane" };

    function svg(tag, attrs = {}) {
        const node = document.createElementNS(NS, tag);
        for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
        return node;
    }

    class OrientationGizmo {
        /**
         * @param onAxis - (axis, {reverse}) when an end is pressed
         */
        constructor({ onAxis = null } = {}) {
            this.onAxis = onAxis;
            this.locked = null;
            this.root = document.createElement("div");
            this.root.className = "viewer-orientation-gizmo";
            this.root.setAttribute("role", "group");
            this.root.setAttribute("aria-label", "Orientation");
            this.svg = svg("svg", { viewBox: `${-SIZE / 2} ${-SIZE / 2} ${SIZE} ${SIZE}`,
                                    width: SIZE, height: SIZE, "aria-hidden": "false" });
            this.axes = {};
            for (const name of ["x", "y", "z"]) {
                const group = svg("g", { class: `gizmo-axis is-${name}` });
                const line = svg("line", { x1: 0, y1: 0, stroke: COLOURS[name],
                                           "stroke-width": 2, "stroke-linecap": "round" });
                const back = svg("circle", { r: 2.6, fill: COLOURS[name], class: "gizmo-back",
                                             role: "button", tabindex: "0",
                                             "aria-label": `View ${VIEWS[name]} from the far side` });
                const end = svg("g", { class: "gizmo-end", role: "button", tabindex: "0",
                                       "aria-label": `View along ${name.toUpperCase()}: ${VIEWS[name]}` });
                const title = svg("title");
                title.textContent = `${name.toUpperCase()}: view ${VIEWS[name]}`
                    + " (again for its far side)";
                const ring = svg("circle", { r: 9.5, class: "gizmo-ring", fill: "none" });
                const disc = svg("circle", { r: 7, fill: COLOURS[name] });
                const label = svg("text", { "text-anchor": "middle", "dominant-baseline": "central",
                                            class: "gizmo-label" });
                label.textContent = name.toUpperCase();
                end.append(title, ring, disc, label);
                group.append(line, back, end);
                this.svg.appendChild(group);
                this.axes[name] = { group, line, back, end };
                const press = (reverse) => (event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    this.onAxis?.(name, { reverse });
                };
                end.addEventListener("pointerdown", (event) => event.stopPropagation());
                back.addEventListener("pointerdown", (event) => event.stopPropagation());
                end.addEventListener("click", press(false));
                back.addEventListener("click", press(true));
                for (const [node, reverse] of [[end, false], [back, true]]) {
                    node.addEventListener("keydown", (event) => {
                        if (event.key === "Enter" || event.key === " ") press(reverse)(event);
                    });
                }
            }
            this.hint = document.createElement("div");
            this.hint.className = "viewer-orientation-hint";
            this.hint.hidden = true;
            this.root.append(this.svg, this.hint);
        }

        /**
         * Redraw for `axisOnScreen(name)` -> [x right, y up, z toward viewer].
         * Ends nearer the viewer are drawn over farther ones.
         */
        update(axisOnScreen) {
            const order = [];
            for (const name of ["x", "y", "z"]) {
                const [x, y, z] = axisOnScreen(name);
                const parts = this.axes[name];
                const ex = x * REACH;
                const ey = -y * REACH;             // SVG y runs down
                parts.line.setAttribute("x2", ex.toFixed(2));
                parts.line.setAttribute("y2", ey.toFixed(2));
                parts.back.setAttribute("cx", (-ex * 0.72).toFixed(2));
                parts.back.setAttribute("cy", (-ey * 0.72).toFixed(2));
                parts.end.setAttribute("transform", `translate(${ex.toFixed(2)} ${ey.toFixed(2)})`);
                // An axis pointing away reads as behind the others (viewer.css).
                parts.group.classList.toggle("is-behind", z < -0.2);
                order.push([z, parts.group]);
            }
            order.sort((a, b) => a[0] - b[0]);
            for (const [, group] of order) this.svg.appendChild(group);
        }

        /** Solid while the view moves; back to faint a moment after. */
        setActive(on) {
            clearTimeout(this._idle);
            if (on) {
                this.root.classList.add("is-active");
            } else {
                this._idle = setTimeout(() => this.root.classList.remove("is-active"), 700);
            }
        }

        /** Show a locked axis (or none). */
        setLocked(name) {
            this.locked = name || null;
            this.root.classList.toggle("is-locked", Boolean(this.locked));
            for (const [axis, parts] of Object.entries(this.axes)) {
                parts.group.classList.toggle("is-locked", axis === this.locked);
            }
            this.hint.hidden = !this.locked;
            this.hint.textContent = this.locked
                ? `Turning about ${this.locked.toUpperCase()} · Esc` : "";
        }

        show(visible) {
            this.root.hidden = !visible;
        }
    }

    OrientationGizmo.COLOURS = COLOURS;
    global.PlexoraOrientationGizmo = OrientationGizmo;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = OrientationGizmo;
    }
})(typeof window !== "undefined" ? window : globalThis);
