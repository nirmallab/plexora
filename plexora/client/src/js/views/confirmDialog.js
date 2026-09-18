/**
 * confirmDialog.js -- core's way of asking a short question.
 *
 * `window.confirm` was doing this job on the Open Project page and it is the
 * wrong instrument for it in three separate ways. It is drawn by the browser,
 * in the browser's typeface, anchored to the top of the window rather than to
 * what is being asked about, and it carries the page's ORIGIN across the top --
 * so the last thing a user reads before deleting a project is "127.0.0.1:8848
 * says". It offers exactly two answers named OK and Cancel, which is not the
 * shape of every question. And it blocks the main thread.
 *
 * Bootstrap's modal was the other thing here, and it is heavier than a
 * question deserves: markup in the template for every dialog the page might
 * ever show, a data-attribute handshake to open it, and a `relatedTarget` to
 * carry which project the answer is about.
 *
 * A native <dialog>: modal, focus-trapped and Esc-dismissible without a line of
 * script, painted in the app's own type, and unable to end up behind anything
 * the way a positioned div can. This is Figure Builder's `figureConfirm.js`
 * ported to core -- the reasoning there is the reasoning here, and the two are
 * deliberately the same shape so neither surprises somebody who has read the
 * other.
 *
 * One dialog element per question, torn down when it closes, rather than one
 * kept and refilled. A kept dialog has to hold the pending resolver between
 * calls, and the moment there are two questions in flight -- which a modal is
 * supposed to prevent and a bug will eventually manage -- that single slot is
 * either a promise resolved twice or a promise resolved never. Per call, the
 * resolver is a closure and the question cannot be confused with any other.
 *
 * Mounted on document.body, and its CSS lives in main.css rather than in a
 * page stylesheet: the app shell disables a page's own stylesheet when it
 * navigates away (adoptStyles), and a dialog that could only be styled on one
 * page is a dialog that renders unstyled everywhere else.
 */
window.PlexoraConfirm = (function () {
    function escapeHtml(value) {
        return String(value).replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        }[c]));
    }

    const KINDS = { danger: " plx-button-danger", primary: " plx-button-primary" };

    /** Body text, as paragraphs. A blank line is a paragraph break, which is
     *  how these messages were already written for `window.confirm`. */
    function paragraphs(body) {
        const lines = Array.isArray(body) ? body : String(body || "").split("\n\n");
        return lines.filter((line) => String(line).trim())
            .map((line) => `<p class="plx-confirm-body">${escapeHtml(String(line))}</p>`)
            .join("");
    }

    /**
     * Open a dialog and settle once, on `close`.
     *
     * `close` and not the click handler, because a button is not the only way
     * out: Escape fires `cancel` then `close`, and so does the browser's own
     * dismissal. One exit means one place the promise is settled and no path
     * that leaves it pending.
     */
    function run(dialog, read) {
        document.body.appendChild(dialog);
        return new Promise((resolve) => {
            let answer = null;
            dialog.addEventListener("click", (event) => {
                const button = event.target.closest?.("[data-choice]");
                if (!button) return;
                answer = read(Number(button.dataset.choice), dialog);
                dialog.close();
            });
            dialog.addEventListener("close", () => {
                dialog.remove();
                resolve(answer);
            });
            if (typeof dialog.showModal !== "function") {
                // Nothing was asked, so nothing can be answered -- and a caller
                // left awaiting a reply that never comes is worse than any
                // dialog. A dismissal, which is the answer that changes
                // nothing.
                dialog.remove();
                resolve(null);
                return;
            }
            dialog.showModal();
        });
    }

    /**
     * The general form: any number of answers, each with its own value.
     *
     * Resolves the chosen `value`, or **null** when the dialog is dismissed
     * without one -- Escape, or the backdrop. Null and not the first choice's
     * value, so "the user did not answer" is a state a caller can test for.
     */
    function choose({ title, body, choices }) {
        const buttons = choices.map((choice, index) =>
            `<button type="button" data-choice="${index}"
                     class="plx-button${KINDS[choice.kind] || ""}"
                     ${choice.focus ? "autofocus" : ""}
             >${escapeHtml(choice.label)}</button>`).join("");

        const dialog = document.createElement("dialog");
        dialog.className = "plx-dialog plx-confirm";
        dialog.innerHTML = `<h2 class="plx-dialog-title">${escapeHtml(title)}</h2>
            ${paragraphs(body)}
            <div class="plx-dialog-actions">${buttons}</div>`;
        return run(dialog, (index) => choices[index].value);
    }

    /**
     * Two answers, one of them destructive. Resolves true or false, and false
     * for Escape and for the backdrop -- so a dismissed question is always the
     * answer that changes nothing.
     */
    function ask({ title, body, confirm, cancel, danger }) {
        return choose({
            title,
            body,
            choices: [
                { value: false, label: cancel || "Cancel", focus: true },
                { value: true, label: confirm || "OK",
                  kind: danger === false ? "primary" : "danger" },
            ],
        }).then((answer) => answer === true);
    }

    /** A statement with nothing to decide. Still a dialog rather than a toast,
     *  because it is said in answer to something the user just tried to do and
     *  it has to be seen before they try again. */
    function tell({ title, body }) {
        return choose({
            title, body,
            choices: [{ value: true, label: "OK", kind: "primary", focus: true }],
        });
    }

    /**
     * One line of text, or null.
     *
     * Null for Escape, for Cancel, and for an empty box -- "" is not a dataset
     * name, and a caller that had to tell an empty confirmation from a
     * dismissal for a field with no valid empty value would be telling apart
     * two things that mean the same thing here.
     *
     * Enter submits, because a one-field form where Enter does nothing is a
     * form people press Enter in twice and then reach for the mouse.
     */
    function prompt({ title, body, value, placeholder, confirm, cancel }) {
        const dialog = document.createElement("dialog");
        dialog.className = "plx-dialog plx-confirm plx-prompt";
        dialog.innerHTML = `<h2 class="plx-dialog-title">${escapeHtml(title)}</h2>
            ${paragraphs(body)}
            <input type="text" class="plx-prompt-input" autocomplete="off"
                   spellcheck="false" value="${escapeHtml(value || "")}"
                   placeholder="${escapeHtml(placeholder || "")}" autofocus>
            <div class="plx-dialog-actions">
                <button type="button" data-choice="0" class="plx-button"
                        >${escapeHtml(cancel || "Cancel")}</button>
                <button type="button" data-choice="1" class="plx-button plx-button-primary"
                        >${escapeHtml(confirm || "Save")}</button>
            </div>`;

        const input = dialog.querySelector(".plx-prompt-input");
        input.addEventListener("keydown", (event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            dialog.querySelector('[data-choice="1"]').click();
        });
        // Selected rather than merely focused: a rename opens on the current
        // name, and the commonest thing to do with it is replace it.
        requestAnimationFrame(() => input.select?.());

        return run(dialog, (index, el) => {
            if (index !== 1) return null;
            const text = el.querySelector(".plx-prompt-input").value.trim();
            return text || null;
        });
    }

    /** Whether a modal dialog currently owns the window. Page-level keyboard
     *  shortcuts bind to `window`, and a <dialog> traps FOCUS but not keydown --
     *  so a page that clears its selection on Escape would clear it while a
     *  confirmation was up, and the tag guard those handlers carry does not
     *  catch it because the focused element is a BUTTON. */
    function modalOpen() {
        return Boolean(document.querySelector("dialog[open]"));
    }

    return { ask, tell, choose, prompt, modalOpen, escapeHtml };
})();
