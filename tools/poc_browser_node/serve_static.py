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
- ``/pyodide/...`` and ``/wheels/...`` (with ``--pyodide DIR``, P9): a Pyodide
  release and the wheels installed on top (``bench/p9_dist.py``), served
  same-origin as viewer.plexoraapp.com would serve them. Cacheable
  (``max-age``), unlike every other tree: P9's return visit measures the
  browser's HTTP cache.

``--throttle MBPS:RTT_MS`` sends every response over one shared link of that
speed after one round trip (P9's first visit on a 50 Mbps / 20 ms line).
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
import threading
import time
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
    ".whl": "application/zip", ".zip": "application/zip",
    ".js": "text/javascript", ".mjs": "text/javascript", ".cjs": "text/javascript",
    ".wasm": "application/wasm", ".glsl": "text/plain", ".json": "application/json",
    ".html": "text/html; charset=utf-8", ".css": "text/css", ".map": "application/json",
    ".svg": "image/svg+xml", ".png": "image/png",
}
PROXY_HOSTS = {"127.0.0.1", "localhost", "::1"}
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailers", "transfer-encoding", "upgrade", "host"}


class Link:
    """One shared link for every response (P9's first-visit profile): each
    response waits one round trip, then its bytes leave at `mbps` in total
    across all connections, as a home line would carry them. Cache hits never
    reach the server, so a return visit is unaffected."""

    CHUNK = 64 * 1024

    def __init__(self, mbps, rtt_ms):
        self.rate = mbps * 1e6 / 8
        self.rtt = rtt_ms / 1000.0
        self.lock = threading.Lock()
        self.free_at = time.monotonic()

    def send(self, wfile, body):
        time.sleep(self.rtt)
        for i in range(0, len(body), self.CHUNK):
            chunk = body[i:i + self.CHUNK]
            with self.lock:
                start = max(time.monotonic(), self.free_at)
                self.free_at = start + len(chunk) / self.rate
                done = self.free_at
            time.sleep(max(0.0, done - time.monotonic()))
            wfile.write(chunk)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    coop_coep = None
    rnode = False
    link = None

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
        # A precompressed sibling (P9's pycbr variant: bench/p9_dist.py) is
        # sent as it is, the way a CDN serves a .br it holds.
        encoded = target.with_name(target.name + ".br")
        brotli = (tree in ("pyodide", "wheels") and encoded.is_file()
                  and "br" in (self.headers.get("Accept-Encoding") or ""))
        source = encoded if brotli else target
        body = b"" if head else source.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if brotli:
            self.send_header("Content-Encoding", "br")
        if tree in ("pyodide", "wheels"):
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(source.stat().st_size if head else len(body)))
        self.send_header("Cache-Control", self._cache_control(tree))
        self._isolation_headers()
        self.end_headers()
        if body and self.link:
            self.link.send(self.wfile, body)
        elif body:
            self.wfile.write(body)

    def _cache_control(self, tree):
        # P9's return visit reads the browser's cache, as a CDN's long-lived
        # immutable files would let it; every other tree stays uncached.
        if tree in ("pyodide", "wheels"):
            return "public, max-age=31536000, immutable"
        return "no-store"

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
    parser.add_argument("--throttle", default=None, metavar="MBPS:RTT_MS",
                        help="P9: every response over one shared link of this speed and round trip")
    parser.add_argument("--pyodide", default=None,
                        help="P9: a folder holding pyodide/ and wheels/ (bench/p9_dist.py)")
    parser.add_argument("--tls-cert", default=None, help="serve https with this PEM certificate")
    parser.add_argument("--tls-key", default=None)
    args = parser.parse_args(argv)
    Handler.coop_coep = args.coop_coep
    Handler.rnode = args.rnode
    if args.throttle:
        mbps, rtt = (float(v) for v in args.throttle.split(":"))
        Handler.link = Link(mbps, rtt)
    if args.pyodide:
        TREES["pyodide"] = Path(args.pyodide) / "pyodide"
        TREES["wheels"] = Path(args.pyodide) / "wheels"
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
