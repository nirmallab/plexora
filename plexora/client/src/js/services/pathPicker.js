/**
 * pathPicker.js -- choosing a file on a machine that has no desktop.
 *
 * The native dialog (browsePicker.js) is the right control whenever there is a
 * screen for it to appear on. On a compute node there is not, and there never
 * will be: the server is running in a batch job on a machine with no display,
 * and `browse_for_path` there does not fail so much as wait for a person who
 * cannot be there. That used to leave one option -- know the path already and
 * type it -- which is a poor answer for somebody looking for a file they last
 * saw three months ago in a directory they named after a date.
 *
 * So this is the fallback: a listing of one directory at a time, from
 * /list_dir, drawn as a modal. It reads names, sizes and which entries are
 * folders. Never bytes.
 *
 * Two things it deliberately is not. It is not a file manager -- there is no
 * rename, no delete, no upload, and the server route has no code for any of
 * them. And it is not a replacement for typing a path: the box at the top
 * takes one, because somebody who knows where their file is should not have to
 * click through six directories to say so.
 */
window.PlexoraPathPicker = (function () {
    //: What each filter accepts, so the listing greys out the rest rather than
    //: letting somebody pick a file the form is going to refuse. Kept in step
    //: with native_dialog.py's _TK_FILTERS -- the two describe the same
    //: choices, one for each kind of picker.
    const EXTENSIONS = {
        image: [".tif", ".tiff", ".ome.tif", ".ome.tiff", ".svs", ".qptiff",
                ".png", ".jpg", ".jpeg"],
        csv: [".csv"],
        h5ad: [".h5ad"],
        data: [".csv", ".tsv", ".txt", ".h5ad"],
        channels: [".csv", ".tsv", ".txt", ".xlsx", ".xlsm"],
        any: null,
    };

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function accepts(name, filter) {
        const allowed = EXTENSIONS[filter];
        if (!allowed) return true;
        const lowered = name.toLowerCase();
        return allowed.some((suffix) => lowered.endsWith(suffix));
    }

    /** Bytes as something a person reads at a glance. */
    function readableSize(bytes) {
        if (bytes === null || bytes === undefined) return "";
        const units = ["B", "KB", "MB", "GB", "TB"];
        let value = bytes;
        let unit = 0;
        while (value >= 1024 && unit < units.length - 1) {
            value /= 1024;
            unit += 1;
        }
        return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`;
    }

    /**
     * @function pick - open the listing and resolve with a path, or null if
     *   the user closed it without choosing.
     * @param mode - "file" or "directory". In directory mode the chosen thing
     *   is the folder currently open, which is how a .zarr store is picked.
     * @param filter - one of EXTENSIONS' keys; greys out everything else.
     * @param start - the directory to open at. The user's home when absent.
     * @param title - what the dialog says it is for.
     */
    function pick({ mode = "file", filter = "any", start = "",
                    title = "Choose a file" } = {}) {
        const dialog = el("dialog", "path-picker");
        dialog.innerHTML = `
            <div class="path-picker-body">
                <h2 class="path-picker-title"></h2>
                <div class="path-picker-bar">
                    <button type="button" class="btn btn-secondary" data-action="up">Up</button>
                    <input type="text" class="form-control" data-role="path"
                           placeholder="/path/to/folder" spellcheck="false">
                    <button type="button" class="btn btn-secondary" data-action="go">Go</button>
                </div>
                <div class="path-picker-error" role="alert" hidden></div>
                <ul class="path-picker-list"></ul>
                <div class="path-picker-note"></div>
                <div class="path-picker-actions">
                    <button type="button" class="btn btn-secondary" data-action="cancel">Cancel</button>
                    <button type="button" class="btn btn-primary" data-action="choose"></button>
                </div>
            </div>
        `;
        dialog.querySelector(".path-picker-title").textContent = title;
        const list = dialog.querySelector(".path-picker-list");
        const pathInput = dialog.querySelector('[data-role="path"]');
        const error = dialog.querySelector(".path-picker-error");
        const note = dialog.querySelector(".path-picker-note");
        const choose = dialog.querySelector('[data-action="choose"]');
        choose.textContent = mode === "directory"
            ? "Use this folder" : "Choose";
        document.body.appendChild(dialog);

        let here = start || "";
        let selected = null;
        //: What `pick` will resolve with. Held here rather than passed to
        //: `resolve` at each call site because closing the dialog is the one
        //: exit every path shares -- Esc included, which the browser handles on
        //: its own and which would otherwise leave the caller waiting forever
        //: for an answer the user has already given.
        let answer = null;

        function finish(result) {
            answer = result;
            dialog.close();
        }

        function setSelected(path, row) {
            selected = path;
            list.querySelectorAll(".is-selected")
                .forEach((other) => other.classList.remove("is-selected"));
            if (row) row.classList.add("is-selected");
            choose.disabled = mode === "file" && !selected;
        }

        async function show(path) {
            error.hidden = true;
            let payload;
            try {
                const response = await fetch(plexoraUrl("list_dir"), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ path: path || "" }),
                });
                payload = await response.json();
                if (!response.ok) throw new Error(payload.error);
            } catch (e) {
                error.textContent = e?.message || "That folder could not be read.";
                error.hidden = false;
                return;
            }

            here = payload.path;
            pathInput.value = here;
            // In directory mode the answer is wherever we are standing, so
            // opening a folder IS choosing it -- there is nothing to select.
            setSelected(mode === "directory" ? here : null, null);

            list.replaceChildren();
            (payload.entries || []).forEach((entry) => {
                const usable = entry.is_dir || accepts(entry.name, filter);
                const row = el("li", "path-picker-row");
                if (entry.is_dir) row.classList.add("is-folder");
                if (!usable) row.classList.add("is-muted");
                row.append(el("span", "path-picker-name", entry.name));
                row.append(el("span", "path-picker-size",
                              entry.is_dir ? "" : readableSize(entry.size)));
                const full = here.endsWith("/")
                    ? `${here}${entry.name}` : `${here}/${entry.name}`;
                if (entry.is_dir) {
                    // One click opens a folder rather than selecting it: a
                    // .zarr store is reached in directory mode, where the
                    // folder you are standing in is the answer.
                    row.addEventListener("click", () => show(full));
                } else if (usable) {
                    row.addEventListener("click", () => setSelected(full, row));
                    row.addEventListener("dblclick", () => {
                        setSelected(full, row);
                        finish(full);
                    });
                }
                list.appendChild(row);
            });
            note.textContent = payload.truncated
                ? `Showing the first ${payload.entries.length} entries — type a `
                  + "path above to go straight there."
                : "";
        }

        dialog.querySelector('[data-action="cancel"]')
            .addEventListener("click", () => finish(null));
        dialog.querySelector('[data-action="choose"]')
            .addEventListener("click", () => finish(selected));
        dialog.querySelector('[data-action="up"]')
            .addEventListener("click", () => {
                const parent = here.replace(/\/+$/, "").replace(/\/[^/]*$/, "");
                show(parent || "/");
            });
        dialog.querySelector('[data-action="go"]')
            .addEventListener("click", () => show(pathInput.value.trim()));
        pathInput.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                show(pathInput.value.trim());
            }
        });

        const promise = new Promise((resolve) => {
            dialog.addEventListener("close", () => {
                dialog.remove();
                resolve(answer);
            });
        });

        choose.disabled = mode === "file";
        dialog.showModal();
        show(here);
        return promise;
    }

    return { pick, accepts };
})();
