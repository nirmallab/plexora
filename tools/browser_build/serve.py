"""The website, served from this checkout for development and the browser tests.

    python tools/browser_build/serve.py [--port 0] [--py DIR] [--host 127.0.0.1]

Serves what viewer.plexoraapp.com and assets.plexoraapp.com will, from one
origin, with the headers they will send:

- ``/client/...``: this checkout's `plexora/client` (what the static build
  copies), never cached;
- ``/py/<version>/...``: the build `tools/browser_build/build.py` wrote (`--py`,
  its `py/` folder), cached as the bucket caches it (versioned, immutable),
  with `Content-Encoding: br` for a file that has a `.br` beside it, as the
  bucket serves a file stored with that encoding;
- ``/tests/browser/...``: the browser tests' harness pages.

Every response is cross-origin isolated (`COOP: same-origin`, `COEP:
require-corp`, `CORP: same-origin`), as the site is (D5). Prints one JSON
line ``{"port": N}`` when listening.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import posixpath
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".wasm": "application/wasm",
         ".json": "application/json", ".html": "text/html; charset=utf-8",
         ".css": "text/css", ".whl": "application/zip", ".zip": "application/zip",
         ".svg": "image/svg+xml", ".png": "image/png", ".glsl": "text/plain",
         ".tar": "application/x-tar"}


class Handler(BaseHTTPRequestHandler):
    trees: dict = {}
    #: The static build (`client/static_build.py`), served at / as Pages
    #: would: a file if there is one, else the loader (`_redirects`).
    site: Path | None = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet; the tests record what matters
        pass

    def do_GET(self):
        self._serve(head=False)

    def do_HEAD(self):
        self._serve(head=True)

    def _serve(self, head):
        path = urllib.parse.urlsplit(self.path).path
        for prefix, (root, cache) in self.trees.items():
            if path.startswith(prefix):
                rel = posixpath.normpath(urllib.parse.unquote(path[len(prefix):])).lstrip("/")
                if rel.startswith("..") or "/.." in rel:
                    return self._send(403, b"forbidden")
                target = (root / rel).resolve()
                if root.resolve() not in target.parents and target != root.resolve():
                    return self._send(403, b"forbidden")
                if target.is_dir():
                    target = target / "index.html"
                return self._file(target, cache, head)
        if self.site is not None:
            rel = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
            target = (self.site / rel).resolve() if rel and rel != "." else self.site / "index.html"
            if self.site.resolve() not in target.parents and target != self.site.resolve():
                return self._send(403, b"forbidden")
            if not target.is_file():
                target = self.site / "index.html"
            return self._file(target, "no-cache", head)
        if path in ("/", ""):
            return self._redirect("/tests/browser/harness/index.html")
        return self._send(404, b"not found")

    def _file(self, target: Path, cache: str, head: bool):
        if not target.is_file():
            return self._send(404, b"not found")
        encoded = target.with_name(target.name + ".br")
        accept = self.headers.get("Accept-Encoding", "")
        use = encoded if encoded.is_file() and "br" in accept else target
        body = b"" if head else use.read_bytes()
        self.send_response(200)
        kind = TYPES.get(target.suffix) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_header("Content-Type", kind)
        if use is encoded:
            self.send_header("Content-Encoding", "br")
        if target.name == "backendRelay.js":
            self.send_header("Service-Worker-Allowed", "/")
        self.send_header("Content-Length", str(use.stat().st_size))
        self.send_header("Cache-Control", cache)
        self._isolation()
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _isolation(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._isolation()
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, where):
        self.send_response(302)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # A browser that closes a connection it no longer needs (WebKit does,
        # often) is not an error worth a traceback.
        if isinstance(sys.exc_info()[1], (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    bench = Path(os.environ.get("PLEXORA_BENCH_HOME") or "C:/plexora-bench") / "browser_node" / "web"
    parser.add_argument("--py", default=str(bench / "py"), help="build.py's py/ folder")
    parser.add_argument("--site", default=None, help="a static build to serve at / (static_build.py --out)")
    parser.add_argument("--tree", action="append", default=[], metavar="PREFIX=DIR",
                        help="also serve DIR at /PREFIX/ (tests' fixtures), never cached")
    args = parser.parse_args(argv)
    Handler.trees = {
        "/py/": (Path(args.py), "public, max-age=31536000, immutable"),
        "/tests/browser/": (REPO / "tests" / "browser", "no-store"),
    }
    if args.site:
        Handler.site = Path(args.site)
    else:
        Handler.trees["/client/"] = (REPO / "plexora" / "client", "no-store")
    for tree in args.tree:
        prefix, _, directory = tree.partition("=")
        Handler.trees[f"/{prefix.strip('/')}/"] = (Path(directory), "no-store")
    server = Server((args.host, args.port), Handler)
    print(json.dumps({"port": server.server_address[1], "pid": os.getpid()}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
