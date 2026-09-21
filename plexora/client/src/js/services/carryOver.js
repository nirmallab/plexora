/**
 * carryOver.js - what survives a walk from one sample to the next.
 *
 * The Prev/Next controls on the canvas (views/datasetNav.js) move between the
 * samples of a dataset. Moving is a FULL PAGE NAVIGATION and cannot be
 * anything else: the server holds one loaded datasource
 * (data_model._loaded_source), ImageViewer has no destroy path, and main.js
 * has document-scoped top-level bindings that can only run once. See
 * services/appRouter.js, which fences cross-project links off for exactly
 * these reasons.
 *
 * So the arrangement is carried rather than kept:
 *
 *     capture() on the way out  ->  sessionStorage  ->  take() on the way in
 *
 * sessionStorage because it is per-tab and survives a navigation, and because
 * two tabs walking two datasets must not hand each other their channels.
 *
 * WHAT IS CARRIED, and the rule behind it: an ARRANGEMENT travels, a
 * MEASUREMENT does not. Which channels are on, what colour each is, which
 * tools are open, which marker is being gated -- those are things the user
 * chose and would choose again. A contrast window, a gate threshold, a
 * viewport: those are readings taken off THIS image, and the next image has
 * its own. The next sample's own saved values are loaded for those, by the
 * same per-project restore that runs on any ordinary open.
 *
 * COMPONENT-WISE AND FAULT TOLERANT. Every capture and every restore is
 * wrapped on its own. A sample whose clustering column is missing still gets
 * its channels; a tool that will not open on this sample does not stop the
 * layers being restored. What could not be carried is collected here and said
 * ONCE, in one notice, rather than as a queue of them -- see flush().
 *
 * NOTHING HERE IS PERSISTED to the new sample. Carried channels are applied
 * through the same path that a notebook's launch state uses
 * (ViewerSidebar.applyLaunchChannels), which is deliberately not written back
 * to the project: walking a dataset must not quietly rewrite every sample's
 * saved arrangement to match the one you started from. The user's first real
 * edit saves as it always did.
 */
window.PlexoraCarryOver = (function () {
    "use strict";

    //: One key, so a second walk replaces the first rather than queueing.
    const KEY = "plexora:carry-over";

    //: Bumped when the snapshot shape changes. An older snapshot left in a tab
    //: by a previous build is dropped rather than half-read.
    const VERSION = 1;

    //: How long a stashed snapshot stays good. Long enough for a slow sample
    //: to open, short enough that a tab left overnight and then reloaded does
    //: not suddenly apply an arrangement from another sitting. The navigation
    //: it belongs to is always seconds away.
    const MAX_AGE_MS = 120000;

    //: How many lines the notice shows before it summarises the rest. A list
    //: longer than this is not read, it is dismissed.
    const MAX_LINES = 6;

    //: The snapshot this page load consumed, or null. Read by every restore
    //: point; set once, by take().
    let taken = null;

    //: What each component managed. `skipped` is what to tell the user;
    //: `appliedAny` is how "partial" is told from "nothing in common".
    let outcome = { skipped: [], appliedAny: false };

    //: One notice per page load, however many restore points report.
    let flushed = false;

    function storage() {
        // Wrapped: sessionStorage throws rather than returning null in a
        // private window with site data blocked, and a viewer that cannot
        // navigate is a worse outcome than one that cannot carry state.
        try {
            return window.sessionStorage || null;
        } catch (error) {
            return null;
        }
    }

    /** Run `fn`, and treat a throw as "this component contributed nothing". */
    function attempt(what, fn) {
        try {
            return fn();
        } catch (error) {
            console.error("carryOver: capturing " + what + " failed", error);
            return null;
        }
    }

    // -- capture ---------------------------------------------------------

    /** Which channels are on, and what colour each is drawn. */
    function captureChannels() {
        const sidebar = window.__plexora && window.__plexora.viewerSidebar;
        if (!sidebar || !Array.isArray(sidebar.channelSlots)) return null;
        const entries = sidebar.channelSlots
            .filter((slot) => slot && slot.enabled && slot.name)
            .map((slot) => ({ name: slot.name, color: slot.colorHex }));
        if (!entries.length) return null;
        // Deliberately no range. A contrast window is a reading off this
        // image's pixels; the next sample's own saved window is used where it
        // has one, and where it does not the channel auto-levels against its
        // own data. Carrying the number would make every sample look like the
        // first one rather than like itself.
        const hd = document.getElementById("viewer_controls_hd");
        return { entries, hd: Boolean(hd && hd.checked) };
    }

    /** How cells are drawn, and how strongly. */
    function captureCells() {
        const controls = window.__plexora && window.__plexora.viewerControls;
        if (!controls || !controls.mode) return null;
        const size = document.getElementById("cell_point_size");
        const fade = document.getElementById("cell_layer_opacity");
        const pointSize = size ? Number(size.value) : NaN;
        const opacity = fade ? Number(fade.value) : NaN;
        return {
            mode: controls.mode,
            pointSize: Number.isFinite(pointSize) ? pointSize : null,
            opacity: Number.isFinite(opacity) ? opacity : null,
        };
    }

    /**
     * Which layers are drawn and how strongly.
     *
     * The mask and the centroids are left out: they are core's own surfaces,
     * driven by the Cells control captured above, and carrying both would be
     * two things setting one state.
     */
    function captureLayers() {
        const stack = window.__plexora && window.__plexora.layers;
        if (!stack || typeof stack.layers !== "function") return null;
        const rows = stack.layers()
            .filter((layer) => layer && layer.id !== "__mask__" && layer.id !== "__centroids__")
            .map((layer) => ({
                id: layer.id,
                modality: (layer.spec && layer.spec.modality) || null,
                kind: layer.kind,
                visible: layer.visible,
                opacity: layer.opacity,
            }));
        return rows.length ? rows : null;
    }

    /**
     * Which tools are open, how each is arranged, and what each one is doing.
     *
     * The arrangement is toolLoader's (open, drawn, folded, pinned); what the
     * tool is DOING is the plugin's own answer, through the optional
     * captureCarryState() hook. Core never names a plugin here.
     */
    function captureTools() {
        const loader = window.PlexoraToolLoader;
        if (!loader || typeof loader.snapshot !== "function") return null;
        const snapshot = loader.snapshot();
        if (!snapshot || !snapshot.loaded || !snapshot.loaded.length) return null;
        snapshot.loaded.forEach((entry) => {
            const record = window.__plexora
                && window.__plexora.plugins
                && window.__plexora.plugins.get(entry.name);
            const controller = record && record.sidebarController;
            if (!controller || !controller.captureCarryState) return;
            const state = attempt('plugin "' + entry.name + '"',
                () => controller.captureCarryState());
            if (state) entry.state = state;
        });
        return snapshot;
    }

    /**
     * What each LAYER SECTION is doing -- Transcripts and anything like it.
     *
     * A layer section is not a tool and toolLoader has never heard of it: it is
     * part of what the viewer IS for a sample that has the data, so it is
     * mounted on every view of that sample with no menu row and no close
     * button. Which means there is no "is it open" to carry -- it is open if
     * this sample has the data -- and only the plugin's own answer to carry.
     *
     * Same hook as a tool's, so a plugin that is both (or that changes from one
     * to the other) needs nothing different.
     */
    function captureSections() {
        const sections = window.PlexoraLayerSections;
        if (!sections || typeof sections.names !== "function") return null;
        const captured = {};
        sections.names().forEach((name) => {
            const record = window.__plexora
                && window.__plexora.plugins
                && window.__plexora.plugins.get(name);
            const controller = record && record.sidebarController;
            if (!controller || !controller.captureCarryState) return;
            const state = attempt('layer section "' + name + '"',
                () => controller.captureCarryState());
            if (state) captured[name] = state;
        });
        return Object.keys(captured).length ? captured : null;
    }

    /**
     * Everything worth carrying, as one plain object.
     *
     * Each component is captured on its own, so a panel mid-rebuild costs the
     * others nothing.
     */
    function capture() {
        const components = {};
        const channels = attempt("channels", captureChannels);
        if (channels) components.channels = channels;
        const cells = attempt("cells", captureCells);
        if (cells) components.cells = cells;
        const layers = attempt("layers", captureLayers);
        if (layers) components.layers = layers;
        const tools = attempt("tools", captureTools);
        if (tools) components.tools = tools;
        const sections = attempt("layer sections", captureSections);
        if (sections) components.sections = sections;
        return components;
    }

    // -- hand-off --------------------------------------------------------

    /**
     * Put this page's arrangement where the next page will look for it.
     *
     * @param to the sample being opened. Checked on the way out again, so a
     *   snapshot cannot be applied to a sample it was not meant for -- a
     *   navigation that is cancelled, or a link followed elsewhere, leaves the
     *   entry behind and the next unrelated open must ignore it.
     */
    function stash(to) {
        const store = storage();
        if (!store || !to) return false;
        try {
            store.setItem(KEY, JSON.stringify({
                version: VERSION,
                from: (window.flaskVariables && window.flaskVariables.datasource) || "",
                to,
                at: Date.now(),
                components: capture(),
            }));
            return true;
        } catch (error) {
            console.error("carryOver: could not stash", error);
            return false;
        }
    }

    /**
     * The snapshot meant for THIS sample, consumed.
     *
     * Deleted in the same call it is read in, which is what makes a reload a
     * fresh open: the arrangement is carried by the act of navigating, once,
     * and a page the user refreshes is a page they are looking at rather than
     * one they just arrived on.
     */
    function take(datasource) {
        const store = storage();
        if (!store) return null;
        let raw = null;
        try {
            raw = store.getItem(KEY);
            if (raw !== null) store.removeItem(KEY);
        } catch (error) {
            return null;
        }
        if (!raw) return null;
        let payload = null;
        try {
            payload = JSON.parse(raw);
        } catch (error) {
            return null;
        }
        if (!payload || payload.version !== VERSION) return null;
        if (!datasource || payload.to !== datasource) return null;
        if (!(Date.now() - Number(payload.at) < MAX_AGE_MS)) return null;
        taken = payload;
        return payload;
    }

    /** What take() consumed on this page load, for the restore points. */
    function current() {
        return taken;
    }

    // -- reporting -------------------------------------------------------

    /** Something did carry across. See flush() for what this decides. */
    function applied() {
        outcome.appliedAny = true;
    }

    /**
     * This component could not carry part of what it was handed.
     *
     * Collected rather than shown: several panels each saying one sentence is
     * a stack of notices about one navigation. flush() says it once.
     */
    function report(component, skipped) {
        const lines = (Array.isArray(skipped) ? skipped : [skipped])
            .filter((line) => typeof line === "string" && line.trim());
        lines.forEach((line) => {
            if (outcome.skipped.indexOf(line) === -1) outcome.skipped.push(line);
        });
    }

    /**
     * Say, once, what did not come across -- or say nothing at all.
     *
     * Three outcomes, and only two of them are worth a word:
     *   everything applied -> silence. Nothing happened that the user did not
     *                         ask for and would not expect.
     *   some of it applied -> name what did not, so a panel that looks
     *                         different is explained rather than odd.
     *   none of it applied -> say the sample opened fresh, so an arrangement
     *                         that vanished reads as "these two samples have
     *                         nothing in common" rather than as a bug.
     *
     * Silent while the image itself failed to load: a notice about a channel
     * that could not be restored, over a canvas that is explaining it has no
     * picture, is the smaller of two problems shouting over the larger.
     */
    function flush() {
        if (flushed || !taken) return null;
        flushed = true;
        const errorState = window.PlexoraViewerError;
        if (errorState && errorState.isShowing && errorState.isShowing()) return null;
        if (!outcome.skipped.length) return null;

        const from = taken.from || "the previous sample";
        const lines = outcome.skipped.slice(0, MAX_LINES);
        const rest = outcome.skipped.length - lines.length;
        if (rest > 0) lines.push("and " + rest + " more");

        if (!window.PlexoraToast) return null;
        if (outcome.appliedAny) {
            return window.PlexoraToast.show({
                title: "Carried over from " + from,
                note: "Not available on this sample:",
                lines,
            });
        }
        return window.PlexoraToast.show({
            title: "Opened fresh",
            note: "Nothing carried over from " + from
                + " — these samples have no channels, layers or tool inputs in common.",
            lines: [],
        });
    }

    /** Test seam: forget everything this module is holding. */
    function reset() {
        taken = null;
        outcome = { skipped: [], appliedAny: false };
        flushed = false;
    }

    return {
        capture, stash, take, current,
        applied, report, flush, reset,
        KEY, VERSION, MAX_AGE_MS,
    };
})();
