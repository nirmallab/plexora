/**
 * A draggable card with an eye: the sidebar's one card, built once.
 *
 * Two lists in the sidebar are the same object. A tool card carries a grip, a
 * collapse chevron, a name, an eye and an X over the panel a plugin rendered; a
 * layer card carries a grip, a collapse chevron, a name, an eye and (sometimes)
 * an X over that layer's controls. They stack by dragging, the top row is the
 * top of the picture, and turning an eye off leaves the row in place.
 *
 * Written twice they drift -- two grips with different titles, two eyes that
 * disagree about which glyph means off, two reorder handlers that reverse the
 * DOM order in different places. Written once they cannot. toolLoader.js built
 * the first one; this is that code, with the tool-specific parts handed in.
 *
 * TWO GLYPHS, NOT ONE. A button with two states gets both spans and lets CSS
 * show whichever the card's class calls for. Rewriting one glyph's class from JS
 * does NOT work here: FontAwesome is loaded as JS (vendor.js) and replaces every
 * `<span class="fas fa-...">` with an `<svg>` before anyone can click anything,
 * so the span the swap goes looking for is no longer on the page -- and nothing
 * reports that. It is why a hidden layer once sat under an open eye.
 *
 * Served as a classic script (see base.html).
 */
(function () {
    "use strict";

    /** `icons` is one class string per glyph. More than one is how a button that
     *  has two states is built here. */
    function iconButton(className, title, icons, onClick) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = className;
        button.title = title;
        button.innerHTML = [].concat(icons)
            .map((icon) => `<span class="${icon}"></span>`).join("");
        button.addEventListener("click", onClick);
        return button;
    }

    /**
     * One card: a header of controls over a body.
     *
     * The body is wrapped, never re-parented later, because collapsing is a
     * class on the card. A controller takes its element handles once at setup;
     * rebuilding the markup under it leaves every one of them pointing at a node
     * that is no longer on the page, and nothing reports that either.
     *
     * @param prefix      - the CSS class stem ("tool-card", "layer-card")
     * @param attr        - the attribute carrying this card's key
     * @param key         - the tool name or layer id
     * @param label       - what the title button says
     * @param hint        - the printed shortcut that opens this card's tool
     *                      ("⌘E"), or "" for a card no key reaches. Handed in
     *                      already formatted: what a chord looks like is
     *                      keyboardShortcuts.js's answer, on its platform.
     * @param body        - the element the card wraps
     * @param onCollapse  - called when the chevron is clicked, or null for a
     *                      card with nothing to fold away
     * @param onSelect    - called when the title is clicked, or null
     * @param onToggle    - called when the eye is clicked, or null for a card
     *                      with nothing to hide
     * @param onLock      - called when the padlock is clicked, or null for a
     *                      card that cannot be pinned. A locked row refuses to
     *                      be dragged, which is what the Layer Manager wants for
     *                      a reference layer somebody has finished registering
     *                      against.
     * @param lockFixed   - draw the padlock but let nobody press it, for a card
     *                      whose lock is a structural fact rather than the
     *                      user's choice. The base image is the only one: it
     *                      cannot leave the bottom of the stack.
     * @param onRemove    - called when the X is clicked, or null for a card that
     *                      cannot be removed. The synthesized layers use this:
     *                      the way to remove the mask layer is to remove the
     *                      mask, and an X that refused would be worse than none.
     * @param extras      - elements to drop into the header after the title and
     *                      before the eye, or null. What a modality's own card
     *                      needs that no other card has: the channel counter
     *                      and CSV-rename button on the base image, the kebab
     *                      on a plugin's. Handed in rather than built here,
     *                      because the point of one card is that it knows
     *                      nothing about any particular layer.
     * @param titles      - { grip, collapse, eye, remove } tooltips
     * @param attrs       - extra attributes to set on the card
     */
    function buildCard({
        prefix, attr, key, label, hint = "", body, extras = null,
        onCollapse = null, onSelect = null, onToggle = null, onLock = null,
        lockFixed = false, onRemove = null,
        titles = {}, attrs = {},
    }) {
        const card = document.createElement("section");
        card.className = `${prefix}`;
        card.setAttribute(attr, key);
        Object.entries(attrs).forEach(([name, value]) => card.setAttribute(name, value));

        const header = document.createElement("div");
        header.className = `${prefix}-header`;

        const grip = document.createElement("span");
        grip.className = `${prefix}-grip fas fa-grip-vertical`;
        grip.title = titles.grip || "Drag to restack the layers";
        header.appendChild(grip);

        if (onCollapse) {
            header.appendChild(iconButton(
                `${prefix}-collapse`,
                titles.collapse || "Collapse or expand this panel",
                "fas fa-chevron-down", onCollapse));
        }

        // THE NAME IS A SPAN, NOT THE BUTTON'S TEXT. A tool card takes its
        // label from the Tools-menu row, and keyboardShortcuts.js prints the
        // chord into that same row -- so `textContent` swept the key up with
        // the name and the card was titled "Cell Explorer⌘E", one word, in the
        // app's own font. Two elements is what lets the key be told apart from
        // the thing it opens. Truncation moves onto the name with it: an
        // ellipsis belongs to the part that can be long, and an elided "⌘E"
        // says nothing at all.
        const title = document.createElement("button");
        title.type = "button";
        title.className = `${prefix}-title`;
        const name = document.createElement("span");
        name.className = `${prefix}-title-text`;
        name.textContent = label;
        title.appendChild(name);
        if (hint) {
            const chord = document.createElement("span");
            chord.className = `${prefix}-key`;
            chord.textContent = hint;
            title.appendChild(chord);
        }
        if (onSelect) title.addEventListener("click", onSelect);
        header.appendChild(title);

        if (extras) {
            const slot = document.createElement("div");
            slot.className = `${prefix}-extras`;
            [].concat(extras).forEach((node) => node && slot.appendChild(node));
            header.appendChild(slot);
        }

        if (onToggle) {
            header.appendChild(iconButton(
                `${prefix}-eye`, titles.eye || "Show or hide this layer",
                [`fas fa-eye ${prefix}-eye-on`, `fas fa-eye-slash ${prefix}-eye-off`],
                onToggle));
        }

        if (onLock || lockFixed) {
            const lock = iconButton(
                `${prefix}-lock`, titles.lock || "Pin this layer in place",
                [`fas fa-lock-open ${prefix}-lock-off`, `fas fa-lock ${prefix}-lock-on`],
                onLock || (() => {}));
            // A LOCK THAT IS A FACT AND NOT A CHOICE. The base image is the
            // ground the rest composite onto: its card is sorted to the foot
            // of the list on every render, so a drag that got past the grip
            // would be undone by the next one. The padlock was therefore left
            // off that card entirely -- which left the grip refusing to drag
            // with nothing on the row to say why. Shown and unpressable says
            // it in the one place the user is already looking.
            if (lockFixed) {
                lock.disabled = true;
                lock.setAttribute("aria-disabled", "true");
            }
            header.appendChild(lock);
        }

        if (onRemove) {
            header.appendChild(iconButton(
                `${prefix}-remove`, titles.remove || "Remove", "fas fa-xmark", onRemove));
        }

        if (onCollapse) {
            // THE WHOLE HEADER IS THE CHEVRON. Folding a card was a 20px
            // target at one end of a row whose every other pixel did nothing,
            // which is the opposite of how a disclosure behaves anywhere else
            // -- and this sidebar now keeps one card open at a time, so
            // folding is the most-used gesture in the panel rather than a
            // tidying afterthought.
            //
            // It costs the row nothing: every control on it keeps its own
            // click, because a click that landed on one of them is let
            // through here rather than swallowed. The grip is on that list
            // too -- a drag begins there, and a drag that ends where it
            // started arrives as a click.
            header.className += ` ${prefix}-header-foldable`;
            header.addEventListener("click", (event) => {
                const hit = event.target?.closest?.(
                    `button, input, select, a, label, .${prefix}-grip`);
                // The title is a button, and on a card that gives it nothing
                // else to do -- every layer card -- it folds with the rest of
                // the row. Where it DOES have a job (a tool card selects
                // itself) that job wins.
                if (hit && !(hit === title && !onSelect)) return;
                onCollapse();
            });
        }

        card.appendChild(header);

        const wrapper = document.createElement("div");
        wrapper.className = `${prefix}-body`;
        if (body) wrapper.appendChild(body);
        card.appendChild(wrapper);
        return card;
    }

    /**
     * Drag-to-restack, on the vendored library the column classifier uses.
     *
     * Handle-only, so a click anywhere else in the header still reaches the
     * button it landed on.
     *
     * @param onMove - Sortable's own onMove hook, returning false to refuse a
     *   drag. The Layer Manager uses it to refuse CROSS-SURFACE moves: tiles
     *   composite inside OSD's world and overlays are drawn above it, so
     *   dragging a points layer under an image cannot be honoured. Refusing at
     *   the drag is the only place the user can see why; allowing it and
     *   ignoring it is a reorder that silently does nothing.
     */
    function ensureSortable(slot, { handle, draggable, onSort, onMove = null }) {
        if (!slot || typeof window.Sortable !== "function") return null;
        return new window.Sortable(slot, {
            handle,
            draggable,
            animation: 150,
            onSort,
            ...(onMove ? { onMove } : {}),
        });
    }

    /**
     * The keys in one slot, bottom of the stack first.
     *
     * THE TOP CARD IS THE TOP LAYER. Core stacks bottom-first, so the DOM order
     * is reversed on the way out rather than the cards being built upside down --
     * a list whose first row is the bottom of the picture reads backwards to
     * everyone who looks at it.
     */
    function orderFromSlot(slot, attr, keep = null) {
        if (!slot?.children) return [];
        const keys = [];
        Array.from(slot.children).forEach((child) => {
            const key = child.getAttribute?.(attr);
            if (key && (!keep || keep(key))) keys.push(key);
        });
        keys.reverse();
        return keys;
    }

    const api = { iconButton, buildCard, ensureSortable, orderFromSlot };
    // Both, and for one reason: in a browser `window` IS the global object, so
    // the bare identifier the callers use resolves either way. In a node vm --
    // which is how the probes run this -- `window` is an ordinary object inside
    // the sandbox, and only the second assignment makes the name reachable.
    if (typeof window !== "undefined") window.PlexoraCardList = api;
    if (typeof globalThis !== "undefined") globalThis.PlexoraCardList = api;
}());
