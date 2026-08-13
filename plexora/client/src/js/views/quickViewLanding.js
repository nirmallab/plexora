/**
 * quickViewLanding.js -- wires the home-page (index.html's {% else %}
 * branch, shown when no datasource is loaded yet). The full path to a
 * local image is always POSTed to /quick_view -- never the file's bytes,
 * so huge OME-TIFFs load instantly instead of being copied over HTTP.
 *
 * Clicking the dropzone asks the *server* (which runs on the same machine,
 * launched from a terminal or Jupyter) to pop a native OS file dialog and
 * hand back the real path -- see /browse_path and
 * server/utils/native_dialog.py. That only works when there's a real
 * desktop session for the dialog to appear on, so the manual path input is
 * a soft fallback, hidden until the browse call actually fails.
 */
(function () {
    const dropzone = document.getElementById("quick_view_dropzone");
    const pathFallback = document.getElementById("quick_view_path_fallback");
    const pathInput = document.getElementById("quick_view_path_input");
    const loadButton = document.getElementById("quick_view_path_load");
    const status = document.getElementById("quick_view_status");

    if (!dropzone) {
        return;
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
        loadButton.disabled = busy || !pathInput.value.trim();
    }

    async function submitQuickView(path) {
        setBusy(true);
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
            window.location.href = result.redirect;
        } catch (error) {
            setBusy(false);
            setStatus(error.message || "Could not load that image.", true);
        }
    }

    // Reveals the manual path input -- used only once the native dialog has
    // failed to produce a full path on its own.
    function showPathFallback(message) {
        pathFallback.hidden = false;
        setStatus(message || null, !!message);
        pathInput.focus();
        pathInput.dispatchEvent(new Event("input"));
    }

    async function browseForImage() {
        setStatus("Opening file browser...", false);
        await browseForPath({
            mode: "file",
            filter: "image",
            onPicked: (path) => {
                setStatus(null);
                submitQuickView(path);
            },
            onUnavailable: () => {
                // Soft fallback: no desktop session for a dialog to appear on
                // (headless/remote server, no display, tkinter missing, ...).
                showPathFallback("Automatic browsing isn't available here -- paste the full path instead.");
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

    let validationRequestId = 0;
    pathInput.addEventListener("input", async () => {
        const path = pathInput.value.trim();
        pathInput.classList.remove("is-invalid");
        loadButton.disabled = true;
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
            submitQuickView(pathInput.value.trim());
        }
    });

    loadButton.addEventListener("click", () => {
        const path = pathInput.value.trim();
        if (path) {
            submitQuickView(path);
        }
    });
})();
