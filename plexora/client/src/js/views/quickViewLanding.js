/**
 * quickViewLanding.js -- wires the home page (index.html's {% else %} branch,
 * shown when no datasource is named). A path is always POSTed -- never the
 * file's bytes, so huge OME-TIFFs load instantly instead of being copied over
 * HTTP.
 *
 * **It POSTs to `/import/sample`, which is the import dialog's own route.**
 * This page is an ENTRY POINT, not an importer: it asks for one path and
 * submits it, and everything that happens after -- what the files are, which
 * of them is the reference, what the sample is called, whether this data is
 * already registered -- is decided by `import_proposal` and `import_sample`
 * exactly as it is for every other surface. A Xenium run or a SpatialData
 * store picked here brings its transcripts, its mask and its table with it,
 * and lands in the viewer with the layer stack already built.
 *
 * What this page deliberately does NOT do is ask questions -- and it does not
 * guess either. It reads the pick first (`readPick`): one sample with
 * nothing to decide opens in the same press; several samples, any question,
 * a warning or a file it could not read opens the import dialog on what was
 * read, because the POST would otherwise take every default unseen and keep
 * only the first sample. A folder that is a whole cohort -- nothing at its
 * top level, or three samples or more -- opens New Dataset on it instead. A name or a dataset is a decision too, which is what
 * the footer link's dialog is for.
 *
 * The card is one question asked in reading order:
 *
 *   1. **File or Folder** -- which kind of thing Browse opens a dialog for.
 *      An OME-Zarr image is a folder and an OME-TIFF is a file; the chips at
 *      the foot of the card say which formats are which, so the tabs answer
 *      the question rather than asking it. A typed path is whatever it is, so
 *      one that is plainly a folder or plainly a file moves the tab with it.
 *
 *   2. **Which machine** -- a chip at the start of the box, "Local" or the
 *      server's name, the way `host:/path` reads. Its menu lists the actual
 *      machines (services/remoteState.js), connects one that is not up yet,
 *      and adds a new one, so Remote is never a second question. Underneath,
 *      it is the same Local/Remote state every data field in Plexora has
 *      (services/dataLocation.js, attached `bare`): that is what turns a path
 *      on another machine into the `node://` address the route reads.
 *
 *   3. **Which image** -- the box, and ONE button whose job follows it:
 *      Browse while the box is empty, Load once there is a path in it. Two
 *      buttons for the two ways in made the typed one look like a detour.
 *
 * Everything the card shows about its state -- which machine, which face the
 * button has, the mark at the end of the box, the line under it -- is drawn
 * by `paint()` from the few variables below, so no handler draws anything
 * itself.
 */
PlexoraPage.register(function () {
    const card = document.getElementById("quick_view_card");
    const pathInput = document.getElementById("quick_view_path_input");
    if (!card || !pathInput) {
        return;
    }
    const kindOptions = Array.from(card.querySelectorAll(".quick-view-kind-option"));
    const whereButton = document.getElementById("quick_view_where");
    const whereLabel = whereButton.querySelector(".quick-view-where-label");
    const whereDot = whereButton.querySelector(".quick-view-where-dot");
    const menu = document.getElementById("quick_view_where_menu");
    const goButton = document.getElementById("quick_view_go");
    const goBusy = document.getElementById("quick_view_go_busy");
    const status = document.getElementById("quick_view_status");
    const whereStatus = document.getElementById("quick_view_where_status");
    const formats = document.getElementById("quick_view_formats");

    const LOCAL = "local";
    const REMOTE = "remote";
    const Remotes = () => window.PlexoraRemotes;

    //: What Browse opens a dialog for: "file" or "directory" -- the modes
    //: `browseForPath` takes, so the tab passes straight through and the
    //: "file or folder?" popup that mode "any" raises can never happen here.
    let kind = "file";
    //: idle | choosing (a file dialog is open) | loading (the import is running)
    let phase = "idle";
    //: What the server said about a plain path in the box:
    //: idle | checking | ok | missing | relative.
    let verdict = "idle";
    //: The last thing that went wrong, said under the box until the next act.
    let problem = "";
    //: The name the busy line says is opening.
    let opening = "";
    //: A path on another machine has to be shared before it can be submitted,
    //: and sharing starts when the box commits. Load (or a pick in the file
    //: browser) sets this, and the share finishing is what submits it.
    let loadWhenShared = false;
    //: A file from Explorer or Finder is being dragged over the page, in the
    //: desktop app. The card lights and its line says what letting go does.
    let dropping = false;

    // Which machine this image is on. `bare`: the chip and its menu below are
    // this page's switch, and they move the service's state with setWhere().
    // The service's status line goes under the box, beside this page's own.
    let location = null;
    if (window.PlexoraDataLocation && window.PlexoraDataLocation.available()) {
        try {
            location = window.PlexoraDataLocation.attach(pathInput, {
                kind: "image",
                bare: true,
                statusMount: whereStatus,
                onChange: onLocationChange,
            });
        } catch (error) {
            console.error("quickViewLanding: no location switch.", error);
        }
    }

    // -- what the card is in ------------------------------------------------

    function busy() {
        return phase === "choosing" || phase === "loading";
    }

    function isRemote() {
        return Boolean(location) && !location.isLocal();
    }

    /** What `/import/sample` should be given: a path, or a node address. */
    function submittedPath() {
        const typed = pathInput.value.trim();
        if (isWebAddress(typed)) return typed;
        return location ? location.submitValue() : typed;
    }

    /** Whether this server can answer "does that file exist?" about it. */
    function checkable() {
        return !location || location.isPlainPath();
    }

    function isWebAddress(path) {
        return Boolean(path && window.PlexoraLocators
                       && window.PlexoraLocators.isRemoteLocator(path));
    }

    function placeLabel() {
        const place = location && location.place();
        return place ? place.label : "a server";
    }

    function machine() {
        return isRemote() ? placeLabel() : "this computer";
    }

    /** The location service's own line, read by its class: is-busy while a
     *  file is being shared, is-ready once it is, is-error when it cannot be. */
    function shareIs(state) {
        const line = whereStatus && whereStatus.querySelector(".data-location-status");
        return Boolean(line && line.textContent && line.classList.contains(state));
    }

    /** The mark at the end of the box. */
    function mark() {
        const typed = pathInput.value.trim();
        if (!typed) return "idle";
        if (isWebAddress(typed)) return "url";
        if (!checkable()) {
            if (shareIs("is-busy")) return "checking";
            if (shareIs("is-error")) return "bad";
            if (shareIs("is-ready")) return "ok";
            return "idle";
        }
        if (verdict === "missing" || verdict === "relative") return "bad";
        return verdict;
    }

    function placeholder() {
        if (isRemote()) {
            return `Paste a ${kind === "directory" ? "folder " : ""}path on ${placeLabel()}…`;
        }
        return kind === "directory" ? "Paste a folder path…" : "Paste an image path or URL…";
    }

    // -- drawing ------------------------------------------------------------

    function paint() {
        const typed = Boolean(pathInput.value.trim());
        const remote = isRemote();
        const check = mark();
        card.classList.toggle("is-remote", remote);
        card.classList.toggle("is-local", !remote);
        card.classList.toggle("is-directory", kind === "directory");
        card.classList.toggle("is-working", busy() || shareIs("is-busy"));
        card.classList.toggle("is-drop-over", dropping);
        card.dataset.face = busy() ? "busy" : (typed ? "load" : "browse");
        card.dataset.check = check;

        kindOptions.forEach((option) => {
            const on = option.dataset.kind === kind;
            option.setAttribute("aria-checked", on ? "true" : "false");
            option.tabIndex = on ? 0 : -1;
            // Real buttons, so `disabled`: a second pick during a load is a
            // second load of the same slide.
            option.disabled = busy();
        });

        whereButton.disabled = busy();
        whereLabel.textContent = remote ? placeLabel() : "Local";
        whereButton.setAttribute("aria-label", remote
            ? `Image on ${placeLabel()}. Change where the image is`
            : "Image on this computer. Change where the image is");
        whereDot.classList.toggle("is-waiting", shareIs("is-busy"));

        pathInput.placeholder = placeholder();
        pathInput.setAttribute("aria-label", `Image ${kind === "directory" ? "folder" : "file"} path on ${machine()}`);
        // readOnly rather than disabled: the location service disables the box
        // itself when nothing can read a path on the chosen machine, and the
        // two must not undo each other.
        pathInput.readOnly = busy();
        pathInput.setAttribute("aria-invalid", check === "bad" ? "true" : "false");

        // Never disabled mid-work -- it is the spinner, and a press is ignored.
        // Otherwise only when it cannot do anything: a box the service has shut
        // (no way to read that machine), or a path that is not there.
        goButton.disabled = !busy() && (pathInput.disabled || (typed && check === "bad"));
        goButton.classList.toggle("is-loading", phase === "loading");
        goButton.setAttribute("aria-busy", busy() ? "true" : "false");
        goBusy.textContent = phase === "choosing" ? "Choosing…" : "Loading…";

        formats.querySelectorAll(".quick-view-chip").forEach((chip) => {
            chip.classList.toggle("is-on", chip.dataset.kind === kind);
        });

        paintLine(typed, check);
    }

    //: What the line said last, so an unchanged line is not rebuilt -- it is a
    //: live region, and rebuilding one re-announces it.
    let lineKey = "";

    /**
     * The one line under the box. In order of what matters most: what is
     * happening, what went wrong, what the box holds, then which machine.
     */
    function paintLine(typed, check) {
        let line = null;
        if (dropping) {
            line = { tone: "info", icon: "arrow-down", text: "Drop to open it." };
        } else if (phase === "choosing") {
            line = { tone: "busy", text: isRemote()
                ? `Choose a ${kind === "directory" ? "folder" : "file"} on ${placeLabel()}.`
                : "Choose in the file browser. It can open behind this window." };
        } else if (phase === "loading") {
            line = { tone: "busy", text: "Opening ", path: opening, after: "…" };
        } else if (problem) {
            line = { tone: "error", icon: "circle-exclamation", text: problem };
        } else if (check === "url") {
            line = { tone: "info", icon: "globe",
                     text: "A web address — read as given, not checked first." };
        } else if (typed && checkable() && check === "ok") {
            const format = formatOf(pathInput.value.trim());
            line = { tone: "ok", icon: "circle-check", text: `Found on ${machine()}`,
                     muted: format ? ` · ${format}` : "" };
        } else if (typed && checkable() && check === "bad") {
            line = { tone: "error", icon: "circle-exclamation",
                     text: verdict === "relative"
                         ? "That isn’t a full path. Start it with / or ~."
                         : `Nothing at this path on ${machine()}.`,
                     // The likeliest reason a path that is right is "not
                     // there": it is right on another machine.
                     action: !isRemote() && verdict === "missing"
                         ? { label: "Look on a server", run: openMenuFromLine } : null };
        } else if (isRemote() && !typed && !whereStatusText()) {
            line = { tone: "", dot: true,
                     text: `Connected to ${placeLabel()}. Paths and Browse are on that machine.` };
        }

        const key = line ? JSON.stringify([line.tone, line.icon, line.text, line.path,
                                           line.muted, line.action && line.action.label]) : "";
        if (key !== lineKey) {
            lineKey = key;
            status.className = "quick-view-status" + (line && line.tone ? ` is-${line.tone}` : "");
            status.replaceChildren(...(line ? lineParts(line) : []));
        }
        card.classList.toggle("has-lines", Boolean(line) || Boolean(whereStatusText()));
    }

    function whereStatusText() {
        return whereStatus ? whereStatus.textContent.trim() : "";
    }

    function lineParts(line) {
        const icon = document.createElement("span");
        icon.className = "quick-view-status-icon";
        icon.setAttribute("aria-hidden", "true");
        const glyph = document.createElement("span");
        if (line.tone === "busy") glyph.className = "quick-view-spinner";
        else if (line.dot) glyph.className = "quick-view-menu-dot";
        else glyph.className = `fas fa-${line.icon}`;
        icon.appendChild(glyph);

        const text = document.createElement("span");
        text.className = "quick-view-status-text";
        text.append(line.text);
        if (line.path) {
            const path = document.createElement("span");
            path.className = "quick-view-status-path";
            path.textContent = line.path;
            text.append(path);
        }
        if (line.after) text.append(line.after);
        if (line.muted) {
            const muted = document.createElement("span");
            muted.className = "quick-view-status-muted";
            muted.textContent = line.muted;
            text.append(muted);
        }
        const parts = [icon, text];
        if (line.action) {
            const action = document.createElement("button");
            action.type = "button";
            action.className = "quick-view-status-action";
            action.textContent = line.action.label;
            action.addEventListener("click", line.action.run);
            parts.push(action);
        }
        return parts;
    }

    /** What the box holds, in the word a library card would use. Said beside
     *  "Found", so it is only ever a confirmation and never a guess -- an
     *  unrecognised ending says nothing rather than something wrong. */
    function formatOf(path) {
        const name = path.toLowerCase().replace(/[\\/]+$/, "");
        if (/\.ome\.tiff?$/.test(name)) return "OME-TIFF";
        if (/\.qptiff$/.test(name)) return "QPTIFF";
        if (/\.tiff?$/.test(name)) return "TIFF";
        if (/\.(svs|ndpi|scn|mrxs|svslide)$/.test(name)) return "slide";
        if (/\.zarr$/.test(name)) return "OME-Zarr";
        if (/\.ims$/.test(name)) return "Imaris";
        return "";
    }

    /** Which tab a typed path belongs under, when it says so plainly. */
    function kindOf(path) {
        const name = path.trim().toLowerCase();
        if (!name) return null;
        if (/\.zarr[\\/]?$/.test(name) || /[\\/]$/.test(name)) return "directory";
        if (/\.(ome\.)?tiff?$/.test(name) || /\.(svs|ndpi|qptiff|mrxs|scn|svslide|ims)$/.test(name)) {
            return "file";
        }
        return null;
    }

    // -- the import ---------------------------------------------------------

    /**
     * The proposal for `path` when one press cannot honestly import it, else
     * null. One inspection, header reads only; any failure means null, and
     * the POST reports it as it always has.
     *
     * This page is for the case with nothing to decide. Anything else is a
     * guess the POST would make silently -- the first of two samples and the
     * other one dropped, the default of a question nobody saw, a file left
     * out with a warning nobody read -- so it goes to the dialog, which shows
     * the guess and lets it be corrected. A pick with nothing readable in it
     * is not one of these: the POST says why, on this card.
     */
    async function readPick(path) {
        if (!window.PlexoraImportSample?.open) return null;
        try {
            const response = await fetch(plexoraUrl("import/inspect"), {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({paths: [path]}),
            });
            if (!response.ok) return null;
            return await response.json();
        } catch (error) {
            return null;
        }
    }

    function uncertain(proposal) {
        const samples = proposal?.samples || [];
        if (!samples.length) return false;
        return samples.length > 1
            || samples.some((sample) => (sample.questions || []).length > 0)
            || (proposal.warnings || []).length > 0
            || (proposal.unrecognised || []).length > 0;
    }

    /**
     * Whether a picked folder is a cohort rather than a sample: nothing to
     * read at its top level (`Sample01/ ... Sample40/`, or `registration/ +
     * segmentation/`), or three or more samples side by side. Those are a
     * dataset to build (views/datasetBuilder.js), not one sample to open.
     * A folder in a bucket says so itself (`unrecognised[].dataset`), since
     * nothing here can tell a bucket's folder from a store by its name.
     */
    function cohortFolder(path, proposal) {
        if (!window.PlexoraDatasetBuilder || !proposal) return false;
        if ((proposal.unrecognised || []).some((entry) => entry.dataset)) return true;
        // Any other web address is a store, never a folder to walk -- except
        // on a files-only server, whose folders New Dataset reads over SFTP.
        if (/^[a-z][a-z0-9+.-]*:\/\//i.test(path) && !/^sftp:\/\//i.test(path)) return false;
        const samples = proposal.samples || [];
        const folder = kind === "directory" || kindOf(path) === "directory"
            || !/\.[a-z0-9]{2,6}$/i.test(path.replace(/[\\/]+$/, ""));
        if (!folder) return false;
        if (samples.length >= 3) return true;
        const missing = (proposal.warnings || []).some((w) => /does not exist/.test(w));
        return !samples.length && !missing;
    }

    /**
     * Hand the path to the one importer and go where it says.
     *
     * `paths` is a list because that is the route's shape -- the dialog sends
     * everything the user accumulated -- and this page sends exactly one,
     * which is its whole design. No `name` and no `dataset`: both are
     * decisions, and a page whose promise is "no setup" does not get to ask.
     * Data that is already registered comes back as the project it is already
     * in (`existing`) rather than as a second copy of itself.
     *
     * The redirect happens as soon as the RECORD exists. Layers that still
     * have to be built are built behind the viewer, which polls
     * `/import/status` for them -- so the wait is spent looking at the slide.
     */
    async function submitQuickView(path) {
        problem = "";
        loadWhenShared = false;
        // The name the user recognises: off their own box when there is one
        // (a node address ends in a resource id), off the path otherwise.
        opening = (pathInput.value.trim() || path).split(/[\\/]/).filter(Boolean).pop() || path;
        phase = "loading";
        paint();
        try {
            // Anything this one-press card would have to guess -- several
            // samples, a question, a file left out, a flat image needing
            // minutes of pyramid building -- goes to the import dialog, handed
            // what was just read so it opens on it rather than reading again.
            const proposal = await readPick(path);
            if (cohortFolder(path, proposal)) {
                phase = "idle";
                paint();
                // A folder of samples (`s3://lin-2023-orion-crc/data`) is
                // scanned into New Dataset's table, every sample ticked, and
                // made into a dataset when the person presses Create.
                window.PlexoraDatasetBuilder.open({root: path});
                return;
            }
            if (uncertain(proposal)) {
                phase = "idle";
                paint();
                window.PlexoraImportSample.open({paths: [path], proposal});
                return;
            }
            const response = await fetch(plexoraUrl("import/sample"), {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({paths: [path]}),
            });
            const result = await response.json();
            if (!response.ok || result.error) {
                throw new Error(result.error || "Could not load that image.");
            }
            PlexoraRouter.go(result.redirect);
        } catch (error) {
            phase = "idle";
            problem = error.message || "Could not load that image.";
            paint();
        }
    }

    /** A path the file browser handed back: open it, in the same gesture. */
    function take(path) {
        pathInput.value = path;
        pathInput.scrollLeft = pathInput.scrollWidth;
        const shape = kindOf(path);
        if (shape) kind = shape;
        const folderNode = nodeFolder(path);
        if (folderNode) {
            openFolderOnNode(path, folderNode);
            return;
        }
        if (!checkable()) {
            // On another machine the path has to become a node address before
            // it means anything. `change` is what starts the share; its
            // finishing is what submits it (onLocationChange).
            loadWhenShared = true;
            pathInput.dispatchEvent(new Event("change", { bubbles: true }));
            paint();
            return;
        }
        // The dialog only hands back things that are there.
        verdict = "ok";
        submitQuickView(path);
    }

    /**
     * Browse for the image and open it, in one gesture -- the button's whole
     * point while the box is empty. The tab that is lit is the kind passed
     * straight through, so the dialog never has to ask.
     */
    async function browseForImage(mode) {
        problem = "";
        phase = "choosing";
        paint();
        try {
            // The native dialog blocks the server for as long as it is up (see
            // native_dialog.py), and the listing picker is a modal this awaits,
            // so this returns when the dialog CLOSES -- and "Choose in the file
            // browser" stays true for exactly as long as it is on screen.
            await browseForPath({
                mode,
                // Read by the listing picker; the native dialog narrows its own
                // file-type dropdown with it.
                filter: "image",
                // Asked at click time: the machine decides whose filesystem is
                // browsed.
                node: location ? location.browseNode() : null,
                // Where the listing picker opens if it is the one that runs.
                start: pathInput.value.trim(),
                onPicked: (path) => {
                    phase = "idle";
                    take(path);
                },
                onUnavailable: () => {
                    // Not "there is no desktop here" -- browseForPath answers
                    // that with the listing picker. What is left is a server
                    // that could not be reached, or a node that would not
                    // answer.
                    problem = "The file browser could not be opened — paste the full path instead.";
                },
            });
        } finally {
            if (phase === "choosing") {
                phase = "idle";
                paint();
                if (problem) pathInput.focus();
            }
        }
    }

    /**
     * The data node a path in the box is a FOLDER on, or null. A node cannot
     * share a folder (it serves files), so a folder there -- a run, a cohort
     * -- goes to New Dataset, which walks it on the node. An OME-Zarr image
     * is a folder that IS one image, and is shared as before.
     */
    function nodeFolder(path) {
        if (!window.PlexoraDatasetBuilder || !location || checkable()) return null;
        const place = location.place && location.place();
        if (place && place.files) return null;
        const node = location.browseNode();
        const trimmed = String(path || "").replace(/[\\/]+$/, "");
        if (!node || !trimmed || /\.zarr$/i.test(trimmed)) return null;
        const folder = kind === "directory" || !/\.[a-z0-9]{2,6}$/i.test(trimmed);
        return folder ? node : null;
    }

    function openFolderOnNode(path, node) {
        phase = "idle";
        paint();
        window.PlexoraDatasetBuilder.open({root: path, node});
    }

    /** The button: Browse with an empty box, Load with a path in it. */
    function go() {
        if (busy() || goButton.disabled) return;
        const typed = pathInput.value.trim();
        if (!typed) {
            browseForImage(kind);
            return;
        }
        const folderNode = isWebAddress(typed) ? null : nodeFolder(typed);
        if (folderNode) {
            openFolderOnNode(typed, folderNode);
            return;
        }
        if (!checkable() && !isWebAddress(typed)) {
            const ready = submittedPath();
            if (ready && !location.blocking()) {
                submitQuickView(ready);
                return;
            }
            // Not shared yet: start it if nothing has, and load when it lands.
            loadWhenShared = true;
            if (!shareIs("is-busy")) {
                pathInput.dispatchEvent(new Event("change", { bubbles: true }));
            }
            paint();
            return;
        }
        if (verdict === "missing" || verdict === "relative") return;
        submitQuickView(submittedPath());
    }

    /** The location service changed what would be submitted -- the machine,
     *  or how far a share has got. */
    function onLocationChange() {
        if (loadWhenShared && !busy()) {
            const ready = submittedPath();
            if (ready && !location.blocking()) {
                submitQuickView(ready);
                return;
            }
            if (shareIs("is-error")) loadWhenShared = false;
        }
        paint();
    }

    // -- the box --------------------------------------------------------------

    let checkTimer = null;
    let checkRequest = 0;

    /**
     * Ask the server whether a plain path is there. A web address is read,
     * not stat'd, and a path on another machine is checked by sharing it --
     * so neither is asked about here.
     */
    function scheduleCheck() {
        clearTimeout(checkTimer);
        const path = pathInput.value.trim();
        const mine = ++checkRequest;
        if (!path || isWebAddress(path) || !checkable()) {
            verdict = "idle";
            return;
        }
        if (!/^(~|[\\/]|[A-Za-z]:[\\/])/.test(path)) {
            verdict = "relative";
            return;
        }
        verdict = "checking";
        checkTimer = setTimeout(async () => {
            try {
                // `check_path_existence`, not `check_file_existence`: an
                // OME-Zarr image is a directory, and the file-only check calls
                // one missing.
                const response = await fetch(plexoraUrl("check_path_existence"), {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({path: path}),
                });
                const { exists } = await response.json();
                if (mine !== checkRequest) return;
                verdict = exists ? "ok" : "missing";
            } catch (error) {
                if (mine !== checkRequest) return;
                verdict = "missing";
            }
            paint();
        }, 220);
    }

    pathInput.addEventListener("input", () => {
        problem = "";
        loadWhenShared = false;
        const shape = kindOf(pathInput.value);
        if (shape) kind = shape;
        scheduleCheck();
        paint();
    });

    pathInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            go();
        }
    });

    // A long path shows its END once the box lets go of it: the file name is
    // the part anybody recognises, and /n/scratch/... is the part they do not.
    // A task later, because the browser puts an input back to its start on
    // blur after the handlers have run. Not a frame later: a background tab
    // draws no frames, so that would wait until somebody looked.
    pathInput.addEventListener("blur", () => {
        setTimeout(() => { pathInput.scrollLeft = pathInput.scrollWidth; }, 0);
    });

    goButton.addEventListener("click", go);

    // -- File | Folder --------------------------------------------------------

    function setKind(next) {
        if (busy() || next === kind) return;
        kind = next;
        paint();
    }

    kindOptions.forEach((option) => {
        option.addEventListener("click", () => setKind(option.dataset.kind));
        // One radio group, so the arrows move between its two options and Tab
        // leaves it.
        option.addEventListener("keydown", (event) => {
            if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
            event.preventDefault();
            setKind(kind === "file" ? "directory" : "file");
            kindOptions.find((other) => other.dataset.kind === kind)?.focus();
        });
    });

    // -- which machine --------------------------------------------------------

    let unwatch = null;
    //: The list drawn last, so a poll that changes nothing does not rebuild
    //: the menu under the keyboard focus.
    let menuKey = "";

    function menuOpen() {
        return !menu.hidden;
    }

    function openMenu() {
        if (busy() || menuOpen()) return;
        menuKey = "";
        drawMenu((Remotes() && Remotes().snapshot().places) || []);
        menu.hidden = false;
        whereButton.setAttribute("aria-expanded", "true");
        // `active`: the list is on screen, so a machine that comes up while it
        // is open -- connected from Settings, or by another tab -- shows here.
        if (Remotes()) {
            unwatch = Remotes().subscribe((snapshot) => drawMenu(snapshot.places || []),
                                          { active: true });
        }
        menuItems()[0]?.focus();
    }

    function closeMenu(refocus) {
        if (!menuOpen()) return;
        menu.hidden = true;
        whereButton.setAttribute("aria-expanded", "false");
        if (unwatch) unwatch();
        unwatch = null;
        if (refocus) whereButton.focus();
    }

    function openMenuFromLine(event) {
        event.stopPropagation();
        openMenu();
    }

    function menuItems() {
        return Array.from(menu.querySelectorAll(".quick-view-menu-item:not(:disabled)"));
    }

    function connected(place) {
        // A files-only server has no node; its SFTP connection is the answer.
        return place.kind === "server" || Boolean(place.node) || Boolean(place.files);
    }

    function asPlace(place) {
        return { id: place.id, kind: place.kind, label: place.label,
                 node: place.node || null, files: place.files || null };
    }

    function menuItem({ icon, dot, name, sub, checked, onPick, href, role, disabled, add }) {
        const item = document.createElement(href ? "a" : "button");
        item.className = "quick-view-menu-item" + (add ? " is-add" : "");
        item.setAttribute("role", role || "menuitem");
        if (role === "menuitemradio") item.setAttribute("aria-checked", checked ? "true" : "false");
        if (href) {
            item.href = href;
        } else {
            item.type = "button";
            item.disabled = Boolean(disabled);
            item.addEventListener("click", onPick);
        }
        const lead = document.createElement("span");
        lead.setAttribute("aria-hidden", "true");
        if (dot) {
            lead.className = dot;
        } else {
            lead.className = "quick-view-menu-icon";
            const glyph = document.createElement("span");
            glyph.className = `fas fa-${icon}`;
            lead.appendChild(glyph);
        }
        const label = document.createElement("span");
        label.className = "quick-view-menu-name";
        label.textContent = name;
        item.append(lead, label);
        if (sub) {
            const note = document.createElement("span");
            note.className = "quick-view-menu-sub";
            note.textContent = sub;
            item.append(note);
        }
        if (checked) {
            const tick = document.createElement("span");
            tick.className = "quick-view-menu-check";
            tick.setAttribute("aria-hidden", "true");
            const glyph = document.createElement("span");
            glyph.className = "fas fa-check";
            tick.appendChild(glyph);
            item.append(tick);
        }
        return item;
    }

    function drawMenu(places) {
        const current = isRemote() && location.place() ? location.place().id : null;
        const key = JSON.stringify([current, places.map((place) =>
            [place.id, place.node, place.state, place.phase])]);
        if (key === menuKey) return;
        menuKey = key;
        const focused = menu.contains(document.activeElement)
            ? document.activeElement.dataset.place : null;

        const rows = [];
        rows.push(menuItem({
            icon: "laptop", name: "Local", sub: "This computer", role: "menuitemradio",
            checked: !isRemote(),
            onPick: () => choose(LOCAL, null),
        }));
        const sep = () => {
            const line = document.createElement("div");
            line.className = "quick-view-menu-sep";
            line.setAttribute("role", "separator");
            return line;
        };
        rows.push(sep());
        const heading = document.createElement("div");
        heading.className = "quick-view-menu-label";
        heading.textContent = "Remote";
        rows.push(heading);
        if (!places.length) {
            const empty = document.createElement("div");
            empty.className = "quick-view-menu-empty";
            empty.textContent = "No servers yet.";
            rows.push(empty);
        }
        places.forEach((place) => {
            const up = connected(place);
            const waiting = !up && Remotes() && Remotes().isOpening(place.state);
            const row = menuItem({
                dot: "quick-view-menu-dot" + (up ? "" : (waiting ? " is-waiting" : " is-off")),
                name: place.label,
                sub: up ? "Connected" : (waiting ? (place.phase || "Connecting…") : "Not connected"),
                role: "menuitemradio",
                checked: place.id === current,
                disabled: waiting,
                onPick: () => (up ? choose(REMOTE, asPlace(place)) : connect(place)),
            });
            row.dataset.place = place.id;
            rows.push(row);
        });
        rows.push(sep());
        rows.push(menuItem({
            icon: "plus", name: "Add a server…", add: true, onPick: () => connect(null),
        }));
        rows.push(menuItem({
            icon: "gear", name: "Manage servers", sub: "Settings",
            href: plexoraUrl("settings") + "#remotes",
        }));
        menu.replaceChildren(...rows);
        if (focused) menu.querySelector(`[data-place="${CSS.escape(focused)}"]`)?.focus();
    }

    /** Move the service to a machine. It clears the box when the machine
     *  changes -- a path that means something over there means nothing here. */
    function choose(where, place) {
        closeMenu(false);
        if (location) location.setWhere(where, place);
        problem = "";
        paint();
        pathInput.focus();
    }

    /**
     * Connect a saved server that is not up yet, or add a new one, through the
     * connection modal -- the surface built for passwords and progress -- and
     * use it the moment it is. Cancelling leaves the machine as it was.
     */
    async function connect(place) {
        closeMenu(false);
        if (!window.PlexoraConnectionModal) return;
        const outcome = await window.PlexoraConnectionModal.open(place
            ? { name: place.id, kind: "node",
                intent: place.files_only
                    ? "Opening an SFTP connection to this machine, so an image on "
                      + "it can be streamed from the home page. Nothing is started "
                      + "there."
                    : "Opening a data node on this machine, so an image on it can "
                      + "be opened from the home page." }
            : { kind: "node",
                intent: "Add the machine the image is on. Plexora will handle the "
                        + "SSH connection." });
        if (outcome && outcome.connected) {
            choose(REMOTE, { id: outcome.name, kind: "remote",
                             label: outcome.label || (place && place.label) || outcome.name,
                             node: outcome.node || null, files: outcome.files || null });
        } else {
            whereButton.focus();
        }
    }

    whereButton.addEventListener("click", () => {
        if (menuOpen()) closeMenu(true);
        else openMenu();
    });

    whereButton.addEventListener("keydown", (event) => {
        if (event.key === "ArrowDown" && !menuOpen()) {
            event.preventDefault();
            openMenu();
        }
    });

    menu.addEventListener("keydown", (event) => {
        const items = menuItems();
        const at = items.indexOf(document.activeElement);
        if (event.key === "Escape") {
            event.preventDefault();
            closeMenu(true);
        } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            const step = event.key === "ArrowDown" ? 1 : -1;
            items[(at + step + items.length) % items.length]?.focus();
        } else if (event.key === "Home" || event.key === "End") {
            event.preventDefault();
            items[event.key === "Home" ? 0 : items.length - 1]?.focus();
        } else if (event.key === "Tab") {
            closeMenu(false);
        }
    });

    // A click anywhere else puts the menu away, as every menu in the navbar
    // does.
    document.addEventListener("click", (event) => {
        if (menuOpen() && !menu.contains(event.target) && !whereButton.contains(event.target)) {
            closeMenu(false);
        }
    });

    // -- the rest of the card -------------------------------------------------

    // The format chips, from the table every Browse button on the import form
    // reads (browsePicker.js) -- two a side, so the examples on this card and
    // on the form can never say different things.
    if (typeof KIND_EXAMPLES !== "undefined" && KIND_EXAMPLES.image) {
        const more = formats.querySelector(".quick-view-more");
        ["file", "directory"].forEach((side) => {
            KIND_EXAMPLES.image[side].split(" · ").forEach((label, index) => {
                const chip = document.createElement("li");
                chip.className = "quick-view-chip" + (side === "directory" && index === 0 ? " is-lead" : "");
                chip.dataset.kind = side;
                chip.textContent = label;
                formats.insertBefore(chip, more);
            });
        });
    }

    // What the import dialog's own `?` opens, from the surface that asks the
    // same question first. No controller of its own: the modal is a classic
    // script on `window`, loaded by base.html everywhere.
    document.getElementById("quick_view_help")?.addEventListener(
        "click", (event) => {
            window.PlexoraImportHelp?.open({returnTo: event.currentTarget});
        });

    // -- a file dropped on the page -----------------------------------------
    //
    // In the desktop app the whole page takes a drop, not only the card: the
    // space around it is the biggest target on the screen, and this page has
    // exactly one thing to do with a file. One file opens as if Browse had
    // picked it -- the same `take`, so a run folder or a slide with anything
    // to decide still goes to the import dialog. Several are a decision about
    // several samples, which is the dialog's from the start.
    //
    // Not while the card is busy: that drop falls through to the bridge's own
    // answer, the import dialog, rather than starting a second load over the
    // first. Nothing to undo when the router swaps the page out --
    // browsePicker.js keys its zones by element (attachDropTarget).
    const landing = document.getElementById("quick_view_landing");
    if (landing && typeof attachDropTarget === "function") {
        attachDropTarget(landing, {
            filter: "any", mode: "any", count: "many",
            enabled: () => !busy(),
            onOver: (lit) => {
                dropping = lit;
                paint();
            },
            hint: (paths, {plan}) => (plan && plan.summary && plan.summary.hint)
                ? {label: plan.summary.hint, tone: plan.summary.tone || "ok"} : "",
            onDrop: (paths, {plan}) => {
                problem = "";
                if (menuOpen()) closeMenu(false);
                // What the server read while the files hovered, when it says
                // more than "a new sample": a project to open, a mask that
                // belongs to one of the samples already here, a channel list
                // with nothing to name. The drop router carries those out.
                const router = window.PlexoraDropRouter;
                if (router && router.claims(plan)) {
                    router.runPlan(plan, paths);
                    return;
                }
                if (paths.length > 1) {
                    window.PlexoraImportSample?.open({paths});
                    return;
                }
                // Dragged out of this computer's own Explorer or Finder, so
                // it is on this computer whatever the chip said.
                if (isRemote()) location.setWhere(LOCAL, null);
                take(paths[0]);
            },
        });
    }

    paint();
});
