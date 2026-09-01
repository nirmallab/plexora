/**
 * PlexoraBrightfieldAdjust -- brightness, contrast and gamma for an H&E slide.
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
 * Deliberately not persisted. A saved gamma is a claim about the slide that
 * outlives the reason it was made, and the honest place to fix a scan that is
 * too dark is the scan.
 */
window.PlexoraBrightfieldAdjust = (function () {
    "use strict";

    //: The identity setting. Also what Reset restores, and the state in which
    //: the filter is dropped entirely rather than set to a no-op chain.
    const NEUTRAL = { brightness: 1, contrast: 1, gamma: 1 };

    const FILTER_ID = "plexora-gamma";

    const CONTROLS = [
        { key: "brightness", input: "adjust_brightness", output: "adjust_brightness_value" },
        { key: "contrast", input: "adjust_contrast", output: "adjust_contrast_value" },
        { key: "gamma", input: "adjust_gamma", output: "adjust_gamma_value" },
    ];

    let state = { ...NEUTRAL };
    let canvas = null;
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
        return CONTROLS.every(({ key }) => Math.abs(state[key] - NEUTRAL[key]) < 0.001);
    }

    function apply() {
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

    function paintReadouts() {
        for (const { key, output } of CONTROLS) {
            const element = document.getElementById(output);
            if (element) element.textContent = state[key].toFixed(2);
        }
    }

    function set(key, value) {
        const parsed = Number(value);
        if (!Number.isFinite(parsed)) return;
        state[key] = parsed;
        paintReadouts();
        apply();
    }

    function reset() {
        state = { ...NEUTRAL };
        for (const { key, input } of CONTROLS) {
            const element = document.getElementById(input);
            if (element) element.value = String(NEUTRAL[key]);
        }
        paintReadouts();
        apply();
    }

    /**
     * Wire the sliders to `imageViewer`'s drawer canvas.
     *
     * A no-op when the section is not on the page, which is every project that
     * is not brightfield -- so main.js can call this without asking twice.
     */
    function init(imageViewer) {
        const section = document.getElementById("image_adjust_section");
        if (!section) return;
        canvas = imageViewer?.viewer?.drawer?.canvas || null;
        if (!canvas) return;

        for (const { key, input } of CONTROLS) {
            const element = document.getElementById(input);
            if (!element) continue;
            element.addEventListener("input", () => set(key, element.value));
        }
        document.getElementById("image_adjust_reset")?.addEventListener("click", reset);

        // Same fold behaviour as the channel section it sits where. Handled
        // here rather than in viewerSidebar because this section only exists
        // for one kind of project and the sidebar has no other reason to know
        // about it.
        const toggle = document.getElementById("image_adjust_collapse");
        toggle?.addEventListener("click", () => {
            const collapsed = !section.classList.contains("is-collapsed");
            section.classList.toggle("is-collapsed", collapsed);
            toggle.setAttribute("aria-expanded", String(!collapsed));
        });

        paintReadouts();
    }

    return { init, reset, get state() { return { ...state }; } };
})();
