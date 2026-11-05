/**
 * @class GateReviewChannels - the channels an automatic gate was decided with,
 * switched on beside the marker while it is under review.
 *
 * The gate's provenance carries a `review` record (server
 * autogate/explain.py): the partners its decision actually drew on -- the
 * lineage it was fitted within, the carriers its start came from, the
 * partner tiles and plots its looks showed, the lineages it was checked on
 * -- each with the image channel it is drawn from. Selecting the marker puts
 * those channels on screen in slots 3, 4, ... (indices 2 and up; slot 1 is
 * the user's and slot 2 the marker's mirror, setGateMarker), and moving to
 * the next marker puts back what the last one took. Both the channels and
 * the notes over the viewer (gatingReviewNotes.js) read the one record.
 *
 * The user's set-up is never written over:
 *  - a channel already on screen is left where and as it is;
 *  - only a hidden row is taken, or a new one added past the end, never a
 *    row the user can see; a hidden row is put back exactly as it was;
 *  - a taken row is `transient`: no save of the project's channel list
 *    includes it, whoever triggers the save (the marker's own mirror into
 *    slot 2 saves, as does its auto-levelling), and the next sample of a
 *    dataset is not handed it (viewerSidebar.persistChannelList,
 *    carryOver.js). Saving itself is never held: holding it would drop a
 *    save the user's own change had just asked for;
 *  - a taken row the user then changes (another channel, another colour,
 *    switched off, removed) is theirs from then on -- core's setters clear
 *    `transient` -- forgotten here, never put back, and its channel is not
 *    taken again for that marker.
 *
 * Switching a channel on fetches that channel's tiles alone, and switching
 * it off keeps them, so following the review never reloads the image: on a
 * remote (`node://`) project the cost is the new channels' tiles.
 */

// Matching the agent's own evidence pictures: the partner a marker is
// expected in drawn cool (cyan, violet, yellow), the one that should exclude
// it warm (magenta, orange). Every one a swatch preset.
const REVIEW_PALETTE = {
    overlap: ["#22e6e6", "#a78bfa", "#ffd60a"],
    exclusion: ["#ec4899", "#f97316"],
};

class GateReviewChannels {
    constructor(controller) {
        this.controller = controller;
        //: [{index, channel, marker, kind, relation, prev, applied}]: the slots
        //: this review holds, with what each held before and what it was given.
        this.claims = [];
        //: The marker and channels last followed, so a repeat is a no-op.
        this.signature = null;
        //: marker -> Set of channels whose claimed slot the user took over.
        this.declined = new Map();
        //: [{marker, channel, why}]: the last follow's channels it could not show.
        this.skipped = [];
        this._onAgentRestore = (event) => this.onAgentRestore(event);
        window.addEventListener("plexora:agent-restore", this._onAgentRestore);
    }

    get sidebar() {
        return this.controller.sidebar;
    }

    /** The slot name a channel is drawn under (short), and whether the image has it. */
    slotName(channel) {
        const data = this.controller.dataLayer;
        const short = data?.getShortChannelName ? data.getShortChannelName(channel) : channel;
        const full = data?.getFullChannelName ? data.getFullChannelName(short) : short;
        const image = this.controller.ctx?.dataset?.image;
        return { name: short, present: Boolean(image?.has?.(full)) };
    }

    /**
     * How a supporting entry's channel is on screen: {color} of the slot
     * drawing it (one of this review's, or the user's own), or {why} not.
     */
    shownAs(entry) {
        const { name, present } = entry?.channel ? this.slotName(entry.channel)
            : { name: null, present: false };
        if (!present) return { color: null, why: "not an image channel" };
        const claim = this.claims.find((c) => c.channel === name);
        const slot = claim ? this.sidebar?.channelSlots?.[claim.index]
            : this.sidebar?.slotShowing?.(name);
        if (slot?.colorHex) return { color: slot.colorHex, why: null };
        const skip = this.skipped.find((s) => s.channel === name || s.marker === entry.marker);
        return { color: null, why: skip ? skip.why : null };
    }

    /**
     * The channels `review` asks for, in its order, and those it cannot show:
     * not the marker's own channel, nor one slots 1 and 2 show.
     */
    wantedFor(marker, review) {
        const slots = this.sidebar?.channelSlots || [];
        const reserved = new Set([0, 1].map((i) => slots[i])
            .filter((slot) => slot && slot.enabled && slot.name).map((slot) => slot.name));
        const wanted = [];
        const skipped = [];
        const seen = new Set();
        (review?.supporting || []).forEach((entry) => {
            if (!entry || entry.display === false) return;
            const { name, present } = entry.channel ? this.slotName(entry.channel)
                : { name: null, present: false };
            if (!present) {
                skipped.push({ marker: entry.marker, channel: entry.channel || null,
                               why: "not an image channel" });
                return;
            }
            if (name === marker || seen.has(name) || reserved.has(name)) return;
            seen.add(name);
            wanted.push({ channel: name, marker: entry.marker, kind: entry.kind,
                          relation: entry.relation });
        });
        return { wanted, skipped };
    }

    /**
     * Put `review`'s channels on screen for `marker`, putting back whatever
     * an earlier marker took that this one does not need. Synchronous;
     * returns {claims, skipped}. The same marker and channels a second time
     * changes nothing.
     */
    follow(marker, review) {
        const sidebar = this.sidebar;
        if (!sidebar || !Array.isArray(sidebar.channelSlots)) return { claims: [], skipped: [] };
        const { wanted, skipped } = this.wantedFor(marker, review);
        const signature = `${marker}|${wanted.map((w) => w.channel).join(",")}`;
        this.forgetTouched();
        if (signature === this.signature) {
            return { claims: [...this.claims], skipped: [...this.skipped] };
        }
        const names = new Set(wanted.map((w) => w.channel));
        const declined = this.declined.get(marker) || new Set();
        this.claims.filter((claim) => !names.has(claim.channel))
            .forEach((claim) => this.putBack(claim));
        this.claims = this.claims.filter((claim) => names.has(claim.channel));
        const mine = new Map(this.claims.map((claim) => [claim.channel, claim]));
        wanted.forEach((want) => {
            const held = mine.get(want.channel);
            if (held) {
                Object.assign(held, { marker: want.marker, kind: want.kind,
                                      relation: want.relation, forMarker: marker });
                // Still ours: a slot core rebuilt (an agent's lease ending)
                // comes back without the mark.
                sidebar.channelSlots[held.index].transient = true;
                return;
            }
            if (declined.has(want.channel)) {
                skipped.push({ ...want, why: "you changed or switched off its slot" });
                return;
            }
            // On screen already: the user's, and the chip shows its colour.
            if (sidebar.slotShowing?.(want.channel)) return;
            const index = this.claimSlot();
            if (index === null) {
                skipped.push({ ...want, why: "no free channel slot" });
                return;
            }
            const claim = this.apply(index, want);
            claim.forMarker = marker;
            this.claims.push(claim);
        });
        this.signature = signature;
        this.skipped = skipped;
        return { claims: [...this.claims], skipped: [...skipped] };
    }

    /**
     * The slot a channel is put in: a hidden row past slot 2, else a new one.
     * Never a row the user can see, on or off: an off row of theirs is their
     * set-up too. Null when the sidebar has no room.
     */
    claimSlot() {
        const sidebar = this.sidebar;
        const slots = sidebar.channelSlots;
        const taken = new Set(this.claims.map((claim) => claim.index));
        const hidden = slots.find((slot) => slot && slot.index >= 2 && slot.visible === false
                                  && !slot.enabled && !taken.has(slot.index));
        if (hidden) return hidden.index;
        const used = slots.filter((slot) => slot.name).map((slot) => slot.name);
        const created = sidebar.createAdditionalSlot?.(used);
        if (!created) return null;
        created._reviewCreated = true;
        return created.index;
    }

    /** Switch `want.channel` on in slot `index`, in its relation's colour. */
    apply(index, want) {
        const sidebar = this.sidebar;
        const slot = sidebar.channelSlots[index];
        const created = Boolean(slot._reviewCreated);
        delete slot._reviewCreated;
        // A hidden row as it was, to put back field for field.
        const prev = { ...slot, range: Array.isArray(slot.range) ? [...slot.range] : slot.range,
                       created };
        sidebar.setSlotMarker(index, want.channel, { keepColor: true, enable: true, reveal: true });
        const hex = this.colorFor(want.kind, index);
        if ((slot.colorHex || "").toLowerCase() !== hex.toLowerCase()) {
            sidebar.setSlotColor(index, hex, false);
        }
        // Set last: core's setters clear it. Before the 400 ms the save these
        // setters asked for waits, so that save leaves the row out.
        slot.transient = true;
        return { index, channel: want.channel, marker: want.marker, kind: want.kind,
                 relation: want.relation, prev,
                 applied: { name: want.channel, colorHex: slot.colorHex } };
    }

    /** The first colour of the relation's family no slot 1/2 or claim wears. */
    colorFor(kind, index) {
        const slots = this.sidebar.channelSlots;
        const worn = new Set([0, 1].map((i) => slots[i])
            .filter((slot) => slot && slot.enabled).map((slot) => (slot.colorHex || "").toLowerCase()));
        this.claims.forEach((claim) => worn.add((claim.applied.colorHex || "").toLowerCase()));
        const first = kind === "mutual_exclusion" ? REVIEW_PALETTE.exclusion : REVIEW_PALETTE.overlap;
        const second = first === REVIEW_PALETTE.exclusion ? REVIEW_PALETTE.overlap
            : REVIEW_PALETTE.exclusion;
        const pick = [...first, ...second].find((hex) => !worn.has(hex.toLowerCase()));
        return pick || this.sidebar.getDefaultColor?.(index)?.hex || first[0];
    }

    /** Whether a claimed slot is still as this review left it. */
    isMine(claim) {
        const slot = this.sidebar?.channelSlots?.[claim.index];
        if (!slot || slot.visible === false || !slot.enabled) return false;
        if (slot.name !== claim.applied.name) return false;
        return !slot.userColorChanged
            || (slot.colorHex || "").toLowerCase() === (claim.applied.colorHex || "").toLowerCase();
    }

    /**
     * Drop the claims the user has taken over, untouched, and remember each
     * channel as declined for the marker it was claimed for: it is not taken
     * again while that marker is reviewed.
     */
    forgetTouched() {
        this.claims = this.claims.filter((claim) => {
            if (this.isMine(claim)) return true;
            if (!this.declined.has(claim.forMarker)) this.declined.set(claim.forMarker, new Set());
            this.declined.get(claim.forMarker).add(claim.channel);
            this.signature = null;
            return false;
        });
    }

    /**
     * Put one claimed slot back: hidden again (core's own removal switches
     * the channel off), and a row that was hidden before with every field it
     * had; one added for the review stays an empty hidden row.
     */
    putBack(claim) {
        const sidebar = this.sidebar;
        const slot = sidebar.channelSlots?.[claim.index];
        if (!slot || !this.isMine(claim)) return;
        sidebar.removeChannelSlot(claim.index);
        if (!claim.prev.created) {
            const { created, ...was } = claim.prev;
            Object.keys(slot).forEach((key) => { if (!(key in was)) delete slot[key]; });
            Object.assign(slot, was);
        }
        delete slot.transient;
        sidebar.syncSlotDom?.(slot);
    }

    /** Put back every claimed slot. */
    release() {
        this.forgetTouched();
        if (!this.claims.length) {
            this.signature = null;
            this.skipped = [];
            return;
        }
        this.claims.forEach((claim) => this.putBack(claim));
        this.claims = [];
        this.signature = null;
        this.skipped = [];
    }

    /**
     * Put back the slot showing `channel`, if this review holds one: the
     * channel is about to be the marker (setGateMarker's mirror into slot 2
     * would otherwise switch the claimed slot off, which reads as the user's).
     */
    releaseChannel(channel) {
        const claim = this.claims.find((c) => c.channel === channel);
        if (!claim) return;
        this.putBack(claim);
        this.claims = this.claims.filter((c) => c !== claim);
        this.signature = null;
    }

    /**
     * The viewer handed back by an agent (core rebuilt the slots): keep the
     * claims still as they were left, forget the rest without putting
     * anything back, then follow the marker again once the bridge has put
     * it back on the panel.
     */
    reconcile() {
        this.claims = this.claims.filter((claim) => this.isMine(claim));
        this.signature = null;
    }

    onAgentRestore(event) {
        this.reconcile();
        const again = () => Promise.resolve().then(() => this.controller.syncReview?.());
        if (typeof event?.detail?.wait === "function") event.detail.wait(again());
        else again();
    }

    dispose() {
        window.removeEventListener("plexora:agent-restore", this._onAgentRestore);
        this.claims = [];
        this.signature = null;
    }
}
