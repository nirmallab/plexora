/**
 * jobWait.js -- a background job watched from inside the viewer: a modal on
 * arrival, a chip in the navbar once it is put away.
 *
 * Shared by every wait the viewer shows -- the mask pyramid being built
 * (views/segmentationWait.js) and a 3-D mask being turned into cell surfaces
 * (views/maskSurfaceWait.js). Each is one `PlexoraJobWait.create(...)` with
 * its own navbar mount and its own words; the behaviour is this file's alone,
 * so the two can never come to behave differently:
 *
 *   - a MODAL when the job starts, saying what is happening and that closing
 *     it is safe;
 *   - a CHIP in the navbar once it is closed -- which is where the modal went,
 *     and how to get it back;
 *   - good news reopens nothing: the result is already on screen, and a modal
 *     announcing it would cover it;
 *   - a failure reopens once, with the server's reason and, when the caller
 *     can retry, a Retry button.
 *
 * It asks the server nothing. The caller owns the one poll and hands this
 * every reading (`progress`, `ready`, `failed`).
 *
 * Classic script: `window.PlexoraJobWait`.
 */
window.PlexoraJobWait = (function () {
    "use strict";

    //: Long enough that a finished bar is actually seen. A job that happens to
    //: be nearly done when the modal opens would otherwise show a modal that
    //: flickers and vanishes.
    const READY_DWELL_MS = 900;

    //: Every open modal, so a page leaving the viewer can put all of them away
    //: and Escape closes only the one on top.
    const open_ = [];

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    /**
     * A percentage when the job reports one, a sliding bar when it does not.
     *
     * A job spends its first stretch opening a large file and reports nothing
     * at all while it does; a bar frozen at zero for that long reads as a job
     * that has died.
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

    function onKeydown(event) {
        if (event.key !== "Escape" || !open_.length) return;
        open_[open_.length - 1].dismiss();
    }

    /**
     * One wait.
     *
     * @param mount - the id of the navbar button the chip fills (base.html).
     * @param copy - the words: `chip`, `working`, `opening`, `note`, `button`,
     *   `readyTitle`, `readyDetail`, `failed`, `failedDetail`, `chipWorkingTip`,
     *   `chipFailedTip`.
     * @param notify - `{title, body}` for the desktop notification when the job
     *   lands while the window is away, or null.
     * @param retry - called by the Retry button of a failed wait; null for no
     *   button. It should call `start` again (or `failed`) itself.
     */
    function create({ mount, copy, notify = null, retry = null }) {
        //: idle -> working -> ready | failed. "idle" also covers "the wait is
        //: over and put away", which is why ready and failed both end there.
        let state = "idle";
        let reading = { progress: null, message: "", error: "", stage: "" };
        let chip = null;
        let chipLabel = null;
        let chipFill = null;
        //: The overlay while it is on screen, null while the chip stands in for it.
        let modal = null;
        let parts = null;

        /** Fill in the navbar's mount point, once. */
        function mountChip() {
            if (chip) return chip;
            const root = document.getElementById(mount);
            if (!root) return null;
            chipLabel = el("span", "segmentation-chip-label", copy.chip);
            chipFill = el("span", "segmentation-chip-fill is-indeterminate");
            const track = el("span", "segmentation-chip-track");
            track.appendChild(chipFill);
            const body = el("span", "segmentation-chip-body");
            body.appendChild(chipLabel);
            body.appendChild(track);
            root.appendChild(body);
            // The way back to the detail, and the reason the chip is a button.
            // A user who closed the modal and now wants the percentage has
            // nowhere else to ask.
            root.addEventListener("click", () => { open(); paint(); });
            chip = root;
            return chip;
        }

        function buildModal() {
            const overlay = el("div", "segmentation-progress-overlay");
            overlay.setAttribute("role", "dialog");
            overlay.setAttribute("aria-modal", "true");
            overlay.setAttribute("aria-label", copy.working);

            const card = el("div", "segmentation-progress-card");
            const title = el("div", "segmentation-progress-title", copy.working);
            const detail = el("div", "segmentation-progress-detail", copy.opening);
            const track = el("div", "segmentation-progress-track");
            const fill = el("div", "segmentation-progress-fill is-indeterminate");
            const note = el("div", "segmentation-progress-note", copy.note);
            const actions = el("div", "segmentation-progress-actions");
            const again = el("button", "btn btn-primary", "Retry");
            again.type = "button";
            again.hidden = true;
            const button = el("button", "btn btn-secondary", copy.button);
            button.type = "button";

            track.appendChild(fill);
            // Dismiss first, Retry -- the action, when there is one -- last.
            actions.appendChild(button);
            actions.appendChild(again);
            [title, detail, track, note, actions].forEach((node) => card.appendChild(node));
            overlay.appendChild(card);

            button.addEventListener("click", dismiss);
            again.addEventListener("click", () => {
                if (state !== "failed" || !retry) return;
                state = "idle";
                close();
                retry();
            });
            // Clicking beside the card, not on it -- the ordinary way out of a
            // dialog, and this one is safe to leave by any of them.
            overlay.addEventListener("click", (event) => {
                if (event.target === overlay) dismiss();
            });

            parts = { title, detail, fill, note, button, again };
            return overlay;
        }

        /** Put the overlay up. Paints nothing -- every caller follows with
         *  paint(), including the ones that reach an already-open modal.
         *
         *  Through PopoverPortal, not <body>, which is what decides where an
         *  overlay has to live for the Fullscreen API's opaque ::backdrop not
         *  to bury it: this runs over a viewer that can go fullscreen. */
        function open() {
            if (modal || state === "idle") return;
            modal = buildModal();
            PopoverPortal.attach(modal);
            if (!open_.length) document.addEventListener("keydown", onKeydown);
            open_.push(handle);
        }

        function close() {
            if (!modal) return;
            const at = open_.indexOf(handle);
            if (at >= 0) open_.splice(at, 1);
            if (!open_.length) document.removeEventListener("keydown", onKeydown);
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
         * A running job is only being put down -- the chip picks it up and the
         * poll never noticed. A failed one is over, so dismissing it ends it: a
         * red chip in the navbar afterwards would be a notification with
         * nothing left behind it.
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
            // Never both. The chip is where the modal went, so with the modal
            // up there is nothing for it to stand in for.
            const wanted = (state === "working" || state === "failed") && !modal;
            root.hidden = !wanted;
            if (!wanted) return;
            const failed = state === "failed";
            root.classList.toggle("has-error", failed);
            // The phase when there is one. The chip is the only surface visible
            // while the modal is closed, and one fixed label for minutes says
            // less than the phases do.
            chipLabel.textContent = failed ? copy.failed : (reading.stage || copy.chip);
            root.title = failed ? copy.chipFailedTip : copy.chipWorkingTip;
            setFill(chipFill, failed ? 100 : reading.progress);
        }

        function paintModal() {
            if (!modal) return;
            const failed = state === "failed";
            const done = state === "ready";
            parts.title.textContent = failed ? copy.failed : done ? copy.readyTitle : copy.working;
            parts.title.classList.toggle("has-error", failed);
            parts.detail.textContent = failed
                ? (reading.error || copy.failedDetail)
                : done ? copy.readyDetail
                : (reading.message || copy.opening);
            parts.fill.classList.toggle("has-error", failed);
            setFill(parts.fill, failed || done ? 100 : reading.progress);
            // Only while there is a wait to opt out of. On either ending it
            // would be offering to leave a modal that has nothing left to run.
            parts.note.hidden = failed || done;
            parts.button.hidden = done;
            parts.button.textContent = failed ? "Dismiss" : copy.button;
            parts.again.hidden = !(failed && retry);
        }

        /**
         * Start watching. `modal: false` announces the job in the chip only --
         * for a job that is already running when this page arrives, or one
         * whose result is already on screen in another form.
         */
        function start(options) {
            if (state !== "idle") return;
            const opts = options || {};
            state = "working";
            reading = { progress: null, message: opts.message || "", error: "",
                        stage: opts.label || "" };
            if (opts.modal !== false) open();
            paint();
        }

        function progress(detail) {
            if (state !== "working") return;
            detail = detail || {};
            reading = {
                progress: typeof detail.progress === "number" ? detail.progress : null,
                // The server's own line for the modal; the phase, in the two
                // words the chip has room for.
                message: detail.message || "",
                stage: detail.stageLabel || "",
                error: "",
            };
            paint();
        }

        function ready() {
            if (state === "idle") return;
            if (notify) window.PlexoraDesktop?.notifyIfAway(notify);
            state = "ready";
            reading = { progress: 100, message: "", error: "", stage: "" };
            // Good news reopens nothing: what it announces is already on
            // screen, and a modal over it would cover it.
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
        }

        function failed(error) {
            if (state === "idle") return;
            state = "failed";
            reading = { progress: null, message: "", stage: "", error: error || "" };
            // The one thing that does reopen. The job the user was promised
            // would finish by itself is not going to, and nothing else on the
            // page will ever mention it. Terminal, so this interrupts exactly
            // once and then stays shut.
            open();
            paint();
        }

        const handle = {
            start, progress, ready, failed, dismiss, close,
            get state() { return state; },
            get open() { return Boolean(modal); },
        };
        // Routed away from the viewer -- appRouter swaps pages inside this
        // same document, so without this the scrim would sit over whatever
        // page arrived. The navbar the chip lives in does not move.
        window.addEventListener("plexora:viewer-hidden", close);
        return handle;
    }

    return { create, READY_DWELL_MS };
}());
