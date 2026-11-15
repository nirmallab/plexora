/**
 * navMenuMotion.js -- the navbar's File, View, Tools and Help menus close the
 * way they open: back into their toggle, as the feedback menu does
 * (services/feedbackMenu.js).
 *
 * Opening is CSS alone (main.css, `#topBar .dropdown-menu.show`). Closing is
 * not, because Bootstrap hides a menu at once: on `hide.bs.dropdown` a copy of
 * the menu -- inert, its ids and shortcut attributes stripped, so nothing can
 * find or use it -- takes its place for the length of the closing animation and
 * is then removed. With reduced motion the duration is 0 and it goes at once.
 */
(function () {
    "use strict";

    document.addEventListener("hide.bs.dropdown", (event) => {
        const toggle = event.target;
        const menu = toggle && toggle.closest("#topBar .dropdown")?.querySelector(":scope > .dropdown-menu.show");
        if (!menu) return;
        const ghost = menu.cloneNode(true);
        ghost.classList.add("is-leaving");
        ghost.setAttribute("aria-hidden", "true");
        ghost.inert = true;
        ghost.removeAttribute("id");
        ghost.querySelectorAll("[id]").forEach((node) => node.removeAttribute("id"));
        ghost.querySelectorAll("[data-shortcut]").forEach((node) => node.removeAttribute("data-shortcut"));
        menu.after(ghost);
        // Gone when its animation has finished (the browser's promise); at
        // once when there is none to run.
        const running = typeof ghost.getAnimations === "function" ? ghost.getAnimations() : [];
        const done = () => ghost.remove();
        if (running.length) Promise.all(running.map((a) => a.finished)).then(done, done);
        else done();
    });
})();
