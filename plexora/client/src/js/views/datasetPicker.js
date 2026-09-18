/**
 * datasetPicker.js -- "which dataset?", asked once and answered in one click.
 *
 * Two callers: the Move to… button on the open-project page, and the Dataset
 * line on the import form, which asks the same question about a project that
 * does not exist yet. Hence `title`: the question differs, the answer does
 * not, and a second dialog that listed the same folders differently would be
 * a second place for the list to go wrong.
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
 * "New dataset…" names the folder in this dialog rather than handing off to a
 * second one. Chaining a prompt onto a picker means the answer to "which
 * folder?" is given in one modal and typed in another, and the Escape that
 * backs out of the second leaves the first already gone -- so the way back
 * from a mistyped name was to start the whole gesture again.
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
     * @param {?string} options.title the question, when it is not a move.
     * @param {?string} options.rootLabel what "in no dataset" is called here.
     * @returns {Promise<{kind:"dataset", id:string}|{kind:"root"}
     *                  |{kind:"new", name:string}|null>}
     *          null when the question was dismissed.
     */
    function choose({ datasets, count, exclude, allowRoot, allowNew,
                      title, rootLabel }) {
        const all = (datasets || []).filter((d) => d && d.id !== exclude);

        const dialog = document.createElement("dialog");
        dialog.className = "plx-dialog plx-picker";
        dialog.innerHTML = `
            <div class="plx-picker-head">
                <h2 class="plx-dialog-title">${escapeHtml(title
                    || `Move ${countPhrase(count || 1, "project")} to…`)}</h2>
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
            </button>
            <form class="plx-picker-newform" data-role="newform" hidden>
                <input type="text" data-role="newname" autocomplete="off"
                       spellcheck="false" placeholder="Melanoma Cohort"
                       aria-label="Name for the new dataset" maxlength="120">
                <button class="plx-button plx-button-primary" type="submit"
                        data-role="create">Create</button>
            </form>` : ""}`;

        const list = dialog.querySelector('[data-role="list"]');
        const search = dialog.querySelector('[data-role="search"]');
        const newButton = dialog.querySelector('[data-role="new"]');
        const newForm = dialog.querySelector('[data-role="newform"]');
        const newName = dialog.querySelector('[data-role="newname"]');

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
                    <span class="plx-picker-label">${escapeHtml(rootLabel || "All projects")}</span>
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
                if (role === "close") return settle(null);
                // Not an answer: it swaps the button for the box that gives
                // one. Seeded with whatever was typed in the search, because
                // the commonest way to reach "New dataset…" is to look for a
                // folder, not find it, and decide to make it.
                if (role === "new") {
                    newButton.hidden = true;
                    newForm.hidden = false;
                    newName.value = search ? search.value.trim() : "";
                    newName.focus();
                    newName.select();
                }
            });

            // A submit, so Enter answers. The list above is a column of
            // buttons and the box is one line: anybody who types a name
            // presses Enter, and a form that ignored it would look broken
            // while the Create button sat beside the cursor doing nothing.
            newForm?.addEventListener("submit", (event) => {
                event.preventDefault();
                const name = newName.value.trim();
                // Silent rather than an error: the box is still open with the
                // cursor in it, which already says what is missing.
                if (name) settle({ kind: "new", name });
            });

            // Escape backs out of naming before it closes the dialog -- one
            // key, undoing one step at a time, so a mistyped name costs the
            // name and not the folder list behind it.
            newForm?.addEventListener("keydown", (event) => {
                if (event.key !== "Escape") return;
                event.stopPropagation();
                event.preventDefault();
                newForm.hidden = true;
                newButton.hidden = false;
                newName.value = "";
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
