"""Talking to the BioCognia AI gateway: Plexora's model call.

The transport is the shared BioCognia client's (`biocognia.gateway`): the
30-minute `BIOCAI1` token obtained against this device's certificate
(`TokenSource(LICENSING)`), the gateway URL, one token refresh, server-sent
event parsing, and the account endpoints (balance, usage, pricing, runs).
What stays here is what belongs to the harness: `messages()` -- one streamed
call, its retries, and reading Anthropic-shaped events between
`biocognia.accepted` and `biocognia.usage` into a `ModelResponse` -- and the
wire types in `wire.py`.

`BIOCOGNIA_AI_TOKEN` supplies a token directly (a CI job, a test);
`BIOCOGNIA_AI_GATEWAY` overrides the gateway's address; `BIOCOGNIA_AI_DEV=1`
(or `dev=True`) uses the dev route, which only internal testing accounts may
call and which bills at provider cost. `BIOCOGNIA_OFFLINE=1` refuses before
any socket opens.

Retries: a refusal that happened before anything reached a provider (rate
limit, provider outage, unreachable gateway) is retried with the SAME
idempotency key, which the gateway frees in exactly those cases; an expired
token is refreshed once. Credit, cap and envelope refusals are never retried:
they are surfaced so the session can pause (`biocognia.codes.PAUSE_CODES`).
"""

from __future__ import annotations

import json
import time

from biocognia import gateway as _shared
from biocognia.gateway import (ENV_DEV, ENV_GATEWAY, ENV_TOKEN, RETRYABLE, GatewayError, dev_default,
                               iter_sse)

from plexora.ai.harness.wire import ModelRequest, ModelResponse, Usage

#: Tries in all for a provider whose circuit is open: it is down for a known
#: while (`retry_after`), so waiting that out once is worth it and six blind
#: tries are not -- the run pauses instead (decision.PAUSE_CODES). The gateway
#: says so as `provider_unavailable` with `details.failure: circuit_open`.
CIRCUIT_ATTEMPTS = 2
CALL_TIMEOUT = 300

__all__ = ["CALL_TIMEOUT", "CIRCUIT_ATTEMPTS", "ENV_DEV", "ENV_GATEWAY", "ENV_TOKEN", "RETRYABLE",
           "GatewayClient", "GatewayError", "TokenSource", "dev_default", "gateway_url", "iter_sse",
           "token_source"]


def TokenSource(token: str | None = None) -> _shared.TokenSource:  # noqa: N802 - a constructor
    """The gateway token for this device: `biocognia.gateway.TokenSource(LICENSING)`.
    `token` (or `BIOCOGNIA_AI_TOKEN`) short-circuits it."""
    from plexora.licensing import LICENSING

    return _shared.TokenSource(LICENSING, token)


token_source = TokenSource


def gateway_url() -> str:
    return _shared.gateway_url()


class GatewayClient(_shared.GatewayClient):
    """The shared transport, plus the harness's streamed model call."""

    def __init__(self, base_url: str | None = None, *, tokens: _shared.TokenSource | None = None,
                 dev: bool | None = None, max_attempts: int = 6, sleep=time.sleep):
        super().__init__(tokens or TokenSource(), base_url or None, dev=dev)
        self.max_attempts = max_attempts
        self._sleep = sleep

    # -- calls ---------------------------------------------------------------------

    def messages(self, request: ModelRequest, *, idempotency_key: str, on_delta=None,
                 on_retry=None) -> ModelResponse:
        """One streamed call. `on_delta(text)` is called with each text delta as
        it arrives (a retried call may repeat the deltas of a cut stream);
        `on_retry(attempt, error)` before each retry. The response's
        `attempts` says how many tries it took."""
        path = "/v1/ai/dev/messages" if self.dev else "/v1/ai/messages"
        body = request.envelope()
        if not self.dev:
            body.pop("model", None)
        delay = 2.0
        for attempt in range(1, self.max_attempts + 1):
            started = time.monotonic()
            try:
                with self.open("POST", path, body, {"Idempotency-Key": idempotency_key},
                               timeout=CALL_TIMEOUT) as response:
                    result = self._read_stream(response, on_delta)
                result.latency_ms = int((time.monotonic() - started) * 1000)
                result.attempts = attempt
                return result
            except GatewayError as exc:
                limit = min(self.max_attempts, CIRCUIT_ATTEMPTS) if exc.circuit_open else self.max_attempts
                if not exc.retryable or attempt >= limit:
                    raise
                if on_retry is not None:
                    on_retry(attempt, exc)
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
            if event == "biocognia.accepted":
                accepted = payload
            elif event == "biocognia.usage":
                usage_event = payload
            elif event == "biocognia.error":
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
            provider=usage_event.get("provider") or accepted.get("provider"),
            balance=usage_event.get("balance") or {}, run=usage_event.get("run"))
