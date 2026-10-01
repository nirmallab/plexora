"""A stand-in Plexora AI gateway for harness tests: no network, no model.

`FakeGateway` is a real HTTP server (like `tests/license_fixtures.py`'s
licence service) that speaks the gateway's wire: `/v1/ai/messages` streams
Anthropic-shaped events between `plexora.accepted` and `plexora.usage`, and
`/v1/ai/runs` quotes and closes runs. The "model" is a brain function given
the decision packet the harness sent (and the whole request body); it answers
with JSON or text, or with a `Reply` that streams `tool_use` blocks the way a
provider does (`input_json_delta` pieces, `stop_reason: tool_use`), which is
what the conversational agent is tested with. The usage is a prompt-cache emulator
that reads from cache the longest prefix of the request it has seen before
and writes the rest, so cache verdicts and prefix stability can be tested.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from plexora.ai.harness.wire import canonical

CHARS_PER_TOKEN = 3.5
IMAGE_TOKENS = 1600
#: The run prices the real gateway's catalog (licensing/src/ai/catalog.ts) quotes.
PRICES = {"gating": ("marker", 25), "qc": ("channel", 12)}
#: $4 / $0.20 / $5 / $20 per MTok, as Opus 5.5 on the real gateway.
UNIT = {"in": 4.0, "read": 0.2, "write": 5.0, "out": 20.0}


def _tokens(value) -> int:
    if isinstance(value, dict):
        if value.get("type") == "image":
            return IMAGE_TOKENS
        if value.get("type") == "text":
            return max(1, int(len(value.get("text", "")) / CHARS_PER_TOKEN))
    if isinstance(value, list):
        return sum(_tokens(v) for v in value)
    if isinstance(value, dict):
        return _tokens(value.get("content", []))
    return max(1, int(len(str(value)) / CHARS_PER_TOKEN))


class Reply:
    """A brain's answer with tool calls: optional text, then `tool_use` blocks
    (`{"name", "input", "id"?}`), streamed as a provider streams them."""

    def __init__(self, text: str = "", tool_uses=()):
        self.text = text
        self.tool_uses = [dict(t) for t in tool_uses]


def tool_results(body: dict) -> list:
    """The `tool_result` blocks of the newest user turn of a request body."""
    messages = body["request"]["messages"]
    if not messages or messages[-1].get("role") != "user":
        return []
    content = messages[-1].get("content")
    return [b for b in (content if isinstance(content, list) else []) if b.get("type") == "tool_result"]


def result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


def last_user_text(body: dict) -> str:
    for message in reversed(body["request"]["messages"]):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        texts = [b.get("text", "") for b in content or [] if b.get("type") == "text"]
        if texts:
            return texts[-1]
    return ""


def last_packet(messages: list) -> dict | None:
    """The newest decision packet among the user turns (skipping repair turns)."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content") or []
        for block in reversed(content if isinstance(content, list) else []):
            if block.get("type") == "text":
                try:
                    value = json.loads(block["text"].split("\nANSWER SCHEMA:")[0])
                except ValueError:
                    break
                if isinstance(value, dict) and "packet_id" in value:
                    return value
    return None


class FakeGateway:
    def __init__(self, brain, *, credits_micro: int = 10_000_000, markup: float = 2.0):
        self.brain = brain
        self.calls: list[dict] = []
        self.runs: dict[str, dict] = {}
        self.seen_prefixes: set[str] = set()
        self.credits = credits_micro
        self.markup = markup
        self.fail_with: list[tuple[int, str]] = []      # queued (status, code) refusals
        self.lock = threading.Lock()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}")

            def do_GET(self):
                if self.path.startswith("/v1/ai/balance"):
                    return self._json(200, {"account_id": "acc_test", "mode": "credits",
                                            "available_micro": gateway.credits})
                if self.path.startswith("/v1/ai/pricing"):
                    return self._json(200, {"credit_micro": 10_000, "features": {
                        name: {"unit": unit, "credits": credits, "price_micro": credits * 10_000}
                        for name, (unit, credits) in PRICES.items()}})
                return self._json(404, {"error": {"code": "not_found", "message": "no"}})

            def do_POST(self):
                body = self._body()
                if self.path in ("/v1/ai/runs", "/v1/ai/dev/runs"):
                    run_id = f"run_{len(gateway.runs) + 1}"
                    credits = PRICES.get(body["feature"], ("unit", 25))[1]
                    run = {"run_id": run_id, "feature": body["feature"], "units": body["units"],
                           "quote_micro": body["units"] * credits * 10_000,
                           "quote_credits": body["units"] * credits,
                           "accrued_micro": 0, "charged_micro": 0, "calls": 0, "status": "open",
                           "billing": "dev" if "/dev/" in self.path else "credits"}
                    gateway.runs[run_id] = run
                    return self._json(201, run)
                if self.path.startswith("/v1/ai/runs/") and self.path.endswith("/finish"):
                    run = gateway.runs[self.path.split("/")[4]]
                    run["status"] = "finished"
                    return self._json(200, run)
                if self.path in ("/v1/ai/messages", "/v1/ai/dev/messages"):
                    return gateway._messages(self, body)
                return self._json(404, {"error": {"code": "not_found", "message": "no"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    # -- the model call ---------------------------------------------------------

    def _usage(self, request: dict) -> dict:
        """Prompt-cache emulation: segments are the system prompt, then each
        message; the longest previously seen prefix is read, the rest written."""
        segments = ([request["tools"]] if request.get("tools") else []) + [request.get("system") or []]             + list(request["messages"])
        read = write = 0
        running = hashlib.sha256()
        hit = True
        with self.lock:
            for segment in segments:
                running.update(canonical(segment).encode())
                key = running.hexdigest()
                tokens = _tokens(segment)
                if hit and key in self.seen_prefixes:
                    read += tokens
                else:
                    hit = False
                    write += tokens
                self.seen_prefixes.add(key)
        return {"input_uncached": 3, "cache_read": read, "cache_write_5m": write, "cache_write_1h": 0}

    def _messages(self, handler, body: dict):
        with self.lock:
            refusal = self.fail_with.pop(0) if self.fail_with else None
        if refusal:
            status, code = refusal
            return handler._json(status, {"error": {"code": code, "message": code, "retry_after": 0}})
        request = body["request"]
        packet = last_packet(request["messages"])
        answer = self.brain(packet, body)
        if isinstance(answer, tuple) and len(answer) == 2 and isinstance(answer[0], int):
            status, code = answer                       # a brain may refuse, like the gateway
            return handler._json(status, {"error": {"code": code, "message": code, "retry_after": 0}})
        tool_uses = []
        if isinstance(answer, Reply):
            text, tool_uses = answer.text, answer.tool_uses
        else:
            text = answer if isinstance(answer, str) else canonical(answer)
        usage = self._usage(request)
        usage["output_tokens"] = max(1, int((len(text) + len(canonical(tool_uses))) / CHARS_PER_TOKEN))
        cost = int((usage["input_uncached"] * UNIT["in"] + usage["cache_read"] * UNIT["read"]
                    + usage["cache_write_5m"] * UNIT["write"] + usage["output_tokens"] * UNIT["out"]))
        dev = handler.path.startswith("/v1/ai/dev/")
        price = cost if dev else int(cost * self.markup)
        run = self.runs.get((body.get("context") or {}).get("run_id") or "")
        with self.lock:
            charged = price
            if run:
                accrued = run["accrued_micro"] + price
                charged = max(0, min(accrued, run["quote_micro"]) - run["charged_micro"])
                run["accrued_micro"], run["charged_micro"] = accrued, run["charged_micro"] + charged
                run["calls"] += 1
            self.credits -= charged
            number = len(self.calls) + 1
            self.calls.append({"path": handler.path, "body": body, "tool_uses": tool_uses,
                               "idempotency_key": handler.headers.get("Idempotency-Key"),
                               "usage": usage, "packet_id": (packet or {}).get("packet_id")})
        rid = f"req_{number}"
        content_events = []
        index = 0
        if text or not tool_uses:
            content_events += [
                ("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "text_delta", "text": text[: len(text) // 2]}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "text_delta", "text": text[len(text) // 2:]}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0})]
            index = 1
        for n, use in enumerate(tool_uses):
            raw = json.dumps(use.get("input") or {})
            tid = use.get("id") or f"toolu_{number}_{n}"
            content_events += [
                ("content_block_start", {"type": "content_block_start", "index": index, "content_block": {
                    "type": "tool_use", "id": tid, "name": use["name"], "input": {}}}),
                ("content_block_delta", {"type": "content_block_delta", "index": index,
                                         "delta": {"type": "input_json_delta", "partial_json": raw[: len(raw) // 2]}}),
                ("content_block_delta", {"type": "content_block_delta", "index": index,
                                         "delta": {"type": "input_json_delta", "partial_json": raw[len(raw) // 2:]}}),
                ("content_block_stop", {"type": "content_block_stop", "index": index})]
            index += 1
        events = [
            ("plexora.accepted", {"gateway_request_id": rid, "billing": "dev" if dev else "credits"}),
            ("message_start", {"type": "message_start", "message": {"id": f"msg_{number}", "usage": {
                "input_tokens": usage["input_uncached"], "cache_read_input_tokens": usage["cache_read"],
                "cache_creation_input_tokens": usage["cache_write_5m"], "output_tokens": 1}}}),
            *content_events,
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "tool_use" if tool_uses else "end_turn"},
                               "usage": {"output_tokens": usage["output_tokens"]}}),
            ("message_stop", {"type": "message_stop"}),
            ("plexora.usage", {"gateway_request_id": rid, "status": "ok", "usage_source": "provider",
                               "usage": usage, "price_micro": price, "charged_micro": charged,
                               "billing": "dev" if dev else "credits",
                               **({"cost_micro": cost} if dev else {}),
                               "run": dict(run) if run else None,
                               "balance": {"available_micro": self.credits}}),
        ]
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        for name, data in events:
            handler.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            handler.wfile.flush()
