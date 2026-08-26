/**
 * What can be done to a selection, declared once.
 *
 * The bar that floats over the selection and the right-click menu are two
 * surfaces onto ONE list. They were two hand-maintained lists, and they had
 * already drifted: the menu marked "Bring to front" disabled when nothing in
 * the selection could be reordered, while the bar offered it live on a text box
 * where `reorderZ` filtered annotations out and did nothing at all. Two lists
 * meant two answers to "can this be done", and only one of them was right.
 *
 * The rule the reference designs encode, and the one used here:
 *
 *   * an action that only means something for one kind appears only for that
 *     kind -- "Split into channels" on a caption is not disabled, it is noise;
 *   * an action that applies to ANY object is always REACHABLE, but a generic
 *     one that cannot run right now is reachable through "More" rather than as
 *     a dead icon on the bar.
 *
 * That second half used to read "always present, and greyed when it cannot
 * run", and on a panel it was fine. On a text box it was not: Align, Distribute,
 * Match size, Layout and Group all need two objects, so selecting one caption
 * gave a bar of five grey icons and one live one, with everything actually
 * worth pressing hidden behind the overflow. A greyed icon with no label is not
 * an affordance; a greyed ROW that says "Align" is, because it names itself and
 * the menu has room to. So the action stays in the vocabulary either way and
 * only its surface moves -- which is still one declaration per action, with
 * nothing branching on the type of the selection.
 *
 * `applies` decides whether an action exists for this selection at all, and
 * `enabled` decides both whether it is clickable and, for a generic action,
 * which of the two surfaces it lands on.
 *
 * Text FORMATTING is deliberately not here. Font, size, colour and the rest
 * live in the text sidebar, because they are properties of one kind of object
 * rather than actions on any object -- and putting them here is what would turn
 * this back into a per-type list.
 */
class FigureSelection {

    /**
     * Everything the predicates below need, computed once per selection change.
     *
     * Pure: it reads the document and the canvas but changes nothing, so a
     * probe can build one from a plain object.
     */
    static describe(ids, state, canvas) {
        const annotations = ids
            .map((id) => state.document.annotations[id]).filter(Boolean);
        const panels = ids.map((id) => state.panel(id)).filter(Boolean);
        const single = ids.length === 1
            ? (panels[0] || annotations[0] || null) : null;
        const kinds = new Set(panels.map(() => "panel")
            .concat(annotations.map((annotation) => annotation.type)));

        return {
            ids: ids,
            count: ids.length,
            panels: panels,
            annotations: annotations,
            single: single,
            kinds: kinds,
            singlePanel: ids.length === 1 && panels.length === 1 ? panels[0] : null,
            singleAnnotation:
                ids.length === 1 && annotations.length === 1 ? annotations[0] : null,
            isText: annotations.length === ids.length && ids.length > 0
                    && annotations.every((a) => a.type === "text"),
            // A box, as opposed to a line or an arrow, whose geometry is a
            // vector and which therefore has no corners to rotate about.
            allBoxes: ids.length > 0 && annotations.length === ids.length
                      && annotations.every((a) => !FigureCanvas.isStrokeType(a.type)),
            grouped: ids.some((id) => canvas.groupFor(id)),
            placed: panels.filter((panel) => panel.placement),
            // How many of the selected objects have a RECTANGLE that Align,
            // Distribute, Match size and Layout can act on, which is not the
            // same as how many objects there are. `count > 1` was the test, and
            // it was wrong twice over: it counted a panel still in the tray,
            // which has no placement to move, and it counted a line, whose
            // `w_mm`/`h_mm` are the two components of a vector rather than a
            // size -- "same width" on one would flip its direction. Two
            // captions now enable these; two arrows do not, and say so by being
            // greyed in "More" rather than by doing nothing when pressed.
            arrangeable: panels.filter((panel) => panel.placement).length
                + annotations.filter(
                    (a) => !FigureCanvas.isStrokeType(a.type)).length,
        };
    }
}


class FigureActions {

    /**
     * The annotation types with no sidebar panel of their own.
     *
     * Not creatable any more -- the shape picker superseded both -- but every
     * figure drawn before it is full of them, and dropping a type from
     * `ANNOTATION_TYPES` deletes every annotation of it on the next read. So
     * they stay readable and editable forever, and the bar's Stroke and Color
     * popovers are the only way in to either.
     */
    static get LEGACY_BOXES() { return ["rect", "ellipse"]; }

    /**
     * The vocabulary. `surface` says where an action can appear:
     *
     *   bar       a button on the floating bar
     *   overflow  a row in the bar's "More" menu
     *   menu      a row in the right-click menu
     *
     * An action may name several. `popover: true` means the bar button opens a
     * popover whose body `FigureContextBar.popoverMarkup` builds -- the detail
     * of a scale bar or a legend is panel-specific and belongs there; what
     * belongs HERE is only whether the button exists at all.
     *
     * `label` is the full sentence a menu row can afford. `short` is what the
     * bar prints under the icon, and it is a separate field rather than a
     * truncation because "Title, label and numbering" cut to fit says nothing
     * and "Titles" says the whole thing. An action with no `short` prints its
     * `label`, which is right for the ones that are already one word.
     *
     * `shortcut` is a FIELD and not two spaces and a glyph on the end of the
     * label, which is how it was written first. A menu row sets the label and
     * the key in two columns -- the labels ranged left, the keys ranged right
     * and greyed -- and that is only possible if the row is given them
     * separately. Concatenated, the keys landed wherever each label happened to
     * end, which is a ragged column of the least important text on screen; and
     * the bar's tooltip read "Group  ⌘G" with the double space in it.
     */
    static get ALL() {
        const always = () => true;
        return [
            // -- type-specific, present only for what they mean something to --
            { id: "edit_text", icon: "i-cursor", label: "Edit the text",
              short: "Edit", surface: ["bar", "menu"],
              applies: (sel) => Boolean(sel.singleAnnotation)
                                && sel.singleAnnotation.type === "text",
              enabled: always,
              run: (ctx) => ctx.handlers.onEditText?.(ctx.ids[0]) },

            // The shape equivalent, and here rather than in the shape sidebar
            // for the same reason `edit_text` is here: ENTERING a mode is a
            // command about the selected object, which is what this registry
            // is. What the mode then offers -- add, delete, corner, smooth,
            // close -- is about the nodes selected inside one object, and that
            // is in the sidebar. Double-clicking a shape does this too; the bar
            // is what makes it findable without knowing that.
            { id: "editpoints", icon: "bezier-curve", label: "Edit points",
              short: "Points", surface: ["bar", "menu"],
              applies: (sel) => Boolean(sel.singleAnnotation)
                                && sel.singleAnnotation.type === "shape",
              enabled: always,
              run: (ctx) => ctx.handlers.onEditPoints?.(ctx.ids[0]) },

            // A character palette rather than a search box: the ones a figure
            // caption actually needs are two dozen, and a grid of two dozen is
            // faster to read than any field is to type into. It is on the BAR
            // and not only in the sidebar because the caret it inserts at is on
            // the canvas, and walking away to a panel to reach it is what loses
            // the caret.
            { id: "symbol", icon: "icons", label: "Insert a symbol",
              short: "Symbol", surface: ["bar"], popover: true,
              applies: (sel) => Boolean(sel.singleAnnotation)
                                && sel.singleAnnotation.type === "text",
              enabled: always },

            // A PANEL's own properties -- these two, the split, the title, the
            // scale bar and the legend -- moved into the image sidebar, the
            // same way text, shape and line properties did before them. What is
            // left on the floating bar is what applies to any object: arrange,
            // align, group, duplicate, delete.
            //
            // They stay in the right-click menu, and Quick Edit is still on
            // double-click. The bar sits ON the artwork, so a button there that
            // opened a popover covered the panel it was about, and only one of
            // them could be open at a time.
            { id: "quick_edit", icon: "sliders", label: "Quick Edit…",
              short: "Quick Edit", surface: ["menu"],
              applies: (sel) => Boolean(sel.singlePanel),
              enabled: (sel, ctx) => FigureActions.reopenable(sel.singlePanel, ctx),
              run: (ctx) => ctx.handlers.onQuickEdit?.(ctx.ids[0]) },

            { id: "edit", icon: "arrow-up-right-from-square",
              label: "Open in Main Viewer", short: "Viewer",
              surface: ["menu"],
              applies: (sel) => Boolean(sel.singlePanel),
              enabled: (sel, ctx) => FigureActions.reopenable(sel.singlePanel, ctx),
              run: (ctx) => ctx.handlers.onEditPanel?.(ctx.ids[0]) },

            // Rendering is the one property of a panel that is genuinely worth
            // copying between panels: eight crops of one slide have to agree
            // about what colour CD8 is and where its contrast sits, and setting
            // that eight times by hand is both slow and unreliable. Two rows
            // rather than one dialog, because they happen at different moments
            // -- copy from the panel you got right, then select the rest.
            { id: "copy_rendering", icon: "eye-dropper",
              label: "Copy rendering settings", short: "Copy rendering",
              surface: ["menu"],
              applies: (sel) => Boolean(sel.singlePanel)
                                && (sel.singlePanel.scene.channels || []).length > 0,
              enabled: (sel, ctx) => FigureActions.reopenable(sel.singlePanel, ctx),
              run: (ctx) => ctx.handlers.onCopyRendering?.(ctx.ids[0]) },

            { id: "apply_rendering", icon: "fill-drip",
              label: "Apply rendering settings", short: "Apply rendering",
              surface: ["menu"],
              applies: (sel) => sel.panels.length > 0,
              // Greyed rather than absent when nothing has been copied: a row
              // that appears only after a step the user has not taken yet is a
              // feature they cannot find in order to learn it.
              enabled: (sel, ctx) => Boolean(ctx.handlers.hasRenderClipboard?.()),
              run: (ctx) => ctx.handlers.onApplyRendering?.(
                  ctx.sel.panels.map((panel) => panel.panel_id)) },

            // Not for text, shapes or lines, which all have a sidebar panel
            // carrying these. What is left is the legacy `rect`/`ellipse` that
            // predate the shape tool -- objects with no panel of their own, for
            // which this popover is the only way in. Two controls for one
            // number, in two places, disagreeing about which is authoritative,
            // is what moving each of the three off this bar avoided.
            { id: "stroke", icon: "pen", label: "Line and fill", short: "Line",
              surface: ["bar"], popover: true,
              applies: (sel) => Boolean(sel.singleAnnotation)
                                && FigureActions.LEGACY_BOXES.includes(
                                    sel.singleAnnotation.type),
              enabled: always },

            { id: "colour", icon: "palette", label: "Color",
              surface: ["bar"], popover: true,
              // Same three exclusions, each for its own reason. A caption's
              // colour belongs with its font and its size; a shape has a fill
              // AND a stroke colour and one "Color" button cannot say which it
              // is setting; a line's colour sits beside the heads and the dash
              // that are the rest of the same decision.
              applies: (sel) => Boolean(sel.singleAnnotation)
                                && FigureActions.LEGACY_BOXES.includes(
                                    sel.singleAnnotation.type),
              enabled: always },

            // -- generic: on the bar when they can run, in "More" when not ----
            { id: "align", icon: "align-center", label: "Align",
              surface: ["bar"], popover: true, generic: true,
              applies: always, enabled: (sel) => sel.arrangeable > 1 },

            { id: "distribute", icon: "arrows-left-right", label: "Distribute",
              surface: ["bar"], popover: true, generic: true,
              applies: always, enabled: (sel) => sel.arrangeable > 2 },

            // `expand`, not `vector-square`. The second is a Font Awesome 5/6
            // name that FA 7 no longer ships, and an icon name the set does not
            // have renders as nothing at all -- so this was a button captioned
            // "Match" with a blank square above it. tests/test_tool_assets.py
            // now checks every name in this tree against the set.
            { id: "resize", icon: "expand", label: "Match size",
              short: "Match", surface: ["bar"], popover: true, generic: true,
              applies: always, enabled: (sel) => sel.arrangeable > 1 },

            // "Layout", not "Arrange". Arrange is z-order in every design tool
            // and in the reference this bar follows; the row/column/grid one
            // needed the other name. `data-arrange` stays as it is -- it is the
            // contract FigureCanvas answers to, and this is a label change.
            { id: "layout", icon: "table-cells", label: "Layout",
              surface: ["bar"], popover: true, generic: true,
              applies: always, enabled: (sel) => sel.arrangeable > 1 },

            // Z-order as one popover rather than four overflow rows. "Arrange"
            // means z-order in every design tool, which is why the row/column
            // /grid action above had to take the name "Layout" -- and it is the
            // one generic action that is live on a selection of ONE, which is
            // what makes it worth a button rather than a menu row.
            { id: "arrange", icon: "layer-group", label: "Arrange",
              surface: ["bar"], popover: true, generic: true,
              applies: always,
              enabled: (sel) => sel.placed.length > 0 || sel.annotations.length > 0 },

            // Position and size as numbers, for the times the pointer cannot
            // get there: a 0.5 mm nudge at 40% zoom, or two captions that have
            // to be exactly the same width as each other.
            { id: "transform", icon: "up-down-left-right", label: "Transform",
              surface: ["bar"], popover: true, generic: true,
              applies: always,
              // One object, and one that HAS a width and a height. A field
              // showing a single number for a selection of three is either
              // lying or blank, and a line's `w_mm`/`h_mm` are the two
              // components of a vector rather than a size -- a box to type a
              // width into would be a box that means something else.
              enabled: (sel) => sel.count === 1
                                && (Boolean(sel.singlePanel) || sel.allBoxes) },

            { id: "group", icon: "object-group", label: "Group", shortcut: "⌘G",
              short: "Group", surface: ["bar", "menu"], generic: true,
              applies: (sel) => !sel.grouped, enabled: (sel) => sel.count > 1,
              run: (ctx) => ctx.canvas.groupSelection() },

            { id: "ungroup", icon: "object-ungroup", label: "Ungroup", shortcut: "⇧⌘G",
              short: "Ungroup", surface: ["bar", "menu"], generic: true,
              applies: (sel) => sel.grouped, enabled: always,
              run: (ctx) => ctx.canvas.ungroupSelection() },

            // Duplicate and Delete earn a button for the same reason Arrange
            // does: they are the two things anyone does to a single object of
            // any kind, and they were the reason the overflow had to be opened
            // at all on a text selection.
            { id: "duplicate", icon: "clone", label: "Duplicate", shortcut: "⌘D",
              short: "Duplicate", surface: ["bar", "menu"],
              generic: true, applies: always, enabled: (sel) => sel.count > 0,
              run: (ctx) => ctx.canvas.duplicateSelection() },

            // -- generic, in the overflow and the right-click menu ------------
            { id: "copy", icon: "copy", label: "Copy", shortcut: "⌘C",
              surface: ["overflow", "menu"],
              generic: true, applies: always, enabled: (sel) => sel.count > 0,
              run: (ctx) => ctx.canvas.copySelection() },

            // The four z-order commands, reached on the bar through the Arrange
            // popover -- which is why none of them is on it directly. One
            // command in two places on the same toolbar is how the bar and the
            // right-click menu ended up disagreeing before.
            //
            // Four rather than two: "to the front" and "one place forward" are
            // different intents, and getting a panel BETWEEN two others is only
            // possible with the relative pair.
            { id: "front", icon: "angles-up", label: "Bring to front", shortcut: "⌘⇧]",
              surface: ["menu"], generic: true, applies: always,
              // Enabled for annotations too, now that `reorderZ` reorders them.
              // This was the drift: the menu said no and the bar said yes, and
              // the bar was wrong.
              enabled: (sel) => sel.placed.length > 0 || sel.annotations.length > 0,
              run: (ctx) => ctx.canvas.reorderZ("front") },

            { id: "forward", icon: "angle-up", label: "Bring forward", shortcut: "⌘]",
              surface: ["menu"], generic: true, applies: always,
              enabled: (sel) => sel.placed.length > 0 || sel.annotations.length > 0,
              run: (ctx) => ctx.canvas.reorderZ("forward") },

            { id: "backward", icon: "angle-down", label: "Send backward", shortcut: "⌘[",
              surface: ["menu"], generic: true, applies: always,
              enabled: (sel) => sel.placed.length > 0 || sel.annotations.length > 0,
              run: (ctx) => ctx.canvas.reorderZ("backward") },

            { id: "back", icon: "angles-down", label: "Send to back", shortcut: "⌘⇧[",
              surface: ["menu"], generic: true, applies: always,
              enabled: (sel) => sel.placed.length > 0 || sel.annotations.length > 0,
              run: (ctx) => ctx.canvas.reorderZ("back") },

            // The tray, because that is where it goes: off the page and back
            // into the figure's holding area, which is the whole difference
            // between this row and Delete two rows down.
            { id: "remove_page", icon: "inbox", label: "Remove from page",
              surface: ["overflow", "menu"], generic: true,
              applies: (sel) => sel.panels.length > 0, enabled: always,
              run: (ctx) => ctx.handlers.onRemoveFromPage?.() },

            { id: "delete", icon: "trash", label: "Delete from figure",
              short: "Delete", shortcut: "⌫", danger: true,
              surface: ["bar", "menu"],
              generic: true, applies: always, enabled: (sel) => sel.count > 0,
              run: (ctx) => ctx.handlers.onDeleteFromFigure?.(ctx.ids) },
        ];
    }

    /** The four z-order commands, in the order they belong in a menu: nearest
     *  the viewer at the top. Named here rather than in the bar, so the popover
     *  and the right-click menu cannot end up listing different ones. */
    static get ARRANGE() {
        return ["front", "forward", "backward", "back"];
    }

    /** A panel can be reopened only if its source is a project that is still
     *  there -- an imported PNG has no viewer to go back to. */
    static reopenable(panel, ctx) {
        if (!panel) return false;
        const source = ctx.state.source(panel.source_id);
        return Boolean(source && source.kind === "plexora_project" && source.datasource);
    }

    /**
     * The actions one surface should show for this selection, in order.
     *
     * The bar and the overflow are one list split in two by whether a generic
     * action can currently run, so an action never falls out of reach: what the
     * bar drops, "More" picks up, named and greyed. Type-specific actions are
     * not moved -- they are on the bar because they are the point of having
     * selected that kind of object, and `applies` has already decided they
     * belong. The right-click menu takes everything its entries declare,
     * enabled or not, because there every row carries its own label.
     */
    static forSurface(surface, sel, ctx) {
        const available = FigureActions.ALL
            .filter((action) => action.applies(sel, ctx))
            .map((action) => ({
                ...action,
                isEnabled: Boolean(action.enabled(sel, ctx)),
                isPressed: Boolean(action.pressed && action.pressed(sel, ctx)),
            }));

        if (surface === "bar") {
            return available.filter((action) => action.surface.includes("bar")
                && (action.isEnabled || !action.generic));
        }
        if (surface === "overflow") {
            return available.filter((action) => action.surface.includes("overflow")
                || (action.surface.includes("bar") && action.generic
                    && !action.isEnabled));
        }
        return available.filter((action) => action.surface.includes(surface));
    }

    static byId(id) {
        return FigureActions.ALL.find((action) => action.id === id) || null;
    }
}
