"""Talking to the Plexora AI gateway (`/v1/ai/*` on the licence service).

Standard library only, like `plexora.licensing.client`: a model call is one
POST whose answer streams back as server-sent events, read line by line.

Authentication is a short-lived `PLXAI1` token the licence service issues
against this environment's certificate; `TokenSource` refreshes it at two
thirds of its life. `PLEXORA_AI_TOKEN` supplies one directly (a CI job, a
test). The gateway is the licence server unless `PLEXORA_AI_GATEWAY` says
otherwise; `PLEXORA_AI_DEV=1` (or `dev=True`) uses the dev route, which only
internal testing accounts may call and which bills at provider cost.

Retries: a refusal that happened before anything reached a provider (rate
limit, provider outage, unreachable gateway) is retried with the SAME
idempotency key, which the gateway frees in exactly those cases; an expired
token is refreshed once. Credit and envelope refusals are never retried: they
are surfaced so the session can pause.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

from plexora.ai.harness.wire import ModelRequest, ModelResponse, Usage

ENV_GATEWAY = "PLEXORA_AI_GATEWAY"
ENV_TOKEN = "PLEXORA_AI_TOKEN"
ENV_DEV = "PLEXORA_AI_DEV"

#: Gateway codes worth another try with the same key: nothing was spent.
RETRYABLE = ("provider_rate_limited", "provider_unavailable", "rate_limited", "unreachable",
             "signing_unavailable", "internal_error")
CALL_TIMEOUT = 300


class GatewayError(Exception):
    def __init__(self, message: str, *, code: str, status: int | None = None,
                 retry_after: float | None = None, detail=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE


def gateway_url() -> str:
    explicit = (os.environ.get(ENV_GATEWAY) or "").strip()
    if explicit:
        return explicit.rstrip("/")
    from plexora.licensing import store

    return store.server_url()


def dev_default() -> bool:
    return (os.environ.get(ENV_DEV) or "").strip().lower() in ("1", "true", "yes", "on")


class TokenSource:
    """The current gateway token, obtained from the licence certificate."""

    def __init__(self, token: str | None = None):
        self._fixed = token or (os.environ.get(ENV_TOKEN) or "").strip() or None
        self._token: str | None = None
        self._refresh_at = 0.0
        self._lock = threading.Lock()
        self.mode: str | None = None

    def get(self, *, force: bool = False) -> str:
        if self._fixed:
            return self._fixed
        with self._lock:
            if force or not self._token or time.time() >= self._refresh_at:
                self._obtain()
            return self._token  # type: ignore[return-value]

    def _obtain(self) -> None:
        from plexora.licensing import client, state
        from plexora.licensing.errors import ServerError

        current = state.current()
        if not current.certificate or current.environment_type == "job":
            raise GatewayError("Plexora AI needs an activated Paid licence on this machine "
                               "(`plexora license activate`).", code="no_license")
        try:
            result = client.ai_token(current.certificate)
        except ServerError as exc:
            raise GatewayError(str(exc), code=exc.code or "unreachable",
                               status=getattr(exc, "status", None),
                               retry_after=getattr(exc, "retry_after", None)) from None
        now = time.time()
        server_time = float(result.get("server_time") or now)
        life = max(60.0, float(result.get("expires_at") or server_time + 1800) - server_time)
        self._token = result["token"]
        self._refresh_at = now + life * 2 / 3
        self.mode = result.get("mode")


def _error(status: int, raw: bytes) -> GatewayError:
    try:
        error = json.loads(raw.decode("utf-8")).get("error") or {}
    except (ValueError, UnicodeDecodeError, AttributeError):
        error = {}
    code = error.get("code") if isinstance(error.get("code"), str) else "bad_response"
    retry = error.get("retry_after")
    return GatewayError(error.get("message") or f"HTTP {status}", code=code, status=status,
                        retry_after=float(retry) if isinstance(retry, (int, float)) else None,
                        detail=error.get("details"))


def iter_sse(response):
    """(event, data) pairs from an SSE response, read line by line."""
    event, data = None, []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield event or "message", "\n".join(data)
            event, data = None, []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event or "message", "\n".join(data)


class GatewayClient:
    def __init__(self, base_url: str | None = None, *, tokens: TokenSource | None = None,
                 dev: bool | None = None, max_attempts: int = 6, sleep=time.sleep):
        self.base = (base_url or gateway_url()).rstrip("/")
        self.tokens = tokens or TokenSource()
        self.dev = dev_default() if dev is None else dev
        self.max_attempts = max_attempts
        self._sleep = sleep

    # -- transport ---------------------------------------------------------------

    def _open(self, method: str, path: str, body=None, headers=None, timeout=60):
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        for refreshed in (False, True):
            request = urllib.request.Request(
                f"{self.base}{path}", data=data, method=method,
                headers={"Content-Type": "application/json", "User-Agent": _user_agent(),
                         "Authorization": f"Bearer {self.tokens.get(force=refreshed)}",
                         **(headers or {})})
            try:
                return urllib.request.urlopen(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                error = _error(exc.code, exc.read(64 * 1024) if exc.fp is not None else b"")
                if error.code == "token_expired" and not refreshed:
                    continue
                raise error from None
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                raise GatewayError(f"Could not reach the Plexora AI gateway ({self.base}): "
                                   f"{type(exc).__name__}.", code="unreachable") from None
        raise GatewayError("The gateway refused a fresh token.", code="invalid_token")

    def _json(self, method: str, path: str, body=None) -> dict:
        with self._open(method, path, body) as response:
            return json.loads(response.read(4 * 1024 * 1024).decode("utf-8"))

    # -- calls ---------------------------------------------------------------------

    def messages(self, request: ModelRequest, *, idempotency_key: str, on_delta=None) -> ModelResponse:
        """One streamed call. `on_delta(text)` is called with each text delta as
        it arrives (a retried call may repeat the deltas of a cut stream)."""
        path = "/v1/ai/dev/messages" if self.dev else "/v1/ai/messages"
        body = request.envelope()
        if not self.dev:
            body.pop("model", None)
        delay = 2.0
        for attempt in range(1, self.max_attempts + 1):
            started = time.monotonic()
            try:
                with self._open("POST", path, body, {"Idempotency-Key": idempotency_key},
                                timeout=CALL_TIMEOUT) as response:
                    result = self._read_stream(response, on_delta)
                result.latency_ms = int((time.monotonic() - started) * 1000)
                return result
            except GatewayError as exc:
                if not exc.retryable or attempt == self.max_attempts:
                    raise
                self._sleep(min(exc.retry_after or delay, 120.0))
                delay = min(delay * 3, 120.0)
        raise AssertionError("unreachable")

    def _read_stream(self, response, on_delta=None) -> ModelResponse:
        text: list[str] = []
        blocks: dict[int, dict] = {}
        partial: dict[int, list[str]] = {}
        usage_event: dict = {}
        accepted: dict = {}
        stop = None
        for event, data in iter_sse(response):
            try:
                payload = json.loads(data)
            except ValueError:
                continue
            if event == "plexora.accepted":
                accepted = payload
            elif event == "plexora.usage":
                usage_event = payload
            elif event == "plexora.error":
                raise GatewayError("The gateway stopped mid-answer.", code=payload.get("code", "interrupted"),
                                   detail=payload)
            elif event == "content_block_start":
                block = dict(payload.get("content_block") or {})
                index = int(payload.get("index") or 0)
                if block.get("type") == "text":
                    blocks[index] = {"type": "text", "text": block.get("text") or ""}
                elif block.get("type") == "tool_use":
                    blocks[index] = {"type": "tool_use", "id": block.get("id"), "name": block.get("name"),
                                     "input": block.get("input") or {}}
                    partial[index] = []
            elif event == "content_block_delta":
                delta = payload.get("delta") or {}
                index = int(payload.get("index") or 0)
                if delta.get("type") == "text_delta":
                    piece = delta.get("text", "")
                    text.append(piece)
                    if index in blocks and blocks[index]["type"] == "text":
                        blocks[index]["text"] += piece
                    elif index not in blocks:
                        blocks[index] = {"type": "text", "text": piece}
                    if on_delta is not None and piece:
                        on_delta(piece)
                elif delta.get("type") == "input_json_delta" and index in partial:
                    partial[index].append(delta.get("partial_json") or "")
            elif event == "content_block_stop":
                index = int(payload.get("index") or 0)
                if index in partial:
                    raw = "".join(partial.pop(index))
                    try:
                        blocks[index]["input"] = json.loads(raw) if raw.strip() else blocks[index]["input"]
                    except ValueError:
                        blocks[index]["input"] = {"_unparsed": raw}
            elif event == "message_delta":
                stop = (payload.get("delta") or {}).get("stop_reason") or stop
            elif event == "error":
                raise GatewayError("The model provider failed mid-answer.", code="provider_unavailable",
                                   detail=payload)
        if not usage_event:
            raise GatewayError("The answer ended before the gateway settled it.", code="interrupted")
        for index, raw in partial.items():           # a stream without content_block_stop
            try:
                blocks[index]["input"] = json.loads("".join(raw) or "{}")
            except ValueError:
                blocks[index]["input"] = {"_unparsed": "".join(raw)}
        return ModelResponse(
            blocks=[blocks[i] for i in sorted(blocks) if blocks[i]["type"] != "text" or blocks[i]["text"]],
            text="".join(text), stop_reason=stop, usage=Usage.of(usage_event.get("usage")),
            gateway_request_id=usage_event.get("gateway_request_id") or accepted.get("gateway_request_id"),
            status=usage_event.get("status", "ok"), price_micro=int(usage_event.get("price_micro") or 0),
            charged_micro=int(usage_event.get("charged_micro") or 0), cost_micro=usage_event.get("cost_micro"),
            billing=usage_event.get("billing", "credits"), model=usage_event.get("model") or accepted.get("model"),
            balance=usage_event.get("balance") or {}, run=usage_event.get("run"))

    def start_run(self, feature: str, units: int, session_id: str | None = None) -> dict:
        path = "/v1/ai/dev/runs" if self.dev else "/v1/ai/runs"
        body = {"feature": feature, "units": int(units)}
        if session_id:
            body["session_id"] = session_id
        return self._json("POST", path, body)

    def finish_run(self, run_id: str) -> dict:
        return self._json("POST", f"/v1/ai/runs/{run_id}/finish", {})

    def balance(self) -> dict:
        return self._json("GET", "/v1/ai/balance")

    def usage(self, days: int = 30) -> dict:
        return self._json("GET", f"/v1/ai/usage?days={int(days)}")

    def pricing(self) -> dict:
        """The price list: `features.<name>.{unit, credits}` is what a run of
        that feature is quoted per unit (the estimate before a start)."""
        return self._json("GET", "/v1/ai/pricing")


def _user_agent() -> str:
    from plexora.licensing import environment

    return f"plexora/{environment.plexora_version()} (ai)"
