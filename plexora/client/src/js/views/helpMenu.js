/**
 * helpMenu.js -- the Help menu's rows other than Check for Updates.
 *
 * About, Keyboard Shortcuts and Documentation are built on PlexoraConfirm
 * with a content node, like pluginHelp.js, so they open, trap focus and close
 * the way every other core dialog does. Report a Bug and Request a Feature
 * open the feedback form (views/feedbackDialog.js), as the navbar's feedback
 * icon does; the native menus reach them through the same rows.
 *
 * About reads `/desktop/info`, which answers in every mode despite its name
 * (desktop_routes.py) and never carries the token or the port -- except on
 * the website, which has no installed Plexora to describe (`siteInfo`).
 */
window.PlexoraHelpMenu = (function () {
    "use strict";

    //: Fallback only. The page renders `plexora.links.DOCS_URL` into
    //: <body data-plexora-docs-url>, so the site's address has one home.
    const DEFAULT_DOCS_URL = "https://plexoraapp.com/";

    /** The documentation site, or one page of it: `path` is relative to the
     *  site's /docs/ root ("plugins/roi"). */
    function docsUrl(path) {
        let base = document.body?.dataset?.plexoraDocsUrl || DEFAULT_DOCS_URL;
        if (!base.endsWith("/")) base += "/";
        if (!path) return base;
        return `${base}docs/${String(path).replace(/^\/+|\/+$/g, "")}/`;
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function openExternal(url) {
        if (window.PlexoraDesktop) {
            window.PlexoraDesktop.openUrl(url).catch(() => {});
            return;
        }
        window.open(url, "_blank", "noopener");
    }

    function mode(info) {
        if (info?.website) return "website";
        if (window.PlexoraDesktop || info?.desktop) return "desktop app";
        if (window.flaskVariables?.notebook_mode || window.self !== window.top) return "notebook";
        return "browser";
    }

    //: On the website there is no installed Plexora to describe
    //: (`/desktop/info` is the desktop's, routes.py HANDOFF): what the site
    //: knows -- the version its loader started (services/shell.js) and the
    //: plan this browser holds.
    function siteInfo() {
        if (window.PlexoraBackend?.kind !== "browser") return null;
        let site = null;
        try {
            site = window.parent?.PlexoraSite || window.PlexoraSite || null;
        } catch (error) { /* another origin */ }
        const plan = window.flaskVariables?.license?.plan || "free";
        return { website: true, version: site?.version || "",
                 plan: plan.charAt(0).toUpperCase() + plan.slice(1) };
    }

    async function info() {
        const site = siteInfo();
        if (site) return site;
        try {
            const response = await fetch(plexoraUrl("desktop/info"), { cache: "no-store" });
            if (response.ok) return await response.json();
        } catch (error) {
            // Drawn as "unknown" below; About must open even when this fails.
        }
        return {};
    }

    /** [label, value] rows. `withPaths` false for anything that leaves the
     *  machine. */
    function facts(data, { withPaths }) {
        const rows = [
            ["Plexora", data.version || "unknown"],
            ["Plan", data.plan || "Free"],
            ["Running as", mode(data)],
        ];
        if (window.PlexoraDesktop?.version) rows.push(["Desktop shell", window.PlexoraDesktop.version]);
        rows.push(["Python", data.python || "unknown"]);
        rows.push(["Platform", data.platform || navigator.platform || "unknown"]);
        rows.push(["Browser", navigator.userAgent]);
        rows.push(["Plugins", (data.plugins || []).join(", ") || "none"]);
        if (withPaths) {
            rows.push(["Data folder", data.data_root || ""]);
            rows.push(["Settings", data.settings_path || ""]);
            rows.push(["Python executable", data.executable || ""]);
            if (data.log_path) rows.push(["Log", data.log_path]);
        }
        return rows.filter(([, value]) => value);
    }

    function asText(rows) {
        return rows.map(([label, value]) => `${label}: ${value}`).join("\n");
    }

    function table(rows) {
        const node = el("table", "plx-about-table");
        rows.forEach(([label, value]) => {
            const tr = el("tr");
            tr.appendChild(el("th", null, label));
            tr.appendChild(el("td", null, value));
            node.appendChild(tr);
        });
        return node;
    }

    async function about() {
        const data = await info();
        const rows = facts(data, { withPaths: true });
        const content = el("div", "plx-about");
        const head = el("div", "plx-about-head");
        const mark = el("img", "plx-about-mark");
        mark.src = plexoraUrl("client/src/img/logo.svg");
        mark.alt = "";
        head.appendChild(mark);
        const words = el("div");
        words.appendChild(el("div", "plx-about-name", "Plexora"));
        words.appendChild(el("div", "plx-about-version", `Version ${data.version || "unknown"}`));
        head.appendChild(words);
        content.appendChild(head);
        // The version is the heading's; the table starts with what is not.
        content.appendChild(table(rows.slice(1)));
        content.appendChild(el("p", "plx-about-legal",
            "Plexora Free is free for any use; Plexora Pro is sold per seat. © BioCognia."));

        const answer = await window.PlexoraConfirm.choose({
            title: "About Plexora",
            content,
            choices: [
                { value: "copy", label: "Copy Diagnostics" },
                { value: "updates", label: "Check for Updates…" },
                { value: "done", label: "Done", kind: "primary", focus: true },
            ],
        });
        if (answer === "copy") {
            try {
                await navigator.clipboard.writeText(asText(rows));
                window.PlexoraToast?.show({ title: "Diagnostics copied", timeout: 3000 });
            } catch (error) {
                window.PlexoraToast?.show({ title: "Could not copy", note: asText(rows),
                                            tone: "warning" });
            }
        } else if (answer === "updates") {
            window.PlexoraUpdates?.open();
        }
    }

    /** Help > Report a Bug: the feedback form, as a bug report. */
    function reportIssue() {
        window.PlexoraFeedback?.open("bug");
    }

    /** Help > Request a Feature: the same form, as a feature request. */
    function requestFeature() {
        window.PlexoraFeedback?.open("feature");
    }

    // -- keyboard shortcuts --------------------------------------------------

    /** Which menu a row is in, by its dropdown's toggle text. */
    function groupOf(element) {
        const menu = element.closest(".dropdown-menu");
        const toggleId = menu?.getAttribute("aria-labelledby");
        const toggle = toggleId ? document.getElementById(toggleId) : null;
        const name = (toggle?.firstChild?.textContent || toggle?.textContent || "").trim();
        return name || "Page";
    }

    function labelOf(element) {
        return (element.querySelector(".nav-item-label")?.textContent
                || element.getAttribute("aria-label") || element.title
                || element.textContent || "").replace(/\s+/g, " ").trim();
    }

    /**
     * Every key bound on this page, grouped by the menu it is in.
     *
     * Read off the rows' painted keys rather than their `data-shortcut`: a
     * chord that lost a clash keeps its attribute and prints nothing, and a
     * sheet that listed it would promise a key that does something else.
     */
    function collect() {
        const groups = new Map();
        const seen = new Set();
        document.querySelectorAll("[data-shortcut]").forEach((element) => {
            const printed = element.querySelector(".nav-item-key")?.textContent?.trim()
                || (element.closest("#topBar") ? "" : window.PlexoraShortcuts?.format(element.dataset.shortcut));
            if (!printed || seen.has(printed)) return;
            seen.add(printed);
            const group = groupOf(element);
            if (!groups.has(group)) groups.set(group, []);
            groups.get(group).push([printed, labelOf(element)]);
        });
        return groups;
    }

    function cap(text) {
        const kbd = el("kbd", "plx-kbd", text);
        return kbd;
    }

    function shortcuts() {
        const groups = collect();
        const content = el("div", "plx-shortcuts");
        if (!groups.size) {
            content.appendChild(el("p", "plx-confirm-body", "No shortcuts are bound on this page."));
        }
        groups.forEach((rows, group) => {
            const section = el("section", "plx-shortcuts-group");
            section.appendChild(el("h3", "plx-shortcuts-head", group));
            const tbl = el("table", "plx-tool-help-shortcuts");
            rows.forEach(([key, label]) => {
                const tr = el("tr");
                const keyCell = el("td", "plx-tool-help-keys");
                keyCell.appendChild(cap(key));
                tr.appendChild(keyCell);
                tr.appendChild(el("td", "plx-tool-help-label", label));
                tbl.appendChild(tr);
            });
            section.appendChild(tbl);
            content.appendChild(section);
        });
        content.appendChild(el("p", "plx-shortcuts-foot",
            "Each open tool lists its own keys under the ? in its header."));
        return window.PlexoraConfirm.tell({ title: "Keyboard Shortcuts", content, confirm: "Done" });
    }

    function onReady(fn) {
        if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
        else fn();
    }

    onReady(() => {
        document.getElementById("nav_about")?.addEventListener("click", () => { about(); });
        document.getElementById("nav_report_issue")?.addEventListener("click", () => { reportIssue(); });
        document.getElementById("nav_request_feature")?.addEventListener("click", () => { requestFeature(); });
        document.getElementById("nav_docs")?.addEventListener("click", () => openExternal(docsUrl()));
        document.getElementById("nav_shortcuts")?.addEventListener("click", () => { shortcuts(); });
    });

    return { about, reportIssue, requestFeature, shortcuts, docsUrl, info, facts,
             openDocs: (path) => openExternal(docsUrl(path)) };
})();
