"""Test helpers for licensing: a throwaway issuer and a stand-in licence service.

`Issuer` generates a fresh Ed25519 key per test and installs ONLY its public
half into `plexora.licensing.keys.PUBLIC_KEYS`, so a test certificate verifies
here and nowhere else -- the shipped px1/px2 keys are not trusted during the
test, and nothing a test signs could ever be valid for a real install.

`FakeLicenseService` is a stdlib HTTP server on a loopback port that records
each request and answers from a script, with happy defaults that sign real
certificates with the issuer's key:

    service.script("/v1/refresh", 200, {"status": "revoked", "server_time": ...})
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from plexora.licensing import certificate, environment

KID = "pxt"


#: What the `paid` marker and the `paid_license` fixture install: the Paid
#: default plus external MCP access, so a Paid test may drive its tools over an
#: in-process MCP client. `Issuer.issue()` alone still mints the plain Paid
#: default (`["ai"]`), which is what a test of the `mcp` add-on starts from.
PAID_TEST_GRANTS = ("ai", "mcp")


class Issuer:
    def __init__(self, monkeypatch, kid: str = KID):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        from plexora.licensing import keys

        # Only test kids are ever trusted during a test. A second issuer in the
        # same test (the `paid` marker's, then a test's own) ADDS its key
        # under a fresh kid rather than replacing the first, so a licence the
        # first one installed does not silently turn invalid.
        trusted = {k: v for k, v in keys.PUBLIC_KEYS.items() if k.startswith(KID)}
        n = 1
        while kid in trusted:
            n += 1
            kid = f"{KID}{n}"
        self.kid = kid
        self.private = Ed25519PrivateKey.generate()
        public = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public_b64 = certificate.b64encode(public)
        monkeypatch.setattr(keys, "PUBLIC_KEYS", {**trusted, kid: self.public_b64})

    def payload(self, **overrides) -> dict:
        now = int(time.time())
        base = {
            "v": 1, "aud": "plexora", "kid": self.kid,
            "cert_id": "crt_test0001", "license_id": "lic_test0001",
            "account_id": "acc_test0001", "seat_id": "sa_test0001",
            "plan": "paid", "trial": False, "use_class": "academic",
            "entitlements": ["ai"],
            "environment_id": "env_test0001", "environment_type": "desktop",
            "env_binding": environment.binding(create=True),
            "delegation_pubkey": None,
            "issued_at": now, "expires_at": now + 90 * 86400,
            "license_expires_at": now + 365 * 86400,
            "grace_days": 14, "offline_until": None,
        }
        base.update(overrides)
        return base

    def sign(self, payload: dict, *, kid: str | None = None) -> str:
        body = certificate.canonical_json(payload)
        signature = self.private.sign(body)
        return (f"{certificate.PREFIX}.{kid or payload.get('kid') or self.kid}."
                f"{certificate.b64encode(body)}.{certificate.b64encode(signature)}")

    def issue(self, **overrides) -> str:
        return self.sign(self.payload(**overrides))

    def install(self, cert: str | None = None, *, last_validated: float | None = None,
                **record) -> str:
        """Write a license.json as an online activation would, and reset state."""
        from plexora import licensing
        from plexora.licensing import store

        cert = cert or self.issue()
        moment = int(time.time())
        store.write_license({
            "certificate": cert, "source": "activation", "credential_hint": None,
            "installed_at": moment,
            "last_validated": moment if last_validated is None else last_validated,
            "last_attempt": None, "hwm": moment, "server": "",
            "environment": {"id": "env_test0001", "name": "Test computer",
                            "type": "desktop"},
            "revoked": None, **record,
        })
        licensing.reset_for_tests()
        return cert


class FakeLicenseService:
    def __init__(self, issuer: Issuer | None = None):
        self.issuer = issuer
        self.requests: list[dict] = []
        self._scripts: dict[str, list] = {}
        self._lock = threading.Lock()
        self.server = None
        self.thread = None

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def script(self, path, status, body=None, *, times=1):
        with self._lock:
            self._scripts.setdefault(path, []).extend([(status, body)] * times)

    def of(self, path):
        return [r for r in self.requests if r["path"] == path]

    def _happy(self, path, body):
        now = int(time.time())
        if path == "/v1/activate" and self.issuer is not None:
            env = (body or {}).get("environment") or {}
            cert = self.issuer.issue(env_binding=env.get("binding"),
                                     environment_type=env.get("kind", "desktop"),
                                     delegation_pubkey=env.get("delegation_pubkey"))
            return 200, {"certificate": cert, "server_time": now,
                         "environment": {"id": "env_test0001",
                                         "name": env.get("display_name"),
                                         "type": env.get("kind", "desktop")}}
        if path == "/v1/refresh":
            return 200, {"status": "ok", "server_time": now}
        if path == "/v1/deactivate":
            return 200, {"status": "released", "server_time": now}
        if path == "/v1/trial/start":
            return 202, {"status": "sent", "message": "Check your email."}
        return 404, {"error": {"code": "not_found", "message": "not found"}}

    def _next(self, path, body):
        with self._lock:
            queue = self._scripts.get(path)
            if queue:
                return queue.pop(0)
        return self._happy(path, body)

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    parsed = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    parsed = None
                fake.requests.append({"path": self.path, "json": parsed,
                                      "headers": dict(self.headers)})
                status, body = fake._next(self.path, parsed)
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
