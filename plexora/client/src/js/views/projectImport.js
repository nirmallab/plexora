/**
 * projectImport.js -- Import: a `.plexora` file's projects into this Plexora
 * (server/models/bundles.py; docs/internal/PROJECT_BUNDLES.md), and "Make a
 * copy" of a project already here.
 *
 *   pick      Choose the file (the app's own Open dialog, or the browser's),
 *             or drop it on the dialog.
 *   reading   Only the file's manifest is read -- a browser uploads the file
 *             first, and says how far it has got.
 *   preview   Each project, with what importing it does: New, "Imported as
 *             name_2" when its name is taken, Already here, Update (the file
 *             carries the next version of a project here), or Changed on both
 *             -- the only cases with a choice to make. Where its images are
 *             ("2 found here · 1 to find after import"), and where it goes: a
 *             Location, the default save location first.
 *   running   Checking the file, Unpacking, Adding the projects. Closing the
 *             dialog does not stop it; Cancel does, and leaves nothing behind.
 *   done      Each project with Open, and "Find missing images…" for one
 *             whose images were not found: a folder the user picks, searched
 *             on its own.
 *
 * Every request goes through `window.PlexoraBundleTransport`, which
 * projectExport.js documents and sets up; the shared drawing is
 * `window.PlexoraBundleKit`, from the same file. This file is loaded after
 * it, from base.html.
 *
 * Opened from: the Samples page's Import Project…, File > Import Project…
 * (the navbar row the desktop menu clicks), a `.plexora` dropped on the
 * window (bundle_routes.py's drop sniffer -> the `import_bundle` executor
 * below), a double-click in Finder or Explorer (`/desktop/open` answers
 * `open: "bundle"`; desktopBridge.js, or `/open_project?bundle=`).
 *
 * The pure parts are on `PlexoraProjectImport._internal` for
 * tests/js/project_bundle_probe.mjs.
 */
(function () {
    "use strict";

    const kit = window.PlexoraBundleKit;
    if (!kit) return;
    const {esc, plural, formatBytes, basename, transport, watch, shell, progressPanel, showProgress, toast} = kit;

    const IMPORT_STAGES = [
        {key: "verify", label: "Checking the file"},
        {key: "unpack", label: "Unpacking"},
        {key: "place", label: "Adding the projects"},
    ];

    //: Each status's badge. `renamed` names the project it becomes.
    const BADGES = {
        new: {label: "New", tone: "new"},
        renamed: {label: "New", tone: "new"},
        already_here: {label: "Already here", tone: "same"},
        update: {label: "Update", tone: "update"},
        conflict: {label: "Changed on both", tone: "conflict"},
    };

    //: The choices a status offers, in order; a status that is not here has
    //: only one sensible outcome and offers none.
    const CHOICES = {
        conflict: [["keep_both", "Keep both"], ["replace", "Replace"]],
        update: [["update", "Update"], ["keep_both", "Keep both"], ["skip", "Skip"]],
        already_here: [["skip", "Skip"], ["keep_both", "Keep both"]],
    };

    //: What each status does unless a choice is made: bundles.DEFAULT_ACTION.
    const DEFAULT_ACTION = {new: "import", renamed: "import", already_here: "skip",
                            update: "update", conflict: "keep_both"};

    //: The one sentence under a choice, saying what it will do.
    const CONSEQUENCE = {
        "conflict:keep_both": "This one is added under a new name; yours stays as it is.",
        "conflict:replace": "Yours is moved to a backup folder, then replaced by this one.",
        "update:update": "This is a newer version of your project. Yours is moved to a backup folder first.",
        "update:keep_both": "This one is added under a new name; yours stays as it is.",
        "update:skip": "Nothing changes.",
        "already_here:skip": "You have this version already.",
        "already_here:keep_both": "Added again, under a new name.",
    };

    // -- the pure parts ------------------------------------------------------------

    /** The choices to offer for a project of this status: `[[action, label]]`. */
    function choicesFor(status) {
        return CHOICES[status] || [];
    }

    /** What happens to this project unless a choice is made. */
    function defaultChoice(project) {
        const offered = choicesFor(project.status).map(([action]) => action);
        if (project.action && (!offered.length || offered.includes(project.action))) return project.action;
        return DEFAULT_ACTION[project.status] || "import";
    }

    /** `{label, tone}` of a project's badge. */
    function badgeFor(project) {
        const badge = BADGES[project.status] || {label: project.status || "", tone: "new"};
        if (project.status === "renamed" && project.target) {
            return {label: `Imported as ${project.target}`, tone: "renamed"};
        }
        return badge;
    }

    /** "2 found here · 1 included · 1 as a smaller copy · 1 to find after
     *  import", or "". A smaller copy is an included image within its own
     *  noise (server/utils/approximate_image.py), used when this computer
     *  has no original of it. */
    function imagesLine(refs) {
        refs = refs || {};
        const parts = [];
        if (refs.found) parts.push(`${refs.found} found here`);
        if (refs.included) parts.push(`${refs.included} included`);
        if (refs.approximate) {
            parts.push(refs.approximate === 1 ? "1 as a smaller copy" : `${refs.approximate} as smaller copies`);
        }
        if (refs.node) {
            const nodes = (refs.nodes || []).filter(Boolean);
            parts.push(`${refs.node} on ${nodes.length ? nodes.join(", ") : "a data node"}`);
        }
        if (refs.missing) parts.push(`${refs.missing} to find after import`);
        return parts.join(" · ");
    }

    /**
     * The places an import may go: the user's own folders, there and
     * writable. A shared root is someone else's, and the old nested website
     * folder is listed only so nothing in it disappears.
     */
    function destinations(answer) {
        const listed = ((answer && answer.locations) || []).filter((place) => place
            && place.writable && place.available && !["legacy", "shared"].includes(place.kind));
        const preferred = answer && answer.default_save && answer.default_save.id;
        const first = listed.find((place) => place.id === preferred) || listed.find((place) => place.is_default);
        return first ? [first, ...listed.filter((place) => place !== first)] : listed;
    }

    /** How many projects the chosen actions bring in. */
    function importing(projects, choices) {
        return (projects || []).filter((project) => (choices[project.id] || defaultChoice(project)) !== "skip");
    }

    function importLabel(projects, choices) {
        const count = importing(projects, choices).length;
        return count > 1 ? `Import ${count} projects` : "Import";
    }

    /** The space line under the destination: `{text, short}`. */
    function spaceLine(preview, placeLabel) {
        if (!preview) return {text: "", short: false};
        const needed = Number(preview.needed || 0);
        const free = preview.free;
        if (preview.fits === false) {
            return {short: true,
                    text: `Not enough space${placeLabel ? ` in ${placeLabel}` : ""}: it needs ${formatBytes(needed)}`
                          + (free != null ? ` and ${formatBytes(free)} is free.` : ".")};
        }
        return {short: false,
                text: `Needs ${formatBytes(needed)}` + (free != null ? ` · ${formatBytes(free)} free` : "")};
    }

    const APP_WORD = {desktop: "the desktop app", website: "the website", server: "a Plexora server"};

    /** "Exported from Plexora 0.0.45 (the desktop app) on 9 Nov 2026." */
    function originLine(bundle) {
        bundle = bundle || {};
        const app = bundle.app || {};
        const parts = ["Exported from Plexora"];
        if (app.version) parts.push(String(app.version));
        let text = parts.join(" ");
        if (APP_WORD[app.kind]) text += ` (${APP_WORD[app.kind]})`;
        const when = bundle.created ? new Date(bundle.created) : null;
        if (when && !Number.isNaN(when.getTime())) {
            text += ` on ${when.toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"})}`;
        }
        return text + ".";
    }

    /**
     * What a finished project's row says happened to it. The row is headed
     * by the name it has now, so this says how it came to have it.
     */
    function outcomeFor(project, copyOf) {
        const renamed = project.target && project.name && project.target !== project.name;
        if (copyOf) return `A copy of ${copyOf}`;
        switch (project.action) {
            case "skip": return project.status === "already_here" ? "Already here" : "Skipped";
            case "update": return "Updated";
            case "replace": return "Replaced";
            case "keep_both": return renamed ? `Imported beside your ${project.name}` : "Imported";
            default: return renamed ? `Imported · ${project.name} was taken` : "Imported";
        }
    }

    // -- the dialog ------------------------------------------------------------------

    let current = null;

    /**
     * Open the import dialog.
     *
     * @param path a `.plexora` on this server's machine: read at once
     * @param file a browser `File`: uploaded and read at once
     * @param copy a project's name: "Make a copy" of it, started at once
     * @param onClose called when the dialog goes away
     */
    function open(options) {
        options = options || {};
        if (current) close();
        const ui = {
            phase: "pick", source: null, sourceName: "", preview: null, choices: {},
            places: null, location: null, job: null, watcher: null, status: null,
            background: false, copyOf: options.copy || null, results: null,
            onClose: options.onClose || null, detachDrop: null,
        };
        ui.shell = shell({extraClass: "plx-bundle-import", onClose: close});
        current = ui;
        const node = ui.shell.node;
        node.addEventListener("click", (event) => onClick(ui, event));
        node.addEventListener("change", (event) => onChange(ui, event));
        attachDrops(ui);
        node.showModal();
        if (ui.copyOf) {
            void startCopy(ui);
        } else if (options.path) {
            void read(ui, {path: options.path});
        } else if (options.file) {
            void read(ui, {file: options.file});
        } else {
            renderPick(ui);
        }
        return node;
    }

    function close() {
        const ui = current;
        if (!ui) return;
        current = null;
        if (ui.phase === "running" && ui.watcher) {
            ui.background = true;
        } else {
            ui.watcher?.stop();
        }
        ui.detachDrop?.();
        ui.shell.remove();
        ui.onClose?.();
    }

    function setPhase(ui, phase) {
        ui.phase = phase;
        ui.shell.node.dataset.phase = phase === "running" ? "importing" : phase;
        ui.shell.part("pinned").innerHTML = "";
    }

    function head(ui, title, subtitle) {
        ui.shell.part("title").textContent = title;
        ui.shell.part("subtitle").textContent = subtitle || "";
    }

    /** A `.plexora` dropped on the dialog: by path in the app, by its bytes
     *  in a browser tab. Taken while nothing is running. */
    function attachDrops(ui) {
        const node = ui.shell.node;
        const takes = () => ["pick", "preview", "failed"].includes(ui.phase) && !ui.copyOf;
        if (typeof attachDropTarget === "function" && window.PlexoraDesktop) {
            ui.detachDrop = attachDropTarget(node, {
                mode: "file", count: "one", filter: "any", enabled: takes,
                several: "Drop one .plexora file.",
                accepts: (path, name) => /\.plexora$/i.test(name) || "This takes a .plexora file.",
                onDrop: (path) => { void read(ui, {path}); },
            });
            return;
        }
        node.addEventListener("dragover", (event) => {
            if (!takes() || !Array.from(event.dataTransfer?.types || []).includes("Files")) return;
            event.preventDefault();
            node.classList.add("is-drop-over");
        });
        node.addEventListener("dragleave", (event) => {
            if (event.target === node) node.classList.remove("is-drop-over");
        });
        node.addEventListener("drop", (event) => {
            node.classList.remove("is-drop-over");
            if (!takes()) return;
            const file = event.dataTransfer?.files?.[0];
            if (!file) return;
            event.preventDefault();
            if (!/\.plexora$/i.test(file.name)) {
                ui.shell.setStatus("That is not a .plexora file.", true);
                return;
            }
            void read(ui, {file});
        });
    }

    // -- pick ----------------------------------------------------------------------------

    function renderPick(ui) {
        setPhase(ui, "pick");
        head(ui, "Import a project", "A .plexora file exported from Plexora, on this computer or another.");
        ui.shell.part("body").innerHTML = `<div class="plx-bundle-pick">
            <span class="plx-bundle-pick-icon" aria-hidden="true"><span class="fas fa-box-open"></span></span>
            <p class="plx-bundle-pick-title">Choose a .plexora file</p>
            <p class="plx-bundle-pick-note">Or drop it here. Plexora reads what is in it before anything is added.</p>
            <button class="plx-button plx-button-primary" type="button" data-act="pick">Choose file…</button>
        </div>`;
        ui.shell.part("foot").innerHTML = "";
    }

    async function pick(ui) {
        let source;
        try {
            source = await transport().pickBundle();
        } catch (error) {
            ui.shell.setStatus(error.message || String(error), true);
            return;
        }
        if (current !== ui || !source) return;
        void read(ui, source);
    }

    // -- reading ------------------------------------------------------------------------

    async function read(ui, source) {
        ui.shell.setStatus("");
        ui.sourceName = source.file ? source.file.name : basename(source.path);
        setPhase(ui, "reading");
        head(ui, "Import a project", `Reading ${ui.sourceName}`);
        const body = ui.shell.part("body");
        body.innerHTML = "";
        const panel = progressPanel(body, {title: ui.sourceName, stages: []});
        panel?.setProgress(null, source.file ? "Sending it to Plexora" : "Reading what is in it");
        ui.shell.part("foot").innerHTML = "";
        if (!ui.places) ui.places = await placesFor();
        if (current !== ui) return;
        ui.location = ui.location || (ui.places[0] && ui.places[0].id) || null;
        let preview;
        try {
            preview = await transport().preview(source, ui.location, (fraction) => {
                if (current !== ui || !panel) return;
                panel.setProgress(Math.round(fraction * 100), fraction >= 1
                    ? "Reading what is in it" : `Sending it to Plexora · ${Math.round(fraction * 100)}%`);
            });
        } catch (error) {
            if (current !== ui) return;
            renderFailed(ui, error.message || String(error), "pick");
            return;
        }
        if (current !== ui) return;
        // A file sent from a browser is kept by the server and named by its
        // `upload` from here on, so a change of Location does not send it again.
        ui.source = preview.upload ? {upload: preview.upload} : {path: preview.path || source.path};
        ui.preview = preview;
        ui.choices = {};
        (preview.projects || []).forEach((project) => { ui.choices[project.id] = defaultChoice(project); });
        renderPreview(ui);
    }

    async function placesFor() {
        try {
            return destinations(await transport().locations());
        } catch (error) {
            return [];
        }
    }

    // -- preview ------------------------------------------------------------------------

    function titleFor(preview) {
        const projects = (preview && preview.projects) || [];
        if (projects.length === 1) return `Import ${projects[0].title || projects[0].name}`;
        const datasets = (preview && preview.datasets) || [];
        if (datasets.length === 1 && datasets[0].projects === projects.length) {
            return `Import dataset ${datasets[0].name}`;
        }
        return `Import ${plural(projects.length, "project")}`;
    }

    function projectMarkup(project, ui) {
        const badge = badgeFor(project);
        const choice = ui.choices[project.id] || defaultChoice(project);
        const offered = choicesFor(project.status);
        const images = imagesLine(project.refs);
        const named = project.title && project.title !== project.name
            ? `<span class="plx-bundle-project-alt">${esc(project.name)}</span>` : "";
        const consequence = CONSEQUENCE[`${project.status}:${choice}`] || "";
        return `<li class="plx-bundle-project${choice === "skip" ? " is-skipped" : ""}" data-project="${esc(project.id)}">
            <span class="plx-bundle-project-icon" aria-hidden="true"><span class="fas fa-microscope"></span></span>
            <div class="plx-bundle-project-main">
                <div class="plx-bundle-project-head">
                    <span class="plx-bundle-project-name">${esc(project.title || project.name)}</span>
                    ${named}
                    <span class="plx-bundle-badge is-${badge.tone}">${esc(badge.label)}</span>
                    <span class="plx-bundle-size">${esc(formatBytes(project.size))}</span>
                </div>
                ${images ? `<p class="plx-bundle-project-meta"><span class="fas fa-image" aria-hidden="true"></span>
                    Images: ${esc(images)}</p>` : ""}
                ${offered.length ? `<div class="plx-bundle-choice" role="radiogroup"
                        aria-label="What to do with ${esc(project.title || project.name)}">
                    ${offered.map(([action, label]) => `<button type="button" role="radio"
                        class="plx-bundle-choice-option${action === choice ? " is-on" : ""}"
                        aria-checked="${action === choice ? "true" : "false"}"
                        data-choose="${action}" data-for="${esc(project.id)}">${esc(label)}</button>`).join("")}
                </div>` : ""}
                ${consequence ? `<p class="plx-bundle-project-note">${esc(consequence)}</p>` : ""}
            </div>
        </li>`;
    }

    function alsoLine(preview) {
        const parts = [];
        (preview.datasets || []).forEach((dataset) => parts.push(`dataset ${dataset.name}`));
        if (preview.captures) parts.push(plural(preview.captures, "bookmark"));
        if ((preview.figures || []).length) parts.push(plural(preview.figures.length, "figure"));
        return parts.length ? `With ${parts.join(" · ")}.` : "";
    }

    function destinationMarkup(ui) {
        const places = ui.places || [];
        const chosen = places.find((place) => place.id === ui.location) || places[0];
        const space = spaceLine(ui.preview, chosen && chosen.label);
        const field = places.length > 1
            ? `<select class="plx-bundle-select" data-role="location" aria-label="Save to">
                ${places.map((place) => `<option value="${esc(place.id)}" ${place.id === (chosen && chosen.id)
                    ? "selected" : ""}>${esc(place.label)}</option>`).join("")}
               </select>`
            : `<span class="plx-bundle-dest-place" title="${esc((chosen && chosen.path) || (ui.preview && ui.preview.root) || "")}">${
                esc((chosen && chosen.label) || "Where new projects go")}</span>`;
        return `<div class="plx-bundle-dest${space.short ? " is-short" : ""}">
            <span class="plx-bundle-dest-label"><span class="fas fa-folder-open" aria-hidden="true"></span>Save to</span>
            ${field}
            <span class="plx-bundle-dest-space">${space.short ? `<span class="fas fa-triangle-exclamation"
                aria-hidden="true"></span>` : ""}${esc(space.text)}</span>
        </div>`;
    }

    function renderPreview(ui) {
        setPhase(ui, "preview");
        const preview = ui.preview;
        const projects = preview.projects || [];
        head(ui, titleFor(preview), originLine(preview.bundle));
        const also = alsoLine(preview);
        ui.shell.part("body").innerHTML = `
            <ul class="plx-bundle-projects">${projects.map((project) => projectMarkup(project, ui)).join("")}</ul>
            ${also ? `<p class="plx-bundle-also">${esc(also)}</p>` : ""}`;
        ui.shell.part("pinned").innerHTML = destinationMarkup(ui);
        paintPreviewFoot(ui);
    }

    function paintPreviewFoot(ui) {
        const projects = (ui.preview && ui.preview.projects) || [];
        const count = importing(projects, ui.choices).length;
        const short = Boolean(ui.preview) && ui.preview.fits === false;
        const blocked = !ui.preview || short || !count || ui.rereading;
        const note = short
            ? ((ui.places || []).length > 1 ? "Choose another place, or make room there." : "Make room there, then try again.")
            : (!count ? "Nothing to import with these choices." : "");
        ui.shell.part("foot").innerHTML = `
            ${note ? `<span class="plx-bundle-foot-note">${esc(note)}</span>` : ""}
            <button class="plx-button plx-button-primary" type="button" data-act="import"
                ${blocked ? "disabled" : ""}>${esc(importLabel(projects, ui.choices))}</button>`;
    }

    function choose(ui, id, action) {
        if (ui.phase !== "preview") return;
        ui.choices[id] = action;
        const project = (ui.preview.projects || []).find((item) => item.id === id);
        const row = ui.shell.node.querySelector(`[data-project="${CSS.escape(id)}"]`);
        if (project && row) {
            const fresh = document.createElement("div");
            fresh.innerHTML = projectMarkup(project, ui);
            row.replaceWith(fresh.firstElementChild);
            ui.shell.node.querySelector(`[data-choose="${action}"][data-for="${CSS.escape(id)}"]`)?.focus();
        }
        paintPreviewFoot(ui);
    }

    async function moveTo(ui, location) {
        ui.location = location;
        ui.rereading = true;
        paintPreviewFoot(ui);
        let preview;
        try {
            // Asked again: a name taken there is not taken here, and the free
            // space is that disk's.
            preview = await transport().preview(ui.source, location);
        } catch (error) {
            ui.rereading = false;
            if (current !== ui) return;
            ui.shell.setStatus(error.message || String(error), true);
            paintPreviewFoot(ui);
            return;
        }
        ui.rereading = false;
        if (current !== ui) return;
        const kept = ui.choices;
        ui.preview = preview;
        ui.choices = {};
        (preview.projects || []).forEach((project) => {
            const offered = choicesFor(project.status).map(([action]) => action);
            ui.choices[project.id] = offered.includes(kept[project.id]) ? kept[project.id] : defaultChoice(project);
        });
        renderPreview(ui);
    }

    // -- running ------------------------------------------------------------------------

    async function startImport(ui) {
        const button = ui.shell.part("foot").querySelector("[data-act='import']");
        if (button) button.disabled = true;
        ui.shell.setStatus("");
        let first;
        try {
            first = await transport().startImport({source: ui.source, choices: ui.choices, location: ui.location});
        } catch (error) {
            if (current !== ui) return;
            ui.shell.setStatus(error.message || String(error), true);
            if (button) button.disabled = false;
            return;
        }
        if (current !== ui) return;
        run(ui, first, {title: ui.sourceName || "Importing"});
    }

    async function startCopy(ui) {
        const name = ui.copyOf;
        setPhase(ui, "reading");
        head(ui, `Make a copy of ${name}`, "A new project of your own, with this one's analysis, tables and bookmarks.");
        let first;
        try {
            first = await transport().copy(name, null);
        } catch (error) {
            if (current !== ui) return;
            renderFailed(ui, error.message || String(error), null);
            return;
        }
        if (current !== ui) return;
        run(ui, first, {title: name});
    }

    function run(ui, first, {title}) {
        setPhase(ui, "running");
        ui.job = first.job;
        const place = (ui.places || []).find((item) => item.id === ui.location);
        if (!ui.copyOf) head(ui, ui.shell.part("title").textContent, place ? `Into ${place.label}` : "");
        const body = ui.shell.part("body");
        body.innerHTML = "";
        ui.panel = progressPanel(body, {title, stages: IMPORT_STAGES});
        ui.shell.part("foot").innerHTML = `<span class="plx-bundle-foot-note">Closing this keeps it going.</span>
            <button class="plx-button" type="button" data-act="cancel">Cancel</button>`;
        ui.watcher = watch("import", first, (status) => {
            ui.status = status;
            if (current === ui) showProgress(ui.panel, status);
        });
        ui.watcher.done.then((status) => finished(ui, status), (error) => {
            if (current === ui) renderFailed(ui, error.message || String(error), null);
            else if (ui.background) toast({title: "Import stopped", note: error.message || String(error),
                                           tone: "warning"});
        });
    }

    async function cancelJob(ui, button) {
        if (!ui.job) return;
        button.disabled = true;
        button.textContent = "Stopping…";
        try {
            await transport().cancel("import", ui.job);
        } catch (error) {
            button.disabled = false;
            button.textContent = "Cancel";
            ui.shell.setStatus(error.message || String(error), true);
        }
    }

    function finished(ui, status) {
        ui.status = status;
        if (status.status === "cancelled") {
            if (current !== ui) return;
            if (ui.copyOf) {
                close();
                toast({title: "Copy stopped", note: "Nothing was added."});
                return;
            }
            renderPreview(ui);
            ui.shell.setStatus("Import stopped. Nothing was added.");
            return;
        }
        if (status.status === "failed") {
            const message = status.error || (ui.copyOf ? "The copy did not finish." : "The import did not finish.");
            if (current === ui) renderFailed(ui, message, ui.copyOf ? null : "preview");
            else toast({title: ui.copyOf ? "Copy failed" : "Import failed", note: message, tone: "warning"});
            return;
        }
        const result = status.result || {};
        window.PlexoraOpenProject?.refresh?.();
        if (current === ui) {
            renderDone(ui, result);
            return;
        }
        // Finished behind a closed dialog: one notice, with the way in.
        const added = (result.projects || []).filter((project) => project.action !== "skip");
        const first = added[0];
        toast({
            title: ui.copyOf ? `Copied ${ui.copyOf}` : `Imported ${added.length === 1 ? first.target : plural(added.length, "project")}`,
            note: ui.copyOf && first ? `The copy is ${first.target}.` : "",
            actions: first ? [{label: "Open", primary: true, onSelect: () => openProject(first.target)}] : [],
            timeout: 12000,
        });
    }

    // -- done ----------------------------------------------------------------------------

    function resultMarkup(project, {copyOf, single}) {
        const skipped = project.action === "skip";
        const refs = project.refs || {};
        const missing = Number(refs.missing || 0);
        const found = imagesLine({found: refs.found, included: refs.included, approximate: refs.approximate,
                                  node: refs.node, nodes: refs.nodes});
        const backup = project.backup
            ? `<p class="plx-bundle-result-backup">Your previous version is in
                <span class="plx-bundle-result-note" title="${esc(project.backup)}">${esc(project.backup)}</span></p>`
            : "";
        // A skipped project has a copy here only when this computer had it.
        const openable = !skipped || ["already_here", "update", "conflict"].includes(project.status);
        const missingLine = missing && !skipped ? `<div class="plx-bundle-missing" data-missing-for="${esc(project.target)}">
                <span class="plx-bundle-missing-text"><span class="fas fa-triangle-exclamation" aria-hidden="true"></span>
                    ${esc(plural(missing, "image"))} still to find</span>
                <button class="plx-button plx-bundle-small" type="button" data-act="find"
                    data-target="${esc(project.target)}">Find missing images…</button>
            </div>` : "";
        return `<li class="plx-bundle-result${skipped ? " is-skipped" : ""}">
            <span class="plx-bundle-result-mark${skipped ? " is-skipped" : ""}" aria-hidden="true">
                <span class="fas ${skipped ? "fa-minus" : "fa-check"}"></span></span>
            <div class="plx-bundle-result-main">
                <div class="plx-bundle-result-head">
                    <span class="plx-bundle-project-name">${esc(project.target || project.name)}</span>
                    <span class="plx-bundle-result-what">${esc(outcomeFor(project, copyOf))}</span>
                    ${single || !openable ? "" : `<button class="plx-button plx-bundle-small plx-bundle-open"
                        type="button" data-act="open" data-target="${esc(project.target)}">Open</button>`}
                </div>
                ${found && !skipped ? `<p class="plx-bundle-project-meta"><span class="fas fa-image" aria-hidden="true"></span>
                    Images: ${esc(found)}</p>` : ""}
                ${missingLine}
                ${backup}
            </div>
        </li>`;
    }

    function renderDone(ui, result) {
        setPhase(ui, "done");
        ui.results = result;
        const projects = result.projects || [];
        const added = projects.filter((project) => project.action !== "skip");
        const title = ui.copyOf ? `Copied ${ui.copyOf}`
            : (added.length ? `Imported ${plural(added.length, "project")}` : "Nothing was imported");
        const place = (ui.places || []).find((item) => item.id === ui.location)
            || (ui.places || []).find((item) => result.root && item.path === result.root);
        head(ui, title, place ? `In ${place.label}` : "");
        if (result.root) ui.shell.part("subtitle").title = result.root;
        // One project: its Open is the next step, so it is the dialog's
        // primary action, and the row does not offer it a second time.
        const only = added.length === 1 && projects.length === 1 ? added[0] : null;
        const datasets = (result.datasets || []).filter((dataset) => dataset && dataset.id);
        ui.shell.part("body").innerHTML = `
            <ul class="plx-bundle-results">${projects.map((project) => resultMarkup(project,
                {copyOf: ui.copyOf, single: Boolean(only)})).join("")}</ul>
            ${datasets.length ? `<div class="plx-bundle-datasets">${datasets.map((dataset) => `
                <button class="plx-button plx-bundle-small" type="button" data-act="dataset"
                    data-dataset="${esc(dataset.id)}"><span class="fas fa-folder" aria-hidden="true"></span>
                    Open dataset ${esc(dataset.name)}</button>`).join("")}</div>` : ""}`;
        ui.shell.part("foot").innerHTML = only
            ? `<button class="plx-button plx-button-primary" type="button" data-act="open"
                data-target="${esc(only.target)}">Open ${esc(only.target)}</button>` : "";
        ui.shell.setStatus("");
    }

    function openProject(target) {
        if (!target) return;
        const href = kit.url(encodeURIComponent(target));
        if (window.PlexoraRouter && typeof window.PlexoraRouter.go === "function") window.PlexoraRouter.go(href);
        else window.location.href = href;
    }

    function openDataset(id) {
        if (window.PlexoraOpenProject?.open) {
            window.PlexoraOpenProject.open(id);
            return;
        }
        const href = kit.url(`open_project?dataset=${encodeURIComponent(id)}`);
        if (window.PlexoraRouter && typeof window.PlexoraRouter.go === "function") window.PlexoraRouter.go(href);
        else window.location.href = href;
    }

    async function findMissing(ui, button) {
        const target = button.dataset.target;
        let folder;
        // The website's folder picker needs the user activation of this very
        // click, which an await before it would spend: its findMissing asks.
        const direct = Boolean(transport().picksFolderItself);
        let pending = null;
        if (direct) {
            button.disabled = true;
            button.textContent = "Looking…";
            pending = transport().findMissing(target);
        } else {
            try {
                folder = await transport().pickFolder();
            } catch (error) {
                ui.shell.setStatus(error.message || String(error), true);
                return;
            }
            if (!folder || current !== ui) return;
            button.disabled = true;
            button.textContent = "Looking…";
        }
        let answer;
        try {
            answer = await (pending || transport().findMissing(target, folder));
        } catch (error) {
            button.disabled = false;
            button.textContent = "Find missing images…";
            ui.shell.setStatus(error.message || String(error), true);
            return;
        }
        if (current !== ui) return;
        const still = Array.isArray(answer.missing) ? answer.missing.length : Number(answer.missing || 0);
        const found = Number(answer.found || 0);
        const holder = ui.shell.node.querySelector(`[data-missing-for="${CSS.escape(target)}"]`);
        if (!holder) return;
        if (!still) {
            holder.outerHTML = `<p class="plx-bundle-found"><span class="fas fa-check" aria-hidden="true"></span>
                ${esc(found ? `Found ${plural(found, "image")}. Every image is here now.` : "Every image is here now.")}</p>`;
            return;
        }
        holder.querySelector(".plx-bundle-missing-text").innerHTML = `<span class="fas fa-triangle-exclamation"
            aria-hidden="true"></span>${esc(`${found ? `Found ${found} · ` : "None found there · "}${still} still to find`)}`;
        button.disabled = false;
        button.textContent = "Try another folder…";
    }

    // -- failed ----------------------------------------------------------------------------

    function renderFailed(ui, message, back) {
        setPhase(ui, "failed");
        if (!ui.copyOf && !ui.preview) head(ui, "Import a project", "");
        ui.shell.part("body").innerHTML = `<div class="plx-bundle-failed">
            <span class="plx-bundle-failed-mark fas fa-circle-exclamation" aria-hidden="true"></span>
            <p>${esc(message)}</p>
        </div>`;
        const label = back === "pick" ? "Choose another file…" : (back === "preview" ? "Back" : "");
        ui.shell.part("foot").innerHTML = label
            ? `<button class="plx-button plx-button-primary" type="button" data-act="${back === "pick" ? "pick" : "back"}">${label}</button>`
            : "";
    }

    // -- events ----------------------------------------------------------------------------

    function onClick(ui, event) {
        const option = event.target.closest?.("[data-choose]");
        if (option) {
            choose(ui, option.dataset.for, option.dataset.choose);
            return;
        }
        const button = event.target.closest?.("[data-act]");
        if (!button || button.disabled) return;
        const act = button.dataset.act;
        if (act === "pick") void pick(ui);
        else if (act === "import") void startImport(ui);
        else if (act === "cancel") void cancelJob(ui, button);
        else if (act === "find") void findMissing(ui, button);
        else if (act === "back" && ui.preview) renderPreview(ui);
        else if (act === "open") {
            const target = button.dataset.target;
            close();
            openProject(target);
        } else if (act === "dataset") {
            const id = button.dataset.dataset;
            close();
            openDataset(id);
        }
    }

    function onChange(ui, event) {
        const select = event.target.closest?.("[data-role='location']");
        if (select && ui.phase === "preview") void moveTo(ui, select.value);
    }

    // -- where Import is offered -----------------------------------------------------------

    //: Import Project… on the Samples page, and the navbar's row (which the
    //: desktop menu's File > Import Project… clicks). Delegated, so a page
    //: the router swaps in is covered without binding again.
    document.addEventListener("click", (event) => {
        const button = event.target.closest?.("#project-bundle-import, #nav_import_project");
        if (!button || button.disabled) return;
        event.preventDefault();
        open({});
    });

    //: A `.plexora` dropped on the window: bundle_routes.py's sniffer names it,
    //: and the drop router hands the action here.
    window.PlexoraDropRouter?.registerExecutor?.("import_bundle", (action) => {
        open({path: action.paths && action.paths[0]});
    });

    window.PlexoraProjectImport = {
        open, close,
        copy: (name) => open({copy: name}),
        available: () => Boolean(transport() && transport().available()),
        _internal: {choicesFor, defaultChoice, badgeFor, imagesLine, destinations, importing, importLabel,
                    spaceLine, originLine, outcomeFor, titleFor, CONSEQUENCE},
    };
})();
