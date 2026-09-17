function passVariablesToFrontend(vars) {
    return vars
}

function plexoraBaseUrl() {
    const base = window.PLEXORA_BASE_URL || "";
    if (!base || base === "/") {
        return "";
    }
    return "/" + String(base).replace(/^\/+|\/+$/g, "");
}

function plexoraUrl(path) {
    const normalizedPath = String(path || "").replace(/^\/+/, "");
    const base = plexoraBaseUrl();
    if (!normalizedPath) {
        return base || "/";
    }
    return (base ? base + "/" : "/") + normalizedPath;
}

//: What the browser says when a request never reached anything at all. It is
//: a TypeError with no status, no body and no detail, and every browser spells
//: it differently -- "Failed to fetch", "NetworkError when attempting to fetch
//: resource", "Load failed". None of the three names what failed.
const PLEXORA_UNREACHABLE =
    "Plexora itself stopped answering. The page is still open, but the "
    + "Plexora that served it is not reachable from this browser — it has "
    + "exited, or something between the two (a VPN that took over the local "
    + "network, a sleeping laptop, a closed tunnel) is in the way. Reload the "
    + "page: if it does not come back, Plexora is no longer running.";

/**
 * `fetch`, with the one failure it reports uselessly translated.
 *
 * Every route in this app is same-origin and relative -- see `plexoraUrl` --
 * so a request that comes back with ANY status has reached Plexora, and the
 * callers all have written prose for those. The case none of them had a
 * sentence for is the request that reaches nothing: `fetch` rejects with a
 * bare TypeError, whose message is a browser implementation detail, and it
 * surfaced verbatim in a red error slot as "Failed to fetch".
 *
 * That string is the worst possible thing to show here, because it is read on
 * the screen where somebody has just described a CLUSTER: it looks like the
 * cluster refused them, and it sends them to check a VPN, a username and a
 * partition name for a problem that is on this side of the ssh. Naming the
 * right machine is the whole of the fix -- there is nothing to retry, and
 * nothing this code can do about it, but knowing WHICH end is missing is the
 * difference between reloading the page and an afternoon on ERISTwo.
 *
 * Only the transport error is touched. A 404, a 409 and a 500 are all real
 * answers from a live Plexora and go back to the caller untouched, because the
 * caller is the one that knows what it asked for.
 */
async function plexoraFetch(path, options) {
    try {
        return await fetch(plexoraUrl(path), options);
    } catch (e) {
        // A TypeError is the transport failure; anything else -- an aborted
        // signal, a caller that passed nonsense -- is a different bug and must
        // not be described as an unreachable server.
        if (e instanceof TypeError) throw new Error(PLEXORA_UNREACHABLE);
        throw e;
    }
}
