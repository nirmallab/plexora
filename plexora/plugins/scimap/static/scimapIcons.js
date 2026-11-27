// SCIMAP's line icons: the approved design's own glyphs, 24-unit boxes drawn
// with a 1.8 stroke in currentColor, so a button's ink colours its icon.
//
// Inline SVG rather than Font Awesome because the design draws these shapes
// (a lasso, a hex cluster, a TV) that FA has no equivalent for, and one family
// of strokes reads as one family.
window.ScimapIcons = (function () {
    "use strict";

    const PATHS = {
        table: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M9 9v11"/>',
        sliders: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
        tag: '<path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
        cluster: '<circle cx="7" cy="8" r="3"/><circle cx="17" cy="7" r="3"/><circle cx="12" cy="17" r="3"/>',
        scatter: '<path d="M3 3v18h18"/><circle cx="8" cy="15" r="1.4"/><circle cx="11" cy="10" r="1.4"/><circle cx="15" cy="14" r="1.4"/><circle cx="17" cy="7" r="1.4"/><circle cx="20" cy="11" r="1.4"/>',
        pen: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
        merge: '<circle cx="6" cy="6" r="2"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="12" r="2"/><path d="M6 8v8M8 6c5 0 8 2 8 6"/>',
        bars: '<rect x="4" y="4" width="6" height="16" rx="1"/><rect x="14" y="9" width="6" height="11" rx="1"/><path d="M4 11h6M14 14h6"/>',
        grid: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18M15 3v18"/>',
        distance: '<circle cx="5" cy="19" r="2"/><circle cx="19" cy="5" r="2"/><path d="M7 17 17 7" stroke-dasharray="2 3"/>',
        network: '<circle cx="6" cy="12" r="3"/><circle cx="18" cy="6" r="3"/><circle cx="18" cy="18" r="3"/><path d="m8.7 10.6 6.6-3.2M8.7 13.4l6.6 3.2"/>',
        hexes: '<path d="M12 2.5l4 2.3v4.6L12 11.7 8 9.4V4.8z"/><path d="M7 12.3l4 2.3v4.6l-4 2.3-4-2.3v-4.6z"/><path d="M17 12.3l4 2.3v4.6l-4 2.3-4-2.3v-4.6z"/>',
        image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/>',
        layers: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
        cog: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
        history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/>',
        database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
        folderPlus: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 11v5M9.5 13.5h5"/>',
        chip: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
        key2: '<circle cx="8" cy="15" r="4"/><path d="m10.8 12.2 8.2-8.2M17 6l2 2M15 8l2 2"/>',
        book: '<path d="M5 4h10a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z"/><path d="M5 17a3 3 0 0 1 3-3h10"/>',
        chevD: '<path d="m6 9 6 6 6-6"/>',
        chevR: '<path d="m9 6 6 6-6 6"/>',
        chevL: '<path d="m15 6-6 6 6 6"/>',
        check: '<path d="M20 6 9 17l-5-5"/>',
        search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
        panelClose: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M16 10l-2 2 2 2"/>',
        panelOpen: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M13 10l2 2-2 2"/>',
        download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
        play: '<path d="M7 5v14l11-7z" fill="currentColor" stroke="none"/>',
        playAll: '<path d="M5 5v14l8-7z M13 5v14l8-7z" fill="currentColor" stroke="none"/>',
        x: '<path d="M6 6l12 12M18 6 6 18"/>',
        eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
        lasso: '<path d="M7 18c-2.5-1-4-3-4-5.5C3 8 7 5 12 5s9 3 9 7.5S17 20 12 20c-1.2 0-2.3-.2-3.3-.5"/><circle cx="7" cy="19" r="1.6"/>',
        tv: '<rect x="2.5" y="7" width="19" height="13" rx="2"/><path d="m8 3 4 4 4-4"/><path d="M6 20.5h12" opacity="0"/>',
        external: '<path d="M14 4h6v6M20 4l-9 9M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
        warning: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
        info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
        lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
        file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>',
        folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
        plus: '<path d="M12 5v14M5 12h14"/>',
        minus: '<path d="M5 12h14"/>',
        fit: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
        sizes: '<circle cx="6" cy="16" r="1.6" fill="currentColor" stroke="none"/><circle cx="12" cy="13" r="2.4" fill="currentColor" stroke="none"/><circle cx="18.5" cy="9" r="3.4" fill="currentColor" stroke="none"/>',
        refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
        stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
        figure: '<rect x="3" y="3" width="8" height="8" rx="1"/><rect x="13" y="3" width="8" height="8" rx="1"/><rect x="3" y="13" width="18" height="8" rx="1"/>',
        dots: '<circle cx="5" cy="12" r="1.7" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.7" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.7" fill="currentColor" stroke="none"/>',
        copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
        trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/>',
        rename: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
        up: '<path d="m6 15 6-6 6 6"/>',
        down: '<path d="m6 9 6 6 6-6"/>',
        collapse: '<path d="m7 15 5-5 5 5"/>',
        expand: '<path d="m7 9 5 5 5-5"/>',
        upload: '<path d="M12 20V9M7 14l5-5 5 5M5 4h14"/>',
        back: '<path d="M19 12H5M11 6l-6 6 6 6"/>',
        key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M17 6l3 3M15 8l2 2"/>',
        undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
        violin: '<path d="M12 3c-1.6 3.4-4 5.6-4 9s2.4 5.6 4 9c1.6-3.4 4-5.6 4-9s-2.4-5.6-4-9z"/><path d="M9.5 12h5"/>',
        map: '<path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2z"/><path d="M9 4v14M15 6v14"/>',
        pie: '<path d="M11 4a8 8 0 1 0 9 9h-9z"/><path d="M14 3.2A8 8 0 0 1 20.8 10H14z"/>',
        density: '<circle cx="12" cy="12" r="2.5"/><circle cx="12" cy="12" r="6" opacity=".65"/><circle cx="12" cy="12" r="9.5" opacity=".35"/>',
        radii: '<circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="9"/>',
        voronoi: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 11l6-2 3-6M9 9l3 7-9 1M12 16l5-4 4 1M17 12l-2-9M12 16l-1 5"/>',
        full: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
        stacked: '<rect x="4" y="4" width="6" height="6" rx="1"/><rect x="4" y="10" width="6" height="10" rx="1"/><rect x="14" y="7" width="6" height="6" rx="1"/><rect x="14" y="13" width="6" height="7" rx="1"/>',
        columns: '<path d="M3 20h18"/><rect x="5" y="11" width="3.2" height="9" rx=".6"/><rect x="10.4" y="5" width="3.2" height="15" rx=".6"/><rect x="15.8" y="14" width="3.2" height="6" rx=".6"/>',
        hbars: '<path d="M4 3v18"/><rect x="4" y="5" width="11" height="3.4" rx=".6"/><rect x="4" y="10.3" width="16" height="3.4" rx=".6"/><rect x="4" y="15.6" width="7" height="3.4" rx=".6"/>',
        grouped: '<path d="M3 20h18"/><rect x="4" y="9" width="3" height="11" rx=".5"/><rect x="7.6" y="13" width="3" height="7" rx=".5"/><rect x="13.4" y="6" width="3" height="14" rx=".5"/><rect x="17" y="11" width="3" height="9" rx=".5"/>',
        hist: '<path d="M3 20h18"/><path d="M5 20v-4h3v-7h3V4h3v8h3v4h2v4"/>',
        unfull: '<path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/>',
        list: '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1.2"/><circle cx="4.5" cy="12" r="1.2"/><circle cx="4.5" cy="18" r="1.2"/>',
        compare: '<path d="M12 3v18"/><rect x="3" y="9" width="6" height="11" rx=".8"/><rect x="15" y="5" width="6" height="15" rx=".8"/>',
        shield: '<path d="M12 3 4.5 6v5.5c0 4.6 3.2 8.3 7.5 9.5 4.3-1.2 7.5-4.9 7.5-9.5V6z"/><path d="m9 12 2 2 4-4"/>',
        forest: '<path d="M12 3v18"/><path d="M5 7h9M8 12h10M4 17h7"/><circle cx="9.5" cy="7" r="1.4"/><circle cx="13" cy="12" r="1.4"/><circle cx="7.5" cy="17" r="1.4"/>',
    };

    function svg(name, extra) {
        const body = PATHS[name] || "";
        return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="' +
            ((extra && extra.weight) || 1.8) +
            '" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
            body + "</svg>";
    }

    // A <span class="sm-ic"> holding the glyph; `size` "sm" (14) or "xs" (12).
    function el(name, size) {
        const span = document.createElement("span");
        span.className = "sm-ic" + (size ? " sm-ic-" + size : "");
        span.innerHTML = svg(name);
        return span;
    }

    // SCIMAP Pro's logo (scimappro's website brand mark), served beside the
    // plugin's scripts.
    function mark() {
        const root = document.getElementById("sm_workspace");
        const img = document.createElement("img");
        img.className = "sm-brand-mark";
        img.src = ((root && root.dataset.base) || "/plugins/scimap") + "/static/scimapLogo.png";
        img.alt = "";
        img.width = 20;
        img.height = 26;
        return img;
    }

    return { svg, el, mark, names: Object.keys(PATHS) };
})();
