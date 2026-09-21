/**
 * PlexoraBrightfieldAdjust -- brightness, contrast, gamma and opacity for an
 * H&E slide.
 *
 * What a brightfield image has instead of a contrast slider. A fluorescence
 * channel's slider moves a quantization window: the server sends 8 bits chosen
 * out of 16, and the shader re-maps them, so changing it changes what the
 * pixels mean. There is no equivalent here -- the tile bytes ARE the colour the
 * scanner recorded, and every one of the three samples is already 8 bits. So
 * this is a display filter over the drawn canvas and nothing else: no refetch,
 * no re-decode, no second copy of the picture.
 *
 * Applied to OpenSeadragon's drawer canvas specifically, not to the container.
 * The cell outlines, centroids and ROI overlays are drawn on a SEPARATE canvas
 * stacked above it (CanvasOverlayHd), and filtering the container would push
 * the annotations through the same gamma curve as the tissue -- which would
 * quietly change what a mask's colour means while the user was adjusting the
 * slide behind it.
 *
 * Gamma needs an SVG filter because CSS has no gamma function; the two CSS
 * functions and the one SVG filter compose in a single `filter` declaration.
 * `color-interpolation-filters="sRGB"` on the SVG is load-bearing: the default
 * is linearRGB, which would shift the hue of every stain as a side effect of
 * changing its brightness.
 *
 * OPACITY is the exception to all of the above and is deliberately NOT part of
 * that filter chain. The mask is a TiledImage in the same OpenSeadragon world
 * as the slide, so it is drawn onto the same drawer canvas -- a `filter:
 * opacity()` there would fade the cell layer along with the tissue, which is
 * the opposite of what dimming a slide is for. OSD composites per item, so this
 * one goes to the LAYER STACK as the base layer's opacity, and ViewerManager
 * sets it on the slide's own TiledImage while everything drawn over it stays at
 * full strength. The stack rather than the item directly, so that this slider
 * and the base layer's card in the Layers list are one control and not two.
 * (The three filters above do reach the mask, which is a separate and much
 * smaller wrongness: they tint what is drawn over the slide rather than hiding
 * it.)
 *
 * Deliberately not persisted. A saved gamma is a claim about the slide that
 * outlives the reason it was made, and the honest place to fix a scan that is
 * too dark is the scan.
 */
window.PlexoraBrightfieldAdjust = (function () {
    "use strict";

    //: The identity setting. Also what Reset restores, and the state in which
    //: the filter is dropped entirely rather than set to a no-op chain.
    const NEUTRAL = { brightness: 1, contrast: 1, gamma: 1, opacity: 1 };

    const FILTER_ID = "plexora-gamma";

    const CONTROLS = [
        { key: "brightness", input: "adjust_brightness", field: "adjust_brightness_value" },
        { key: "contrast", input: "adjust_contrast", field: "adjust_contrast_value" },
        { key: "gamma", input: "adjust_gamma", field: "adjust_gamma_value" },
        { key: "opacity", input: "adjust_opacity", field: "adjust_opacity_value" },
    ];

    //: The subset of CONTROLS that composes into the canvas `filter`. Opacity
    //: is not one of them -- see the note at the top of this file -- so it must
    //: not decide whether that filter is worth declaring either.
    const FILTER_KEYS = ["brightness", "contrast", "gamma"];

    let state = { ...NEUTRAL };
    //: key -> the PlexoraSlider wrapped around that row's staged input. Built
    //: in `init`, which is also the only place that knows the page has one.
    let sliders = {};
    let canvas = null;
    let viewer = null;
    let gammaFuncs = [];

    /** The hidden SVG that owns the gamma transfer function.
     *
     *  One filter, mutated in place: a `filter: url(#id)` reference is live, so
     *  changing the exponent on the existing element repaints without the
     *  browser rebuilding the filter graph, which a new element per drag would.
     */
    function ensureGammaFilter() {
        if (gammaFuncs.length) return;
        const NS = "http://www.w3.org/2000/svg";
        const svg = document.createElementNS(NS, "svg");
        svg.setAttribute("width", "0");
        svg.setAttribute("height", "0");
        svg.setAttribute("aria-hidden", "true");
        svg.style.position = "absolute";
        const filter = document.createElementNS(NS, "filter");
        filter.setAttribute("id", FILTER_ID);
        filter.setAttribute("color-interpolation-filters", "sRGB");
        const transfer = document.createElementNS(NS, "feComponentTransfer");
        for (const name of ["feFuncR", "feFuncG", "feFuncB"]) {
            const func = document.createElementNS(NS, name);
            func.setAttribute("type", "gamma");
            func.setAttribute("amplitude", "1");
            func.setAttribute("offset", "0");
            func.setAttribute("exponent", "1");
            transfer.appendChild(func);
            gammaFuncs.push(func);
        }
        filter.appendChild(transfer);
        svg.appendChild(filter);
        document.body.appendChild(svg);
    }

    function isNeutral() {
        return FILTER_KEYS.every((key) => Math.abs(state[key] - NEUTRAL[key]) < 0.001);
    }

    /**
     * Hand the opacity to the layer stack, which owns it now.
     *
     * It used to be set straight onto the slide's TiledImage, which is why
     * this file needed an `add-item` handler: `rebuildTileLayers` drops that
     * item on a routing repair and the replacement arrived at full strength,
     * so the slider quietly stopped describing what was on screen. The stack
     * holds the number instead, ViewerManager applies it, and every path that
     * adds the base image reads it back on the way in -- so there is nothing
     * left to catch up after the fact. This is also what makes this slider and
     * the base layer card the same control rather than two.
     */
    function pushOpacity() {
        const stack = window.__plexora && window.__plexora.layers;
        const id = window.PlexoraLayerStack && window.PlexoraLayerStack.REFERENCE_LAYER_ID;
        if (!stack || !id) return;
        stack.setOpacity(id, state.opacity);
    }

    function apply() {
        pushOpacity();
        if (!canvas) return;
        if (isNeutral()) {
            canvas.style.removeProperty("--brightfield-filter");
            canvas.classList.remove("brightfield-adjusted");
            return;
        }
        ensureGammaFilter();
        // The SVG filter is inverted against the slider: an exponent below 1
        // brightens midtones, and a "more gamma" slider that darkened the image
        // would read backwards to anyone who has used one before.
        const exponent = 1 / state.gamma;
        for (const func of gammaFuncs) func.setAttribute("exponent", String(exponent));
        canvas.classList.add("brightfield-adjusted");
        canvas.style.setProperty(
            "--brightfield-filter",
            `brightness(${state.brightness}) contrast(${state.contrast}) url(#${FILTER_ID})`);
    }

    /** Put the controls back in step with `state`, silently: this is called
     *  where the app moved the value, and a slider that told us about it
     *  would be telling us what we had just told it. */
    function paintControls() {
        for (const { key } of CONTROLS) sliders[key]?.set(state[key], { silent: true });
    }

    function set(key, value) {
        const parsed = Number(value);
        if (!Number.isFinite(parsed)) return;
        state[key] = parsed;
        apply();
    }

    function reset() {
        state = { ...NEUTRAL };
        paintControls();
        apply();
    }

    /**
     * Wire the sliders to `imageViewer`'s drawer canvas.
     *
     * A no-op when the section is not on the page, which is every project that
     * is not brightfield -- so main.js can call this without asking twice.
     */
    function init(imageViewer) {
        // Gated on a slider rather than on a section: these controls are the
        // base image layer's card body now, and the card is built by
        // layerManager.js from the stack -- there is no wrapper of this
        // module's own to look for. The sliders are still exactly the markup
        // index.html stages, with exactly these ids.
        if (!document.getElementById("adjust_opacity")) return;
        viewer = imageViewer?.viewer || null;
        canvas = viewer?.drawer?.canvas || null;
        if (!canvas) return;

        for (const { key, input, field } of CONTROLS) {
            const element = document.getElementById(input);
            if (!element) continue;
            // The <output> these four had is the slider's own number box now,
            // keeping the id it was staged with -- and typeable, which is what
            // "set the gamma to 1.4" always wanted and could only approach by
            // dragging. `onInput` only: a filter is a CSS string and a
            // composite, so a tick costs nothing and nothing is persisted.
            sliders[key] = new PlexoraSlider(element, {
                decimals: 2, fieldId: field,
                onInput: (value) => set(key, value),
            });
        }
        document.getElementById("image_adjust_reset")?.addEventListener("click", reset);

        // The fold is the card's chevron now, and the card owns it for every
        // layer alike -- a section that folded differently because of what was
        // inside it is the drift one card list exists to prevent.
    }

    return { init, reset, get state() { return { ...state }; } };
})();
