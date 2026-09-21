/**
 * roiTree.js - the Category -> ROI list, and everything done to a row.
 *
 * The panel's middle, and the only part of it that is a hierarchy: categories
 * as collapsible groups, each region a row of its own under the one it belongs
 * to. Two things are visible at once and must never be confused, so they are
 * marked in different currencies -- the ACTIVE CATEGORY, which is where the
 * next stroke lands, carries a rule in its own colour and a pen; the SELECTED
 * REGION, which is what an edit acts on, is blue, the same blue everywhere.
 *
 * RECONCILED BY ID, never rebuilt. `RoiStore.setStatus()` calls `changed()`,
 * so the 400 ms autosave repaints this panel while the user is still typing:
 * a list that cleared itself would take the rename input out from under the
 * caret mid-word, drop the hover that revealed the row's buttons, and orphan
 * any menu portaled off a button that no longer exists. Rows are therefore
 * built once, kept in two Maps, updated in place, and moved only when they are
 * genuinely out of order -- see `place`, and the note on it about focus.
 *
 * Every piece of user text reaches the DOM through `textContent`. Category and
 * region names are user input, they survive a round trip through the store,
 * and they come back through an import from a file that may have been written
 * by anything at all.
 *
 * The owner supplies every mutation as a callback: this file decides what a
 * click MEANS and the controller decides what it DOES, because the doing is
 * commit/undo bookkeeping that belongs with the rest of it.
 */
class RoiTree {

    constructor(options = {}) {
        // No DOM in here: the controller is constructed before its panel is in
        // the document (the boot test builds one against a bare context), and
        // the list is resolved on the first render instead.
        this.store = options.store;
        this.colorPresets = options.colorPresets || null;
        this.onCategoryRename = options.onCategoryRename || (() => true);
        this.onCategoryUpdate = options.onCategoryUpdate || (() => {});
        this.onCategoryDelete = options.onCategoryDelete || (() => {});
        this.onFeatureRename = options.onFeatureRename || (() => {});
        this.onFeatureChange = options.onFeatureChange || (() => {});
        this.onFeatureDelete = options.onFeatureDelete || (() => {});
        //: Selecting a region highlights it on the IMAGE as well as here, and
        //: nothing else repaints the overlay for a selection made from the
        //: panel -- the renderer does not subscribe to the store, and the
        //: tools only schedule after their own canvas clicks.
        this.onSelect = options.onSelect || (() => {});

        this.list = null;
        this._bound = false;
        this.categoryRows = new Map();
        this.featureRows = new Map();
        //: Which categories are folded shut. Keyed by id and kept across
        //: renders, because a repaint is not a reason to unfold what somebody
        //: deliberately folded.
        this.collapsed = new Set();
        //: Every category this tree has already drawn once. The auto-collapse
        //: below is a first-sight decision only -- re-expanding a big category
        //: and then drawing in it must not fold it again on the next repaint.
        this.known = new Set();
        this._lastSelection = undefined;
        this._editing = null;
    }

    /** A category arriving with more regions than this comes in folded. An
     *  import of several hundred shapes is otherwise a wall of rows with the
     *  rest of the panel pushed off the bottom. */
    static get COLLAPSE_ABOVE() { return 50; }

    // -- rendering -------------------------------------------------------

    render() {
        if (!this.list) this.list = document.getElementById("roi_category_list");
        const list = this.list;
        if (!list) return;
        if (!this._bound) this.bindList();

        // A blocked or conflicted project can still be read and exported; it
        // just cannot be edited, and a row that looks pressable but is not is
        // worse than one that plainly is not.
        list.classList.toggle("is-inert", !this.store.editable);

        const selectionId = this.store.selectionId || null;
        const selectionMoved = selectionId !== this._lastSelection;
        this._lastSelection = selectionId;

        // Selecting a region inside a folded category -- by clicking it on the
        // image, or by drawing one -- opens the category holding it. Otherwise
        // the panel answers a click with nothing visibly happening.
        if (selectionMoved && selectionId) {
            const selected = this.store.feature(selectionId);
            if (selected) this.collapsed.delete(selected.category_id);
        }

        // The fallback matters: `activeCategory` is what createFrom draws into,
        // and with nothing explicitly chosen that is the first category -- so
        // the first category has to LOOK active, or the pen lands somewhere the
        // panel never pointed at.
        const activeId = this.store.activeCategory ? this.store.activeCategory.id : null;

        const liveCategories = new Set();
        const liveFeatures = new Set();
        let previous = null;

        for (const category of this.store.sortedCategories()) {
            liveCategories.add(category.id);
            let row = this.categoryRows.get(category.id);
            if (!row) {
                row = this.buildCategoryRow(category);
                this.categoryRows.set(category.id, row);
            }

            const members = this.store.features.filter((f) => f.category_id === category.id);
            if (!this.known.has(category.id)) {
                this.known.add(category.id);
                if (members.length > RoiTree.COLLAPSE_ABOVE) this.collapsed.add(category.id);
            }

            this.updateCategoryRow(row, category, members.length, category.id === activeId);
            RoiTree.place(list, row.el, previous);
            RoiTree.place(list, row.children, row.el);
            previous = row.children;

            const folded = this.collapsed.has(category.id);
            row.children.hidden = folded;
            row.el.classList.toggle("is-collapsed", folded);
            row.el.setAttribute("aria-expanded", folded ? "false" : "true");

            let previousFeature = null;
            for (const feature of members) {
                liveFeatures.add(feature.id);
                let frow = this.featureRows.get(feature.id);
                if (!frow) {
                    frow = this.buildFeatureRow(feature);
                    this.featureRows.set(feature.id, frow);
                }
                this.updateFeatureRow(frow, feature, category);
                // Also how a recategorised region moves: its row is asked for
                // under its new parent, and place() reparents it.
                RoiTree.place(row.children, frow.el, previousFeature);
                previousFeature = frow.el;
            }
        }

        for (const [id, frow] of this.featureRows) {
            if (liveFeatures.has(id)) continue;
            if (this._editing && this._editing.kind === "feature" && this._editing.id === id) {
                this._editing = null;
            }
            frow.el.remove();
            this.featureRows.delete(id);
        }
        for (const [id, row] of this.categoryRows) {
            if (liveCategories.has(id)) continue;
            if (this._editing && this._editing.kind === "category" && this._editing.id === id) {
                this._editing = null;
            }
            // The picker's popover lives in the portal, not in the row, so
            // removing the row would leave it on the page.
            row.picker?.destroy();
            row.el.remove();
            row.children.remove();
            this.categoryRows.delete(id);
            this.collapsed.delete(id);
            this.known.delete(id);
        }

        if (selectionMoved && selectionId) {
            const frow = this.featureRows.get(selectionId);
            frow?.el.scrollIntoView({ block: "nearest" });
        }
    }

    destroy() {
        RoiTree.closePopup();
        for (const row of this.categoryRows.values()) row.picker?.destroy();
        this.categoryRows.clear();
        this.featureRows.clear();
        this._editing = null;
    }

    /**
     * Put `node` after `previous` inside `parent` -- but only if it is not
     * already there.
     *
     * The guard is the whole point. `appendChild` on a node that is already in
     * position still MOVES it, and moving a subtree blurs whatever is focused
     * inside it: with an autosave repainting this list every 400 ms, an
     * unguarded reconcile takes the caret out of a rename on the first tick.
     */
    static place(parent, node, previous) {
        const expected = previous ? previous.nextSibling : parent.firstChild;
        if (node === expected) return;
        parent.insertBefore(node, expected);
    }

    // -- category rows ---------------------------------------------------

    buildCategoryRow(category) {
        const el = document.createElement("div");
        el.className = "roi-row roi-cat";
        el.setAttribute("role", "treeitem");
        el.tabIndex = 0;
        el.dataset.categoryId = category.id;

        // Both chevrons ship and CSS picks. Swapping a class in JS does not
        // work: FontAwesome's SVG mode has replaced these spans with <svg> by
        // the time anything is clicked, so there is no span left to rewrite.
        const chevron = document.createElement("button");
        chevron.type = "button";
        chevron.tabIndex = -1;
        chevron.className = "roi-cat-chevron";
        chevron.innerHTML = '<span class="fas fa-chevron-down"></span>'
            + '<span class="fas fa-chevron-right"></span>';

        const colorMount = document.createElement("span");
        colorMount.className = "roi-cat-color";

        const label = document.createElement("span");
        label.className = "roi-row-label roi-cat-label";

        const count = document.createElement("span");
        count.className = "roi-cat-count";

        const activeMark = document.createElement("span");
        activeMark.className = "roi-cat-active-mark";
        activeMark.title = "New ROIs are drawn here";
        activeMark.innerHTML = '<span class="fas fa-pen"></span>';

        const lock = document.createElement("span");
        lock.className = "roi-row-lock";
        lock.innerHTML = '<span class="fas fa-lock"></span>';

        const eye = RoiTree.rowAction("roi-row-eye",
            '<span class="fas fa-eye"></span><span class="fas fa-eye-slash"></span>');
        const more = RoiTree.rowAction("roi-row-more", '<span class="fas fa-ellipsis"></span>');
        more.title = "More";
        more.setAttribute("aria-haspopup", "true");
        more.setAttribute("aria-expanded", "false");

        el.append(chevron, colorMount, label, count, activeMark, lock, eye, more);

        const children = document.createElement("div");
        children.className = "roi-children";
        children.setAttribute("role", "group");

        const row = { el, children, chevron, colorMount, label, count, eye, more, picker: null };

        if (typeof ColorSwatchPicker !== "undefined") {
            // The dot IS the picker's button: one control, not a swatch that
            // opens something. The closure captures the ID, never the category
            // object -- an undo replaces the object, and a handler holding the
            // old one would write the colour back onto a ghost.
            const id = category.id;
            row.picker = new ColorSwatchPicker(colorMount, {
                value: category.color,
                presets: this.colorPresets || undefined,
                title: "Category colour",
                onChange: (hex) => this.onCategoryUpdate(id, { color: hex }),
            });
        } else {
            const dot = document.createElement("span");
            dot.className = "roi-cat-dot";
            colorMount.append(dot);
        }
        return row;
    }

    updateCategoryRow(row, category, count, active) {
        row.el.style.setProperty("--roi-cat-color", category.color);
        if (!row.el.classList.contains("is-editing")) {
            row.label.textContent = category.label;
            row.label.title = `Draw in ${category.label}`;
        }
        row.count.textContent = String(count);

        const hidden = category.visible === false;
        row.el.classList.toggle("is-active", Boolean(active));
        row.el.classList.toggle("is-hidden", hidden);
        row.el.classList.toggle("is-locked", Boolean(category.locked));
        row.el.classList.toggle("is-empty", count === 0);

        row.eye.title = hidden ? "Show this category" : "Hide this category";
        if (row.picker && row.picker.value !== category.color) row.picker.setValue(category.color);
    }

    // -- region rows -----------------------------------------------------

    buildFeatureRow(feature) {
        const el = document.createElement("div");
        el.className = "roi-row roi-item";
        el.setAttribute("role", "treeitem");
        el.tabIndex = 0;
        el.dataset.roiId = feature.id;

        const dot = document.createElement("span");
        dot.className = "roi-item-dot";
        dot.setAttribute("aria-hidden", "true");

        const label = document.createElement("span");
        label.className = "roi-row-label roi-item-label";

        const lock = document.createElement("span");
        lock.className = "roi-row-lock";
        lock.innerHTML = '<span class="fas fa-lock"></span>';

        const eye = RoiTree.rowAction("roi-row-eye",
            '<span class="fas fa-eye"></span><span class="fas fa-eye-slash"></span>');
        const more = RoiTree.rowAction("roi-row-more", '<span class="fas fa-ellipsis"></span>');
        more.title = "More";
        more.setAttribute("aria-haspopup", "true");
        more.setAttribute("aria-expanded", "false");

        el.append(dot, label, lock, eye, more);
        return { el, dot, label, lock, eye, more };
    }

    updateFeatureRow(row, feature, category) {
        const selected = feature.id === this.store.selectionId;
        row.el.classList.toggle("is-selected", selected);
        row.el.setAttribute("aria-selected", selected ? "true" : "false");

        const ownHidden = feature.visible === false;
        // The eye glyph follows the region's OWN flag; the struck-through row
        // follows the effective one. A region left shown inside a category that
        // is hidden reads as "shown, but its category is off", which is what it
        // is -- turning its own eye back on would do nothing.
        row.el.dataset.ownHidden = ownHidden ? "true" : "false";
        row.el.classList.toggle("is-hidden", !this.store.isVisible(feature));

        const locked = this.store.isLocked(feature);
        row.el.classList.toggle("is-locked", locked);
        row.el.classList.toggle("is-locked-inherited", locked && !feature.locked);

        const name = feature.name || "Unnamed";
        if (!row.el.classList.contains("is-editing")) {
            row.label.textContent = name;
            // The notes the old SELECTED block carried, now on the row itself:
            // they are facts about one region, and there is one row per region.
            const parts = [name];
            if (locked && !feature.locked) parts.push("locked by its category");
            if (typeof RoiGeometry !== "undefined"
                && !RoiGeometry.isVertexEditable(feature.geometry)) {
                parts.push("imported shape: vertices cannot be edited");
            }
            if (feature.flags && feature.flags.self_intersecting) {
                parts.push("outline crosses itself");
            }
            row.label.title = parts.join(" — ");
        }

        row.eye.title = ownHidden
            ? (category && category.visible === false
                ? "Show (its category is hidden)" : "Show this ROI")
            : "Hide this ROI";
    }

    static rowAction(className, glyphs) {
        const button = document.createElement("button");
        button.type = "button";
        button.tabIndex = -1;
        button.className = `roi-row-action ${className}`;
        button.innerHTML = glyphs;
        return button;
    }

    // -- interaction -----------------------------------------------------

    /** One delegated listener per event for the whole tree, bound once. Rows
     *  come and go; these do not, so nothing has to be unbound when they do. */
    bindList() {
        this._bound = true;

        this.list.addEventListener("click", (event) => {
            const row = event.target.closest(".roi-row");
            if (!row || !this.list.contains(row)) return;

            if (event.target.closest(".roi-cat-chevron")) {
                event.stopPropagation();
                this.toggleCollapse(row.dataset.categoryId);
                return;
            }
            if (event.target.closest(".roi-row-eye")) {
                event.stopPropagation();
                this.toggleEye(row);
                return;
            }
            const more = event.target.closest(".roi-row-more");
            if (more) {
                event.stopPropagation();
                this.openMenuFor(row, more);
                return;
            }
            // The picker owns its own click, and a click in the rename field
            // is a click in a text box -- neither is a click on the row.
            if (event.target.closest(".roi-cat-color, .roi-rename")) return;
            this.activateRow(row);
        });

        this.list.addEventListener("dblclick", (event) => {
            const label = event.target.closest(".roi-row-label");
            if (!label) return;
            const row = label.closest(".roi-row");
            if (!row) return;
            event.preventDefault();
            if (row.dataset.categoryId) this.startRename("category", row.dataset.categoryId);
            else this.startRename("feature", row.dataset.roiId);
        });

        this.list.addEventListener("keydown", (event) => {
            // The row itself only. A keystroke inside the rename input is the
            // input's, and it has its own handler.
            const row = event.target;
            if (!row.classList || !row.classList.contains("roi-row")) return;

            if (event.key === "Enter" || event.key === " ") {
                // Both are the tools': Space pans the image and Enter finishes
                // a polygon, and both listen on the document.
                event.preventDefault();
                event.stopPropagation();
                this.activateRow(row);
                return;
            }
            if (event.key === "F2") {
                event.preventDefault();
                if (row.dataset.categoryId) this.startRename("category", row.dataset.categoryId);
                else this.startRename("feature", row.dataset.roiId);
                return;
            }
            const id = row.dataset.categoryId;
            if (!id) return;
            if (event.key === "ArrowLeft" && !this.collapsed.has(id)) {
                event.preventDefault();
                this.toggleCollapse(id);
            } else if (event.key === "ArrowRight" && this.collapsed.has(id)) {
                event.preventDefault();
                this.toggleCollapse(id);
            }
        });
    }

    /** What a plain click on a row does, and the whole of it. A category
     *  becomes the one being drawn into; a region becomes the selection. The
     *  tool is not touched either way -- picking a region to rename is not a
     *  request to stop drawing. */
    activateRow(row) {
        if (!this.store.editable) return;
        if (row.dataset.categoryId) {
            this.store.setActiveCategory(row.dataset.categoryId);
        } else if (row.dataset.roiId) {
            this.store.select(row.dataset.roiId);
            this.onSelect(row.dataset.roiId);
        }
    }

    toggleCollapse(id) {
        if (!id) return;
        if (this.collapsed.has(id)) this.collapsed.delete(id);
        else this.collapsed.add(id);
        this.render();
    }

    toggleEye(row) {
        if (row.dataset.categoryId) {
            const category = this.store.category(row.dataset.categoryId);
            if (!category) return;
            this.onCategoryUpdate(category.id, { visible: category.visible === false });
            return;
        }
        const feature = this.store.feature(row.dataset.roiId);
        if (!feature) return;
        this.onFeatureChange(feature, { visible: feature.visible === false });
    }

    openMenuFor(row, anchor) {
        // Off the button's own aria-expanded, the same way the toolbar's ?
        // does it: the row click handler stops propagation, so the document
        // listener that dismisses a menu never sees a second click on the
        // dots -- without this the menu would close and reopen in the one
        // gesture and look stuck open.
        if (anchor.getAttribute("aria-expanded") === "true") {
            RoiTree.closePopup();
            return;
        }
        if (row.dataset.categoryId) {
            const category = this.store.category(row.dataset.categoryId);
            if (category) this.openCategoryMenu(anchor, category);
            return;
        }
        const feature = this.store.feature(row.dataset.roiId);
        if (feature) this.openFeatureMenu(anchor, feature);
    }

    // -- renaming in place -----------------------------------------------

    startRename(kind, id) {
        if (!id || !this.store.editable) return;
        // Only one field at a time, and the one being abandoned keeps what was
        // typed into it -- the same thing a blur would do.
        if (this._editing) this.finishRename(true);

        const row = kind === "category" ? this.categoryRows.get(id) : this.featureRows.get(id);
        if (!row) return;
        const current = kind === "category"
            ? (this.store.category(id)?.label || "")
            : (this.store.feature(id)?.name || "");

        const input = document.createElement("input");
        input.type = "text";
        input.className = "roi-input roi-rename";
        input.maxLength = 200;
        input.autocomplete = "off";
        input.spellcheck = false;
        input.value = current;

        input.addEventListener("click", (event) => event.stopPropagation());
        input.addEventListener("dblclick", (event) => event.stopPropagation());
        input.addEventListener("keydown", (event) => {
            event.stopPropagation();   // V/P/F/R are tools while this is not open
            if (event.key === "Enter") {
                event.preventDefault();
                this.finishRename(true);
            } else if (event.key === "Escape") {
                // Both, so Escape here does not travel on to the tools and
                // deselect the region the user is in the middle of naming.
                event.preventDefault();
                event.stopPropagation();
                this.finishRename(false);
            }
        });
        input.addEventListener("blur", () => this.finishRename(true));

        row.el.classList.add("is-editing");
        row.label.after(input);
        this._editing = { kind, id, input, row, done: false };
        input.focus();
        input.select();
    }

    finishRename(commit) {
        const editing = this._editing;
        // Idempotent: Enter commits and then blurs, and the blur arrives here
        // second.
        if (!editing || editing.done) return;
        editing.done = true;
        this._editing = null;

        const value = editing.input.value.trim();
        // Cleared BEFORE the commit, so the render that the commit triggers
        // writes the new label into a row that is no longer in edit mode.
        editing.row.el.classList.remove("is-editing");
        editing.input.remove();
        editing.row.el.focus();

        if (commit && value) {
            if (editing.kind === "category") {
                // Rejected on a clash: the controller says so and the render
                // below puts the old label back.
                this.onCategoryRename(editing.id, value);
            } else {
                const feature = this.store.feature(editing.id);
                if (feature && value !== (feature.name || "")) {
                    this.onFeatureRename(feature, value);
                }
            }
        }
        this.render();
    }

    // -- menus -----------------------------------------------------------

    openCategoryMenu(anchor, category) {
        const id = category.id;
        const count = this.store.countFor(id);
        const editable = this.store.editable;
        const items = [
            {
                label: "Rename",
                disabled: !editable,
                onSelect: () => this.startRename("category", id),
            },
            {
                label: "Change colour…",
                disabled: !editable || !this.categoryRows.get(id)?.picker,
                // Next tick: this runs from inside a click, and the picker
                // closes itself on any document click it did not stop -- so
                // opening it now would open and shut it in the same gesture.
                onSelect: () => window.setTimeout(
                    () => this.categoryRows.get(id)?.picker?.open(), 0),
            },
            {
                label: category.visible === false ? "Show all ROIs" : "Hide all ROIs",
                onSelect: () => this.onCategoryUpdate(id, { visible: category.visible === false }),
            },
            {
                label: category.locked ? "Unlock all ROIs" : "Lock all ROIs",
                disabled: !editable,
                onSelect: () => this.onCategoryUpdate(id, { locked: !category.locked }),
            },
            {
                label: count > 0 ? `Delete category and ${count} ROI${count === 1 ? "" : "s"}…`
                    : "Delete category",
                className: "is-sectioned is-destructive",
                disabled: !editable,
                onSelect: () => this.onCategoryDelete(id),
            },
        ];
        RoiTree.menu(anchor, items);
    }

    openFeatureMenu(anchor, feature) {
        const editable = this.store.editable;
        const locked = this.store.isLocked(feature);
        const hidden = feature.visible === false;
        // Every handler looks the region up again by id rather than closing
        // over the object the labels were written from. A menu stays open
        // across a repaint, and an undo taken while it is open replaces the
        // object -- acting on the one captured here would edit an orphan.
        const id = feature.id;
        const act = (changes) => {
            const live = this.store.feature(id);
            if (live) this.onFeatureChange(live, changes);
        };
        const items = [
            {
                label: "Rename",
                disabled: !editable,
                onSelect: () => this.startRename("feature", id),
            },
            {
                label: feature.locked ? "Unlock" : "Lock",
                disabled: !editable,
                onSelect: () => act({ locked: !feature.locked }),
            },
            {
                label: hidden ? "Show" : "Hide",
                onSelect: () => act({ visible: hidden }),
            },
        ];

        // Moving a region is a one-click choice here rather than a dropdown,
        // because the list of destinations is the category list and it is
        // usually two or three long.
        let first = true;
        for (const category of this.store.sortedCategories()) {
            if (category.id === feature.category_id) continue;
            items.push({
                label: `Move to "${category.label}"`,
                className: first ? "is-sectioned" : "",
                disabled: !editable,
                onSelect: () => act({ category_id: category.id }),
            });
            first = false;
        }

        items.push({
            label: locked ? "Delete (locked)" : "Delete",
            className: "is-sectioned is-destructive",
            disabled: !editable || locked,
            onSelect: () => this.onFeatureDelete(this.store.feature(id)),
        });
        RoiTree.menu(anchor, items);
    }

    /**
     * Float `el` under `anchor` and keep it there until something dismisses it.
     *
     * There is no core primitive for this -- `PopoverPortal` re-parents, it
     * does not position -- so this is the transcripts panel's menu, generalised
     * to the two things this panel floats. Fixed rather than absolute: the tree
     * scrolls, and an absolutely placed menu would scroll out of its own anchor.
     */
    static popup(anchor, el, options = {}) {
        RoiTree.closePopup();
        el.classList.add("roi-popup");
        if (typeof PopoverPortal !== "undefined") PopoverPortal.attach(el);
        else document.body.appendChild(el);

        const box = anchor.getBoundingClientRect();
        el.style.position = "fixed";
        el.style.top = `${box.bottom + 4}px`;
        const width = el.offsetWidth || 200;
        const left = options.align === "left" ? box.left : box.right - width;
        el.style.left = `${Math.max(8, Math.min(left, window.innerWidth - width - 8))}px`;
        // Flipped above when there is no room below: this panel is in a sidebar
        // that runs the height of the window, and a menu on its last row would
        // otherwise open off the bottom of the screen.
        const height = el.offsetHeight || 0;
        if (box.bottom + 4 + height > window.innerHeight - 8 && box.top - 4 - height > 8) {
            el.style.top = `${box.top - 4 - height}px`;
        }
        anchor.setAttribute("aria-expanded", "true");

        const close = () => {
            if (typeof PopoverPortal !== "undefined") PopoverPortal.detach(el);
            else el.remove();
            document.removeEventListener("click", close);
            document.removeEventListener("keydown", onKey, true);
            document.removeEventListener("scroll", onScroll, true);
            window.removeEventListener("resize", close);
            anchor.setAttribute("aria-expanded", "false");
            if (RoiTree._popup && RoiTree._popup.el === el) RoiTree._popup = null;
        };
        const onKey = (event) => {
            // Capture: the tools' own Escape handler is on the document too,
            // and it deselects. Shutting a menu should not also throw away
            // what the menu was about to act on.
            if (event.key !== "Escape") return;
            event.stopPropagation();
            close();
        };
        const onScroll = (event) => { if (!el.contains(event.target)) close(); };

        // Next tick, or the click that opened this would immediately shut it.
        window.setTimeout(() => document.addEventListener("click", close), 0);
        document.addEventListener("keydown", onKey, true);
        document.addEventListener("scroll", onScroll, true);
        window.addEventListener("resize", close);

        RoiTree._popup = { el, close };
        return close;
    }

    static menu(anchor, items) {
        const menu = document.createElement("div");
        menu.className = "roi-menu";
        menu.setAttribute("role", "menu");
        menu.addEventListener("click", (event) => event.stopPropagation());

        for (const item of items) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = `roi-menu-item ${item.className || ""}`.trim();
            button.setAttribute("role", "menuitem");
            button.textContent = item.label;
            button.title = item.label;
            button.disabled = Boolean(item.disabled);
            button.addEventListener("click", () => {
                RoiTree.closePopup();
                item.onSelect?.();
            });
            menu.appendChild(button);
        }

        RoiTree.popup(anchor, menu, { align: "right" });
        menu.querySelector(".roi-menu-item:not([disabled])")?.focus();
        return menu;
    }

    /** One floating thing at a time, from anywhere in the plugin. */
    static closePopup() {
        RoiTree._popup?.close();
        RoiTree._popup = null;
    }
}

RoiTree._popup = null;
