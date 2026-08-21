/**
 * roiSidebarController.js - the panel, and the plugin's registration.
 *
 * Everything user-facing that is not on the image itself: the tool buttons, the
 * category list, the selected-region fields, the save indicator, import/export,
 * and the two banners that appear when saving cannot proceed.
 *
 * Every piece of user text on this panel is written with `textContent`, never
 * `innerHTML`. Category and region names are user input, they survive a round
 * trip through the store, and they come back through an import from a file that
 * may have been written by anything at all -- so the one place they reach the
 * DOM is the one place that has to be careful.
 *
 * Registration is at the bottom of this file because it is loaded last (see
 * PLUGIN.scripts): by then RoiApi, RoiGeometry, RoiStore, RoiRenderer and
 * RoiInteraction all exist.
 */
class RoiSidebarController {

    constructor(ctx) {
        this.ctx = ctx;
        this.api = new RoiApi(ctx);
        this.store = new RoiStore(ctx, this.api);
        this.renderer = new RoiRenderer(ctx, this.store);
        this.tools = new RoiInteraction(ctx, this.store, this.renderer);
        this.tools.onNotify = (message) => this.notify(message);
        this._messageTimer = null;
        this._nameTimer = null;
        this._unsubscribe = null;
        this._destinationOpen = false;
    }

    // -- lifecycle -------------------------------------------------------

    setup() {
        this.bindToolbar();
        this.bindCategoryForm();
        this.bindSelection();
        this.bindTransfer();
        this.bindBanners();

        this.el("roi_panel_close")?.addEventListener("click", () => {
            window.PlexoraToolLoader?.hideToolPanel("roi");
        });

        this._unsubscribe = this.store.onChange(() => this.render());

        // A tab closed with regions still queued loses them, and the user has no
        // way to know that from looking at the panel. Registered through the
        // plugin's cleanup list so it goes when the plugin does.
        const beforeUnload = (event) => {
            if (!this.store.hasUnsavedWork) return;
            event.preventDefault();
            event.returnValue = "";
        };
        window.addEventListener("beforeunload", beforeUnload);
        this.ctx.onCleanup?.(() => window.removeEventListener("beforeunload", beforeUnload));
        this.ctx.onCleanup?.(() => this.destroy());
    }

    /** ViewerSidebar's restore hook. Loading happens here so it is awaited in
     *  the same place core awaits every other module's saved state. */
    async fetchSaved() {
        await this.store.load();
        return null;
    }

    applyOrDefault() {
        this.renderer.attach();
        this.render();
    }

    /** Called by toolLoader when this panel becomes the selected one. */
    onShow() {
        this.renderer.attach();
        this.tools.arm();
        this.render();
    }

    /**
     * The eye on this tool's card: draw the regions, or stop drawing them.
     *
     * Every other plugin's layer is a cell layer core switches off by itself.
     * ROI's is its own overlay canvas, so core has nothing to switch and the
     * card's toggle would be inert without this.
     *
     * Turning the overlay off disarms the tools as well. A pen that goes on
     * drawing invisible regions -- and goes on swallowing V/P/F/R -- while its
     * layer says "off" is the same failure onHide() exists to prevent, arrived at
     * from the other direction. Turning it back on re-arms only when this is
     * still the selected tool: a background layer being made visible is a request
     * to SEE it, not to type into it.
     */
    onVisibilityChange(visible) {
        this.renderer.setEnabled(Boolean(visible));
        if (!visible) {
            this.tools.disarm();
            this.el("roi_map_info_dialog")?.close();
            return;
        }
        this.renderer.attach();
        if (window.PlexoraToolLoader?.activeTool() === "roi") this.tools.arm();
        this.renderer.schedule();
    }

    /**
     * Called when another tool is opened over this one, or this one is closed.
     *
     * This is the hook ROI genuinely needs: its handlers are on the viewer
     * canvas and the document, neither of which is hidden along with the panel.
     * Left armed, ROI would keep drawing over another tool's session and keep
     * swallowing V/P/F/R.
     */
    onHide() {
        this.tools.disarm();
        this.renderer.setEnabled(false);
        // The info dialog lives inside the panel, so hiding the panel takes it
        // off the screen without closing it -- and it would be waiting, open,
        // over whatever the user came back to.
        this.el("roi_map_info_dialog")?.close();
    }

    destroy() {
        this._unsubscribe?.();
        if (this._messageTimer) clearTimeout(this._messageTimer);
        if (this._nameTimer) clearTimeout(this._nameTimer);
        this.tools.destroy();
        this.renderer.destroy();
        this.store.destroy();
    }

    el(id) {
        return document.getElementById(id);
    }

    // -- wiring ----------------------------------------------------------

    bindToolbar() {
        for (const button of document.querySelectorAll("#roi_toolbar .roi-tool")) {
            button.addEventListener("click", () => this.tools.setTool(button.dataset.tool));
        }
    }

    bindCategoryForm() {
        const input = this.el("roi_category_name");
        const add = () => {
            this.createCategory(input.value);
            input.value = "";
        };
        // Enter and the [+] button, bound separately rather than through a
        // form's submit -- see the note in panel.html on why there is no form.
        this.el("roi_category_add_button")?.addEventListener("click", add);
        input?.addEventListener("keydown", (event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            add();
        });
    }

    bindSelection() {
        const name = this.el("roi_selection_name");
        name?.addEventListener("input", () => {
            // Debounced, so typing a name is one operation rather than one per
            // keystroke -- and committed on blur too, since a user who types a
            // name and immediately clicks the image expects it kept.
            if (this._nameTimer) clearTimeout(this._nameTimer);
            this._nameTimer = setTimeout(() => this.commitName(name.value), 500);
        });
        name?.addEventListener("blur", () => this.commitName(name.value));
        name?.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                name.blur();
            }
        });

        this.el("roi_selection_category")?.addEventListener("change", (event) => {
            this.recategorize(event.target.value);
        });
        this.el("roi_selection_locked")?.addEventListener("click", () => {
            this.setLocked(!this.store.selected?.locked);
        });
        this.el("roi_selection_delete")?.addEventListener("click", () => {
            this.tools.deleteSelected();
        });
    }

    bindTransfer() {
        this.el("roi_export_button")?.addEventListener("click", () => this.exportGeoJSON());
        this.el("roi_export_download")?.addEventListener("click", () => this.exportGeoJSON());

        const picker = this.el("roi_import_file");
        this.el("roi_import_button")?.addEventListener("click", () => picker?.click());
        picker?.addEventListener("change", async () => {
            const file = picker.files && picker.files[0];
            picker.value = "";  // so re-picking the same file fires again
            if (file) await this.importGeoJSON(file);
        });

        this.el("roi_save_to_source")?.addEventListener("click", () => this.saveToSource());
        this.el("roi_map_to_cells")?.addEventListener("click", () => this.mapToCells());
        this.el("roi_destination_close")?.addEventListener("click", () => this.closeDestination());

        this.el("roi_map_info")?.addEventListener("click", () => this.openMapInfo());
        const info = this.el("roi_map_info_dialog");
        this.el("roi_map_info_close")?.addEventListener("click", () => info?.close());
        // Clicking outside the dialog closes it. The <dialog> element IS the
        // backdrop's hit area -- the visible panel is its padding box -- so a
        // click landing on the dialog itself and not on anything inside it is a
        // click on the backdrop.
        info?.addEventListener("click", (event) => {
            if (event.target === info) info.close();
        });

        const destination = this.el("roi_destination_name");
        // Live, so the line underneath always names the entry the button is
        // about to write -- including while it is being renamed.
        destination?.addEventListener("input", () => this.renderDestination());
        // The field is opened by the save button and the user is already typing
        // in it, so Enter finishes the job they started rather than making them
        // travel back to the button. Escape backs out, as it does everywhere
        // else on this panel. Neither reaches the drawing tools: they ignore
        // keys while an input has focus (see RoiInteraction.acceptsKeys).
        destination?.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                this.saveToSource();
            } else if (event.key === "Escape") {
                event.preventDefault();
                this.closeDestination();
            }
        });
    }

    bindBanners() {
        this.el("roi_conflict_reload")?.addEventListener("click", async () => {
            await this.store.reloadRemote();
            this.renderer.invalidate();
            this.renderer.schedule();
        });
        this.el("roi_conflict_keep")?.addEventListener("click", async () => {
            await this.store.keepMineAndExport();
            this.renderer.invalidate();
            this.renderer.schedule();
        });
    }

    // -- categories ------------------------------------------------------

    createCategory(rawLabel) {
        const label = (rawLabel || "").trim();
        if (!label) return;
        if (this.store.categories.some((c) => c.label.toLowerCase() === label.toLowerCase())) {
            this.notify(`There is already a category called "${label}".`);
            return;
        }
        const category = {
            id: RoiStore.newId("c"),
            label,
            color: RoiSidebarController.nextColor(this.store.categories.length),
            visible: true,
            locked: false,
            sort_order: this.store.categories.length,
        };
        this.store.commit({
            label: "Add category",
            redo: [{ op: "category.create", category }],
            undo: [{ op: "category.delete", id: category.id, orphans: "delete" }],
        });
        // Selected immediately: the reason to make a category is to draw in it.
        this.store.setActiveCategory(category.id);
    }

    updateCategory(id, changes) {
        const category = this.store.category(id);
        if (!category) return;
        const before = {};
        for (const key of Object.keys(changes)) before[key] = category[key];
        this.store.commit({
            label: "Edit category",
            redo: [{ op: "category.update", id, changes }],
            undo: [{ op: "category.update", id, changes: before }],
        });
        this.renderer.schedule();
    }

    /**
     * Delete a category, having asked what happens to what is in it.
     *
     * There is no default answer, and the prompt is not skippable when the
     * category has regions: silently deleting somebody's annotations because
     * they tidied up a label is the one outcome that cannot be undone by
     * looking at the screen.
     *
     * Every category can be deleted, including the last one -- there is no
     * reserved catch-all any more. What that costs is a place to put orphaned
     * shapes: "move them" is only offered when there is somewhere to move them
     * TO, and the destination is named in the prompt rather than assumed.
     */
    deleteCategory(id) {
        const category = this.store.category(id);
        if (!category) return;

        const count = this.store.countFor(id);
        const destination = this.store.sortedCategories().find((c) => c.id !== id) || null;
        let orphans = "delete";

        if (count > 0) {
            const regions = `${count} ROI${count === 1 ? "" : "s"}`;
            if (destination) {
                const keep = window.confirm(
                    `"${category.label}" has ${regions}.\n\n`
                    + `OK: keep them and move them to "${destination.label}".\n`
                    + "Cancel: delete the category and its ROIs."
                );
                orphans = keep ? "reassign" : "delete";
            }
            if (orphans === "delete"
                && !window.confirm(`Delete "${category.label}" and its ${regions}?`)) {
                return;
            }
        }

        const affected = this.store.features
            .filter((f) => f.category_id === id)
            .map((f) => JSON.parse(JSON.stringify(f)));
        const image = this.store.image;
        const undo = [{ op: "category.create", category: { ...category } }];
        if (orphans === "delete") {
            for (const feature of affected) undo.push({ op: "roi.create", image, feature });
        } else {
            for (const feature of affected) {
                undo.push({
                    op: "roi.update_properties", image, id: feature.id,
                    changes: { category_id: id },
                });
            }
        }

        const redo = { op: "category.delete", id, orphans };
        if (orphans === "reassign") redo.reassign_to = destination.id;

        this.store.commit({ label: "Delete category", redo: [redo], undo });
        this.renderer.invalidate();
        this.renderer.schedule();
    }

    // -- selection -------------------------------------------------------

    commitName(value) {
        const feature = this.store.selected;
        if (!feature) return;
        const name = (value || "").trim();
        if (name === (feature.name || "")) return;
        this.propertyChange(feature, { name });
    }

    recategorize(categoryId) {
        const feature = this.store.selected;
        if (!feature || !this.store.category(categoryId)) return;
        if (categoryId === feature.category_id) return;
        this.propertyChange(feature, { category_id: categoryId });
        this.renderer.schedule();
    }

    setLocked(locked) {
        const feature = this.store.selected;
        if (!feature || Boolean(feature.locked) === Boolean(locked)) return;
        this.propertyChange(feature, { locked: Boolean(locked) });
        this.renderer.schedule();
    }

    propertyChange(feature, changes) {
        const before = {};
        for (const key of Object.keys(changes)) before[key] = feature[key];
        this.store.commit({
            label: "Edit ROI",
            redo: [{
                op: "roi.update_properties", image: this.store.image,
                id: feature.id, changes,
            }],
            undo: [{
                op: "roi.update_properties", image: this.store.image,
                id: feature.id, changes: before,
            }],
        });
    }

    // -- import / export -------------------------------------------------

    async exportGeoJSON() {
        // Straight from local state when there is anything unsaved or the
        // server has refused: the moments a user most needs an export are the
        // ones where asking the server for one would hand back a version
        // without their work in it.
        if (this.store.hasUnsavedWork || this.store.dimensionMismatch
            || this.store.status === "failed") {
            this.store.exportLocal();
            this.notify("Exported this session's regions.");
            return;
        }
        const result = await this.api.downloadExport();
        if (!result.ok) {
            this.store.exportLocal();
            this.notify("The server could not build the export; exported locally instead.");
        }
    }

    async importGeoJSON(file, acceptMismatch = false) {
        let document;
        try {
            document = JSON.parse(await file.text());
        } catch (error) {
            this.notify("That file is not valid JSON.");
            return;
        }

        const result = await this.api.importGeojson(document, this.store.revision, acceptMismatch);

        if (!result.ok && result.status === 409) {
            this.store.setStatus("conflict", "");
            return;
        }
        if (result.data && result.data.warning === "dimension_mismatch") {
            const found = result.data.found || [];
            const expected = result.data.expected || [];
            // Default is Cancel. Geometry from a differently-sized image lands
            // somewhere entirely plausible and completely wrong, which is
            // exactly the kind of mistake that is never noticed.
            const proceed = window.confirm(
                `These ROIs were drawn on an image ${found[0]} x ${found[1]} px.\n`
                + `This image is ${expected[0]} x ${expected[1]} px.\n\n`
                + "Import anyway, without transforming them?"
            );
            if (!proceed) return;
            return this.importGeoJSON(file, true);
        }
        if (!result.ok || !result.data.success) {
            this.notify(result.data.error || "That file could not be imported.");
            return;
        }

        // The server applied it and told us what it did; replay the same
        // operation locally so it becomes ONE undo step rather than none.
        const operation = result.data.operation;
        this.store.revision = result.data.revision;
        this.store.applyLocal(operation);
        this.store.undoStack.push({
            label: `Import ${result.data.imported} ROIs`,
            redo: [operation],
            undo: [
                { op: "roi.bulk_delete", image: this.store.image,
                  ids: (operation.features || []).map((f) => f.id) },
                ...(operation.categories || []).map((c) => ({
                    op: "category.delete", id: c.id, orphans: "delete",
                })),
            ],
        });
        this.store.redoStack.length = 0;
        this.renderer.invalidate();
        this.renderer.schedule();
        this.store.changed();
        this.notify(`Imported ${result.data.imported} region${result.data.imported === 1 ? "" : "s"}.`);
    }

    /**
     * Write the regions into the file the project came from, under the name in
     * the "Save as" field.
     *
     * Two presses: the first opens the field on the name this project last
     * saved to, the second writes. The field is where the one irreversible
     * decision on this panel is made -- which entry in somebody's file gets
     * overwritten -- so it is shown before the write rather than after, and
     * showing it costs the press that would otherwise have gone straight
     * through. Everything below runs on the second press.
     *
     * The name is what makes several passes possible in one file -- a second
     * annotator, a second read, a version worth keeping -- and it is also what
     * makes a collision possible, so this is where that gets decided:
     *
     *   the name this project last saved to  -> written, no question. That is
     *       the ordinary draw-more-then-save-again loop, and a dialog in front
     *       of every save is a toll on the safe path.
     *   a free name                          -> written, no question.
     *   a name already in the file, not ours -> asked once. Somebody else's
     *       annotations are under it and replacing them cannot be undone from
     *       this panel.
     *
     * `replace` is only ever sent as the user's answer. The server refuses an
     * existing key without it, so a mistake here costs a refusal rather than
     * somebody's work.
     */
    async saveToSource() {
        const button = this.el("roi_save_to_source");
        const destination = this._destination;
        if (!destination || !destination.kind) return;
        if (!this._destinationOpen) {
            this.openDestination();
            return;
        }

        const name = this.destinationName();
        if (destination.kind === "anndata" && name !== destination.remembered
            && destination.existing.includes(name)) {
            const proceed = window.confirm(
                `"${name}" already exists in this file.\n\n`
                + "Replace it? The annotations currently stored under that name will be lost."
            );
            if (!proceed) return;
            destination.replaceOnce = true;
        }

        button.disabled = true;
        try {
            const result = destination.kind === "anndata"
                ? await window.PlexoraStatus.track("Saving ROIs to file",
                    this.api.saveToAnndata(name, destination.replaceOnce
                        || name === destination.remembered))
                : await window.PlexoraStatus.track("Saving ROIs to store",
                    this.api.saveToSpatialdata(name));
            this.afterSaveToSource(result);
        } finally {
            destination.replaceOnce = false;
            button.disabled = false;
        }
    }

    /** What the server made of it, said in terms of the user's own file. */
    afterSaveToSource(result) {
        const data = result.data || {};
        const destination = this._destination;

        if (!result.ok || !data.success) {
            // Both refusals mean the same thing to the user -- that name is
            // taken -- and both carry a free one, so the field becomes the
            // suggestion rather than the user having to invent another.
            if (data.error === "element_exists" || data.error === "key_exists") {
                destination.existing = data.elements || data.keys || destination.existing;
                if (data.suggestion) this.el("roi_destination_name").value = data.suggestion;
                this.renderDestination();
                this.notify(`That name is already taken${data.suggestion
                    ? ` -- try "${data.suggestion}".` : "."}`);
                return;
            }
            this.notify(data.error || "The file could not be written.");
            return;
        }

        destination.remembered = data.name || destination.remembered;
        if (data.name && !destination.existing.includes(data.name)) {
            destination.existing = [...destination.existing, data.name].sort();
        }
        // Written, so the question the field was asking has been answered. A
        // refusal above returns early instead: that one is still open.
        this.closeDestination();
        this.notify(`Saved to ${data.element || data.key}.`);
    }

    /** The name in the field, or the default when it has been emptied. */
    destinationName() {
        const field = this.el("roi_destination_name");
        const typed = (field?.value || "").trim();
        return typed || (this._destination?.default_name || "");
    }

    /** Show the "Save as" field, with the name it is going to use selected --
     *  the common case is accepting it, and the next-common is typing over it
     *  whole, so neither should need the mouse. */
    openDestination() {
        this._destinationOpen = true;
        this.renderSourceButton();
        const field = this.el("roi_destination_name");
        field?.focus();
        field?.select();
    }

    closeDestination() {
        this._destinationOpen = false;
        this.renderSourceButton();
    }

    // -- rendering -------------------------------------------------------

    render() {
        this.renderBanners();
        this.renderToolbar();
        this.renderCategories();
        this.renderSelection();
        this.renderStatus();
        this.renderSourceButton();
    }

    renderBanners() {
        const blocked = this.el("roi_blocked_banner");
        if (blocked) {
            blocked.hidden = !this.store.dimensionMismatch;
            if (this.store.dimensionMismatch) {
                const [sw, sh] = this.store.storedImageSize;
                const [cw, ch] = this.store.imageSize;
                this.el("roi_blocked_detail").textContent =
                    `Drawn on ${sw} x ${sh} px; this image is ${cw} x ${ch} px.`;
            }
        }
        const conflict = this.el("roi_conflict_banner");
        if (conflict) conflict.hidden = this.store.status !== "conflict";
    }

    renderToolbar() {
        for (const button of document.querySelectorAll("#roi_toolbar .roi-tool")) {
            button.classList.toggle("is-active", button.dataset.tool === this.tools.tool);
            // Select stays available whenever the pointer works at all; the
            // three that MAKE a shape also need a category to put it in.
            button.disabled = button.dataset.tool === "select"
                ? !this.tools.ready
                : !this.tools.canDraw;
        }
    }

    renderCategories() {
        const list = this.el("roi_category_list");
        if (!list) return;
        list.textContent = "";

        for (const category of this.store.sortedCategories()) {
            const row = document.createElement("div");
            row.className = "roi-category";
            row.classList.toggle("is-active", category.id === this.store.activeCategoryId);
            row.classList.toggle("is-hidden", category.visible === false);

            const swatch = document.createElement("input");
            swatch.type = "color";
            swatch.className = "roi-swatch";
            swatch.value = category.color;
            swatch.title = `Colour for ${category.label}`;
            swatch.addEventListener("change", () => {
                this.updateCategory(category.id, { color: swatch.value });
            });

            const label = document.createElement("button");
            label.type = "button";
            label.className = "roi-category-label";
            // textContent, not innerHTML: this string is whatever the user (or
            // an imported file) typed.
            label.textContent = category.label;
            label.title = `Draw in ${category.label}`;
            label.addEventListener("click", () => this.store.setActiveCategory(category.id));
            label.addEventListener("dblclick", () => this.renameCategory(category));

            const count = document.createElement("span");
            count.className = "roi-category-count";
            count.textContent = String(this.store.countFor(category.id));

            row.append(swatch, label, count);
            row.append(
                this.categoryButton(
                    category.visible === false ? "fa-eye-slash" : "fa-eye",
                    category.visible === false ? "Show" : "Hide",
                    () => this.updateCategory(category.id, { visible: category.visible === false })),
                this.categoryButton(
                    category.locked ? "fa-lock" : "fa-lock-open",
                    category.locked ? "Unlock" : "Lock",
                    () => this.updateCategory(category.id, { locked: !category.locked })),
            );
            row.append(this.categoryButton("fa-trash", "Delete category",
                () => this.deleteCategory(category.id), "roi-category-delete"));
            list.append(row);
        }

        // Shown only when the list is empty, and it is what the panel says
        // instead of shipping a category the user did not ask for.
        const empty = this.el("roi_category_empty");
        if (empty) empty.hidden = this.store.categories.length > 0;
    }

    categoryButton(icon, title, onClick, extraClass = "") {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `icon-button roi-category-action ${extraClass}`.trim();
        button.title = title;
        const glyph = document.createElement("span");
        glyph.className = `fas ${icon}`;
        button.append(glyph);
        button.addEventListener("click", onClick);
        return button;
    }

    renameCategory(category) {
        const label = window.prompt("Category name", category.label);
        if (label === null) return;
        const trimmed = label.trim();
        if (!trimmed || trimmed === category.label) return;
        if (this.store.categories.some(
            (c) => c.id !== category.id && c.label.toLowerCase() === trimmed.toLowerCase())) {
            this.notify(`There is already a category called "${trimmed}".`);
            return;
        }
        this.updateCategory(category.id, { label: trimmed });
    }

    renderSelection() {
        const panel = this.el("roi_selection_panel");
        const feature = this.store.selected;
        if (!panel) return;
        panel.hidden = !feature;
        if (!feature) return;

        const name = this.el("roi_selection_name");
        // Not overwritten while it has focus: doing so would fight the user
        // mid-word every time an autosave came back.
        if (name && document.activeElement !== name) name.value = feature.name || "";

        const select = this.el("roi_selection_category");
        if (select) {
            select.textContent = "";
            for (const category of this.store.sortedCategories()) {
                const option = document.createElement("option");
                option.value = category.id;
                option.textContent = category.label;
                option.selected = category.id === feature.category_id;
                select.append(option);
            }
        }

        // The dot in front of the dropdown, which is the only thing telling the
        // user that "Stroma" here is the same Stroma they picked a colour for.
        const swatch = this.el("roi_selection_swatch");
        if (swatch) {
            const category = this.store.category(feature.category_id);
            swatch.style.background = category ? category.color : "transparent";
        }

        // Which padlock is drawn is CSS's business, off `aria-pressed` -- see
        // the note in panel.html. Setting it here is the whole state change.
        const locked = this.el("roi_selection_locked");
        if (locked) {
            const on = Boolean(feature.locked);
            locked.setAttribute("aria-pressed", String(on));
            locked.classList.toggle("is-active", on);
            locked.title = on ? "Unlock this region" : "Lock this region";
        }

        const note = this.el("roi_selection_note");
        if (note) {
            const parts = [];
            if (this.store.isLocked(feature) && !feature.locked) {
                parts.push("Its category is locked.");
            }
            if (!RoiGeometry.isVertexEditable(feature.geometry)) {
                parts.push("Imported shape: it can be moved but its vertices cannot be edited.");
            }
            if (feature.flags && feature.flags.self_intersecting) {
                parts.push("This outline crosses itself.");
            }
            note.textContent = parts.join(" ");
            // Hidden when it has nothing to say, rather than an empty line that
            // still costs its margin under every selection.
            note.hidden = parts.length === 0;
        }
    }

    renderStatus() {
        const text = this.el("roi_status_text");
        const dot = this.el("roi_status_dot");
        if (!text || !dot) return;

        const labels = {
            saved: "Saved",
            saving: "Saving…",
            dirty: "Unsaved changes",
            failed: "Save failed",
            conflict: "Changed elsewhere",
            blocked: "Not saving",
        };
        text.textContent = this.store.statusDetail
            ? `${labels[this.store.status]} — ${this.store.statusDetail}`
            : labels[this.store.status] || "";
        dot.className = `roi-status-dot roi-status-${this.store.status}`;
    }

    /**
     * The row at the bottom: where this work goes when it leaves the panel.
     *
     * Every project gets a Save, because "how do I keep this?" is the same
     * question whatever the project was built from -- and the answer used to be
     * a small icon in the panel heading for anyone without an .h5ad, which is
     * not an answer anybody finds. A native destination gets the file write; a
     * CSV or image-only project gets the GeoJSON download in the same place,
     * the same size, saying what it does. Never both at once.
     *
     * Map to cells sits beside it rather than under it. Under, it read as the
     * step after saving; it is not one, and neither is a prerequisite of the
     * other. Its ? is hidden and shown with it for the same reason.
     *
     * The heading icons stay: they are the quick path for someone who already
     * knows where they are.
     */
    renderSourceButton() {
        const native = this.el("roi_save_to_source");
        const download = this.el("roi_export_download");
        if (!native || !download) return;

        if (this._destination === undefined) {
            this._destination = null;
            // Asked once, from the server: which native destination this
            // project has is a fact about how it was imported, and the panel
            // should show one button rather than three that mostly error.
            this.api.destination().then((result) => {
                const data = (result.ok && result.data.success) ? result.data : {};
                this._destination = {
                    kind: data.kind || null,
                    default_name: data.default_name || "",
                    remembered: data.remembered || "",
                    existing: data.existing || [],
                    // Whether this project has cells at all. Carried on the
                    // same answer because it is part of the same question --
                    // what can this project's regions be written to -- and the
                    // panel would otherwise make a second round trip to learn
                    // one boolean.
                    hasTable: Boolean(data.has_table),
                    replaceOnce: false,
                };
                if (this._destination.kind) {
                    // Just "Save". It used to name the format -- "Save to
                    // SpatialData store" -- which is both wider than half a
                    // 288px row and an answer to a question the button is not
                    // being asked: the user knows what they imported. Where it
                    // lands is spelled out under the name field the moment the
                    // button is pressed, down to `uns/plexora/rois`, and the
                    // tooltip carries it for anyone hovering first.
                    this.el("roi_save_to_source_label").textContent = "Save";
                    native.title = this._destination.kind === "anndata"
                        ? "Save these regions into your AnnData file"
                        : "Save these regions into your SpatialData store";
                    const field = this.el("roi_destination_name");
                    // Seeded with where this project last saved, so the second
                    // save is a click rather than a name typed correctly twice.
                    if (field) {
                        field.value = this._destination.remembered
                            || this._destination.default_name;
                    }
                }
                this.renderSourceButton();
            }).catch(() => { /* no native target; the GeoJSON download stands in */ });
        }

        // Nothing drawn yet means nothing to save, and a button that would
        // write an empty file is worse than no button.
        const kind = this._destination && this._destination.kind;
        const anything = this.store.features.length > 0;
        native.hidden = !kind || !anything;
        download.hidden = Boolean(kind) || !anything;

        // Mapping is a different offer from saving, and gated on a different
        // fact: saving needs somewhere to put polygons, mapping needs rows to
        // put labels on. A project that is nothing but an image has neither
        // button; a CSV project has the download and this one.
        const mapper = this.el("roi_map_to_cells");
        const info = this.el("roi_map_info");
        if (mapper) {
            const canMap = Boolean(this._destination
                && this._destination.hasTable) && anything;
            mapper.hidden = !canMap;
            // The ? goes with the button, not with the panel: an explanation of
            // a control that is not on screen is a control of its own.
            if (info) info.hidden = !canMap;
        }

        // The field cannot outlive the button that opened it: undoing the last
        // region while it is open would otherwise leave a "Save as" for a save
        // there is no longer anything to make.
        if (native.hidden) this._destinationOpen = false;
        const field = this.el("roi_destination");
        if (field) field.hidden = !this._destinationOpen;
        native.setAttribute("aria-expanded", this._destinationOpen ? "true" : "false");
        this.renderDestination();
    }

    /**
     * The name the two cell columns are derived from.
     *
     * Not `destinationName()`, which falls back to `default_name` -- that is
     * `plexora_rois` for a SpatialData project, and `plexora_rois_category` is
     * not a column name anybody wants to type. A blank is left blank and the
     * server supplies `rois` for every format, so the two ends cannot drift.
     */
    mapPrefix() {
        const field = this.el("roi_destination_name");
        const typed = (field?.value || "").trim();
        return typed || (this._destination?.remembered || "");
    }

    /**
     * The two columns the button is about to write, named.
     *
     * The blank `mapPrefix()` returns is the server's cue to use `rois`, so
     * that default is spelled out here too -- this is the one thing in the
     * dialog that is about the user's project rather than about the feature,
     * and "it depends" is not what they came to read.
     */
    mapColumnNames() {
        const prefix = this.mapPrefix() || "rois";
        return [`${prefix}_category`, `${prefix}_name`];
    }

    /**
     * What Map to cells does, at length.
     *
     * Filled in at open rather than at render: the column names follow the save
     * name, which the user can be typing while the panel is open, and a dialog
     * nobody has looked at yet is the cheapest place in the panel to be right.
     */
    openMapInfo() {
        const dialog = this.el("roi_map_info_dialog");
        if (!dialog) return;
        const [category, name] = this.mapColumnNames();
        const categoryCell = this.el("roi_map_info_category");
        const nameCell = this.el("roi_map_info_name");
        if (categoryCell) categoryCell.textContent = category;
        if (nameCell) nameCell.textContent = name;
        dialog.showModal();
    }

    /**
     * Write the ROI columns onto this project's cells.
     *
     * Two questions can come back unanswered -- which column holds the cell id,
     * which holds the image id -- and both are asked here rather than at launch
     * because this is the only action that needs them. `requirements.require`
     * ignores whether the user was already offered the field and skipped it,
     * which is exactly right: skipping was a fine answer until they pressed a
     * button that cannot proceed without one.
     *
     * Branching on `needs` rather than on the message. The wording is for the
     * user; a client that greps it breaks the moment it is improved.
     */
    async mapToCells(replace = false) {
        const button = this.el("roi_map_to_cells");
        if (!button || button.hidden) return;

        button.disabled = true;
        try {
            const result = await window.PlexoraStatus.track(
                "Mapping ROIs to cells",
                this.api.mapToCells(this.mapPrefix(), replace));
            const data = result.data || {};

            if (!result.ok || !data.success) {
                if (data.needs && !replace) {
                    const collected = await this.ctx.requirements?.require([data.needs]);
                    // Retried once, and only once: a second refusal means the
                    // answer did not fix it, and asking again in a loop is how
                    // a modal becomes impossible to get out of.
                    if (collected) return this.mapToCells(replace);
                    this.notify(data.error || "Plexora needs one more answer first.");
                    return;
                }
                if (data.error === "column_exists") {
                    const suggestion = data.suggestion;
                    const proceed = window.confirm(
                        `Your cells already have a "${this.mapPrefix() || "rois"}" mapping.\n\n`
                        + "Replace it? The existing values in those two columns will be lost."
                        + (suggestion ? `\n\nCancel to save it as "${suggestion}" instead.` : ""));
                    if (proceed) return this.mapToCells(true);
                    if (suggestion) {
                        const field = this.el("roi_destination_name");
                        if (field) field.value = suggestion;
                        this.renderSourceButton();
                    }
                    return;
                }
                this.notify(data.error || "The cells could not be annotated.");
                return;
            }

            const columns = (data.columns || []).join(" and ");
            this.notify(`${data.n_assigned} of ${data.n_cells} cells are in an ROI. `
                + `Wrote ${columns}.`);
        } finally {
            button.disabled = false;
        }
    }

    /**
     * What the save button is about to write, spelled out underneath it.
     *
     * The full path rather than just the name: `uns/plexora/rois` is where this
     * ends up in somebody's file, and a user who is choosing between two names
     * is exactly the user who wants to see it. The other names in the file are
     * listed for the same reason -- a collision should be visible before it is
     * typed, not after it is refused.
     */
    renderDestination() {
        const hint = this.el("roi_destination_hint");
        const destination = this._destination;
        if (!hint || !destination || !destination.kind) return;

        const name = this.destinationName();
        const where = destination.kind === "anndata"
            ? `uns/plexora/${name}` : `shapes/${name}`;
        const others = destination.existing.filter((each) => each !== name);
        hint.textContent = others.length
            ? `Writes ${where}. Also in this file: ${others.join(", ")}.`
            : `Writes ${where}.`;
    }

    notify(message) {
        const box = this.el("roi_message");
        if (!box) return;
        box.textContent = message;
        box.hidden = false;
        if (this._messageTimer) clearTimeout(this._messageTimer);
        this._messageTimer = setTimeout(() => { box.hidden = true; }, 6000);
    }

    /** Distinct, readable-on-dark colours for new categories, in a fixed order
     *  so the same project gets the same palette every time. */
    static nextColor(index) {
        const palette = [
            "#e05c5c", "#38bdf8", "#34d399", "#f3b845", "#c084fc",
            "#f472b6", "#22d3ee", "#a3e635", "#fb923c", "#94a3b8",
        ];
        return palette[index % palette.length];
    }
}


if (window.Plexora) {
    window.Plexora.registerPlugin({
        name: "roi",
        // ROI draws its own overlay and never colours cells, so it does not
        // claim the cell layer -- claiming is exclusive, and taking it would
        // evict whichever plugin actually needs it.
        ownsCellLayer: false,
        createSidebarController(ctx) {
            return new RoiSidebarController(ctx);
        },
    });
}
