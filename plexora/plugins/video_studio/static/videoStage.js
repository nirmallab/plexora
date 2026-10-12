/**
 * videoStage.js - the viewer resized to the video for the length of an export.
 *
 * A frame is rendered by the viewer itself at the video's resolution, not
 * upscaled from whatever the window happens to be. OpenSeadragon draws its
 * canvas at the container's size times the device pixel ratio, so the stage
 * sets `#openseadragon_wrapper` to (output / devicePixelRatio) CSS pixels: a
 * 1920 x 1080 video on a 2x display is a 960 x 540 viewer whose canvas is
 * 1920 x 1080. The 3-D renderer is pinned to the output size directly
 * (`renderer.pinSize`). A 4K video on a 1x display is a 3840 px wide viewer,
 * clipped by its parent: it is drawn whole whether or not it is all on
 * screen, and the render cover is over it either way.
 *
 * Everything the viewer shows that is not the picture -- the dataset name,
 * the legend, the depth strip, plugin docks (`data-viewer-furniture`) -- is
 * hidden while the stage is up; the video draws its own overlays at output
 * resolution (videoOverlays.js). `exit()` puts every inline style back as it
 * was, whatever happened.
 *
 * Classic script: `window.PlexoraVideoStage`.
 */
(function (global) {
    "use strict";

    const FURNITURE = "[data-viewer-furniture], .viewer-volume-gizmo, .depth-strip, "
        + ".plx-canvas-safe > *";

    /** Make the viewer take its container's size now. */
    function resize(env) {
        try {
            env.iv?.viewer?.forceResize?.();
            env.iv?.viewer?.forceRedraw?.();
            global.dispatchEvent(new Event("resize"));
        } catch (error) {
            // the readiness barrier waits for the redraw anyway
        }
    }

    /** Two animation frames: long enough for a resize to be laid out and
     *  noticed by everything that measures the viewer. */
    function settleLayout() {
        return new Promise((resolve) => global.requestAnimationFrame(
            () => global.requestAnimationFrame(resolve)));
    }

    function enter(env, { width, height }) {
        const wrapper = env.wrapper || document.getElementById("openseadragon_wrapper");
        if (!wrapper) throw new Error("no viewer to render from");
        const dpr = global.devicePixelRatio || 1;
        const cssWidth = Math.ceil(width / dpr);
        const cssHeight = Math.ceil(height / dpr);
        const parent = wrapper.parentElement;
        const saved = {
            wrapper: wrapper.getAttribute("style"),
            parent: parent ? parent.getAttribute("style") : null,
            hidden: [],
        };
        if (parent) parent.style.overflow = "hidden";
        Object.assign(wrapper.style, {
            width: `${cssWidth}px`, height: `${cssHeight}px`, minWidth: `${cssWidth}px`,
            minHeight: `${cssHeight}px`, maxWidth: `${cssWidth}px`, maxHeight: `${cssHeight}px`,
            flex: "0 0 auto",
        });
        wrapper.classList.add("vs-staged");
        for (const node of wrapper.querySelectorAll(FURNITURE)) {
            if (node.closest(".vs-render-cover") || node.classList.contains("vs-render-cover")) continue;
            saved.hidden.push([node, node.style.visibility]);
            node.style.visibility = "hidden";
        }
        env.renderer?.pinSize?.({ width, height });
        // OpenSeadragon notices a container resize on its next update; told
        // now, so the first frame is fitted to the stage and not the window.
        resize(env);
        return {
            width, height, dpr, cssWidth, cssHeight, wrapper,
            /** Pin a renderer built after the stage went up (entering 3-D). */
            pinRenderer() {
                env.renderer?.pinSize?.({ width, height });
            },
            exit() {
                if (saved.wrapper === null) wrapper.removeAttribute("style");
                else wrapper.setAttribute("style", saved.wrapper);
                if (parent) {
                    if (saved.parent === null) parent.removeAttribute("style");
                    else parent.setAttribute("style", saved.parent);
                }
                wrapper.classList.remove("vs-staged");
                for (const [node, visibility] of saved.hidden) node.style.visibility = visibility;
                saved.hidden.length = 0;
                env.renderer?.pinSize?.(null);
                resize(env);
            },
        };
    }

    global.PlexoraVideoStage = { enter, resize, settleLayout };
})(typeof window !== "undefined" ? window : globalThis);
