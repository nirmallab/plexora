/**
 * volumeWindows.js - the window each of a volume's channels is quantized with.
 *
 * A volume's 8-bit tiles map one window per channel, [qmin, qmax], onto bytes
 * (server/volume/windows.py). The exact window is a scan of the whole stack --
 * seconds to minutes for a large one -- so the server answers first with a
 * PROVISIONAL window and computes the exact one in the background
 * (`GET /volume/<ds>/window`). Every tile, section and level the viewer asks
 * for names the window it is to be made with (`w=qmin,qmax`), which fixes its
 * bytes by its address: a provisional tile can never be mistaken for an exact
 * one, in any cache.
 *
 * This keeps the windows the viewer has been given, asks again while any is
 * provisional, and announces each change (`plexora:volume-window`, detail
 * `{name, window: [qmin, qmax], exact, previous}`): the sidebar moves its
 * ranges into the new bytes, the plane and the panes hand over to tiles made
 * with it, the 3-D view fetches its levels again. A planar image has none of
 * this.
 *
 * Classic script, `window.PlexoraVolumeWindows`.
 */
(function (global) {
    "use strict";

    const POLL_MS = 1500;
    const POLL_MAX_MS = 10000;

    function url(path) {
        return typeof global.plexoraUrl === "function" ? global.plexoraUrl(path) : `/${path}`;
    }

    /** A window as the server's `f"{x:g}"` writes it (its `w=` check). */
    function formatG(value) {
        const number = Number(value);
        return Number.isInteger(number) && Math.abs(number) < 1e6
            ? String(number) : String(Number(number.toPrecision(6)));
    }

    class VolumeWindows {
        constructor(datasource, { fetchImpl = null } = {}) {
            this.datasource = datasource;
            this._fetch = fetchImpl || ((...args) => global.fetch(...args));
            //: name -> {qmin, qmax, exact}
            this.windows = new Map();
            this._asking = new Map();
            this._timer = 0;
            this._delay = POLL_MS;
        }

        /** The window held for `name`, or null. */
        peek(name) {
            return this.windows.get(name) || null;
        }

        /** `qmin,qmax` for `name`, as a request names it; "" when unknown. */
        stamp(name) {
            const window = this.peek(name);
            return window ? `${formatG(window.qmin)},${formatG(window.qmax)}` : "";
        }

        /** Have the windows of `names`; resolves once each is held (or failed). */
        ensure(names) {
            const missing = [...new Set(names || [])].filter(
                (name) => name && !this.windows.has(name) && !this._asking.has(name));
            if (missing.length) {
                const asking = this.ask(missing);
                for (const name of missing) this._asking.set(name, asking);
                asking.finally(() => {
                    for (const name of missing) {
                        if (this._asking.get(name) === asking) this._asking.delete(name);
                    }
                });
            }
            return Promise.all([...new Set(names || [])].map((name) => this._asking.get(name)))
                .then(() => undefined);
        }

        async ask(names) {
            const ds = encodeURIComponent(this.datasource);
            const query = names.map((name) => `channel=${encodeURIComponent(name)}`).join("&");
            let answer = null;
            try {
                const response = await this._fetch(url(`volume/${ds}/window?${query}`));
                answer = response.ok ? await response.json() : null;
            } catch (error) {
                console.error("volumeWindows: could not read", names, error);
            }
            for (const [name, held] of Object.entries(answer?.windows || {})) {
                this.adopt(name, held);
            }
            this.schedule();
        }

        adopt(name, held) {
            const window = { qmin: Number(held.qmin), qmax: Number(held.qmax), exact: Boolean(held.exact) };
            const previous = this.windows.get(name) || null;
            this.windows.set(name, window);
            if (previous && (previous.qmin !== window.qmin || previous.qmax !== window.qmax)) {
                try {
                    global.dispatchEvent?.(new CustomEvent("plexora:volume-window", {
                        detail: { name, window: [window.qmin, window.qmax], exact: window.exact,
                                  previous: [previous.qmin, previous.qmax] } }));
                } catch (error) {
                    // No CustomEvent in a bare runtime.
                }
            }
        }

        /** Ask again, backing off, while any window is provisional. */
        schedule() {
            if (this._timer) return;
            const provisional = [...this.windows].filter(([, w]) => !w.exact).map(([name]) => name);
            if (!provisional.length) {
                this._delay = POLL_MS;
                return;
            }
            this._timer = setTimeout(async () => {
                this._timer = 0;
                this._delay = Math.min(POLL_MAX_MS, this._delay * 1.5);
                await this.ask(provisional);
            }, this._delay);
        }
    }

    VolumeWindows.formatG = formatG;
    global.PlexoraVolumeWindows = VolumeWindows;
    if (typeof module !== "undefined" && module.exports) {
        module.exports = VolumeWindows;
    }
})(typeof window !== "undefined" ? window : globalThis);
