/**
 * segmentationWait.js -- a mask pyramid being built, watched from inside the
 * viewer.
 *
 * A mask attached from the edit page is usually not pyramidized, and converting
 * a real slide's mask is minutes of work. That wait used to happen on the EDIT
 * page: the save handler dropped a blocking overlay over the form and reloaded
 * into the viewer only once the job finished. So the user sat on a form
 * watching a bar, with the image they had already imported one navigation away
 * and unreachable -- for a job that was running on the server either way and
 * needed nothing from the browser at all.
 *
 * Nothing has to be waited for, so the viewer opens straight away and this
 * shows the job in two places, one at a time:
 *
 *   - a MODAL on arrival, saying what is happening and that closing it is safe;
 *   - a CHIP in the navbar once it is closed -- which is where the modal went,
 *     and how to get it back.
 *
 * It asks the server nothing. main.js runs the one poll (`segmentation_status
 * === 'pending'` there) and announces every reading as
 * `plexora:segmentation-progress` / `-ready` / `-failed`; this is one listener
 * and Cell Explorer's mask wait is another. Two surfaces polling the same job
 * would be two answers free to disagree about one fact.
 *
 * Loaded by index.html, and only there: the pending state belongs to a viewer
 * looking at the project whose mask is converting. The chip's mount point is in
 * base.html because the navbar is, and stays empty everywhere else.
 */
(function () {
    "use strict";

    //: The navbar's wording, fixed. The modal's detail line is the server's and
    //: changes as the job proceeds; this one has to stay recognisable at a
    //: glance over the several minutes it is up.
    const CHIP_LABEL = "Pyramidizing segmentation mask…";
    const FAILED_TITLE = "Segmentation mask failed";
    const WORKING_TITLE = "Pyramidizing segmentation mask";
    //: Shown until the first poll answers. The job was started by the save that
    //: attached the mask, so this reports a fact rather than promising one.
    const OPENING_DETAIL =
        "This mask is not pyramidized. Processing has started in the background.";
    //: Present tense, and deliberately. main.js starts the switch to the mask
    //: as it announces this and does not wait for it, so on a large pyramid the
    //: first tiles are still arriving while this is on screen.
    const READY_DETAIL = "The mask is ready. Cells are being drawn on the image.";
    //: The whole point of the modal being dismissible, said plainly. A progress
    //: bar with no way out reads as something the user has to sit through.
    const NOTE = "You can close this at any time and keep viewing the image. "
        + "Processing continues in the background, and the mask is drawn as "
        + "outlines by itself when it is ready.";
    //: Long enough that a finished bar is actually seen. A conversion that
    //: happens to be nearly done when the viewer opens would otherwise show a
    //: modal that flickers and vanishes.
    const READY_DWELL_MS = 900;

    //: idle -> working -> ready | failed. "idle" also covers "the wait is over
    //: and put away", which is why ready and failed both end there.
    let state = "idle";
    let reading = { progress: null, message: "", error: "" };
    let chip = null;
    let chipLabel = null;
    let chipFill = null;
    //: The overlay while it is on screen, null while the chip stands in for it.
    let modal = null;
    let parts = null;

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    /**
     * A percentage when the job reports one, a sliding bar when it does not.
     *
     * A conversion spends its first stretch opening a large file and reports
     * nothing at all while it does; a bar frozen at zero for that long reads as
     * a job that has died.
     */
    function setFill(fill, percent) {
        if (typeof percent !== "number") {
            fill.classList.add("is-indeterminate");
            fill.style.width = "";
            return;
        }
        fill.classList.remove("is-indeterminate");
        fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    }

    /** Fill in the navbar's mount point, once. */
    function mountChip() {
        if (chip) return chip;
        const root = document.getElementById("segmentation_chip");
        if (!root) return null;
        chipLabel = el("span", "segmentation-chip-label", CHIP_LABEL);
        chipFill = el("span", "segmentation-chip-fill is-indeterminate");
        const track = el("span", "segmentation-chip-track");
        track.appendChild(chipFill);
        const body = el("span", "segmentation-chip-body");
        body.appendChild(chipLabel);
        body.appendChild(track);
        root.appendChild(body);
        // The way back to the detail, and the reason the chip is a button. A
        // user who closed the modal and now wants the percentage has nowhere
        // else to ask.
        root.addEventListener("click", () => { open(); paint(); });
        chip = root;
        return chip;
    }

    function buildModal() {
        const overlay = el("div", "segmentation-progress-overlay");
        overlay.setAttribute("role", "dialog");
        overlay.setAttribute("aria-modal", "true");
        overlay.setAttribute("aria-label", WORKING_TITLE);

        const card = el("div", "segmentation-progress-card");
        const title = el("div", "segmentation-progress-title", WORKING_TITLE);
        const detail = el("div", "segmentation-progress-detail", OPENING_DETAIL);
        const track = el("div", "segmentation-progress-track");
        const fill = el("div", "segmentation-progress-fill is-indeterminate");
        const note = el("div", "segmentation-progress-note", NOTE);
        const actions = el("div", "segmentation-progress-actions");
        const button = el("button", "btn btn-secondary", "Continue viewing");
        button.type = "button";

        track.appendChild(fill);
        actions.appendChild(button);
        [title, detail, track, note, actions].forEach((node) => card.appendChild(node));
        overlay.appendChild(card);

        button.addEventListener("click", dismiss);
        // Clicking beside the card, not on it -- the ordinary way out of a
        // dialog, and this one is safe to leave by any of them.
        overlay.addEventListener("click", (event) => {
            if (event.target === overlay) dismiss();
        });

        parts = { title, detail, fill, note, button };
        return overlay;
    }

    function onKeydown(event) {
        if (event.key === "Escape") dismiss();
    }

    /** Put the overlay up. Paints nothing -- every caller follows with paint(),
     *  including the ones that reach an already-open modal and get no-opped.
     *
     *  Through PopoverPortal, not <body>, which is what decides where an
     *  overlay has to live for the Fullscreen API's opaque ::backdrop not to
     *  bury it. This is the one difference from segmentationProgress.js's
     *  otherwise identical panel: the import pages have no way to go
     *  fullscreen and this one runs over a viewer that does. */
    function open() {
        if (modal || state === "idle") return;
        modal = buildModal();
        PopoverPortal.attach(modal);
        document.addEventListener("keydown", onKeydown);
    }

    function close() {
        if (!modal) return;
        document.removeEventListener("keydown", onKeydown);
        // detach, not remove: a portal still holding a destroyed element
        // re-attaches the orphan on the next fullscreen toggle.
        PopoverPortal.detach(modal);
        modal = null;
        parts = null;
        paintChip();
    }

    /**
     * The user putting the wait away.
     *
     * A running job is only being put down -- the chip picks it up and the poll
     * never noticed. A failed one is over, so dismissing it ends it: leaving a
     * red chip in the navbar afterwards would be a notification with nothing
     * left behind it.
     */
    function dismiss() {
        if (state === "failed") state = "idle";
        close();
    }

    function paint() {
        paintChip();
        paintModal();
    }

    function paintChip() {
        const root = mountChip();
        if (!root) return;
        // Never both. The chip is where the modal went, so with the modal up
        // there is nothing for it to stand in for.
        const wanted = (state === "working" || state === "failed") && !modal;
        root.hidden = !wanted;
        if (!wanted) return;
        const failed = state === "failed";
        root.classList.toggle("has-error", failed);
        chipLabel.textContent = failed ? FAILED_TITLE : CHIP_LABEL;
        root.title = failed
            ? "The mask could not be converted — click for the reason"
            : "Converting in the background — click to see progress";
        setFill(chipFill, failed ? 100 : reading.progress);
    }

    function paintModal() {
        if (!modal) return;
        const failed = state === "failed";
        const done = state === "ready";
        parts.title.textContent = failed ? FAILED_TITLE
            : done ? "Segmentation mask ready" : WORKING_TITLE;
        parts.title.classList.toggle("has-error", failed);
        parts.detail.textContent = failed
            ? (reading.error || "The mask could not be converted.")
            : done ? READY_DETAIL
            : (reading.message || OPENING_DETAIL);
        parts.fill.classList.toggle("has-error", failed);
        setFill(parts.fill, failed || done ? 100 : reading.progress);
        // Only while there is a wait to opt out of. On either ending it would
        // be offering to leave a modal that has nothing left to run.
        parts.note.hidden = failed || done;
        parts.button.hidden = done;
        parts.button.textContent = failed ? "Dismiss" : "Continue viewing";
    }

    /**
     * Start watching. Called by main.js the moment it sees a pending job --
     * deliberately before its own poll, which runs at the end of viewer boot
     * behind the sidebar and every plugin's init. A mask the user attached
     * seconds ago should not be an unexplained wait until then.
     */
    function start() {
        if (state !== "idle") return;
        state = "working";
        reading = { progress: null, message: "", error: "" };
        open();
        paint();
    }

    window.addEventListener("plexora:segmentation-progress", (event) => {
        if (state !== "working") return;
        const detail = event.detail || {};
        reading = {
            progress: typeof detail.progress === "number" ? detail.progress : null,
            // The server's own line, which says which kind of mask is being
            // built and -- when the supplied one could not be used as it stood
            // -- which requirement it missed.
            message: detail.message || "",
            error: "",
        };
        paint();
    });

    window.addEventListener("plexora:segmentation-ready", () => {
        if (state === "idle") return;
        state = "ready";
        reading = { progress: 100, message: "", error: "" };
        // Good news reopens nothing. main.js switches the viewer to the mask as
        // this fires, and a modal appearing over it to announce that would
        // cover the very thing it is announcing.
        if (!modal) {
            state = "idle";
            paintChip();
            return;
        }
        paintModal();
        window.setTimeout(() => {
            state = "idle";
            close();
        }, READY_DWELL_MS);
    });

    window.addEventListener("plexora:segmentation-failed", (event) => {
        if (state === "idle") return;
        state = "failed";
        reading = { progress: null, message: "", error: (event.detail || {}).error || "" };
        // The one thing that does reopen. The job the user was promised would
        // finish by itself is not going to; they attached the mask minutes ago
        // and nothing else on the page will ever mention it. Terminal, so this
        // interrupts exactly once and then stays shut.
        open();
        paint();
    });

    // Routed away from the viewer -- appRouter swaps pages inside this same
    // document, so without this the scrim would sit over whatever page arrived.
    // A modal about a background job has no business covering a page that has
    // nothing to do with it, and the navbar the chip lives in does not move.
    window.addEventListener("plexora:viewer-hidden", close);

    window.PlexoraSegmentationWait = { start };
})();
