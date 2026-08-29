/**
 * quickViewLanding.js -- wires the home-page (index.html's {% else %}
 * branch, shown when no datasource is loaded yet). A path is always POSTed to
 * /quick_view -- never the file's bytes, so huge OME-TIFFs load instantly
 * instead of being copied over HTTP.
 *
 * Clicking the dropzone asks for a native OS file dialog on whichever machine
 * the switch is pointing at -- see /browse_path and
 * server/utils/native_dialog.py -- and falls back to the listing picker where
 * there is no desktop, which is every cluster.
 *
 * This page takes one image and nothing else, and it gets the same
 * Local/Remote switch every other data input has. "Take a quick look" should
 * not stop meaning anything the moment the slide is on a cluster: it is the
 * same act, and the same one gesture.
 */
PlexoraPage.register(function () {
    const dropzone = document.getElementById("quick_view_dropzone");
    const pathFallback = document.getElementById("quick_view_path_fallback");
    const pathInput = document.getElementById("quick_view_path_input");
    const browseButton = document.getElementById("quick_view_path_browse");
    const loadButton = document.getElementById("quick_view_path_load");
    const status = document.getElementById("quick_view_status");

    if (!dropzone) {
        return;
    }

    // Which machine this image is on. Mounts into `.quick-view-path-row`,
    // beside the box, exactly as it does on the import form.
    let location = null;
    if (window.PlexoraDataLocation && window.PlexoraDataLocation.available()) {
        try {
            location = window.PlexoraDataLocation.attach(pathInput, {
                kind: "image",
                onChange: () => pathInput.dispatchEvent(new Event("input")),
            });
        } catch (error) {
            console.error("quickViewLanding: no location switch.", error);
        }
    }

    /** What /quick_view should be given: a path, or a node address. */
    function submittedPath() {
        return location ? location.submitValue() : pathInput.value.trim();
    }

    /** Whether this server can answer "does that file exist?" about it. */
    function checkable() {
        return !location || location.isPlainPath();
    }

    function setStatus(message, isError) {
        if (!message) {
            status.hidden = true;
            status.textContent = "";
            status.classList.remove("error");
            return;
        }
        status.hidden = false;
        status.textContent = message;
        status.classList.toggle("error", !!isError);
    }

    function setBusy(busy) {
        dropzone.style.pointerEvents = busy ? "none" : "";
        loadButton.disabled = busy || !submittedPath();
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

    // The path row is always on the page now, so this only has to draw
    // attention to it and say why.
    function showPathFallback(message) {
        pathFallback.hidden = false;
        setStatus(message || null, !!message);
        pathInput.focus();
        pathInput.dispatchEvent(new Event("input"));
    }

    /**
     * Browse, then load in one gesture -- the dropzone's whole point.
     *
     * `fill` is what the row's own Browse button wants instead: put the path
     * in the box and let the switch share it, because on a machine that is not
     * this one the path has to become a node address before it means anything.
     */
    async function browseForImage({ fill = false } = {}) {
        setStatus("Opening file browser...", false);
        await browseForPath({
            mode: "file",
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
                if (fill || !checkable()) {
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
                // Not "there is no desktop here" any more -- that case never
                // reaches this, because browseForPath answers it with the
                // listing picker. What is left is a browser that could not
                // reach the server at all, or a node that would not answer.
                showPathFallback("The file browser could not be opened -- type the full path instead.");
            },
        });
    }

    dropzone.addEventListener("click", () => browseForImage());
    dropzone.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            browseForImage();
        }
    });
    if (browseButton) {
        browseButton.addEventListener("click", () => browseForImage({ fill: true }));
    }

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
            const response = await fetch(plexoraUrl("check_file_existence"), {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({path: path}),
            });
            const exists = await response.json();
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
});
