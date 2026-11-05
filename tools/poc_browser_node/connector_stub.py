"""A stub of the minimal connector (investigation §6.5), for P5 scenario (g).

    python connector_stub.py --origin http://127.0.0.1:PORT --node ENDPOINT --node-token TOKEN

What the real connector (plexora-connector, Rust, shared with the desktop
shell) would answer on loopback, minus the SSH: /connector/v1/hello, profiles,
connect, status, disconnect. `connect` returns the node it was given instead
of building a tunnel. Its own token is generated here and handed to the page
ONCE, in the pairing URL's fragment (printed), which a browser never sends to
any server; every request must then carry it as X-Plexora-Connector-Token.
CORS is answered for exactly `--origin`, as a node's is.

Prints one JSON line {port, token, pairing} when listening.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN_HEADER = "X-Plexora-Connector-Token"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    origin = ""
    token = ""
    node = {}
    state = {"connected": False, "since": None}

    def log_message(self, *_):
        pass

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin and origin == self.origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            # Chrome's Private/Local Network Access preflight: a public page
            # asking a loopback server must be answered with this.
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        if self.headers.get("Origin") == self.origin:
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", f"Content-Type, {TOKEN_HEADER}")
            self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorised(self):
        return secrets.compare_digest(self.headers.get(TOKEN_HEADER) or "", self.token)

    def do_GET(self):
        self._route()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        self._route()

    def _route(self):
        path = self.path.split("?")[0]
        if path == "/connector/v1/hello":
            # Unauthenticated on purpose: the page must be able to ask "is a
            # connector here, and which version" before it is paired. Nothing
            # in it is a secret.
            return self._json(200, {"connector": "plexora-connector-stub", "api_version": 1,
                                    "paired": self._authorised()})
        if not self._authorised():
            return self._json(403, {"error": "not paired: open the pairing link this connector printed"})
        if path == "/connector/v1/profiles":
            return self._json(200, {"profiles": [{"id": "o2", "host": "o2.hms.harvard.edu", "srun": True,
                                                  "jump": None, "secret": "password+duo"}]})
        if path == "/connector/v1/connect":
            time.sleep(0.05)   # stands in for ssh + srun + the node's announce line
            self.state.update(connected=True, since=time.time())
            return self._json(200, {"state": "connected", "endpoint": self.node["endpoint"],
                                    "token": self.node["token"], "node_id": "stub"})
        if path == "/connector/v1/status":
            return self._json(200, {"state": "connected" if self.state["connected"] else "idle"})
        if path == "/connector/v1/disconnect":
            self.state.update(connected=False, since=None)
            return self._json(200, {"state": "idle"})
        return self._json(404, {"error": "no such route"})


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--node-token", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    Handler.origin = args.origin
    Handler.token = secrets.token_hex(16)
    Handler.node = {"endpoint": args.node, "token": args.node_token}
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = server.server_address[1]
    pairing = f"{args.origin}/poc/index.html#pair=127.0.0.1:{port}:{Handler.token}"
    print(json.dumps({"port": port, "token": Handler.token, "pairing": pairing, "pid": os.getpid()}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
