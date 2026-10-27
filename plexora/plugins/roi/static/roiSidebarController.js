/**
 * roiSidebarController.js - the panel, and the plugin's registration.
 *
 * Everything user-facing that is not on the image itself: the new-category
 * field, the save indicator, the import/export/save row, and the two banners
 * that appear when saving cannot proceed. The tools are on the image
 * (roiToolbar.js), shown and kept current from here. The category-and-region
 * tree between them is roiTree.js, which owns its own rows and calls back here
 * for every edit -- this file keeps the commit/undo bookkeeping in one place.
 *
 * Every piece of user text on this panel is written with `textContent`, never
 * `innerHTML`. Category and region names are user input, they survive a round
 * trip through the store, and they come back through an import from a file that
 * may have been written by anything at all -- so the one place they reach the
 * DOM is the one place that has to be careful.
 *
 * Registration is at the bottom of this file because it is loaded last (see
 * PLUGIN.scripts): by then RoiApi, RoiGeometry, RoiStore, RoiRenderer,
 * RoiInteraction and RoiTree all exist.
 */
class RoiSidebarController {

    constructor(ctx) {
        this.ctx = ctx;
        this.api = new RoiApi(ctx);
        this.store = new RoiStore(ctx, this.api);
        this.renderer = new RoiRenderer(ctx, this.store);
        // Regions where the XZ / YZ section panes cut them, on a z-stack.
        ctx.view?.addSectionOverlay?.("regions", (query) => this.renderer.sectionSpans(query));
        this.tools = new RoiInteraction(ctx, this.store, this.renderer);
        this.tools.onNotify = (message) => this.notify(message);
        // The wand on the bar opens magic select, and a second click puts it
        // away; every other tool is picked.
        this.toolbar = new RoiToolbar({
            onTool: (tool) => {
                if (tool === "magic") this.tools.toggleMagic();
                else this.tools.setTool(tool);
            },
        });
        this.tools.magicBars = this.toolbar.magicBars();
        // A carry started from a row's menu is busy too, with the options shut.
        this.tools.onBusy = (busy) => this.toolbar.setBusy(busy);
        // The tree decides what a click on a row MEANS; every one of these
        // decides what it DOES, because doing it is a commit with an undo
        // beside it and that bookkeeping belongs together.
        this.tree = new RoiTree({
            store: this.store,
            colorPresets: RoiSidebarController.PALETTE.map((hex, i) => ({
                hex, label: `Colour ${i + 1}`,
            })),
            onCategoryRename: (id, label) => this.renameCategory(id, label),
            onCategoryUpdate: (id, changes) => this.updateCategory(id, changes),
            onCategoryDelete: (id) => this.deleteCategory(id),
            onFeatureRename: (feature, name) => this.propertyChange(feature, { name }),
            onFeatureChange: (feature, changes) => this.propertyChange(feature, changes),
            onFeatureDelete: (feature) => this.tools.deleteFeature(feature),
            onFeatureSolid: (feature, action, shown) => {
                if (action === "carry") this.tools.carryThroughPlanes(feature);
                else if (action === "ellipsoid") this.tools.makeEllipsoid(feature, shown);
                else if (action === "flat-here") this.tools.makeFlat(feature, shown[0]);
                else if (action === "flat-all") this.tools.makeFlat(feature, null);
            },
            onSelect: () => this.renderer.schedule(),
            view: ctx.view,
        });
        this._messageTimer = null;
        this._unsubscribe = null;
        this._destinationOpen = false;
        //: Unsubscribes the wait for a clean store, while a reload another
        //: writer asked for is held back behind unsaved work.
        this._remoteWait = null;
    }

    // -- lifecycle -------------------------------------------------------

    setup() {
        this.bindNewCategory();
        this.bindTransfer();
        this.bindBanners();

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
        // Only while the panel is the one on screen: this is the restore hook,
        // and it runs for every project opened, ROI panel or not.
        if (window.PlexoraToolLoader?.activeTool() === "roi") this.ensureDefaultCategory();
    }

    /** Called by toolLoader when this panel becomes the selected one. */
    onShow() {
        this.renderer.attach();
        this.tools.arm();
        this.render();
        this.syncToolbar();
        this.ensureDefaultCategory();
    }

    /** The tools bar is on the image exactly while the tools are armed: a
     *  bar left over another tool's session would offer a pen that is not
     *  listening. */
    syncToolbar() {
        if (this.tools.armed) {
            this.toolbar.show();
            this.renderToolbar();
        } else {
            this.toolbar.hide();
        }
    }

    /**
     * A project with no categories gets one called "Undefined", selected, so
     * the first drag on the image draws a region instead of asking for a name.
     * It is an ordinary category -- renamed, recoloured or deleted like any
     * other -- and is saved without an undo entry (RoiStore.seed), because it
     * is where the project starts rather than something the user did.
     *
     * Only on a project with none, and only once the server's state is in:
     * a project reopened keeps its categories and its active one untouched.
     */
    ensureDefaultCategory() {
        const store = this.store;
        if (!store.loaded || !store.editable || store.categories.length > 0) return;
        const category = {
            id: RoiStore.newId("c"),
            label: RoiSidebarController.DEFAULT_CATEGORY,
            color: RoiSidebarController.nextColor(0),
            visible: true,
            locked: false,
            sort_order: 0,
        };
        if (store.seed([{ op: "category.create", category }])) {
            store.setActiveCategory(category.id);
        }
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
            this.syncToolbar();
            return;
        }
        this.renderer.attach();
        if (window.PlexoraToolLoader?.activeTool() === "roi") this.tools.arm();
        this.syncToolbar();
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
        this.syncToolbar();
        this.renderer.setEnabled(false);
        // A row menu is portaled out of this panel, so hiding the panel leaves
        // it floating over whatever the user came back to.
        RoiTree.closePopup();
    }

    destroy() {
        this._unsubscribe?.();
        this.cancelRemoteReload();
        if (this._messageTimer) clearTimeout(this._messageTimer);
        RoiTree.closePopup();
        this.tree.destroy();
        this.tools.destroy();
        this.toolbar.destroy();
        this.renderer.destroy();
        this.store.destroy();
    }

    el(id) {
        return document.getElementById(id);
    }

    // -- wiring ----------------------------------------------------------

    /**
     * The new-category field: always open, under the Categories heading.
     *
     * Enter makes the category -- the key the field's Enter cap names, and the
     * cap itself for a mouse. Leaving the field keeps what was typed and makes
     * nothing: a field that is always on screen is clicked away from all the
     * time, and a category made by the click that was going somewhere else is
     * one nobody asked for.
     */
    bindNewCategory() {
        const input = this.el("roi_category_name");
        this.el("roi_category_new")?.addEventListener("click", () => this.submitNewCategory());
        this.el("roi_category_all_eye")?.addEventListener("click", () => this.toggleAllCategories());
        // Bound to the key rather than a form's submit -- see the note in
        // panel.html on why there is no form.
        input?.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                this.submitNewCategory();
            } else if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();   // not the tools' Escape
                this.closeNewCategory();
            }
        });
    }

    /** Hide every category, or -- when all are already hidden -- show every
     *  one. One step on the undo stack, not one per category. */
    toggleAllCategories() {
        const categories = this.store.sortedCategories();
        if (!categories.length) return;
        const show = categories.every((c) => c.visible === false);
        const redo = [];
        const undo = [];
        for (const category of categories) {
            if ((category.visible !== false) === show) continue;
            redo.push({ op: "category.update", id: category.id, changes: { visible: show } });
            undo.push({ op: "category.update", id: category.id,
                        changes: { visible: category.visible !== false } });
        }
        if (!redo.length) return;
        this.store.commit({
            label: show ? "Show all categories" : "Hide all categories", redo, undo,
        });
        this.renderer.schedule();
    }

    renderAllEye() {
        const button = this.el("roi_category_all_eye");
        if (!button) return;
        const categories = this.store.sortedCategories();
        button.hidden = !categories.length;
        const allHidden = categories.length > 0 && categories.every((c) => c.visible === false);
        button.classList.toggle("is-off", allHidden);
        button.title = allHidden ? "Show all categories" : "Hide all categories";
        button.setAttribute("aria-pressed", allHidden ? "true" : "false");
    }

    /** Empty the field and let go of it, so V/P/F/R/E reach the tools again
     *  rather than being typed into the next name. */
    closeNewCategory() {
        const input = this.el("roi_category_name");
        if (!input) return;
        input.value = "";
        input.blur();
    }

    submitNewCategory() {
        const input = this.el("roi_category_name");
        if (!input) return;
        // An empty field: the Enter cap is not shown, but Enter still lands.
        if (!input.value.trim()) {
            input.focus();
            return;
        }
        // Refused rather than emptied on a duplicate name: the text is still
        // in the field and still wrong, and emptying it would throw it away
        // without the user having agreed to that.
        if (this.createCategory(input.value)) this.closeNewCategory();
        else { input.focus(); input.select(); }
    }

    bindTransfer() {
        this.el("roi_export_button")?.addEventListener("click", () => this.exportGeoJSON());

        const picker = this.el("roi_import_file");
        this.el("roi_import_button")?.addEventListener("click", () => picker?.click());
        picker?.addEventListener("change", async () => {
            const file = picker.files && picker.files[0];
            picker.value = "";  // so re-picking the same file fires again
            if (file) await this.importGeoJSON(file);
        });
        // A GeoJSON dropped on this panel from Explorer or Finder (desktop
        // app) is an Import. Anything else dropped here is not this panel's
        // and goes on to the viewer around it, which knows masks and tables.
        const panel = this.el("roi_panel_section");
        if (panel && typeof attachDropTarget === "function") {
            this._detachDrop?.();
            this._detachDrop = attachDropTarget(panel, {
                filter: "any", mode: "file", count: "one",
                accepts: (path, name) => /\.(geo)?json$/i.test(name),
                hint: () => "Import regions",
                onDrop: async (path) => {
                    let file;
                    try {
                        file = await window.PlexoraDesktop.fileFromPath(path);
                    } catch (error) {
                        this.notify(error.message || "That file could not be read.");
                        return;
                    }
                    await this.importGeoJSON(file);
                },
            });
        }

        this.el("roi_save_to_source")?.addEventListener("click", () => this.saveToSource());
        this.el("roi_map_to_cells")?.addEventListener("click", () => this.mapToCells());
        const fromCells = this.el("roi_from_cells");
        if (fromCells) {
            // Only where there are whole cells in depth to make a region of.
            fromCells.hidden = !this.ctx.view?.labelsAre3d?.();
            fromCells.addEventListener("click", () => this.regionFromCells());
        }
        this.el("roi_destination_close")?.addEventListener("click", () => this.closeDestination());

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

    // -- the viewer control plane (roiAgentBridge.js) ---------------------

    /**
     * Take on regions somebody else saved -- an agent, from another process.
     *
     * The same `load()` a page open does, then a repaint with every cached
     * path dropped, since any of them may have moved.
     *
     * NEVER over unsaved work. A queue that has not reached the server is the
     * user's drawing, and `load()` empties the queue: reloading now would be
     * the silent overwrite the revision check on save exists to prevent,
     * committed from the other side. So the reload is deferred -- the notice
     * says so -- and runs by itself the moment the store is clean again, which
     * is either the queued save landing or the user settling a conflict
     * through the banner (which itself reloads; the second load is a no-op).
     *
     * @returns whether it reloaded now.
     */
    async reloadFromServer() {
        if (this.store.hasUnsavedWork || this.store._flushing) {
            this.deferRemoteReload();
            return false;
        }
        this.cancelRemoteReload();
        const loaded = await this.store.load();
        this.renderer.invalidate();
        this.renderer.schedule();
        return loaded;
    }

    deferRemoteReload() {
        this.notify("These regions were changed elsewhere. Your unsaved edits are kept; "
            + "the new version loads as soon as they are saved.");
        if (this._remoteWait) return;
        this._remoteWait = this.store.onChange(() => {
            if (this.store.hasUnsavedWork || this.store._flushing
                || this.store.status !== "saved") return;
            this.cancelRemoteReload();
            this.reloadFromServer().catch((error) => {
                console.error("ROI: could not take on the new regions", error);
            });
        });
    }

    cancelRemoteReload() {
        if (!this._remoteWait) return;
        this._remoteWait();
        this._remoteWait = null;
    }

    /**
     * Select a region and frame it -- `focus_roi`.
     *
     * Framed with a tenth of its size spare on every side, so its outline is
     * not drawn on the edge of the screen. Through core's viewport helper
     * (services/viewerScene.js): regions are stored in full-resolution image
     * pixels, which is exactly what it takes.
     */
    focusRegion(id) {
        const feature = this.store.feature(id);
        if (!feature) throw new Error(`no region ${JSON.stringify(id)} in this project`);
        this.store.select(id);
        this.renderer.schedule();
        const box = RoiGeometry.bounds(feature.geometry);
        if (box && window.PlexoraViewerScene && this.ctx.viewer) {
            const pad = Math.max(box.maxX - box.minX, box.maxY - box.minY, 1) * 0.1;
            window.PlexoraViewerScene.fitRegion(this.ctx.viewer, {
                x: box.minX - pad, y: box.minY - pad,
                width: box.maxX - box.minX + 2 * pad, height: box.maxY - box.minY + 2 * pad,
            });
        }
        return {
            roi_id: id,
            selected: true,
            name: feature.name || null,
            bounds: box ? { x: box.minX, y: box.minY,
                            width: box.maxX - box.minX, height: box.maxY - box.minY } : null,
        };
    }

    // -- categories ------------------------------------------------------

    /** True when a category was made. The caller uses that to decide whether
     *  to close the field or leave the rejected name in it. */
    createCategory(rawLabel) {
        const label = (rawLabel || "").trim();
        if (!label) return false;
        if (this.store.categories.some((c) => c.label.toLowerCase() === label.toLowerCase())) {
            this.notify(`There is already a category called "${label}".`);
            return false;
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
        return true;
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
    async deleteCategory(id) {
        const category = this.store.category(id);
        if (!category) return;

        const count = this.store.countFor(id);
        const destination = this.store.sortedCategories().find((c) => c.id !== id) || null;
        let orphans = "delete";

        if (count > 0) {
            const regions = `${count} ROI${count === 1 ? "" : "s"}`;
            // One question with the two outcomes named, rather than two
            // yes/no boxes in a row where "Cancel" meant "delete them" in the
            // first and "do nothing" in the second.
            const choices = [];
            if (destination) {
                choices.push({
                    value: "reassign",
                    label: `Move ${regions} to "${destination.label}"`,
                    kind: "primary",
                });
            }
            choices.push({ value: "delete", label: `Delete ${regions}`, kind: "danger" });
            choices.push({ value: null, label: "Cancel", focus: true });

            const answer = await PlexoraConfirm.choose({
                title: `Delete "${category.label}"?`,
                body: destination
                    ? `It has ${regions}. They can be kept in another category, `
                        + "or deleted with it."
                    : `It has ${regions}, and there is no other category to move `
                        + "them to. Deleting it deletes them.",
                choices,
                stack: true,
            });
            // Null is Escape and the backdrop as well as Cancel: a dismissed
            // question has to be the answer that changes nothing.
            if (answer === null) return;
            orphans = answer;
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

    /** Rename in place, from the tree. False when the name is taken, which
     *  is the tree's cue to put the old one back. Renaming a category does
     *  NOT rename the regions already named after it: those are their own
     *  labels now, and somebody who typed over one would lose it. */
    renameCategory(id, rawLabel) {
        const category = this.store.category(id);
        if (!category) return false;
        const label = (rawLabel || "").trim();
        if (!label || label === category.label) return false;
        if (this.store.categories.some(
            (c) => c.id !== id && c.label.toLowerCase() === label.toLowerCase())) {
            this.notify(`There is already a category called "${label}".`);
            return false;
        }
        this.updateCategory(id, { label });
        return true;
    }

    // -- regions ---------------------------------------------------------

    propertyChange(feature, changes) {
        const before = {};
        for (const key of Object.keys(changes)) {
            // `visible` is absent from anything drawn before the flag existed,
            // and `undefined` does not survive JSON -- so an undo of the first
            // hide would arrive at the server with no change in it at all.
            // `extent` likewise: absent means every plane, and the undo has to
            // say so out loud.
            before[key] = key === "visible" ? feature.visible !== false
                : key === "extent" ? (feature.extent || { kind: "all" })
                    : feature[key];
        }
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
        // The renderer does not subscribe to the store, and all three of
        // visible, locked and category_id change what is drawn.
        this.renderer.schedule();
    }

    // -- import / export -------------------------------------------------

    async exportGeoJSON() {
        window.PlexoraTelemetry?.feature("roi.export", "roi");
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
        if (!acceptMismatch) window.PlexoraTelemetry?.feature("roi.import", "roi");
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
            const proceed = await window.PlexoraConfirm.ask({
                title: "Import ROIs drawn on a different image size?",
                body: [`These ROIs were drawn on an image ${found[0]} x ${found[1]} px. `
                       + `This image is ${expected[0]} x ${expected[1]} px.`,
                       "They would be imported without transforming them."],
                confirm: "Import anyway",
            });
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
        // Already saved: a QC export brought back in moves QC's calls.
        this.store.announceQc([operation]);
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
        window.PlexoraTelemetry?.feature("roi.save", "roi");

        const name = this.destinationName();
        if (destination.kind === "anndata" && name !== destination.remembered
            && destination.existing.includes(name)) {
            const proceed = await window.PlexoraConfirm.fromText(
                `"${name}" already exists in this file.\n\n`
                + "Replace it? The annotations currently stored under that name will be lost.",
                { confirm: "Replace" });
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
        this.tree.render();
        this.renderStatus();
        this.renderSourceButton();
        this.renderAllEye();
        const name = this.el("roi_category_name");
        if (name) name.disabled = !this.store.editable;
        const add = this.el("roi_category_new");
        if (add) add.disabled = !this.store.editable;
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
        // Select stays available whenever the pointer works at all; the three
        // that MAKE a shape also need a category to put it in. Magic select
        // can also refine a selected region with no category.
        this.toolbar.render(this.tools.tool, (tool) => (tool === "select"
            ? !this.tools.ready
            : tool === "magic"
                ? !(this.tools.canDraw || (this.tools.ready && this.store.selected))
                : !this.tools.canDraw));
    }

    /**
     * The half of the old status row that still has something to say.
     *
     * There is no resting indicator any more: "Saved", "Saving" and "Unsaved
     * changes" are the states the panel is in for all but a few hundred
     * milliseconds of a session, and a permanent line reporting them earned
     * none of the room it took. A FAILED autosave is different -- it was
     * announced nowhere else, so losing the row outright would have made a
     * server that stopped answering look exactly like one that was keeping up.
     *
     * Announced on the EDGE, not on every render: `render()` runs on every
     * store change, and a stuck retry would otherwise re-raise the same
     * sentence every few hundred ms and keep resetting its own dismissal.
     * Conflict and blocked have banners of their own; this is the one case
     * with no other voice.
     */
    renderStatus() {
        const status = this.store.status;
        if (status === this._lastStatus) return;
        this._lastStatus = status;
        if (status !== "failed") return;
        this.notify(this.store.statusDetail
            ? `Save failed — ${this.store.statusDetail}`
            : "Save failed.");
    }

    /**
     * The row at the bottom: where this work goes when it leaves the panel.
     *
     * Every project gets a Save, because "how do I keep this?" is the same
     * question whatever the project was built from -- and the answer used to be
     * a small icon in the panel heading for anyone without an .h5ad, which is
     * not an answer anybody finds. A native destination gets the file write.
     *
     * Export is no longer the stand-in for it -- it is offered alongside, for
     * every project with anything drawn, because "give me the file" is a
     * separate want from "put it back where it came from", and a user with an
     * .h5ad had no way to ask for it once the icons left the card header.
     *
     * Map to cells sits on the same row rather than under it. Under, it read
     * as the step after saving; it is not one, and neither is a prerequisite
     * of the other. It is a glyph, so Import and Export keep their words
     * whatever else is on the row; what it does is under the card's ?.
     */
    renderSourceButton() {
        const native = this.el("roi_save_to_source");
        const download = this.el("roi_export_button");
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
        download.hidden = !anything;

        // Mapping is a different offer from saving, and gated on a different
        // fact: saving needs somewhere to put polygons, mapping needs rows to
        // put labels on. A project that is nothing but an image has neither
        // button; a CSV project has the download and this one.
        const mapper = this.el("roi_map_to_cells");
        if (mapper) {
            const canMap = Boolean(this._destination
                && this._destination.hasTable) && anything;
            mapper.hidden = !canMap;
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
     * card's help that is about the user's project rather than about the
     * feature, and "it depends" is not what they came to read.
     */
    mapColumnNames() {
        const prefix = this.mapPrefix() || "rois";
        return [`${prefix}_category`, `${prefix}_name`];
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
    /** The cells selected in the viewer, as one region of the 3-D mask. */
    async regionFromCells() {
        const selection = this.ctx.dataLayer?.getCurrentSelection?.();
        const ids = selection ? [...selection.keys()].map(Number).filter((v) => v > 0) : [];
        if (!ids.length) {
            this.notify("Select cells in the viewer first (click a cell; Shift-click adds more), "
                + "then make them a region.");
            return;
        }
        const result = await window.PlexoraStatus.track(
            "Outlining the cells", this.api.fromCells(ids, this.ctx.view?.frame?.() || 0));
        const data = result.data || {};
        if (!result.ok || !data.success) {
            this.notify(data.error || "The cells could not be made a region.");
            return;
        }
        await this.store.load();
        if (data.roi?.id) this.store.select?.(data.roi.id);
    }

    /**
     * Check, confirm, then write.
     *
     * The write goes into the user's own file and Plexora has no undo for it,
     * so the first request is a preview: the server runs the assignment and
     * says what it found and where it would go, and nothing is written until
     * the user has seen that and said yes. An existing mapping is decided in
     * the same dialog -- replace it, or write beside it under a free name --
     * rather than in a second one after the first.
     *
     * The write runs the assignment again rather than reusing the preview's:
     * the table can change while the dialog is open, and what is written
     * should be what it holds when the user presses the button.
     */
    async mapToCells(retried = false) {
        const button = this.el("roi_map_to_cells");
        if (!button || button.hidden) return;

        button.disabled = true;
        try {
            const preview = await window.PlexoraStatus.track(
                "Finding the cells in each ROI",
                this.api.mapToCells(this.mapPrefix(), false, true));
            const found = preview.data || {};
            // A reply that is not a preview came from code that does not know
            // the flag -- a cached roiApi.js, or a data node on an older
            // Plexora -- and was handled as a WRITE. Say so plainly rather
            // than show it as the answer to a question it was never asked.
            if (found.preview !== true && (found.success || found.error === "column_exists")) {
                this.notify(found.success
                    ? "Part of Plexora is out of date, so the columns were written without "
                        + "the check. Reload the page (or restart Plexora) before mapping again."
                    : "Part of Plexora is out of date and skipped the check; nothing was "
                        + "written. Reload the page (or restart Plexora) and try again.");
                return;
            }
            if (!preview.ok || !found.success) {
                if (found.needs && !retried) {
                    const collected = await this.ctx.requirements?.require([found.needs]);
                    // Retried once, and only once: a second refusal means the
                    // answer did not fix it, and asking again in a loop is how
                    // a modal becomes impossible to get out of.
                    if (collected) return this.mapToCells(true);
                    this.notify(found.error || "Plexora needs one more answer first.");
                    return;
                }
                this.notify(found.error || "The cells could not be annotated.");
                return;
            }

            const choice = await this.confirmMapping(found);
            if (!choice) return;
            if (choice === "rename" && found.suggestion) {
                const field = this.el("roi_destination_name");
                if (field) field.value = found.suggestion;
                this.renderSourceButton();
            }

            const result = await window.PlexoraStatus.track(
                "Writing ROI columns to the cell table",
                this.api.mapToCells(this.mapPrefix(), choice === "replace"));
            const data = result.data || {};
            if (!result.ok || !data.success) {
                // Only possible if the columns appeared while the dialog was
                // open: the preview said they were free.
                this.notify(data.error === "column_exists"
                    ? "Those columns were added to the file since the check. "
                        + "Press Map to cells again to see them."
                    : (data.error || "The cells could not be annotated."));
                return;
            }

            // The table has new (or replaced) columns: any column the viewer
            // cached for its browser-side gate may now be stale.
            this.ctx.viewer?.numericData?.invalidateColumns?.();
            const columns = (data.columns || []).join(" and ");
            this.notify(`${RoiSidebarController.count(data.n_assigned)} of `
                + `${RoiSidebarController.count(data.n_cells)} cells are in an ROI. `
                + `Wrote ${columns}.`);
        } finally {
            button.disabled = false;
        }
    }

    /**
     * The confirmation: what will be written, where, and to how many cells.
     *
     * Totals only. A breakdown by category is a list as long as the project's
     * category list, which has no upper bound; "how many cells, in how many of
     * my regions" is what tells a user whether this is the mapping they meant,
     * at any size. The regions that caught nothing are named, a few at most,
     * because those are the usual sign of a region on the wrong plane or frame.
     *
     * Resolves "write", "replace", "rename" (write under the suggested free
     * name), or null for no.
     */
    confirmMapping(found) {
        const n = RoiSidebarController.count;
        const el = (tag, className, text) => {
            const node = document.createElement(tag);
            if (className) node.className = className;
            if (text !== undefined) node.textContent = text;
            return node;
        };
        const root = el("div", "roi-map-preview");

        const headline = el("p", "roi-map-headline");
        if (found.n_assigned > 0) {
            headline.append(el("strong", "", n(found.n_assigned)),
                ` of ${n(found.n_cells)} cells fall in a region`);
        } else {
            headline.textContent = `None of the ${n(found.n_cells)} cells falls in a region`;
        }
        root.append(headline);

        const facts = el("ul", "roi-map-facts");
        const fact = (text) => facts.append(el("li", "", text));
        const plural = (value, one, many) => `${n(value)} ${value === 1 ? one : many}`;
        if (found.n_assigned > 0) {
            const regions = found.n_regions_used === found.n_regions
                ? `In ${found.n_regions === 1 ? "the one region" : `all ${n(found.n_regions)} regions`}`
                : `In ${n(found.n_regions_used)} of ${plural(found.n_regions, "region", "regions")}`;
            fact(`${regions}, across ${plural(found.n_categories_used, "category", "categories")}.`);
        } else {
            fact("Both columns would be blank for every cell.");
        }
        if (found.n_overlap > 0) {
            fact(`${plural(found.n_overlap, "cell is", "cells are")} in more than one region; `
                + "their values are joined with _.");
        }
        if (found.n_empty_regions > 0 && found.n_assigned > 0) {
            const shown = (found.empty_regions || []).join(", ");
            const more = found.n_empty_regions - (found.empty_regions || []).length;
            fact(`${plural(found.n_empty_regions, "region holds", "regions hold")} no cells: `
                + shown + (more > 0 ? `, and ${n(more)} more` : "") + ".");
        }
        if (found.location === "obs") {
            fact("Cells of other images in this file keep their values.");
        }
        root.append(facts);

        const target = el("dl", "roi-map-target");
        target.append(el("dt", "", "Writes to"));
        const where = el("dd", "");
        where.append(el("code", "roi-map-path", found.path || ""));
        if (found.location === "obs") where.append(el("span", "roi-map-where", " (obs)"));
        target.append(where);
        target.append(el("dt", "", "Columns"));
        const columns = el("dd", "roi-map-columns");
        for (const column of found.columns || []) columns.append(el("code", "", column));
        const existing = (found.existing || []).length > 0;
        columns.append(el("span", `roi-map-badge ${existing ? "is-replace" : "is-new"}`,
            existing ? "Already exist" : "New"));
        target.append(columns);
        root.append(target);

        const choices = [{ value: null, label: "Cancel", focus: existing }];
        if (existing) {
            if (found.suggestion) {
                choices.push({ value: "rename", label: `Write as ${found.suggestion}` });
            }
            choices.push({ value: "replace", label: "Replace columns", kind: "danger" });
        } else {
            choices.push({ value: "write", label: "Write to file", kind: "primary", focus: true });
        }
        return window.PlexoraConfirm.choose({
            title: "Write ROI labels to your cells?",
            body: existing
                ? "These columns are already in your file. Replacing them loses "
                    + "their current values; Plexora cannot undo it."
                : "This adds two columns to your file. Plexora cannot undo it.",
            content: root,
            choices,
        });
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
     *  so the same project gets the same palette every time. Also what the
     *  colour picker on a category row offers, so recolouring one lands on the
     *  same ten a new one would have been given. */
    /** A count as the user reads one: 52,107, not 52107. */
    static count(value) {
        return Number(value || 0).toLocaleString("en-US");
    }

    static nextColor(index) {
        return RoiSidebarController.PALETTE[index % RoiSidebarController.PALETTE.length];
    }

    /**
     * The card's help, in the order the panel is used: make a category, draw
     * into it, tidy what was drawn, take it elsewhere, label the cells.
     *
     * Built at open, from the live controller when there is one: Map to cells
     * names the two columns the current save name will produce, and is left
     * out once the project is known to have no cell table to write them to.
     */
    static helpSections() {
        const live = window.__plexora?.plugins?.get?.("roi")?.sidebarController;
        const [category, name] = live?.mapColumnNames?.() || ["rois_category", "rois_name"];
        const hasTable = live?._destination ? live._destination.hasTable !== false : true;

        const sections = [
            {
                title: "Start with a category",
                icon: "fas fa-layer-group",
                text: "Every region belongs to a category, such as Tumour or Stroma. "
                    + "Type a name in Add new category and press Enter, then click it to draw into it: new "
                    + "regions land under it and are named after it (Tumour 1, Tumour 2).",
                points: [
                    "The eye on a row hides that region or category on the image; the "
                        + "eye at the end of the Categories heading hides them all.",
                    "A category's ⋯ renames, recolours, locks or deletes it with its regions.",
                ],
                shortcuts: [{ keys: ["←", "→"], label: "Collapse or expand a category" }],
            },
            {
                title: "Draw",
                icon: "fas fa-pen-nib",
                text: "Pick a tool from the bar at the top of the image. Polygon places "
                    + "a point per click; Freehand and Rectangle follow a press and drag.",
                shortcuts: [
                    { keys: "P", label: "Polygon" },
                    { keys: "F", label: "Freehand" },
                    { keys: "R", label: "Rectangle" },
                    { keys: "Enter", label: "Finish a polygon (or double-click)" },
                    { keys: "⌫", label: "Remove the polygon's last point" },
                    { keys: "Esc", label: "Cancel the shape, then deselect" },
                    { keys: "Space", label: "Hold and drag to pan" },
                ],
            },
            {
                title: "Magic select",
                icon: "fas fa-wand-magic-sparkles",
                text: "Click something on the image and Plexora outlines it. Its options "
                    + "open beside the wand on the tools bar and set how you point at it; "
                    + "click the wand again to put them away. It sets itself up the first "
                    + "time you use it (a short one-time download).",
                points: [
                    "Add: click to outline; click again to grow the outline.",
                    "Remove: click an area to take it out.",
                    "Box: drag round a large object.",
                    "Scribble: draw over a long or patchy object, or a loop round it.",
                    "To refine a region later, select it and click inside it.",
                ],
                shortcuts: [
                    { keys: "E", label: "Magic select" },
                    { keys: "⇧ Click", label: "Remove an area" },
                    { keys: "⇧ Drag", label: "Scribble out what to leave out" },
                    { keys: "Esc", label: "Finish the outline" },
                ],
            },
            {
                title: "Edit regions",
                icon: "fas fa-hand",
                text: "With Select, click a region to select it, drag it to move it, or "
                    + "drag a handle to reshape it. A region's ⋯ renames, locks, hides, "
                    + "moves it to another category or deletes it; a locked region "
                    + "cannot be moved or reshaped.",
                shortcuts: [
                    { keys: "V", label: "Select" },
                    { keys: "⌫", label: "Delete the selected region" },
                    { keys: "F2", label: "Rename (or double-click the name)" },
                    { keys: "Mod+Z", label: "Undo" },
                    { keys: "Mod+Shift+Z", label: "Redo" },
                ],
            },
            {
                title: "Import, export and save",
                icon: "fas fa-file-export",
                text: "Regions are kept with this sample as you work. The buttons at "
                    + "the foot of the panel take them in and out:",
                points: [
                    "Import reads a GeoJSON file of regions.",
                    "Export downloads these regions as a GeoJSON file.",
                    "Save, for an AnnData or SpatialData project, writes them into your "
                        + "file. The first press shows the name and where it will land; "
                        + "the second writes.",
                ],
            },
        ];
        if (hasTable) {
            sections.push({
                title: "Map to cells",
                icon: "fas fa-table-cells",
                text: "The grid button at the foot of the panel finds the region(s) each "
                    + `cell falls in and writes two columns onto the cell table: ${category} `
                    + `(the category, e.g. Tumour) and ${name} (the region, e.g. Tumour 1). `
                    + "The names follow the save name.",
                points: [
                    "A cell in overlapping regions gets their values joined with _; a "
                        + "category is listed once.",
                    "Cells outside every region are left blank. Only the cells of the "
                        + "image on screen are touched.",
                    "Nothing is written until you confirm: the button first shows how "
                        + "many cells fall in a region and which file and columns it "
                        + "will write, and asks before replacing columns already there.",
                    "A hidden region is still mapped: the eye is a viewing aid, not a filter.",
                    "Save keeps the shapes; Map to cells labels the cells. Neither needs "
                        + "the other.",
                ],
            });
        }
        return sections;
    }
}


RoiSidebarController.PALETTE = [
    "#e05c5c", "#38bdf8", "#34d399", "#f3b845", "#c084fc",
    "#f472b6", "#22d3ee", "#a3e635", "#fb923c", "#94a3b8",
];

//: The category a project with none is given (ensureDefaultCategory).
RoiSidebarController.DEFAULT_CATEGORY = "Undefined";


if (window.Plexora) {
    window.Plexora.registerPlugin({
        name: "roi",
        // What the `?` on the ROI card explains (views/pluginHelp.js); `docs`
        // is its page on the documentation site. The only help this panel has:
        // the keys, the tools and Map to cells used to sit behind two more ?s
        // inside the body, which made three for one card.
        help: {
            summary: "Draw regions of interest on the image and keep them with this "
                + "sample. With a cell table, Plexora can also record which region "
                + "each cell falls in, so the regions can be used in analysis.",
            docs: "plugins/roi",
            sections: () => RoiSidebarController.helpSections(),
        },
        // ROI draws its own overlay and never colours cells, so it does not
        // claim the cell layer -- claiming is exclusive, and taking it would
        // evict whichever plugin actually needs it.
        ownsCellLayer: false,
        createSidebarController(ctx) {
            return new RoiSidebarController(ctx);
        },
    });
}
