/**
 * depthTools.js - core's Depth tool: the card behind the Z strip.
 *
 * Registered exactly the way viewTransformTools.js registers Rotate & Flip
 * (lazy, no layer), and like it the controller holds NO state: it paints from
 * services/viewState.js and writes to it, so the card, the strip on the canvas
 * and the keyboard are three handles on one value and cannot drift.
 *
 * What lives here rather than on the strip: the projection's operation and
 * its exact range typed in, the frame of a time series, and the z step -- the
 * one number a physical 3-D measurement needs and many stacks never state.
 * Setting it posts `/set_pixel_size` with `axis: "z"`, the same route the
 * scale bar's calibration uses for x/y.
 */
(function (global) {
    "use strict";

    function viewStateOf() {
        return global.__plexora?.viewState || null;
    }

    function number(value, fallback) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : fallback;
    }

    function formatLength(value, unit) {
        return `${Number(value).toFixed(value < 10 ? 2 : 1).replace(/\.?0+$/, "")} ${unit || "µm"}`;
    }

    function createController(ctx) {
        let root = null;
        let unsubscribe = null;

        function q(selector) {
            return root?.querySelector(selector) || null;
        }

        function paint() {
            const vs = viewStateOf();
            const state = vs?.get();
            if (!root || !state) return;
            for (const button of root.querySelectorAll("[data-depth-mode]")) {
                const on = state.mode === button.dataset.depthMode;
                button.classList.toggle("is-active", on);
                button.setAttribute("aria-checked", on ? "true" : "false");
            }
            const top = vs.depth - 1;
            const plane = q("#depth_plane_input");
            if (plane && document.activeElement !== plane) {
                plane.max = String(top);
                plane.value = String(state.z);
            }
            const of = q("[data-depth-of]");
            if (of) of.textContent = `of ${vs.depth}`;
            const framesRows = root.querySelectorAll("[data-depth-frames-row]");
            framesRows.forEach((row) => { row.hidden = vs.frames < 2; });
            const frame = q("#depth_frame_input");
            if (frame && document.activeElement !== frame) {
                frame.max = String(vs.frames);
                frame.value = String(state.t + 1);
            }
            const framesOf = q("[data-depth-frames-of]");
            if (framesOf) framesOf.textContent = `of ${vs.frames}`;

            const op = q("#depth_op_select");
            if (op && document.activeElement !== op) op.value = state.slab.op;
            const pct = q("#depth_q_input");
            if (pct) {
                pct.hidden = state.slab.op !== "pct";
                if (document.activeElement !== pct) pct.value = String(state.slab.q ?? 95);
            }
            for (const [selector, key] of [["#depth_z0_input", "z0"], ["#depth_z1_input", "z1"]]) {
                const input = q(selector);
                if (input && document.activeElement !== input) {
                    input.max = String(top);
                    input.value = String(state.slab[key]);
                }
            }
            const thickness = q("[data-depth-thickness]");
            if (thickness) {
                const planes = state.slab.z1 - state.slab.z0 + 1;
                const step = vs.zStep();
                thickness.textContent = `${planes} plane${planes === 1 ? "" : "s"}`
                    + (step ? ` · ${formatLength(planes * step.value, step.unit)}` : "");
            }
            const follow = q("#depth_follow_input");
            if (follow) follow.checked = Boolean(state.slab.follow);
            paintPin(vs, state);
            paintSpacing(vs);
        }

        //: The projection layers this card started, by layer id -> label, so
        //: the note can follow their builds on the viewer's layer events.
        const pinning = new Map();

        function pinnedLayer(vs) {
            const token = vs.spec();
            const id = `proj-${token}`;
            // The layer stack holds every registered layer's spec, status
            // included (main.js marks it as builds finish).
            return { id, layer: global.__plexora?.layers?.get?.(id)?.spec || null };
        }

        function paintPin(vs, state) {
            const row = q("[data-depth-pin]");
            if (!row) return;
            row.hidden = state.mode !== "slab";
            if (row.hidden) return;
            const button = q("#depth_pin_button");
            const note = q("[data-depth-pin-note]");
            const { id, layer } = pinnedLayer(vs);
            const busy = pinning.has(id) || layer?.status === "pending";
            if (button) button.disabled = busy || Boolean(layer && layer.status !== "failed");
            if (!note) return;
            if (busy) note.textContent = `Adding ${vs.label()} as a layer…`;
            else if (layer) note.textContent = `${vs.label()} is in Layers, computed exactly at full resolution.`;
            else note.textContent = "Computes this projection exactly, at full resolution, "
                + "as a layer you can keep, measure and export.";
        }

        async function pin() {
            const vs = viewStateOf();
            if (!vs) return;
            const { id } = pinnedLayer(vs);
            const label = vs.label();
            pinning.set(id, label);
            paint();
            try {
                const url = global.plexoraUrl
                    ? global.plexoraUrl(`volume/${encodeURIComponent(vs.datasource)}/pin`)
                    : `/volume/${encodeURIComponent(vs.datasource)}/pin`;
                const response = await fetch(url, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ view: vs.spec() }),
                });
                const answer = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(answer.error || `HTTP ${response.status}`);
                // Re-read /config so the Layers panel shows the card now, and
                // the viewer's one poll follows the build from here.
                await global.__plexora?.adoptLayers?.();
                if (!answer.created) pinning.delete(id);
            } catch (error) {
                pinning.delete(id);
                global.PlexoraToast?.show?.({
                    title: `${label} could not be added as a layer.`,
                    body: String(error.message || error), tone: "warning" });
            }
            paint();
        }

        function onLayerSettled(event) {
            const id = event.detail?.layer;
            if (!pinning.has(id)) return;
            const label = pinning.get(id);
            pinning.delete(id);
            if (event.type === "plexora:layer-failed") {
                global.PlexoraToast?.show?.({
                    title: `${label} could not be added as a layer.`,
                    body: event.detail?.error || "", tone: "warning" });
            }
            paint();
        }

        function paintSpacing(vs) {
            const step = vs.zStep();
            const value = q("[data-depth-spacing]");
            if (value) {
                if (step) {
                    const from = step.source === "manual" ? "typed" : "from the file";
                    value.textContent = `${formatLength(step.value, step.unit)} (${from})`;
                    value.classList.remove("is-missing");
                } else {
                    value.textContent = "Not stated";
                    value.classList.add("is-missing");
                }
            }
            const note = q("[data-depth-note]");
            if (note) {
                const info = vs.info || {};
                const parts = [`${vs.depth} plane${vs.depth === 1 ? "" : "s"}`];
                if (vs.frames > 1) parts.push(`${vs.frames} frames`);
                if (step && Number(info.anisotropy) > 0) {
                    parts.push(`z step ${Number(info.anisotropy).toFixed(1)}× the pixel size`);
                }
                note.textContent = parts.join(" · ") + (step ? "" :
                    ". Sections and 3-D measurements need the distance between planes.");
            }
        }

        async function saveSpacing() {
            const vs = viewStateOf();
            const input = q("#depth_spacing_input");
            const value = number(input?.value, NaN);
            if (!vs || !(value >= 0)) return;
            try {
                const response = await fetch(global.plexoraUrl
                    ? global.plexoraUrl("set_pixel_size") : "/set_pixel_size", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ datasource: vs.datasource, value, axis: "z" }),
                });
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const info = await vs._json(global.plexoraUrl
                    ? global.plexoraUrl(`volume/${encodeURIComponent(vs.datasource)}/info`)
                    : `/volume/${encodeURIComponent(vs.datasource)}/info`);
                if (info?.volumetric) vs.info = info;
                setEditing(false);
                vs.set({}, { save: false });
            } catch (error) {
                console.error("depthTools: could not save the z step", error);
                global.PlexoraToast?.show?.({ title: "The z step could not be saved.", tone: "warning" });
            }
        }

        function setEditing(on) {
            const edit = q("[data-depth-spacing-edit]");
            const value = q("[data-depth-spacing]");
            const button = q("#depth_spacing_button");
            if (edit) edit.hidden = !on;
            if (value) value.hidden = on;
            if (button) button.hidden = on;
            if (on) {
                const input = q("#depth_spacing_input");
                const step = viewStateOf()?.zStep();
                if (input) {
                    input.value = step ? String(step.value) : "";
                    input.focus();
                }
            }
        }

        return {
            setup() {
                const vs = viewStateOf();
                root = document.getElementById("depth_panel_section");
                if (!root || !vs?.enabled()) return;
                for (const button of root.querySelectorAll("[data-depth-mode]")) {
                    button.addEventListener("click", () => vs.set({ mode: button.dataset.depthMode }));
                }
                q("#depth_plane_input")?.addEventListener("change", (event) => {
                    vs.set({ z: number(event.target.value, vs.get().z) });
                });
                q("#depth_frame_input")?.addEventListener("change", (event) => {
                    vs.set({ t: number(event.target.value, vs.get().t + 1) - 1 });
                });
                q("#depth_op_select")?.addEventListener("change", (event) => {
                    vs.set({ mode: "slab", slab: { op: event.target.value } });
                });
                q("#depth_q_input")?.addEventListener("change", (event) => {
                    vs.set({ mode: "slab", slab: { op: "pct", q: number(event.target.value, 95) } });
                });
                const range = () => {
                    const z0 = number(q("#depth_z0_input")?.value, vs.get().slab.z0);
                    const z1 = number(q("#depth_z1_input")?.value, vs.get().slab.z1);
                    const lo = Math.min(z0, z1);
                    const hi = Math.max(z0, z1);
                    vs.set({ mode: "slab", z: Math.round((lo + hi) / 2),
                             slab: { z0: lo, z1: hi, thickness: hi - lo + 1, follow: false } });
                };
                q("#depth_z0_input")?.addEventListener("change", range);
                q("#depth_z1_input")?.addEventListener("change", range);
                q("#depth_follow_input")?.addEventListener("change", (event) => {
                    vs.set({ slab: { follow: event.target.checked } });
                });
                q("#depth_pin_button")?.addEventListener("click", pin);
                global.addEventListener("plexora:layer-ready", onLayerSettled);
                global.addEventListener("plexora:layer-failed", onLayerSettled);
                q("#depth_spacing_button")?.addEventListener("click", () => setEditing(true));
                q("#depth_spacing_save")?.addEventListener("click", saveSpacing);
                q("#depth_spacing_input")?.addEventListener("keydown", (event) => {
                    if (event.key === "Enter") saveSpacing();
                    if (event.key === "Escape") setEditing(false);
                });
                unsubscribe = vs.subscribe(paint);
                paint();
                ctx?.onCleanup?.(() => {
                    unsubscribe?.();
                    unsubscribe = null;
                    global.removeEventListener("plexora:layer-ready", onLayerSettled);
                    global.removeEventListener("plexora:layer-failed", onLayerSettled);
                    root = null;
                });
            },
        };
    }

    const DEFINITIONS = [
        {
            name: "depth",
            lazy: true,
            hasLayer: false,
            help: {
                summary: "Moves through the planes of a z-stack and combines a range of "
                    + "planes into one picture. The strip at the right edge of the image "
                    + "does the common part -- drag its rail, or press ] and [ -- and this "
                    + "card holds the rest: the projection's operation and exact range, "
                    + "the frame of a time series, and the distance between planes.\n\n"
                    + "Saved with this image, so it reopens on the same plane.",
                notes: [
                    "A projection combines planes: two markers overlapping in it may "
                    + "never touch in 3-D, and a bright spot may be two cells at "
                    + "different depths. Every capture of one says which planes it combines.",
                    "Shift+] and Shift+[ move ten planes; Alt+] and Alt+[ jump to the "
                    + "last and first. Shift with the mouse wheel over the image scrubs.",
                    "Every plane of a channel is drawn with the same contrast, so a "
                    + "plane that looks dimmer is dimmer.",
                    "Add as a layer computes the projection once, over every plane in "
                    + "the range at full resolution, and keeps it in Layers. On screen a "
                    + "zoomed-out projection combines planes that were already shrunk, "
                    + "which is right for looking but not for numbers. Removing the layer "
                    + "deletes the file it made.",
                ],
            },
            createSidebarController: createController,
        },
    ];

    function register() {
        const registerPlugin = global.Plexora?.registerPlugin
            || ((definition) => global.Plexora?.plugins?.register(definition));
        for (const definition of DEFINITIONS) registerPlugin(definition);
    }

    register();

    global.PlexoraDepthTools = { DEFINITIONS, createController };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraDepthTools;
    }
})(typeof window !== "undefined" ? window : globalThis);
