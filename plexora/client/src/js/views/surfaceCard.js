/**
 * surfaceCard.js - a surface layer's card body in the Layers panel.
 *
 * The card IS where a surface is configured, like every other layer's: its
 * colour, its style (solid, glass, wire, outline), its opacity, its material
 * and what it is coloured by; a line saying exactly what it is ("CD31 at
 * 1,240 · level 0 · 184 k triangles"); and its actions -- inspect its objects,
 * compare it with the unsmoothed mesh, export it, make another like it.
 * Changing any of it is a uniform in the 3-D view and a saved render bag
 * (services/surfaceLayers.js `style`), never a rebuild.
 *
 * While it builds the card follows `plexora:layer-progress` with the stage the
 * server names; a failed build says why and offers Retry, which posts the
 * request it was made with again.
 *
 * Classic script: `window.PlexoraSurfaceCard`.
 */
window.PlexoraSurfaceCard = (function () {
    "use strict";

    const STYLES = [
        { value: "solid", label: "Solid", title: "Opaque, lit surface" },
        { value: "glass", label: "Glass", title: "Translucent: what is inside shows through" },
        { value: "wireframe", label: "Wire", title: "The mesh's triangles as lines" },
        { value: "outline", label: "Outline", title: "Solid with a dark silhouette line" },
    ];
    const MATERIALS = [
        { value: "soft", label: "Soft" },
        { value: "matte", label: "Matte" },
        { value: "glossy", label: "Glossy" },
        { value: "glass", label: "Glass" },
    ];
    const COLOURINGS = [
        { value: "uniform", label: "One colour" },
        { value: "id", label: "Each object" },
        { value: "cells", label: "Cell colours", cellsOnly: true },
        { value: "table", label: "A measurement", title: "Set in the Objects table" },
    ];

    function service() { return window.PlexoraSurfaceLayers || null; }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function number(value) {
        const n = Number(value);
        if (!Number.isFinite(n)) return "–";
        return n >= 1e6 ? `${(n / 1e6).toFixed(1)} M` : (n >= 1e4 ? `${Math.round(n / 1e3)} k`
            : n.toLocaleString());
    }

    function iso(value) {
        const n = Number(value);
        return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 3 }) : "–";
    }

    /** What the surface is, in one line, from its request and its manifest. */
    function describe(layer, summary) {
        const spec = layer.spec?.surface || {};
        const parts = [];
        if (spec.type === "label_surfaces") {
            parts.push(spec.ids ? `${spec.ids.length} selected cell${spec.ids.length === 1 ? "" : "s"}`
                : "Cells of the 3-D mask");
        } else if (spec.channel) {
            parts.push(`${spec.channel} at ${iso(spec.iso)}`);
        }
        if (summary) {
            if (Number.isFinite(summary.level)) parts.push(`level ${summary.level}`);
            if (summary.triangles) parts.push(`${number(summary.triangles)} triangles`);
            const count = summary.objects?.count;
            if (count) {
                const noun = spec.type === "label_surfaces" ? "object" : "piece";
                parts.push(`${number(count)} ${noun}${count === 1 ? "" : "s"}`);
            }
        }
        return parts.join(" · ");
    }

    function segmented(options, current, onPick, label) {
        const group = el("div", "plx-surf-seg");
        group.setAttribute("role", "radiogroup");
        group.setAttribute("aria-label", label);
        const buttons = options.map((option) => {
            const button = el("button", "plx-surf-seg-option", option.label);
            button.type = "button";
            button.dataset.value = option.value;
            button.setAttribute("role", "radio");
            if (option.title) button.title = option.title;
            button.addEventListener("click", () => {
                buttons.forEach((b) => b.setAttribute("aria-checked", String(b === button)));
                onPick(option.value);
            });
            group.appendChild(button);
            return button;
        });
        const set = (value) => buttons.forEach((b) => b.setAttribute("aria-checked",
                                                                      String(b.dataset.value === value)));
        set(current);
        group.setValue = set;
        return group;
    }

    function select(options, current, onPick, label) {
        const node = el("select", "plx-surf-select");
        node.setAttribute("aria-label", label);
        for (const option of options) {
            const item = el("option", null, option.label);
            item.value = option.value;
            node.appendChild(item);
        }
        node.value = current;
        node.addEventListener("change", () => onPick(node.value));
        return node;
    }

    /** The card body for a surface layer. `api.persist` is the panel's. */
    function build(layer) {
        const body = el("div", "layer-card-controls plx-surf-card");
        body.dataset.surface = layer.id;
        const render = () => layer.spec?.render || {};
        const set = (patch) => service()?.style(layer.id, patch);

        // Row 1: colour and style.
        const top = el("div", "plx-surf-row");
        const swatch = el("label", "plx-surf-swatch");
        swatch.title = "Colour";
        const colour = el("input");
        colour.type = "color";
        colour.value = render().color || "#d8b45a";
        colour.setAttribute("aria-label", "Surface colour");
        swatch.style.setProperty("--swatch", colour.value);
        colour.addEventListener("input", () => {
            swatch.style.setProperty("--swatch", colour.value);
            set({ color: colour.value });
        });
        swatch.appendChild(colour);
        top.appendChild(swatch);
        const styles = segmented(STYLES, render().style || "solid", (value) => {
            set({ style: value });
            syncOpacity();
        }, "Style");
        top.appendChild(styles);
        body.appendChild(top);

        // Row 2: opacity -- the image card's row (layerManager buildOpacityRow),
        // typeable box included. Per tick: a uniform, never a re-upload.
        const opacityRow = el("div", "layer-card-row plx-surf-opacity");
        const opacityLabel = el("label", null, "Opacity");
        const opacityId = `surf-opacity-${layer.id}`;
        opacityLabel.htmlFor = opacityId;
        const opacity = new PlexoraSlider(null, {
            id: opacityId, min: 0.05, max: 1, step: 0.01,
            value: Number(render().opacity ?? 1), decimals: 2, ariaLabel: "Opacity",
            onInput: (value) => set({ opacity: value }),
        });
        // Choosing Glass sets its own opacity (surfaceLayers.style); the
        // slider follows without echoing it back.
        const syncOpacity = () => opacity.set(Number(render().opacity ?? 1), { silent: true });
        opacityRow.append(opacityLabel, opacity.el);
        body.appendChild(opacityRow);

        // Row 3: material and colouring.
        const looks = el("div", "plx-surf-row plx-surf-looks");
        const material = select(MATERIALS, render().material || "soft",
                                (value) => set({ material: value }), "Material");
        const isCells = layer.spec?.surface?.type === "label_surfaces";
        const colouring = select(COLOURINGS.filter((option) => !option.cellsOnly || isCells),
                                 render().colorBy || "uniform",
                                 (value) => set({ colorBy: value }), "Colour by");
        const materialLabel = el("label", "plx-surf-field");
        materialLabel.append(el("span", null, "Look"), material);
        const colouringLabel = el("label", "plx-surf-field");
        colouringLabel.append(el("span", null, "Colour"), colouring);
        looks.append(materialLabel, colouringLabel);
        body.appendChild(looks);

        // What it is, and the build.
        const what = el("p", "plx-surf-what", describe(layer, null));
        body.appendChild(what);
        const state = el("div", "plx-surf-state");
        body.appendChild(state);
        const note = el("p", "layer-card-alignment is-ok", "Computed from this image");
        body.appendChild(note);

        // Actions.
        const actions = el("div", "plx-surf-actions");
        const action = (icon, text, title, onClick) => {
            const button = el("button", "plx-surf-action");
            button.type = "button";
            button.title = title;
            const glyph = el("i", `fas ${icon}`);
            glyph.setAttribute("aria-hidden", "true");
            button.append(glyph, el("span", null, text));
            button.addEventListener("click", onClick);
            actions.appendChild(button);
            return button;
        };
        action("fa-cube", "Show in 3-D", "Switch the view to 3-D", () => {
            const vs = window.__plexora?.viewState;
            if (vs?.enabled?.() && !vs.isVolume()) vs.set({ mode: "volume" });
        });
        action("fa-table-list", "Objects", "Every object of this surface, measured", () => {
            window.PlexoraSurfaceObjects?.open(layer.id);
        });
        const compare = action("fa-code-compare", "Unsmoothed", "Compare with the mesh before smoothing",
            () => {
                const next = !render().compareRaw;
                set({ compareRaw: next });
                compare.setAttribute("aria-pressed", String(next));
            });
        compare.setAttribute("aria-pressed", String(Boolean(render().compareRaw)));
        action("fa-file-export", "Export", "Save the mesh (PLY, OBJ, STL or glTF) with its provenance",
            (event) => window.PlexoraSurfaceObjects?.exportMenu?.(layer.id, event.currentTarget));
        // Remaking a surface is Plexora 3D; everything above is a read.
        if (window.PlexoraPaid?.allows?.("volume:surfaces")) {
            action("fa-sliders", "Edit", "Open Surface Studio with this surface's settings", () => {
                window.PlexoraSurfaceStudio?.open({ from: layer.spec?.surface || null });
            });
        }
        body.appendChild(actions);

        let summary = null;
        const paintState = (progress = null) => {
            const status = layer.spec?.status || "ready";
            state.textContent = "";
            state.className = "plx-surf-state";
            actions.hidden = status !== "ready";
            if (status === "pending") {
                state.classList.add("is-pending");
                const text = el("span", null, progress?.stageLabel || progress?.message || "Preparing…");
                const bar = el("progress", "plx-surf-progress");
                bar.max = 100;
                if (Number.isFinite(progress?.progress)) bar.value = progress.progress;
                const stop = el("button", "plx-surf-link", "Stop");
                stop.type = "button";
                stop.addEventListener("click", () => {
                    const ds = encodeURIComponent(window.flaskVariables?.datasource || "");
                    fetch(plexoraUrl(`volume/${ds}/surfaces/${encodeURIComponent(layer.id)}/cancel`),
                          { method: "POST" }).catch(() => {});
                });
                state.append(text, bar, stop);
            } else if (status === "failed") {
                state.classList.add("is-bad");
                state.append(el("span", null, progress?.error || "Could not be built"));
                const retry = el("button", "plx-surf-link", "Retry");
                retry.type = "button";
                retry.addEventListener("click", async () => {
                    retry.disabled = true;
                    try {
                        await service()?.retry(layer.id);
                    } catch (error) {
                        window.PlexoraToast?.show?.({ title: error.message, tone: "warning" });
                    }
                    retry.disabled = false;
                });
                state.appendChild(retry);
            }
        };
        const loadSummary = async () => {
            if ((layer.spec?.status || "ready") !== "ready") return;
            try {
                const listing = await service()?.list();
                const entry = (listing?.surfaces || []).find((s) => s.id === layer.id);
                summary = entry?.summary || null;
                what.textContent = describe(layer, summary);
                body.querySelector(".plx-surf-stale")?.remove();
                if (entry?.stale) {
                    // Made from an image or mask that has changed since.
                    const stale = el("p", "plx-surf-stale plx-surf-warn",
                                     "The image or mask changed since this was made. ");
                    const rebuild = el("button", "plx-surf-link", "Rebuild");
                    rebuild.type = "button";
                    rebuild.addEventListener("click", async () => {
                        rebuild.disabled = true;
                        try {
                            await service()?.retry(layer.id);
                        } catch (error) {
                            window.PlexoraToast?.show?.({ title: error.message, tone: "warning" });
                        }
                    });
                    stale.appendChild(rebuild);
                    what.after(stale);
                }
                if (summary?.units === "voxel") {
                    note.textContent = "Computed from this image · sizes in voxels (no z step)";
                }
            } catch (error) {
                /* the line stays as the request describes it */
            }
        };
        const onProgress = (event) => {
            if (event.detail?.layer !== layer.id) return;
            paintState(event.detail);
        };
        const onReady = (event) => {
            if (event.detail?.layer !== layer.id) return;
            paintState();
            loadSummary();
        };
        // A new version (an edit, an undo) is a new mesh: its line follows.
        const onLoaded = (event) => {
            if (event.detail?.id === layer.id) loadSummary();
        };
        window.addEventListener("plexora:surface-loaded", onLoaded);
        const onFailed = (event) => {
            if (event.detail?.layer !== layer.id) return;
            paintState({ error: event.detail.error });
        };
        window.addEventListener("plexora:layer-progress", onProgress);
        window.addEventListener("plexora:layer-ready", onReady);
        window.addEventListener("plexora:layer-failed", onFailed);
        body.refresh = (next) => {
            if (next) layer = next;
            colour.value = render().color || colour.value;
            swatch.style.setProperty("--swatch", colour.value);
            styles.setValue(render().style || "solid");
            material.value = render().material || "soft";
            colouring.value = render().colorBy || "uniform";
            syncOpacity();
            paintState();
        };
        body.destroy = () => {
            window.removeEventListener("plexora:layer-progress", onProgress);
            window.removeEventListener("plexora:layer-ready", onReady);
            window.removeEventListener("plexora:layer-failed", onFailed);
            window.removeEventListener("plexora:surface-loaded", onLoaded);
        };
        paintState();
        loadSummary();
        return body;
    }

    /** Bring a built card's controls in line with its layer (a re-sync). */
    function refresh(card, layer) {
        const body = card?.querySelector?.(".plx-surf-card");
        body?.refresh?.(layer);
    }

    return { build, refresh, describe };
}());
