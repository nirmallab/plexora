/**
 * browsePicker.js -- shared client for POST /browse_path (see
 * server/utils/native_dialog.py), which pops a native OS file/folder dialog
 * on the machine the server runs on and hands back the chosen path. Used by
 * both the quick-view landing page and the upload page's per-field "Browse..."
 * buttons; only works when there's a real desktop session for the dialog to
 * appear on, so every caller needs an `onUnavailable` fallback (e.g. a manual
 * text input) for the headless/remote-server case.
 */

/**
 * @function browseForPath - opens a native file ("file") or folder
 * ("directory") dialog and calls back with the chosen path.
 * @param mode - "file" or "directory"
 * @param filter - narrows the file-type dropdown for mode="file": one of
 *   "image", "csv", "h5ad", or "any" (default -- just "All files"). Ignored
 *   for mode="directory". Must match one of native_dialog.py's FILTERS keys.
 * @param onPicked - called with the chosen path string; not called if the
 *   user cancels the dialog (result.path === null)
 * @param onUnavailable - called (with the error) if the dialog itself
 *   couldn't be shown at all -- no desktop session, tkinter missing, etc.
 */
async function browseForPath({mode = "file", filter = "any", onPicked, onUnavailable} = {}) {
    try {
        const response = await fetch(plexoraUrl("browse_path"), {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mode: mode, filter: filter}),
        });
        const result = await response.json();
        if (!response.ok) {
            throw new Error(result.error);
        }
        if (result.path && onPicked) {
            onPicked(result.path);
        }
        // result.path === null just means the user cancelled the dialog.
    } catch (error) {
        if (onUnavailable) {
            onUnavailable(error);
        }
    }
}

/**
 * @function attachBrowseButton - wires a "Browse..." button to fill a text
 * input with the picked path, dispatching an `input` event afterward so any
 * existing onkeyup/oninput live-validation on that field runs unchanged.
 */
function attachBrowseButton(buttonEl, inputEl, {mode = "file", filter = "any"} = {}) {
    if (!buttonEl || !inputEl) return;
    buttonEl.addEventListener("click", () => {
        browseForPath({
            mode,
            filter,
            onPicked: (path) => {
                inputEl.value = path;
                inputEl.dispatchEvent(new Event("input"));
                inputEl.dispatchEvent(new Event("keyup"));
            },
            // A dialog that cannot be shown must say so. This used to swallow
            // the error, which turned a rejected filter name into a button
            // that looked ordinary and did nothing when clicked -- with the
            // path input right next to it, there was no way to tell the
            // difference between "no dialog here" and "this button is broken".
            onUnavailable: (error) => {
                console.error("browsePicker: could not open the file dialog.", error);
                window.PlexoraStatus?.begin?.("Browse")?.fail?.(
                    error?.message || "Could not open the file browser — type the path instead.");
            },
        });
    });
}
