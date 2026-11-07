/**
 * The page Google's sign-in returns to (`google-auth.html`, written by
 * plexora/client/static_build.py): hands the token in its address's
 * fragment to the page that asked (services/googleAuth.js) and closes.
 *
 * The fragment is read once and cleared from the address and the history
 * before anything else, so the token is not left in either; a fragment is
 * never sent to a server. The hand-over is a BroadcastChannel, which reaches
 * the asking page in this same origin although `Cross-Origin-Opener-Policy`
 * cut this window off from its opener when it went to Google.
 */
(function () {
    "use strict";

    const fragment = new URLSearchParams((window.location.hash || "").replace(/^#/, ""));
    try {
        window.history.replaceState(null, "", window.location.pathname);
    } catch (error) { /* an address that cannot be rewritten */ }
    const answer = {};
    for (const key of ["access_token", "expires_in", "scope", "state", "error", "token_type"]) {
        if (fragment.has(key)) answer[key] = fragment.get(key);
    }
    const said = document.getElementById("google-auth-said");
    if (!answer.state) {
        if (said) said.textContent = "Nothing to hand over: this window can be closed.";
        return;
    }
    try {
        const channel = new BroadcastChannel("plexora-google-auth");
        channel.postMessage(answer);
        channel.close();
    } catch (error) {
        if (said) said.textContent = "This browser could not hand the sign-in back to Plexora.";
        return;
    }
    if (said) {
        said.textContent = answer.error
            ? "The sign-in did not complete. You can close this window."
            : "Signed in. You can close this window.";
    }
    setTimeout(() => window.close(), 50);
})();
