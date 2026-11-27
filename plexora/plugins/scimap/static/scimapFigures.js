// Scimap Pro's figures on the page.
//
// The server asks `scimappro.pl` for a plotly figure (server/figures.py) and
// sends it as JSON; this draws it with plotly.js, loaded once, the first time a
// figure is needed, from the plotly package the server has installed
// (/plugins/scimap/vendor/plotly.min.js). Nothing here decides what a plot
// shows: ordering, colours and statistics are Scimap Pro's.
//
// A drawn figure is kept and moved, not redrawn, when its step re-renders
// (collapse, a poll, an edit elsewhere): the same plot comes back at the same
// height, so nothing on the page jumps.
window.ScimapFigures = (function () {
    "use strict";

    let loading = null;
    const kept = new Map();

    function base() {
        const root = document.getElementById("sm_workspace");
        return (root && root.dataset.base) || "/plugins/scimap";
    }

    function plotly() {
        if (window.Plotly) return Promise.resolve(window.Plotly);
        if (!loading) {
            loading = new Promise((resolve, reject) => {
                const script = document.createElement("script");
                script.src = base() + "/vendor/plotly.min.js";
                script.async = true;
                script.onload = () => (window.Plotly ? resolve(window.Plotly) : reject(new Error("plotly.js did not load")));
                script.onerror = () => {
                    loading = null;
                    reject(new Error("plotly.js could not be loaded"));
                };
                document.head.appendChild(script);
            });
        }
        return loading;
    }

    const CONFIG = {
        displaylogo: false,
        displayModeBar: false,
        responsive: true,
        scrollZoom: false,
        doubleClick: "reset",
    };

    // Draw `entry.figure` into `host`, or move the plot drawn for `key` before.
    // Resolves with the plot's element (plotly's graph div).
    async function mount(host, entry, key, options) {
        const opts = options || {};
        const height = (entry.figure && entry.figure.layout && entry.figure.layout.height) || 320;
        host.style.minHeight = height + "px";
        const old = key ? kept.get(key) : null;
        if (old) {
            host.appendChild(old);
            // Drawn for a column of another width: fill this one.
            requestAnimationFrame(() => refit(old));
            return old;
        }
        const Plotly = await plotly();
        const div = document.createElement("div");
        div.className = "sm-plotly";
        host.appendChild(div);
        const layout = Object.assign({}, entry.figure.layout, opts.layout || {});
        await Plotly.newPlot(div, entry.figure.data, layout, Object.assign({}, CONFIG, opts.config || {}));
        fitAspect(div, host);
        if (typeof ResizeObserver === "function") {
            // plotly follows the window, not its column: a sidebar opening or
            // closing, a view going full screen, a view added beside it all
            // change the width the plot has without a window resize.
            let last = div.clientWidth;
            let timer = null;
            new ResizeObserver(() => {
                if (Math.abs(div.clientWidth - last) < 4) return;
                last = div.clientWidth;
                clearTimeout(timer);
                timer = setTimeout(() => refit(div), 120);
            }).observe(div);
        }
        if (key) {
            kept.set(key, div);
            if (kept.size > 60) {
                const first = kept.keys().next().value;
                const gone = kept.get(first);
                kept.delete(first);
                if (gone && !gone.isConnected && window.Plotly) window.Plotly.purge(gone);
            }
        }
        return div;
    }

    // A tissue map keeps x and y at one scale, so a fixed height leaves
    // bands of white above and below it: make the plot as tall as its data
    // is at the width it has.
    function equalAspect(div) {
        const fl = div._fullLayout;
        // Only a map of points; a heatmap's square cells are its own business.
        const points = (div._fullData || []).some((t) => t.type === "scatter" || t.type === "scattergl");
        return Boolean(points && fl && fl.yaxis && fl.xaxis && (fl.yaxis.scaleanchor === "x" || fl.xaxis.scaleanchor === "y"));
    }

    function fitAspect(div, host) {
        if (!equalAspect(div) || !window.Plotly || !div.isConnected) return;
        const fl = div._fullLayout;
        // The data's own extent: with the scales locked, the axis ranges are
        // stretched to fill whatever box the plot has.
        const ext = { x: [Infinity, -Infinity], y: [Infinity, -Infinity] };
        (div._fullData || []).filter((t) => t.type === "scatter" || t.type === "scattergl").forEach((t) => {
            ["x", "y"].forEach((k) => {
                const arr = t[k];
                if (!arr || typeof arr.length !== "number") return;
                for (let i = 0; i < arr.length; i += 1) {
                    const v = Number(arr[i]);
                    if (!isFinite(v)) continue;
                    if (v < ext[k][0]) ext[k][0] = v;
                    if (v > ext[k][1]) ext[k][1] = v;
                }
            });
        });
        const xs = ext.x[1] - ext.x[0];
        const ys = ext.y[1] - ext.y[0];
        if (!(xs > 0 && ys > 0)) return;
        // The plot area plotly settled on, after a colour bar or a legend
        // pushed its margins out.
        const size = fl._size || { w: div.clientWidth, h: fl.height };
        if (size.w < 50) return;
        // As tall as the window allows, so a tall tissue in a wide column
        // still fills the width it can.
        const cap = Math.max(480, Math.min(1100, (window.innerHeight || 900) - 160));
        const height = Math.round(Math.max(220, Math.min(cap, size.w * (ys / xs) * 1.03 + (fl.height - size.h))));
        if (Math.abs(height - fl.height) < 6) return;
        host.style.minHeight = "";
        window.Plotly.relayout(div, { height });
    }

    // Fill the width the plot has now, then fit a map's height to it.
    function refit(div) {
        if (!window.Plotly || !div || !div.isConnected || !div._fullLayout) return Promise.resolve();
        const host = div.parentElement;
        const width = div.clientWidth;
        // Set outright: plotly's own resize does nothing once a height has
        // been set (a map's fitted height, a full-screen view).
        const go = width > 50 && Math.abs(width - div._fullLayout.width) >= 3
            ? window.Plotly.relayout(div, { width }) : Promise.resolve();
        return Promise.resolve(go).then(() => fitAspect(div, host)).catch(() => {});
    }

    function forget(prefix) {
        for (const [key, div] of kept) {
            if (key.startsWith(prefix)) {
                kept.delete(key);
                if (!div.isConnected && window.Plotly) window.Plotly.purge(div);
            }
        }
    }

    return { plotly, mount, forget, refit };
})();
