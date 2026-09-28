/**
 * errors.js - one place a front-end failure is reported, and what telemetry
 * may know about it.
 *
 *     PlexoraErrors.report(err, { component: "tool_loader", action: "load", plugin: "gating" });
 *
 * prints the same `console.error` line a call site would have printed itself,
 * so swapping one for the other loses nothing, and -- when telemetry is on --
 * counts a FINGERPRINT: a hash of the error's class, the component and action
 * the caller named, and the basename of the Plexora script it was thrown from.
 * The message is never read into it, and neither is the stack beyond that one
 * basename, which is kept only when the script is one of Plexora's own
 * (`/client/src/`, `/client/dist/`, `/client/external/`, `/plugins/<id>/`) on
 * this page's own origin. A message is where a project name, a path or a URL
 * with a token in it would be.
 *
 * Also listens for uncaught errors and unhandled rejections, as components
 * `window` and `promise`. `console.error` is deliberately NOT patched: most of
 * its callers log a condition, not a failure.
 */
window.PlexoraErrors = (function () {
    "use strict";

    const COMPONENTS = new Set([
        "tool_loader", "plugin", "viewer", "tiles", "decode", "webgl", "labels", "routing",
        "settings", "import", "agent", "figure", "gating", "roi", "cell_explorer",
        "transcripts", "visium_hd", "window", "promise", "other",
    ]);
    const ACTIONS = new Set(["load", "show", "hide", "activate", "deactivate", "render",
                             "decode", "fetch", "init", "save", "export", "import", "commit",
                             "other"]);
    const OWN_SCRIPT = /\/(?:client\/(?:src|dist|external)|plugins\/[a-z_]+\/static)\/(?:[^?#\s)]*\/)?([A-Za-z0-9_-]{1,56}\.m?js)(?=[?#:\s)]|$)/;
    const NAME = /^[A-Z][A-Za-z0-9]{0,63}$/;
    //: New fingerprints per tab per window; a loop that throws is counted,
    //: not allowed to fill the aggregate.
    const MAX_NEW = 20;
    const WINDOW_MS = 5 * 60 * 1000;

    let windowStart = Date.now();
    let fresh = new Set();
    const seen = new Set();

    /** The basename of the Plexora script `stack` was thrown from, or
     *  `external` / `inline`. Only the pathname's last segment survives. */
    function assetOf(stack) {
        if (!stack) return "inline";
        const origin = (window.location && window.location.origin) || "";
        for (const line of String(stack).split("\n")) {
            const match = line.match(/(https?:\/\/[^/\s)]+)(\/[^\s)]*)/);
            if (!match) continue;
            if (match[1] !== origin) return "external";
            const own = match[2].match(OWN_SCRIPT);
            return own ? own[1] : "external";
        }
        return "inline";
    }

    function fingerprintOf(error, component, action) {
        const name = error && NAME.test(String(error.name || "")) ? String(error.name) : "Error";
        const file = assetOf(error && error.stack);
        const telemetry = window.PlexoraTelemetry;
        const hash = telemetry && telemetry.fnv8
            ? telemetry.fnv8(`${name}|${component}|${action}|${file}`) : "00000000";
        return { fp: hash, name, file };
    }

    function count(error, context) {
        const telemetry = window.PlexoraTelemetry;
        if (!telemetry || telemetry.enabled === false) return;
        const component = COMPONENTS.has(context.component) ? context.component : "other";
        const action = ACTIONS.has(context.action) ? context.action : "other";
        const { fp, name, file } = fingerprintOf(error, component, action);
        const now = Date.now();
        if (now - windowStart > WINDOW_MS) {
            windowStart = now;
            fresh = new Set();
        }
        if (!seen.has(fp)) {
            if (fresh.size >= MAX_NEW) return;
            fresh.add(fp);
            seen.add(fp);
        }
        const dims = { where: "browser", fp, exc_type: name, component, action, file };
        if (context.plugin) dims.plugin = telemetry.ownerLabel(context.plugin);
        telemetry.count("error.fingerprint", "n", 1, dims);
    }

    /**
     * Report a failure: log it, and count its fingerprint.
     *
     * @param error the Error (or anything thrown)
     * @param context `{component, action, plugin?, message?}` -- `message` is
     *   the console line's own prefix, and is never counted.
     */
    function report(error, context) {
        const settings = context || {};
        const prefix = settings.message
            || `${settings.plugin ? settings.plugin + ": " : ""}${settings.component || "plexora"}: ${settings.action || "failed"}`;
        try {
            console.error(prefix, error);
        } catch (e) {
            // A console that throws is not worth a second failure.
        }
        try {
            count(error instanceof Error ? error : { name: "Error", stack: "" }, settings);
            // So a caller further up that catches the same error knows it has
            // been said already, and does not count it twice.
            if (error && typeof error === "object") error.__plexoraReported = true;
        } catch (e) {
            // Reporting must never be the second failure.
        }
    }

    window.addEventListener("error", (event) => {
        // A resource that failed to load raises `error` on its element, not
        // here, unless it bubbles; only script errors carry `event.error`.
        if (!event || !event.error) return;
        try {
            count(event.error, { component: "window", action: "other" });
        } catch (e) {
            // never a second failure
        }
    });

    window.addEventListener("unhandledrejection", (event) => {
        const reason = event && event.reason;
        try {
            count(reason instanceof Error ? reason : { name: "Error", stack: "" },
                  { component: "promise", action: "other" });
        } catch (e) {
            // never a second failure
        }
    });

    return { report, assetOf, fingerprintOf };
})();
