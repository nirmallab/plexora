/**
 * The workspace's own way of asking a question.
 *
 * `window.confirm` was doing this job and it is the wrong instrument for it in
 * three separate ways. It is drawn by the browser, in the browser's typeface,
 * anchored to the top of the window rather than to the figure, and it carries
 * the page's ORIGIN across the top -- so the last thing a user reads before
 * deleting a panel is "127.0.0.1:8848 says". It offers exactly two answers
 * named OK and Cancel, which is not the shape of every question: deleting a
 * page that holds panels has three answers, and squeezing it into two is what
 * left Cancel meaning "delete the page but keep the panels" rather than
 * "stop". And it blocks the main thread, so an autosave in flight while the
 * box is up is an autosave that finishes when the user finally answers.
 *
 * A native <dialog>, which is the same choice the export dialog and the
 * capture destination chooser already made here: modal, focus-trapped and
 * Esc-dismissible without a line of script, painted in the app's own type, and
 * unable to end up behind the canvas the way a positioned div can.
 *
 * Mounted INSIDE #fb_workspace and not on <body>. Everything about this
 * workspace's palette is declared on `.fb-workspace` -- it is the one light
 * surface in an otherwise dark app -- and a card outside it inherits core's
 * dark tokens, which is how `.fb-menu` ended up carrying a hand-copied set of
 * its own further down the stylesheet. A dialog in the top layer is positioned
 * by the viewport regardless of where it sits in the tree, so there is nothing
 * to pay for putting it where the colours are.
 */
class FigureConfirm {

    /**
     * Two answers, one of them destructive. Resolves true or false, and false
     * for Escape and for the backdrop -- so a dismissed question is always the
     * answer that changes nothing.
     */
    static ask({ title, body, confirm, cancel, danger }) {
        return FigureConfirm.choose({
            title: title,
            body: body,
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
    static tell({ title, body }) {
        return FigureConfirm.choose({
            title: title, body: body,
            choices: [{ value: true, label: "OK", kind: "primary", focus: true }],
        });
    }

    /**
     * The general form: any number of answers, each with its own value.
     *
     * Resolves the chosen `value`, or **null** when the dialog is dismissed
     * without one -- Escape, or the backdrop. Null and not the first choice's
     * value, so that "the user did not answer" is a state a caller can test
     * for. Deleting a page is the case that needs it: neither of its two real
     * answers is what pressing Escape means.
     */
    static choose({ title, body, choices }) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        // A blank line is a paragraph break, which is how these messages were
        // already written for `window.confirm` -- it renders "\n\n" as one.
        const lines = Array.isArray(body) ? body : String(body || "").split("\n\n");
        const paragraphs = lines.filter((line) => String(line).trim())
            .map((line) => `<p class="fb-confirm-body">${escape(String(line))}</p>`)
            .join("");
        const buttons = choices.map((choice, index) =>
            `<button type="button" data-choice="${index}"
                     class="fb-button${FigureConfirm.KINDS[choice.kind] || ""}"
                     ${choice.focus ? "autofocus" : ""}
             >${escape(choice.label)}</button>`).join("");

        // One element per question, torn down when it closes, rather than one
        // kept and refilled. A kept dialog has to hold the pending resolver
        // between calls, and the moment there are two questions in flight --
        // which a modal is supposed to prevent and a bug will eventually
        // manage -- that single slot is either a promise resolved twice or a
        // promise resolved never. Per-call, the resolver is a closure and the
        // question cannot be confused with any other.
        const dialog = document.createElement("dialog");
        dialog.className = "fb-dialog fb-confirm";
        dialog.innerHTML = `<h2>${escape(title)}</h2>${paragraphs}
            <div class="fb-dialog-actions fb-confirm-actions">${buttons}</div>`;
        (document.getElementById("fb_workspace") || document.body).appendChild(dialog);

        return new Promise((resolve) => {
            let answer = null;
            dialog.addEventListener("click", (event) => {
                const button = event.target.closest?.("[data-choice]");
                if (!button) return;
                answer = choices[Number(button.dataset.choice)].value;
                dialog.close();
            });
            // `close` and not the click handler, because a button is not the
            // only way out: Escape fires `cancel` then `close`, and so does the
            // browser's own dismissal. One exit means one place the promise is
            // settled and no path that leaves it pending.
            dialog.addEventListener("close", () => {
                dialog.remove();
                resolve(answer);
            });
            if (typeof dialog.showModal !== "function") {
                // Nothing was asked, so nothing can be answered -- and a caller
                // left awaiting an answer that will never arrive is worse than
                // any dialog. A dismissal, which is always the reply that
                // changes nothing.
                dialog.remove();
                resolve(null);
                return;
            }
            dialog.showModal();
        });
    }

    static get KINDS() {
        return { danger: " fb-button-danger", primary: " fb-button-primary" };
    }

    /** Whether a modal dialog -- this one, the export sheet, the destination
     *  chooser -- currently owns the window. The canvas and the workspace both
     *  bind their shortcuts to `window`, and a <dialog> traps FOCUS but not
     *  keydown: with the delete confirmation up and its Cancel button focused,
     *  pressing Delete again reached `FigureCanvas.keyDown` and asked the same
     *  question a second time. The tag guard those handlers already have does
     *  not catch it, because the focused element is a BUTTON. */
    static get modalOpen() {
        return Boolean(document.querySelector("dialog[open]"));
    }
}
