/**
 * Import Sample: one dialog, three states, no wizard.
 *
 * The whole of importing data into Plexora. Somebody presses one button,
 * points at files or a folder, sees what Plexora found, and presses Import.
 * There are no format tabs, no role-labelled fields and no second page.
 *
 *   pick       Which machine, Choose a file / Choose a folder, or paste a
 *              path. One sentence names the formats and links to the rest.
 *   proposal   One card per sample, one row per detected layer, each saying
 *              what it becomes. A question appears UNDER the row it concerns,
 *              with its default already chosen, and never blocks Import.
 *   importing  The same rows become a progress rail, and the sample opens as
 *              soon as its record exists -- whatever is still building keeps
 *              building behind the viewer, which already knows how to wait.
 *
 * A line under the title carries the whole of the dialog's voice: what to do,
 * then what pressing Import would create, then what is happening. Nothing
 * inside the body repeats it. The `?` beside the close button opens
 * views/importHelp.js, which is where formats, folder layouts and rules are
 * documented -- so this dialog never has to explain itself in place.
 *
 * The three states are one `<dialog>` that changes in place rather than three
 * screens: a modal that grew a Back button would be a wizard, and the entire
 * point is that the default path is one gesture and one press.
 *
 * Nothing here decides what a file IS. `POST /import/inspect` answers that,
 * and this draws the answer -- so a new vendor format becomes a row in this
 * list with no change to this file. Detection is also the only thing that
 * puts a question on screen: a question here is a failure of detection, shown
 * where it applies, not a step in a flow.
 *
 * Scoped mode (`open({sample})`) is the same dialog for "+ Add Layer": no name
 * and no dataset, everything proposed as a layer of a sample that already
 * exists, and the viewer adopts the result without a reload unless a MASK was
 * added -- which shifts channel indices and is the one case a reload is for.
 *
 * Served as a classic script from base.html, after datasetPicker.js: the
 * dataset row is that picker, and the library page and the viewer both open
 * this.
 */
window.PlexoraImportSample = (function () {
    "use strict";

    //: How often the importing state asks what has landed. The same interval
    //: the mask's own wait uses, for the same reason: fast enough that a short
    //: build does not look stalled, slow enough that a long one is not a poll
    //: storm.
    const POLL_MS = 1500;

    //: How long the dialog stays up after a successful import before it opens
    //: the sample. Long enough to read "registered" against each row, short
    //: enough that it never feels like a step.
    const OPEN_AFTER_MS = 1200;

    //: A glyph per layer kind. The kind is a rendering strategy, so this is
    //: honest about what the row will look like rather than about what the
    //: data is -- the modality is in the words beside it.
    const KIND_ICON = {
        image: "fa-layer-group",
        labels: "fa-shapes",
        points: "fa-braille",
        shapes: "fa-draw-polygon",
        table: "fa-table",
        warning: "fa-triangle-exclamation",
        info: "fa-circle-info",
    };

    //: What each row BECOMES, as the badge ranged right on it. `role` is where
    //: the thing is stored -- the image spec, the segmentation, the feature
    //: table, a layer -- which is the distinction a row's name and its detail
    //: line do not carry and the one somebody scanning for "did it find my
    //: mask?" is looking for. Never coloured: a role is a fact, not a status.
    const ROLE_BADGE = {
        image: "Image",
        mask: "Mask",
        table: "Table",
        layer: "Layer",
        //: Recorded on the sample and not read -- a Xenium expression matrix.
        //: Saying so on the row is the difference between a feature somebody
        //: is waiting for and a bug they report.
        note: "Recorded",
    };

    //: What a single pick that expands into a whole sample is called, keyed
    //: by the `format` on the bundle the server sends back.
    const BUNDLE_WORD = {
        xenium: "a Xenium run",
        spatialdata: "a SpatialData store",
        visium: "a Visium run",
    };

    //: The same four in the summary line's own register, where they are read
    //: as a list rather than as a label.
    const ROLE_WORD = {
        image: "image",
        mask: "mask",
        table: "cell table",
        layer: "layer",
        note: "recorded data",
    };

    let dialog = null;
    let state = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }

    /** Whether this dialog is scoped to one existing sample. */
    function scoped() {
        return Boolean(state && state.sample);
    }

    // -- the dialog itself --------------------------------------------------

    function build() {
        const node = document.createElement("dialog");
        node.className = "plx-dialog plx-import";
        node.innerHTML = `
            <div class="plx-import-head">
                <div class="plx-import-head-text">
                    <h2 class="plx-dialog-title" data-role="title">Import sample</h2>
                    <p class="plx-import-subtitle" data-role="subtitle"></p>
                </div>
                <div class="plx-import-head-actions">
                    <button class="plx-picker-close plx-import-help" type="button"
                            data-role="help" title="How importing works"
                            aria-label="How importing works" aria-haspopup="dialog">
                        <span class="fas fa-circle-question" aria-hidden="true"></span>
                    </button>
                    <button class="plx-picker-close" type="button" data-role="close"
                            title="Close" aria-label="Close">
                        <span class="fas fa-xmark" aria-hidden="true"></span>
                    </button>
                </div>
            </div>
            <div class="plx-import-where" data-role="where">
                <span class="plx-import-where-label">Data location</span>
                <span data-role="where-mount"></span>
                <span class="plx-import-where-caption" data-role="where-caption" hidden></span>
                <span class="plx-import-where-status" data-role="where-status"></span>
            </div>
            <div class="plx-import-body" data-role="body"></div>
            <p class="plx-import-status" data-role="status" role="status" aria-live="polite"></p>
            <div class="plx-import-foot">
                <button class="plx-button" type="button" data-role="add" hidden>
                    <span class="fas fa-plus" aria-hidden="true"></span> Add files
                </button>
                <span class="plx-import-blocked" data-role="reason"></span>
                <button class="plx-button plx-button-primary" type="button" data-role="go" disabled>
                    Import sample
                </button>
            </div>`;
        node.querySelector('[data-role="close"]').addEventListener("click", close);
        // The `?`. Opens on Formats when something on screen was not
        // recognised, because that is the question actually being asked --
        // "what DOES it read?" -- and on Overview otherwise.
        node.querySelector('[data-role="help"]').addEventListener("click", (event) => {
            window.PlexoraImportHelp?.open({
                tab: (state?.proposal?.unrecognised || []).length
                    ? "formats" : "overview",
                returnTo: event.currentTarget,
            });
        });
        node.querySelector('[data-role="add"]').addEventListener("click", () => {
            render("pick");
            // `autofocus` only fires when the dialog OPENS. Coming back here
            // from a proposal, the focus has to be placed by hand or it stays
            // on a button that is no longer on screen.
            state?.halves?.[0]?.focus();
        });
        // Wrapped rather than passed straight in: `submit` takes the name of a
        // sample it is replacing, and a listener would hand it a MouseEvent.
        node.querySelector('[data-role="go"]').addEventListener("click", () => submit());
        // Escape closes; the browser fires `cancel` for it. Prevented while an
        // import is in flight -- the work is server-side and would carry on
        // with nothing on screen saying so.
        node.addEventListener("cancel", (event) => {
            if (state && state.phase === "importing") event.preventDefault();
            else close();
        });
        portal("attach", node);
        return node;
    }

    /**
     * Put the dialog where a fullscreen viewer can still see it.
     *
     * `PopoverPortal` is a classic script's top-level `const`, which is a
     * shared binding and NOT a property of `window` -- reaching for it through
     * `window` silently does nothing, and a <dialog> that was never appended
     * throws on `showModal`. Guarded by `typeof` because this dialog also opens
     * from the library page, which loads the portal but need not.
     *
     * The fallback is <body>, which is what every other dialog here uses and
     * is right everywhere except inside a fullscreened subtree -- which is
     * exactly the case "+ Add Layer" can be opened from.
     */
    function portal(verb, node) {
        if (typeof PopoverPortal !== "undefined") {
            PopoverPortal[verb](node);
            return;
        }
        if (verb === "attach") document.body.appendChild(node);
        else node.remove();
    }

    function part(role) {
        return dialog ? dialog.querySelector(`[data-role="${role}"]`) : null;
    }

    function setStatus(message, isError) {
        const status = part("status");
        if (!status) return;
        status.textContent = message || "";
        status.classList.toggle("is-error", Boolean(message) && Boolean(isError));
    }

    function close() {
        if (!dialog) return;
        const after = state && state.onClose;
        state = null;
        try {
            dialog.close();
        } catch (error) { /* already closed */ }
        portal("detach", dialog);
        dialog = null;
        if (after) after();
    }

    /**
     * Open the dialog.
     *
     * @param sample - an existing sample's name, for "+ Add Layer". Scoped
     *   mode: no name, no dataset, everything added to that sample.
     * @param modality - filter the invitation text to one kind of data, for
     *   the requirements modal's "Add layer..." row.
     * @param dataset - `{id, name}` to preselect, for importing from inside a
     *   dataset folder.
     * @param onClose - called when the dialog goes away, whatever happened.
     *   The library page re-lists from it.
     */
    function open(options) {
        options = options || {};
        if (dialog) close();
        state = {
            phase: "pick",
            picks: [],
            proposal: null,
            answers: {},
            sample: options.sample || null,
            modality: options.modality || null,
            dataset: options.dataset || null,
            name: null,
            node: null,
            location: null,
            onClose: options.onClose || null,
        };
        dialog = build();
        part("title").textContent = scoped()
            ? `Add a layer to ${state.sample}` : "Import sample";
        part("go").textContent = scoped() ? "Add layers" : "Import sample";
        mountLocation();
        render("pick");
        dialog.showModal();
        return dialog;
    }

    /**
     * The Local/Remote switch, mounted once above the controls.
     *
     * The landing page's arrangement, and for its reason: the switch decides
     * WHOSE filesystem the pick controls browse, so it belongs above them
     * rather than inside a row it would qualify.
     */
    function mountLocation() {
        if (!window.PlexoraDataLocation || !window.PlexoraDataLocation.available()) {
            // Nothing to choose between, so nothing to ask. The row goes
            // rather than standing empty above the panel.
            part("where").hidden = true;
            return;
        }
        const box = document.createElement("input");
        box.type = "hidden";
        dialog.appendChild(box);
        try {
            state.location = window.PlexoraDataLocation.attach(box, {
                kind: "image",
                mount: part("where-mount"),
                statusMount: part("where-status"),
                onChange: () => paintCaption(),
            });
        } catch (error) {
            console.error("importSample: no location switch.", error);
        }
        paintCaption();
    }

    /**
     * The word the two letters do not carry, in the one arrangement that has
     * room for it.
     *
     * The home page's, to the word (`views/quickViewLanding.js`): a label
     * before the chip and this after it. Local only -- on Remote the switch's
     * own place button stands here instead and NAMES the machine, and unlike
     * this caption it is clickable, which is how the machine is changed
     * without toggling back through Local and losing the pick on the way.
     */
    function paintCaption() {
        const caption = part("where-caption");
        if (!caption) return;
        const local = !state.location || state.location.isLocal();
        caption.hidden = !local;
        caption.textContent = local ? "this computer" : "";
    }

    function browseNode() {
        return state.location ? state.location.browseNode() : null;
    }

    // -- state 1: pick ------------------------------------------------------

    function renderPick() {
        const body = part("body");
        body.innerHTML = "";
        const drop = el("div", "plx-import-drop");
        // No heading over the panel: the subtitle in the header already said
        // what to do, and saying it twice on one screen is half the clutter
        // this state used to carry.
        const panel = buildSplitControl("sample", pickWith,
                                        {file: "Choose a file",
                                         directory: "Choose a folder"});
        panel.classList.add("is-panel");
        panel.addEventListener("keydown", (event) => stepBetweenHalves(panel, event));
        drop.appendChild(panel);
        //: The two halves, held so they can be disabled while something is in
        //: flight. Real buttons, so `disabled` rather than the pointer-events
        //: trick a dropzone would use -- and it is needed either way: a second
        //: press while a file dialog is opening opens a second one.
        state.halves = [...panel.querySelectorAll(".browse-kind-half")];
        // Where `showModal()` puts the keyboard. Without it the browser
        // focuses the first focusable descendant, which is the × -- so the
        // dialog opened with a cyan ring around Close and nothing else.
        // `autofocus` is honoured on open and inert afterwards, which is why
        // "Add files" focuses the half by hand below.
        if (state.halves[0]) state.halves[0].autofocus = true;

        const row = el("div", "plx-import-path");
        const box = el("input", "plx-import-path-input");
        box.type = "text";
        box.placeholder = "…or paste a path and press Enter, or drop a table here";
        box.spellcheck = false;
        box.addEventListener("keydown", (event) => {
            if (event.key !== "Enter") return;
            const typed = box.value.trim();
            if (typed) {
                box.value = "";
                addPick(typed);
            }
        });
        row.appendChild(box);
        drop.appendChild(row);

        body.appendChild(drop);

        // One sentence, under the panel rather than inside it, and ending in
        // the way to see the whole list. The two halves already carry their
        // own examples (KIND_EXAMPLES.sample); this used to repeat them
        // underneath in a second, longer list saying the same thing again.
        const formats = el("p", "plx-import-formats",
            "Works with Xenium, Visium, SpatialData, OME-TIFF, OME-Zarr, "
            + "H&E slides, masks, AnnData and CSV tables. ");
        const link = el("button", "plx-import-formats-link", "All formats");
        link.type = "button";
        link.addEventListener("click", () => {
            window.PlexoraImportHelp?.open({tab: "formats", returnTo: link});
        });
        formats.appendChild(link);
        body.appendChild(formats);

        // Files dropped on the dialog. Small tables only, and the copy says so:
        // a browser cannot give a server the PATH of a 40 GB image, only its
        // bytes, and uploading a slide through a form is not an import.
        drop.addEventListener("dragover", (event) => {
            event.preventDefault();
            drop.classList.add("is-over");
        });
        drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
        drop.addEventListener("drop", onDrop);

        part("add").hidden = true;
        part("go").disabled = state.picks.length === 0;
        if (state.picks.length) {
            // Coming back here from a proposal to add more. The button still
            // imports what is already there.
            part("go").disabled = false;
        }
        // A disabled primary with nothing beside it reads as a broken button.
        setReason(state.picks.length ? "" : "Choose a file or folder to continue");
    }

    /**
     * Why the primary button is not pressable, beside the primary button.
     *
     * Only ever about the PICKS. An unanswered question is not a reason --
     * every question has a default or is deferred, and none of them blocks an
     * import -- so putting one here would tell the user to do something the
     * dialog does not actually require of them.
     */
    function setReason(text) {
        const reason = part("reason");
        if (reason) reason.textContent = text || "";
    }

    async function onDrop(event) {
        event.preventDefault();
        const drop = event.currentTarget;
        drop.classList.remove("is-over");
        const files = Array.from(event.dataTransfer?.files || []);
        if (!files.length) return;
        const big = files.find((file) => file.size > 64 * 1024 * 1024);
        if (big) {
            window.PlexoraConfirm?.tell({
                title: "Too big to drop",
                body: "A browser can only hand Plexora a dropped file's BYTES, "
                    + "not where it lives — which is fine for a table and wrong "
                    + "for a slide. For images and run folders, use Select.",
            });
            return;
        }
        setStatus("Uploading…");
        for (const file of files) {
            const form = new FormData();
            form.append("file", file);
            try {
                const response = await fetch(plexoraUrl("upload_data_file"),
                                             {method: "POST", body: form});
                const result = await response.json();
                if (!response.ok || !result.path) throw new Error(result.error || "");
                state.picks.push(result.path);
            } catch (error) {
                setStatus(`Could not take ${file.name}.`, true);
                return;
            }
        }
        setStatus(null);
        inspect();
    }

    function setBusy(busy) {
        (state?.halves || []).forEach((half) => { half.disabled = busy; });
    }

    function pickWith(mode) {
        setStatus("Opening file browser…");
        setBusy(true);
        const settle = setTimeout(() => setStatus(null), 1500);
        browseForPath({
            mode,
            filter: "any",
            node: browseNode(),
            onPicked: (path) => {
                clearTimeout(settle);
                setStatus(null);
                addPick(path);
            },
            onUnavailable: () => {
                clearTimeout(settle);
                setStatus("The file browser could not be opened — paste the "
                          + "full path instead.", true);
            },
        }).finally(() => {
            clearTimeout(settle);
            setBusy(false);
        });
    }

    function addPick(path) {
        const node = browseNode();
        // A path on another machine is addressed rather than read: the node is
        // the only process that can open it.
        state.picks.push(node ? `node://${node}/${path}` : path);
        inspect();
    }

    // -- state 2: proposal --------------------------------------------------

    //: Guards against an out-of-order answer. Every inspection is a round trip
    //: and the user can answer a second question while the first is in flight;
    //: without this the older answer's proposal can land last and undo it.
    let inspectToken = 0;

    async function inspect() {
        const token = ++inspectToken;
        render("proposal");
        setStatus("Looking at what is there…");
        try {
            const response = await fetch(plexoraUrl("import/inspect"), {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    paths: state.picks,
                    answers: state.answers,
                    sample: state.sample || undefined,
                }),
            });
            const proposal = await response.json();
            if (token !== inspectToken || !state) return;
            state.proposal = proposal;
            setStatus(null);
            render("proposal");
        } catch (error) {
            if (token !== inspectToken || !state) return;
            setStatus("Plexora could not read those files.", true);
        }
    }

    function renderProposal() {
        const body = part("body");
        body.innerHTML = "";
        const proposal = state.proposal;
        part("add").hidden = false;

        if (!proposal) {
            body.appendChild(el("p", "plx-import-empty", "Reading…"));
            part("go").disabled = true;
            setReason("");
            return;
        }

        const samples = proposal.samples || [];
        samples.forEach((sample, index) => {
            body.appendChild(renderSample(sample, index, samples.length));
        });

        (proposal.unrecognised || []).forEach((entry) => {
            const row = el("div", "plx-import-row is-warning");
            row.appendChild(iconFor("warning"));
            const text = el("div", "plx-import-row-text");
            text.appendChild(el("span", "plx-import-row-name", basename(entry.path)));
            text.appendChild(el("span", "plx-import-row-detail", entry.reason));
            row.appendChild(text);
            row.appendChild(removeButton(entry.path));
            body.appendChild(row);
        });

        // A warning is about the pick as a whole rather than about one file,
        // so it has no row to sit under and no ✕ to leave it out with.
        (proposal.warnings || []).forEach((warning) => {
            const row = el("div", "plx-import-row is-warning");
            row.appendChild(iconFor("warning"));
            const text = el("div", "plx-import-row-text");
            text.appendChild(el("span", "plx-import-row-detail", warning));
            row.appendChild(text);
            body.appendChild(row);
        });

        // Enabled as soon as ANY sample has a layer. An unreadable file among
        // five must not stop the other four from being imported.
        const importable = (proposal.samples || []).some((s) => (s.layers || []).length);
        part("go").textContent = goLabel(samples);
        setReason(importable ? "" : "Nothing to import");
        part("go").disabled = !importable;
    }

    /**
     * What the primary button says it will do, counted.
     *
     * "Import sample" over a screen showing two of them is the one place this
     * dialog could mislead somebody into a second press.
     */
    function goLabel(samples) {
        const layers = samples.reduce(
            (total, sample) => total + (sample.layers || []).length, 0);
        if (scoped()) return layers === 1 ? "Add layer" : `Add ${layers} layers`;
        return samples.length > 1
            ? `Import ${samples.length} samples` : "Import sample";
    }

    /**
     * The one line under the title: what pressing Import would create.
     *
     * Presentation only -- every number here is already on the proposal. A
     * bundle counts as ONE source, because a Xenium run is one thing the user
     * picked and "1 sample from 6 files" describes a folder they chose once.
     */
    function summarize(proposal) {
        const samples = proposal.samples || [];
        const layers = samples.reduce(
            (all, sample) => all.concat(sample.layers || []), []);
        if (!layers.length) return "Nothing here Plexora can read.";

        //: A run folder is ONE thing the user pointed at, and it has a name
        //: -- so it is named rather than counted. `format` and not `label`,
        //: which already carries a middle dot and would collide with this
        //: line's own.
        const bundles = new Set();
        const files = new Set();
        layers.forEach((layer) => {
            if (layer.bundle) bundles.add(BUNDLE_WORD[layer.bundle.format]
                                          || "a run folder");
            else files.add(layer.src || layer.id);
        });
        const sources = [...bundles];
        if (files.size) {
            sources.push(files.size === 1 ? "1 file" : `${files.size} files`);
        }
        const roles = [];
        const seen = new Set();
        layers.forEach((layer) => {
            const word = layer.reference ? "image" : ROLE_WORD[layer.role];
            if (!word || seen.has(word)) return;
            seen.add(word);
            roles.push(word);
        });

        // Scoped, nothing here is a sample: these are layers of one that
        // already exists, and saying "1 sample" over the + Add Layer dialog
        // would promise a second copy of it.
        const count = scoped()
            ? (layers.length === 1 ? "1 layer" : `${layers.length} layers`)
            : (samples.length === 1 ? "1 sample" : `${samples.length} samples`);
        const from = list(sources);
        const said = list(roles);
        return said ? `${count} from ${from} · ${said}` : `${count} from ${from}`;
    }

    function renderSample(sample, index, total) {
        const block = el("div", "plx-import-sample");

        // Only when there is more than one, and then it has to be there: two
        // unheaded stacks of rows is the arrangement in which somebody types a
        // name into the wrong one.
        if (total > 1) {
            block.appendChild(el("div", "plx-import-sample-head",
                sample.name ? `Sample ${index + 1} · ${sample.name}`
                            : `Sample ${index + 1}`));
        }

        if (sample.existing && !scoped()) {
            const already = el("div", "plx-import-existing");
            already.appendChild(iconFor("info"));
            already.appendChild(el("span", "plx-import-existing-text",
                `Already imported as ${sample.existing}.`));
            const buttons = el("div", "plx-import-existing-actions");
            // Re-reading the same files can produce a DIFFERENT sample: which
            // of a run's three morphology images Plexora opens, whether a
            // Z-stack is one channel or fourteen and where the pixel size
            // comes from are all answers detection gives, and they improve.
            // Without this the old answer is the only one reachable -- the
            // record is matched by its bundle, so every later import of the
            // same folder lands back on it.
            const again = el("button", "plx-button", "Re-import");
            again.type = "button";
            again.title = `Read these files again and rewrite ${sample.existing}`;
            again.addEventListener("click", () => submit(sample.existing));
            buttons.appendChild(again);
            const openIt = el("button", "plx-button plx-button-primary", "Open it");
            openIt.type = "button";
            openIt.addEventListener("click", () => {
                const target = sample.existing;
                close();
                PlexoraRouter.go(`/${encodeURIComponent(target)}`);
            });
            buttons.appendChild(openIt);
            already.appendChild(buttons);
            block.appendChild(already);
        }

        (sample.layers || []).forEach((layer) => {
            block.appendChild(renderLayer(layer));
            questionsFor(sample, `layer:${layer.id}`).forEach((question) => {
                block.appendChild(renderQuestion(question));
            });
        });

        questionsFor(sample, "sample").forEach((question) => {
            block.appendChild(renderQuestion(question));
        });

        if (!scoped()) block.appendChild(renderNameRow(sample, index));
        return block;
    }

    function questionsFor(sample, scope) {
        return (sample.questions || []).filter((q) => q.scope === scope);
    }

    function iconFor(kind) {
        const wrap = el("span", "plx-import-row-icon");
        const glyph = el("span", `fas ${KIND_ICON[kind] || "fa-circle-question"}`);
        glyph.setAttribute("aria-hidden", "true");
        wrap.appendChild(glyph);
        return wrap;
    }

    function renderLayer(layer) {
        const row = el("div", "plx-import-row");
        row.dataset.layer = layer.id;
        row.appendChild(iconFor(layer.kind));

        const text = el("div", "plx-import-row-text");
        text.appendChild(el("span", "plx-import-row-name", layer.label || layer.id));
        text.appendChild(el("span", "plx-import-row-detail", layer.detail || ""));
        if (layer.dependency && layer.dependency.install) {
            // A package this environment has not got. The row stays -- what is
            // missing is an install, not the data -- and the command to fix it
            // is shown rather than a stack trace after the fact.
            const hint = el("code", "plx-import-install", layer.dependency.install);
            text.appendChild(hint);
        }
        row.appendChild(text);
        // The reference is the image everything else is registered against, so
        // it says so rather than saying "image": which of three is the frame
        // is the one thing on this screen worth correcting before importing.
        row.appendChild(el("span", "plx-import-role",
            layer.reference ? "Reference image"
                            : ROLE_BADGE[layer.role] || "Layer"));
        if (layer.src) row.appendChild(removeButton(layer.src));
        return row;
    }

    function removeButton(path) {
        const button = el("button", "plx-import-remove");
        button.type = "button";
        button.title = "Leave this out";
        button.setAttribute("aria-label", "Leave this out");
        button.innerHTML = '<span class="fas fa-xmark" aria-hidden="true"></span>';
        button.addEventListener("click", () => {
            // Matched by prefix, because a bundle's layers all came from one
            // pick and removing one of them means removing the run.
            state.picks = state.picks.filter(
                (pick) => pick !== path && !String(path).startsWith(pick));
            if (!state.picks.length) {
                state.proposal = null;
                render("pick");
                return;
            }
            inspect();
        });
        return button;
    }

    /**
     * One question, attached under the row it is about.
     *
     * A callout rather than an indented paragraph: a question here is a
     * failure of DETECTION, and it has to be findable among eight rows without
     * being alarming. It is never required -- `part("go")` is gated on layers
     * alone -- so the unanswered state is a warning-coloured rule and a word,
     * not a barrier.
     */
    function renderQuestion(question) {
        const row = el("div", "plx-import-question");
        const current = state.answers[question.id] ?? question.default;
        // The one question with no default: a store with several tables,
        // where guessing would silently give you a different set of cells.
        // Said out loud, with what happens if it is left alone.
        const unanswered = current === null || current === undefined;
        row.classList.toggle("is-unanswered", unanswered);

        const label = el("label", "plx-import-question-label", question.label);
        if (unanswered) {
            label.appendChild(el("span", "plx-import-badge", "unanswered"));
        }
        row.appendChild(label);

        if (question.kind === "select" || (question.options || []).length > 3) {
            const select = el("select", "plx-import-question-select");
            if (unanswered) {
                const placeholder = el("option", null, "Choose…");
                placeholder.value = "";
                placeholder.disabled = true;
                placeholder.selected = true;
                select.appendChild(placeholder);
            }
            (question.options || []).forEach((option) => {
                const item = el("option", null, option.label);
                item.value = option.value;
                if (option.value === current) item.selected = true;
                select.appendChild(item);
            });
            select.addEventListener("change", () => answer(question.id, select.value));
            label.htmlFor = select.id = `q_${cssId(question.id)}`;
            row.appendChild(select);
        } else {
            const group = el("div", "plx-import-question-choices");
            (question.options || []).forEach((option) => {
                const button = el("button", "plx-import-choice");
                button.type = "button";
                const chosen = option.value === current;
                // A dot rather than colour alone, so which one is chosen
                // survives a colour-blind reader and a screenshot.
                const dot = el("span", `fas ${chosen ? "fa-circle-dot" : "fa-circle"}`);
                dot.setAttribute("aria-hidden", "true");
                button.appendChild(dot);
                button.appendChild(el("span", null, option.label));
                button.classList.toggle("is-chosen", chosen);
                button.setAttribute("aria-pressed", chosen ? "true" : "false");
                button.addEventListener("click", () => answer(question.id, option.value));
                group.appendChild(button);
            });
            row.appendChild(group);
        }
        if (unanswered) {
            row.appendChild(el("p", "plx-import-question-hint",
                "The import goes ahead either way — Plexora asks again when a "
                + "tool needs it."));
        }
        return row;
    }

    //: "a, b and c". Its own function because both halves of the summary line
    //: need it and an Oxford-comma-less list is the one bit of prose here.
    function list(items) {
        if (items.length < 2) return items[0] || "";
        return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
    }

    function cssId(value) {
        return String(value).replace(/[^\w-]/g, "_");
    }

    function answer(id, value) {
        state.answers[id] = value;
        // Re-inspected rather than patched locally: an answer can change what
        // the other rows ARE -- picking a different reference re-registers
        // every layer against it -- and the server is the only thing that
        // knows that.
        inspect();
    }

    /**
     * Name and Dataset: both already answered, both editable.
     *
     * A two-column grid with its labels in the left column rather than two
     * cells side by side. Both values arrive filled in -- the name from the
     * files, the dataset from wherever the dialog was opened -- so this is a
     * place to CORRECT something, which is why it sits at the foot of the card
     * and not at the top of the dialog.
     */
    function renderNameRow(sample, index) {
        const row = el("div", "plx-import-meta");

        row.appendChild(el("span", "plx-import-meta-label", "Name"));
        const nameBox = el("input", "plx-import-name");
        nameBox.type = "text";
        nameBox.value = (index === 0 && state.name) || sample.name || "";
        nameBox.addEventListener("input", () => {
            if (index === 0) state.name = nameBox.value.trim();
        });
        row.appendChild(nameBox);

        row.appendChild(el("span", "plx-import-meta-label", "Dataset"));
        const datasetButton = el("button", "plx-import-dataset");
        datasetButton.type = "button";
        datasetButton.appendChild(el("span", null, state.dataset?.name
                                                  || "No dataset"));
        const chevron = el("span", "fas fa-chevron-down");
        chevron.setAttribute("aria-hidden", "true");
        datasetButton.appendChild(chevron);
        datasetButton.addEventListener("click", chooseDataset.bind(null, datasetButton));
        row.appendChild(datasetButton);

        return row;
    }

    async function chooseDataset(button) {
        let datasets = [];
        try {
            const response = await fetch(plexoraUrl("datasets"));
            const payload = await response.json();
            datasets = payload.datasets || [];
        } catch (error) { /* offered without the list */ }
        const picked = await window.PlexoraDatasetPicker.choose({
            datasets,
            count: 1,
            allowRoot: true,
            allowNew: true,
            title: "Put this sample in…",
            rootLabel: "No dataset",
        });
        if (!picked) return;
        if (picked.kind === "root") state.dataset = null;
        else if (picked.kind === "new") state.dataset = {new: picked.name, name: picked.name};
        else {
            const found = datasets.find((d) => d.id === picked.id);
            state.dataset = {id: picked.id, name: found?.name || picked.id};
        }
        button.firstChild.textContent = state.dataset?.name || "No dataset";
    }

    // -- state 3: importing -------------------------------------------------

    /**
     * Register the sample these picks make.
     *
     * @param replace - the name of an existing sample this rewrites. Sent as
     *   the NAME as well, because a name and a replacement that disagree are
     *   a request for a second copy under a deduplicated name -- which is the
     *   opposite of what "Re-import" means.
     */
    async function submit(replace) {
        if (!state || !state.picks.length) return;
        const target = scoped() ? "import/layers" : "import/sample";
        const payload = scoped()
            ? {sample: state.sample, paths: state.picks, answers: state.answers}
            : {
                paths: state.picks,
                answers: state.answers,
                name: replace || state.name || undefined,
                replace: replace || undefined,
                dataset: state.dataset || undefined,
            };
        render("importing");
        setStatus(scoped() ? "Adding…"
            : replace ? `Re-reading ${replace}…` : "Registering…");
        try {
            const response = await fetch(plexoraUrl(target), {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(payload),
            });
            const result = await response.json();
            if (response.status === 409 && result.suggestion) {
                // The name is taken. Offered rather than silently renamed:
                // somebody who typed a name meant it, and quietly filing their
                // import under `melanoma_2` is how two copies of one slide
                // happen.
                state.name = result.suggestion;
                render("proposal");
                setStatus(`${result.error} Using ${result.suggestion} instead — `
                          + "edit the name if you want another.", true);
                return;
            }
            if (!response.ok) throw new Error(result.error || "Import failed.");
            if (scoped()) return finishScoped(result);
            finishSample(result);
        } catch (error) {
            render("proposal");
            setStatus(error.message || "Import failed.", true);
        }
    }

    function finishSample(result) {
        state.result = result;
        state.registered = result.name;
        render("importing");
        setStatus(result.pending
            ? "Registered. Opening — the rest keeps building behind the viewer."
            : "Registered. Opening…");
        const go = part("go");
        go.disabled = false;
        go.textContent = "Open sample";
        go.onclick = () => {
            const target = result.redirect || `/${encodeURIComponent(result.name)}`;
            close();
            PlexoraRouter.go(target);
        };
        // The record exists, so the sample is openable now. Whatever is still
        // building keeps building and the viewer shows it the way it already
        // shows a converting mask.
        window.setTimeout(() => {
            if (!state || state.registered !== result.name) return;
            go.onclick();
        }, OPEN_AFTER_MS);
    }

    async function finishScoped(result) {
        setStatus("Added.");
        if (result.reload) {
            // A mask inserts the "Area" placeholder into imageData, and every
            // channel index on this page is wired into the GL pass. Said out
            // loud rather than reloading under the user's feet.
            setStatus("Added. Reloading, because a mask changes the channel list…");
            window.setTimeout(() => window.location.reload(), 600);
            return;
        }
        try {
            await window.__plexora?.adoptLayers?.();
        } catch (error) {
            console.error("importSample: could not adopt the new layers.", error);
        }
        close();
    }

    /**
     * The same rows, as the progress rail the rest of the app already uses.
     *
     * `.connect-steps` is what the connection wizard and the job panel draw a
     * multi-step wait with -- a mark per step that fills as it lands. Reused
     * rather than restated, because a user who has watched a node connect has
     * already learnt to read it, and because "waiting…" against eight
     * identical rows says nothing about which of them is moving.
     */
    function renderImporting() {
        const body = part("body");
        body.innerHTML = "";
        const layers = [];
        (state.proposal?.samples || []).forEach((sample) => {
            (sample.layers || []).forEach((layer) => layers.push(layer));
        });
        const steps = el("ul", "connect-steps plx-import-steps");
        state.bars = new Map();
        layers.forEach((layer) => {
            const step = el("li", "connect-step");
            step.appendChild(el("span", "connect-step-mark"));
            step.appendChild(el("span", "connect-step-label",
                                layer.label || layer.id));
            const stage = el("span", "plx-import-step-stage", "waiting");
            step.appendChild(stage);
            steps.appendChild(step);
            state.bars.set(layer.id, {step, stage});
        });
        body.appendChild(steps);
        part("add").hidden = true;
        part("go").disabled = true;
        setReason("");
        if (state.registered) watch(state.registered);
    }

    function watch(name) {
        if (state.watching) return;
        state.watching = true;
        const tick = async () => {
            if (!state || state.registered !== name) return;
            let document_ = null;
            try {
                const response = await fetch(
                    `${plexoraUrl("import/status")}?sample=${encodeURIComponent(name)}`);
                document_ = await response.json();
            } catch (error) { /* one missed tick */ }
            if (!state || state.registered !== name) return;
            Object.entries(document_?.layers || {}).forEach(([id, entry]) => {
                const line = state.bars?.get(id);
                if (!line) return;
                // The job document has three statuses and `pending` covers
                // two different things: a build that is running, and a layer
                // nothing has started on -- a modality whose plugin is not
                // installed, which parks at `stage: "waiting"` with a message
                // naming what would prepare it. Only the first of those is the
                // moving mark; the second is not progress and must not pulse.
                const done = entry.status === "ready";
                const failed = entry.status === "failed";
                const waiting = !done && !failed && entry.stage === "waiting";
                line.step.classList.toggle("is-done", done);
                line.step.classList.toggle("is-failed", failed);
                line.step.classList.toggle("is-active", !done && !failed && !waiting);
                line.stage.textContent = done ? "ready"
                    : failed ? (entry.error || "failed")
                    : waiting ? (entry.message || "not prepared")
                    : `${entry.stage_label || "preparing"}…`;
            });
            if (document_?.pending) window.setTimeout(tick, POLL_MS);
        };
        tick();
    }

    // -- rendering ----------------------------------------------------------

    function render(phase) {
        if (!state) return;
        state.phase = phase;
        dialog.dataset.phase = phase;
        if (phase === "pick") renderPick();
        else if (phase === "proposal") renderProposal();
        else renderImporting();
        paintSubtitle();
    }

    /**
     * The line under the title, which is the dialog's whole voice.
     *
     * In `pick` it says what to do; in `proposal` it says what pressing Import
     * would create; in `importing` it says what is happening. One line, and it
     * is why nothing inside the body has to repeat any of it.
     */
    function paintSubtitle() {
        const line = part("subtitle");
        if (!line) return;
        if (state.phase === "pick") {
            // The modality path is always scoped -- it is the requirements
            // modal asking for the one thing a tool needs -- so it names both
            // what to pick and which sample it joins.
            const kind = state.modality
                ? state.modality.replace(/_/g, " ") : null;
            line.textContent = kind && scoped()
                ? `Pick the ${kind} data for ${state.sample}.`
                : kind
                    ? `Pick the ${kind} data.`
                    : scoped()
                        ? `Pick the files to add as layers of ${state.sample}.`
                        : "Point Plexora at a file or a folder. It works out "
                          + "what each one is.";
        } else if (state.phase === "proposal") {
            line.textContent = state.proposal
                ? summarize(state.proposal) : "Reading…";
        } else {
            line.textContent = state.registered
                ? `Opening ${state.registered}…`
                : scoped() ? `Adding to ${state.sample}…` : "Registering…";
        }
    }

    function basename(path) {
        return String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop();
    }

    // -- the buttons that open it ------------------------------------------

    /**
     * Every "Import Sample..." on every page, bound in one place.
     *
     * By id rather than by each page's own controller, because the same
     * dialog is opened from four surfaces that otherwise share nothing: the
     * library header, its empty state, the home page's footer and the navbar
     * menu. A controller per surface would be four copies of one line.
     *
     * The home page's is `sample-import-home`, and it is a FOOTER link rather
     * than that page's primary action: the landing page opens one pick in one
     * gesture through `/import/sample` itself (views/quickViewLanding.js), and
     * this is where somebody goes when the pick needs a decision first -- several
     * samples in one folder, a name, a dataset, a question answered.
     *
     * `PlexoraPage.register` re-runs on every routed navigation, and binding is
     * idempotent (`dataset.bound`), so a page visited twice does not open two
     * dialogs per click.
     */
    if (typeof PlexoraPage !== "undefined") {
        PlexoraPage.register(function () {
            ["sample-import", "sample-import-empty", "sample-import-home",
             "sample-import-menu"].forEach((id) => {
                const button = document.getElementById(id);
                if (!button || button.dataset.importBound) return;
                button.dataset.importBound = "1";
                button.addEventListener("click", (event) => {
                    event.preventDefault();
                    open({
                        // The library page re-lists when the dialog closes, so
                        // a sample imported into the folder currently being
                        // looked at appears without a reload.
                        onClose: () => window.PlexoraOpenProject?.refresh?.(),
                    });
                });
            });
        });
    }

    return {open, close};
})();
