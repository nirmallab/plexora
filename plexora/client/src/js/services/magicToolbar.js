/**
 * magicToolbar.js -- magic select's floating bar on the canvas.
 *
 * While magic select is on (in the ROI panel or the QC panel) a small glass
 * bar floats at the top centre of the image with the three ways to prompt it
 * and a way out:
 *
 *   Add      a click includes what is under it (a new outline, or more of
 *            the one being made)
 *   Remove   a click takes what is under it out of the outline
 *   Box      a drag boxes the object to outline (large or textured things)
 *   Scribble a line drawn over the object becomes points along it -- the
 *            way to say "all of this" for a long, patchy or uneven thing a
 *            single click reads as only its nearest part; Shift draws a line
 *            over what to leave out
 *   ×        magic select off; the bar goes with it
 *
 * In Add and Remove a drag still pans, so navigating never needs a mode
 * change; Shift held in Add removes for that click. The active mode is lit,
 * and the wand at the left breathes while an outline is on its way.
 *
 * One bar for the page, owned by whichever tool showed it last: the ROI and
 * QC tools never both have magic select on (each yields while the other is on
 * screen), and a second `show` takes the bar over rather than stacking. The
 * tool keeps its own mode; the bar only reports clicks (`onMode`, `onClose`).
 *
 * Core, so both plugins share one look. Mounted in the viewer's wrapper, at
 * the top centre -- the caption owns the top left, the dataset arrows the top
 * right, the chat composer the bottom centre.
 */
(function (global) {
    "use strict";

    const MODES = [
        { id: "add", icon: "circle-plus", label: "Add",
          title: "Add -- click to outline something, or to add to the outline" },
        { id: "remove", icon: "circle-minus", label: "Remove",
          title: "Remove -- click an area to take it out of the outline (or Shift-click in Add)" },
        { id: "box", icon: "expand", label: "Box",
          title: "Box -- drag a box round the object to outline it" },
        { id: "scribble", icon: "paintbrush", label: "Scribble",
          title: "Scribble -- draw a line over the object to outline it "
               + "(Shift: a line over what to leave out)" },
    ];

    let bar = null;      // {root, buttons, owner, onMode, onClose}

    function host() {
        return document.getElementById("openseadragon_wrapper")
            || document.querySelector(".openseadragon-container")?.parentElement || null;
    }

    function build() {
        const root = document.createElement("div");
        root.className = "plx-magic-bar";
        root.setAttribute("role", "toolbar");
        root.setAttribute("aria-label", "Magic select");
        const mark = document.createElement("span");
        mark.className = "plx-magic-bar-mark fas fa-wand-magic-sparkles";
        mark.setAttribute("aria-hidden", "true");
        mark.title = "Magic select";
        root.appendChild(mark);
        const group = document.createElement("div");
        group.className = "plx-magic-bar-modes";
        group.setAttribute("role", "radiogroup");
        group.setAttribute("aria-label", "How a click prompts");
        const buttons = {};
        for (const mode of MODES) {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "plx-magic-bar-button";
            button.dataset.mode = mode.id;
            button.setAttribute("role", "radio");
            button.setAttribute("aria-checked", "false");
            button.setAttribute("aria-label", mode.label);
            button.title = mode.title;
            const icon = document.createElement("span");
            icon.className = `fas fa-${mode.icon}`;
            icon.setAttribute("aria-hidden", "true");
            button.appendChild(icon);
            button.addEventListener("click", (event) => {
                event.stopPropagation();
                bar?.onMode?.(mode.id);
            });
            group.appendChild(button);
            buttons[mode.id] = button;
        }
        root.appendChild(group);
        const close = document.createElement("button");
        close.type = "button";
        close.className = "plx-magic-bar-button plx-magic-bar-close";
        close.title = "Close magic select";
        close.setAttribute("aria-label", "Close magic select");
        const x = document.createElement("span");
        x.className = "fas fa-xmark";
        x.setAttribute("aria-hidden", "true");
        close.appendChild(x);
        close.addEventListener("click", (event) => {
            event.stopPropagation();
            bar?.onClose?.();
        });
        root.appendChild(close);
        // A press on the bar is never a press on the image under it.
        for (const name of ["pointerdown", "mousedown", "click", "dblclick", "wheel"]) {
            root.addEventListener(name, (event) => event.stopPropagation());
        }
        return { root, buttons, mark };
    }

    /**
     * Show the bar for `owner` (any token; the caller's own object), with the
     * mode lit. Returns a handle: {setMode, setBusy, hide, isShown}.
     */
    function show({ owner, mode = "add", onMode, onClose } = {}) {
        const mount = host();
        if (!mount) return null;
        if (!bar) {
            bar = build();
        }
        bar.owner = owner;
        bar.onMode = onMode;
        bar.onClose = onClose;
        if (bar.root.parentNode !== mount) mount.appendChild(bar.root);
        bar.root.hidden = false;
        setMode(owner, mode);
        setBusy(owner, false);
        return {
            setMode: (next) => setMode(owner, next),
            setBusy: (busy) => setBusy(owner, busy),
            hide: () => hide(owner),
            isShown: () => Boolean(bar && bar.owner === owner && !bar.root.hidden),
        };
    }

    function setMode(owner, mode) {
        if (!bar || bar.owner !== owner) return;
        for (const [id, button] of Object.entries(bar.buttons)) {
            button.setAttribute("aria-checked", id === mode ? "true" : "false");
        }
        bar.root.dataset.mode = mode;
    }

    function setBusy(owner, busy) {
        if (!bar || bar.owner !== owner) return;
        bar.root.setAttribute("aria-busy", busy ? "true" : "false");
    }

    /** Gone at once -- only for the owner that showed it. */
    function hide(owner) {
        if (!bar || (owner !== undefined && bar.owner !== owner)) return;
        bar.root.hidden = true;
        if (bar.root.parentNode) bar.root.parentNode.removeChild(bar.root);
        bar.owner = null;
        bar.onMode = null;
        bar.onClose = null;
    }

    const api = { MODES: MODES.map((m) => m.id), show, hide };
    global.PlexoraMagicBar = api;
    if (typeof globalThis !== "undefined" && globalThis !== global) {
        globalThis.PlexoraMagicBar = api;
    }
})(typeof window !== "undefined" ? window : globalThis);
