"""A static origin for the browser-node POC pages, standing in for plexoraapp.com.

    python serve_static.py [--port 0] [--coop-coep require-corp|credentialless]
                           [--rnode]

Serves three trees and nothing else:

- ``/poc/...``  the POC pages (``page/``), including the esbuild output.
- ``/repo/plexora/client/...``  the product client exactly as it is checked out
  (vendor bundle, classic scripts, shaders, workers). Nothing is copied, so a
  page that works here works with the product files unmodified -- which is
  P0's question.
- ``/nm/...``  this folder's ``node_modules`` (DuckDB-Wasm's worker and wasm).

``--tls-cert``/``--tls-key`` serve https (a secure context, as plexoraapp.com
would be; P5 scenario b). ``--coop-coep`` adds Cross-Origin-Opener-Policy and Cross-Origin-Embedder-Policy
to every page response (P5 scenario d). ``--rnode`` adds an Open OnDemand-style
reverse proxy: ``/rnode/<host>/<port>/app/...`` serves the POC pages as if the
node served them, and every other ``/rnode/<host>/<port>/<path>`` is forwarded
to ``http://<host>:<port>/<path>`` (P5 scenario h). Only loopback hosts are
proxied.

Prints one JSON line ``{"port": N, "pid": N}`` when listening.
"""

from __future__ import annotations

import argparse
import http.client
import json
import mimetypes
import os
import posixpath
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
TREES = {
    "poc": HERE / "page",
    "repo": REPO,
    "nm": HERE / "node_modules",
}
#: Under /repo only the client is served: the page needs nothing else, and a
#: static origin that served the whole checkout would serve its secrets too.
REPO_ALLOWED = ("plexora/client/",)
TYPES = {
    ".js": "text/javascript", ".mjs": "text/javascript", ".cjs": "text/javascript",
    ".wasm": "application/wasm", ".glsl": "text/plain", ".json": "application/json",
    ".html": "text/html; charset=utf-8", ".css": "text/css", ".map": "application/json",
    ".svg": "image/svg+xml", ".png": "image/png",
}
PROXY_HOSTS = {"127.0.0.1", "localhost", "::1"}
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade", "host"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    coop_coep = None
    rnode = False

    def log_message(self, fmt, *args):  # quiet: the driver records what matters
        if os.environ.get("POC_SERVE_LOG"):
            sys.stderr.write("%s\n" % (fmt % args))

    # -- dispatch ---------------------------------------------------------

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch(head=True)

    def do_POST(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def _dispatch(self, head=False):
        path = urllib.parse.urlsplit(self.path).path
        parts = path.lstrip("/").split("/")
        if self.rnode and parts[0] == "rnode" and len(parts) >= 3:
            host, port, rest = parts[1], parts[2], parts[3:]
            if rest[:1] == ["app"]:
                # What a node serving the static build would answer at /app/:
                # the same three trees, under the node's own prefix.
                if len(rest) > 2 and rest[1] in TREES:
                    return self._file(rest[1], "/".join(rest[2:]), head)
                return self._redirect(path.rstrip("/").rsplit("/app", 1)[0] + "/app/poc/index.html")
            return self._proxy(host, port, "/".join(rest))
        if self.command not in ("GET", "HEAD"):
            return self._send(405, b"method not allowed")
        if parts[0] in TREES and len(parts) > 1:
            return self._file(parts[0], "/".join(parts[1:]), head)
        if path in ("", "/"):
            return self._redirect("/poc/index.html")
        return self._send(404, b"not found")

    # -- files ------------------------------------------------------------

    def _file(self, tree, rel, head=False):
        rel = posixpath.normpath(urllib.parse.unquote(rel)).lstrip("/")
        if rel.startswith("..") or (tree == "repo" and not rel.startswith(REPO_ALLOWED)):
            return self._send(403, b"forbidden")
        target = (TREES[tree] / rel).resolve()
        if not str(target).startswith(str(TREES[tree].resolve())) or not target.is_file():
            return self._send(404, b"not found")
        ctype = TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] \
            or "application/octet-stream"
        body = b"" if head else target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(target.stat().st_size if head else len(body)))
        self.send_header("Cache-Control", "no-store")
        self._isolation_headers()
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _isolation_headers(self):
        if self.coop_coep:
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Embedder-Policy", self.coop_coep)
            # Our own files are same-origin; CORP keeps them loadable from a
            # worker that is itself under COEP.
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    # -- the /rnode proxy ---------------------------------------------------

    def _proxy(self, host, port, rest):
        if host not in PROXY_HOSTS or not port.isdigit():
            return self._send(403, b"only loopback nodes are proxied")
        query = urllib.parse.urlsplit(self.path).query
        target = "/" + rest + ("?" + query if query else "")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        try:
            conn = http.client.HTTPConnection(host, int(port), timeout=120)
            conn.request(self.command, target, body=body, headers=headers)
            upstream = conn.getresponse()
            data = upstream.read()
        except OSError as exc:
            return self._send(502, f"node unreachable: {exc}".encode())
        self.send_response(upstream.status)
        for key, value in upstream.getheaders():
            if key.lower() in HOP_HEADERS or key.lower() == "content-length":
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        conn.close()

    # -- helpers ------------------------------------------------------------

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, where):
        self.send_response(302)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--coop-coep", choices=("require-corp", "credentialless"), default=None)
    parser.add_argument("--rnode", action="store_true")
    parser.add_argument("--tls-cert", default=None, help="serve https with this PEM certificate")
    parser.add_argument("--tls-key", default=None)
    args = parser.parse_args(argv)
    Handler.coop_coep = args.coop_coep
    Handler.rnode = args.rnode
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    if args.tls_cert:
        import ssl

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    print(json.dumps({"port": server.server_address[1], "pid": os.getpid()}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
