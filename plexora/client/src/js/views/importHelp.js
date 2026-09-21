/**
 * How importing works, as four tabs behind the `?` in the import dialog.
 *
 * Documentation that somebody will actually read: a card per format with the
 * marker that identifies it and what comes out, three file trees, and ten
 * one-line rules. No paragraphs -- a wall of text in a modal is a wall of text
 * nobody reads, and the question this answers ("what do I point this at?") is
 * answered by a shape, not by a sentence.
 *
 * The catalogue below is the ONE place a modality is described to a user. It
 * carries the server's own strings (`xenium_morphology`, `cell_boundaries`,
 * `images-grouping`) rather than prose paraphrases of them, so
 * tests/test_import_help.py can hold this file to what the detector actually
 * produces: a modality the server emits and this does not mention is a test
 * failure, which is what keeps documentation from drifting behind the code.
 * Supporting a new format is one entry here and nothing in importSample.js.
 *
 * Its own <dialog>, opened with `showModal()` while the import dialog is still
 * open -- which is why that one does not have to close first: a second modal
 * enters the top layer ABOVE the first, is not clipped by its `overflow:
 * auto`, and takes Escape for itself, because `cancel` does not bubble. It is
 * hosted BESIDE the import dialog rather than inside it; `hostFor` says why.
 *
 * Served as a classic script from base.html, before importSample.js -- the `?`
 * in that dialog's header is the only thing that opens this.
 */
window.PlexoraImportHelp = (function () {
    "use strict";

    //: What each thing the importer can produce is called in a chip. The
    //: server's own long names are in `import_proposal.describe()` and belong
    //: to a ROW, which has a line to itself; these have to fit four to a card.
    //: Keys are the server's `modality` strings and, after them, its `kind`s
    //: -- a SpatialData store's elements carry whatever modality the store
    //: wrote, so that card lists kinds.
    const CHIP = {
        xenium_morphology: "morphology",
        multiplex: "multiplex image",
        he: "H&E image",
        picture: "picture",
        mask: "mask",
        transcripts: "transcripts",
        cell_boundaries: "cell boundaries",
        nucleus_boundaries: "nucleus boundaries",
        visium_spots: "spots",
        cells: "cell table",
        expression: "expression matrix",
        annotations: "annotations",
        blank: "blank frame",
        image: "images",
        labels: "masks",
        points: "points",
        shapes: "shapes",
        table: "cell table",
    };

    //: Every format the importer recognises, in the order somebody scanning
    //: for their own data would look: folders first, because pointing at the
    //: wrong level of a run folder is the one mistake that produces nothing.
    //:
    //: `marker` is what the detector actually keys on. It is here so the card
    //: answers "why didn't it see my run?" without anybody reading Python.
    const FORMATS = [
        {
            name: "Xenium run", shape: "folder", bundle: "xenium",
            pointAt: "the run folder itself",
            marker: "experiment.xenium",
            produces: ["xenium_morphology", "cell_boundaries",
                       "nucleus_boundaries", "transcripts", "cells",
                       "expression"],
            mayAsk: [],
            note: "cell_feature_matrix.h5 is recorded, not read yet.",
        },
        {
            name: "SpatialData store", shape: "folder", bundle: "spatialdata",
            pointAt: "the .zarr store",
            marker: "images/ labels/ points/ shapes/ tables/",
            produces: ["image", "labels", "points", "shapes", "table"],
            mayAsk: ["reference", "table"],
            note: "Read in place. Nothing is copied.",
        },
        {
            name: "Visium (Space Ranger)", shape: "folder", bundle: "visium",
            pointAt: "the outs folder",
            marker: "spatial/scalefactors_json.json + tissue_positions*.csv "
                  + "+ a feature matrix",
            produces: ["he", "visium_spots"],
            mayAsk: [],
            note: "Spots are placed by tissue_hires_scalef.",
        },
        {
            name: "OME-Zarr image", shape: "folder",
            pointAt: "the .zarr, .ome.zarr or .n5",
            marker: "a multiscale group, or numbered series inside one",
            produces: ["multiplex"],
            mayAsk: ["image"],
            note: "",
        },
        {
            name: "DICOM slide", shape: "folder",
            pointAt: "the folder, or one .dcm file",
            marker: ".dcm",
            produces: ["he"],
            mayAsk: [],
            note: "Needs plexora[wsi].",
        },
        {
            name: "Multiplex image", shape: "file",
            pointAt: "the image",
            marker: ".ome.tif · .tif · .tiff · .qptiff",
            produces: ["multiplex"],
            mayAsk: [],
            note: "Channel names come from the file's own metadata.",
        },
        {
            name: "H&E / brightfield slide", shape: "file",
            pointAt: "the slide",
            marker: ".svs · .ndpi · .scn · .mrxs · .svslide, or an RGB TIFF",
            produces: ["he", "picture"],
            mayAsk: [],
            note: "Needs plexora[wsi] for the vendor formats.",
        },
        {
            name: "Segmentation mask", shape: "file",
            pointAt: "the label image",
            marker: "one plane, integer labels — or a name saying mask, "
                  + "label, seg, nuclei, cells",
            produces: ["mask"],
            mayAsk: ["mask-or-image"],
            note: "Must match the image's width and height.",
        },
        {
            name: "Cell table", shape: "file",
            pointAt: "the table",
            marker: ".csv · .tsv · .txt · .parquet with x/y columns",
            produces: ["cells"],
            mayAsk: [],
            note: "Under 64 MB it can simply be dropped on the dialog.",
        },
        {
            name: "AnnData (.h5ad)", shape: "file",
            pointAt: "the file",
            marker: ".h5ad",
            produces: ["cells"],
            mayAsk: [],
            note: "Read in place, never copied.",
        },
        {
            name: "Transcripts", shape: "file",
            pointAt: "the parquet",
            marker: "feature_name · x_location · y_location columns",
            produces: ["transcripts"],
            mayAsk: [],
            note: "Recognised by its columns, whatever it is called.",
        },
        {
            name: "Boundaries", shape: "file",
            pointAt: "the parquet",
            marker: "cell_boundaries.parquet · nucleus_boundaries.parquet",
            produces: ["cell_boundaries", "nucleus_boundaries"],
            mayAsk: [],
            note: "A boundary table can also stand in for a mask.",
        },
        {
            name: "Annotations", shape: "file",
            pointAt: "the GeoJSON",
            marker: ".geojson · .json holding a FeatureCollection",
            produces: ["annotations"],
            mayAsk: [],
            note: "QuPath and ImageJ exports land here.",
        },
        {
            name: "No image at all", shape: "file",
            pointAt: "transcripts or a mask on their own",
            marker: "—",
            produces: ["blank"],
            mayAsk: [],
            note: "A blank frame is sized to the data, to draw in.",
        },
    ];

    //: What each question the detector can ask actually means, keyed by the
    //: id the server sends. A question is a failure of detection shown where
    //: it applies -- never a step -- and none of them blocks the import.
    const QUESTIONS = {
        reference: "which image the sample is drawn in",
        table: "which table holds the cells",
        image: "which image, when a store holds several",
        "mask-or-image": "whether a single-plane image is a mask",
        "images-grouping": "separate samples, or layers of one",
    };

    const EXAMPLES = [
        {
            title: "One folder, one pick",
            tree: "run_0042/\n"
                + "├─ experiment.xenium\n"
                + "├─ morphology_focus/\n"
                + "├─ cells.parquet\n"
                + "├─ cell_boundaries.parquet\n"
                + "└─ transcripts.parquet",
            caption: "Pick the folder. One sample, four layers and a cell "
                   + "table, no questions.",
        },
        {
            title: "Three files, one sample",
            tree: "slide_12.ome.tif      → image\n"
                + "slide_12_mask.tif     → mask\n"
                + "slide_12.csv          → cell table",
            caption: "Same stem before the dot, so they group. _mask, _seg, "
                   + "_labels and _cells are stripped when grouping.",
        },
        {
            title: "Two slides at once",
            tree: "a.ome.tif\n"
                + "b.ome.tif",
            caption: "Different stems, so Plexora asks once: two samples, or "
                   + "one sample with two layers?",
        },
    ];

    //: One line each, and each one is something that has surprised somebody.
    const NOTES = [
        {icon: "image", text: "An image is the only thing a sample must "
            + "have. Everything else can be added later, and Plexora asks for "
            + "what a tool needs when it needs it."},
        {icon: "ruler", text: "Pixel size is read from the file and never "
            + "guessed. Set it in the viewer if the file carries none."},
        {icon: "shapes", text: "A mask must match its image's width and "
            + "height. It attaches after the image and builds in the "
            + "background."},
        {icon: "arrow-down", text: "Dropping a file uploads it: tables "
            + "only, up to 64 MB. Images and run folders are read where they "
            + "are — select or paste a path."},
        {icon: "database", text: "AnnData and SpatialData are read in "
            + "place. Nothing is copied and nothing is uploaded."},
        {icon: "folder-tree", text: "Two runs side by side are not merged. "
            + "Point at one run folder."},
        {icon: "server", text: "For files on another machine, connect it "
            + "in Settings → Remotes, then choose Another machine."},
        {icon: "circle-question",
         text: "A question never stops the import. An unanswered one is "
            + "asked again by the first tool that needs the answer."},
        {icon: "rotate", text: "Adding a mask to an open sample reloads "
            + "the page, because the channel list changes."},
        {icon: "box", text: "A missing reader shows its install command on "
            + "the row: plexora[spatial], plexora[wsi]."},
    ];

    const TABS = [
        {id: "overview", label: "Overview", render: renderOverview},
        {id: "formats", label: "Formats", render: renderFormats},
        {id: "examples", label: "Examples", render: renderExamples},
        {id: "notes", label: "Good to know", render: renderNotes},
    ];

    let dialog = null;
    let opener = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }

    function icon(name) {
        const glyph = el("span", "fas " + name);
        glyph.setAttribute("aria-hidden", "true");
        return glyph;
    }

    /**
     * Where this dialog goes: BESIDE the one that opened it, never inside it.
     *
     * A sibling modal still enters the top layer above the dialog already
     * there, so nothing is lost -- and two things are avoided. importSample's
     * `part(role)` is a `querySelector` over its whole subtree, which a nested
     * help would be part of. And `PopoverPortal` moves every element it hosts
     * into whichever modal is topmost, on every `close` and every fullscreen
     * change: with this dialog hosted by the portal AND on top, that rule says
     * to append the import dialog into this one. Staying out of the portal is
     * what makes that unreachable.
     *
     * The opener's own parent is the right host whatever it is -- <body>, or
     * the fullscreened subtree the portal put the import dialog in, which is
     * the case "+ Add Layer" opens from.
     */
    function hostFor(from) {
        const dialogOf = from && from.closest && from.closest("dialog");
        return (dialogOf && dialogOf.parentNode) || document.body;
    }

    // -- the tabs -----------------------------------------------------------

    function renderOverview(panel) {
        const steps = el("ol", "plx-help-steps");
        [["Pick", "a file or a folder, on this computer or on a machine you "
                + "have connected."],
         ["Check", "one row per thing found. A question appears only where a "
                 + "file is genuinely ambiguous."],
         ["Import", "one press. The sample opens while anything slow keeps "
                  + "building behind it."]].forEach(([name, body], index) => {
            const step = el("li", "plx-help-step");
            step.appendChild(el("span", "plx-help-step-num", String(index + 1)));
            const text = el("div", "plx-help-step-text");
            text.appendChild(el("h3", "plx-help-step-name", name));
            text.appendChild(el("p", null, body));
            step.appendChild(text);
            steps.appendChild(step);
        });
        panel.appendChild(steps);

        const cols = el("div", "plx-help-cols");
        [["Required", [
            ["fa-image", "One image", "or a folder that holds one: a Xenium "
                + "run, Visium, SpatialData, OME-Zarr"],
         ]], ["Optional", [
            ["fa-shapes", "Segmentation mask", "a label image, or a boundary "
                + "parquet"],
            ["fa-table", "Cell table", "CSV · Parquet · AnnData"],
            ["fa-braille", "Transcripts, boundaries, annotations", ""],
            ["fa-pen", "A name and a dataset", "both are filled in for you"],
         ]]].forEach(([heading, rows]) => {
            const col = el("section", "plx-help-col");
            col.appendChild(el("h3", "plx-help-col-head", heading));
            rows.forEach(([glyph, name, detail]) => {
                const row = el("div", "plx-help-col-row");
                row.appendChild(icon(glyph));
                const text = el("div", "plx-help-col-text");
                text.appendChild(el("strong", null, name));
                if (detail) text.appendChild(el("span", null, " " + detail));
                row.appendChild(text);
                col.appendChild(row);
            });
            cols.appendChild(col);
        });
        panel.appendChild(cols);
        panel.appendChild(el("p", "plx-help-foot",
            "Anything optional can be added to a sample later with + Add "
            + "Layer, and Plexora asks for what a tool needs when it needs "
            + "it."));
    }

    function renderFormats(panel) {
        const grid = el("div", "plx-help-grid");
        FORMATS.forEach((format) => {
            const card = el("article", "plx-help-card");
            const head = el("header", "plx-help-card-head");
            head.appendChild(icon(format.shape === "folder"
                                  ? "fa-folder" : "fa-file"));
            head.appendChild(el("h3", "plx-help-card-name", format.name));
            head.appendChild(el("span", "plx-help-shape",
                                format.shape === "folder" ? "Folder" : "File"));
            card.appendChild(head);

            card.appendChild(el("p", "plx-help-card-line",
                                "Point at " + format.pointAt));
            // Three keyed lines down one column, so the values line up and
            // the eye can run "what does it look for" down the grid without
            // reading the cards.
            if (format.marker && format.marker !== "—") {
                const marker = el("p", "plx-help-card-line plx-help-card-pair");
                marker.appendChild(el("span", "plx-help-card-key", "Looks for"));
                marker.appendChild(el("code", null, format.marker));
                card.appendChild(marker);
            }

            const chips = el("p", "plx-help-card-pair");
            chips.appendChild(el("span", "plx-help-card-key", "Gives you"));
            const bag = el("span", "plx-help-chip-bag");
            format.produces.forEach((key) => {
                bag.appendChild(el("span", "plx-help-chip", CHIP[key] || key));
            });
            chips.appendChild(bag);
            card.appendChild(chips);

            // Not warning-coloured, and deliberately: a question the importer
            // might ask is a fact about the format, not something wrong with
            // it. Amber on six of fourteen cards would read as six problems.
            if (format.mayAsk.length) {
                const ask = el("p", "plx-help-card-ask plx-help-card-pair");
                ask.appendChild(el("span", "plx-help-card-key", "May ask"));
                ask.appendChild(el("span", null, format.mayAsk
                    .map((id) => QUESTIONS[id] || id).join(" · ")));
                card.appendChild(ask);
            }
            if (format.note) {
                card.appendChild(el("p", "plx-help-card-note", format.note));
            }
            grid.appendChild(card);
        });
        panel.appendChild(grid);
    }

    function renderExamples(panel) {
        EXAMPLES.forEach((example) => {
            const block = el("section", "plx-help-example");
            block.appendChild(el("h3", "plx-help-col-head", example.title));
            block.appendChild(el("pre", "plx-help-tree", example.tree));
            block.appendChild(el("p", "plx-help-card-note", example.caption));
            panel.appendChild(block);
        });
    }

    function renderNotes(panel) {
        const list = el("ul", "plx-help-bullets");
        NOTES.forEach((note) => {
            const item = el("li");
            // NOTES carries the BARE name, the way the figure builder's
            // action registry does, because tests/test_icon_names.py reads an
            // `icon:` key as the icon's whole name -- so a prefixed one there
            // is a name Font Awesome does not ship, and an unshipped name
            // draws an empty span in silence.
            item.appendChild(icon("fa-" + note.icon));
            item.appendChild(el("span", null, note.text));
            list.appendChild(item);
        });
        panel.appendChild(list);
    }

    // -- the dialog ---------------------------------------------------------

    /**
     * Every `data-role` here is prefixed `help-`, and stays that way.
     *
     * `hostFor` puts this dialog outside the import dialog, so the collision
     * this guards against is not reachable today -- but importSample's
     * `part(role)` is a `querySelector` over its whole subtree, and one
     * `close` or `body` in here would be enough to make a future nesting
     * silently wrong rather than obviously wrong.
     */
    function build(host) {
        const node = document.createElement("dialog");
        node.className = "plx-dialog plx-help";
        node.setAttribute("aria-label", "How importing works");

        const head = el("div", "plx-help-head");
        head.appendChild(el("h2", "plx-dialog-title", "How importing works"));
        const closer = el("button", "plx-picker-close");
        closer.type = "button";
        closer.dataset.role = "help-close";
        closer.title = "Close";
        closer.setAttribute("aria-label", "Close");
        closer.appendChild(icon("fa-xmark"));
        closer.addEventListener("click", close);
        head.appendChild(closer);
        node.appendChild(head);

        const tabs = el("div", "plx-help-tabs");
        tabs.dataset.role = "help-tabs";
        tabs.setAttribute("role", "tablist");
        tabs.setAttribute("aria-label", "Import help");
        node.appendChild(tabs);

        TABS.forEach((tab) => {
            const button = el("button", "plx-help-tab", tab.label);
            button.type = "button";
            button.id = `plx-help-tab-${tab.id}`;
            button.setAttribute("role", "tab");
            button.setAttribute("aria-controls", `plx-help-panel-${tab.id}`);
            button.addEventListener("click", () => select(tab.id));
            tabs.appendChild(button);

            const panel = el("section", "plx-help-panel");
            panel.dataset.role = `help-panel-${tab.id}`;
            panel.id = `plx-help-panel-${tab.id}`;
            panel.setAttribute("role", "tabpanel");
            panel.setAttribute("aria-labelledby", button.id);
            panel.hidden = true;
            tab.render(panel);
            node.appendChild(panel);
        });

        // Arrows move between tabs, which is what a tablist is expected to do
        // and what Tab does NOT do: Tab leaves the strip.
        tabs.addEventListener("keydown", (event) => {
            const step = event.key === "ArrowRight" ? 1
                       : event.key === "ArrowLeft" ? -1 : 0;
            if (!step) return;
            event.preventDefault();
            const ids = TABS.map((tab) => tab.id);
            const at = ids.indexOf(currentTab(node));
            const next = ids[(at + step + ids.length) % ids.length];
            select(next);
            node.querySelector(`#plx-help-tab-${next}`).focus();
        });

        // Escape. `cancel` does not bubble, so this takes it and the import
        // dialog underneath never sees it -- including while it is importing,
        // where it refuses to close at all.
        node.addEventListener("cancel", (event) => {
            event.preventDefault();
            close();
        });

        host.appendChild(node);
        return node;
    }

    function currentTab(node) {
        const chosen = (node || dialog).querySelector(
            '.plx-help-tab[aria-selected="true"]');
        return chosen ? chosen.id.replace("plx-help-tab-", "") : TABS[0].id;
    }

    function select(id) {
        if (!dialog) return;
        TABS.forEach((tab) => {
            const chosen = tab.id === id;
            const button = dialog.querySelector(`#plx-help-tab-${tab.id}`);
            const panel = dialog.querySelector(`#plx-help-panel-${tab.id}`);
            button.setAttribute("aria-selected", chosen ? "true" : "false");
            button.classList.toggle("is-active", chosen);
            // Only the selected tab is in the tab order; the arrows reach the
            // others. A tablist that puts four stops in the sequence is four
            // presses between the strip and what it governs.
            button.tabIndex = chosen ? 0 : -1;
            panel.hidden = !chosen;
        });
    }

    /**
     * @param tab - which tab to open on. The `?` sends "formats" when the
     *   proposal on screen has a file Plexora could not read, because that is
     *   the question being asked.
     * @param returnTo - the button that opened this, focused again on close.
     *   The close algorithm restores focus by itself in Chrome and Firefox and
     *   does not in Safari, and losing focus to <body> inside a modal that is
     *   still open strands the keyboard.
     */
    function open(options) {
        options = options || {};
        if (dialog) close();
        opener = options.returnTo || null;
        const wanted = TABS.some((tab) => tab.id === options.tab)
            ? options.tab : TABS[0].id;
        dialog = build(hostFor(opener));
        select(wanted);
        // Focus the strip rather than the close button: it is what the arrows
        // drive, and it is the first thing worth reading.
        dialog.querySelector(`#plx-help-tab-${wanted}`).autofocus = true;
        dialog.showModal();
        return dialog;
    }

    function close() {
        if (!dialog) return;
        const node = dialog;
        const back = opener;
        dialog = null;
        opener = null;
        try {
            node.close();
        } catch (error) { /* already closed */ }
        node.remove();
        if (back && typeof back.focus === "function") back.focus();
    }

    return {open, close};
})();
