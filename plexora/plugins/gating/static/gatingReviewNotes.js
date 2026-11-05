/**
 * @class GateReviewNotes - the agent's notes on the gate being reviewed, on a
 * pane of glass at the top centre of the viewer.
 *
 * Everything it says is the gate's own `review` record (server
 * autogate/explain.py), written from what the run recorded: how the gate
 * was reached, which partners it drew on and in what role, and what stays
 * uncertain. Nothing is composed here. A chip per partner wears the colour
 * of the slot showing it (gatingReviewChannels.js), so the chip and the
 * channel on the image are read together; a partner that could not be put on
 * screen says so.
 *
 * Dismissed (the x, or Escape), it stays away for that gate -- the marker
 * and the session that wrote it -- until the "AI Notes" link beside the
 * gate's provenance line brings it back (gatingSidebarController.js
 * paintProvenance). Moving to another marker shows that marker's notes in
 * place, without the entrance again.
 *
 * Docked by the canvas layout (`ctx.view.dock`, top centre under core's back
 * button), which takes it away with the plugin. Without a dock it is never
 * shown.
 */

const REVIEW_NOTES_LEAVE_MS = 200;
// How a chip names the partner's relation to the marker under review.
const REVIEW_CHIP_WORDS = { parent_child: "within", coexpression: "with",
                            mutual_exclusion: "excludes" };

class GateReviewNotes {
    constructor(controller) {
        this.controller = controller;
        this.el = null;
        //: The review on show (`key`: marker @ the session that wrote it).
        this.key = null;
        this._leaveTimer = 0;
        this._undock = null;
    }

    /** Built once, on the first show. Null where the viewer has no dock. */
    mount() {
        if (this.el) return this.el;
        const view = this.controller.ctx?.view;
        if (typeof view?.dock !== "function") return null;
        const el = document.createElement("div");
        el.className = "gate-ai-notes";
        el.setAttribute("role", "status");
        el.setAttribute("aria-live", "polite");
        el.hidden = true;

        const head = document.createElement("div");
        head.className = "gate-ai-notes-head";
        const label = document.createElement("span");
        label.className = "gate-ai-notes-label";
        label.textContent = "AI notes";
        const marker = document.createElement("span");
        marker.className = "gate-ai-notes-marker";
        // Muted beside the name: the provenance line under the gate says it too.
        const flag = document.createElement("span");
        flag.className = "gate-ai-notes-flag";
        flag.textContent = "Needs review";
        flag.hidden = true;
        const close = document.createElement("button");
        close.type = "button";
        close.className = "gate-ai-notes-dismiss";
        close.title = "Dismiss";
        close.setAttribute("aria-label", "Dismiss");
        close.textContent = "×";
        close.addEventListener("click", () => this.dismiss());
        head.append(label, marker, flag, close);

        const text = document.createElement("p");
        text.className = "gate-ai-notes-text";
        const chips = document.createElement("div");
        chips.className = "gate-ai-notes-chips";
        const doubt = document.createElement("p");
        doubt.className = "gate-ai-notes-uncertainty";
        el.append(head, text, chips, doubt);
        el.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.stopPropagation();
                this.dismiss();
            }
        });
        this._undock = view.dock(el, { corner: "top-center", order: 30 });
        this.el = el;
        return el;
    }

    /**
     * Show `review` for `marker`, each partner's chip in the colour of the
     * slot drawing it (gatingReviewChannels.js `shownAs`). With `channelsOff`
     * (the AI evidence row's eye shut) no channel was meant to be on screen.
     */
    show(marker, review, { channelsOff = false } = {}) {
        if (!review || !this.controller._shown || this.controller.isReviewDismissed(review.key)) {
            this.hide();
            return;
        }
        const el = this.mount();
        if (!el) return;
        const fresh = el.hidden || el.classList.contains("is-leaving");
        window.clearTimeout(this._leaveTimer);
        el.classList.remove("is-leaving");
        this.key = review.key;
        this.render(marker, review, channelsOff);
        el.hidden = false;
        if (fresh) {
            el.classList.remove("is-entering");
            void el.offsetWidth;  // restart the entrance
            el.classList.add("is-entering");
        }
    }

    render(marker, review, channelsOff) {
        const el = this.el;
        el.dataset.needsReview = review.needs_review ? "true" : "false";
        el.querySelector(".gate-ai-notes-marker").textContent = marker;
        el.querySelector(".gate-ai-notes-flag").hidden = !review.needs_review;
        el.querySelector(".gate-ai-notes-text").textContent = review.notes || "";
        const chips = el.querySelector(".gate-ai-notes-chips");
        chips.replaceChildren(...(review.supporting || []).map(
            (entry) => this.chip(entry, channelsOff)));
        chips.hidden = !chips.childElementCount;
        // What stays uncertain that the notes do not already say: they often
        // end on the first of it.
        const doubt = el.querySelector(".gate-ai-notes-uncertainty");
        const said = review.notes || "";
        const lines = (review.uncertainty || []).filter((line) => line && !said.includes(line));
        doubt.textContent = lines.join(" · ");
        doubt.hidden = !lines.length;
    }

    /** One partner: its relation and name, in the colour of the slot showing it. */
    chip(entry, channelsOff) {
        const chip = document.createElement("span");
        chip.className = "gate-ai-chip";
        const words = REVIEW_CHIP_WORDS[entry.kind] || "with";
        const shown = this.controller.reviewChannels?.shownAs?.(entry) || { color: null, why: null };
        let label = `${words} ${entry.marker}`;
        const notes = [entry.influence].filter(Boolean);
        if (shown.color) {
            chip.style.setProperty("--chip-color", shown.color);
        } else if (channelsOff || entry.display === false) {
            chip.classList.add("is-off");
        } else {
            chip.classList.add("is-missing");
            label += " · not shown";
            notes.push(shown.why ? `Not on screen: ${shown.why}` : "Not on screen");
        }
        chip.textContent = label;
        chip.title = notes.join("\n");
        return chip;
    }

    /** The x or Escape: away until the "AI Notes" link brings it back. */
    dismiss() {
        const el = this.el;
        if (!el || el.hidden || !this.key) return;
        const hadFocus = el.contains(document.activeElement);
        this.controller.setReviewDismissed(this.key, true);
        this.leave();
        if (hadFocus) document.querySelector?.("[data-ai-notes]")?.focus?.();
    }

    /** The link beside the provenance line: this gate's notes again. */
    reopen() {
        const review = this.controller.reviewFor(this.controller.gateMarker);
        if (!review) return;
        this.controller.setReviewDismissed(review.key, false);
        this.controller.syncReview();
    }

    leave() {
        const el = this.el;
        el.classList.remove("is-entering");
        el.classList.add("is-leaving");
        window.clearTimeout(this._leaveTimer);
        this._leaveTimer = window.setTimeout(() => {
            el.hidden = true;
            el.classList.remove("is-leaving");
        }, REVIEW_NOTES_LEAVE_MS);
    }

    /** Off screen at once; what was dismissed stays dismissed. */
    hide() {
        window.clearTimeout(this._leaveTimer);
        this.key = null;
        if (!this.el) return;
        this.el.hidden = true;
        this.el.classList.remove("is-entering", "is-leaving");
    }

    dispose() {
        window.clearTimeout(this._leaveTimer);
        this._undock?.();
        this._undock = null;
        this.el = null;
        this.key = null;
    }
}
