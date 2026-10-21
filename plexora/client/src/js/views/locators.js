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
    //: spellings. `sftp` is a file on a files-only saved server (an SSH
    //: machine nothing runs on), read the same way: bytes travel, only here.
    const REMOTE_RE = /^(https?|s3|gs|gcs|az|abfss?|sftp):\/\//i;
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
        if (!segments.length) return parsed.host;
        const last = segments[segments.length - 1];
        // An sftp address keeps a path's spaces literal (the server builds it
        // that way, see `sftp.url_for`), and `URL` re-escapes them: undone
        // here, or "my image.tif" would be offered as a name with a %20 in it.
        if (parsed.protocol === "sftp:") {
            try { return decodeURIComponent(last); } catch (error) { return last; }
        }
        return last;
    }

    /**
     * The `sftp://` address of a path typed for a files-only server, built the
     * way the server builds the ones its listing hands back (`sftp.url_for`):
     * only what a URL parser would misread -- `%`, `?`, `#` -- is escaped. A
     * path that is not absolute is taken from `home`, the account's home
     * folder, which is where the server's own relative paths start too.
     */
    function sftpUrl(base, path, home) {
        let text = String(path == null ? "" : path).trim();
        if (!text.startsWith("/")) {
            const root = String(home || "/").replace(/\/+$/, "");
            text = `${root}/${text.replace(/^~\/?/, "")}`;
        }
        const escaped = text.replace(/[%?#]/g, (c) => encodeURIComponent(c));
        return `${String(base).replace(/\/+$/, "")}${escaped}`;
    }

    return { isRemoteLocator, isNodeLocator, remoteScheme, urlName, sftpUrl };
})();
