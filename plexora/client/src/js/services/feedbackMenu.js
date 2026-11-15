/**
 * feedbackMenu.js -- the navbar's feedback icon and its two-row menu:
 * Report a Bug and Request a Feature, each opening views/feedbackDialog.js.
 *
 * The menu grows out of the icon (scale and fade from its top-right corner,
 * the rows rising in a short stagger) and the icon's glyph turns into a close
 * mark while it is open. Transform and opacity only, so the compositor does
 * the work, on the shared --duration-* and --ease-standard tokens, which are
 * zero under prefers-reduced-motion (tokens.css).
 *
 * Keyboard: Enter, Space or ArrowDown on the icon opens the menu with the
 * first row focused; ArrowUp/ArrowDown move, Home/End jump, Enter chooses,
 * Escape closes and returns focus to the icon, Tab closes and moves on.
 *
 * Mounted like the globe beside it (services/remoteGlobe.js): once per
 * navbar, guarded, because the router swaps the page under the navbar and
 * registers page controllers again on every navigation.
 */
window.PlexoraFeedbackMenu = (function () {
    "use strict";

    const ITEMS = [
        { kind: "bug", icon: "bug", label: "Report a Bug", note: "Something is broken or wrong" },
        { kind: "feature", icon: "lightbulb", label: "Request a Feature", note: "Something Plexora should do" },
    ];

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function icon(name) {
        const node = el("span", `fas fa-${name}`);
        node.setAttribute("aria-hidden", "true");
        return node;
    }

    let mounted = null;

    function mount(root) {
        if (!root || mounted === root) return null;
        mounted = root;

        const glyphOpen = el("span", "feedback-button-glyph is-idle");
        glyphOpen.appendChild(icon("comment-dots"));
        const glyphClose = el("span", "feedback-button-glyph is-open");
        glyphClose.appendChild(icon("xmark"));
        root.replaceChildren(glyphOpen, glyphClose);
        root.setAttribute("aria-haspopup", "menu");
        root.setAttribute("aria-expanded", "false");
        root.setAttribute("aria-label", "Send feedback");
        root.setAttribute("data-tooltip", "Send feedback");
        root.setAttribute("data-tooltip-placement", "bottom");
        root.setAttribute("data-tooltip-align", "right");
        root.hidden = false;

        let panel = null;
        let closing = null;

        function rows() {
            return panel ? Array.from(panel.querySelectorAll(".feedback-menu-item")) : [];
        }

        function openMenu({ focus = "first" } = {}) {
            if (closing) finishClose();
            if (panel) return;
            panel = el("div", "feedback-menu");
            panel.setAttribute("role", "menu");
            panel.setAttribute("aria-label", "Send feedback");
            panel.id = "feedback_menu";
            root.setAttribute("aria-controls", panel.id);
            ITEMS.forEach((item, index) => {
                const row = el("button", "feedback-menu-item");
                row.type = "button";
                row.setAttribute("role", "menuitem");
                row.tabIndex = -1;
                row.dataset.kind = item.kind;
                row.style.setProperty("--feedback-row", String(index));
                const badge = el("span", `feedback-menu-icon is-${item.kind}`);
                badge.appendChild(icon(item.icon));
                const words = el("span", "feedback-menu-words");
                words.append(el("span", "feedback-menu-label", item.label), el("span", "feedback-menu-note", item.note));
                row.append(badge, words);
                row.addEventListener("click", () => choose(item.kind));
                panel.appendChild(row);
            });
            panel.addEventListener("keydown", onMenuKey);
            // Through the portal (see remoteGlobe.js): a fullscreen viewer's
            // ::backdrop would cover a panel parked on <body>.
            PopoverPortal.attach(panel);
            position();
            root.setAttribute("aria-expanded", "true");
            root.classList.add("is-open");
            // The next frame, so the transition runs from the closed state.
            requestAnimationFrame(() => {
                if (!panel) return;
                panel.classList.add("is-open");
                if (focus) (focus === "last" ? rows().at(-1) : rows()[0])?.focus({ preventScroll: true });
            });
            document.addEventListener("mousedown", onClickAway, true);
            document.addEventListener("keydown", onDocumentKey, true);
            window.addEventListener("resize", position);
        }

        function closeMenu({ restoreFocus = false } = {}) {
            if (!panel || closing) return;
            const leaving = panel;
            root.setAttribute("aria-expanded", "false");
            root.classList.remove("is-open");
            leaving.classList.remove("is-open");
            leaving.classList.add("is-closing");
            document.removeEventListener("mousedown", onClickAway, true);
            document.removeEventListener("keydown", onDocumentKey, true);
            window.removeEventListener("resize", position);
            if (restoreFocus) root.focus({ preventScroll: true });
            const done = () => finishClose();
            closing = { node: leaving, timer: setTimeout(done, 400) };
            leaving.addEventListener("transitionend", (event) => {
                if (event.target === leaving && event.propertyName === "opacity") done();
            });
            // Nothing to wait for when motion is off (the durations are 0).
            if (!parseFloat(getComputedStyle(leaving).transitionDuration)) done();
        }

        function finishClose() {
            if (!closing) return;
            clearTimeout(closing.timer);
            PopoverPortal.detach(closing.node);
            if (panel === closing.node) panel = null;
            closing = null;
            root.removeAttribute("aria-controls");
        }

        function choose(kind) {
            // Focus goes back to the icon first, so the dialog returns it there.
            closeMenu({ restoreFocus: true });
            window.PlexoraFeedback?.open(kind);
        }

        function toggle(event) {
            if (panel && !closing) closeMenu();
            // Enter or Space (a click with no pointer, detail 0) opens with the
            // first row focused; a pointer click leaves focus where it is.
            else openMenu({ focus: event && event.detail === 0 ? "first" : null });
        }

        function onRootKey(event) {
            if (event.key === "ArrowDown") {
                event.preventDefault();
                if (panel && !closing) rows()[0]?.focus();
                else openMenu({ focus: "first" });
            } else if (event.key === "ArrowUp") {
                event.preventDefault();
                openMenu({ focus: "last" });
            }
        }

        function onMenuKey(event) {
            const items = rows();
            const at = items.indexOf(document.activeElement);
            const move = (to) => {
                event.preventDefault();
                items[(to + items.length) % items.length]?.focus();
            };
            if (event.key === "ArrowDown") move(at + 1);
            else if (event.key === "ArrowUp") move(at - 1);
            else if (event.key === "Home") move(0);
            else if (event.key === "End") move(items.length - 1);
            else if (event.key === "Tab") closeMenu();
        }

        function onDocumentKey(event) {
            if (event.key === "Escape" && panel) {
                event.stopPropagation();
                closeMenu({ restoreFocus: true });
            }
        }

        function onClickAway(event) {
            if (!panel) return;
            if (!panel.contains(event.target) && !root.contains(event.target)) closeMenu();
        }

        /** Under the icon, its right edge on the icon's, inside the window. */
        function position() {
            if (!panel) return;
            const box = root.getBoundingClientRect();
            const width = panel.offsetWidth || 260;
            const right = Math.round(Math.min(Math.max(8, window.innerWidth - box.right), window.innerWidth - width - 8));
            panel.style.top = `${Math.round(box.bottom + 8)}px`;
            panel.style.right = `${right}px`;
            // The panel grows from the icon's centre, even when it is clamped.
            const left = window.innerWidth - right - width;
            const origin = Math.max(12, Math.min(width - 12, box.left + box.width / 2 - left));
            panel.style.setProperty("--feedback-origin-x", `${Math.round(origin)}px`);
        }

        root.addEventListener("click", toggle);
        root.addEventListener("keydown", onRootKey);
        return null;
    }

    return { mount };
})();

PlexoraPage.register(() => {
    window.PlexoraFeedbackMenu.mount(document.getElementById("feedback_button"));
    return null;
});
