/**
 * surfaceInspect.js - the card a picked surface object opens beside the pointer.
 *
 * Click an object in the 3-D view (no drag) and this says what it is: its id
 * (a cell's id, for cell surfaces -- the table's cell_id), its mesh
 * measurements beside the voxel ones (never mixed: the voxel volume is the
 * mask's, the mesh volume is the smoothed surface's), whether it touches the
 * edge of the volume, and what can be done with it:
 *
 *   Hide · Isolate · Show all · In planes (the plane through it, centred)
 *   · Neighbours within … (distances, and contact areas for cells)
 *
 * Neighbours are Paid like the rest of Surface Studio; the card itself is a
 * read and stays open on any plan.
 *
 * Classic script: `window.PlexoraSurfaceInspect`.
 */
window.PlexoraSurfaceInspect = (function () {
    "use strict";

    let card = null;
    let current = null;

    function service() { return window.PlexoraSurfaceLayers || null; }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function value(n, digits = 3) {
        const x = Number(n);
        if (n === null || n === undefined || !Number.isFinite(x)) return "–";
        return x.toLocaleString(undefined, { maximumSignificantDigits: digits });
    }

    function close() {
        card?.remove();
        card = null;
        current = null;
    }

    function place(node, x, y) {
        const host = document.getElementById("openseadragon_wrapper");
        if (!host) return;
        host.appendChild(node);
        const box = host.getBoundingClientRect();
        const width = node.offsetWidth || 260;
        const height = node.offsetHeight || 200;
        // Beside the point, not over it: the picked object stays in sight.
        const left = Math.min(Math.max(8, x - box.left + 36), box.width - width - 8);
        const top = Math.min(Math.max(8, y - box.top - 24), box.height - height - 8);
        node.style.left = `${left}px`;
        node.style.top = `${top}px`;
    }

    function rowsOf(answer) {
        const columns = answer?.columns || [];
        const row = answer?.rows?.[0];
        if (!row) return null;
        return Object.fromEntries(columns.map((name, i) => [name, row[i]]));
    }

    async function show(selection) {
        if (!selection?.surface) {
            close();
            return;
        }
        const layer = window.__plexora?.seaDragonViewer?.layerStack?.get(selection.surface);
        const spec = layer?.spec?.surface || {};
        const cells = spec.type === "label_surfaces";
        close();
        current = selection;
        card = el("section", "plx-surf-inspect");
        card.setAttribute("role", "dialog");
        const head = el("header", "plx-surf-inspect-head");
        head.appendChild(el("h3", null, cells ? `Cell ${selection.object}` : `Piece ${selection.object}`));
        head.appendChild(el("span", "plx-surf-inspect-of", layer?.label || ""));
        const shut = el("button", "plx-surf-icon-button");
        shut.type = "button";
        shut.setAttribute("aria-label", "Close");
        shut.innerHTML = '<i class="fas fa-xmark" aria-hidden="true"></i>';
        shut.addEventListener("click", () => service()?.select(null));
        head.appendChild(shut);
        card.appendChild(head);
        const facts = el("dl", "plx-surf-facts");
        facts.appendChild(el("dt", null, "Reading…"));
        card.appendChild(facts);
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
        const id = selection.surface;
        const object = Number(selection.object);
        action("fa-eye-slash", "Hide", "Hide this object", () => {
            service()?.hideObjects(id, [object]);
            service()?.select(null);
        });
        action("fa-bullseye", "Isolate", "Show only this object", () => service()?.isolate(id, [object]));
        action("fa-eye", "Show all", "Show every object again", () => {
            service()?.isolate(id, null);
            service()?.style(id, { hidden: null });
        });
        action("fa-crosshairs", "In planes", "Show the plane through this object, centred on it",
               () => goToPlanes(selection));
        card.appendChild(actions);

        const near = el("div", "plx-surf-near");
        const within = el("input", "plx-number plx-surf-number");
        within.type = "number";
        within.min = "0";
        within.step = "any";
        within.value = "5";
        within.setAttribute("aria-label", "Distance");
        const go = el("button", "plx-surf-link", "Neighbours");
        go.type = "button";
        const list = el("ol", "plx-surf-near-list");
        near.append(el("span", null, "Within"), within, el("span", "plx-surf-unit", "µm"), go);
        // Spatial neighbours are Plexora 3D: absent without it.
        if (window.PlexoraPaid?.allows?.("volume:surfaces")) {
            card.append(near, list);
            go.addEventListener("click", () => neighbours(id, object, Number(within.value) || 5, list));
        }

        place(card, selection.clientX || 0, selection.clientY || 0);

        try {
            const answer = await service()?.objects(id, [object]);
            if (current !== selection) return;
            const row = rowsOf(answer);
            facts.textContent = "";
            if (!row) {
                facts.appendChild(el("dt", null, "No measurements for this object"));
                return;
            }
            const physical = "mesh_area_um2" in row;
            const u = physical ? "µm" : "vox";
            const add = (label, text) => {
                facts.append(el("dt", null, label), el("dd", null, text));
            };
            if (row.voxel_count !== undefined && row.voxel_count !== null) {
                add("Voxel volume", row.voxel_volume_um3 !== undefined && row.voxel_volume_um3 !== null
                    ? `${value(row.voxel_volume_um3)} µm³ (${value(row.voxel_count, 6)} voxels)`
                    : `${value(row.voxel_count, 6)} voxels`);
            }
            add("Mesh volume", `${value(physical ? row.mesh_volume_um3 : row.mesh_volume_vox3)} ${u}³`
                + (row.closed ? "" : " (open)"));
            add("Mesh area", `${value(physical ? row.mesh_area_um2 : row.mesh_area_vox2)} ${u}²`);
            add("Sphericity", value(row.sphericity, 3));
            add("Elongation", value(row.elongation, 3));
            add("Centre", `${value(row.centroid_x, 5)}, ${value(row.centroid_y, 5)}, z ${value(row.centroid_z, 4)}`);
            if (row.touches_bounds) {
                const note = el("p", "plx-surf-warn", "Touches the edge of the volume: it is cut, "
                    + "and its volume is of what is inside.");
                card.insertBefore(note, actions);
            }
            selection.centroid = [row.centroid_x, row.centroid_y, row.centroid_z];
        } catch (error) {
            facts.textContent = error.message;
        }
    }

    async function neighbours(id, object, within, list) {
        list.textContent = "";
        list.appendChild(el("li", "plx-surf-muted", "Looking…"));
        const ds = encodeURIComponent(window.flaskVariables?.datasource || "");
        try {
            const response = await fetch(plexoraUrl(
                `volume/${ds}/surfaces/${encodeURIComponent(id)}/neighbours?object=${object}&within=${within}`));
            if (response.status === 403) {
                const body = await response.json().catch(() => ({}));
                if (body.license) window.PlexoraPaid?.explain(body.license);
                list.textContent = "";
                return;
            }
            const body = await response.json();
            list.textContent = "";
            if (!response.ok) {
                list.appendChild(el("li", "plx-surf-warn", body.error || "Failed"));
                return;
            }
            if (!body.neighbours.length) {
                list.appendChild(el("li", "plx-surf-muted", `Nothing within ${within} ${body.units === "um" ? "µm" : "voxels"}.`));
                return;
            }
            for (const row of body.neighbours.slice(0, 12)) {
                const item = el("li");
                const pick = el("button", "plx-surf-link", `#${row.object}`);
                pick.type = "button";
                pick.addEventListener("click", () => service()?.select({ surface: id, object: row.object,
                                                                         clientX: current?.clientX,
                                                                         clientY: current?.clientY }));
                item.append(pick, el("span", null, ` ${value(row.distance)} ${body.units === "um" ? "µm" : "vox"}`
                    + (row.contact_area ? ` · contact ${value(row.contact_area)} µm²` : "")));
                list.appendChild(item);
            }
            // The neighbours lit with the object, in the 3-D view.
            service()?.isolate(id, [object, ...body.neighbours.map((row) => row.object)]);
        } catch (error) {
            list.textContent = error.message;
        }
    }

    /** The plane through the object, centred on it: what the mask says there. */
    function goToPlanes(selection) {
        const vs = window.__plexora?.viewState;
        const point = selection.centroid || selection.point;
        if (!vs?.enabled?.() || !point) return;
        const z = Math.round(Number(point[2]));
        vs.set({ mode: "plane", z: Math.max(0, Math.min((vs.depth || 1) - 1, z)) });
        const viewer = window.__plexora?.seaDragonViewer;
        const item = viewer?.viewer?.world?.getItemAt?.(0);
        if (item && window.OpenSeadragon) {
            const centre = item.imageToViewportCoordinates(new OpenSeadragon.Point(point[0], point[1]));
            viewer.viewer.viewport.panTo(centre);
        }
    }

    function init() {
        window.addEventListener("plexora:surface-selected", (event) => show(event.detail));
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && card) service()?.select(null);
        });
    }

    return { init, show, close };
}());
