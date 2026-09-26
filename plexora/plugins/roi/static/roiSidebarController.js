/**
 * roiSidebarController.js - the panel, and the plugin's registration.
 *
 * Everything user-facing that is not on the image itself: the tool buttons, the
 * new-category control, the save indicator, the import/export/save row, and the
 * two banners that appear when saving cannot proceed. The category-and-region
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
        this.tools = new RoiInteraction(ctx, this.store, this.renderer);
        this.tools.onNotify = (message) => this.notify(message);
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
            onSelect: () => this.renderer.schedule(),
        });
        this._messageTimer = null;
        this._unsubscribe = null;
        this._destinationOpen = false;
        //: Guards the new-category field against the blur that follows Enter
        //: making a second, empty category out of the same keystroke.
        this._creating = false;
        //: Unsubscribes the wait for a clean store, while a reload another
        //: writer asked for is held back behind unsaved work.
        this._remoteWait = null;
    }

    // -- lifecycle -------------------------------------------------------

    setup() {
        this.bindToolbar();
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
        // An empty project's first act is naming a category -- there is
        // nothing else this panel can do until one exists -- so the field is
        // already open and focused rather than behind a button. Escape closes
        // it for anyone who opened the panel to look rather than to draw.
        if (this.store.editable && this.store.categories.length === 0) {
            this.openNewCategory();
        }
    }

    /** Called by toolLoader when this panel becomes the selected one. */
    onShow() {
        this.renderer.attach();
        this.tools.arm();
        this.render();
        // applyOrDefault() opens the field on an empty project, but the panel
        // may not have been on screen yet -- focus() on an unrendered element
        // does nothing -- so the caret is placed when it becomes visible.
        // Narrow on purpose: only the first-run state, never a return visit
        // to a project that already has categories.
        if (this.store.editable && this.store.categories.length === 0) {
            const row = this.el("roi_category_new_row");
            const input = this.el("roi_category_name");
            if (row && !row.hidden && input && !input.value) input.focus();
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
        // A row menu is portaled out of this panel, so hiding the panel leaves
        // it floating over whatever the user came back to.
        RoiTree.closePopup();
        // The info dialog lives inside the panel, so hiding the panel takes it
        // off the screen without closing it -- and it would be waiting, open,
        // over whatever the user came back to.
        this.el("roi_map_info_dialog")?.close();
    }

    destroy() {
        this._unsubscribe?.();
        this.cancelRemoteReload();
        if (this._messageTimer) clearTimeout(this._messageTimer);
        RoiTree.closePopup();
        this.tree.destroy();
        this.tools.destroy();
        this.renderer.destroy();
        this.store.destroy();
    }

    el(id) {
        return document.getElementById(id);
    }

    // -- wiring ----------------------------------------------------------

    bindToolbar() {
        for (const button of document.querySelectorAll("#roi_toolbar [data-tool]")) {
            button.addEventListener("click", () => this.tools.setTool(button.dataset.tool));
        }
        const help = this.el("roi_help_button");
        help?.addEventListener("click", (event) => {
            event.stopPropagation();
            // Off the button's own aria-expanded, so a second click on the ?
            // shuts what the first one opened instead of closing and
            // reopening it in the same gesture.
            if (help.getAttribute("aria-expanded") === "true") RoiTree.closePopup();
            else RoiTree.popup(help, this.helpContent(), { align: "right" });
        });
    }

    /**
     * Everything the panel used to say in prose, on demand.
     *
     * It was two lines under the toolbar and four shortcut chips inside it,
     * permanently, for something read once. A ? costs one icon and holds more
     * than the panel had room to print.
     */
    helpContent() {
        const card = document.createElement("div");
        card.className = "roi-help";
        const mod = /Mac|iPhone|iPad/.test(navigator.platform) ? "\u2318" : "Ctrl";

        const section = (title, rows) => {
            const heading = document.createElement("div");
            heading.className = "roi-help-title";
            heading.textContent = title;
            card.append(heading);
            for (const [text, key] of rows) {
                const row = document.createElement("div");
                row.className = "roi-help-row";
                const label = document.createElement("span");
                label.textContent = text;
                row.append(label);
                if (key) {
                    const kbd = document.createElement("kbd");
                    kbd.className = "roi-kbd";
                    kbd.textContent = key;
                    row.append(kbd);
                }
                card.append(row);
            }
        };

        section("Tools", [
            ["Select", "V"], ["Polygon", "P"], ["Freehand", "F"], ["Rectangle", "R"],
        ]);
        section("The image", [
            ["Pan (hold and drag)", "Space"],
            ["Zoom", "Wheel"],
        ]);
        section("Drawing", [
            ["Polygon: click to add a point", ""],
            ["Finish a polygon", "Enter"],
            ["Remove the last point", "\u232b"],
            ["Cancel, then deselect", "Esc"],
        ]);
        section("Regions", [
            ["Delete the selected region", "\u232b"],
            ["Rename (or double-click)", "F2"],
            ["Undo", `${mod}Z`],
            ["Redo", `${mod}\u21e7Z`],
            ["Collapse or expand a category", "\u2190 \u2192"],
        ]);

        const note = document.createElement("div");
        note.className = "roi-help-note";
        note.textContent = "Click a category to draw into it. New regions are "
            + "named after it and land under it.";
        card.append(note);
        return card;
    }

    /**
     * The new-category control: a button that becomes a field.
     *
     * At the top of the section rather than under the list it fills, because
     * making a category is the first thing to do in an empty project and the
     * thing a user goes looking for when they need another one -- and a field
     * standing open at the bottom of everything it creates is neither.
     */
    bindNewCategory() {
        const input = this.el("roi_category_name");
        this.el("roi_category_new")?.addEventListener("click", () => this.openNewCategory());
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
        input?.addEventListener("blur", () => {
            if (this._creating) return;
            if (input.value.trim()) this.submitNewCategory();
            else this.closeNewCategory();
        });
    }

    openNewCategory() {
        const row = this.el("roi_category_new_row");
        const input = this.el("roi_category_name");
        if (!row || !input) return;
        row.hidden = false;
        const button = this.el("roi_category_new");
        if (button) button.hidden = true;
        input.value = "";
        input.focus();
    }

    closeNewCategory() {
        const row = this.el("roi_category_new_row");
        if (row) row.hidden = true;
        const button = this.el("roi_category_new");
        if (button) button.hidden = false;
        const input = this.el("roi_category_name");
        if (input) input.value = "";
    }

    submitNewCategory() {
        const input = this.el("roi_category_name");
        if (!input) return;
        this._creating = true;
        // Refused rather than closed on a duplicate name: the text is still
        // in the field and still wrong, and closing would throw it away
        // without the user having agreed to that.
        if (this.createCategory(input.value)) this.closeNewCategory();
        else { input.focus(); input.select(); }
        this._creating = false;
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
            before[key] = key === "visible" ? feature.visible !== false : feature[key];
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
        for (const button of document.querySelectorAll("#roi_toolbar [data-tool]")) {
            const active = button.dataset.tool === this.tools.tool;
            button.classList.toggle("is-active", active);
            button.setAttribute("aria-pressed", active ? "true" : "false");
            // Select stays available whenever the pointer works at all; the
            // three that MAKE a shape also need a category to put it in.
            // Freehand is routinely both active and disabled -- it is the tool
            // in hand on a project with no category yet -- and reads as chosen
            // and waiting rather than as nothing at all. See roi.css.
            button.disabled = button.dataset.tool === "select"
                ? !this.tools.ready
                : !this.tools.canDraw;
        }
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
     * of the other. Its ? is hidden and shown with it for the same reason.
     * Five controls do not fit a 288px column at full width, so Import and
     * Export give up their labels when Map to cells is on the row.
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
        const info = this.el("roi_map_info");
        if (mapper) {
            const canMap = Boolean(this._destination
                && this._destination.hasTable) && anything;
            mapper.hidden = !canMap;
            // The ? goes with the button, not with the panel: an explanation of
            // a control that is not on screen is a control of its own.
            if (info) info.hidden = !canMap;
            // `?.` and not a bare call: the map-button probe drives this
            // method against a stub DOM that knows only the ids it asserts on.
            this.el("roi_actions")?.classList.toggle("is-compact", canMap);
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
                    const proceed = await window.PlexoraConfirm.fromText(
                        `Your cells already have a "${this.mapPrefix() || "rois"}" mapping.\n\n`
                        + "Replace it? The existing values in those two columns will be lost."
                        + (suggestion ? `\n\nCancel to save it as "${suggestion}" instead.` : ""),
                        { confirm: "Replace" });
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
     *  so the same project gets the same palette every time. Also what the
     *  colour picker on a category row offers, so recolouring one lands on the
     *  same ten a new one would have been given. */
    static nextColor(index) {
        return RoiSidebarController.PALETTE[index % RoiSidebarController.PALETTE.length];
    }
}


RoiSidebarController.PALETTE = [
    "#e05c5c", "#38bdf8", "#34d399", "#f3b845", "#c084fc",
    "#f472b6", "#22d3ee", "#a3e635", "#fb923c", "#94a3b8",
];


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
