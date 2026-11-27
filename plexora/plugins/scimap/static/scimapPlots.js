// What the page still does with numbers itself: a category's colour where
// Scimap Pro's figure is not the thing drawing it (a table's swatch), and the
// CSV and file downloads. Every plot is Scimap Pro's (scimapFigures.js).
window.ScimapPlots = (function () {
    "use strict";

    // Scimap Pro's palettes (pl/_theme.py): Okabe-Ito, then Vega 20. Used only
    // when a column arrives without its colours; the server sends those.
    let palette = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00", "#F0E442", "#000000"];

    function setPalette(colors) {
        if (Array.isArray(colors) && colors.length) palette = colors.slice();
    }

    function color(i) {
        return palette[((i % palette.length) + palette.length) % palette.length];
    }

    function download(name, blob) {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = name;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    }

    function csv(name, header, rows) {
        const esc = (v) => {
            const s = v === null || v === undefined ? "" : String(v);
            return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
        };
        const text = [header.map(esc).join(",")].concat(rows.map((r) => r.map(esc).join(","))).join("\n");
        download(name, new Blob([text], { type: "text/csv" }));
    }

    return { setPalette, color, csv, download };
})();
