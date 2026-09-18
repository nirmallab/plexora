/**
 * Import Sample: one dialog, three states, no wizard.
 *
 * The whole of importing data into Plexora. Somebody presses one button,
 * points at files or a folder, sees what Plexora found, and presses Import.
 * There are no format tabs, no role-labelled fields and no second page.
 *
 *   pick       Local/Remote, Select File / Select Folder, or paste a path.
 *   proposal   One row per detected layer, with a name and a dataset. A
 *              question appears UNDER the row it concerns, with its default
 *              already chosen, and never blocks the Import button.
 *   importing  The same rows become progress lines, and the sample opens as
 *              soon as its record exists -- whatever is still building keeps
 *              building behind the viewer, which already knows how to wait.
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
                <h2 class="plx-dialog-title" data-role="title">Import Sample</h2>
                <button class="plx-picker-close" type="button" data-role="close"
                        title="Close" aria-label="Close">
                    <span class="fas fa-xmark" aria-hidden="true"></span>
                </button>
            </div>
            <div class="plx-import-where">
                <span data-role="where-mount"></span>
                <span class="plx-import-where-status" data-role="where-status"></span>
                <span class="plx-import-where-caption" data-role="where-caption"></span>
            </div>
            <div class="plx-import-body" data-role="body"></div>
            <p class="plx-import-status" data-role="status" role="status" aria-live="polite"></p>
            <div class="plx-import-foot">
                <button class="plx-button" type="button" data-role="add" hidden>
                    <span class="fas fa-plus" aria-hidden="true"></span> Add more
                </button>
                <button class="plx-button plx-button-primary" type="button" data-role="go" disabled>
                    Import sample
                </button>
            </div>`;
        node.querySelector('[data-role="close"]').addEventListener("click", close);
        node.querySelector('[data-role="add"]').addEventListener("click", () => {
            render("pick");
        });
        node.querySelector('[data-role="go"]').addEventListener("click", submit);
        // Escape closes; the browser fires `cancel` for it. Prevented while an
        // import is in flight -- the work is server-side and would carry on
        // with nothing on screen saying so.
        node.addEventListener("cancel", (event) => {
            if (state && state.phase === "importing") event.preventDefault();
            else close();
        });
        window.PlexoraPopoverPortal?.attach(node);
        return node;
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
        window.PlexoraPopoverPortal?.detach(dialog);
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
            ? `Add a layer to ${state.sample}` : "Import Sample";
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
            part("where-caption").textContent = "";
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

    function paintCaption() {
        const caption = part("where-caption");
        if (!caption) return;
        const local = !state.location || state.location.isLocal();
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
        drop.appendChild(el("p", "plx-import-drop-title",
            state.modality
                ? `Select the ${state.modality.replace(/_/g, " ")} data`
                : "Select a file or folder"));

        const panel = buildSplitControl("sample", pickWith,
                                        {file: "Select File",
                                         directory: "Select Folder"});
        panel.classList.add("is-panel");
        panel.addEventListener("keydown", (event) => stepBetweenHalves(panel, event));
        drop.appendChild(panel);

        const row = el("div", "plx-import-path");
        const box = el("input", "plx-import-path-input");
        box.type = "text";
        box.placeholder = "or paste a path";
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

        drop.appendChild(el("p", "plx-import-formats",
            "Xenium run · SpatialData · Visium · OME-TIFF · "
            + "OME-Zarr · H&E · masks · AnnData · CSV · "
            + "transcripts"));
        body.appendChild(drop);

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

    function pickWith(mode) {
        setStatus("Opening file browser…");
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
        }).finally(() => clearTimeout(settle));
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
            return;
        }

        (proposal.samples || []).forEach((sample, index) => {
            body.appendChild(renderSample(sample, index));
        });

        (proposal.unrecognised || []).forEach((entry) => {
            const row = el("div", "plx-import-row is-muted");
            row.appendChild(iconFor(null));
            const text = el("div", "plx-import-row-text");
            text.appendChild(el("span", "plx-import-row-name", basename(entry.path)));
            text.appendChild(el("span", "plx-import-row-detail", entry.reason));
            row.appendChild(text);
            row.appendChild(removeButton(entry.path));
            body.appendChild(row);
        });

        (proposal.warnings || []).forEach((warning) => {
            body.appendChild(el("p", "plx-import-warning", warning));
        });

        // Enabled as soon as ANY sample has a layer. An unreadable file among
        // five must not stop the other four from being imported.
        const importable = (proposal.samples || []).some((s) => (s.layers || []).length);
        part("go").disabled = !importable;
    }

    function renderSample(sample, index) {
        const block = el("div", "plx-import-sample");

        if (sample.existing && !scoped()) {
            const already = el("div", "plx-import-existing");
            already.appendChild(el("span", null,
                `Already imported as ${sample.existing}.`));
            const openIt = el("button", "plx-button", "Open it");
            openIt.type = "button";
            openIt.addEventListener("click", () => {
                const target = sample.existing;
                close();
                PlexoraRouter.go(`/${encodeURIComponent(target)}`);
            });
            already.appendChild(openIt);
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
        const name = el("span", "plx-import-row-name", layer.label || layer.id);
        if (layer.reference) name.appendChild(el("span", "plx-import-badge", "reference"));
        text.appendChild(name);
        text.appendChild(el("span", "plx-import-row-detail", layer.detail || ""));
        if (layer.dependency && layer.dependency.install) {
            // A package this environment has not got. The row stays -- what is
            // missing is an install, not the data -- and the command to fix it
            // is shown rather than a stack trace after the fact.
            const hint = el("code", "plx-import-install", layer.dependency.install);
            text.appendChild(hint);
        }
        row.appendChild(text);
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

    function renderQuestion(question) {
        const row = el("div", "plx-import-question");
        const label = el("label", "plx-import-question-label", question.label);
        row.appendChild(label);

        const current = state.answers[question.id] ?? question.default;
        if (question.kind === "select" || (question.options || []).length > 3) {
            const select = el("select", "plx-import-question-select");
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
                const button = el("button", "plx-import-choice", option.label);
                button.type = "button";
                button.classList.toggle("is-chosen", option.value === current);
                button.addEventListener("click", () => answer(question.id, option.value));
                group.appendChild(button);
            });
            row.appendChild(group);
        }
        return row;
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

    function renderNameRow(sample, index) {
        const row = el("div", "plx-import-meta");

        const nameCell = el("div", "plx-import-meta-cell");
        nameCell.appendChild(el("span", "plx-import-meta-label", "Name"));
        const nameBox = el("input", "plx-import-name");
        nameBox.type = "text";
        nameBox.value = (index === 0 && state.name) || sample.name || "";
        nameBox.addEventListener("input", () => {
            if (index === 0) state.name = nameBox.value.trim();
        });
        nameCell.appendChild(nameBox);
        row.appendChild(nameCell);

        const datasetCell = el("div", "plx-import-meta-cell");
        datasetCell.appendChild(el("span", "plx-import-meta-label", "Dataset"));
        const datasetButton = el("button", "plx-import-dataset",
                                 state.dataset?.name || "None");
        datasetButton.type = "button";
        datasetButton.addEventListener("click", chooseDataset.bind(null, datasetButton));
        datasetCell.appendChild(datasetButton);
        row.appendChild(datasetCell);

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
        button.textContent = state.dataset?.name || "None";
    }

    // -- state 3: importing -------------------------------------------------

    async function submit() {
        if (!state || !state.picks.length) return;
        const target = scoped() ? "import/layers" : "import/sample";
        const payload = scoped()
            ? {sample: state.sample, paths: state.picks, answers: state.answers}
            : {
                paths: state.picks,
                answers: state.answers,
                name: state.name || undefined,
                dataset: state.dataset || undefined,
            };
        render("importing");
        setStatus(scoped() ? "Adding…" : "Registering…");
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

    function renderImporting() {
        const body = part("body");
        body.innerHTML = "";
        const layers = [];
        (state.proposal?.samples || []).forEach((sample) => {
            (sample.layers || []).forEach((layer) => layers.push(layer));
        });
        state.bars = new Map();
        layers.forEach((layer) => {
            const row = el("div", "plx-import-row");
            row.appendChild(iconFor(layer.kind));
            const text = el("div", "plx-import-row-text");
            text.appendChild(el("span", "plx-import-row-name", layer.label || layer.id));
            const detail = el("span", "plx-import-row-detail", "waiting…");
            text.appendChild(detail);
            row.appendChild(text);
            body.appendChild(row);
            state.bars.set(layer.id, detail);
        });
        part("add").hidden = true;
        part("go").disabled = true;
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
                line.textContent = entry.status === "ready" ? "ready"
                    : entry.status === "failed" ? (entry.error || "failed")
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
     * library header, its empty state, the home card and the navbar menu. A
     * controller per surface would be four copies of one line.
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
