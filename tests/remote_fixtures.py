"""A real HTTP server over a directory, for the remote-store tests.

Real rather than mocked because the behaviour under test is fsspec's and
aiohttp's as much as ours: which requests a zarr open actually makes, what a
403 turns into, what a dropped connection raises. A mock would answer the
questions we thought to ask; this answers the ones the libraries ask.

Three modes, each a kind of host a remote store really lives on:

* ``gateway`` (the default): files are served, directories are 404 and nothing
  can be listed -- an S3 bucket behind HTTPS, which is what IDR is.
* ``forbidden``: the same, but a missing key is 403 rather than 404 -- a public
  bucket without list permission, which is what Vitessce's data host does.
* ``listing``: directories answer with an HTML index fsspec's HTTP filesystem
  parses -- standing in for a host that can be listed, as ``s3://`` can.

``requests`` is every request as ``(method, path, status)``; ``outage()`` makes
the server drop every connection until the block ends.
"""

from __future__ import annotations

import contextlib
import email.utils
import hashlib
import html
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    @property
    def host(self) -> "StoreServer":
        return self.server.plexora_host  # type: ignore[attr-defined]

    def _send(self, status, body=b"", headers=None, head=False):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head and body:
            self.wfile.write(body)
        self.host.log(self.command, self.path, status)

    def _handle(self, head=False):
        host = self.host
        if host.down:
            # A dropped connection, the way a dead link looks to a client.
            self.close_connection = True
            host.log(self.command, self.path, 0)
            try:
                self.connection.shutdown(2)
            except OSError:
                pass
            return
        if host.delay:
            time.sleep(host.delay)
        relative = unquote(urlsplit(self.path).path).lstrip("/")
        target = (host.root / relative).resolve()
        if host.root.resolve() not in target.parents and target != host.root.resolve():
            return self._send(404, head=head)
        if target.is_dir():
            if host.mode != "listing":
                return self._send(404, head=head)
            links = []
            for child in sorted(target.iterdir()):
                name = child.name + ("/" if child.is_dir() else "")
                links.append(f'<a href="{html.escape(name)}">{html.escape(name)}</a>')
            body = ("<html><body>" + "\n".join(links) + "</body></html>").encode()
            return self._send(200, body, {"Content-Type": "text/html"}, head=head)
        if not target.is_file():
            return self._send(403 if host.mode == "forbidden" else 404, head=head)
        data = target.read_bytes()
        stat = target.stat()
        headers = {
            "Content-Type": "application/octet-stream",
            "ETag": '"' + hashlib.sha1(data).hexdigest() + '"',
            "Last-Modified": email.utils.formatdate(stat.st_mtime, usegmt=True),
            "Accept-Ranges": "bytes",
        }
        wanted = self.headers.get("Range")
        if wanted and wanted.startswith("bytes="):
            spec = wanted[len("bytes="):]
            start_text, _, end_text = spec.partition("-")
            size = len(data)
            if start_text == "":
                start, end = max(0, size - int(end_text)), size - 1
            else:
                start = int(start_text)
                end = int(end_text) if end_text else size - 1
            end = min(end, size - 1)
            if start > end:
                return self._send(416, head=head)
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            return self._send(206, data[start:end + 1], headers, head=head)
        return self._send(200, data, headers, head=head)

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle(head=True)


class StoreServer:
    def __init__(self, root: Path, mode: str = "gateway"):
        self.root = Path(root)
        self.mode = mode
        self.down = False
        self.delay = 0.0
        self.requests: list[tuple] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.plexora_host = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="remote-fixture-http", daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def url(self, *parts) -> str:
        tail = "/".join(str(p).strip("/") for p in parts if str(p).strip("/"))
        return f"http://127.0.0.1:{self.port}/{tail}" if tail else \
            f"http://127.0.0.1:{self.port}"

    def log(self, method, path, status):
        with self._lock:
            self.requests.append((method, unquote(urlsplit(path).path), status))

    def clear(self):
        with self._lock:
            self.requests.clear()

    def count(self, suffix=None, method=None) -> int:
        with self._lock:
            return sum(1 for m, p, _ in self.requests
                       if (suffix is None or p.endswith(suffix))
                       and (method is None or m == method))

    @contextlib.contextmanager
    def outage(self):
        self.down = True
        try:
            yield
        finally:
            self.down = False

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """A fresh chunk cache for this test, with retries that do not sleep."""
    from plexora.server.utils import remote_store

    root = tmp_path / ".remote_cache"
    remote_store._reset_for_tests(root)
    monkeypatch.setattr(remote_store, "_RETRY_DELAYS_S", (0.0,))
    monkeypatch.setattr(remote_store, "_DOWN_BACKOFF_S", 0.0)
    yield root
    remote_store._reset_for_tests()


@pytest.fixture
def http_store(tmp_path, cache_root):
    """`make(mode="gateway")` -> a `StoreServer` over `tmp_path / "served"`."""
    served = tmp_path / "served"
    served.mkdir(exist_ok=True)
    servers = []

    def make(mode="gateway", root=None):
        server = StoreServer(root or served, mode)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def closed_port_url(path="x.zarr") -> str:
    """A URL on a loopback port nothing is listening on."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/{path}"


def touch_later(path, seconds=2):
    """Bump a file's mtime, so Last-Modified (second resolution) changes."""
    stat = os.stat(path)
    os.utime(path, (stat.st_atime + seconds, stat.st_mtime + seconds))
