/**
 * feedbackDiagnostics.js -- what a bug report or feature request carries
 * besides what the person wrote (views/feedbackDialog.js).
 *
 *     const diagnostics = await PlexoraFeedbackDiagnostics.collect(context);
 *
 * `collect` returns exactly the object that is sent, and the form shows it
 * under "What is sent" before anything leaves. It holds facts about the
 * software and the screen -- versions, the operating system, the browser,
 * sizes, language, time zone -- the KIND of page (viewer, settings; never a
 * project's name or a path) and the tool open, and the last few errors this
 * tab caught (services/errors.js `recent`, already scrubbed of URLs, paths,
 * emails and tokens). Never a file's contents, a dataset or project name, a
 * path, a certificate or a token. A value that cannot be read is left out.
 *
 * `context` is `GET /feedback/context`'s answer (the server's version, build
 * and platform), or the website's equivalent; may be empty.
 */
window.PlexoraFeedbackDiagnostics = (function () {
    "use strict";

    const KNOWN_PAGES = { "": "home", settings: "settings", open_project: "open project",
                          edit: "project edit", project_edit: "project edit" };

    /** desktop, website, browser or notebook: where this Plexora runs. */
    function modeOf(context) {
        if (context?.website || window.PlexoraBackend?.kind === "browser") return "website";
        if (window.PlexoraDesktop || context?.desktop) return "desktop";
        if (window.flaskVariables?.notebook_mode || window.self !== window.top) return "notebook";
        return "browser";
    }

    /** The kind of page, never its address: a project's name is in the URL. */
    function pageKind() {
        const base = String(window.flaskVariables?.base_url || "").replace(/\/+$/, "");
        let path = window.location.pathname || "/";
        if (base && path.startsWith(base)) path = path.slice(base.length);
        const first = path.replace(/^\/+/, "").split("/")[0];
        if (Object.prototype.hasOwnProperty.call(KNOWN_PAGES, first)) return KNOWN_PAGES[first];
        return document.body?.dataset?.plexoraDatasource ? "viewer" : "other";
    }

    /** {os, os_version} from the client hints where there are some, else the user agent. */
    async function operatingSystem() {
        const ua = navigator.userAgent || "";
        const hints = navigator.userAgentData;
        if (hints && typeof hints.getHighEntropyValues === "function") {
            try {
                const values = await hints.getHighEntropyValues(["platformVersion"]);
                const name = hints.platform || "";
                let version = values.platformVersion || "";
                // Windows 11 reports a platform version of 13 or above.
                if (name === "Windows" && version) version = Number(version.split(".")[0]) >= 13 ? "11" : "10";
                if (name) return { os: name === "macOS" ? "macOS" : name, os_version: version };
            } catch (error) { /* fall through to the user agent */ }
        }
        let match = ua.match(/Mac OS X (\d+[._]\d+(?:[._]\d+)?)/);
        if (match) return { os: "macOS", os_version: match[1].replace(/_/g, ".") };
        match = ua.match(/Windows NT (\d+\.\d+)/);
        if (match) return { os: "Windows", os_version: match[1] === "10.0" ? "10 or 11" : match[1] };
        if (/Android/.test(ua)) return { os: "Android", os_version: (ua.match(/Android ([\d.]+)/) || [])[1] || "" };
        if (/iPhone|iPad/.test(ua)) return { os: "iOS", os_version: ((ua.match(/OS (\d+_\d+)/) || [])[1] || "").replace("_", ".") };
        if (/CrOS/.test(ua)) return { os: "ChromeOS", os_version: "" };
        if (/Linux/.test(ua)) return { os: "Linux", os_version: "" };
        return { os: navigator.platform || "", os_version: "" };
    }

    /** {browser, browser_version}: Edge, Chrome, Firefox or Safari, or the desktop app's web view. */
    function browser() {
        const ua = navigator.userAgent || "";
        const rules = [
            ["Edge", /Edg\/([\d.]+)/], ["Opera", /OPR\/([\d.]+)/], ["Firefox", /Firefox\/([\d.]+)/],
            ["Chrome", /Chrome\/([\d.]+)/], ["Safari", /Version\/([\d.]+).*Safari/],
        ];
        for (const [name, pattern] of rules) {
            const match = ua.match(pattern);
            if (match) return { browser: name, browser_version: match[1] };
        }
        const webkit = ua.match(/AppleWebKit\/([\d.]+)/);
        if (webkit) return { browser: "WebKit", browser_version: webkit[1] };
        return { browser: "", browser_version: "" };
    }

    /** The graphics card the browser draws with, as WebGL names it; "" when it will not say. */
    function gpu() {
        try {
            const canvas = document.createElement("canvas");
            const gl = canvas.getContext("webgl2") || canvas.getContext("webgl");
            if (!gl) return "no WebGL";
            const ext = gl.getExtension("WEBGL_debug_renderer_info");
            const name = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
            gl.getExtension("WEBGL_lose_context")?.loseContext();
            return String(name || "");
        } catch (error) {
            return "";
        }
    }

    function siteVersion() {
        try {
            const site = window.parent?.PlexoraSite || window.PlexoraSite || null;
            return { version: site?.version || "", build: site?.build || "" };
        } catch (error) {
            return { version: "", build: "" };
        }
    }

    /** Everything that is sent, in the order the form lists it. Empty values dropped. */
    async function collect(context = {}) {
        const site = siteVersion();
        const mode = modeOf(context);
        const system = await operatingSystem();
        const web = browser();
        const screenInfo = window.screen || {};
        let tool = "";
        try { tool = window.PlexoraToolLoader?.activeTool?.() || ""; } catch (error) { tool = ""; }
        let timezone = "";
        try { timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (error) { timezone = ""; }
        const plan = window.flaskVariables?.license?.plan || "";
        const out = {
            app_version: context.version || site.version || "",
            build: context.build || site.build || "",
            mode,
            plan: plan ? plan.charAt(0).toUpperCase() + plan.slice(1) : "",
            desktop_shell: window.PlexoraDesktop?.version || "",
            os: system.os,
            os_version: system.os_version,
            server_platform: mode === "website" ? "" : (context.platform || ""),
            python: context.python || "",
            browser: web.browser,
            browser_version: web.browser_version,
            screen: screenInfo.width ? `${screenInfo.width}×${screenInfo.height}` : "",
            pixel_ratio: Math.round((window.devicePixelRatio || 1) * 100) / 100,
            viewport: `${window.innerWidth}×${window.innerHeight}`,
            gpu: gpu(),
            cpu_cores: navigator.hardwareConcurrency || "",
            device_memory_gb: navigator.deviceMemory || "",
            touch: (navigator.maxTouchPoints || 0) > 0,
            language: navigator.language || "",
            timezone,
            route: pageKind(),
            tool,
            sent_at: new Date().toISOString(),
        };
        const kept = Object.fromEntries(Object.entries(out).filter(([, v]) => v !== "" && v !== null && v !== undefined));
        const errors = window.PlexoraErrors?.recent?.() || [];
        if (errors.length) kept.errors = errors;
        return kept;
    }

    /** The diagnostics as [label, value] rows for the form's "What is sent". */
    function rows(diagnostics) {
        const labels = {
            app_version: "Plexora", build: "Build", mode: "Running as", plan: "Plan", desktop_shell: "Desktop shell",
            os: "Operating system", os_version: "OS version", server_platform: "Server platform", python: "Python",
            browser: "Browser", browser_version: "Browser version", screen: "Screen", pixel_ratio: "Pixel ratio",
            viewport: "Window", gpu: "Graphics", cpu_cores: "CPU cores", device_memory_gb: "Memory (GB)",
            touch: "Touch screen", language: "Language", timezone: "Time zone", route: "Page", tool: "Open tool",
            sent_at: "Time",
        };
        const out = [];
        for (const [key, value] of Object.entries(diagnostics || {})) {
            if (key === "errors") continue;
            out.push([labels[key] || key, typeof value === "boolean" ? (value ? "yes" : "no") : String(value)]);
        }
        return out;
    }

    return { collect, rows, modeOf, pageKind };
})();
