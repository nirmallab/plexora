"""A stand-in for Plexora's telemetry Worker (backend/), for the website's
browser tests.

    python tests/browser/mock_telemetry.py [--port 0]

Answers `POST /v1/telemetry/register` with an install token and
`POST /v1/telemetry/events` with a 202, as the Worker does, with the CORS the
Worker sends a page (backend/src/cors.ts): a preflight from an origin is
answered with that origin, POST and OPTIONS, the headers the uploader sends
(`Authorization, Content-Type, Content-Encoding`) and no credentials; every
answer carries the origin and exposes Retry-After. A page on one loopback
port calls this on another.

Prints one JSON line, `{"port"}`, when listening. For the driver:
`GET /_requests` -> every request: method, path, the Origin, the preflight's
asked headers, and the body (un-gzipped, parsed).
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALLOW_HEADERS = "Authorization, Content-Type, Content-Encoding"
CONFIG = {"level_max": "diagnostics", "upload_interval_s": 3600, "sample": 1, "disabled_until": 0}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    seen = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *rest):
            pass

        def _cors(self):
            origin = self.headers.get("Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Expose-Headers", "Retry-After")
                self.send_header("Vary", "Origin")

        def _json(self, status, body):
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self._cors()
            self.end_headers()
            self.wfile.write(payload)

        def do_OPTIONS(self):  # noqa: N802
            with lock:
                seen.append({"method": "OPTIONS", "path": self.path, "origin": self.headers.get("Origin"),
                             "asked": self.headers.get("Access-Control-Request-Headers", "")})
            self.send_response(204)
            if self.headers.get("Origin"):
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", ALLOW_HEADERS)
                self.send_header("Access-Control-Max-Age", "7200")
                self.send_header("Vary", "Origin")
            self.end_headers()

        def do_GET(self):  # noqa: N802
            if self.path == "/_requests":
                with lock:
                    return self._json(200, {"requests": list(seen)})
            return self._json(404, {"error": "Not found."})

        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            data = raw
            if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
                try:
                    data = gzip.decompress(raw)
                except OSError:
                    data = b""
            try:
                body = json.loads(data.decode("utf-8")) if data else None
            except ValueError:
                body = None
            with lock:
                seen.append({"method": "POST", "path": self.path, "origin": self.headers.get("Origin"),
                             "authorization": bool(self.headers.get("Authorization")),
                             "encoding": self.headers.get("Content-Encoding"), "body": body})
            if self.path == "/v1/telemetry/register":
                return self._json(200, {"schema": 1, "install_token": "PLEXORAT1.mock.token",
                                        "expires": time.time() + 86400 * 90, "unknown": 0,
                                        "config": CONFIG})
            if self.path == "/v1/telemetry/events":
                events = (body or {}).get("events") or []
                return self._json(202, {"schema": 1, "accepted": len(events), "rejected": 0,
                                        "dropped": 0, "unknown": 0, "duplicate": False,
                                        "rotate": False, "config": CONFIG})
            return self._json(404, {"error": "Not found."})

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print(json.dumps({"port": server.server_address[1]}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
