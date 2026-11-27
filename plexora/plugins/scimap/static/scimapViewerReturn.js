// On every viewer page: the SCIMAP step being looked at, and the way back.
//
// A step's TV button records an overlay on the server (server/overlay.py) and
// opens the viewer. This script, loaded with every viewer page, asks for that
// overlay; when it is about the project on screen it colours the cells by it
// (Cell Explorer's colour-by, the annotation `scimap`) and floats a card with
// the way back to the notebook.
//
// Walking a dataset image by image is a full page load each time, so this
// runs again on the next image and the overlay follows -- until the user
// dismisses it here or views another step. It is its own card, not a
// PlexoraToast: a dataset walk shows a toast of its own, which would replace
// ours.
(function () {
    "use strict";
    if (typeof window === "undefined" || window.__scimapReturn) return;
    window.__scimapReturn = true;

    const RETURN_KEY = "plexora:scimap-return";
    const ANNOTATION = "scimap";

    function url(path) {
        return typeof window.plexoraUrl === "function" ? window.plexoraUrl(path) : path;
    }

    function project() {
        return (window.flaskVariables && window.flaskVariables.datasource) ||
            (document.body && document.body.dataset && document.body.dataset.plexoraDatasource) || "";
    }

    function whenReady(timeout) {
        const deadline = Date.now() + (timeout || 30000);
        return new Promise((resolve) => {
            (function check() {
                if (window.__plexoraReady && window.PlexoraAgentBridge) return resolve(true);
                if (Date.now() > deadline) return resolve(false);
                setTimeout(check, 150);
            })();
        });
    }

    async function colour(attempts) {
        for (let i = 0; i < attempts; i += 1) {
            try {
                await window.PlexoraAgentBridge.run("set_color_by", { column: ANNOTATION });
                return true;
            } catch (err) {
                await new Promise((r) => setTimeout(r, 500));
            }
        }
        return false;
    }

    function backHref(overlay) {
        try {
            const note = JSON.parse(window.sessionStorage.getItem(RETURN_KEY) || "null");
            if (note && note.notebook_id === overlay.notebook_id && note.href) return note.href;
        } catch (err) { /* fall through */ }
        return url("/plugins/scimap/notebook/" + encodeURIComponent(overlay.notebook_id));
    }

    const ICON = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2.5" y="7" width="19" height="13" rx="2"/>' +
        '<path d="m8 3 4 4 4-4"/></svg>';
    const BACK = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 12H5M11 6l-6 6 6 6"/></svg>';
    const CLOSE = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>';

    // The viewer's own liquid glass (viewer.css `.plx-view-back`, the
    // bookmark tray): the tissue blurred through a dark pill.
    const STYLE = `
#sm_viewer_return{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:950;display:flex;
 align-items:center;gap:10px;height:40px;padding:0 5px 0 14px;box-sizing:border-box;max-width:calc(100vw - 32px);
 border-radius:999px;color:var(--text-primary,#eef1f6);font:13px 'Source Sans Pro','Source Sans 3',system-ui,sans-serif;
 -webkit-backdrop-filter:blur(18px) saturate(170%) brightness(1.05);backdrop-filter:blur(18px) saturate(170%) brightness(1.05);
 background:linear-gradient(180deg,rgba(34,40,52,.66),rgba(12,16,22,.62) 70%);border:1px solid rgba(255,255,255,.16);
 box-shadow:inset 0 1px 0 rgba(255,255,255,.16),inset 0 -1px 0 rgba(255,255,255,.03),0 8px 24px rgba(0,0,0,.32);
 animation:sm-return-in var(--duration-fast,140ms) var(--ease-standard,cubic-bezier(.2,0,0,1))}
#sm_viewer_return.is-leaving{animation:sm-return-out 120ms var(--ease-standard,cubic-bezier(.2,0,0,1)) forwards}
#sm_viewer_return .sm-r-text{display:flex;align-items:center;gap:8px;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#sm_viewer_return .sm-r-text svg{flex:none;color:var(--text-secondary,#aab3c2)}
#sm_viewer_return .sm-r-title{font-weight:600}
#sm_viewer_return .sm-r-rest{color:var(--text-secondary,#aab3c2);overflow:hidden;text-overflow:ellipsis}
#sm_viewer_return button{font:inherit;cursor:pointer;flex:none;display:inline-flex;align-items:center;justify-content:center;
 color:inherit;transition:background 120ms,border-color 120ms,transform 120ms}
#sm_viewer_return .sm-r-back{height:30px;gap:6px;padding:0 12px;border-radius:999px;font-size:12px;font-weight:600;
 background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.2)}
#sm_viewer_return .sm-r-back:hover{background:rgba(255,255,255,.18);border-color:rgba(255,255,255,.3)}
#sm_viewer_return .sm-r-back:active{transform:scale(.98)}
#sm_viewer_return .sm-r-close{width:30px;height:30px;border-radius:999px;border:0;background:transparent;color:var(--text-secondary,#aab3c2)}
#sm_viewer_return .sm-r-close:hover{background:rgba(255,255,255,.1);color:var(--text-primary,#eef1f6)}
#sm_viewer_return button:focus-visible{outline:2px solid var(--accent-channel,#38bdf8);outline-offset:2px}
@keyframes sm-return-in{from{opacity:0;transform:translate(-50%,6px)}to{opacity:1;transform:translate(-50%,0)}}
@keyframes sm-return-out{to{opacity:0;transform:translate(-50%,6px)}}
@media (prefers-reduced-motion:reduce){#sm_viewer_return,#sm_viewer_return.is-leaving{animation:none}}`;

    function style() {
        if (document.getElementById("sm_viewer_return_style")) return;
        const node = document.createElement("style");
        node.id = "sm_viewer_return_style";
        node.textContent = STYLE;
        document.head.appendChild(node);
    }

    function remove(animate) {
        const node = document.getElementById("sm_viewer_return");
        if (!node) return;
        if (!animate) { node.remove(); return; }
        node.classList.add("is-leaving");
        setTimeout(() => node.remove(), 130);
    }

    function card(overlay, coloured) {
        remove(false);
        style();
        const node = document.createElement("div");
        node.id = "sm_viewer_return";
        node.setAttribute("role", "status");
        const text = document.createElement("span");
        text.className = "sm-r-text";
        text.innerHTML = ICON;
        const strong = document.createElement("span");
        strong.className = "sm-r-title";
        strong.textContent = overlay.title || "SCIMAP";
        const rest = document.createElement("span");
        rest.className = "sm-r-rest";
        const where = overlay.context && overlay.context.kind === "dataset"
            ? " · on every image of " + overlay.context.name : "";
        rest.textContent = (coloured ? "" : " · open Cell Explorer and color by SCIMAP") + where;
        text.append(strong, rest);

        const back = document.createElement("button");
        back.type = "button";
        back.className = "sm-r-back";
        back.innerHTML = BACK + "<span>Back to analysis</span>";
        back.addEventListener("click", () => {
            const href = backHref(overlay);
            // Gone at once: the viewer stays alive behind the notebook page,
            // and this card is the viewer's, not the notebook's.
            remove(false);
            if (window.PlexoraRouter && typeof window.PlexoraRouter.go === "function") window.PlexoraRouter.go(href);
            else window.location.href = href;
        });

        const close = document.createElement("button");
        close.type = "button";
        close.className = "sm-r-close";
        close.setAttribute("aria-label", "Stop showing the SCIMAP result");
        close.title = "Stop showing the SCIMAP result";
        close.innerHTML = CLOSE;
        close.addEventListener("click", async () => {
            remove(true);
            try {
                await fetch(url("/plugins/scimap/api/overlay"), { method: "DELETE" });
            } catch (err) { /* it will be offered again next load */ }
        });
        node.append(text, back, close);
        document.body.appendChild(node);
    }

    async function arrive() {
        const here = project();
        if (!here) return;
        let overlay = null;
        try {
            const response = await fetch(url("/plugins/scimap/api/overlay"));
            if (!response.ok) return;
            overlay = (await response.json()).overlay;
        } catch (err) {
            return;
        }
        if (!overlay || (overlay.projects || []).indexOf(here) < 0) return;
        const ready = await whenReady(30000);
        const coloured = ready && overlay.column !== null ? await colour(8) : false;
        card(overlay, coloured || overlay.kind === "selection" ? coloured : false);
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", arrive);
    else arrive();
    // The router keeps the viewer alive (hidden) behind other pages: the card
    // leaves with it and comes back with it.
    window.addEventListener("plexora:viewer-hidden", () => remove(false));
    window.addEventListener("plexora:viewer-shown", () => {
        window.__scimapReturnShown = (window.__scimapReturnShown || 0) + 1;
        arrive();
    });
})();
