// Prepare data: the projects a notebook analyzes, the dataset they make, their
// columns and their sample metadata -- managed here, written to Plexora's own
// records (server/data.py), never to a copy. Then the working AnnData, and
// Scimap Pro's own answer to how it will read it (io.init).
//
// Plexora stays the one source of truth: the workspace polls a records stamp
// and redraws this step when a project or dataset changed anywhere.
window.ScimapPrepare = (function () {
    "use strict";
    const { h, icon, button, n, tildePath } = window.ScimapDom;
    const API = window.ScimapApi;
    const NUCLEAR = /(dna|dapi|hoechst|nucle|^h3$|draq|sytox)/i;

    function grid() {
        return h("div.sm-prep-grid");
    }

    function row(gridNode, label, ...control) {
        gridNode.append(h("span.sm-prep-label", null, label), h("div.sm-prep-control", null, ...control));
    }

    // -- configure: the manager -------------------------------------------------

    function manager(cell, ctx) {
        const local = ctx.local(cell.id);
        const box = h("div.sm-prep", null, h("div.sm-prep-loading", null, "Reading the projects…"));
        const load = () => API.data(ctx.nb.id).then((a) => {
            local.data = a.data;
            paint();
        }).catch((err) => {
            box.innerHTML = "";
            box.append(h("div.sm-failure", null, h("p", null, err.message)));
        });
        const paint = () => {
            const data = local.data;
            box.innerHTML = "";
            const g = grid();
            row(g, "Projects", projectsBlock(cell, ctx, data, reload));
            row(g, "Dataset", datasetLine(cell, ctx, data, reload));
            row(g, "Sample metadata", samplesBlock(cell, ctx, data, reload));
            const markers = markersLine(cell, ctx, data);
            if (markers) row(g, "Markers", markers);
            if (data.rows.some((r) => r.type === "csv" || r.type === "parquet")) row(g, "Intensities", intensities(cell, ctx, data));
            row(g, "Results folder", folderBlock(cell, ctx, data));
            box.append(g, runRow(cell, ctx, data));
        };
        const reload = (fresh) => {
            // A row opening for editing, a column added: the manager slides
            // to its new height rather than jumping.
            if (fresh) { local.data = fresh; window.ScimapDom.morph(box, paint); return Promise.resolve(); }
            return load();
        };
        if (local.data && local.dataStamp === ctx.stamp()) paint();
        else load().then(() => { local.dataStamp = ctx.stamp(); });
        return box;
    }

    function projectsBlock(cell, ctx, data, reload) {
        const local = ctx.local(cell.id);
        const dataset = data.context.kind === "dataset";
        const table = h("div.sm-ptable");
        table.append(h("div.sm-ptable-row.is-head", null, h("span"), h("span", null, "Project"), h("span", null, "Table"),
            h("span.is-num", null, "Cells"), h("span.is-num", null, "Markers"), h("span.is-cols", null, "Columns"), h("span")));
        data.rows.forEach((r) => {
            const open = local.editing === r.project;
            const use = h("input.sm-check", { type: "checkbox", checked: true, "aria-label": "Use " + r.project,
                disabled: (!dataset || data.rows.length < 2) || null,
                title: !dataset ? "Add projects to make a dataset" : data.rows.length < 2 ? "A dataset keeps at least one project" : "Remove from the dataset" });
            use.addEventListener("change", () => removeProject(ctx, r.project, data, reload));
            const warn = r.has_table && r.missing && r.missing.length;
            const status = h("span.sm-ptable-status" + (r.ok ? ".is-ok" : ".is-warn"), null,
                icon(r.ok ? "check" : "warning", "xs"), r.status);
            const edit = r.has_table ? button(open ? "Close" : warn ? "Set columns" : "Edit data", {
                class: (open ? "is-open " : "") + (warn && !open ? "is-warn " : "") + "sm-btn-sm",
                onclick: () => { local.editing = open ? null : r.project; reload(local.data); },
            }) : button("Import data…", { class: "sm-btn-sm", onclick: () => importInto(ctx, r.project, data, reload) });
            const more = h("button.sm-icon-btn.sm-icon-btn-sm", { type: "button", "aria-label": "More for " + r.project,
                onclick: (e) => ctx.menu(e.currentTarget, [
                    { label: "Open in the viewer", icon: "tv", onSelect: () => { window.location.href = ctx.viewerUrl(r.project); } },
                    { label: "Import data into " + r.project + "…", icon: "upload", onSelect: () => importInto(ctx, r.project, data, reload) },
                    { sep: true },
                    { label: "Remove from the dataset", icon: "x", disabled: !dataset || data.rows.length < 2,
                      onSelect: () => removeProject(ctx, r.project, data, reload) },
                ]) }, icon("dots"));
            table.append(h("div.sm-ptable-row" + (warn ? ".is-warn" : "") + (open ? ".is-open" : ""), null,
                use, h("span.sm-ptable-name", null, r.project), h("span.sm-kind", null, r.kind || "—"),
                h("span.is-num", null, r.cells === null || r.cells === undefined ? "—" : n(r.cells)),
                h("span.is-num", null, r.has_table ? String(r.markers) : "—"), status,
                h("div.sm-ptable-actions", null, edit, more)));
            if (open) table.append(editPanel(cell, ctx, r, data, reload));
        });
        const used = data.rows.length;
        const actions = h("div.sm-prep-actions", null,
            addProjectsButton(cell, ctx, data, reload),
            button("New project…", { icon: "folderPlus", class: "sm-btn-sm", onclick: () => newProject(ctx, data, reload) }),
            button("Import data…", { icon: "upload", class: "sm-btn-sm", onclick: (e) => {
                if (data.rows.length === 1) return importInto(ctx, data.rows[0].project, data, reload);
                ctx.menu(e.currentTarget, data.rows.map((r) => ({ label: "Into " + r.project, onSelect: () => importInto(ctx, r.project, data, reload) })));
            } }),
            h("span.sm-adv-note", null, used + " of " + used + " used · " + n(data.cells) + " cells"));
        return h("div.sm-prep-stack", null, table, actions,
            h("span.sm-adv-note", null, "Edits here are made to the projects themselves, so the viewer and every notebook see the same data."));
    }

    function addProjectsButton(cell, ctx, data, reload) {
        const btn = button("Add projects", { icon: "plus", class: "sm-btn-sm" });
        btn.append(icon("chevD", "xs"));
        btn.setAttribute("aria-haspopup", "menu");
        btn.addEventListener("click", () => {
            const picked = new Set();
            const pop = h("div.sm-pop.sm-addp", { role: "menu", "aria-label": "Add projects" });
            const search = h("input", { placeholder: "Search projects", "aria-label": "Search projects" });
            const list = h("div.sm-addp-list");
            const foot = h("div.sm-addp-foot");
            const paint = () => {
                list.innerHTML = "";
                const q = search.value.trim().toLowerCase();
                data.available.filter((p) => !q || p.project.toLowerCase().includes(q)).forEach((p) => {
                    const box = h("input.sm-check", { type: "checkbox", checked: picked.has(p.project) || null, disabled: !p.has_table || null });
                    box.addEventListener("change", () => { if (box.checked) picked.add(p.project); else picked.delete(p.project); paintFoot(); });
                    list.append(h("label.sm-addp-item" + (p.has_table ? "" : ".is-off"), null, box,
                        h("span.sm-addp-name", null, p.project), h("span.sm-spacer"),
                        h("span.sm-muted", null, p.has_table ? (p.cells !== null && p.cells !== undefined ? n(p.cells) + " cells" : p.type || "") : "No cell table")));
                });
                if (!list.childNodes.length) list.append(h("span.sm-addp-empty", null, data.available.length ? "No project matches." : "Every project is already here."));
            };
            const paintFoot = () => {
                foot.innerHTML = "";
                const count = picked.size;
                const where = data.context.kind === "dataset" ? "join " + data.context.name
                    : "makes a dataset with " + data.context.name;
                foot.append(h("span.sm-adv-note", null, count ? count + " selected · " + (count === 1 ? "it " : count === 2 ? "both " : "they ") + where : "Choose projects to add"),
                    h("span.sm-spacer"),
                    button(count ? "Add " + count + (count === 1 ? " project" : " projects") : "Add", { kind: "primary", class: "sm-btn-sm", disabled: !count || null,
                        onclick: async () => {
                            close();
                            try {
                                const a = await API.editData(ctx.nb.id, { add: Array.from(picked) });
                                ctx.adopt(a.view);
                                reload(a.data);
                                ctx.toast(count === 1 ? "Added " + Array.from(picked)[0] + "." : "Added " + count + " projects.");
                            } catch (err) { ctx.toast(err.message); }
                        } }));
            };
            search.addEventListener("input", paint);
            pop.append(h("label.sm-addp-search", null, icon("search", "xs"), search), list, h("div.sm-rule-line"), foot);
            const close = ctx.popover(btn, pop);
            paint();
            paintFoot();
            setTimeout(() => search.focus(), 0);
        });
        return btn;
    }

    async function removeProject(ctx, name, data, reload) {
        try {
            const a = await API.editData(ctx.nb.id, { remove: name });
            ctx.adopt(a.view);
            reload(a.data);
            ctx.toast(name + " left " + data.context.name + ".", { label: "Undo", run: async () => {
                const b = await API.editData(ctx.nb.id, { add: [name] });
                ctx.adopt(b.view);
                reload(b.data);
            } });
        } catch (err) {
            ctx.toast(err.message);
            reload();
        }
    }

    function newProject(ctx, data, reload) {
        const importer = window.PlexoraImportSample;
        if (!importer || typeof importer.open !== "function") {
            ctx.toast("Plexora's import is not available on this page.");
            return;
        }
        importer.open({
            stay: true,
            dataset: data.context.kind === "dataset" ? { id: data.context.id, name: data.context.name } : undefined,
            onDone: async (done) => {
                const names = (done && (done.projects || [done.project])) || [];
                try {
                    const a = await API.editData(ctx.nb.id, { add: names.filter(Boolean) });
                    ctx.adopt(a.view);
                    reload(a.data);
                } catch (err) {
                    ctx.toast(err.message);
                    reload();
                }
            },
        });
    }

    function importInto(ctx, project, data, reload) {
        const importer = window.PlexoraImportSample;
        if (!importer || typeof importer.open !== "function") {
            ctx.toast("Plexora's import is not available on this page.");
            return;
        }
        importer.open({ sample: project, stay: true, onDone: () => reload() });
    }

    // -- edit data in place --------------------------------------------------

    function editPanel(cell, ctx, r, data, reload) {
        const local = ctx.local(cell.id);
        const panel = h("div.sm-pedit", null, h("div.sm-prep-loading", null, "Reading " + r.project + "'s table…"));
        API.projectColumns(ctx.nb.id, r.project).then((a) => {
            const info = a.project;
            const draft = local.draft && local.draft.project === r.project ? local.draft
                : (local.draft = { project: r.project, roles: Object.assign({}, info.roles), markers: info.marker_names.slice(),
                                    metadata: (info.metadata || []).slice(), pixel_size: info.pixel_size, table_src: null });
            panel.innerHTML = "";
            const g = h("div.sm-pedit-grid");
            const add = (label, ...control) => g.append(h("span.sm-prep-label", null, label), h("div.sm-prep-control", null, ...control));
            const tableLine = h("div.sm-prep-inline", null, h("span.sm-mono-text", null, tildePath(draft.table_src || info.src)),
                button("Replace…", { class: "sm-btn-sm", onclick: async () => {
                    const picker = window.PlexoraPathPicker || window.PlexoraFileExplorer;
                    if (!picker || typeof picker.pick !== "function") { ctx.toast("Type the table's path in Plexora's project page."); return; }
                    const chosen = await picker.pick({ mode: "file", title: "Choose the new cell table", start: info.src });
                    const path = chosen && (typeof chosen === "string" ? chosen : chosen.path || chosen[0]);
                    if (path) { draft.table_src = path; panel.replaceWith(editPanel(cell, ctx, r, data, reload)); }
                } }));
            const facts = [n(info.cells) + " cells", (info.n_vars ?? info.markers) + " " + (info.type === "anndata" ? "variables" : "markers")];
            if (info.type === "anndata") {
                facts.push("obs has " + info.columns.length + " columns");
                facts.push(info.raw ? "raw layer kept" : "no raw layer (X is used" + (info.log_transformed ? ", already logged)" : ")"));
            }
            add("Cell table", h("div.sm-prep-stack.is-tight", null, tableLine, h("span.sm-adv-note", null, facts.join(" · "))));
            const choices = info.columns || [];
            const roleRow = (key, label, optional) => {
                const value = draft.roles[key] || "";
                const missing = !optional && (r.missing || []).includes(key) && !value;
                const select = h("select", { "aria-label": label + " column" },
                    h("option", { value: "" }, optional ? "None" : "Choose…"),
                    choices.map((c) => h("option", { value: c, selected: c === value || null }, c)));
                select.addEventListener("change", () => { draft.roles[key] = select.value || null; paintSave(); });
                return h("label.sm-role-pick" + (missing ? ".is-warn" : ""), null,
                    h("span", null, label, optional ? h("span.sm-muted", null, " (optional)") : null), select);
            };
            const imageRow = () => {
                const select = h("select", { "aria-label": "Image column" },
                    h("option", { value: "" }, "One image (" + r.project + ")"),
                    choices.map((c) => h("option", { value: c, selected: c === draft.roles.image_id || null }, c)));
                select.addEventListener("change", () => { draft.roles.image_id = select.value || null; paintSave(); });
                return h("label.sm-role-pick", null, h("span", null, "Image"), select);
            };
            const pixel = h("input", { "aria-label": "Pixel size", value: draft.pixel_size ?? "", inputmode: "decimal", placeholder: "—" });
            pixel.addEventListener("change", () => { draft.pixel_size = pixel.value.trim() === "" ? null : pixel.value.trim(); paintSave(); });
            add("Columns", h("div.sm-prep-stack", null,
                h("div.sm-role-grid", null, roleRow("cell_id", "Cell ID"), imageRow(), roleRow("x", "X position"), roleRow("y", "Y position"),
                    roleRow("celltype", "Cell type", true),
                    h("label.sm-role-pick", null, h("span", null, "Pixel size"), h("span.sm-prep-inline", null, pixel, h("span.sm-muted", null, "µm/px")))),
                h("span.sm-adv-note", null, "Plexora's own column questions, answered here. Suggestions come from the table; nothing is guessed silently.")));
            const chips = h("div.sm-mm-chips");
            const paintChips = () => {
                chips.innerHTML = "";
                const show = local.allMarkers ? draft.markers : draft.markers.slice(0, 10);
                show.forEach((m) => chips.append(h("button.sm-mm.is-marker", { type: "button", title: "Move to metadata",
                    onclick: () => { draft.markers = draft.markers.filter((x) => x !== m); draft.metadata.push(m); paintChips(); paintSave(); } }, m)));
                if (!local.allMarkers && draft.markers.length > 10) {
                    chips.append(h("button.sm-btn-link", { type: "button", onclick: () => { local.allMarkers = true; paintChips(); } },
                        "and " + (draft.markers.length - 10) + " more markers"));
                }
                if (draft.metadata.length) chips.append(h("span.sm-mm-rule"));
                draft.metadata.forEach((m) => chips.append(h("button.sm-mm.is-meta", { type: "button", title: "Move to markers",
                    onclick: () => { draft.metadata = draft.metadata.filter((x) => x !== m); draft.markers.push(m); paintChips(); paintSave(); } }, m)));
            };
            paintChips();
            add("Markers and metadata", h("div.sm-prep-stack", null, chips,
                h("span.sm-adv-note", null, "Filled: markers. Outlined: metadata. Click one to move it to the other group.")));
            const image = info.image;
            const mask = info.mask;
            add("Image and mask", h("div.sm-prep-stack", null,
                h("div.sm-prep-inline", null, image ? icon("image", "sm") : h("span.sm-muted", null, "—"), h("span", null, "Image"),
                    h("span.sm-mono-text.sm-muted", null, image ? (String(image.src || "").split("/").pop() + (image.channels ? " · " + image.channels.length + " channels" : "")) : "none"),
                    image ? null : button("Add image…", { class: "sm-btn-sm", onclick: () => importInto(ctx, r.project, data, reload) })),
                h("div.sm-prep-inline", null, mask ? icon("layers", "sm") : h("span.sm-muted", null, "—"), h("span", null, "Segmentation mask"),
                    h("span.sm-muted", null, mask ? String(mask.src || "").split("/").pop() : "none"),
                    mask ? null : button("Add mask…", { class: "sm-btn-sm", onclick: () => importInto(ctx, r.project, data, reload) })),
                h("span.sm-adv-note", null, "Add mask… and Import data… open Plexora's import and come back to this step when it finishes.")));
            const save = button("Save to " + r.project, { kind: "primary", class: "sm-btn-sm" });
            const why = h("span.sm-adv-note");
            const paintSave = () => {
                const needed = info.type === "anndata" ? [] : ["cell_id", "x", "y"];
                const gap = needed.find((k) => !draft.roles[k]);
                const words = { cell_id: "Cell ID", x: "X", y: "Y" };
                save.disabled = !!gap;
                why.textContent = gap ? "Choose the " + words[gap] + " column to save" : "Saves to the project in Plexora";
            };
            save.addEventListener("click", async () => {
                save.disabled = true;
                try {
                    const edits = { roles: draft.roles, markers: draft.markers, metadata: draft.metadata,
                                    pixel_size: draft.pixel_size, table_src: draft.table_src };
                    const a = await API.editProject(ctx.nb.id, r.project, edits);
                    local.editing = null;
                    local.draft = null;
                    ctx.adopt(a.view);
                    reload(a.data);
                    ctx.toast("Saved to " + r.project + ".");
                } catch (err) {
                    ctx.toast(err.message);
                    paintSave();
                }
            });
            paintSave();
            panel.append(g, h("div.sm-pedit-foot", null, save,
                button("Cancel", { kind: "quiet", class: "sm-btn-sm", onclick: () => { local.editing = null; local.draft = null; reload(local.data); } }),
                h("span.sm-spacer"), why));
        }).catch((err) => {
            panel.innerHTML = "";
            panel.append(h("div.sm-failure", null, h("p", null, err.message)));
        });
        return panel;
    }

    // -- dataset, samples, markers, folder -------------------------------------

    function datasetLine(cell, ctx, data, reload) {
        const local = ctx.local(cell.id);
        if (data.context.kind !== "dataset") {
            return h("div.sm-prep-inline", null, icon("database", "sm"),
                h("span", null, "One project. Add projects to make a dataset of them in Plexora."));
        }
        if (local.renaming) {
            const input = h("input.sm-input", { value: data.context.name, "aria-label": "Dataset name", style: { width: "260px" } });
            const done = async () => {
                local.renaming = false;
                const name = input.value.trim();
                if (!name || name === data.context.name) { reload(local.data); return; }
                try {
                    const a = await API.editData(ctx.nb.id, { rename: name });
                    ctx.adopt(a.view);
                    reload(a.data);
                } catch (err) { ctx.toast(err.message); reload(); }
            };
            input.addEventListener("keydown", (e) => { if (e.key === "Enter") done(); if (e.key === "Escape") { local.renaming = false; reload(local.data); } });
            input.addEventListener("blur", done);
            setTimeout(() => { input.focus(); input.select(); }, 0);
            return h("div.sm-prep-inline", null, icon("database", "sm"), input);
        }
        return h("div.sm-prep-inline", null, icon("database", "sm"),
            h("span", null, "These projects are the dataset ", h("strong", null, data.context.name), "."),
            h("button.sm-btn-link", { type: "button", onclick: () => { local.renaming = true; reload(local.data); } }, "Rename"),
            h("span.sm-adv-note", null, "Adding or removing a project here changes the dataset in Plexora."));
    }

    function samplesBlock(cell, ctx, data, reload) {
        const local = ctx.local(cell.id);
        const waits = (ctx.status && ctx.status.needs && ctx.status.needs.metadata) || "";
        const table = local.samples && local.samplesStamp === data.stamp ? local.samples
            : (local.samples = JSON.parse(JSON.stringify(data.samples || { columns: [], rows: {} })));
        local.samplesStamp = data.stamp;
        const images = data.images || [];
        let timer = null;
        const save = () => {
            clearTimeout(timer);
            timer = setTimeout(async () => {
                try {
                    const a = await API.saveSamples(ctx.nb.id, table);
                    local.data.samples = a.samples;
                    local.data.match = a.match;
                    ctx.adopt(a.view);
                    paintMatch(a.match);
                } catch (err) { ctx.toast(err.message); }
            }, 500);
        };
        const cols = table.columns || [];
        const template = "minmax(0, 1.3fr) " + cols.map(() => "minmax(0, 1fr)").join(" ") + " 36px";
        const sheet = h("div.sm-meta");
        const headRow = h("div.sm-meta-row.is-head", { style: { gridTemplateColumns: template } },
            h("span.sm-meta-key", null, h("code", null, "imageid"), icon("key2", "xs")));
        cols.forEach((c, i) => {
            const name = h("input.sm-meta-col", { value: c, "aria-label": "Column name", spellcheck: "false" });
            name.addEventListener("change", () => {
                const next = name.value.trim();
                if (!next) {
                    // An emptied name removes the column.
                    table.columns.splice(i, 1);
                    Object.values(table.rows).forEach((v) => { delete v[c]; });
                } else if (next !== c && !table.columns.includes(next)) {
                    table.columns[i] = next;
                    Object.values(table.rows).forEach((v) => { v[next] = v[c]; delete v[c]; });
                }
                save();
                reload(local.data);
            });
            headRow.append(name);
        });
        headRow.append(h("button.sm-meta-add", { type: "button", "aria-label": "Add a column", title: "Add a column",
            onclick: () => {
                let i = (table.columns || []).length + 1;
                while ((table.columns || []).includes("column_" + i)) i += 1;
                table.columns = (table.columns || []).concat(["column_" + i]);
                reload(local.data);
                setTimeout(() => { const all = document.querySelectorAll(".sm-meta-col"); if (all.length) { all[all.length - 1].focus(); all[all.length - 1].select(); } }, 0);
            } }, icon("plus", "xs")));
        sheet.append(headRow);
        images.forEach((image) => {
            const r = h("div.sm-meta-row", { style: { gridTemplateColumns: template } }, h("span.sm-meta-image", null, image));
            cols.forEach((c) => {
                const value = ((table.rows || {})[image] || {})[c] || "";
                const input = h("input.sm-meta-cell", { value, placeholder: "—", "aria-label": c + " for " + image });
                input.addEventListener("input", () => {
                    table.rows = table.rows || {};
                    table.rows[image] = table.rows[image] || {};
                    table.rows[image][c] = input.value;
                    save();
                });
                r.append(input);
            });
            r.append(h("span.sm-meta-end"));
            sheet.append(r);
        });
        const match = h("span.sm-meta-match");
        const paintMatch = (m) => {
            match.innerHTML = "";
            const filled = Object.values(table.rows || {}).some((v) => Object.values(v || {}).some((x) => String(x || "").trim()));
            if (!cols.length || !filled) return;
            const ok = m && m.matched === m.images;
            match.className = "sm-meta-match" + (ok ? " is-ok" : " is-warn");
            match.append(icon(ok ? "check" : "warning", "xs"), (m ? m.matched : 0) + " of " + (m ? m.images : images.length) + " images matched");
            if (m && m.unknown && m.unknown.length) match.append(h("span.sm-muted", null, " · " + m.unknown.length + " not in the data"));
        };
        paintMatch(data.match);
        const file = h("input", { type: "file", accept: ".csv,.tsv,.txt,.xlsx,.xls", hidden: true });
        file.addEventListener("change", async () => {
            const chosen = file.files && file.files[0];
            if (!chosen) return;
            try {
                const a = await API.importSamples(ctx.nb.id, chosen);
                local.samples = a.samples;
                Object.assign(table, a.samples);
                const b = await API.saveSamples(ctx.nb.id, a.samples);
                local.data.samples = b.samples;
                local.data.match = b.match;
                ctx.adopt(b.view);
                reload(local.data);
                ctx.toast("Read " + a.samples.columns.length + (a.samples.columns.length === 1 ? " column" : " columns") + " from " + chosen.name + ".");
            } catch (err) { ctx.toast(err.message); }
        });
        return h("div.sm-prep-stack", null,
            cols.length ? sheet : h("div.sm-meta-empty", null,
                h("span", null, "No sample metadata yet."),
                h("button.sm-btn-link", { type: "button", onclick: () => { table.columns = ["patient"]; reload(local.data); } }, "Add a column")),
            h("div.sm-prep-inline", null,
                button("Import CSV or Excel…", { icon: "upload", class: "sm-btn-sm", onclick: () => file.click() }), file,
                match, cols.length ? h("span.sm-adv-note", null, "An empty cell stays empty in the data.") : null),
            h("span.sm-adv-note", null, "Matched to the cells by ", h("code", null, "imageid"), ". Kept once, with the " +
                (data.context.kind === "dataset" ? "dataset" : "project") + " in Plexora; each column is written to every cell of its image when the data is prepared."),
            waits ? h("span.sm-adv-note.is-warn", null, icon("warning", "xs"), " Saved with the " +
                (data.context.kind === "dataset" ? "dataset" : "project") + ". " + waits) : null);
    }

    function markersLine(cell, ctx, data) {
        const common = (data.markers && data.markers.common) || [];
        if (!common.length) return null;
        const params = cell.params || {};
        const chosen = params.markers && params.markers.length ? params.markers.filter((m) => common.includes(m)) : common;
        const projects = data.rows.filter((r) => r.has_table).length;
        const out = data.markers.left_out || [];
        const notes = [];
        if (out.length) notes.push((projects > 1 ? "markers not in every project are left out (" : "left out (") + out.slice(0, 3).join(", ") + (out.length > 3 ? "…" : "") + ")");
        const nuclear = common.filter((m) => NUCLEAR.test(m));
        if (nuclear.length && chosen.some((m) => NUCLEAR.test(m))) notes.push(nuclear.join(", ") + " kept as " + (nuclear.length > 1 ? "markers" : "a marker"));
        return h("div.sm-prep-inline", null,
            h("span", null, chosen.length + " markers" + (projects > 1 ? (projects === 2 ? " in both projects" : " in all " + projects + " projects") : "")),
            notes.length ? h("span.sm-adv-note", null, notes.join("; ")) : null,
            button("Choose markers", { class: "sm-btn-sm", onclick: (e) => ctx.chooseMarkers(e.currentTarget, common, chosen, (picked) => {
                ctx.setParams(cell.id, { markers: picked.length === common.length ? null : picked }, true);
                ctx.rerender(cell.id);
            }) }));
    }

    function intensities(cell, ctx, data) {
        const first = data.rows.find((r) => r.type === "csv" || r.type === "parquet") || {};
        const params = cell.params || {};
        const log = params.log !== undefined && params.log !== null ? params.log : !first.log_transformed;
        const box = h("input.sm-check", { type: "checkbox", checked: log || null });
        box.addEventListener("change", () => ctx.setParams(cell.id, { log: box.checked }, true));
        return h("label.sm-prep-inline", null, box, first.log_transformed
            ? "Log-transform (log1p). The table says it is already logged; leave this off."
            : "Log-transform (log1p). The raw values are kept beside them.");
    }

    function folderBlock(cell, ctx, data) {
        const field = h("input", { "aria-label": "Results folder", value: tildePath(data.results_dir), spellcheck: "false" });
        field.addEventListener("change", () => ctx.setResultsDir(cell.id, field.value));
        const shared = data.context.kind === "dataset" && data.rows.length > 1;
        const isDefault = data.results_dir === data.default_results_dir;
        return h("div.sm-prep-stack.is-tight", null,
            h("div.sm-prep-inline", { style: { maxWidth: "600px" } },
                h("label.sm-path-field", { style: { flex: "1" } }, icon("folder"), field),
                button("Browse…", { onclick: () => ctx.browseFolder(cell.id, data.results_dir) })),
            h("span.sm-adv-note", null, (isDefault ? (shared ? "Beside the data, in the folder the tables share. " : "Beside the data, in the table's folder. ")
                : "") + "The working file, every step's results and their history go here."));
    }

    function runRow(cell, ctx, data) {
        const problems = data.problems || [];
        const tables = data.rows.filter((r) => r.has_table).length;
        const hint = (problems.length ? problems[0] + " · " : "") + "runs on this computer · the project tables are not changed";
        return h("div.sm-run-row.sm-prep-run", null,
            button("Prepare data", { kind: "primary", icon: "play", disabled: problems.length || !tables || null,
                onclick: () => ctx.run(cell.id) }),
            button("Reset to defaults", { kind: "quiet", onclick: () => ctx.op({ op: "reset_params", id: cell.id }) }),
            cell.run ? button("Cancel", { kind: "quiet", onclick: () => { ctx.local(cell.id).settings = false; ctx.rerender(cell.id); } }) : null,
            h("span.sm-spacer"),
            h("span.sm-hint" + (problems.length ? ".is-warn" : ""), null, hint));
    }

    // -- done: the working file and how Scimap Pro reads it ----------------------

    function done(cell, ctx) {
        const w = ctx.work;
        const run = cell.run || {};
        const made = run.working || {};
        const reading = w.reading || made.reading;
        const facts = h("div.sm-kv.is-wide");
        const add = (k, ...v) => facts.append(h("span.sm-kv-k", null, k), h("span", null, ...v));
        add("File", h("span.sm-mono-text", null, tildePath(w.path)));
        add("Images", (w.images || []).join(", "));
        const meta = w.metadata || made.metadata || [];
        if (meta.length) add("Sample metadata", meta.join(", ") + ", on all " + n(w.n_cells) + " cells");
        if ((w.markers_dropped || []).length) add("Left out when joining", w.markers_dropped.join(", "));
        if (w.sample) add("Cells", n(w.n_cells) + " (a random sample; seed 0)");
        return h("div.sm-prep-done", null, facts, reading ? readingCard(cell, ctx, reading) : null);
    }

    function readingCard(cell, ctx, r) {
        if (r.error) {
            return h("div.sm-reading", null, h("div.sm-reading-head", null, h("span.sm-reading-title", null, "How Scimap Pro reads this data")),
                h("p.sm-reading-text", null, "Scimap Pro could not be asked: " + r.error));
        }
        const memory = ctx.nb.memory || "auto";
        const select = h("select", { "aria-label": "Memory" },
            [["auto", "Automatic"], ["memory", "In memory"], ["stream", "Streamed from disk"]].map(([v, t]) =>
                h("option", { value: v, selected: v === memory || null, disabled: (v === "stream" && !r.licensed_streaming) || null }, t)));
        select.addEventListener("change", async () => {
            select.disabled = true;
            try {
                const a = await API.reading(ctx.nb.id, select.value);
                ctx.adopt(a.view);
                ctx.rerender(cell.id);
            } catch (err) { ctx.toast(err.message); select.disabled = false; }
        });
        const kv = h("div.sm-kv.is-small");
        const add = (k, v) => { if (v) kv.append(h("span.sm-kv-k", null, k), h("span", null, v)); };
        add("Machine", r.machine);
        add("Memory budget", r.budget);
        add("Dataset", r.dataset);
        add("Loading it costs", r.cost);
        add("Faster with", r.faster);
        add("Software", r.software);
        return h("div.sm-reading", null,
            h("div.sm-reading-head", null,
                h("span.sm-reading-title", null, "How Scimap Pro reads this data"),
                h("span.sm-reading-mode", null, icon(r.mode === "stream" ? "layers" : "chip", "xs"), r.title),
                h("span.sm-spacer"),
                h("a.sm-adv-docs", { href: r.docs, target: "_blank", rel: "noopener" }, "io.init reference", icon("external", "xs"))),
            h("p.sm-reading-text", null, r.sentence),
            kv,
            h("div.sm-reading-foot", null, h("span", null, "Every step uses this decision."),
                h("label.sm-prep-inline", null, "Memory", select),
                h("span", null, r.licensed_streaming ? "Changing it asks Scimap Pro again; nothing reruns."
                    : "Streaming from disk is part of a license; changing it asks Scimap Pro again.")));
    }

    function subtitle(ctx) {
        const w = ctx.work;
        if (!w.exists) return "";
        const parts = [];
        const sources = w.sources || [];
        if (sources.length > 1) parts.push(sources.length + " projects joined");
        parts.push(n(w.n_cells) + " cells", (w.markers || []).length + " markers");
        const reading = w.reading;
        if (reading && reading.mode) parts.push(reading.mode === "stream" ? "streamed from disk" : "in memory");
        return parts.join(" · ");
    }

    return { manager, done, subtitle };
})();
