"""A stand-in for the telemetry Worker, for the uploader tests.

A stdlib HTTP server on a loopback port that records every request (headers,
and the body un-gzipped and parsed) and answers from a script:

    fake.script("/v1/telemetry/events", 503, {"error": "busy"}, {"Retry-After": "120"})

Unscripted requests get the Worker's happy answers: a token for register, 202
for events.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HAPPY_CONFIG = {"level_max": "diagnostics", "upload_interval_s": 3600, "sample": 1,
                "disabled_until": 0}


class FakeIngest:
    def __init__(self):
        self.requests = []
        self._scripts = {}
        self._lock = threading.Lock()
        self.delay = 0.0
        self.server = None
        self.thread = None

    @property
    def url(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def script(self, path, status, body=None, headers=None, *, times=1):
        with self._lock:
            self._scripts.setdefault(path, []).extend(
                [(status, body, headers or {})] * times)

    def of(self, path):
        return [r for r in self.requests if r["path"] == path]

    @property
    def uploads(self):
        return [r["json"] for r in self.of("/v1/telemetry/events") if r["json"]]

    def _next(self, path):
        with self._lock:
            queue = self._scripts.get(path)
            if queue:
                return queue.pop(0)
        if path == "/v1/telemetry/register":
            return 200, {"schema": 1, "install_token": "PLEXORAT1.test.token",
                         "expires": time.time() + 86400 * 365, "config": HAPPY_CONFIG}, {}
        if path == "/v1/telemetry/events":
            return 202, {"schema": 1, "accepted": 1, "rejected": 0, "dropped": 0,
                         "duplicate": False, "rotate": False, "config": HAPPY_CONFIG}, {}
        return 404, {"error": "not found"}, {}

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                data = raw
                if self.headers.get("Content-Encoding") == "gzip":
                    try:
                        data = gzip.decompress(raw)
                    except OSError:
                        data = b""
                try:
                    parsed = json.loads(data.decode("utf-8")) if data else None
                except ValueError:
                    parsed = None
                fake.requests.append({"path": self.path, "headers": dict(self.headers),
                                      "raw": raw, "body": data, "json": parsed})
                if fake.delay:
                    time.sleep(fake.delay)
                status, body, headers = fake._next(self.path)
                payload = json.dumps(body).encode() if body is not None else b""
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                       name="fake-ingest")
        self.thread.start()
        return self

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
