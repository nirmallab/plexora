/**
 * canvasHint.js -- one quiet key hint at the bottom of the image.
 *
 * While a drawing tool owns a drag (the ROI tool, QC's pen and magic select),
 * a drag draws instead of panning, and the way to move the image is to hold
 * Space. That is said here, once, as text on the image: a key cap and a few
 * words in muted text centred along the bottom edge, under the chat composer.
 * The same object as the caption's T hint (`.viewer-overlay-hint`, built by
 * imageViewer.js), so the page has one way of naming a key.
 *
 * One hint for the page, owned by whichever tool showed it last; `hide` from
 * anybody else is ignored, so a tool switching off never takes away the hint
 * another tool has just put up.
 *
 *   PlexoraCanvasHint.show(owner, {key: "Space", text: "Hold to pan"})
 *   PlexoraCanvasHint.hide(owner)
 *
 * Core, so both plugins share it. Never takes the pointer.
 */
(function (global) {
    "use strict";

    let hint = null;     // {root, key, text, owner}

    function host() {
        return document.getElementById("openseadragon_wrapper") || null;
    }

    function build() {
        const root = document.createElement("div");
        root.className = "viewer-overlay-hint plx-canvas-hint";
        root.setAttribute("aria-hidden", "true");
        const key = document.createElement("kbd");
        key.className = "viewer-overlay-hint-key";
        const text = document.createElement("span");
        root.append(key, text);
        return { root, key, text, owner: null };
    }

    function show(owner, { key = "Space", text = "Hold to pan" } = {}) {
        const mount = host();
        if (!mount) return false;
        if (!hint) hint = build();
        hint.owner = owner;
        hint.key.textContent = key;
        hint.text.textContent = text;
        if (hint.root.parentNode !== mount) mount.appendChild(hint.root);
        hint.root.hidden = false;
        return true;
    }

    function hide(owner) {
        if (!hint || (owner !== undefined && hint.owner !== owner)) return;
        hint.root.hidden = true;
        hint.owner = null;
    }

    function isShown(owner) {
        return Boolean(hint && !hint.root.hidden && (owner === undefined || hint.owner === owner));
    }

    const api = { show, hide, isShown };
    global.PlexoraCanvasHint = api;
    if (typeof globalThis !== "undefined" && globalThis !== global) {
        globalThis.PlexoraCanvasHint = api;
    }
})(typeof window !== "undefined" ? window : globalThis);
