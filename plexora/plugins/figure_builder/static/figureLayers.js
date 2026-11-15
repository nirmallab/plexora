/**
 * FigureLayers - every object on the current page, as a list.
 *
 * Built from the DOCUMENT on every change, never from what has recently been
 * captured or added: a page holding four panels lists four panels, whatever
 * route they arrived by. The list and the canvas are two views of one
 * selection (`FigureCanvas.selection`), so a click in either is reflected in
 * the other and there is no second selection to keep in step.
 *
 * ## Order
 *
 * Front to back, the way every layers list reads, in the page's own stacking
 * order (`FigureSchema.stackOnPage`): panels and annotations share one stack,
 * so dragging an image above a shape here puts it above the shape on the page
 * and in the export. A group is a folder at the position of its frontmost member,
 * with its members under it. A LAYOUT group lists its members in cell order
 * instead -- A, B, C, D -- because that is the order the layout is edited in,
 * and dragging one of them to another place in the folder moves it to that
 * cell (`FigureCanvas.applyLayout`'s `order`).
 *
 * ## What a row can do
 *
 * Click selects (Shift or Cmd/Ctrl adds); a member row selects that member
 * alone, which is how one panel of a layout is reached without ungrouping.
 * Double-click renames. The eye hides and the padlock locks, and both are
 * statements about the object, saved with the figure
 * (`schema.normalize_layer_fields`). Right-click is the canvas's own menu.
 * Dragging a row restacks the page (`FigureCanvas.restack`).
 */
class FigureLayers {

    constructor({ root, state, canvas, api, figureId, workspace }) {
        this.root = root;
        this.state = state;
        this.canvas = canvas;
        this.api = api;
        this.figureId = figureId;
        this.workspace = workspace;
        //: Group ids whose folders are shut. Kept for the session only: which
        //: folders somebody had open is not a fact about the figure.
        this.collapsed = new Set();
        //: The row being renamed, while its field is open. Rendering is held
        //: off meanwhile, or the autosave the rename triggers would replace
        //: the field mid-word.
        this.renaming = null;
        this.sortable = null;
    }

    setup() {
        if (!this.root) return;
        this.root.addEventListener("click", (event) => this.click(event));
        this.root.addEventListener("contextmenu", (event) => this.contextMenu(event));
        this.root.addEventListener("keydown", (event) => this.keyDown(event));
    }

    // -- what is listed ------------------------------------------------------

    /** The page's objects as rows, front to back, groups as folders. */
    rows() {
        const document_ = this.state.document;
        const pageId = this.canvas.pageId;
        if (!document_ || !pageId) return [];
        const reading = FigureSchema.panelsOnPage(document_, pageId);
        const letters = new Map();
        const style = document_.settings.label_style;
        reading.forEach((panel, index) => {
            const text = panel.label.auto ? FigureSchema.labelFor(index, style) : panel.label.text;
            letters.set(panel.panel_id, panel.label.visible ? text : "");
        });

        // Front to back, in the order the page draws them: one stack for
        // panels and annotations (FigureSchema.stackOnPage).
        const ordered = FigureSchema.stackOnPage(document_, pageId).reverse()
            .map((entry) => ({ kind: entry.kind, id: entry.id, object: entry.object }));
        const onPage = new Set(ordered.map((item) => item.id));

        const out = [];
        const emitted = new Set();
        for (const item of ordered) {
            if (emitted.has(item.id)) continue;
            const group = this.canvas.groupFor(item.id);
            if (!group) {
                out.push(this.objectRow(item, letters, 0, null));
                emitted.add(item.id);
                continue;
            }
            const members = (group.layout ? group.member_ids
                : ordered.filter((entry) => group.member_ids.includes(entry.id)).map((e) => e.id))
                .filter((id) => onPage.has(id));
            out.push(this.groupRow(group, members));
            for (const id of members) {
                emitted.add(id);
                if (this.collapsed.has(group.group_id)) continue;
                const entry = ordered.find((candidate) => candidate.id === id);
                out.push(this.objectRow(entry, letters, 1, group));
            }
        }
        return out;
    }

    objectRow(item, letters, depth, group) {
        const object = item.object;
        const isPanel = item.kind === "panel";
        const letter = isPanel ? letters.get(item.id) || "" : "";
        return {
            kind: item.kind, id: item.id, depth: depth, group: group,
            name: object.name || this.defaultName(item, letter),
            named: Boolean(object.name),
            letter: letter,
            icon: isPanel ? null : FigureLayers.iconFor(object),
            hidden: Boolean(object.hidden), locked: Boolean(object.locked),
            low: isPanel && Boolean(FigureCanvas.panelQuality(object, this.state.document)?.low),
            panel: isPanel ? object : null,
        };
    }

    groupRow(group, members) {
        const objects = members.map((id) => this.canvas.objectById(id)).filter(Boolean);
        const preset = group.layout ? FigureLayouts.byId(group.layout.preset) : null;
        return {
            kind: "group", id: group.group_id, depth: 0, group: group, members: members,
            name: group.name || (preset ? "Layout" : "Group"),
            named: Boolean(group.name),
            meta: preset ? `${preset.label} layout` : `${members.length} objects`,
            hidden: objects.length > 0 && objects.every((o) => o.hidden),
            locked: objects.length > 0 && objects.every((o) => o.locked),
            open: !this.collapsed.has(group.group_id),
        };
    }

    /** What an unnamed object is called: what the user put on it, else what it
     *  is. A panel by its letter and its first caption or its source; a text
     *  box by its words. */
    defaultName(item, letter) {
        const object = item.object;
        if (item.kind === "panel") {
            const source = this.state.source(object.source_id);
            const words = FigureSchema.panelCaption(object)
                || (source && (source.display_name || source.datasource)) || "Image";
            return words;
        }
        if (object.type === "text") {
            const words = String(object.rich ? FigureRichText.plainText(object.rich) : object.text || "")
                .trim().replace(/\s+/g, " ");
            // Cut by the row's width (an ellipsis in CSS), not by a count: a
            // wider sidebar shows more of the caption.
            return words ? words.slice(0, 240) : "Text";
        }
        if (object.type === "shape") {
            const preset = object.shape && object.shape.preset;
            const def = preset && preset !== "custom" && FigureShapeDefs.byId
                ? FigureShapeDefs.byId(preset) : null;
            return def ? def.label : "Shape";
        }
        if (FigureCanvas.isStrokeType(object.type)) {
            const style = object.style || {};
            return (style.end_head && style.end_head !== "none")
                || (style.start_head && style.start_head !== "none") ? "Arrow" : "Line";
        }
        return { rect: "Rectangle", ellipse: "Ellipse" }[object.type] || "Object";
    }

    static iconFor(annotation) {
        if (annotation.type === "text") return "font";
        if (annotation.type === "ellipse") return "circle";
        if (FigureCanvas.isStrokeType(annotation.type)) return "arrow-right-long";
        return "square";
    }

    // -- drawing -------------------------------------------------------------

    /** The page headers: this page open above its objects, every other page
     *  below as a row that goes to it. */
    pageHeads() {
        const document_ = this.state.document;
        const pages = (document_ && document_.pages) || [];
        const count = (pageId) => FigureSchema.panelsOnPage(document_, pageId).length
            + Object.values(document_.annotations || {}).filter((a) => a.page_id === pageId).length;
        const escape = (value) => FigureSchema.escapeHtml(String(value ?? ""));
        const head = (page, index, open) => {
            const n = count(page.page_id);
            return `<button type="button" class="fb-layers-page${open ? " is-open" : ""}"
                        ${open ? "" : `data-goto-page="${escape(page.page_id)}"`}
                        aria-label="${open ? "This page" : "Go to"} ${escape(page.name || `Page ${index + 1}`)}">
                ${FigureIcons.svg(open ? "chevD" : "chevR", 12)}
                <span class="fb-layers-page-name">${escape(page.name || `Page ${index + 1}`)}</span>
                <span class="fb-layer-meta">${n === 0 ? "Empty" : (n === 1 ? "1 object" : `${n} objects`)}</span></button>`;
        };
        const at = pages.findIndex((page) => page.page_id === this.canvas.pageId);
        return {
            current: at >= 0 ? head(pages[at], at, true) : "",
            others: pages.map((page, index) => (index === at ? "" : head(page, index, false))).join(""),
        };
    }

    render() {
        if (!this.root || this.renaming) return;
        const rows = this.rows();
        const escape = (value) => FigureSchema.escapeHtml(String(value ?? ""));
        const heads = this.pageHeads();
        if (!rows.length) {
            this._markup = null;
            this.destroySortable();
            this.root.innerHTML = heads.current + `<div class="fb-layers-empty">
                <strong>No objects on this page</strong>
                <span>Add an image from your captures, drop an image file on the page,
                    or draw on it from Annotate.</span>
                <button type="button" class="fb-button fb-button-primary" data-layers-act="assets">
                    ${FigureIcons.fa("plus")} Add image</button>
            </div>` + heads.others;
            return;
        }
        const selection = this.canvas.selection;
        const markup = rows.map((row) => {
            if (row.kind === "group") {
                const selected = row.members.length > 0
                    && row.members.every((id) => selection.has(id));
                return `<div class="fb-layer fb-layer-group${selected ? " is-selected" : ""}
                                ${row.hidden ? " is-hidden" : ""}"
                             role="treeitem" aria-expanded="${row.open}" aria-selected="${selected}"
                             data-group-id="${escape(row.id)}" style="--depth:0">
                    <button type="button" class="fb-layer-fold" data-fold="${escape(row.id)}"
                            aria-label="${row.open ? "Collapse" : "Expand"} ${escape(row.name)}"
                            tabindex="-1">
                        ${FigureIcons.svg(row.open ? "chevD" : "chevR", 12)}</button>
                    <button type="button" class="fb-layer-main" data-pick-group="${escape(row.id)}">
                        <span class="fb-layer-icon">${FigureIcons.svg(row.group.layout ? "grid" : "group", 14)}</span>
                        <span class="fb-layer-name">${escape(row.name)}</span>
                        <span class="fb-layer-meta">${escape(row.meta)}</span>
                    </button>
                    ${this.toggles(row, escape)}
                </div>`;
            }
            const selected = selection.has(row.id);
            const thumb = row.panel
                ? `<span class="fb-layer-thumb"><img alt="" draggable="false" loading="lazy"
                       src="${escape(this.canvas.panelImageUrl(row.panel, this.state.source(row.panel.source_id)))}"></span>`
                : `<span class="fb-layer-icon">${FigureIcons.fa(row.icon, 14)}</span>`;
            return `<div class="fb-layer${selected ? " is-selected" : ""}${row.hidden ? " is-hidden" : ""}
                            ${row.locked ? " is-locked" : ""}"
                         role="treeitem" aria-selected="${selected}" aria-level="${row.depth + 1}"
                         data-id="${escape(row.id)}" data-kind="${row.kind}"
                         ${row.group ? `data-member-of="${escape(row.group.group_id)}"` : ""}
                         style="--depth:${row.depth}">
                <button type="button" class="fb-layer-main" data-pick="${escape(row.id)}">
                    ${thumb}
                    ${row.letter ? `<span class="fb-layer-letter">${escape(row.letter)}</span>` : ""}
                    <span class="fb-layer-name${row.named ? "" : " is-derived"}">${escape(row.name)}</span>
                    ${row.low ? `<span class="fb-layer-warn" title="Low effective resolution">
                        ${FigureIcons.svg("warn", 12)}
                        <span class="fb-visually-hidden">Low effective resolution</span></span>` : ""}
                </button>
                ${this.toggles(row, escape)}
            </div>`;
        }).join("");
        // Most redraws change nothing here -- a nudge moves a panel, it does
        // not rename one -- so an identical list is left exactly as it is: no
        // DOM written, no Sortable rebuilt.
        const full = heads.current + markup + heads.others;
        if (full === this._markup && this.root.childElementCount) return;
        this._markup = full;
        FigureCanvas.replaceKeepingImages(this.root, full);
        this.bindSortable();
    }

    /** Each object's display name on the current page, by id -- one pass,
     *  for callers that name several objects at once. */
    names() {
        return new Map(this.rows().map((row) => [row.id, row.name]));
    }

    toggles(row, escape) {
        const target = row.kind === "group" ? `data-group="${escape(row.id)}"`
            : `data-object="${escape(row.id)}"`;
        return `<button type="button" class="fb-layer-toggle${row.locked ? " is-on" : ""}"
                        data-toggle="locked" ${target} aria-pressed="${row.locked}"
                        title="${row.locked ? "Unlock" : "Lock"}"
                        aria-label="${row.locked ? "Unlock" : "Lock"} ${escape(row.name)}">
                    ${FigureIcons.svg(row.locked ? "lock" : "unlock", 12)}</button>
                <button type="button" class="fb-layer-toggle${row.hidden ? " is-on" : ""}"
                        data-toggle="hidden" ${target} aria-pressed="${row.hidden}"
                        title="${row.hidden ? "Show" : "Hide"}"
                        aria-label="${row.hidden ? "Show" : "Hide"} ${escape(row.name)}">
                    ${FigureIcons.svg(row.hidden ? "eyeOff" : "eye", 12)}</button>`;
    }

    /**
     * Follow a selection change without rebuilding the list: a selection
     * changes which rows are highlighted and nothing else, and rewriting every
     * row to move a highlight was most of the cost of a click on a busy page.
     */
    syncSelection() {
        if (!this.root || this.renaming) return;
        const selection = this.canvas.selection;
        for (const row of this.root.querySelectorAll(".fb-layer")) {
            let on;
            if (row.dataset.id) on = selection.has(row.dataset.id);
            else {
                const members = this.state.document?.groups?.[row.dataset.groupId]?.member_ids || [];
                on = members.length > 0 && members.every((id) => selection.has(id));
            }
            row.classList.toggle("is-selected", on);
            row.setAttribute("aria-selected", String(on));
        }
        // The markup no longer matches what is on screen; the next full render
        // must write rather than skip.
        this._markup = null;
    }

    /** Bring the first selected row into view, after a selection made on the
     *  canvas -- the list may be long and the row scrolled away. */
    reveal() {
        const row = this.root?.querySelector(".fb-layer.is-selected");
        row?.scrollIntoView?.({ block: "nearest" });
    }

    // -- the pointer and the keyboard ------------------------------------------

    click(event) {
        const goto = event.target.closest("[data-goto-page]");
        if (goto) {
            this.workspace.goToPage(goto.dataset.gotoPage);
            return;
        }
        if (event.target.closest("[data-layers-act='assets']")) {
            this.workspace.showTab("assets");
            return;
        }
        const fold = event.target.closest("[data-fold]");
        if (fold) {
            const id = fold.dataset.fold;
            if (this.collapsed.has(id)) this.collapsed.delete(id);
            else this.collapsed.add(id);
            this.render();
            return;
        }
        const toggle = event.target.closest("[data-toggle]");
        if (toggle) {
            const key = toggle.dataset.toggle;
            const ids = toggle.dataset.group
                ? (this.state.document.groups[toggle.dataset.group]?.member_ids || [])
                : [toggle.dataset.object];
            const now = toggle.getAttribute("aria-pressed") === "true";
            this.canvas.setLayerFields(ids, { [key]: !now });
            return;
        }
        // The second click of a double-click renames. Read from the click's
        // own count rather than from a `dblclick` listener: the first click
        // selects, selecting redraws this list, and the two clicks then land
        // on different elements -- which is exactly when a browser declines to
        // send `dblclick` at all (the canvas met the same thing; see
        // FigureCanvas.secondPress).
        if (event.detail === 2) {
            const main = event.target.closest(".fb-layer-main");
            if (main) {
                this.startRename(main.closest(".fb-layer"));
                return;
            }
        }
        const additive = event.shiftKey || event.metaKey || event.ctrlKey;
        const group = event.target.closest("[data-pick-group]");
        if (group) {
            const members = this.state.document.groups[group.dataset.pickGroup]?.member_ids || [];
            this.canvas.select(members, additive, true);
            return;
        }
        const pick = event.target.closest("[data-pick]");
        if (pick) this.canvas.select([pick.dataset.pick], additive, true);
    }

    keyDown(event) {
        if (event.target.classList?.contains("fb-layer-rename")) return;
        const main = event.target.closest?.(".fb-layer-main");
        if (!main) return;
        if (event.key === "F2" || (event.key === "Enter" && event.altKey)) {
            event.preventDefault();
            this.startRename(main.closest(".fb-layer"));
            return;
        }
        const step = { ArrowDown: 1, ArrowUp: -1 }[event.key];
        // Alt and an arrow moves the object one place in the stack -- the
        // keyboard's way to do what dragging a row does.
        if (step && event.altKey) {
            event.preventDefault();
            const row = main.closest(".fb-layer");
            const ids = Array.from(this.root.querySelectorAll(".fb-layer[data-id]"))
                .map((each) => each.dataset.id);
            const at = ids.indexOf(row?.dataset.id);
            const to = at + step;
            if (at < 0 || to < 0 || to >= ids.length) return;
            [ids[at], ids[to]] = [ids[to], ids[at]];
            this.canvas.restack(ids);
            this.render();
            this.root.querySelector(`.fb-layer[data-id="${FigureSchema.cssEscape(ids[to])}"] .fb-layer-main`)?.focus();
            return;
        }
        if (step) {
            event.preventDefault();
            const all = Array.from(this.root.querySelectorAll(".fb-layer-main"));
            all[Math.max(0, Math.min(all.length - 1, all.indexOf(main) + step))]?.focus();
        }
    }

    contextMenu(event) {
        const row = event.target.closest(".fb-layer");
        if (!row || !this.workspace.contextMenu) return;
        event.preventDefault();
        const id = row.dataset.id;
        const ids = id ? [id]
            : (this.state.document.groups[row.dataset.groupId]?.member_ids || []);
        if (!ids.every((each) => this.canvas.selection.has(each))) {
            this.canvas.select(ids, false, true);
        }
        const selected = Array.from(this.canvas.selection);
        this.workspace.contextMenu.point = null;   // nothing to paste "here"
        this.workspace.contextMenu.open(event.clientX, event.clientY,
            this.workspace.contextMenu.selectionEntries(selected));
    }

    // -- renaming ------------------------------------------------------------

    startRename(row) {
        if (!row) return;
        const nameEl = row.querySelector(".fb-layer-name");
        if (!nameEl) return;
        const groupId = row.dataset.groupId || null;
        const id = row.dataset.id || null;
        const current = groupId ? (this.state.document.groups[groupId]?.name || "")
            : (this.canvas.objectById(id)?.name || "");
        const input = document.createElement("input");
        input.type = "text";
        input.className = "fb-layer-rename";
        input.value = current || nameEl.textContent.trim();
        input.maxLength = 80;
        input.setAttribute("aria-label", "Layer name");
        nameEl.replaceWith(input);
        this.renaming = { id: id, groupId: groupId };
        input.focus();
        input.select();
        let done = false;
        const finish = (commit) => {
            if (done) return;
            done = true;
            this.renaming = null;
            const value = input.value.trim();
            if (commit && value !== current) {
                // Typing the name it already showed is not a rename: an object
                // named after its own content would stop following it.
                const shown = nameEl.textContent.trim();
                const name = (!current && value === shown) ? "" : value;
                if (name === current) { /* nothing to change */ }
                else if (groupId) this.canvas.updateGroup(groupId, { name: name });
                else this.canvas.setLayerFields([id], { name: name });
            }
            this.render();
        };
        input.addEventListener("keydown", (event) => {
            event.stopPropagation();
            if (event.key === "Enter") finish(true);
            else if (event.key === "Escape") finish(false);
        });
        input.addEventListener("blur", () => finish(true));
    }

    // -- dragging to restack ---------------------------------------------------

    destroySortable() {
        this.sortable?.destroy?.();
        this.sortable = null;
    }

    /**
     * SortableJS, as everywhere else rows are dragged in this plugin -- the
     * image panel's caption list says why hand-rolled HTML5 drag-and-drop is
     * not an option on a page that binds pointerdown everywhere. Re-bound after
     * every render, because the list is rebuilt from markup each time.
     */
    bindSortable() {
        this.destroySortable();
        if (typeof window === "undefined" || typeof window.Sortable !== "function") return;
        this.sortable = new window.Sortable(this.root, {
            animation: 120,
            draggable: ".fb-layer",
            filter: ".fb-layer-toggle, .fb-layer-fold, .fb-layer-rename",
            preventOnFilter: false,
            ghostClass: "is-drag-ghost",
            // Pointer-driven in the desktop app, whose native file drop
            // swallows HTML5 drag events on Windows (as the image panel's list).
            forceFallback: Boolean(window.PlexoraDesktop),
            onEnd: (event) => this.dropped(event),
        });
    }

    dropped(event) {
        if (event.oldIndex === event.newIndex) return;
        const moved = event.item;
        const rows = Array.from(this.root.querySelectorAll(".fb-layer"));
        // A member dropped among its own layout's members moves to that CELL.
        const layoutId = moved.dataset.memberOf;
        const layout = layoutId && this.state.document.groups[layoutId]?.layout;
        if (layout) {
            const order = rows.filter((row) => row.dataset.memberOf === layoutId)
                .map((row) => row.dataset.id);
            const before = this.state.document.groups[layoutId].member_ids;
            if (order.length === before.length && order.join() !== before.join()) {
                this.canvas.relayout(layoutId, {}, { order: order });
                return;
            }
        }
        // Otherwise the list's order is the new stacking. A group header stands
        // for its members, in the order they are listed under it.
        const order = [];
        for (const row of rows) {
            if (row.dataset.id) {
                if (!order.includes(row.dataset.id)) order.push(row.dataset.id);
                continue;
            }
            const group = this.state.document.groups[row.dataset.groupId];
            for (const id of group?.member_ids || []) {
                if (!order.includes(id) && this.canvas.objectById(id)) order.push(id);
            }
        }
        this.canvas.restack(order);
        this.render();
    }
}
