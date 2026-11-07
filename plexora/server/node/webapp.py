"""`/app/` on a data node: the static build of the viewer, served beside the data.

The layout this is for is a node whose browser cannot be pointed anywhere
else -- an Open OnDemand job, a site proxy -- where the page and the data are
best served from one address: same origin, so no CORS, no local-network
prompt and no mixed content (investigation section 6.5, "node serves the
app"). The build itself is `plexora/client/dist/static/`, written by
`plexora/client/static_build.py`.

**No token, on purpose, and only here.** What is under `/app/` is the same
HTML, JavaScript, CSS and wasm that plexoraapp.com hands to anybody; it names
no file and no resource. Everything that does is under `/node/v1/`, whose
guard (`api.require_node_token`) is untouched by this module. The node's `/`
stays as uninformative as it was (`app._root`).

**Off unless asked for.** `plexora node serve --app`, and then only when the
build is actually there: a wheel built without it is a node that says so
(`/hello.features` leaves out `app`) rather than one that serves 404s from a
route it claims to have.

**Behind a path prefix.** OOD's `/rnode/<host>/<port>/` strips its prefix
before the request arrives, so the prefix matters here in two places: a
redirect -- `/app` to `/app/`, a directory to its trailing slash -- and the
loader's `<base href>`, which names the app's root for the page (it reads its
base path back from it: services/shell.js `BASE`, Python's `base_url`). A
prefix the operator named (`--url-prefix`) or the proxy sent
(`X-Forwarded-Prefix`) is put in front; with neither, both are relative --
`../` once per folder between the address and `/app/` -- which resolves under
any prefix without knowing it.

**Cross-origin isolated**, as the website is (plan D5): the page's Python
needs SharedArrayBuffer. Every answer here says so (`ISOLATION`), and the
site's service worker may control the app's whole root.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from flask import Response, request, send_from_directory
from werkzeug.security import safe_join

#: Where `static_build.py` writes the build, inside the installed package.
DEFAULT_DIR = Path(__file__).resolve().parents[2] / "client" / "dist" / "static"

INDEX = "index.html"

#: Spelled out rather than left to `mimetypes`, which on Windows reads the
#: registry -- where `.js` is often `text/plain`, and a module script served
#: as that is refused by every browser. `.wasm` must be `application/wasm` or
#: streaming compilation falls back to a slower path. Werkzeug adds the
#: charset to the text types.
CONTENT_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
    ".wasm": "application/wasm",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".txt": "text/plain",
    ".glsl": "text/plain",
    ".zip": "application/zip",
    ".whl": "application/zip",
    ".gz": "application/gzip",
}

#: What the website sends with every file (client/static_build.py HEADERS):
#: the page is cross-origin isolated.
ISOLATION = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "same-origin",
}

#: The site's service worker, which controls the app's root from a folder
#: below it.
RELAY = "client/src/js/workers/backendRelay.js"

#: What the static build writes into the loader for the site's root
#: (client/static_build.py BASE_HREF), replaced per address here.
BASE_HREF = b'<base href="/">'

#: One path segment of a prefix: what RFC 3986 allows in a path, minus '/'.
_SEGMENT = re.compile(r"^[A-Za-z0-9._~!$&'()*+,;=:@%-]+$")


class BadPrefix(ValueError):
    """A `--url-prefix` that is not a plain absolute path."""


def resolve_dir(directory=None):
    """The build directory when there is a build in it, else None.

    "There is a build" means an `index.html`: an empty or half-written
    directory is not something to advertise as `app`.
    """
    path = Path(directory) if directory else DEFAULT_DIR
    try:
        return path if (path / INDEX).is_file() else None
    except OSError:
        return None


def clean_prefix(value):
    """A prefix as `/a/b` (no trailing slash), "" for none, or BadPrefix.

    `plexora._url.clean_prefix`'s form, and stricter, because this one ends up
    at the front of a `Location` header: a backslash there is read as a slash
    (`/\\host` is another site), and `..` is a way out of the prefix.
    """
    from plexora import _url

    if "//" in str(value or ""):
        # `_url.clean_prefix` would make `//host` into `/host`, harmless but
        # not what was sent; a prefix with an empty segment is not one.
        raise BadPrefix(f"{value!r} is not a path prefix such as /rnode/host/8642")
    try:
        text = _url.clean_prefix(value)
    except ValueError as exc:
        raise BadPrefix(str(exc)) from None
    if not text:
        return ""
    for segment in text[1:].split("/"):
        if segment in ("", ".", "..") or not _SEGMENT.match(segment):
            raise BadPrefix(
                f"{value!r} is not a path prefix such as /rnode/host/8642")
    return text


def content_type(name):
    suffix = os.path.splitext(name)[1].lower()
    return CONTENT_TYPES.get(suffix, "application/octet-stream")


def install(app, directory, *, url_prefix=""):
    """Add `/app`, `/app/` and `/app/<path>` to `app`, serving `directory`.

    Routes on the app rather than on the node blueprint, so the blueprint's
    token guard never sees them -- and so nothing about `/node/v1/` changes.
    """
    root = Path(directory)
    configured = clean_prefix(url_prefix)
    app.config["PLEXORA_NODE_APP_DIR"] = str(root)
    app.config["PLEXORA_NODE_URL_PREFIX"] = configured

    def prefix():
        """The configured prefix, else a well-formed X-Forwarded-Prefix, else
        None (unknown: redirect relatively)."""
        if configured:
            return configured
        sent = request.headers.get("X-Forwarded-Prefix")
        if not sent:
            return None
        try:
            return clean_prefix(sent) or None
        except BadPrefix:
            return None

    def add_slash():
        """Redirect to this path with a trailing slash, query kept."""
        known = prefix()
        if known:
            target = known + request.path + "/"
        else:
            # Relative to the current URL: `/x/app` -> `app/` is `/x/app/`
            # whatever `/x` is.
            target = request.path.rsplit("/", 1)[-1] + "/"
        query = request.query_string.decode("latin-1")
        if query:
            target += "?" + query
        response = Response(status=302)
        response.headers["Location"] = target
        response.headers["Cache-Control"] = "no-store"
        return response

    def base_href():
        """The app's root as the loader's `<base href>`: absolute under a
        known prefix, else relative to the address asked for."""
        known = prefix()
        if known:
            return known + "/app/"
        below = request.path[len("/app/"):] if request.path.startswith("/app/") else ""
        return "../" * below.count("/") or "./"

    def stamp(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.update(ISOLATION)
        return response

    def send(relative):
        if relative.endswith(INDEX):
            # The loader, with the app's root for this address. Never
            # cached: the page names every asset, and a stale one after an
            # upgrade would ask for assets that are no longer there.
            html = (root / relative).read_bytes()
            href = base_href().replace("&", "&amp;").replace('"', "&quot;")
            html = html.replace(BASE_HREF, b'<base href="' + href.encode("utf-8") + b'">', 1)
            response = Response(html, mimetype="text/html")
            response.headers["Cache-Control"] = "no-cache"
            return stamp(response)
        response = send_from_directory(root, relative,
                                       mimetype=content_type(relative))
        if relative == RELAY:
            response.headers["Service-Worker-Allowed"] = "/"
            response.headers["Cache-Control"] = "no-cache"
        return stamp(response)

    def not_found():
        return stamp(Response("Not found\n", status=404, mimetype="text/plain"))

    def app_root():
        return add_slash()

    def app_index():
        return send(INDEX)

    def app_file(subpath):
        segments = [part for part in subpath.split("/") if part]
        if any(part.startswith(".") for part in segments):
            return not_found()
        full = safe_join(str(root), subpath)
        if full is None:
            return not_found()
        if os.path.isfile(full):
            return send(subpath)
        if os.path.isdir(full):
            if not os.path.isfile(os.path.join(full, INDEX)):
                return not_found()
            if not subpath.endswith("/"):
                return add_slash()
            return send(subpath + INDEX)
        # A path the build has no file for. With no extension it is one of
        # the page's own routes (`/app/connect`, `/app/<project>`), which the
        # page resolves itself; with one it is a missing asset, and handing
        # back HTML for a script would only move the error somewhere worse.
        last = segments[-1] if segments else ""
        if "." in last:
            return not_found()
        return send(INDEX)

    app.add_url_rule("/app", "plexora_app_root", app_root)
    app.add_url_rule("/app/", "plexora_app_index", app_index)
    app.add_url_rule("/app/<path:subpath>", "plexora_app_file", app_file)
    return app
