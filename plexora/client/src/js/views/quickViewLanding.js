/**
 * quickViewLanding.js -- wires the home page (index.html's {% else %} branch,
 * shown when no datasource is loaded yet). A path is always POSTed to
 * /quick_view -- never the file's bytes, so huge OME-TIFFs load instantly
 * instead of being copied over HTTP.
 *
 * The page is one vertical run of four things, in the order they are needed:
 * which machine, which image, the typed way in, and the way out to the full
 * import. Everything here serves that order.
 *
 *   1. **Which machine.** The same Local/Remote switch every data field in
 *      Plexora carries, mounted ONCE above the page instead of inside a row.
 *      It governs both controls below it, which is the whole reason it moved:
 *      this page takes one image, so a page-level switch and a field-level
 *      switch are the same switch, and rendering two of them would be asking
 *      the same question twice.
 *
 *   2. **Which image.** Select File / Select Folder, always both, on every
 *      platform. An OME-Zarr image is a folder and an OME-TIFF is a file, and
 *      which one you have is a fact about your FORMAT -- so the example line
 *      under each word answers it rather than asking. Pressing a half opens
 *      that machine's own native dialog (/browse_path), or the in-app listing
 *      where there is no desktop to open one on, which is every cluster.
 *
 *      This used to be one big target asking for a dialog that takes either
 *      kind -- which only macOS has. Everywhere else the question came back as
 *      a popup after the click, or as halves swapped in by a capability probe
 *      on load. Asking it up front, in the control itself, costs macOS one
 *      decision and buys every platform the same page and no probe at all.
 *
 *   3. **The typed way in.** A path box and Load, for the case where the path
 *      is already on a clipboard or in a terminal scrollback. No Browse button
 *      beside it: the pair above IS this page's file browser, and a second
 *      route to the same dialog made the big obvious target look like the
 *      slower of the two.
 */
PlexoraPage.register(function () {
    const openPanel = document.getElementById("quick_view_open");
    const pathInput = document.getElementById("quick_view_path_input");
    const loadButton = document.getElementById("quick_view_path_load");
    const status = document.getElementById("quick_view_status");
    const whereMount = document.getElementById("quick_view_where_control");
    const whereStatus = document.getElementById("quick_view_where_status");
    const whereCaption = document.getElementById("quick_view_where_caption");

    if (!openPanel || !pathInput) {
        return;
    }

    // Which machine this image is on, for the whole page. `mount`/`statusMount`
    // put the switch in the row of its own above the File/Folder pair rather
    // than inside the path row -- see services/dataLocation.js, where those two
    // options are the exception this page is.
    let location = null;
    if (window.PlexoraDataLocation && window.PlexoraDataLocation.available()) {
        try {
            location = window.PlexoraDataLocation.attach(pathInput, {
                kind: "image",
                mount: whereMount,
                statusMount: whereStatus,
                onChange: () => {
                    paintCaption();
                    pathInput.dispatchEvent(new Event("input"));
                },
            });
        } catch (error) {
            console.error("quickViewLanding: no location switch.", error);
        }
    }

    /**
     * The word beside the switch. Two letters standing alone on a landing page
     * say nothing -- on a form the box they sit against supplies the context,
     * and here there is no box next to them.
     *
     * Local only. On Remote the switch's own place button occupies this spot
     * and names the machine, and unlike this caption it is clickable, which is
     * how the machine gets changed without toggling back through Local.
     */
    function paintCaption() {
        if (!whereCaption) return;
        const local = !location || location.isLocal();
        whereCaption.hidden = !local;
        whereCaption.textContent = local ? "this computer" : "";
    }

    /** What /quick_view should be given: a path, or a node address. */
    function submittedPath() {
        return location ? location.submitValue() : pathInput.value.trim();
    }

    /** Whether this server can answer "does that file exist?" about it. */
    function checkable() {
        return !location || location.isPlainPath();
    }

    /**
     * The status line. Always in the DOM and empty when it has nothing to say
     * (`.quick-view-status:empty` hides it) rather than toggled with `hidden`:
     * a live region that appears and gains its text in the same tick is the
     * shape screen readers miss.
     */
    function setStatus(message, isError) {
        status.textContent = message || "";
        status.classList.toggle("error", Boolean(message) && Boolean(isError));
    }

    //: The two halves, held rather than looked up: this page builds them once
    //: and unconditionally, so there is no swap to re-find them after.
    const halves = [];

    function setBusy(busy) {
        // Real buttons, so `disabled` rather than the pointer-events trick the
        // single dropzone used -- and it is needed either way: a second press
        // during the load submits the same slide again.
        halves.forEach((half) => { half.disabled = busy; });
        loadButton.disabled = busy || !submittedPath();
    }

    //: What the status line says between the click and the dialog appearing.
    //: Named because clearing it has to be able to tell it apart from whatever
    //: replaced it -- see clearOpening().
    const OPENING = "Opening file browser...";

    /**
     * Take the "Opening..." line down, but only if it is still the one there.
     *
     * By the time this runs the dialog has either appeared -- in which case
     * the line has said everything it can -- or the whole attempt is over. In
     * both cases something else may already have written to the status:
     * `submitQuickView` puts "Loading <file>..." there the instant a path
     * comes back, and `browseForImage` puts an error there. Neither may be
     * wiped by a line that is only still around because it was never cleared.
     */
    function clearOpening() {
        if (status.textContent === OPENING) {
            setStatus(null);
        }
    }

    async function submitQuickView(path) {
        setBusy(true);
        // The name at the end, whether that came off a path or a node address
        // -- both end in the thing the user recognises.
        setStatus("Loading " + path.split(/[\\/]/).pop() + "...", false);
        try {
            const response = await fetch(plexoraUrl("quick_view"), {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({path: path}),
            });
            const result = await response.json();
            if (!response.ok || !result.success) {
                throw new Error(result.error || "Could not load that image.");
            }
            PlexoraRouter.go(result.redirect);
        } catch (error) {
            setBusy(false);
            setStatus(error.message || "Could not load that image.", true);
        }
    }

    /**
     * Browse for the image and open it, in one gesture -- the panel's whole
     * point, and why the half that was pressed passes its own kind straight
     * through rather than asking again.
     *
     * The exception is a path this server cannot stat: on another machine the
     * path has to become a node address before it means anything, so it goes
     * into the box and the switch takes it from there.
     */
    async function browseForImage(mode) {
        setStatus(OPENING, false);
        // The native dialog blocks the server for as long as it is up (see
        // native_dialog.py -- it waits, with a 300s timeout), and the listing
        // picker is a modal this awaits. So browseForPath returning is the
        // moment the dialog CLOSED, not the moment it opened: waiting for it
        // left "Opening file browser..." sitting under an open Finder window
        // for the whole time it was there -- and, if the user cancelled,
        // sitting there for good, because only onPicked ever cleared it.
        //
        // A short timer is the only signal there is for "it is on screen now",
        // and it is enough: the line exists to answer "did my click do
        // anything", which the dialog itself answers the instant it appears.
        const opened = setTimeout(clearOpening, 1500);
        try {
            await browseForPath({
                // Never "any", so the "file or folder?" popup this page used to
                // raise can no longer happen: the half that was pressed has
                // already answered it. That is also why no `anchorEl` is passed
                // -- there is nothing left to anchor.
                mode,
                // Read by the listing picker; the native dialog narrows its own
                // file-type dropdown with it.
                filter: "image",
                // Asked at click time: the switch can be flipped long after this
                // was wired, and it decides whose filesystem is browsed.
                node: location ? location.browseNode() : null,
                // And where the listing picker opens if it is the one that runs:
                // whatever is already in the box, so a corrected filename starts
                // in the folder it came from.
                start: pathInput.value.trim(),
                onPicked: (path) => {
                    setStatus(null);
                    if (!checkable()) {
                        // Straight into the box, which dispatches `change` and
                        // sends the share on its way.
                        pathInput.value = path;
                        pathInput.dispatchEvent(new Event("change", { bubbles: true }));
                        pathInput.dispatchEvent(new Event("input"));
                        return;
                    }
                    submitQuickView(path);
                },
                onUnavailable: () => {
                    // Not "there is no desktop here" -- that case never reaches
                    // this, because browseForPath answers it with the listing
                    // picker. What is left is a browser that could not reach the
                    // server at all, or a node that would not answer.
                    setStatus("The file browser could not be opened — type the full path instead.", true);
                    pathInput.focus();
                },
            });
        } finally {
            clearTimeout(opened);
            clearOpening();
        }
    }

    // The page's primary action. Built from the shared control rather than
    // written into the template, so the two halves, their icons and the format
    // examples under them are defined in exactly one place -- the same place
    // every Browse button on the import form gets them from.
    //
    // "Select File" / "Select Folder" rather than the row variant's bare
    // "File" / "Folder": in a row the control qualifies the box beside it, and
    // standing alone it is a button, which is named for what pressing it does.
    const panel = buildSplitControl("image", browseForImage,
                                    { file: "Select File",
                                      directory: "Select Folder" });
    panel.classList.add("is-panel");
    panel.setAttribute("aria-label", "Select an image file or an image folder");
    // `role="group"` gets Tab between the halves for free; the arrow keys are
    // what the shared control adds on top, and they have to be asked for.
    panel.addEventListener("keydown", (event) => {
        stepBetweenHalves(panel, event);
    });
    openPanel.appendChild(panel);
    halves.push(...panel.querySelectorAll(".browse-kind-half"));

    let validationRequestId = 0;
    pathInput.addEventListener("input", async () => {
        const path = pathInput.value.trim();
        loadButton.disabled = true;
        if (!checkable()) {
            // The box holds a path on another machine, which this server
            // cannot stat and would call missing. The share is the check
            // there: the switch either gets an address back or says why not,
            // and until it does there is nothing to load.
            const blocked = location.blocking();
            loadButton.disabled = Boolean(blocked) || !submittedPath();
            return;
        }
        pathInput.classList.remove("is-invalid");
        if (!path) {
            return;
        }
        const requestId = ++validationRequestId;
        try {
            // `check_path_existence`, not `check_file_existence`: an OME-Zarr
            // image is a directory, and the file-only check calls one missing.
            const response = await fetch(plexoraUrl("check_path_existence"), {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({path: path}),
            });
            const { exists } = await response.json();
            if (requestId !== validationRequestId) {
                return;
            }
            pathInput.classList.toggle("is-invalid", !exists);
            loadButton.disabled = !exists;
        } catch (error) {
            if (requestId === validationRequestId) {
                pathInput.classList.add("is-invalid");
            }
        }
    });

    pathInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !loadButton.disabled) {
            submitQuickView(submittedPath());
        }
    });

    loadButton.addEventListener("click", () => {
        const path = submittedPath();
        if (path) {
            submitQuickView(path);
        }
    });

    paintCaption();
});
