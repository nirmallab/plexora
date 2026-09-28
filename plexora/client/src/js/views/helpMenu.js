/**
 * helpMenu.js -- the Help menu's rows other than Check for Updates.
 *
 * About, Keyboard Shortcuts, Documentation and Report an Issue. All four are
 * built on PlexoraConfirm.tell with a content node, like pluginHelp.js, so
 * they open, trap focus and close the way every other core dialog does.
 *
 * About reads `/desktop/info`, which answers in every mode despite its name
 * (desktop_routes.py) and never carries the token or the port. The same
 * document, minus the paths, is what Report an Issue puts in the new issue's
 * body: a GitHub issue is public, and a data root is usually a home directory
 * with somebody's name in it.
 */
window.PlexoraHelpMenu = (function () {
    "use strict";

    const REPO = "https://github.com/nirmallab/plexora";
    const ISSUES_URL = `${REPO}/issues/new`;
    //: Fallback only. The page renders `plexora.links.DOCS_URL` into
    //: <body data-plexora-docs-url>, so the site's address has one home.
    const DEFAULT_DOCS_URL = "https://nirmallab.github.io/plexora/";

    /** The documentation site, or one page of it: `path` is relative to the
     *  site's /docs/ root ("plugins/roi"). */
    function docsUrl(path) {
        let base = document.body?.dataset?.plexoraDocsUrl || DEFAULT_DOCS_URL;
        if (!base.endsWith("/")) base += "/";
        if (!path) return base;
        return `${base}docs/${String(path).replace(/^\/+|\/+$/g, "")}/`;
    }
    //: GitHub refuses a URL much past 8 KB; the body is kept well inside it.
    const ISSUE_BODY_LIMIT = 5000;

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
        if (window.PlexoraDesktop || info?.desktop) return "desktop app";
        if (window.flaskVariables?.notebook_mode || window.self !== window.top) return "notebook";
        return "browser";
    }

    async function info() {
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
            "Free for academic and noncommercial use. © Nirmal Lab."));

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

    async function reportIssue() {
        const data = await info();
        const environment = asText(facts(data, { withPaths: false }));
        let body = "### What happened\n\n\n\n### What you expected\n\n\n\n"
                   + "### Steps to reproduce\n\n1. \n\n"
                   + "### Environment\n\n```\n" + environment + "\n```\n";
        if (body.length > ISSUE_BODY_LIMIT) body = body.slice(0, ISSUE_BODY_LIMIT);
        const go = await window.PlexoraConfirm.choose({
            title: "Report an Issue",
            body: "This opens a new issue on GitHub with your Plexora version, Python, "
                  + "platform and plugins filled in. No file paths or data are included.\n\n"
                  + "A screenshot, and the steps that led to the problem, help most.",
            choices: [
                { value: false, label: "Cancel" },
                { value: true, label: "Open GitHub", kind: "primary", focus: true },
            ],
        });
        if (!go) return;
        openExternal(`${ISSUES_URL}?body=${encodeURIComponent(body)}`);
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
        document.getElementById("nav_docs")?.addEventListener("click", () => openExternal(docsUrl()));
        document.getElementById("nav_shortcuts")?.addEventListener("click", () => { shortcuts(); });
    });

    return { about, reportIssue, shortcuts, docsUrl, openDocs: (path) => openExternal(docsUrl(path)) };
})();
