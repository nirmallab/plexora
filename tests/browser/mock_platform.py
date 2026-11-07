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

It is also the AI gateway the token names (`FakeService` names its own
address): the account routes are `FakeService`'s, and the model call,
`POST /v1/ai/messages` (and `/v1/ai/dev/messages`), streams a scripted model's
answer as the real gateway does -- `biocognia.accepted`, Anthropic-shaped
`message_start` / `content_block_*` / `message_delta` / `message_stop`, then
`biocognia.usage` -- text in as many deltas as the script says, a pause
between them, and `tool_use` blocks as `input_json_delta` pieces, with the
CORS headers a page's call needs (the worker's Python calls it with `fetch`).

Prints one JSON line, `{"port", "kid", "public_key"}`, when listening. For the
driver: `GET /_requests` (what was asked: path, method, and the body's field
NAMES and its binding -- never a certificate, code or key; for a model call
its task, `context.origin`, the tool results it carried and the newest user
text), `POST /_script` `{path, status, body}` (the next answer for `path`),
`POST /_down` `{down}` (every product route fails to connect: the socket
closes unanswered), `POST /_brain` `{replies: [...]}` (the model's next
answers, in order: `{text, tool_uses: [{name, input}], chunks, delay_ms}`, or
`{refuse: [status, code]}`; once they run out it answers "done.").
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

#: What the stand-in issues: two roots, enough to unlock and to name.
PRODUCT = SimpleNamespace(id="plexora", roots=("ai", "volume"), licensed_limits={}, manifest={},
                          client_min_version=None)


def _text_of(block: dict) -> str:
    """A tool_result block's text (a string, or text blocks)."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content or []
                   if isinstance(part, dict) and part.get("type") == "text")


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
    state = {"down": False, "requests": [], "replies": [], "calls": 0}
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
            if path == "/_brain" and method == "POST":
                with lock:
                    state["replies"] = list((body or {}).get("replies") or [])
                return self._send(200, {"replies": len(state["replies"])})
            if state["down"]:
                # As a network that is not there: no answer at all.
                self.close_connection = True
                self.connection.shutdown(2)
                return None
            if method == "POST" and path in ("/v1/ai/messages", "/v1/ai/dev/messages"):
                return self._messages(path, body)
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

        def _messages(self, path, body):
            """One model call, answered from the script and streamed."""
            body = body if isinstance(body, dict) else {}
            request = body.get("request") or {}
            messages = request.get("messages") or []
            newest = messages[-1] if messages else {}
            content = newest.get("content") if isinstance(newest.get("content"), list) else []
            results = [block for block in content if block.get("type") == "tool_result"]
            text_in = [block.get("text", "") for block in content if block.get("type") == "text"]
            with lock:
                state["calls"] += 1
                number = state["calls"]
                reply = state["replies"].pop(0) if state["replies"] else {"text": "done."}
                state["requests"].append({
                    "path": path[len("/v1"):], "method": "POST", "fields": sorted(body),
                    "task": body.get("task"), "origin": (body.get("context") or {}).get("origin"),
                    "agent": (body.get("context") or {}).get("agent"),
                    "authorized": (self.headers.get("Authorization") or "").startswith("Bearer "),
                    "client": self.headers.get("BioCognia-Client"),
                    "idempotency": bool(self.headers.get("Idempotency-Key")),
                    "tool_results": [{"ok": not block.get("is_error"),
                                      "text": _text_of(block)[:400]} for block in results],
                    "user_text": text_in[-1][:400] if text_in else None,
                    "tools": [tool.get("name") for tool in request.get("tools") or []][:40],
                })
            if reply.get("refuse"):
                status, code = reply["refuse"]
                return self._send(int(status), {"error": {"code": code, "message": code, "retry_after": 0}})
            text = str(reply.get("text") or "")
            uses = list(reply.get("tool_uses") or [])
            chunks = max(1, int(reply.get("chunks") or 1))
            delay = max(0, int(reply.get("delay_ms") or 0)) / 1000.0
            events = [("biocognia.accepted", {"gateway_request_id": f"req_{number}", "billing": "credits"}),
                      ("message_start", {"type": "message_start", "message": {"id": f"msg_{number}", "usage": {
                          "input_tokens": 3, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 100,
                          "output_tokens": 1}}})]
            index = 0
            pieces = []
            if text or not uses:
                size = max(1, -(-len(text) // chunks))
                pieces = [text[at:at + size] for at in range(0, len(text), size)] or [""]
                events.append(("content_block_start", {"type": "content_block_start", "index": 0,
                                                       "content_block": {"type": "text", "text": ""}}))
                for piece in pieces:
                    events.append(("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                           "delta": {"type": "text_delta", "text": piece}}))
                events.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
                index = 1
            for n, use in enumerate(uses):
                raw = json.dumps(use.get("input") or {})
                events += [
                    ("content_block_start", {"type": "content_block_start", "index": index, "content_block": {
                        "type": "tool_use", "id": use.get("id") or f"toolu_{number}_{n}", "name": use["name"],
                        "input": {}}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {
                        "type": "input_json_delta", "partial_json": raw[:len(raw) // 2]}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {
                        "type": "input_json_delta", "partial_json": raw[len(raw) // 2:]}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": index})]
                index += 1
            usage = {"input_uncached": 3, "cache_read": 0, "cache_write_5m": 100, "cache_write_1h": 0,
                     "output_tokens": max(1, len(text) // 4)}
            events += [
                ("message_delta", {"type": "message_delta", "delta": {
                    "stop_reason": "tool_use" if uses else "end_turn"},
                    "usage": {"output_tokens": usage["output_tokens"]}}),
                ("message_stop", {"type": "message_stop"}),
                ("biocognia.usage", {"gateway_request_id": f"req_{number}", "status": "ok",
                                     "usage_source": "provider", "usage": usage, "price_micro": 2000,
                                     "charged_micro": 2000, "billing": "credits", "model": "approved-model-a",
                                     "provider": "provider-x", "run": None,
                                     "balance": {"available_micro": 10_000_000}})]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self._cors()
            self.end_headers()
            self.close_connection = True
            for name, data in events:
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
                self.wfile.flush()
                if name == "content_block_delta" and data["delta"].get("type") == "text_delta" and delay:
                    time.sleep(delay)
            return None

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
