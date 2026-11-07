"""Build the website's static files: what viewer.plexoraapp.com serves.

    python -m plexora.client.static_build [--out DIR] [--py URL] [--models URL] [--noindex]

Plexora's pages are rendered on the website by Plexora's own Python in a
worker beside the page (plexora/browser), so what the site serves as files is
small: the client exactly as the desktop's server serves it, every plugin's
static assets at the address the app gives them, and a loader that every path
answers with (`client/src/js/services/shell.js`), which starts the worker and
writes into the document the page the address asks for.

Writes `--out` (default `plexora/client/dist/static/`):

- `index.html`: the loader;
- `client/{src,dist,external}/...` and `plugins/<name>/static/...`;
- `client/shell.json`: `{version, py, models}`, `py` the base the Python
  bundle is served from (`tools/browser_build/build.py`; the assets bucket on
  the site, `/py/<version>/` in development; onnxruntime-web is its `ort/`),
  `models` the base magic select's model files are under
  (`vision/sam_weights.py` `WEB_DIR` below it: the assets bucket's /models/ on
  the site, `/models/` in development, where `tools/browser_build/serve.py
  --tree` serves a folder holding them);
- `_headers` and `_redirects` for Cloudflare Pages: every response
  cross-origin isolated (the site needs SharedArrayBuffer), the service worker
  allowed the whole site, and every path that is not a file answered with the
  loader (`--noindex` adds `X-Robots-Tag: noindex` everywhere: a preview).
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

CLIENT = Path(__file__).resolve().parent
PACKAGE = CLIENT.parent
DEFAULT_OUT = CLIENT / "dist" / "static"

LOADER = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Plexora</title>
<link rel="icon" type="image/svg+xml" href="/client/src/img/logo.svg">
<link rel="stylesheet" href="/client/src/css/tokens.css">
<link rel="stylesheet" href="/client/src/css/shell.css">
<script src="/client/src/js/services/shell.js"></script>
</head>
<body>
<div id="plexora-boot" role="status" aria-live="polite">
  <img src="/client/src/img/logo.svg" alt="">
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


def build(out: Path = DEFAULT_OUT, py: str | None = None, noindex: bool = False,
          models: str | None = None) -> dict:
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
    (out / "client" / "shell.json").write_bytes(
        json.dumps({"version": __version__, "py": py, "models": models}, indent=1).encode("utf-8"))
    headers = HEADERS.replace("/*\n", "/*\n  X-Robots-Tag: noindex\n", 1) if noindex else HEADERS
    (out / "_headers").write_bytes(headers.encode("utf-8"))
    (out / "_redirects").write_bytes(REDIRECTS.encode("utf-8"))
    return {"out": str(out), "files": files + 4, "plugins": plugins, "version": __version__, "py": py,
            "models": models}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--py", default=None, help="where the Python bundle is served from")
    parser.add_argument("--models", default=None,
                        help="where magic select's model files are served from (default /models/)")
    parser.add_argument("--noindex", action="store_true", help="ask search engines not to index it")
    args = parser.parse_args(argv)
    print(json.dumps(build(Path(args.out), args.py, args.noindex, args.models), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
