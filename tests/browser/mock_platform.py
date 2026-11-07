"""A stand-in BioCognia platform for the website's browser tests.

    python tests/browser/mock_platform.py [--port 0] [--bind-version 2] [--pending 1] [--home DIR]

The product API the website's page calls (`/v1/activate/start`,
`/activate/poll`, `/activate`, `/refresh`, `/deactivate`, `/ai/token`,
`/account`, `GET /products/<id>`), answered in the shapes of the `biocognia`
package's own stand-in (`biocognia.testing.FakeService`, whose `_happy` this
reuses), with certificates signed by a throwaway `biocognia.testing.Issuer`
key that only a test build of the site trusts
(`static_build --licence-test-key bct=<key>`), and with the CORS headers the
real platform sends a page (biocognia-platform packages/shared/src/cors.ts):
the request's Origin echoed, `Content-Type, BioCognia-Client` allowed, no
credentials. A page on one loopback port calls this on another.

Prints one JSON line, `{"port", "kid", "public_key"}`, when listening. For the
driver: `GET /_requests` (what was asked: path, method, and the body's field
NAMES and its binding -- never a certificate, code or key), `POST /_script`
`{path, status, body}` (the next answer for `path`), `POST /_down` `{down}`
(every product route fails to connect: the socket closes unanswered).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

#: What the stand-in issues: two roots, enough to unlock and to name.
PRODUCT = SimpleNamespace(id="plexora", roots=("ai", "volume"), licensed_limits={}, manifest={},
                          client_min_version=None)


class _Patch:
    """`Issuer` installs its key in this process's table through a pytest
    monkeypatch; here it is only set."""

    @staticmethod
    def setattr(target, name, value):
        setattr(target, name, value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--bind-version", type=int, default=2, choices=(1, 2))
    parser.add_argument("--pending", type=int, default=1, help="polls answered pending before approval")
    parser.add_argument("--home", default=None,
                        help="a folder of the caller's for the issuer's own files (it removes it); else a temp one")
    args = parser.parse_args(argv)
    # The issuer makes a device secret of its own when it builds a payload:
    # somewhere of this process's own, never the user's licence directory.
    os.environ["BIOCOGNIA_DIR"] = args.home or tempfile.mkdtemp(prefix="mock-platform-")
    from biocognia.testing import FakeService, Issuer

    issuer = Issuer(_Patch, PRODUCT)
    service = FakeService(issuer, products=(PRODUCT,))
    service.bind_version = args.bind_version
    service.pending_polls = args.pending
    state = {"down": False, "requests": []}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _cors(self):
            origin = self.headers.get("Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Expose-Headers", "Retry-After")
            self.send_header("Vary", "Origin")

        def _send(self, status, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            path = self.path.split("?", 1)[0]
            if self.headers.get("Origin"):
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
                # The AI gateway's account routes (this stand-in is also the
                # gateway the token names): Python in the worker calls them with
                # its bearer token. ai.biocognia.com sends no CORS yet (plan
                # Phase 3); here whatever the worker asks for is allowed.
                gateway = path.startswith("/v1/ai/") and path != "/v1/ai/token"
                allowed = self.headers.get("Access-Control-Request-Headers", "") if gateway \
                    else "Content-Type, BioCognia-Client"
                self.send_header("Access-Control-Allow-Headers", allowed)
                self.send_header("Access-Control-Max-Age", "7200")
            self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw.decode("utf-8")) if raw else None
            except ValueError:
                return None

        def _handle(self, method):
            path = self.path.split("?", 1)[0]
            body = self._body()
            if path == "/_requests":
                with lock:
                    return self._send(200, {"requests": list(state["requests"])})
            if path == "/_script" and method == "POST":
                service.script(body["path"], int(body["status"]), body.get("body"))
                return self._send(200, {"ok": True})
            if path == "/_down" and method == "POST":
                state["down"] = bool(body.get("down"))
                return self._send(200, {"down": state["down"]})
            if state["down"]:
                # As a network that is not there: no answer at all.
                self.close_connection = True
                self.connection.shutdown(2)
                return None
            product_path = path[len("/v1"):] if path.startswith("/v1/") else path
            environment = (body or {}).get("environment") or {}
            with lock:
                state["requests"].append({
                    "path": product_path, "method": method,
                    "fields": sorted((body or {}).keys()) if isinstance(body, dict) else [],
                    "binding": (body or {}).get("binding") or environment.get("binding"),
                    "kind": environment.get("kind"), "name": environment.get("display_name"),
                    "client": self.headers.get("BioCognia-Client"),
                })
            # FakeService's own queue and answers, under its own paths.
            with service._lock:
                queue = service._scripts.get("/v1" + product_path) or service._scripts.get(product_path) or []
                scripted = queue.pop(0) if queue else None
            status, payload = scripted if scripted else service._happy("/v1" + product_path, body)
            return self._send(status, payload if payload is not None else {})

        def do_POST(self):  # noqa: N802
            self._handle("POST")

        def do_GET(self):  # noqa: N802
            self._handle("GET")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    service.server = server  # FakeService names its own address as the AI gateway
    print(json.dumps({"port": server.server_address[1], "kid": issuer.kid, "public_key": issuer.public_b64}),
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
