/**
 * proFeature.js -- the one way the page talks about a Pro feature.
 *
 * Not granted means not shown: a control whose entitlement `allows` says no
 * is never built (and a template leaves it out with `licensed()`), so on a
 * Free page nothing below ever opens. `explain`, `badge`, `startTrial` and
 * `goToLicense` are kept for the upsell the product will add later; nothing
 * calls them today. Settings uses `adopt` and `forget`.
 *
 *   PlexoraPro.explain({entitlement, label, summary, state, off_by_default})
 *       The modal a locked feature opens: what it is, that it is part of
 *       Pro, and two ways forward -- start a trial (the trial page at
 *       account.biocognia.com, in the browser) or connect this device
 *       (Settings > Account). A feature that is off by default
 *       (`off_by_default` in the server's refusal) is never called Pro and
 *       offers no trial and no price, none of which would unlock it: external
 *       MCP access is turned on for a licence by an administrator, and a
 *       third-party plugin (`plugin:<name>`, the only plugin that can be off
 *       by default) comes from its maker.
 *       Resolves "trial", "license" or null.
 *   PlexoraPro.badge()      a small "Pro" tag for a menu entry
 *   PlexoraPro.allows(ent)  what the page was told at render; a HINT for
 *                            drawing badges, never the decision -- the server
 *                            refuses a Pro action whatever the page thinks
 *   PlexoraPro.startTrial() / goToLicense()
 *
 * Nothing here runs on a Free path. There are no banners, no nags and no
 * timers: the modal opens only when somebody asks for a Pro feature. The
 * licence itself never reaches the browser -- no certificate, key or token is
 * ever in this page or in its storage.
 */
window.PlexoraPro = (function () {
    "use strict";

    let cached = null;
    let cachedAt = 0;
    const CACHE_MS = 30000;

    function status(force) {
        const fresh = cached && Date.now() - cachedAt < CACHE_MS;
        if (fresh && !force) return Promise.resolve(cached);
        return fetch(plexoraUrl("license/status"))
            .then((response) => response.json())
            .then((body) => {
                cached = body.license || {};
                cachedAt = Date.now();
                adopt(cached);
                return cached;
            })
            .catch(() => ({ plan: "free", state: "free", service_configured: false }));
    }

    function forget() {
        cached = null;
    }

    /**
     * Take a fresh licence status as the page's own. `allows` reads the
     * render-time summary in flaskVariables, and Settings opens inside the same
     * document -- so without this a licence activated there stayed unknown to
     * the AI button until a reload. Fires `plexora:license-changed`.
     */
    function adopt(license) {
        if (!license || !license.state) return;
        cached = license;
        cachedAt = Date.now();
        if (window.flaskVariables) {
            window.flaskVariables.license = {
                plan: license.plan, state: license.state, paid: Boolean(license.paid),
                entitlements: Array.from(license.entitlements || []),
            };
        }
        if (typeof window.dispatchEvent === "function" && typeof CustomEvent === "function") {
            window.dispatchEvent(new CustomEvent("plexora:license-changed", { detail: license }));
        }
    }

    /** A hint from the page's render-time licence summary. */
    function allows(entitlement) {
        if (!entitlement || entitlement === "free") return true;
        const license = window.flaskVariables?.license;
        if (!license || !license.paid) return false;
        return (license.entitlements || []).some(
            (grant) => grant === entitlement || entitlement.startsWith(grant + ":"));
    }

    function openExternal(url) {
        if (window.PlexoraDesktop) {
            window.PlexoraDesktop.openUrl(url).catch(() => {});
            return;
        }
        window.open(url, "_blank", "noopener");
    }

    async function startTrial() {
        const info = await status(true);
        if (info.trial_url) {
            openExternal(info.trial_url);
            return true;
        }
        await window.PlexoraConfirm.tell({
            title: "Trials are not available here",
            body: info.offline_only
                ? "BIOCOGNIA_OFFLINE is set, so Plexora makes no licensing network call. "
                  + "Ask whoever manages your licence for an offline licence file instead."
                : "This Plexora build has no BioCognia platform configured. "
                  + "If you have a licence file, install it in Settings > Account.",
        });
        return false;
    }

    function goToLicense() {
        const target = plexoraUrl("settings") + "#license";
        if (window.PlexoraRouter?.go) window.PlexoraRouter.go(target);
        else window.location.href = target;
    }

    const LAPSED = {
        expired: "Your Pro licence has expired.",
        revoked: "Your Pro licence is no longer active.",
        invalid: "The licence on this machine could not be verified.",
    };

    /**
     * The locked-feature modal. `label` and `summary` come from the server's
     * refusal (the entitlement manifest), so the page never has to know what
     * a Pro feature is called.
     */
    async function explain({ entitlement, label, summary, state, off_by_default: off } = {}) {
        const info = await status();
        const name = label || "This feature";
        // Off by default and a plugin: a third-party one, from its maker.
        const fromMaker = Boolean(off) && String(entitlement || "").startsWith("plugin:");
        const lines = [];
        if (summary) lines.push(summary);
        const lapsed = !off && LAPSED[state || info.state];
        if (lapsed) lines.push(lapsed);
        let why = `${name} is part of Plexora Pro. `;
        if (fromMaker) {
            why = `${name} is a plugin from its maker: it is not part of Plexora Pro or its trial, `
                + "and its maker licenses it. ";
        } else if (off) {
            why = `${name} is off by default and is not part of Plexora Pro or its trial; `
                + "an administrator can turn it on for your licence. ";
        }
        lines.push(why + "Everything Free keeps working, and everything you have made stays yours.");
        const choices = [{ value: null, label: "Not Now" },
                         { value: "license", label: "Enter License…" }];
        if (!info.paid && !off) {
            choices.push({ value: "trial", label: "Start a Trial…", kind: "primary", focus: true });
        } else choices[1].kind = "primary";
        let title = `${name} is a Pro feature`;
        if (fromMaker) title = `${name} comes from its maker`;
        else if (off) title = `${name} is off by default`;
        const answer = await window.PlexoraConfirm.choose({
            title,
            body: lines,
            choices,
        });
        if (answer === "trial") await startTrial();
        if (answer === "license") goToLicense();
        return answer;
    }

    function badge(text) {
        const node = document.createElement("span");
        node.className = "plx-pro-badge";
        node.textContent = text || "Pro";
        node.title = "Part of Plexora Pro";
        return node;
    }

    return { explain, badge, allows, status, forget, adopt, startTrial, goToLicense };
}());
