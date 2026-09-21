/**
 * Making gene groups: name one here, or bring a file that already has them.
 *
 * WHY A DIALOG AND NOT THE ONE-LINE FORM IT REPLACES. Naming a group is one
 * field, and a dialog for one field is a dialog too many -- which is what the
 * inline form in the panel was, and it was right for as long as making a
 * group meant nothing but naming it. It is not what somebody actually has.
 * A panel is 300 to 500 genes and the groups that matter are somebody's
 * marker list: twelve rows of "this gene, these cell types", already written
 * down, usually in a spreadsheet. Retyping that through a one-field form
 * twelve times, then dragging forty genes into place one dropdown at a time,
 * is the work this is here to not make anybody do.
 *
 * So two ways in, side by side, because they are two answers to one question
 * and neither is the advanced one:
 *
 *   here    a name and the genes that go in it, for the group somebody is
 *           thinking of right now
 *   a file  gene in the first column, every group it belongs to in the
 *           columns after -- the shape Xenium Explorer's own import takes,
 *           because that is what people already have
 *
 * NOTHING HERE PARSES THE FILE. It is posted to this plugin's own route and
 * read by core's spreadsheet reader (server/utils/channel_file.py), for the
 * reason `channelNamesUpload.js` gives at length: a CSV's delimiter has to be
 * sniffed and an .xlsx is a zip full of XML, and a browser that got either
 * subtly wrong would report a group with the WRONG genes in it rather than an
 * error. Which file, and which machine it is on, is core's `PlexoraFileSourceRow`
 * -- the same row and the same Local/Remote switch every other file field in
 * Plexora has, so a marker list on a cluster has a way in.
 *
 * A <dialog> opened with showModal(), like requirementsModal.js: a modal
 * dialog is promoted to the top layer, ABOVE a fullscreened viewer and its
 * opaque ::backdrop, which an ordinary positioned element on <body> is not.
 * The same promotion is what used to hide the gene dropdown -- a popup parked
 * on <body> is under the top layer too -- which is fixed in PopoverPortal, not
 * here; there is nothing a dialog can do about it from the inside.
 *
 * HOW THE CHROME IS LAID OUT, because the first cut got it wrong twice over
 * and both mistakes are easy to make again:
 *
 *   ONE ACTION ROW, AT THE BOTTOM. There used to be two -- a "Create group"
 *   floating at the end of the pane and a "Done" under a rule beneath it --
 *   so the dialog asked twice, in two different weights, which of them
 *   finished the job. The footer now carries both, and the primary is
 *   whatever the open tab means by "do it": its word and its enabled state
 *   come from the pane. `Done` never leaves.
 *
 *   NO BOOTSTRAP CLASSES. Bootstrap is in the vendor bundle and its
 *   `.form-control` and `.btn-primary` are a white field and a #0d6efd
 *   button, which is what this dialog looked like: a white box in the middle
 *   of an application that has no white anywhere else. Every control here
 *   carries a class of this plugin's own. The one exception is the shared
 *   file-source row, which is core's markup and is toned down by scoped
 *   overrides in transcripts.css instead.
 */
class TranscriptGroupModal {

    /**
     * @param options.api      - TranscriptsApi, for the CSV read
     * @param options.layerId  - which transcript layer the panel is showing
     * @param options.genes    - the panel's whole vocabulary
     * @param options.existing - group names already made, so a clash is
     *                           caught in the field rather than on submit
     * @param options.onApply  - `([{name, genes}]) => void`. Called once per
     *                           accepted batch; the dialog stays open, so a
     *                           second group is a second Create and not a
     *                           second trip through the kebab.
     */
    static open(options) {
        return new TranscriptGroupModal(options).show();
    }

    constructor({ api, layerId, genes = [], existing = [], onApply = null } = {}) {
        this.api = api;
        this.layerId = layerId;
        this.genes = genes;
        this.existing = new Set(existing);
        this.onApply = onApply;
        this.dialog = null;
        this.select = null;
        //: Which pane is up. The footer's primary button reads this to know
        //: what it does, so it cannot be left out of sync with the tabs.
        this.pane = "here";
        //: The genes picked for the group being made here, in the order they
        //: were picked. An array and not a Set: the order is the order they
        //: will be drawn in, which is a decision the user is making.
        this.picked = [];
        //: What the last CSV read found, awaiting confirmation. Held rather
        //: than applied on arrival, because a file that turns out to name
        //: forty groups is something to look at before it lands in the tree.
        this.parsed = null;
    }

    static el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    static button(className, text, onClick) {
        const node = TranscriptGroupModal.el("button", className, text);
        node.type = "button";
        node.addEventListener("click", onClick);
        return node;
    }

    /** A Font Awesome glyph, hidden from a screen reader. */
    static icon(name) {
        const node = TranscriptGroupModal.el("i", `fa-solid ${name}`);
        node.setAttribute("aria-hidden", "true");
        return node;
    }

    // -- the shell ----------------------------------------------------------

    show() {
        const el = TranscriptGroupModal.el;
        const dialog = el("dialog", "transcripts-modal");
        dialog.setAttribute("aria-labelledby", "transcripts_group_modal_title");
        const form = el("form", "transcripts-modal-form");
        // method="dialog" so that if the browser ever does submit this form,
        // it closes the dialog rather than navigating the page away from the
        // viewer. Nothing here relies on that happening: every action is a
        // button, and Enter in the name field is bound by hand below.
        form.method = "dialog";

        form.appendChild(this.buildHead());
        form.appendChild(this.buildTabs());

        this.panes = {
            here: this.buildHerePane(),
            file: this.buildFilePane(),
        };
        const body = el("div", "transcripts-modal-body");
        body.append(this.panes.here, this.panes.file);
        form.appendChild(body);

        form.appendChild(this.buildFooter());

        dialog.appendChild(form);
        document.body.appendChild(dialog);
        this.dialog = dialog;
        dialog.addEventListener("close", () => this.destroy());
        dialog.showModal();
        this.showPane("here");
        return this;
    }

    buildHead() {
        const el = TranscriptGroupModal.el;
        const head = el("header", "transcripts-modal-head");
        const text = el("div", "transcripts-modal-heading");
        const title = el("h2", "transcripts-modal-title", "Create gene groups");
        title.id = "transcripts_group_modal_title";
        text.append(title, el("p", "transcripts-modal-subtitle",
            "A heading in the selected-genes tree, so a forty-gene view stays "
            + "readable and a whole cell type switches off at once."));
        // An X as well as the footer's Done, which is not the duplication it
        // looks like: Done is where somebody who has just made a group is
        // already looking, and the X is where somebody who opened this by
        // mistake looks first.
        const close = TranscriptGroupModal.button(
            "transcripts-modal-close", "", () => this.close());
        close.appendChild(TranscriptGroupModal.icon("fa-xmark"));
        close.setAttribute("aria-label", "Close");
        head.append(text, close);
        return head;
    }

    /** The two ways in, as tabs. Underlined rather than filled: a solid block
     *  of accent for "which half of a dialog am I in" is the loudest thing on
     *  a surface whose actual subject is a list of gene names. */
    buildTabs() {
        const el = TranscriptGroupModal.el;
        const strip = el("div", "transcripts-modal-tabs");
        strip.setAttribute("role", "tablist");
        strip.setAttribute("aria-label", "How to make the group");
        this.tabs = {};
        for (const [key, glyph, label] of [["here", "fa-pen", "Name one here"],
                                           ["file", "fa-file-arrow-up", "From a file"]]) {
            const tab = TranscriptGroupModal.button(
                "transcripts-modal-tab", "", () => this.showPane(key));
            tab.append(TranscriptGroupModal.icon(glyph), el("span", "", label));
            tab.setAttribute("role", "tab");
            // Both halves of the pairing, because a tab with no `aria-controls`
            // is a button a screen reader announces as one of two without ever
            // saying what either switches to.
            tab.id = `transcripts_group_tab_${key}`;
            tab.setAttribute("aria-controls", `transcripts_group_pane_${key}`);
            this.tabs[key] = tab;
            strip.appendChild(tab);
        }
        return strip;
    }

    /** One action row, at the bottom. `status` is the only place this dialog
     *  ever says a group WAS made -- it stays open after each one, so without
     *  it the whole of the feedback is a field going blank. */
    buildFooter() {
        const el = TranscriptGroupModal.el;
        const footer = el("footer", "transcripts-modal-actions");

        this.error = el("div", "transcripts-modal-error");
        this.error.setAttribute("role", "alert");
        this.error.hidden = true;

        this.status = el("p", "transcripts-modal-status");
        this.status.setAttribute("aria-live", "polite");

        const said = el("div", "transcripts-modal-said");
        said.append(this.error, this.status);

        const buttons = el("div", "transcripts-modal-buttons");
        this.primary = TranscriptGroupModal.button(
            "transcripts-modal-btn is-primary", "Create group",
            () => this.runPrimary());
        buttons.append(
            TranscriptGroupModal.button(
                "transcripts-modal-btn", "Done", () => this.close()),
            this.primary);

        footer.append(said, buttons);
        return footer;
    }

    showPane(which) {
        this.pane = which;
        for (const [key, pane] of Object.entries(this.panes)) {
            pane.hidden = key !== which;
            this.tabs[key].classList.toggle("is-active", key === which);
            this.tabs[key].setAttribute("aria-selected", String(key === which));
        }
        this.clearError();
        this.primary.textContent = which === "here"
            ? "Create group" : "Add selected";
        this.refreshPrimary();
        if (which === "file") this.mountFileRow();
        else this.nameField?.focus();
    }

    /** Whatever the open pane means by "do it". */
    runPrimary() {
        if (this.pane === "here") this.createHere();
        else this.applyParsed();
    }

    /** Off until there is something for it to do -- a primary button that is
     *  always live on a pane with nothing in it is a button that has to be
     *  pressed to find out it was not ready. */
    refreshPrimary() {
        if (!this.primary) return;
        const ready = this.pane === "here"
            ? Boolean(this.nameField?.value.trim())
            : this.tickedGroups().length > 0;
        this.primary.disabled = !ready;
    }

    // -- pane: name one here -------------------------------------------------

    buildHerePane() {
        const el = TranscriptGroupModal.el;
        const pane = el("div", "transcripts-modal-pane");
        pane.setAttribute("role", "tabpanel");
        pane.id = "transcripts_group_pane_here";
        pane.setAttribute("aria-labelledby", "transcripts_group_tab_here");

        // Labels ABOVE their fields, not in a 4.5em column beside them. The
        // column was a compromise for two one-line controls and this pane no
        // longer is: the gene field carries a count, a chip well and a hint
        // under it, none of which line up with a label parked to the left.
        const nameField = el("div", "transcripts-modal-field");
        this.nameField = el("input", "transcripts-modal-input");
        this.nameField.type = "text";
        this.nameField.id = "transcripts_group_name";
        this.nameField.placeholder = "e.g. Excitatory neurons";
        this.nameField.autocomplete = "off";
        this.nameField.addEventListener("input", () => {
            this.clearError();
            this.refreshPrimary();
        });
        // Enter makes the group. Said explicitly because the form's
        // `method="dialog"` means the browser will do one of two unhelpful
        // things otherwise: nothing at all (there is no submit button and
        // more than one field, so implicit submission is blocked) or, if that
        // ever stops being true, close the dialog. Neither is what somebody
        // who has just typed a name into a one-field pane is asking for.
        this.nameField.addEventListener("keydown", (event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            if (!this.primary?.disabled) this.createHere();
        });
        const nameLabel = el("label", "transcripts-modal-label", "Group name");
        nameLabel.htmlFor = this.nameField.id;
        nameField.append(nameLabel, this.nameField);
        pane.appendChild(nameField);

        const geneField = el("div", "transcripts-modal-field");
        const geneHead = el("div", "transcripts-modal-field-head");
        this.pickedCount = el("span", "transcripts-modal-field-note", "");
        geneHead.append(el("label", "transcripts-modal-label", "Genes"),
                        this.pickedCount);
        const mount = el("div", "transcripts-modal-select");
        geneField.append(geneHead, mount);
        pane.appendChild(geneField);

        // Core's own combobox, so a five-hundred-gene panel behaves the way
        // every other long list in Plexora does. Single-select and clearing
        // after each pick, because picking a gene ADDS it to the chips below
        // -- the same shape the panel's own search box has.
        if (typeof SearchableSelect !== "undefined") {
            this.select = new SearchableSelect(mount, {
                options: this.genes,
                placeholder: "Search genes…",
                emptyText: "No genes match",
                ariaLabel: "Genes in this group",
                onChange: (gene) => this.pick(gene),
            });
        }

        this.chips = el("div", "transcripts-modal-chips");
        geneField.appendChild(this.chips);
        this.emptyNote = el("p", "transcripts-modal-hint",
            "Nothing picked yet. A group can start empty — genes move into it "
            + "from the tree afterwards.");
        geneField.appendChild(this.emptyNote);

        this.paintChips();
        return pane;
    }

    pick(gene) {
        if (gene && !this.picked.includes(gene)) this.picked.push(gene);
        this.select?.setValue?.("");
        this.paintChips();
    }

    paintChips() {
        if (!this.chips) return;
        this.chips.replaceChildren();
        for (const gene of this.picked) {
            const chip = TranscriptGroupModal.el("span", "transcripts-modal-chip");
            chip.appendChild(TranscriptGroupModal.el("span", "", gene));
            const drop = TranscriptGroupModal.button(
                "transcripts-modal-chip-remove", "", () => {
                    this.picked = this.picked.filter((name) => name !== gene);
                    this.paintChips();
                });
            drop.appendChild(TranscriptGroupModal.icon("fa-xmark"));
            drop.setAttribute("aria-label", `Remove ${gene}`);
            chip.appendChild(drop);
            this.chips.appendChild(chip);
        }
        const any = this.picked.length > 0;
        this.chips.hidden = !any;
        if (this.emptyNote) this.emptyNote.hidden = any;
        if (this.pickedCount) {
            this.pickedCount.textContent = any
                ? `${this.picked.length} picked` : "";
        }
    }

    createHere() {
        const name = this.nameField.value.trim();
        if (!name) return this.showError("Give the group a name.");
        if (this.existing.has(name)) {
            return this.showError(`There is already a group called ${name}.`);
        }
        const count = this.picked.length;
        this.apply([{ name, genes: [...this.picked] }]);
        this.nameField.value = "";
        this.picked = [];
        this.paintChips();
        this.nameField.focus();
        this.say(count
            ? `Added ${name} with ${count} gene${count === 1 ? "" : "s"}.`
            : `Added ${name}.`);
        this.refreshPrimary();
        return undefined;
    }

    // -- pane: a file --------------------------------------------------------

    buildFilePane() {
        const el = TranscriptGroupModal.el;
        const pane = el("div", "transcripts-modal-pane");
        pane.setAttribute("role", "tabpanel");
        pane.id = "transcripts_group_pane_file";
        pane.setAttribute("aria-labelledby", "transcripts_group_tab_file");
        pane.hidden = true;

        // Folded, because it is reference and not a step: somebody who has
        // done this once does not need the table again, and somebody who has
        // not needs it before they go looking for their file.
        const details = el("details", "transcripts-modal-help");
        const summary = el("summary", "transcripts-modal-help-summary");
        summary.append(TranscriptGroupModal.icon("fa-chevron-right"),
                       el("span", "", "What the file should look like"));
        details.appendChild(summary);
        const inside = el("div", "transcripts-modal-help-body");
        inside.appendChild(el("p", "transcripts-modal-hint",
            "One row per gene. The first column is the gene; every column "
            + "after it is a group that gene belongs to, so a gene in three "
            + "groups is one row with three names on it. A header row is "
            + "optional. CSV, TSV, TXT, XLSX or XLSM."));
        inside.appendChild(TranscriptGroupModal.buildExample());
        details.appendChild(inside);
        pane.appendChild(details);

        this.filePane = el("div", "transcripts-modal-file");
        pane.appendChild(this.filePane);
        this.preview = el("div", "transcripts-modal-preview");
        pane.appendChild(this.preview);
        return pane;
    }

    /** The shape of the file, as a file. Two columns and three, so the "a
     *  gene can be in several" rule is shown rather than only stated. */
    static buildExample() {
        const el = TranscriptGroupModal.el;
        const table = el("table", "transcripts-modal-example");
        const rows = [
            ["gene", "group", ""],
            ["Slc17a7", "Neurons", "Glutamatergic"],
            ["Gad1", "Neurons", "GABAergic"],
            ["Cd14", "Macrophages", ""],
        ];
        rows.forEach((cells, index) => {
            const row = el("tr");
            for (const cell of cells) {
                const node = el(index === 0 ? "th" : "td", "", cell);
                row.appendChild(node);
            }
            table.appendChild(row);
        });
        return table;
    }

    /** Built on first sight of the pane: `PlexoraFileSourceRow` is a deferred
     *  core script and a plugin's own scripts are not, so it may not exist
     *  yet when this class is parsed. */
    mountFileRow() {
        if (this.fileRow || !this.filePane) return;
        const factory = window.PlexoraFileSourceRow;
        if (!factory) {
            this.filePane.appendChild(TranscriptGroupModal.el(
                "p", "transcripts-modal-hint",
                "This page cannot open a file picker."));
            return;
        }
        this.fileRow = factory.create({
            id: "transcripts_group_file",
            label: "Path to the file",
            placeholder: "/path/to/gene_groups.csv",
            accept: ".csv,.tsv,.txt,.xlsx,.xlsm",
            filter: "channels",
            actionLabel: "Read",
            intent: "No other machine is connected yet. Open one and this "
                    + "dialog can read a gene-group file from it.",
            onChoose: (chosen) => this.readFile(chosen),
        });
        this.filePane.appendChild(this.fileRow.element);
    }

    async readFile(chosen) {
        this.clearError();
        const result = await this.api.parseGroups(this.layerId, chosen);
        this.parsed = result;
        this.paintPreview();
    }

    paintPreview() {
        const el = TranscriptGroupModal.el;
        this.preview.replaceChildren();
        this.groupList = null;
        const result = this.parsed;
        if (!result) return this.refreshPrimary();
        const groups = result.groups || [];
        if (!groups.length) {
            this.preview.appendChild(TranscriptGroupModal.note("warning",
                "No groups in that file. The first column has to be a gene "
                + "this panel carries, with the group names beside it."));
            return this.refreshPrimary();
        }

        const head = el("div", "transcripts-modal-preview-head");
        head.append(el("span", "transcripts-modal-preview-title",
                       "Groups in this file"),
                    el("span", "transcripts-modal-pill", String(groups.length)));
        // All / None, because the ordinary file is a marker list of a dozen
        // rows and the ordinary answer to it is all of them or the four that
        // are new -- neither of which is worth twelve clicks.
        const bulk = el("div", "transcripts-modal-bulk");
        bulk.append(
            TranscriptGroupModal.button("transcripts-modal-link", "All",
                                        () => this.tickAll(true)),
            TranscriptGroupModal.button("transcripts-modal-link", "None",
                                        () => this.tickAll(false)));
        head.appendChild(bulk);
        this.preview.appendChild(head);

        const list = el("div", "transcripts-modal-groups");
        for (const group of groups) {
            const row = el("label", "transcripts-modal-group");
            const box = el("input");
            box.type = "checkbox";
            // Off for a name the tree already has, so the ordinary "read the
            // file again after editing it" ticks only what is new.
            box.checked = !this.existing.has(group.name);
            box.setAttribute("data-group", group.name);
            box.addEventListener("change", () => this.refreshPrimary());
            row.append(box, el("span", "transcripts-modal-group-name",
                               group.name));
            if (this.existing.has(group.name)) {
                row.appendChild(el("span", "transcripts-modal-tag",
                                   "already in the tree"));
            }
            row.appendChild(el("span", "transcripts-modal-group-count",
                               `${group.genes.length} gene`
                               + (group.genes.length === 1 ? "" : "s")));
            list.appendChild(row);
        }
        this.preview.appendChild(list);
        this.groupList = list;

        // Said out loud rather than dropped silently: a marker list written
        // for a bigger panel is the ordinary case, and importing the twenty
        // genes that ARE here is what somebody wants -- but they should be
        // told which twelve were not.
        const unknown = result.unknown || [];
        if (unknown.length) {
            const shown = unknown.slice(0, 8).join(", ");
            this.preview.appendChild(TranscriptGroupModal.note("warning",
                `${unknown.length} name${unknown.length === 1 ? "" : "s"} in `
                + `that file ${unknown.length === 1 ? "is" : "are"} not in this `
                + `panel and will be skipped: ${shown}`
                + (unknown.length > 8 ? "…" : "")));
        }

        return this.refreshPrimary();
    }

    /** A boxed aside -- amber for something the user has to know, which on
     *  this pane is always about what the file did not contain. */
    static note(kind, message) {
        const note = TranscriptGroupModal.el("p",
            `transcripts-modal-note is-${kind}`);
        note.append(TranscriptGroupModal.icon("fa-triangle-exclamation"),
                    TranscriptGroupModal.el("span", "", message));
        return note;
    }

    tickAll(on) {
        if (!this.groupList) return;
        this.groupList.querySelectorAll("input[type=checkbox]")
            .forEach((box) => { box.checked = on; });
        this.refreshPrimary();
    }

    /** The groups the ticks currently name, in the file's own order. */
    tickedGroups() {
        if (!this.groupList) return [];
        const wanted = new Set(
            Array.from(this.groupList.querySelectorAll("input:checked"))
                .map((box) => box.getAttribute("data-group")));
        return (this.parsed?.groups || [])
            .filter((group) => wanted.has(group.name));
    }

    applyParsed() {
        const groups = this.tickedGroups();
        if (!groups.length) return this.showError("Nothing is ticked.");
        this.apply(groups);
        this.preview.replaceChildren();
        this.groupList = null;
        this.parsed = null;
        this.fileRow?.say("");
        this.say(groups.length === 1
            ? `Added ${groups[0].name}.`
            : `Added ${groups.length} groups.`);
        this.refreshPrimary();
        return undefined;
    }

    // -- handing groups back ---------------------------------------------------

    apply(groups) {
        for (const group of groups) this.existing.add(group.name);
        this.clearError();
        try {
            this.onApply?.(groups);
        } catch (error) {
            this.showError(error?.message || "Those groups could not be added.");
        }
    }

    /** What just happened, in the footer. Cleared by the next error, so the
     *  two never sit next to each other contradicting one another. */
    say(message) {
        if (!this.status) return;
        this.status.textContent = message || "";
    }

    showError(message) {
        if (!this.error) return;
        this.say("");
        this.error.textContent = message;
        this.error.hidden = false;
    }

    clearError() {
        if (!this.error) return;
        this.error.hidden = true;
        this.error.textContent = "";
    }

    close() {
        this.dialog?.close();
    }

    destroy() {
        this.select?.destroy?.();
        this.select = null;
        this.dialog?.remove();
        this.dialog = null;
    }
}

if (typeof window !== "undefined") {
    window.TranscriptGroupModal = TranscriptGroupModal;
}
if (typeof globalThis !== "undefined") {
    globalThis.TranscriptGroupModal = TranscriptGroupModal;
}
