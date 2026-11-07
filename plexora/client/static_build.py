"""Build the website's static files: what viewer.plexoraapp.com serves.

    python -m plexora.client.static_build [--out DIR] [--py URL] [--models URL] [--noindex]
        [--licence-api URL] [--licence-portal URL] [--licence-test-key KID=KEY]
        [--telemetry URL]

Plexora's pages are rendered on the website by Plexora's own Python in a
worker beside the page (plexora/browser), so what the site serves as files is
small: the client exactly as the desktop's server serves it, every plugin's
static assets at the address the app gives them, and a loader that every path
answers with (`client/src/js/services/shell.js`), which starts the worker and
writes into the document the page the address asks for.

Writes `--out` (default `plexora/client/dist/static/`):

- `index.html`: the loader;
- `client/{src,dist,external}/...` and `plugins/<name>/static/...`;
- `client/shell.json`: `{version, py, models, licence?, telemetry?}`, `py` the base the
  Python bundle is served from (`tools/browser_build/build.py`; the assets
  bucket on the site, `/py/<version>/` in development; onnxruntime-web is its
  `ort/`), `models` the base magic select's model files are under
  (`vision/sam_weights.py` `WEB_DIR` below it: the assets bucket's /models/ on
  the site, `/models/` in development, where `tools/browser_build/serve.py
  --tree` serves a folder holding them); `licence` only when an option below
  moves it off the defaults (services/licenseClient.js): `api`, the
  BioCognia platform's product API (default https://api.biocognia.com/v1),
  `portal`, and `test_keys`, throwaway `bct*` keys a TEST build trusts
  (tests/browser/license.mjs). `tools/browser_build/publish.py site` refuses
  to publish a build that carries test keys; `telemetry` `{endpoint}` only
  with `--telemetry`: the service the website sends the desktop's anonymous
  usage counts to (plexora/browser/telemetry.py; the site's builds name
  https://analytics.plexoraapp.com, which admits their origins). A build
  without it -- every test's, a node's -- sends nothing;
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

#: Cloudflare Pages' `_headers`: the site is cross-origin isolated everywhere
#: (plan D5); its service worker may control every path.
HEADERS = """/*
  Cross-Origin-Opener-Policy: same-origin
  Cross-Origin-Embedder-Policy: require-corp
  Cross-Origin-Resource-Policy: same-origin
  X-Content-Type-Options: nosniff
  Referrer-Policy: same-origin

/client/src/js/workers/backendRelay.js
  Service-Worker-Allowed: /
  Cache-Control: no-cache

/index.html
  Cache-Control: no-cache

/client/shell.json
  Cache-Control: no-cache
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


def build(out: Path = DEFAULT_OUT, py: str | None = None, noindex: bool = False,
          licence: dict | None = None, models: str | None = None,
          telemetry: str | None = None) -> dict:
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
    shell = {"version": __version__, "py": py, "models": models}
    if licence:
        shell["licence"] = licence
    if telemetry:
        shell["telemetry"] = {"endpoint": telemetry.rstrip("/")}
    (out / "client" / "shell.json").write_bytes(json.dumps(shell, indent=1).encode("utf-8"))
    headers = HEADERS.replace("/*\n", "/*\n  X-Robots-Tag: noindex\n", 1) if noindex else HEADERS
    (out / "_headers").write_bytes(headers.encode("utf-8"))
    (out / "_redirects").write_bytes(REDIRECTS.encode("utf-8"))
    return {"out": str(out), "files": files + 4, "plugins": plugins, "version": __version__, "py": py,
            "models": models, "telemetry": shell.get("telemetry"),
            "licence": {k: (sorted(v) if k == "test_keys" else v) for k, v in (licence or {}).items()}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--py", default=None, help="where the Python bundle is served from")
    parser.add_argument("--models", default=None,
                        help="where magic select's model files are served from (default /models/)")
    parser.add_argument("--noindex", action="store_true", help="ask search engines not to index it")
    parser.add_argument("--licence-api", default=None,
                        help="the BioCognia platform's product API (default https://api.biocognia.com/v1)")
    parser.add_argument("--licence-portal", default=None, help="the portal a person approves a code at")
    parser.add_argument("--licence-test-key", action="append", default=[], metavar="KID=KEY",
                        help="a throwaway key a TEST build trusts (never published)")
    parser.add_argument("--telemetry", default=None, metavar="URL",
                        help="send the desktop's anonymous usage counts to this service "
                             f"(the site's builds: {TELEMETRY_ENDPOINT}); none: nothing is sent")
    args = parser.parse_args(argv)
    licence = licence_config(args.licence_api, args.licence_portal, args.licence_test_key)
    print(json.dumps(build(Path(args.out), args.py, args.noindex, licence, args.models,
                           args.telemetry), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
