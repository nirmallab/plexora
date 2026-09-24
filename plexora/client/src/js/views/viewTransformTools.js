/**
 * viewTransformTools.js - core's Rotate & Flip tool.
 *
 * ONE tool, one card, one View-menu row, for both: which way round the image is
 * shown is one question, and turning it and then mirroring it should not be an
 * open, a close and another open. The card holds the four right angles and the
 * two mirrors on one line, and the slider with its typed number under them.
 *
 * Registered exactly the way a plugin registers, so the card, the `?`, the
 * View-menu row, `?tool=` and the one-tool-at-a-time rule are toolLoader's and
 * cost nothing here. Two differences, both declared (see pluginRegistry.js):
 *
 *   lazy: true       this script is on every viewer page, so boot must not
 *                    set it up against a panel that is not there. It is
 *                    activated when opened, or at boot when `?tool=` staged the
 *                    panel.
 *   hasLayer: false  turning the view draws nothing, so the card has no eye.
 *
 * The controller holds NO state. The orientation lives in the viewer's
 * PlexoraViewTransform service (services/viewTransform.js), which saves it
 * with the image; the controller paints from it and writes to it. Closing the
 * card therefore leaves the view as it is, and reopening it shows the live
 * value, with no fetchSaved/applyOrDefault round trip of its own. Several
 * widgets for one value cannot drift, because every one of them is repainted
 * from the one state on every change, whoever made it.
 */
(function (global) {
    "use strict";

    function serviceFrom(ctx) {
        return ctx?.viewer?.viewTransform || null;
    }

    /** Degrees as the number box shows them: whole where whole, else 0.1. */
    function displayDegrees(degrees) {
        return Math.round(degrees * 10) / 10;
    }

    function createController(ctx) {
        let slider = null;
        let unsubscribe = null;
        let root = null;

        function paint(state) {
            if (!root) return;
            const degrees = state.degrees;
            for (const button of root.querySelectorAll("[data-rotate-to]")) {
                const on = Number(button.dataset.rotateTo) === degrees;
                button.classList.toggle("is-active", on);
                button.setAttribute("aria-checked", on ? "true" : "false");
            }
            // Silent: this is a readout of the state, not the user moving it.
            // Compared modulo a turn, so a thumb dragged to 360 stays at the
            // end of the track instead of jumping back to 0 under the hand.
            if (slider && displayDegrees(global.PlexoraViewTransform?.normalize?.(slider.get())
                    ?? slider.get()) !== displayDegrees(degrees)) {
                slider.set(displayDegrees(degrees), { silent: true });
            }
            const reset = root.querySelector("#rotate_reset_button");
            if (reset) reset.disabled = degrees === 0;
            for (const button of root.querySelectorAll("[data-flip]")) {
                const on = !!state[button.dataset.flip];
                button.classList.toggle("is-active", on);
                button.setAttribute("aria-pressed", on ? "true" : "false");
            }
        }

        return {
            setup() {
                const service = serviceFrom(ctx);
                root = document.getElementById("rotate_panel_section");
                if (!service || !root) return;
                const mount = root.querySelector("#rotate_slider");
                if (mount && typeof global.PlexoraSlider === "function") {
                    slider = new global.PlexoraSlider(mount, {
                        mode: "single",
                        min: 0,
                        max: 360,
                        step: 1,
                        unit: "°",
                        value: displayDegrees(service.get().degrees),
                        accent: "var(--accent-channel)",
                        ariaLabel: "Rotation in degrees",
                        // Per tick, straight onto the viewport: a drag has to
                        // follow the hand, and the service debounces the save.
                        // 360 is legal on the track and means a full turn,
                        // which the service stores as 0.
                        onInput: (value) => service.set({ degrees: value }, { immediately: true }),
                        onChange: (value) => service.set({ degrees: value }, { immediately: true }),
                    });
                }
                for (const button of root.querySelectorAll("[data-rotate-to]")) {
                    button.addEventListener("click", () => {
                        // Animated: a jump of a right angle is easier to follow
                        // as a turn than as a cut.
                        service.set({ degrees: Number(button.dataset.rotateTo) },
                            { immediately: false });
                    });
                }
                root.querySelector("#rotate_reset_button")
                    ?.addEventListener("click", () => service.reset());
                for (const button of root.querySelectorAll("[data-flip]")) {
                    button.addEventListener("click", () => {
                        const key = button.dataset.flip;
                        service.set({ [key]: !service.get()[key] });
                    });
                }
                unsubscribe = service.subscribe(paint);
                paint(service.get());
                ctx.onCleanup?.(() => {
                    unsubscribe?.();
                    unsubscribe = null;
                    slider?.destroy?.();
                    slider = null;
                    root = null;
                });
            },
        };
    }

    const DEFINITIONS = [
        {
            name: "rotate",
            lazy: true,
            hasLayer: false,
            help: {
                summary: "Turns and mirrors the whole view: every channel, the mask, "
                    + "the overlays and any registered layer together. Pick a right "
                    + "angle, drag the slider, or click the number and type one. The "
                    + "two buttons beside the angles mirror the view on the screen's "
                    + "own axes: left and right, or top and bottom, as you see them.\n\n"
                    + "Saved with this image, so it opens the same way next time. "
                    + "Closing this card keeps it.",
                notes: [
                    "With one mirror on, a larger angle turns the picture the other "
                    + "way on screen, because the mirror reverses it.",
                    "Home fits the turned image into the view.",
                    "Text drawn over the image mirrors with it.",
                ],
            },
            createSidebarController: createController,
        },
    ];

    function register() {
        const registerPlugin = global.Plexora?.registerPlugin
            || ((definition) => global.Plexora?.plugins?.register(definition));
        for (const definition of DEFINITIONS) registerPlugin(definition);
    }

    register();

    global.PlexoraViewTransformTools = { DEFINITIONS, createController };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraViewTransformTools;
    }
})(typeof window !== "undefined" ? window : globalThis);
