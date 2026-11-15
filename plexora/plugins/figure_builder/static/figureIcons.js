/**
 * FigureIcons - the Figure Canvas's line icons, from its approved design.
 *
 * One stroke weight, one 24-unit grid, drawn as inline SVG in currentColor, so
 * an icon takes its colour from the control it sits in and its size from CSS
 * (`.fb-ic`). Inline rather than an icon font: a glyph injected after boot
 * still draws, and a name that is missing draws nothing loudly -- `svg()`
 * falls back to an empty square the size of an icon, which a screenshot shows.
 *
 * Markup rendered on the server carries `<span class="fb-ic" data-ic="name">`
 * and `hydrate()` fills it once at setup; markup built in JavaScript calls
 * `svg()` directly.
 */
class FigureIcons {

    static get PATHS() {
        return {
            plus: "M12 5v14M5 12h14", minus: "M5 12h14", x: "M6 6l12 12M18 6L6 18",
            chevL: "M15 6l-6 6 6 6", chevR: "M9 6l6 6-6 6", chevD: "M6 9l6 6 6-6", chevU: "M6 15l6-6 6 6",
            check: "M5 12.5l4.5 4.5L19 7.5",
            undo: "M9 14L4 9l5-5M4 9h10a6 6 0 0 1 0 12h-3", redo: "M15 14l5-5-5-5M20 9H10a6 6 0 0 0 0 12h3",
            eye: "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
            eyeOff: "M3 3l18 18M10.6 5.1A10.6 10.6 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4M6.5 6.6C3.7 8.4 2 12 2 12s3.6 7 10 7c1.7 0 3.2-.5 4.5-1.2M9.9 9.9a3 3 0 0 0 4.2 4.2",
            lock: "M6 11h12v9H6zM8 11V8a4 4 0 0 1 8 0v3", unlock: "M6 11h12v9H6zM8 11V8a4 4 0 0 1 7.5-2",
            image: "M4 5h16v14H4zM4 15l4.5-4.5 4 4 2.5-2.5L20 16M15 9.5h.01",
            images: "M7 3h14v12H7zM3 7v14h14M7 12l3.5-3.5 3 3 2-2L21 13",
            text: "M5 7V5h14v2M12 5v14M9 19h6", square: "M5 5h14v14H5z",
            circle: "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16z",
            line: "M5 19L19 5", arrow: "M5 19L19 5M10 5h9v9",
            grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
            layers: "M12 3l9 5-9 5-9-5zM3 12.5l9 5 9-5M3 17l9 5 9-5",
            plusSq: "M4 4h16v16H4zM12 8v8M8 12h8",
            shapes: "M8 3l5 8H3zM14 14h7v7h-7zM7.5 14a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7z",
            crop: "M6 2v16h16M2 6h16v16", swap: "M4 8h13l-3-3M20 16H7l3 3",
            copy: "M9 9h11v11H9zM5 15H4V4h11v1", more: "M5 12h.01M12 12h.01M19 12h.01",
            moreV: "M12 5h.01M12 12h.01M12 19h.01",
            trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13",
            exp: "M12 4v11M7 10l5 5 5-5M5 20h14", upload: "M12 15V4M7 9l5-5 5 5M5 20h14",
            bookmark: "M7 4h10v16l-5-4-5 4z", inbox: "M4 13l3-8h10l3 8v6H4zM4 13h5l1 2h4l1-2h5",
            camera: "M4 8h3l2-3h6l2 3h3v11H4zM12 10.5a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
            viewer: "M14 5h5v5M19 5l-8 8M11 6H5v13h13v-6",
            search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4",
            warn: "M12 4l9.5 16h-19zM12 10v4.5M12 17.5v.01",
            bar: "M4 13v5h16v-5", legend: "M4 7h3v3H4zM10 8.5h10M4 14h3v3H4zM10 15.5h10",
            label: "M4 4h16v16H4zM8.5 16l3.5-8 3.5 8M9.8 13.5h4.4",
            colorbar: "M9 4h6v16H9zM9 9h6M9 14h6",
            sideL: "M4 5h16v14H4zM9 5v14", sideR: "M4 5h16v14H4zM15 5v14",
            fit: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
            group: "M3 3h4v4H3zM17 3h4v4h-4zM3 17h4v4H3zM17 17h4v4h-4zM7 5h10M7 19h10M5 7v10M19 7v10",
            ungroup: "M3 3h4v4H3zM17 17h4v4h-4zM7 5h5M5 7v5M19 12v5M12 19h5",
            link: "M9 15l6-6M10.5 6.5l1-1a4 4 0 0 1 5.7 5.7l-1 1M13.5 17.5l-1 1a4 4 0 0 1-5.7-5.7l1-1",
            rotate: "M20 12a8 8 0 1 1-2.6-5.9M20 4v5h-5",
            sliders: "M4 7h10M18 7h2M4 17h4M12 17h8M16 5v4M10 15v4",
            alignL: "M4 4v16M8 7h10v4H8zM8 14h6v4H8z", alignC: "M12 4v16M6 7h12v4H6zM8 14h8v4H8z",
            alignR: "M20 4v16M6 7h10v4H6zM10 14h6v4h-6z",
            alignT: "M4 4h16M7 8h4v10H7zM14 8h4v6h-4z", alignM: "M4 12h16M7 6h4v12H7zM14 8h4v8h-4z",
            alignB: "M4 20h16M7 6h4v10H7zM14 10h4v6h-4z",
            distH: "M4 4v16M20 4v16M9 8h6v8H9z", distV: "M4 4h16M4 20h16M8 9h8v6H8z",
            front: "M8 8h12v12H8zM4 4h10v2M4 4v10h2", back: "M4 4h12v12H4zM10 20h10V10",
            up: "M12 19V5M6 11l6-6 6 6", down: "M12 5v14M6 13l6 6 6-6",
            palette: "M12 3a9 9 0 1 0 0 18c1 0 1.5-.8 1.5-1.6 0-.9-.6-1.4-.6-2.3 0-1 .8-1.6 1.8-1.6H17a4 4 0 0 0 4-4C21 6.6 17 3 12 3zM7.5 11h.01M10 7.5h.01M15 7.5h.01",
            file: "M6 3h8l4 4v14H6zM14 3v4h4",
            pages: "M7 4h10v14H7zM4 7v13h10",
            globe: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18",
            txL: "M4 6h16M4 10h10M4 14h16M4 18h10", txC: "M4 6h16M7 10h10M4 14h16M7 18h10",
            txR: "M4 6h16M10 10h10M4 14h16M10 18h10", txJ: "M4 6h16M4 10h16M4 14h16M4 18h16",
            vTop: "M4 4h16M12 20V8M8 12l4-4 4 4", vMid: "M4 12h16M12 3v5M12 21v-5M9 6l3 2 3-2M9 18l3-2 3 2",
            vBot: "M4 20h16M12 4v12M8 12l4 4 4-4",
            flipH: "M12 3v18M9 6L3 18h6zM15 6l6 12h-6z", flipV: "M3 12h18M6 9L18 3v6zM6 15l12 6v-6z",
        };
    }

    /** The design's position picker glyph: a 3 × 3 field of dots with a small
     *  square on the chosen cell. `anchor` is one of the schema's nine names
     *  (`top_left` ... `bottom_right`, `center`). */
    static anchor(anchor, size = 16) {
        const [row, col] = FigureIcons.anchorCell(anchor);
        const at = [5, 12, 19];
        let d = "";
        for (let r = 0; r < 3; r++) {
            for (let c = 0; c < 3; c++) {
                d += r === row && c === col
                    ? `M${at[c] - 3} ${at[r] - 3}h6v6h-6z`
                    : `M${at[c]} ${at[r]}h.01`;
            }
        }
        return `<svg class="fb-ic" width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true"`
            + ` focusable="false"><path d="${d}"/></svg>`;
    }

    static anchorCell(anchor) {
        const name = String(anchor || "top_left");
        const row = name.startsWith("top") ? 0 : (name.startsWith("bottom") ? 2 : 1);
        const col = name.endsWith("left") ? 0 : (name.endsWith("right") ? 2 : 1);
        return [row, col];
    }

    /** One icon as inline SVG. `size` in CSS px; the stroke is the design's
     *  1.6 px at 16 px and scales with it. */
    static svg(name, size = 16, extraClass = "") {
        const path = FigureIcons.PATHS[name];
        const d = path || "M5 5h14v14H5z";
        return `<svg class="fb-ic${extraClass ? ` ${extraClass}` : ""}${path ? "" : " is-missing"}"`
            + ` width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true"`
            + ` focusable="false"><path d="${d}"/></svg>`;
    }

    /** The design's icon for a Font Awesome name the rest of the plugin uses,
     *  so a registry entry (`icon: "clone"`) draws the design's line icon in
     *  the new chrome. Null when the design has no counterpart. */
    static fromFontAwesome(name) {
        return {
            "plus": "plus", "minus": "minus", "xmark": "x", "check": "check",
            "chevron-left": "chevL", "chevron-right": "chevR", "chevron-down": "chevD", "chevron-up": "chevU",
            "rotate-left": "undo", "rotate-right": "redo", "eye": "eye", "eye-slash": "eyeOff",
            "lock": "lock", "lock-open": "unlock", "image": "image", "images": "images",
            "font": "text", "square": "square", "circle": "circle", "minus-line": "line",
            "arrow-right-long": "arrow", "table-cells-large": "grid", "table-cells": "grid",
            "layer-group": "layers", "shapes": "shapes", "crop": "crop",
            "arrow-right-arrow-left": "swap", "clone": "copy", "copy": "copy",
            "ellipsis": "more", "ellipsis-vertical": "moreV", "trash": "trash",
            "file-export": "exp", "file-image": "upload", "bookmark": "bookmark", "inbox": "inbox",
            "camera": "camera", "up-right-from-square": "viewer", "arrow-up-right-from-square": "viewer",
            "magnifying-glass": "search", "triangle-exclamation": "warn",
            "ruler-horizontal": "bar", "palette": "palette", "swatchbook": "colorbar",
            "expand": "fit", "object-group": "group", "object-ungroup": "ungroup",
            "link": "link", "sliders": "sliders", "align-left": "alignL", "align-center": "alignC",
            "align-right": "alignR", "arrow-up": "alignT", "arrow-down": "alignB",
            "arrows-up-down": "distV", "arrows-left-right": "distH",
            "angles-up": "front", "angles-down": "back", "angle-up": "up", "angle-down": "down",
            "circle-check": "check", "inbox-in": "inbox", "file": "file",
        }[name] || null;
    }

    /** `svg()` for a Font Awesome name, or the Font Awesome span itself when
     *  the design has no counterpart -- never a blank. */
    static fa(name, size = 16) {
        const own = FigureIcons.fromFontAwesome(name);
        return own ? FigureIcons.svg(own, size)
            : `<span class="fas fa-${name}" aria-hidden="true"></span>`;
    }

    /** Fill every `[data-ic]` placeholder under `root` with its icon, once. */
    static hydrate(root) {
        if (!root || typeof root.querySelectorAll !== "function") return;
        for (const el of root.querySelectorAll("[data-ic]")) {
            if (el.dataset.icDone) continue;
            el.innerHTML = FigureIcons.svg(el.dataset.ic, Number(el.dataset.icSize) || 16);
            el.dataset.icDone = "1";
        }
    }
}
