/**
 * surfaceObjects.js - a surface's objects as a table, coloured and exported from it.
 *
 * Every object of a surface, one row each, with its mesh measurements (and,
 * for cell surfaces, the mask's voxel ones beside them): sortable, filterable
 * by a range of any column, a row picks the object in the 3-D view. "Colour
 * by" a column paints the objects on a continuous ramp (low blue to high
 * yellow) through the surface's colour table; "Show only these" isolates what
 * the filter keeps. The export menu writes the mesh as PLY, OBJ, STL or glTF
 * with its manifest (all objects, or the filtered ones).
 *
 * Classic script: `window.PlexoraSurfaceObjects`.
 */
window.PlexoraSurfaceObjects = (function () {
    "use strict";

    const MAX_ROWS = 2000;
    const HIDDEN = new Set(["bbox_x0", "bbox_x1", "bbox_y0", "bbox_y1", "bbox_z0", "bbox_z1",
                            "centroid_x_um", "centroid_y_um", "centroid_z_um",
                            "centroid_x_vox", "centroid_y_vox", "centroid_z_vox"]);
    const LABELS = {
        object: "Id", n_triangles: "Triangles", closed: "Closed", touches_bounds: "Edge",
        mesh_volume_um3: "Mesh vol µm³", mesh_area_um2: "Mesh area µm²",
        mesh_volume_vox3: "Mesh vol vox³", mesh_area_vox2: "Mesh area vox²",
        equivalent_diameter_um: "Eq. diam µm", equivalent_diameter_vox: "Eq. diam vox",
        sphericity: "Sphericity", elongation: "Elongation", centroid_x: "X", centroid_y: "Y",
        centroid_z: "Z", voxel_count: "Voxels", voxel_volume_um3: "Voxel vol µm³",
        voxel_surface_area_um2: "Voxel area µm²", voxel_sphericity: "Voxel sphericity",
    };

    let panel = null;
    let state = null;

    function service() { return window.PlexoraSurfaceLayers || null; }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function fmt(value) {
        if (value === null || value === undefined) return "–";
        if (typeof value === "boolean") return value ? "yes" : "";
        const n = Number(value);
        return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumSignificantDigits: 4 }) : String(value);
    }

    /** A continuous ramp, low to high: viridis-like stops. */
    function ramp(f) {
        const stops = [[68, 1, 84], [59, 82, 139], [33, 145, 140], [94, 201, 98], [253, 231, 37]];
        const x = Math.min(1, Math.max(0, f)) * (stops.length - 1);
        const i = Math.min(stops.length - 2, Math.floor(x));
        const t = x - i;
        return stops[i].map((v, k) => Math.round(v + (stops[i + 1][k] - v) * t));
    }

    function close() {
        panel?.remove();
        panel = null;
        state = null;
    }

    function visibleRows() {
        const { rows, columns, sort, filter } = state;
        let out = rows;
        if (filter.column) {
            const k = columns.indexOf(filter.column);
            out = out.filter((row) => {
                const v = Number(row[k]);
                return (!Number.isFinite(filter.lo) || v >= filter.lo)
                    && (!Number.isFinite(filter.hi) || v <= filter.hi);
            });
        }
        if (sort.column) {
            const k = columns.indexOf(sort.column);
            out = out.slice().sort((a, b) => ((Number(a[k]) || 0) - (Number(b[k]) || 0)) * sort.dir);
        }
        return out;
    }

    function paintTable() {
        const { columns, table, count } = state;
        const shown = columns.filter((name) => !HIDDEN.has(name));
        table.textContent = "";
        const head = el("thead");
        const tr = el("tr");
        for (const name of shown) {
            const th = el("th");
            const button = el("button", "plx-surf-th", LABELS[name] || name);
            button.type = "button";
            if (state.sort.column === name) button.dataset.dir = state.sort.dir > 0 ? "up" : "down";
            button.addEventListener("click", () => {
                state.sort = { column: name, dir: state.sort.column === name ? -state.sort.dir : -1 };
                paintTable();
            });
            th.appendChild(button);
            tr.appendChild(th);
        }
        head.appendChild(tr);
        table.appendChild(head);
        const body = el("tbody");
        const rows = visibleRows();
        const objectAt = columns.indexOf("object");
        for (const row of rows.slice(0, MAX_ROWS)) {
            const line = el("tr");
            line.tabIndex = 0;
            for (const name of shown) line.appendChild(el("td", null, fmt(row[columns.indexOf(name)])));
            const pick = () => service()?.select({ surface: state.id, object: row[objectAt] });
            line.addEventListener("click", pick);
            line.addEventListener("keydown", (event) => {
                if (event.key === "Enter") pick();
            });
            body.appendChild(line);
        }
        table.appendChild(body);
        count.textContent = rows.length === state.rows.length
            ? `${rows.length.toLocaleString()} objects`
            : `${rows.length.toLocaleString()} of ${state.rows.length.toLocaleString()} objects`;
        if (rows.length > MAX_ROWS) count.textContent += ` (first ${MAX_ROWS} listed)`;
    }

    function numericColumns() {
        const { columns, rows } = state;
        return columns.filter((name, k) => name !== "object"
            && rows.some((row) => typeof row[k] === "number"));
    }

    function colourBy(name) {
        const id = state.id;
        if (!name) {
            service()?.setTableLut(id, null);
            service()?.style(id, { colorBy: "uniform" });
            return;
        }
        const { columns, rows } = state;
        const k = columns.indexOf(name);
        const objectAt = columns.indexOf("object");
        const values = rows.map((row) => Number(row[k])).filter(Number.isFinite).sort((a, b) => a - b);
        if (!values.length) return;
        // The 2nd to 98th percentile spans the ramp, so one outlier does not
        // flatten everything else to one colour.
        const lo = values[Math.floor(values.length * 0.02)];
        const hi = values[Math.min(values.length - 1, Math.floor(values.length * 0.98))];
        let maxId = 0;
        for (const row of rows) maxId = Math.max(maxId, Number(row[objectAt]) || 0);
        const colors = new Uint8Array((maxId + 1) * 4);
        for (const row of rows) {
            const v = Number(row[k]);
            const rgb = Number.isFinite(v) ? ramp((v - lo) / Math.max(1e-12, hi - lo)) : [120, 120, 120];
            colors.set([...rgb, 255], Number(row[objectAt]) * 4);
        }
        service()?.setTableLut(id, { colors, maxId });
        service()?.style(id, { colorBy: "table" });
        state.legend.textContent = `${LABELS[name] || name}: ${fmt(lo)} → ${fmt(hi)}`;
    }

    async function open(id) {
        close();
        const layer = window.__plexora?.seaDragonViewer?.layerStack?.get(id);
        panel = el("section", "plx-surf-objects");
        panel.setAttribute("role", "dialog");
        panel.setAttribute("aria-label", "Surface objects");
        const head = el("header", "plx-surf-studio-head");
        const glyph = el("i", "fas fa-table-list plx-surf-studio-mark");
        glyph.setAttribute("aria-hidden", "true");
        head.append(glyph, el("h2", "plx-surf-studio-title", layer?.label || "Objects"));
        const shut = el("button", "plx-surf-icon-button");
        shut.type = "button";
        shut.setAttribute("aria-label", "Close the objects table");
        shut.innerHTML = '<i class="fas fa-xmark" aria-hidden="true"></i>';
        shut.addEventListener("click", close);
        head.appendChild(shut);
        panel.appendChild(head);
        const tools = el("div", "plx-surf-objects-tools");
        const colour = el("select", "plx-surf-select");
        colour.setAttribute("aria-label", "Colour objects by");
        const filterColumn = el("select", "plx-surf-select");
        filterColumn.setAttribute("aria-label", "Filter by");
        const lo = el("input", "plx-number plx-surf-number");
        lo.type = "number";
        lo.placeholder = "min";
        const hi = el("input", "plx-number plx-surf-number");
        hi.type = "number";
        hi.placeholder = "max";
        const only = el("button", "plx-surf-link", "Show only these");
        only.type = "button";
        const all = el("button", "plx-surf-link", "Show all");
        all.type = "button";
        tools.append(el("span", "plx-surf-studio-label", "Colour"), colour,
                     el("span", "plx-surf-studio-label", "Filter"), filterColumn, lo, hi, only, all);
        panel.appendChild(tools);
        const legend = el("p", "plx-surf-objects-legend");
        panel.appendChild(legend);
        const scroller = el("div", "plx-surf-objects-scroll");
        const table = el("table", "plx-surf-table");
        scroller.appendChild(table);
        panel.appendChild(scroller);
        // Edits: each a new version of the mesh (server/surface/refine.py);
        // Undo steps back. The mask is never touched.
        const edits = el("div", "plx-surf-objects-tools plx-surf-objects-edits");
        const minimum = el("input", "plx-number plx-surf-number");
        minimum.type = "number";
        minimum.min = "0";
        minimum.placeholder = "size";
        const removeSmall = el("button", "plx-surf-link", "Remove smaller");
        removeSmall.type = "button";
        const keepShown = el("button", "plx-surf-link", "Keep only listed");
        keepShown.type = "button";
        keepShown.title = "Keep the objects the filter lists, as a new version";
        const undo = el("button", "plx-surf-link", "Undo last edit");
        undo.type = "button";
        const editNote = el("span", "plx-surf-studio-label");
        edits.append(el("span", "plx-surf-studio-label", "Edit"), minimum, removeSmall, keepShown, undo,
                     editNote);
        panel.appendChild(edits);
        const runEdit = async (body) => {
            editNote.textContent = "Working…";
            try {
                const answer = await service()?.refine(id, body);
                if (!answer) {
                    editNote.textContent = "";
                    return;
                }
                editNote.textContent = answer.note || "Done";
                const fresh = await service()?.objects(id);
                if (state && state.id === id && fresh) {
                    state.rows = fresh.rows || [];
                    state.columns = fresh.columns || [];
                    paintTable();
                }
            } catch (error) {
                editNote.textContent = error.message;
            }
        };
        removeSmall.addEventListener("click", () => {
            const value = parseFloat(minimum.value);
            if (Number.isFinite(value) && value > 0) runEdit({ op: "remove_small", min_size: value });
        });
        keepShown.addEventListener("click", () => {
            const k = state.columns.indexOf("object");
            const keep = visibleRows().map((row) => row[k]);
            if (keep.length && keep.length < state.rows.length) runEdit({ op: "keep", ids: keep });
        });
        undo.addEventListener("click", () => runEdit({ op: "undo" }));

        const foot = el("footer", "plx-surf-studio-foot");
        const count = el("p", "plx-surf-studio-status", "Reading…");
        const exporter = el("button", "plx-surf-link", "Export…");
        exporter.type = "button";
        exporter.addEventListener("click", (event) => exportMenu(id, event.currentTarget, true));
        foot.append(count, exporter);
        panel.appendChild(foot);
        document.getElementById("openseadragon_wrapper")?.appendChild(panel);
        state = { id, table, count, legend, rows: [], columns: [], sort: { column: null, dir: -1 },
                  filter: { column: null, lo: NaN, hi: NaN } };
        try {
            const answer = await service()?.objects(id);
            if (!state || state.id !== id) return;
            state.rows = answer?.rows || [];
            state.columns = answer?.columns || [];
        } catch (error) {
            count.textContent = error.message;
            return;
        }
        const numeric = numericColumns();
        const none = el("option", null, "One colour");
        none.value = "";
        colour.appendChild(none);
        const noFilter = el("option", null, "No filter");
        noFilter.value = "";
        filterColumn.appendChild(noFilter);
        for (const name of numeric) {
            for (const target of [colour, filterColumn]) {
                const option = el("option", null, LABELS[name] || name);
                option.value = name;
                target.appendChild(option);
            }
        }
        colour.addEventListener("change", () => colourBy(colour.value || null));
        const applyFilter = () => {
            state.filter = { column: filterColumn.value || null, lo: parseFloat(lo.value),
                             hi: parseFloat(hi.value) };
            paintTable();
        };
        filterColumn.addEventListener("change", applyFilter);
        lo.addEventListener("change", applyFilter);
        hi.addEventListener("change", applyFilter);
        only.addEventListener("click", () => {
            const k = state.columns.indexOf("object");
            service()?.isolate(id, visibleRows().map((row) => row[k]));
        });
        all.addEventListener("click", () => {
            service()?.isolate(id, null);
            service()?.style(id, { hidden: null });
        });
        paintTable();
    }

    /** The export choices for surface `id`, as a small menu by `anchor`. */
    function exportMenu(id, anchor, filtered = false) {
        document.querySelector(".plx-surf-menu")?.remove();
        const menu = el("div", "plx-surf-menu");
        menu.setAttribute("role", "menu");
        const ds = encodeURIComponent(window.flaskVariables?.datasource || "");
        const ids = filtered && state && state.id === id && state.filter.column
            ? visibleRows().map((row) => row[state.columns.indexOf("object")]) : null;
        for (const [fmt, label] of [["ply", "PLY (object ids)"], ["obj", "OBJ (a group per object)"],
                                    ["stl", "STL (no ids)"], ["glb", "glTF binary (.glb)"]]) {
            const item = el("a", "plx-surf-menu-item", label);
            item.setAttribute("role", "menuitem");
            const query = new URLSearchParams({ format: fmt });
            if (ids) query.set("ids", ids.join(","));
            item.href = plexoraUrl(`volume/${ds}/surfaces/${encodeURIComponent(id)}/export?${query}`);
            item.setAttribute("download", "");
            item.addEventListener("click", () => menu.remove());
            menu.appendChild(item);
        }
        menu.appendChild(el("p", "plx-surf-menu-note",
                            "Zipped with its manifest. Microns when the z step is known."));
        document.body.appendChild(menu);
        const box = anchor?.getBoundingClientRect?.() || { left: 40, bottom: 40 };
        menu.style.left = `${Math.max(8, Math.min(window.innerWidth - 240, box.left))}px`;
        menu.style.top = `${Math.min(window.innerHeight - 180, box.bottom + 4)}px`;
        const away = (event) => {
            if (!menu.contains(event.target)) {
                menu.remove();
                document.removeEventListener("pointerdown", away, true);
            }
        };
        setTimeout(() => document.addEventListener("pointerdown", away, true), 0);
    }

    return { open, close, exportMenu, ramp };
}());
