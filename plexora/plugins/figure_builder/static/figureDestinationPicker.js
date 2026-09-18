/**
 * FigureDestinationPicker - "Add panels to…", asked once and answered in one click.
 *
 * What this replaces was a form. It had a heading that was a question, a line
 * of prose under it, two competing call-to-action buttons, a <select> of every
 * figure by name, an Open button that only mattered after the select had been
 * revealed, and a Cancel button as wide as the primary one -- five controls and
 * two steps to say "that one". None of it showed the user a single figure.
 *
 * A figure is a picture. The only thing that identifies one at a glance is what
 * it looks like, so this is a thumbnail grid: the recent few in a row, and the
 * whole library one click away behind "Open existing figure →", searchable.
 * Picking is a single click on the thing you recognise -- no second Open press,
 * no confirmation, because the action is not destructive and getting it wrong
 * costs one press of Back.
 *
 * Three things are deliberately absent:
 *
 *   - **no Cancel button.** The × closes it and so does Escape, and a modal
 *     with a large "do nothing" button ranged beside the real answers makes
 *     "nothing" look like a choice on the same footing as the others.
 *   - **no dropdown.** A list of names in a 260px control is the one shape that
 *     cannot show what a figure is.
 *   - **no dashed drop-zones, no rules between sections.** Whitespace and one
 *     small caption carry the hierarchy; a border for every grouping is what
 *     made the old dialog read as a settings screen.
 *
 * The box is ONE size. The shortlist always has four places in it, and the
 * ones with no figure in them yet are drawn as outlines: a dialog that is
 * 280px wide on Monday and 560 on Friday puts its close button somewhere new
 * every time it opens, and a single card in a four-card grid looked like a
 * rendering fault. Every preview is square for the same reason -- a figure's
 * page can be portrait or landscape, and a row of tiles in two shapes is not
 * a row.
 *
 * A native <dialog>, like FigureConfirm and the export sheet: modal,
 * focus-trapped and Esc-dismissible without a line of script, and it cannot end
 * up behind the canvas the way a positioned div can. One element per question,
 * torn down when it closes -- the reasoning is in FigureConfirm's docstring and
 * it is the same here.
 */
class FigureDestinationPicker {

    /** How many figures the front page offers. Four fits one row at this width
     *  and is about as many as anybody has in mind at once; the rest are behind
     *  the browser, which is searchable. */
    static get RECENT() { return 4; }

    /**
     * Ask, and resolve what the user chose.
     *
     * @param {object} options
     * @param {FigureBuilderApi} options.api for thumbnail URLs.
     * @param {Array} options.figures readable figures, newest first.
     * @param {number} options.count how many panels are waiting, for the title.
     * @param {?string} options.preferred a figure to put first and focus --
     *        the one this browser was last working on. It is a hint, not a
     *        default: nothing is chosen without a click.
     * @returns {Promise<{kind: "new"}|{kind: "figure", figureId: string}|null>}
     *          null when the question was dismissed.
     */
    static choose({ api, figures, count, preferred }) {
        const client = api || new FigureBuilderApi();
        const all = (figures || []).filter((figure) => figure && figure.readable !== false);
        const ordered = FigureDestinationPicker.order(all, preferred);
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);

        const dialog = document.createElement("dialog");
        dialog.className = "fb-dialog fb-picker";
        dialog.innerHTML = `
            <div class="fb-picker-head">
                <h2 class="fb-picker-title">${escape(FigureDestinationPicker.title(count))}</h2>
                <button class="fb-picker-close" type="button" data-role="close"
                        title="Close" aria-label="Close">
                    <span class="fas fa-xmark" aria-hidden="true"></span>
                </button>
            </div>

            <div class="fb-picker-pick" data-role="pick">
                <button class="fb-picker-create" type="button" data-role="create">
                    <span class="fas fa-plus" aria-hidden="true"></span>
                    <span>Create new figure</span>
                </button>
                <p class="fb-picker-caption">Recent</p>
                <div class="fb-picker-row" data-role="recent"></div>
                ${ordered.length ? `
                <button class="fb-picker-more" type="button" data-role="more">
                    Open existing figure
                    <span class="fas fa-arrow-right" aria-hidden="true"></span>
                </button>` : `
                <p class="fb-picker-none">No figures yet — the new one opens straight away.</p>`}
            </div>

            <div class="fb-picker-browse" data-role="browse" hidden>
                <div class="fb-picker-search">
                    <span class="fas fa-magnifying-glass" aria-hidden="true"></span>
                    <input type="search" data-role="search" autocomplete="off"
                           spellcheck="false" placeholder="Search figures"
                           aria-label="Search figures">
                </div>
                <div class="fb-picker-grid" data-role="grid"></div>
                <p class="fb-picker-none" data-role="noresults" hidden>No figures match.</p>
                <button class="fb-picker-back" type="button" data-role="back">
                    <span class="fas fa-arrow-left" aria-hidden="true"></span> Back
                </button>
            </div>`;

        // Beside the colours it should inherit. The workspace is the one light
        // surface in an otherwise dark app and declares its palette on
        // `.fb-workspace`; a dialog in the top layer is positioned by the
        // viewport wherever it sits in the tree, so there is nothing to pay for
        // putting it where the tokens are.
        (document.getElementById("fb_workspace") || document.body).appendChild(dialog);

        const el = (role) => dialog.querySelector(`[data-role="${role}"]`);
        const cards = (list) => list.map((figure) =>
            FigureDestinationPicker.card(client, figure, figure.figure_id === preferred)).join("");

        const shown = ordered.slice(0, FigureDestinationPicker.RECENT);
        const recent = el("recent");
        if (recent) {
            recent.innerHTML = cards(shown)
                + FigureDestinationPicker.slots(
                    FigureDestinationPicker.RECENT - shown.length);
        }
        const grid = el("grid");
        if (grid) grid.innerHTML = cards(ordered);

        return new Promise((resolve) => {
            let answer = null;
            const settle = (value) => {
                answer = value;
                dialog.close();
            };

            dialog.addEventListener("click", (event) => {
                const target = event.target.closest?.("[data-role], [data-figure-id]");
                if (!target) return;
                const figureId = target.dataset.figureId;
                if (figureId) return settle({ kind: "figure", figureId: figureId });
                const role = target.dataset.role;
                // "New" resolves at once and creates nothing here: the caller
                // owns the figure it makes, and a confirmation step on an
                // action this cheap is a step that only ever gets pressed
                // through.
                if (role === "create") return settle({ kind: "new" });
                if (role === "close") return settle(null);
                if (role === "more") return FigureDestinationPicker.browse(dialog, true);
                if (role === "back") return FigureDestinationPicker.browse(dialog, false);
            });

            const search = el("search");
            const filter = () => {
                const query = ((search && search.value) || "").trim().toLowerCase();
                const matches = query
                    ? ordered.filter((figure) => FigureDestinationPicker.matches(figure, query))
                    : ordered;
                if (grid) grid.innerHTML = cards(matches);
                const none = el("noresults");
                if (none) none.hidden = matches.length > 0;
            };
            search?.addEventListener("input", filter);
            // Escape inside a search field clears it in some browsers and
            // closes the dialog in others. Neither is wrong, but "clear the box
            // I am typing in" is what the key means while the box has focus, so
            // it is stopped from also dismissing the question.
            search?.addEventListener("keydown", (event) => {
                if (event.key !== "Escape" || !search.value) return;
                event.stopPropagation();
                event.preventDefault();
                search.value = "";
                filter();
            });

            // `close` and not the click handler: Escape and the browser's own
            // dismissal both arrive here and nowhere else, so there is one place
            // the promise is settled and no path that leaves it pending.
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
     * The empty places in the shortlist, drawn rather than left out.
     *
     * The row is four wide whether or not there are four figures. With one
     * card and no slots, the grid either stretched that card across the dialog
     * or left three columns of nothing beside it; with the slots, a new
     * installation and a full one are the same shape, the buttons are in the
     * same place both times, and the row says without a word that the
     * shortlist holds four.
     *
     * `aria-hidden` and no tabindex: there is nothing here to choose, and a
     * screen reader reading out four empty boxes would be worse than silence.
     */
    static slots(count) {
        return Array.from({ length: Math.max(0, Number(count) || 0) },
            () => `<span class="fb-picker-slot" aria-hidden="true"></span>`).join("");
    }

    /** "Add 3 panels to…", or just "Open figure" when nothing is waiting --
     *  pressing the button with an empty bin is a request to go to the canvas,
     *  and a title about panels would be describing panels that do not exist. */
    static title(count) {
        const waiting = Number(count) || 0;
        return waiting
            ? `Add ${FigureSchema.countPhrase(waiting, "panel")} to…`
            : "Open figure";
    }

    /** The remembered figure first, everything else in the order it arrived
     *  (which is newest first). A hint about where the user was, not a default:
     *  it is still a click. */
    static order(figures, preferred) {
        const first = figures.filter((figure) => figure.figure_id === preferred);
        return first.concat(figures.filter((figure) => figure.figure_id !== preferred));
    }

    static matches(figure, query) {
        return String(figure.title || "").toLowerCase().includes(query)
            || (figure.sources || []).some((name) => String(name).toLowerCase().includes(query));
    }

    /**
     * One figure, as the thing it actually is: a picture with a name under it.
     *
     * The whole card is the button -- not a card with an Open button in it --
     * because there is one thing to do with a figure here and a control inside
     * a clickable card is two targets for one intention.
     */
    static card(api, figure, focus) {
        const escape = FigureSchema.escapeHtml.bind(FigureSchema);
        const title = escape(figure.title || "Untitled figure");
        const when = FigureSchema.timeAgo(figure.updated_at);
        const panels = FigureSchema.countPhrase(figure.panel_count || 0, "panel");
        // Two facts, and only one of them fits. "9 panels · 14 minutes ago" is
        // 21 characters in a 120px card, so it was rendering as "9 panels · 14
        // minutes a..." -- an at-a-glance line cut off mid-word. The count is
        // what distinguishes two figures at this size; the rest is on the
        // tooltip, which has room for it and is where a card's detail belongs.
        const summary = [panels, when].filter(Boolean).join(" · ");
        const thumbnail = figure.has_thumbnail
            ? `<img src="${escape(api.thumbnailUrl(figure.figure_id, figure.revision))}"
                    alt="" loading="lazy" draggable="false">`
            : `<span class="fb-picker-sheet" aria-hidden="true">
                   <span class="fas fa-image fb-picker-thumb-icon"></span>
               </span>`;
        return `<button class="fb-picker-card" type="button" ${focus ? "autofocus" : ""}
                        data-figure-id="${escape(figure.figure_id)}"
                        title="${title} — ${escape(summary)}">
            <span class="fb-picker-thumb">${thumbnail}</span>
            <span class="fb-picker-name">${title}</span>
            <span class="fb-picker-meta">${escape(panels)}</span>
        </button>`;
    }

    /**
     * Swap between the two faces of the same dialog.
     *
     * One dialog and not two, so the × stays where it was and the box does not
     * jump to a different place on screen when the user asks for the full list.
     */
    static browse(dialog, on) {
        dialog.classList.toggle("is-browsing", Boolean(on));
        const pick = dialog.querySelector('[data-role="pick"]');
        const browse = dialog.querySelector('[data-role="browse"]');
        if (pick) pick.hidden = Boolean(on);
        if (browse) browse.hidden = !on;
        if (on) dialog.querySelector('[data-role="search"]')?.focus();
    }
}
