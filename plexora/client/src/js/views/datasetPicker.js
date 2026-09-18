/**
 * datasetPicker.js -- "Move to…", asked once and answered in one click.
 *
 * Drag and drop is the fast way to move a project into a dataset and it is not
 * the only way it should be possible: the folder may be scrolled off screen,
 * the user may be on a trackpad, and dragging is not available to anybody
 * working by keyboard. So the selection bar has a Move to… button, and this is
 * what it opens.
 *
 * A list of folders, each a single click. No <select> and no Move button
 * beside it: a dropdown of names in a narrow control, plus a second press to
 * confirm what was already chosen, is two steps for "that one" -- and the
 * second step is the one people forget, leaving a dialog that looks answered
 * and has done nothing. The action is not destructive and getting it wrong
 * costs one more move, which is what makes a single click the right cost.
 *
 * No Cancel button either. The × closes it and so does Escape, and a modal
 * with a large "do nothing" button ranged beside the real answers makes
 * "nothing" look like a choice on the same footing as the others.
 *
 * Modelled on Figure Builder's destination picker, down to the native
 * <dialog>: modal, focus-trapped and Esc-dismissible without a line of script,
 * one element per question, torn down when it closes.
 */
window.PlexoraDatasetPicker = (function () {
    /** Past this many folders the list stops being scannable and needs a box
     *  to narrow it. Under it, a search field is one more control between the
     *  user and an answer they can already see. */
    const FILTER_AT = 8;

    const escapeHtml = (value) => window.PlexoraConfirm.escapeHtml(value);

    function countPhrase(n, noun) {
        return `${n} ${noun}${n === 1 ? "" : "s"}`;
    }

    /**
     * Ask where these projects should go.
     *
     * @param {object} options
     * @param {Array}  options.datasets every dataset, as `GET /datasets` gives
     *        them: {id, name, projectCount}.
     * @param {number} options.count how many projects are moving, for the title.
     * @param {?string} options.exclude a dataset to leave out -- the one they
     *        are already in, which is not a move.
     * @param {boolean} options.allowRoot offer "All projects", i.e. no dataset.
     *        Only when something in the selection is actually in one.
     * @param {boolean} options.allowNew offer making a folder from here.
     * @returns {Promise<{kind:"dataset", id:string}|{kind:"root"}|{kind:"new"}|null>}
     *          null when the question was dismissed.
     */
    function choose({ datasets, count, exclude, allowRoot, allowNew }) {
        const all = (datasets || []).filter((d) => d && d.id !== exclude);

        const dialog = document.createElement("dialog");
        dialog.className = "plx-dialog plx-picker";
        dialog.innerHTML = `
            <div class="plx-picker-head">
                <h2 class="plx-dialog-title">Move ${escapeHtml(countPhrase(count || 1, "project"))} to…</h2>
                <button class="plx-picker-close" type="button" data-role="close"
                        title="Close" aria-label="Close">
                    <span class="fas fa-xmark" aria-hidden="true"></span>
                </button>
            </div>
            ${all.length > FILTER_AT ? `
            <div class="plx-picker-search">
                <span class="fas fa-magnifying-glass" aria-hidden="true"></span>
                <input type="search" data-role="search" autocomplete="off"
                       spellcheck="false" placeholder="Search datasets"
                       aria-label="Search datasets">
            </div>` : ""}
            <div class="plx-picker-list" data-role="list"></div>
            <p class="plx-picker-none" data-role="noresults" hidden>No datasets match.</p>
            ${allowNew ? `
            <button class="plx-picker-new" type="button" data-role="new">
                <span class="fas fa-folder-plus" aria-hidden="true"></span>
                <span>New dataset…</span>
            </button>` : ""}`;

        const list = dialog.querySelector('[data-role="list"]');
        const search = dialog.querySelector('[data-role="search"]');

        function row(dataset, index) {
            return `<button class="plx-picker-row" type="button"
                            data-dataset-id="${escapeHtml(dataset.id)}"
                            ${index === 0 ? "autofocus" : ""}>
                <span class="fas fa-folder plx-picker-icon" aria-hidden="true"></span>
                <span class="plx-picker-label">${escapeHtml(dataset.name)}</span>
                <span class="plx-picker-count">${escapeHtml(
                    countPhrase(dataset.projectCount || 0, "project"))}</span>
            </button>`;
        }

        function paint(matches) {
            // "All projects" first when it applies: taking something out of a
            // folder is the one answer here that is not a folder, and burying
            // it under a list of them is where people go looking for a Remove
            // button that does not exist.
            const root = allowRoot ? `<button class="plx-picker-row" type="button"
                        data-role="root">
                    <span class="fas fa-inbox plx-picker-icon" aria-hidden="true"></span>
                    <span class="plx-picker-label">All projects</span>
                    <span class="plx-picker-count">no dataset</span>
                </button>` : "";
            list.innerHTML = root + matches.map(row).join("");
            const none = dialog.querySelector('[data-role="noresults"]');
            if (none) none.hidden = Boolean(matches.length || root);
        }

        paint(all);

        return new Promise((resolve) => {
            let answer = null;
            const settle = (value) => { answer = value; dialog.close(); };

            dialog.addEventListener("click", (event) => {
                const target = event.target.closest?.("[data-role], [data-dataset-id]");
                if (!target) return;
                const id = target.dataset.datasetId;
                if (id) return settle({ kind: "dataset", id });
                const role = target.dataset.role;
                if (role === "root") return settle({ kind: "root" });
                if (role === "new") return settle({ kind: "new" });
                if (role === "close") return settle(null);
            });

            search?.addEventListener("input", () => {
                const query = search.value.trim().toLowerCase();
                paint(query
                    ? all.filter((d) => d.name.toLowerCase().includes(query))
                    : all);
            });
            // Escape inside a search field clears it in some browsers and
            // closes the dialog in others. Neither is wrong, but "clear the box
            // I am typing in" is what the key means while the box has focus.
            search?.addEventListener("keydown", (event) => {
                if (event.key !== "Escape" || !search.value) return;
                event.stopPropagation();
                event.preventDefault();
                search.value = "";
                paint(all);
            });

            dialog.addEventListener("close", () => {
                dialog.remove();
                resolve(answer);
            });
            document.body.appendChild(dialog);
            if (typeof dialog.showModal !== "function") {
                dialog.remove();
                resolve(null);
                return;
            }
            dialog.showModal();
        });
    }

    return { choose };
})();
