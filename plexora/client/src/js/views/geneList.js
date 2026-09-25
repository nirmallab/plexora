/**
 * geneList.js - the selected-genes tree, its groups, and the search behind it.
 *
 * CORE'S, AND NOT A PLUGIN'S, because two plugins draw it: the Transcripts
 * layer (a Xenium run's molecules) and the Visium HD bin layer. It was the
 * Transcripts panel's first, modelled on Xenium Explorer -- a search box that
 * ADDS, a tree of what is selected with an eye, a colour and a count on every
 * row, and groups the user makes -- and the Visium HD panel grew a second,
 * flatter copy of the same idea. Two copies of one interaction drift apart
 * one fix at a time; one copy cannot. Neither plugin can read the other's
 * globals (they exist only on a page where that plugin's section mounted), so
 * the shared part lives here, loaded from base.html before any plugin script.
 *
 * Three things, each usable alone:
 *
 *   PlexoraGeneGroups      the bookkeeping: groups, folds and removals over a
 *                          layer's `state`. Pure functions of arrays, which
 *                          is what lets the node probes check them bare.
 *   PlexoraGeneTree        the tree itself: rows, group headings, the whole-
 *                          list eye and fold, and the list's menu.
 *   PlexoraGeneVocabulary  a search over a vocabulary too long to list --
 *                          eighteen thousand genes on a Visium HD run.
 *
 * WHAT A LAYER HAS TO OFFER THE TREE is the same short list of methods both
 * layers already had under the same names: `state` (`selected`, `hidden`,
 * `groups`, `collapsed`, `colors`), `countOf`, `isHidden`, `setGeneHidden`,
 * `allGenesHidden`, `setAllGenesHidden`, `colorFor`, `setColor`, `addGene`,
 * `removeGene`, `clearGenes`, `resetAppearance`, and the group methods, which
 * each layer delegates to PlexoraGeneGroups. The tree never reaches past
 * them, so it does not know whether a gene is drawn as molecules or squares.
 *
 * Deliberately NOT here: what a gene is DRAWN as (points, icons, a ramp) and
 * anything that reads the data. Those are the plugins'.
 */
class PlexoraGeneGroups {

    /** `extra` is a plugin's own fields for the group (the Visium HD
     *  composition's aggregation), written beside the name and the genes. */
    static create(state, name, extra = {}) {
        const label = String(name || "").trim();
        if (!label || state.groups.some((group) => group.name === label)) return false;
        state.groups = [...state.groups, { ...extra, name: label, genes: [] }];
        return true;
    }

    static rename(state, from, to) {
        const label = String(to || "").trim();
        if (!label) return false;
        state.groups = state.groups.map(
            (group) => (group.name === from ? { ...group, name: label } : group));
        return true;
    }

    static remove(state, name) {
        state.groups = state.groups.filter((group) => group.name !== name);
        // Or a group made again under the same name would come back rolled
        // up, from a set nothing on the page has pointed at since.
        PlexoraGeneGroups.setCollapsed(state, name, false);
    }

    /** Move a gene into a group, or out of every group when `name` is null. */
    static assign(state, gene, name) {
        state.groups = state.groups.map((group) => ({
            ...group,
            genes: group.name === name
                ? [...new Set([...group.genes, gene])]
                : group.genes.filter((member) => member !== gene),
        }));
    }

    /** A gene no longer selected leaves every group with it: a group counting
     *  a gene nobody can see is a count nobody can explain. */
    static dropGene(state, gene) {
        state.groups = state.groups.map((group) => ({
            ...group, genes: group.genes.filter((member) => member !== gene),
        }));
    }

    /** Every group emptied and kept: clearing the genes is not deleting the
     *  user's organisation of them. */
    static empty(state) {
        state.groups = state.groups.map((group) => ({ ...group, genes: [] }));
    }

    /** Selected genes that are in no group -- the tree's top level. */
    static ungrouped(state) {
        const claimed = new Set(state.groups.flatMap((group) => group.genes));
        return state.selected.filter((gene) => !claimed.has(gene));
    }

    static isCollapsed(state, name) { return state.collapsed.includes(name); }

    static setCollapsed(state, name, collapsed) {
        const without = state.collapsed.filter((entry) => entry !== name);
        state.collapsed = collapsed ? [...without, name] : without;
    }

    /** Every group rolled up, or every one open. */
    static collapseAll(state, collapsed) {
        state.collapsed = collapsed ? state.groups.map((group) => group.name) : [];
    }

    /** True while there is a group and none of them is open. */
    static allCollapsed(state) {
        return state.groups.length > 0
            && state.groups.every((group) => state.collapsed.includes(group.name));
    }

    /**
     * Saved groups brought into line with a vocabulary.
     *
     * A gene this layer has not got leaves its group, and a group left with
     * no genes goes -- a partly-matching group keeps the part that matches,
     * because the grouping is the user's own organisation and half of it is
     * still useful. A group name that is not a string is dropped rather than
     * drawn as "[object Object]". Any other field on a group is a plugin's
     * and rides through untouched; the plugin validates its own.
     */
    static normalize(state, known = () => true) {
        const seen = new Set();
        state.groups = (Array.isArray(state.groups) ? state.groups : [])
            .filter((group) => group && typeof group.name === "string"
                     && group.name.trim() && !seen.has(group.name)
                     && seen.add(group.name))
            .map((group) => ({
                ...group,
                name: group.name,
                genes: [...new Set((group.genes || []).filter(
                    (gene) => known(gene) && state.selected.includes(gene)))],
            }));
        const names = new Set(state.groups.map((group) => group.name));
        state.collapsed = (Array.isArray(state.collapsed) ? state.collapsed : [])
            .filter((name) => names.has(name));
    }
}


class PlexoraGeneTree {

    /**
     * @param container         the element the rows go into; emptied on paint
     * @param options.layer     the model (see the header for its methods)
     * @param options.classes   { eye, collapse } extra classes, kept for a
     *                          plugin whose own rules still style them
     * @param options.count     gene -> { text, title }, for the number at the
     *                          end of a row. Plugins count different things:
     *                          molecules, or UMIs.
     * @param options.rowExtras gene -> [nodes] put between the swatch and the
     *                          name (the Transcripts icon button)
     * @param options.groupExtras group -> [nodes] put between a group's name
     *                          and its delete button (the Visium HD
     *                          composition's aggregation button)
     * @param options.onChange  kind -> void, after the tree changed the layer.
     *                          `kind` is "color" for a colour pick, which must
     *                          NOT repaint the tree -- the picker that made it
     *                          is still open in it -- and "list" for anything
     *                          that changed which rows exist or how they read.
     */
    constructor(container, options = {}) {
        this.container = container;
        this.options = options;
        this.pickers = [];
        this.eye = null;
        this.fold = null;
    }

    get layer() { return this.options.layer; }

    changed(kind) { this.options.onChange?.(kind); }

    /**
     * One row per selected gene, under its group.
     *
     * Rebuilt rather than patched: a row owns no state of its own -- the
     * layer holds all of it -- which is what makes this safe to call on every
     * change. The scroll position is carried across the rebuild, because the
     * tree is a bounded scroller and replacing its rows resets scrollTop:
     * hiding a gene thirty rows down would otherwise throw the list back to
     * the top and scroll the eye that was just clicked out from under the
     * pointer. The browser clamps it for a shorter list.
     */
    paint() {
        const tree = this.container;
        const layer = this.layer;
        if (!tree || !layer) return;
        const scrollTop = tree.scrollTop;
        this.destroyPickers();
        tree.replaceChildren();
        for (const group of layer.state.groups) tree.appendChild(this.buildGroup(group));
        for (const gene of layer.ungrouped()) tree.appendChild(this.buildRow(gene));
        tree.scrollTop = scrollTop;
        this.paintListActions();
    }

    destroyPickers() {
        for (const picker of this.pickers) picker.destroy?.();
        this.pickers = [];
    }

    destroy() {
        this.destroyPickers();
        this.container?.replaceChildren?.();
    }

    /** Two glyphs and a class, never a glyph swap: FontAwesome rewrites every
     *  icon span into an svg before anything can click it, so a JS swap edits
     *  a node that is no longer on the page. Same rule cardList.js states. */
    static eyeButton(off, title, extra = "") {
        const eye = document.createElement("button");
        eye.type = "button";
        eye.className = `gene-eye ${extra}`.trim();
        eye.classList.toggle("is-off", Boolean(off));
        eye.title = title;
        eye.setAttribute("aria-label", title);
        eye.innerHTML = '<span class="fas fa-eye"></span><span class="fas fa-eye-slash"></span>';
        return eye;
    }

    static removeButton(title) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "gene-remove";
        remove.title = title;
        remove.setAttribute("aria-label", title);
        remove.innerHTML = '<span class="fas fa-xmark"></span>';
        return remove;
    }

    buildGroup(group) {
        const layer = this.layer;
        const box = document.createElement("div");
        box.className = "gene-group";
        //: Read by a plugin's hover delegation: the pointer on a group's
        //: heading means every gene in it, the pointer on one of its rows
        //: means only that gene (the row's own `data-gene` is the closer
        //: match).
        box.setAttribute("data-group", group.name);

        const heading = document.createElement("div");
        heading.className = "gene-group-heading";

        const collapsed = layer.isCollapsed(group.name);
        const fold = document.createElement("button");
        fold.type = "button";
        fold.className = "gene-collapse";
        fold.classList.toggle("is-collapsed", collapsed);
        fold.title = collapsed ? `Open ${group.name}` : `Roll up ${group.name}`;
        fold.setAttribute("aria-label", fold.title);
        fold.setAttribute("aria-expanded", String(!collapsed));
        fold.innerHTML = '<span class="fas fa-chevron-down"></span>'
            + '<span class="fas fa-chevron-right"></span>';
        const toggle = () => {
            layer.setGroupCollapsed(group.name, !collapsed);
            this.changed("list");
        };
        fold.addEventListener("click", toggle);
        heading.appendChild(fold);

        const allHidden = group.genes.length > 0
            && group.genes.every((gene) => layer.isHidden(gene));
        const eye = PlexoraGeneTree.eyeButton(
            allHidden, allHidden ? `Show ${group.name}` : `Hide ${group.name}`);
        eye.addEventListener("click", () => {
            for (const gene of group.genes) layer.setGeneHidden(gene, !allHidden);
            this.changed("list");
        });
        heading.appendChild(eye);

        const name = document.createElement("span");
        name.className = "gene-group-name";
        name.textContent = `${group.name} (${group.genes.length})`;
        name.title = group.name;
        // The whole name is the chevron's hit target as well. A 14-pixel
        // glyph is a small thing to ask somebody to hit for an action whose
        // label is sitting right beside it.
        name.addEventListener("click", toggle);
        heading.appendChild(name);

        for (const extra of this.options.groupExtras?.(group) || []) {
            if (extra) heading.appendChild(extra);
        }

        const remove = PlexoraGeneTree.removeButton(
            `Delete the group ${group.name}. Its genes stay selected.`);
        remove.addEventListener("click", () => {
            layer.deleteGroup(group.name);
            this.changed("list");
        });
        heading.appendChild(remove);
        box.appendChild(heading);

        // Not built at all when it is rolled up, rather than built and
        // hidden: the heading already carries the count, and a 480-gene
        // panel in eight groups is eight hundred rows of swatch pickers
        // nobody can see.
        if (!collapsed) {
            const list = document.createElement("div");
            list.className = "gene-group-genes";
            for (const gene of group.genes) list.appendChild(this.buildRow(gene));
            box.appendChild(list);
        }
        return box;
    }

    buildRow(gene) {
        const layer = this.layer;
        const row = document.createElement("div");
        row.className = "gene-row";
        row.setAttribute("data-gene", gene);

        const hidden = layer.isHidden(gene);
        const eye = PlexoraGeneTree.eyeButton(hidden, hidden ? `Show ${gene}` : `Hide ${gene}`);
        eye.addEventListener("click", () => {
            layer.setGeneHidden(gene, !layer.isHidden(gene));
            this.changed("list");
        });
        row.appendChild(eye);

        const swatch = document.createElement("span");
        swatch.className = "gene-swatch-mount";
        row.appendChild(swatch);
        if (typeof ColorSwatchPicker !== "undefined") {
            this.pickers.push(new ColorSwatchPicker(swatch, {
                value: layer.colorFor(gene),
                title: `Colour for ${gene}`,
                onChange: (color) => {
                    layer.setColor(gene, color);
                    this.changed("color");
                },
            }));
        }

        for (const extra of this.options.rowExtras?.(gene) || []) {
            if (extra) row.appendChild(extra);
        }

        const name = document.createElement("span");
        name.className = "gene-name";
        name.textContent = gene;
        name.title = gene;
        row.appendChild(name);

        const described = this.options.count?.(gene)
            || { text: (layer.countOf(gene) || 0).toLocaleString(), title: "" };
        const count = document.createElement("span");
        count.className = "gene-count";
        count.textContent = described.text;
        if (described.title) count.title = described.title;
        row.appendChild(count);

        if (layer.state.groups.length) {
            const move = document.createElement("select");
            move.className = "gene-group-select";
            move.title = `Which group ${gene} belongs to`;
            move.setAttribute("aria-label", move.title);
            const none = document.createElement("option");
            none.value = "";
            none.textContent = "—";
            move.appendChild(none);
            for (const group of layer.state.groups) {
                const option = document.createElement("option");
                option.value = group.name;
                option.textContent = group.name;
                option.selected = group.genes.includes(gene);
                move.appendChild(option);
            }
            move.addEventListener("change", () => {
                layer.assignToGroup(gene, move.value || null);
                this.changed("list");
            });
            row.appendChild(move);
        }

        const remove = PlexoraGeneTree.removeButton(`Stop drawing ${gene}`);
        remove.addEventListener("click", () => {
            layer.removeGene(gene);
            this.changed("list");
        });
        row.appendChild(remove);
        return row;
    }

    // -- the heading's two whole-list buttons ------------------------------------

    /**
     * Every eye, and every group, from the list's own heading.
     *
     * Both read their next state off the layer at click time rather than off
     * the class on the button, so a list changed from anywhere else -- a
     * group imported, a gene's own eye clicked -- cannot leave these two
     * arguing with what is under them.
     */
    bindListActions(eye, fold) {
        this.eye = eye || null;
        this.fold = fold || null;
        if (eye && !eye.dataset.geneBound) {
            eye.dataset.geneBound = "1";
            eye.addEventListener("click", () => {
                this.layer?.setAllGenesHidden(!this.layer.allGenesHidden());
                this.changed("list");
            });
        }
        if (fold && !fold.dataset.geneBound) {
            fold.dataset.geneBound = "1";
            fold.addEventListener("click", () => {
                this.layer?.collapseAll(!this.layer.allCollapsed());
                this.changed("list");
            });
        }
        this.paintListActions();
    }

    /**
     * Each button says what it will DO rather than what it is looking at --
     * "Hide every gene" while anything is on, "Show every gene" once they are
     * all off -- because a toggle in a heading has no room for a label.
     * Hidden rather than disabled when there is nothing to act on: a
     * permanently grey glyph is a thing the eye has to learn to skip.
     */
    paintListActions() {
        const layer = this.layer;
        if (!layer) return;
        if (this.eye) {
            const allHidden = layer.allGenesHidden();
            this.eye.hidden = layer.state.selected.length === 0;
            this.eye.classList.toggle("is-off", allHidden);
            const label = allHidden ? "Show every gene" : "Hide every gene";
            this.eye.title = label;
            this.eye.setAttribute("aria-label", label);
        }
        if (this.fold) {
            const allCollapsed = layer.allCollapsed();
            this.fold.hidden = layer.state.groups.length === 0;
            this.fold.classList.toggle("is-collapsed", allCollapsed);
            const label = allCollapsed ? "Open every gene group" : "Collapse every gene group";
            this.fold.title = label;
            this.fold.setAttribute("aria-label", label);
        }
    }

    // -- the list's menu ----------------------------------------------------------

    /**
     * What is done to the LIST, not to a row of it, from the button beside
     * the search box.
     *
     * ORDERED BY WHAT A MISS COSTS. The button is a plus sign, so a hand
     * arriving here meant to add something: the first item builds a group,
     * and the two that undo work sit under a rule with the one that empties
     * the list last. Three rows of equal weight hung off an additive button
     * is how "clear all genes" gets clicked by somebody who meant "create".
     *
     * @param options.onCreateGroups opens the group dialog
     * @param options.resetLabel     what "reset" means for this layer
     */
    openListMenu(anchor, { onCreateGroups, resetLabel = "Reset colours to default" } = {}) {
        const layer = this.layer;
        if (!anchor || typeof PlexoraMenu === "undefined") return null;
        return PlexoraMenu.open(anchor, [
            { label: "Create gene groups…", onSelect: () => onCreateGroups?.() },
            { separator: true },
            {
                label: resetLabel,
                disabled: !layer?.state.selected.length,
                onSelect: () => {
                    layer?.resetAppearance();
                    this.changed("list");
                },
            },
            {
                label: "Clear all genes",
                className: "is-destructive",
                disabled: !layer?.state.selected.length,
                onSelect: () => {
                    layer?.clearGenes();
                    this.changed("list");
                },
            },
        ]);
    }

    /**
     * Put a batch of groups into the tree.
     *
     * A gene named by a group is SELECTED by it, which is the whole point of
     * importing a marker list: a group of twelve genes that were not already
     * ticked would be an empty heading, and ticking them afterwards one by
     * one is the work the import was meant to save.
     */
    addGroups(groups) {
        const layer = this.layer;
        if (!layer) return;
        for (const group of groups || []) {
            layer.createGroup(group.name);
            for (const gene of group.genes || []) {
                layer.addGene(gene);
                layer.assignToGroup(gene, group.name);
            }
        }
        this.changed("list");
    }
}


class PlexoraGeneVocabulary {

    /** Names, their lower-case forms, and indices by descending count. */
    constructor(names = [], counts = []) {
        this.names = names;
        this.lower = names.map((name) => String(name).toLowerCase());
        this.order = names.map((_, index) => index)
            .sort((a, b) => (Number(counts[b]) || 0) - (Number(counts[a]) || 0));
    }

    static get LIMIT() { return 50; }

    /**
     * At most `limit` genes for a query: exact, then prefix, then substring,
     * each in order of abundance.
     *
     * An empty query lists the most abundant genes, which is a better first
     * screen than the first fifty alphabetically -- those are mostly
     * `A1BG`-style names nobody is looking for. One pass over the vocabulary
     * per keystroke, stopping once the prefixes alone fill the list. Handed
     * to SearchableSelect as its `match`, because that control renders every
     * option it is given, which is right for a forty-marker panel and a
     * stall for eighteen thousand genes.
     */
    match(query, limit = PlexoraGeneVocabulary.LIMIT) {
        const { names, lower, order } = this;
        const q = String(query || "").trim().toLowerCase();
        if (!q) return order.slice(0, limit).map((index) => names[index]);
        const exact = [];
        const prefix = [];
        const inside = [];
        for (const index of order) {
            const name = lower[index];
            if (name === q) exact.push(names[index]);
            else if (name.startsWith(q)) {
                prefix.push(names[index]);
                if (prefix.length >= limit) break;
            } else if (inside.length < limit && name.includes(q)) {
                inside.push(names[index]);
            }
        }
        return [...exact, ...prefix, ...inside].slice(0, limit);
    }
}

if (typeof window !== "undefined") {
    window.PlexoraGeneGroups = PlexoraGeneGroups;
    window.PlexoraGeneTree = PlexoraGeneTree;
    window.PlexoraGeneVocabulary = PlexoraGeneVocabulary;
}
if (typeof globalThis !== "undefined") {
    globalThis.PlexoraGeneGroups = PlexoraGeneGroups;
    globalThis.PlexoraGeneTree = PlexoraGeneTree;
    globalThis.PlexoraGeneVocabulary = PlexoraGeneVocabulary;
}
