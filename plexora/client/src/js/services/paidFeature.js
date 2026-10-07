/**
 * paidFeature.js -- the one way the page talks about a Paid feature.
 *
 *   PlexoraPaid.explain({entitlement, label, summary, state})
 *       The modal a locked feature opens: what it is, that it is part of
 *       Paid, and two ways forward -- start a trial (the trial page at
 *       account.biocognia.com, in the browser) or connect this device
 *       (Settings > License).
 *       Resolves "trial", "license" or null.
 *   PlexoraPaid.badge()      a small "Paid" tag for a menu entry
 *   PlexoraPaid.allows(ent)  what the page was told at render; a HINT for
 *                            drawing badges, never the decision -- the server
 *                            refuses a Paid action whatever the page thinks
 *   PlexoraPaid.startTrial() / goToLicense()
 *
 * Nothing here runs on a Free path. There are no banners, no nags and no
 * timers: the modal opens only when somebody asks for a Paid feature. The
 * licence itself never reaches the browser -- no certificate, key or token is
 * ever in this page or in its storage.
 */
window.PlexoraPaid = (function () {
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
                  + "If you have a licence file, install it in Settings > License.",
        });
        return false;
    }

    function goToLicense() {
        const target = plexoraUrl("settings") + "#license";
        if (window.PlexoraRouter?.go) window.PlexoraRouter.go(target);
        else window.location.href = target;
    }

    const LAPSED = {
        expired: "Your Paid licence has expired.",
        revoked: "Your Paid licence is no longer active.",
        invalid: "The licence on this machine could not be verified.",
    };

    /**
     * The locked-feature modal. `label` and `summary` come from the server's
     * refusal (the entitlement manifest), so the page never has to know what
     * a Paid feature is called.
     */
    async function explain({ label, summary, state } = {}) {
        const info = await status();
        const name = label || "This feature";
        const lines = [];
        if (summary) lines.push(summary);
        const lapsed = LAPSED[state || info.state];
        if (lapsed) lines.push(lapsed);
        lines.push(`${name} is part of Plexora Paid. Everything Free keeps working, and everything `
                   + "you have made stays yours.");
        const choices = [{ value: null, label: "Not Now" },
                         { value: "license", label: "Enter License…" }];
        if (!info.paid) choices.push({ value: "trial", label: "Start a Trial…", kind: "primary", focus: true });
        else choices[1].kind = "primary";
        const answer = await window.PlexoraConfirm.choose({
            title: `${name} is a Paid feature`,
            body: lines,
            choices,
        });
        if (answer === "trial") await startTrial();
        if (answer === "license") goToLicense();
        return answer;
    }

    function badge(text) {
        const node = document.createElement("span");
        node.className = "plx-paid-badge";
        node.textContent = text || "Paid";
        node.title = "Part of Plexora Paid";
        return node;
    }

    return { explain, badge, allows, status, forget, adopt, startTrial, goToLicense };
}());
