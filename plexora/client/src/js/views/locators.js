/**
 * locators.js -- what kind of address a string is, before anything tries to
 * read it.
 *
 * Three surfaces ask this same question about whatever somebody typed or
 * picked -- the import dialog, the home page's path box, and whatever names
 * a dataset before the server has seen it -- and each used to test it its own
 * way, which is how a pasted `s3://` URL could end up wrapped in `node://`
 * (see importSample.js's addPick) or sent to `check_path_existence`, which
 * can only ever say no to a web address. One test, shared.
 *
 * Regex only, deliberately: this never touches the network and never asks the
 * server anything. `providers/base.py`'s `is_remote_locator` is the same
 * regex on the server side, and `is_node_locator` is `NODE_SCHEME` there --
 * this is the browser's copy of both, not a new rule.
 */
window.PlexoraLocators = (function () {
    "use strict";

    //: Mirrors providers/base.py's `_REMOTE_RE` exactly -- gs/gcs both spelled
    //: out because a store may be named either way before `gcs://` is folded
    //: into `gs://`, and abfs/abfss for the same reason with Azure's two
    //: spellings.
    const REMOTE_RE = /^(https?|s3|gs|gcs|az|abfss?):\/\//i;
    const NODE_PREFIX = "node://";

    /**
     * A pasted value often carries the quotes a shell or a spreadsheet cell
     * added around it, and a scheme is only ever recognisable at the start of
     * the string underneath them.
     */
    function unquoted(value) {
        const trimmed = String(value == null ? "" : value).trim();
        const match = trimmed.match(/^(["'])([\s\S]*)\1$/);
        return match ? match[2].trim() : trimmed;
    }

    function isRemoteLocator(value) {
        return REMOTE_RE.test(unquoted(value));
    }

    function isNodeLocator(value) {
        return unquoted(value).toLowerCase().startsWith(NODE_PREFIX);
    }

    /** The scheme itself, lower-cased ("s3", "https", "abfs"…), or null. */
    function remoteScheme(value) {
        const found = unquoted(value).match(REMOTE_RE);
        return found ? found[1].toLowerCase() : null;
    }

    /**
     * The last non-empty path segment of a URL, query dropped -- the same
     * rule `remote_store.url_name` applies on the server, so a picked layer
     * and the name Plexora suggests for it agree.
     *
     * `URL` parses every one of these schemes the same way it parses `http:`
     * -- a scheme, an authority and a path -- so this needs no per-scheme
     * case, and a value `URL` cannot parse at all is handed back unchanged
     * rather than thrown on: callers use this to make a name, not to validate
     * one.
     */
    function urlName(value) {
        const text = unquoted(value);
        let parsed;
        try {
            parsed = new URL(text);
        } catch (error) {
            return text;
        }
        const segments = parsed.pathname.split("/").filter(Boolean);
        return segments.length ? segments[segments.length - 1] : parsed.host;
    }

    return { isRemoteLocator, isNodeLocator, remoteScheme, urlName };
})();
