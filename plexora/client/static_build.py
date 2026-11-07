"""Build the website's static files: what viewer.plexoraapp.com serves.

    python -m plexora.client.static_build [--out DIR] [--py URL] [--models URL] [--noindex]
        [--flags URL] [--licence-api URL] [--licence-portal URL] [--licence-test-key KID=KEY]
        [--telemetry URL] [--google-client-id ID] [--google-auth URL]

Plexora's pages are rendered on the website by Plexora's own Python in a
worker beside the page (plexora/browser), so what the site serves as files is
small: the client exactly as the desktop's server serves it, every plugin's
static assets at the address the app gives them, and a loader that every path
answers with (`client/src/js/services/shell.js`), which starts the worker and
writes into the document the page the address asks for.

Writes `--out` (default `plexora/client/dist/static/`):

- `index.html`: the loader;
- `client/{src,dist,external}/...` and `plugins/<name>/static/...`;
- `google-auth.html`: the page Google's sign-in returns to
  (services/googleAuthReturn.js), the `redirect_uri` the site's OAuth client
  registers (`https://<site>/google-auth.html`);
- `selfcheck.html`: the self-check page, at `/selfcheck` (Pages serves a
  page without its `.html`): how Plexora runs in this browser on this
  computer, its results saved as a file (services/selfcheck.js);
- `client/shell.json`: `{version, py, models, flags?, licence?, telemetry?, google?}`, `py` the base the
  Python bundle is served from (`tools/browser_build/build.py`; the assets
  bucket on the site, `/py/<version>/` in development; onnxruntime-web is its
  `ort/`), `models` the base magic select's model files are under
  (`vision/sam_weights.py` `WEB_DIR` below it: the assets bucket's /models/ on
  the site, `/models/` in development, where `tools/browser_build/serve.py
  --tree` serves a folder holding them); `flags` the site's switches
  (`--flags`: the assets bucket's flags.json, `publish.py flags`; the loader
  reads it first -- the kill switch, a minimum version, a model switched off;
  none without it); `licence` only when an option below
  moves it off the defaults (services/licenseClient.js): `api`, the
  BioCognia platform's product API (default https://api.biocognia.com/v1),
  `portal`, and `test_keys`, throwaway `bct*` keys a TEST build trusts
  (tests/browser/license.mjs). `tools/browser_build/publish.py site` refuses
  to publish a build that carries test keys; `telemetry` `{endpoint}` only
  with `--telemetry`: the service the website sends the desktop's anonymous
  usage counts to (plexora/browser/telemetry.py; the site's builds name
  https://analytics.plexoraapp.com, which admits their origins). A build
  without it -- every test's, a node's -- sends nothing; `google` only when a client id
  is given: `client_id`, the site's Google OAuth client for Google Cloud
  (services/googleAuth.js; plan L9), and `auth`, Google's authorization
  endpoint unless a stand-in is named (tests/browser/mock_google.py);
- `_headers` and `_redirects` for Cloudflare Pages: every response
  cross-origin isolated (the site needs SharedArrayBuffer), the service worker
  allowed the whole site, and every path that is not a file answered with the
  loader (`--noindex` adds `X-Robots-Tag: noindex` everywhere: a preview).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

CLIENT = Path(__file__).resolve().parent
PACKAGE = CLIENT.parent
DEFAULT_OUT = CLIENT / "dist" / "static"

#: The loader. Its own files are named relative to `<base href>`, which is
#: the site's root: "/" as built (viewer.plexoraapp.com serves it at every
#: path), and the app's root behind a portal's proxy, where a data node
#: serving the app (`plexora node serve --app`, server/node/webapp.py) writes
#: the base for each address it answers -- the page reads it back as the
#: site's base path (services/shell.js `BASE`). `BASE_HREF` is what the node
#: replaces.
BASE_HREF = '<base href="/">'

LOADER = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
""" + BASE_HREF + """
<title>Plexora</title>
<link rel="icon" type="image/svg+xml" href="client/src/img/logo.svg">
<link rel="stylesheet" href="client/src/css/tokens.css">
<link rel="stylesheet" href="client/src/css/shell.css">
<script src="client/src/js/services/shell.js"></script>
</head>
<body>
<div id="plexora-boot" role="status" aria-live="polite">
  <img src="client/src/img/logo.svg" alt="">
  <div id="plexora-boot-status">Starting Plexora</div>
</div>
</body>
</html>
"""

#: This computer's loopback, where a paired node or the desktop app answers.
#: (A CSP host source cannot name an IPv6 address: `[::1]` is refused.)
_LOOPBACK = "http://127.0.0.1:* http://localhost:*"

#: The site's Content-Security-Policy (plan Phase 5). Scripts only from the
#: site and the assets bucket -- no inline script: the website's pages carry
#: their variables as JSON (base.html) -- and WebAssembly compiled; workers
#: from the site (blob: for a library that starts its own); requests anywhere
#: over https and to the loopback; images also from the loopback (a node's
#: tiles); inline styles allowed (the app sets them everywhere); no plugins;
#: the site framed by nobody but itself.
CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self' 'wasm-unsafe-eval' https://assets.plexoraapp.com blob:",
    "worker-src 'self' blob:",
    f"connect-src 'self' https: {_LOOPBACK} blob: data:",
    f"img-src 'self' data: blob: https: {_LOOPBACK}",
    "media-src 'self' blob: data:",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self' data:",
    "frame-src 'self' blob:",
    "frame-ancestors 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
))

#: Where Google's sign-in comes back to: hands the token to the page that
#: asked and closes (services/googleAuthReturn.js). No referrer, nothing but
#: the site's own script, and the token is out of the address before it runs
#: anything else.
GOOGLE_AUTH = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Plexora - Google sign-in</title>
</head>
<body style="font-family: system-ui, sans-serif; margin: 2rem">
<p id="google-auth-said">Signing in to Google&hellip;</p>
<script src="client/src/js/services/googleAuthReturn.js"></script>
</body>
</html>
"""

#: The self-check page (services/selfcheck.js): a heading, one line, the
#: buttons on one row, then a row per measurement as it is made.
SELFCHECK = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
""" + BASE_HREF + """
<title>Plexora - Self-check</title>
<link rel="icon" type="image/svg+xml" href="client/src/img/logo.svg">
<link rel="stylesheet" href="client/src/css/tokens.css">
<link rel="stylesheet" href="client/src/css/selfcheck.css">
<script src="client/src/js/services/pythonClient.js"></script>
<script src="client/src/js/services/selfcheck.js"></script>
</head>
<body>
<main id="selfcheck">
  <header><img src="client/src/img/logo.svg" alt=""><h1>Plexora on this computer</h1></header>
  <p>About a minute. Nothing is sent: download the results to share them.</p>
  <div class="selfcheck-actions">
    <button id="selfcheck-run" class="is-primary" type="button">Run the check</button>
    <button id="selfcheck-save" type="button" disabled>Download results</button>
  </div>
  <table id="selfcheck-results"></table>
</main>
</body>
</html>
"""

#: Cloudflare Pages' `_headers`: the site is cross-origin isolated everywhere
#: (plan D5) and under its Content-Security-Policy; its service worker may
#: control every path.
HEADERS = f"""/*
  Cross-Origin-Opener-Policy: same-origin
  Cross-Origin-Embedder-Policy: require-corp
  Cross-Origin-Resource-Policy: same-origin
  X-Content-Type-Options: nosniff
  Referrer-Policy: same-origin
  Content-Security-Policy: {CSP}

/client/src/js/workers/backendRelay.js
  Service-Worker-Allowed: /
  Cache-Control: no-cache

/index.html
  Cache-Control: no-cache

/client/shell.json
  Cache-Control: no-cache

/google-auth.html
  Cache-Control: no-store
  Referrer-Policy: no-referrer
"""

#: Every path that is not a file is the loader (it reads the address itself).
REDIRECTS = "/* /index.html 200\n"

#: Source maps, and the logo's Illustrator sources (4.5 MB no page loads).
_SKIP = (".map", ".ai")


def _copy(source: Path, target: Path) -> int:
    count = 0
    for path in source.rglob("*"):
        if path.is_dir() or path.suffix in _SKIP:
            continue
        relative = path.relative_to(source)
        if relative.parts and relative.parts[0] == "static" and source.name == "dist":
            continue  # a build's own output, when it lives inside dist/
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        count += 1
    return count


def licence_config(api: str | None = None, portal: str | None = None, test_keys=()) -> dict:
    """shell.json's `licence`: only what differs from the page's defaults.
    `test_keys` are `KID=KEY` (biocognia.testing's `bct*` kids)."""
    out = {}
    if api:
        out["api"] = api.rstrip("/")
    if portal:
        out["portal"] = portal.rstrip("/")
    keys = {}
    for item in test_keys or ():
        kid, _, key = str(item).partition("=")
        if not re.fullmatch(r"bct\d*", kid) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", key):
            raise ValueError(f"--licence-test-key wants bct<N>=<base64url Ed25519 public key>, not {item!r}")
        keys[kid] = key
    if keys:
        out["test_keys"] = keys
    return out


#: The telemetry service the website's own builds name (`--telemetry`):
#: plexora/telemetry/config.py's DEFAULT_ENDPOINT, the Worker in backend/.
TELEMETRY_ENDPOINT = "https://analytics.plexoraapp.com"


def google_config(client_id: str | None = None, auth: str | None = None) -> dict:
    """shell.json's `google`: the OAuth client id, and the authorization
    endpoint when it is not Google's (a test's stand-in). Empty without a
    client id: the page then says Google sign-in is not set up."""
    if not client_id:
        return {}
    if not re.fullmatch(r"[A-Za-z0-9._-]+", client_id):
        raise ValueError(f"--google-client-id wants an OAuth client id, not {client_id!r}")
    out = {"client_id": client_id}
    if auth:
        out["auth"] = auth
    return out


def build(out: Path = DEFAULT_OUT, py: str | None = None, noindex: bool = False,
          licence: dict | None = None, models: str | None = None,
          flags: str | None = None, telemetry: str | None = None,
          google: dict | None = None) -> dict:
    from plexora import __version__

    py = py or f"/py/{__version__}/"
    models = models or "/models/"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    files = 0
    for tree in ("src", "dist", "external"):
        if (CLIENT / tree).is_dir():
            files += _copy(CLIENT / tree, out / "client" / tree)
    plugins = []
    for static in sorted((PACKAGE / "plugins").glob("*/static")):
        files += _copy(static, out / "plugins" / static.parent.name / "static")
        plugins.append(static.parent.name)
    (out / "index.html").write_bytes(LOADER.encode("utf-8"))
    (out / "google-auth.html").write_bytes(GOOGLE_AUTH.encode("utf-8"))
    (out / "selfcheck.html").write_bytes(SELFCHECK.encode("utf-8"))
    shell = {"version": __version__, "py": py, "models": models}
    if flags:
        shell["flags"] = flags
    if licence:
        shell["licence"] = licence
    if telemetry:
        shell["telemetry"] = {"endpoint": telemetry.rstrip("/")}
    if google:
        shell["google"] = google
    (out / "client" / "shell.json").write_bytes(json.dumps(shell, indent=1).encode("utf-8"))
    headers = HEADERS.replace("/*\n", "/*\n  X-Robots-Tag: noindex\n", 1) if noindex else HEADERS
    (out / "_headers").write_bytes(headers.encode("utf-8"))
    (out / "_redirects").write_bytes(REDIRECTS.encode("utf-8"))
    return {"out": str(out), "files": files + 6, "plugins": plugins, "version": __version__, "py": py,
            "models": models, "flags": flags, "telemetry": shell.get("telemetry"),
            "google": dict(google or {}),
            "licence": {k: (sorted(v) if k == "test_keys" else v) for k, v in (licence or {}).items()}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--py", default=None, help="where the Python bundle is served from")
    parser.add_argument("--models", default=None,
                        help="where magic select's model files are served from (default /models/)")
    parser.add_argument("--noindex", action="store_true", help="ask search engines not to index it")
    parser.add_argument("--flags", default=None,
                        help="the site's flags.json (the kill switch); none when not given")
    parser.add_argument("--licence-api", default=None,
                        help="the BioCognia platform's product API (default https://api.biocognia.com/v1)")
    parser.add_argument("--licence-portal", default=None, help="the portal a person approves a code at")
    parser.add_argument("--licence-test-key", action="append", default=[], metavar="KID=KEY",
                        help="a throwaway key a TEST build trusts (never published)")
    parser.add_argument("--telemetry", default=None, metavar="URL",
                        help="send the desktop's anonymous usage counts to this service "
                             f"(the site's builds: {TELEMETRY_ENDPOINT}); none: nothing is sent")
    parser.add_argument("--google-client-id", default=None,
                        help="the site's Google OAuth client id, for Google Cloud (plan L9)")
    parser.add_argument("--google-auth", default=None,
                        help="the OAuth authorization endpoint, for a stand-in (default Google's)")
    args = parser.parse_args(argv)
    licence = licence_config(args.licence_api, args.licence_portal, args.licence_test_key)
    google = google_config(args.google_client_id, args.google_auth)
    print(json.dumps(build(Path(args.out), args.py, args.noindex, licence, args.models,
                           flags=args.flags, telemetry=args.telemetry, google=google),
                     indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
