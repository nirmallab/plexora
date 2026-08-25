/**
 * toolLoader.js - Always loaded, unlike a plugin's own scripts, which are fetched
 * lazily and named by the plugin's descriptor (see plexora/api/plugin.py's
 * Plugin.scripts and asset_urls).
 * Intercepts the navbar Tools-menu links so opening/closing an addon tool mid-session
 * never navigates: on first open it fetches the tool's sidebar HTML, stylesheets and
 * scripts from `/<datasource>/tools/<tool>/panel`, injects all three, and hands off to
 * main.js's `__plexora.activatePlugin()` to register the plugin exactly as it
 * would be at boot. Later opens just toggle visibility -- nothing is re-fetched.
 *
 * The row is a TOGGLE: picking it for a tool that is already on screen closes
 * that tool rather than re-opening what is already open. Its shortcut is a
 * synthetic click on the same element (keyboardShortcuts.js), so the key
 * toggles for free and cannot come to mean something the row does not.
 *
 * Each tool gets its OWN mount inside the shared slot. That was not so while
 * gating was the only plugin: the fragment was written straight to
 * `slot.innerHTML`, which is a whole-slot replace. With a second plugin that is
 * destructive in a way nothing reports -- opening B wipes A's panel out of the
 * DOM while A's controller keeps the element handles it took at setup(), and the
 * re-open path here (which only unhides the slot) then shows an empty panel and
 * a live controller wired to nodes that are no longer on the page.
 *
 * THREE STATES, kept apart on purpose, because collapsing any two of them is
 * what made a second plugin unusable:
 *
 *   LOADED  - the tool's record, panel DOM, controller and cached data exist. It
 *             has a card in the sidebar and draws nothing.
 *   VISIBLE - it contributes a layer to the image. Several tools may be visible
 *             at once, and the card order is the order they stack in.
 *   ACTIVE  - it is the one the shared Cells control, the opacity slider, picking
 *             and the gate flows act on, and the one whose panel is expanded.
 *             Exactly one, or none.
 *
 * Opening a tool makes it all three and stands the previous one down to LOADED,
 * so the default is still one thing on screen. Turning another card's eye back on
 * is what stacks them -- one click, and it is the whole feature. That click also
 * PINS the layer, and a pinned layer is exempt from the stand-down: the default
 * is for the first switch, not a rule that keeps dismantling a stack somebody
 * built on purpose.
 *
 * The one exception to single-active is a COEXISTING PAIR: two tools opened
 * together through openToolAlongside(), which stay expanded and drawn side by
 * side while the selection moves freely between them. Cell Explorer's Open ROIs
 * button is the only caller, because its ROI composition card summarises the
 * cells under an overlay that has to still be drawn for the answer to mean
 * anything. Opening a third tool, or closing either half, dissolves the pair --
 * the exception never outlives the pairing that justified it.
 *
 * Visibility of the PANELS is therefore a function of each entry's own
 * `collapsed` flag, applied in `paint()`, rather than something each call site
 * toggles for itself.
 *
 * Switching away also has to TELL the tool. A sidebar panel can be hidden and
 * left running, but a tool that reaches outside its panel -- viewer-canvas
 * pointer handlers, document-level keyboard shortcuts -- has to stand those down
 * or it keeps eating input for a panel the user cannot see. That is what
 * `onHide()` is for (see pluginRegistry.js); `onVisibilityChange()` is its
 * counterpart for the eye toggle, and `ctx.onCleanup` remains the full-teardown
 * path for when a plugin is removed outright.
 *
 * Deliberately kept off the `__plexora` object: that object is created fresh by
 * main.js (`const __plexora = window.__plexora = {...}`), which -- because this
 * script runs first in document order -- would clobber anything stored on it before
 * main.js's own init() runs.
 */
window.PlexoraToolLoader = (function () {
    //: toolName -> { slotIds, sidebarController, visible, collapsed, pinned }
    const loadedTools = new Map();

    //: The tool the shared controls point at, or null when none is. Every
    //: activation decision is derived from this rather than tracked per element.
    let activeToolName = null;

    //: The one sanctioned exception to single-active: a Set of exactly two tool
    //: names that stay expanded and drawn together. Cell Explorer opens ROI this
    //: way, because its ROI composition card only means anything while the
    //: metadata overlay it summarises is still on screen underneath. Kept
    //: deliberately narrow -- only openToolAlongside() forms a pair, and a third
    //: tool or closing either half dissolves it rather than leaving a standing
    //: exception behind.
    let coexistPair = null;

    /** The other half of the engaged pair, or null. */
    function pairPartner(name) {
        if (!coexistPair || !coexistPair.has(name)) return null;
        let partner = null;
        coexistPair.forEach((other) => { if (other !== name) partner = other; });
        return partner;
    }

    /** Whether `name` is sharing the screen with whichever tool is selected. A
     *  pair is only ever engaged AROUND the active tool, so a name still in
     *  coexistPair while the active tool is not in it is stale, not coexisting. */
    function isCoexisting(name) {
        return Boolean(coexistPair && coexistPair.has(name)
            && coexistPair.has(activeToolName));
    }

    /**
     * Whether this tool is ON SCREEN, which is the question the Tools-menu row
     * toggles against.
     *
     * Not the same as `loadedTools.has()`. A tool stood down when another one
     * opened is still loaded -- its data, its colours and its panel are all
     * intact -- and picking its row is a request to bring it back, not to close
     * something the user cannot see. Both halves of a coexisting pair count,
     * because both are on screen and either can be the one being closed.
     */
    function isOpen(name) {
        const entry = loadedTools.get(name);
        if (!entry) return false;
        return (name === activeToolName || isCoexisting(name)) && !entry.collapsed;
    }

    const HIDDEN = "tool-panel-hidden";
    const MOUNT_ATTR = "data-tool-panel";
    const CARD_ATTR = "data-tool-card";
    const ACCENT_ATTR = "data-tool-accent";

    //: How many card hues viewer.css defines. Slots wrap past this, so two cards
    //: can end up the same colour -- no worse than the single shared accent this
    //: replaced, and it takes nine tools in one session to get there.
    const ACCENT_SLOTS = 8;

    //: toolName -> the slot it drew. Never released, not even when the tool is
    //: removed: a tool closed and reopened comes back the colour it was, so the
    //: user's "the green one" outlives the card.
    const accentByTool = new Map();

    //: The one slot that gets cards. `tool_panel_legacy_slot` is off-screen
    //: scaffolding (gating mounts its download panel there); wrapping that in a
    //: card would put a draggable header on something nobody can see.
    const CARD_SLOT = "tool_panel_slot";

    //: The drag-to-restack binding, created once the first card exists.
    let sortable = null;

    function detach(node) {
        if (!node) return;
        if (node.remove) node.remove();
        else node.parentNode?.removeChild?.(node);
    }

    /** The tool's human name, taken from the Tools-menu link that opens it, so
     *  a plugin does not have to send its label twice. */
    function toolLabel(toolName) {
        const link = document.querySelector?.(`a[data-tool="${toolName}"]`);
        const text = link?.textContent?.trim?.();
        return text || toolName;
    }

    /**
     * Which of viewer.css's card hues this tool draws, decided once per session.
     *
     * Hashed from the tool's NAME rather than handed out in load order, so a
     * plugin keeps the same colour between sessions instead of changing with
     * whatever the user happened to open first. Core names no plugin here: a
     * hand-picked colour for the four plugins that ship would be a look a
     * third-party plugin could not get, which is the thing the plugin API exists
     * to prevent.
     *
     * Hashing ALONE is not enough, and this is the part worth keeping. Four
     * names into eight slots collide better than half the time -- FNV-1a puts
     * three of the four bundled plugins on the same slot -- and two identical
     * edges is the one outcome that makes the whole feature pointless. So the
     * hash only picks a PREFERENCE and the walk guarantees distinctness while
     * fewer than nine tools have been opened. viewer.css orders its hues so each
     * step of that walk is a long jump around the wheel.
     *
     * `>>> 0` because Math.imul returns a SIGNED 32-bit int: a negative hash
     * would produce a negative slot, which matches no rule and leaves the card
     * on the default hue.
     */
    function accentSlot(toolName) {
        if (accentByTool.has(toolName)) return accentByTool.get(toolName);
        let hash = 2166136261;
        for (let i = 0; i < toolName.length; i++) {
            hash = Math.imul(hash ^ toolName.charCodeAt(i), 16777619);
        }
        const preferred = (hash >>> 0) % ACCENT_SLOTS;
        const taken = new Set(accentByTool.values());
        let slot = preferred + 1;
        for (let step = 0; step < ACCENT_SLOTS; step++) {
            const candidate = ((preferred + step) % ACCENT_SLOTS) + 1;
            if (!taken.has(candidate)) {
                slot = candidate;
                break;
            }
        }
        accentByTool.set(toolName, slot);
        return slot;
    }


    /** `icons` is one class string per glyph. More than one is how a button that
     *  has two states is built here -- see the eye in buildCard. */
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
     * One tool's card: a grip, a collapse chevron, the tool's name, an eye and a
     * remove button, over the panel the plugin rendered.
     *
     * The panel is wrapped, never re-parented later: collapsing is a class on
     * the card, so the controller's element handles -- taken once at setup() --
     * stay valid for the life of the tool. That is what makes expanding instant
     * instead of a re-render.
     */
    function buildCard(toolName, mount) {
        const card = document.createElement("section");
        card.className = "tool-card";
        card.setAttribute(CARD_ATTR, toolName);
        // An attribute rather than an inline style, so every colour stays in
        // viewer.css and this decides only WHICH one. setAttribute rather than
        // dataset to match CARD_ATTR above -- the two are read by the same
        // selectors and drifting between the two APIs helps nobody.
        card.setAttribute(ACCENT_ATTR, String(accentSlot(toolName)));

        const header = document.createElement("div");
        header.className = "tool-card-header";

        const grip = document.createElement("span");
        grip.className = "tool-card-grip fas fa-grip-vertical";
        grip.title = "Drag to restack the layers";
        header.appendChild(grip);

        header.appendChild(iconButton(
            "tool-card-collapse", "Collapse or expand this panel", "fas fa-chevron-down",
            () => setToolCollapsed(toolName, !loadedTools.get(toolName)?.collapsed)));

        const title = document.createElement("button");
        title.type = "button";
        title.className = "tool-card-title";
        title.textContent = toolLabel(toolName);
        // Selecting a card is what moves the shared controls onto it -- and,
        // being the single-active path, folds the previous one away.
        title.addEventListener("click", () => show(toolName));
        header.appendChild(title);

        // Both glyphs go in, and CSS shows whichever the card's is-layer-off
        // class calls for. Rewriting one glyph's class from JS does NOT work:
        // FontAwesome is loaded as JS (vendor.js), so it replaces every
        // `<span class="fas fa-...">` with an `<svg>` before anyone can click
        // anything -- the span the swap went looking for is no longer on the
        // page, and nothing reports that. It is why a hidden layer used to sit
        // under an open eye.
        header.appendChild(iconButton(
            "tool-card-eye", "Show or hide this tool's layer",
            ["fas fa-eye tool-card-eye-on", "fas fa-eye-slash tool-card-eye-off"],
            () => setToolVisible(toolName, !loadedTools.get(toolName)?.visible)));

        header.appendChild(iconButton(
            "tool-card-remove", "Remove this tool", "fas fa-xmark",
            () => removeTool(toolName)));

        card.appendChild(header);

        const body = document.createElement("div");
        body.className = "tool-card-body";
        body.appendChild(mount);
        card.appendChild(body);
        return card;
    }

    function cardFor(toolName) {
        const slot = document.getElementById(CARD_SLOT);
        return slot?.querySelector?.(`[${CARD_ATTR}="${toolName}"]`) || null;
    }

    /** One tool's wrapper inside one slot, created on demand. */
    function mountFor(slotId, toolName, create) {
        const slot = document.getElementById(slotId);
        if (!slot) return null;
        let mount = slot.querySelector?.(`[${MOUNT_ATTR}="${toolName}"]`) || null;
        if (!mount && create) {
            mount = document.createElement("div");
            mount.className = "tool-panel-mount";
            mount.setAttribute(MOUNT_ATTR, toolName);
            if (slotId === CARD_SLOT) {
                // At the TOP of the slot: the cards read downwards and the top
                // one is the topmost layer, so a tool the user just opened is
                // over everything already loaded.
                const card = buildCard(toolName, mount);
                if (slot.firstChild) slot.insertBefore(card, slot.firstChild);
                else slot.appendChild(card);
            } else {
                slot.appendChild(mount);
            }
        }
        return mount;
    }

    /**
     * Show every expanded tool's panel and hide the collapsed ones.
     *
     * A slot is hidden when nothing in it is showing, which is what the class on
     * the slot itself has always meant -- the server renders it that way for a
     * page opened with no tool. The card slot is the exception: a collapsed card
     * still shows its header, so that slot stays open for as long as it holds a
     * card at all.
     */
    function paint() {
        const slotIds = new Set();
        loadedTools.forEach((entry) => entry.slotIds.forEach((id) => slotIds.add(id)));

        slotIds.forEach((slotId) => {
            const slot = document.getElementById(slotId);
            if (!slot) return;
            const isCardSlot = slotId === CARD_SLOT;
            let showing = false;
            loadedTools.forEach((entry, name) => {
                if (!entry.slotIds.includes(slotId)) return;
                // In the card slot a panel shows unless its own card is folded.
                // In the off-screen legacy slot there are no cards, so only the
                // active tool's mount shows -- which is what it always did.
                const visible = isCardSlot
                    ? !entry.collapsed
                    : (name === activeToolName || isCoexisting(name));
                showing = showing || visible || isCardSlot;
                const mount = mountFor(slotId, name);
                if (mount) mount.classList.toggle(HIDDEN, !visible);
            });
            slot.classList.toggle(HIDDEN, !showing);
        });
        paintCards();
    }

    /**
     * The card headers: which is folded, which is selected, which layer is off.
     *
     * Three classes and nothing else -- the chevron's direction and which of the
     * eye's two glyphs shows both hang off these in CSS, so there is no glyph
     * here to keep in step and no way for the icons to drift out of it.
     */
    function paintCards() {
        loadedTools.forEach((entry, name) => {
            const card = cardFor(name);
            if (!card) return;
            card.classList.toggle("is-collapsed", Boolean(entry.collapsed));
            // Both halves of a coexisting pair read as selected: only one of
            // them owns the shared controls, but the user opened them together
            // and one greyed-out card would look like something had failed.
            card.classList.toggle("is-active",
                name === activeToolName || isCoexisting(name));
            card.classList.toggle("is-layer-off", !entry.visible);
        });
        paintMenuRows();
    }

    /**
     * Mark the Tools-menu rows whose tool is on screen.
     *
     * The row is a toggle, so the menu has to say which way round it is:
     * without this, picking a row for something already open looks like the row
     * failed rather than like it closed. main.css draws it -- one class, and no
     * glyph here to keep in step, for the same reason the cards carry classes
     * rather than icons.
     *
     * Over EVERY row rather than the loaded ones, so a tool that has just been
     * removed loses its mark too.
     */
    function paintMenuRows() {
        const links = document.querySelectorAll?.("a[data-tool]") || [];
        links.forEach((link) => {
            link.classList.toggle("is-open", isOpen(link.dataset?.tool));
        });
    }

    /**
     * Sidebar order is layer order, and the top card is the top layer.
     *
     * Core stacks bottom-first, so the DOM order is reversed on the way out
     * rather than the cards being built upside down -- a list whose first row is
     * the bottom of the picture reads backwards to everyone.
     */
    function syncLayerOrder() {
        const slot = document.getElementById(CARD_SLOT);
        if (!slot?.children) return;
        const names = [];
        Array.from(slot.children).forEach((child) => {
            const name = child.getAttribute?.(CARD_ATTR);
            if (name && loadedTools.has(name)) names.push(name);
        });
        names.reverse();
        try {
            window.__plexora?.setToolLayerOrder?.(names);
        } catch (error) {
            console.error("toolLoader: setToolLayerOrder() failed", error);
        }
    }

    /** Drag-to-restack, on the same vendored library the column classifier uses
     *  (columnClassifier.js). Handle-only, so a click anywhere else in the
     *  header still reaches the button it landed on. */
    function ensureSortable() {
        const slot = document.getElementById(CARD_SLOT);
        if (!slot || sortable || typeof window.Sortable !== "function") return;
        sortable = new window.Sortable(slot, {
            handle: ".tool-card-grip",
            draggable: ".tool-card",
            animation: 150,
            onSort: syncLayerOrder,
        });
    }

    /**
     * Push one tool's on/off state everywhere it has to land.
     *
     * Core switches the cell layer for a plugin that has one; a tool that draws
     * its own overlay (ROI) has nothing for core to switch, so its controller is
     * told directly. Without the second half the eye on ROI's card would be a
     * button that does nothing.
     */
    function applyToolVisible(toolName, on) {
        const entry = loadedTools.get(toolName);
        if (!entry) return;
        entry.visible = Boolean(on);
        try {
            window.__plexora?.setToolLayerVisible?.(toolName, entry.visible);
        } catch (error) {
            console.error("toolLoader: setToolLayerVisible() failed", error);
        }
        try {
            entry.sidebarController?.onVisibilityChange?.(entry.visible);
        } catch (error) {
            console.error("toolLoader: onVisibilityChange() failed", error);
        }
    }

    /**
     * The card's eye. Unlike every other path that changes visibility, this one
     * is a DECISION, and it is recorded as one.
     *
     * `pinned` is what stops the single-active default from undoing a stack the
     * user built on purpose. Opening a tool turns the previous one's layer off,
     * which is right the first time -- but once somebody has explicitly put a
     * second layer back on to compare the two, clicking between their cards must
     * stop taking it away again. Same shape as a layer's `userMode`: an
     * automatic choice fills a gap, an explicit one is kept.
     */
    function setToolVisible(toolName, on) {
        const entry = loadedTools.get(toolName);
        if (!entry) return;
        entry.pinned = Boolean(on);
        if (entry.visible === Boolean(on)) return;
        applyToolVisible(toolName, on);
        paintCards();
    }

    function setToolCollapsed(toolName, collapsed) {
        const entry = loadedTools.get(toolName);
        if (!entry) return;
        entry.collapsed = Boolean(collapsed);
        paint();
    }

    /** Put one tool back to LOADED: folded away, no longer drawing unless the
     *  user pinned it, and told so anything it hung outside its own panel --
     *  canvas handlers, document shortcuts -- stands down with it. */
    function fold(toolName) {
        const entry = loadedTools.get(toolName);
        if (!entry) return;
        entry.collapsed = true;
        if (!entry.pinned) applyToolVisible(toolName, false);
        try {
            entry.sidebarController?.onHide?.();
        } catch (error) {
            console.error("toolLoader: onHide() failed", error);
        }
    }

    /**
     * Fold the rest of the sidebar away for a tool that has just arrived.
     *
     * A newly loaded card lands at the BOTTOM of a stack that may already be
     * several panels and the whole channel list tall, so on a laptop the thing
     * the user just asked for opens below the fold and the click looks like it
     * did nothing. Folding its neighbours is what brings it on screen.
     *
     * Only on a tool's FIRST load, which is why this sits here and not in
     * show(): reopening a tool that is already loaded is a switch between panels
     * the user has since arranged by hand, and re-folding their channel list on
     * every switch would be taking that arrangement away from them again and
     * again. standDown() already handles what the OUTGOING tool does on a
     * switch, and it is the only thing that should.
     *
     * Cards only -- `collapsed`, not fold(). Which layers are drawn is
     * standDown()'s business and stays its business; this is a tidy-up of the
     * sidebar, and a tool whose layer the user pinned with the eye keeps drawing
     * with its card shut, exactly as it does when folded by hand.
     *
     * Both halves of a coexisting pair are spared, for the same reason
     * standDown() spares them: the pairing is an explicit arrangement and it
     * outranks the tidy-up.
     *
     * And nothing happens at all for a tool that puts nothing IN the sidebar.
     * Figure Builder declares `panels={}` -- its controls are a dock over the
     * image and its canvas has its own page -- so there is no card to make room
     * for, and folding the user's channel list to reveal it would clear a space
     * that stays empty. Keyed on the tool having no slots rather than on its
     * name: any future page-only plugin is then covered by construction.
     */
    function collapseForNewTool(toolName) {
        if (!loadedTools.get(toolName)?.slotIds?.length) return;
        loadedTools.forEach((entry, name) => {
            if (name === toolName || coexistPair?.has(name)) return;
            entry.collapsed = true;
        });
        // Core's own section, which knows nothing about tools -- reached through
        // the same bridge the rest of this module uses, and guarded the same
        // way, because an RGB image has no channel section to fold.
        try {
            window.__plexora?.viewerSidebar?.setChannelSectionCollapsed?.(true);
        } catch (error) {
            console.error("toolLoader: collapsing the channel section failed", error);
        }
    }

    /**
     * Stand the selected tool down, unless it is the one being opened.
     *
     * Called before another tool is shown rather than when its own close button
     * is pressed, so a tool never has to know it was switched away from.
     *
     * Single-active by default: the outgoing tool folds up AND its layer goes
     * off, so opening a second tool shows one picture rather than two stacked
     * ones nobody asked to compare. It stays LOADED -- its data, its colours and
     * its panel are all still there -- so turning it back on is one click.
     *
     * Unless the user has pinned it with the eye, which is the one thing that
     * outranks the default -- see setToolVisible -- or the outgoing tool is
     * half of a coexisting pair, which is the other.
     */
    function standDown(except) {
        if (!activeToolName || activeToolName === except) return;
        const previous = activeToolName;
        // Moving the selection between the two halves of a coexisting pair is
        // not a switch away from anything: both cards stay open and both layers
        // stay drawn, and only which one the shared controls point at changes.
        if (except && isCoexisting(previous) && coexistPair.has(except)) return;
        // A third tool does end the arrangement, and it folds BOTH halves --
        // otherwise the exception outlives the pairing that justified it and the
        // user is left with a stray layer nobody asked to keep.
        const partner = pairPartner(previous);
        coexistPair = null;
        activeToolName = null;
        fold(previous);
        if (partner) fold(partner);
    }

    function show(toolName) {
        standDown(toolName);
        const entry = loadedTools.get(toolName);
        if (entry) {
            entry.collapsed = false;
            applyToolVisible(toolName, true);
        }
        activeToolName = toolName;
        paint();
        syncLayerOrder();
        // Before onShow(), not after: a controller that re-applies its cell
        // colours there is talking to controls that have to be pointing at this
        // tool already, and core's own Cells control has to have adopted this
        // layer's mode before the panel reads it back. See main.js's
        // setActiveTool.
        try {
            window.__plexora?.setActiveTool?.(toolName);
        } catch (error) {
            console.error("toolLoader: setActiveTool() failed", error);
        }
        try {
            loadedTools.get(toolName)?.sidebarController?.onShow?.();
        } catch (error) {
            console.error("toolLoader: onShow() failed", error);
        }
    }

    function loadScript(src) {
        if (document.querySelector(`script[src="${src}"]`)) return Promise.resolve();
        return new Promise((resolve, reject) => {
            const script = document.createElement("script");
            script.src = src;
            script.onload = () => resolve();
            script.onerror = () => reject(new Error(`toolLoader: failed to load ${src}`));
            document.head.appendChild(script);
        });
    }

    function loadStyle(href) {
        if (document.querySelector(`link[rel="stylesheet"][href="${href}"]`)) return Promise.resolve();
        return new Promise((resolve) => {
            const link = document.createElement("link");
            link.rel = "stylesheet";
            link.href = href;
            // Resolves either way, unlike loadScript: a plugin whose stylesheet
            // is missing still works, so refusing to open it would turn a
            // cosmetic fault into a broken tool. The console line is what says
            // an unstyled panel was not intended.
            link.onload = () => resolve();
            link.onerror = () => {
                console.error(`toolLoader: failed to load ${href}`);
                resolve();
            };
            document.head.appendChild(link);
        });
    }

    async function openTool(toolName, linkEl) {
        if (loadedTools.has(toolName)) {
            show(toolName);
            return;
        }

        linkEl?.classList.add("tool-loading");
        try {
            const datasource = window.flaskVariables?.datasource;
            const baseUrl = window.PLEXORA_BASE_URL || "";
            const response = await fetch(`${baseUrl}/${datasource}/tools/${toolName}/panel`);
            const payload = await response.json();

            if (payload.needs) {
                // The tool is installed and compatible but the project is
                // missing something it declared. Ask for exactly that, then
                // re-enter -- navigating away to collect a column name would
                // tear down and rebuild the whole viewer to answer one
                // question, which is the reason this lazy path exists.
                const satisfied = await window.PlexoraRequirements.collect(
                    datasource, payload.needs);
                if (!satisfied) return;
                return openTool(toolName, linkEl);
            }

            if (payload.redirect) {
                // Unknown datasource or an uninstalled tool -- the server has
                // decided where to send us.
                PlexoraRouter.go(payload.redirect);
                return;
            }

            // Stylesheets before the markup, and awaited: the fragments below
            // are shown as soon as their slots are unhidden, and a plugin's own
            // CSS is the only thing that styles them. Skipping this is what
            // made gating's panels render raw -- the file input as a bare
            // "Choose File", the download panel with no surface -- whenever the
            // tool was opened from the Tools menu rather than loaded with
            // ?tool=..., which is the path base.html covers.
            await Promise.all((payload.styles || []).map(loadStyle));

            const slotIds = Object.keys(payload.fragments || {});
            slotIds.forEach((slotId) => {
                // Into this tool's own mount, never the slot: the slot is shared
                // and writing the whole of it destroys any other tool's panel.
                const mount = mountFor(slotId, toolName, true);
                if (mount) mount.innerHTML = payload.fragments[slotId];
            });

            for (const src of payload.scripts || []) {
                await loadScript(src);
            }

            // main.js runs before any tool can be opened (deferred, but earlier in
            // document order isn't guaranteed here -- this script loads first --
            // so wait on its own readiness promise rather than assuming it's done).
            await window.__plexoraReady;
            const moduleDef = window.Plexora?.plugins?.get(toolName);
            if (!moduleDef || !window.__plexora?.activatePlugin) return;
            const { sidebarController } = await window.__plexora.activatePlugin(moduleDef);

            loadedTools.set(toolName, {
                slotIds,
                sidebarController,
                visible: true,
                collapsed: false,
                pinned: false,
            });
            collapseForNewTool(toolName);
            ensureSortable();
            show(toolName);
        } finally {
            linkEl?.classList.remove("tool-loading");
        }
    }

    /**
     * Open `toolName` alongside `anchorToolName` rather than in place of it.
     *
     * Single-active is the right default for tools that are alternative views of
     * one picture, but Cell Explorer and ROI are not alternatives: the ROI
     * composition card only means anything while the metadata overlay it
     * summarises is still drawn underneath. Rather than weaken standDown() for
     * everyone, the exception is a named pair that only this entry point can
     * form -- opening ROI from the Tools menu still behaves exactly as before.
     *
     * The pair is set BEFORE openTool(), because openTool() reaches show() ->
     * standDown() synchronously on the already-loaded path, and standDown() is
     * the call the pair exists to short-circuit.
     */
    async function openToolAlongside(toolName, anchorToolName) {
        if (!toolName) return;
        if (!anchorToolName || anchorToolName === toolName
            || !loadedTools.has(anchorToolName)) {
            // Nothing to ride along with -- an ordinary open.
            return openTool(toolName);
        }
        if (activeToolName !== anchorToolName) show(anchorToolName);
        const anchor = loadedTools.get(anchorToolName);
        if (anchor) anchor.collapsed = false;
        coexistPair = new Set([anchorToolName, toolName]);
        try {
            await openTool(toolName);
        } finally {
            // openTool() returns without loading anything on the redirect and
            // requirements-declined paths; there is no pair if the second half
            // never arrived.
            if (!loadedTools.has(toolName)) coexistPair = null;
            paint();
        }
    }

    /**
     * The panel's own close button: fold the card away and stop drawing, but
     * keep the tool loaded.
     *
     * Removing it outright is the card's X instead. A close that unloaded
     * everything would throw away a cached column and a rebuilt lookup table to
     * reclaim a strip of sidebar, and there would be no sign the tool had ever
     * been open.
     */
    function hideTool(toolName) {
        const entry = loadedTools.get(toolName);
        if (!entry) return;
        // Explicit, so it clears the pin the same way the eye does -- a close
        // that left the layer drawing would be a button whose name is a lie.
        entry.pinned = false;
        const partner = pairPartner(toolName);
        if (partner) coexistPair = null;
        if (activeToolName === toolName) {
            standDown();
        } else {
            entry.collapsed = true;
            applyToolVisible(toolName, false);
        }
        // The surviving half of a pair becomes the sole selected tool. Without
        // this, closing ROI from its own panel would leave Cell Explorer
        // expanded but unselected, with the shared controls pointing at nothing.
        if (partner && loadedTools.has(partner) && activeToolName !== partner) {
            show(partner);
            return;
        }
        paint();
    }

    /**
     * Unload a tool: tear the plugin down, take its card and every one of its
     * mounts off the page, and forget it.
     *
     * Re-opening from the Tools menu afterwards goes the full way round again --
     * the script tags persist so loadScript no-ops, re-registering the same name
     * in the plugin registry is harmless, and a fresh instance and controller are
     * built. The mounts have to go from EVERY slot for that to work, including
     * the off-screen legacy one: a stale wrapper there is found by mountFor,
     * returned as if it were new, and the freshly fetched fragment is written
     * into markup the new controller never saw.
     */
    function removeTool(toolName) {
        const entry = loadedTools.get(toolName);
        if (!entry) return;
        // Read before the pair is dissolved: closing one half of a coexisting
        // pair has to leave the other one selected, not drop the selection to
        // nothing the way standDown() on its own would.
        const partner = pairPartner(toolName);
        // Only when this tool is IN the pair -- removing an unrelated third tool
        // has nothing to say about two others sharing the screen.
        if (partner) coexistPair = null;
        if (activeToolName === toolName) standDown();
        try {
            window.__plexora?.deactivatePlugin?.(toolName);
        } catch (error) {
            console.error("toolLoader: deactivatePlugin() failed", error);
        }
        entry.slotIds.forEach((slotId) => {
            const mount = mountFor(slotId, toolName);
            const card = slotId === CARD_SLOT ? cardFor(toolName) : null;
            detach(card || mount);
        });
        loadedTools.delete(toolName);
        if (partner && loadedTools.has(partner) && activeToolName !== partner) {
            show(partner);  //: paints and re-syncs layer order for us
            return;
        }
        paint();
        syncLayerOrder();
    }

    /**
     * Close a tool through whichever close it actually has.
     *
     * A tool with a card has two, and this is the CHEAP one: the panel's close,
     * which folds the card away and stops it drawing but keeps everything
     * loaded. The card's X is still the way to unload it outright. Throwing away
     * a cached column and a rebuilt lookup table on a keystroke somebody may
     * have pressed twice by accident is not something a toggle should be able to
     * do, and the folded card is what says the tool is one click from coming
     * back.
     *
     * A tool with NO card has neither, and there the PLUGIN says what closing
     * means. Figure Builder's own Close asks before discarding captures that are
     * not in a figure yet and then removes the plugin; going round it to
     * removeTool() here would skip the question and lose the work. Removing is
     * only the fallback for a card-less tool that offers no close of its own,
     * where there is otherwise nothing on screen to close at all.
     */
    function closeTool(toolName) {
        const entry = loadedTools.get(toolName);
        if (!entry) return;
        if (entry.slotIds.length) {
            hideTool(toolName);
            return;
        }
        if (!entry.sidebarController?.close) {
            removeTool(toolName);
            return;
        }
        try {
            entry.sidebarController.close();
        } catch (error) {
            console.error("toolLoader: close() failed", error);
        }
    }

    /** What a Tools-menu row does, and therefore what its shortcut does: open
     *  the tool, or close it if it is already on screen. */
    function toggleTool(toolName, linkEl) {
        if (isOpen(toolName)) closeTool(toolName);
        else openTool(toolName, linkEl);
    }

    /**
     * Wrap a server-rendered panel in the same per-tool mount and card the lazy
     * path creates, so both paths leave the slot in the same shape.
     *
     * Without this, a page opened with ?tool=gating has gating's markup sitting
     * loose in the slot; opening a second tool would append its card alongside
     * and `paint()` would have nothing to hide the first one by.
     */
    function adopt(slotId, toolName) {
        const slot = document.getElementById(slotId);
        if (!slot || !slot.appendChild) return;
        if (slot.querySelector?.(`[${MOUNT_ATTR}="${toolName}"]`)) return;
        const mount = document.createElement("div");
        mount.className = "tool-panel-mount";
        mount.setAttribute(MOUNT_ATTR, toolName);
        while (slot.firstChild) mount.appendChild(slot.firstChild);
        slot.appendChild(slotId === CARD_SLOT ? buildCard(toolName, mount) : mount);
    }

    // Called by main.js when a tool was already active at boot (a direct/bookmarked
    // ?tool=gating load rendered its panel server-side, not through openTool() above) --
    // without this, the close button's hideToolPanel() would find nothing to hide, and
    // a later Tools-menu click would re-fetch/re-activate a module that's already live.
    function registerLoaded(toolName, slotIds, sidebarController) {
        if (loadedTools.has(toolName)) return;
        // Only the slots the server actually filled. main.js works the list out
        // from `data-tool-mount`, which index.html stamps on EVERY slot with the
        // active tool's name -- so a plugin that declared one panel is still
        // named on all of them. Adopting an empty slot builds a card with
        // nothing in it: a header, a grip and an eye over a panel that does not
        // exist. A plugin whose controls live somewhere other than the sidebar
        // (figure_builder puts them on the image) is entitled to no card at all,
        // and this is what makes the boot path agree with the lazy one, which
        // only ever sees the slots the payload named.
        //
        // `children`, not `firstChild` or textContent: a server-rendered slot
        // with no panel in it still holds the template's whitespace, which is a
        // text node and would read as content.
        const filled = slotIds.filter(
            (slotId) => Boolean(document.getElementById(slotId)?.children?.length));
        filled.forEach((slotId) => adopt(slotId, toolName));
        loadedTools.set(toolName, {
            slotIds: filled,
            sidebarController,
            visible: true,
            collapsed: false,
            pinned: false,
        });
        ensureSortable();
        // Through show(), so this path is not a second, quieter version of the
        // menu one. A server-rendered panel is every bit as visible as a lazily
        // opened one and needs the same onShow(): ROI attaches its viewer-canvas
        // and document handlers there, so setting activeToolName directly gave
        // a ?tool=roi page a panel that looked right and a pen that drew
        // nothing -- no pointer handlers, no shortcuts, and no error to say so.
        show(toolName);
    }

    document.addEventListener("DOMContentLoaded", () => {
        document.querySelectorAll("a[data-tool]").forEach((link) => {
            link.addEventListener("click", (event) => {
                event.preventDefault();
                toggleTool(link.dataset.tool, link);
            });
        });
    });

    /**
     * The viewer leaving the screen, and coming back, are onHide() and onShow().
     *
     * appRouter.js can put a whole page over the live viewer without unloading
     * anything, so a tool's panel can stop being on screen without any tool
     * having been switched. To a controller that is exactly what folding its
     * card is -- its panel is not visible, and anything it hung on the document
     * or the viewer canvas must stand down -- so it is reported through the hook
     * that already means that, rather than as a second lifecycle every plugin
     * would have to learn.
     *
     * Only the tool that is ON SCREEN, which is the same set fold() and show()
     * act on: a folded card was already stood down, and standing it down twice
     * would be matched by waking it when the user comes back to a card they
     * left closed.
     */
    function forEachShownTool(action) {
        for (const name of loadedTools.keys()) {
            if (!isOpen(name)) continue;
            try {
                action(loadedTools.get(name)?.sidebarController);
            } catch (error) {
                console.error(`toolLoader: ${name} failed on a viewer change`, error);
            }
        }
    }

    window.addEventListener("plexora:viewer-hidden",
        () => forEachShownTool((controller) => controller?.onHide?.()));
    window.addEventListener("plexora:viewer-shown",
        () => forEachShownTool((controller) => controller?.onShow?.()));

    return {
        hideToolPanel: hideTool,
        registerLoaded,
        /** Which tool's panels are showing, for a tool deciding whether its own
         *  global shortcuts should be listening. */
        activeTool: () => activeToolName,
        /** Whether a tool's layer is currently drawn, for a controller that has
         *  to answer the same question its card's eye does. */
        isToolVisible: (name) => Boolean(loadedTools.get(name)?.visible),
        setToolVisible,
        setToolCollapsed,
        removeTool,
        /** Whether this tool is on screen -- selected and expanded, or sharing
         *  the screen as half of a coexisting pair. */
        isToolOpen: isOpen,
        /** Open the tool, or close it if it is already open. What the Tools-menu
         *  row does, and what its shortcut therefore does. */
        toggleTool,
        /** Open `toolName` WITHOUT standing `anchorToolName` down -- the one
         *  sanctioned exception to single-active. Both cards stay expanded and
         *  both layers stay drawn until a third tool opens or either half is
         *  closed. Cell Explorer's Open ROIs button is the caller. */
        openToolAlongside,
        /** Whether this tool is sharing the screen with another right now. */
        isCoexisting,
        /** The tool it is sharing with, or null. */
        coexistPartner: (name) => (isCoexisting(name) ? pairPartner(name) : null),
        /** Every loaded tool, top card first. */
        loadedTools: () => Array.from(loadedTools.keys()),
    };
})();
