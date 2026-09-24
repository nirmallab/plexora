/**
 * viewTransformTools.js - core's Rotate and Flip tools.
 *
 * Registered exactly the way a plugin registers, so the card, the `?`, the
 * View-menu row, `?tool=` and the one-tool-at-a-time rule are toolLoader's and
 * cost nothing here. Two differences, both declared (see pluginRegistry.js):
 *
 *   lazy: true       this script is on every viewer page, so boot must not
 *                    set these up against a panel that is not there. They are
 *                    activated when opened, or at boot when `?tool=` staged the
 *                    panel.
 *   hasLayer: false  turning the view draws nothing, so the card has no eye.
 *
 * The controllers hold NO state. The orientation lives in the viewer's
 * PlexoraViewTransform service (services/viewTransform.js), which saves it
 * with the image; a controller paints from it and writes to it. Closing a card
 * therefore leaves the view as it is, and reopening one shows the live value,
 * with no fetchSaved/applyOrDefault round trip of its own. Several widgets for
 * one value cannot drift, because every one of them is repainted from the one
 * state on every change, whoever made it.
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

    function createRotateController(ctx) {
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

    function createFlipController(ctx) {
        let unsubscribe = null;
        let root = null;

        function paint(state) {
            if (!root) return;
            for (const button of root.querySelectorAll("[data-flip]")) {
                const on = !!state[button.dataset.flip];
                button.classList.toggle("is-active", on);
                button.setAttribute("aria-pressed", on ? "true" : "false");
            }
        }

        return {
            setup() {
                const service = serviceFrom(ctx);
                root = document.getElementById("flip_panel_section");
                if (!service || !root) return;
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
                summary: "Turns the whole view: every channel, the mask, the "
                    + "overlays and any registered layer together. Pick a right "
                    + "angle, drag the slider, or click the number and type one.\n\n"
                    + "Saved with this image, so it opens turned next time. "
                    + "Closing this card keeps the rotation.",
                notes: [
                    "With one flip on, a larger angle turns the picture the other "
                    + "way on screen, because the mirror reverses it.",
                    "Home fits the turned image into the view.",
                ],
            },
            createSidebarController: createRotateController,
        },
        {
            name: "flip",
            lazy: true,
            hasLayer: false,
            help: {
                summary: "Mirrors the view on the screen's own axes: horizontally "
                    + "swaps left and right as you see them, vertically swaps top "
                    + "and bottom, at any rotation.\n\n"
                    + "Saved with this image. Closing this card keeps the flip.",
                notes: ["Text drawn over the image mirrors with it."],
            },
            createSidebarController: createFlipController,
        },
    ];

    function register() {
        const registerPlugin = global.Plexora?.registerPlugin
            || ((definition) => global.Plexora?.plugins?.register(definition));
        for (const definition of DEFINITIONS) registerPlugin(definition);
    }

    register();

    global.PlexoraViewTransformTools = { DEFINITIONS, createRotateController, createFlipController };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = global.PlexoraViewTransformTools;
    }
})(typeof window !== "undefined" ? window : globalThis);
