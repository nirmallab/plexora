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
    //: While the POST itself runs. Its steps move every few seconds.
    const REGISTER_POLL_MS = 700;

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
        visium_hd: "a Visium HD run",
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
                    <span class="fas fa-plus" aria-hidden="true"></span>
                    <span data-role="add-text">Add more samples</span>
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
        // The FOOTER's add, which is "another sample" and carries no intent:
        // a file that belongs to a sample already on screen is added on that
        // sample's own card, where it can say which sample it joins. This is
        // the way back to an empty picker.
        node.querySelector('[data-role="add"]').addEventListener("click", () => {
            state.adding = null;
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
     * @param paths - files or folders to start with: what was dropped on the
     *   desktop app's window, or opened from Explorer/Finder.
     */
    function open(options) {
        options = options || {};
        if (dialog) close();
        state = {
            phase: "pick",
            picks: [],
            //: The picks the proposal on screen was computed FROM, so a row's
            //: `pick` index always resolves against the array it indexes.
            //: `state.picks` can be one ahead of it -- a removal edits it and
            //: re-renders the old proposal while the next inspection is in
            //: flight -- and indexing THAT is how a second fast ✕ took out the
            //: file below the one it was on.
            picked: [],
            proposal: null,
            answers: {},
            sample: options.sample || null,
            modality: options.modality || null,
            dataset: options.dataset || null,
            //: Per sample, keyed by `SampleProposal.key`: `{name, dataset}`,
            //: present only where the user has said something. Keyed by what
            //: the sample IS rather than by its position, because an added
            //: mask can merge two cards into one and renumber the rest.
            meta: {},
            //: `{key, role}` while a card's inline "choose a file" row is
            //: open. One at a time: two open rows on one screen is two places
            //: a path could go and no way to tell which.
            adding: null,
            node: null,
            location: null,
            onClose: options.onClose || null,
        };
        dialog = build();
        part("title").textContent = scoped()
            ? `Add a layer to ${state.sample}` : "Import sample";
        part("go").textContent = scoped() ? "Add layers" : "Import sample";
        // "Add more samples" is a promise the scoped dialog cannot keep: it
        // adds layers to one sample that already exists.
        part("add-text").textContent = scoped()
            ? "Add more files" : "Add more samples";
        mountLocation();
        render("pick");
        dialog.showModal();
        if (Array.isArray(options.paths) && options.paths.length) addPaths(options.paths);
        return dialog;
    }

    /** Several picks at once, inspected once. */
    function addPaths(paths) {
        paths.forEach((path) => {
            if (path && !state.picks.includes(path)) state.picks.push(path);
        });
        state.adding = null;
        inspect();
    }

    /**
     * Paths dropped on the desktop app's window: into the dialog if it is
     * open, a new dialog otherwise. The shell delivers a drop as PATHS, so
     * nothing is uploaded and nothing is too big to drop.
     */
    function dropPaths(paths) {
        if (!Array.isArray(paths) || !paths.length) return;
        if (dialog && state && (state.phase === "pick" || state.phase === "proposal")) {
            addPaths(paths);
            return;
        }
        open({paths});
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
        // Whatever a card was being added to, this is not it: the pick state's
        // drop zone and paste box add loose files.
        state.adding = null;
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

        // Coming back here from a proposal to add another sample. Without a
        // way back, the only route to the cards is to pick a file -- so
        // pressing Add and changing your mind meant losing sight of what you
        // already had.
        if (state.picks.length && state.proposal) {
            const back = el("button", "plx-import-back",
                            "← Back to what was found");
            back.type = "button";
            back.addEventListener("click", () => render("proposal"));
            body.appendChild(back);
        }

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
        // In the desktop app the same drop also arrives natively, as paths
        // (desktopBridge.js -> dropPaths). Uploading the bytes as well would
        // import it twice, and lose where it lives.
        if (window.PlexoraDesktop) return;
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
        // Read before the first await: the card's inline row is torn down by
        // the re-render that follows, and `state.adding` with it.
        const intent = state.adding;
        setStatus("Uploading…");
        for (const file of files) {
            const form = new FormData();
            form.append("file", file);
            try {
                const response = await fetch(plexoraUrl("upload_data_file"),
                                             {method: "POST", body: form});
                const result = await response.json();
                if (!response.ok || !result.path) throw new Error(result.error || "");
                if (!state.picks.includes(result.path)) state.picks.push(result.path);
                if (intent) {
                    const name = basename(result.path);
                    state.answers[`sample-for:${name}`] = intent.key;
                    state.answers[`added-as:${name}`] = intent.role;
                }
            } catch (error) {
                setStatus(`Could not take ${file.name}.`, true);
                return;
            }
        }
        state.adding = null;
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
        //: Read at the moment the browser OPENS, because the pick that comes
        //: back belongs to the card the user pressed -- and a card's inline
        //: row is torn down by the re-render that follows.
        const intent = state.adding;
        browseForPath({
            mode,
            filter: "any",
            node: browseNode(),
            onPicked: (path) => {
                clearTimeout(settle);
                setStatus(null);
                addPick(path, intent);
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

    /**
     * Take a path, and say what it was added AS if a card's action added it.
     *
     * @param intent - `{key, role}` from the card whose slot was pressed:
     *   which sample the file joins, and which modality the user said it is.
     *   Both travel in `answers` rather than in a second array beside `paths`
     *   -- an array would have to stay aligned with a list the server filters
     *   and the user removes from, and `answers` survives re-inspection,
     *   reaches the Python API unchanged, and has nothing to line up with.
     *   Null for a loose pick, which is the whole of the old behaviour.
     */
    function addPick(path, intent) {
        const node = browseNode();
        // A path on another machine is addressed rather than read: the node is
        // the only process that can open it. A web address is left exactly as
        // typed either way -- it already names the machine it is on, and
        // wrapping an `s3://` URL in `node://` would ask a data node to open a
        // path it has never heard of instead of asking this server to read
        // the bucket directly.
        const remote = window.PlexoraLocators && window.PlexoraLocators.isRemoteLocator(path);
        const address = (node && !remote) ? `node://${node}/${path}` : path;
        // Deduplicated: the same file picked twice is one pick, and two rows
        // over one file is two ✕ buttons that each look broken.
        if (!state.picks.includes(address)) state.picks.push(address);
        if (intent) {
            const name = basename(path);
            state.answers[`sample-for:${name}`] = intent.key;
            state.answers[`added-as:${name}`] = intent.role;
        }
        state.adding = null;
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
        //: The exact array this proposal's `pick` indices will refer to.
        const sent = state.picks.slice();
        try {
            const response = await fetch(plexoraUrl("import/inspect"), {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    paths: sent,
                    answers: state.answers,
                    sample: state.sample || undefined,
                }),
            });
            const proposal = await response.json();
            if (token !== inspectToken || !state) return;
            state.proposal = proposal;
            state.picked = sent;
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
            row.appendChild(removeButton(pickOf(entry)));
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

        if (!scoped()) block.appendChild(renderCardHead(sample, index, total));

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
            // This card, not the screen: the other samples on it are not being
            // re-imported and must not be registered by a press on this one.
            again.addEventListener("click", () => submit(sample.existing, index));
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
            const remoteOptions = renderRemoteOptions(layer);
            if (remoteOptions) block.appendChild(remoteOptions);
            questionsFor(sample, `layer:${layer.id}`).forEach((question) => {
                block.appendChild(renderQuestion(question));
            });
        });

        questionsFor(sample, "sample").forEach((question) => {
            block.appendChild(renderQuestion(question));
        });

        block.appendChild(renderCardActions(sample));
        if (state.adding && state.adding.key === sample.key) {
            block.appendChild(renderInlinePick(sample));
        }
        return block;
    }

    //: What each missing modality's slot says, and which set of format
    //: examples its picker shows (browsePicker's KIND_EXAMPLES).
    const SLOT = {
        mask: {label: "Add segmentation mask", examples: "mask"},
        table: {label: "Add data", examples: "data"},
        layer: {label: "Add layer", examples: "sample"},
    };

    /**
     * The card's own actions: what this sample still wants, offered here.
     *
     * A sample is an image and the things registered against it -- a mask, a
     * cell table, whatever else -- and the only way to add any of them used to
     * be a generic "Add files" in the footer, which put the new pick beside
     * the others as a candidate for a sample of its own. A mask added that way
     * routinely became a second card, and the user was left assembling by hand
     * something the screen could simply have offered.
     *
     * Driven by `sample.missing`, so a card that already has both offers only
     * "+ Add layer" and a card with neither offers all three, in the order the
     * record stores them.
     */
    function renderCardActions(sample) {
        const strip = el("div", "plx-import-card-actions");
        const wanted = [...(sample.missing || []), "layer"];
        wanted.forEach((role) => {
            const spec = SLOT[role];
            if (!spec) return;
            const open = state.adding && state.adding.key === sample.key
                         && state.adding.role === role;
            const button = el("button", "plx-import-slot");
            button.type = "button";
            button.classList.toggle("is-open", Boolean(open));
            button.setAttribute("aria-expanded", open ? "true" : "false");
            const glyph = el("span", `fas ${open ? "fa-xmark" : "fa-plus"}`);
            glyph.setAttribute("aria-hidden", "true");
            button.appendChild(glyph);
            button.appendChild(el("span", null, spec.label));
            button.addEventListener("click", () => {
                // A second press on the open slot closes it, which is the only
                // way out of a row nobody meant to open.
                state.adding = open ? null : {key: sample.key, role};
                render("proposal");
            });
            strip.appendChild(button);
        });
        return strip;
    }

    /**
     * The picker, opened INSIDE the card rather than over it.
     *
     * The card is the context: which sample this file joins is the one thing
     * the user must not have to remember, and a modal picker takes it off the
     * screen at the moment they choose. The same two halves the pick state
     * uses, the same paste box, the same drop target -- narrower, and with
     * format examples that match the slot that opened it.
     */
    function renderInlinePick(sample) {
        const spec = SLOT[state.adding.role] || SLOT.layer;
        const wrap = el("div", "plx-import-inline-pick");

        const panel = buildSplitControl(spec.examples, pickWith,
                                        {file: "Choose a file",
                                         directory: "Choose a folder"});
        panel.addEventListener("keydown", (event) => stepBetweenHalves(panel, event));
        wrap.appendChild(panel);
        // Held so they can be disabled while a file browser is opening: the
        // same guard the pick state uses, against a second press opening a
        // second native dialog.
        state.halves = [...panel.querySelectorAll(".browse-kind-half")];

        const row = el("div", "plx-import-path");
        const box = el("input", "plx-import-path-input");
        box.type = "text";
        box.placeholder = `…or paste a path for ${sample.name || "this sample"}`;
        box.spellcheck = false;
        box.addEventListener("keydown", (event) => {
            if (event.key !== "Enter") return;
            const typed = box.value.trim();
            if (!typed) return;
            const intent = state.adding;
            box.value = "";
            addPick(typed, intent);
        });
        row.appendChild(box);
        wrap.appendChild(row);

        wrap.addEventListener("dragover", (event) => {
            event.preventDefault();
            wrap.classList.add("is-over");
        });
        wrap.addEventListener("dragleave", () => wrap.classList.remove("is-over"));
        wrap.addEventListener("drop", onDrop);
        return wrap;
    }

    /**
     * The card's title line: which sample, what it is called, where it goes.
     *
     * Name and Dataset used to be a labelled input and a full-width button in
     * a two-column grid at the foot of every card -- two form fields, drawn at
     * the weight of the thing being imported, for two answers that arrive
     * already filled in. They are metadata about a sample, so they are drawn
     * as metadata: muted text that becomes editable where it stands. The
     * ordinal is only there when there is more than one card, because "Sample
     * 1" over the only sample on screen is a number with nothing to tell apart.
     */
    function renderCardHead(sample, index, total) {
        const head = el("div", "plx-import-card-head");
        if (total > 1) {
            head.appendChild(el("span", "plx-import-card-ordinal",
                                `Sample ${index + 1}`));
            head.appendChild(el("span", "plx-import-card-dot", "·"));
        }

        const meta = state.meta[sample.key] || {};
        const name = el("button", "plx-import-name-inline",
                        meta.name || sample.name || "Untitled");
        name.type = "button";
        name.title = "Click to rename";
        name.addEventListener("click", () => editName(name, sample));
        head.appendChild(name);

        head.appendChild(el("span", "plx-import-card-dot", "·"));
        const dataset = el("button", "plx-import-dataset-inline");
        dataset.type = "button";
        dataset.title = "Click to choose a dataset";
        dataset.appendChild(el("span", null, datasetFor(sample)?.name
                                             || "No dataset"));
        const chevron = el("span", "fas fa-chevron-down");
        chevron.setAttribute("aria-hidden", "true");
        dataset.appendChild(chevron);
        dataset.addEventListener("click", () => chooseDataset(dataset, sample));
        head.appendChild(dataset);
        return head;
    }

    /** This sample's dataset: its own answer, or the dialog's default. */
    function datasetFor(sample) {
        const meta = state.meta[sample.key];
        return meta && "dataset" in meta ? meta.dataset : state.dataset;
    }

    /**
     * The muted name, edited where it stands.
     *
     * An input with the same metrics as the text it replaces, so nothing moves
     * when it appears -- which is what makes it read as the same line rather
     * than as a form opening. Enter and blur commit; Escape puts back what was
     * there, because a name typed over by accident is otherwise unrecoverable
     * once the original is gone.
     */
    function editName(button, sample) {
        const before = button.textContent;
        const box = el("input", "plx-import-name-edit");
        box.type = "text";
        box.value = before === "Untitled" ? "" : before;
        box.spellcheck = false;
        box.setAttribute("aria-label", "Sample name");
        let settled = false;
        const commit = (keep) => {
            if (settled) return;
            settled = true;
            const typed = box.value.trim();
            if (keep && typed && typed !== before) {
                state.meta[sample.key] = Object.assign(
                    {}, state.meta[sample.key], {name: typed});
            }
            button.textContent = (keep && typed) ? typed : before;
            box.replaceWith(button);
            button.focus();
        };
        box.addEventListener("keydown", (event) => {
            if (event.key === "Enter") { event.preventDefault(); commit(true); }
            else if (event.key === "Escape") { event.preventDefault(); commit(false); }
        });
        box.addEventListener("blur", () => commit(true));
        button.replaceWith(box);
        box.focus();
        box.select();
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
        const nameLine = el("span", "plx-import-row-name", layer.label || layer.id);
        if (window.PlexoraLocators && window.PlexoraLocators.isRemoteLocator(layer.src)) {
            // Read from a web address rather than this disk -- worth a glance
            // on a screen that is otherwise all local files, and not a
            // warning: an https layer is exactly as read-only-safe as a local
            // one, only slower the first time.
            nameLine.appendChild(el("span", "plx-import-badge is-remote", "Web"));
        }
        text.appendChild(nameLine);
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
        const pick = pickOf(layer);
        if (pick) row.appendChild(removeButton(pick));
        return row;
    }

    //: Schemes a bucket sits under -- the ones that can need an endpoint, an
    //: anonymous read or a named profile before they will open at all. `https`
    //: is deliberately not here: it is either public or it is not reachable,
    //: and there is no field on this form that changes which.
    const CONFIGURABLE_REMOTE = ["s3", "gs", "gcs", "az", "abfs", "abfss"];

    /**
     * "Set endpoint / access…", collapsed under a layer read from a bucket.
     *
     * The same options Settings > Web data keeps, reached from the moment
     * they are first needed instead of asking somebody to leave the dialog,
     * remember which bucket failed, and go find the address book. Saving
     * re-runs `inspect()`, which is the point: a store that could not be read
     * for want of a credential is read again with the one just given it.
     *
     * Returns null for anything this has nothing to offer -- a local file, a
     * `node://` locator, an `https` layer, or a bucket URL with no host to
     * key a prefix by.
     */
    function renderRemoteOptions(layer) {
        const locators = window.PlexoraLocators;
        const scheme = locators && locators.remoteScheme(layer.src);
        if (!scheme || CONFIGURABLE_REMOTE.indexOf(scheme) === -1) return null;
        let host = "";
        try { host = new URL(layer.src).host; } catch (error) { /* no prefix to key by */ }
        if (!host) return null;
        const prefix = `${scheme}://${host}/`;

        const row = el("div", "plx-import-question");
        const label = el("div", "plx-import-question-label");
        const toggle = el("button", "plx-import-formats-link", "Set endpoint / access…");
        toggle.type = "button";
        toggle.setAttribute("aria-expanded", "false");
        label.appendChild(toggle);
        row.appendChild(label);

        const form = el("div", "plx-import-remote-form");
        form.hidden = true;

        const endpointField = el("label", "plx-import-remote-field");
        endpointField.appendChild(el("span", null, "Endpoint URL"));
        const endpoint = el("input", "plx-import-path-input");
        endpoint.type = "text";
        endpoint.placeholder = "https://s3.example.org (leave blank for AWS)";
        endpoint.spellcheck = false;
        endpointField.appendChild(endpoint);
        form.appendChild(endpointField);

        const anonField = el("label", "plx-import-remote-check");
        const anon = document.createElement("input");
        anon.type = "checkbox";
        anon.checked = true;
        anonField.appendChild(anon);
        anonField.appendChild(el("span", null, "Anonymous — no credentials needed"));
        form.appendChild(anonField);

        const profileField = el("label", "plx-import-remote-field");
        profileField.appendChild(el("span", null, "Profile"));
        const profile = el("input", "plx-import-path-input");
        profile.type = "text";
        profile.placeholder = "default";
        profileField.appendChild(profile);
        form.appendChild(profileField);

        // Disabled rather than hidden: an anonymous read has no profile, and a
        // field that vanished when it stopped applying would move everything
        // under it -- this only greys out, in place.
        function syncProfile() {
            profile.disabled = anon.checked;
            profileField.classList.toggle("is-disabled", anon.checked);
        }
        anon.addEventListener("change", syncProfile);
        syncProfile();

        form.appendChild(el("p", "plx-import-question-hint",
            "Options for private buckets or S3-compatible servers. Keys are "
            + "never stored here — Plexora uses your AWS/Google/Azure "
            + "credentials from the environment."));

        const error = el("p", "plx-import-remote-error");
        error.hidden = true;
        form.appendChild(error);

        const save = el("button", "plx-button plx-button-primary", "Save");
        save.type = "button";
        save.addEventListener("click", async () => {
            error.hidden = true;
            save.disabled = true;
            try {
                const response = await fetch(plexoraUrl("settings/webdata/options"), {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        prefix,
                        endpoint_url: endpoint.value.trim() || undefined,
                        anon: anon.checked,
                        profile: anon.checked ? undefined : (profile.value.trim() || undefined),
                    }),
                });
                const result = await response.json();
                if (!response.ok || result.error) {
                    throw new Error(result.error || "Could not save that.");
                }
                inspect();
            } catch (err) {
                error.textContent = err.message || "Could not save that.";
                error.hidden = false;
            } finally {
                save.disabled = false;
            }
        });
        form.appendChild(save);

        // Filled in from the address book the first time this is opened --
        // not on every render, which would be one request per bucket layer on
        // a screen that may have several.
        let filled = false;
        toggle.addEventListener("click", () => {
            form.hidden = !form.hidden;
            toggle.setAttribute("aria-expanded", String(!form.hidden));
            if (!form.hidden && !filled) {
                filled = true;
                prefillRemoteOptions(prefix, {endpoint, anon, profile, syncProfile});
            }
        });

        row.appendChild(form);
        return row;
    }

    /** Fill a just-opened access form from the longest address-book prefix
     *  this layer's own prefix starts with -- the same rule the server
     *  applies when it reads the book back for an open. Left blank on any
     *  failure, which is exactly what an unopened form already looks like. */
    async function prefillRemoteOptions(prefix, fields) {
        try {
            const response = await fetch(plexoraUrl("settings/webdata/options"));
            const body = await response.json();
            const options = body.options || [];
            let best = null;
            options.forEach((option) => {
                const saved = String(option.prefix || "");
                if (saved && prefix.startsWith(saved)
                    && (!best || saved.length > best.prefix.length)) {
                    best = option;
                }
            });
            if (!best) return;
            fields.endpoint.value = best.endpoint_url || "";
            // Absent means it was never set, which this form treats the same
            // as ticked -- see the callout's own default.
            fields.anon.checked = best.anon !== false;
            fields.profile.value = best.profile || "";
            fields.syncProfile();
        } catch (error) { /* offered empty, which is what a blank form is */ }
    }

    /**
     * The path a row came from, as the caller itself sent it.
     *
     * NOT `layer.src`, and that was the bug behind "Remove does nothing".
     * A row's `src` is where the data will be READ from, which is not the
     * string that was picked: a browsed path on a node comes back as
     * `node://o2/<derived resource id>`, and a pasted `~/slide.tif` comes back
     * expanded. Filtering the pick list by `src` matched neither, so the ✕ on
     * a remote image ran, removed nothing, re-inspected, and drew the same row
     * again.
     *
     * `pick` is the index of the entry in the array that produced this
     * proposal, which `state.picked` holds -- so a bundle's six rows all
     * resolve to the one folder that was picked, and removing any of them
     * removes the run.
     */
    function pickOf(row) {
        if (!row || row.pick === null || row.pick === undefined) return null;
        return state.picked[row.pick] ?? null;
    }

    function removeButton(pick) {
        const button = el("button", "plx-import-remove");
        button.type = "button";
        button.title = "Leave this out";
        button.setAttribute("aria-label", "Leave this out");
        button.innerHTML = '<span class="fas fa-xmark" aria-hidden="true"></span>';
        button.addEventListener("click", () => removePick(pick));
        return button;
    }

    /**
     * Take one pick back out, and forget everything that was said about it.
     *
     * The answers go with it, or a file removed and picked again arrives
     * carrying the role and the sample it had last time -- including a
     * `sample-for` naming a card that may no longer exist.
     */
    function removePick(pick) {
        if (!pick) return;
        const name = basename(pick);
        state.picks = state.picks.filter((entry) => entry !== pick);
        delete state.answers[`added-as:${name}`];
        delete state.answers[`sample-for:${name}`];
        delete state.answers[`mask-or-image:${name}`];
        if (!state.picks.length) {
            state.proposal = null;
            state.picked = [];
            state.adding = null;
            render("pick");
            return;
        }
        inspect();
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
     * Where this sample goes in the library. Per sample, not per dialog.
     *
     * A bulk import of six slides is one pick and six samples, and they do not
     * all have to land in the same folder. The first answer is the dialog's
     * default, which is where it was opened from; anything the user says here
     * is remembered against that sample's `key`.
     */
    async function chooseDataset(button, sample) {
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
        if (!picked || !state) return;
        let chosen = null;
        if (picked.kind === "root") chosen = null;
        else if (picked.kind === "new") chosen = {new: picked.name, name: picked.name};
        else {
            const found = datasets.find((d) => d.id === picked.id);
            chosen = {id: picked.id, name: found?.name || picked.id};
        }
        state.meta[sample.key] = Object.assign(
            {}, state.meta[sample.key], {dataset: chosen});
        button.firstChild.textContent = chosen?.name || "No dataset";
    }

    // -- state 3: importing -------------------------------------------------

    /**
     * Register the samples these picks make. One POST each.
     *
     * `/import/sample` registers ONE sample -- `proposal.samples[index]` --
     * and this used to post once whatever was on screen, so "Import 3 samples"
     * created one project and quietly dropped the other two. Each POST carries
     * its sample's `key` beside the index, so a request composed against an
     * older reading of the files is refused rather than registering whichever
     * sample now happens to sit at that position.
     *
     * @param replace - the name of an existing sample this rewrites. Sent as
     *   the NAME as well, because a name and a replacement that disagree are
     *   a request for a second copy under a deduplicated name -- which is the
     *   opposite of what "Re-import" means.
     * @param only - the index of the single sample to register. The Re-import
     *   button acts on the card it is on, not on the screen.
     */
    async function submit(replace, only) {
        if (!state || !state.picks.length) return;
        if (scoped()) return submitScoped();

        // No proposal yet means Import was pressed before the first inspection
        // landed. One POST at index 0 and no key, which is what this did
        // before it knew about several samples -- and the server re-inspects
        // anyway, so the files still decide.
        const samples = state.proposal?.samples || [{key: "", name: ""}];
        const targets = (only === null || only === undefined)
            ? samples.map((sample, at) => ({sample, at}))
            : [{sample: samples[only], at: only}];
        if (!targets.length || !targets[0].sample) return;

        render("importing");
        setStatus(replace ? `Re-reading ${replace}…`
            : targets.length > 1 ? `Registering ${targets.length} samples…`
            : "Registering…");
        const results = [];
        for (const {sample, at} of targets) {
            const meta = state.meta[sample.key] || {};
            const dataset = datasetFor(sample);
            let result = null;
            // The POST does the slow part -- tiling the image, converting a
            // 10x matrix -- before the sample has a name to poll by, so the
            // request names itself and the rail reads that while it runs.
            const token = `imp-${Date.now().toString(36)}-`
                + Math.random().toString(36).slice(2, 10);
            const stop = watchRegistration(token, at);
            try {
                const response = await fetch(plexoraUrl("import/sample"), {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        paths: state.picks,
                        answers: state.answers,
                        index: at,
                        key: sample.key || undefined,
                        name: replace || meta.name || undefined,
                        replace: replace || undefined,
                        dataset: dataset || undefined,
                        token,
                    }),
                });
                result = await response.json();
                stop();
                if (response.status === 409 && result.suggestion) {
                    // The name is taken. Offered rather than silently renamed:
                    // somebody who typed a name meant it, and quietly filing
                    // their import under `melanoma_2` is how two copies of one
                    // slide happen. Stored against the sample it belongs to,
                    // so it is the line the user reads that changes.
                    state.meta[sample.key] = Object.assign(
                        {}, state.meta[sample.key], {name: result.suggestion});
                    render("proposal");
                    setStatus(`${result.error} Using ${result.suggestion} `
                              + "instead — edit the name if you want another.",
                              true);
                    return;
                }
                if (!response.ok) throw new Error(result.error || "Import failed.");
            } catch (error) {
                stop();
                // Whatever already landed stays landed -- those projects
                // exist -- so the message names how far it got.
                render("proposal");
                setStatus(results.length
                    ? `${error.message || "Import failed."} ${results.length} `
                      + `of ${targets.length} were imported.`
                    : (error.message || "Import failed."), true);
                return;
            }
            results.push(result);
        }
        finishSamples(results);
    }

    async function submitScoped() {
        render("importing");
        setStatus("Adding…");
        try {
            const response = await fetch(plexoraUrl("import/layers"), {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({sample: state.sample, paths: state.picks,
                                      answers: state.answers}),
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || "Import failed.");
            return finishScoped(result);
        } catch (error) {
            render("proposal");
            setStatus(error.message || "Import failed.", true);
        }
    }

    /**
     * What happens once the records exist.
     *
     * One sample opens itself, which is the whole point of the dialog: the
     * record is there, and whatever is still building keeps building behind
     * the viewer, which already knows how to wait. SEVERAL do not -- opening
     * one of six is a choice nobody made, and it would take the other five's
     * progress off the screen -- so the rails stay up and the primary offers
     * the first by name.
     */
    function finishSamples(results) {
        state.results = results;
        state.registered = results.map((result) => result.name);
        render("importing");
        const pending = results.some((result) => result.pending);
        const go = part("go");
        go.disabled = false;
        if (results.length === 1) {
            setStatus(pending
                ? "Registered. Opening — the rest keeps building behind the viewer."
                : "Registered. Opening…");
            go.textContent = "Open sample";
            go.onclick = () => openResult(results[0]);
            // The record exists, so the sample is openable now.
            window.setTimeout(() => {
                if (!state || state.results !== results) return;
                go.onclick();
            }, OPEN_AFTER_MS);
            return;
        }
        setStatus(pending
            ? `${results.length} samples registered — what is still building `
              + "carries on without this window."
            : `${results.length} samples registered.`);
        go.textContent = `Open ${results[0].name}`;
        go.onclick = () => openResult(results[0]);
    }

    function openResult(result) {
        const target = result.redirect || `/${encodeURIComponent(result.name)}`;
        close();
        PlexoraRouter.go(target);
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
        const samples = state.proposal?.samples || [];
        state.bars = new Map();
        samples.forEach((sample, at) => {
            // A heading per sample once there is more than one, or six rails
            // run together into one list and nothing says which slide a
            // failing mask belongs to.
            if (samples.length > 1) {
                body.appendChild(el("div", "plx-import-steps-head",
                                    state.registered?.[at] || sample.name));
            }
            const steps = el("ul", "connect-steps plx-import-steps");
            (sample.layers || []).forEach((layer) => {
                // A note is recorded, not prepared: a rail row for it would
                // say "waiting" for something that never starts.
                if (layer.role === "note") return;
                const step = el("li", "connect-step");
                step.appendChild(el("span", "connect-step-mark"));
                step.appendChild(el("span", "connect-step-label",
                                    layer.label || layer.id));
                const stage = el("span", "plx-import-step-stage", "waiting");
                step.appendChild(stage);
                const bar = el("span", "plx-import-step-bar");
                const fill = el("span", "plx-import-step-fill");
                bar.appendChild(fill);
                bar.hidden = true;
                step.appendChild(bar);
                steps.appendChild(step);
                // Keyed by SAMPLE and layer: two slides in one import both
                // hold a layer called `image`, and one map would have the
                // second sample's progress paint over the first's.
                const line = {step, stage, bar, fill};
                state.bars.set(`${at}:${layer.id}`, line);
                // The status document names the mask by its job, not by the
                // proposal row it came from.
                if (layer.role === "mask") state.bars.set(`${at}:__mask__`, line);
            });
            body.appendChild(steps);
        });
        part("add").hidden = true;
        part("go").disabled = true;
        setReason("");
        (state.registered || []).forEach((name, at) => watch(name, at));
    }

    function watch(name, at) {
        state.watching = state.watching || new Set();
        if (state.watching.has(name)) return;
        state.watching.add(name);
        const live = () => Boolean(state) && (state.registered || []).includes(name);
        let wasPending = false;
        const tick = async () => {
            if (!live()) return;
            let document_ = null;
            try {
                const response = await fetch(
                    `${plexoraUrl("import/status")}?sample=${encodeURIComponent(name)}`);
                document_ = await response.json();
            } catch (error) { /* one missed tick */ }
            if (!live()) return;
            paintSteps(at, document_);
            if (document_?.pending) {
                wasPending = true;
                window.setTimeout(tick, POLL_MS);
            } else if (wasPending && document_) {
                window.PlexoraDesktop?.notifyIfAway({
                    title: `${name} is ready`,
                    body: "Everything in the sample has finished preparing.",
                });
            }
        };
        tick();
    }

    /**
     * The rail while `/import/sample` is still running, read by its token.
     *
     * Polled faster than `watch`, because what it shows is the wait somebody
     * is staring at. Returns the stop function the POST calls when it
     * answers; `watch` takes over from there, by the sample's name.
     */
    function watchRegistration(token, at) {
        let stopped = false;
        const tick = async () => {
            if (stopped || !state) return;
            let document_ = null;
            try {
                const response = await fetch(
                    `${plexoraUrl("import/status")}?token=${encodeURIComponent(token)}`);
                document_ = await response.json();
            } catch (error) { /* one missed tick */ }
            if (stopped || !state) return;
            if (document_?.registering) paintSteps(at, document_);
            window.setTimeout(tick, REGISTER_POLL_MS);
        };
        window.setTimeout(tick, REGISTER_POLL_MS);
        return () => { stopped = true; };
    }

    function paintSteps(at, document_) {
        Object.entries(document_?.layers || {}).forEach(([id, entry]) => {
            const line = state.bars?.get(`${at}:${id}`);
            if (!line) return;
            // The job document has three statuses and `pending` covers
            // two different things: a build that is running, and a layer
            // nothing has started on -- a modality whose plugin is not
            // installed, or a build that starts once the sample exists, which
            // park at `stage: "waiting"` with a message saying why. Only the
            // first of those is the moving mark; the second is not progress
            // and must not pulse.
            const done = entry.status === "ready";
            const failed = entry.status === "failed";
            const waiting = !done && !failed
                && (entry.stage === "waiting" || !entry.stage);
            const active = !done && !failed && !waiting;
            line.step.classList.toggle("is-done", done);
            line.step.classList.toggle("is-failed", failed);
            line.step.classList.toggle("is-active", active);
            const percent = Math.max(0, Math.min(100, Number(entry.progress) || 0));
            line.stage.textContent = done ? "ready"
                : failed ? (entry.error || "failed")
                : waiting ? (entry.message || "waiting")
                : `${entry.stage_label || "preparing"}…`
                  + (percent ? ` ${percent}%` : "");
            // A bar only where there is a number to show: a step that has
            // not reported one keeps the pulsing mark and nothing else.
            if (line.bar) {
                line.bar.hidden = !(active && percent > 0);
                line.fill.style.width = `${percent}%`;
            }
        });
    }

    // -- rendering ----------------------------------------------------------

    function render(phase) {
        if (!state) return;
        state.phase = phase;
        dialog.dataset.phase = phase;
        // The Local/Remote switch is hidden outside `pick`, because there is
        // nothing to browse from a list of results -- except while a card's
        // inline picker is open, which browses exactly as the pick state does
        // and would otherwise reach whichever machine was chosen last with no
        // way to see or change it.
        if (state.adding && phase === "proposal") dialog.dataset.adding = "1";
        else delete dialog.dataset.adding;
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
            const names = state.registered || [];
            line.textContent = names.length > 1
                ? `${names.length} samples registered.`
                : names.length
                    ? `Opening ${names[0]}…`
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

    return {open, close, dropPaths};
})();
