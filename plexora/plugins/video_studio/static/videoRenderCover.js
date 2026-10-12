/**
 * videoRenderCover.js - the transparent cover over the viewer while frames render.
 *
 * During an export the viewer is being driven frame by frame; a click, a wheel
 * or a key reaching it would move the camera between "applied" and
 * "captured" and put a frame in the video that the document never asked for.
 * The cover takes every pointer, wheel and key event for the wrapper's area
 * and says what is happening: "Rendering frame 120 of 240 · Esc stops", with a
 * 2 px progress line. Escape is the one key it lets through, to `onCancel`.
 *
 * Classic script: `window.PlexoraVideoRenderCover`.
 */
(function (global) {
    "use strict";

    function mount(wrapper, { onCancel } = {}) {
        const cover = document.createElement("div");
        cover.className = "vs-render-cover";
        cover.setAttribute("role", "status");
        cover.setAttribute("aria-live", "polite");
        cover.setAttribute("aria-busy", "true");
        cover.setAttribute("data-viewer-furniture", "");
        const banner = document.createElement("div");
        banner.className = "vs-render-banner";
        const text = document.createElement("span");
        text.className = "vs-render-text";
        text.textContent = "Preparing…";
        const stop = document.createElement("button");
        stop.type = "button";
        stop.className = "vs-chip";
        stop.textContent = "Stop";
        stop.addEventListener("click", (event) => {
            event.stopPropagation();
            onCancel?.();
        });
        const line = document.createElement("div");
        line.className = "vs-render-line";
        const fill = document.createElement("div");
        fill.className = "vs-render-fill";
        line.appendChild(fill);
        banner.append(text, stop);
        cover.append(banner, line);

        const swallow = (event) => {
            if (event.target === stop) return;
            event.preventDefault();
            event.stopPropagation();
        };
        for (const type of ["pointerdown", "pointermove", "pointerup", "click", "dblclick",
                            "wheel", "contextmenu", "touchstart", "touchmove"]) {
            cover.addEventListener(type, swallow, { passive: false });
        }
        const onKey = (event) => {
            if (event.key === "Escape") {
                event.preventDefault();
                event.stopImmediatePropagation();
                onCancel?.();
                return;
            }
            // Tab stays usable for reaching Stop; everything else would reach
            // a viewer shortcut (plane step, channel toggle) mid-render.
            if (event.key === "Tab") return;
            event.preventDefault();
            event.stopImmediatePropagation();
        };
        window.addEventListener("keydown", onKey, true);
        wrapper.appendChild(cover);

        return {
            element: cover,
            progress(done, total, label) {
                const fraction = total > 0 ? Math.min(1, done / total) : 0;
                fill.style.transform = `scaleX(${fraction})`;
                text.textContent = label || `Rendering frame ${Math.min(done + 1, total)} of ${total} · Esc stops`;
            },
            message(label) {
                text.textContent = label;
            },
            remove() {
                window.removeEventListener("keydown", onKey, true);
                cover.remove();
            },
        };
    }

    global.PlexoraVideoRenderCover = { mount };
})(typeof window !== "undefined" ? window : globalThis);
