/**
 * renderClipboard.js - the Image card's copy and paste (layerManager.js).
 *
 * Two independent slots, so copying one never drops the other:
 *
 *   names      the reference image's channel names, in imageData order with
 *              Area excluded -- what POST /rename_channels takes back.
 *   rendering  the channel slots as they are drawn (marker, colour, on/off,
 *              raw contrast window), the Image layer's opacity and HD mode.
 *              Never the names themselves, and never any image data.
 *
 * sessionStorage, for the same reason as services/carryOver.js: every image
 * switch is a full page load, so anything held in memory is gone by the time
 * there is somewhere to paste it, and a clipboard is a per-tab, per-sitting
 * thing -- two tabs must not paste each other's arrangements, and tomorrow's
 * session should not open holding yesterday's.
 *
 * The planners (mergeNames, resolveSlots) are pure, and exported, so a node
 * probe can pin what a paste does without a page.
 */
window.PlexoraRenderClipboard = (function () {
    "use strict";

    const KEY = "plexora:clipboard";
    //: Bumped when the shape changes; an older document reads as empty.
    const VERSION = 1;

    function storage() {
        // sessionStorage THROWS, rather than returning null, in a private
        // window with site data blocked.
        try {
            return window.sessionStorage || null;
        } catch (error) {
            return null;
        }
    }

    function read() {
        const store = storage();
        if (!store) return { version: VERSION };
        try {
            const doc = JSON.parse(store.getItem(KEY) || "null");
            if (!doc || doc.version !== VERSION) return { version: VERSION };
            return doc;
        } catch (error) {
            return { version: VERSION };
        }
    }

    function write(slot, value) {
        const store = storage();
        if (!store) return false;
        try {
            const doc = read();
            doc[slot] = value;
            store.setItem(KEY, JSON.stringify(doc));
            return true;
        } catch (error) {
            return false;
        }
    }

    function copyNames(names, from) {
        const list = (Array.isArray(names) ? names : []).map((name) => String(name ?? ""));
        if (!list.length) return false;
        return write("names", { from: from || "", at: Date.now(), names: list });
    }

    function names() {
        const slot = read().names;
        return slot && Array.isArray(slot.names) && slot.names.length ? slot : null;
    }

    function copyRendering(snapshot, from) {
        const slots = snapshot && Array.isArray(snapshot.slots) ? snapshot.slots : [];
        if (!slots.length) return false;
        return write("rendering", {
            from: from || "",
            at: Date.now(),
            opacity: Number.isFinite(snapshot.opacity) ? snapshot.opacity : 1,
            hd: Boolean(snapshot.hd),
            slots,
        });
    }

    function rendering() {
        const slot = read().rendering;
        return slot && Array.isArray(slot.slots) && slot.slots.length ? slot : null;
    }

    function clear() {
        const store = storage();
        if (!store) return;
        try {
            store.removeItem(KEY);
        } catch (error) {
            // Nothing to clear, or nowhere to clear it from.
        }
    }

    /**
     * Copied names onto this image's names, by position.
     *
     * Position is the only thing a list of names can be matched on -- the
     * names are what is being changed. The overlap is applied; where the two
     * lists differ in length the rest of the image keeps its own names.
     *
     * A name an image may hold only once: a copied name that would collide
     * with one this image KEEPS (a position not being renamed, or one past the
     * copied list) is skipped, and so is the second of two identical copied
     * names. Repeated until nothing changes, because dropping a rename keeps
     * that position's old name, which can collide in turn -- so the result can
     * never hold a duplicate, which the server would refuse outright.
     *
     *   [A,B,C]  onto [X,Y,Z,A]  ->  [X,B,C,A]   2 of 4 applied
     *   [B,B,Z]  onto [A,B,C]    ->  [A,B,Z]     2 of 3 applied
     *
     * `applied` counts positions that now carry the copied name, including
     * ones that already did; `total` is the longer list, so a toast can say
     * "18 of 20 channel names applied."
     */
    function mergeNames(copied, current) {
        const source = (Array.isArray(copied) ? copied : []).map((name) => String(name ?? "").trim());
        const target = (Array.isArray(current) ? current : []).map((name) => String(name ?? ""));
        const n = Math.min(source.length, target.length);
        let assigned = [];
        for (let i = 0; i < n; i++) {
            if (source[i] && source[i] !== target[i]) assigned.push(i);
        }
        for (;;) {
            const renamed = new Set(assigned);
            const kept = new Set(target.filter((_, j) => !renamed.has(j)));
            const seen = new Set();
            const next = [];
            for (const i of assigned) {
                const name = source[i];
                if (kept.has(name) || seen.has(name)) continue;
                seen.add(name);
                next.push(i);
            }
            if (next.length === assigned.length) break;
            assigned = next;
        }
        const result = [...target];
        for (const i of assigned) result[i] = source[i];
        let applied = 0;
        for (let i = 0; i < n; i++) if (result[i] === source[i]) applied += 1;
        return {
            names: result,
            applied,
            skipped: n - applied,
            total: Math.max(source.length, target.length),
            changed: result.some((name, i) => name !== target[i]),
        };
    }

    /**
     * Copied channel slots onto this image's channels: by NAME first, and by
     * position only for a name this image does not have -- so two panels that
     * share names line up whatever order they are in, and two that do not
     * still get the arrangement slot for slot. A channel is used at most once.
     *
     * @param slots   the copied `rendering.slots`
     * @param columns this image's channel names, as the slots use them
     * @returns {{entries, matched, skipped}} entries in applyLaunchChannels'
     *   shape: `{ name, enabled, color?, range? }`, range in raw units.
     */
    function resolveSlots(slots, columns) {
        const names = Array.isArray(columns) ? columns : [];
        const used = new Set();
        const entries = [];
        let skipped = 0;
        for (const slot of Array.isArray(slots) ? slots : []) {
            const name = slot ? String(slot.name ?? "").trim() : "";
            if (!name) {
                skipped += 1;
                continue;
            }
            let target = null;
            if (names.includes(name)) target = name;
            else if (Number.isInteger(slot.index) && slot.index >= 0 && slot.index < names.length) {
                target = names[slot.index];
            }
            if (!target || used.has(target)) {
                skipped += 1;
                continue;
            }
            used.add(target);
            const entry = { name: target, enabled: slot.enabled !== false };
            if (slot.colorHex) entry.color = slot.colorHex;
            const range = slot.range;
            if (Array.isArray(range) && range.length === 2
                    && Number.isFinite(range[0]) && Number.isFinite(range[1])) {
                entry.range = [range[0], range[1]];
            }
            entries.push(entry);
        }
        return { entries, matched: entries.length, skipped };
    }

    return {
        copyNames,
        names,
        hasNames: () => Boolean(names()),
        copyRendering,
        rendering,
        hasRendering: () => Boolean(rendering()),
        clear,
        mergeNames,
        resolveSlots,
    };
})();
