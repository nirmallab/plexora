/**
 * updateDialog.js -- Help > Check for Updates.
 *
 * One dialog, three ways of getting a new version, chosen by where the page
 * is running rather than by anything the user picks:
 *
 * - A browser tab or a notebook iframe of a pip-installed server asks the
 *   server (`/update/*`, update_routes.py). The server runs pip, then stops
 *   and starts itself again on the same port; this page waits for `/health`
 *   to answer with the new version in its header and reloads.
 * - The desktop app asks its shell (PlexoraDesktop.checkUpdate), which
 *   downloads the signed bundle, replaces the app and relaunches it. There is
 *   no reload to do here: the window goes away with the old app.
 * - Anything that cannot update in place -- a source checkout, a read-only
 *   environment, a container, a desktop build made without the update key --
 *   gets the exact command or the release page instead of a button that
 *   would fail.
 *
 * A state machine drawn into one body element: every state replaces the
 * body, so no state can leave a stray button behind for the next one. The
 * dialog is a native <dialog> in the shape of importHelp.js's (head, close
 * button, body), and its CSS is in main.css for the reason that file gives.
 *
 * Also owns the once-a-day background check and the dot it puts on Help.
 * The check never opens anything: finding an update is not a reason to
 * interrupt someone who is looking at an image.
 */
window.PlexoraUpdates = (function () {
    "use strict";

    //: How often the dialog reads the pip log while an install runs.
    const POLL_MS = 1000;
    //: How long a restart may take before the dialog stops waiting. A cold
    //: start imports the whole scientific stack, which on a network home
    //: directory can take most of a minute.
    const RESTART_TIMEOUT_MS = 90000;
    //: The background check waits this long after load, so it never competes
    //: with the first tiles.
    const AUTO_CHECK_DELAY_MS = 8000;
    //: Desktop only (the server throttles its own check): the last time the
    //: shell was asked, per browser profile.
    const DESKTOP_CHECK_KEY = "plexora.updates.desktopLastCheck";
    const DAY_MS = 24 * 3600 * 1000;

    let dialog = null;
    let body = null;
    let pollTimer = null;
    let restarting = false;
    let lastInfo = null;

    // -- small DOM helpers ----------------------------------------------------

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function icon(name) {
        const node = el("span", `fas ${name}`);
        node.setAttribute("aria-hidden", "true");
        return node;
    }

    function button(label, { kind, onClick, iconName } = {}) {
        const node = el("button", `plx-button${kind ? ` plx-button-${kind}` : ""}`);
        node.type = "button";
        if (iconName) node.appendChild(icon(iconName));
        node.appendChild(document.createTextNode(label));
        if (onClick) node.addEventListener("click", onClick);
        return node;
    }

    function actions(...buttons) {
        const row = el("div", "plx-dialog-actions");
        buttons.filter(Boolean).forEach((b) => row.appendChild(b));
        return row;
    }

    function versionPair(current, latest) {
        const row = el("div", "plx-update-versions");
        const from = el("div", "plx-update-version");
        from.appendChild(el("span", "plx-update-version-label", "Installed"));
        from.appendChild(el("span", "plx-update-version-value", current || "unknown"));
        row.appendChild(from);
        if (latest) {
            row.appendChild(icon("fa-arrow-right plx-update-arrow"));
            const to = el("div", "plx-update-version is-new");
            to.appendChild(el("span", "plx-update-version-label", "Available"));
            to.appendChild(el("span", "plx-update-version-value", latest));
            row.appendChild(to);
        }
        return row;
    }

    function status(iconName, title, note, tone) {
        const box = el("div", `plx-update-status${tone ? ` is-${tone}` : ""}`);
        box.appendChild(icon(`${iconName} plx-update-status-icon`));
        const words = el("div", "plx-update-status-words");
        words.appendChild(el("div", "plx-update-status-title", title));
        if (note) words.appendChild(el("div", "plx-update-status-note", note));
        box.appendChild(words);
        return box;
    }

    /** Text a user might want to paste somewhere: a box and a Copy button. */
    function copyBox(text, { multiline = false } = {}) {
        const wrap = el("div", "plx-update-copy");
        const code = el(multiline ? "pre" : "code", "plx-update-copy-text", text);
        wrap.appendChild(code);
        const copy = el("button", "plx-update-copy-button");
        copy.type = "button";
        copy.title = "Copy";
        copy.setAttribute("aria-label", "Copy");
        copy.appendChild(icon("fa-copy"));
        copy.addEventListener("click", async () => {
            try {
                await navigator.clipboard.writeText(text);
                copy.replaceChildren(icon("fa-check"));
                setTimeout(() => copy.replaceChildren(icon("fa-copy")), 1500);
            } catch (error) {
                // Selecting it is the next best thing: Cmd+C still works.
                const range = document.createRange();
                range.selectNodeContents(code);
                const selection = window.getSelection();
                selection.removeAllRanges();
                selection.addRange(range);
            }
        });
        wrap.appendChild(copy);
        return wrap;
    }

    /** The inline markdown a release body uses, as plain text: links keep
     *  their words, emphasis and code lose their markers. */
    function plainInline(text) {
        return String(text)
            .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
            .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
            .replace(/(\*\*|__)(.+?)\1/g, "$2")
            .replace(/`([^`]+)`/g, "$1")
            .trim();
    }

    /**
     * A GitHub release body, drawn as headings, bullets and paragraphs.
     *
     * Built node by node with textContent -- the notes come from the network,
     * and nothing from the network is ever parsed as HTML here. Only the
     * three shapes a release body is written in are recognised; anything else
     * is a paragraph, which is still readable.
     */
    function renderNotes(markdown) {
        const box = el("div", "plx-update-notes-text");
        let list = null;
        String(markdown).replace(/\r\n?/g, "\n").split("\n").forEach((raw) => {
            const line = raw.trimEnd();
            const heading = line.match(/^#{1,6}\s+(.*)$/);
            const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
            if (!line.trim()) { list = null; return; }
            if (heading) {
                list = null;
                box.appendChild(el("div", "plx-update-notes-h", plainInline(heading[1])));
            } else if (bullet) {
                if (!list) { list = el("ul", "plx-update-notes-list"); box.appendChild(list); }
                list.appendChild(el("li", null, plainInline(bullet[1])));
            } else {
                list = null;
                box.appendChild(el("p", "plx-update-notes-p", plainInline(line)));
            }
        });
        return box;
    }

    /** Release notes, and the way to the full page. */
    function notesBlock(notes, url) {
        const box = el("div", "plx-update-notes");
        if (notes) {
            box.appendChild(el("div", "plx-update-notes-head", "What's new"));
            box.appendChild(renderNotes(notes));
        }
        if (url) {
            const link = el("a", "plx-update-notes-link", notes ? "Full release notes" : "Release notes");
            link.href = url;
            link.target = "_blank";
            link.rel = "noopener";
            link.appendChild(icon("fa-arrow-up-right-from-square"));
            link.addEventListener("click", (event) => {
                if (window.PlexoraDesktop) {
                    event.preventDefault();
                    window.PlexoraDesktop.openUrl(url).catch(() => {});
                }
            });
            box.appendChild(link);
        }
        return box.childNodes.length ? box : null;
    }

    function relativeTime(iso) {
        if (!iso) return "";
        const then = Date.parse(iso);
        if (Number.isNaN(then)) return "";
        const minutes = Math.round((Date.now() - then) / 60000);
        if (minutes < 1) return "just now";
        if (minutes < 60) return `${minutes} min ago`;
        const hours = Math.round(minutes / 60);
        if (hours < 24) return `${hours} h ago`;
        return new Date(then).toLocaleDateString();
    }

    // -- the dialog -------------------------------------------------------------

    function build() {
        const node = el("dialog", "plx-dialog plx-help plx-update");
        node.setAttribute("aria-label", "Software update");
        const head = el("div", "plx-help-head");
        head.appendChild(el("h2", "plx-dialog-title", "Software Update"));
        const closer = el("button", "plx-picker-close");
        closer.type = "button";
        closer.title = "Close";
        closer.setAttribute("aria-label", "Close");
        closer.appendChild(icon("fa-xmark"));
        closer.addEventListener("click", close);
        head.appendChild(closer);
        node.appendChild(head);
        body = el("div", "plx-update-body");
        node.appendChild(body);
        // Escape and the close button both land here. A restart in progress
        // is not stopped by closing: the server is already on its way down,
        // so the wait carries on and the page still reloads.
        node.addEventListener("close", () => {
            if (!restarting) stopPolling();
            node.remove();
            if (dialog === node) dialog = null;
        });
        return node;
    }

    function show(...children) {
        if (!body) return;
        body.replaceChildren(...children.filter(Boolean));
    }

    function close() {
        if (dialog && dialog.open) dialog.close();
    }

    function stopPolling() {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = null;
    }

    async function open() {
        if (dialog) {
            if (!dialog.open) dialog.showModal();
            return;
        }
        dialog = build();
        document.body.appendChild(dialog);
        if (typeof dialog.showModal === "function") dialog.showModal();
        await check(true);
    }

    // -- talking to whichever updater applies -------------------------------

    async function getJSON(path, options) {
        const response = await fetch(plexoraUrl(path), { cache: "no-store", ...(options || {}) });
        let payload = {};
        try { payload = await response.json(); } catch (error) { payload = {}; }
        return { ok: response.ok, status: response.status, payload };
    }

    function postJSON(path, data) {
        return getJSON(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data || {}),
        });
    }

    function savePrefs(changes) {
        return postJSON("update/prefs", changes).catch(() => null);
    }

    async function check(force) {
        show(status("fa-spinner fa-spin", "Checking for updates…"));
        let info;
        try {
            const reply = await getJSON(`update/check${force ? "?force=1" : ""}`);
            info = reply.payload;
        } catch (error) {
            info = { error: "Could not reach the Plexora server." };
        }
        lastInfo = info;

        if (info.mode === "desktop") {
            if (window.PlexoraDesktop) return checkDesktop(info);
            return renderFromBrowserOfApp(info);
        }
        if (info.error) return renderError(info.error);
        setBadge(Boolean(info.available && !info.skipped));

        const job = info.job;
        if (job && job.state === "running") return renderInstalling(info, job.version, false);
        if (job && job.state === "done" && job.installed === job.version
                && job.version !== info.current) {
            return renderInstalled(info, job.version);
        }
        if (!info.available) return renderUpToDate(info);
        if (!info.can_install) return renderManual(info);
        return renderAvailable(info);
    }

    // -- states: the pip server ------------------------------------------------

    function kernelNote(info) {
        if (info.mode !== "notebook") return null;
        return el("p", "plx-update-hint",
            "The viewer reloads on its own. Restart your notebook kernel as "
            + "well, so that Python code using Plexora picks up the new "
            + "version too.");
    }

    function busyNote(info) {
        if (!info.busy || !info.busy.length) return null;
        const box = status("fa-triangle-exclamation", "Work in progress",
            `Restarting stops it: ${info.busy.slice(0, 3).join("; ")}`
            + (info.busy.length > 3 ? ` and ${info.busy.length - 3} more.` : "."), "warning");
        return box;
    }

    function autoCheckToggle(info) {
        const label = el("label", "plx-update-auto");
        const box = el("input");
        box.type = "checkbox";
        box.checked = info.auto_check !== false;
        box.addEventListener("change", () => {
            savePrefs({ auto_check: box.checked });
            if (!box.checked) setBadge(false);
        });
        label.appendChild(box);
        label.appendChild(el("span", null, "Check for updates automatically"));
        return label;
    }

    function renderError(message) {
        show(status("fa-cloud-bolt", "Couldn't check for updates", message, "warning"),
             actions(button("Close", { onClick: close }),
                     button("Try Again", { kind: "primary", onClick: () => check(true) })));
    }

    function renderUpToDate(info) {
        const when = relativeTime(info.last_checked);
        show(status("fa-circle-check", "Plexora is up to date",
                    `Version ${info.current} is the newest release${when ? ` (checked ${when})` : ""}.`,
                    "success"),
             autoCheckToggle(info),
             actions(button("Check Again", { onClick: () => check(true) }),
                     button("Done", { kind: "primary", onClick: close })));
    }

    function renderAvailable(info) {
        const skip = button("Skip This Version", {
            onClick: async () => {
                await savePrefs({ skipped_version: info.latest });
                setBadge(false);
                close();
            },
        });
        const later = button("Later", { onClick: close });
        const go = button(info.can_restart ? "Update and Restart" : "Install Update", {
            kind: "primary", iconName: "fa-download", onClick: () => install(info),
        });
        show(status("fa-gift", `Plexora ${info.latest} is available`,
                    info.skipped ? "You chose to skip this version earlier." : null),
             versionPair(info.current, info.latest),
             notesBlock(info.notes, info.notes_url),
             busyNote(info),
             kernelNote(info),
             autoCheckToggle(info),
             actions(skip, later, go));
        go.focus();
    }

    function renderManual(info) {
        show(status("fa-gift", `Plexora ${info.latest} is available`, info.reason),
             versionPair(info.current, info.latest),
             notesBlock(info.notes, info.notes_url),
             info.command ? el("p", "plx-update-hint", "To update, run:") : null,
             info.command ? copyBox(info.command) : null,
             autoCheckToggle(info),
             actions(info.kind === "editable" ? null : button("Skip This Version", {
                         onClick: async () => {
                             await savePrefs({ skipped_version: info.latest });
                             setBadge(false);
                             close();
                         },
                     }),
                     button("Done", { kind: "primary", onClick: close })));
    }

    function renderFromBrowserOfApp(info) {
        show(status("fa-desktop", "Update from the Plexora app",
                    "This tab belongs to the Plexora desktop app. Choose Help › "
                    + "Check for Updates in the app's own window; the tab keeps "
                    + "working and reloads when the app comes back."),
             versionPair(info.current, null),
             actions(button("Done", { kind: "primary", onClick: close })));
    }

    function logView(lines) {
        const pre = el("pre", "plx-update-log");
        pre.textContent = (lines || []).join("\n");
        return pre;
    }

    async function install(info) {
        restarting = false;
        const reply = await postJSON("update/install", { version: info.latest }).catch(() => null);
        if (!reply || !reply.ok) {
            const message = reply?.payload?.error || "The server did not start the install.";
            return renderFailed(message, [], () => check(true));
        }
        renderInstalling(info, info.latest, true);
    }

    /** `andRestart`: this dialog's own Update and Restart, which restarts as
     *  soon as pip is done. A dialog reopened on a job started earlier only
     *  offers the restart -- that click may have been "Restart Later". */
    function renderInstalling(info, version, andRestart) {
        const log = logView([]);
        const bar = el("div", "plx-update-bar is-indeterminate");
        bar.appendChild(el("div", "plx-update-bar-fill"));
        show(status("fa-spinner fa-spin", `Installing Plexora ${version}…`,
                    "pip is downloading and installing the new version. Plexora "
                    + "keeps working until it restarts."),
             bar, log);
        const poll = async () => {
            let view;
            try {
                view = (await getJSON("update/install/status")).payload;
            } catch (error) {
                pollTimer = setTimeout(poll, POLL_MS);
                return;
            }
            log.textContent = (view.log_tail || []).join("\n");
            log.scrollTop = log.scrollHeight;
            if (view.state === "running") {
                pollTimer = setTimeout(poll, POLL_MS);
                return;
            }
            pollTimer = null;
            if (view.state === "done") return renderInstalled(info, version, andRestart);
            renderFailed("pip could not install the update. Nothing was changed "
                         + "that a restart would pick up.", view.log_tail,
                         () => check(true));
        };
        stopPolling();
        pollTimer = setTimeout(poll, 300);
    }

    function renderInstalled(info, version, andRestart) {
        if (!info.can_restart) {
            show(status("fa-circle-check", `Plexora ${version} is installed`,
                        "Stop Plexora and start it again to switch to the new "
                        + "version. This server was not started by a Plexora "
                        + "launcher, so it cannot restart itself.", "success"),
                 kernelNote(info),
                 actions(button("Done", { kind: "primary", onClick: close })));
            return;
        }
        const now = button("Restart Now", { kind: "primary", iconName: "fa-rotate-right",
                                            onClick: () => restart(info, version, false) });
        show(status("fa-circle-check", `Plexora ${version} is installed`,
                    "Restart to finish. This page reloads by itself when the new "
                    + "version is running.", "success"),
             busyNote(info),
             kernelNote(info),
             actions(button("Restart Later", { onClick: close }), now));
        now.focus();
        // Straight on unless something is running: the button said "Update
        // and Restart", and asking again would be asking twice.
        if (andRestart && !(info.busy && info.busy.length)) restart(info, version, false);
    }

    async function restart(info, version, force) {
        const reply = await postJSON("update/restart", { force }).catch(() => null);
        if (reply && reply.status === 409 && reply.payload?.busy) {
            const go = await window.PlexoraConfirm.ask({
                title: "Stop work in progress?",
                body: `Restarting now stops: ${reply.payload.busy.join("; ")}.\n\n`
                      + "It can be started again afterwards.",
                confirm: "Restart Anyway",
            });
            if (go) return restart(info, version, true);
            return;
        }
        if (!reply || !reply.ok) {
            return renderFailed(reply?.payload?.error || "The server did not restart.", [],
                                () => check(true));
        }
        waitForVersion(version, info);
    }

    function waitForVersion(version, info) {
        restarting = true;
        const bar = el("div", "plx-update-bar is-indeterminate");
        bar.appendChild(el("div", "plx-update-bar-fill"));
        show(status("fa-rotate-right fa-spin", "Restarting Plexora…",
                    `Waiting for version ${version} to come up.`),
             bar, kernelNote(info));
        const started = Date.now();
        const poll = async () => {
            let answered = null;
            try {
                const response = await fetch(plexoraUrl("health"), { cache: "no-store" });
                if (response.ok) answered = response.headers.get("X-Plexora-Version");
            } catch (error) {
                // Expected while it is down.
            }
            if (answered && answered === version) {
                window.location.reload();
                return;
            }
            if (Date.now() - started > RESTART_TIMEOUT_MS) {
                restarting = false;
                const came = answered && answered !== info.current;
                renderFailed(came
                    ? `Plexora came back as ${answered} rather than ${version}.`
                    : "Plexora did not come back within 90 seconds. Start it again "
                      + "from a terminal; the new version is installed.", [], null);
                return;
            }
            pollTimer = setTimeout(poll, POLL_MS);
        };
        stopPolling();
        pollTimer = setTimeout(poll, 1500);
    }

    function renderFailed(message, lines, retry) {
        restarting = false;
        const text = (lines || []).join("\n");
        show(status("fa-circle-xmark", "The update did not finish", message, "danger"),
             text ? copyBox(text, { multiline: true }) : null,
             actions(button("Close", { onClick: close }),
                     retry ? button("Try Again", { kind: "primary", onClick: retry }) : null));
    }

    // -- states: the desktop shell -----------------------------------------------

    async function checkDesktop(prefs) {
        let info;
        try {
            info = await window.PlexoraDesktop.checkUpdate();
        } catch (error) {
            return renderError(String(error?.message || error || "The update server did not answer."));
        }
        rememberDesktopCheck();
        const skipped = info.available && prefs?.skipped_version === info.latest;
        setBadge(Boolean(info.available && !skipped));
        const merged = { ...info, auto_check: prefs?.auto_check !== false, mode: "desktop" };
        if (info.kind === "unsupported") return renderDesktopUnsupported(merged);
        if (!info.available) {
            return renderUpToDate({ ...merged, last_checked: new Date().toISOString() });
        }
        const go = button("Update and Relaunch", { kind: "primary", iconName: "fa-download",
                                                   onClick: () => installDesktop(merged) });
        const linux = window.PlexoraDesktop.platform === "linux";
        show(status("fa-gift", `Plexora ${info.latest} is available`,
                    linux ? "Installing asks for your password, as installing any "
                            + "system package does." : null),
             versionPair(info.current, info.latest),
             notesBlock(info.notes, info.url),
             autoCheckToggle(merged),
             actions(button("Skip This Version", {
                         onClick: async () => {
                             await savePrefs({ skipped_version: info.latest });
                             setBadge(false);
                             close();
                         },
                     }),
                     button("Later", { onClick: close }), go));
        go.focus();
    }

    /** A build with no update key -- made locally, or before signing was
     *  set up. It cannot verify a download, so it does not offer one. */
    function renderDesktopUnsupported(info) {
        const open = button("Open Releases", {
            kind: "primary", iconName: "fa-arrow-up-right-from-square",
            onClick: () => window.PlexoraDesktop.openUrl(info.url).catch(() => {}),
        });
        show(status("fa-circle-info", "This copy of Plexora cannot update itself",
                    "It was built without the key that signs updates, so it has no "
                    + "way to check a download is genuine. Newer versions are on the "
                    + "release page."),
             versionPair(info.current, null),
             actions(button("Done", { onClick: close }), open));
    }

    async function installDesktop(info) {
        restarting = true;
        const bar = el("div", "plx-update-bar");
        const fill = el("div", "plx-update-bar-fill");
        bar.appendChild(fill);
        const note = el("div", "plx-update-status-note", "Starting the download…");
        const head = status("fa-spinner fa-spin", `Updating to Plexora ${info.latest}…`,
                            "Plexora stops, installs the new version and opens again.");
        head.querySelector(".plx-update-status-words").appendChild(note);
        show(head, bar);
        const stop = window.PlexoraDesktop.onUpdateProgress((p) => {
            if (p.phase === "installing") {
                note.textContent = "Installing…";
                bar.classList.add("is-indeterminate");
                return;
            }
            if (p.total) {
                const pct = Math.min(100, Math.round((p.downloaded / p.total) * 100));
                fill.style.width = `${pct}%`;
                note.textContent = `Downloaded ${(p.downloaded / 1048576).toFixed(1)} of `
                                   + `${(p.total / 1048576).toFixed(1)} MB`;
            } else {
                bar.classList.add("is-indeterminate");
                note.textContent = `Downloaded ${(p.downloaded / 1048576).toFixed(1)} MB`;
            }
        });
        try {
            await window.PlexoraDesktop.installUpdate();
        } catch (error) {
            stop();
            renderFailed(String(error?.message || error || "The update could not be installed."),
                         [], () => check(true));
        }
    }

    function rememberDesktopCheck() {
        try { localStorage.setItem(DESKTOP_CHECK_KEY, String(Date.now())); } catch (error) { /* private window */ }
    }

    function desktopCheckedRecently() {
        try {
            const at = Number(localStorage.getItem(DESKTOP_CHECK_KEY) || 0);
            return at && Date.now() - at < DAY_MS;
        } catch (error) {
            return false;
        }
    }

    // -- the badge and the background check --------------------------------------

    function setBadge(on) {
        ["nav_help_badge", "nav_update_badge"].forEach((id) => {
            const node = document.getElementById(id);
            if (node) node.hidden = !on;
        });
    }

    async function autoCheck() {
        let info;
        try {
            info = (await getJSON("update/check?auto=1")).payload;
        } catch (error) {
            return;
        }
        if (!info || info.disabled || info.error) return;
        if (info.mode === "desktop") {
            if (!window.PlexoraDesktop || desktopCheckedRecently()) return;
            try {
                const found = await window.PlexoraDesktop.checkUpdate();
                rememberDesktopCheck();
                setBadge(Boolean(found.available && found.latest !== info.skipped_version));
            } catch (error) {
                // Offline: say nothing. The menu item still works.
            }
            return;
        }
        setBadge(Boolean(info.available && !info.skipped));
    }

    /** A different version answering /health than the one this page first
     *  saw: the server was updated underneath this tab. */
    function watchServerVersion() {
        let first = null;
        let told = false;
        document.addEventListener("plexora:server-version", (event) => {
            const version = event.detail?.version;
            if (!version) return;
            if (first === null) { first = version; return; }
            if (version === first || told || restarting) return;
            told = true;
            window.PlexoraToast?.show({
                title: `Plexora was updated to ${version}`,
                note: "Reload this page to use the new version.",
                timeout: 0,
                actions: [{ label: "Reload", primary: true,
                            onSelect: () => window.location.reload() }],
            });
        });
    }

    function onReady(fn) {
        if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
        else fn();
    }

    onReady(() => {
        document.getElementById("nav_check_updates")?.addEventListener("click", () => open());
        watchServerVersion();
        // Not in an iframe that is not ours to decorate, and not on every
        // router navigation: this script runs once per real page load.
        setTimeout(() => { autoCheck().catch(() => {}); }, AUTO_CHECK_DELAY_MS);
    });

    return { open, check: () => open(), autoCheck, setBadge, savePrefs,
             get lastInfo() { return lastInfo; } };
})();
