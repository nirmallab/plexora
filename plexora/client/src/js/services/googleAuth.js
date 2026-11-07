/**
 * PlexoraGoogleAuth -- the website's Google sign-in, for Google Cloud.
 *
 * The desktop asks the Google Cloud CLI; a web page cannot run it, so on the
 * website the page signs in with Google itself and Plexora's Python (in the
 * page's worker, plexora/gcloud_rest.py) calls Google's JSON APIs with the
 * access token. This file holds that token, and only this file.
 *
 * **The token model, held in memory only.** An access token for about an
 * hour and no refresh token: when it lapses Google answers 401 and the form
 * asks for the sign-in again (hourly re-consent, plan L9). It lives in this
 * closure, in the tab's top document (services/shell.js loads this file in
 * the host and hands it to each page's frame), and goes nowhere but into the
 * `X-Plexora-Google-Token` header of the page's own Google Cloud requests to
 * its worker (`sign`, called by services/backend.js). Never localStorage,
 * sessionStorage, IndexedDB, a cookie or a URL; a reload forgets it.
 *
 * **A popup that comes back to this site, not Google Identity Services'
 * script.** GIS's token client answers its popup through `window.opener`,
 * which the site's `Cross-Origin-Opener-Policy: same-origin` severs -- and
 * the site needs that header for cross-origin isolation (SharedArrayBuffer).
 * So the same request is made as GIS's token client makes it -- Google's
 * authorization endpoint, `response_type=token` -- in a popup whose
 * `redirect_uri` is this site's own `google-auth.html`
 * (services/googleAuthReturn.js), which reads the token out of its fragment,
 * clears it from its address, hands it here on a BroadcastChannel (same
 * origin, so it crosses the severed opener) and closes. `state` ties the
 * answer to the request.
 *
 * Configured from shell.json's `google` (plexora/client/static_build.py
 * `--google-client-id`, `--google-auth` for a stand-in): no client id, no
 * sign-in, and the form says so. The scopes are gcloud_rest.SCOPES.
 *
 *   configure({client_id, auth})  set by the loader
 *   configured()                   whether a client id is set
 *   signIn(opener, {prompt})       a promise of {account?, expiresAt}; call it
 *                                  inside the click (it opens the popup)
 *   token()                        the token, or "" (none, or expired)
 *   signOut()                      forget it
 *   sign(request, route)           the request with the header, for a Google
 *                                  Cloud route and a held token
 */
(function (global) {
    "use strict";

    const AUTH = "https://accounts.google.com/o/oauth2/v2/auth";
    //: plexora/gcloud_rest.py SCOPES, in the same order.
    const SCOPES = [
        "openid",
        "email",
        "https://www.googleapis.com/auth/compute",
        "https://www.googleapis.com/auth/devstorage.read_only",
        "https://www.googleapis.com/auth/cloudplatformprojects.readonly",
    ];
    const HEADER = "X-Plexora-Google-Token";
    const CHANNEL = "plexora-google-auth";
    //: How long a sign-in may take before the form stops waiting for it.
    const WAIT_MS = 5 * 60 * 1000;
    //: A token is treated as gone this long before Google says it is.
    const MARGIN_MS = 60 * 1000;
    //: The routes whose requests carry the token: the Google Cloud form's
    //: lookups, a saved profile's VM verbs, and saving that profile (which
    //: asks Google for a zone).
    const ROUTES = /^\/settings\/(?:gcloud\/|remotes\/[^/]+\/vm(?:\/|$)|recipes\/gcloud$)/;

    let config = { client_id: "", auth: AUTH };
    let held = null;  // {token, expiresAt, scope}
    let pending = null;  // {state, resolve, reject, timer}
    let channel = null;

    function configure(options) {
        const given = options || {};
        config = {
            client_id: String(given.client_id || ""),
            auth: String(given.auth || AUTH),
        };
        listen();
    }

    function configured() {
        return Boolean(config.client_id);
    }

    //: The page Google sends the person back to: this site's own, at its base.
    function redirectUri() {
        const base = global.PLEXORA_SITE_BASE || "";
        return new URL(base + "/google-auth.html", global.location.origin).href;
    }

    function listen() {
        if (channel || typeof BroadcastChannel !== "function") return;
        channel = new BroadcastChannel(CHANNEL);
        channel.onmessage = (event) => answered(event.data || {});
    }

    function answered(data) {
        if (!pending || data.state !== pending.state) return;
        const { resolve, reject, timer } = pending;
        pending = null;
        clearTimeout(timer);
        if (data.error || !data.access_token) {
            reject(new Error(data.error === "access_denied"
                ? "The Google sign-in was cancelled."
                : "Google did not sign you in (" + (data.error || "no token") + ")."));
            return;
        }
        const seconds = Number(data.expires_in) || 3600;
        held = {
            token: String(data.access_token),
            expiresAt: Date.now() + seconds * 1000 - MARGIN_MS,
            scope: String(data.scope || ""),
        };
        resolve({ expiresAt: held.expiresAt, scope: held.scope });
    }

    function randomState() {
        const bytes = new Uint8Array(16);
        global.crypto.getRandomValues(bytes);
        return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    }

    /**
     * Open Google's sign-in and wait for the token. `opener` is the window
     * whose click this is (a frame's, usually): the popup has to be opened
     * inside that click, so nothing here waits before `open`.
     */
    function signIn(opener, options) {
        if (!configured()) {
            return Promise.reject(new Error(
                "Google sign-in is not set up on this site (it has no OAuth client id)."));
        }
        listen();
        if (pending) {
            clearTimeout(pending.timer);
            pending.reject(new Error("Another sign-in started."));
            pending = null;
        }
        const state = randomState();
        const params = new URLSearchParams({
            client_id: config.client_id,
            redirect_uri: redirectUri(),
            response_type: "token",
            scope: SCOPES.join(" "),
            include_granted_scopes: "true",
            state,
        });
        const prompt = options && options.prompt;
        if (prompt) params.set("prompt", prompt);
        const win = opener || global;
        const popup = win.open(config.auth + "?" + params.toString(), "plexora-google-signin",
                               "popup,width=520,height=680");
        return new Promise((resolve, reject) => {
            if (!popup) {
                reject(new Error("The browser blocked Google's sign-in window. Allow pop-ups "
                                 + "for this site and press Sign in again."));
                return;
            }
            const timer = setTimeout(() => {
                if (pending && pending.state === state) pending = null;
                reject(new Error("The Google sign-in did not finish. Press Sign in to try again."));
            }, WAIT_MS);
            pending = { state, resolve, reject, timer };
        });
    }

    function token() {
        if (!held) return "";
        if (Date.now() >= held.expiresAt) {
            held = null;
            return "";
        }
        return held.token;
    }

    function expiresAt() {
        return held ? held.expiresAt : 0;
    }

    function signOut() {
        held = null;
    }

    function sign(request, route) {
        const value = token();
        if (!value || !ROUTES.test(String(route || ""))) return request;
        const headers = new Headers(request.headers);
        headers.set(HEADER, value);
        return new Request(request, { headers });
    }

    global.PlexoraGoogleAuth = Object.freeze({
        configure, configured, signIn, token, expiresAt, signOut, sign,
        redirectUri, SCOPES, HEADER,
    });
})(typeof window !== "undefined" ? window : globalThis);
